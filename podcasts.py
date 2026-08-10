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
    if not name or name.startswith('.') or not name.lower().endswith('.mp3'):
        raise PodcastError(f'Unsafe podcast filename: {filename!r}')
    base = os.path.realpath(account_dir(account_id, create=False))
    path = os.path.realpath(os.path.join(base, name))
    if not path.startswith(base + os.sep):
        raise PodcastError(f'Podcast path escapes its account directory: {filename!r}')
    return path


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

    def synthesize(text: str, n: int, rescued: bool = False) -> bytes:
        try:
            return openrouter.speech(api_key, tts_model, text, voice=voice,
                                     input_references=refs)
        except openrouter.OpenRouterError as e:
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

    step('store')
    safe = ''.join(ch for ch in title if ch.isalnum() or ch in ' -_').strip()[:80] \
           or f'episode {episode}'
    filename = f'ep{episode:03d}_{safe}_{db.format_friendly_date(today)}.mp3'
    path = os.path.join(account_dir(account_id), filename)
    tmp = path + '.building'
    with open(tmp, 'wb') as fh:
        fh.write(bytes(audio))
    os.replace(tmp, path)

    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f'INSERT INTO {TABLE} (account_id, episode, title, filename, '
                f'tts_model, voice, bytes, script_chars, chunks, created_at) '
                f'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                (int(account_id), episode, title, filename, tts_model,
                 voice or None, len(audio), len(script), len(chunks), today))
        conn.commit()
    finally:
        conn.close()

    step('done')
    logging.info(f'Podcast episode {episode} ({filename}) generated: '
                 f'{len(audio)} bytes from {len(chunks)} part(s) via {tts_model}')
    return {
        'podcast': True,
        'episode': episode,
        'title': title,
        'filename': filename,
        'bytes': len(audio),
        'chunks': len(chunks),
        'tts_model': tts_model,
        'voice': voice or None,
    }
