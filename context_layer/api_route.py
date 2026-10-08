"""Offline routing suggestions for bounded Context Layer API work.

This planner never receives task text, reads a vault, loads credentials, calls an
API, starts a process, or authorizes an action. A route is only a suggestion.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

NEEDS = ("local", "relevance", "claim_support", "review_priority", "generate")
DATA_SCOPES = ("public", "synthetic", "private")
DECISIONS_TASKS = {
    "relevance": "relevance",
    "claim_support": "claim_support",
    "review_priority": "review_priority",
}
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}\Z", re.ASCII)


class RouteInputError(ValueError):
    """A routing request is outside the planner's small explicit vocabulary."""


def plan(need: str, data_scope: str, model: str | None = None) -> dict:
    """Return an offline, advisory route. No user task text is accepted here."""
    if need not in NEEDS:
        raise RouteInputError(f"need must be one of {', '.join(NEEDS)}")
    if data_scope not in DATA_SCOPES:
        raise RouteInputError(f"data_scope must be one of {', '.join(DATA_SCOPES)}")
    if need != "generate" and model is not None:
        raise RouteInputError("model applies to generate only")
    if model is not None and not isinstance(model, str):
        raise RouteInputError("model must be an ASCII model identifier")
    if model is not None and not _MODEL_ID.fullmatch(model):
        raise RouteInputError("model must match [A-Za-z0-9][A-Za-z0-9._:-]{0,99}")

    base = {
        "schema": "context-layer-api-route-v1",
        "need": need,
        "data_scope": data_scope,
        "source_status": "NOT_CHECKED",
        "source_verification_required": True,
        "advisory": True,
        "network_call": False,
        "preview_only": True,
        "send_requires_explicit_flag": False,
        "endpoint_kind": None,
        "next_cli_argv_template": None,
    }

    if need == "local":
        return {
            **base,
            "route": "local",
            "reason": "local_need_requested",
            "endpoint_kind": "local_retrieval",
            "next_cli_argv_template": [
                "context-layer", "search", "<VAULT>", "--prompt", "<QUERY>"
            ],
        }
    if data_scope == "private":
        return {**base, "route": "blocked", "reason": "private_scope_not_sent_to_provider"}
    if need in DECISIONS_TASKS:
        task = DECISIONS_TASKS[need]
        scope = data_scope
        return {
            **base,
            "route": "decisions",
            "reason": "fixed_advisory_evaluation_task",
            "endpoint_kind": "decisions_api",
            "next_cli_argv_template": [
                "context-layer", "decisions", "assess", "--task", task,
                "--data-scope", scope, "--input", "<JSON_FILE>",
            ],
            "send_requires_explicit_flag": True,
        }
    if need == "generate" and (not model or not model.strip()):
        return {**base, "route": "needs_configuration", "reason": "explicit_model_required"}
    if need == "generate":
        return {
            **base,
            "route": "responses",
            "reason": "generation_requires_explicit_model",
            "endpoint_kind": "responses_api",
            "model": model,
            "send_requires_explicit_flag": True,
            "next_cli_argv_template": [
                "context-layer", "responses", "run", "--input-file", "<INPUT_FILE>",
                "--data-scope", data_scope, "--model", model,
            ],
        }
    raise AssertionError("the need vocabulary is exhaustive")


def cmd_plan(args: argparse.Namespace) -> int:
    try:
        result = plan(args.need, args.data_scope, args.model)
    except RouteInputError as exc:
        print(f"context-layer api plan: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    """Register `api plan`; integration is owned by the CLI component table."""
    api = sub.add_parser("api", help="Preview a bounded API route without sending data.")
    api_sub = api.add_subparsers(dest="api_command", required=True)
    parser = api_sub.add_parser("plan", help="Choose a local, Decisions, or Responses route.")
    parser.add_argument("--need", choices=NEEDS, default="local")
    parser.add_argument("--data-scope", choices=DATA_SCOPES, default="private")
    parser.add_argument("--model", default=None,
                        help="Explicit whitespace-free Responses model for generation.")
    parser.set_defaults(func=cmd_plan, forward_to=None)
