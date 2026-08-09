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
             title: str | None = None, account_id: int, progress=None) -> dict:
    """Synthesize one episode end to end and record it."""
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

    chunks = split_script(script)
    if not chunks:
        raise PodcastError("'script' contains no speakable text.")

    episode = _next_episode(account_id)
    today = datetime.now()
    title = (title or '').strip() or f'Episode {episode} — {db.format_friendly_date(today)}'

    audio = bytearray()
    for n, chunk in enumerate(chunks, start=1):
        pct = 10 + int(80 * (n - 1) / len(chunks))
        step('synthesize', pct, f'Synthesizing speech — part {n} of {len(chunks)}')
        try:
            audio.extend(openrouter.speech(api_key, tts_model, chunk, voice=voice))
        except openrouter.OpenRouterError as e:
            detail = f'{e} | upstream: {e.body}' if e.body else str(e)
            raise PodcastError(f'Text-to-speech failed on part {n} of '
                               f'{len(chunks)}: {detail[:400]}', 502)

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
                 f'{len(audio)} bytes from {len(chunks)} chunk(s) via {tts_model}')
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
