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
MAX_SPEAKERS = 6
MAX_CLONE_B64 = 12_000_000   # base64 chars per voice sample; OpenRouter caps at 20 MiB
_NAME_RE = re.compile(r"[A-Za-z0-9 ._'\-]{1,40}")

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
# Speakers
# ---------------------------------------------------------------------------
def normalize_speakers(raw) -> list[dict] | None:
    """Validate a request's speaker roster into a clean list, or None.

    Each speaker is a name plus how to voice it: a provider voice ID, and/or a
    stateless-cloning reference (a data:audio base64 sample with an optional
    transcript) for models whose OpenRouter endpoint supports cloning.
    """
    if raw in (None, '', []):
        return None
    if not isinstance(raw, list):
        raise PodcastError("'speakers' must be a list of speaker objects.")
    if len(raw) > MAX_SPEAKERS:
        raise PodcastError(f'At most {MAX_SPEAKERS} speakers per episode.')
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise PodcastError("Each speaker must be an object with a 'name'.")
        name = str(item.get('name') or '').strip()
        if not name or not _NAME_RE.fullmatch(name):
            raise PodcastError(
                "Every speaker needs a name of up to 40 characters using "
                "letters, numbers, spaces, dots, dashes or apostrophes.")
        if name.lower() in seen:
            raise PodcastError(f'Speaker name {name!r} appears twice.')
        seen.add(name.lower())
        voice = str(item.get('voice') or '').strip() or None
        if voice and len(voice) > 120:
            raise PodcastError(f'Voice ID for {name!r} is too long.')
        clone = str(item.get('clone_audio') or '').strip() or None
        if clone:
            if not clone.startswith('data:audio/'):
                raise PodcastError(
                    f"The voice sample for {name!r} must be a "
                    "data:audio/...;base64 URI.")
            if len(clone) > MAX_CLONE_B64:
                raise PodcastError(
                    f'The voice sample for {name!r} is too large '
                    '(about 8 MB of audio at most).', 413)
        transcript = str(item.get('clone_transcript') or '').strip() or None
        out.append({'name': name, 'voice': voice,
                    'clone_audio': clone, 'clone_transcript': transcript})
    return out or None


def speakers_signature(speakers: list[dict] | None) -> str:
    """A short stable digest of the roster, for job dedup keys."""
    if not speakers:
        return ''
    parts = [f"{s['name']}={s['voice'] or ''}:{(s['clone_audio'] or '')[:80]}"
             for s in speakers]
    return '|'.join(parts)


_LABEL_RE = re.compile(r"^\s*[*_]{0,2}([^:\n]{1,40}?)[*_]{0,2}\s*:\s*(.*)$")


def split_dialogue(script: str, names: list[str]) -> list[tuple[str, str]]:
    """(speaker, text) turns in script order, with the name labels stripped.

    A turn starts at a line beginning with a configured speaker's name and a
    colon (markdown bold/italic around the name is tolerated, since LLMs love
    writing '**HOST:**'). Only configured names count as labels — 'Score: 21-14'
    inside a turn stays spoken text. Anything before the first label belongs to
    the first configured speaker.
    """
    lookup = {n.lower(): n for n in names}
    turns: list[tuple[str, list[str]]] = []
    current = names[0]
    lines: list[str] = []

    def flush():
        text = '\n'.join(lines).strip()
        if text:
            if turns and turns[-1][0] == current:
                turns[-1][1].append(text)
            else:
                turns.append((current, [text]))

    for line in script.split('\n'):
        m = _LABEL_RE.match(line)
        label = m.group(1).strip().lower() if m else None
        if label in lookup:
            flush()
            lines = []
            current = lookup[label]
            rest = m.group(2).strip()
            if rest:
                lines.append(rest)
        else:
            lines.append(line)
    flush()
    return [(name, '\n\n'.join(chunks)) for name, chunks in turns]


def _clone_refs(speaker: dict | None) -> list | None:
    if not speaker or not speaker.get('clone_audio'):
        return None
    refs = [{'type': 'input_audio',
             'input_audio': {'data': speaker['clone_audio']}}]
    if speaker.get('clone_transcript'):
        refs.append({'type': 'text', 'text': speaker['clone_transcript']})
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
             speakers: list[dict] | None = None,
             title: str | None = None, account_id: int, progress=None) -> dict:
    """Synthesize one episode end to end and record it.

    With one speaker (or the legacy bare voice) the whole script is read in
    that voice. With several, the script is a dialogue: it is split into turns
    at the speakers' name labels, every turn is synthesized in its speaker's
    voice, and the segments are stitched in order — which is what makes
    multi-voice episodes work identically on every TTS model, one voice per
    /audio/speech call.
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

    script = (script or '').strip()
    if not script:
        raise PodcastError("'script' is empty.")
    if len(script) > MAX_SCRIPT_CHARS:
        raise PodcastError(
            f'Script is too long ({len(script)} chars; the limit is '
            f'{MAX_SCRIPT_CHARS}).', 413)

    # The synthesis plan: (voice, cloning refs, text) per chunk, in episode order.
    plan: list[tuple[str | None, list | None, str]] = []
    if speakers and len(speakers) > 1:
        turns = split_dialogue(script, [s['name'] for s in speakers])
        by_name = {s['name']: s for s in speakers}
        for name, text in turns:
            sp = by_name[name]
            for chunk in split_script(text):
                plan.append((sp['voice'], _clone_refs(sp), chunk))
        voice_label = ' + '.join(s['name'] for s in speakers)[:60]
    else:
        solo = speakers[0] if speakers else None
        solo_voice = (solo['voice'] if solo and solo['voice'] else voice)
        refs = _clone_refs(solo)
        for chunk in split_script(script):
            plan.append((solo_voice, refs, chunk))
        voice_label = solo_voice or voice
    if not plan:
        raise PodcastError("'script' contains no speakable text.")

    episode = _next_episode(account_id)
    today = datetime.now()
    title = (title or '').strip() or f'Episode {episode} — {db.format_friendly_date(today)}'

    audio = bytearray()
    for n, (part_voice, part_refs, chunk) in enumerate(plan, start=1):
        pct = 10 + int(80 * (n - 1) / len(plan))
        step('synthesize', pct, f'Synthesizing speech — part {n} of {len(plan)}')
        try:
            audio.extend(openrouter.speech(api_key, tts_model, chunk,
                                           voice=part_voice,
                                           input_references=part_refs))
        except openrouter.OpenRouterError as e:
            detail = f'{e} | upstream: {e.body}' if e.body else str(e)
            raise PodcastError(f'Text-to-speech failed on part {n} of '
                               f'{len(plan)}: {detail[:400]}', 502)

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
                 voice_label or None, len(audio), len(script), len(plan), today))
        conn.commit()
    finally:
        conn.close()

    step('done')
    logging.info(f'Podcast episode {episode} ({filename}) generated: '
                 f'{len(audio)} bytes from {len(plan)} part(s) via {tts_model}')
    return {
        'podcast': True,
        'episode': episode,
        'title': title,
        'filename': filename,
        'bytes': len(audio),
        'chunks': len(plan),
        'tts_model': tts_model,
        'voice': voice_label or None,
        'speakers': ([{'name': s['name'], 'voice': s['voice']} for s in speakers]
                     if speakers else None),
    }
