"""context_layer.memory — shared, append-only memory for decisions/tasks/results.

What this is
------------
One JSONL file per vault, `<vault>/.context/memory/records.jsonl`, plus a
derived Markdown mirror. A record carries what was decided or done, which
sources it rests on and the SHA-256 those sources had at the time. Records are
never rewritten: a state change or a correction is a new record that names the
record it supersedes (or several, to merge a fork). `resume()` reads the file
back as a continuation packet for the next session or the other tool: the
records in force, every source whose bytes changed or disappeared since it was
recorded, the tasks no result has closed, and every fork (two records in force
that replace the same one).

What this is NOT
----------------
It is not a database, not a sync service and not a claim that the recorded text
is true. It records what a tool asserted and what the sources hashed to; a
reader still has to judge the text. The lock below protects concurrent writers
on one local filesystem, not two machines sharing a network drive.

The file is the record of truth: JSON Lines a person can read, diff and delete
without this tool. A line ends at a line feed (one trailing carriage return is
tolerated) and at nothing else; new lines are written as ASCII JSON, so no
character inside a record can end a line for any other reader either.
`MEMORY.md` beside it is regenerated output, and `memory mirror --notes` writes
an opt-in visible copy of decisions and tasks as notes.

Record formats. Format 1 records carry no `format_version`; their id hashes the
sources in the order given. Format 2 records (written by this version) carry
`format_version: 2`, sources sorted by path, `supersedes` as one id or a list of
ids, and `closes`, the tasks a result completes. `verify` recomputes each line
with its own format, so one store may hold both.

Interface contract: `record` and `resume` keep the signatures other phases
call; new parameters are keyword-only with defaults.

Python 3.10+; standard library only; no network access.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import sys
from urllib.parse import quote
from .platform_support import (LockTimeout, atomic_write as _portable_atomic_write,
                               file_lock)

KINDS = ("decision", "task", "result", "note")
STATES = ("draft", "approved", "published")
SECTIONS = (("decision", "Decisions"), ("task", "Tasks"),
            ("result", "Results"), ("note", "Notes"))

STORE_PARTS = (".context", "memory")
RECORDS_NAME = "records.jsonl"
MIRROR_NAME = "MEMORY.md"
LOCK_NAME = ".lock"
TORN_INFIX = ".torn-"                # records.jsonl.torn-<utc>: what `repair` moved aside
MANIFEST_NAME = "index-manifest.json"  # router/build_index.py writes it beside the index

# Record format this code writes. Format 1 records carry no version key; a
# record whose version is newer than this is refused rather than half-understood.
FORMAT_VERSION = 2

ID_PREFIX = "m-"
ID_HEX = 16
LOCK_TIMEOUT = 30.0          # seconds; only the O_EXCL fallback can time out
LOCK_RETRY = 0.02
SHORT_HASH = 12              # hash characters shown in the Markdown mirrors
READ_BLOCK = 1 << 20

# Opt-in session bill of materials (docs/memory.md): ids, paths and hashes only.
SESSION_PARTS = (".context", "sessions")
SESSION_ENV = "CONTEXT_LAYER_SESSION"
BOM_SCHEMA = "session-bom/v1"
_SAFE_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_ID_IN_TEXT = re.compile(r"m-[0-9a-f]{16}")

# Opt-in visible mirror (`memory mirror --notes FOLDER`).
NOTES_MARKER = ".memory-mirror.json"
NOTES_SCHEMA = "context-layer-memory-notes/v1"
NOTE_KINDS = ("decision", "task")
SUPERSEDED_DIR = "superseded"
_UNLINKABLE = set("[]|#^")

_POLICY = None


class MemoryStoreError(ValueError):
    """A refusal a person is meant to see: bad input, a boundary, source drift.

    ValueError so a caller that only catches ValueError still sees it.
    """


# ---------------------------------------------------------------------------
# Paths, policy and small helpers
# ---------------------------------------------------------------------------

def _policy():
    """router/source_policy.py through the package's one import route.

    `mcp_server.router_module` finds router/ beside the package (wheel) or in a
    checkout, so every component shares one module object and one ConfigError
    class. Boundary checks are never reimplemented here.
    """
    global _POLICY
    if _POLICY is None:
        try:
            from .mcp_server import policy
            _POLICY = policy()
        except ImportError:
            raise MemoryStoreError(
                "router/source_policy.py was not found, so source boundaries cannot be "
                "checked; refusing to touch memory") from None
    return _POLICY


def _vault(vault) -> Path:
    path = Path(vault).expanduser()
    if not path.is_dir():
        raise MemoryStoreError(f"Vault not found: {path.name or vault}")
    return path.resolve()


def store_dir(vault) -> Path:
    """`<vault>/.context/memory`, created on first write."""
    return _vault(vault).joinpath(*STORE_PARTS)


def records_path(vault) -> Path:
    return store_dir(vault) / RECORDS_NAME


def mirror_path(vault) -> Path:
    return store_dir(vault) / MIRROR_NAME


def _store_name(name: str) -> str:
    """Vault-relative name of a store file. Output never shows a home path."""
    return "/".join((*STORE_PARTS, name))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _choice(label: str, value, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise MemoryStoreError(f"Unknown {label}: {value!r}. Use one of: {', '.join(allowed)}")
    return value


def _is_id(value) -> bool:
    if not isinstance(value, str) or not value.startswith(ID_PREFIX):
        return False
    body = value[len(ID_PREFIX):]
    return len(body) == ID_HEX and all(c in "0123456789abcdef" for c in body)


def _is_sha256(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(READ_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def _env(name: str) -> str | None:
    value = (os.environ.get(name) or "").strip()
    return value or None


def _tool(value) -> str:
    # "unknown" is the contract's default, i.e. the caller did not say.
    if isinstance(value, str) and value.strip() and value.strip() != "unknown":
        return value.strip()
    return _env("CONTEXT_LAYER_TOOL") or "cli"


def _session(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return _env(SESSION_ENV)


def _exclude_prefixes(vault: Path) -> tuple[str, ...]:
    """Exclusions from routes.json, honoured before any source is opened."""
    # One strict loader for every entry point: an unusable config would
    # silently widen scope, so it is refused instead.
    try:
        return tuple(_policy().load_exclusions(vault))
    except ValueError as exc:
        raise MemoryStoreError(f"Cannot use .context/routes.json for exclusions: {exc}") from None


def _fsync_dir(directory: Path) -> None:
    """Make a created or renamed entry durable; a no-op where a folder cannot be opened."""
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


def _write_atomic(path: Path, data: bytes) -> None:
    """Replace `path` with `data`: staging file in the same folder, fsync, rename, fsync.

    A crash leaves the old file or the new one, never half of either. The
    staging file gets the usual umask-derived mode, not mkstemp's 0600.
    """
    _portable_atomic_write(path, data, mode=0o666)
    _fsync_dir(path.parent)


def _write_new(path: Path, data: bytes) -> None:
    """Create `path` (refusing to replace anything) and make its bytes durable."""
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_dir(path.parent)


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------

@contextmanager
def _lock(directory: Path):
    """Serialise append + mirror regeneration with an OS-released file lock."""
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with file_lock(directory / LOCK_NAME, timeout=LOCK_TIMEOUT,
                       poll_interval=LOCK_RETRY):
            yield
    except LockTimeout as exc:
        raise MemoryStoreError(
            "Timed out waiting for the memory store lock; retry after the other "
            "writer finishes. The lock file is persistent and must not be deleted."
        ) from exc


# ---------------------------------------------------------------------------
# Reading and writing the store
# ---------------------------------------------------------------------------

def _parse(raw: bytes, label: str) -> tuple[list[tuple[int, dict]], list[str]]:
    """[(line number, record)], plus one problem string per unusable line.

    The package's line model: a line ends at b"\\n", one trailing b"\\r" is
    stripped, and nothing else ends a line (U+2028, U+2029, U+0085 and form
    feed stay inside it). Lines are decoded one by one, so a torn last line
    holding half a UTF-8 sequence is one bad line, not an unreadable file.
    """
    entries: list[tuple[int, dict]] = []
    problems: list[str] = []
    for number, chunk in enumerate(raw.split(b"\n"), 1):
        if chunk.endswith(b"\r"):
            chunk = chunk[:-1]
        try:
            line = chunk.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"line {number}: not valid UTF-8")
            continue
        if not line.strip():
            continue  # tolerated on read, never written
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"line {number}: invalid JSON ({exc.msg})")
            continue
        if not isinstance(value, dict):
            problems.append(f"line {number}: not a JSON object")
            continue
        version = value.get("format_version", 1)
        if isinstance(version, bool) or not isinstance(version, int):
            problems.append(f"line {number}: format_version must be an integer, not {version!r}")
            continue
        if version > FORMAT_VERSION:
            raise MemoryStoreError(
                f"{label} line {number} has format_version {version}, but this "
                f"context-layer reads only up to {FORMAT_VERSION}; upgrade context-layer")
        if version < 1:
            problems.append(f"line {number}: format_version {version} was never written "
                            "by any version of this tool")
            continue
        entries.append((number, value))
    return entries, problems


def _read(path: Path) -> tuple[list[tuple[int, dict]], list[str]]:
    """[(line number, record)], plus one problem string per unusable line."""
    if not path.is_file():
        return [], []
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise MemoryStoreError(f"Cannot read {_store_name(path.name)}: "
                               f"{exc.strerror or exc}") from None
    return _parse(raw, _store_name(path.name))


def _require_readable(problems: list[str], path: Path) -> None:
    if problems:
        raise MemoryStoreError(
            f"{_store_name(path.name)} has {len(problems)} unusable line(s) - {problems[0]}. "
            "Run `context-layer memory verify <vault>`: a torn last line (a crash during "
            "a write) is moved aside by `context-layer memory repair <vault>`; any other "
            "bad line has to be fixed by hand.")


def _append(path: Path, stored: dict) -> None:
    """Append one record as one ASCII JSON line and make it durable.

    ASCII: U+2028, U+2029 and U+0085 are escaped, so even a reader that splits
    on them (str.splitlines does) sees one record per line.
    """
    line = json.dumps(stored, ensure_ascii=True) + "\n"
    created = not path.exists()
    needs_newline = False
    if not created:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell():
                handle.seek(-1, os.SEEK_END)
                needs_newline = handle.read(1) != b"\n"
    with path.open("a", encoding="utf-8", newline="") as handle:
        if needs_newline:  # a hand-edited file may have lost its final newline
            handle.write("\n")
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    if created:
        _fsync_dir(path.parent)


def _unique(items: list[dict]) -> list[dict]:
    out: list[dict] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def _normalise_sources(vault: Path, sources, allow_stale: bool,
                       events: list | None = None) -> list[dict]:
    """Vault-relative {path, sha256}, boundary-checked, hashed now, sorted by path."""
    if sources is None:
        return []
    if isinstance(sources, (str, dict)):
        raise MemoryStoreError("sources must be a list of {path, sha256} objects")
    policy = _policy()
    prefixes = _exclude_prefixes(vault)
    out: list[dict] = []
    for entry in sources:
        if isinstance(entry, str):
            entry = {"path": entry}
        if not isinstance(entry, dict):
            raise MemoryStoreError(
                f"Source must be a path or a {{path, sha256}} object: {entry!r}")
        try:
            name = policy.relative_name(entry.get("path"))
            path = policy.source_path(vault, name, prefixes)
        except ValueError as exc:
            raise MemoryStoreError(f"Source refused: {exc}") from None
        if not path.is_file():
            raise MemoryStoreError(f"Source is not a file in this vault: {name}")
        try:
            current = _digest(path)
        except OSError as exc:
            raise MemoryStoreError(
                f"Source cannot be read: {name} ({exc.strerror or exc})") from None
        given = entry.get("sha256")
        if given is None:
            given = current
        else:
            given = str(given).strip().lower()
            if not _is_sha256(given):
                raise MemoryStoreError(f"Source sha256 must be 64 hex characters: {name}")
            if given != current and not allow_stale:
                raise MemoryStoreError(
                    f"Source drift: {name} hashes to {current[:SHORT_HASH]} on disk but the "
                    f"record claims {given[:SHORT_HASH]}. Re-read the source, or record the "
                    "given hash with --allow-stale / allow_stale=True.")
        if events is not None:
            events.append({"event": "source.check", "path": name, "sha256": given,
                           "current_sha256": current})
        out.append({"path": name, "sha256": given})
    return sorted(_unique(out), key=lambda s: (s["path"], s["sha256"]))


def _ids(label: str, value) -> list[str]:
    """Distinct record ids, sorted, from one id, a list of ids, or None."""
    if value is None:
        return []
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)):
        raise MemoryStoreError(f"{label} must be a record id or a list of record ids")
    out: list[str] = []
    for item in items:
        name = item.strip() if isinstance(item, str) else item
        if not _is_id(name):
            raise MemoryStoreError(
                f"{label} must name record ids like m-0123456789abcdef: {item!r}")
        if name not in out:
            out.append(name)
    return sorted(out)


def _supersedes_value(targets: list[str]):
    """How a format-2 record stores its targets: null, one id, or a list (a merge)."""
    if not targets:
        return None
    return targets[0] if len(targets) == 1 else list(targets)


def _targets(item: dict) -> list[str]:
    """Ids a stored record supersedes, whatever its format; a malformed value gives []."""
    value = item.get("supersedes")
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _closes(item: dict) -> list[str]:
    value = item.get("closes")
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _format(item: dict) -> int:
    version = item.get("format_version", 1)
    return version if isinstance(version, int) and not isinstance(version, bool) else 1


def _record_id(kind: str, text: str, sources: list[dict], supersedes,
               closes: list[str] | None = None, *, version: int = FORMAT_VERSION) -> str:
    """Content address over what the record asserts, not when or by whom.

    Format 1 hashes the sources in the order given and knows no `closes`.
    Format 2 sorts the sources by path and adds `closes` when it is not empty,
    so a format-2 record without `closes` whose sources were already in order
    has the id format 1 gave the same content.
    """
    items = [{"path": s["path"], "sha256": s["sha256"]} for s in sources]
    body = {"kind": kind, "sources": items, "supersedes": supersedes, "text": text}
    if version >= 2:
        items.sort(key=lambda s: (str(s["path"]), str(s["sha256"])))
        targets = [supersedes] if isinstance(supersedes, str) else list(supersedes or [])
        body["supersedes"] = _supersedes_value(
            sorted({t for t in targets if isinstance(t, str)}))
        if closes:
            body["closes"] = sorted(set(closes))
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return ID_PREFIX + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:ID_HEX]


def record_id(kind: str, text: str, sources: list[dict], supersedes: str | None) -> str:
    """The id `record()` would give this content today (format 2, no `closes`).

    For exact-duplicate checks outside this module, for example a component
    comparing a proposal with the records in force. `text` is stripped the way
    `record()` strips it; `sources` are {path, sha256} objects as stored.
    """
    items = _unique([{"path": str(s["path"]), "sha256": str(s["sha256"]).strip().lower()}
                     for s in sources or []])
    return _record_id(kind, str(text).strip(), items, supersedes)


def _content_id(item: dict) -> str | None:
    """Format-2 id of a stored record's content: duplicates are found across formats."""
    kind, text, sources = item.get("kind"), item.get("text"), item.get("sources")
    if not isinstance(kind, str) or not isinstance(text, str) or not isinstance(sources, list):
        return None
    if not all(isinstance(s, dict) and isinstance(s.get("path"), str)
               and isinstance(s.get("sha256"), str) for s in sources):
        return None
    items = _unique([{"path": s["path"], "sha256": s["sha256"]} for s in sources])
    closes = _closes(item) if _format(item) >= 2 else None
    return _record_id(kind, text, items, _supersedes_value(sorted(set(_targets(item)))),
                      closes)


def _line_id(item: dict) -> str:
    """The id a stored line should have, computed with that line's own format."""
    sources = [{"path": s["path"], "sha256": s["sha256"]} for s in item["sources"]]
    return _record_id(item["kind"], item["text"], sources, item.get("supersedes"),
                      _closes(item), version=_format(item))


class _Chain:
    """Supersession over a list of records: who replaces whom, and what is in force."""

    def __init__(self, records: list[dict]):
        self.order: dict[str, int] = {}
        self.by_id: dict[str, dict] = {}
        self.successors: dict[str, list[str]] = {}
        for position, item in enumerate(records):
            identifier = item.get("id")
            if not isinstance(identifier, str):
                continue
            if identifier not in self.by_id:
                self.by_id[identifier] = item
                self.order[identifier] = position
            for target in _targets(item):
                successors = self.successors.setdefault(target, [])
                if identifier not in successors:
                    successors.append(identifier)

    def in_force(self, identifier) -> bool:
        return identifier not in self.successors

    def superseded_by(self, identifier) -> list[str]:
        return list(self.successors.get(identifier, []))

    def _position(self, identifier) -> int:
        return self.order.get(identifier, len(self.order))

    def heads(self, identifier) -> list[str]:
        """Records in force that replace `identifier`, directly or down a chain."""
        if self.in_force(identifier):
            return [identifier]
        found: list[str] = []
        seen = {identifier}
        stack = list(self.successors.get(identifier, []))
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            if self.in_force(current):
                found.append(current)
            else:
                stack.extend(self.successors.get(current, []))
        return sorted(found, key=self._position)

    def conflicts(self) -> list[dict]:
        """Forks: a record replaced twice whose replacements are still both in force."""
        out = []
        for target, successors in self.successors.items():
            if len(successors) < 2:
                continue
            heads = self.heads(target)
            if len(heads) >= 2:
                out.append({"target": target, "heads": heads})
        return sorted(out, key=lambda conflict: self._position(conflict["target"]))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _adopt_command(item: dict, heads: list[str]) -> str:
    """The exact `memory add` that makes a superseded record's content current again."""
    argv = ["context-layer", "memory", "add", "<vault>", "--kind", str(item.get("kind")),
            "--state", str(item.get("state") or "draft"), "--text", str(item.get("text"))]
    for source in item.get("sources") or []:
        if isinstance(source, dict):
            argv += ["--source", f"{source.get('path')}@{source.get('sha256')}"]
    for target in _closes(item):
        argv += ["--closes", target]
    for head in heads:
        argv += ["--supersedes", head]
    return " ".join(arg if arg == "<vault>" else shlex.quote(arg) for arg in argv)


def _find_duplicate(records: list[dict], identifier: str, kind: str, text: str):
    for item in records:
        if item.get("id") == identifier:
            return item
    for item in records:  # format-1 lines whose sources were stored in another order
        if item.get("kind") == kind and item.get("text") == text \
                and _content_id(item) == identifier:
            return item
    return None


def _check_targets(chain: _Chain, targets: list[str], closing: list[str],
                   allow_fork: bool) -> None:
    for target in targets:
        if target not in chain.by_id:
            raise MemoryStoreError(
                f"supersedes names a record this vault does not hold: {target}")
        if not chain.in_force(target) and not allow_fork:
            heads = chain.heads(target)
            hint = " ".join(f"--supersedes {head}" for head in heads) or "--supersedes <id>"
            raise MemoryStoreError(
                f"{target} is already superseded by {', '.join(chain.superseded_by(target))}; "
                "superseding it again would fork the chain. Supersede the record in force "
                f"instead ({hint}), or pass --allow-fork to record a competing version on "
                "purpose")
    for target in closing:
        item = chain.by_id.get(target)
        if item is None:
            raise MemoryStoreError(f"closes names a record this vault does not hold: {target}")
        if item.get("kind") != "task":
            raise MemoryStoreError(
                f"closes names a {item.get('kind')} record, not a task: {target}")
        if not chain.in_force(target):
            heads = ", ".join(chain.heads(target)) or "the task in force"
            raise MemoryStoreError(
                f"closes names a task that is no longer in force: {target} was superseded "
                f"by {', '.join(chain.superseded_by(target))}; close {heads} instead")


def record(vault: Path, *, kind: str, text: str, sources: list[dict] | None = None,
           state: str = "draft", tool: str = "unknown", session: str | None = None,
           supersedes: str | list[str] | None = None, allow_stale: bool = False,
           closes: list[str] | None = None, allow_fork: bool = False) -> dict:
    """Append one record; return the stored record plus derived flags.

    The flags, never written to the file: `duplicate` (nothing was appended
    because a record with the same content exists), `in_force` and
    `superseded_by`. The same kind, text, sources, `supersedes` and `closes`
    give the same id, so re-running a call appends nothing. Content that a later
    record superseded is refused, with the command that adopts it again.
    `supersedes` names one record, or several to merge a fork; a record that is
    already superseded is refused unless `allow_fork`. `closes` (results only)
    names the tasks the result completes.
    """
    vault_path = _vault(vault)
    kind = _choice("kind", kind, KINDS)
    state = _choice("state", state, STATES)
    if not isinstance(text, str) or not text.strip():
        raise MemoryStoreError("Record text must be a nonempty string")
    text = text.strip()
    targets = _ids("supersedes", supersedes)
    closing = _ids("closes", closes)
    if closing and kind != "result":
        raise MemoryStoreError("Only a result closes tasks: closes needs kind result")
    events: list = []
    normalised = _normalise_sources(vault_path, sources, allow_stale, events)
    identifier = _record_id(kind, text, normalised, _supersedes_value(targets), closing)
    directory = vault_path.joinpath(*STORE_PARTS)
    path = directory / RECORDS_NAME

    with _lock(directory):
        entries, problems = _read(path)
        _require_readable(problems, path)
        records = [r for _, r in entries]
        chain = _Chain(records)
        existing = _find_duplicate(records, identifier, kind, text)
        if existing is not None:
            found = existing.get("id")
            if not chain.in_force(found):
                heads = chain.heads(found)
                raise MemoryStoreError(
                    f"This content is already record {found}, which is no longer in force: "
                    f"it was superseded by {', '.join(chain.superseded_by(found))}"
                    + (f" (in force now: {', '.join(heads)})"
                       if heads != chain.superseded_by(found) else "")
                    + ". To make it current again, record it as the replacement of what is "
                    f"in force: {_adopt_command(existing, heads)}")
            result = dict(existing)
            result.update(duplicate=True, in_force=True, superseded_by=[])
            events.append({"event": "memory.duplicate", "id": found})
        else:
            _check_targets(chain, targets, closing, allow_fork)
            stored = {
                "id": identifier,
                "prev": records[-1].get("id") if records else None,
                "ts": _now(),
                "tool": _tool(tool),
                "session": _session(session),
                "kind": kind,
                "state": state,
                "text": text,
                "sources": normalised,
                "supersedes": _supersedes_value(targets),
                "closes": closing,
                "format_version": FORMAT_VERSION,
            }
            _append(path, stored)
            _write_mirror(vault_path, records + [stored])
            result = dict(stored)
            result.update(duplicate=False, in_force=True, superseded_by=[])
            events.append({"event": "memory.write", "id": identifier})
    _session_log(vault_path, "record", events)
    return result


def load(vault: Path) -> list[dict]:
    """Every record in file order. Refuses if any line is unusable."""
    path = records_path(vault)
    entries, problems = _read(path)
    _require_readable(problems, path)
    return [r for _, r in entries]


def _manifest_paths(vault: Path) -> dict:
    """sha256 -> vault-relative paths from the index manifest; {} when there is none."""
    try:
        payload = json.loads((vault / ".context" / MANIFEST_NAME).read_text(encoding="utf-8"))
        sources = payload["sources"]
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return {}
    by_hash: dict = {}
    if isinstance(sources, list):
        for entry in sources:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str) \
                    and _is_sha256(entry.get("sha256")):
                by_hash.setdefault(entry["sha256"], []).append(entry["path"])
    return by_hash


def _current(vault: Path, policy, prefixes, name, cache: dict):
    """SHA-256 of a source now, or None when it is gone, unreadable or out of bounds."""
    if isinstance(name, str) and name in cache:
        return cache[name]
    digest = None
    try:
        path = policy.source_path(vault, name, prefixes)
        if path.is_file():
            digest = _digest(path)
    except (ValueError, OSError, TypeError):
        digest = None  # deleted, excluded since, or no longer readable
    if isinstance(name, str):
        cache[name] = digest
    return digest


def _stale_for(vault: Path, records: list[dict], events: list | None = None) -> list[dict]:
    """Sources whose bytes changed, vanished or fell outside the boundary.

    A missing source is looked up by its recorded SHA-256 in the index manifest;
    a copy with those exact bytes at another in-scope path is `moved_to`.
    """
    policy = _policy()
    prefixes = _exclude_prefixes(vault)
    cache: dict = {}
    by_hash = None
    seen: set = set()
    stale: list[dict] = []
    for item in records:
        for source in item.get("sources") or []:
            if not isinstance(source, dict):
                continue
            name, recorded = source.get("path"), source.get("sha256")
            current = _current(vault, policy, prefixes, name, cache)
            if events is not None and isinstance(name, str) and _is_sha256(recorded):
                events.append({"event": "source.check", "path": name, "sha256": recorded,
                               "current_sha256": current})
            if current == recorded:
                continue
            key = (item.get("id"), name)
            if key in seen:
                continue
            seen.add(key)
            moved: list[str] = []
            if current is None and _is_sha256(recorded):
                if by_hash is None:
                    by_hash = _manifest_paths(vault)
                moved = sorted({candidate for candidate in by_hash.get(recorded, [])
                                if candidate != name and _current(
                                    vault, policy, prefixes, candidate, cache) == recorded})
            stale.append({"id": item.get("id"), "path": name, "recorded_sha256": recorded,
                          "current_sha256": current, "moved_to": moved})
    return stale


def _open_tasks(records: list[dict], chain: _Chain) -> tuple[list[dict], list[dict]]:
    """(tasks in force that no result in force closes, tasks closed only by a mention).

    A result closes a task by superseding it or by naming it in `closes`. A
    format-1 result that quotes a task id in its text still closes it, reported
    as closed by mention; format-2 text never closes anything.
    """
    tasks = [r for r in records if r.get("kind") == "task" and chain.in_force(r.get("id"))]
    task_ids = [r.get("id") for r in tasks if isinstance(r.get("id"), str)]
    open_ids = set(task_ids)
    closed: set = set()
    mention: dict = {}
    for item in records:
        if item.get("kind") != "result" or not chain.in_force(item.get("id")):
            continue
        closed.update(_closes(item))
        body = item.get("text")
        if _format(item) == 1 and isinstance(body, str):
            # Ids found in the text, not every task id tried against it: linear in the text.
            for task_id in _ID_IN_TEXT.findall(body):
                if task_id in open_ids:
                    mention.setdefault(task_id, item.get("id"))
    by_mention = [{"task": task_id, "result": mention[task_id]} for task_id in task_ids
                  if task_id in mention and task_id not in closed]
    shut = closed | set(mention)
    return [r for r in tasks if r.get("id") not in shut], by_mention


def _closers(records: list[dict], chain: _Chain) -> dict:
    """task id -> ids of the results in force that close it through `closes`."""
    out: dict = {}
    for item in records:
        if item.get("kind") == "result" and chain.in_force(item.get("id")):
            for target in _closes(item):
                out.setdefault(target, []).append(item.get("id"))
    return out


def resume(vault: Path, *, limit: int = 20, kinds: list[str] | None = None) -> dict:
    """The continuation packet for the next session or tool.

    Keys: `generated_at`, `vault` (name only), `records` (in force, newest
    first), `stale`, `open_tasks`, `conflicts` (forks: {target, heads}) and
    `closed_by_mention` (tasks a format-1 result closed only by quoting the id).
    """
    vault_path = _vault(vault)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise MemoryStoreError("limit must be a non-negative integer")
    wanted = None
    if kinds is not None:
        wanted = {_choice("kind", kind, KINDS) for kind in kinds}
    records = load(vault_path)
    chain = _Chain(records)
    effective = [r for r in records if chain.in_force(r.get("id"))]
    selected = [r for r in reversed(effective)
                if wanted is None or r.get("kind") in wanted][:limit]
    open_tasks, by_mention = _open_tasks(records, chain)
    open_tasks = list(reversed(open_tasks))[:limit]
    events: list = []
    packet = {
        "generated_at": _now(),
        "vault": vault_path.name,   # basename only; never an absolute personal path
        "records": [dict(r) for r in selected],
        "stale": _stale_for(vault_path, selected, events),
        "open_tasks": [dict(r) for r in open_tasks],
        "conflicts": chain.conflicts(),
        "closed_by_mention": by_mention,
    }
    reads = [{"event": "memory.read", "id": r.get("id")} for r in selected + open_tasks
             if _is_id(r.get("id"))]
    _session_log(vault_path, "resume", reads + events)
    return packet


def _moved_hint(entry: dict) -> str:
    if not entry.get("moved_to"):
        return ""
    return (f"; the same bytes are now at {', '.join(entry['moved_to'])} "
            f"(`context-layer memory rebind <vault> {entry['id']}` records the new path)")


def verify(vault: Path) -> list[str]:
    """Problems with the store; an empty list means healthy."""
    vault_path = _vault(vault)
    path = records_path(vault_path)
    entries, problems = _read(path)
    problems = list(problems)
    records = [r for _, r in entries]
    known = {r.get("id") for r in records}
    kind_of = {}
    for item in records:
        if isinstance(item.get("id"), str):
            kind_of.setdefault(item["id"], item.get("kind"))
    seen: dict = {}
    previous_id = None
    for number, item in entries:
        label = f"line {number}"
        version = _format(item)
        identifier = item.get("id")
        if not _is_id(identifier):
            problems.append(f"{label}: id is missing or malformed")
        elif identifier in seen:
            problems.append(f"{label}: duplicate id {identifier} "
                            f"(first seen on line {seen[identifier]})")
        else:
            seen[identifier] = number
        for key in ("ts", "kind", "state", "text"):
            if not isinstance(item.get(key), str) or not item.get(key):
                problems.append(f"{label}: {key} is missing or not a string")
        if isinstance(item.get("kind"), str) and item["kind"] not in KINDS:
            problems.append(f"{label}: unknown kind {item['kind']!r}")
        if isinstance(item.get("state"), str) and item["state"] not in STATES:
            problems.append(f"{label}: unknown state {item['state']!r}")
        sources = item.get("sources")
        if not isinstance(sources, list):
            problems.append(f"{label}: sources must be a list")
            sources = []
        usable = isinstance(item.get("sources"), list)
        for source in sources:
            if (not isinstance(source, dict) or not isinstance(source.get("path"), str)
                    or not _is_sha256(source.get("sha256"))):
                problems.append(f"{label}: source entry must be {{path, sha256}} "
                                "with a hex digest")
                usable = False
        value = item.get("supersedes")
        if version >= 2:
            well_formed = (value is None or _is_id(value) or (
                isinstance(value, list) and len(value) >= 2
                and all(_is_id(v) for v in value) and len(set(value)) == len(value)))
            shape = "null, a record id, or a list of two or more distinct ids"
        else:
            well_formed = value is None or _is_id(value)
            shape = "null or a record id"
        if not well_formed:
            problems.append(f"{label}: supersedes must be {shape}")
            usable = False
        else:
            for target in _targets(item):
                if target not in known:
                    problems.append(f"{label}: supersedes names an unknown record {target}")
        if version >= 2:
            closes = item.get("closes")
            if not isinstance(closes, list) or not all(_is_id(v) for v in closes) \
                    or len(set(closes)) != len(closes):
                problems.append(f"{label}: closes must be a list of distinct record ids")
                usable = False
            else:
                if closes and item.get("kind") != "result":
                    problems.append(f"{label}: only a result may close tasks")
                for target in closes:
                    if target not in known:
                        problems.append(f"{label}: closes names an unknown record {target}")
                    elif kind_of.get(target) != "task":
                        problems.append(f"{label}: closes names a {kind_of.get(target)} "
                                        f"record, not a task: {target}")
        elif "closes" in item:
            problems.append(f"{label}: closes is a format 2 field, but the line has no "
                            "format_version 2")
        if _is_id(identifier) and usable and isinstance(item.get("kind"), str) \
                and isinstance(item.get("text"), str):
            if _line_id(item) != identifier:
                problems.append(f"{label}: content does not match its id ({identifier}); "
                                "the line was edited in place")
        if item.get("prev") != previous_id:
            problems.append(f"{label}: prev is {item.get('prev')!r}, expected {previous_id!r} "
                            "(a line was removed, reordered or inserted)")
        previous_id = item.get("id")
    for conflict in _Chain(records).conflicts():
        heads = conflict["heads"]
        problems.append(
            f"fork: {conflict['target']} is replaced by {len(heads)} records that are all in "
            f"force ({', '.join(heads)}); record one that supersedes all of them "
            f"({' '.join('--supersedes ' + head for head in heads)}) to resolve it")
    # A superseded record no longer asserts anything, so only records in force
    # can be stale: after `rebind` the replaced record's missing path is history.
    chain = _Chain(records)
    in_force = [r for r in records if chain.in_force(r.get("id"))]
    for entry in _stale_for(vault_path, in_force):
        current = entry["current_sha256"][:SHORT_HASH] if entry["current_sha256"] else "missing"
        problems.append(f"stale source: {entry['path']} recorded as "
                        f"{str(entry['recorded_sha256'])[:SHORT_HASH]}, now {current} "
                        f"(record {entry['id']}){_moved_hint(entry)}")
    return problems


def _is_record_line(chunk: bytes) -> bool:
    if chunk.endswith(b"\r"):
        chunk = chunk[:-1]
    try:
        return isinstance(json.loads(chunk.decode("utf-8")), dict)
    except (UnicodeDecodeError, ValueError):
        return False


def repair(vault: Path, *, dry_run: bool = False) -> dict:
    """Move a torn last line (no final newline, not a JSON object) aside.

    Only that fragment is touched: its bytes are copied to
    `records.jsonl.torn-<utc>` and cut from the store, and MEMORY.md is rebuilt.
    Every other unusable line is reported, never changed. Returns {store, torn,
    bytes, moved_to, dry_run, problems}; `problems` are what remains afterwards.
    """
    vault_path = _vault(vault)
    directory = vault_path.joinpath(*STORE_PARTS)
    path = directory / RECORDS_NAME
    result = {"store": _store_name(RECORDS_NAME), "torn": False, "bytes": 0,
              "moved_to": None, "dry_run": dry_run, "problems": []}
    if not path.is_file():
        return result
    with _lock(directory):
        raw = path.read_bytes()
        cut = raw.rfind(b"\n") + 1
        tail = raw[cut:]
        remaining = raw
        if tail.strip() and not _is_record_line(tail):
            base = f"{RECORDS_NAME}{TORN_INFIX}{_stamp()}"
            name, counter = base, 1
            while (directory / name).exists():
                counter += 1
                name = f"{base}-{counter}"
            result.update(torn=True, bytes=len(tail), moved_to=_store_name(name))
            remaining = raw[:cut]
            if not dry_run:
                _write_new(directory / name, tail)
                with path.open("r+b") as handle:
                    handle.truncate(cut)
                    handle.flush()
                    os.fsync(handle.fileno())
        entries, problems = _parse(remaining, _store_name(RECORDS_NAME))
        result["problems"] = problems
        if result["torn"] and not dry_run and not problems:
            _write_mirror(vault_path, [r for _, r in entries])
    return result


# ---------------------------------------------------------------------------
# Moved sources
# ---------------------------------------------------------------------------

def rebind(vault: Path, identifier: str, *, moves: dict | None = None,
           tool: str = "unknown", session: str | None = None) -> dict:
    """Append a record that supersedes `identifier` with the new path of moved sources.

    Nothing is edited: the new record carries the same kind, state, text,
    `closes` and hashes, and only the path of a source whose bytes now sit
    elsewhere changes. Without `moves` ({old path: new path}) a missing source is
    rebound to its one `moved_to` copy; a new path must hold the recorded bytes.
    Returns {"record": <stored>, "rebound": identifier, "changes": [{from, to}]}.
    """
    vault_path = _vault(vault)
    moves = dict(moves or {})
    identifier = str(identifier).strip()
    if not _is_id(identifier):
        raise MemoryStoreError(f"Not a record id: {identifier}")
    records = load(vault_path)
    chain = _Chain(records)
    item = chain.by_id.get(identifier)
    if item is None:
        raise MemoryStoreError(f"No record {identifier} in this vault")
    if not chain.in_force(identifier):
        raise MemoryStoreError(
            f"{identifier} is superseded by {', '.join(chain.superseded_by(identifier))}; "
            f"rebind the record in force instead: {', '.join(chain.heads(identifier))}")
    sources = [s for s in item.get("sources") or [] if isinstance(s, dict)]
    unknown = sorted(set(moves) - {s.get("path") for s in sources})
    if unknown:
        raise MemoryStoreError(f"{identifier} does not rest on {', '.join(unknown)}")
    stale = {entry["path"]: entry for entry in _stale_for(vault_path, [item])}
    policy, prefixes, cache = _policy(), _exclude_prefixes(vault_path), {}
    rebound, changes = [], []
    for source in sources:
        old, digest = source.get("path"), source.get("sha256")
        new = old
        if old in moves:
            try:
                new = policy.relative_name(moves[old])
            except ValueError as exc:
                raise MemoryStoreError(f"New path refused: {exc}") from None
            now = _current(vault_path, policy, prefixes, new, cache)
            if now != digest:
                raise MemoryStoreError(
                    f"{new} does not hold the bytes {identifier} recorded for {old} "
                    f"({str(digest)[:SHORT_HASH]}, found "
                    f"{now[:SHORT_HASH] if now else 'no readable file'}); that is a new "
                    "source, so record a new record instead of a rebind")
        elif old in stale and stale[old]["current_sha256"] is None:
            candidates = stale[old]["moved_to"]
            if len(candidates) == 1:
                new = candidates[0]
            elif len(candidates) > 1:
                raise MemoryStoreError(
                    f"{old} has {len(candidates)} copies with the recorded bytes "
                    f"({', '.join(candidates)}); name one with --move {old} NEW")
        if new != old:
            changes.append({"from": old, "to": new})
        rebound.append({"path": new, "sha256": digest})
    if not changes:
        raise MemoryStoreError(
            f"No source of {identifier} has moved: nothing is missing with a copy in the index "
            "manifest (rebuild the index after a rename), and no --move was given")
    closing = [target for target in _closes(item)
               if chain.in_force(target) and chain.by_id.get(target, {}).get("kind") == "task"]
    stored = record(vault_path, kind=item.get("kind"), text=item.get("text"), sources=rebound,
                    state=item.get("state") or "draft", tool=tool, session=session,
                    supersedes=identifier, closes=closing or None)
    return {"record": stored, "rebound": identifier, "changes": changes}


# ---------------------------------------------------------------------------
# Session bill of materials (opt-in)
# ---------------------------------------------------------------------------

def _session_file(vault: Path, session: str) -> Path:
    stem = session if _SAFE_SESSION.fullmatch(session) else \
        "sha256-" + hashlib.sha256(session.encode("utf-8")).hexdigest()[:32]
    return vault.joinpath(*SESSION_PARTS) / f"{stem}.jsonl"


def session_path(vault, session: str) -> Path:
    """`.context/sessions/<id>.jsonl`; an id that is not a plain file name is hashed."""
    return _session_file(_vault(vault), str(session))


def _session_log(vault: Path, op: str, events: list) -> None:
    """With CONTEXT_LAYER_SESSION set, append what this call used: ids, paths, hashes.

    Never text: a line holds the schema, time, session id, operation, event and
    either a record id or a source path with its recorded and current hashes.
    Identical events of one call are written once. A failure to write is
    reported on stderr and never fails the memory operation itself.
    """
    session = _env(SESSION_ENV)
    if not session or not events:
        return
    stamp = _now()
    lines, seen = [], set()
    for event in events:
        row = {"schema": BOM_SCHEMA, "ts": stamp, "session": session, "op": op}
        row.update(event)
        key = json.dumps(row, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        lines.append(json.dumps(row, ensure_ascii=True) + "\n")
    path = _session_file(vault, session)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        created = not path.exists()
        with file_lock(path.with_name(path.name + ".lock")):
            with path.open("a", encoding="utf-8", newline="") as handle:
                handle.write("".join(lines))
                handle.flush()
                os.fsync(handle.fileno())
        if created:
            _fsync_dir(path.parent)
    except OSError as exc:
        print(f"context-layer memory: the session record in {'/'.join(SESSION_PARTS)} was "
              f"not written: {exc.strerror or exc}", file=sys.stderr)


def session_view(vault, session: str) -> dict:
    """One session's bill of materials, with what is stale now.

    Records carry the events that touched them and whether they are still in
    force; sources carry whether they were stale when checked and whether the
    file still holds the recorded bytes now.
    """
    vault_path = _vault(vault)
    session = str(session).strip()
    path = _session_file(vault_path, session)
    shown = "/".join((*SESSION_PARTS, path.name))
    if not session or not path.is_file():
        raise MemoryStoreError(
            f"No session record {shown}: it is written only while {SESSION_ENV} is set")
    events, problems = [], []
    for number, chunk in enumerate(path.read_bytes().split(b"\n"), 1):
        chunk = chunk[:-1] if chunk.endswith(b"\r") else chunk
        if not chunk.strip():
            continue
        try:
            row = json.loads(chunk.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            problems.append(f"line {number}: not a JSON line")
            continue
        if not isinstance(row, dict) or row.get("schema") != BOM_SCHEMA:
            problems.append(f"line {number}: not a {BOM_SCHEMA} line")
            continue
        events.append(row)
    try:
        chain = _Chain(load(vault_path))
    except MemoryStoreError:
        chain = None
    records: dict = {}
    sources: dict = {}
    other: dict = {}
    for row in events:
        name = row.get("event")
        if name in ("memory.write", "memory.duplicate", "memory.read") and _is_id(row.get("id")):
            entry = records.setdefault(row["id"], {"id": row["id"], "events": []})
            if name not in entry["events"]:
                entry["events"].append(name)
        elif name == "source.check" and isinstance(row.get("path"), str) \
                and _is_sha256(row.get("sha256")):
            entry = sources.setdefault((row["path"], row["sha256"]), {
                "path": row["path"], "sha256": row["sha256"], "checks": 0,
                "stale_when_checked": False})
            entry["checks"] += 1
            if row.get("current_sha256") != row["sha256"]:
                entry["stale_when_checked"] = True
        else:
            other[str(name)] = other.get(str(name), 0) + 1
    for entry in records.values():
        if chain is None or entry["id"] not in chain.by_id:
            entry.update(in_force=None, superseded_by=[])
        else:
            entry.update(in_force=chain.in_force(entry["id"]),
                         superseded_by=chain.superseded_by(entry["id"]))
    policy, prefixes, cache = _policy(), _exclude_prefixes(vault_path), {}
    for entry in sources.values():
        now = _current(vault_path, policy, prefixes, entry["path"], cache)
        entry["now"] = "current" if now == entry["sha256"] else (
            "missing" if now is None else "changed")
    stamps = [row.get("ts") for row in events if isinstance(row.get("ts"), str)]
    return {"schema": "session-bom-view/v1", "session": session, "file": shown,
            "events": len(events), "first": min(stamps) if stamps else None,
            "last": max(stamps) if stamps else None, "records": list(records.values()),
            "sources": list(sources.values()), "other_events": other, "problems": problems}


def session_list(vault) -> list[dict]:
    """Every session record in the vault: file name, event count, last timestamp."""
    folder = _vault(vault).joinpath(*SESSION_PARTS)
    out = []
    for path in sorted(folder.glob("*.jsonl")) if folder.is_dir() else []:
        lines = [line for line in path.read_bytes().split(b"\n") if line.strip()]
        last = None
        if lines:
            try:
                last = json.loads(lines[-1].decode("utf-8")).get("ts")
            except (UnicodeDecodeError, ValueError, AttributeError):
                last = None
        out.append({"session": path.stem, "events": len(lines), "last": last})
    return out


# ---------------------------------------------------------------------------
# Markdown mirror (.context/memory/MEMORY.md)
# ---------------------------------------------------------------------------

def _link(name: str) -> str:
    """Link from .context/memory/MEMORY.md back to a vault-relative source."""
    return f"[{name}](../../{quote(name)})"


def _text_lines(text) -> list[str]:
    """The record text split with the package's line model (line feed only)."""
    return [line[:-1] if line.endswith("\r") else line for line in str(text).split("\n")]


def _ids_list(ids: list[str]) -> str:
    return ", ".join(f"`{i}`" for i in ids)


def _render_mirror(vault_name: str, records: list[dict], generated_at: str) -> str:
    chain = _Chain(records)
    _, by_mention = _open_tasks(records, chain)
    mention = {entry["task"]: entry["result"] for entry in by_mention}
    closers = _closers(records, chain)
    in_force = len([r for r in records if chain.in_force(r.get("id"))])
    lines = [
        "# Memory",
        "",
        "Derived file. `context-layer memory` rewrites it from `records.jsonl` after",
        "every write, so edits made here are lost. Edit or delete `records.jsonl`",
        "instead; that file is the record, this one is only a view of it.",
        "",
        f"Vault `{vault_name}` · generated {generated_at} · "
        f"{len(records)} record(s), {in_force} in force.",
    ]
    if not records:
        lines += ["", "No records yet."]
        return "\n".join(lines) + "\n"
    conflicts = chain.conflicts()
    if conflicts:
        lines += ["", "## Conflicts", ""]
        for conflict in conflicts:
            lines.append(f"- `{conflict['target']}` is replaced by {len(conflict['heads'])} "
                         f"records in force: {_ids_list(conflict['heads'])}. A record that "
                         "supersedes all of them resolves it.")
    for kind, heading in SECTIONS:
        section = [r for r in reversed(records) if r.get("kind") == kind]
        if not section:
            continue
        lines += ["", f"## {heading}"]
        for item in section:
            identifier = item.get("id", "(no id)")
            meta = [f"`{item.get('ts', '')}`", f"tool `{item.get('tool', '')}`",
                    f"state `{item.get('state', '')}`"]
            if item.get("session"):
                meta.append(f"session `{item['session']}`")
            lines += ["", f"### {identifier}", "", " · ".join(meta)]
            if _targets(item):
                lines += ["", f"Supersedes {_ids_list(_targets(item))}."]
            if not chain.in_force(identifier):
                lines += ["", f"Superseded by {_ids_list(chain.superseded_by(identifier))}."]
            if _closes(item):
                lines += ["", f"Closes {_ids_list(_closes(item))}."]
            if kind == "task" and chain.in_force(identifier):
                if identifier in closers:
                    lines += ["", f"Closed by {_ids_list(closers[identifier])}."]
                elif identifier in mention:
                    lines += ["", f"Closed by mention in `{mention[identifier]}` (a format 1 "
                                  "result that quotes the id; check it)."]
                else:
                    lines += ["", "Open."]
            lines.append("")
            # Quoted so a heading inside a record cannot restructure this file.
            lines += [f"> {line}" if line else ">" for line in _text_lines(item.get("text", ""))]
            sources = [s for s in item.get("sources") or [] if isinstance(s, dict)]
            if sources:
                lines += ["", "Sources:"]
                for source in sources:
                    digest = str(source.get("sha256", ""))[:SHORT_HASH]
                    lines.append(f"- {_link(str(source.get('path', '')))} `{digest}`")
    return "\n".join(lines) + "\n"


def _write_mirror(vault: Path, records: list[dict]) -> Path:
    """Regenerate MEMORY.md atomically. Callers hold the lock."""
    path = vault.joinpath(*STORE_PARTS) / MIRROR_NAME
    _write_atomic(path, _render_mirror(vault.name, records, _now()).encode("utf-8"))
    return path


def rebuild_mirror(vault: Path) -> Path:
    """Regenerate MEMORY.md from the store; for use after a hand edit."""
    vault_path = _vault(vault)
    directory = vault_path.joinpath(*STORE_PARTS)
    with _lock(directory):
        return _write_mirror(vault_path, load(vault_path))


# ---------------------------------------------------------------------------
# Opt-in visible mirror: one note per decision and task (`mirror --notes`)
# ---------------------------------------------------------------------------

def _yaml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value, ensure_ascii=False)


def _wikilink(name: str) -> str:
    """[[path]] for Obsidian, or the path in backticks when a wikilink cannot hold it."""
    if any(ch in _UNLINKABLE for ch in name) or not name:
        return f"`{name}`"
    return f"[[{name}]]"


def _notes_folder(vault: Path, folder) -> tuple[str, Path]:
    policy = _policy()
    try:
        name = policy.relative_name(str(folder).strip())
    except ValueError as exc:
        raise MemoryStoreError(f"--notes needs a vault-relative folder: {exc}") from None
    parts = PurePosixPath(name).parts
    if any(part.startswith(".") or part in policy.BLOCKED_PARTS for part in parts):
        raise MemoryStoreError(
            f"--notes {name}: Obsidian does not show dot or tool folders; choose a visible "
            "folder such as Memory")
    try:
        target = policy.source_path(vault, name, ())
    except ValueError as exc:
        raise MemoryStoreError(f"--notes {name}: {exc}") from None
    if target.exists() and not target.is_dir():
        raise MemoryStoreError(f"--notes {name} exists and is not a folder")
    return name, target


def _read_marker(path: Path):
    if not path.is_file():
        return None
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise MemoryStoreError(f"{path.name} in the notes folder cannot be read; remove the "
                               "folder by hand or restore the file") from None
    if not isinstance(marker, dict) or marker.get("schema") != NOTES_SCHEMA \
            or not isinstance(marker.get("files"), dict):
        raise MemoryStoreError(f"{path.name} in the notes folder was not written by this "
                               "version of `memory mirror`")
    return marker


def _render_note(item: dict, chain: _Chain, closers: dict, mention: dict, state_of,
                 folder: str, excluded: bool, generated_at: str) -> str:
    identifier = item["id"]
    kind = item.get("kind")
    in_force = chain.in_force(identifier)
    sources = [s for s in item.get("sources") or [] if isinstance(s, dict)]
    states = [state_of(s.get("path"), s.get("sha256")) for s in sources]
    front = ["---", f"memory_id: {identifier}", f"kind: {kind}",
             f"state: {item.get('state')}", f"recorded: {_yaml(item.get('ts'))}",
             f"in_force: {_yaml(in_force)}"]
    targets = _targets(item)
    if len(targets) == 1:
        front.append(f"supersedes: {_yaml('[[' + targets[0] + ']]')}")
    elif targets:
        front += ["supersedes:"] + [f"  - {_yaml('[[' + t + ']]')}" for t in targets]
    successors = chain.superseded_by(identifier)
    if successors:
        front += ["superseded_by:"] + [f"  - {_yaml('[[' + s + ']]')}" for s in successors]
    if kind == "task":
        closed = identifier in closers or identifier in mention
        front.append(f"status: {'closed' if closed else 'open'}")
    front += [f"stale: {_yaml(any(state != 'current' for state in states))}",
              "derived: true", f"generated: {_yaml(generated_at)}", "---"]
    body = ["", f"# {str(kind).capitalize()} {identifier}", ""]
    body += [f"> {line}" if line else ">" for line in _text_lines(item.get("text", ""))]
    if sources:
        body += ["", f"Sources, with the SHA-256 prefix recorded and the file's state at "
                     f"{generated_at}:", ""]
        for source, state in zip(sources, states):
            label = {"current": "current", "changed": "changed since it was recorded",
                     "missing": "missing"}[state]
            body.append(f"- {_wikilink(str(source.get('path', '')))} "
                        f"`{str(source.get('sha256', ''))[:SHORT_HASH]}` {label}")
    links = []
    if targets:
        links.append("Supersedes " + ", ".join(f"[[{t}]]" for t in targets) + ".")
    if successors:
        links.append("Superseded by " + ", ".join(f"[[{s}]]" for s in successors) + ".")
    if kind == "task" and in_force:
        if identifier in closers:
            links.append(f"Closed by result {_ids_list(closers[identifier])}.")
        elif identifier in mention:
            links.append(f"Closed by mention in result `{mention[identifier]}` (a format 1 "
                         "record that quotes the id; check it).")
        else:
            links.append("Open: no result in force closes it.")
    if links:
        body += [""] + links
    where = (f"The `{folder}` folder is excluded from retrieval (`exclude_prefixes` in "
             "`.context/routes.json`), so this agent-written text is never served as evidence."
             if excluded else
             f"The `{folder}` folder is not excluded from retrieval, so this text can be served "
             "as search evidence; add it to `exclude_prefixes` in `.context/routes.json` to "
             "keep it out.")
    body += ["", "---", "",
             f"Derived note: `context-layer memory mirror --notes {folder}` wrote it from "
             "`.context/memory/records.jsonl`, the record of truth. Editing it changes nothing "
             "in memory, and the mirror leaves an edited note alone. " + where, ""]
    return "\n".join(front + body)


def _planned_notes(vault: Path, folder: str, excluded: bool, generated_at: str) -> dict:
    """{path inside the folder: note bytes} for every decision and task in the store."""
    records = load(vault)
    chain = _Chain(records)
    _, by_mention = _open_tasks(records, chain)
    mention = {entry["task"]: entry["result"] for entry in by_mention}
    closers = _closers(records, chain)
    policy, prefixes, cache = _policy(), _exclude_prefixes(vault), {}

    def state_of(name, recorded):
        now = _current(vault, policy, prefixes, name, cache)
        return "current" if now == recorded else ("missing" if now is None else "changed")

    planned = {}
    for item in records:
        identifier = item.get("id")
        if item.get("kind") not in NOTE_KINDS or not _is_id(identifier):
            continue
        rel = f"{identifier}.md" if chain.in_force(identifier) \
            else f"{SUPERSEDED_DIR}/{identifier}.md"
        if rel in planned or f"{SUPERSEDED_DIR}/{identifier}.md" in planned:
            continue  # a duplicated id: the first line wins, as everywhere else
        planned[rel] = _render_note(item, chain, closers, mention, state_of, folder, excluded,
                                    generated_at).encode("utf-8")
    return planned


def _routes_config(vault: Path) -> dict:
    """routes.json through the one strict loader; a missing or unusable file is refused."""
    policy = _policy()
    try:
        return policy.load_config(policy.config_path(vault), required=True)
    except ValueError as exc:
        raise MemoryStoreError(f"Cannot use .context/routes.json: {exc}") from None


def _routes_update(vault: Path, change) -> None:
    """Rewrite routes.json with one change; every other key is kept as it was."""
    config = change(dict(_routes_config(vault)))
    _write_atomic(_policy().config_path(vault),
                  (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def mirror_notes(vault: Path, folder, *, remove: bool = False, dry_run: bool = False) -> dict:
    """Write (or remove) the opt-in visible mirror: one note per decision and task.

    In force: `<folder>/<id>.md`; superseded: `<folder>/superseded/<id>.md`.
    The folder is added to `exclude_prefixes` in routes.json the first time, so
    agent-written memory never becomes evidence; if a person removes that entry,
    it is not added again. Files the mirror did not write are never touched, and
    a note edited since the mirror wrote it is left alone and reported. A
    snapshot: run the command again to refresh it.
    """
    vault_path = _vault(vault)
    policy = _policy()
    name, target = _notes_folder(vault_path, folder)
    marker_path = target / NOTES_MARKER
    marker = _read_marker(marker_path)
    previous = dict(marker["files"]) if marker else {}
    edited = sorted(rel for rel, digest in previous.items()
                    if (target / rel).is_file() and _digest(target / rel) != digest)
    if remove:
        return _remove_notes(vault_path, name, target, marker, previous, edited, dry_run)
    if target.is_dir() and marker is None and any(target.iterdir()):
        raise MemoryStoreError(f"{name} already holds files that `memory mirror` did not "
                               "write; choose a new or empty folder")
    if not policy.config_path(vault_path).is_file():
        raise MemoryStoreError("No .context/routes.json in this vault; run `context-layer init "
                               "<vault>` first, so the notes folder can be kept out of retrieval")
    prefixes = _exclude_prefixes(vault_path)
    added_before = bool(marker and marker.get("exclusion_added"))
    if policy.excluded(name, prefixes):
        exclusion = "already excluded"
    elif added_before:
        exclusion = "removed by hand"
    else:
        exclusion = "added"
    excluded = exclusion != "removed by hand"
    generated_at = _now()
    with _lock(vault_path.joinpath(*STORE_PARTS)):
        planned = _planned_notes(vault_path, name, excluded, generated_at)
    foreign = sorted(rel for rel in planned if (target / rel).exists() and rel not in previous)
    if foreign:
        raise MemoryStoreError(f"{name}/{foreign[0]} exists and was not written by `memory "
                               "mirror`; move it away first")
    write = {rel: data for rel, data in planned.items() if rel not in edited}
    obsolete = sorted(rel for rel in previous if rel not in planned and rel not in edited)
    result = {"folder": name, "notes": len(planned),
              "in_force": len([rel for rel in planned if "/" not in rel]),
              "superseded": len([rel for rel in planned if "/" in rel]),
              "written": sorted(write), "removed": obsolete, "edited_kept": edited,
              "exclusion": exclusion, "dry_run": dry_run}
    if dry_run:
        return result
    if exclusion == "added":
        def add(config):
            # A trailing slash marks a folder, as `init` writes its prefixes.
            config["exclude_prefixes"] = list(config.get("exclude_prefixes") or []) + [name + "/"]
            return config
        _routes_update(vault_path, add)
    for rel, data in write.items():
        _write_atomic(target / rel, data)
    for rel in obsolete:
        (target / rel).unlink(missing_ok=True)
    superseded_dir = target / SUPERSEDED_DIR
    if superseded_dir.is_dir() and not any(superseded_dir.iterdir()):
        superseded_dir.rmdir()
    files = {rel: hashlib.sha256(data).hexdigest() for rel, data in write.items()}
    files.update({rel: previous[rel] for rel in edited})
    new_marker = {"schema": NOTES_SCHEMA, "folder": name, "generated_at": generated_at,
                  "exclusion_added": added_before or exclusion == "added",
                  "files": dict(sorted(files.items()))}
    _write_atomic(marker_path, (json.dumps(new_marker, indent=2) + "\n").encode("utf-8"))
    return result


def _remove_notes(vault: Path, name: str, target: Path, marker, previous: dict,
                  edited: list, dry_run: bool) -> dict:
    if marker is None:
        raise MemoryStoreError(f"{name} holds no memory mirror ({NOTES_MARKER} is missing); "
                               "nothing to remove")
    delete = sorted(rel for rel in previous if rel not in edited and (target / rel).is_file())
    policy = _policy()
    try:
        config = _routes_config(vault)
    except MemoryStoreError:
        config = None
    present = config is not None and any(
        policy.relative_name(prefix) == name for prefix in config.get("exclude_prefixes") or [])
    if not marker.get("exclusion_added"):
        exclusion = "not added by the mirror"
    elif config is None:
        exclusion = "left as it is: .context/routes.json cannot be read"
    elif not present:
        exclusion = "already removed"
    elif edited:
        exclusion = "kept for the edited notes"
    else:
        exclusion = "removed"
    result = {"folder": name, "removed": delete, "edited_kept": edited,
              "exclusion": exclusion, "dry_run": dry_run}
    if dry_run:
        return result
    for rel in delete:
        (target / rel).unlink(missing_ok=True)
    if not edited:
        (target / NOTES_MARKER).unlink(missing_ok=True)
    else:
        kept = {rel: previous[rel] for rel in edited}
        _write_atomic(target / NOTES_MARKER, (json.dumps(
            dict(marker, files=kept), indent=2) + "\n").encode("utf-8"))
    for directory in (target / SUPERSEDED_DIR, target):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    if exclusion == "removed":
        def drop(config):
            prefixes = list(config.get("exclude_prefixes") or [])
            for index, prefix in enumerate(prefixes):
                if policy.relative_name(prefix) == name:
                    del prefixes[index]
                    break
            config["exclude_prefixes"] = prefixes
            return config
        _routes_update(vault, drop)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_source(value: str) -> dict:
    """`path` or `path@<64 hex>`; an @ that is not a digest stays in the path."""
    head, at, tail = str(value).rpartition("@")
    if at and _is_sha256(tail.strip().lower()):
        return {"path": head, "sha256": tail.strip().lower()}
    return {"path": value}


def _fail(command: str, message: str) -> int:
    print(f"context-layer memory {command}: {message}", file=sys.stderr)
    return 1


def _unexpected(args: argparse.Namespace, command: str) -> bool:
    extra = getattr(args, "rest", []) or []
    if extra:
        print(f"context-layer memory {command}: unrecognised arguments: {' '.join(extra)}",
              file=sys.stderr)
        return True
    return False


def _one_line(text, width: int = 100) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= width else flat[:width - 1] + "…"


def _summary(item: dict) -> str:
    return (f"{item.get('id', '(no id)')}  {str(item.get('kind', '?')):<8} "
            f"{str(item.get('state', '?')):<9} {item.get('ts', '')}  "
            f"tool {item.get('tool', '?')}")


def cmd_add(args: argparse.Namespace) -> int:
    if _unexpected(args, "add"):
        return 2
    try:
        stored = record(Path(args.vault).expanduser(), kind=args.kind, text=args.text,
                        sources=[_parse_source(value) for value in args.source],
                        state=args.state, tool=args.tool or "unknown", session=args.session,
                        supersedes=args.supersedes, allow_stale=args.allow_stale,
                        closes=args.closes, allow_fork=args.allow_fork)
    except MemoryStoreError as exc:
        return _fail("add", str(exc))
    if args.as_json:
        print(json.dumps(stored, ensure_ascii=False, indent=2))
        return 0
    verb = "duplicate, nothing appended:" if stored["duplicate"] else "recorded"
    print(f"{verb} {stored['id']}  {stored['kind']}/{stored['state']}  {stored['ts']}")
    if stored["duplicate"] and stored.get("state") != args.state:
        print(f"  the stored record is {stored.get('state')}; to record it as {args.state}, "
              f"repeat the command with --supersedes {stored['id']}")
    for source in stored.get("sources") or []:
        print(f"  source {source['path']}  {source['sha256'][:SHORT_HASH]}")
    if _targets(stored):
        print(f"  supersedes {', '.join(_targets(stored))}")
    if _closes(stored):
        print(f"  closes {', '.join(_closes(stored))}")
    print(f"  store  {_store_name(RECORDS_NAME)}  (mirror: {_store_name(MIRROR_NAME)})")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    if _unexpected(args, "list"):
        return 2
    if args.limit < 0:
        return _fail("list", "--limit must be a non-negative integer")
    try:
        vault = _vault(args.vault)
        records = load(vault)
    except MemoryStoreError as exc:
        return _fail("list", str(exc))
    selected = [r for r in reversed(records)
                if not args.kind or r.get("kind") == args.kind][:args.limit]
    _session_log(vault, "list", [{"event": "memory.read", "id": r.get("id")}
                                 for r in selected if _is_id(r.get("id"))])
    if args.as_json:
        print(json.dumps({"records": selected}, ensure_ascii=False, indent=2))
        return 0
    if not selected:
        print(f"No records in {_store_name(RECORDS_NAME)}.")
        return 0
    for item in selected:
        print(_summary(item))
        print(f"    {_one_line(item.get('text', ''))}")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    if _unexpected(args, "resume"):
        return 2
    try:
        packet = resume(Path(args.vault).expanduser(), limit=args.limit)
    except MemoryStoreError as exc:
        return _fail("resume", str(exc))
    if args.as_json:
        print(json.dumps(packet, ensure_ascii=False, indent=2))
        return 0
    stale_by_id: dict = {}
    for entry in packet["stale"]:
        stale_by_id.setdefault(entry["id"], []).append(entry)
    print(f"Continuation packet — vault {packet['vault']} — "
          f"generated {packet['generated_at']}")
    if not packet["records"]:
        print("No records yet. `context-layer memory add` writes the first one.")
        return 0
    print(f"\nRecords ({len(packet['records'])}, newest first):")
    for item in packet["records"]:
        print(f"  {_summary(item)}")
        print(f"      {_one_line(item.get('text', ''))}")
        for source in item.get("sources") or []:
            flag = ""
            for entry in stale_by_id.get(item.get("id"), []):
                if entry["path"] == source["path"]:
                    flag = ("  STALE: changed" if entry["current_sha256"]
                            else "  STALE: missing or unreadable")
                    if entry.get("moved_to"):
                        flag += f" (same bytes at {', '.join(entry['moved_to'])})"
            print(f"      source {source['path']}  {source['sha256'][:SHORT_HASH]}{flag}")
    if packet["open_tasks"]:
        print(f"\nOpen tasks ({len(packet['open_tasks'])}):")
        for item in packet["open_tasks"]:
            print(f"  {item.get('id')}  {_one_line(item.get('text', ''))}")
    if packet["closed_by_mention"]:
        print(f"\nClosed only by mention ({len(packet['closed_by_mention'])}): a format 1 "
              "result quotes the task id; check that it really completes the task.")
        for entry in packet["closed_by_mention"]:
            print(f"  task {entry['task']}  result {entry['result']}")
    if packet["conflicts"]:
        print(f"\nConflicts ({len(packet['conflicts'])}): records in force that replace the "
              "same record. Record one that supersedes all heads to resolve a conflict.")
        for conflict in packet["conflicts"]:
            print(f"  {conflict['target']}  heads {', '.join(conflict['heads'])}")
    if packet["stale"]:
        print(f"\nStale sources ({len(packet['stale'])}): re-read them before trusting "
              "the record above.")
        for entry in packet["stale"]:
            current = (entry["current_sha256"][:SHORT_HASH]
                       if entry["current_sha256"] else "missing")
            moved = (f"  moved to {', '.join(entry['moved_to'])}"
                     if entry.get("moved_to") else "")
            print(f"  {entry['path']}  recorded "
                  f"{str(entry['recorded_sha256'])[:SHORT_HASH]}  now {current}{moved}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    if _unexpected(args, "verify"):
        return 2
    vault = Path(args.vault).expanduser()
    try:
        if not records_path(vault).is_file():
            print(f"No memory store at {_store_name(RECORDS_NAME)}; nothing to verify.")
            return 0
        problems = verify(vault)
        total = len(load(vault)) if not problems else 0
    except MemoryStoreError as exc:
        return _fail("verify", str(exc))
    if problems:
        for problem in problems:
            print(f"context-layer memory verify: {problem}", file=sys.stderr)
        print(f"context-layer memory verify: {len(problems)} problem(s) in "
              f"{_store_name(RECORDS_NAME)}", file=sys.stderr)
        return 1
    print(f"{_store_name(RECORDS_NAME)}: {total} record(s), chain intact, no duplicate "
          "ids, no fork, every source still matches its recorded hash.")
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    if _unexpected(args, "repair"):
        return 2
    try:
        report = repair(Path(args.vault).expanduser(), dry_run=args.dry_run)
    except MemoryStoreError as exc:
        return _fail("repair", str(exc))
    if report["torn"]:
        verb = "would move" if report["dry_run"] else "moved"
        print(f"{verb} the torn last line ({report['bytes']} bytes) of {report['store']} "
              f"to {report['moved_to']}")
    else:
        print(f"{report['store']}: no torn last line; nothing to repair.")
    if report["problems"]:
        for problem in report["problems"]:
            print(f"context-layer memory repair: {problem}", file=sys.stderr)
        print("context-layer memory repair: `repair` moves only a torn last line; fix the "
              "line(s) above by hand, then run `context-layer memory verify`.", file=sys.stderr)
        return 1
    if report["torn"] and not report["dry_run"]:
        print(f"{_store_name(MIRROR_NAME)} rebuilt; run `context-layer memory verify` next.")
    return 0


def cmd_mirror(args: argparse.Namespace) -> int:
    if _unexpected(args, "mirror"):
        return 2
    vault = Path(args.vault).expanduser()
    if args.remove and not args.notes:
        return _fail("mirror", "--remove needs --notes FOLDER")
    try:
        if not args.notes:
            if args.dry_run:
                load(vault)
                print(f"would rebuild {_store_name(MIRROR_NAME)} from "
                      f"{_store_name(RECORDS_NAME)}")
            else:
                rebuild_mirror(vault)
                print(f"rebuilt {_store_name(MIRROR_NAME)} from {_store_name(RECORDS_NAME)}")
            return 0
        report = mirror_notes(vault, args.notes, remove=args.remove, dry_run=args.dry_run)
    except MemoryStoreError as exc:
        return _fail("mirror", str(exc))
    prefix = "would " if report["dry_run"] else ""
    if args.remove:
        print(f"{prefix}remove {len(report['removed'])} note(s) from {report['folder']}")
        if report["exclusion"] == "removed":
            print(f"{prefix}remove `{report['folder']}` from exclude_prefixes in "
                  ".context/routes.json")
        else:
            print(f"exclude_prefixes entry for `{report['folder']}`: {report['exclusion']}")
    else:
        print(f"{prefix}write {len(report['written'])} of {report['notes']} note(s) to "
              f"{report['folder']} ({report['in_force']} in force, {report['superseded']} "
              f"superseded); {len(report['removed'])} obsolete note(s) removed")
        if report["exclusion"] == "added":
            print(f"notice: {prefix}add `{report['folder']}` to exclude_prefixes in "
                  ".context/routes.json, so agent-written memory is never served as evidence; "
                  "remove that entry to make the notes searchable")
        elif report["exclusion"] == "removed by hand":
            print(f"notice: `{report['folder']}` is not excluded from retrieval (its "
                  "exclude_prefixes entry was removed by hand), so these notes can be served "
                  "as evidence")
    for rel in report["edited_kept"]:
        print(f"kept {report['folder']}/{rel}: edited since the mirror wrote it")
    return 0


def cmd_rebind(args: argparse.Namespace) -> int:
    if _unexpected(args, "rebind"):
        return 2
    moves = {old: new for old, new in (args.move or [])}
    try:
        report = rebind(Path(args.vault).expanduser(), args.id, moves=moves,
                        tool=args.tool or "unknown", session=args.session)
    except MemoryStoreError as exc:
        return _fail("rebind", str(exc))
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    stored = report["record"]
    print(f"recorded {stored['id']}, which supersedes {report['rebound']}")
    for change in report["changes"]:
        print(f"  source {change['from']} -> {change['to']}")
    return 0


def cmd_session_show(args: argparse.Namespace) -> int:
    if _unexpected(args, "session show"):
        return 2
    try:
        view = session_view(Path(args.vault).expanduser(), args.session_id)
    except MemoryStoreError as exc:
        return _fail("session show", str(exc))
    if args.as_json:
        print(json.dumps(view, ensure_ascii=False, indent=2))
        return 0
    print(f"Session {view['session']} — {view['file']} — {view['events']} event(s), "
          f"{view['first'] or '-'} .. {view['last'] or '-'}")
    if view["records"]:
        print(f"\nRecords ({len(view['records'])}):")
        for entry in view["records"]:
            if entry["in_force"] is None:
                state = "not in this store"
            elif entry["in_force"]:
                state = "in force"
            else:
                state = f"superseded by {', '.join(entry['superseded_by'])}"
            events = ", ".join(event.split(".")[1] for event in entry["events"])
            print(f"  {entry['id']}  {events}  {state}")
    if view["sources"]:
        print(f"\nSources ({len(view['sources'])}):")
        for entry in view["sources"]:
            when = "STALE when checked" if entry["stale_when_checked"] else "matched when checked"
            now = {"current": "current now", "changed": "STALE now: changed",
                   "missing": "STALE now: missing"}[entry["now"]]
            print(f"  {entry['path']}  {entry['sha256'][:SHORT_HASH]}  {when}; {now}")
    for name, count in sorted(view["other_events"].items()):
        print(f"  other event {name}: {count}")
    for problem in view["problems"]:
        print(f"context-layer memory session show: {problem}", file=sys.stderr)
    return 1 if view["problems"] else 0


def cmd_session_list(args: argparse.Namespace) -> int:
    if _unexpected(args, "session list"):
        return 2
    try:
        sessions = session_list(Path(args.vault).expanduser())
    except MemoryStoreError as exc:
        return _fail("session list", str(exc))
    if args.as_json:
        print(json.dumps({"sessions": sessions}, ensure_ascii=False, indent=2))
        return 0
    if not sessions:
        print(f"No session records in {'/'.join(SESSION_PARTS)} (they are written only while "
              f"{SESSION_ENV} is set).")
        return 0
    for entry in sessions:
        print(f"{entry['session']}  {entry['events']} event(s)  last {entry['last'] or '-'}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    """Attach `memory add|list|resume|verify|repair|mirror|rebind|session` to the CLI."""
    parser = sub.add_parser(
        "memory",
        help="Append-only shared memory: decisions, tasks, results and notes.",
        description="One JSONL file per vault (.context/memory/records.jsonl) plus a "
                    "derived MEMORY.md. Records are never rewritten: a change is a new "
                    "record that supersedes the old one. Sources carry the SHA-256 they "
                    "had when recorded, and `resume` flags the ones that changed since.",
    )
    inner = parser.add_subparsers(dest="memory_command", required=True)

    p_add = inner.add_parser("add", help="Append one record.", allow_abbrev=False)
    p_add.add_argument("vault")
    p_add.add_argument("--kind", required=True, choices=list(KINDS))
    p_add.add_argument("--text", required=True)
    p_add.add_argument("--source", action="append", default=[], metavar="PATH[@SHA256]",
                       help="Vault-relative source. With @<sha256> the hash is checked "
                            "against the file and a mismatch is refused.")
    p_add.add_argument("--state", choices=list(STATES), default="draft")
    p_add.add_argument("--tool", default=None,
                       help="Recording tool (default: $CONTEXT_LAYER_TOOL, else cli).")
    p_add.add_argument("--session", default=None,
                       help="Session id (default: $CONTEXT_LAYER_SESSION, else none).")
    p_add.add_argument("--supersedes", action="append", default=None, metavar="ID",
                       help="Id of the record this one replaces. Repeat it to merge a fork: "
                            "every record named must be in force.")
    p_add.add_argument("--closes", action="append", default=None, metavar="TASK_ID",
                       help="With --kind result: a task this result completes (repeatable).")
    p_add.add_argument("--allow-stale", action="store_true",
                       help="Record a given hash that no longer matches the file.")
    p_add.add_argument("--allow-fork", action="store_true",
                       help="Supersede a record that is already superseded (a deliberate fork).")
    p_add.add_argument("--json", action="store_true", dest="as_json")
    p_add.set_defaults(func=cmd_add, forward_to=None)

    p_list = inner.add_parser("list", help="Show stored records, newest first.",
                              allow_abbrev=False)
    p_list.add_argument("vault")
    p_list.add_argument("--kind", choices=list(KINDS), default=None)
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--json", action="store_true", dest="as_json")
    p_list.set_defaults(func=cmd_list, forward_to=None)

    p_resume = inner.add_parser(
        "resume", help="Continuation packet for the next session or tool.", allow_abbrev=False)
    p_resume.add_argument("vault")
    p_resume.add_argument("--limit", type=int, default=20)
    p_resume.add_argument("--json", action="store_true", dest="as_json")
    p_resume.set_defaults(func=cmd_resume, forward_to=None)

    p_verify = inner.add_parser(
        "verify", help="Check chain, ids, forks, JSON lines and source hashes.",
        allow_abbrev=False)
    p_verify.add_argument("vault")
    p_verify.set_defaults(func=cmd_verify, forward_to=None)

    p_repair = inner.add_parser(
        "repair", help="Move a torn last line aside so writes work again.", allow_abbrev=False,
        description="Moves only a final line that has no newline and is not a JSON object "
                    "(a crash during a write) to records.jsonl.torn-<utc>, then rebuilds "
                    "MEMORY.md. Any other unusable line is reported, never changed.")
    p_repair.add_argument("vault")
    p_repair.add_argument("--dry-run", action="store_true",
                          help="Report what would move; write nothing.")
    p_repair.set_defaults(func=cmd_repair, forward_to=None)

    p_mirror = inner.add_parser(
        "mirror", help="Rebuild MEMORY.md, or write decisions and tasks as visible notes.",
        allow_abbrev=False,
        description="Without --notes: rebuild .context/memory/MEMORY.md (for example after a "
                    "hand edit of records.jsonl). With --notes FOLDER: write one note per "
                    "decision and task (superseded ones under FOLDER/superseded/), a snapshot "
                    "Obsidian shows as ordinary notes. The first run adds FOLDER to "
                    "exclude_prefixes in .context/routes.json, so agent-written memory is "
                    "never served as evidence. --remove deletes what the mirror wrote.")
    p_mirror.add_argument("vault")
    p_mirror.add_argument("--notes", default=None, metavar="FOLDER",
                          help="Vault-relative folder for the visible notes, e.g. Memory.")
    p_mirror.add_argument("--remove", action="store_true",
                          help="With --notes: remove the notes and the exclusion it added.")
    p_mirror.add_argument("--dry-run", action="store_true",
                          help="Report what would be written or removed; write nothing.")
    p_mirror.set_defaults(func=cmd_mirror, forward_to=None)

    p_rebind = inner.add_parser(
        "rebind", help="Record a moved source's new path (a superseding record).",
        allow_abbrev=False,
        description="Appends a record that supersedes ID with the same content and the new "
                    "path of a source whose recorded bytes now sit elsewhere. A missing "
                    "source with one copy in the index manifest is rebound automatically; "
                    "--move OLD NEW names the copy. No line is ever edited.")
    p_rebind.add_argument("vault")
    p_rebind.add_argument("id")
    p_rebind.add_argument("--move", nargs=2, action="append", metavar=("OLD", "NEW"),
                          help="Rebind source OLD to NEW (NEW must hold the recorded bytes).")
    p_rebind.add_argument("--tool", default=None)
    p_rebind.add_argument("--session", default=None)
    p_rebind.add_argument("--json", action="store_true", dest="as_json")
    p_rebind.set_defaults(func=cmd_rebind, forward_to=None)

    p_session = inner.add_parser(
        "session", help="Show what a session read and wrote (opt-in session records).",
        allow_abbrev=False,
        description="Session records exist only when CONTEXT_LAYER_SESSION was set: "
                    ".context/sessions/<id>.jsonl holds record ids and source paths with "
                    "hashes (session-bom/v1), never text.")
    session_inner = p_session.add_subparsers(dest="session_command", required=True)
    p_show = session_inner.add_parser("show", help="Render one session with stale flags.",
                                      allow_abbrev=False)
    p_show.add_argument("vault")
    p_show.add_argument("session_id", metavar="ID")
    p_show.add_argument("--json", action="store_true", dest="as_json")
    p_show.set_defaults(func=cmd_session_show, forward_to=None)
    p_slist = session_inner.add_parser("list", help="List the session records of a vault.",
                                       allow_abbrev=False)
    p_slist.add_argument("vault")
    p_slist.add_argument("--json", action="store_true", dest="as_json")
    p_slist.set_defaults(func=cmd_session_list, forward_to=None)
