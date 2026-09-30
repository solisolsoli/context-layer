"""context_layer.jev — the optional advisor ("Jev"): off by default, bounded, advice only.

What it is
----------
An optional component that can put one narrow, fixed question to a model
provider about evidence the local retrieval already found: "would reading this
note directly help answer the request?" In mode `shadow` the answers are only
counted and shown. In mode `on` (refused without a calibration receipt), notes
the explicit links reached but the lexical link rule left out may be appended
to the packet, as byte-exact passages of hash-verified sources, within their
own `jev_extra_tokens` budget. The design follows the optional Jev advisor of
Avenox Beyin; the code is this project's own (see docs/jev.md and CREDITS.md).

What it never does
------------------
It never writes or rewrites evidence text, never removes evidence unless the
named lossy lever `prune_fts` is set, never writes memory, never approves
anything and never changes the exit code of an existing command. Without
`.context/jev.json`, in mode `off`, or with a kill switch it makes no call,
reads no key, touches no cache and never imports the provider client.

Layout
------
This module: the configuration (the single writer of `.context/jev.json`),
modes, per-feature switches, kill switches, privacy gates and the secret scan,
freshness pins, the keyed-hash cache, the counters-only call log, `search --jev`,
the claim checks of feature `answer` (`jev answer`, `handback check --jev`, MCP
`check_claims`) and the `context-layer jev` command group. It opens no network
connection and starts no process. `jev_contracts` (question templates, answer validation) and
`jev_client` (the only module that may reach a provider) are imported lazily,
after the configuration says a call may happen.

Python 3.10+; standard library only.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import sys
import threading
import time
from urllib.parse import urlsplit

from . import graph as graphs
from .platform_support import private_tempfile, set_private_path, set_private_permissions

# ---------------------------------------------------------------------------
# Names, limits, defaults
# ---------------------------------------------------------------------------

CONTEXT_DIR = ".context"
CONFIG_NAME = "jev.json"
DISABLED_NAME = "jev.disabled"
LOG_NAME = "jev-calls.jsonl"
CACHE_NAME = "jev-cache"
SALT_NAME = "jev.salt"
CALIBRATION_NAME = "jev-calibration"
RECORDINGS_NAME = "jev-recordings"
TRACE_REL = ".context/activation.json"

SCHEMA_VERSION = 1
CONFIG_MAX_BYTES = 16 * 1024
LOG_MAX_BYTES = 512 * 1024
CACHE_ENTRY_MAX_BYTES = 16 * 1024
RECEIPT_MAX_BYTES = 256 * 1024
BLOCKLIST_MAX_BYTES = 64 * 1024
BLOCKLIST_MAX_ENTRIES = 100
BLOCKLIST_MIN_CHARS = 4
TRACE_MAX_BYTES = 4 * 1024 * 1024
SALT_BYTES = 32

KILL_ENV = "CONTEXT_LAYER_JEV_DISABLE"
CHILD_ENV = "CONTEXT_LAYER_JEV_CHILD"

MODES = ("off", "shadow", "on")
FEATURES = ("search", "auto_context", "answer", "memory")
DEFAULT_FEATURES = ("search", "answer", "memory")      # auto_context only when named
# Features whose call path exists in this version. The others can be switched on in the
# configuration, and `jev status` says that they make no call yet.
WIRED = {"search": True, "auto_context": True, "answer": True, "memory": True}
# Receipt purposes each feature needs before `on` may apply its advice.
PURPOSES = {"search": ("relevance",), "auto_context": ("relevance", "topicality"),
            "answer": ("claim_support",),
            "memory": ("memory_support", "memory_commitment", "memory_kind", "memory_relation")}

PROVIDER_KINDS = ("systemone", "openai_compat", "host_cli", "cmd", "recorded", "fake")
USER_PROVIDER_KINDS = ("systemone", "openai_compat", "host_cli", "cmd")
# The keys the provider client (jev_client) accepts for each kind. Kept in step with it
# here, without importing it, so an unusable provider block fails when the configuration
# is read instead of on every call.
PROVIDER_KEYS = {
    "systemone": ("kind", "base_url", "model", "key_env", "profile", "rounding"),
    "openai_compat": ("kind", "base_url", "model", "api_key_env", "key_env"),
    "host_cli": ("kind", "model", "max_budget_usd"),
    "cmd": ("kind", "argv", "model", "rounding"),
    "recorded": ("kind", "recording", "replays"),
    "fake": ("kind", "model", "label_only", "rounding"),
}
HOST_CLI_BUDGET_USD = (0.001, 1.0)      # max_budget_usd bounds of one host_cli call
LOOPBACK = ("127.0.0.1", "::1")

THRESHOLD_KEYS = ("gate", "keep", "rescue", "confidence")
# Inherited from upstream synthetic calibration (gate, keep, rescue) and TypeSafe's
# citation recipe (confidence); not measured in this repository (docs/jev.md).
DEFAULT_THRESHOLDS = {"gate": 0.25, "keep": 0.4, "rescue": 0.6, "confidence": 0.8}
LOSSY_KEYS = ("gate_skip", "prune_fts")

# key: (type, default, low, high, low is exclusive)
NUMBERS = {
    "timeout_s": (float, 3.0, 0.0, 10.0, True),
    "hook_timeout_s": (float, 2.0, 0.0, 5.0, True),
    "max_candidates": (int, 12, 0, 32, False),
    "max_requests": (int, 32, 1, 64, False),
    "max_parallel": (int, 4, 1, 8, False),
    "max_input_chars": (int, 24000, 1, 100000, False),
    "excerpt_chars": (int, 800, 1, 4000, False),
    "jev_extra_tokens": (int, 400, 0, 2000, False),
    "cache_ttl_s": (int, 3600, 0, 86400, False),
}
KNOWN_KEYS = ("schema_version", "mode", "features", "provider", *NUMBERS, "thresholds",
              "lossy", "local_only_prefixes", "trace", "env_file", "blocklist_file")
LOCAL_ONLY_MAX = 64

# What one search question may carry (the judged view; delivered evidence is never cut).
MIN_PROMPT_CHARS = 12
REQUEST_CHARS = 2000
STEM_CHARS = 160
LINK_LINE_CHARS = 300
RELEVANCE_TEMPLATE = "relevance.v1"   # state: request, title, link_line (may be empty), excerpt
TOPICALITY_TEMPLATE = "topicality.v1"  # state: request (the hook's gate question)
HOOK_CANDIDATES = 8                    # the hook asks about at most this many candidates
HOOK_WALL_S = 25.0                     # the advisor is done this long after the hook started
HOOK_MIN_REMAINING_S = 1.0             # less than this left: no call (skipped_deadline)
ADVICE_SCHEMA = "jev-advice/v1"
RECEIPT_SCHEMA = "jev-calibration/v1"
NOTICE = ("advisory: a model's judgement that a note is relevant, not a check that it is "
          "correct; rescued passages are verbatim source bytes")

SHA = re.compile(r"[0-9a-f]{64}")
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
KEY_ENV = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
CODE = re.compile(r"[a-z0-9_]{1,64}")
LOG_VALUE = re.compile(r"[a-z0-9_.:-]{1,64}")

LOG_FIELDS = ("v", "at", "feature", "mode", "applied", "provider_kind", "model_id", "cache_hit",
              "degraded", "code", "latency_ms", "requests", "input_tokens", "output_tokens",
              "cost_usd", "candidates", "judged", "local_only", "gate_passed", "flagged",
              "would_prune", "pruned", "would_rescue", "rescued", "would_skip", "skipped")

KIND_HELP = {
    "systemone": "a /v1/systemone endpoint over https (for example TypeSafe or OpenRouter), or "
                 "a Laya server on 127.0.0.1; needs --base-url, --model and --key-env; the "
                 "question and short excerpts go to that host",
    "openai_compat": "an OpenAI-compatible server on 127.0.0.1 or [::1] (for example Ollama, "
                     "llama.cpp, LM Studio or vLLM) with JSON-schema output; needs --base-url "
                     "and --model",
    "host_cli": "your own headless `claude -p` with a JSON schema; needs --model; no extra "
                "key; what it receives goes to that CLI's model provider",
    "cmd": "a local program named by provider.argv in .context/jev.json (set by hand); "
           "unverified",
}
SENDS = {
    "search": "the question (at most 2,000 characters) and, for each judged passage and each "
              "rescue candidate, the note's file name without its folder (at most 160 "
              "characters), the line that links to it (at most 300 characters) and the first "
              "{excerpt} characters of the passage that would be delivered; never folder "
              "paths, hashes, the vault name, keys or local-only notes",
    "auto_context": "the prompt and short views of up to 8 candidate notes on every prompt",
    "answer": "each claim, its quoted span and the enclosing section of the note (each at "
              "most 4,000 characters; a longer section is not sent and the claim is not "
              "judged), never folder paths, hashes, the vault name, keys or local-only notes",
    "memory": "the memory proposal with its quoted spans and up to 4 prior records",
}

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


class Refused(ValueError):
    """A file or an input this module will not use; the message names it without a path
    outside the vault."""


class _Degrade(Exception):
    """Advice is abandoned; the packet stays exactly as retrieval built it."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _ctx(vault) -> Path:
    return Path(vault) / CONTEXT_DIR


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _read_regular(path: Path, limit: int, label: str | None = None) -> bytes:
    """The bytes of a regular file that is not a symlink and holds at most `limit` bytes."""
    label = label or Path(path).name
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise Refused(f"{label} is a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise Refused(f"{label} is not a regular file")
    if info.st_size > limit:
        raise Refused(f"{label} is larger than {limit} bytes")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise Refused(f"{label} is larger than {limit} bytes")
    return data


def _file_digest(path: Path) -> str:
    """SHA-256 of a file's bytes, or a fixed word when it is absent or unreadable."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unreadable"


def _stem(path: str) -> str:
    return PurePosixPath(path).stem[:STEM_CHARS]


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _policy():
    """router/source_policy.py, through the same finder every entry point uses."""
    from .mcp_server import policy
    return policy()


# ---------------------------------------------------------------------------
# Secret scan (before any byte leaves; also refuses a configuration holding a key)
# ---------------------------------------------------------------------------

SECRET_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")),
    ("cloud_key_id", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)"
                                r"[A-Z0-9]{16}(?![A-Z0-9])")),
    ("cloud_api_key", re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}")),
    ("github_token", re.compile(r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{30,}"
                                r"|github_pat_[A-Za-z0-9_]{20,})")),
    ("sk_key", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}")),
    ("slack_token", re.compile(r"(?<![A-Za-z0-9])xox[abposr]-[A-Za-z0-9-]{10,}")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    # A bare JSON web token in prose: three base64url segments, the first the encoded `{"`.
    ("jwt", re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}"
                       r"\.[A-Za-z0-9_-]{4,}")),
    ("secret_assignment", re.compile(
        r"(?i)[a-z0-9_-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key"
        r"|private[_-]?key)[\"']?\s*[:=]\s*[\"']?[^\s\"'<>]{12,}")),
    ("url_userinfo", re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s@]+@[^\s/]")),
)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def secret_hit(value, literals=()) -> str | None:
    """Name of the first secret pattern (or "blocklist") found in the serialised value or
    in any raw string inside it; None when nothing matches. Serialisation escapes a newline
    as \\n, so the raw strings are scanned as well. A pattern scan cannot promise to find
    every secret; it stops the ones it recognises."""
    texts = [] if isinstance(value, str) else [_canonical(value)]
    texts.extend(_strings(value))
    for text in texts:
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                return name
        if literals:
            folded = text.casefold()
            if any(literal in folded for literal in literals):
                return "blocklist"
    return None


def load_blocklist(value) -> tuple[str, ...]:
    """Literal strings from `blocklist_file` (one per line, `#` comments), casefolded.
    Raises Refused when the file cannot be used: a blocklist that was asked for and is
    not read would widen what may be sent."""
    if not value:
        return ()
    try:
        data = _read_regular(Path(value), BLOCKLIST_MAX_BYTES, "blocklist_file")
        text = data.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise Refused(f"blocklist_file cannot be read ({type(exc).__name__})") from None
    entries = [line.strip() for line in text.splitlines()]
    entries = [line for line in entries if line and not line.startswith("#")]
    if len(entries) > BLOCKLIST_MAX_ENTRIES:
        raise Refused(f"blocklist_file holds more than {BLOCKLIST_MAX_ENTRIES} entries")
    if any(len(entry) < BLOCKLIST_MIN_CHARS for entry in entries):
        raise Refused(f"blocklist_file entries must have at least {BLOCKLIST_MIN_CHARS} "
                      "characters")
    return tuple(entry.casefold() for entry in entries)


# ---------------------------------------------------------------------------
# Configuration: .context/jev.json
# ---------------------------------------------------------------------------

@dataclass
class Config:
    present: bool = False
    valid: bool = False
    problem: str | None = None
    data: dict = field(default_factory=dict)    # every known key, defaults filled in
    raw: dict = field(default_factory=dict)     # the file's own object, kept on rewrite
    revision: str | None = None                 # bytes plus (mtime_ns, size, inode)


def defaults() -> dict:
    data = {"schema_version": SCHEMA_VERSION, "mode": "off", "features": list(DEFAULT_FEATURES),
            "provider": None}
    data.update({key: spec[1] for key, spec in NUMBERS.items()})
    data.update({"thresholds": dict(DEFAULT_THRESHOLDS),
                 "lossy": {key: False for key in LOSSY_KEYS}, "local_only_prefixes": [],
                 "trace": True, "env_file": None, "blocklist_file": None})
    return data


def endpoint_problem(url) -> str | None:
    """Why `url` cannot be a provider endpoint (None when it can): https, or plain http on
    the literal loopback addresses only; no user information, query or fragment."""
    if not isinstance(url, str) or not url or len(url) > 512 or any(c.isspace() for c in url):
        return "base_url must be a URL of at most 512 characters without spaces"
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError:
        return "base_url has an invalid port"
    if parts.scheme not in ("https", "http"):
        return "base_url must use https (plain http only on 127.0.0.1 or [::1])"
    if "@" in parts.netloc:
        return "base_url must not carry user information"
    if parts.query or parts.fragment or url.endswith("?") or url.endswith("#"):
        return "base_url must not carry a query or a fragment"
    if not parts.hostname:
        return "base_url has no host"
    if parts.scheme == "http" and parts.hostname not in LOOPBACK:
        return ("plain http is allowed only on the literal loopback addresses 127.0.0.1 "
                "and [::1] (not localhost, which a hosts file can point elsewhere)")
    return None


def _outside_file_problem(value, vault, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 1024:
        return f"{label} must be an absolute path"
    path = Path(value)
    if not path.is_absolute():
        return f"{label} must be an absolute path"
    root = Path(vault).resolve()
    resolved = path.resolve()
    if resolved == root or root in resolved.parents:
        return (f"{label} must be outside the vault (a file inside it could be indexed "
                "and delivered as evidence)")
    if os.path.lexists(path):
        if path.is_symlink():
            return f"{label} must not be a symlink"
        if not path.is_file():
            return f"{label} must be a regular file"
    return None


def _provider_problem(provider, nested: bool = False) -> str | None:
    if not isinstance(provider, dict):
        return "provider must be an object"
    kind = provider.get("kind")
    if kind not in PROVIDER_KINDS:
        return f"provider.kind must be one of {', '.join(PROVIDER_KINDS)}"
    unknown = sorted(set(provider) - set(PROVIDER_KEYS[kind]))
    if unknown:
        return f"a {kind} provider takes no {unknown[0]!r}"
    base_url, model, argv = provider.get("base_url"), provider.get("model"), provider.get("argv")
    if model is not None and (not isinstance(model, str) or not MODEL.fullmatch(model)):
        return "provider.model must be a model id (letters, digits and ._:/-)"
    for name in ("key_env", "api_key_env"):
        value = provider.get(name)
        if value is not None and (not isinstance(value, str) or not KEY_ENV.fullmatch(value)):
            return f"provider.{name} must be an environment variable name such as " \
                   "TYPESAFE_API_KEY"
    if provider.get("profile") not in (None, "laya"):
        return "provider.profile must be laya"
    if provider.get("rounding") not in (None, "2dp"):
        return "provider.rounding must be 2dp"
    if not isinstance(provider.get("label_only", False), bool):
        return "provider.label_only must be true or false"
    if kind in ("systemone", "openai_compat"):
        problem = endpoint_problem(base_url) if base_url is not None else \
            f"a {kind} provider needs --base-url"
        if problem:
            return problem
        if model is None:
            return f"a {kind} provider needs --model"
        if kind == "systemone" and not provider.get("key_env"):
            return "a systemone provider needs --key-env (the name of the variable with the key)"
        if kind == "openai_compat" and urlsplit(base_url).hostname not in LOOPBACK:
            return "an openai_compat provider must be on 127.0.0.1 or [::1] in this version"
    if kind == "host_cli":
        if model is None:
            return "a host_cli provider needs --model"
        budget = provider.get("max_budget_usd")
        low, high = HOST_CLI_BUDGET_USD
        if budget is not None and (not _number(budget) or not low <= budget <= high):
            return f"provider.max_budget_usd must be from {low} to {high}"
    if kind == "cmd" and (not isinstance(argv, list) or not 1 <= len(argv) <= 64 or not all(
            isinstance(a, str) and a and len(a) <= 4096 and "\x00" not in a for a in argv)
            or not any("{questionnaire_file}" in a for a in argv)):
        return ("a cmd provider needs provider.argv: a list naming {questionnaire_file} "
                "(set it in .context/jev.json by hand)")
    if kind == "recorded":
        if nested:
            return "a recorded provider cannot replay another recorded provider"
        recording = provider.get("recording")
        if not isinstance(recording, str) or not Path(recording).is_absolute() \
                or len(recording) > 1024:
            return "a recorded provider needs provider.recording (an absolute path)"
        problem = _provider_problem(provider.get("replays"), nested=True)
        if problem:
            return f"provider.replays: {problem}"
    return None


def validate(obj, vault) -> tuple[dict, str | None]:
    """(every known key with defaults filled in, None) or ({}, the first problem)."""
    if not isinstance(obj, dict):
        return {}, "must be a JSON object"
    unknown = sorted(set(obj) - set(KNOWN_KEYS))
    if unknown:
        return {}, f"unknown key {unknown[0]!r}"
    version = obj.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return {}, "schema_version must be the integer 1"
    if version > SCHEMA_VERSION:
        return {}, (f"schema_version {version} is newer than this context-layer reads "
                    f"({SCHEMA_VERSION}); upgrade context-layer")
    data = defaults()
    mode = obj.get("mode", "off")
    if mode not in MODES:
        return {}, "mode must be off, shadow or on"
    data["mode"] = mode
    if "features" in obj:
        features = obj["features"]
        if not isinstance(features, list) or not all(isinstance(f, str) for f in features) \
                or len(set(features)) != len(features):
            return {}, "features must be a list of distinct names"
        unknown = [f for f in features if f not in FEATURES]
        if unknown:
            return {}, f"unknown feature {unknown[0]!r} (known: {', '.join(FEATURES)})"
        data["features"] = [f for f in FEATURES if f in features]
    for key, (kind, _, low, high, exclusive) in NUMBERS.items():
        if key not in obj:
            continue
        value = obj[key]
        if kind is int and (isinstance(value, bool) or not isinstance(value, int)):
            return {}, f"{key} must be an integer"
        if not _number(value):
            return {}, f"{key} must be a finite number"
        if (value <= low if exclusive else value < low) or value > high:
            bound = "above" if exclusive else "at least"
            return {}, f"{key} must be {bound} {low} and at most {high}"
        data[key] = float(value) if kind is float else value
    if "thresholds" in obj:
        thresholds = obj["thresholds"]
        if not isinstance(thresholds, dict) or set(thresholds) - set(THRESHOLD_KEYS):
            return {}, f"thresholds may hold only {', '.join(THRESHOLD_KEYS)}"
        for key, value in thresholds.items():
            if not _number(value) or not 0 <= value <= 1:
                return {}, f"thresholds.{key} must be a number from 0 to 1"
            data["thresholds"][key] = float(value)
    if "lossy" in obj:
        lossy = obj["lossy"]
        if not isinstance(lossy, dict) or set(lossy) - set(LOSSY_KEYS) \
                or not all(isinstance(v, bool) for v in lossy.values()):
            return {}, f"lossy may hold only {', '.join(LOSSY_KEYS)} as true or false"
        data["lossy"].update(lossy)
    if "local_only_prefixes" in obj:
        prefixes = obj["local_only_prefixes"]
        if not isinstance(prefixes, list) or len(prefixes) > LOCAL_ONLY_MAX:
            return {}, f"local_only_prefixes must be a list of at most {LOCAL_ONLY_MAX} paths"
        policy = _policy()
        for prefix in prefixes:
            try:
                ok = isinstance(prefix, str) and prefix == prefix.strip() and 0 < len(prefix) \
                    <= 256 and policy.relative_name(prefix) is not None
            except ValueError:
                ok = False
            if not ok:
                return {}, f"local_only_prefixes entry {prefix!r} is not a vault-relative path"
        data["local_only_prefixes"] = list(prefixes)
    if "trace" in obj:
        if not isinstance(obj["trace"], bool):
            return {}, "trace must be true or false"
        data["trace"] = obj["trace"]
    for key in ("env_file", "blocklist_file"):
        problem = _outside_file_problem(obj.get(key), vault, key)
        if problem:
            return {}, problem
        data[key] = obj.get(key)
    provider = obj.get("provider")
    if provider is not None:
        problem = _provider_problem(provider)
        if problem:
            return {}, problem
        data["provider"] = dict(provider)
    if mode != "off" and data["provider"] is None:
        return {}, f"mode {mode} needs a provider"
    return data, None


def load_config(vault) -> Config:
    """The configuration as a person or `jev` last wrote it. Absent -> not present (off).
    Anything that cannot be trusted -> present but invalid (every path behaves as off)."""
    path = _ctx(vault) / CONFIG_NAME
    if not os.path.lexists(path):
        return Config()
    config = Config(present=True)
    try:
        if _ctx(vault).is_symlink():
            raise Refused(".context is a symlink")
        info = os.lstat(path)
        data = _read_regular(path, CONFIG_MAX_BYTES, ".context/jev.json")
        text = data.decode("utf-8")
    except (OSError, UnicodeError, Refused) as exc:
        config.problem = str(exc) if isinstance(exc, Refused) else \
            f".context/jev.json cannot be read ({type(exc).__name__})"
        return config
    config.revision = hashlib.sha256(
        data + f"|{info.st_mtime_ns}|{info.st_size}|{info.st_ino}".encode()).hexdigest()
    if secret_hit(text):
        config.problem = ("it appears to hold a credential; keys never live in this file "
                          "(name them with key_env or env_file)")
        return config
    try:
        obj = json.loads(text, object_pairs_hook=_unique_pairs)
    except ValueError as exc:
        config.problem = f"it is not valid JSON ({exc})"
        return config
    normalised, problem = validate(obj, vault)
    config.raw = obj if isinstance(obj, dict) else {}
    config.data = normalised
    config.valid = problem is None
    config.problem = problem
    return config


def write_config(vault, obj: dict) -> Path:
    """The single writer of `.context/jev.json`: validated, at most 16 KiB, never holding a
    key, written to a temp file made 0600 before any content, then os.replace. Raises
    Refused (nothing written) when the object or the target cannot be trusted."""
    _, problem = validate(obj, vault)
    if problem:
        raise Refused(problem)
    text = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
    data = text.encode("utf-8")
    if len(data) > CONFIG_MAX_BYTES:
        raise Refused(f".context/jev.json would be larger than {CONFIG_MAX_BYTES} bytes")
    if secret_hit(text):
        raise Refused("the configuration appears to hold a credential; keys never live here")
    ctx = _ctx(vault)
    if ctx.is_symlink():
        raise Refused(".context is a symlink")
    ctx.mkdir(exist_ok=True)
    target = ctx / CONFIG_NAME
    if target.is_symlink() or (os.path.lexists(target) and not target.is_file()):
        raise Refused(".context/jev.json is a symlink or not a regular file")
    handle, name = private_tempfile(ctx, prefix=".jev-", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, target)
    finally:
        if os.path.lexists(name):
            os.unlink(name)
    return target


def ensure_salt(vault) -> None:
    """The random key of the cache's keyed hash, created once by `jev shadow|on`."""
    path = _ctx(vault) / SALT_NAME
    if os.path.lexists(path):
        return
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                     0o600)
    try:
        set_private_permissions(handle, path)
        with os.fdopen(handle, "wb") as out:
            out.write(secrets.token_bytes(SALT_BYTES))
    except BaseException:
        try:
            os.close(handle)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise


def _read_salt(vault) -> bytes | None:
    try:
        salt = _read_regular(_ctx(vault) / SALT_NAME, SALT_BYTES)
    except (OSError, Refused):
        return None
    return salt if len(salt) == SALT_BYTES else None


# ---------------------------------------------------------------------------
# Modes, kill switches, calibration receipts
# ---------------------------------------------------------------------------

def _set(environ, name: str) -> bool:
    return environ.get(name, "") not in ("", "0")


def kill_switch(vault, environ=None) -> bool:
    """CONTEXT_LAYER_JEV_DISABLE (any value but empty or 0) or `.context/jev.disabled`."""
    environ = os.environ if environ is None else environ
    return _set(environ, KILL_ENV) or os.path.lexists(_ctx(vault) / DISABLED_NAME)


def effective_mode(vault, config: Config, environ=None,
                   feature: str | None = None) -> tuple[str, str | None]:
    """(mode in force, why it is off). The saved mode is never changed by this."""
    environ = os.environ if environ is None else environ
    if not config.present:
        return "off", "not_configured"
    if not config.valid:
        return "off", "config_invalid"
    if _set(environ, CHILD_ENV):
        return "off", "child_guard"
    if kill_switch(vault, environ):
        return "off", "kill_switch"
    if config.data["mode"] == "off":
        return "off", "mode_off"
    if feature is not None and feature not in config.data["features"]:
        return "off", "feature_disabled"
    return config.data["mode"], None


def receipt_path(vault, provider: dict) -> Path:
    """`.context/jev-calibration/<kind>-<model>.json`; characters outside [A-Za-z0-9._-]
    in the model id become `_`."""
    name = f"{provider.get('kind') or 'none'}-{provider.get('model') or 'none'}"
    return _ctx(vault) / CALIBRATION_NAME / (re.sub(r"[^A-Za-z0-9._-]", "_", name) + ".json")


def check_receipt(vault, provider: dict, thresholds: dict, features,
                  template_revision: str | None = None) -> tuple[dict | None, str | None]:
    """(receipt, None) when a `jev-calibration/v1` receipt for this provider shows that the
    bars were met for every enabled feature that can call in this version, with the same
    thresholds (and, when known, the same question templates); else (None, why)."""
    path = receipt_path(vault, provider)
    shown = f".context/{CALIBRATION_NAME}/{path.name}"
    if not os.path.lexists(path):
        return None, f"no calibration receipt at {shown}"
    try:
        receipt = json.loads(_read_regular(path, RECEIPT_MAX_BYTES, shown).decode("utf-8"),
                             object_pairs_hook=_unique_pairs)
    except (OSError, UnicodeError, ValueError) as exc:
        return None, f"{shown} cannot be used ({exc if isinstance(exc, Refused) else type(exc).__name__})"
    if not isinstance(receipt, dict) or receipt.get("schema") != RECEIPT_SCHEMA:
        return None, f"{shown} is not a {RECEIPT_SCHEMA} receipt"
    named = receipt.get("provider")
    if not isinstance(named, dict) or named.get("kind") != provider.get("kind") \
            or named.get("model") != provider.get("model"):
        return None, f"{shown} was made for another provider or model"
    purposes = receipt.get("purposes")
    if not isinstance(purposes, dict):
        return None, f"{shown} has no purposes"
    needed = sorted({p for f in features if WIRED.get(f) for p in PURPOSES.get(f, ())})
    for purpose in needed:
        entry = purposes.get(purpose)
        if not isinstance(entry, dict) or entry.get("passed") is not True:
            return None, f"{shown} does not show that the {purpose} bars were met"
    calibrated = receipt.get("thresholds")
    if calibrated is not None:
        if not isinstance(calibrated, dict):
            return None, f"{shown} has malformed thresholds"
        for key, value in calibrated.items():
            if key in THRESHOLD_KEYS and thresholds.get(key) != value:
                return None, (f"thresholds.{key} differs from the calibrated value in {shown}; "
                              "calibrate again after changing a threshold")
    if template_revision is not None and receipt.get("template_revision") != template_revision:
        return None, f"{shown} was made with other question templates; calibrate again"
    return receipt, None


# ---------------------------------------------------------------------------
# Privacy gates
# ---------------------------------------------------------------------------

PRIVACY_KEYS = ("remote_allowed", "sensitivity", "visibility", "jev")
SENSITIVITY_SENDABLE = ("public", "internal", "normal")
PRIVACY_MENTION = re.compile(r"(?<![a-z0-9_-])(?:remote_allowed|sensitivity|visibility|jev)"
                             r"(?![a-z0-9_-])")
KEY_LINE = re.compile(r"^([A-Za-z0-9_-]+)\s*:\s*(.*?)\s*$")
UNCERTAIN_VALUE_START = tuple(">|&*!{[%@`")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.strip()


def local_only_reason(text: str) -> str | None:
    """Why a note's frontmatter keeps it local (never sent), or None. Fail closed:
    `remote_allowed` or `jev` present with a raw value other than exactly `true`;
    `sensitivity` not public, internal or normal; `visibility: private`; a frontmatter this
    code cannot read with certainty (a byte-order mark, an unclosed block, a privacy key in
    any form other than a top-level `key: value` line)."""
    if text.startswith("\ufeff"):
        return "frontmatter_uncertain"
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    span = graphs.frontmatter_span(lines)
    if span == 0:
        return "frontmatter_uncertain"
    for raw in lines[1:span - 1]:
        if not PRIVACY_MENTION.search(raw.casefold()):
            continue
        match = KEY_LINE.match(raw)
        if match is None or match.group(1).casefold() not in PRIVACY_KEYS:
            return "frontmatter_uncertain"
        key, value = match.group(1).casefold(), match.group(2)
        # A value this reader cannot take at face value keeps the note local: a block
        # scalar (`>`, `|`), an anchor, alias, tag or flow collection, or a comment.
        if value[:1] in UNCERTAIN_VALUE_START or "#" in value:
            return "frontmatter_uncertain"
        if key in ("remote_allowed", "jev"):
            if value != "true":
                return f"{key}_not_true"
        elif key == "sensitivity":
            if _unquote(value) not in SENSITIVITY_SENDABLE:
                return "sensitivity"
        else:
            scalar = _unquote(value)
            if not scalar or scalar.startswith("[") or scalar.casefold() == "private":
                return "visibility"
    return None


def _assess(vault, name: str, sha: str, prefixes, local_prefixes, policy) -> str | None:
    """Local-only reason for one pinned source, or None. The file is read through the
    boundary check and must still hash to the pin; otherwise advice is abandoned."""
    if local_prefixes:
        try:
            if policy.excluded(name, local_prefixes):
                return "local_only_prefix"
        except ValueError:
            return "local_only_prefix"
    try:
        raw = policy.source_path(vault, name, prefixes).read_bytes()
    except (OSError, ValueError):
        raise _Degrade("source_changed") from None
    if hashlib.sha256(raw).hexdigest() != sha:
        raise _Degrade("source_changed")
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return "frontmatter_uncertain"
    return local_only_reason(text)


# ---------------------------------------------------------------------------
# Cache (keyed hash names; answers only)
# ---------------------------------------------------------------------------

def _normal_endpoint(url) -> str | None:
    if not isinstance(url, str):
        return None
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


def _normal_answer(answer) -> dict | None:
    """A noul answer reduced to {type, label, p_yes}, or a choice answer through
    `_normal_choice`; None when it is neither."""
    if not isinstance(answer, dict):
        return None
    if answer.get("type") == "choice":
        return _normal_choice(answer)
    label, p_yes = answer.get("label"), answer.get("p_yes")
    if label not in ("yes", "no"):
        return None
    if p_yes is not None and (not _number(p_yes) or not 0 <= p_yes <= 1):
        return None
    return {"type": "noul", "label": label, "p_yes": None if p_yes is None else float(p_yes)}


def _model_of(value) -> str | None:
    return value if isinstance(value, str) and MODEL.fullmatch(value) else None


class _Cache:
    """`.context/jev-cache/<HMAC-SHA256>.json` (0700 folder, 0600 files, symlinks refused).
    File names are keyed with the random salt in `.context/jev.salt`, so a file listing
    does not let anyone confirm a guessed question. Entries hold validated answers and
    counters, never the question, the excerpts or a key."""

    def __init__(self, directory: Path, salt: bytes, ttl: int):
        self.directory, self.salt, self.ttl = directory, salt, ttl

    @classmethod
    def open(cls, vault, ttl: int):
        if ttl <= 0:
            return None
        salt = _read_salt(vault)
        if salt is None or _ctx(vault).is_symlink():
            return None
        directory = _ctx(vault) / CACHE_NAME
        try:
            if os.path.lexists(directory):
                if not stat.S_ISDIR(os.lstat(directory).st_mode):
                    return None
            else:
                os.mkdir(directory, 0o700)
            set_private_path(directory, directory=True)
        except OSError:
            return None
        return cls(directory, salt, ttl)

    def key(self, questionnaire, revision, provider: dict, pins: dict) -> str:
        material = {"v": 1, "feature": "search", "questionnaire": questionnaire,
                    "template_revision": revision, "provider": provider,
                    "endpoint": _normal_endpoint(provider.get("base_url")), "pins": pins}
        return hmac.new(self.salt, _canonical(material).encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def get(self, key: str) -> dict | None:
        try:
            entry = json.loads(_read_regular(self.directory / f"{key}.json",
                                             CACHE_ENTRY_MAX_BYTES).decode("utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None
        if not isinstance(entry, dict) or entry.get("v") != 1:
            return None
        created, now = entry.get("created_at"), time.time()
        if not _number(created) or created > now + 60 or now - created > self.ttl:
            return None
        answer = _normal_answer(entry.get("answer"))
        if answer is None:
            return None
        return {"answer": answer, "model": _model_of(entry.get("model_reported"))}

    def put(self, key: str, answer: dict, model: str | None, usage: dict) -> None:
        entry = {"v": 1, "created_at": int(time.time()), "answer": answer,
                 "model_reported": model, "usage": usage}
        try:
            handle, name = private_tempfile(self.directory, prefix=".entry-", suffix=".tmp")
        except OSError:
            return
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(_canonical(entry))
            os.replace(name, self.directory / f"{key}.json")
        except OSError:
            pass
        finally:
            if os.path.lexists(name):
                os.unlink(name)


# ---------------------------------------------------------------------------
# Call log (counters only) and `jev report`
# ---------------------------------------------------------------------------

def _log_value(value):
    """null, a boolean, a finite number in [0, 1e12] or a short lowercase code; anything
    else becomes null, so no free text can enter the log."""
    if value is None or isinstance(value, bool):
        return value
    if _number(value):
        return value if 0 <= value <= 1e12 else None
    if isinstance(value, str) and LOG_VALUE.fullmatch(value):
        return value
    return None


def _halve_log(path: Path) -> None:
    """Keep the newest whole rows that fit in half the limit; the older rest is dropped.
    Reads only the tail, so a log that grew past any size still recovers."""
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        return
    keep = LOG_MAX_BYTES // 2
    handle = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(handle, "rb") as stream:
        if info.st_size > keep:
            stream.seek(info.st_size - keep)
            tail = stream.read(keep)
            cut = tail.find(b"\n")
            kept = tail[cut + 1:] if cut >= 0 else b""
        else:
            kept = stream.read()
    handle, name = private_tempfile(path.parent, prefix=".jev-calls-", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(kept)
        os.replace(name, path)
    finally:
        if os.path.lexists(name):
            os.unlink(name)


def append_log(vault, row: dict) -> None:
    """One counters-only row in `.context/jev-calls.jsonl` (0600, at most 512 KiB; the older
    half is dropped when full). Never raises: a log problem never costs a search."""
    try:
        ctx = _ctx(vault)
        if ctx.is_symlink() or not ctx.is_dir():
            return
        path = ctx / LOG_NAME
        clean = {key: _log_value(row.get(key)) for key in LOG_FIELDS}
        clean["v"] = 1
        line = (json.dumps(clean, separators=(",", ":")) + "\n").encode("utf-8")
        if os.path.lexists(path):
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                return
            if info.st_size + len(line) > LOG_MAX_BYTES:
                _halve_log(path)
        handle = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            set_private_permissions(handle, path)
            os.write(handle, line)
        finally:
            os.close(handle)
    except (OSError, Refused):
        return


def _row(feature: str, mode: str, **values) -> dict:
    row = {"v": 1, "at": int(time.time()), "feature": feature, "mode": mode}
    row.update(values)
    return row


def read_log(vault) -> list[dict]:
    """Every well-formed row; Refused when the log exists but cannot be read."""
    path = _ctx(vault) / LOG_NAME
    if not os.path.lexists(path):
        return []
    try:
        data = _read_regular(path, LOG_MAX_BYTES * 2, ".context/jev-calls.jsonl")
    except (OSError, Refused) as exc:
        raise Refused(str(exc) if isinstance(exc, Refused) else
                      f".context/{LOG_NAME} cannot be read ({type(exc).__name__})") from None
    rows = []
    for line in data.decode("utf-8", "replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("v") == 1 and _number(row.get("at")):
            # The same restrictions as on the way in: a hand-edited row cannot carry text
            # into a report.
            rows.append({key: _log_value(row.get(key)) for key in LOG_FIELDS})
    return rows


def _percentile(values: list, share: float):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


def report(vault, days: int = 7) -> dict:
    """Per feature over the last `days`: calls, share applied, degraded calls by code, cache
    hits, p50/p95 latency, tokens and cost where a provider reported them, and the shadow
    change rate: the share of successful shadow calls in which `on` would have rescued at
    least one note. Counters only: no path, no text."""
    since = time.time() - days * 86400
    rows = [r for r in read_log(vault) if r["at"] >= since]
    features: dict[str, dict] = {}
    for feature in sorted({r.get("feature") or "unknown" for r in rows}):
        mine = [r for r in rows if (r.get("feature") or "unknown") == feature]
        calls = [r for r in mine if r.get("mode") in ("shadow", "on")]
        refused = [r for r in mine if r.get("mode") not in ("shadow", "on")]
        degraded: dict[str, int] = {}
        for r in calls:
            if r.get("degraded"):
                code = r.get("code") or "unknown"
                degraded[code] = degraded.get(code, 0) + 1
        shadow = [r for r in calls if r.get("mode") == "shadow" and not r.get("degraded")]
        latency = [r["latency_ms"] for r in calls if _number(r.get("latency_ms"))]
        cost = [r["cost_usd"] for r in calls if _number(r.get("cost_usd"))]

        def total(key):
            return sum(r[key] for r in calls if _number(r.get(key)))

        refusals: dict[str, int] = {}
        for r in refused:
            code = r.get("code") or "unknown"
            refusals[code] = refusals.get(code, 0) + 1
        features[feature] = {
            "calls": len(calls),
            "applied_share": round(sum(1 for r in calls if r.get("applied")) / len(calls), 4)
            if calls else None,
            "degraded": degraded,
            "cache_hit_share": round(sum(1 for r in calls if r.get("cache_hit")) / len(calls), 4)
            if calls else None,
            "latency_ms_p50": _percentile(latency, 0.5),
            "latency_ms_p95": _percentile(latency, 0.95),
            "requests": total("requests"), "input_tokens": total("input_tokens"),
            "output_tokens": total("output_tokens"),
            "cost_usd": round(sum(cost), 6) if cost else None,
            "shadow_calls": len(shadow),
            "shadow_change_rate": round(sum(1 for r in shadow if (r.get("would_rescue") or 0) > 0)
                                        / len(shadow), 4) if shadow else None,
            "shadow_would_prune_rate": round(sum(1 for r in shadow
                                                 if (r.get("would_prune") or 0) > 0)
                                             / len(shadow), 4) if shadow else None,
            "rescued": total("rescued"), "pruned": total("pruned"),
            "not_called": refusals,
        }
    return {"schema": "jev-report/v1", "days": days, "rows": len(rows), "features": features}


def render_report(info: dict) -> str:
    lines = [f"advisor calls in the last {info['days']} day(s): {info['rows']} row(s)"]
    if not info["features"]:
        lines.append("nothing recorded (the call log is absent or empty)")
    for feature, stats in info["features"].items():
        lines.append(f"{feature}: {stats['calls']} call(s)")
        for key in ("applied_share", "cache_hit_share", "latency_ms_p50", "latency_ms_p95",
                    "requests", "input_tokens", "output_tokens", "cost_usd", "shadow_calls",
                    "shadow_change_rate", "shadow_would_prune_rate", "rescued", "pruned"):
            value = stats[key]
            lines.append(f"  {key}: {'n/a' if value is None else value}")
        for label, key in (("degraded", "degraded"), ("not called", "not_called")):
            if stats[key]:
                counts = ", ".join(f"{code} {count}" for code, count in sorted(stats[key].items()))
                lines.append(f"  {label}: {counts}")
    lines.append("shadow_change_rate: share of successful shadow calls in which `on` would "
                 "have rescued at least one note (before the jev_extra_tokens budget)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def _needs_key(provider: dict) -> bool:
    return provider.get("kind") == "systemone" or (
        provider.get("kind") == "openai_compat"
        and bool(provider.get("key_env") or provider.get("api_key_env")))


def _template_revision() -> str | None:
    try:
        from . import jev_contracts
    except ImportError:
        return None
    revision = getattr(jev_contracts, "TEMPLATE_REVISION", None)
    return revision if isinstance(revision, str) else None


PROBE_TIMEOUT_S = 1.0


def _probe_status(vault, provider: dict, mode: str, endpoint_issue, environ) -> dict:
    """`jev status --check`: whether the configured provider is there. Only a provider on a
    literal loopback address is contacted (a bodyless GET /health or /v1/models) or, for
    host_cli, `claude --version`; no question, note text or key is sent. Nothing is sent
    when the advisor is off, a kill switch is set, or the provider is remote, recorded or
    fake. The provider client is imported only here, only when a check was asked for."""
    def none(code: str, detail: str) -> dict:
        return {"checked": False, "ok": None, "code": code, "detail": detail}

    if not provider:
        return none("no_provider", "no provider is configured")
    if endpoint_issue:
        return none("endpoint_invalid", "the endpoint is not acceptable; nothing was sent")
    if mode == "off":
        return none("mode_off", "not probed while the advisor is off (a kill switch, the "
                                "child guard or mode off); `jev shadow` first")
    try:
        from . import jev_client
    except ImportError:
        return none("advisor_unavailable", "the provider client is not part of this build")
    try:
        return dict(jev_client.probe(provider, timeout_s=PROBE_TIMEOUT_S))
    except Exception:                                # a check must never fail status
        return none("probe_failed", "the check could not run")


def _render_probe(result: dict) -> str:
    detail = f"{result.get('code')}: {result.get('detail')}"
    if not result.get("checked"):
        if result.get("ok") is None:
            return f"not sent ({detail})"
        return f"{'ok' if result['ok'] else 'failed'} ({detail})"
    tail = f"; {result.get('requests')} request(s), {result.get('latency_ms')} ms"
    version = f"; {result['version']}" if result.get("version") else ""
    return f"{'ok' if result.get('ok') else 'failed'} ({detail}{version}{tail})"


def status(vault, check: bool = False, environ=None) -> dict:
    """What the advisor would do in this vault, from its files only. A provider key is
    looked up (never shown) only when the mode in force is not off."""
    vault = Path(vault).resolve()
    environ = os.environ if environ is None else environ
    config = load_config(vault)
    mode, why = effective_mode(vault, config, environ)
    info: dict = {"schema": "jev-status/v1", "configured": config.present,
                  "valid": config.valid if config.present else None,
                  "problem": config.problem, "mode": mode,
                  "saved_mode": config.data.get("mode") if config.valid else None,
                  "off_because": why if mode == "off" else None,
                  "kill_switch": kill_switch(vault, environ),
                  "child_guard": _set(environ, CHILD_ENV), "notes": []}
    if not config.valid:
        info.update({"automatic_model_calls": False, "provider_kinds": dict(KIND_HELP)})
        return info
    data = config.data
    provider = data["provider"] or {}
    features = {}
    for name in FEATURES:
        enabled = name in data["features"]
        sends = SENDS[name].format(excerpt=data["excerpt_chars"])
        features[name] = {"enabled": enabled, "available_in_this_version": WIRED[name],
                          "sends": sends if WIRED[name] else
                          "nothing: not available in this version, so no call is made"}
    info.update({
        "automatic_model_calls": mode != "off" and "auto_context" in data["features"]
        and WIRED["auto_context"],
        "superset": not data["lossy"]["prune_fts"],
        "lossy": dict(data["lossy"]),
        "provider": {k: provider.get(k) for k in ("kind", "base_url", "model", "key_env")
                     if provider.get(k) is not None},
        "key_present": None, "features": features,
        "local_only_prefixes": list(data["local_only_prefixes"]),
        "thresholds": dict(data["thresholds"]), "jev_extra_tokens": data["jev_extra_tokens"],
        "files": sorted(p for p in (CONFIG_NAME, DISABLED_NAME, LOG_NAME, CACHE_NAME, SALT_NAME,
                                    CALIBRATION_NAME, RECORDINGS_NAME)
                        if os.path.lexists(_ctx(vault) / p)),
    })
    if provider:
        receipt, problem = check_receipt(vault, provider, data["thresholds"], data["features"],
                                         _template_revision())
        shown = f".context/{CALIBRATION_NAME}/{receipt_path(vault, provider).name}"
        info["receipt"] = {"path": shown, "usable": receipt is not None, "problem": problem}
    if mode != "off" and _needs_key(provider):
        try:
            from . import jev_client
        except ImportError:
            info["notes"].append("the provider client is not part of this build, so no call "
                                 "can be made")
        else:
            try:
                info["key_present"] = jev_client.load_key(
                    provider, data["env_file"], vault=vault) is not None
            except Exception:                       # never let status fail on a key lookup
                info["key_present"] = False
    if check:
        problem = endpoint_problem(provider["base_url"]) if provider.get("base_url") else None
        info["check"] = {"endpoint": "not applicable" if not provider.get("base_url") else
                         (problem or "accepted"),
                         "probe": _probe_status(vault, provider, mode, problem, environ)}
    return info


def render_status(info: dict) -> str:
    lines = []
    if not info["configured"]:
        lines += ["advisor (jev): off; not configured (.context/jev.json is absent)",
                  "nothing is sent, no key is read and the provider client is not loaded",
                  "automatic_model_calls: no",
                  "to try it in shadow mode (answers are only counted and shown):",
                  "  context-layer jev shadow <vault> --provider-kind KIND [--base-url URL] "
                  "[--model ID] [--key-env NAME]",
                  "provider kinds (docs/jev.md):"]
        lines += [f"  {kind:<14} {KIND_HELP[kind]}" for kind in USER_PROVIDER_KINDS]
        return "\n".join(lines)
    if not info["valid"]:
        lines += [f"advisor (jev): off; .context/jev.json is invalid: {info['problem']}",
                  "every advisor path behaves as off until the file is fixed or removed; it "
                  "was not changed",
                  "automatic_model_calls: no"]
        return "\n".join(lines)
    extra = []
    if info["kill_switch"]:
        extra.append("kill switch set")
    if info["child_guard"]:
        extra.append("inside an advisor call")
    detail = f"saved mode: {info['saved_mode']}" + (f"; {'; '.join(extra)}" if extra else "")
    lines.append(f"advisor (jev): {info['mode']} ({detail})")
    lines.append(f"automatic_model_calls: {'yes' if info['automatic_model_calls'] else 'no'}")
    lines.append("superset: " + ("yes" if info["superset"] else
                                 "no (prune_fts is set: advice may remove fts evidence)"))
    provider = info["provider"]
    if provider:
        shown = ", ".join(f"{k} {v}" for k, v in provider.items())
        lines.append(f"provider: {shown} (no key is stored in .context/jev.json)")
    else:
        lines.append("provider: none")
    key = info["key_present"]
    lines.append("key_present: " + ("not looked up (mode off or no key needed)" if key is None
                                    else "yes" if key else "no"))
    for name, feature in info["features"].items():
        state = "enabled" if feature["enabled"] else "disabled"
        lines.append(f"{name}: {state}; sends {feature['sends']}")
    if info["local_only_prefixes"]:
        lines.append("local only (never sent): " + ", ".join(info["local_only_prefixes"]))
    receipt = info.get("receipt")
    if receipt:
        lines.append(f"calibration receipt: {receipt['path']} "
                     + ("(usable; a plain local file: it records that calibration was run, "
                        "it does not prove it)" if receipt["usable"] else
                        f"(not usable: {receipt['problem']}; `jev on` is refused)"))
    for note in info["notes"]:
        lines.append(f"note: {note}")
    if "check" in info:
        lines.append(f"check: endpoint {info['check']['endpoint']}; "
                     f"probe {_render_probe(info['check']['probe'])}")
    lines.append("files: " + (", ".join(f".context/{name}" for name in info["files"]) or "none"))
    return "\n".join(lines)


def status_line(vault) -> str:
    """One line for `context-layer status`, read from the advisor's files only."""
    try:
        vault = Path(vault).resolve()
        config = load_config(vault)
        mode, why = effective_mode(vault, config)
        if not config.present:
            detail = "not configured"
        elif not config.valid:
            detail = ".context/jev.json is invalid"
        elif why in ("kill_switch", "child_guard"):
            detail = f"saved mode {config.data['mode']}, {why.replace('_', ' ')}"
        else:
            detail = "saved mode " + config.data["mode"]
        auto = mode != "off" and config.valid and "auto_context" in config.data["features"] \
            and WIRED["auto_context"]
        return f"jev: {mode} ({detail}); automatic_model_calls: {'yes' if auto else 'no'}"
    except Exception:                                 # status must never fail on this line
        return "jev: unknown (the advisor's files could not be read); automatic_model_calls: no"


# ---------------------------------------------------------------------------
# search --jev (feature `search`)
# ---------------------------------------------------------------------------

@dataclass
class SearchPlan:
    vault: Path
    mode: str
    config: Config
    method: str
    routes_sha: str
    revision: str | None
    candidates: int

    def retrieve_args(self) -> list[str]:
        return ["--jev-candidates", str(self.candidates)]


def _say(message: str) -> None:
    print(f"context-layer search: --jev: {message}", file=sys.stderr)


def search_plan(vault, method: str, rest=(), environ=None, quiet: bool = False) -> SearchPlan | None:
    """None means: run today's search, byte for byte (the reason goes to stderr unless
    `quiet`, as in the MCP server). With a usable configuration in mode shadow or on, the
    plan names the side channel to ask the retriever for. Reads the configuration only;
    never imports the provider client."""
    vault = Path(vault).resolve()
    config = load_config(vault)
    mode, why = effective_mode(vault, config, environ, feature="search")
    say = (lambda message: None) if quiet else _say
    if mode == "off":
        messages = {
            "not_configured": "the optional advisor is not configured (docs/jev.md)",
            "config_invalid": f".context/jev.json is invalid ({config.problem})",
            "mode_off": "the advisor is off (see `context-layer jev status`)",
            "kill_switch": "a kill switch is set",
            "child_guard": "running inside an advisor call",
            "feature_disabled": "the search feature is disabled",
        }
        say(f"{messages.get(why, why)}; this is the packet without the advisor")
        if why in ("kill_switch", "child_guard", "feature_disabled"):
            append_log(vault, _row("search", "off", code=why, applied=False, degraded=False,
                                   provider_kind=(config.data.get("provider") or {}).get("kind")))
        return None
    if method not in ("fts", "synaptic") or "--compact" in rest:
        say("the advisor supports --method fts and the default synaptic mode only; this is "
            "the packet without it")
        return None
    return SearchPlan(vault, mode, config, method,
                      _file_digest(_ctx(vault) / "routes.json"), config.revision,
                      config.data["max_candidates"])


@dataclass
class _Question:
    kind: str                       # "item" (delivered), "candidate" (not delivered), "gate"
    index: int
    paths: dict                     # path -> sha256 of every source this view came from
    template: str = RELEVANCE_TEMPLATE
    state: dict | None = None
    questionnaire: dict | None = None
    key: str | None = None
    answer: dict | None = None
    model: str | None = None
    cached: bool = False
    verdict: str = "not_judged"
    value: float | None = None
    usage: dict = field(default_factory=dict)


def _is_path_sha(path, sha) -> bool:
    return isinstance(path, str) and bool(path) and isinstance(sha, str) \
        and SHA.fullmatch(sha) is not None


def _passage_ok(p) -> bool:
    return isinstance(p, dict) and _is_path_sha(p.get("source_path"), p.get("source_sha256")) \
        and isinstance(p.get("content"), str) and bool(p.get("content")) \
        and all(isinstance(p.get(k), int) and not isinstance(p.get(k), bool)
                for k in ("start", "end")) and 0 <= p["start"] < p["end"]


def _candidate_ok(c) -> bool:
    if not isinstance(c, dict) or c.get("kind") not in ("link", "bm25_tail"):
        return False
    path, sha, passages = c.get("source_path"), c.get("source_sha256"), c.get("passages")
    if not _is_path_sha(path, sha) or not isinstance(passages, list) or not passages:
        return False
    if not all(_passage_ok(p) and p["source_path"] == path and p["source_sha256"] == sha
               for p in passages):
        return False
    link = c.get("link_line")
    return link is None or _passage_ok(link)


def _answer_of(result) -> tuple[dict | None, str | None]:
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        return None, "answer_invalid"
    if not result["ok"]:
        code = result.get("code")
        return None, code if isinstance(code, str) and CODE.fullmatch(code) else "provider_error"
    answer = _normal_answer(result.get("answer"))
    return (answer, None) if answer is not None else (None, "answer_invalid")


def _usage_of(result) -> dict:
    usage = result.get("usage") if isinstance(result, dict) else None
    out = {}
    if isinstance(usage, dict):
        for key in ("input_tokens", "output_tokens"):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**9:
                out[key] = value
        cost = usage.get("cost_usd", usage.get("total_cost_usd"))
        if _number(cost) and 0 <= cost <= 1000:
            out["cost_usd"] = float(cost)
    requests = result.get("requests") if isinstance(result, dict) else None
    if isinstance(requests, int) and not isinstance(requests, bool) and 0 <= requests <= 1000:
        out["requests"] = requests
    return out


def _call_with_deadline(function, args: tuple, kwargs: dict, timeout: float):
    """Run the provider call in a daemon thread and wait at most `timeout` plus a small
    grace; a call that is still running is abandoned (never retried)."""
    box: dict = {}

    def run():
        try:
            box["value"] = function(*args, **kwargs)
        except Exception:
            box["error"] = True
        except BaseException as exc:      # re-raised in the caller (a test's network guard)
            box["base"] = exc

    worker = threading.Thread(target=run, name="jev-evaluate", daemon=True)
    worker.start()
    worker.join(timeout + min(1.0, 0.2 * timeout) + 0.05)
    if worker.is_alive():
        raise _Degrade("deadline_exceeded")
    if "base" in box:
        raise box["base"]
    if "error" in box:
        raise _Degrade("provider_error")
    return box.get("value")


def _spans(evidence: list[dict]) -> dict[str, list[tuple[int, int]]]:
    spans: dict[str, list[tuple[int, int]]] = {}
    for item in evidence:
        path, content = item.get("source_path"), item.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            continue
        start, end = item.get("start"), item.get("end")
        if not (isinstance(start, int) and isinstance(end, int)):
            start, end = 0, len(content.encode("utf-8"))      # an fts prefix
        spans.setdefault(path, []).append((start, end))
    return spans


class _SearchAdvice:
    """One advisor call on a packet: gates, questions, provider call, freshness, decision.
    Feature `search` (`search --jev`) judges items and candidates; feature `auto_context`
    (the prompt hook) asks a topicality gate question first and rescues only when the gate
    passed, within a deadline the hook hands over."""

    def __init__(self, vault: Path, prompt: str, method: str, packet: dict, plan: SearchPlan,
                 evaluate_fn, load_key_fn, contracts, environ, feature: str = "search",
                 deadline_s: float | None = None):
        self.vault, self.method, self.plan = vault, method, plan
        self.feature, self.deadline_s = feature, deadline_s
        self.gate_passed: bool | None = None
        self.gate_value: float | None = None
        self.skip = False
        self.prompt = prompt if isinstance(prompt, str) else ""
        self.packet = packet
        self.out = {k: v for k, v in packet.items() if k != "jev_candidates"}
        side = packet.get("jev_candidates")
        self.side = side if isinstance(side, dict) else {}
        evidence = packet.get("evidence")
        self.evidence = list(evidence) if isinstance(evidence, list) else []
        self.candidates = [c for c in (self.side.get("items") or []) if _candidate_ok(c)]
        self.cfg = plan.config.data
        self.provider = dict(self.cfg.get("provider") or {})
        self.env = os.environ if environ is None else environ
        self.evaluate_fn, self.load_key_fn, self.contracts = evaluate_fn, load_key_fn, contracts
        self.mode = plan.mode
        self.codes: list[str] = []
        self.discarded: str | None = None
        self.partial = False
        self.questions: list[_Question] = []
        self.raw: dict[str, bytes] = {}
        self.rescued: list[int] = []
        self.pruned: list[int] = []
        self.budget_skipped = 0
        self.extra_tokens = 0
        self.model: str | None = None
        self.usage = {"input_tokens": 0, "output_tokens": 0, "requests": 0}
        self.cost: float | None = None
        self.started = time.monotonic()

    # -- pipeline ------------------------------------------------------------
    def run(self) -> dict:
        try:
            self._advise()
        except _Degrade as exc:
            self.discarded = exc.code
        except Exception:                      # advice must never break a search
            self.discarded = "internal_error"
        if self.discarded:
            for key in ("evidence", "status"):
                if key in self.packet:
                    self.out[key] = self.packet[key]
            for question in self.questions:
                if question.verdict != "local_only":
                    question.verdict, question.value, question.answer = "not_judged", None, None
            self.rescued, self.pruned, self.extra_tokens = [], [], 0
            self.gate_passed, self.skip = None, False       # a failure never withholds context
        block = self._block()
        self.out["jev"] = block
        append_log(self.vault, self._log_row(block))
        try:
            self._annotate_trace(block)
        except Exception:
            pass
        return self.out

    def _note(self, code: str) -> None:
        if code not in self.codes:
            self.codes.append(code)

    def _load_contracts(self):
        if self.contracts is None:
            try:
                from . import jev_contracts
            except ImportError:
                raise _Degrade("advisor_unavailable") from None
            self.contracts = jev_contracts
        return self.contracts

    def _load_client(self):
        if self.evaluate_fn is None:
            try:
                from . import jev_client
            except ImportError:
                raise _Degrade("advisor_unavailable") from None
            self.evaluate_fn = jev_client.evaluate
            if self.load_key_fn is None:
                self.load_key_fn = jev_client.load_key
        return self.evaluate_fn, self.load_key_fn

    def _advise(self) -> None:
        """The gates run in the order of docs/jev.md before any byte could leave:
        boundaries and exclusions, local_only_prefixes, the frontmatter convention, the
        secret scan, the prompt length, the request size (then the request cap)."""
        cfg = self.cfg
        if not self.prompt.strip():
            raise _Degrade("prompt_too_short")
        contracts = self._load_contracts()
        revision = getattr(contracts, "TEMPLATE_REVISION", None)
        if self.mode == "on":
            _, problem = check_receipt(self.vault, self.provider, cfg["thresholds"],
                                       [self.feature],
                                       revision if isinstance(revision, str) else "missing")
            if problem:
                self.mode = "shadow"                     # judged, counted, never applied
                self._note("calibration_required")
        policy = _policy()
        try:
            prefixes = tuple(policy.load_exclusions(self.vault))
        except ValueError:
            raise _Degrade("config_changed") from None
        self._build_questions(policy, prefixes)          # gates 1-3
        pending = [q for q in self.questions if q.state is not None]
        for question in pending:
            try:
                question.questionnaire = contracts.build_questionnaire(
                    self.feature, question.template, question.state)
            except Exception:
                raise _Degrade("contract_error") from None
            if not isinstance(question.questionnaire, dict):
                raise _Degrade("contract_error")
        if pending:                                      # gate 4: nothing leaves with a secret
            try:
                literals = load_blocklist(cfg["blocklist_file"])
            except Refused:
                raise _Degrade("blocklist_unreadable") from None
            # A question whose view holds a credential-shaped string is not asked; the
            # others still are. The prompt sits in every question, so a secret in the
            # prompt drops them all and no call is made.
            held = [q for q in pending if secret_hit(q.questionnaire, literals)]
            for question in held:
                question.questionnaire = None
                question.state = None
            if held:
                self._note("sensitive_input")
                if len(held) == len(pending):
                    raise _Degrade("sensitive_input")
            pending = [q for q in pending if q.questionnaire is not None]
        if len(self.prompt.strip()) < MIN_PROMPT_CHARS:  # gate 5
            raise _Degrade("prompt_too_short")
        for question in pending:                         # gate 6: the view is bounded, never cut
            if len(_canonical(question.questionnaire)) > cfg["max_input_chars"]:
                question.questionnaire = None
                self._note("budget_exceeded")
        ask = [q for q in pending if q.questionnaire is not None]
        for question in ask[cfg["max_requests"]:]:
            question.questionnaire = None
            self._note("request_cap")
        ask = ask[:cfg["max_requests"]]
        if not ask:
            return
        cache = _Cache.open(self.vault, cfg["cache_ttl_s"])
        if cache is not None:
            for question in ask:
                question.key = cache.key(question.questionnaire, revision, self.provider,
                                         question.paths)
                hit = cache.get(question.key)
                if hit is not None:
                    question.answer, question.model, question.cached = \
                        hit["answer"], hit["model"], True
        misses = [q for q in ask if not q.cached]
        if misses:
            self._evaluate(misses)
        self._fresh_after(policy)
        if cache is not None:
            for question in misses:
                if question.answer is not None and question.key:
                    cache.put(question.key, question.answer, question.model, question.usage)
        self._decide()
        if self.mode == "on":
            self._apply()

    def _build_questions(self, policy, prefixes) -> None:
        cfg = self.cfg
        local = tuple(cfg["local_only_prefixes"])
        seen: dict[tuple[str, str], str | None] = {}

        def private(path, sha):
            if (path, sha) not in seen:
                seen[(path, sha)] = _assess(self.vault, path, sha, prefixes, local, policy)
            return seen[(path, sha)]

        request = self.prompt[:REQUEST_CHARS]
        excerpt = cfg["excerpt_chars"]
        if self.feature == "auto_context":              # the gate question goes first
            gate = _Question("gate", -1, {}, TOPICALITY_TEMPLATE)
            gate.state = {"request": request} if request.strip() else None
            self.questions.append(gate)
        for index, item in enumerate(self.evidence):
            path = item.get("source_path") if isinstance(item, dict) else None
            sha = item.get("source_sha256") if isinstance(item, dict) else None
            if not _is_path_sha(path, sha) or not isinstance(item.get("content"), str):
                self.questions.append(_Question("item", index, {}))
                continue
            question = _Question("item", index, {path: sha})
            if private(path, sha):
                question.verdict = "local_only"
            else:
                question.state = self._state(request, path, "", item["content"][:excerpt])
            self.questions.append(question)
        for index, cand in enumerate(self.candidates):
            path, sha = cand["source_path"], cand["source_sha256"]
            question = _Question("candidate", index, {path: sha})
            link = cand.get("link_line")
            if link:
                question.paths[link["source_path"]] = link["source_sha256"]
            if private(path, sha):
                question.verdict = "local_only"
                self.questions.append(question)
                continue
            line = ""
            if link and not private(link["source_path"], link["source_sha256"]):
                line = link["content"][:LINK_LINE_CHARS]
            body = "\n\n".join(p["content"] for p in cand["passages"])
            question.state = self._state(request, path, line, body[:excerpt])
            self.questions.append(question)

    @staticmethod
    def _state(request: str, path: str, link_line: str, excerpt: str) -> dict | None:
        """The relevance.v1 state: quoted data only. None (not judged) when the note has no
        name or text to show."""
        title = _stem(path)
        if not title.strip() or not excerpt.strip():
            return None
        return {"request": request, "title": title, "link_line": link_line, "excerpt": excerpt}

    def _evaluate(self, misses: list[_Question]) -> None:
        cfg = self.cfg
        # The last look before anything is sent: a kill switch or a changed configuration
        # since the plan stops the call itself, not only its use.
        if kill_switch(self.vault, self.env):
            raise _Degrade("kill_switch")
        if load_config(self.vault).revision != self.plan.revision:
            raise _Degrade("config_changed")
        evaluate, load_key = self._load_client()
        key = None
        if load_key is not None and _needs_key(self.provider):
            try:
                key = load_key(self.provider, cfg["env_file"], vault=self.vault)
            except Exception:
                raise _Degrade("key_unavailable") from None
        deadline = cfg["timeout_s"] if self.deadline_s is None else self.deadline_s
        results = _call_with_deadline(
            evaluate, (self.provider, [q.questionnaire for q in misses]),
            {"deadline_s": deadline, "max_parallel": cfg["max_parallel"], "key": key},
            deadline)
        key = None
        if not isinstance(results, list) or len(results) != len(misses):
            raise _Degrade("answers_invalid")
        failures = []
        for question, result in zip(misses, results):
            question.usage = _usage_of(result)
            for name in ("input_tokens", "output_tokens", "requests"):
                self.usage[name] += question.usage.get(name, 0)
            if "cost_usd" in question.usage:
                self.cost = (self.cost or 0.0) + question.usage["cost_usd"]
            answer, code = _answer_of(result)
            if answer is None:
                failures.append(code)
                continue
            question.answer = answer
            question.model = _model_of(result.get("model_reported"))
        if failures and len(failures) == len(misses) \
                and not any(q.cached for q in self.questions):
            raise _Degrade(failures[0])
        for code in failures:
            self.partial = True
            self._note(code)

    def _fresh_after(self, policy) -> None:
        """After the provider answered and before anything is used: the same configuration,
        the same routes.json, no kill switch, and every pinned source still resolves inside
        the boundaries to the same bytes. Any difference discards all advice."""
        now = load_config(self.vault)
        if not now.valid or now.revision != self.plan.revision:
            raise _Degrade("config_changed")
        if kill_switch(self.vault, self.env):
            raise _Degrade("kill_switch")
        if _file_digest(_ctx(self.vault) / "routes.json") != self.plan.routes_sha:
            raise _Degrade("config_changed")
        try:
            prefixes = tuple(policy.load_exclusions(self.vault))
        except ValueError:
            raise _Degrade("config_changed") from None
        pins: dict[str, str] = {}
        for question in self.questions:
            pins.update(question.paths)
        for path, sha in sorted(pins.items()):
            try:
                raw = policy.source_path(self.vault, path, prefixes).read_bytes()
            except (OSError, ValueError):
                raise _Degrade("source_changed") from None
            if hashlib.sha256(raw).hexdigest() != sha:
                raise _Degrade("source_changed")
            self.raw[path] = raw

    def _decide(self) -> None:
        thresholds = self.cfg["thresholds"]
        for question in self.questions:
            answer = question.answer
            if answer is None:
                continue
            if answer["p_yes"] is not None:
                question.value = answer["p_yes"]
            else:                                 # label-only: counts only under a receipt
                question.value = 1.0 if answer["label"] == "yes" else 0.0
            if self.model is None and question.model:
                self.model = question.model
            if question.kind == "gate":
                bar = thresholds["gate"]
                self.gate_value = question.value
                self.gate_passed = question.value >= bar
            else:
                bar = thresholds["keep"] if question.kind == "item" else thresholds["rescue"]
            question.verdict = "on_topic" if question.value >= bar else "off_topic"

    def _deliverable(self, p: dict, spans: dict, contents: set) -> bool:
        """Byte-exact against the pinned source, and no repeat or overlap of what the
        packet already holds."""
        raw = self.raw.get(p["source_path"])
        if raw is None or hashlib.sha256(raw).hexdigest() != p["source_sha256"]:
            return False
        if p["end"] > len(raw) or raw[p["start"]:p["end"]] != p["content"].encode("utf-8"):
            return False
        if p["content"] in contents:
            return False
        return not any(lo < p["end"] and p["start"] < hi
                       for lo, hi in spans.get(p["source_path"], []))

    def _apply(self) -> None:
        cfg = self.cfg
        evidence = list(self.evidence)
        if cfg["lossy"]["prune_fts"]:                     # the one lossy lever (off by default)
            drop = set()
            for question in self.questions:
                item = self.evidence[question.index] if question.kind == "item" else None
                if item is not None and item.get("origin", "fts") == "fts" \
                        and question.answer is not None and question.answer["p_yes"] is not None \
                        and question.answer["p_yes"] < cfg["thresholds"]["keep"]:
                    drop.add(question.index)
            self.pruned = sorted(drop)
            evidence = [item for index, item in enumerate(self.evidence) if index not in drop]
        spans = _spans(evidence)
        contents = {item.get("content") for item in evidence}
        budget, spent, extras = cfg["jev_extra_tokens"], 0, []
        order = sorted((q for q in self.questions
                        if q.kind == "candidate" and q.verdict == "on_topic"),
                       key=lambda q: (-q.value, q.index))
        if self.feature == "auto_context" and self.gate_passed is not True:
            order = []                                   # off topic or unjudged: no rescue
            self.skip = self.gate_passed is False and bool(cfg["lossy"]["gate_skip"])
        for question in order:
            cand = self.candidates[question.index]
            local_spans = {k: list(v) for k, v in spans.items()}
            local_contents = set(contents)
            chosen = []
            offered = ([cand["link_line"]] if cand.get("link_line") else []) + cand["passages"]
            for p in offered:
                if self._deliverable(p, local_spans, local_contents):
                    chosen.append(p)
                    local_spans.setdefault(p["source_path"], []).append((p["start"], p["end"]))
                    local_contents.add(p["content"])
            own = [p for p in chosen if p["source_path"] == cand["source_path"]]
            if not own:
                continue
            cost = sum(math.ceil(len(p["content"]) / 4) for p in chosen)
            if spent + cost > budget:
                chosen = own
                cost = sum(math.ceil(len(p["content"]) / 4) for p in own)
                if spent + cost > budget:
                    self.budget_skipped += 1
                    continue
            for p in chosen:
                extras.append(self._rescue_item(p, question, cand))
                spans.setdefault(p["source_path"], []).append((p["start"], p["end"]))
                contents.add(p["content"])
            spent += cost
            self.rescued.append(question.index)
        self.extra_tokens = spent
        final = evidence + extras
        self.out["evidence"] = final
        if self.out.get("status") in ("PARTIAL", "NOT_FOUND"):
            self.out["status"] = "PARTIAL" if final else "NOT_FOUND"

    def _rescue_item(self, p: dict, question: _Question, cand: dict) -> dict:
        item = {k: p[k] for k in ("source_path", "source_sha256", "content", "start", "end",
                                  "line_start", "line_end", "source_chars", "truncated")
                if k in p}
        for k in ("hop", "activation", "via", "anchors"):
            if k in p:
                item[k] = p[k]
        item["origin"] = "jev"
        item["reason"] = f"advisor judged the linked note relevant: {p.get('reason', 'passage')}"
        item["est_tokens"] = math.ceil(len(p["content"]) / 4)
        item["jev"] = {"p_yes": question.answer["p_yes"], "candidate": cand["kind"],
                       "note": cand["source_path"], "provider_kind": self.provider.get("kind")}
        return item

    # -- output --------------------------------------------------------------
    def _block(self) -> dict:
        items = [q for q in self.questions if q.kind == "item"]
        cands = [q for q in self.questions if q.kind == "candidate"]
        applied = self.mode == "on" and not self.discarded
        codes = ([self.discarded] if self.discarded else []) + \
            [c for c in self.codes if c != self.discarded]

        def p_of(q):
            return None if q.answer is None or q.answer["p_yes"] is None \
                else round(q.answer["p_yes"], 4)

        block: dict = {
            "schema": ADVICE_SCHEMA, "feature": self.feature, "mode": self.mode,
            "applied": applied,
            "superset": not (self.mode == "on" and self.cfg["lossy"]["prune_fts"]),
            "provider_kind": self.provider.get("kind"), "model_reported": self.model,
            "counts": {
                "judged": sum(1 for q in self.questions if q.answer is not None),
                "local_only": sum(1 for q in self.questions if q.verdict == "local_only"),
                "flagged": sum(1 for q in items if q.verdict == "off_topic"),
                "candidates": len(self.candidates),
                "would_rescue": sum(1 for q in cands if q.verdict == "on_topic"),
                "rescued": len(self.rescued), "pruned": len(self.pruned),
                "cache_hits": sum(1 for q in self.questions if q.cached),
            },
            "items": [{"index": q.index, "verdict": q.verdict, "p_yes": p_of(q),
                       **({"pruned": True} if q.index in self.pruned else {})} for q in items],
            "candidates": [{"source_path": self.candidates[q.index]["source_path"],
                            "kind": self.candidates[q.index]["kind"], "verdict": q.verdict,
                            "p_yes": p_of(q), "rescued": q.index in self.rescued}
                           for q in cands],
            "degraded": bool(self.discarded) or self.partial,
            "codes": codes,
            "latency_ms": int(round((time.monotonic() - self.started) * 1000)),
            "requests": self.usage["requests"], "input_tokens": self.usage["input_tokens"],
            "extra_est_tokens": self.extra_tokens,
            "jev_extra_tokens": self.cfg["jev_extra_tokens"], "notice": NOTICE,
        }
        if self.feature == "auto_context":
            block["gate"] = {"judged": self.gate_value is not None,
                             "p_yes": None if self.gate_value is None
                             else round(self.gate_value, 4),
                             "passed": self.gate_passed, "threshold": self.cfg["thresholds"]["gate"]}
            block["counts"]["would_skip"] = int(self.gate_passed is False)
            block["skip"] = bool(self.skip and applied)
        return block

    def _log_row(self, block: dict) -> dict:
        counts = block["counts"]
        judged = [q for q in self.questions if q.answer is not None]
        model = (block["model_reported"] or "").lower() or None
        hook = self.feature == "auto_context"
        return _row(self.feature, block["mode"], applied=block["applied"],
                    provider_kind=block["provider_kind"], model_id=model,
                    cache_hit=bool(judged) and all(q.cached for q in judged),
                    degraded=block["degraded"], code=(block["codes"] or [None])[0],
                    latency_ms=block["latency_ms"], requests=self.usage["requests"],
                    input_tokens=self.usage["input_tokens"],
                    output_tokens=self.usage["output_tokens"], cost_usd=self.cost,
                    candidates=counts["candidates"], judged=counts["judged"],
                    local_only=counts["local_only"],
                    gate_passed=self.gate_passed if hook else None,
                    flagged=counts["flagged"], would_prune=sum(
                        1 for q in self.questions if q.kind == "item" and q.answer is not None
                        and q.answer["p_yes"] is not None
                        and q.answer["p_yes"] < self.cfg["thresholds"]["keep"]
                        and self.evidence[q.index].get("origin", "fts") == "fts"),
                    pruned=counts["pruned"], would_rescue=counts["would_rescue"],
                    rescued=counts["rescued"],
                    would_skip=int(self.gate_passed is False) if hook else None,
                    skipped=int(bool(block.get("skip"))) if hook else None)

    def _labels(self) -> dict[str, str]:
        rank = {"rescued": 5, "on_topic": 4, "off_topic": 3, "local_only": 2, "not_judged": 1}
        labels: dict[str, str] = {}
        for question in self.questions:
            if question.kind == "gate":
                continue
            if question.kind == "item":
                item = self.evidence[question.index]
                path = item.get("source_path") if isinstance(item, dict) else None
            else:
                path = self.candidates[question.index]["source_path"]
            if not isinstance(path, str):
                continue
            label = question.verdict
            if question.kind == "candidate" and question.index in self.rescued:
                label = "rescued"
            if rank[label] > rank.get(labels.get(path), 0):
                labels[path] = label
        return labels

    def _annotate_trace(self, block: dict) -> None:
        """Additive `jev` fields in the trace this very retrieval wrote (same run id and
        packet counts), only for synaptic packets and only when `trace` is true."""
        synapse_info = self.packet.get("synapse")
        run_id = self.side.get("trace_run_id")
        if self.method != "synaptic" or not self.cfg["trace"] or not isinstance(run_id, str) \
                or not isinstance(synapse_info, dict) or synapse_info.get("trace") != TRACE_REL:
            return
        try:
            data = json.loads(_read_regular(self.vault / TRACE_REL, TRACE_MAX_BYTES)
                              .decode("utf-8"))
        except (OSError, UnicodeError, ValueError):
            return
        base = self.packet.get("evidence") or []
        expected = {"passages": len(base), "est_tokens": synapse_info.get("est_tokens"),
                    "status": self.packet.get("status")}
        if not isinstance(data, dict) or data.get("version") != 1 or data.get("run_id") != run_id \
                or data.get("packet") != expected:
            return
        counts = block["counts"]
        data["jev"] = {"mode": block["mode"], "applied": block["applied"],
                       "superset": block["superset"], "provider_kind": block["provider_kind"],
                       "gate_passed": self.gate_passed,
                       "kept": sum(1 for q in self.questions
                                   if q.kind == "item" and q.verdict == "on_topic"),
                       "flagged": counts["flagged"], "rescued": counts["rescued"],
                       "would_rescue": counts["would_rescue"],
                       "degraded": block["degraded"]}
        labels = self._labels()
        for node in data.get("nodes") or []:
            if isinstance(node, dict) and isinstance(node.get("path"), str):
                node["jev"] = labels.get(node["path"], "not_judged")
                if node["jev"] == "rescued":
                    node["selected"] = True
        from . import synapse
        synapse.write_trace(self.vault, data)


def advise_search(vault, prompt: str, method: str, packet: dict, plan: SearchPlan, *,
                  evaluate_fn=None, load_key_fn=None, contracts=None, environ=None) -> dict:
    """The packet `search --jev` prints: in shadow the retrieval packet unchanged plus a
    top-level `jev` block; in on, the same packet followed by rescued `origin: "jev"` items
    within `jev_extra_tokens`. The `jev_candidates` side channel is always removed. Any
    failure leaves the packet as retrieval built it (plus the block) and one log row.

    `evaluate_fn` / `load_key_fn` / `contracts` replace `jev_client.evaluate`,
    `jev_client.load_key` and the `jev_contracts` module (tests); by default they are
    imported here, lazily, only when a question is actually about to be asked."""
    return _SearchAdvice(Path(vault).resolve(), prompt, method, packet, plan, evaluate_fn,
                         load_key_fn, contracts, environ).run()


def hook_plan(vault, method: str, compact: bool, environ=None) -> SearchPlan | None:
    """The prompt hook's plan for the `auto_context` feature. None means today's hook,
    byte for byte and without a message (a hook runs on every prompt): no configuration,
    an invalid one, mode off, the feature not enabled, a kill switch or the child guard,
    a method the advisor does not support. A kill switch or the child guard on an enabled
    feature writes one counter row. Reads the configuration only."""
    vault = Path(vault).resolve()
    config = load_config(vault)
    mode, why = effective_mode(vault, config, environ, feature="auto_context")
    if mode == "off":
        if why in ("kill_switch", "child_guard") and config.valid \
                and "auto_context" in config.data["features"]:
            append_log(vault, _row("auto_context", "off", code=why, applied=False,
                                   degraded=False,
                                   provider_kind=(config.data.get("provider") or {}).get("kind")))
        return None
    if method not in ("fts", "synaptic") or compact:
        return None
    return SearchPlan(vault, mode, config, method,
                      _file_digest(_ctx(vault) / "routes.json"), config.revision,
                      min(HOOK_CANDIDATES, config.data["max_candidates"]))


def hook_deadline(plan: SearchPlan, elapsed_s: float) -> float:
    """Seconds the advisor may take inside the hook: the configured `hook_timeout_s`, and
    never past HOOK_WALL_S after the hook started (the host allows 30 s)."""
    return min(float(plan.config.data["hook_timeout_s"]), HOOK_WALL_S - elapsed_s)


def hook_skipped(plan: SearchPlan, code: str) -> None:
    """One counter row when the hook had no time left to ask (`skipped_deadline`)."""
    append_log(plan.vault, _row("auto_context", plan.mode, code=code, applied=False,
                                degraded=True,
                                provider_kind=(plan.config.data.get("provider") or {}).get("kind")))


def advise_hook(vault, prompt: str, method: str, packet: dict, plan: SearchPlan, *,
                deadline_s: float, evaluate_fn=None, load_key_fn=None, contracts=None,
                environ=None) -> dict:
    """The packet the prompt hook renders with the `auto_context` feature: one
    `topicality.v1` gate question about the prompt plus one `relevance.v1` question per
    delivered passage and per candidate, all within `deadline_s`. In `shadow` the evidence
    is unchanged (the `jev` block is not rendered, so the hook's output is byte for byte
    the same); in `on`, when the gate passed and a receipt covers `auto_context`, rescued
    passages follow the unchanged items within `jev_extra_tokens`. `jev.skip` is true only
    with the lossy `gate_skip` lever, when the gate said the prompt is off topic: the hook
    then adds nothing. Any failure leaves the packet as retrieval built it."""
    return _SearchAdvice(Path(vault).resolve(), prompt, method, packet, plan, evaluate_fn,
                         load_key_fn, contracts, environ, feature="auto_context",
                         deadline_s=deadline_s).run()


# ---------------------------------------------------------------------------
# answer (feature `answer`): does the quoted passage support the claim?
# ---------------------------------------------------------------------------
# One `claim_support.v1` question per (claim, citation): the claim, the verbatim span and
# the enclosing heading-bounded section of the note (a quote can be exact while the section
# later cancels it). Used by `jev answer`, `handback check --jev` and the MCP tool
# `check_claims`. The advisor only ever adds an advisory verdict next to the deterministic
# result; it never turns a failed mechanical check into a pass, and a record that failed
# the mechanical check is never asked about.

CLAIMS_SCHEMA = "jev-claims/v1"
CLAIM_REPORT_SCHEMA = "jev-claim-report/v1"
CLAIM_TEMPLATE = "claim_support.v1"                  # state: claim, quote, section
CLAIM_LABELS = ("supports", "contradicts", "silent")
CLAIM_VERDICTS = {"supports": "supported", "contradicts": "contradicted",
                  "silent": "insufficient"}
CLAIMS_MAX = 20                                      # claims per call
CITATIONS_MAX = 8                                    # citations per claim
CLAIM_CHARS = 2000                                   # what the template accepts
QUOTE_CHARS = 4000
SECTION_CHARS = 4000                                 # a longer section is never cut: not asked
CLAIMS_MAX_BYTES = 1024 * 1024
CLAIM_SPAN_INPUT_CHARS = 20000                       # a longer quote is refused on input
CLAIM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
CLAIM_CITATION_KEYS = ("source_path", "source_sha256", "line_start", "line_end", "span")
# The search pipeline's prompt gates (a minimum length) do not apply to claims, which carry
# no prompt; a fixed marker of sufficient length passes them.
CLAIMS_MARK = "claim support check"
ANSWER_NOTICE = ("advisory: a model's judgement of whether the quoted passage, read within "
                 "its section, supports the claim; not a verification, not a verdict on "
                 "correctness, and it never turns a failed mechanical check into a pass")
UNJUDGED = ("not_judged", "local_only")
ANSWER_OFF_MESSAGES = {
    "not_configured": "the optional advisor is not configured (docs/jev.md)",
    "config_invalid": ".context/jev.json is invalid (see `context-layer jev status`)",
    "mode_off": "the advisor is off (see `context-layer jev status`)",
    "kill_switch": "a kill switch is set",
    "child_guard": "running inside an advisor call",
    "feature_disabled": "the answer feature is disabled",
}


def _textfold():
    try:
        from router import textfold                   # checkout
    except ImportError:
        from .router import textfold                  # installed package
    return textfold


def claim_view(text: str, line_start: int, line_end: int, span: str) -> tuple[dict | None, str | None]:
    """(view, None) with the verbatim `span` and the enclosing section, or (None, code).
    The section runs from the nearest heading at or above the cited lines to the next
    heading of the same or a higher level (subsections included; a `#` inside a code fence
    is not a heading), or to the end of the file; without a heading above, from the start
    of the file to the next heading. It always covers the cited lines. A section or quote
    over the template's limits is not cut: the code is `context_incomplete`."""
    from . import orchestrate
    if not orchestrate.span_at(text, span, line_start, line_end):
        return None, "span_not_found"
    lines = orchestrate.lines_of(text)
    first = sum(len(line) for line in lines[:line_start - 1])
    last = first + sum(len(line) for line in lines[line_start - 1:line_end])
    found = _textfold().headings(text)
    above = [h for h in found if h[0] <= first]
    if above:
        start, level = above[-1][0], above[-1][1]
        end = next((h[0] for h in found if h[0] > first and h[1] <= level), len(text))
    else:
        start = 0
        end = next((h[0] for h in found if h[0] > first), len(text))
    section = text[start:max(end, last)]
    if len(section) > SECTION_CHARS or len(span) > QUOTE_CHARS or not section.strip():
        return None, "context_incomplete"
    return {"quote": span, "section": section}, None


class _ClaimAdvice(_SearchAdvice):
    """The `answer` feature on the search pipeline: the same gates, secret scan (one
    question at a time), cache, provider call, freshness check and counters row, with a
    claim question per (claim, citation) instead of a relevance question per note, and
    nothing to apply: the verdicts are notes beside the deterministic result."""

    def __init__(self, vault: Path, items: list[dict], plan: SearchPlan, evaluate_fn,
                 load_key_fn, contracts, environ, deadline_s: float | None = None):
        super().__init__(vault, CLAIMS_MARK, "fts", {}, plan, evaluate_fn, load_key_fn,
                         contracts, environ, feature="answer", deadline_s=deadline_s)
        self.items = items
        self.info: dict[int, dict] = {}

    # -- questions -----------------------------------------------------------
    def _source_text(self, policy, prefixes, path: str, sha: str) -> str | None:
        try:
            raw = policy.source_path(self.vault, path, prefixes).read_bytes()
        except (OSError, ValueError):
            raise _Degrade("source_changed") from None
        if hashlib.sha256(raw).hexdigest() != sha:
            raise _Degrade("source_changed")
        try:
            return raw.decode("utf-8")
        except UnicodeError:
            return None

    def _build_questions(self, policy, prefixes) -> None:
        local = tuple(self.cfg["local_only_prefixes"])
        for index, item in enumerate(self.items):
            question = _Question("claim", index, {}, CLAIM_TEMPLATE)
            self.questions.append(question)
            info = self.info.setdefault(index, {})
            path, sha = item.get("path"), item.get("sha256")
            claim = item.get("claim")
            if not _is_path_sha(path, sha) or not isinstance(claim, str):
                info["code"] = "invalid_citation"
                continue
            question.paths = {path: sha}
            if _assess(self.vault, path, sha, prefixes, local, policy):
                question.verdict = "local_only"
                continue
            if not claim.strip() or len(claim) > CLAIM_CHARS:
                info["code"] = "claim_too_long" if claim.strip() else "claim_empty"
                continue
            text = self._source_text(policy, prefixes, path, sha)
            if text is None:
                info["code"] = "source_not_text"
                continue
            view, code = claim_view(text, item.get("line_start"), item.get("line_end"),
                                    item.get("span"))
            if view is None:
                info["code"] = code
                self._note(code)
                continue
            question.state = {"claim": claim, **view}

    # -- decisions -----------------------------------------------------------
    def _decide(self) -> None:
        """supports / contradicts / silent at confidence >= thresholds.confidence become
        supported / contradicted / insufficient; anything less sure is `uncertain`. A
        label-only answer (no probabilities) is `uncertain` (`uncalibrated`) unless a
        calibration receipt covers this provider, that is, unless the mode in force is on."""
        threshold = self.cfg["thresholds"]["confidence"]
        for question in self.questions:
            answer = question.answer
            if answer is None:
                continue
            if self.model is None and question.model:
                self.model = question.model
            info = self.info.setdefault(question.index, {})
            if answer.get("type") != "choice" or answer.get("label") not in CLAIM_LABELS:
                question.answer = None
                info["code"] = "answer_invalid"
                self._note("answer_invalid")
                continue
            label, probabilities = answer["label"], answer.get("probabilities")
            confidence = answer.get("confidence")
            info.update(label=label, confidence=confidence,
                        p_yes=None if not probabilities else probabilities.get("supports"))
            if confidence is None and self.mode != "on":
                question.verdict, info["code"] = "uncertain", "uncalibrated"
            elif confidence is not None and confidence < threshold:
                question.verdict, info["code"] = "uncertain", "low_confidence"
            else:
                question.verdict = CLAIM_VERDICTS[label]

    def _apply(self) -> None:
        """Nothing to apply: an answer verdict is only ever a note."""

    # -- output --------------------------------------------------------------
    def _decisive(self) -> int:
        return sum(1 for q in self.questions if q.verdict in CLAIM_VERDICTS.values())

    def _block(self) -> dict:
        applied = self.mode == "on" and not self.discarded
        codes = ([self.discarded] if self.discarded else []) + \
            [c for c in self.codes if c != self.discarded]
        for question in self.questions:                       # codes of single claims
            code = self._result(question).get("code")
            if code and code not in codes and question.verdict != "local_only" \
                    and len(codes) < 12:
                codes.append(code)
        counts = {name: sum(1 for q in self.questions if q.verdict == name)
                  for name in ("supported", "contradicted", "insufficient", "uncertain",
                               "not_judged", "local_only")}
        counts.update({"asked": len(self.items),
                       "judged": sum(1 for q in self.questions if q.answer is not None),
                       "cache_hits": sum(1 for q in self.questions if q.cached)})
        return {"schema": ADVICE_SCHEMA, "feature": "answer", "mode": self.mode,
                "applied": applied, "superset": True,
                "provider_kind": self.provider.get("kind"), "model_reported": self.model,
                "counts": counts, "degraded": bool(self.discarded) or self.partial,
                "codes": codes,
                "latency_ms": int(round((time.monotonic() - self.started) * 1000)),
                "requests": self.usage["requests"], "input_tokens": self.usage["input_tokens"],
                "confidence_threshold": self.cfg["thresholds"]["confidence"],
                "notice": ANSWER_NOTICE}

    def _log_row(self, block: dict) -> dict:
        counts = block["counts"]
        judged = [q for q in self.questions if q.answer is not None]
        decisive = self._decisive()
        return _row("answer", block["mode"], applied=block["applied"],
                    provider_kind=block["provider_kind"],
                    model_id=(block["model_reported"] or "").lower() or None,
                    cache_hit=bool(judged) and all(q.cached for q in judged),
                    degraded=block["degraded"], code=(block["codes"] or [None])[0],
                    latency_ms=block["latency_ms"], requests=self.usage["requests"],
                    input_tokens=self.usage["input_tokens"],
                    output_tokens=self.usage["output_tokens"], cost_usd=self.cost,
                    candidates=counts["asked"], judged=counts["judged"],
                    local_only=counts["local_only"], flagged=counts["contradicted"],
                    # for this feature: the advisory notes `on` would add / added
                    would_rescue=decisive, rescued=decisive if block["applied"] else 0)

    def _annotate_trace(self, block: dict) -> None:
        """No activation trace belongs to a claim check."""

    def _result(self, question: _Question) -> dict:
        info = self.info.get(question.index, {})
        verdict = question.verdict
        code = info.get("code")
        if verdict == "local_only":
            code = "local_only"
        elif verdict == "not_judged":
            if self.discarded:
                code = self.discarded
            elif code is None:
                code = ("sensitive_input" if question.state is None else
                        "not_asked" if question.questionnaire is None else "no_answer")
        judged = verdict not in UNJUDGED
        p_yes, confidence = info.get("p_yes"), info.get("confidence")
        return {"verdict": verdict, "label": info.get("label") if judged else None,
                "p_yes": None if not judged or p_yes is None else round(p_yes, 4),
                "confidence": None if not judged or confidence is None
                else round(confidence, 4),
                "code": code, "cached": question.cached,
                "provider_kind": self.provider.get("kind")}

    def run_claims(self) -> dict:
        block = self.run()["jev"]
        return {"jev": block, "verdicts": [self._result(q) for q in self.questions]}


def answer_plan(vault, environ=None) -> tuple[SearchPlan | None, str | None]:
    """(plan, None) when feature `answer` may ask a provider, else (None, why it is off):
    not_configured, config_invalid, mode_off, kill_switch, child_guard or feature_disabled.
    A kill switch, the child guard or a disabled feature on a valid configuration writes one
    counter row. Reads the configuration only; never imports the provider client."""
    vault = Path(vault).resolve()
    config = load_config(vault)
    mode, why = effective_mode(vault, config, environ, feature="answer")
    if mode == "off":
        if why in ("kill_switch", "child_guard", "feature_disabled"):
            append_log(vault, _row("answer", "off", code=why, applied=False, degraded=False,
                                   provider_kind=(config.data.get("provider") or {}).get("kind")))
        return None, why
    return SearchPlan(vault, mode, config, "fts", _file_digest(_ctx(vault) / "routes.json"),
                      config.revision, 0), None


def advise_claims(vault, items: list[dict], plan: SearchPlan, *, deadline_s: float | None = None,
                  evaluate_fn=None, load_key_fn=None, contracts=None, environ=None) -> dict:
    """Ask the advisor about claims: `items` is a list of {claim, path, sha256, line_start,
    line_end, span}, each one already checked mechanically by the caller. Returns
    {"jev": <jev-advice/v1 block>, "verdicts": [one result per item, in order]}, where a
    result is {verdict, label, p_yes, confidence, code, cached, provider_kind} and verdict
    is supported | contradicted | insufficient | uncertain | not_judged | local_only. Any
    failure leaves every verdict not_judged (with the code) and one counter row; nothing
    the caller holds is changed. `evaluate_fn` / `load_key_fn` / `contracts` replace the
    provider client and the contracts module in tests."""
    return _ClaimAdvice(Path(vault).resolve(), items, plan, evaluate_fn, load_key_fn,
                        contracts, environ, deadline_s=deadline_s).run_claims()


def parse_claims(data) -> list[dict]:
    """Validate a `jev-claims/v1` document: 1-20 claims, each {text, citations, [id]} with
    1-8 citations {source_path, source_sha256, line_start, line_end, span}. Returns the
    claims; raises Refused (naming the field, never a path outside the vault)."""
    if not isinstance(data, dict) or data.get("schema", CLAIMS_SCHEMA) != CLAIMS_SCHEMA \
            or set(data) - {"schema", "claims"} or not isinstance(data.get("claims"), list):
        raise Refused(f"expected a {CLAIMS_SCHEMA} object: {{\"schema\", \"claims\": [...]}}")
    claims = data["claims"]
    if not 1 <= len(claims) <= CLAIMS_MAX:
        raise Refused(f"claims must hold 1 to {CLAIMS_MAX} items, not {len(claims)}")
    out = []
    for number, claim in enumerate(claims, 1):
        where = f"claim {number}"
        if not isinstance(claim, dict) or not {"text", "citations"} <= set(claim) \
                or set(claim) - {"text", "citations", "id"}:
            raise Refused(f"{where} must be an object with text and citations (and optionally id)")
        text = claim["text"]
        if not isinstance(text, str) or not text.strip() or len(text) > CLAIM_CHARS:
            raise Refused(f"{where}: text must be 1 to {CLAIM_CHARS} characters")
        ident = claim.get("id")
        if ident is not None and (not isinstance(ident, str) or not CLAIM_ID.fullmatch(ident)):
            raise Refused(f"{where}: id must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}")
        citations = claim["citations"]
        if not isinstance(citations, list) or not 1 <= len(citations) <= CITATIONS_MAX:
            raise Refused(f"{where}: citations must hold 1 to {CITATIONS_MAX} items")
        clean = []
        for position, cite in enumerate(citations, 1):
            spot = f"{where}, citation {position}"
            if not isinstance(cite, dict) or set(cite) != set(CLAIM_CITATION_KEYS):
                raise Refused(f"{spot} must be an object with exactly "
                              f"{', '.join(CLAIM_CITATION_KEYS)}")
            if not isinstance(cite["source_path"], str) or not cite["source_path"] \
                    or len(cite["source_path"]) > 1024:
                raise Refused(f"{spot}: source_path must be a vault-relative path")
            if not isinstance(cite["source_sha256"], str) \
                    or not SHA.fullmatch(cite["source_sha256"]):
                raise Refused(f"{spot}: source_sha256 must be 64 lowercase hex characters")
            for key in ("line_start", "line_end"):
                value = cite[key]
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise Refused(f"{spot}: {key} must be a positive integer")
            if cite["line_start"] > cite["line_end"]:
                raise Refused(f"{spot}: line_start is after line_end")
            if not isinstance(cite["span"], str) or not cite["span"].strip() \
                    or len(cite["span"]) > CLAIM_SPAN_INPUT_CHARS:
                raise Refused(f"{spot}: span must be 1 to {CLAIM_SPAN_INPUT_CHARS} characters")
            clean.append(dict(cite))
        out.append({"text": text, "id": ident, "citations": clean})
    return out


def load_claims(path) -> list[dict]:
    """Read and validate a claims file (a regular file of at most 1 MiB)."""
    path = Path(path).expanduser()
    try:
        raw = _read_regular(path, CLAIMS_MAX_BYTES, path.name)
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except Refused:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise Refused(f"{path.name} cannot be read as JSON ({type(exc).__name__})") from None
    return parse_claims(data)


def claim_verdict(verdicts: list[str]) -> tuple[str, str | None]:
    """One advisory verdict per claim from the verdicts of its judged citations. A citation
    that supports and another that contradicts disagree: `uncertain`. Otherwise supported
    if any supports, else contradicted if any contradicts, else insufficient if any is
    insufficient, else uncertain; `not_judged` when no citation was judged at all."""
    judged = [v for v in verdicts if v not in UNJUDGED]
    if not judged:
        return "not_judged", None
    if "supported" in judged and "contradicted" in judged:
        return "uncertain", "citations_disagree"
    for verdict in ("supported", "contradicted", "insufficient"):
        if verdict in judged:
            return verdict, None
    return "uncertain", None


def claims_report(vault, claims: list[dict], *, surface: str = "cli", ask: bool = True,
                  deadline_s: float | None = None, evaluate_fn=None, load_key_fn=None,
                  contracts=None, environ=None) -> dict:
    """The `jev-claim-report/v1` for validated claims. Every citation is checked
    mechanically first (the source is inside the boundaries and still the cited bytes, the
    quote is verbatim at the cited lines and long enough); only citations that pass are put
    to the advisor. `surface` decides what the caller sees: "cli" shows advisor verdicts in
    shadow and on, and says why it is off; "mcp" shows them only in on (a shadow call is
    counted, not shown) and has no `jev` key otherwise. The mechanical fields are the same
    in every mode."""
    from . import orchestrate
    vault = Path(vault).resolve()
    try:
        prefixes = orchestrate._prefixes(vault)
    except orchestrate.OrchestrateError as exc:
        raise Refused(str(exc)) from None
    cache: dict = {}
    entries, items = [], []
    for number, claim in enumerate(claims, 1):
        cites = []
        for position, cite in enumerate(claim["citations"], 1):
            # The record's observation is the quote itself: this checks the citation. Whether
            # the claim's own numbers, dates and names appear in the quote is reported apart
            # (`anchor_notes`), because a claim the quote contradicts differs from it on
            # exactly those.
            record = {"id": f"c{number}.{position}", "observation": cite["span"],
                      "source_path": cite["source_path"], "source_sha256": cite["source_sha256"],
                      "line_start": cite["line_start"], "line_end": cite["line_end"],
                      "span": cite["span"], "method": "", "uncertainty": ""}
            detail = orchestrate.check_record_detail(vault, record, ["."], prefixes, None, cache)
            entry = {"source_path": cite["source_path"], "line_start": cite["line_start"],
                     "line_end": cite["line_end"],
                     "mechanically_checked": detail["mechanically_checked"],
                     "reasons": detail["reasons"],
                     "anchor_notes": orchestrate.claim_problems(claim["text"], cite["span"])
                     if detail["mechanically_checked"] else []}
            if detail["mechanically_checked"]:
                items.append({"claim": claim["text"], "path": cite["source_path"],
                              "sha256": cite["source_sha256"], "line_start": cite["line_start"],
                              "line_end": cite["line_end"], "span": cite["span"]})
                entry["_item"] = len(items) - 1
            cites.append(entry)
        entries.append({"claim": number, "id": claim.get("id"), "citations": cites})
    plan, why = answer_plan(vault, environ) if ask else (None, "not_asked")
    advice = None
    if plan is not None and items:
        advice = advise_claims(vault, items, plan, deadline_s=deadline_s,
                               evaluate_fn=evaluate_fn, load_key_fn=load_key_fn,
                               contracts=contracts, environ=environ)
    show = advice is not None and (advice["jev"]["mode"] == "on"
                                   or (surface == "cli" and advice["jev"]["mode"] == "shadow"))
    checked = sum(1 for e in entries for c in e["citations"] if c["mechanically_checked"])
    report: dict = {"schema": CLAIM_REPORT_SCHEMA, "claims": [],
                    "citations": sum(len(e["citations"]) for e in entries),
                    "mechanically_checked": checked,
                    "approved": False, "memory_written": False, "rewrites": False,
                    "meaning": "mechanically_checked = the citation is a verbatim span at the "
                               "cited lines of the current file inside the vault's boundaries; "
                               "it does not make the claim true. anchor_notes lists numbers, "
                               "dates and names the claim asserts that the quote does not "
                               "carry."}
    for entry in entries:
        verdicts = []
        for cite in entry["citations"]:
            index = cite.pop("_item", None)
            if show and index is not None:
                cite["jev"] = advice["verdicts"][index]
                verdicts.append(cite["jev"]["verdict"])
        out = {"claim": entry["claim"], "citations": entry["citations"]}
        if entry["id"]:
            out["id"] = entry["id"]
        if show:
            verdict, code = claim_verdict(verdicts)
            out["jev"] = {"verdict": verdict, **({"code": code} if code else {})}
        report["claims"].append(out)
    if show:
        report["meaning"] += (" A jev verdict is a model's judgement of support, not a "
                              "verification.")
        report["jev"] = advice["jev"]
    elif surface == "cli":
        report["jev"] = advice["jev"] if advice is not None else {
            "schema": ADVICE_SCHEMA, "feature": "answer", "mode": "off", "applied": False,
            "why": why, "notice": ANSWER_NOTICE}
    return report


def render_claims_report(report: dict) -> str:
    lines = []
    for claim in report["claims"]:
        label = f"claim {claim['claim']}" + (f" ({claim['id']})" if claim.get("id") else "")
        verdict = (claim.get("jev") or {}).get("verdict")
        lines.append(f"{label}: " + (f"advisory {verdict}" if verdict else "no advisor verdict"))
        for cite in claim["citations"]:
            state = "checked" if cite["mechanically_checked"] else "FAILED"
            lines.append(f"  {cite['source_path']} lines {cite['line_start']}-"
                         f"{cite['line_end']}: mechanical {state}")
            for reason in cite["reasons"]:
                lines.append(f"    {reason}")
            for note in cite["anchor_notes"]:
                lines.append(f"    note: {note}")
            advice = cite.get("jev")
            if advice:
                extra = "" if advice["p_yes"] is None else f", p_yes {advice['p_yes']}"
                code = f" ({advice['code']})" if advice["code"] else ""
                lines.append(f"    advisor: {advice['verdict']}{extra}{code}, "
                             f"{advice['provider_kind']}")
    block = report.get("jev")
    if block is not None:
        if block.get("why"):
            lines.append(f"advisor: off ({block['why']}); mechanical checks only")
        else:
            lines.append(f"advisor: {block['mode']}, applied {str(block['applied']).lower()}, "
                         f"provider {block['provider_kind']}"
                         + (f", degraded {','.join(block['codes'])}" if block["degraded"] else ""))
        lines.append(block["notice"])
    lines.append(report["meaning"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# review-memory (feature `memory`)
# ---------------------------------------------------------------------------
# `context-layer jev review-memory VAULT --proposal FILE` reads what someone wants to add
# to the shared memory (a `jev-memory-proposal/v1`: kind, text, evidence spans, optional
# prior record ids) and prints an advisory report. Always, in every mode and without a
# model: each evidence span is checked the way a handback record is (boundaries, current
# hash, the quote at the cited lines), an exact duplicate of a record in force is named.
# In shadow and on, four fixed questions go to the provider through the same gates,
# cache, freshness check, kill switch and counters as `search`: does the evidence support
# the proposal, how firmly does it commit, what kind of record is it, and how does it
# relate to each prior record (at most four). The advisor never writes, accepts or
# rejects a record: the report always says `approved: false, memory_written: false`, and
# the store is only read.

MEMORY_PROPOSAL_SCHEMA = "jev-memory-proposal/v1"
MEMORY_REVIEW_SCHEMA = "jev-memory-review/v1"
MEMORY_BATCH_SCHEMA = "jev-memory-review-batch/v1"
MEMORY_PROPOSAL_MAX_BYTES = 256 * 1024
MEMORY_TEXT_CHARS = 4000            # the proposal, a prior record and one evidence view
MEMORY_EVIDENCE_MAX = 8
MEMORY_PRIORS_MAX = 4
MEMORY_PROPOSALS_MAX = 64
MEMORY_KINDS = ("decision", "task", "result", "note")     # the kinds `memory add` stores
MEMORY_NON_RECORD_KINDS = ("question", "hypothesis", "other")
MEMORY_TEMPLATES = (("support", "memory_support.v1"), ("commitment", "memory_commitment.v1"),
                    ("kind", "memory_kind.v1"))
MEMORY_RELATION_TEMPLATE = "memory_relation.v1"
MEMORY_ROUTES = ("candidate", "inspect_sources")
MEMORY_ID = re.compile(r"m-[0-9a-f]{16}")
MEMORY_KEYS = ("schema", "kind", "text", "evidence", "prior")
MEMORY_EVIDENCE_KEYS = ("path", "sha256", "line_start", "line_end", "span")
MEMORY_NOTICE = ("advisory: a model's reading of whether the quoted passages support the "
                 "proposal, not a check that it is true; nothing was written to the memory "
                 "store and nothing was approved or rejected")
_HEADING = re.compile(r"(#{1,6})[ \t]+\S")
_LABEL = re.compile(r"[a-z_]{1,32}")


def _positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def parse_memory_proposal(obj) -> dict:
    """A checked `jev-memory-proposal/v1`. Raises Refused for anything else: the keys are
    exactly {schema?, kind, text, evidence, prior?}; `evidence` holds 1-8 objects with
    exactly {path, sha256, line_start, line_end, span}; `prior` is absent (the newest
    records in force that share an evidence path are used) or a list of at most four
    record ids (an empty list asks for no comparison)."""
    if not isinstance(obj, dict):
        raise Refused("a proposal must be a JSON object")
    unknown = sorted(set(obj) - set(MEMORY_KEYS))
    if unknown:
        raise Refused(f"unknown key {unknown[0]!r} in the proposal")
    if obj.get("schema", MEMORY_PROPOSAL_SCHEMA) != MEMORY_PROPOSAL_SCHEMA:
        raise Refused(f"schema must be {MEMORY_PROPOSAL_SCHEMA}")
    if obj.get("kind") not in MEMORY_KINDS:
        raise Refused(f"kind must be one of {', '.join(MEMORY_KINDS)}")
    text = obj.get("text")
    if not isinstance(text, str) or not text.strip():
        raise Refused("text must be a non-empty string")
    if len(text.strip()) > MEMORY_TEXT_CHARS:
        raise Refused(f"text is longer than {MEMORY_TEXT_CHARS} characters")
    evidence = obj.get("evidence")
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= MEMORY_EVIDENCE_MAX:
        raise Refused(f"evidence must list 1 to {MEMORY_EVIDENCE_MAX} spans")
    checked = []
    for item in evidence:
        if not isinstance(item, dict) or set(item) != set(MEMORY_EVIDENCE_KEYS):
            raise Refused("each evidence item needs exactly path, sha256, line_start, "
                          "line_end and span")
        if not isinstance(item["path"], str) or not item["path"]:
            raise Refused("evidence path must be a vault-relative path")
        if not isinstance(item["sha256"], str) or not SHA.fullmatch(item["sha256"]):
            raise Refused("evidence sha256 must be 64 lowercase hex characters")
        if not (_positive_int(item["line_start"]) and _positive_int(item["line_end"])
                and item["line_start"] <= item["line_end"]):
            raise Refused("evidence line_start and line_end must be integers with "
                          "1 <= start <= end")
        if not isinstance(item["span"], str) or not item["span"].strip():
            raise Refused("evidence span must be a non-empty string")
        checked.append({key: item[key] for key in MEMORY_EVIDENCE_KEYS})
    prior = obj.get("prior")
    if prior is not None:
        if not isinstance(prior, list) or len(prior) > MEMORY_PRIORS_MAX \
                or not all(isinstance(p, str) and MEMORY_ID.fullmatch(p) for p in prior) \
                or len(set(prior)) != len(prior):
            raise Refused(f"prior must list at most {MEMORY_PRIORS_MAX} distinct record ids "
                          "(m- and 16 hex characters)")
        prior = list(prior)
    return {"kind": obj["kind"], "text": text.strip(), "evidence": checked, "prior": prior}


def load_memory_proposals(path) -> tuple[list[dict], bool]:
    """(proposals, the file held one object). A file holds one proposal object, a JSON
    array of them, or JSON lines. Raises Refused when it cannot be read or any proposal is
    not a valid `jev-memory-proposal/v1`; nothing is reviewed then."""
    label = Path(path).name
    try:
        data = _read_regular(Path(path), MEMORY_PROPOSAL_MAX_BYTES, label)
        text = data.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise Refused(str(exc) if isinstance(exc, Refused) else
                      f"{label} cannot be read ({type(exc).__name__})") from None
    single = False
    try:
        whole = json.loads(text, object_pairs_hook=_unique_pairs)
    except ValueError:
        whole = None
        try:
            rows = [json.loads(line, object_pairs_hook=_unique_pairs)
                    for line in text.splitlines() if line.strip()]
        except ValueError:
            raise Refused(f"{label} is not JSON (one object, an array or JSON lines)") from None
    else:
        single = isinstance(whole, dict)
        rows = [whole] if single else whole
    if not isinstance(rows, list) or not 1 <= len(rows) <= MEMORY_PROPOSALS_MAX:
        raise Refused(f"{label} must hold 1 to {MEMORY_PROPOSALS_MAX} proposals")
    return [parse_memory_proposal(row) for row in rows], single


def _section_of(text: str, start: int, end: int) -> str:
    """The heading-bounded section (the notes' own line model) that holds lines
    start..end: from the nearest heading at or above `start` to the next heading of the
    same or a higher level. Without a heading: from after the frontmatter to the first
    heading. The frontmatter itself is never part of it."""
    from . import orchestrate
    lines = orchestrate.lines_of(text)
    top, level = 0, 0
    for index in range(min(start, len(lines)) - 1, -1, -1):
        match = _HEADING.match(lines[index])
        if match:
            top, level = index, len(match.group(1))
            break
    if not level:
        top = graphs.frontmatter_span(lines)
    bottom = len(lines)
    for index in range(min(end, len(lines)), len(lines)):
        match = _HEADING.match(lines[index])
        if match and len(match.group(1)) <= (level or 6):
            bottom = index
            break
    return "".join(lines[top:bottom]).strip()


def _memory_in_force(records: list[dict]) -> list[dict]:
    replaced: set = set()
    for item in records:
        target = item.get("supersedes")
        if isinstance(target, str):
            replaced.add(target)
        elif isinstance(target, list):
            replaced.update(t for t in target if isinstance(t, str))
    return [item for item in records if isinstance(item.get("id"), str)
            and item["id"] not in replaced]


def _record_sources(item: dict) -> list[dict]:
    return [s for s in item.get("sources") or []
            if isinstance(s, dict) and isinstance(s.get("path"), str)]


class _ChoiceCache(_Cache):
    """The advisor cache for choice answers: same folder, salt, permissions and time to
    live as `_Cache`; entries hold a label, probabilities and a confidence, never text."""

    def key(self, questionnaire, revision, provider: dict, pins: dict) -> str:
        material = {"v": 1, "feature": "memory", "questionnaire": questionnaire,
                    "template_revision": revision, "provider": provider,
                    "endpoint": _normal_endpoint(provider.get("base_url")), "pins": pins}
        return hmac.new(self.salt, _canonical(material).encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def get(self, key: str) -> dict | None:
        try:
            entry = json.loads(_read_regular(self.directory / f"{key}.json",
                                             CACHE_ENTRY_MAX_BYTES).decode("utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None
        if not isinstance(entry, dict) or entry.get("v") != 1:
            return None
        created, now = entry.get("created_at"), time.time()
        if not _number(created) or created > now + 60 or now - created > self.ttl:
            return None
        answer = _normal_choice(entry.get("answer"))
        if answer is None:
            return None
        return {"answer": answer, "model": _model_of(entry.get("model_reported"))}


def _normal_choice(answer, options=None) -> dict | None:
    """A choice answer reduced to {type, label, probabilities, confidence}; None when it
    is not one (or, given `options`, when the label is not among them)."""
    if not isinstance(answer, dict):
        return None
    label, confidence = answer.get("label"), answer.get("confidence")
    if not isinstance(label, str) or not _LABEL.fullmatch(label):
        return None
    if options is not None and label not in options:
        return None
    if confidence is not None and (not _number(confidence) or not 0 <= confidence <= 1):
        return None
    probabilities = answer.get("probabilities")
    if probabilities is not None:
        if not isinstance(probabilities, dict) or not 1 <= len(probabilities) <= 8 \
                or not all(isinstance(k, str) and _LABEL.fullmatch(k) and _number(v)
                           and 0 <= v <= 1 for k, v in probabilities.items()):
            return None
        probabilities = {k: float(v) for k, v in probabilities.items()}
    return {"type": "choice", "label": label, "probabilities": probabilities,
            "confidence": None if confidence is None else float(confidence)}


def _memory_answer_of(result, options) -> tuple[dict | None, str | None]:
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        return None, "answer_invalid"
    if not result["ok"]:
        code = result.get("code")
        return None, code if isinstance(code, str) and CODE.fullmatch(code) else "provider_error"
    answer = _normal_choice(result.get("answer"), options)
    return (answer, None) if answer is not None else (None, "answer_invalid")


@dataclass
class MemoryPlan:
    vault: Path
    mode: str                       # the mode in force: off, shadow or on
    why: str | None                 # why it is off
    config: Config
    routes_sha: str
    revision: str | None


def memory_plan(vault, environ=None) -> MemoryPlan:
    """What the advisor may do for `review-memory`: reads the configuration only and never
    imports the provider client. A kill switch, the child guard or a disabled feature on a
    valid configuration writes one counter row (mode off)."""
    vault = Path(vault).resolve()
    config = load_config(vault)
    mode, why = effective_mode(vault, config, environ, feature="memory")
    if mode == "off" and why in ("kill_switch", "child_guard", "feature_disabled"):
        append_log(vault, _row("memory", "off", code=why, applied=False, degraded=False,
                               provider_kind=(config.data.get("provider") or {}).get("kind")))
    return MemoryPlan(vault, mode, why, config, _file_digest(_ctx(vault) / "routes.json"),
                      config.revision)


class _MemoryReview:
    """One proposal: the mechanical review, then (shadow and on) the four questions."""

    def __init__(self, vault: Path, proposal: dict, plan: MemoryPlan, evaluate_fn,
                 load_key_fn, contracts, environ, notes: list | None):
        self.vault, self.proposal, self.plan = vault, proposal, plan
        self.cfg = plan.config.data if plan.config.valid else {}
        self.provider = dict(self.cfg.get("provider") or {})
        self.evaluate_fn, self.load_key_fn, self.contracts = evaluate_fn, load_key_fn, contracts
        self.env = os.environ if environ is None else environ
        self.notes = notes if notes is not None else []
        self.mode = plan.mode
        self.codes: list[str] = []
        self.discarded: str | None = None
        self.skipped: str | None = None
        self.partial = False
        self.questions: list[_Question] = []
        self.raw_pins: dict[str, str] = {}
        self.model: str | None = None
        self.usage = {"input_tokens": 0, "output_tokens": 0, "requests": 0}
        self.cost: float | None = None
        self.started = time.monotonic()
        self.texts: dict[str, str] = {}
        self.mechanical: dict = {}
        self.priors: list[dict] = []
        self.store_problem: str | None = None
        self.by_dimension: dict[str, _Question] = {}

    # -- the model-free part -------------------------------------------------
    def _mechanical(self) -> None:
        from . import orchestrate
        try:
            policy = _policy()
            prefixes = list(policy.load_exclusions(self.vault))
        except ValueError:
            raise Refused("routes.json cannot be read, so no source boundary can be "
                          "applied") from None
        cache: dict = {}
        found = []
        for index, item in enumerate(self.proposal["evidence"], 1):
            record = {"id": f"e{index}", "observation": item["span"],
                      "source_path": item["path"], "source_sha256": item["sha256"],
                      "line_start": item["line_start"], "line_end": item["line_end"],
                      "span": item["span"], "method": "", "uncertainty": ""}
            detail = orchestrate.check_record_detail(self.vault, record, ["."], prefixes, None,
                                                     cache)
            reasons = list(detail["reasons"])
            stale = any(r.startswith(("source_sha256 does not match", "source file missing"))
                        for r in reasons)
            found.append({"path": item["path"], "ok": bool(detail["mechanically_checked"]),
                          "stale": stale, "reasons": reasons})
            current = cache.get(item["path"])
            if current is not None:
                self.texts[item["path"]] = current[1]
        duplicate = None
        try:
            from . import memory as store
            records = store.load(self.vault)
        except Exception:                       # an unreadable store is reported, not fatal
            records, self.store_problem = [], "store_unreadable"
        else:
            force = _memory_in_force(records)
            pairs = {(e["path"], e["sha256"]) for e in self.proposal["evidence"]}
            ident = store.record_id(self.proposal["kind"], self.proposal["text"],
                                    [{"path": p, "sha256": s} for p, s in sorted(pairs)], None)
            for item in force:
                same = {(s.get("path"), s.get("sha256")) for s in _record_sources(item)} == pairs
                if item["id"] == ident or (item.get("kind") == self.proposal["kind"]
                                           and item.get("text") == self.proposal["text"]
                                           and same):
                    duplicate = item["id"]
                    break
            self.priors = self._choose_priors(force)
        self.mechanical = {"ok": all(f["ok"] for f in found) and not duplicate,
                           "evidence": found, "exact_duplicate_of": duplicate}

    def _choose_priors(self, force: list[dict]) -> list[dict]:
        wanted = self.proposal["prior"]
        by_id = {item["id"]: item for item in force}
        if wanted is not None:
            missing = [p for p in wanted if p not in by_id]
            if missing:
                raise Refused(f"prior {missing[0]} is not a record in force in this vault")
            return [by_id[p] for p in wanted]
        paths = {e["path"] for e in self.proposal["evidence"]}
        shared = [item for item in reversed(force)
                  if any(s["path"] in paths for s in _record_sources(item))]
        return shared[:MEMORY_PRIORS_MAX]

    # -- the four questions --------------------------------------------------
    def _note(self, code: str) -> None:
        if code not in self.codes:
            self.codes.append(code)

    def _contracts(self):
        if self.contracts is None:
            try:
                from . import jev_contracts
            except ImportError:
                raise _Degrade("advisor_unavailable") from None
            self.contracts = jev_contracts
        return self.contracts

    def _advise(self) -> None:
        cfg = self.cfg
        contracts = self._contracts()
        revision = getattr(contracts, "TEMPLATE_REVISION", None)
        policy = _policy()
        prefixes = tuple(policy.load_exclusions(self.vault))
        local = tuple(cfg["local_only_prefixes"])
        pins = {e["path"]: e["sha256"] for e in self.proposal["evidence"]}
        for path, sha in pins.items():                    # gates 1-3 on the evidence
            if _assess(self.vault, path, sha, prefixes, local, policy):
                self.skipped = "local_only"
                return
        views = []
        for item in self.proposal["evidence"]:
            section = _section_of(self.texts.get(item["path"], ""), item["line_start"],
                                  item["line_end"])
            view = f"Passage:\n{item['span']}\n\nSection it comes from:\n{section or item['span']}"
            if len(view) > MEMORY_TEXT_CHARS:
                self.skipped = "context_incomplete"      # never cut: the judge sees it whole or not
                return
            views.append(view)
        text = self.proposal["text"]
        for name, template in MEMORY_TEMPLATES:
            question = _Question(name, -1, dict(pins), template)
            question.state = {"proposal": text, "evidence": list(views)}
            self.questions.append(question)
            self.by_dimension[name] = question
        for index, prior in enumerate(self.priors):
            question = _Question("relation", index, {}, MEMORY_RELATION_TEMPLATE)
            body = prior.get("text")
            if not isinstance(body, str) or not body.strip() or len(body) > MEMORY_TEXT_CHARS:
                question.verdict = "context_incomplete"
            else:
                reason = self._prior_private(prior, question, policy, prefixes, local)
                if reason:
                    question.verdict = "local_only"
                else:
                    question.state = {"proposal": text, "prior": body}
            self.questions.append(question)
        pending = [q for q in self.questions if q.state is not None]
        for question in pending:
            try:
                question.questionnaire = contracts.build_questionnaire(
                    self._purpose(question), question.template, question.state)
            except Exception as exc:
                if str(exc) == "state_too_large":
                    self.skipped = "context_incomplete"
                    return
                raise _Degrade("contract_error") from None
            if not isinstance(question.questionnaire, dict):
                raise _Degrade("contract_error")
        try:
            literals = load_blocklist(cfg["blocklist_file"])
        except Refused:
            raise _Degrade("blocklist_unreadable") from None
        held = [q for q in pending if secret_hit(q.questionnaire, literals)]
        for question in held:                              # gate 4: only the questions it hits
            question.questionnaire, question.state = None, None
            question.verdict = "sensitive"
        if held:
            self._note("sensitive_input")
            if len(held) == len(pending):
                raise _Degrade("sensitive_input")
        pending = [q for q in pending if q.questionnaire is not None]
        for question in pending:                           # gate 6: bounded, never cut
            if len(_canonical(question.questionnaire)) > cfg["max_input_chars"]:
                question.questionnaire = None
                self._note("budget_exceeded")
        ask = [q for q in pending if q.questionnaire is not None][:cfg["max_requests"]]
        for question in pending[cfg["max_requests"]:]:
            question.questionnaire = None
            self._note("request_cap")
        if not ask:
            return
        cache = _ChoiceCache.open(self.vault, cfg["cache_ttl_s"])
        if cache is not None:
            for question in ask:
                question.key = cache.key(question.questionnaire, revision, self.provider,
                                         question.paths)
                hit = cache.get(question.key)
                options = tuple(question.questionnaire["question"]["criteria"])
                if hit is not None and hit["answer"]["label"] in options:
                    question.answer, question.model, question.cached = \
                        hit["answer"], hit["model"], True
        misses = [q for q in ask if not q.cached]
        if misses:
            self._evaluate(misses)
        self._fresh_after(policy)
        if cache is not None:
            for question in misses:
                if question.answer is not None and question.key:
                    cache.put(question.key, question.answer, question.model, question.usage)
        for question in self.questions:
            if question.answer is not None and self.model is None and question.model:
                self.model = question.model

    @staticmethod
    def _purpose(question: _Question) -> str:
        return "memory_" + question.kind

    def _prior_private(self, prior, question, policy, prefixes, local) -> str | None:
        """Why a prior record's sources keep it local (never sent), or None. A source that
        cannot be read, or has left the boundaries, counts as local: fail closed."""
        for source in _record_sources(prior):
            name = source["path"]
            try:
                if local and policy.excluded(name, local):
                    return "local_only_prefix"
                raw = policy.source_path(self.vault, name, prefixes).read_bytes()
                text = raw.decode("utf-8")
            except (OSError, ValueError, UnicodeError):
                return "source_unreadable"
            reason = local_only_reason(text)
            if reason:
                return reason
            question.paths[name] = hashlib.sha256(raw).hexdigest()
        return None

    def _evaluate(self, misses: list[_Question]) -> None:
        cfg = self.cfg
        if kill_switch(self.vault, self.env):
            raise _Degrade("kill_switch")
        if load_config(self.vault).revision != self.plan.revision:
            raise _Degrade("config_changed")
        if self.evaluate_fn is None:
            try:
                from . import jev_client
            except ImportError:
                raise _Degrade("advisor_unavailable") from None
            self.evaluate_fn = jev_client.evaluate
            if self.load_key_fn is None:
                self.load_key_fn = jev_client.load_key
        key = None
        if self.load_key_fn is not None and _needs_key(self.provider):
            try:
                key = self.load_key_fn(self.provider, cfg["env_file"], vault=self.vault)
            except Exception:
                raise _Degrade("key_unavailable") from None
        results = _call_with_deadline(
            self.evaluate_fn, (self.provider, [q.questionnaire for q in misses]),
            {"deadline_s": cfg["timeout_s"], "max_parallel": cfg["max_parallel"], "key": key},
            cfg["timeout_s"])
        key = None
        if not isinstance(results, list) or len(results) != len(misses):
            raise _Degrade("answers_invalid")
        failures = []
        for question, result in zip(misses, results):
            question.usage = _usage_of(result)
            for name in ("input_tokens", "output_tokens", "requests"):
                self.usage[name] += question.usage.get(name, 0)
            if "cost_usd" in question.usage:
                self.cost = (self.cost or 0.0) + question.usage["cost_usd"]
            options = tuple(question.questionnaire["question"]["criteria"])
            answer, code = _memory_answer_of(result, options)
            if answer is None:
                failures.append(code)
                continue
            question.answer = answer
            question.model = _model_of(result.get("model_reported"))
        if failures and len(failures) == len(misses) \
                and not any(q.cached for q in self.questions):
            raise _Degrade(failures[0])
        for code in failures:
            self.partial = True
            self._note(code)

    def _fresh_after(self, policy) -> None:
        """After the provider answered and before anything is shown: the same
        configuration and routes.json, no kill switch, every pinned source in the same
        bytes, the same mechanical result, and every prior record still in force with
        the same text. Any difference discards all advice."""
        now = load_config(self.vault)
        if not now.valid or now.revision != self.plan.revision:
            raise _Degrade("config_changed")
        if kill_switch(self.vault, self.env):
            raise _Degrade("kill_switch")
        if _file_digest(_ctx(self.vault) / "routes.json") != self.plan.routes_sha:
            raise _Degrade("config_changed")
        try:
            prefixes = tuple(policy.load_exclusions(self.vault))
        except ValueError:
            raise _Degrade("config_changed") from None
        pins: dict[str, str] = {}
        for question in self.questions:
            pins.update(question.paths)
        for path, sha in sorted(pins.items()):
            try:
                raw = policy.source_path(self.vault, path, prefixes).read_bytes()
            except (OSError, ValueError):
                raise _Degrade("source_changed") from None
            if hashlib.sha256(raw).hexdigest() != sha:
                raise _Degrade("source_changed")
        before = (self.mechanical, [(p["id"], p.get("text")) for p in self.priors])
        again = _MemoryReview(self.vault, self.proposal, self.plan, None, None, None, self.env,
                              None)
        try:
            again._mechanical()
        except Refused:
            raise _Degrade("source_changed") from None
        if (again.mechanical, [(p["id"], p.get("text")) for p in again.priors]) != before:
            raise _Degrade("source_changed")

    # -- decision and output -------------------------------------------------
    def _confident(self, answer: dict) -> bool:
        confidence = answer.get("confidence")
        # A label-only answer counts only here: the report is shown only under a
        # calibration receipt, whose bars were measured on label-only answers too.
        return True if confidence is None else confidence >= self.cfg["thresholds"]["confidence"]

    def _decide(self) -> tuple[str, list[str]]:
        reasons: list[str] = []
        expected = {"support": "supports", "commitment": "asserted"}
        for name, _ in MEMORY_TEMPLATES:
            question = self.by_dimension.get(name)
            answer = question.answer if question is not None else None
            if answer is None:
                reasons.append(f"{name}_not_judged")
                continue
            if not self._confident(answer):
                reasons.append(f"{name}_low_confidence")
            if name in expected and answer["label"] != expected[name]:
                reasons.append(f"{name}_not_{expected[name]}")
            if name == "kind" and answer["label"] in MEMORY_NON_RECORD_KINDS:
                reasons.append("kind_not_a_record")
        for question in self.questions:
            if question.kind != "relation":
                continue
            answer = question.answer
            if answer is None:
                reasons.append("prior_not_judged")
                continue
            if not self._confident(answer):
                reasons.append("relation_low_confidence")
            if answer["label"] == "contradicts":
                reasons.append("relation_contradicts")
        reasons = list(dict.fromkeys(reasons))
        return ("inspect_sources" if reasons else "candidate"), reasons

    def _answers(self) -> dict:
        def shown(question):
            answer = question.answer if question is not None else None
            if answer is None:
                return {"judged": False, "label": None, "confidence": None, "confident": None,
                        "why": question.verdict if question is not None
                        and question.verdict not in ("not_judged",) else None}
            confidence = answer["confidence"]
            return {"judged": True, "label": answer["label"],
                    "confidence": None if confidence is None else round(confidence, 4),
                    "confident": self._confident(answer), "cached": question.cached}
        out = {name: shown(self.by_dimension.get(name)) for name, _ in MEMORY_TEMPLATES}
        out["relations"] = [
            {"prior": self.priors[q.index]["id"], **shown(q)}
            for q in self.questions if q.kind == "relation"]
        return out

    def _suggestions(self, route: str) -> dict:
        supersedes, duplicates = [], []
        for question in self.questions:
            answer = question.answer
            if question.kind != "relation" or answer is None or not self._confident(answer):
                continue
            prior = self.priors[question.index]["id"]
            if answer["label"] == "replaces":
                supersedes.append(prior)
            elif answer["label"] == "duplicate":
                duplicates.append(prior)
        command = None
        if route == "candidate" and not duplicates:
            import shlex
            argv = ["context-layer", "memory", "add", "<vault>", "--kind",
                    self.proposal["kind"], "--state", "draft", "--text", self.proposal["text"]]
            for path, sha in sorted({(e["path"], e["sha256"])
                                     for e in self.proposal["evidence"]}):
                argv += ["--source", f"{path}@{sha}"]
            for prior in supersedes:
                argv += ["--supersedes", prior]
            command = " ".join(a if a == "<vault>" else shlex.quote(a) for a in argv)
        return {"suggested_supersedes": supersedes, "semantic_duplicate_of": duplicates,
                "suggested_command": command}

    def _block(self) -> dict:
        judged = [q for q in self.questions if q.answer is not None]
        asked = [q for q in self.questions if q.state is not None or q.answer is not None]
        return {
            "schema": ADVICE_SCHEMA, "feature": "memory", "mode": "on", "applied": True,
            "provider_kind": self.provider.get("kind"), "model_reported": self.model,
            "skipped": self.skipped, "answers": self._answers(),
            "confidence_threshold": self.cfg["thresholds"]["confidence"],
            "counts": {"questions": len(asked), "judged": len(judged),
                       "local_only": sum(1 for q in self.questions if q.verdict == "local_only"),
                       "cache_hits": sum(1 for q in self.questions if q.cached)},
            "degraded": self.partial, "codes": list(self.codes),
            "latency_ms": int(round((time.monotonic() - self.started) * 1000)),
            "requests": self.usage["requests"], "input_tokens": self.usage["input_tokens"],
            "notice": MEMORY_NOTICE,
        }

    def run(self) -> dict:
        self._mechanical()
        mech = self.mechanical
        fallback = []
        if not all(f["ok"] for f in mech["evidence"]):
            fallback.append("mechanical_failed")
        if mech["exact_duplicate_of"]:
            fallback.append("exact_duplicate")
        decided = None
        if self.mode == "on":
            try:                                   # `on` needs a receipt; else it is shadow
                revision = getattr(self._contracts(), "TEMPLATE_REVISION", None)
                _, problem = check_receipt(self.vault, self.provider, self.cfg["thresholds"],
                                           ["memory"],
                                           revision if isinstance(revision, str) else "missing")
            except _Degrade as exc:
                self.discarded, problem = exc.code, None
            if problem:
                self.mode = "shadow"
                self._note("calibration_required")
                self.notes.append("calibration_required: " + problem
                                  + "; the answers were counted, not shown")
        if self.mode in ("shadow", "on") and not self.discarded:
            if fallback:
                self.skipped = fallback[0]
            else:
                try:
                    self._advise()
                except _Degrade as exc:
                    self.discarded = exc.code
                except Exception:                  # advice must never break a review
                    self.discarded = "internal_error"
        if self.discarded:
            self.notes.append(f"advisor not applied: {self.discarded}; this is the mechanical "
                              "review only")
            for question in self.questions:
                question.answer = None
            self.skipped = None
        elif self.mode in ("shadow", "on") and not self.skipped:
            decided = self._decide()
        shown = self.mode == "on" and not self.discarded
        route, reasons = "inspect_sources", fallback + ["advisor_not_applied"]
        advisor = None
        suggestions = {"suggested_supersedes": [], "semantic_duplicate_of": [],
                       "suggested_command": None}
        if shown:
            if decided is not None:
                route, reasons = decided
                suggestions = self._suggestions(route)
            else:
                reasons = fallback if self.skipped in fallback else fallback + [self.skipped]
            advisor = self._block()
        if self.mode in ("shadow", "on") or self.discarded:
            self._log(decided[0] if decided else None, shown)
        report = {
            "schema": MEMORY_REVIEW_SCHEMA, "route": route, "reasons": reasons,
            "approved": False, "memory_written": False,
            "proposal": {"kind": self.proposal["kind"], "text_chars": len(self.proposal["text"]),
                         "evidence": len(self.proposal["evidence"])},
            "mechanical": mech,
            "priors": [p["id"] for p in self.priors],
            **suggestions, "advisor": advisor,
        }
        if self.store_problem:
            report["memory_store"] = self.store_problem
        return report

    def _log(self, route, shown: bool) -> None:
        """One counters-only row. A proposal that was not asked (a mechanical failure, a
        local-only source, a view that does not fit) is a row of mode off with its code."""
        judged = [q for q in self.questions if q.answer is not None]
        code = self.discarded or self.skipped or (self.codes[0] if self.codes else None)
        model = (self.model or "").lower() or None
        append_log(self.vault, _row(
            "memory", "off" if self.skipped else self.mode,
            applied=shown and not self.skipped and not self.discarded,
            provider_kind=self.provider.get("kind"), model_id=model,
            cache_hit=bool(judged) and all(q.cached for q in judged),
            degraded=bool(self.discarded) or self.partial, code=code,
            latency_ms=int(round((time.monotonic() - self.started) * 1000)),
            requests=self.usage["requests"], input_tokens=self.usage["input_tokens"],
            output_tokens=self.usage["output_tokens"], cost_usd=self.cost,
            candidates=sum(1 for q in self.questions if q.state is not None or q.answer),
            judged=len(judged),
            local_only=sum(1 for q in self.questions if q.verdict == "local_only"),
            flagged=int(route == "inspect_sources")))


def review_memory(vault, proposal: dict, plan: MemoryPlan, *, evaluate_fn=None,
                  load_key_fn=None, contracts=None, environ=None,
                  notes: list | None = None) -> dict:
    """The `jev-memory-review/v1` report for one proposal (checked by `parse_memory_proposal`,
    which raises Refused for a malformed one).
    Off: the mechanical review only, no call. Shadow: the same report byte for byte (the
    four questions are asked, counted in the call log and not shown). On (needs a
    calibration receipt, else it is shadow): the report adds the `advisor` block and the
    route the answers give. The memory store is only read; the report always carries
    `approved: false` and `memory_written: false`. Any failure leaves the mechanical
    review and one counter row. `notes` receives one line per reason the advisor did not
    show its answers (for stderr). Raises Refused for an input the vault contradicts (a
    prior id that is not in force, routes.json unreadable)."""
    return _MemoryReview(Path(vault).resolve(), parse_memory_proposal(proposal), plan,
                         evaluate_fn, load_key_fn, contracts, environ, notes).run()


def render_memory_review(report: dict) -> str:
    lines = [f"route: {report['route']}", "reasons: " + ", ".join(report["reasons"])]
    mech = report["mechanical"]
    lines.append("mechanical: " + ("ok" if mech["ok"] else "problems"))
    for item in mech["evidence"]:
        if not item["ok"]:
            lines.append(f"  {item['path']}: " + "; ".join(item["reasons"]))
    if mech["exact_duplicate_of"]:
        lines.append(f"  exact duplicate of {mech['exact_duplicate_of']} (in force)")
    advisor = report["advisor"]
    if advisor is None:
        lines.append("advisor: no answers shown (off, shadow, or not applied)")
    else:
        answers = advisor["answers"]
        for name, _ in MEMORY_TEMPLATES:
            item = answers[name]
            lines.append(f"advisor {name}: " + (
                f"{item['label']} (confidence {item['confidence']})" if item["judged"]
                else "not judged"))
        for item in answers["relations"]:
            lines.append(f"advisor relation to {item['prior']}: " + (
                f"{item['label']} (confidence {item['confidence']})" if item["judged"]
                else "not judged"))
        lines.append(advisor["notice"])
    for label, key in (("suggested supersedes", "suggested_supersedes"),
                       ("semantic duplicate of", "semantic_duplicate_of")):
        if report[key]:
            lines.append(f"{label}: {', '.join(report[key])}")
    if report["suggested_command"]:
        lines.append("a person could run (nothing was run): " + report["suggested_command"])
    lines.append("approved: false; memory_written: false")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------

def purge_targets(vault, all_files: bool = False, receipts: bool = False) -> list[Path]:
    """What `jev purge` removes: the cache, its salt and stray temp files; with --all also
    the call log; with --receipts also calibration receipts and recordings. Never the
    configuration or the kill switch file."""
    ctx = _ctx(vault)
    names = [CACHE_NAME, SALT_NAME]
    if all_files:
        names.append(LOG_NAME)
    if receipts:
        names += [CALIBRATION_NAME, RECORDINGS_NAME]
    targets = [ctx / name for name in names if os.path.lexists(ctx / name)]
    if ctx.is_dir() and not ctx.is_symlink():
        targets += sorted(p for p in ctx.iterdir()
                          if p.name.startswith((".jev-", ".jev-calls-")) and p.name.endswith(".tmp"))
    return targets


def _remove(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)



# ---------------------------------------------------------------------------
# Recording and calibration (development runs; `on` needs a receipt)
# ---------------------------------------------------------------------------
# `jev record` asks the configured provider every question of a file and keeps the
# validated answers as `jev-recording/v1` rows: hashes, labels, numbers and counters,
# never text. `jev calibrate` reads the dev-set evaluation of such a recording
# (tests/jev_dev_eval.py --provider recorded:FILE --json) and writes the
# `jev-calibration/v1` receipt that `on` requires, with `passed` per question purpose.
# The bars below were written before the first recording (audit F section 8) and are
# proposals adopted as the release rule, not measurements; positives_min is 12, not the
# proposed 20, because the development set holds 16 answer notes to rescue (the oracle
# plumbing run judged 16 positives and 131 negatives).

REPORT_SCHEMA = "jev-dev-eval/v1"
REPORT_MAX_BYTES = 16 * 1024 * 1024
QUESTIONS_MAX_BYTES = 64 * 1024 * 1024
RECORDING_MAX_BYTES = 64 * 1024 * 1024
RECORD_DEADLINE_DEFAULT = 60.0
RECORD_DEADLINE_MAX = 600.0
BARS = {
    "relevance": {"precision_min": 0.85, "positives_min": 12, "negatives_min": 20,
                  "injection_not_above_neutral": True},
    "topicality": {"not_topical_share_min": 0.9, "topical_share_min": 0.95},
    "claim_support": {"clear_accuracy_min": 0.9, "cancelled_plan_wrong_clear_max": 0,
                      "cancelled_plan_supported_max": 0},
    "memory_support": {"accuracy_min": 0.8},
    "memory_commitment": {"accuracy_min": 0.8},
    "memory_kind": {"accuracy_min": 0.8},
    "memory_relation": {"accuracy_min": 0.8},
}
MEMORY_DIMENSIONS = (("support", "memory_support"), ("commitment", "memory_commitment"),
                     ("kind", "memory_kind"), ("relation", "memory_relation"))


def _contracts():
    try:
        from . import jev_contracts
    except ImportError:
        raise Refused("the question contracts are not part of this build") from None
    return jev_contracts


def _client():
    try:
        from . import jev_client
    except ImportError:
        raise Refused("the provider client is not part of this build") from None
    return jev_client


def _json_rows(data: bytes, label: str) -> list:
    """A JSON array, or one JSON object per line."""
    text = data.decode("utf-8")
    stripped = text.strip()
    if stripped.startswith("["):
        rows = json.loads(stripped, object_pairs_hook=_unique_pairs)
        if not isinstance(rows, list):
            raise Refused(f"{label} is not a JSON array")
        return rows
    rows = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line, object_pairs_hook=_unique_pairs))
        except ValueError as exc:
            raise Refused(f"{label} line {number} is not JSON ({exc})") from None
    return rows


def load_questions(path: Path, contracts) -> list[dict]:
    """The `jev-questionnaire/v1` objects of a file (JSON array or JSON lines), each
    checked against the installed contracts. Refused on the first bad one."""
    shown = path.name
    try:
        rows = _json_rows(_read_regular(path, QUESTIONS_MAX_BYTES, shown), shown)
    except (OSError, UnicodeError, ValueError) as exc:
        raise Refused(f"{shown} cannot be read ({exc if isinstance(exc, Refused) else type(exc).__name__})") from None
    if not rows:
        raise Refused(f"{shown} holds no question")
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise Refused(f"{shown}: question {number} is not an object")
        try:
            contracts.check_questionnaire(row)
        except Exception as exc:                        # ContractError, TypeError, ...
            raise Refused(f"{shown}: question {number} is not a valid questionnaire "
                          f"({type(exc).__name__}: {exc})") from None
    return rows


@dataclass
class RecordPlan:
    vault: Path
    config: Config
    provider: dict
    questions: list
    templates: dict
    input_chars: int


def record_plan(vault, questions_path, environ=None) -> RecordPlan:
    """Everything a recording needs, checked before any byte could leave: a valid
    configuration with a live provider, no kill switch, every question a valid
    questionnaire, none holding a credential, none over `max_input_chars`."""
    vault = Path(vault).resolve()
    environ = os.environ if environ is None else environ
    config = load_config(vault)
    if not config.present:
        raise Refused("the advisor is not configured (.context/jev.json is absent); run "
                      "`context-layer jev shadow <vault> --provider-kind ...` first")
    if not config.valid:
        raise Refused(f".context/jev.json is invalid ({config.problem})")
    if _set(environ, CHILD_ENV):
        raise Refused("running inside an advisor call (child guard)")
    if kill_switch(vault, environ):
        raise Refused("a kill switch is set (CONTEXT_LAYER_JEV_DISABLE or .context/jev.disabled)")
    provider = dict(config.data.get("provider") or {})
    if not provider:
        raise Refused("no provider is configured")
    if provider.get("kind") == "recorded":
        raise Refused("the recorded provider replays a recording; it cannot make one")
    contracts = _contracts()
    questions = load_questions(Path(questions_path), contracts)
    literals = load_blocklist(config.data["blocklist_file"])
    templates: dict = {}
    total = 0
    for number, question in enumerate(questions, 1):
        if secret_hit(question, literals):
            raise Refused(f"question {number} appears to hold a credential; nothing was sent")
        size = len(_canonical(question))
        if size > config.data["max_input_chars"]:
            raise Refused(f"question {number} is {size} characters, over max_input_chars "
                          f"{config.data['max_input_chars']}; nothing was sent")
        total += size
        template = str(question.get("template", "")).split("@")[0]
        templates[template] = templates.get(template, 0) + 1
    return RecordPlan(vault, config, provider, questions, templates, total)


def _open_new(path: Path, append: bool):
    """A 0600 file for recording rows: new (refused when it exists) or appended to."""
    if path.is_symlink():
        raise Refused(f"{path.name} is a symlink")
    if not append and os.path.lexists(path):
        raise Refused(f"{path.name} exists; add --append to add rows to it")
    if append and os.path.lexists(path) and not path.is_file():
        raise Refused(f"{path.name} is not a regular file")
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    flags |= os.O_APPEND if append else os.O_EXCL
    handle = os.open(path, flags, 0o600)
    return os.fdopen(handle, "a", encoding="utf-8")


def record(plan: RecordPlan, out: Path, *, append: bool, max_requests: int, max_parallel: int,
           deadline_s: float, environ=None) -> dict:
    """Ask the provider in batches and append one `jev-recording/v1` row per question to
    `out`. Stops early on a kill switch (rows so far are kept). Returns counters."""
    environ = os.environ if environ is None else environ
    client = _client()
    key = None
    if _needs_key(plan.provider):
        try:
            key = client.load_key(plan.provider, plan.config.data["env_file"], vault=plan.vault)
        except Exception as exc:
            raise Refused(f"the provider key cannot be loaded ({type(exc).__name__})") from None
    counters = {"questions": len(plan.questions), "rows": 0, "answered": 0, "failed": 0,
                "codes": {}, "input_tokens": 0, "output_tokens": 0, "cost_usd": None,
                "stopped": None, "batches": 0}
    started = time.monotonic()
    with _open_new(out, append) as sink:
        for begin in range(0, len(plan.questions), max_requests):
            if kill_switch(plan.vault, environ) or _set(environ, CHILD_ENV):
                counters["stopped"] = "kill_switch"
                break
            if load_config(plan.vault).revision != plan.config.revision:
                counters["stopped"] = "config_changed"
                break
            batch = plan.questions[begin:begin + max_requests]
            rows: list = []
            results = client.evaluate(plan.provider, batch, deadline_s=deadline_s,
                                      max_parallel=max_parallel, key=key, capture=rows)
            counters["batches"] += 1
            for result in results:
                usage = result.get("usage") if isinstance(result, dict) else None
                if isinstance(usage, dict):
                    for name in ("input_tokens", "output_tokens"):
                        if _number(usage.get(name)):
                            counters[name] += int(usage[name])
                    if _number(usage.get("cost_usd")):
                        counters["cost_usd"] = (counters["cost_usd"] or 0.0) + usage["cost_usd"]
                if isinstance(result, dict) and result.get("ok"):
                    counters["answered"] += 1
                else:
                    code = (result.get("code") if isinstance(result, dict) else None) or "unknown"
                    counters["failed"] += 1
                    counters["codes"][code] = counters["codes"].get(code, 0) + 1
            for row in rows:
                sink.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
            counters["rows"] += len(rows)
            sink.flush()
    key = None
    counters["latency_ms"] = int(round((time.monotonic() - started) * 1000))
    first_code = next(iter(counters["codes"]), None)
    append_log(plan.vault, _row(
        "record", "run", applied=False, provider_kind=plan.provider.get("kind"),
        model_id=(str(plan.provider.get("model") or "").lower() or None),
        cache_hit=False, degraded=counters["failed"] > 0 or counters["stopped"] is not None,
        code=counters["stopped"] or first_code, latency_ms=counters["latency_ms"],
        requests=counters["answered"] + counters["failed"],
        input_tokens=counters["input_tokens"], output_tokens=counters["output_tokens"],
        cost_usd=counters["cost_usd"], judged=counters["answered"]))
    return counters


def load_recording_rows(path: Path) -> list[dict]:
    """Every `jev-recording/v1` row of a recording file; Refused when unreadable."""
    shown = path.name
    try:
        rows = _json_rows(_read_regular(path, RECORDING_MAX_BYTES, shown), shown)
    except (OSError, UnicodeError, ValueError) as exc:
        raise Refused(f"{shown} cannot be read ({exc if isinstance(exc, Refused) else type(exc).__name__})") from None
    kept = [row for row in rows if isinstance(row, dict)
            and row.get("contract") == "jev-recording/v1" and isinstance(row.get("key"), str)]
    if not kept:
        raise Refused(f"{shown} holds no jev-recording/v1 row")
    return kept


def load_report(path: Path) -> dict:
    """A `jev-dev-eval/v1` report as tests/jev_dev_eval.py prints it with --json."""
    shown = path.name
    try:
        report = json.loads(_read_regular(path, REPORT_MAX_BYTES, shown).decode("utf-8"),
                            object_pairs_hook=_unique_pairs)
    except (OSError, UnicodeError, ValueError) as exc:
        raise Refused(f"{shown} cannot be read ({exc if isinstance(exc, Refused) else type(exc).__name__})") from None
    if not isinstance(report, dict) or report.get("schema") != REPORT_SCHEMA:
        raise Refused(f"{shown} is not a {REPORT_SCHEMA} report")
    return report


def _at_least(value, minimum) -> bool:
    return _number(value) and value >= minimum


def _at_most(value, maximum) -> bool:
    return _number(value) and value <= maximum


def _metric(section, key):
    return section.get(key) if isinstance(section, dict) else None


def bars_met(report: dict) -> dict:
    """`purposes` for a receipt: per question purpose, `passed` (a real boolean) and the
    numbers it rests on, taken from the evaluation report."""
    purposes: dict = {}
    rel = report.get("relevance") if isinstance(report.get("relevance"), dict) else {}
    injection = rel.get("injection_vs_neutral") if isinstance(rel.get("injection_vs_neutral"), dict) else {}
    bar = BARS["relevance"]
    purposes["relevance"] = {
        "passed": bool(_at_least(rel.get("precision"), bar["precision_min"])
                       and _at_least(rel.get("positives_judged"), bar["positives_min"])
                       and _at_least(rel.get("negatives_judged"), bar["negatives_min"])
                       and injection.get("injection_not_above_neutral") is True),
        "precision": rel.get("precision"), "recall": rel.get("recall"),
        "positives": rel.get("positives_judged"), "negatives": rel.get("negatives_judged"),
        "injection_rate": injection.get("injection_rate"),
        "neutral_rate": injection.get("neutral_rate"),
        "injection_not_above_neutral": injection.get("injection_not_above_neutral")}
    gate = report.get("gate") if isinstance(report.get("gate"), dict) else {}
    bar = BARS["topicality"]
    purposes["topicality"] = {
        "passed": bool(_at_least(gate.get("judged"), 1)
                       and gate.get("judged") == gate.get("prompts")
                       and _at_least(gate.get("not_topical_share"), bar["not_topical_share_min"])
                       and _at_least(gate.get("topical_share"), bar["topical_share_min"])),
        "judged": gate.get("judged"), "prompts": gate.get("prompts"),
        "not_topical_share": gate.get("not_topical_share"),
        "topical_share": gate.get("topical_share")}
    claims = report.get("claims") if isinstance(report.get("claims"), dict) else {}
    bar = BARS["claim_support"]
    purposes["claim_support"] = {
        "passed": bool(_at_least(claims.get("judged"), 1)
                       and claims.get("judged") == claims.get("claims")
                       and _at_least(claims.get("clear_accuracy"), bar["clear_accuracy_min"])
                       and _at_most(claims.get("cancelled_plan_wrong_clear"),
                                    bar["cancelled_plan_wrong_clear_max"])
                       and _at_most(claims.get("cancelled_plan_supported"),
                                    bar["cancelled_plan_supported_max"])),
        "judged": claims.get("judged"), "claims": claims.get("claims"),
        "clear_accuracy": claims.get("clear_accuracy"),
        "cancelled_plan_wrong_clear": claims.get("cancelled_plan_wrong_clear"),
        "cancelled_plan_supported": claims.get("cancelled_plan_supported")}
    memory = report.get("memory") if isinstance(report.get("memory"), dict) else {}
    for dimension, purpose in MEMORY_DIMENSIONS:
        entry = memory.get(dimension) if isinstance(memory.get(dimension), dict) else {}
        purposes[purpose] = {
            "passed": bool(_at_least(entry.get("judged"), 1)
                           and entry.get("judged") == memory.get("proposals")
                           and _at_least(entry.get("accuracy"),
                                         BARS[purpose]["accuracy_min"])),
            "judged": entry.get("judged"), "proposals": memory.get("proposals"),
            "accuracy": entry.get("accuracy")}
    return purposes


def calibrate(vault, report_path, recording_path, environ=None) -> tuple[dict, list[str]]:
    """(receipt, purposes the enabled and wired features need but the bars refuse).
    Raises Refused when no receipt can be made at all: no usable configuration or
    provider, a report that is not a recorded-provider evaluation, a privacy or
    integrity failure, other thresholds, a recording for another provider or made with
    other question templates."""
    vault = Path(vault).resolve()
    config = load_config(vault)
    if not config.present:
        raise Refused("the advisor is not configured (.context/jev.json is absent)")
    if not config.valid:
        raise Refused(f".context/jev.json is invalid ({config.problem})")
    provider = dict(config.data.get("provider") or {})
    if not provider:
        raise Refused("no provider is configured")
    contracts = _contracts()
    report = load_report(Path(report_path))
    if report.get("status") != "evaluated":
        raise Refused(f"the report's status is {report.get('status')!r}, not 'evaluated'")
    if report.get("provider") != "recorded":
        raise Refused("a receipt needs an evaluation of a recording (--provider recorded:FILE); "
                      f"this report's provider is {report.get('provider')!r}, which proves "
                      "plumbing, not quality")
    for section in ("privacy", "integrity"):
        if _metric(report.get(section), "ok") is not True:
            raise Refused(f"the report's {section} check did not pass; no receipt")
    thresholds = report.get("thresholds") if isinstance(report.get("thresholds"), dict) else {}
    for key in THRESHOLD_KEYS:
        value = thresholds.get(key)
        if not _number(value) or abs(value - config.data["thresholds"][key]) > 1e-9:
            raise Refused(f"the report used thresholds.{key} {value!r}, the configuration has "
                          f"{config.data['thresholds'][key]}; evaluate with the same thresholds")
    dev_sha = report.get("dev_set_sha256")
    if not isinstance(dev_sha, str) or not SHA.fullmatch(dev_sha):
        raise Refused("the report names no dev_set_sha256")
    rows = load_recording_rows(Path(recording_path))
    revisions = set()
    for row in rows:
        named = row.get("provider") if isinstance(row.get("provider"), dict) else {}
        if named.get("kind") != provider.get("kind") or named.get("model") != provider.get("model"):
            raise Refused("the recording was made with another provider or model "
                          f"({named.get('kind')}/{named.get('model')} against the configured "
                          f"{provider.get('kind')}/{provider.get('model')})")
        template = row.get("template")
        if isinstance(template, str) and "@" in template:
            revisions.add(template.split("@", 1)[1])
    if len(revisions) != 1:
        raise Refused("the recording's rows do not name one question-template revision")
    revision = revisions.pop()
    if revision != contracts.TEMPLATE_REVISION:
        raise Refused("the recording was made with other question templates than the installed "
                      "contracts; record again")
    recorded = report.get("recording") if isinstance(report.get("recording"), dict) else {}
    if recorded.get("rows") != len(rows):
        raise Refused(f"the report evaluated a recording of {recorded.get('rows')} rows; this "
                      f"file holds {len(rows)}")
    purposes = bars_met(report)
    needed = sorted({p for f in config.data["features"] if WIRED.get(f)
                     for p in PURPOSES.get(f, ())})
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provider": {"kind": provider.get("kind"), "model": provider.get("model")},
        "template_revision": revision,
        "thresholds": {k: config.data["thresholds"][k] for k in THRESHOLD_KEYS},
        "dev_set_sha256": dev_sha, "dev_set_version": report.get("dev_set_version"),
        "report_sha256": _file_digest(Path(report_path)),
        "recording_sha256": _file_digest(Path(recording_path)),
        "recording_rows": len(rows),
        "questionnaires_found": recorded.get("found"),
        "questionnaires": recorded.get("questionnaires"),
        "purposes": purposes, "bars": BARS, "needed_now": needed,
        "note": "bars written before the first recording (proposals adopted as the rule); a "
                "receipt is necessary, not sufficient: a fictional development set is not "
                "your vault",
    }
    missing = [p for p in needed if not purposes.get(p, {}).get("passed")]
    return receipt, missing


def write_receipt(vault, provider: dict, receipt: dict) -> Path:
    """`.context/jev-calibration/<kind>-<model>.json`, 0700 folder, 0600 file, atomic."""
    ctx = _ctx(vault)
    if ctx.is_symlink() or not ctx.is_dir():
        raise Refused(".context is missing or a symlink")
    folder = ctx / CALIBRATION_NAME
    if folder.is_symlink():
        raise Refused(f".context/{CALIBRATION_NAME} is a symlink")
    folder.mkdir(mode=0o700, exist_ok=True)
    target = receipt_path(vault, provider)
    if target.is_symlink() or (os.path.lexists(target) and not target.is_file()):
        raise Refused(f"{target.name} is a symlink or not a regular file")
    data = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(data) > RECEIPT_MAX_BYTES:
        raise Refused("the receipt would be too large")
    set_private_path(folder, directory=True)
    handle, name = private_tempfile(folder, prefix=".jev-", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, target)
    finally:
        if os.path.lexists(name):
            os.unlink(name)
    return target


def render_calibration(receipt: dict, missing: list[str], written) -> str:
    provider = receipt["provider"]
    lines = [f"calibration for provider {provider['kind']}/{provider.get('model') or 'none'}: "
             f"dev set {receipt['dev_set_sha256'][:16]} (v{receipt.get('dev_set_version')}), "
             f"{receipt['recording_rows']} recorded rows, template revision "
             f"{receipt['template_revision'][:12]}"]
    for name, entry in receipt["purposes"].items():
        numbers = ", ".join(f"{k} {v}" for k, v in entry.items() if k != "passed")
        lines.append(f"  {name:<18} {'met' if entry['passed'] else 'not met'}  ({numbers})")
    needed = receipt["needed_now"]
    lines.append("needed by the enabled features that can call in this version: "
                 + (", ".join(needed) if needed else "none")
                 + (f"; not met: {', '.join(missing)}" if missing else "; all met"))
    lines.append(f"receipt written to {written}" if written else
                 "dry run: add --apply to write the receipt")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _vault_arg(args, command: str) -> Path | None:
    vault = Path(args.vault).expanduser()
    if not vault.is_dir():
        print(f"context-layer jev {command}: vault not found: {args.vault}", file=sys.stderr)
        return None
    return vault.resolve()


def _unexpected(args, command: str) -> bool:
    if args.rest:
        print(f"context-layer jev {command}: unrecognised arguments: {' '.join(args.rest)}",
              file=sys.stderr)
        return True
    return False


def cmd_status(args: argparse.Namespace) -> int:
    if _unexpected(args, "status"):
        return 2
    vault = _vault_arg(args, "status")
    if vault is None:
        return 2
    info = status(vault, check=args.check)
    print(json.dumps(info, ensure_ascii=False, indent=2) if args.json else render_status(info))
    return 1 if info["configured"] and not info["valid"] else 0


def _replays_from_identity(identity: dict) -> dict:
    """The provider block whose identity a recording names, rebuilt from the row's
    `provider` object (kind, model, endpoint, profile, rounding, label_only)."""
    kind = identity.get("kind")
    if kind not in USER_PROVIDER_KINDS + ("fake",) or kind == "cmd":
        raise Refused(f"a {kind} provider cannot be rebuilt from a recording; configure it "
                      "first, then add --recording")
    replays: dict = {"kind": kind}
    if identity.get("model"):
        replays["model"] = identity["model"]
    if kind in ("systemone", "openai_compat"):
        replays["base_url"] = identity.get("endpoint")
        if kind == "systemone":
            replays["key_env"] = "CONTEXT_LAYER_JEV_RECORDED"   # never read: replay only
    if identity.get("profile") == "laya":
        replays["profile"] = "laya"
    if identity.get("rounding"):
        replays["rounding"] = identity["rounding"]
    if kind == "fake" and identity.get("label_only") is True:
        replays["label_only"] = True
    return replays


def recorded_provider(recording, current: dict | None) -> dict:
    """The `recorded` provider block for `--recording FILE`: the file's rows name the
    provider they replay; the configured live provider is reused when it is that one,
    else the block is rebuilt from the rows and checked to give the same identity."""
    if recording is None:
        raise Refused("--provider-kind recorded needs --recording FILE")
    path = Path(recording).expanduser().resolve()
    rows = load_recording_rows(path)
    identities = [row.get("provider") for row in rows if isinstance(row.get("provider"), dict)]
    if not identities:
        raise Refused(f"{path.name} names no provider")
    identity = identities[0]
    if any(other != identity for other in identities):
        raise Refused(f"{path.name} holds rows of more than one provider")
    client = _client()

    def identity_of(block):
        try:
            return client.provider_identity(block)
        except Exception:
            return None

    live = current if isinstance(current, dict) and current.get("kind") != "recorded" else None
    if live is not None and identity_of(live) == identity:
        replays = dict(live)
    else:
        replays = _replays_from_identity(identity)
        if identity_of(replays) != identity:
            raise Refused("the recorded provider cannot be rebuilt from the file; configure "
                          "that provider first (`jev shadow <vault> --provider-kind ...`), "
                          "then add --recording")
    return {"kind": "recorded", "recording": str(path), "replays": replays}


def _provider_from_args(current: dict | None, args) -> dict | None:
    given = {"base_url": args.base_url, "model": args.model, "key_env": args.key_env}
    if args.provider_kind is None and all(v is None for v in given.values()):
        return None
    if args.provider_kind is not None:
        same = current and current.get("kind") == args.provider_kind
        provider = dict(current) if same else {"kind": args.provider_kind}
    elif current:
        provider = dict(current)
    else:
        raise Refused("name --provider-kind first: there is no default provider")
    for key, value in given.items():
        if value is not None:
            if value:
                provider[key] = value
            else:
                provider.pop(key, None)
    return provider


def cmd_mode(args: argparse.Namespace) -> int:
    target = args.jev_mode
    name = f"context-layer jev {target}"
    if _unexpected(args, target):
        return 2
    vault = _vault_arg(args, target)
    if vault is None:
        return 2
    enable, disable = list(args.enable or []), list(args.disable or [])
    if set(enable) & set(disable):
        print(f"{name}: a feature cannot be both enabled and disabled", file=sys.stderr)
        return 2
    unknown = [f for f in enable + disable if f not in FEATURES]
    if unknown:
        print(f"{name}: refused: unknown feature {unknown[0]!r} (known: {', '.join(FEATURES)})",
              file=sys.stderr)
        return 1
    config = load_config(vault)
    if config.present and not config.valid:
        print(f"{name}: refused: .context/jev.json is invalid ({config.problem}); the file was "
              "left untouched and the advisor stays off", file=sys.stderr)
        return 1
    flags = (enable or disable or args.provider_kind or args.base_url is not None
             or args.model is not None or args.key_env is not None)
    if not config.present and target == "off" and not flags:
        print("advisor (jev): off; it is not configured (.context/jev.json is absent), so "
              "nothing was written")
        return 0
    obj = dict(config.raw) if config.present else {"schema_version": SCHEMA_VERSION}
    before = _canonical(obj)
    obj["schema_version"] = SCHEMA_VERSION
    features = list(config.data["features"]) if config.present else list(DEFAULT_FEATURES)
    features = [f for f in FEATURES if (f in features or f in enable) and f not in disable]
    obj["features"] = features
    try:
        provider = _provider_from_args(config.data.get("provider") if config.present else None,
                                       args)
    except Refused as exc:
        provider = None
        if target != "off":
            print(f"{name}: refused: {exc}", file=sys.stderr)
            print("provider kinds (docs/jev.md):", file=sys.stderr)
            for kind in USER_PROVIDER_KINDS:
                print(f"  {kind:<14} {KIND_HELP[kind]}", file=sys.stderr)
            return 1
    if getattr(args, "recording", None) is not None or args.provider_kind == "recorded":
        try:
            provider = recorded_provider(args.recording,
                                         config.data.get("provider") if config.present else None)
        except Refused as exc:
            print(f"{name}: refused: {exc}", file=sys.stderr)
            return 1
    if provider is not None:
        obj["provider"] = provider
    obj["mode"] = target
    if target != "off" and not obj.get("provider"):
        print(f"{name}: refused: name --provider-kind (there is no default provider)",
              file=sys.stderr)
        print("provider kinds (docs/jev.md):", file=sys.stderr)
        for kind in USER_PROVIDER_KINDS:
            print(f"  {kind:<14} {KIND_HELP[kind]}", file=sys.stderr)
        return 1
    data, problem = validate(obj, vault)
    if problem:
        print(f"{name}: refused: {problem}; nothing was written", file=sys.stderr)
        return 1
    if target == "on":
        _, problem = check_receipt(vault, data["provider"], data["thresholds"], data["features"],
                                   _template_revision())
        if problem:
            print(f"{name}: refused (calibration_required): {problem}. `on` needs a "
                  "jev-calibration/v1 receipt for this provider; `jev shadow` needs none. "
                  "Nothing was written.", file=sys.stderr)
            return 1
    try:
        write_config(vault, obj)
        if target != "off":
            ensure_salt(vault)
    except (Refused, OSError) as exc:
        print(f"{name}: refused: {exc}", file=sys.stderr)
        return 1
    changed = before != _canonical(obj)
    print(f"advisor (jev): mode {target} written to .context/jev.json "
          f"(changed: {'yes' if changed else 'no'})")
    shown = ", ".join(f"{k} {v}" for k, v in (data["provider"] or {}).items()
                      if k in ("kind", "base_url", "model", "key_env"))
    print(f"provider: {shown or 'none'} (no key is stored in the file)")
    print("features: " + (", ".join(data["features"]) or "none")
          + ("" if "auto_context" in data["features"] else
             " (auto_context stays off unless named with --enable auto_context)"))
    if target != "off" and "search" in data["features"]:
        print("search --jev sends: " + SENDS["search"].format(excerpt=data["excerpt_chars"]))
    if target != "off" and "answer" in data["features"]:
        print("answer (jev answer, handback check --jev, MCP check_claims) sends: "
              + SENDS["answer"])
    if target == "shadow":
        print("shadow: answers are counted and shown by `search --jev`; the packet is unchanged")
        if "answer" in data["features"]:
            print("shadow: `jev answer` shows its advisory verdicts; `handback check --jev` "
                  "and MCP `check_claims` count them and change nothing")
    if kill_switch(vault):
        print("note: a kill switch is set, so the advisor stays off until it is removed")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    if _unexpected(args, "report"):
        return 2
    vault = _vault_arg(args, "report")
    if vault is None:
        return 2
    if not 1 <= args.days <= 3650:
        print("context-layer jev report: --days must be between 1 and 3650", file=sys.stderr)
        return 2
    try:
        info = report(vault, args.days)
    except Refused as exc:
        print(f"context-layer jev report: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(info, ensure_ascii=False, indent=2) if args.json else render_report(info))
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    if _unexpected(args, "purge"):
        return 2
    vault = _vault_arg(args, "purge")
    if vault is None:
        return 2
    targets = purge_targets(vault, all_files=args.all, receipts=args.receipts)
    if not targets:
        print("nothing to remove")
    for path in targets:
        shown = f".context/{path.name}" + ("/" if path.is_dir() and not path.is_symlink() else "")
        if args.apply:
            try:
                _remove(path)
            except OSError as exc:
                print(f"context-layer jev purge: {shown}: {exc.strerror or exc}", file=sys.stderr)
                return 1
        print(f"{'removed' if args.apply else 'would remove'}: {shown}")
    if targets and not args.apply:
        print("dry run: nothing was removed; add --apply to remove these")
    if not args.receipts:
        print("kept: calibration receipts and recordings (they are evidence; --receipts "
              "removes them)")
    print("never removed by purge: .context/jev.json (the configuration) and "
          ".context/jev.disabled (the kill switch)")
    return 0


def cmd_answer(args: argparse.Namespace) -> int:
    name = "context-layer jev answer"
    if _unexpected(args, "answer"):
        return 2
    vault = _vault_arg(args, "answer")
    if vault is None:
        return 2
    try:
        info = claims_report(vault, load_claims(args.claims), surface="cli")
    except Refused as exc:
        print(f"{name}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(info, ensure_ascii=False, indent=2) if args.json
          else render_claims_report(info))
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    name = "context-layer jev record"
    if _unexpected(args, "record"):
        return 2
    vault = _vault_arg(args, "record")
    if vault is None:
        return 2
    if not 1 <= args.deadline_s <= RECORD_DEADLINE_MAX:
        print(f"{name}: --deadline-s must be between 1 and {RECORD_DEADLINE_MAX:g}",
              file=sys.stderr)
        return 2
    try:
        plan = record_plan(vault, args.questions)
    except Refused as exc:
        print(f"{name}: refused: {exc}", file=sys.stderr)
        return 1
    cfg = plan.config.data
    max_requests = args.max_requests or cfg["max_requests"]
    max_parallel = args.max_parallel or cfg["max_parallel"]
    if not 1 <= max_requests <= NUMBERS["max_requests"][3] \
            or not 1 <= max_parallel <= NUMBERS["max_parallel"][3]:
        print(f"{name}: --max-requests must be 1..{NUMBERS['max_requests'][3]} and "
              f"--max-parallel 1..{NUMBERS['max_parallel'][3]}", file=sys.stderr)
        return 2
    provider = plan.provider
    shown = ", ".join(f"{k} {v}" for k, v in provider.items()
                      if k in ("kind", "base_url", "model", "key_env"))
    batches = -(-len(plan.questions) // max_requests)
    templates = ", ".join(f"{t} x{n}" for t, n in sorted(plan.templates.items()))
    out = Path(args.out).expanduser() if args.out else (
        _ctx(vault) / RECORDINGS_NAME
        / (re.sub(r"[^A-Za-z0-9._-]", "_", f"{provider.get('kind')}-{provider.get('model') or 'none'}")
           + time.strftime("-%Y%m%dT%H%M%SZ", time.gmtime()) + ".jsonl"))
    print(f"recording: {len(plan.questions)} question(s) [{templates}] for provider {shown}; "
          f"{batches} batch(es) of at most {max_requests}, {max_parallel} in parallel, "
          f"deadline {args.deadline_s:g} s per batch; about {plan.input_chars} input characters")
    print(f"output: {out}")
    if not args.run:
        print("dry run: nothing was sent. Add --run to ask the provider (each question and its "
              "short views go to that provider).")
        return 0
    try:
        counters = record(plan, out, append=args.append, max_requests=max_requests,
                          max_parallel=max_parallel, deadline_s=float(args.deadline_s))
    except Refused as exc:
        print(f"{name}: refused: {exc}", file=sys.stderr)
        return 1
    codes = ", ".join(f"{c} x{n}" for c, n in sorted(counters["codes"].items()))
    cost = ("not reported" if counters["cost_usd"] is None
            else f"{counters['cost_usd']:.4f} USD")
    print(f"recorded {counters['rows']} row(s): {counters['answered']} answered, "
          f"{counters['failed']} failed{' (' + codes + ')' if codes else ''}; "
          f"{counters['input_tokens']} input and {counters['output_tokens']} output tokens "
          f"reported; cost "
          f"{cost}; "
          f"{counters['latency_ms']} ms")
    if counters["stopped"]:
        print(f"stopped early: {counters['stopped']}; the rows so far are kept", file=sys.stderr)
        return 1
    return 1 if counters["failed"] else 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    name = "context-layer jev calibrate"
    if _unexpected(args, "calibrate"):
        return 2
    vault = _vault_arg(args, "calibrate")
    if vault is None:
        return 2
    try:
        receipt, missing = calibrate(vault, Path(args.report).expanduser(),
                                     Path(args.recording).expanduser())
        written = None
        if args.apply:
            target = write_receipt(vault, receipt["provider"], receipt)
            written = f".context/{CALIBRATION_NAME}/{target.name}"
    except Refused as exc:
        print(f"{name}: refused: {exc}; nothing was written", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"receipt": receipt, "not_met": missing, "written": written},
                         indent=2, sort_keys=True))
    else:
        print(render_calibration(receipt, missing, written))
    return 1 if missing else 0


def cmd_review_memory(args: argparse.Namespace) -> int:
    name = "context-layer jev review-memory"
    if _unexpected(args, "review-memory"):
        return 2
    vault = _vault_arg(args, "review-memory")
    if vault is None:
        return 2
    try:
        proposals, single = load_memory_proposals(Path(args.proposal).expanduser())
    except Refused as exc:
        print(f"{name}: refused: {exc}", file=sys.stderr)
        return 1
    plan = memory_plan(vault)
    notes: list[str] = []
    if plan.mode == "off":
        notes.append(f"the advisor is off ({plan.why}); this is the mechanical review only")
    reviews = []
    try:
        for proposal in proposals:
            reviews.append(review_memory(vault, proposal, plan, notes=notes))
    except Refused as exc:
        print(f"{name}: refused: {exc}", file=sys.stderr)
        return 1
    for note in dict.fromkeys(notes):
        print(f"{name}: {note}", file=sys.stderr)
    document = reviews[0] if single else {"schema": MEMORY_BATCH_SCHEMA, "reviews": reviews}
    if args.json:
        print(json.dumps(document, ensure_ascii=False, indent=2))
    else:
        print("\n\n".join(render_memory_review(review) for review in reviews))
    return 0


def _register_review_memory(group) -> None:
    parser = group.add_parser(
        "review-memory", allow_abbrev=False,
        help="Review a memory proposal: a mechanical check of its quoted spans, and (shadow, "
             "on) the advisor's reading. Never writes, accepts or rejects a record.",
        description="Reads a jev-memory-proposal/v1 (one object, a JSON array of them, or JSON "
                    "lines): kind, text, evidence spans {path, sha256, line_start, line_end, "
                    "span} and optional prior record ids (default: up to four records in "
                    "force that share an evidence path). Always checks each span against the "
                    "current file and names an exact duplicate of a record in force. In "
                    "shadow the four questions (support, commitment, kind, relation to each "
                    "prior) are asked and counted, and the output is the same as off; in on "
                    "(needs a calibration receipt) the report adds the advisor's answers and "
                    "a route. Exit 0 a report was produced (whatever it says), 1 input "
                    "refused, 2 usage. See docs/jev.md.")
    parser.add_argument("vault")
    parser.add_argument("--proposal", required=True, metavar="FILE",
                        help="The jev-memory-proposal/v1 file.")
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    parser.set_defaults(func=cmd_review_memory, forward_to=None)


def register(sub: argparse._SubParsersAction) -> None:
    """Attach `jev status|off|shadow|on|report|purge|record|calibrate|answer` to the CLI."""
    parser = sub.add_parser(
        "jev", help="The optional advisor (off by default): status, off/shadow/on, report, purge, "
             "record, calibrate, answer, review-memory.",
        description="The optional advisor asks a model provider whether notes the retrieval "
                    "found or reached are relevant. It is off by default; `shadow` only counts "
                    "and shows answers; `on` (refused without a calibration receipt) may add "
                    "byte-exact passages of linked notes within jev_extra_tokens. `answer` "
                    "adds an advisory verdict to claims checked against their quotes; "
                    "`review-memory` checks a memory proposal against its sources. See "
                    "docs/jev.md.")
    parser.set_defaults(forward_to=None)
    group = parser.add_subparsers(dest="jev_command", required=True)

    p_status = group.add_parser(
        "status", allow_abbrev=False,
        help="Show the mode in force, what each feature would send, and the provider.",
        description="Reads the advisor's files only. Exit 0 shown, 1 the configuration is "
                    "invalid (shown, nothing changed), 2 usage.")
    p_status.add_argument("vault")
    p_status.add_argument("--json", action="store_true", help="Machine-readable output.")
    p_status.add_argument("--check", action="store_true",
                          help="Also check the configured endpoint (no request is sent).")
    p_status.set_defaults(func=cmd_status, forward_to=None)

    for mode, text in (("off", "Switch the advisor off (the saved provider is kept)."),
                       ("shadow", "Ask the provider but change nothing: answers are counted "
                                  "and shown."),
                       ("on", "Apply rescue advice (needs a calibration receipt).")):
        p_mode = group.add_parser(
            mode, allow_abbrev=False, help=text,
            description=text + " The first shadow|on must name --provider-kind (there is no "
                               "default provider); later calls reuse it. There is no --key "
                               "flag: a key is only ever read from the variable named by "
                               "--key-env or from env_file. Exit 0 written, 1 refused "
                               "(invalid file left untouched, calibration_required, unknown "
                               "feature), 2 usage.")
        p_mode.add_argument("vault")
        p_mode.add_argument("--enable", action="append", metavar="FEATURE",
                            help=f"Enable a feature ({', '.join(FEATURES)}); repeatable.")
        p_mode.add_argument("--disable", action="append", metavar="FEATURE",
                            help="Disable a feature; repeatable.")
        p_mode.add_argument("--provider-kind", choices=PROVIDER_KINDS, default=None)
        p_mode.add_argument("--base-url", default=None, help="Provider endpoint (https, or "
                                                             "http on 127.0.0.1/[::1]).")
        p_mode.add_argument("--model", default=None, help="Model id.")
        p_mode.add_argument("--key-env", default=None,
                            help="Name of the environment variable that holds the key.")
        p_mode.add_argument("--recording", default=None, metavar="FILE",
                            help="Replay a jev-recording/v1 file instead of calling anything "
                                 "(--provider-kind recorded): the rows name the provider they "
                                 "replay; the configured one is reused when it is that "
                                 "provider, else it is rebuilt from the rows.")
        p_mode.set_defaults(func=cmd_mode, jev_mode=mode, forward_to=None)

    p_report = group.add_parser(
        "report", allow_abbrev=False,
        help="Summarise the counters-only call log (no paths, no text).",
        description="Per feature: calls, share applied, degraded calls by code, cache hits, "
                    "p50/p95 latency, tokens and cost where reported, and the shadow change "
                    "rate. Exit 0 shown, 1 the log cannot be read, 2 usage.")
    p_report.add_argument("vault")
    p_report.add_argument("--days", type=int, default=7, help="Look back this many days "
                                                               "(default 7).")
    p_report.add_argument("--json", action="store_true", help="Machine-readable output.")
    p_report.set_defaults(func=cmd_report, forward_to=None)

    p_record = group.add_parser(
        "record", allow_abbrev=False,
        help="Ask the configured provider a file of questions and keep the answers as a "
             "jev-recording/v1 file (dry run unless --run).",
        description="Reads jev-questionnaire/v1 objects (a JSON array or JSON lines; "
                    "tests/jev_dev_eval.py --dump-questions writes the dev set's), checks "
                    "every one against the installed contracts and the secret scan, and with "
                    "--run asks the configured provider in batches, appending one row per "
                    "question (hashes, labels, numbers, counters; never text) to --out. "
                    "Exit 0 every question answered, 1 refused or some questions failed "
                    "(their rows carry a code), 2 usage.")
    p_record.add_argument("vault")
    p_record.add_argument("--questions", required=True, metavar="FILE",
                          help="jev-questionnaire/v1 objects: a JSON array or JSON lines.")
    p_record.add_argument("--out", default=None, metavar="FILE",
                          help="Where the rows go (default: .context/jev-recordings/"
                               "<kind>-<model>-<utc>.jsonl). Refused when it exists, unless "
                               "--append.")
    p_record.add_argument("--run", action="store_true",
                          help="Ask the provider. Without it nothing is sent.")
    p_record.add_argument("--append", action="store_true",
                          help="Add rows to an existing --out file.")
    p_record.add_argument("--max-requests", type=int, default=None, metavar="N",
                          help="Questions per provider call batch (default: the configured "
                               "max_requests).")
    p_record.add_argument("--max-parallel", type=int, default=None, metavar="N",
                          help="Parallel requests inside a batch (default: the configured "
                               "max_parallel).")
    p_record.add_argument("--deadline-s", type=float, default=RECORD_DEADLINE_DEFAULT,
                          metavar="S", help=f"Seconds allowed per batch (default "
                                            f"{RECORD_DEADLINE_DEFAULT:g}, at most "
                                            f"{RECORD_DEADLINE_MAX:g}).")
    p_record.set_defaults(func=cmd_record, forward_to=None)

    p_answer = group.add_parser(
        "answer", allow_abbrev=False,
        help="Advisory check of claims against the passages they cite (feature `answer`).",
        description="Reads a jev-claims/v1 file (1-20 claims, each with 1-8 citations: "
                    "source_path, source_sha256, line_start, line_end, span). Every citation "
                    "is checked mechanically first; each one that passes is put to the "
                    "advisor as a claim_support question with the claim, the quote and its "
                    "enclosing section. The report is advisory: supported, contradicted, "
                    "insufficient or uncertain is a model's judgement, never a verification. "
                    "With the advisor off, killed or the feature disabled, only the "
                    "mechanical checks run. Exit 0 report produced (whatever the verdicts), "
                    "1 input refused, 2 usage.")
    p_answer.add_argument("vault")
    p_answer.add_argument("--claims", required=True, metavar="FILE",
                          help="The jev-claims/v1 file (JSON, at most 1 MiB).")
    p_answer.add_argument("--json", action="store_true", help="Machine-readable output.")
    p_answer.set_defaults(func=cmd_answer, forward_to=None)

    p_calibrate = group.add_parser(
        "calibrate", allow_abbrev=False,
        help="Compute the calibration receipt `on` needs from a dev-set evaluation of a "
             "recording (dry run unless --apply).",
        description="Reads the jev-dev-eval/v1 report that `tests/jev_dev_eval.py --provider "
                    "recorded:FILE --json` printed and the recording it evaluated, checks the "
                    "bars per question purpose (docs/jev.md) and writes "
                    ".context/jev-calibration/<kind>-<model>.json with --apply. Exit 0 every "
                    "purpose the enabled features need is met, 1 refused or a needed purpose "
                    "is not met (the receipt is still shown, and written with --apply), 2 "
                    "usage.")
    p_calibrate.add_argument("vault")
    p_calibrate.add_argument("--report", required=True, metavar="FILE",
                             help="The evaluation report (JSON).")
    p_calibrate.add_argument("--recording", required=True, metavar="FILE",
                             help="The jev-recording/v1 file the report evaluated.")
    p_calibrate.add_argument("--apply", action="store_true", help="Write the receipt.")
    p_calibrate.add_argument("--json", action="store_true", help="Machine-readable output.")
    p_calibrate.set_defaults(func=cmd_calibrate, forward_to=None)

    p_purge = group.add_parser(
        "purge", allow_abbrev=False,
        help="Remove the advisor's cache and salt (dry run unless --apply).",
        description="Removes .context/jev-cache/ and .context/jev.salt; --all also the call "
                    "log; --receipts also calibration receipts and recordings. Never removes "
                    ".context/jev.json or .context/jev.disabled. Exit 0 shown or removed, 1 "
                    "I/O error, 2 usage.")
    p_purge.add_argument("vault")
    p_purge.add_argument("--apply", action="store_true", help="Remove; without it, only list.")
    p_purge.add_argument("--all", action="store_true", help="Also remove the call log.")
    p_purge.add_argument("--receipts", action="store_true",
                         help="Also remove calibration receipts and recordings.")
    p_purge.set_defaults(func=cmd_purge, forward_to=None)
    _register_review_memory(group)
