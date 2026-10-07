"""context_layer.rules - one rule text for every agent, and a record of every step.

What this is
------------
A vault that two agent hosts share carries two rule files: `CLAUDE.md`, which
Claude Code reads, and `AGENTS.md`, which Codex and other agents read. By
default they are byte-identical twins. `rules init --single-source` instead
keeps the rules in `AGENTS.md` and makes `CLAUDE.md` an `@AGENTS.md` import,
which Claude Code expands. Either way the agents work from one rule text.
Every meaningful work step is recorded, in the same run, as one short dated
entry in that rule text (inside a marked "Work records" section) and as a
longer entry in `LOG.md`; open work lives in `BACKLOG.md`.

Commands:

- `rules init <vault>`   install the four template files; never overwrites an
                         existing file unless `--force` (which backs it up first).
                         It also adds CLAUDE.md and AGENTS.md to the search
                         exclusions in `.context/routes.json`: the hosts load them
                         at session start, so search would only repeat them.
- `rules check <vault>`  exit 0 only if the rule text is one: identical twins, or
                         a CLAUDE.md that imports an existing AGENTS.md.
- `rules record <vault>` append the same entry to the rule text and LOG.md, under
                         a lock, then re-check parity.
- `rules hook session-start|post-tool-use|stop --vault V`  Claude Code hooks.
- `rules settings --vault V`  print the settings.json hook entries (writes nothing).

What this is NOT
----------------
It does not make an agent follow the rules; it makes a skipped record or a
diverged rule file visible. The Stop hook asks for a record only about vault
files the agent itself wrote with Claude Code's file tools (Write, Edit,
MultiEdit, NotebookEdit, seen by the PostToolUse hook), at most once per path
and session, and never in plan mode. Other changes (the user's own edits, a
sync, a shell command) are reported once, without asking. "Changed" means the
content changed: size and SHA-256 decide, while an unchanged size and
modification time let a file be skipped without reading it, and a modification
time in the future is never trusted. Paths excluded in `.context/routes.json`
are never walked, stored or named. It is not an OS sandbox.

Python 3.10+; standard library only; no network access.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import difflib
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import stat
import sys
import time
import unicodedata



# Imported on first use: most rules commands (and `brain init`, which reads this module)
# never take a lock or name a session file, and these imports cost more than the rest.
def safe_stem(value):
    from .session_evidence import safe_stem as real
    return real(value)


def file_lock(*args, **kwargs):
    from .platform_support import file_lock as real
    return real(*args, **kwargs)

RULE_FILES = ("CLAUDE.md", "AGENTS.md")
LOG_FILE = "LOG.md"
BACKLOG_FILE = "BACKLOG.md"
TEMPLATE_FILES = (*RULE_FILES, LOG_FILE, BACKLOG_FILE)
RECORD_SET = frozenset(TEMPLATE_FILES)      # writing these is recording, not work
SINGLE_SOURCE_TEMPLATE = "CLAUDE.single-source.md"
IMPORT_LINE = "@AGENTS.md"
ROUTES_FILE = ".context/routes.json"
EXCLUSION_KEYS = ("exclude_prefixes", "retrieval_exclude_prefixes")
# Loaded by the hosts at session start (Claude Code: CLAUDE.md; Codex and others:
# AGENTS.md), so retrieval would only repeat them. LOG.md and BACKLOG.md stay searchable.
RULE_EXCLUSIONS = RULE_FILES

START_MARK = "<!-- context-layer:records:start -->"
END_MARK = "<!-- context-layer:records:end -->"
RECORDS_HEADING = "## Work records"
DEFAULT_KEEP = 10                           # entries kept in the rule text; LOG.md keeps all

STATE_NAME = "session-rules.json"
SNAPSHOT_DIR = "session-rules"              # .context/session-rules/<session>.json
LOCK_NAME = "rules.lock"
STATE_VERSION = 2
MAX_SESSIONS = 20
MAX_SNAPSHOTS = 8                           # sessions whose vault snapshot is kept
MAX_WALK = 200_000                          # directory entries examined per walk
MAX_LISTED = 8                              # paths named in one hook message
MAX_TRACKED = 1000                          # paths remembered per session and list
HASH_FILE_CAP = 4 * 1024 * 1024             # larger files: size and mtime only
HASH_RUN_CAP = 256 * 1024 * 1024            # bytes hashed per hook run at most
FUTURE_SKEW_NS = 2_000_000_000              # an mtime this far ahead is "unknown"
LOCK_TIMEOUT = 30.0
STALE_LOCK_S = 120.0                        # an O_EXCL marker older than this is broken
SHORT = 12

ATTRIBUTED_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
POST_TOOL_MATCHER = "|".join(ATTRIBUTED_TOOLS)
HOOK_EVENTS = {"SessionStart": "session-start", "PostToolUse": "post-tool-use",
               "Stop": "stop"}
HOOK_TIMEOUTS = {"SessionStart": 30, "PostToolUse": 10, "Stop": 30}
BRIEF_HOOK_CHARS = 3000

LOG_HEADER = ("# Log\n\n"
              "Dated, append-only record of what was actually done. New entries go at the "
              "end.\n")


class RulesError(ValueError):
    """A refusal a person is meant to read."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _vault(vault) -> Path:
    path = Path(vault).expanduser()
    if not path.is_dir():
        raise RulesError(f"vault not found: {path.name or vault}")
    return path.resolve()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(path: Path) -> str | None:
    try:
        return _digest_bytes(path.read_bytes())
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None


def _decode(name: str, data: bytes) -> str:
    """UTF-8 text, or a one-line refusal that names the file (never a traceback)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RulesError(f"{name} is not UTF-8 text (invalid byte at offset {exc.start}); "
                         "convert it to UTF-8 and try again") from None


def _read(path: Path, name: str | None = None) -> str | None:
    """The file as text with its newlines untouched, or None when it is absent."""
    if not path.is_file():
        return None
    return _decode(name or path.name, path.read_bytes())


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _neutral(text: str) -> str:
    """Break HTML comment delimiters, so no field can open, close or forge a marker."""
    return text.replace("<!--", "< !--").replace("-->", "-- >")


def _newline(text: str) -> str:
    """The file's dominant line ending: CRLF when most of its lines use it."""
    crlf = text.count("\r\n")
    return "\r\n" if crlf and crlf >= text.count("\n") - crlf else "\n"


def _stamp() -> str:
    return _now().strftime("%Y%m%dT%H%M%SZ")


def _backup(path: Path) -> Path:
    """Copy `path` to `<name>.bak-<UTC stamp>`, never overwriting an older backup."""
    candidate = path.with_name(f"{path.name}.bak-{_stamp()}")
    counter = 2
    base = candidate
    while candidate.exists():
        candidate = base.with_name(f"{base.name}-{counter}")
        counter += 1
    candidate.write_bytes(path.read_bytes())
    return candidate


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def template_dir(name: str = "vault") -> Path:
    """`templates/<name>` in a checkout, or `context_layer/templates/<name>` in a wheel."""
    override = os.environ.get("CONTEXT_LAYER_HOME")
    roots = [Path(override).expanduser()] if override else []
    here = Path(__file__).resolve().parent
    roots += [here] + list(here.parents)
    for root in roots:
        candidate = root / "templates" / name
        if candidate.is_dir():
            return candidate
    raise RulesError(f"template directory templates/{name} was not found next to the package; "
                     "set CONTEXT_LAYER_HOME to the checkout")


def router_module(name: str):
    """A router/ module (source_policy, build_index) from a wheel or a checkout.

    Does not import context_layer.cli, so the PostToolUse hook starts quickly.
    """
    for dotted in (f"{__package__}.router.{name}", f"router.{name}"):
        try:
            return importlib.import_module(dotted)
        except ImportError:
            continue
    override = os.environ.get("CONTEXT_LAYER_HOME")
    here = Path(__file__).resolve().parent
    for parent in ([Path(override).expanduser()] if override else []) + list(here.parents):
        folder = parent / "router"
        if (folder / f"{name}.py").is_file():
            if str(folder) not in sys.path:
                sys.path.insert(0, str(folder))
            return importlib.import_module(name)
    raise RulesError(f"router/{name}.py was not found next to the package; "
                     "set CONTEXT_LAYER_HOME to the checkout")


# ---------------------------------------------------------------------------
# Parity
# ---------------------------------------------------------------------------

def imports_agents(data: bytes | str | None) -> bool:
    """True when CLAUDE.md's first non-blank line is the `@AGENTS.md` import."""
    if data is None:
        return False
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    for line in text.lstrip("﻿").splitlines():
        if line.strip():
            return line.strip() == IMPORT_LINE
    return False


def _parity(blobs: dict) -> dict:
    files = {}
    for name in RULE_FILES:
        data = blobs[name]
        files[name] = {"exists": data is not None,
                       "sha256": _digest_bytes(data) if data is not None else None,
                       "bytes": len(data) if data is not None else None}
    single = imports_agents(blobs["CLAUDE.md"])
    problems = []
    first_difference = None
    if single:
        if blobs["AGENTS.md"] is None:
            problems.append("AGENTS.md is missing (CLAUDE.md imports it)")
    else:
        missing = [name for name in RULE_FILES if blobs[name] is None]
        for name in missing:
            problems.append(f"{name} is missing")
        if not missing and blobs["CLAUDE.md"] != blobs["AGENTS.md"]:
            claude, agents = blobs["CLAUDE.md"], blobs["AGENTS.md"]
            if claude.replace(b"\r\n", b"\n") == agents.replace(b"\r\n", b"\n"):
                # Same text, different line endings: splitlines() would point at the last line.
                crlf = "CLAUDE.md" if b"\r\n" in claude else "AGENTS.md"
                lf = "AGENTS.md" if crlf == "CLAUDE.md" else "CLAUDE.md"
                problems.append(f"CLAUDE.md and AGENTS.md differ only in line endings "
                                f"({crlf} uses CRLF, {lf} uses LF)")
            else:
                left = claude.decode("utf-8", "replace").splitlines()
                right = agents.decode("utf-8", "replace").splitlines()
                line = next((i + 1 for i, (a, b) in enumerate(zip(left, right)) if a != b),
                            min(len(left), len(right)) + 1)
                first_difference = line
                problems.append(f"CLAUDE.md and AGENTS.md differ (first difference at line "
                                f"{line})")
    return {"ok": not problems, "mode": "single-source" if single else "twins",
            "files": files, "problems": problems, "first_difference_line": first_difference}


def _blobs(root: Path) -> dict:
    return {name: (root / name).read_bytes() if (root / name).is_file() else None
            for name in RULE_FILES}


def parity(vault) -> dict:
    """One rule text? Twins must be identical; a single-source CLAUDE.md must import an
    existing AGENTS.md. Never raises for a missing file."""
    return _parity(_blobs(_vault(vault)))


def records_file(root: Path) -> str:
    """The rule file that holds the work records: AGENTS.md when CLAUDE.md imports it."""
    claude = root / "CLAUDE.md"
    return "AGENTS.md" if claude.is_file() and imports_agents(claude.read_bytes()) \
        else "CLAUDE.md"


def _records_region(text: str | None) -> str | None:
    """The text between the record markers, or None when there are no markers."""
    if text is None:
        return None
    start = text.find(START_MARK)
    end = text.find(END_MARK, start + 1) if start != -1 else -1
    if start == -1 or end == -1:
        return None
    return text[start + len(START_MARK):end]


def _records_digest(root: Path) -> str | None:
    name = records_file(root)
    region = _records_region(_read(root / name, name))
    return _digest_bytes(region.encode("utf-8")) if region is not None else None


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def _diff(name: str, old: str, new: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        fromfile=f"{name} (yours)", tofile=f"{name} (template)"))


def _templates(single_source: bool) -> dict[str, str]:
    source = template_dir("vault")
    texts = {name: (source / name).read_text(encoding="utf-8") for name in TEMPLATE_FILES}
    if texts["CLAUDE.md"] != texts["AGENTS.md"]:
        raise RulesError("packaged templates CLAUDE.md and AGENTS.md differ; reinstall the package")
    if single_source:
        texts["CLAUDE.md"] = (source / SINGLE_SOURCE_TEMPLATE).read_text(encoding="utf-8")
    return texts


def exclude_rule_files(config: dict) -> dict:
    """A copy of a routes.json config with CLAUDE.md and AGENTS.md kept out of search.

    Both names go into every exclusion key; a route whose only canonical sources
    were those files is dropped, since excluded sources can never be delivered.
    """
    out = json.loads(json.dumps(config))
    for key in EXCLUSION_KEYS:
        values = list(out.get(key) or [])
        values += [name for name in RULE_EXCLUSIONS if name not in values]
        out[key] = values
    routes = out.get("routes")
    if isinstance(routes, dict):
        for name in list(routes):
            route = routes[name]
            sources = route.get("canonical_sources") if isinstance(route, dict) else None
            if not isinstance(sources, list):
                continue
            kept = [item for item in sources
                    if not (isinstance(item, dict) and item.get("path") in RULE_EXCLUSIONS)]
            if len(kept) == len(sources):
                continue
            if kept:
                route["canonical_sources"] = kept
            else:
                del routes[name]
    return out


def _routes_step(root: Path) -> dict:
    """The `.context/routes.json` part of `init`: add the rule-file exclusions."""
    path = root / ".context" / "routes.json"
    step = {"file": ROUTES_FILE, "exists": path.is_file(), "action": None, "content": None,
            "diff": None, "note": None}
    if not path.exists():
        step["action"] = "absent"
        step["note"] = ("no .context/routes.json yet; after `context-layer init <vault>`, run "
                        "`context-layer rules init <vault> --apply` again so CLAUDE.md and "
                        "AGENTS.md stay out of search")
        return step
    try:
        text = _read(path, ROUTES_FILE)
        config = router_module("source_policy").parse_config(text or "", ROUTES_FILE)
    except ValueError as exc:            # RulesError and the loader's ConfigError
        step["action"] = "skip"
        step["note"] = f"{exc}; CLAUDE.md and AGENTS.md were not added to its exclusions"
        return step
    updated = exclude_rule_files(config)
    if updated == config:
        step["action"] = "unchanged"
        return step
    new = json.dumps(updated, indent=2, ensure_ascii=False) + "\n"
    step["action"], step["content"] = "update", new
    step["diff"] = "".join(difflib.unified_diff(
        (text or "").splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"{ROUTES_FILE} (yours)", tofile=f"{ROUTES_FILE} (rule files excluded)"))
    step["note"] = ("adds CLAUDE.md and AGENTS.md to the search exclusions: the hosts load "
                    "them at session start, so search would only repeat them")
    return step


def init_plan(vault, force: bool = False, single_source: bool = False) -> list[dict]:
    """What `init` would do for each template file and routes.json. Pure: writes nothing."""
    root = _vault(vault)
    templates = _templates(single_source)
    current = {name: _read(root / name) for name in TEMPLATE_FILES}
    plan = []
    for name in TEMPLATE_FILES:
        old, new = current[name], templates[name]
        step = {"file": name, "exists": old is not None, "action": None, "content": None,
                "diff": None, "note": None}
        if old == new:
            step["action"] = "unchanged"
        elif old is None:
            step["action"], step["content"] = "create", new
            partner = {"CLAUDE.md": "AGENTS.md", "AGENTS.md": "CLAUDE.md"}.get(name)
            existing = current[partner] if partner else None
            if (partner and existing is not None and existing != templates[partner]
                    and not force and not imports_agents(existing)
                    and not (single_source and name == "CLAUDE.md")):
                # Keep one rule text with the file the user already has; the template rules
                # are offered as a merge suggestion on that file, not silently split off.
                step["action"], step["content"] = "mirror", existing
                step["note"] = (f"created as a byte copy of your existing {partner} so the two "
                                "stay one rule text; merge the template rules shown for "
                                f"{partner} by hand, then run `rules check`")
        elif force:
            step["action"], step["content"] = "replace", new
            step["diff"] = _diff(name, old, new)
        else:
            step["action"] = "keep"
            step["diff"] = _diff(name, old, new)
            step["note"] = "exists and differs; kept as is (merge suggestion below, or --force)"
            if single_source and name == "CLAUDE.md" and not imports_agents(old):
                step["note"] = ("holds its own rules; kept. --force replaces it with the "
                                "one-line @AGENTS.md import after a backup; merge any rule "
                                "that exists only here into AGENTS.md first")
        plan.append(step)
    plan.append(_routes_step(root))
    return plan


def init(vault, apply_now: bool = False, force: bool = False,
         single_source: bool = False) -> dict:
    root = _vault(vault)
    plan = init_plan(root, force=force, single_source=single_source)
    written, backups = [], []
    if apply_now:
        for step in plan:
            if step["action"] not in ("create", "mirror", "replace", "update"):
                continue
            path = root / step["file"]
            if step["action"] in ("replace", "update"):
                backups.append((PurePosixPath(step["file"]).parent
                                / _backup(path).name).as_posix())
            _atomic_write(path, step["content"].encode("utf-8"))
            written.append(step["file"])
    return {"vault": root.name, "applied": apply_now, "single_source": single_source,
            "plan": plan, "written": written, "backups": backups,
            "parity": parity(root) if apply_now else None}


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------

def _marker_age(marker: Path) -> float | None:
    """Seconds since the O_EXCL marker was written (its own timestamp, else its mtime)."""
    try:
        text = marker.read_text(encoding="ascii", errors="replace").split()
        written = float(text[1]) if len(text) >= 2 else marker.stat().st_mtime
    except (OSError, ValueError):
        try:
            written = marker.stat().st_mtime
        except OSError:
            return None
    return time.time() - written


@contextmanager
def _lock(root: Path):
    directory = root / ".context"
    directory.mkdir(parents=True, exist_ok=True)
    with file_lock(directory / LOCK_NAME, timeout=LOCK_TIMEOUT, poll_interval=0.02):
        yield


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------

def _clean_files(root: Path, files: list[str]) -> list[dict]:
    """Vault-relative paths with their current short hash; anything outside is refused."""
    out = []
    seen = set()
    for raw in files:
        for part in str(raw).split(","):
            value = part.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            if value.lower() == "none":
                out.append({"path": "none", "sha256": None, "state": "none"})
                continue
            posix = PurePosixPath(value.replace("\\", "/"))
            if posix.is_absolute() or ".." in posix.parts or re.match(r"^[A-Za-z]:", value):
                raise RulesError(f"--files takes vault-relative paths; refused: {value}")
            path = root / posix
            if path.is_file():
                out.append({"path": str(posix), "sha256": _digest(path), "state": "present"})
            elif path.is_dir():
                out.append({"path": str(posix).rstrip("/") + "/", "sha256": None,
                            "state": "folder"})
            else:
                out.append({"path": str(posix), "sha256": None, "state": "absent"})
    if not out:
        raise RulesError("--files needs at least one vault-relative path (or `none`)")
    return out


def _files_text(files: list[dict]) -> str:
    parts = []
    for item in files:
        if item["state"] == "none":
            parts.append("none")
        elif item["state"] == "present":
            parts.append(f"`{item['path']}` ({item['sha256'][:SHORT]})")
        elif item["state"] == "folder":
            parts.append(f"`{item['path']}`")
        else:
            parts.append(f"`{item['path']}` (absent now)")
    return _neutral(", ".join(parts))


def _trim(region: str, keep: int) -> tuple[str, int]:
    """Keep the last `keep` `### ` entries of the records region (0 = keep all)."""
    if keep <= 0:
        return region, 0
    lines = region.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.startswith("### ")]
    if len(starts) <= keep:
        return region, 0
    cut = starts[len(starts) - keep]
    head = lines[:starts[0]]
    return "".join(head + lines[cut:]), len(starts) - keep


def _with_entry(text: str, entry: str, keep: int, nl: str = "\n") -> tuple[str, int]:
    """Insert `entry` at the end of the records region, adding the region if absent.

    Every line this adds ends with `nl`, the file's own line ending.
    """
    region = _records_region(text)
    if region is None:
        if text and not text.endswith("\n"):
            text += nl
        text += f"{nl}{RECORDS_HEADING}{nl}{nl}{START_MARK}{nl}{END_MARK}{nl}"
        region = nl
    existing = region.strip("\r\n")
    body = nl + (existing + nl + nl if existing else "") + entry.rstrip("\r\n") + nl
    body, trimmed = _trim(body, keep)
    start = text.find(START_MARK) + len(START_MARK)
    end = text.find(END_MARK, start)
    return text[:start] + body + text[end:], trimmed


def record(vault, *, summary: str, files: list[str], verified: str, next_step: str,
           why: str | None = None, link: str | None = None, tool: str | None = None,
           keep: int = DEFAULT_KEEP, now: datetime | None = None) -> dict:
    """Append one entry to the rule text (both twins, or AGENTS.md when CLAUDE.md imports
    it) and to LOG.md, then re-check parity."""
    root = _vault(vault)
    fields = {"--summary": summary, "--verified": verified, "--next": next_step}
    for flag, value in fields.items():
        if not _one_line(value or ""):
            raise RulesError(f"{flag} must not be empty")
    if keep < 0:
        raise RulesError("--keep must be zero or a positive integer")
    given = [summary, verified, next_step, why, link, tool]
    summary, verified, next_step = (_neutral(_one_line(summary)), _neutral(_one_line(verified)),
                                    _neutral(_one_line(next_step)))
    why = _neutral(_one_line(why)) if why else None
    link = _neutral(_one_line(link)) if link else None
    tool = _neutral(_one_line(tool or os.environ.get("CONTEXT_LAYER_TOOL") or "cli"))
    escaped = any(isinstance(value, str) and ("<!--" in value or "-->" in value)
                  for value in given)
    moment = now or _now()
    record_id = f"r-{moment.strftime('%Y%m%dt%H%M%Sz')}-{secrets.token_hex(3)}"
    date = moment.strftime("%Y-%m-%d")
    clock = moment.strftime("%H:%M")
    cleaned = _clean_files(root, files)
    files_text = _files_text(cleaned)
    detail = f"[[LOG#^{record_id}]]" + (f", {link}" if link else "")
    short = [f"### {date} {clock} UTC - {summary}",
             *([f"- Why: {why}"] if why else []),
             f"- Files: {files_text}",
             f"- Verified: {verified}",
             f"- Next: {next_step}",
             f"- Detail: {detail}"]
    long_lines = [f"## {date} - {summary}",
                  "",
                  f"Record `{record_id}` | {_iso(moment)} | tool `{tool}` ^{record_id}",
                  "",
                  f"- What was done: {summary}",
                  f"- Why: {why or '(not stated)'}",
                  f"- Files changed: {files_text}",
                  f"- Verification: {verified}",
                  f"- Remaining / next step: {next_step}",
                  *([f"- Detail: {link}"] if link else [])]

    with _lock(root):
        # Re-read inside the lock: a copy loaded earlier may already be stale.
        blobs = _blobs(root)
        check = _parity(blobs)
        if check["mode"] == "twins":
            missing = [name for name, data in blobs.items() if data is None]
            if missing:
                raise RulesError(f"{', '.join(missing)} missing; run "
                                 "`context-layer rules init` first")
        if not check["ok"]:
            raise RulesError("refusing to record while parity is broken: "
                             + "; ".join(check["problems"])
                             + ". Restore one rule text (identical files, or a CLAUDE.md that "
                               "imports an existing AGENTS.md), then record again")
        single = check["mode"] == "single-source"
        targets_rules = ("AGENTS.md",) if single else RULE_FILES
        holder = "AGENTS.md" if single else "CLAUDE.md"
        rule_text = _decode(holder, blobs[holder])
        nl = _newline(rule_text)
        new_rules, trimmed = _with_entry(rule_text, nl.join(short) + nl, keep, nl)
        log_path = root / LOG_FILE
        log_old = _read(log_path, LOG_FILE)
        log_nl = _newline(log_old) if log_old else nl
        log_text = log_old if log_old is not None else LOG_HEADER.replace("\n", log_nl)
        if not log_text.endswith("\n"):
            log_text += log_nl
        new_log = log_text + log_nl + log_nl.join(long_lines) + log_nl
        originals = {name: (root / name).read_bytes() if (root / name).is_file() else None
                     for name in (*targets_rules, LOG_FILE)}
        targets = {name: new_rules.encode("utf-8") for name in targets_rules}
        targets[LOG_FILE] = new_log.encode("utf-8")
        done = []
        try:
            for name, data in targets.items():
                _atomic_write(root / name, data)
                done.append(name)
        except OSError:
            for name in done:                       # best-effort rollback of the group
                original = originals[name]
                if original is None:
                    (root / name).unlink(missing_ok=True)
                else:
                    _atomic_write(root / name, original)
            raise
        state = parity(root)
    if not state["ok"]:                             # pragma: no cover - a concurrent writer
        raise RulesError("recorded, but parity is broken afterwards: " + "; ".join(state["problems"]))
    return {"id": record_id, "ts": _iso(moment), "tool": tool, "summary": summary, "why": why,
            "files": cleaned, "verified": verified, "next": next_step, "link": link,
            "entry": nl.join(short) + nl, "trimmed_from_rule_files": trimmed,
            "mode": state["mode"], "escaped": escaped,
            "sha256": {name: _digest(root / name) for name in (*RULE_FILES, LOG_FILE)},
            "parity": True}


# ---------------------------------------------------------------------------
# Claude Code hooks: state, snapshots, attribution
# ---------------------------------------------------------------------------

def _state_path(root: Path) -> Path:
    return root / ".context" / STATE_NAME


def _snapshot_path(root: Path, name: str) -> Path:
    return root / ".context" / SNAPSHOT_DIR / name


def _load_state(root: Path) -> dict:
    path = _state_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {"version": STATE_VERSION, "sessions": {}}
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
        return {"version": STATE_VERSION, "sessions": {}}
    data["sessions"] = {key: value for key, value in data["sessions"].items()
                        if isinstance(value, dict)}
    data["version"] = STATE_VERSION
    return data


def _recency(item) -> str:
    session = item[1]
    return str(session.get("touched_at") or session.get("started_at") or "")


def _save_state(root: Path, state: dict, touched: str | None = None) -> None:
    """Write the state; keep the MAX_SESSIONS most recently used sessions, and vault
    snapshots only for the MAX_SNAPSHOTS most recent of them. A session that lost its
    snapshot starts a new one at its next Stop and asks nothing until then."""
    sessions = state.get("sessions", {})
    if touched and touched in sessions:
        sessions[touched]["touched_at"] = _iso(_now())
    ordered = sorted(sessions.items(), key=_recency)
    state["sessions"] = dict(ordered[-MAX_SESSIONS:])
    wanted = {session.get("snapshot") for _, session in ordered[-MAX_SNAPSHOTS:]}
    for _, session in ordered[:-MAX_SNAPSHOTS]:
        if session.get("snapshot") not in wanted:
            session["snapshot"] = None
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, (json.dumps(state, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    folder = root / ".context" / SNAPSHOT_DIR
    if folder.is_dir():
        for path in folder.glob("*.json"):
            if path.name not in wanted:
                path.unlink(missing_ok=True)


def _session_id(payload: dict) -> str:
    value = payload.get("session_id")
    return value.strip() if isinstance(value, str) and value.strip() else "default"


def _exclusions(root: Path):
    """(policy, prefixes, problem). A problem means routes.json cannot be trusted."""
    policy = router_module("source_policy")
    try:
        return policy, policy.load_exclusions(root), None
    except ValueError as exc:
        return policy, (), str(exc)


def _excluded(policy, name: str, prefixes) -> bool:
    try:
        return policy.excluded(name, prefixes)
    except ValueError:
        return True           # not a name the router accepts as a source: leave it alone


def _key(name: str) -> str:
    """Matching form of a path: a case-insensitive file system may report either spelling."""
    return unicodedata.normalize("NFC", name).casefold()


def _walk(root: Path, policy, prefixes) -> tuple[dict[str, tuple[int, int]], bool]:
    """{vault-relative path: (size, mtime_ns)} of every file in scope, and whether the
    walk stopped at MAX_WALK. Hidden and excluded folders are never entered; symlinks
    and the root record files are skipped."""
    found: dict[str, tuple[int, int]] = {}
    seen = 0
    for current, dirs, files in os.walk(root):
        here = Path(current)
        relative = here.relative_to(root)
        keep = []
        for name in sorted(dirs):
            seen += 1
            rel = (relative / name).as_posix()
            if name.startswith(".") or (here / name).is_symlink() \
                    or _excluded(policy, rel, prefixes):
                continue
            keep.append(name)
        dirs[:] = keep
        for name in sorted(files):
            seen += 1
            if seen > MAX_WALK:
                return found, True
            if name.startswith(".") or (not relative.parts and name in RECORD_SET):
                continue
            rel = (relative / name).as_posix()
            if _excluded(policy, rel, prefixes):
                continue
            try:
                info = (here / name).lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                found[rel] = (info.st_size, info.st_mtime_ns)
    return found, False


def _hash(path: Path, size: int, budget: list[int]) -> str | None:
    """Full SHA-256 hex of a file, or None when it is too large or the run's budget is spent."""
    if size > HASH_FILE_CAP or size > budget[0]:
        return None
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return None
    budget[0] -= size
    return digest.hexdigest()


def _now_ns() -> int:
    return time.time_ns()


def _take_snapshot(root: Path, policy, prefixes, reuse: dict | None) -> dict:
    """The files in scope with size, mtime and content hash; unchanged entries of `reuse`
    (same size and mtime, mtime not in the future) are carried over without reading."""
    files, truncated = _walk(root, policy, prefixes)
    budget = [HASH_RUN_CAP]
    limit = _now_ns() + FUTURE_SKEW_NS
    old = (reuse or {}).get("files") or {}
    entries = {}
    for rel, (size, mtime) in files.items():
        previous = old.get(rel)
        if (isinstance(previous, list) and len(previous) == 3 and previous[0] == size
                and previous[1] == mtime and mtime <= limit):
            entries[rel] = previous
        else:
            entries[rel] = [size, mtime, _hash(root / rel, size, budget)]
    return {"version": 1, "files": entries, "truncated": truncated}


def _load_snapshot(root: Path, name: str | None) -> dict | None:
    if not name:
        return None
    try:
        data = json.loads(_snapshot_path(root, name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return None
    return data


def _newest_snapshot(root: Path) -> dict | None:
    folder = root / ".context" / SNAPSHOT_DIR
    try:
        candidates = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for path in candidates[:3]:
        data = _load_snapshot(root, path.name)
        if data is not None:
            return data
    return None


def _write_snapshot(root: Path, sid: str, snapshot: dict) -> str:
    name = f"{safe_stem(sid)}.json"
    path = _snapshot_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, (json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False)
                         + "\n").encode("utf-8"))
    return name


def _changes(root: Path, base: dict, files: dict, truncated: bool, policy,
             prefixes) -> dict[str, str]:
    """{path: added|modified|deleted} between a snapshot and the files now in scope."""
    changes: dict[str, str] = {}
    budget = [HASH_RUN_CAP]
    limit = _now_ns() + FUTURE_SKEW_NS
    before = base.get("files") or {}
    for rel, (size, mtime) in files.items():
        old = before.get(rel)
        if not (isinstance(old, list) and len(old) == 3):
            changes[rel] = "added"
            continue
        old_size, old_mtime, old_sha = old
        future = mtime > limit
        if not future and size == old_size and mtime == old_mtime:
            continue                        # same size and time: not read again
        if size != old_size:
            changes[rel] = "modified"
            continue
        sha = _hash(root / rel, size, budget)
        if sha is not None and old_sha is not None:
            if sha != old_sha:
                changes[rel] = "modified"
        elif not future:
            changes[rel] = "modified"       # too large to hash: its time moved
    if not truncated and not base.get("truncated"):
        for rel in before:
            if rel in files or (root / rel).exists() or _excluded(policy, rel, prefixes):
                continue                    # never report a path that is now excluded
            changes[rel] = "deleted"
    return changes


def _new_session(root: Path, sid: str, source: str | None, moment: datetime, policy,
                 prefixes, problem: str | None, reuse: dict | None) -> dict:
    blobs = _blobs(root)
    session = {"started_at": _iso(moment), "snapshot_at": _iso(moment), "source": source,
               "mode": _parity(blobs)["mode"],
               "sha256": {name: _digest(root / name) for name in (*RULE_FILES, LOG_FILE)},
               "records_sha256": _records_digest(root),
               "snapshot": None, "snapshot_files": 0, "snapshot_truncated": False,
               "config_problem": problem, "attributed": {}, "attributed_dropped": 0,
               "written": [], "asked": [], "reported": [], "blocked": None}
    if problem is None:
        snapshot = _take_snapshot(root, policy, prefixes, reuse)
        session["snapshot"] = _write_snapshot(root, sid, snapshot)
        session["snapshot_files"] = len(snapshot["files"])
        session["snapshot_truncated"] = snapshot["truncated"]
    return session


def _listed(paths: list[str]) -> str:
    shown = ", ".join(paths[:MAX_LISTED])
    if len(paths) > MAX_LISTED:
        shown += f" and {len(paths) - MAX_LISTED} more"
    return shown


def _bounded(values) -> list[str]:
    return sorted(set(values))[:MAX_TRACKED]


def _parity_text(root: Path, check: dict) -> str:
    return ("Vault rules check failed: " + "; ".join(check["problems"])
            + ". CLAUDE.md and AGENTS.md must hold one rule text (byte-identical files, or a "
              "CLAUDE.md whose first line imports AGENTS.md). Fix that first, then run "
              f"`context-layer rules check {shlex.quote(str(root))}`.")


def hook_session_start(vault, payload: dict, now: datetime | None = None,
                       brief: bool = False) -> dict | None:
    """Snapshot the vault for this session; report broken parity; optionally add the brief.

    Returns the Claude Code SessionStart output to print, or None (print nothing).
    `resume`, `compact` and `fork` keep an existing snapshot for the same session
    id, so changes made before compaction are not forgotten. A vault without rule
    files gets no state at all; the brief (`brief=True`) is still added.
    """
    root = _vault(vault)
    moment = now or _now()
    source = payload.get("source") if isinstance(payload.get("source"), str) else None
    parts: list[str] = []
    notes: list[str] = []
    if any((root / name).is_file() for name in RULE_FILES):
        check = parity(root)
        policy, prefixes, problem = _exclusions(root)
        with _lock(root):
            state = _load_state(root)
            sid = _session_id(payload)
            existing = state["sessions"].get(sid)
            keep = (existing and existing.get("snapshot")
                    and _load_snapshot(root, existing.get("snapshot")) is not None
                    and source in ("resume", "compact", "fork"))
            if not keep:
                state["sessions"][sid] = _new_session(root, sid, source, moment, policy,
                                                      prefixes, problem, _newest_snapshot(root))
            _save_state(root, state, touched=sid)
        if not check["ok"]:
            parts.append(_parity_text(root, check))
        if problem:
            notes.append(f"context-layer rules: {problem}; vault changes are not tracked in "
                         "this session.")
    if brief:
        # The whole additionalContext, parity text included, stays within the cap.
        from . import brief as brief_module
        room = BRIEF_HOOK_CHARS - (len("\n\n".join(parts)) + 2 if parts else 0)
        text, error = brief_module.hook_text(root, max_chars=room)
        if text:
            parts.append(text)
        if error:
            notes.append(f"context-layer brief: {error}")
    output: dict = {}
    if parts:
        output["hookSpecificOutput"] = {"hookEventName": "SessionStart",
                                        "additionalContext": "\n\n".join(parts)}
    if notes:
        output["systemMessage"] = "\n".join(notes)
    return output or None


def _tool_path(payload: dict) -> str | None:
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    response = payload.get("tool_response") if isinstance(payload.get("tool_response"),
                                                         dict) else {}
    for value in (tool_input.get("file_path"), tool_input.get("notebook_path"),
                  response.get("filePath")):
        if isinstance(value, str) and value.strip():
            return value
    return None


def _vault_relative(root: Path, raw: str, cwd) -> str | None:
    """The vault-relative POSIX form of a tool's file path, or None when it is outside.

    Claude Code sends file-tool paths absolute (it expands `~` and relative paths
    before hooks run); a relative one is resolved against the input's `cwd`.
    """
    candidate = Path(raw)
    if not candidate.is_absolute():
        if not isinstance(cwd, str) or not cwd:
            return None
        candidate = Path(cwd) / candidate
    try:
        relative = Path(os.path.realpath(candidate)).relative_to(root)
    except ValueError:
        return None
    return relative.as_posix() if relative.parts else None


def hook_post_tool_use(vault, payload: dict, now: datetime | None = None) -> None:
    """Remember that the agent wrote a vault file (Write/Edit/MultiEdit/NotebookEdit).

    Only paths inside the vault, in scope and outside the record files are kept; an
    excluded path is dropped before anything is stored. Prints nothing.
    """
    root = _vault(vault)
    tool = payload.get("tool_name")
    if tool not in ATTRIBUTED_TOOLS or not any((root / n).is_file() for n in RULE_FILES):
        return None
    raw = _tool_path(payload)
    rel = _vault_relative(root, raw, payload.get("cwd")) if raw else None
    if rel is None or rel in RECORD_SET:
        return None
    policy, prefixes, problem = _exclusions(root)
    if problem or _excluded(policy, rel, prefixes):
        return None
    moment = now or _now()
    with _lock(root):
        state = _load_state(root)
        sid = _session_id(payload)
        session = state["sessions"].setdefault(sid, {
            "started_at": _iso(moment), "source": "post-tool-use", "snapshot": None,
            "attributed": {}, "attributed_dropped": 0, "written": [], "asked": [],
            "reported": [], "blocked": None})
        attributed = session.setdefault("attributed", {})
        if rel not in attributed:
            if len(attributed) >= MAX_TRACKED:
                session["attributed_dropped"] = int(session.get("attributed_dropped") or 0) + 1
            else:
                attributed[rel] = {"tool": tool, "at": _iso(moment)}
        written = session.get("written") or []
        if rel not in written and len(written) < MAX_TRACKED:
            session["written"] = sorted(set(written) | {rel})
        _save_state(root, state, touched=sid)
    return None


def _stop_changes(root: Path, session: dict, policy, prefixes):
    """(changes, files now in scope, truncated) for a session with a readable snapshot."""
    base = _load_snapshot(root, session.get("snapshot"))
    files, truncated = _walk(root, policy, prefixes)
    if base is None:
        return None, files, truncated
    return _changes(root, base, files, truncated, policy, prefixes), files, truncated


def hook_stop(vault, payload: dict, now: datetime | None = None,
              check_citations: bool = False) -> dict | None:
    """Ask once for a record of agent-written, unrecorded changes; report the rest.

    Returns the Stop output to print, or None. `decision: block` (with `reason`) only
    when the agent's own file edits changed vault content without a new record, or
    when parity broke; never in plan mode and never while `stop_hook_active` is true;
    at most once per path (and per parity problem) in a session. Everything else is a
    non-blocking `systemMessage`: vault changes the agent's file tools did not make,
    and, with `check_citations`, citations never delivered in this session.
    """
    root = _vault(vault)
    moment = now or _now()
    sid = _session_id(payload)
    can_block = payload.get("stop_hook_active") is not True \
        and payload.get("permission_mode") != "plan"
    reasons: list[str] = []
    notes: list[str] = []
    files: dict | None = None
    written: list[str] = []
    policy, prefixes, problem = _exclusions(root)
    if any((root / name).is_file() for name in RULE_FILES):
        with _lock(root):
            state = _load_state(root)
            check = parity(root)
            session = state["sessions"].get(sid)
            fresh = session is None or not session.get("snapshot") or problem is not None
            changes = None
            if session is not None and not fresh:
                changes, files, _ = _stop_changes(root, session, policy, prefixes)
            if changes is None:
                # No baseline to compare with (hooks installed mid-session, or an
                # unreadable routes.json): start one now and ask about nothing yet.
                previous = session or {}
                session = _new_session(root, sid, previous.get("source") or "stop", moment,
                                       policy, prefixes, problem, _newest_snapshot(root))
                for key in ("asked", "reported", "written", "started_at", "parity_problems",
                            "parity_reported"):
                    if previous.get(key):
                        session[key] = previous[key]
                if problem and not previous.get("config_problem"):
                    notes.append(f"context-layer rules: {problem}; vault changes are not "
                                 "tracked until it is fixed.")
                changes = {}
            attributed = {_key(p) for p in session.get("attributed") or {}
                          if not _excluded(policy, p, prefixes)}
            asked = set(session.get("asked") or [])
            reported = set(session.get("reported") or [])
            agent = sorted(p for p in changes if _key(p) in attributed)
            other = sorted(p for p in changes if _key(p) not in attributed)
            recorded = _records_digest(root) != session.get("records_sha256")
            new_other = [p for p in other if p not in reported]
            if new_other and not recorded:
                # After a new record the stretch is closed; its changes are not re-listed.
                notes.append(f"context-layer rules: {len(new_other)} vault path(s) changed in "
                             "this session that the agent's file tools did not write (not "
                             f"asked about): {_listed(new_other)}.")
            reported.update(new_other)
            if recorded:
                # A new record closes this stretch of work: the baseline moves forward.
                snapshot = _take_snapshot(root, policy, prefixes,
                                          _load_snapshot(root, session.get("snapshot")))
                session["snapshot"] = _write_snapshot(root, sid, snapshot)
                session["snapshot_files"] = len(snapshot["files"])
                session["snapshot_truncated"] = snapshot["truncated"]
                session["snapshot_at"] = _iso(moment)
                session["records_sha256"] = _records_digest(root)
                session["sha256"] = {name: _digest(root / name)
                                     for name in (*RULE_FILES, LOG_FILE)}
                session["attributed"] = {}
                session["blocked"] = None
                pending: list[str] = []
            else:
                pending = [p for p in agent if p not in asked]
            if check["ok"]:
                session.pop("parity_problems", None)
                session.pop("parity_reported", None)
            elif session.get("parity_problems") != check["problems"]:
                if can_block:
                    reasons.append("Rule parity is broken: " + "; ".join(check["problems"])
                                   + ". Write the same change to CLAUDE.md and AGENTS.md so "
                                     "they hold one rule text, then run `context-layer rules "
                                     f"check {shlex.quote(str(root))}`.")
                    session["parity_problems"] = check["problems"]
                elif session.get("parity_reported") != check["problems"]:
                    notes.append("context-layer rules: " + "; ".join(check["problems"]) + ".")
                    session["parity_reported"] = check["problems"]
            if pending and can_block:
                reasons.append(
                    f"{len(pending)} vault path(s) the agent wrote in this session changed and "
                    f"no work record was appended: {_listed(pending)}. Run: context-layer "
                    f"rules record {shlex.quote(str(root))} --summary \"<what was done and "
                    "why>\" --files <changed paths> --verified \"<how it was checked>\" --next "
                    "\"<what remains>\". If the change was not meaningful, say so in one line "
                    "and stop again.")
                asked.update(pending)
            if reasons:
                session["blocked"] = {"at": _iso(moment), "paths": pending if can_block else [],
                                      "parity_ok": check["ok"]}
            session["asked"] = _bounded(asked)
            session["reported"] = _bounded(reported)
            written = list(session.get("written") or [])
            state["sessions"][sid] = session
            _save_state(root, state, touched=sid)
    if check_citations:
        from . import session_evidence
        text, _ = session_evidence.final_message(payload)
        if text:
            if files is None and problem is None:
                files, _ = _walk(root, policy, prefixes)
            if files is not None:
                known = [digest for digest in (_digest(root / name) for name in RECORD_SET)
                         if digest]
                known += [digest for digest in (_digest(root / p) for p in written) if digest]
                result = session_evidence.check(root, sid, text, list(files), known, written)
                line = session_evidence.message_line(result)
                if line:
                    notes.append(line)
    output: dict = {}
    if reasons:
        output["decision"] = "block"
        output["reason"] = " ".join(reasons)
    if notes:
        output["systemMessage"] = "\n".join(notes)
    return output or None


def _read_payload(raw: str) -> dict:
    try:
        payload = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError as exc:
        raise RulesError(f"unreadable hook JSON on stdin: {exc.msg}") from None
    if not isinstance(payload, dict):
        raise RulesError("hook JSON on stdin must be an object")
    return payload


# ---------------------------------------------------------------------------
# settings.json entries
# ---------------------------------------------------------------------------

def hook_groups(vault: Path, brief: bool = False, check_citations: bool = False) -> dict:
    """{event: hook group} for `.claude/settings.json`: SessionStart, PostToolUse (with a
    Write|Edit|MultiEdit|NotebookEdit matcher) and Stop, each one command hook."""
    from . import install          # reuse the launcher and platform-safe command serializer
    flags = {"SessionStart": ["--brief"] if brief else [], "PostToolUse": [],
             "Stop": ["--check-citations"] if check_citations else []}
    groups = {}
    for event, name in HOOK_EVENTS.items():
        argv = install.cli_argv(vault, "rules", "hook", name, *flags[event])
        group: dict = {"hooks": [{"type": "command", "command": install.hook_command(argv),
                                  "timeout": HOOK_TIMEOUTS[event]}]}
        if event == "PostToolUse":
            group = {"matcher": POST_TOOL_MATCHER, **group}
        groups[event] = group
    return groups


def settings_snippet(vault: Path, plan_default: bool = False, brief: bool = False,
                     check_citations: bool = False) -> dict:
    """The `.claude/settings.json` entries that wire the three hooks (and optional plan mode)."""
    groups = hook_groups(vault, brief=brief, check_citations=check_citations)
    snippet: dict = {"hooks": {event: [group] for event, group in groups.items()}}
    if plan_default:
        snippet["permissions"] = {"defaultMode": "plan"}
    return snippet


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fail(command: str, message: str, code: int = 1) -> int:
    print(f"context-layer rules {command}: {message}", file=sys.stderr)
    return code


def _unexpected(args: argparse.Namespace, command: str, code: int = 2) -> bool:
    extra = [token for token in getattr(args, "rest", []) or [] if token]
    if extra:
        _fail(command, f"unrecognised arguments: {' '.join(extra)}", code)
        return True
    return False


def cmd_init(args: argparse.Namespace) -> int:
    if _unexpected(args, "init"):
        return 2
    try:
        result = init(args.vault, apply_now=args.apply, force=args.force,
                      single_source=args.single_source)
    except (RulesError, OSError) as exc:
        return _fail("init", str(exc))
    if args.as_json:
        for step in result["plan"]:
            step.pop("content", None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for step in result["plan"]:
            action = step["action"]
            if action == "unchanged":
                print(f"unchanged: {step['file']}", file=sys.stderr)
            elif action in ("create", "mirror", "replace", "update"):
                verb = ("wrote" if result["applied"] else "would write")
                print(f"{verb}: {step['file']} ({action})")
            elif action in ("absent", "skip"):
                print(f"note: {step['file']}: {step['note']}", file=sys.stderr)
            else:
                print(f"kept: {step['file']} - {step['note']}")
            if step["diff"]:
                sys.stdout.write(step["diff"])
            if step["note"] and action in ("mirror", "update"):
                print(f"note: {step['file']}: {step['note']}", file=sys.stderr)
        for name in result["backups"]:
            print(f"backup: {name}")
        if not result["applied"]:
            print("dry run: nothing was written. Re-run with --apply.", file=sys.stderr)
    if result["applied"] and result["parity"] and not result["parity"]["ok"]:
        return _fail("init", "parity is broken after init: "
                     + "; ".join(result["parity"]["problems"]))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    if _unexpected(args, "check"):
        return 2
    try:
        result = parity(args.vault)
    except RulesError as exc:
        if args.as_json:
            print(json.dumps({"ok": False, "problems": [str(exc)]}, indent=2))
        return _fail("check", str(exc))
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["ok"] and result["mode"] == "single-source":
        digest = result["files"]["AGENTS.md"]["sha256"]
        print(f"OK: CLAUDE.md imports AGENTS.md (single source; AGENTS.md sha256 {digest}).")
    elif result["ok"]:
        digest = result["files"]["CLAUDE.md"]["sha256"]
        print(f"OK: CLAUDE.md and AGENTS.md are byte-identical (sha256 {digest}).")
    else:
        for problem in result["problems"]:
            print(f"context-layer rules check: {problem}", file=sys.stderr)
        for name, item in result["files"].items():
            if item["exists"]:
                print(f"  {name}  sha256 {item['sha256']}", file=sys.stderr)
        print("Mirror the change so both files are identical (or run `rules init` if one is "
              "missing), then check again.", file=sys.stderr)
    return 0 if result["ok"] else 1


def cmd_record(args: argparse.Namespace) -> int:
    if _unexpected(args, "record"):
        return 2
    try:
        stored = record(args.vault, summary=args.summary, files=args.files,
                        verified=args.verified, next_step=args.next, why=args.why,
                        link=args.link, tool=args.tool, keep=args.keep)
    except (RulesError, OSError) as exc:
        if args.as_json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return _fail("record", str(exc))
    if args.as_json:
        print(json.dumps({"ok": True, **stored}, ensure_ascii=False, indent=2))
        return 0
    where = ("AGENTS.md (imported by CLAUDE.md)" if stored["mode"] == "single-source"
             else "CLAUDE.md, AGENTS.md")
    print(f"recorded {stored['id']} in {where} and LOG.md")
    holder = "AGENTS.md" if stored["mode"] == "single-source" else "CLAUDE.md"
    print(f"  parity OK: sha256 {stored['sha256'][holder]}")
    if stored["escaped"]:
        print("  note: '<!--' and '-->' in the text were written as '< !--' and '-- >' so "
              "the records section stays intact", file=sys.stderr)
    if stored["trimmed_from_rule_files"]:
        print(f"  {stored['trimmed_from_rule_files']} older entr(y/ies) left the rule files; "
              "LOG.md keeps them all")
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    # Every failure here exits 1: for Claude Code that is a visible, non-blocking error.
    # Exit 2 would block (Stop: force another turn; SessionStart: show an error).
    event = args.event or ""
    label = f"hook {event}".strip()
    if _unexpected(args, label, code=1):
        return 1
    if event not in HOOK_EVENTS.values():
        return _fail(label, f"unknown hook event {event!r}; use one of: "
                     + ", ".join(HOOK_EVENTS.values()))
    if not args.vault:
        return _fail(label, "--vault is required")
    if args.brief and event != "session-start":
        return _fail(label, "--brief applies to session-start only")
    if args.check_citations and event != "stop":
        return _fail(label, "--check-citations applies to stop only")
    try:
        payload = _read_payload(sys.stdin.read())
        if event == "session-start":
            output = hook_session_start(args.vault, payload, brief=args.brief)
        elif event == "post-tool-use":
            output = hook_post_tool_use(args.vault, payload)
        else:
            output = hook_stop(args.vault, payload, check_citations=args.check_citations)
    except (RulesError, OSError, UnicodeError) as exc:
        return _fail(label, str(exc))
    except Exception as exc:  # noqa: BLE001 - a hook fails in one line, never a traceback
        return _fail(label, f"internal error: {type(exc).__name__}: {exc}")
    if output is not None:
        print(json.dumps(output, ensure_ascii=False))
    return 0


def cmd_settings(args: argparse.Namespace) -> int:
    if _unexpected(args, "settings"):
        return 2
    vault = Path(args.vault).expanduser()
    if not vault.is_dir():
        return _fail("settings", f"vault not found: {vault.name or args.vault}")
    print("Merge into <project>/.claude/settings.json (this command writes nothing):",
          file=sys.stderr)
    print(json.dumps(settings_snippet(vault.resolve(), args.plan_default, args.brief,
                                      args.check_citations), indent=2, ensure_ascii=False))
    return 0


def _hook_parser_error(message: str):
    # argparse would exit 2, which Claude Code reads as "block"; hooks exit 1 instead.
    print(f"context-layer rules hook: {message}", file=sys.stderr)
    raise SystemExit(1)


def register(sub: argparse._SubParsersAction) -> None:
    """Attach `rules init|check|record|hook|settings` to the CLI."""
    parser = sub.add_parser(
        "rules",
        help="One CLAUDE.md/AGENTS.md rule text and a record of every work step.",
        description="Installs the vault rule templates, checks that CLAUDE.md and AGENTS.md "
                    "hold one rule text, appends one work record to it (and LOG.md), and "
                    "runs the Claude Code SessionStart/PostToolUse/Stop hooks that ask for a "
                    "missing record of the agent's own edits.",
    )
    inner = parser.add_subparsers(dest="rules_command", required=True)

    p_init = inner.add_parser("init", help="Install CLAUDE.md, AGENTS.md, LOG.md, BACKLOG.md "
                                           "and keep the rule files out of search "
                                           "(dry run unless --apply).")
    p_init.add_argument("vault")
    p_init.add_argument("--apply", action="store_true", help="Write the files.")
    p_init.add_argument("--force", action="store_true",
                        help="Replace existing files with the templates, after a backup.")
    p_init.add_argument("--single-source", action="store_true",
                        help="Keep the rules in AGENTS.md and write CLAUDE.md as a one-line "
                             "@AGENTS.md import (Claude Code expands it). Default: identical "
                             "twins.")
    p_init.add_argument("--json", action="store_true", dest="as_json")
    p_init.set_defaults(func=cmd_init, forward_to=None)

    p_check = inner.add_parser("check", help="Exit 0 only if CLAUDE.md and AGENTS.md hold one "
                                             "rule text (twins, or a CLAUDE.md importing "
                                             "AGENTS.md).")
    p_check.add_argument("vault")
    p_check.add_argument("--json", action="store_true", dest="as_json")
    p_check.set_defaults(func=cmd_check, forward_to=None)

    p_record = inner.add_parser("record", help="Append one work record to the rule text and "
                                               "LOG.md, then re-check parity.")
    p_record.add_argument("vault")
    p_record.add_argument("--summary", required=True, help="What was done (one line).")
    p_record.add_argument("--files", required=True, nargs="+", action="extend",
                          help="Vault-relative paths changed (space or comma separated), or none.")
    p_record.add_argument("--verified", required=True,
                          help="How the result was checked, and the outcome.")
    p_record.add_argument("--next", required=True, help="What remains, or the next step.")
    p_record.add_argument("--why", default=None, help="Why it was done.")
    p_record.add_argument("--link", default=None, help="Link to the detail ([[note]] or URL).")
    p_record.add_argument("--tool", default=None,
                          help="Recording tool (default: $CONTEXT_LAYER_TOOL, else cli).")
    p_record.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                          help=f"Entries kept in the rule text (default {DEFAULT_KEEP}; "
                               "0 keeps all). LOG.md always keeps every entry.")
    p_record.add_argument("--json", action="store_true", dest="as_json")
    p_record.set_defaults(func=cmd_record, forward_to=None)

    p_hook = inner.add_parser("hook", help="Claude Code hook handler: hook JSON on stdin. "
                                           "Errors exit 1, never 2.")
    p_hook.add_argument("event", nargs="?", default=None,
                        help="session-start, post-tool-use or stop.")
    p_hook.add_argument("--vault", default=None)
    p_hook.add_argument("--brief", action="store_true",
                        help="session-start: also add the evidence-pinned brief "
                             f"(`context-layer brief`, at most {BRIEF_HOOK_CHARS} characters).")
    p_hook.add_argument("--check-citations", action="store_true",
                        help="stop: add one non-blocking line naming paths and hashes the "
                             "last answer cited that context-layer never delivered in this "
                             "session.")
    p_hook.error = _hook_parser_error
    p_hook.set_defaults(func=cmd_hook, forward_to=None)

    p_settings = inner.add_parser("settings", help="Print the settings.json hook entries "
                                                   "(writes nothing).")
    p_settings.add_argument("--vault", required=True)
    p_settings.add_argument("--plan-default", action="store_true",
                            help="Also set permissions.defaultMode to \"plan\".")
    p_settings.add_argument("--brief", action="store_true",
                            help="SessionStart entry with --brief.")
    p_settings.add_argument("--check-citations", action="store_true",
                            help="Stop entry with --check-citations.")
    p_settings.set_defaults(func=cmd_settings, forward_to=None)
