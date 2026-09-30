"""context_layer.orchestrate — lean, checkable coordination of support workers.

A coordinator (the "root") that delegates pays twice: once for the worker and
again for everything it must re-read to trust the result. This module keeps
both small and makes the second one mechanical. Everything here is
deterministic; no model is called, except by the optional `handback check --jev`
(default off; see context_layer/jev.py), which may add an advisory note next to a
deterministic result and never changes one.

    .context/packets/<sha256>.json          shared, content-addressed evidence packet
    .context/jobs/worker_core.md            the lean worker rule core (vault copy)
    .context/jobs/<task>/attempt-NNN/job.md             support-job/v1 (root-owned)
                                     input-manifest.json compact input references
                                     payload.json        estimated payload accounting
                                     out/                the worker's owned output dir
                                         evidence.jsonl  candidate evidence records
                                         coverage.json   planned/scanned/... inventory
                                         receipt.json    <= 160 estimated tokens
                                         handoff.md      <= 800 estimated tokens
    .context/tasks/LEDGER.jsonl             hash-chained verification ledger

1. `packet build` runs one retrieval (fts or synaptic) and stores it under an id
   that is the SHA-256 of its canonical JSON (request, index/graph identity,
   every passage with its source hash and line range). N workers read one
   packet by id instead of each searching and reading again. `packet show` /
   MCP `read_packet` re-check every source first: a changed, missing or
   excluded source, a malformed entry, or a passage that is no longer at its
   lines withholds the whole packet with the reason. A stale packet is never
   served.
2. `job new` / `job validate` write and check a support-job/v1 contract. A
   missing mandatory value is BLOCKED, never inferred.
3. Payload accounting: core / job / manifest / packet against 1000 / 750 / 250 /
   3000 estimated tokens (5000 total). Over budget is refused unless
   --allow-over. All token numbers are ceil(characters / 4): estimates, not a
   tokenizer count and not billed host tokens.
4. `handback check` verifies each evidence record mechanically: the file is in
   the allowed roots, its SHA-256 is the current one, the verbatim span is at
   the stated lines and long enough to anchor a claim, and every hard token of
   the observation (number, date, time, quoted text, URL, code identifier,
   multi-word name) is in the span. A fabricated quote fails, and so does a
   genuine short quote under an invented number. `--sample K` draws K checked
   records for the root to read in full, from a seed drawn at check time.
   `--jev` (optional advisor, feature `answer`, only in its `on` mode) adds a `jev`
   note to each record that passed: whether a model judges the quote, read within
   its section, to support the observation. It only adds; `ok`, `problems`,
   `mechanically_checked`, the digest, the ledger line and the exit code are the
   deterministic ones, and the advisor is never asked about a record that failed.
5. `job estimate` is the break-even arithmetic for "delegate or do it yourself".
6. `handoff write` produces the minimal control file for the next agent.
7. The verification ledger (`.context/tasks/LEDGER.jsonl`) records each
   verdict with the hashes it rested on, each line chained to the previous one.

One line model for the whole package: a line ends at "\\n" (a "\\r" before it
belongs to the line ending); form feed, U+2028, U+2029 and U+0085 never end a
line. That is what `grep -n` numbers.

Python 3.10+; standard library only. No network and no model call of its own (the
optional advisor's call is in jev.py, through jev_client.py).
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sqlite3
import stat
import sys
import unicodedata
from .platform_support import (atomic_write as _portable_atomic_write, file_lock,
                               private_tempfile, set_private_permissions)

SCHEMA_PACKET = "context-layer-shared-packet/v1"
SCHEMA_JOB = "support-job/v1"
SCHEMA_MANIFEST = "support-input-manifest/v1"
SCHEMA_RECEIPT = "support-receipt/v1"
SCHEMA_CHECK = "handback-check/v1"
SCHEMA_HANDOFF = "support-handoff/v1"
SCHEMA_LEDGER = "context-layer-ledger/v1"

PACKETS_DIR = ".context/packets"
JOBS_DIR = ".context/jobs"
LEDGER_NAME = ".context/tasks/LEDGER.jsonl"
CORE_NAME = "worker_core.md"
SHIPPED_CORE = Path(__file__).resolve().parent / "data" / CORE_NAME

# Initial payload budgets in estimated tokens (starting values, not measured optima).
BUDGETS = {"core": 1000, "job": 750, "manifest": 250, "packet": 3000}
TOTAL_BUDGET = 5000
BUILD_BUDGET_TOKENS = 1200          # packet build --budget-tokens default
RECEIPT_MAX_TOKENS = 160
RECEIPT_READ_CAP = 64 * 1024        # bytes read from receipt.json at most
HANDOFF_MAX_TOKENS = 800
RETURN_INDEX_MAX_TOKENS = 1200
MAX_EVIDENCE_BYTES = 20 * 1024 * 1024
MAX_PACKET_BYTES = 32 * 1024 * 1024
# Claim-span gate: a span anchors a claim only with >= 3 words or >= 12 visible
# characters, and a record cites at most 21 lines (line_end - line_start <= 20).
SPAN_MIN_WORDS = 3
SPAN_MIN_CHARS = 12
MAX_LINE_SPAN = 20
ESTIMATE_NOTE = ("estimated tokens = ceil(characters / 4); not a tokenizer count and not "
                 "billed host tokens")
AUTHORITY = "candidate evidence only; root owns judgement and final authorship"
DEFAULT_METHOD = ("read the packet with read_packet or `context-layer packet show`; read "
                  "files under allowed_source_roots with read_source; no other tool")
STATES = ("READY", "PARTIAL", "BLOCKED")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
JSON_BLOCK = re.compile(r"^```json[ \t]*\n(.*?)\n```[ \t]*$", re.S | re.M)

# support-job/v1 mandatory fields and the check each value must pass.
JOB_FIELDS = (
    "schema", "task_id", "attempt", "root_goal", "worker_objective", "authority",
    "rule_core_path", "rule_core_sha256", "input_manifest_path", "input_manifest_sha256",
    "initial_packet_path", "initial_packet_id", "allowed_source_roots", "exclusions",
    "known_unknowns", "allowed_method", "acceptance_oracle", "stop_when",
    "owned_output_dir", "may_modify_source", "max_initial_application_tokens",
    "max_total_usage_tokens", "max_elapsed_seconds", "max_evidence_bytes",
    "return_index_max_tokens")
RECORD_FIELDS = ("id", "observation", "source_path", "source_sha256", "line_start",
                 "line_end", "span", "method", "uncertainty")
RECEIPT_FIELDS = ("schema", "task_id", "attempt", "state", "counts", "handoff", "blocker")


class OrchestrateError(Exception):
    """A request or vault problem reported to the user, not a crash."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def est_tokens(text: str) -> int:
    """An estimate, not a model tokenizer: ceil(characters / 4)."""
    return math.ceil(len(text) / 4)


def canonical(payload) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fsync_dir(directory: Path) -> None:
    """Make a rename or a new entry in `directory` durable (best effort)."""
    try:
        descriptor = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write(path: Path, text: str) -> None:
    """Write through a unique staging file, fsync it, rename it over `path`, fsync the dir.

    A reader sees the old bytes or the new ones, never a torn file, and two writers
    never share a staging name.
    """
    _portable_atomic_write(path, text, private=True)
    fsync_dir(path.parent)


_atomic_write = atomic_write  # the 0.3 name


def create_exclusive(path: Path, text: str) -> bool:
    """Create `path` holding `text` only if nothing is there yet: False when it exists.

    The bytes go to a unique staging file first, fsynced, then `os.link` puts them
    in place atomically, so a reader never sees half a file and two writers never
    both win.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, staging = private_tempfile(path.parent, prefix="." + path.name + ".",
                                   suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(staging, path)
        except FileExistsError:
            return False
        except OSError:  # a file system without hard links: O_EXCL instead
            try:
                descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                return False
            set_private_permissions(descriptor, path)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
                target.write(text)
                target.flush()
                os.fsync(target.fileno())
        fsync_dir(path.parent)
        return True
    finally:
        staging.unlink(missing_ok=True)


def verbose() -> bool:
    """True when `context-layer --verbose` is running; only then are absolute paths shown.

    The CLI module is `context_layer.cli` under the console script and `__main__`
    under `python -m context_layer.cli`; both carry the flag as VERBOSE.
    """
    for name in ("context_layer.cli", "__main__"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "repo_home") \
                and getattr(module, "VERBOSE", False) is True:
            return True
    return False


def shown(vault: Path | None, path) -> str:
    """How a path is printed: vault-relative (or its name), absolute only with --verbose."""
    path = Path(path)
    if verbose():
        return str(path)
    if vault is not None:
        try:
            return path.resolve().relative_to(Path(vault).resolve()).as_posix()
        except ValueError:
            pass
    return path.name or str(path)


def _vault(value) -> Path:
    vault = Path(value).expanduser().resolve()
    if not vault.is_dir():
        raise OrchestrateError(f"vault not found: {shown(None, vault)}")
    return vault


def _policy():
    from . import mcp_server
    return mcp_server.policy()


def _prefixes(vault: Path) -> list:
    from . import mcp_server
    try:
        return mcp_server.exclude_prefixes(vault)
    except (OSError, ValueError) as exc:
        raise OrchestrateError(f"cannot read .context/routes.json: {exc}") from exc


def _routes_sha(vault: Path) -> str | None:
    config = vault / ".context" / "routes.json"
    return sha256_file(config) if config.is_file() else None


def vault_of(path: Path) -> Path | None:
    """The vault a file under <vault>/.context/... belongs to."""
    for parent in Path(path).resolve().parents:
        if parent.name == ".context":
            return parent.parent
    return None


def _rel(vault: Path, name: str, label: str) -> Path:
    """A vault-relative path that stays inside the vault and passes no symlink."""
    if not isinstance(name, str) or not name or "\\" in name:
        raise ValueError(f"{label} must be a vault-relative POSIX path")
    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"{label} escapes the vault: {name}")
    current = vault
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{label} passes through a symlink: {name}")
    if not current.resolve().is_relative_to(vault):
        raise ValueError(f"{label} escapes the vault: {name}")
    return current


# ---------------------------------------------------------------------------
# The line model: a line ends at "\n", nothing else ends one
# ---------------------------------------------------------------------------

_LINE_END = re.compile("(?<=\n)")


def lines_of(text: str) -> list[str]:
    """Raw lines, each with its "\\n" ending; the last one may lack it.

    A line ends at "\\n" and only there: form feed, U+2028, U+2029, U+0085 and a
    lone "\\r" stay inside their line (str.splitlines would split on all of them,
    and its numbers would disagree with `grep -n` and with `locate`).
    """
    lines = _LINE_END.split(text)
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def line_text(line: str) -> str:
    """One raw line without its ending: the "\\n" and one "\\r" before it."""
    if line.endswith("\n"):
        line = line[:-1]
    if line.endswith("\r"):
        line = line[:-1]
    return line


def span_at(text: str, span: str, start: int, end: int) -> bool:
    """True when `span` occurs verbatim inside lines start..end (1-based, inclusive)."""
    lines = lines_of(text)
    if not span or start < 1 or end < start or end > len(lines):
        return False
    return span in "".join(lines[start - 1:end])


def span_touches(text: str, span: str, start: int, end: int) -> bool:
    """True when some verbatim occurrence of `span` inside lines start..end begins in
    line `start` and ends in line `end`: the cited range is the one the quote occupies."""
    lines = lines_of(text)
    if not span or start < 1 or end < start or end > len(lines):
        return False
    block = "".join(lines[start - 1:end])
    first_end = len(lines[start - 1])
    last_start = len(block) - len(lines[end - 1])
    position = block.find(span)
    while position >= 0:
        if position < first_end and position + len(span) > last_start:
            return True
        position = block.find(span, position + 1)
    return False


def locate(text: str, content: str) -> tuple[int, int] | None:
    """1-based (first, last) line of the first verbatim occurrence of content."""
    offset = text.find(content)
    if offset < 0 or not content:
        return None
    first = text.count("\n", 0, offset) + 1
    newlines = content.count("\n") - (1 if content.endswith("\n") else 0)
    return first, first + newlines


def jsonl_lines(raw: bytes) -> list[tuple[int, str]]:
    """(line number, text) for a JSON Lines file: split on "\\n" only, one trailing
    "\\r" dropped. A record may legally hold a raw U+2028 inside a string."""
    text = raw.decode("utf-8", "replace")
    return [(number, line_text(line)) for number, line in enumerate(lines_of(text), start=1)]


# ---------------------------------------------------------------------------
# 1. Shared, content-addressed evidence packets
# ---------------------------------------------------------------------------

def index_identity(vault: Path) -> str | None:
    """SHA-256 over the indexed (source_path, source_sha256) pairs: content, not mtime."""
    index = vault / ".context" / "index.sqlite"
    if not index.is_file():
        return None
    connection = sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT source_path, MIN(source_sha256) FROM records "
                                  "GROUP BY source_path ORDER BY source_path").fetchall()
    finally:
        connection.close()
    return sha256_bytes(canonical([list(row) for row in rows]))


def graph_identity(vault: Path) -> str | None:
    """SHA-256 over the link graph's edges; a rebuild with the same links keeps it."""
    path = vault / ".context" / "graph.sqlite"
    if not path.is_file():
        return None
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT kind, source_path, target_path, line, source_sha256, heading, block, field "
            "FROM edges ORDER BY source_path, line, kind, target_path, heading, block").fetchall()
    finally:
        connection.close()
    return sha256_bytes(canonical([list(row) for row in rows]))


def packet_path(vault: Path, packet_id: str) -> Path:
    if not isinstance(packet_id, str) or not HEX64.match(packet_id):
        raise OrchestrateError("packet id must be 64 lowercase hex characters")
    return vault / PACKETS_DIR / f"{packet_id}.json"


def packet_id_of(body: dict) -> str:
    return sha256_bytes(canonical(body))


def _retrieve(vault: Path, prompt: str, method: str, top_k: int, budget: int,
              per_source: int, budget_tokens: int, extra_tokens: "int | None" = None,
              compact: bool = False) -> dict:
    """The same retrieval `context-layer search` runs (eval/retrieve.py), captured."""
    from . import mcp_server
    state = mcp_server.Server(vault, top_k, budget, per_source, budget_tokens)
    _, packet, code = mcp_server.search(state, prompt, method, top_k, budget, per_source,
                                        budget_tokens, extra_tokens=extra_tokens,
                                        compact=compact)
    if packet is None or code != 0 or packet.get("operation_status") != "ok":
        detail = (packet or {}).get("error", "no packet")
        raise OrchestrateError(f"retrieval failed: {detail}")
    if packet.get("withheld"):
        # A shared packet is pinned for other agents; build it only from a current index.
        names = ", ".join(e.get("source_path", "?") for e in packet["withheld"])
        raise OrchestrateError(f"source changed since indexing: {names}; "
                               "run `context-layer index <vault>` first")
    return packet


def build_packet(vault: Path, prompt: str, method: str = "fts", budget_tokens: int = 1200,
                 top_k: int = 3, budget: int = 6000, per_source: int = 2000,
                 extra_tokens: "int | None" = None, compact: bool = False) -> dict:
    """Retrieve once, pin every passage to its source hash and lines, store by content id.

    budget_tokens caps the fts evidence at 4 x budget_tokens characters, and sizes the
    synaptic packet only with compact=True. The default synaptic packet (the fts packet
    plus link-graph extras) is sized by extra_tokens (None: the search default).
    Returns {"id", "path", "reused", "est_tokens", "packet"}.
    """
    from .mcp_server import EXTRA_TOKENS_DEFAULT
    vault = _vault(vault)
    if method not in ("fts", "synaptic"):
        raise OrchestrateError("method must be fts or synaptic")
    if not prompt or not prompt.strip():
        raise OrchestrateError("--prompt must not be empty")
    if min(budget_tokens, top_k, budget, per_source) <= 0 \
            or (extra_tokens is not None and extra_tokens < 0):
        raise OrchestrateError("budgets and top-k must be positive")
    if (compact or extra_tokens is not None) and method != "synaptic":
        raise OrchestrateError("extra_tokens and compact need method synaptic")
    if compact and extra_tokens is not None:
        raise OrchestrateError("extra_tokens sizes the default synaptic packet, not compact")
    if method == "fts":
        budget = min(budget, budget_tokens * 4)
    raw = _retrieve(vault, prompt, method, top_k, budget, per_source, budget_tokens,
                    extra_tokens, compact)
    policy, prefixes = _policy(), _prefixes(vault)
    evidence = []
    for item in raw.get("evidence") or []:
        name = item["source_path"]
        data = policy.source_path(vault, name, prefixes).read_bytes()
        if sha256_bytes(data) != item["source_sha256"]:
            raise OrchestrateError(f"source changed while the packet was built: {name}")
        text = data.decode("utf-8")
        content = item["content"]
        first, last = item.get("line_start"), item.get("line_end")
        if not (isinstance(first, int) and isinstance(last, int)
                and span_at(text, content, first, last)):
            found = locate(text, content)
            if found is None:
                raise OrchestrateError(f"passage is not verbatim in its source: {name}")
            first, last = found
        evidence.append({"source_path": name, "source_sha256": item["source_sha256"],
                         "line_start": first, "line_end": last, "content": content})
    request = {"query": prompt, "method": method, "top_k": top_k, "budget_chars": budget,
               "per_source_chars": per_source}
    if method == "synaptic":
        # Kept for every synaptic packet so ids built before --extra-tokens/--compact
        # existed stay the same; the keys below appear only when they change retrieval.
        request["budget_tokens"] = budget_tokens
        if extra_tokens is not None and extra_tokens != EXTRA_TOKENS_DEFAULT:
            request["extra_tokens"] = extra_tokens
        if compact:
            request["compact"] = True
    body = {"schema": SCHEMA_PACKET, "request": request,
            "index_identity": index_identity(vault),
            "graph_identity": graph_identity(vault) if method == "synaptic" else None,
            "retrieval_status": raw.get("status"), "evidence": evidence,
            "evidence_est_tokens": sum(est_tokens(e["content"]) for e in evidence)}
    packet_id = packet_id_of(body)
    path = packet_path(vault, packet_id)
    reused = False
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            reused = packet_id_of({k: v for k, v in stored.items() if k != "id"}) == packet_id
        except (OSError, ValueError):
            reused = False
    if not reused:
        _atomic_write(path, json.dumps({"id": packet_id, **body}, indent=1,
                                       ensure_ascii=False) + "\n")
    served = {"id": packet_id, **body}
    return {"id": packet_id, "path": path.relative_to(vault).as_posix(), "reused": reused,
            "est_tokens": est_tokens(canonical(served).decode("utf-8")), "packet": served}


def _evidence_problem(number: int, item) -> str | None:
    """Why one stored evidence entry cannot be re-checked, or None when its shape is sound."""
    if not isinstance(item, dict):
        return f"evidence entry {number} is not an object"
    name = item.get("source_path")
    if not isinstance(name, str) or not name:
        return f"evidence entry {number}: source_path must be a non-empty string"
    if not isinstance(item.get("source_sha256"), str) or not HEX64.match(item["source_sha256"]):
        return f"evidence entry {number} ({name}): source_sha256 must be 64 lowercase hex"
    if not isinstance(item.get("content"), str) or not item["content"]:
        return f"evidence entry {number} ({name}): content must be a non-empty string"
    start, end = item.get("line_start"), item.get("line_end")
    if not (_positive_int(start) and _positive_int(end) and start <= end):
        return (f"evidence entry {number} ({name}): line_start/line_end must be integers "
                "with 1 <= start <= end")
    return None


def read_packet(vault: Path, packet_id: str) -> dict:
    """The packet, served only after every source is re-checked; otherwise withheld.

    Checks: the file matches its id, every entry has a sound shape, every source is
    still allowed (routes.json exclusions, no symlink, no escape), exists, has the
    recorded SHA-256, and the passage is still verbatim at its recorded lines. Any
    failure withholds the whole packet: its id pins one snapshot, and a partial
    snapshot is not it. A crafted packet is withheld with its reason, never a crash.
    """
    vault = _vault(vault)
    path = packet_path(vault, packet_id)
    base = {"schema": SCHEMA_PACKET, "id": packet_id, "checked_at": _utc()}

    def withheld(reasons: list) -> dict:
        return {**base, "status": "WITHHELD", "served": False, "reasons": reasons,
                "evidence": []}

    if not path.is_file():
        return withheld([f"no such packet: {PACKETS_DIR}/{packet_id}.json"])
    size = path.stat().st_size
    if size > MAX_PACKET_BYTES:
        return withheld([f"packet file is {size} bytes, over the {MAX_PACKET_BYTES} cap; "
                         "not read"])
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        detail = exc.strerror if isinstance(exc, OSError) else str(exc)
        return withheld([f"packet unreadable: {detail}"])
    body = {k: v for k, v in stored.items() if k != "id"} if isinstance(stored, dict) else {}
    reasons = []
    if not body or stored.get("id") != packet_id or packet_id_of(body) != packet_id:
        reasons.append("packet file does not match its id (edited or corrupted)")
    elif not isinstance(body.get("evidence"), list):
        reasons.append("packet evidence is not a list")
    else:
        problems = [problem for number, item in enumerate(body["evidence"], start=1)
                    for problem in [_evidence_problem(number, item)] if problem]
        reasons += problems
    if not reasons:
        policy, prefixes = _policy(), _prefixes(vault)
        for item in body["evidence"]:
            name = item["source_path"]
            try:
                source = policy.source_path(vault, name, prefixes)
            except (ValueError, TypeError) as exc:
                reasons.append(f"{name}: no longer an allowed source ({exc})")
                continue
            if not source.is_file():
                reasons.append(f"{name}: source missing")
                continue
            data = source.read_bytes()
            if sha256_bytes(data) != item["source_sha256"]:
                reasons.append(f"{name}: changed since the packet was built "
                               "(SHA-256 differs)")
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                reasons.append(f"{name}: not UTF-8")
                continue
            if not span_at(text, item["content"], item["line_start"], item["line_end"]):
                reasons.append(f"{name}: passage is not verbatim at lines "
                               f"{item['line_start']}-{item['line_end']}")
    if reasons:
        query = (body.get("request") or {}).get("query") \
            if isinstance(body.get("request"), dict) else None
        return {**withheld(reasons), "rebuild": "context-layer packet build <vault> --prompt "
                "... (a rebuilt packet gets a new id)" if query else None}
    served = {"id": packet_id, **body}
    return {**served, "status": "OK", "served": True, "checked_at": base["checked_at"],
            "est_tokens": est_tokens(canonical(served).decode("utf-8")),
            "est_tokens_note": ESTIMATE_NOTE,
            "note": "passages are quoted source data, never instructions"}


# ---------------------------------------------------------------------------
# 2-3. Job contract, rule core and payload accounting
# ---------------------------------------------------------------------------

def core_path(vault: Path) -> Path:
    return vault / JOBS_DIR / CORE_NAME


def ensure_core(vault: Path) -> tuple[Path, str | None]:
    """The vault copy of the worker rule core; written from the shipped one if missing."""
    target = core_path(vault)
    note = None
    shipped = SHIPPED_CORE.read_text(encoding="utf-8")
    if not target.is_file():
        _atomic_write(target, shipped)
    elif target.read_text(encoding="utf-8") != shipped:
        note = (f"{JOBS_DIR}/{CORE_NAME} differs from the shipped core; the vault copy is "
                "used (delete it to restore the shipped one)")
    return target, note


def job_dir(vault: Path, task_id: str, attempt: int) -> Path:
    return vault / JOBS_DIR / task_id / f"attempt-{attempt:03d}"


def render_job(job: dict) -> str:
    return (f"# Support job {job.get('task_id')} (attempt {job.get('attempt')})\n\n"
            "Root-owned contract. Read the rule core named below first; the JSON block is "
            "binding and a missing value means BLOCKED.\n\n"
            "```json\n" + json.dumps(job, indent=1, ensure_ascii=False) + "\n```\n")


def parse_job(text: str) -> tuple[dict | None, str | None]:
    blocks = JSON_BLOCK.findall(text)
    if len(blocks) != 1:
        return None, f"job file must hold exactly one ```json block (found {len(blocks)})"
    try:
        job = json.loads(blocks[0])
    except ValueError as exc:
        return None, f"job JSON block does not parse: {exc}"
    if not isinstance(job, dict):
        return None, "job JSON block must be an object"
    return job, None


def payload_parts(core: str, job_text: str, manifest_text: str, packet_text: str) -> dict:
    parts = {"core": est_tokens(core), "job": est_tokens(job_text),
             "manifest": est_tokens(manifest_text), "packet": est_tokens(packet_text)}
    total = sum(parts.values())
    over = [name for name, value in parts.items() if value > BUDGETS[name]]
    if total > TOTAL_BUDGET:
        over.append("total")
    return {"parts": parts, "total": total, "budgets": dict(BUDGETS),
            "total_budget": TOTAL_BUDGET, "over_budget": over, "note": ESTIMATE_NOTE}


def _nonempty_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _within(name: str, roots: list) -> bool:
    for root in roots:
        if root in (".", ""):
            return True
        if name == root or name.startswith(root.rstrip("/") + "/"):
            return True
    return False


def own_output_dir(job: dict) -> str | None:
    """The job's own default output directory, `.context/jobs/<task>/attempt-NNN/out`."""
    task_id, attempt = job.get("task_id"), job.get("attempt")
    if not (isinstance(task_id, str) and TASK_ID.match(task_id) and _positive_int(attempt)):
        return None
    return f"{JOBS_DIR}/{task_id}/attempt-{attempt:03d}/out"


def indexed_sources(vault: Path) -> list | None:
    """Every source path in the vault's index, or None when there is no readable index."""
    index = Path(vault) / ".context" / "index.sqlite"
    if not index.is_file():
        return None
    try:
        connection = sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)
        try:
            return [row[0] for row in connection.execute(
                "SELECT DISTINCT source_path FROM records ORDER BY source_path")]
        finally:
            connection.close()
    except sqlite3.Error:
        return None


def _overlaps(first: str, second: str) -> bool:
    """Equal, or one contains the other ('.' is the whole vault)."""
    if "." in (first, second):
        return True
    first, second = first.rstrip("/"), second.rstrip("/")
    return first == second or first.startswith(second + "/") or second.startswith(first + "/")


def _output_scope(vault: Path, out: str, roots, job: dict) -> tuple[list, list]:
    """(BLOCKED reasons, warnings) for a job's owned_output_dir.

    A worker must never write among the sources it reads or that the index serves:
    the directory may not contain an indexed source or overlap an allowed root.
    Hidden and tool-state folders are refused except the job's own default
    `.context/jobs/<task>/attempt-NNN/out`, which is excluded from retrieval.
    """
    policy, prefixes = _policy(), _prefixes(vault)
    try:
        name = policy.relative_name(out)
    except ValueError as exc:
        return [f"owned_output_dir: {exc}"], []
    blocked: list = []
    warnings: list = []
    if policy.excluded(name) and name != own_output_dir(job):
        blocked.append(f"owned_output_dir may not sit in a hidden folder or the tool's own "
                       f"state: {name} (only this job's {own_output_dir(job)} may)")
    if policy.excluded(name, prefixes):
        return blocked, warnings            # nothing there can be a source
    for root in roots or []:
        try:
            other = root if root == "." else policy.relative_name(root)
        except (ValueError, TypeError):
            continue                        # already BLOCKED as an unusable root
        if _overlaps(name, other):
            blocked.append(f"owned_output_dir {name} overlaps allowed_source_root {root}: "
                           "the worker would write among the sources it reads")
    indexed = indexed_sources(vault)
    if indexed is None:
        target = vault / name
        indexed = sorted(path.relative_to(vault).as_posix() for path in target.rglob("*")
                         if path.is_file() and not path.is_symlink()
                         and not policy.excluded(path.relative_to(vault).as_posix(),
                                                 prefixes)) if target.is_dir() else []
    inside = [source for source in indexed if _within(source, [name])]
    if inside:
        blocked.append(f"owned_output_dir {name} contains indexed source(s): "
                       + ", ".join(inside[:3]) + (" ..." if len(inside) > 3 else ""))
    warnings.append(f"owned_output_dir {name} is not excluded by routes.json: the worker's "
                    "files will be indexed as sources by the next `context-layer index`; "
                    "exclude it, or use the default output directory")
    return blocked, warnings


def validate_job(vault: Path, job_text: str, overrides: dict | None = None) -> dict:
    """OK or BLOCKED with every missing field, mismatch and scope problem listed.

    `overrides` maps vault-relative paths to text not yet on disk (used by
    `job new` to validate before anything is written).
    """
    overrides = overrides or {}
    blocked: list = []
    warnings: list = []

    def read(name: str, label: str) -> str | None:
        if name in overrides:
            return overrides[name]
        try:
            path = _rel(vault, name, label)
        except ValueError as exc:
            blocked.append(str(exc))
            return None
        if not path.is_file():
            blocked.append(f"{label} not found: {name}")
            return None
        return path.read_text(encoding="utf-8")

    job, problem = parse_job(job_text)
    report = {"schema": "support-job-validation/v1", "status": "BLOCKED",
              "job_sha256": sha256_bytes(job_text.encode("utf-8")),
              "blocked": blocked, "warnings": warnings, "payload": None}
    if job is None:
        blocked.append(problem)
        return report
    missing = [key for key in JOB_FIELDS if key not in job or job[key] is None
               or (isinstance(job[key], str) and not job[key].strip())]
    if missing:
        blocked.append("missing mandatory value: " + ", ".join(missing))
    if job.get("schema") not in (None, SCHEMA_JOB):
        blocked.append(f"schema must be {SCHEMA_JOB}")
    if job.get("task_id") is not None and not (isinstance(job["task_id"], str)
                                               and TASK_ID.match(job["task_id"])):
        blocked.append("task_id must be 1-80 of A-Z a-z 0-9 . _ -")
    for key in ("attempt", "max_initial_application_tokens", "max_total_usage_tokens",
                "max_elapsed_seconds", "max_evidence_bytes", "return_index_max_tokens"):
        if key in job and job[key] is not None and not _positive_int(job[key]):
            blocked.append(f"{key} must be a positive integer")
    if "may_modify_source" in job and job["may_modify_source"] is not False:
        blocked.append("may_modify_source must be false")
    if job.get("authority") not in (None, "") and job.get("authority") != AUTHORITY:
        blocked.append(f"authority must read exactly: {AUTHORITY}")
    for key in ("exclusions", "known_unknowns"):
        if key in job and job[key] is not None and not (
                isinstance(job[key], list) and all(_nonempty_str(v) for v in job[key])):
            blocked.append(f"{key} must be a list of non-empty strings (empty list allowed)")
    roots = job.get("allowed_source_roots")
    if roots is not None:
        if not (isinstance(roots, list) and roots and all(_nonempty_str(r) for r in roots)):
            blocked.append("allowed_source_roots must be a non-empty list of vault-relative "
                           "paths ('.' for the whole vault)")
            roots = None
        else:
            policy, prefixes = _policy(), _prefixes(vault)
            for root in roots:
                if root == ".":
                    continue
                try:
                    path = _rel(vault, root, "allowed_source_root")
                    if policy.excluded(root, prefixes):
                        blocked.append(f"allowed_source_root is excluded by routes.json or "
                                       f"hidden: {root}")
                    elif not path.exists():
                        blocked.append(f"allowed_source_root does not exist: {root}")
                except ValueError as exc:
                    blocked.append(str(exc))
    out = job.get("owned_output_dir")
    if _nonempty_str(out):
        try:
            _rel(vault, out, "owned_output_dir")
        except ValueError as exc:
            blocked.append(str(exc))
        else:
            problems, notes = _output_scope(vault, out, roots, job)
            blocked += problems
            warnings += notes

    texts = {}
    for key, sha_key, label in (("rule_core_path", "rule_core_sha256", "rule core"),
                                ("input_manifest_path", "input_manifest_sha256",
                                 "input manifest")):
        name = job.get(key)
        if not _nonempty_str(name):
            continue
        text = read(name, label)
        if text is None:
            continue
        texts[key] = text
        actual = sha256_bytes(text.encode("utf-8"))
        if _nonempty_str(job.get(sha_key)) and job[sha_key] != actual:
            blocked.append(f"{label} hash mismatch: {name} is {actual[:12]}, the job pins "
                           f"{str(job[sha_key])[:12]}")
    manifest = None
    if "input_manifest_path" in texts:
        try:
            manifest = json.loads(texts["input_manifest_path"])
        except ValueError:
            blocked.append("input manifest is not JSON")
    if isinstance(manifest, dict):
        pinned = (manifest.get("rule_core") or {}).get("sha256")
        if _nonempty_str(job.get("rule_core_sha256")) and pinned != job["rule_core_sha256"]:
            blocked.append("input manifest pins a different rule core than the job")
        if (manifest.get("packet") or {}).get("id") != job.get("initial_packet_id"):
            blocked.append("input manifest names a different packet than the job")
        if "routes_json_sha256" in manifest and manifest["routes_json_sha256"] != \
                _routes_sha(vault):
            blocked.append(".context/routes.json changed since the job was written; the "
                           "source scope may differ (write a new attempt)")

    packet_text = ""
    packet_id = job.get("initial_packet_id")
    if _nonempty_str(packet_id):
        try:
            expected = packet_path(vault, packet_id).relative_to(vault).as_posix()
        except OrchestrateError as exc:
            blocked.append(str(exc))
            expected = None
        if expected and job.get("initial_packet_path") not in (None, expected):
            blocked.append(f"initial_packet_path must be {expected}")
        if expected:
            served = read_packet(vault, packet_id)
            if not served["served"]:
                blocked.append("initial packet withheld: " + "; ".join(served["reasons"]))
            else:
                packet_text = canonical({k: v for k, v in served.items()
                                         if k in ("id", "schema", "request", "index_identity",
                                                  "graph_identity", "retrieval_status",
                                                  "evidence", "evidence_est_tokens")}
                                        ).decode("utf-8")
                if roots:
                    for item in served["evidence"]:
                        if not _within(item["source_path"], roots):
                            warnings.append(f"packet source outside allowed_source_roots: "
                                            f"{item['source_path']} (records citing it will "
                                            "fail handback check)")
    payload = payload_parts(texts.get("rule_core_path", ""), job_text,
                            texts.get("input_manifest_path", ""), packet_text)
    report["payload"] = payload
    if payload["over_budget"]:
        message = "payload over budget: " + ", ".join(
            f"{name} {payload['parts'].get(name, payload['total'])} > "
            f"{BUDGETS.get(name, TOTAL_BUDGET)}" for name in payload["over_budget"])
        if job.get("payload_over_budget_allowed") is True:
            warnings.append(message + " (allowed by the root)")
        else:
            blocked.append(message)
    cap = job.get("max_initial_application_tokens")
    if _positive_int(cap) and payload["total"] > cap and \
            job.get("payload_over_budget_allowed") is not True:
        blocked.append(f"payload total {payload['total']} exceeds "
                       f"max_initial_application_tokens {cap}")
    report["status"] = "BLOCKED" if blocked else "OK"
    report["task_id"] = job.get("task_id")
    return report


def _new_task_id() -> str:
    return "job-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + \
        secrets.token_hex(3)


def new_job(vault: Path, *, objective: str, packet_id: str, root_goal: str | None,
            allowed_roots: list, exclusions: list, known_unknowns: list,
            method: str | None, acceptance: str | None, stop_when: str | None,
            out: str | None, task_id: str | None, attempt: int,
            max_total_tokens: int | None, max_seconds: int | None,
            max_evidence_bytes: int = MAX_EVIDENCE_BYTES, allow_over: bool = False) -> dict:
    """Build, validate and (only when not BLOCKED) write a support job. Returns the report."""
    vault = _vault(vault)
    task_id = task_id or _new_task_id()
    if not TASK_ID.match(task_id):
        raise OrchestrateError("--task-id must be 1-80 of A-Z a-z 0-9 . _ -")
    if attempt <= 0:
        raise OrchestrateError("--attempt must be positive")
    directory = job_dir(vault, task_id, attempt)
    if (directory / "job.md").exists():
        raise OrchestrateError(f"{directory.relative_to(vault).as_posix()}/job.md exists; "
                               "a job is immutable, use a new --attempt")
    core, core_note = ensure_core(vault)
    core_rel = core.relative_to(vault).as_posix()
    core_sha = sha256_file(core)
    rel_dir = directory.relative_to(vault).as_posix()
    manifest_rel = f"{rel_dir}/input-manifest.json"
    out_rel = out or f"{rel_dir}/out"
    try:
        packet_rel = packet_path(vault, packet_id).relative_to(vault).as_posix()
    except OrchestrateError:
        packet_rel = None
    served = read_packet(vault, packet_id) if packet_rel else {"served": False}
    manifest = {"schema": SCHEMA_MANIFEST, "task_id": task_id, "attempt": attempt,
                "rule_core": {"path": core_rel, "sha256": core_sha},
                "packet": {"id": packet_id, "path": packet_rel,
                           "sources": len(served.get("evidence") or []),
                           "index_identity": served.get("index_identity")},
                "allowed_source_roots": list(allowed_roots),
                "routes_json_sha256": _routes_sha(vault),
                "exclusions": "enforced from .context/routes.json by read_source and "
                              "read_packet",
                "full_source_inventory": "the packet lists every source with its SHA-256; "
                                         "its id pins them"}
    manifest_text = json.dumps(manifest, indent=1, ensure_ascii=False) + "\n"
    job = {"schema": SCHEMA_JOB, "task_id": task_id, "attempt": attempt,
           "root_goal": root_goal, "worker_objective": objective, "authority": AUTHORITY,
           "rule_core_path": core_rel, "rule_core_sha256": core_sha,
           "input_manifest_path": manifest_rel,
           "input_manifest_sha256": sha256_bytes(manifest_text.encode("utf-8")),
           "initial_packet_path": packet_rel, "initial_packet_id": packet_id,
           "allowed_source_roots": list(allowed_roots) or None,
           "exclusions": list(exclusions), "known_unknowns": list(known_unknowns),
           "allowed_method": method or DEFAULT_METHOD, "acceptance_oracle": acceptance,
           "stop_when": stop_when, "owned_output_dir": out_rel, "may_modify_source": False,
           "max_initial_application_tokens": TOTAL_BUDGET,
           "max_total_usage_tokens": max_total_tokens, "max_elapsed_seconds": max_seconds,
           "max_evidence_bytes": max_evidence_bytes,
           "return_index_max_tokens": RETURN_INDEX_MAX_TOKENS}
    if allow_over:
        job["payload_over_budget_allowed"] = True
    job_text = render_job(job)
    report = validate_job(vault, job_text, overrides={manifest_rel: manifest_text})
    report["job_path"] = f"{rel_dir}/job.md"
    report["written"] = False
    if core_note:
        report["warnings"].append(core_note)
    if report["status"] == "OK":
        # job.md first and exclusively: a job is immutable, and of two `job new` runs
        # racing for one attempt exactly one may write it (and then its manifest).
        if not create_exclusive(directory / "job.md", job_text):
            raise OrchestrateError(f"{rel_dir}/job.md exists; a job is immutable, use a "
                                   "new --attempt")
        atomic_write(directory / "input-manifest.json", manifest_text)
        atomic_write(directory / "payload.json",
                     json.dumps(report["payload"], indent=1) + "\n")
        report["written"] = True
    return report


# ---------------------------------------------------------------------------
# 4. Evidence-record returns: mechanical handback check and sampling
# ---------------------------------------------------------------------------

# The claim-span consistency gate: model-free. An observation may paraphrase its
# span, but every hard token it asserts - a number, a date, a time, quoted text,
# a URL, a code identifier, a multi-word name - must also be in the span, after
# NFC, case folding, whitespace and thousands-separator normalisation. An
# observation with no hard token is reported unanchored, never guessed about.

_MONTHS = ("january|february|march|april|may|june|july|august|september|october|"
           "november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec")
_URL = re.compile(r"https?://[^\s<>\"'`]+", re.I)
_QUOTED = re.compile(r"\"([^\"\n]{2,})\"|\u201c([^\u201d\n]{2,})\u201d|"
                     r"\u2018([^\u2019\n]{2,})\u2019|`([^`\n]+)`|(?<!\w)'([^'\n]{2,})'(?!\w)")
_DATE_ISO = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)|(?<!\d)\d{1,2}/\d{1,2}/\d{2,4}(?!\d)")
_DATE_WORDS = re.compile(rf"\b(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?\b|"
                         rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{_MONTHS})\.?"
                         rf"(?:,?\s+\d{{4}})?\b", re.I)
_TIME = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)")
# A number in an observation stands alone (not glued to letters: "48k" and "E1" are not
# numbers); in a span, digits glued to letters still count ("12th", "48k"), so a
# faithful paraphrase is not rejected over a suffix.
_NUMBER = re.compile(r"(?<![\w.,])[$\u20ac\u00a3\u00a5]?((?:\d{1,3}(?:[,'\u00a0\u202f]\d{3})+|\d+)"
                     r"(?:\.\d+)?)(?:\s?%)?(?!\w)")
_SPAN_NUMBER = re.compile(r"(?<![\d.,])((?:\d{1,3}(?:[,'\u00a0\u202f]\d{3})+|\d+)(?:\.\d+)?)")
_MONTH_WORD = re.compile(rf"\b(?:{_MONTHS})\b")
_IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+(?:\(\))?|"
                         r"\b[a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+(?:\(\))?|"
                         r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+(?:\(\))?|\b[A-Za-z_]\w*\(\)")
_WORD = re.compile(r"[^\W\d_][\w'\u2019-]*")
_CONNECTORS = {"of", "the", "de", "la", "le", "du", "van", "von", "der", "den", "da", "di",
               "and", "&"}
_LEADING = {"the", "a", "an", "this", "that", "these", "those", "it", "its", "in", "on",
            "at", "for", "and", "but", "or", "if", "when", "while", "after", "before",
            "our", "their", "his", "her", "each", "every", "no", "some", "any", "all",
            "both", "per", "by", "from", "to", "with", "as", "there", "here", "then",
            "since", "until", "under", "over", "during", "according"}
# Capitalised by grammar, not because they name anything: they break a name run.
_CALENDAR = {"january", "february", "march", "april", "may", "june", "july", "august",
             "september", "october", "november", "december", "monday", "tuesday",
             "wednesday", "thursday", "friday", "saturday", "sunday"}
_THOUSANDS = re.compile(r"(?<=\d)[,'\u00a0\u202f](?=\d{3}(?!\d))")
_LOOSE = re.compile(r"[-_/.'\u2019`]")


def _fold(text: str) -> str:
    """NFC, case-folded, straight quotes, whitespace collapsed to single spaces."""
    text = unicodedata.normalize("NFC", text).casefold()
    text = text.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'") \
        .replace("\u2019", "'")
    return re.sub(r"\s+", " ", text).strip()


def _bounded(needle: str, haystack: str) -> bool:
    """`needle` occurs in `haystack` and is not glued to a word character on either side."""
    if not needle:
        return True
    left = r"(?<!\w)" if (needle[0].isalnum() or needle[0] == "_") else ""
    right = r"(?!\w)" if (needle[-1].isalnum() or needle[-1] == "_") else ""
    return re.search(left + re.escape(needle) + right, haystack) is not None


def _canonical_number(text: str) -> str:
    """'48,000' -> '48000', '007' -> '7', '3.50' -> '3.5': one spelling per value."""
    whole, _, fraction = _THOUSANDS.sub("", text).partition(".")
    whole = whole.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    return whole + ("." + fraction if fraction else "")


def _number_in(core: str, span: str) -> bool:
    wanted = _canonical_number(core)
    return any(_canonical_number(match.group(1)) == wanted
               for match in _SPAN_NUMBER.finditer(unicodedata.normalize("NFC", span)))


def _names(text: str) -> list:
    """Runs of two or more capitalised words (connectors such as 'of' allowed inside),
    leading function words dropped: 'The Harbor Board' gives 'Harbor Board'."""
    words = [(m.group(), m.start(), m.end()) for m in _WORD.finditer(text)]
    runs: list = []
    current: list = []
    for word, start, end in words:
        joined = bool(current) and text[current[-1][2]:start].strip() == ""
        if current and not joined:
            runs.append(current)
            current = []
        if word.casefold() in _CALENDAR:
            if current:
                runs.append(current)
                current = []
            continue
        if word[0].isupper() or (current and word.casefold() in _CONNECTORS):
            current.append((word, start, end))
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    names = []
    for run in runs:
        while run and (run[0][0].casefold() in _LEADING or run[0][0].casefold() in _CONNECTORS):
            run = run[1:]
        while run and run[-1][0].casefold() in _CONNECTORS:
            run = run[:-1]
        if sum(1 for word in run if word[0][0].isupper()) >= 2:
            name = text[run[0][1]:run[-1][2]]
            names.append(re.sub(r"['\u2019]s$", "", name))
    return names


def hard_tokens(observation: str) -> list:
    """[(kind, text as written)] for every hard token the observation asserts."""
    tokens: list = []
    taken: list = []

    def add(kind: str, text: str, start: int, end: int) -> None:
        tokens.append((kind, text.strip()))
        taken.append((start, end))

    for match in _URL.finditer(observation):
        url = match.group().rstrip(".,;:!?)]}'\"")
        add("url", url, match.start(), match.start() + len(url))
    for match in _QUOTED.finditer(observation):
        inner = next(group for group in match.groups() if group is not None)
        add("quoted", inner, match.start(), match.end())
    for pattern, kind in ((_DATE_ISO, "date"), (_DATE_WORDS, "date"), (_TIME, "time")):
        for match in pattern.finditer(observation):
            add(kind, match.group(), match.start(), match.end())
    for match in _IDENTIFIER.finditer(observation):
        parts = match.group().rstrip("()").split(".")
        if "." in match.group() and all(len(part) <= 1 for part in parts):
            continue                        # e.g, i.e, a.m: abbreviations, not identifiers
        if not any(start <= match.start() < end for start, end in taken):
            add("identifier", match.group(), match.start(), match.end())
    for match in _NUMBER.finditer(observation):
        tokens.append(("number", match.group(1)))
    for name in _names(observation):
        tokens.append(("name", name))
    seen: set = set()
    unique = []
    for kind, text in tokens:
        if text and (kind, text) not in seen:
            seen.add((kind, text))
            unique.append((kind, text))
    return unique


def _token_in_span(kind: str, text: str, span: str) -> bool:
    folded = _fold(span)
    if kind == "number":
        return _number_in(text, span)
    if kind == "date":
        numbers = re.findall(r"\d+", text)
        month = _MONTH_WORD.search(text.casefold())
        if month is not None and month.group()[:3] not in {
                found.group()[:3] for found in _MONTH_WORD.finditer(folded)}:
            return False
        return all(_number_in(number, span) for number in numbers)
    if kind == "name":
        loose = lambda value: re.sub(r"\s+", " ", _LOOSE.sub(" ", _fold(value))).strip()  # noqa: E731
        return _bounded(loose(text), loose(span))
    return _bounded(_fold(text), folded)


def claim_problems(observation: str, span: str) -> list:
    """One reason per hard token of the observation that the span does not carry."""
    problems = []
    for kind, text in hard_tokens(observation):
        if not _token_in_span(kind, text, span):
            problems.append(f'observation asserts "{text}"; not in span')
    return problems


def _normalised_kind(block: str, span: str) -> str | None:
    """What differs when the span matches the cited lines only after normalising."""
    crlf = lambda value: value.replace("\r\n", "\n")  # noqa: E731
    nfc = lambda value: unicodedata.normalize("NFC", value)  # noqa: E731
    spaces = lambda value: re.sub(r"\s+", " ", value).strip()  # noqa: E731
    if crlf(span) in crlf(block):
        return "line endings (the file has CRLF where the quote has LF, or the reverse)"
    if nfc(span) in nfc(block):
        return "Unicode normalisation (NFC and NFD spell the same letters with other bytes)"
    if spaces(span) in spaces(block):
        return "whitespace or line breaks"
    if spaces(nfc(crlf(span))) in spaces(nfc(crlf(block))):
        return "whitespace, line endings and Unicode normalisation"
    return None


def _span_problems(text: str, span: str, start: int, end: int) -> tuple[list, str | None]:
    """(reasons, normalised-match kind) for the span rules against the current file."""
    lines = lines_of(text)
    reasons: list = []
    words = len(span.split())
    visible = len("".join(span.split()))
    if words < SPAN_MIN_WORDS and visible < SPAN_MIN_CHARS:
        reasons.append(f"span is too short to anchor a claim ({words} word(s), {visible} "
                       f"characters): quote at least {SPAN_MIN_WORDS} words or "
                       f"{SPAN_MIN_CHARS} non-space characters")
    if end - start > MAX_LINE_SPAN:
        reasons.append(f"lines {start}-{end} cover {end - start + 1} lines; a record cites at "
                       f"most {MAX_LINE_SPAN + 1}")
    if end > len(lines):
        return reasons + [f"lines {start}-{end} do not exist: the file has {len(lines)} lines "
                          "(a line ends at \\n)"], None
    block = "".join(lines[start - 1:end])
    if span not in block:
        where = "elsewhere in the file" if span in text else "nowhere in the file"
        kind = _normalised_kind(block, span)
        if kind:
            reasons.append(f"span matches lines {start}-{end} only after normalising {kind}: "
                           "copy the text exactly as the file has it")
        else:
            reasons.append(f"span not found in lines {start}-{end} (found {where})")
        return reasons, kind
    if not span_touches(text, span, start, end):
        reasons.append(f"span does not reach both line_start {start} and line_end {end}: "
                       "cite the lines the quote occupies")
    return reasons, None


def check_record_detail(vault: Path, record, roots: list, prefixes: list, out_rel: str | None,
                        cache: dict) -> dict:
    """{mechanically_checked, reasons, normalized_match, observation_anchored}."""
    detail = {"mechanically_checked": False, "reasons": [], "normalized_match": None,
              "observation_anchored": None}
    if not isinstance(record, dict):
        detail["reasons"] = ["record is not a JSON object"]
        return detail
    reasons = detail["reasons"]
    missing = [key for key in RECORD_FIELDS if key not in record]
    if missing:
        reasons.append("missing field: " + ", ".join(missing))
    if "root_verified" in record:
        reasons.append("worker set root_verified (only the root may)")
    for key in ("id", "observation", "span"):
        if key in record and not _nonempty_str(record[key]):
            reasons.append(f"{key} must be a non-empty string")
    for key in ("method", "uncertainty"):
        if key in record and not isinstance(record[key], str):
            reasons.append(f"{key} must be a string")
    start, end = record.get("line_start"), record.get("line_end")
    if not (_positive_int(start) and _positive_int(end) and start <= end):
        reasons.append("line_start/line_end must be integers with 1 <= start <= end")
    name = record.get("source_path")
    sha = record.get("source_sha256")
    if not isinstance(sha, str) or not HEX64.match(sha):
        reasons.append("source_sha256 must be 64 lowercase hex characters")
    if not isinstance(name, str):
        reasons.append("source_path must be a string")
        return detail
    policy = _policy()
    try:
        path = policy.source_path(vault, name, prefixes)
    except (ValueError, TypeError) as exc:
        reasons.append(f"source_path refused: {exc}")
        return detail
    if not _within(policy.relative_name(name), roots):
        reasons.append(f"source_path outside allowed_source_roots: {name}")
    if out_rel and _within(policy.relative_name(name), [out_rel]):
        reasons.append("source_path is inside the worker's own output directory")
    if name not in cache:
        try:
            data = path.read_bytes() if path.is_file() else None
            cache[name] = None if data is None else (sha256_bytes(data),
                                                     data.decode("utf-8", "replace"))
        except OSError:
            cache[name] = None
    current = cache[name]
    if current is None:
        reasons.append(f"source file missing: {name}")
        return detail
    if isinstance(sha, str) and sha != current[0]:
        reasons.append(f"source_sha256 does not match the current file ({current[0][:12]})")
    elif not reasons:
        span_reasons, kind = _span_problems(current[1], record["span"], start, end)
        reasons += span_reasons
        detail["normalized_match"] = kind
        if span_at(current[1], record["span"], start, end):
            tokens = hard_tokens(record["observation"])
            detail["observation_anchored"] = bool(tokens)
            reasons += claim_problems(record["observation"], record["span"])
    detail["mechanically_checked"] = not reasons
    return detail


def check_record(vault: Path, record, roots: list, prefixes: list, out_rel: str | None,
                 cache: dict) -> tuple[bool, list]:
    """(mechanically_checked, reasons). True only if every check passes."""
    detail = check_record_detail(vault, record, roots, prefixes, out_rel, cache)
    return detail["mechanically_checked"], detail["reasons"]


def _receipt_problems(receipt, records: int, job: dict | None) -> list:
    if not isinstance(receipt, dict):
        return ["receipt.json is not a JSON object"]
    problems = []
    missing = [key for key in RECEIPT_FIELDS if key not in receipt]
    if missing:
        problems.append("receipt missing: " + ", ".join(missing))
    if receipt.get("schema") != SCHEMA_RECEIPT:
        problems.append(f"receipt schema must be {SCHEMA_RECEIPT}")
    if receipt.get("state") not in STATES:
        problems.append("receipt state must be READY, PARTIAL or BLOCKED")
    if receipt.get("state") in ("PARTIAL", "BLOCKED") and not _nonempty_str(
            receipt.get("blocker")):
        problems.append(f"a {receipt.get('state')} receipt must name its blocker")
    counts = receipt.get("counts")
    if not isinstance(counts, dict) or counts.get("records") != records:
        problems.append(f"receipt counts.records does not equal the {records} records in "
                        "evidence.jsonl")
    if job:
        if receipt.get("task_id") != job.get("task_id") or \
                receipt.get("attempt") != job.get("attempt"):
            problems.append("receipt task_id/attempt do not match the job")
    return problems


def sample(ids: list, k: int, seed: str) -> list:
    """Deterministic draw without replacement: order by SHA-256(seed:id), take k."""
    ranked = sorted(dict.fromkeys(ids), key=lambda i: (sha256_bytes(f"{seed}:{i}".encode()), i))
    return ranked[:max(0, k)]


def min_sample(total: int, defective: int, alpha: float = 0.05) -> int:
    """Smallest n with C(N-D, n) / C(N, n) <= alpha: a zero-defect sample of n then has at
    most alpha chance of missing a lot holding D defective units (hypergeometric)."""
    if total <= 0 or defective <= 0:
        return 0
    for n in range(1, total + 1):
        if math.comb(total - defective, n) / math.comb(total, n) <= alpha:
            return n
    return total


def _read_capped(path: Path, cap: int, label: str, problems: list) -> bytes | None:
    """At most `cap` bytes of a regular file, sized with lstat before anything is read."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        problems.append(f"{label} missing")
        return None
    if not stat.S_ISREG(info.st_mode):
        problems.append(f"{label} is not a regular file (a symlink or a special file); "
                        "not read")
        return None
    if info.st_size > cap:
        problems.append(f"{label} is {info.st_size} bytes, over the {cap} cap; not read")
        return None
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        data = handle.read(cap + 1)
    if len(data) > cap:
        problems.append(f"{label} grew past the {cap} cap while it was read; not used")
        return None
    return data


def check_handback(directory: Path, *, job_file: Path | None = None, vault: Path | None = None,
                   allowed_roots: list | None = None, k: int | str = 0,
                   seed: str | None = None, record: bool = False, jev: bool = False) -> dict:
    """Mechanically check a worker's return directory. Never runs anything it wrote.

    Without `seed` the sample is drawn from fresh randomness at check time, so the
    bytes the worker controls cannot steer which records the root reads; the seed
    and the SHA-256 of evidence.jsonl are in the report, and `seed=` replays the
    draw. With `record=True` the check is appended to the verification ledger. With
    `jev=True` the optional advisor may add advisory notes (`attach_advice`); the
    deterministic fields, the digest and the ledger line are the same either way.
    """
    directory = Path(directory).expanduser().resolve()
    job = None
    if job_file is not None:
        job_text = Path(job_file).read_text(encoding="utf-8")
        job, problem = parse_job(job_text)
        if job is None:
            raise OrchestrateError(problem)
        vault = vault or vault_of(Path(job_file))
        allowed_roots = allowed_roots or job.get("allowed_source_roots")
    if vault is None:
        vault = vault_of(directory)
    if vault is None or not allowed_roots:
        raise OrchestrateError("no scope: pass --job, or --vault with --allowed-root")
    vault = _vault(vault)
    roots = [r if r == "." else _policy().relative_name(r) for r in allowed_roots]
    prefixes = _prefixes(vault)
    out_rel = None
    try:
        out_rel = directory.relative_to(vault).as_posix()
    except ValueError:
        pass
    problems = []
    if job and job.get("owned_output_dir") and out_rel != job["owned_output_dir"]:
        problems.append(f"checked directory is not the job's owned_output_dir "
                        f"({job['owned_output_dir']})")
    limit = (job or {}).get("max_evidence_bytes") or MAX_EVIDENCE_BYTES
    raw = _read_capped(directory / "evidence.jsonl", limit, "evidence.jsonl", problems)
    records = []
    for number, line in jsonl_lines(raw or b""):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            records.append({"_unparsable_line": number})
    receipt_info = {"present": False}
    receipt_problems: list = []
    receipt_raw = _read_capped(directory / "receipt.json", RECEIPT_READ_CAP, "receipt.json",
                               receipt_problems)
    if receipt_raw is not None:
        text = receipt_raw.decode("utf-8", "replace")
        receipt_info = {"present": True, "est_tokens": est_tokens(text)}
        if receipt_info["est_tokens"] > RECEIPT_MAX_TOKENS:
            problems.append(f"receipt.json is {receipt_info['est_tokens']} estimated tokens, "
                            f"over {RECEIPT_MAX_TOKENS}")
        try:
            receipt = json.loads(text)
        except ValueError:
            receipt = None
        problems += _receipt_problems(receipt, len(records), job)
        if isinstance(receipt, dict):
            receipt_info.update({k: receipt.get(k) for k in ("state", "task_id", "attempt",
                                                              "blocker")})
    else:
        problems += receipt_problems
    results = []
    cache: dict = {}
    seen: dict = {}
    for item in records:
        if isinstance(item, dict) and "_unparsable_line" in item:
            results.append({"id": None, "mechanically_checked": False,
                            "reasons": [f"line {item['_unparsable_line']} is not JSON"],
                            "normalized_match": None, "observation_anchored": None})
            continue
        detail = check_record_detail(vault, item, roots, prefixes, out_rel, cache)
        rid = item.get("id") if isinstance(item, dict) else None
        if rid in seen:
            detail["mechanically_checked"] = False
            detail["reasons"].append("duplicate id")
            results[seen[rid]]["mechanically_checked"] = False
            results[seen[rid]]["reasons"].append("duplicate id")
        seen.setdefault(rid, len(results))
        results.append({"id": rid, **detail})
    passed = [r["id"] for r in results if r["mechanically_checked"]]
    unanchored = [r["id"] for r in results
                  if r["mechanically_checked"] and r.get("observation_anchored") is False]
    if k == "auto":
        k = min_sample(len(passed), math.ceil(0.1 * len(passed)))
    if seed:
        seed_value, seed_source = seed, "root (--seed)"
    elif k:
        seed_value = secrets.token_hex(16)
        seed_source = "fresh random seed drawn at check time; replay the draw with --seed"
    else:
        seed_value, seed_source = None, "no sample drawn"
    drawn = sample(passed, int(k), seed_value) if k else []
    by_id = {r.get("id"): r for r in records if isinstance(r, dict)}
    report = {"schema": SCHEMA_CHECK,
              "directory": out_rel or shown(None, directory),
              "vault_relative": out_rel is not None, "receipt": receipt_info,
              "evidence_sha256": sha256_bytes(raw) if raw is not None else None,
              "problems": problems, "records": len(results),
              "mechanically_checked": len(passed), "failed": len(results) - len(passed),
              "observation_unanchored": len(unanchored),
              "results": results,
              "sample": {"k": len(drawn), "seed": seed_value, "seed_source": seed_source,
                         "ids": drawn, "records": [by_id[i] for i in drawn]},
              "ok": not problems and len(passed) == len(results),
              "meaning": "mechanically_checked = a verbatim span at the cited lines of the "
                         "current file, inside the allowed roots, long enough, carrying every "
                         "hard token of the observation. It does not make the observation "
                         "true; critical claims still need root reproduction."}
    if jev:
        attach_advice(vault, records, results, report)
    if record:
        report["ledger"] = ledger_append(vault, {
            "event": "handback", "task_id": (job or {}).get("task_id"),
            "attempt": (job or {}).get("attempt"), "directory": out_rel,
            "evidence_sha256": report["evidence_sha256"],
            "check_sha256": check_digest(report), "seed": seed_value, "sampled": drawn,
            "verdict": "ok" if report["ok"] else "failed", "records": report["records"],
            "mechanically_checked": report["mechanically_checked"]})
    return report


JEV_MEANING = (" jev = an advisor's note (a model's judgement of whether the quote, read "
               "within its section, supports the observation): advisory, never a "
               "verification, and it never changes ok, problems or mechanically_checked.")


def _say_jev(message: str) -> None:
    print(f"context-layer handback check: --jev: {message}", file=sys.stderr)


def attach_advice(vault: Path, records: list, results: list, report: dict) -> None:
    """`handback check --jev`: ask the optional advisor (feature `answer`) about every
    record that passed the mechanical check, and add what it says to the report. Only in
    the advisor's `on` mode (with a calibration receipt) does the report change: each such
    result gains `jev` and the report gains `jev_summary`. In `shadow` the answers are
    counted and nothing is added; off, killed, disabled or failed, the report is the
    deterministic one. A failed record is never asked about, and nothing here can change
    `ok`, `problems`, `mechanically_checked` or the exit code."""
    try:
        from . import jev
        plan, why = jev.answer_plan(vault)
        if plan is None:
            _say_jev(f"{jev.ANSWER_OFF_MESSAGES.get(why, why)}; the report is the "
                     "deterministic check alone")
            return
        items, where = [], []
        for index, (record, result) in enumerate(zip(records, results)):
            if result.get("mechanically_checked") and isinstance(record, dict):
                items.append({"claim": record["observation"], "path": record["source_path"],
                              "sha256": record["source_sha256"],
                              "line_start": record["line_start"],
                              "line_end": record["line_end"], "span": record["span"]})
                where.append(index)
        if not items:
            return
        advice = jev.advise_claims(vault, items, plan)
    except Exception:            # advice never costs the deterministic check
        _say_jev("the advisor could not be used; the report is the deterministic check alone")
        return
    block = advice["jev"]
    if block["mode"] != "on":
        _say_jev("shadow mode: the answers were counted (`context-layer jev report`), not "
                 "added to the report")
        return
    for index, verdict in zip(where, advice["verdicts"]):
        results[index]["jev"] = verdict
    report["jev_summary"] = block
    report["meaning"] += JEV_MEANING


def check_digest(report: dict) -> str:
    """SHA-256 over what a check found: directory, evidence hash, problems, per-record
    results, the sample (k, seed, ids) and the verdict. Wording such as `seed_source`
    and the ledger citation added afterwards, and the advisory `jev` notes, are left out,
    so the same bytes, scope and seed give the same digest on replay."""
    sample = report.get("sample") or {}
    return sha256_bytes(canonical({
        "schema": report.get("schema"), "directory": report.get("directory"),
        "evidence_sha256": report.get("evidence_sha256"), "problems": report.get("problems"),
        # advisory `jev` notes are not part of what the deterministic check found
        "results": [{k: v for k, v in r.items() if k != "jev"} if isinstance(r, dict) else r
                    for r in report.get("results") or []],
        "ok": report.get("ok"),
        "sample": {"k": sample.get("k"), "seed": sample.get("seed"),
                   "ids": sample.get("ids")}}))


def render_check(report: dict) -> str:
    receipt = report["receipt"]
    lines = [f"handback {report['directory']}: receipt "
             + (f"{receipt.get('state')} task {receipt.get('task_id')} attempt "
                f"{receipt.get('attempt')} ({receipt.get('est_tokens')} est tokens)"
                if receipt.get("present") else "MISSING")]
    for problem in report["problems"]:
        lines.append(f"  PROBLEM {problem}")
    lines.append(f"records {report['records']}: mechanically_checked "
                 f"{report['mechanically_checked']}, failed {report['failed']}")
    for result in report["results"]:
        if not result["mechanically_checked"]:
            lines.append(f"  FAIL {result['id']}: {'; '.join(result['reasons'])}")
    summary = report.get("jev_summary")
    if summary:
        lines.append(f"advisor ({summary['provider_kind']}, {summary['mode']}): advisory "
                     "notes only, never a verification")
        for result in report["results"]:
            note = result.get("jev")
            if note:
                extra = "" if note["p_yes"] is None else f", p_yes {note['p_yes']}"
                code = f" ({note['code']})" if note["code"] else ""
                lines.append(f"  ADVISORY {result['id']}: {note['verdict']}{extra}{code}")
    if report.get("observation_unanchored"):
        lines.append(f"  {report['observation_unanchored']} checked record(s) assert no hard "
                     "token; their observations were not compared with their spans")
    drawn = report["sample"]
    if drawn["k"]:
        lines.append(f"sample for the root to read in full ({drawn['k']} of "
                     f"{report['mechanically_checked']} checked, seed {drawn['seed']}, "
                     f"{drawn['seed_source']}): {' '.join(map(str, drawn['ids']))}")
        for record in drawn["records"]:
            lines.append("  " + json.dumps(record, ensure_ascii=False))
    if report.get("ledger"):
        lines.append(f"ledger line {report['ledger']['n']} sha256 {report['ledger']['sha256']}")
    lines.append(report["meaning"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. Delegate-or-not estimator
# ---------------------------------------------------------------------------

CHECKLIST = (
    "Authorship (mandatory): is the job only location, enumeration, measurement, exact "
    "transformation or an approved check - never judgement or final authorship?",
    "Deterministic first: does a ready command (search, hash, diff, count, test) already do "
    "it? Then run the command; do not delegate.",
    "Verification (mandatory): can the root check decisive results from compact anchors "
    "(handback check + samples) instead of redoing the investigation?",
    "Avoided work (mandatory): what does the worker let the root NOT read or do? If "
    "nothing, a cheaper worker is still extra work.",
    "Scope (mandatory): allowed sources, exclusions, unknowns, acceptance oracle and stop "
    "condition all fit the job without the worker inventing policy?",
    "Independence: for parallel workers, disjoint frozen inputs and file ownership, no need "
    "for each other's provisional conclusions?",
    "Return (mandatory): can the full result come back as a small receipt plus durable "
    "evidence records, without dropping findings?",
    "All-in estimate (mandatory): startup, verification, retries and interruptions fit "
    "inside the avoided work with a buffer?",
)


def estimate(*, root_read: float, reread: float, price_ratio: float, startup: float,
             verify: float, worker_read: float | None = None, dispatch: float = 0.0,
             worker_output: float = 0.0, output_multiplier: float = 5.0,
             integration: float = 0.0, retry_rate: float = 0.0, buffer: float = 0.2) -> dict:
    """The delegation break-even condition in root-input-token equivalents.

        direct    = W
        delegated = D + (1 + q) * r * (S + Wk + m * O) + V + f * W + I
        delegate only when direct - delegated > buffer * direct

    W root-read tokens of the direct arm (its own discovery and checking),
    f re-read fraction, r worker/root input price ratio, S worker startup
    payload, Wk worker reading (default W), O worker output at m x input price,
    V root verification reading, D root dispatch, I integration, q retry rate.
    Work common to both arms (C0) is left out on purpose.
    """
    for name, value in (("root_read", root_read), ("price_ratio", price_ratio),
                        ("startup", startup), ("verify", verify), ("dispatch", dispatch),
                        ("worker_output", worker_output), ("integration", integration),
                        ("output_multiplier", output_multiplier), ("buffer", buffer),
                        ("retry_rate", retry_rate)):
        if value < 0:
            raise OrchestrateError(f"{name} must not be negative")
    if not 0 <= reread <= 1:
        raise OrchestrateError("reread fraction must be between 0 and 1")
    worker_read = root_read if worker_read is None else worker_read
    worker = (1 + retry_rate) * price_ratio * (startup + worker_read
                                               + output_multiplier * worker_output)
    reread_cost = reread * root_read
    delegated = dispatch + worker + verify + reread_cost + integration
    saving = root_read - delegated
    verdict = "DELEGATE" if saving > buffer * root_read else "DO IT YOURSELF"
    breakeven = None
    if root_read > 0:
        breakeven = 1 - price_ratio * worker_read / root_read
    return {"verdict": verdict, "direct": root_read, "delegated": round(delegated, 2),
            "terms": {"dispatch": dispatch, "worker": round(worker, 2), "verify": verify,
                      "reread": round(reread_cost, 2), "integration": integration},
            "saving": round(saving, 2), "required_saving": round(buffer * root_read, 2),
            "input_only_breakeven_reread_fraction": None if breakeven is None
            else round(breakeven, 4),
            "note": "an estimate in root-input-token equivalents from the numbers given; "
                    "not a measurement and not a quality judgement"}


def render_estimate(result: dict, args: dict) -> str:
    t = result["terms"]
    lines = [
        f"{result['verdict']}  (estimate; root-input-token equivalents)",
        f"  direct    W = {result['direct']:g}",
        f"  delegated = dispatch {t['dispatch']:g} + worker {t['worker']:g} "
        f"[(1+q)*r*(S+Wk+m*O) = (1+{args['retry_rate']:g})*{args['price_ratio']:g}*"
        f"({args['startup']:g}+{args['worker_read']:g}+{args['output_multiplier']:g}*"
        f"{args['worker_output']:g})] + verify {t['verify']:g} + reread {t['reread']:g} "
        f"[f*W = {args['reread']:g}*{result['direct']:g}] + integration {t['integration']:g}"
        f" = {result['delegated']:g}",
        f"  saving    = {result['saving']:g}; required (buffer {args['buffer']:g} x W) = "
        f"{result['required_saving']:g}",
    ]
    if result["input_only_breakeven_reread_fraction"] is not None:
        lines.append(f"  input-only break-even: re-reading more than "
                     f"{result['input_only_breakeven_reread_fraction']:.0%} of W leaves no "
                     "input saving to pay for startup (a conditional calculation, not a rule)")
    lines.append(f"  {result['note']}")
    lines.append("")
    lines.append("Before dispatch, answer every question (unknown = narrow the job or keep it):")
    lines += [f"  [{number}] {question}" for number, question in enumerate(CHECKLIST, 1)]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 6. Handoff file
# ---------------------------------------------------------------------------

def _file_identity(vault: Path, path: Path) -> str:
    rel = path.relative_to(vault).as_posix() if path.is_relative_to(vault) else path.name
    if not path.is_file():
        return f"{rel} (absent)"
    return f"{rel} sha256 {sha256_file(path)}"


def write_handoff(job_file: Path, *, state: str, done: str, next_action: str,
                  not_established: list, producer: str = "worker", consumer: str = "root",
                  predecessors: list | None = None, blocker: str | None = None,
                  check: dict | None = None, out: Path | None = None) -> dict:
    """The minimal control file. Refuses (writes nothing) above HANDOFF_MAX_TOKENS.

    It names the verification ledger's head line, so a copy of the handoff kept
    outside the vault anchors the ledger as it stood.
    """
    job_file = Path(job_file).expanduser().resolve()
    job_text = job_file.read_text(encoding="utf-8")
    job, problem = parse_job(job_text)
    if job is None:
        raise OrchestrateError(problem)
    vault = vault_of(job_file)
    if vault is None:
        raise OrchestrateError("the job file must sit under <vault>/.context/")
    if state not in STATES:
        raise OrchestrateError("--state must be READY, PARTIAL or BLOCKED")
    if not _nonempty_str(done) or not _nonempty_str(next_action) or not not_established:
        raise OrchestrateError("--done, --next and at least one --not-established are required")
    if state != "READY" and not _nonempty_str(blocker):
        raise OrchestrateError(f"a {state} handoff must name --blocker")
    validation = validate_job(vault, job_text)
    inputs = "unchanged since the job was written" if validation["status"] == "OK" else \
        "INVALID: " + "; ".join(validation["blocked"])
    owned = _rel(vault, job["owned_output_dir"], "owned_output_dir")
    out = Path(out).expanduser().resolve() if out else owned / "handoff.md"
    counts = "no receipt"
    receipt = owned / "receipt.json"
    if receipt.is_file():
        try:
            data = json.loads(receipt.read_text(encoding="utf-8"))
            counts = json.dumps(data.get("counts"), sort_keys=True)
        except ValueError:
            counts = "receipt unreadable"
    coverage = owned / "coverage.json"
    gaps = "no coverage.json"
    if coverage.is_file():
        try:
            data = json.loads(coverage.read_text(encoding="utf-8"))
            gaps = ", ".join(f"{key} {len(data.get(key) or [])}" for key in
                             ("planned", "scanned", "excluded", "failed", "unprocessed"))
        except ValueError:
            gaps = "coverage.json unreadable"
    if check:
        checked = [r["id"] for r in check.get("results", []) if r["mechanically_checked"]]
        failed = [r["id"] for r in check.get("results", []) if not r["mechanically_checked"]]
        verification = (f"handback check: {len(checked)} mechanically checked, failed "
                        f"{', '.join(map(str, failed)) or 'none'}; sampled for root reading: "
                        f"{', '.join(map(str, check.get('sample', {}).get('ids', []))) or 'none'}")
    else:
        verification = "no handback check recorded"
    head = ledger_head(vault)
    ledger = (f"line {head['n']} sha256 {head['sha256']}" if head else
              "no ledger yet (nothing verified or recorded in this vault)")
    lines = [
        f"# Handoff {job['task_id']} attempt {job['attempt']}", "",
        f"- schema: {SCHEMA_HANDOFF}",
        f"- producer: {producer}; consumer: {consumer}; created: {_utc()}",
        f"- predecessors: {', '.join(predecessors or []) or 'none'}", "",
        "## Purpose and authority",
        f"- root goal: {job.get('root_goal')}",
        f"- worker objective: {job.get('worker_objective')}",
        f"- authority: {AUTHORITY}", "",
        "## Inputs",
        f"- job: {_file_identity(vault, job_file)}",
        f"- rule core: {job.get('rule_core_path')} sha256 {job.get('rule_core_sha256')}",
        f"- input manifest: {job.get('input_manifest_path')} sha256 "
        f"{job.get('input_manifest_sha256')}",
        f"- packet: {job.get('initial_packet_path')}",
        f"- input state: {inputs}", "",
        "## Execution",
        f"- state: {state}" + (f"; blocker: {blocker}" if blocker else ""),
        f"- done: {done}", "",
        "## Outputs",
        *[f"- {_file_identity(vault, owned / name)}" for name in
          ("receipt.json", "evidence.jsonl", "coverage.json", "return-index.md")],
        "", "## Completeness",
        f"- receipt counts: {counts}",
        f"- coverage: {gaps}",
        *[f"- does NOT establish: {item}" for item in not_established],
        "", "## Verification",
        f"- {verification}",
        f"- ledger head: {ledger}",
        "- Worker completion is not root approval. Nothing here is verified until the root "
        "records it in its own verification file.", "",
        "## Continuation",
        f"- next permitted action: {next_action}",
    ]
    text = "\n".join(lines) + "\n"
    tokens = est_tokens(text)
    if tokens > HANDOFF_MAX_TOKENS:
        raise OrchestrateError(f"handoff would be {tokens} estimated tokens, over "
                               f"{HANDOFF_MAX_TOKENS}; shorten --done/--not-established and "
                               "point to artifacts instead")
    atomic_write(out, text)
    return {"path": str(out), "shown": shown(vault, out), "est_tokens": tokens,
            "input_state": inputs, "ledger_head": head}


# ---------------------------------------------------------------------------
# Hooks for context_layer.tasks (`tasks new --packet/--job`, `tasks verify`)
# ---------------------------------------------------------------------------

def task_packet(vault: Path, packet_id: str, goal: str, patterns: list, allowed,
                bounds: dict, notes: list, schema: str) -> dict:
    """A tasks packet.json built from a shared packet instead of a fresh search.

    The shared packet is served only after its re-check; sources outside the
    task's allowed list are dropped and listed, exactly like a fresh packet.
    """
    served = read_packet(vault, packet_id)
    if not served["served"]:
        raise OrchestrateError("shared packet withheld: " + "; ".join(served["reasons"]))
    evidence, dropped = [], []
    for item in served["evidence"]:
        if allowed is not None and item["source_path"] not in allowed:
            dropped.append(item["source_path"])
            continue
        evidence.append({k: item[k] for k in ("source_path", "source_sha256", "content",
                                              "line_start", "line_end")})
    return {"schema": schema, "built_at": _utc(), "method": "shared-packet",
            "shared_packet_id": packet_id, "goal": goal, "sources": list(patterns),
            "allowed_sources": list(allowed or []), "bounds": dict(bounds),
            "retrieval_status": served.get("retrieval_status"), "evidence": evidence,
            "dropped_outside_spec": sorted(dict.fromkeys(dropped)), "notes": list(notes)}


def task_job(vault: Path, job_file) -> dict:
    """What `tasks new --job` needs from a validated job; BLOCKED jobs are refused.

    A root that names a file is used as its own glob; a directory root becomes
    `root/**/*`. The job's time limit and usage cap travel with it.
    """
    vault = Path(vault).resolve()
    path = Path(job_file).expanduser().resolve()
    if vault_of(path) != vault:
        raise OrchestrateError("the job file must sit under this vault's .context/jobs/")
    text = path.read_text(encoding="utf-8")
    report = validate_job(vault, text)
    if report["status"] != "OK":
        raise OrchestrateError("job is BLOCKED: " + "; ".join(report["blocked"]))
    job, _ = parse_job(text)
    core = _rel(vault, job["rule_core_path"], "rule core").read_text(encoding="utf-8")
    roots = job["allowed_source_roots"]
    globs = []
    if "." not in roots:
        for root in roots:
            name = root.rstrip("/")
            globs.append(name if (vault / name).is_file() else f"{name}/**/*")
    preface = ("## Worker rule core\n\n" + core.strip() + "\n\n## Job\n\n" + text.strip()
               + "\n")
    return {"goal": job["worker_objective"], "packet_id": job["initial_packet_id"],
            "output_dir": job["owned_output_dir"], "sources": globs,
            "own_output_dir": own_output_dir(job),
            "max_elapsed_seconds": job.get("max_elapsed_seconds"),
            "max_total_usage_tokens": job.get("max_total_usage_tokens"),
            "job": {"path": path.relative_to(vault).as_posix(),
                    "sha256": report["job_sha256"], "task_id": job["task_id"],
                    "attempt": job["attempt"]},
            "preface": preface}


def modified_source_problems(vault: Path, outputs: list) -> list:
    """may_modify_source is false: a `modified` output that is an indexed source, or that
    sits where the index would pick it up, is a source edit."""
    policy, prefixes = _policy(), _prefixes(vault)
    indexed = set(indexed_sources(vault) or [])
    problems = []
    for item in outputs:
        if item.get("change") != "modified":
            continue
        name = item.get("path")
        if name in indexed:
            problems.append(f"modified an indexed source: {name} (the job says "
                            "may_modify_source: false)")
        elif isinstance(name, str) and not policy.excluded(name, prefixes):
            problems.append(f"modified a vault file outside the tool's state: {name} (the job "
                            "says may_modify_source: false)")
    return problems


def task_handback(vault: Path, task: dict) -> tuple[list, dict | None]:
    """(`tasks verify` problems for a job task, the handback report or None)."""
    info = task.get("job")
    if not isinstance(info, dict):
        return [], None
    vault = Path(vault).resolve()
    try:
        path = _rel(vault, info.get("path"), "job")
    except ValueError as exc:
        return [f"job path refused: {exc}"], None
    if not path.is_file() or sha256_file(path) != info.get("sha256"):
        return [f"job file changed or missing since dispatch: {info.get('path')}"], None
    try:
        report = check_handback(vault / task["output_dir"], job_file=path)
    except (OrchestrateError, OSError, ValueError) as exc:
        return [f"handback check could not run: {exc}"], None
    problems = [f"handback: {problem}" for problem in report["problems"]]
    problems += [f"handback record {r['id']}: {'; '.join(r['reasons'])}"
                 for r in report["results"] if not r["mechanically_checked"]]
    return problems, report


def task_handback_problems(vault: Path, task: dict) -> list:
    """`tasks verify` problems for a job task: job changed, or the handback fails."""
    return task_handback(vault, task)[0]


# ---------------------------------------------------------------------------
# 7. Verification ledger: hash-chained, append-only
# ---------------------------------------------------------------------------
#
# One JSON object per line, ASCII only, each carrying `prev`, the SHA-256 of the
# previous line's bytes, and `n`, its 1-based line number. A verdict whose line is
# missing, whose chain is broken, or whose line says otherwise is UNATTESTED in
# `tasks list` and `tasks show`. The same user can rewrite this file too: the
# chain makes an edit visible, not impossible. Keep the head hash somewhere else
# (a commit, or the handoff file, which names it) to anchor it.

def ledger_path(vault: Path) -> Path:
    return Path(vault) / LEDGER_NAME


@contextmanager
def _ledger_lock(vault: Path):
    directory = ledger_path(vault).parent
    directory.mkdir(parents=True, exist_ok=True)
    with file_lock(directory / ".ledger.lock"):
        yield


def _last_line(path: Path) -> tuple[bytes | None, bool]:
    """(the last line's bytes, whether the file ends with a newline), reading only its tail."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None, True
    if size == 0:
        return None, True
    with open(path, "rb") as handle:
        window = 65536
        while True:
            start = max(0, size - window)
            handle.seek(start)
            chunk = handle.read(size - start)
            complete = chunk.endswith(b"\n")
            body = chunk[:-1] if complete else chunk
            cut = body.rfind(b"\n")
            if cut >= 0 or start == 0:
                return body[cut + 1:], complete
            window *= 4


def _actor() -> dict:
    tool = (os.environ.get("CONTEXT_LAYER_TOOL") or "").strip() or "cli"
    session = (os.environ.get("CONTEXT_LAYER_SESSION") or "").strip() or None
    return {"tool": tool, "session": session}


def ledger_head(vault: Path) -> dict | None:
    """{"n", "sha256"} of the last ledger line, or None when there is no ledger."""
    last, _ = _last_line(ledger_path(vault))
    if last is None:
        return None
    try:
        number = json.loads(last).get("n")
    except (ValueError, AttributeError):
        number = None
    return {"n": number, "sha256": sha256_bytes(last)}


def ledger_append(vault: Path, event: dict) -> dict:
    """Append one chained line under the ledger lock; return {"n", "sha256", "prev"}."""
    path = ledger_path(vault)
    with _ledger_lock(vault):
        last, complete = _last_line(path)
        if last is not None and not complete:
            raise OrchestrateError(f"{LEDGER_NAME} ends in a torn line; check it with "
                                   "`context-layer tasks ledger <vault>` before appending")
        if last is None:
            prev, number = None, 1
        else:
            try:
                previous = json.loads(last)
                number = int(previous["n"]) + 1
            except (ValueError, KeyError, TypeError) as exc:
                raise OrchestrateError(f"the last line of {LEDGER_NAME} is not a ledger line "
                                       f"({type(exc).__name__}); check it with "
                                       "`context-layer tasks ledger <vault>`") from None
            prev = sha256_bytes(last)
        line = {"schema": SCHEMA_LEDGER, "n": number, **event, **_actor(), "ts": _utc(),
                "prev": prev}
        data = json.dumps(line, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True).encode("ascii")
        created = not path.exists()
        with open(path, "ab") as handle:
            handle.write(data + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        if created:
            fsync_dir(path.parent)
    return {"n": number, "sha256": sha256_bytes(data), "prev": prev}


def read_ledger(vault: Path) -> tuple[list, list]:
    """([{"n", "sha256", "record"}], chain problems) for the whole ledger."""
    path = ledger_path(vault)
    if not path.is_file():
        return [], []
    data = path.read_bytes()
    lines = data.split(b"\n")
    problems: list = []
    if lines and lines[-1] == b"":
        lines.pop()
    elif lines:
        problems.append(f"line {len(lines)} has no newline (a torn write?)")
    entries: list = []
    prev = None
    for number, raw in enumerate(lines, start=1):
        sha = sha256_bytes(raw)
        try:
            record = json.loads(raw)
        except ValueError:
            problems.append(f"line {number} is not JSON")
            prev = sha
            continue
        if not isinstance(record, dict) or record.get("schema") != SCHEMA_LEDGER:
            problems.append(f"line {number} is not a {SCHEMA_LEDGER} line")
        elif record.get("prev") != prev:
            problems.append(f"line {number} does not chain to line {number - 1} (its prev "
                            "is not that line's SHA-256): the ledger was edited")
        elif record.get("n") != number:
            problems.append(f"line {number} says it is line {record.get('n')}")
        if isinstance(record, dict):
            entries.append({"n": number, "sha256": sha, "record": record})
        prev = sha
    return entries, problems


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _payload_table(payload: dict) -> list:
    lines = [f"payload ({payload['note']})", "  part       est_tokens  budget"]
    for name, value in payload["parts"].items():
        flag = "  OVER" if value > BUDGETS[name] else ""
        lines.append(f"  {name:<9} {value:>11} {BUDGETS[name]:>7}{flag}")
    flag = "  OVER" if payload["total"] > TOTAL_BUDGET else ""
    lines.append(f"  {'total':<9} {payload['total']:>11} {TOTAL_BUDGET:>7}{flag}")
    return lines


def _print_validation(report: dict) -> None:
    print(f"{report['status']}  job {report.get('job_path') or ''} sha256 {report['job_sha256']}")
    for item in report["blocked"]:
        print(f"  BLOCKED {item}")
    for item in report["warnings"]:
        print(f"  warn    {item}")
    if report.get("payload"):
        print("\n".join(_payload_table(report["payload"])))


def cmd_packet_build(args: argparse.Namespace) -> int:
    # Same flag rules, and the same messages, as `install --hook --method synaptic`.
    if (args.compact or args.extra_tokens is not None) and args.method != "synaptic":
        print("context-layer packet build: --extra-tokens and --compact need --method synaptic",
              file=sys.stderr)
        return 2
    if args.extra_tokens is not None and (args.extra_tokens < 0 or args.compact):
        print("context-layer packet build: --extra-tokens sizes the default synaptic packet; "
              "it needs a value >= 0 and no --compact", file=sys.stderr)
        return 2
    if args.method == "synaptic" and args.budget_tokens is not None and not args.compact:
        print("context-layer packet build: --budget-tokens sizes only the --compact synaptic "
              "packet and needs a positive value; the default synaptic packet is sized by "
              "--extra-tokens", file=sys.stderr)
        return 2
    budget_tokens = BUILD_BUDGET_TOKENS if args.budget_tokens is None else args.budget_tokens
    result = build_packet(args.vault, args.prompt, args.method, budget_tokens,
                          args.top_k, args.budget, args.per_source,
                          extra_tokens=args.extra_tokens, compact=args.compact)
    if args.json:
        print(json.dumps({k: v for k, v in result.items() if k != "packet"}, indent=2))
        return 0
    evidence = result["packet"]["evidence"]
    print(f"packet {result['id']}")
    print(f"  {'reused' if result['reused'] else 'written'}  {result['path']}")
    print(f"  est_tokens {result['est_tokens']} (served JSON; evidence text "
          f"{result['packet']['evidence_est_tokens']}); {ESTIMATE_NOTE}")
    print(f"  {len(evidence)} passage(s) from "
          f"{len({e['source_path'] for e in evidence})} source(s), "
          f"status {result['packet']['retrieval_status']}")
    return 0


def _target(args: argparse.Namespace) -> tuple:
    values = list(args.target)
    if len(values) == 2:
        return _vault(values[0]), values[1]
    if len(values) == 1:
        return _vault(args.vault or os.environ.get("CONTEXT_LAYER_VAULT") or "."), values[0]
    raise OrchestrateError("expected VAULT ID, or ID with --vault")


def cmd_packet_show(args: argparse.Namespace) -> int:
    vault, packet_id = _target(args)
    served = read_packet(vault, packet_id)
    print(json.dumps(served, indent=None if args.compact else 1, ensure_ascii=False))
    return 0 if served["served"] else 1


def cmd_job_new(args: argparse.Namespace) -> int:
    report = new_job(args.vault, objective=args.objective, packet_id=args.packet,
                     root_goal=args.root_goal, allowed_roots=args.allowed_root,
                     exclusions=args.exclude, known_unknowns=args.known_unknown,
                     method=args.method, acceptance=args.acceptance,
                     stop_when=args.stop_when, out=args.out, task_id=args.task_id,
                     attempt=args.attempt, max_total_tokens=args.max_total_tokens,
                     max_seconds=args.max_seconds, max_evidence_bytes=args.max_evidence_bytes,
                     allow_over=args.allow_over)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        _print_validation(report)
        print(f"  {'written' if report['written'] else 'NOT written'}  {report['job_path']}")
    return 0 if report["written"] else 1


def cmd_job_validate(args: argparse.Namespace) -> int:
    path = Path(args.file).expanduser().resolve()
    vault = _vault(args.vault) if args.vault else vault_of(path)
    if vault is None:
        raise OrchestrateError("cannot tell the vault from the job path; pass --vault")
    report = validate_job(vault, path.read_text(encoding="utf-8"))
    report["job_path"] = shown(vault, path)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        _print_validation(report)
    return 0 if report["status"] == "OK" else 1


def cmd_job_estimate(args: argparse.Namespace) -> int:
    values = {"root_read": args.root_read_tokens, "reread": args.reread_fraction,
              "price_ratio": args.worker_price_ratio, "startup": args.startup_tokens,
              "verify": args.verify_tokens, "worker_read": args.worker_read_tokens,
              "dispatch": args.dispatch_tokens, "worker_output": args.worker_output_tokens,
              "output_multiplier": args.output_multiplier,
              "integration": args.integration_tokens, "retry_rate": args.retry_rate,
              "buffer": args.buffer}
    result = estimate(**values)
    if values["worker_read"] is None:
        values["worker_read"] = values["root_read"]
    if args.json:
        print(json.dumps({**result, "inputs": values}, indent=2))
    else:
        print(render_estimate(result, values))
    return 0


def cmd_handback_check(args: argparse.Namespace) -> int:
    k = args.sample
    if k != "auto":
        try:
            k = int(k)
        except ValueError as exc:
            raise OrchestrateError("--sample must be an integer or 'auto'") from exc
        if k < 0:
            raise OrchestrateError("--sample must not be negative")
    report = check_handback(Path(args.dir), job_file=Path(args.job) if args.job else None,
                            vault=Path(args.vault) if args.vault else None,
                            allowed_roots=args.allowed_root or None, k=k, seed=args.seed,
                            record=args.record, jev=args.jev)
    if args.out:
        atomic_write(Path(args.out).expanduser(),
                     json.dumps(report, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps(report, indent=1, ensure_ascii=False) if args.json else render_check(report))
    return 0 if report["ok"] else 1


def cmd_handoff_write(args: argparse.Namespace) -> int:
    check = None
    if args.check:
        check = json.loads(Path(args.check).expanduser().read_text(encoding="utf-8"))
    result = write_handoff(Path(args.job), state=args.state, done=args.done,
                           next_action=args.next, not_established=args.not_established,
                           producer=args.producer, consumer=args.consumer,
                           predecessors=args.predecessor, blocker=args.blocker, check=check,
                           out=Path(args.out) if args.out else None)
    print(f"handoff {result['shown']} ({result['est_tokens']} est tokens <= "
          f"{HANDOFF_MAX_TOKENS}); inputs {result['input_state']}")
    head = result["ledger_head"]
    print(f"ledger head {'line ' + str(head['n']) + ' sha256 ' + head['sha256'] if head else 'none'}"
          "  (keep this line outside the vault to anchor the ledger)")
    return 0


def _guarded(function):
    def wrapper(args: argparse.Namespace) -> int:
        extra = [token for token in getattr(args, "rest", []) if token]
        if extra:
            print(f"context-layer: unrecognised arguments: {' '.join(extra)}", file=sys.stderr)
            return 2
        try:
            return function(args)
        except (OrchestrateError, OSError, ValueError) as exc:
            detail = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
            print(f"context-layer: {detail}", file=sys.stderr)
            return 1
    return wrapper


def register(sub: argparse._SubParsersAction) -> None:
    """Attach `packet`, `job`, `handback` and `handoff` to the CLI."""
    p_packet = sub.add_parser(
        "packet", help="Shared, content-addressed evidence packets for several workers.",
        description="Build one evidence packet and hand workers its id instead of letting "
                    "each search and read again. Stored at .context/packets/<sha256>.json.")
    packet_sub = p_packet.add_subparsers(dest="packet_command", required=True)
    p_build = packet_sub.add_parser("build", help="Retrieve once and store by content id.")
    p_build.add_argument("vault")
    p_build.add_argument("--prompt", required=True)
    p_build.add_argument("--method", choices=["fts", "synaptic"], default="fts")
    p_build.add_argument("--budget-tokens", type=int, default=None, metavar="N",
                         help="Estimated-token budget (ceil(chars/4)): caps fts characters at "
                              f"4x this, or sizes the --compact synaptic packet. Default "
                              f"{BUILD_BUDGET_TOKENS}. Not used by the default synaptic "
                              "packet (see --extra-tokens).")
    p_build.add_argument("--extra-tokens", type=int, default=None, metavar="N",
                         help="--method synaptic only: estimated-token budget for link-graph "
                              "extras added after the unchanged fts packet (default: the "
                              "search default, 600).")
    p_build.add_argument("--compact", action="store_true",
                         help="--method synaptic only: the compact packer (passages within "
                              "--budget-tokens) instead of fts packet + extras.")
    p_build.add_argument("--top-k", type=int, default=3)
    p_build.add_argument("--budget", type=int, default=6000, help="Evidence characters.")
    p_build.add_argument("--per-source", type=int, default=2000)
    p_build.add_argument("--json", action="store_true")
    p_build.set_defaults(func=_guarded(cmd_packet_build), forward_to=None)
    p_show = packet_sub.add_parser("show", help="Serve a packet after re-checking its sources.")
    p_show.add_argument("target", nargs="+", metavar="ID", help="VAULT ID, or ID with --vault.")
    p_show.add_argument("--vault", default=None)
    p_show.add_argument("--compact", action="store_true", help="One-line JSON.")
    p_show.set_defaults(func=_guarded(cmd_packet_show), forward_to=None)

    p_job = sub.add_parser(
        "job", help="support-job/v1 contracts: new, validate, estimate.",
        description="A bounded job for one support worker: lean rule core, job, input "
                    "manifest and a shared packet id, each within an estimated-token budget.")
    job_sub = p_job.add_subparsers(dest="job_command", required=True)
    p_new = job_sub.add_parser("new", help="Write a job; refused if BLOCKED or over budget.")
    p_new.add_argument("vault")
    p_new.add_argument("--objective", required=True, help="One bounded worker objective.")
    p_new.add_argument("--packet", required=True, metavar="ID", help="Shared packet id.")
    p_new.add_argument("--root-goal", default=None, help="What decision this job serves.")
    p_new.add_argument("--allowed-root", action="append", default=[], metavar="REL",
                       help="Vault-relative source root the worker may read ('.' = vault); "
                            "a file names itself.")
    p_new.add_argument("--exclude", action="append", default=[], metavar="TEXT")
    p_new.add_argument("--known-unknown", action="append", default=[], metavar="TEXT")
    p_new.add_argument("--method", default=None, help="Allowed method (default: packet + "
                                                      "read_source only).")
    p_new.add_argument("--acceptance", default=None, help="Acceptance oracle.")
    p_new.add_argument("--stop-when", default=None)
    p_new.add_argument("--out", default=None, metavar="REL",
                       help="Owned output directory (default: the job's own "
                            ".context/jobs/<task>/attempt-NNN/out). It may not contain an "
                            "indexed source or overlap an allowed root.")
    p_new.add_argument("--task-id", default=None)
    p_new.add_argument("--attempt", type=int, default=1)
    p_new.add_argument("--max-total-tokens", type=int, default=None,
                       help="Usage cap, recorded; `tasks` checks it between attempts only "
                            "(no backend enforces a token cap).")
    p_new.add_argument("--max-seconds", type=int, default=None,
                       help="Time limit; `tasks new --job` uses it as the timeout when it is "
                            "the smaller.")
    p_new.add_argument("--max-evidence-bytes", type=int, default=MAX_EVIDENCE_BYTES)
    p_new.add_argument("--allow-over", action="store_true",
                       help="Write even if the payload is over budget (recorded in the job).")
    p_new.add_argument("--json", action="store_true")
    p_new.set_defaults(func=_guarded(cmd_job_new), forward_to=None)
    p_validate = job_sub.add_parser("validate", help="OK or BLOCKED, with every reason.")
    p_validate.add_argument("file")
    p_validate.add_argument("--vault", default=None)
    p_validate.add_argument("--json", action="store_true")
    p_validate.set_defaults(func=_guarded(cmd_job_validate), forward_to=None)
    p_est = job_sub.add_parser("estimate", help="Break-even: DELEGATE or DO IT YOURSELF.")
    p_est.add_argument("--root-read-tokens", type=float, required=True, metavar="W")
    p_est.add_argument("--reread-fraction", type=float, required=True, metavar="F")
    p_est.add_argument("--worker-price-ratio", type=float, required=True, metavar="R",
                       help="Worker input price / root input price.")
    p_est.add_argument("--startup-tokens", type=float, required=True, metavar="S")
    p_est.add_argument("--verify-tokens", type=float, required=True, metavar="V")
    p_est.add_argument("--worker-read-tokens", type=float, default=None, metavar="WK",
                       help="Default: W.")
    p_est.add_argument("--dispatch-tokens", type=float, default=0.0)
    p_est.add_argument("--worker-output-tokens", type=float, default=0.0)
    p_est.add_argument("--output-multiplier", type=float, default=5.0,
                       help="Output price / input price (assumption; default 5).")
    p_est.add_argument("--integration-tokens", type=float, default=0.0)
    p_est.add_argument("--retry-rate", type=float, default=0.0)
    p_est.add_argument("--buffer", type=float, default=0.2,
                       help="Required saving as a share of W (default 0.2).")
    p_est.add_argument("--json", action="store_true")
    p_est.set_defaults(func=_guarded(cmd_job_estimate), forward_to=None)

    p_back = sub.add_parser(
        "handback", help="Mechanically check a worker's evidence records.",
        description="Checks receipt.json and every evidence.jsonl record: allowed root, "
                    "current SHA-256, verbatim span at the stated lines, span long enough to "
                    "anchor a claim, and the observation's hard tokens in the span. Never "
                    "runs anything the worker wrote.")
    back_sub = p_back.add_subparsers(dest="handback_command", required=True)
    p_check = back_sub.add_parser("check", help="Per-record mechanically_checked + summary.")
    p_check.add_argument("dir")
    p_check.add_argument("--job", default=None, help="The job file (gives vault and scope).")
    p_check.add_argument("--vault", default=None)
    p_check.add_argument("--allowed-root", action="append", default=[], metavar="REL")
    p_check.add_argument("--sample", default="0", metavar="K|auto",
                         help="Records to draw for full root reading; 'auto' = the "
                              "zero-defect sample size for 10%% at alpha 0.05.")
    p_check.add_argument("--seed", default=None,
                         help="Replay a draw: without it the seed is fresh randomness drawn at "
                              "check time and printed.")
    p_check.add_argument("--record", action="store_true",
                         help="Append the check (evidence hash, seed, sample, verdict) to the "
                              "verification ledger .context/tasks/LEDGER.jsonl.")
    p_check.add_argument("--out", default=None, help="Also write the JSON report here.")
    p_check.add_argument("--json", action="store_true")
    p_check.add_argument("--jev", action="store_true",
                         help="Ask the optional advisor (docs/jev.md, off by default) whether "
                              "each quote supports its observation. Only its `on` mode adds "
                              "advisory `jev` notes; `shadow` counts and shows nothing; it "
                              "never changes ok, problems, mechanically_checked or the exit "
                              "code, and never turns a failed record into a pass.")
    p_check.set_defaults(func=_guarded(cmd_handback_check), forward_to=None)

    p_handoff = sub.add_parser("handoff", help="Write the minimal handoff control file.")
    handoff_sub = p_handoff.add_subparsers(dest="handoff_command", required=True)
    p_write = handoff_sub.add_parser("write", help=f"<= {HANDOFF_MAX_TOKENS} est tokens.")
    p_write.add_argument("--job", required=True)
    p_write.add_argument("--state", required=True, choices=list(STATES))
    p_write.add_argument("--done", required=True)
    p_write.add_argument("--next", required=True, help="Next permitted action.")
    p_write.add_argument("--not-established", action="append", default=[], metavar="TEXT",
                         required=True)
    p_write.add_argument("--blocker", default=None)
    p_write.add_argument("--producer", default="worker")
    p_write.add_argument("--consumer", default="root")
    p_write.add_argument("--predecessor", action="append", default=[])
    p_write.add_argument("--check", default=None, help="JSON from `handback check --out`.")
    p_write.add_argument("--out", default=None)
    p_write.set_defaults(func=_guarded(cmd_handoff_write), forward_to=None)
