"""Podcast episodes: a pasted script, synthesized to MP3 and stored durably.

The public site runs on an ephemeral container, so audio lives here, beside the
reports — one directory per account, one row per episode. Synthesis goes through
OpenRouter's OpenAI-dialect /audio/speech endpoint; TTS providers cap input
length, so the script is split at paragraph boundaries and the MP3 segments are
concatenated (same model, same encoding — players treat the join as gapless
enough for speech).
"""

import logging
import os
import re
import threading
from datetime import datetime

import config
import db
import openrouter

TABLE = 'podcasts'
MAX_SCRIPT_CHARS = 100_000
CHUNK_CHARS = 3_500          # inside every mainstream TTS input cap, with margin
MAX_CLONE_B64 = 12_000_000   # base64 chars per voice sample; OpenRouter caps at 20 MiB

# Providers with tighter per-request input caps than the default chunk size.
# Deepgram documents a 2,000-character request limit; the small open-weight
# models get conservative room so a chunk never lands near a context edge.
_PROVIDER_CHUNK_CAPS = {
    'deepgram': 1_800,
    'hexgrad': 1_500,
    'canopylabs': 1_500,
    'sesame': 1_500,
    'zyphra': 1_500,
}


def chunk_limit(tts_model: str) -> int:
    author = (tts_model or '').split('/', 1)[0].lower()
    return _PROVIDER_CHUNK_CAPS.get(author, CHUNK_CHARS)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    account_id   INT          NOT NULL,
    episode      INT          NOT NULL,
    title        VARCHAR(200) NOT NULL,
    filename     VARCHAR(255) NOT NULL,
    tts_model    VARCHAR(120) NOT NULL,
    voice        VARCHAR(60)  DEFAULT NULL,
    bytes        INT          NOT NULL DEFAULT 0,
    script_chars INT          NOT NULL DEFAULT 0,
    chunks       INT          NOT NULL DEFAULT 0,
    created_at   DATETIME     NOT NULL,
    KEY idx_podcast_account (account_id, episode)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


class PodcastError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


_schema_ready = False


def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    _schema_ready = True


# ---------------------------------------------------------------------------
# Script chunking
# ---------------------------------------------------------------------------
_SENTENCE_RE = re.compile(r'(?<=[.!?])\s+')


def split_script(script: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Chunks that respect paragraph, then sentence, boundaries — never mid-word.

    A chunk boundary is where the synthesized voice resets its prosody, so the
    splits land where a reader would pause anyway.
    """
    chunks: list[str] = []
    current = ''
    for paragraph in re.split(r'\n\s*\n', script):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        pieces = ([paragraph] if len(paragraph) <= limit
                  else _SENTENCE_RE.split(paragraph))
        for piece in pieces:
            piece = piece.strip()
            while len(piece) > limit:          # a monster sentence: hard-wrap it
                cut = piece.rfind(' ', 0, limit)
                cut = cut if cut > 0 else limit
                head, piece = piece[:cut], piece[cut:].strip()
                if current:
                    chunks.append(current)
                    current = ''
                chunks.append(head)
            if not piece:
                continue
            joined = f'{current}\n\n{piece}' if current else piece
            if len(joined) <= limit:
                current = joined
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------------------
# Script cleanup and voice cloning
# ---------------------------------------------------------------------------
# Pasted scripts arrive with word-processor and LLM artifacts that TTS
# providers either read aloud ("asterisk asterisk") or reject outright.
_CTRL_RE = re.compile('[\\x00-\\x08\\x0b-\\x1f\\x7f\\u200b-\\u200d\\ufeff]')
_FENCE_RE = re.compile(r'^\s*```.*$', re.M)
_HEADING_RE = re.compile(r'^\s{0,3}#{1,6}\s+', re.M)
_BOLD_RE = re.compile(r'(\*\*|__)(.+?)\1', re.S)


def clean_script(script: str) -> str:
    """TTS-safe cleanup: keep every spoken word, drop the markup around it.

    Deliberately light-handed — square-bracket, parenthetical and angle-bracket
    delivery tags are meaningful to several models and pass through untouched.
    """
    text = (script or '').replace('\r\n', '\n').replace('\r', '\n')
    text = _CTRL_RE.sub('', text)
    text = _FENCE_RE.sub('', text)          # ``` marker lines; the code stays
    text = _HEADING_RE.sub('', text)
    text = _BOLD_RE.sub(r'\2', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return '\n'.join(line.rstrip() for line in text.split('\n')).strip()


# ---------------------------------------------------------------------------
# PCM handling — some adapters (Gemini TTS) only emit raw PCM
# ---------------------------------------------------------------------------
# The PCM these adapters return: 24 kHz, 16-bit, mono (documented for Gemini
# TTS, and the de-facto standard shape for TTS PCM output).
PCM_RATE = 24_000
PCM_WIDTH = 2
PCM_CHANNELS = 1


def wants_pcm(err) -> bool:
    """Does this 400 say the adapter only supports response_format=pcm?"""
    blob = f'{err} {getattr(err, "body", "") or ""}'.lower()
    return getattr(err, 'status', None) == 400 and \
        'response_format' in blob and 'pcm' in blob


def encode_pcm_to_mp3(pcm: bytes) -> bytes | None:
    """Raw 24 kHz mono PCM -> MP3, via lameenc or ffmpeg. None if neither exists."""
    try:
        import lameenc
        encoder = lameenc.Encoder()
        encoder.set_bit_rate(64)
        encoder.set_in_sample_rate(PCM_RATE)
        encoder.set_channels(PCM_CHANNELS)
        encoder.set_quality(2)
        return bytes(encoder.encode(pcm)) + bytes(encoder.flush())
    except ImportError:
        pass
    except Exception as e:
        logging.warning(f'lameenc MP3 encode failed, trying ffmpeg: {e}')
    import shutil
    import subprocess
    if shutil.which('ffmpeg'):
        try:
            proc = subprocess.run(
                ['ffmpeg', '-loglevel', 'error',
                 '-f', 's16le', '-ar', str(PCM_RATE), '-ac', str(PCM_CHANNELS),
                 '-i', 'pipe:0', '-b:a', '64k', '-f', 'mp3', 'pipe:1'],
                input=pcm, capture_output=True, timeout=600)
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
            logging.warning(f'ffmpeg MP3 encode failed: {proc.stderr[:300]}')
        except Exception as e:
            logging.warning(f'ffmpeg MP3 encode failed: {e}')
    return None


def wrap_pcm_as_wav(pcm: bytes) -> bytes:
    """Raw PCM in a WAV container — pure stdlib, always available."""
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wav:
        wav.setnchannels(PCM_CHANNELS)
        wav.setsampwidth(PCM_WIDTH)
        wav.setframerate(PCM_RATE)
        wav.writeframes(pcm)
    return buf.getvalue()


def validate_clone(clone_audio: str | None) -> None:
    if not clone_audio:
        return
    if not clone_audio.startswith('data:audio/'):
        raise PodcastError(
            "'clone_audio' must be a data:audio/...;base64 URI.")
    if len(clone_audio) > MAX_CLONE_B64:
        raise PodcastError(
            'The voice sample is too large (about 8 MB of audio at most).', 413)


def _clone_refs(clone_audio: str | None,
                clone_transcript: str | None) -> list | None:
    if not clone_audio:
        return None
    refs = [{'type': 'input_audio', 'input_audio': {'data': clone_audio}}]
    if clone_transcript:
        refs.append({'type': 'text', 'text': clone_transcript})
    return refs


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def account_dir(account_id: int, create: bool = True) -> str:
    path = os.path.join(config.PODCASTS_DIR, f'account_{int(account_id)}')
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def resolve(account_id: int, filename: str) -> str:
    """Absolute path to one episode, confined to that account's directory."""
    name = os.path.basename((filename or '').strip())
    if not name or name.startswith('.') \
            or not name.lower().endswith(('.mp3', '.wav')):
        raise PodcastError(f'Unsafe podcast filename: {filename!r}')
    base = os.path.realpath(account_dir(account_id, create=False))
    path = os.path.realpath(os.path.join(base, name))
    if not path.startswith(base + os.sep):
        raise PodcastError(f'Podcast path escapes its account directory: {filename!r}')
    return path


# Episode numbers are allocated by reading MAX(episode)+1, so two episodes
# publishing at the same moment would collide on the number and the filename.
# The number is provisional while audio is being made; the final allocation
# happens under this lock at publish time.
_publish_lock = threading.Lock()


def _finalize(account_id: int, episode: int, filename: str) -> tuple[int, str]:
    """Re-resolve the episode number at publish time, under the lock."""
    final = _next_episode(account_id)
    if final != episode:
        filename = filename.replace(f'ep{episode:03d}_', f'ep{final:03d}_', 1)
        episode = final
    return episode, filename


def _next_episode(account_id: int) -> int:
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT COALESCE(MAX(episode), 0) FROM {TABLE} '
                        f'WHERE account_id=%s', (int(account_id),))
            row = cur.fetchone()
    finally:
        conn.close()
    return int(row[0] if row and row[0] is not None else 0) + 1


def list_for(account_id: int) -> list[dict]:
    """One account's episodes, newest first."""
    ensure_schema()
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT episode, title, filename, tts_model, voice, bytes, '
                f'script_chars, chunks, created_at FROM {TABLE} '
                f'WHERE account_id=%s ORDER BY episode DESC', (int(account_id),))
            rows = cur.fetchall() or []
    finally:
        conn.close()
    return [{
        'episode': r[0], 'title': r[1], 'filename': r[2], 'tts_model': r[3],
        'voice': r[4], 'bytes': r[5], 'script_chars': r[6], 'chunks': r[7],
        'created_at': r[8],
    } for r in rows]


def delete(account_id: int, filename: str) -> dict:
    ensure_schema()
    path = resolve(account_id, filename)
    removed_bytes = 0
    if os.path.isfile(path):
        removed_bytes = os.path.getsize(path)
        os.remove(path)
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f'DELETE FROM {TABLE} WHERE account_id=%s AND filename=%s',
                        (int(account_id), os.path.basename(path)))
        conn.commit()
    finally:
        conn.close()
    return {'ok': True, 'filename': os.path.basename(path), 'bytes': removed_bytes}


# ---------------------------------------------------------------------------
# Manual upload — episodes produced outside this service
# ---------------------------------------------------------------------------
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_UPLOAD_CHUNK = 256 * 1024


def sniff_audio(head: bytes) -> str | None:
    """'mp3' or 'wav' from a file's first bytes; None when it is neither.

    Extension claims lie; the magic bytes do not. MP3 files open with an ID3
    tag or an MPEG frame sync, WAV files with RIFF....WAVE.
    """
    if head[:4] == b'RIFF' and head[8:12] == b'WAVE':
        return 'wav'
    if head[:3] == b'ID3':
        return 'mp3'
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return 'mp3'
    return None


def store_upload(*, stream, account_id: int, title: str | None = None,
                 max_bytes: int | None = None) -> dict:
    """A finished episode produced elsewhere: stream it to disk and catalogue it.

    The file is written in chunks — never held whole in memory — with the size
    cap enforced as it arrives. The stored row looks like any generated
    episode's, so listing, streaming, the RSS feed and deletion all behave
    identically; the tts_model column records that it was a manual upload.
    """
    ensure_schema()
    max_bytes = max_bytes or MAX_UPLOAD_BYTES

    head = stream.read(_UPLOAD_CHUNK)
    ext = sniff_audio(head or b'')
    if not ext:
        raise PodcastError(
            'The uploaded file is not recognizable MP3 or WAV audio.')

    episode = _next_episode(account_id)
    today = datetime.now()
    title = (title or '').strip() or \
        f'Episode {episode} — {db.format_friendly_date(today)}'
    safe = ''.join(ch for ch in title if ch.isalnum() or ch in ' -_').strip()[:80] \
           or f'episode {episode}'
    # Stream to a per-request temp name; the episode number and filename are
    # finalized under the publish lock only once the bytes are all here.
    tmp = os.path.join(account_dir(account_id),
                       f'.upload-{os.getpid()}-{threading.get_ident()}.tmp')

    size = 0
    try:
        with open(tmp, 'wb') as fh:
            chunk = head
            while chunk:
                size += len(chunk)
                if size > max_bytes:
                    raise PodcastError(
                        f'The upload exceeds the '
                        f'{max_bytes // (1024 * 1024)} MB limit.', 413)
                fh.write(chunk)
                chunk = stream.read(_UPLOAD_CHUNK)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise

    with _publish_lock:
        episode, filename = _finalize(
            account_id, episode,
            f'ep{episode:03d}_{safe}_{db.format_friendly_date(today)}.{ext}')
        path = os.path.join(account_dir(account_id), filename)
        os.replace(tmp, path)

        conn = db.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f'INSERT INTO {TABLE} (account_id, episode, title, filename, '
                    f'tts_model, voice, bytes, script_chars, chunks, created_at) '
                    f'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                    (int(account_id), episode, title, filename, 'manual upload',
                     None, size, 0, 0, today))
            conn.commit()
        finally:
            conn.close()

    logging.info(f'Podcast episode {episode} ({filename}) uploaded manually: '
                 f'{size} bytes')
    return {
        'podcast': True,
        'episode': episode,
        'title': title,
        'filename': filename,
        'bytes': size,
        'chunks': 0,
        'tts_model': 'manual upload',
        'voice': None,
    }


def set_source(account_id: int, filename: str, *, tts_model: str,
               voice: str | None = None) -> None:
    """Re-label how an uploaded episode was produced.

    store_upload() writes 'manual upload' because that is true of a file dragged into
    the browser. An episode that arrived from a VibeVoice studio came through the same
    function but is not the same thing, and the console's listing should say so.
    """
    ensure_schema()
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f'UPDATE {TABLE} SET tts_model=%s, voice=%s '
                f'WHERE account_id=%s AND filename=%s',
                (tts_model[:120], voice[:60] if voice else None,
                 int(account_id), os.path.basename(filename)))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
STAGES = {
    'start':      (5,   'Starting up'),
    'synthesize': (10,  'Synthesizing speech'),
    'store':      (92,  'Writing the episode'),
    'done':       (100, 'Complete'),
}


def generate(*, script: str, tts_model: str, voice: str | None = None,
             clone_audio: str | None = None, clone_transcript: str | None = None,
             title: str | None = None, account_id: int, progress=None) -> dict:
    """Synthesize one episode end to end and record it.

    One voice reads the whole script — OpenRouter's /audio/speech carries a
    single voice per request, so that is the shape this feature keeps. The
    script is cleaned of markup artifacts, chunked to the provider's input
    cap, and any chunk a provider still rejects is re-split and retried at
    half size before the job is allowed to fail.
    """
    progress = progress or (lambda *a: None)

    def step(key, pct=None, label=None):
        base_pct, base_label = STAGES[key]
        progress(key, pct if pct is not None else base_pct, label or base_label)

    step('start')
    ensure_schema()

    api_key = db.resolve_openrouter_key()
    if not api_key:
        raise PodcastError('No OpenRouter API key is configured.', 500)

    script = clean_script(script)
    if not script:
        raise PodcastError("'script' is empty.")
    if len(script) > MAX_SCRIPT_CHARS:
        raise PodcastError(
            f'Script is too long ({len(script)} chars; the limit is '
            f'{MAX_SCRIPT_CHARS}).', 413)
    validate_clone(clone_audio)
    refs = _clone_refs(clone_audio, clone_transcript)

    limit = chunk_limit(tts_model)
    chunks = split_script(script, limit)
    if not chunks:
        raise PodcastError("'script' contains no speakable text.")

    episode = _next_episode(account_id)
    today = datetime.now()
    title = (title or '').strip() or f'Episode {episode} — {db.format_friendly_date(today)}'

    # The whole episode is synthesized in one format. We ask for MP3; if the
    # adapter answers "pcm only" (Gemini TTS does), the episode switches to
    # PCM and is encoded to MP3 here after synthesis.
    fmt = {'value': 'mp3'}

    def synthesize(text: str, n: int, rescued: bool = False) -> bytes:
        try:
            return openrouter.speech(api_key, tts_model, text, voice=voice,
                                     input_references=refs,
                                     response_format=fmt['value'])
        except openrouter.OpenRouterError as e:
            if fmt['value'] == 'mp3' and wants_pcm(e):
                logging.info(f'{tts_model} only emits PCM; switching the '
                             'episode to PCM and encoding locally')
                fmt['value'] = 'pcm'
                return synthesize(text, n, rescued=rescued)
            # A 400/413 on a sizeable chunk usually means this provider's input
            # cap is tighter than our default — halve the chunk and try the
            # pieces before giving up. One level of rescue only.
            if not rescued and e.status in (400, 413) and len(text) > 900:
                logging.warning(
                    f'TTS rejected a {len(text)}-char part on {tts_model} '
                    f'(HTTP {e.status}); retrying it in halves')
                pieces = split_script(text, max(900, len(text) // 2))
                if len(pieces) > 1:
                    return b''.join(synthesize(p, n, rescued=True)
                                    for p in pieces)
            detail = f'{e} | upstream: {e.body}' if e.body else str(e)
            raise PodcastError(f'Text-to-speech failed on part {n} of '
                               f'{len(chunks)}: {detail[:400]}', 502)

    audio = bytearray()
    for n, chunk in enumerate(chunks, start=1):
        pct = 10 + int(80 * (n - 1) / len(chunks))
        step('synthesize', pct, f'Synthesizing speech — part {n} of {len(chunks)}')
        audio.extend(synthesize(chunk, n))

    # Concatenated MP3 segments play as one file; concatenated raw PCM frames
    # are literally one continuous recording — encode those to MP3 (or fall
    # back to a WAV container when no encoder is installed on this host).
    ext = 'mp3'
    data = bytes(audio)
    if fmt['value'] == 'pcm':
        step('synthesize', 90, 'Encoding the episode audio')
        mp3 = encode_pcm_to_mp3(data)
        if mp3:
            data = mp3
        else:
            logging.warning('No MP3 encoder available (pip install lameenc '
                            'or install ffmpeg); storing the episode as WAV')
            data = wrap_pcm_as_wav(data)
            ext = 'wav'

    step('store')
    safe = ''.join(ch for ch in title if ch.isalnum() or ch in ' -_').strip()[:80] \
           or f'episode {episode}'
    with _publish_lock:
        episode, filename = _finalize(
            account_id, episode,
            f'ep{episode:03d}_{safe}_{db.format_friendly_date(today)}.{ext}')
        path = os.path.join(account_dir(account_id), filename)
        tmp = path + '.building'
        with open(tmp, 'wb') as fh:
            fh.write(data)
        os.replace(tmp, path)

        conn = db.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f'INSERT INTO {TABLE} (account_id, episode, title, filename, '
                    f'tts_model, voice, bytes, script_chars, chunks, created_at) '
                    f'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                    (int(account_id), episode, title, filename, tts_model,
                     voice or None, len(data), len(script), len(chunks), today))
            conn.commit()
        finally:
            conn.close()

    step('done')
    logging.info(f'Podcast episode {episode} ({filename}) generated: '
                 f'{len(data)} bytes from {len(chunks)} part(s) via {tts_model}')
    return {
        'podcast': True,
        'episode': episode,
        'title': title,
        'filename': filename,
        'bytes': len(data),
        'chunks': len(chunks),
        'tts_model': tts_model,
        'voice': voice or None,
    }
