"""Thin OpenRouter client.

Every LLM call in this service goes through here: the eight live-web research calls
(GPT-5.6 Luna) and the single report-synthesis call (Kimi K3). OpenRouter speaks the
OpenAI Chat Completions dialect, and normalizes web-search results from every engine
into the same `message.annotations[].url_citation` shape.
"""

import json
import logging
import re
import threading
import time

import requests

import config


# ---------------------------------------------------------------------------
# The model catalog
# ---------------------------------------------------------------------------
# OpenRouter marks a model that can browse on its own by accepting the
# web_search_options parameter — the Perplexity Sonar family, OpenAI's 4o line
# and a few others. That is the ONLY class for which engine="native" is valid.
# Every other model still searches fine through OpenRouter's own plugin (Exa),
# which is why the console frames this as a capability, not a requirement.
NATIVE_SEARCH_PARAM = 'web_search_options'
_MODELS_TTL = 6 * 3600
_MODELS_CACHE: dict = {'at': 0.0, 'rows': []}
_MODELS_LOCK = threading.Lock()


def text_models(force: bool = False) -> list[dict]:
    """Every text-generating model on OpenRouter, annotated and sorted.

    Cached in-process for six hours: the catalogue moves on the order of days,
    and this is read every time an operator opens the console.
    """
    with _MODELS_LOCK:
        if (not force and _MODELS_CACHE['rows']
                and time.time() - _MODELS_CACHE['at'] < _MODELS_TTL):
            return _MODELS_CACHE['rows']

    url = f"{config.OPENROUTER_BASE_URL.rstrip('/')}/models"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json().get('data') or []
    except Exception as e:
        logging.warning(f'OpenRouter model catalogue unavailable: {e}')
        with _MODELS_LOCK:
            return _MODELS_CACHE['rows']       # stale beats empty

    rows = []
    for m in data:
        arch = m.get('architecture') or {}
        if 'text' not in (arch.get('input_modalities') or ['text']):
            continue
        if 'text' not in (arch.get('output_modalities') or ['text']):
            continue                            # image/speech-only models
        model_id = (m.get('id') or '').strip()
        if not model_id:
            continue
        params = m.get('supported_parameters') or []
        pricing = m.get('pricing') or {}
        try:
            prompt_cost = float(pricing.get('prompt') or 0) * 1_000_000
        except (TypeError, ValueError):
            prompt_cost = 0.0
        rows.append({
            'id': model_id,
            'name': (m.get('name') or model_id).strip(),
            'native_search': NATIVE_SEARCH_PARAM in params,
            # What the model will actually accept. Sending a parameter a model
            # rejects fails the whole call, so callers consult these first.
            'structured_output': ('response_format' in params
                                  or 'structured_outputs' in params),
            'reasoning': 'reasoning' in params,
            'context': m.get('context_length'),
            'context_length': m.get('context_length'),
            'max_completion_tokens': (m.get('top_provider') or {}).get(
                'max_completion_tokens'),
            'prompt_price': pricing.get('prompt'),
            'completion_price': pricing.get('completion'),
            'prompt_cost_per_million': round(prompt_cost, 4),
        })
    # Native-search models first — they are the ones the wire benefits from —
    # then alphabetically so a long list stays scannable.
    rows.sort(key=lambda r: (not r['native_search'], r['name'].lower()))

    with _MODELS_LOCK:
        _MODELS_CACHE.update(at=time.time(), rows=rows)
    return rows



def capabilities(model: str) -> dict:
    """What this model accepts, from the cached catalogue.

    'known' is False when the catalogue could not be read or does not carry
    the model; callers then fall back to sending everything, which is what the
    service did before capabilities existed.
    """
    for row in text_models():
        if row['id'] == model:
            return {'known': True,
                    'structured_output': row['structured_output'],
                    'reasoning': row['reasoning'],
                    'native_search': row['native_search']}
    return {'known': False, 'structured_output': True,
            'reasoning': True, 'native_search': False}


class OpenRouterError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # Attribution headers so spend is traceable in the OpenRouter dashboard.
        "HTTP-Referer": config.OPENROUTER_REFERER,
        "X-Title": config.OPENROUTER_APP_TITLE,
    }


def web_search_plugin(
    settings: dict | None = None,
    search_prompt: str | None = None,
) -> list[dict]:
    """Build the OpenRouter web-search plugin block for a resolved settings dict.

    engine="native" uses the upstream provider's own live browsing, which only exists
    for OpenAI/Anthropic/Google/xAI models — DeepSeek has none, so it must use "exa".
    Either engine returns citations normalized as url_citation annotations.
    """
    settings = settings or config.default_settings()
    plugin: dict = {
        "id": "web",
        "max_results": int(settings.get("search_max_results")
                           or config.OPENROUTER_SEARCH_MAX_RESULTS),
    }
    engine = str(settings.get("search_engine") or "").strip().lower()
    if engine in ("native", "exa"):
        plugin["engine"] = engine
    if search_prompt:
        plugin["search_prompt"] = search_prompt
    return [plugin]



def fit_max_tokens(model: str, requested: int | None, messages: list | None = None):
    """Trim an output budget to what the model can actually accept.

    A generous max_tokens is harmless on a million-token model and fatal on a
    128k one: OpenRouter rejects the whole request when input + output exceeds
    the context window, before any work happens. Callers should not have to
    know each model's ceiling, so the clamp lives here, next to the catalogue
    that knows it. Returns the request unchanged when the model is unknown.
    """
    if not requested:
        return requested
    row = None
    for candidate in text_models():
        if candidate['id'] == model:
            row = candidate
            break
    if not row:
        return requested
    allowed = requested
    ceiling = row.get('max_completion_tokens')
    if ceiling:
        allowed = min(allowed, int(ceiling))
    context = row.get('context_length')
    if context:
        # There is no local tokenizer here, and the message text is not the
        # whole input: the search plugin's instructions and the results it
        # injects land on the input side too, which is how a 4KB prompt bills
        # as 3900 input tokens. A tuned characters-per-token ratio would be
        # false precision against that, so assume a dense two characters per
        # token and keep an eighth of the window free on top. Anything this
        # still gets wrong, the upstream's own numbers correct on retry.
        chars = sum(len(str(m.get('content') or '')) for m in (messages or []))
        room = int(context) - (chars // 2) - max(2048, int(context) // 8)
        allowed = min(allowed, max(room, 256))
    if allowed < requested:
        logging.info(f"max_tokens trimmed {requested} -> {allowed} to fit "
                     f"{model} (context {context}, cap {ceiling})")
    return allowed


_CONTEXT_LIMIT_RE = re.compile(
    r"maximum context length is (\d+) tokens.*?\(\s*(\d+) of text input", re.S
)


def refit_from_context_error(detail: str, current: int | None) -> int | None:
    """Recompute max_tokens from an over-context rejection's own numbers.

    OpenRouter's 400 states the window and how much of the request was input:
    "maximum context length is 127072 tokens. However, you requested about
    128529 tokens (3908 of text input, 124621 in the output)". Those figures
    beat any estimate we could make locally, so a rejection on size is worth
    one more attempt rather than a lost research call. Returns None when the
    message is not that error, or the numbers do not explain it. The budget
    it returns is always strictly smaller than the one that was rejected.
    """
    match = _CONTEXT_LIMIT_RE.search(detail or "")
    if not match:
        return None
    window, prompt_tokens = int(match.group(1)), int(match.group(2))
    # Nine tenths of what is left, because the input side can grow between
    # attempts as the plugin pulls different search results.
    room = int((window - prompt_tokens) * 0.9)
    if room < 256:
        return None
    if current and room >= current:
        # The stated figures leave room for the budget that was just refused,
        # so the size is not what upstream is actually objecting to. Retrying
        # the same request would spend an attempt to learn nothing.
        return None
    return room


def chat(
    api_key: str,
    model: str,
    messages: list[dict],
    *,
    plugins: list[dict] | None = None,
    response_format: dict | None = None,
    effort: str | None = None,
    max_tokens: int | None = None,
    timeout: int = 300,
    retries: int = 2,
) -> dict:
    """POST /chat/completions with backoff on transient failures.

    If the request is rejected while a json_schema response_format is attached, we retry
    once without it — some upstream providers reject schema-constrained decoding when a
    server-side tool (web search) is also in play, and a lenient parse of prose JSON is
    far better than losing the whole research section.
    """
    url = f"{config.OPENROUTER_BASE_URL.rstrip('/')}/chat/completions"
    body: dict = {"model": model, "messages": messages}
    if plugins:
        body["plugins"] = plugins
    if response_format:
        body["response_format"] = response_format
    if effort:
        body["reasoning"] = {"effort": effort}
    if max_tokens:
        body["max_tokens"] = fit_max_tokens(model, max_tokens, messages)

    dropped_schema = False
    dropped_reasoning = False
    refitted = False
    last_err: Exception | None = None

    # Shedding a rejected parameter is not a transient failure, so it gets its
    # own small budget rather than eating the backoff retries: a model that
    # turns down both a schema and a reasoning effort used to arrive at the
    # first real timeout with no attempts left.
    attempt = 0
    ladder = 0
    while attempt <= retries and ladder <= 3:
        try:
            resp = requests.post(url, json=body, headers=_headers(api_key), timeout=timeout)
        except requests.RequestException as e:
            last_err = e
            logging.warning(f"OpenRouter {model} transport error (attempt {attempt + 1}): {e}")
            time.sleep(2 ** attempt)
            attempt += 1
            continue

        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                raise OpenRouterError("OpenRouter returned non-JSON", resp.status_code, resp.text[:800])
            # OpenRouter surfaces upstream failures as a 200 with an "error" member.
            if isinstance(data, dict) and data.get("error") and not data.get("choices"):
                err = data["error"]
                raise OpenRouterError(f"OpenRouter upstream error: {err}", 200, json.dumps(err)[:800])
            # A provider can also die MID-generation: HTTP 200, choices present, but
            # finish_reason "error" and no content — typically after some reasoning
            # tokens, billed at zero. That is the provider's outage, not this
            # request's fault, so it gets the same backoff-and-retry as a 502.
            if finish_reason(data) == "error" and attempt < retries:
                wait = 2 ** attempt * 2
                logging.warning(
                    f"OpenRouter {model} provider failed mid-generation "
                    f"(finish_reason=error, usage={json.dumps(data.get('usage') or {})[:200]}); "
                    f"retrying in {wait}s"
                )
                last_err = OpenRouterError("provider finish_reason=error", 200)
                time.sleep(wait)
                attempt += 1
                continue
            return data

        # A rejection on size names its own fix, so take the upstream's
        # numbers over the estimate that got us here. This runs ahead of the
        # parameter drops below: an over-context request is not the schema's
        # fault, and shedding one would only lose the schema and fail again.
        if resp.status_code == 400 and not refitted:
            fitted = refit_from_context_error(resp.text, body.get("max_tokens"))
            if fitted:
                logging.warning(
                    f"OpenRouter {model} over context; refitting max_tokens "
                    f"{body.get('max_tokens')} -> {fitted} from the upstream's own count"
                )
                body["max_tokens"] = fitted
                refitted = True
                ladder += 1
                continue

        # A model that rejects one unsupported parameter usually rejects the
        # next as well, so the retry ladder sheds them one at a time rather
        # than giving up after the schema.
        if (resp.status_code in (400, 404, 422) and body.get("reasoning")
                and dropped_schema and not dropped_reasoning):
            logging.warning(f"OpenRouter {model} rejected reasoning "
                            f"({resp.status_code}); retrying without it")
            body.pop("reasoning", None)
            dropped_reasoning = True
            ladder += 1
            continue
        if resp.status_code in (400, 404, 422) and response_format and not dropped_schema:
            logging.warning(
                f"OpenRouter {model} rejected response_format ({resp.status_code}); "
                f"retrying without schema. Body: {resp.text[:300]}"
            )
            body.pop("response_format", None)
            dropped_schema = True
            ladder += 1
            continue

        if resp.status_code in (408, 409, 429, 500, 502, 503, 504) and attempt < retries:
            wait = 2 ** attempt * 2
            logging.warning(f"OpenRouter {model} HTTP {resp.status_code}; retrying in {wait}s")
            time.sleep(wait)
            last_err = OpenRouterError(f"HTTP {resp.status_code}", resp.status_code, resp.text[:800])
            attempt += 1
            continue

        raise OpenRouterError(
            f"OpenRouter request failed for {model}", resp.status_code, resp.text[:800]
        )

    raise OpenRouterError(f"OpenRouter request failed for {model} after retries: {last_err}")


def extract_text(resp: dict) -> str:
    """Pull assistant text out of a Chat Completions response."""
    for choice in resp.get("choices") or []:
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        # Some providers return content as a list of typed parts.
        if isinstance(content, list):
            parts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") in ("text", "output_text")
            ]
            joined = "\n".join(p for p in parts if p).strip()
            if joined:
                return joined
    return ""


def extract_citations(resp: dict) -> list[dict]:
    """Return [{url, title, content}] from url_citation annotations, de-duplicated."""
    out: list[dict] = []
    seen: set[str] = set()
    for choice in resp.get("choices") or []:
        message = choice.get("message") or {}
        for ann in message.get("annotations") or []:
            if not isinstance(ann, dict):
                continue
            cite = ann.get("url_citation") or (ann if ann.get("url") else None)
            if not isinstance(cite, dict):
                continue
            url = (cite.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            out.append({
                "url": url,
                "title": (cite.get("title") or "").strip(),
                "content": (cite.get("content") or "").strip(),
                # Exa dates the results it returns. That is an independent
                # read on freshness — worth keeping for the pages that refuse
                # to be fetched, where the alternative is no date at all.
                "published": next(
                    (str(cite[k]).strip() for k in
                     ("published_date", "publishedDate", "published_time",
                      "publishedTime", "published", "date")
                     if cite.get(k)),
                    ""),
            })
    return out


def finish_reason(resp: dict) -> str:
    for choice in resp.get("choices") or []:
        fr = choice.get("finish_reason") or choice.get("native_finish_reason")
        if fr:
            return str(fr)
    return ""


def has_reasoning(resp: dict) -> bool:
    """True when the model emitted reasoning tokens but no visible content."""
    for choice in resp.get("choices") or []:
        message = choice.get("message") or {}
        if message.get("reasoning") or message.get("reasoning_details"):
            return True
    return False


def extract_usage(resp: dict) -> dict:
    usage = resp.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    return {
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": details.get("reasoning_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cost": usage.get("cost"),
    }


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def parse_json_lenient(text: str) -> dict | None:
    """Best-effort JSON extraction from a model response.

    Handles: clean JSON, fenced code blocks, and JSON preceded/followed by prose.
    Returns None when nothing parseable is found so callers can fall back to raw text.
    """
    if not text:
        return None

    candidates: list[str] = [text.strip()]

    fenced = _FENCE_RE.findall(text)
    candidates = [f.strip() for f in fenced] + candidates

    # Widest {...} span in the response.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])

    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"findings": parsed}
    return None


def speech(api_key: str, model: str, text: str, voice: str | None = None,
           input_references: list | None = None, response_format: str = 'mp3',
           timeout: int = 300, retries: int = 3) -> bytes:
    """One text-to-speech synthesis call. Returns audio bytes.

    response_format is 'mp3' or 'pcm'. Some adapters (Gemini TTS) only emit
    raw PCM and reject an mp3 ask with a 400 — the caller detects that and
    re-requests as PCM, then encodes the audio itself.

    OpenRouter speaks the OpenAI dialect, whose TTS endpoint is /audio/speech.
    Callers chunk long scripts themselves — TTS providers cap input length — and
    concatenate the returned MP3 segments.

    input_references carries stateless voice cloning for models that support it
    (an input_audio part with the base64 voice sample, optionally a text part
    with its transcript). Cloning is per-request by design, so the caller sends
    the same references with every chunk.

    Every transient failure shape retries with backoff: transport errors, 408/425/429,
    any 5xx, and the empty-audio 200 (a JSON error envelope where audio should
    be — providers emit these mid-incident and a retry usually lands on a
    healthy one). Only a definitive 4xx fails fast.
    """
    if not (text or '').strip():
        raise OpenRouterError("Empty text passed to TTS — nothing to synthesize.", 400)
    url = f"{config.OPENROUTER_BASE_URL.rstrip('/')}/audio/speech"
    body: dict = {"model": model, "input": text, "response_format": response_format}
    if voice:
        body["voice"] = voice
    if input_references:
        body["input_references"] = input_references

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, json=body, headers=_headers(api_key),
                                 timeout=timeout)
        except requests.RequestException as e:
            last_err = e
            logging.warning(f"OpenRouter TTS {model} transport error "
                            f"(attempt {attempt + 1}): {e}")
            time.sleep(2 ** attempt)
            continue

        if resp.status_code == 200:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if resp.content and "json" not in content_type:
                return resp.content
            # A JSON body on 200 is an error envelope, not audio — treat it
            # like any other transient provider failure.
            last_err = OpenRouterError(
                f"OpenRouter returned no audio for {model}", 200, resp.text[:400])
            if attempt < retries:
                wait = 2 ** attempt * 2
                logging.warning(f"OpenRouter TTS {model} sent an error envelope "
                                f"instead of audio; retrying in {wait}s")
                time.sleep(wait)
                continue
            raise last_err

        retryable = resp.status_code in (408, 425, 429) or resp.status_code >= 500
        if retryable and attempt < retries:
            wait = 2 ** attempt * 2
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = min(15, max(wait, int(float(retry_after))))
                except ValueError:
                    pass
            logging.warning(f"OpenRouter TTS {model} HTTP {resp.status_code}; "
                            f"retrying in {wait}s")
            time.sleep(wait)
            last_err = OpenRouterError(f"HTTP {resp.status_code}", resp.status_code,
                                       resp.text[:400])
            continue

        hint = ""
        if resp.status_code in (400, 404):
            hint = (" — check that this model id is a TTS model available on "
                    "OpenRouter (the endpoint speaks the OpenAI /audio/speech "
                    "dialect), and that the voice name is one it supports")
        raise OpenRouterError(
            f"OpenRouter TTS request failed for {model}{hint}",
            resp.status_code, resp.text[:400])

    raise OpenRouterError(f"OpenRouter TTS failed for {model} after retries: {last_err}")
