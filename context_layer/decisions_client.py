"""Explicit, bounded transport for public or synthetic Decisions API inputs.

Importing this module performs no I/O. Call ``evaluate`` only after the caller
has applied its local source, privacy, and authorization gates. The returned
probabilities are model estimates, never source verification or approval.
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request


ENDPOINT = "https://api.openai.com/v1/decisions"
MODEL = "gpt-6-luna"
MAX_REQUEST_BYTES = 32_000
MAX_RESPONSE_BYTES = 256_000
MAX_QUESTIONS = 20
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")


class DecisionsError(ValueError):
    """Fixed error code; never includes input, URL, response, or key."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, url):
        return None


def _probability(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def _request(input_text, questions):
    if not isinstance(input_text, str) or not input_text or len(input_text) > 24_000:
        raise DecisionsError("input_invalid")
    if not isinstance(questions, list) or not 1 <= len(questions) <= MAX_QUESTIONS:
        raise DecisionsError("questions_invalid")
    names = set()
    for question in questions:
        if not isinstance(question, dict) or question.get("type") not in ("predicate", "choice", "score"):
            raise DecisionsError("question_invalid")
        name, instructions = question.get("name"), question.get("instructions")
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in names:
            raise DecisionsError("question_invalid")
        if not isinstance(instructions, str) or not 1 <= len(instructions) <= 2_000:
            raise DecisionsError("question_invalid")
        names.add(name)
        kind = question["type"]
        expected = {"type", "name", "instructions"}
        if kind == "choice":
            expected.add("choices")
            options = question.get("choices")
            label_key = "value"
        elif kind == "score":
            expected.add("levels")
            options = question.get("levels")
            label_key = "label"
        else:
            options = None
        if set(question) != expected:
            raise DecisionsError("question_invalid")
        if kind in ("choice", "score"):
            if not isinstance(options, list) or not 2 <= len(options) <= 20:
                raise DecisionsError("question_invalid")
            labels = set()
            for option in options:
                if not isinstance(option, dict) or set(option) != {label_key, "description"}:
                    raise DecisionsError("question_invalid")
                label, description = option[label_key], option["description"]
                if not isinstance(label, str) or not 1 <= len(label) <= 160 or label in labels:
                    raise DecisionsError("question_invalid")
                if not isinstance(description, str) or not 1 <= len(description) <= 500:
                    raise DecisionsError("question_invalid")
                labels.add(label)
    body = json.dumps({"model": MODEL, "input": input_text, "questions": questions},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise DecisionsError("request_too_large")
    return body


def _validate_answer(answer, question):
    if not isinstance(answer, dict) or answer.get("name") != question["name"]:
        raise DecisionsError("response_invalid")
    kind = answer.get("type")
    if kind == "refusal":
        return {"name": question["name"], "type": "refusal"}
    if kind != question["type"]:
        raise DecisionsError("response_invalid")
    result = {"name": question["name"], "type": kind}
    if kind == "predicate":
        if not _probability(answer.get("probability")):
            raise DecisionsError("response_invalid")
        result["probability"] = float(answer["probability"])
        return result
    options = question["choices"] if kind == "choice" else question["levels"]
    expected_values = [option["value"] for option in options] if kind == "choice" else list(range(len(options)))
    distribution = answer.get("probabilities")
    if not isinstance(distribution, list) or len(distribution) != len(options):
        raise DecisionsError("response_invalid")
    probabilities = {}
    for item in distribution:
        if not isinstance(item, dict) or not _probability(item.get("probability")):
            raise DecisionsError("response_invalid")
        value = item.get("value")
        if type(value) is not (str if kind == "choice" else int) or value not in expected_values or value in probabilities:
            raise DecisionsError("response_invalid")
        if kind == "score" and item.get("label") != options[value]["label"]:
            raise DecisionsError("response_invalid")
        probabilities[value] = float(item["probability"])
    if set(probabilities) != set(expected_values) or abs(sum(probabilities.values()) - 1) > 0.02:
        raise DecisionsError("response_invalid")
    if not _probability(answer.get("confidence")):
        raise DecisionsError("response_invalid")
    result["confidence"] = float(answer["confidence"])
    result["probabilities"] = probabilities
    if kind == "choice":
        if answer.get("choice") not in expected_values:
            raise DecisionsError("response_invalid")
        result["choice"] = answer["choice"]
    else:
        score = answer.get("score")
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= len(options) - 1:
            raise DecisionsError("response_invalid")
        if abs(score - sum(index * probability for index, probability in probabilities.items())) > 0.05:
            raise DecisionsError("response_invalid")
        result["score"] = float(score)
    return result


def validate_response(payload, questions):
    """Return typed answers in request order, or reject the whole response."""
    if not isinstance(payload, dict) or not isinstance(payload.get("answers"), list):
        raise DecisionsError("response_invalid")
    answers = payload["answers"]
    if len(answers) != len(questions):
        raise DecisionsError("response_invalid")
    by_name = {}
    for answer in answers:
        if not isinstance(answer, dict) or not isinstance(answer.get("name"), str) or answer["name"] in by_name:
            raise DecisionsError("response_invalid")
        by_name[answer["name"]] = answer
    if set(by_name) != {question["name"] for question in questions}:
        raise DecisionsError("response_invalid")
    return [_validate_answer(by_name[question["name"]], question) for question in questions]


def create_decision(payload: dict, *, api_key: str | None = None,
                    timeout: float = 10.0, data_scope: str = "public") -> dict:
    """Send one text-only, public or synthetic request to the fixed endpoint.

    ``data_scope`` is the caller's classification assertion, not a detector.
    The default permits public data only. Private input must be refused by the
    caller's policy before this boundary. No network occurs until this call.
    """
    if data_scope not in ("public", "synthetic"):
        raise DecisionsError("data_scope_refused")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise DecisionsError("timeout_invalid")
    if not isinstance(payload, dict) or set(payload) != {"model", "input", "questions"} or payload.get("model") != MODEL:
        raise DecisionsError("request_invalid")
    body = _request(payload["input"], payload["questions"])
    key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")
    if not key or not isinstance(key, str) or any(char.isspace() for char in key):
        raise DecisionsError("key_missing")
    request = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=float(timeout)) as response:
            if response.status != 200:
                raise DecisionsError("http_error")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            raise DecisionsError("redirect_refused") from None
        if exc.code == 429:
            raise DecisionsError("rate_limited") from None
        raise DecisionsError("http_error") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise DecisionsError("transport_error") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise DecisionsError("response_too_large")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        raise DecisionsError("response_invalid") from None
    if not isinstance(payload, dict):
        raise DecisionsError("response_invalid")
    reported_model = payload.get("model", MODEL)
    if reported_model != MODEL:
        raise DecisionsError("response_invalid")
    usage = payload.get("usage")
    if usage is not None:
        if not isinstance(usage, dict):
            raise DecisionsError("response_invalid")
        safe_usage = {}
        for field in ("input_tokens", "output_tokens", "total_tokens"):
            if field in usage:
                value = usage[field]
                if type(value) is not int or not 0 <= value <= 1_000_000_000:
                    raise DecisionsError("response_invalid")
                safe_usage[field] = value
        usage = safe_usage
    return {"model": MODEL, "answers": validate_response(payload, json.loads(body)["questions"]),
            "usage": usage}


def evaluate(input_text, questions, *, data_scope, timeout_s=5.0):
    """Typed answer convenience wrapper over the explicit request boundary."""
    return create_decision({"model": MODEL, "input": input_text, "questions": questions},
                           timeout=timeout_s, data_scope=data_scope)["answers"]
