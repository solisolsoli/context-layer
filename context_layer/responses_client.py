"""One explicit, bounded Responses API request for public or synthetic text.

This is the only Responses network surface. Importing it performs no I/O.
The caller must review the input and opt in to sending it. Model output is a
candidate answer, never source verification or permission to take an action.
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request


ENDPOINT = "https://api.openai.com/v1/responses"
MAX_INPUT_BYTES = 32_768
MAX_REQUEST_BYTES = 40_000
MAX_RESPONSE_BYTES = 256_000
MAX_ANSWER_CHARACTERS = 64_000
MAX_CITATIONS = 64
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}\Z")
SENSITIVE_MARKERS = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)OPENAI_API_KEY\s*[:=]\s*['\"]?\S+"),
)


class ResponsesError(ValueError):
    """A fixed error code that never contains input, credentials or server text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, url):
        return None


def _request_body(payload: dict) -> bytes:
    if not isinstance(payload, dict):
        raise ResponsesError("request_invalid")
    base = {"model", "input", "store", "max_output_tokens"}
    web = base | {"tools", "max_tool_calls", "parallel_tool_calls"}
    if set(payload) not in (base, web):
        raise ResponsesError("request_invalid")
    model = payload.get("model")
    if not isinstance(model, str) or not MODEL_ID.fullmatch(model):
        raise ResponsesError("model_invalid")
    value = payload.get("input")
    if not isinstance(value, str) or not value.strip():
        raise ResponsesError("input_invalid")
    try:
        encoded_input_size = len(value.encode("utf-8"))
    except UnicodeError:
        raise ResponsesError("input_invalid") from None
    if encoded_input_size > MAX_INPUT_BYTES:
        raise ResponsesError("input_invalid")
    if any(marker.search(value) for marker in SENSITIVE_MARKERS):
        raise ResponsesError("credential_pattern_refused")
    if payload.get("store") is not False:
        raise ResponsesError("request_invalid")
    budget = payload.get("max_output_tokens")
    if type(budget) is not int or not 64 <= budget <= 4096:
        raise ResponsesError("output_budget_invalid")
    if set(payload) == web and (payload["tools"] != [{"type": "web_search"}]
                                or type(payload["max_tool_calls"]) is not int
                                or payload["max_tool_calls"] != 1
                                or payload["parallel_tool_calls"] is not False):
        raise ResponsesError("request_invalid")
    try:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ResponsesError("request_invalid") from None
    if len(body) > MAX_REQUEST_BYTES:
        raise ResponsesError("request_too_large")
    return body


def _usage(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ResponsesError("response_invalid")
    safe = {}
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        if name in value:
            count = value[name]
            if type(count) is not int or not 0 <= count <= 1_000_000_000:
                raise ResponsesError("response_invalid")
            safe[name] = count
    return safe


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _citation(annotation: dict, chunk: str, offset: int) -> dict:
    if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
        raise ResponsesError("response_invalid")
    start, end = annotation.get("start_index"), annotation.get("end_index")
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(chunk):
        raise ResponsesError("response_invalid")
    url, title = annotation.get("url"), annotation.get("title")
    if (not isinstance(url, str) or not 1 <= len(url) <= 2048
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url)):
        raise ResponsesError("response_invalid")
    if (not isinstance(title, str) or not title.strip() or len(title) > 500
            or any(ord(c) < 32 or ord(c) == 127 for c in title)):
        raise ResponsesError("response_invalid")
    try:
        url.encode("utf-8")
        title.encode("utf-8")
        parsed = urllib.parse.urlsplit(url)
        valid_port = parsed.port is None or 0 < parsed.port <= 65535
    except (UnicodeError, ValueError):
        raise ResponsesError("response_invalid") from None
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or "@" in parsed.netloc or not valid_port):
        raise ResponsesError("response_invalid")
    return {"url": url, "title": title.strip(),
            "start_index": start + offset, "end_index": end + offset}


def _extract_result(response: dict, *, web_search: bool) -> dict:
    if not isinstance(response, dict) or response.get("status") != "completed":
        raise ResponsesError("response_incomplete")
    model = response.get("model")
    if not isinstance(model, str) or not MODEL_ID.fullmatch(model):
        raise ResponsesError("response_invalid")
    output = response.get("output")
    if not isinstance(output, list) or not output:
        raise ResponsesError("response_invalid")
    chunks = []
    citations = []
    answer_length = 0
    tool_calls = 0
    for item in output:
        if not isinstance(item, dict):
            raise ResponsesError("response_invalid")
        kind = item.get("type")
        if kind == "reasoning":
            continue
        if kind == "web_search_call" and web_search:
            if item.get("status") not in (None, "completed"):
                raise ResponsesError("response_invalid")
            tool_calls += 1
            if tool_calls > 1:
                raise ResponsesError("response_invalid")
            continue
        if kind != "message" or item.get("role") != "assistant":
            raise ResponsesError("response_invalid")
        if item.get("status") not in (None, "completed"):
            raise ResponsesError("response_invalid")
        content = item.get("content")
        if not isinstance(content, list) or not content:
            raise ResponsesError("response_invalid")
        for part in content:
            if isinstance(part, dict) and part.get("type") == "refusal":
                raise ResponsesError("response_refused")
            if not isinstance(part, dict) or part.get("type") != "output_text":
                raise ResponsesError("response_invalid")
            chunk = part.get("text")
            if not isinstance(chunk, str):
                raise ResponsesError("response_invalid")
            try:
                chunk.encode("utf-8")
            except UnicodeError:
                raise ResponsesError("response_invalid") from None
            annotations = part.get("annotations", [])
            if not isinstance(annotations, list) or (annotations and not web_search):
                raise ResponsesError("response_invalid")
            offset = answer_length + (1 if chunks else 0)
            for annotation in annotations:
                if len(citations) >= MAX_CITATIONS:
                    raise ResponsesError("response_invalid")
                citations.append(_citation(annotation, chunk, offset))
            chunks.append(chunk)
            answer_length = offset + len(chunk)
    answer = "\n".join(chunks)
    if not answer.strip() or len(answer) > MAX_ANSWER_CHARACTERS:
        raise ResponsesError("response_invalid")
    return {"model": model, "text": answer, "citations": citations,
            "tool_calls": tool_calls,
            "usage": _usage(response.get("usage"))}


def create_response(payload: dict, *, data_scope: str, timeout: float = 10.0) -> dict:
    """Send exactly one request to OpenAI after an explicit caller opt-in."""
    if data_scope not in ("public", "synthetic"):
        raise ResponsesError("data_scope_refused")
    if type(timeout) not in (int, float) or not 0 < timeout <= 30 or not math.isfinite(timeout):
        raise ResponsesError("timeout_invalid")
    body = _request_body(payload)
    key = os.environ.get("OPENAI_API_KEY")
    if not isinstance(key, str) or not 1 <= len(key) <= 1024 or any(not 33 <= ord(char) <= 126 for char in key):
        raise ResponsesError("key_missing")
    request = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=float(timeout)) as result:
            if result.status != 200:
                raise ResponsesError("http_error")
            raw = result.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            raise ResponsesError("redirect_refused") from None
        if exc.code == 429:
            raise ResponsesError("rate_limited") from None
        raise ResponsesError("http_error") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ResponsesError("transport_error") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ResponsesError("response_too_large")
    try:
        response = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise ResponsesError("response_invalid") from None
    return _extract_result(response, web_search="tools" in payload)
