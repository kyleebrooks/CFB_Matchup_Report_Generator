"""Thin OpenRouter client.

Every LLM call in this service goes through here: the eight live-web research calls
(GPT-5.6 Luna) and the single report-synthesis call (Kimi K3). OpenRouter speaks the
OpenAI Chat Completions dialect, and normalizes web-search results from every engine
into the same `message.annotations[].url_citation` shape.
"""

import json
import logging
import re
import time

import requests

import config


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
        body["max_tokens"] = max_tokens

    dropped_schema = False
    last_err: Exception | None = None

    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, json=body, headers=_headers(api_key), timeout=timeout)
        except requests.RequestException as e:
            last_err = e
            logging.warning(f"OpenRouter {model} transport error (attempt {attempt + 1}): {e}")
            time.sleep(2 ** attempt)
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
                continue
            return data

        if resp.status_code in (400, 404, 422) and response_format and not dropped_schema:
            logging.warning(
                f"OpenRouter {model} rejected response_format ({resp.status_code}); "
                f"retrying without schema. Body: {resp.text[:300]}"
            )
            body.pop("response_format", None)
            dropped_schema = True
            continue

        if resp.status_code in (408, 409, 429, 500, 502, 503, 504) and attempt < retries:
            wait = 2 ** attempt * 2
            logging.warning(f"OpenRouter {model} HTTP {resp.status_code}; retrying in {wait}s")
            time.sleep(wait)
            last_err = OpenRouterError(f"HTTP {resp.status_code}", resp.status_code, resp.text[:800])
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
           timeout: int = 300, retries: int = 2) -> bytes:
    """One text-to-speech synthesis call. Returns audio bytes (MP3).

    OpenRouter speaks the OpenAI dialect, whose TTS endpoint is /audio/speech.
    Callers chunk long scripts themselves — TTS providers cap input length — and
    concatenate the returned MP3 segments.
    """
    url = f"{config.OPENROUTER_BASE_URL.rstrip('/')}/audio/speech"
    body: dict = {"model": model, "input": text, "response_format": "mp3"}
    if voice:
        body["voice"] = voice

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
            # A JSON body on 200 is an error envelope, not audio.
            raise OpenRouterError(
                f"OpenRouter returned no audio for {model}", 200, resp.text[:400])

        if resp.status_code in (408, 429, 500, 502, 503, 504) and attempt < retries:
            wait = 2 ** attempt * 2
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
