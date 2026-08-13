"""Writing a two-host podcast script from reports this account already owns.

The console's script builder posts a handful of report filenames, some instructions and
two host names; this turns that into a script the VibeVoice studio can render directly.

Doing it here rather than on the website is deliberate. The reports live on this disk, the
OpenRouter key lives in this process, and the web tier is a thin public front end that
holds neither. Extracting the PDFs' text here means the report content makes one hop to
the LLM instead of being shipped out to the site and back.

The output contract is VibeVoice's own script format, which is what the studio's parser
expects (`vibevoice_automation_service.VibeVoiceScriptParser`):

    Speaker 1: (fired up) Welcome in, we've got a big one today.
    Speaker 2: Look, the tape says otherwise.

One line per turn, `Speaker 1` and `Speaker 2` only, an optional parenthetical emotion
immediately after the colon. Anything else — markdown headings, stage directions on their
own line, bold text — makes the parser drop or mangle lines, so the prompt forbids it and
`tidy()` strips what slips through anyway.
"""

import logging
import re

import config
import db
import openrouter
import reports_store

# Per report. Enough for a full matchup report's prose while leaving room for several
# reports plus the instructions inside a normal context window.
MAX_REPORT_CHARS = 24_000
MAX_REPORTS = 8
MAX_INSTRUCTION_CHARS = 8_000

DEFAULT_MODEL = 'deepseek/deepseek-v4-flash-0731'


class ScriptError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Reading the source reports
# ---------------------------------------------------------------------------
def report_text(account_id: int, filename: str, limit: int = MAX_REPORT_CHARS) -> str:
    """The text layer of one stored report, truncated.

    Charts and tables are images and contribute nothing here; what comes back is the
    written analysis, which is exactly what a host would talk from.
    """
    try:
        import fitz                      # PyMuPDF, already a dependency for rendering
    except ImportError:
        raise ScriptError('PyMuPDF is not installed on the service.', 503)

    path = reports_store.resolve(account_id, filename)
    parts: list[str] = []
    total = 0
    try:
        with fitz.open(path) as doc:
            for page in doc:
                text = (page.get_text() or '').strip()
                if not text:
                    continue
                parts.append(text)
                total += len(text)
                if total >= limit:
                    break
    except Exception as e:
        logging.warning(f'Could not read text from {filename}: {e}')
        raise ScriptError(f'Could not read "{filename}".', 422)

    joined = '\n\n'.join(parts).strip()
    if not joined:
        raise ScriptError(f'"{filename}" has no extractable text.', 422)
    return joined[:limit]


def gather(account_id: int, filenames: list[str]) -> list[dict]:
    """Text for each requested report. One unreadable report does not sink the batch."""
    if not filenames:
        return []
    if len(filenames) > MAX_REPORTS:
        raise ScriptError(f'Select at most {MAX_REPORTS} reports.')
    out = []
    for name in filenames:
        try:
            out.append({'filename': name, 'text': report_text(account_id, name)})
        except ScriptError as e:
            logging.warning(f'Skipping report {name} in script build: {e}')
    if filenames and not out:
        raise ScriptError('None of the selected reports could be read.', 422)
    return out


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------
_SYSTEM = (
    "You write scripts for a two-host college football podcast. You write dialogue that "
    "sounds spoken, not written: contractions, interruptions, short sentences, real "
    "reactions. You never invent statistics — every number you use comes from the source "
    "material you are given."
)


def build_prompt(*, instructions: str, reports: list[dict], host_a: str, host_b: str,
                 minutes: int = 12) -> str:
    parts = [
        f'Write a {minutes}-minute podcast segment for two hosts.',
        '',
        f'HOST 1 — {host_a} (lead host): drives the show, opens and closes it, sets up '
        f'the talking points, brings energy and fan perspective, commits to takes.',
        f'HOST 2 — {host_b} (analyst): the football brain. Xs and Os, tape, situational '
        f'detail. Pushes back on {host_a} when the take outruns the evidence.',
        '',
        'WHAT THIS EPISODE COVERS:',
        instructions.strip() or 'Break down the material below for a general audience.',
    ]

    if reports:
        parts += ['', 'SOURCE MATERIAL — this is the only place your facts may come '
                      'from. Use the specific numbers, names and records in it:']
        for r in reports:
            parts += ['', f'--- BEGIN {r["filename"]} ---', r['text'],
                      f'--- END {r["filename"]} ---']

    parts += [
        '',
        'OUTPUT FORMAT — follow this exactly, it is parsed by a machine:',
        '',
        '  Speaker 1: (fired up) Welcome into the show, we have got a big one today.',
        '  Speaker 2: Look, the tape says something different.',
        '',
        f'  - "Speaker 1" is {host_a}. "Speaker 2" is {host_b}. Use only these two labels.',
        '  - One turn per line. No blank lines inside the script.',
        '  - An optional emotion in parentheses may follow the colon, e.g. '
        '"(laughing)", "(deadpan)". Nothing else in parentheses.',
        '  - Plain text only: no markdown, no headings, no bold, no bullet points, no '
        'stage directions on their own line, no sound-effect cues, no episode title, '
        'no commentary before or after the script.',
        f'  - The hosts refer to each other by name — {host_a} and {host_b} — the way '
        'real co-hosts do, not every line.',
        '  - Spell out numbers the way they are said aloud: "twenty-one to seventeen", '
        '"third and long", "a hundred and forty rushing yards".',
        '',
        'Begin with Speaker 1 welcoming the audience and end with a sign-off. Output the '
        'script and nothing else.',
    ]
    return '\n'.join(parts)


# ---------------------------------------------------------------------------
# Cleaning what comes back
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r'^\s*```.*$', re.M)
_SPEAKER_RE = re.compile(r'^Speaker\s*([12])\s*:\s*(.*)$', re.IGNORECASE)
# Models like to answer with "**Speaker 1:**" or "HOST 1:" no matter what they are told.
_LOOSE_SPEAKER_RE = re.compile(
    r'^\s*(?:\*\*|__)?\s*(?:speaker|host)\s*([12])\s*(?:\*\*|__)?\s*[:\-—]\s*(.*)$',
    re.IGNORECASE)
_BOLD_RE = re.compile(r'(\*\*|__)(.+?)\1', re.S)


def tidy(raw: str) -> str:
    """Coerce a model's answer into lines the studio's parser accepts.

    Everything that is not a recognizable speaker turn is dropped rather than repaired:
    a stray heading or a closing "Hope this helps!" would otherwise be read aloud.
    """
    text = _FENCE_RE.sub('', raw or '')
    text = _BOLD_RE.sub(r'\2', text)
    kept: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _SPEAKER_RE.match(line) or _LOOSE_SPEAKER_RE.match(line)
        if not m:
            continue
        speaker, said = m.group(1), m.group(2).strip()
        if said:
            kept.append(f'Speaker {speaker}: {said}')
    return '\n'.join(kept)


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------
def generate(*, account_id: int, instructions: str, report_filenames: list[str],
             model: str | None = None, host_a: str = 'Jake',
             host_b: str = 'Coach Mac', minutes: int = 12) -> dict:
    """Write the episode script. Returns the script plus what it was built from."""
    instructions = (instructions or '').strip()[:MAX_INSTRUCTION_CHARS]
    report_filenames = [f for f in (report_filenames or []) if f]
    if not instructions and not report_filenames:
        raise ScriptError('Give some instructions, or select at least one report.')

    key = db.resolve_openrouter_key()
    if not key:
        raise ScriptError('No OpenRouter key is configured on the service.', 503)

    reports = gather(account_id, report_filenames)
    prompt = build_prompt(instructions=instructions, reports=reports,
                          host_a=host_a or 'Jake', host_b=host_b or 'Coach Mac',
                          minutes=minutes)

    try:
        resp = openrouter.chat(
            key, model or DEFAULT_MODEL,
            [{'role': 'system', 'content': _SYSTEM},
             {'role': 'user', 'content': prompt}],
            max_tokens=16_000, timeout=600)
    except openrouter.OpenRouterError as e:
        raise ScriptError(f'The model call failed: {e}', 502)

    raw = openrouter.extract_text(resp)
    script = tidy(raw)
    if not script:
        raise ScriptError(
            'The model did not return anything in the two-speaker script format.', 502)

    lines = script.count('\n') + 1
    logging.info(f'Podcast script built for account {account_id}: {lines} lines, '
                 f'{len(script)} chars from {len(reports)} report(s)')
    return {
        'script': script,
        'lines': lines,
        'chars': len(script),
        'model': model or DEFAULT_MODEL,
        'hosts': {'1': host_a, '2': host_b},
        'reports_used': [r['filename'] for r in reports],
        'usage': openrouter.extract_usage(resp),
    }


# ---------------------------------------------------------------------------
# Model catalog for the console's picker
# ---------------------------------------------------------------------------
def text_models() -> list[dict]:
    """OpenRouter models that take text and return text, cheapest metadata only.

    The console shows this in the script builder's model dropdown. Speech and image-only
    models are filtered out — they cannot write a script.
    """
    import requests

    url = f"{config.OPENROUTER_BASE_URL.rstrip('/')}/models"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise ScriptError(f'Could not read the OpenRouter model list: {e}', 502)

    out = []
    for m in data.get('data') or []:
        arch = m.get('architecture') or {}
        if 'text' not in (arch.get('input_modalities') or ['text']):
            continue
        if 'text' not in (arch.get('output_modalities') or ['text']):
            continue
        pricing = m.get('pricing') or {}
        out.append({
            'id': m.get('id'),
            'name': m.get('name') or m.get('id'),
            'context': m.get('context_length'),
            'prompt_price': pricing.get('prompt'),
            'completion_price': pricing.get('completion'),
        })
    out.sort(key=lambda m: (m['name'] or '').lower())
    return out
