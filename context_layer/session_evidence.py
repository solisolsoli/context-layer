"""context_layer.session_evidence - what context-layer delivered in one host session.

The ledger (schema `session-evidence/v1`)
-----------------------------------------
`<vault>/.context/session-evidence/<session>.jsonl` holds one JSON object per
evidence item that context-layer delivered to the host in that session:

    {"path": "notes/release.md", "sha256": "<64 hex>", "packet_id": "<id or null>",
     "at": "2026-01-15T14:30:00Z"}

Ids, vault-relative paths and hashes only: never note text, never a prompt.
`path` is a vault-relative POSIX path that passed the vault's exclusions;
`sha256` is the delivered bytes' full SHA-256; `packet_id` names the delivery it
came in (the prompt hook's per-packet nonce, a shared packet's 64-hex id, or
null); `at` is the UTC time of the append. Readers ignore unknown keys and skip
lines they cannot use. The file for one session stops growing at
`MAX_LEDGER_BYTES` (one `{"overflow": true}` line marks that), and only the
`MAX_LEDGER_FILES` most recently written session files are kept.

The prompt hook and the MCP server append to it through `record_delivery`, the one
writer (the MCP server's wrapper only checks the opt-in and words the refusal). When they
pass a `channel`, each line also carries `schema`, `session`, `channel` and, for a passage
with a line span, `lines`. The session id comes from the hook's JSON input, or from `CLAUDE_CODE_SESSION_ID`,
which Claude Code sets for hook and stdio MCP server subprocesses
(code.claude.com/docs/en/env-vars).

The citation check
------------------
`check()` compares the citations in the host's final answer with the ledger:
vault paths the answer names, and SHA-256 prefixes (8 to 64 hex) that it
presents as hashes. It reports only what was cited but never delivered by
context-layer in the session. It never blocks, never judges whether the answer
is right, and never calls a model. The Stop hook (`context-layer rules hook
stop --check-citations`) turns the result into one non-blocking line.

The answer comes from the Stop input's `last_assistant_message`, which the
hooks reference documents as the text of Claude's final response. Without it,
the transcript JSONL at `transcript_path` is read as a fallback; Claude Code
documents that format as internal and subject to change
(code.claude.com/docs/en/sessions), so that reader is best-effort.

Python 3.10+; standard library only; no network access.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
from .platform_support import file_lock, set_private_permissions

SCHEMA = "session-evidence/v1"
LEDGER_PARTS = (".context", "session-evidence")
MAX_LEDGER_BYTES = 4 * 1024 * 1024      # per session file; then one overflow line
MAX_LEDGER_FILES = 50                   # session files kept, newest first
MAX_PATH_CHARS = 1024
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024  # tail of the transcript that is read
MAX_LISTED = 8                          # citations named in the Stop line
HEX64 = re.compile(r"^[0-9a-f]{64}$")
PACKET_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
STEM = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,99}$")
# A hex run of 8-64 characters, not part of a longer word.
HEX_TOKEN = re.compile(r"(?<![0-9A-Za-z])([0-9a-fA-F]{8,64})(?![0-9A-Za-z])")
# Text right before a hex run that presents it as a hash: `sha256=`, `sha256: `,
# `(sha256 `, `"source_sha256": "`, `hash `, `digest: `.
HASH_CUE = re.compile(r"(?:sha-?256|sha|hash|digest)(?:[\s:=`'\"(\[]{0,4}(?:prefix|is))?"
                      r"[\s:=`'\"(\[]{0,4}$", re.IGNORECASE)
# Characters allowed between a cited path and the hash that follows it:
# "`notes/a.md` (3f2a9c1b)", "notes/a.md@3f2a9c1b", "notes/a.md, 3f2a9c1b".
PATH_GAP = " \t`'\"()[]@:=,;"


class LedgerError(ValueError):
    """A refusal a person is meant to read."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_stem(session_id: str) -> str:
    """A file-name stem for a host session id; never a path, never empty.

    Plain ids (letters, digits, `.`, `_`, `-`; Claude Code uses UUIDs) are kept as
    they are; anything else becomes `sid-` plus 24 hex of its SHA-256.
    """
    value = str(session_id)
    if STEM.match(value) and value not in (".", ".."):
        return value
    return "sid-" + hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:24]


def session_from_env() -> str | None:
    """The host session id Claude Code gives hook and stdio MCP subprocesses, if any."""
    value = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    return value or None


def ledger_path(vault, session_id: str) -> Path:
    return Path(vault).joinpath(*LEDGER_PARTS) / f"{safe_stem(session_id)}.jsonl"


def _router_policy():
    """router/source_policy: the one strict reader of exclusions (wheel or checkout)."""
    for name in (f"{__package__}.router.source_policy", "router.source_policy"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    here = Path(__file__).resolve().parent
    for parent in here.parents:
        folder = parent / "router"
        if (folder / "source_policy.py").is_file():
            if str(folder) not in sys.path:
                sys.path.insert(0, str(folder))
            return importlib.import_module("source_policy")
    raise LedgerError("router/source_policy.py was not found next to the package")


def _exclusions(vault: Path):
    """(policy, prefixes); raises ValueError when routes.json cannot be trusted."""
    policy = _router_policy()
    return policy, policy.load_exclusions(vault)


def _allowed(policy, prefixes, name: str) -> bool:
    try:
        return not policy.excluded(name, prefixes)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _item_lines(item) -> list | None:
    """[line_start, line_end] of a passage item, when it has a usable span."""
    if not isinstance(item, dict):
        return None
    start, end = item.get("line_start"), item.get("line_end")
    if all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
           for value in (start, end)):
        return [start, end]
    return None


def _item_fields(item) -> tuple[str | None, str | None]:
    if isinstance(item, dict):
        path = item.get("source_path", item.get("path"))
        digest = item.get("source_sha256", item.get("sha256"))
    elif isinstance(item, (tuple, list)) and len(item) == 2:
        path, digest = item
    else:
        return None, None
    path = path if isinstance(path, str) else None
    digest = digest.strip().lower() if isinstance(digest, str) else None
    return path, digest


def _prune(directory: Path, keep_path: Path) -> None:
    try:
        files = [p for p in directory.glob("*.jsonl") if p.is_file()]
    except OSError:
        return
    if len(files) <= MAX_LEDGER_FILES:
        return

    def mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    files.sort(key=lambda p: (mtime(p), p.name))
    for path in files[:len(files) - MAX_LEDGER_FILES]:
        if path != keep_path:
            path.unlink(missing_ok=True)


def record_delivery(vault, session_id: str | None, items, packet_id: str | None = None,
                    now: datetime | None = None, channel: str | None = None,
                    require_folder: bool = False) -> dict:
    """Append one ledger line per delivered item; return what was written.

    `items`: evidence-delivery-v1 items (`source_path`, `source_sha256`), or dicts
    with `path`/`sha256`, or `(path, sha256)` pairs. An item whose path is not a
    vault-relative POSIX path, falls under the vault's exclusions, or whose hash
    is not 64 hex characters is skipped. When routes.json cannot be read nothing
    is written: the exclusions that would have been checked are unknown.
    `channel` ("hook", "mcp") adds `schema`, `session`, `channel` and `lines` to each line.
    `require_folder` is the opt-in the hook and the server use: nothing is written unless
    `.context/session-evidence/` already exists (and is not a symlink); a ledger file that is
    a symlink is never followed.
    Returns {"written", "skipped", "ledger" (vault-relative) or None, "reason"}.
    """
    root = Path(vault).expanduser().resolve()
    items = list(items or [])
    session = session_id if isinstance(session_id, str) and session_id.strip() else None
    session = session or session_from_env()
    if session is None:
        return {"written": 0, "skipped": len(items), "ledger": None,
                "reason": "no session id (hook input or CLAUDE_CODE_SESSION_ID)"}
    folder = root.joinpath(*LEDGER_PARTS)
    if require_folder and (folder.is_symlink() or not folder.is_dir()):
        return {"written": 0, "skipped": len(items), "ledger": None,
                "reason": "no .context/session-evidence/ directory (the ledger is opt-in)"}
    if packet_id is not None and (not isinstance(packet_id, str)
                                  or not PACKET_ID.match(packet_id)):
        packet_id = None
    try:
        policy, prefixes = _exclusions(root)
    except ValueError as exc:
        return {"written": 0, "skipped": len(items), "ledger": None,
                "reason": f"exclusions unknown ({exc})"}
    stamp = _now_iso(now)
    lines = []
    skipped = 0
    for item in items:
        path, digest = _item_fields(item)
        if (path is None or digest is None or not HEX64.match(digest)
                or len(path) > MAX_PATH_CHARS or not _allowed(policy, prefixes, path)):
            skipped += 1
            continue
        name = policy.relative_name(path)
        record = {"path": name, "sha256": digest, "packet_id": packet_id, "at": stamp}
        if channel is not None:
            record = {"schema": SCHEMA, "session": session, "channel": channel, **record}
            span = _item_lines(item)
            if span is not None:
                record["lines"] = span
        lines.append(json.dumps(record, ensure_ascii=True, separators=(",", ":")))
    target = ledger_path(root, session)
    relative = PurePosixPath(*LEDGER_PARTS, target.name).as_posix()
    if not lines:
        return {"written": 0, "skipped": skipped, "ledger": relative, "reason": None}
    target.parent.mkdir(parents=True, exist_ok=True)
    created = not target.exists()
    data = ("\n".join(lines) + "\n").encode("ascii")
    written = len(lines)
    try:
        with file_lock(target.parent / ".session-evidence.lock"):
            fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                set_private_permissions(fd, target)
                size = os.fstat(fd).st_size
                if size + len(data) > MAX_LEDGER_BYTES:
                    written, skipped = 0, skipped + len(lines)
                    if not _ends_with_overflow(target):
                        os.write(fd, (json.dumps({"overflow": True, "at": stamp},
                                                 separators=(",", ":")) + "\n").encode("ascii"))
                else:
                    os.write(fd, data)
            finally:
                os.close(fd)
            if created:
                _prune(target.parent, target)
    except OSError as exc:
        return {"written": 0, "skipped": skipped + len(lines), "ledger": relative,
                "reason": f"the ledger file cannot be opened ({exc.strerror or exc}); "
                          "a symlink is never followed"}
    return {"written": written, "skipped": skipped, "ledger": relative,
            "reason": None if written else "ledger full for this session"}


def _ends_with_overflow(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            handle.seek(max(0, path.stat().st_size - 256))
            tail = handle.read().splitlines()
    except OSError:
        return False
    return bool(tail) and b'"overflow":true' in tail[-1]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def delivered(vault, session_id: str) -> dict:
    """Everything the ledger says was delivered in one session.

    Returns {"present", "complete", "items", "paths", "sha256", "packet_ids"};
    `complete` is False once the ledger overflowed (or was unreadable), so a
    missing citation cannot be told from a dropped line.
    """
    path = ledger_path(Path(vault).expanduser().resolve(), session_id)
    out = {"present": False, "complete": True, "items": 0, "paths": set(), "sha256": set(),
           "packet_ids": set()}
    if not path.is_file():
        return out
    out["present"] = True
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_LEDGER_BYTES + 4096)
    except OSError:
        out["complete"] = False
        return out
    if len(raw) > MAX_LEDGER_BYTES + 1024:
        out["complete"] = False
    for line in raw.splitlines():
        try:
            entry = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("overflow") is True:
            out["complete"] = False
            continue
        name, digest = entry.get("path"), entry.get("sha256")
        if not isinstance(name, str) or not isinstance(digest, str) \
                or not HEX64.match(digest.lower()):
            continue
        out["items"] += 1
        out["paths"].add(name)
        out["sha256"].add(digest.lower())
        packet = entry.get("packet_id")
        if isinstance(packet, str) and PACKET_ID.match(packet):
            out["packet_ids"].add(packet.lower())
    return out


def _entry_text(entry: dict) -> str | None:
    """Text of one transcript entry if it is an assistant message, else None."""
    message = entry.get("message") if isinstance(entry.get("message"), dict) else None
    role = (message or {}).get("role")
    if entry.get("type") != "assistant" and role != "assistant":
        return None
    content = (message or entry).get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = [block.get("text") for block in content
             if isinstance(block, dict) and block.get("type") == "text"
             and isinstance(block.get("text"), str)]
    return "\n".join(parts) if parts else ""


def last_assistant_text(transcript_path, max_bytes: int = MAX_TRANSCRIPT_BYTES) -> str | None:
    """The text of the last assistant message in a Claude Code transcript (JSONL).

    Best-effort: Claude Code documents the transcript entry format as internal.
    Entries are read from the end of the file (at most `max_bytes`). An entry
    counts as assistant text when `type` or `message.role` is "assistant"; its
    text is the `text` of its `message.content` blocks. Consecutive assistant
    entries that share one `message.id` are joined, since one reply may be
    written as several entries. Returns None when no assistant text is found.
    """
    path = Path(str(transcript_path)).expanduser()
    try:
        if not path.is_file():
            return None
        size = path.stat().st_size
        with open(path, "rb") as handle:
            handle.seek(max(0, size - max_bytes))
            raw = handle.read(max_bytes)
    except OSError:
        return None
    lines = raw.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]                     # the first line may be cut in half
    unset = object()
    target = unset
    collected: list[str] = []
    for line in reversed(lines):
        try:
            entry = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        text = _entry_text(entry)
        if text is None:                      # a user turn, a tool result or metadata
            if target is not unset:
                break                         # the final message ended here
            continue
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        this_id = message.get("id") if isinstance(message.get("id"), str) else None
        if target is unset:
            target = this_id
        elif this_id is None or this_id != target:
            break                             # an earlier message, not the final one
        if text.strip():
            collected.append(text)
        if this_id is None:
            break                             # without an id one entry is all that is known
    if not collected:
        return None
    return "\n".join(reversed(collected)).strip() or None


def final_message(payload: dict) -> tuple[str | None, str]:
    """(answer text, where it came from) for a Stop hook input."""
    text = payload.get("last_assistant_message")
    if isinstance(text, str) and text.strip():
        return text, "last_assistant_message"
    transcript = payload.get("transcript_path")
    if isinstance(transcript, str) and transcript.strip():
        text = last_assistant_text(transcript)
        if text:
            return text, "transcript_path"
    return None, "none"


# ---------------------------------------------------------------------------
# Citations
# ---------------------------------------------------------------------------

def _starts_path(text: str, start: int) -> bool:
    """True when a path may begin at `start`: not inside a longer name or folder path.

    `./notes/a.md` still counts as `notes/a.md`; `my-notes/a.md` and
    `/elsewhere/notes/a.md` do not, so a root `a.md` is never read out of them.
    """
    def free(index: int) -> bool:
        char = text[index] if index >= 0 else ""
        return not (char.isalnum() or char in "_-./\\")
    if free(start - 1):
        return True
    return text[max(0, start - 2):start] == "./" and free(start - 3)


def _path_spans(text: str, paths) -> list[tuple[int, int, str]]:
    """(start, end, path) for each place the text names a vault path as a whole path."""
    spans = []
    for name in paths:
        start = text.find(name)
        while start != -1:
            end = start + len(name)
            after = text[end] if end < len(text) else ""
            if _starts_path(text, start) and not (after.isalnum() or after == "_"):
                spans.append((start, end, name))
            start = text.find(name, start + 1)
    # A root `a.md` inside `notes/a.md` belongs to the longer path, not a second citation.
    spans.sort(key=lambda span: (span[0], -(span[1] - span[0])))
    kept: list[tuple[int, int, str]] = []
    reach = -1
    for span in spans:
        if kept and span[1] <= reach and span[0] >= kept[-1][0]:
            continue                        # inside a longer span already kept
        kept.append(span)
        reach = max(reach, span[1])
    return kept


def extract_citations(text: str, vault_paths) -> dict:
    """Vault paths and hash prefixes that `text` cites.

    A path counts when a vault path appears in the text as a whole path (not
    inside a longer name). A hex run of 8-64 characters counts as a hash when it
    is 64 characters long, when a hash word precedes it on the same line
    (`sha256`, `sha`, `hash`, `digest`), or when it directly follows a cited path
    (`notes/a.md (3f2a9c1b)`, `notes/a.md@3f2a9c1b`). Other hex runs, such as a
    commit id or a UUID part, are not treated as citations.
    """
    text = text or ""
    spans = _path_spans(text, sorted(set(vault_paths or ()), key=len, reverse=True))
    paths = sorted({name for _, _, name in spans})
    ends = {end for _, end, _ in spans}
    hashes = []
    for match in HEX_TOKEN.finditer(text):
        token = match.group(1).lower()
        start = match.start(1)
        line_start = text.rfind("\n", 0, start) + 1
        before = text[line_start:start]
        cited = len(token) == 64 or bool(HASH_CUE.search(before[-16:]))
        if not cited:
            trimmed = start
            while trimmed > line_start and start - trimmed < 6 and text[trimmed - 1] in PATH_GAP:
                trimmed -= 1
            cited = trimmed in ends
        if cited and token not in hashes:
            hashes.append(token)
    return {"paths": paths, "hashes": hashes}


def check(vault, session_id: str, text: str | None, vault_paths,
          known_hashes=(), written_paths=()) -> dict:
    """Citations in `text` that the session ledger does not show as delivered.

    `vault_paths`: in-scope vault paths (the caller's walk, exclusions applied).
    `known_hashes`: hashes of bytes the session produced itself (files the agent
    wrote, the rule files), which are not citations of evidence.
    `written_paths`: files the agent wrote in this session; naming them is a
    report of work, not a citation.
    """
    ledger = delivered(vault, session_id)
    written = set(written_paths or ())
    citations = extract_citations(text or "", [p for p in vault_paths if p not in written])
    known = {h.lower() for h in known_hashes if isinstance(h, str)} | ledger["sha256"]
    undelivered_paths = [p for p in citations["paths"] if p not in ledger["paths"]]
    undelivered_hashes = [h for h in citations["hashes"]
                          if not any(full.startswith(h) for full in known)
                          and not any(pid.startswith(h) for pid in ledger["packet_ids"])]
    return {"schema": SCHEMA, "ledger_present": ledger["present"],
            "ledger_complete": ledger["complete"], "delivered_items": ledger["items"],
            "cited_paths": citations["paths"], "cited_hashes": citations["hashes"],
            "undelivered_paths": undelivered_paths, "undelivered_hashes": undelivered_hashes}


def message_line(result: dict) -> str | None:
    """The one non-blocking line for the Stop hook, or None when nothing to say."""
    if not result["ledger_complete"]:
        return None                     # a dropped ledger line cannot be told from a gap
    named = [f"`{p}`" for p in result["undelivered_paths"]] \
        + [f"hash {h}" for h in result["undelivered_hashes"]]
    if not named:
        return None
    shown = ", ".join(named[:MAX_LISTED])
    if len(named) > MAX_LISTED:
        shown += f" and {len(named) - MAX_LISTED} more"
    line = ("context-layer: cited in the last answer but not delivered by context-layer in "
            f"this session: {shown}. Check them at the source before relying on them.")
    if not result["ledger_present"]:
        line += " (No delivery was recorded for this session.)"
    return line
