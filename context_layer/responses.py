"""On-demand Responses API command, separate from retrieval and Decisions."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys

from . import responses_client


SCOPES = ("public", "synthetic")
MAX_INPUT_BYTES = responses_client.MAX_INPUT_BYTES
SENSITIVE_MARKERS = responses_client.SENSITIVE_MARKERS


class InvalidResponseInput(ValueError):
    """A fixed local input error; do not put input text or paths in it."""


def build_payload(prompt: str, model: str, max_output_tokens: int = 1024,
                  web_search: bool = False) -> dict:
    if not isinstance(prompt, str) or not prompt.strip():
        raise InvalidResponseInput("input_invalid")
    if type(web_search) is not bool:
        raise InvalidResponseInput("web_search_invalid")
    try:
        input_bytes = len(prompt.encode("utf-8"))
    except UnicodeError:
        raise InvalidResponseInput("input_invalid") from None
    if input_bytes > MAX_INPUT_BYTES:
        raise InvalidResponseInput("input_too_large")
    if any(marker.search(prompt) for marker in SENSITIVE_MARKERS):
        raise InvalidResponseInput("credential_pattern_refused")
    payload = {"model": model, "input": prompt, "store": False,
               "max_output_tokens": max_output_tokens}
    if web_search:
        payload.update({"tools": [{"type": "web_search"}], "max_tool_calls": 1,
                        "parallel_tool_calls": False})
    try:
        responses_client._request_body(payload)
    except responses_client.ResponsesError as exc:
        raise InvalidResponseInput(exc.code) from None
    return payload


def run(prompt: str, *, data_scope: str, model: str, max_output_tokens: int = 1024,
        timeout: float = 10.0, web_search: bool = False, send: bool = False) -> dict:
    if data_scope not in SCOPES:
        raise InvalidResponseInput("data_scope_refused")
    if type(send) is not bool:
        raise InvalidResponseInput("send_invalid")
    if type(timeout) not in (int, float) or not 0 < timeout <= 30:
        raise InvalidResponseInput("timeout_invalid")
    payload = build_payload(prompt, model, max_output_tokens, web_search)
    if not send:
        return {"status": "preview", "data_scope": data_scope, "model": model,
                "input_bytes": len(prompt.encode("utf-8")), "store": False,
                "max_output_tokens": max_output_tokens, "web_search": web_search,
                "max_tool_calls": 1 if web_search else 0,
                "network_call": False, "advisory_only": True}
    result = responses_client.create_response(payload, data_scope=data_scope, timeout=timeout)
    return {"status": "answered", "data_scope": data_scope, "model": result["model"],
            "text": result["text"], "citations": result["citations"],
            "tool_calls": result["tool_calls"],
            "usage": result["usage"], "advisory_only": True,
            "gate": "VERIFY_WITH_ORIGINAL_SOURCES"}


def _read_input(path_text: str) -> str:
    path = Path(path_text)
    if path.is_symlink():
        raise InvalidResponseInput("input_symlink_refused")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as source:
        details = os.fstat(source.fileno())
        if not stat.S_ISREG(details.st_mode):
            raise InvalidResponseInput("input_not_regular_file")
        if details.st_size > MAX_INPUT_BYTES:
            raise InvalidResponseInput("input_too_large")
        raw = source.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise InvalidResponseInput("input_too_large")
    try:
        return raw.decode("utf-8")
    except UnicodeError:
        raise InvalidResponseInput("input_not_utf8") from None


def cmd_run(args: argparse.Namespace) -> int:
    try:
        prompt = _read_input(args.input_file)
        result = run(prompt, data_scope=args.data_scope, model=args.model,
                     max_output_tokens=args.max_output_tokens, timeout=args.timeout,
                     web_search=args.web_search, send=args.send)
    except OSError:
        print("context-layer responses: input_read_failed", file=sys.stderr)
        return 2
    except (InvalidResponseInput, responses_client.ResponsesError) as exc:
        print(f"context-layer responses: {exc}", file=sys.stderr)
        return 2 if isinstance(exc, InvalidResponseInput) else 1
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("responses", help="Preview or send one selected public or synthetic task.")
    group = parser.add_subparsers(dest="responses_command", required=True)
    command = group.add_parser("run", help="Create a candidate answer with the Responses API.")
    command.add_argument("--input-file", required=True, help="UTF-8 task file to review before sending.")
    command.add_argument("--data-scope", required=True, choices=SCOPES,
                         help="Assert all input is public or synthetic.")
    command.add_argument("--model", required=True, help="Select a currently available model.")
    command.add_argument("--max-output-tokens", type=int, default=1024)
    command.add_argument("--timeout", type=float, default=10.0, help="At most 30 seconds.")
    command.add_argument("--web-search", action="store_true", help="Allow at most one web search tool call.")
    command.add_argument("--send", action="store_true",
                         help="Send the request using OPENAI_API_KEY. Omit for a local preview.")
    command.set_defaults(func=cmd_run, forward_to=None)
