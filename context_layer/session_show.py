"""context_layer.session_show - one agent session's bill of materials, joined read-only.

`context-layer session show VAULT [SESSION_ID]` reads files that other components
already wrote and joins them on the session id. It writes nothing, calls no
model and opens no network connection. Every row names the file and the line (or
the record or task id) it came from; a source that is missing, torn or larger than
the read bound is reported as such and never filled in.

Sources (formats are owned by the modules named here; nothing is invented):

  evidence ledger  `.context/session-evidence/<id>.jsonl`, `session-evidence/v1`
                   (session_evidence.py): path, sha256, packet_id, at.
  memory records   `.context/memory/records.jsonl` (memory.py): records whose
                   `session` field is the id; never their text.
  session record  `.context/sessions/<id>.jsonl`, `session-bom/v1` (memory.py,
                   written only while CONTEXT_LAYER_SESSION is set).
  task ledger      `.context/tasks/LEDGER.jsonl`, `context-layer-ledger/v1`
                   (orchestrate.py): lines whose `session` field is the id, i.e.
                   the verdicts and memory records made while CONTEXT_LAYER_SESSION
                   was set; joined to `.context/tasks/<task>/task.json` and
                   `result.json`, with the ledger attestation status that
                   `tasks list` computes.
  packets          `.context/packets/<64 hex>.json` (orchestrate.py): whether a
                   delivered packet id still names a packet file, and which tasks
                   were dispatched from it (`task.json` `shared_packet_id`).
  historical advisor log `.context/jev-calls.jsonl`: counters only, and the log
                   has no session field, so rows are matched by time window and
                   labelled that way. This reader does not make advisor calls.

The two id spaces differ by design: the evidence ledger's session id comes from
the host (CLAUDE_CODE_SESSION_ID or the hook input), the others from
CONTEXT_LAYER_SESSION or `--session`. They join only when the same id was used.

Python 3.10+; standard library only.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from . import memory, orchestrate, session_evidence, tasks

SCHEMA = "session-show/v1"
LIST_SCHEMA = "session-list/v1"
DEFAULT_LIMIT = 50                      # rows per section in the output
DEFAULT_MAX_BYTES = 8 * 1024 * 1024     # read bound per source file
MAX_LINE_BYTES = 1024 * 1024            # a longer line is reported, not parsed
MAX_TASK_DIRS = 2000                    # task folders looked at for the packet join
MAX_TASK_FILE = 1024 * 1024
SHORT = 12                              # hash characters in text output
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

EVIDENCE_DIR = ".context/session-evidence"
SESSIONS_DIR = ".context/sessions"
MEMORY_FILE = ".context/memory/records.jsonl"
LEDGER_FILE = orchestrate.LEDGER_NAME
TASKS_DIR = tasks.TASKS_DIR
PACKETS_DIR = orchestrate.PACKETS_DIR
JEV_LOG = ".context/jev-calls.jsonl"


class ShowError(ValueError):
    """A refusal a person is meant to read."""


# ---------------------------------------------------------------------------
# Bounded, tolerant line reading
# ---------------------------------------------------------------------------

def _source(name: str, rel: str, path: Path) -> dict:
    """The status record of one source file; `status` starts as missing or ok."""
    info = {"name": name, "file": rel, "status": "missing", "size": None, "scanned": 0,
            "lines": 0, "problems": []}
    try:
        if path.is_symlink() or not path.is_file():
            if path.is_symlink():
                info.update(status="unreadable", problems=["is a symbolic link; not read"])
            return info
        info["size"] = path.stat().st_size
        info["status"] = "ok"
    except OSError as exc:
        info.update(status="unreadable", problems=[f"cannot be examined: {exc.strerror or exc}"])
    return info


def _lines(path: Path, info: dict, max_bytes: int):
    """Yield (line number, bytes) for each non-blank line, reading at most `max_bytes`.

    The package's line model: a line ends at b"\\n" and one trailing b"\\r" is
    dropped. Adds to `info`: `lines` (non-blank, seen), `scanned`, a `truncated`
    problem past the bound, an over-long line, a last line without a newline.
    """
    if info["status"] != "ok":
        return
    try:
        handle = open(path, "rb")
    except OSError as exc:
        info["status"] = "unreadable"
        info["problems"].append(f"cannot be read: {exc.strerror or exc}")
        return
    number = 0
    with handle:
        while True:
            budget = max_bytes - info["scanned"]
            try:
                if budget <= 0:
                    if handle.peek(1)[:1]:
                        info["status"] = "partial"
                        info["problems"].append(
                            f"only the first {max_bytes} of {info['size']} bytes were read "
                            f"(through line {number}); raise --max-bytes")
                    return
                raw = handle.readline(min(MAX_LINE_BYTES + 1, budget))
                if not raw:
                    return
                number += 1
                info["scanned"] += len(raw)
                complete = raw.endswith(b"\n")
                if not complete and handle.peek(1)[:1]:
                    if info["scanned"] >= max_bytes:
                        info["status"] = "partial"
                        info["problems"].append(
                            f"only the first {max_bytes} of {info['size']} bytes were read "
                            f"(line {number} is cut); raise --max-bytes")
                        return
                    # Too long: skip to the end of the line without holding it.
                    info["problems"].append(f"line {number}: longer than {MAX_LINE_BYTES} "
                                            "bytes; not parsed")
                    while True:
                        rest = handle.readline(MAX_LINE_BYTES)
                        info["scanned"] += len(rest)
                        if not rest or rest.endswith(b"\n"):
                            break
                    continue
            except OSError as exc:
                info["status"] = "unreadable"
                info["problems"].append(f"read failed after line {number}: "
                                        f"{exc.strerror or exc}")
                return
            body = raw[:-1] if complete else raw
            if body.endswith(b"\r"):
                body = body[:-1]
            if not body.strip():
                continue
            info["lines"] += 1
            yield number, body, complete


def _json_line(body: bytes):
    """The parsed object, or None when the line is not a JSON object."""
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _bad(info: dict, number: int, complete: bool) -> None:
    info["problems"].append(
        f"line {number}: not a JSON object"
        + ("" if complete else " (the last line has no newline: a torn write?)"))
    if info["status"] == "ok":
        info["status"] = "damaged" if complete else "torn"


def _epoch(stamp) -> float | None:
    if not isinstance(stamp, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(stamp, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _safe(value) -> str:
    """Text for a terminal: control characters replaced."""
    return "".join(ch if ch.isprintable() else "?" for ch in str(value))


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# One reader per source
# ---------------------------------------------------------------------------

def _read_evidence(root: Path, session: str, max_bytes: int) -> tuple[dict, list]:
    path = session_evidence.ledger_path(root, session)
    rel = f"{EVIDENCE_DIR}/{path.name}"
    info = _source("evidence_ledger", rel, path)
    rows: list = []
    overflow = False
    for number, body, complete in _lines(path, info, max_bytes):
        entry = _json_line(body)
        if entry is None:
            _bad(info, number, complete)
            continue
        if entry.get("overflow") is True:
            overflow = True
            continue
        name, digest = entry.get("path"), entry.get("sha256")
        if not isinstance(name, str) or not isinstance(digest, str) \
                or not HEX64.match(digest.lower()):
            info["problems"].append(f"line {number}: no usable path and sha256; skipped")
            continue
        packet = entry.get("packet_id")
        rows.append({"cite": f"{rel}:{number}", "path": name, "sha256": digest.lower(),
                     "packet_id": packet if isinstance(packet, str) else None,
                     "at": entry.get("at") if isinstance(entry.get("at"), str) else None})
    if overflow:
        info["overflow"] = True
        info["problems"].append("the ledger overflowed (it stopped growing at "
                                f"{session_evidence.MAX_LEDGER_BYTES} bytes): later deliveries "
                                "are not listed")
    return info, rows


def _read_bom(root: Path, session: str, max_bytes: int) -> tuple[dict, list]:
    path = memory.session_path(root, session)
    rel = f"{SESSIONS_DIR}/{path.name}"
    info = _source("session_record", rel, path)
    events: list = []
    for number, body, complete in _lines(path, info, max_bytes):
        row = _json_line(body)
        if row is None:
            _bad(info, number, complete)
            continue
        if row.get("schema") != memory.BOM_SCHEMA:
            info["problems"].append(f"line {number}: not a {memory.BOM_SCHEMA} line; skipped")
            continue
        event = {"cite": f"{rel}:{number}", "ts": row.get("ts") if isinstance(row.get("ts"), str)
                 else None, "op": row.get("op"), "event": row.get("event")}
        if isinstance(row.get("id"), str):
            event["id"] = row["id"]
        if isinstance(row.get("path"), str):
            event["path"] = row["path"]
            event["sha256"] = row.get("sha256")
            event["stale_when_checked"] = row.get("current_sha256") != row.get("sha256")
        events.append(event)
    return info, events


def _read_memory(root: Path, session: str, max_bytes: int) -> tuple[dict, list]:
    path = memory.records_path(root)
    info = _source("memory_records", MEMORY_FILE, path)
    mine: list = []
    superseded: dict = {}
    closed: dict = {}
    for number, body, complete in _lines(path, info, max_bytes):
        record = _json_line(body)
        if record is None:
            _bad(info, number, complete)
            continue
        identifier = record.get("id")
        if not isinstance(identifier, str):
            continue
        targets = record.get("supersedes")
        for target in ([targets] if isinstance(targets, str) else
                       targets if isinstance(targets, list) else []):
            if isinstance(target, str):
                superseded.setdefault(target, []).append(identifier)
        closes = record.get("closes")
        for target in closes if isinstance(closes, list) else []:
            if isinstance(target, str):
                closed.setdefault(target, []).append(identifier)
        if record.get("session") == session:
            mine.append((number, record))
    sound = info["status"] == "ok"        # only a whole, clean read can say "in force"
    rows = []
    for number, record in mine:
        sources = [{"path": s["path"], "sha256": s.get("sha256")}
                   for s in record.get("sources") or []
                   if isinstance(s, dict) and isinstance(s.get("path"), str)]
        rows.append({
            "cite": f"{MEMORY_FILE}:{number}", "id": record["id"],
            "kind": record.get("kind"), "state": record.get("state"),
            "ts": record.get("ts") if isinstance(record.get("ts"), str) else None,
            "tool": record.get("tool"), "sources": sources,
            "supersedes": record.get("supersedes"),
            "closes": record.get("closes") if isinstance(record.get("closes"), list) else [],
            "superseded_by": superseded.get(record["id"], []) if sound else None,
            "closed_by": closed.get(record["id"], []) if sound else None,
            "in_force": (not superseded.get(record["id"])) if sound else None})
    return info, rows


def _read_task_ledger(root: Path, session: str, max_bytes: int) -> tuple[dict, list]:
    path = orchestrate.ledger_path(root)
    info = _source("task_ledger", LEDGER_FILE, path)
    rows: list = []
    for number, body, complete in _lines(path, info, max_bytes):
        record = _json_line(body)
        if record is None:
            _bad(info, number, complete)
            continue
        if record.get("session") != session:
            continue
        produced = record.get("produced")
        rows.append({
            "cite": f"{LEDGER_FILE}:{number}", "n": record.get("n"),
            "line_sha256": _sha_bytes(body), "event": record.get("event"),
            "task_id": record.get("task_id") if isinstance(record.get("task_id"), str) else None,
            "verdict": record.get("verdict"), "memory_id": record.get("memory_id"),
            "packet_id": record.get("packet_id"), "ts": record.get("ts"),
            "produced": len(produced) if isinstance(produced, list) else None})
    return info, rows


def _small_json(path: Path):
    """(object or None, problem or None) for a small regular file inside the vault."""
    try:
        if path.is_symlink() or not path.is_file():
            return None, "missing"
        if path.stat().st_size > MAX_TASK_FILE:
            return None, f"larger than {MAX_TASK_FILE} bytes; not read"
        value = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, f"unreadable ({type(exc).__name__})"
    return (value, None) if isinstance(value, dict) else (None, "not a JSON object")


def _attest(root: Path, ledger_info: dict, max_bytes: int):
    """(latest verify lines, chain problems, unchecked reason or None), as `tasks list` does."""
    size = ledger_info.get("size")
    if ledger_info["status"] == "missing":
        return {}, [], None
    if ledger_info["status"] == "unreadable":
        return {}, [], "the task ledger cannot be read"
    if size is not None and size > max_bytes:
        return {}, [], f"the task ledger is larger than --max-bytes ({size} bytes); chain not checked"
    try:
        latest, problems = tasks._attestations(root)
    except (OSError, ValueError, orchestrate.OrchestrateError) as exc:
        return {}, [], f"the task ledger could not be checked ({type(exc).__name__})"
    return latest, problems, None


def _task_view(root: Path, task_id: str, joined_by: str, ledger_rows: list,
               latest, chain_problems, unchecked) -> dict:
    """One task: its files, its ledger lines for this session and its attestation."""
    rel = f"{TASKS_DIR}/{task_id}"
    view = {"task_id": task_id, "joined_by": joined_by, "files": rel, "problems": [],
            "ledger_lines": [r for r in ledger_rows if r["task_id"] == task_id]}
    if not TASK_ID.match(task_id):
        view.update(state=None, attestation="unchecked",
                    problems=["the ledger names a task id that is not a plain folder name; "
                              "not opened"])
        return view
    directory = root / TASKS_DIR / task_id
    task, why = _small_json(directory / "task.json")
    result, why_result = _small_json(directory / "result.json")
    if task is None:
        view["problems"].append(f"{rel}/task.json: {why}")
    if result is None:
        view["problems"].append(f"{rel}/result.json: {why_result}")
    job = (task or {}).get("job")
    view.update(
        created_at=(task or {}).get("created_at"), backend=(task or {}).get("backend"),
        shared_packet_id=(task or {}).get("shared_packet_id"),
        job={"path": job.get("path"), "sha256": job.get("sha256")} if isinstance(job, dict)
        else None,
        state=(result or {}).get("state"),
        attempts=len((result or {}).get("attempts") or []) if result else None)
    verification = (result or {}).get("verification")
    view["verification"] = (
        {"state": verification.get("state"), "checked_at": verification.get("checked_at"),
         "problems": len(verification.get("problems") or [])}
        if isinstance(verification, dict) else None)
    state = view["state"]
    if state not in tasks.ATTESTED:
        view["attestation"], view["attestation_reason"] = "not_applicable", None
    elif unchecked:
        view["attestation"], view["attestation_reason"] = "unchecked", unchecked
    else:
        reason = tasks._unattested(task_id, result, latest, chain_problems)
        view["attestation"] = "UNATTESTED" if reason else "attested"
        view["attestation_reason"] = reason
        entry = latest.get(task_id)
        if entry:
            view["ledger_verdict"] = {"n": entry["n"], "sha256": entry["sha256"],
                                      "verdict": entry["record"].get("verdict")}
    return view


def _joined_tasks(root: Path, ledger_rows: list, packet_ids: set, ledger_info: dict,
                  max_bytes: int) -> tuple[list, dict]:
    latest, chain_problems, unchecked = _attest(root, ledger_info, max_bytes)
    order: list = []
    for row in ledger_rows:
        if row["task_id"] and row["task_id"] not in order:
            order.append(row["task_id"])
    views = [_task_view(root, task_id, "ledger session", ledger_rows, latest, chain_problems,
                        unchecked) for task_id in order]
    info = {"name": "tasks", "file": TASKS_DIR, "status": "ok", "size": None, "scanned": 0,
            "lines": len(views), "problems": []}
    wanted = {p for p in packet_ids if HEX64.match(p)}
    folder = root / TASKS_DIR
    if wanted and folder.is_dir() and not folder.is_symlink():
        try:
            names = sorted(n for n in os.listdir(folder) if TASK_ID.match(n))
        except OSError as exc:
            names = []
            info["problems"].append(f"cannot list {TASKS_DIR}: {exc.strerror or exc}")
        if len(names) > MAX_TASK_DIRS:
            info["status"] = "partial"
            info["problems"].append(f"{len(names)} task folders; only the first "
                                    f"{MAX_TASK_DIRS} were checked for the packet join")
            names = names[:MAX_TASK_DIRS]
        for name in names:
            if name in order:
                continue
            task, _ = _small_json(folder / name / "task.json")
            if task and task.get("shared_packet_id") in wanted:
                views.append(_task_view(root, name, "delivered packet id", ledger_rows,
                                        latest, chain_problems, unchecked))
    elif not folder.is_dir():
        info["status"] = "missing"
    info["lines"] = len(views)
    return views, info


def _advisor(root: Path, window: tuple) -> dict:
    """Counters from the advisor call log inside [first, last] of this session's own times."""
    out = {"name": "advisor", "file": JEV_LOG, "attributed": False,
           "basis": "time window of this session's other records; the log has no session "
                    "field, so rows of other sessions that overlap in time are included",
           "status": "missing", "size": None, "lines": 0, "problems": [], "window": None,
           "rows": 0, "features": {}}
    if not os.path.lexists(root / JEV_LOG):
        return out
    info = _source("advisor", JEV_LOG, root / JEV_LOG)
    rows = []
    for number, body, complete in _lines(root / JEV_LOG, info, DEFAULT_MAX_BYTES):
        row = _json_line(body)
        if row is None:
            _bad(info, number, complete)
        else:
            rows.append(row)
    out["status"] = info["status"]
    out["problems"] = info["problems"]
    out["size"] = info["size"]
    if window[0] is None:
        out["status"] = "no_window"      # nothing in this session is dated to match against
        return out
    out["window"] = {"from": _stamp(window[0]), "to": _stamp(window[1])}
    inside = [r for r in rows if isinstance(r.get("at"), (int, float))
              and window[0] <= r["at"] <= window[1] + 1]
    out["rows"] = out["lines"] = len(inside)
    for feature in sorted({r.get("feature") or "unknown" for r in inside}):
        mine = [r for r in inside if (r.get("feature") or "unknown") == feature]

        def total(key):
            return sum(r[key] for r in mine if isinstance(r.get(key), (int, float))
                       and not isinstance(r.get(key), bool))
        cost = [r["cost_usd"] for r in mine if isinstance(r.get("cost_usd"), (int, float))]
        out["features"][feature] = {
            "rows": len(mine),
            "modes": {m: sum(1 for r in mine if r.get("mode") == m)
                      for m in sorted({str(r.get("mode")) for r in mine})},
            "applied": sum(1 for r in mine if r.get("applied")),
            "degraded": sum(1 for r in mine if r.get("degraded")),
            "cache_hits": sum(1 for r in mine if r.get("cache_hit")),
            "requests": total("requests"), "input_tokens": total("input_tokens"),
            "output_tokens": total("output_tokens"),
            "cost_usd": round(sum(cost), 6) if cost else None}
    return out


def _stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def _vault(value) -> Path:
    root = Path(value).expanduser()
    if not root.is_dir():
        raise ShowError(f"vault not found: {root.name or value}")
    return root.resolve()


def _cut(items: list, limit: int) -> tuple[list, int]:
    return items[:limit], max(0, len(items) - limit)


def build(vault, session: str, limit: int = DEFAULT_LIMIT,
          max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    """The joined report for one session id; raises ShowError when no source knows it."""
    root = _vault(vault)
    session = str(session).strip()
    if not session:
        raise ShowError("the session id is empty")
    ev_info, ev_rows = _read_evidence(root, session, max_bytes)
    bom_info, bom_rows = _read_bom(root, session, max_bytes)
    mem_info, mem_rows = _read_memory(root, session, max_bytes)
    led_info, led_rows = _read_task_ledger(root, session, max_bytes)
    if not (ev_info["status"] != "missing" or bom_info["status"] != "missing"
            or mem_rows or led_rows):
        raise ShowError(f"unknown session id {session!r}: no evidence ledger, session record, "
                        "memory record or task-ledger line carries it "
                        "(`context-layer session list` shows the ids)")

    delivered = {(r["path"], r["sha256"]) for r in ev_rows}
    have_ledger = ev_info["status"] != "missing"
    for record in mem_rows:
        for source in record["sources"]:
            source["delivered_in_session"] = (
                (source["path"], source["sha256"]) in delivered if have_ledger else None)

    packets: dict = {}
    for row in ev_rows:
        key = row["packet_id"]
        entry = packets.setdefault(key, {"packet_id": key, "items": 0, "first_at": row["at"],
                                         "last_at": row["at"], "first_cite": row["cite"]})
        entry["items"] += 1
        entry["last_at"] = row["at"] or entry["last_at"]
    packet_list = []
    for entry in packets.values():
        pid = entry["packet_id"]
        if pid is None:
            entry.update(kind="none", packet_file=None, packet_file_status=None)
        elif HEX64.match(pid):
            present = (root / PACKETS_DIR / f"{pid}.json").is_file()
            entry.update(kind="shared_packet_id", packet_file=(
                f"{PACKETS_DIR}/{pid}.json" if present else None), packet_file_status=(
                "present" if present else "absent"))
        else:
            entry.update(kind="other_id", packet_file=None, packet_file_status=None)
        packet_list.append(entry)

    task_views, task_info = _joined_tasks(
        root, led_rows, {p for p in packets if p}, led_info, max_bytes)

    stamps = [_epoch(r["at"]) for r in ev_rows] + [_epoch(r["ts"]) for r in bom_rows] \
        + [_epoch(r["ts"]) for r in mem_rows] + [_epoch(r["ts"]) for r in led_rows]
    stamps = [s for s in stamps if s is not None]
    advisor = _advisor(root, (min(stamps) if stamps else None, max(stamps) if stamps else None))

    sources = [ev_info, mem_info, bom_info, led_info, task_info, advisor]
    problems = [f"{s['file']}: {p}" for s in sources for p in s["problems"]]
    complete = all(s["status"] in ("ok", "missing") and not s["problems"] for s in sources)

    ev_shown, ev_more = _cut(ev_rows, limit)
    mem_shown, mem_more = _cut(mem_rows, limit)
    bom_shown, bom_more = _cut(bom_rows, limit)
    task_shown, task_more = _cut(task_views, limit)
    pk_shown, pk_more = _cut(packet_list, limit)
    for view in task_shown:
        view["ledger_lines"], _ = _cut(view["ledger_lines"], limit)
    return {
        "schema": SCHEMA, "session": session, "read_only": True,
        "bounds": {"limit": limit, "max_bytes": max_bytes},
        "complete": complete, "problems": problems,
        "sources": [{k: s[k] for k in ("name", "file", "status", "size", "lines", "problems")}
                    for s in sources],
        "evidence": {"total": len(ev_rows), "not_shown": ev_more, "items": ev_shown,
                     "packets_total": len(packet_list), "packets_not_shown": pk_more,
                     "packets": pk_shown},
        "memory": {"records_total": len(mem_rows), "records_not_shown": mem_more,
                   "records": mem_shown,
                   "session_record_events_total": len(bom_rows),
                   "session_record_events_not_shown": bom_more,
                   "session_record_events": bom_shown},
        "tasks": {"total": len(task_views), "not_shown": task_more, "items": task_shown},
        "advisor": {k: advisor[k] for k in ("file", "status", "attributed", "basis", "window",
                                            "rows", "features")},
    }


def _ids(root: Path, max_bytes: int) -> dict:
    """{session id: row} across every source, for `session list`."""
    found: dict = {}
    problems: list = []

    def entry(key):
        return found.setdefault(key, {"session": key, "sources": [], "counts": {}, "last": None})

    def note(key, source, count, stamp):
        row = entry(key)
        if source not in row["sources"]:
            row["sources"].append(source)
        row["counts"][source] = row["counts"].get(source, 0) + count
        if stamp and (row["last"] is None or stamp > row["last"]):
            row["last"] = stamp

    raw_ids: set = set()
    for name, rel, path, source in (
            ("memory_records", MEMORY_FILE, memory.records_path(root), "memory_records"),
            ("task_ledger", LEDGER_FILE, orchestrate.ledger_path(root), "task_ledger")):
        info = _source(name, rel, path)
        for number, body, complete in _lines(path, info, max_bytes):
            record = _json_line(body)
            if record is None:
                _bad(info, number, complete)
                continue
            sid = record.get("session")
            if isinstance(sid, str) and sid.strip():
                raw_ids.add(sid)
                note(sid, source, 1, record.get("ts") if isinstance(record.get("ts"), str)
                     else None)
        problems += [f"{rel}: {p}" for p in info["problems"]]
    alias = {}
    for sid in raw_ids:
        alias[session_evidence.safe_stem(sid)] = sid
        alias[memory.session_path(root, sid).stem] = sid
    for source, rel, folder in (("evidence_ledger", EVIDENCE_DIR, root / EVIDENCE_DIR),
                                ("session_record", SESSIONS_DIR, root / SESSIONS_DIR)):
        try:
            files = sorted(p for p in folder.glob("*.jsonl") if p.is_file() and not p.is_symlink()) \
                if folder.is_dir() and not folder.is_symlink() else []
        except OSError as exc:
            problems.append(f"{rel}: cannot be listed: {exc.strerror or exc}")
            continue
        for path in files:
            info = _source(source, f"{rel}/{path.name}", path)
            last = None
            for _number, body, _complete in _lines(path, info, max_bytes):
                row = _json_line(body)
                stamp = (row or {}).get("at", (row or {}).get("ts"))
                last = stamp if isinstance(stamp, str) else last
            note(alias.get(path.stem, path.stem), source, info["lines"], last)
            problems += [f"{rel}/{path.name}: {p}" for p in info["problems"]]
    return {"sessions": found, "problems": problems}


def list_sessions(vault, limit: int = DEFAULT_LIMIT, max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    root = _vault(vault)
    found = _ids(root, max_bytes)
    for row in found["sessions"].values():
        row["sources"].sort()
    rows = sorted(found["sessions"].values(), key=lambda r: r["session"])
    rows.sort(key=lambda r: r["last"] or "", reverse=True)     # newest first, undated last
    shown, more = _cut(rows, limit)
    return {"schema": LIST_SCHEMA, "read_only": True, "total": len(rows), "not_shown": more,
            "bounds": {"limit": limit, "max_bytes": max_bytes},
            "sessions": shown, "problems": found["problems"]}


def latest_session(vault, max_bytes: int = DEFAULT_MAX_BYTES) -> tuple[str | None, int]:
    """(the session with the newest timestamp, how many sessions exist)."""
    listing = list_sessions(vault, limit=1, max_bytes=max_bytes)
    return (listing["sessions"][0]["session"] if listing["sessions"] else None), listing["total"]


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------

def _more(out: list, count: int) -> None:
    if count:
        out.append(f"  ... {count} more not shown (raise --limit)")


def render(report: dict) -> str:
    out = [f"Session {_safe(report['session'])}  (read-only: nothing was written)",
           "", "Sources"]
    for s in report["sources"]:
        extra = f"  {s['lines']} line(s)" if s["lines"] else ""
        out.append(f"  {s['name']:<16} {s['file']}  {s['status']}{extra}")
        out += [f"      ! {_safe(p)}" for p in s["problems"]]
    ev = report["evidence"]
    out += ["", f"Evidence delivered: {ev['total']} item(s) in {ev['packets_total']} packet id(s)"]
    for p in ev["packets"]:
        label = _safe(p["packet_id"]) if p["packet_id"] else "(no packet id)"
        status = f", packet file {p['packet_file_status']}" if p.get("packet_file_status") else ""
        out.append(f"  packet {label}  {p['items']} item(s){status}  [{p['first_cite']}]")
    _more(out, ev["packets_not_shown"])
    for r in ev["items"]:
        out.append(f"  {r['cite']}  {_safe(r['path'])}  sha256 {r['sha256'][:SHORT]}  "
                   f"at {r['at'] or '-'}")
    _more(out, ev["not_shown"])
    mem = report["memory"]
    out += ["", f"Memory records written in this session: {mem['records_total']}"]
    for r in mem["records"]:
        force = {True: "in force", False: "superseded by " + ", ".join(r["superseded_by"] or []),
                 None: "in-force state unknown (source incomplete)"}[r["in_force"]]
        out.append(f"  {r['cite']}  {r['id']}  {r['kind']}/{r['state']}  {force}")
        for s in r["sources"]:
            seen = {True: "delivered in this session", False: "not in this session's ledger",
                    None: "no evidence ledger"}[s.get("delivered_in_session")]
            out.append(f"      source {_safe(s['path'])} sha256 "
                       f"{(s['sha256'] or '-')[:SHORT]}  ({seen})")
    _more(out, mem["records_not_shown"])
    out += ["", f"Session record events: {mem['session_record_events_total']}"]
    for e in mem["session_record_events"]:
        what = e.get("id") or (f"{_safe(e.get('path'))} sha256 {(e.get('sha256') or '-')[:SHORT]}"
                               + (" STALE when checked" if e.get("stale_when_checked") else ""))
        out.append(f"  {e['cite']}  {e.get('op')}/{e.get('event')}  {what}")
    _more(out, mem["session_record_events_not_shown"])
    t = report["tasks"]
    out += ["", f"Tasks: {t['total']}"]
    for v in t["items"]:
        out.append(f"  task {_safe(v['task_id'])}  state {v.get('state')}  "
                   f"ledger {v.get('attestation')}  (joined by {v['joined_by']})  [{v['files']}]")
        if v.get("attestation_reason"):
            out.append(f"      {_safe(v['attestation_reason'])}")
        for line in v["ledger_lines"]:
            out.append(f"      {line['cite']}  {line['event']}"
                       + (f"  verdict {line['verdict']}" if line.get("verdict") else "")
                       + (f"  memory {line['memory_id']}" if line.get("memory_id") else ""))
        if v.get("job"):
            out.append(f"      job {_safe(v['job'].get('path'))}")
        out += [f"      ! {_safe(p)}" for p in v["problems"]]
    _more(out, t["not_shown"])
    a = report["advisor"]
    out += ["", f"Advisor counters ({a['file']}, {a['status']}; matched by time window, "
                "not attributed to this session)"]
    for feature, c in a["features"].items():
        out.append(f"  {feature}: {c['rows']} row(s), applied {c['applied']}, degraded "
                   f"{c['degraded']}, cache hits {c['cache_hits']}, tokens in "
                   f"{c['input_tokens']} / out {c['output_tokens']}")
    out += ["", "Complete: " + ("yes" if report["complete"] else
                                "no; see the problems marked ! above")]
    return "\n".join(out) + "\n"


def render_list(listing: dict) -> str:
    if not listing["sessions"]:
        return "No sessions found in this vault.\n" + "".join(
            f"! {_safe(p)}\n" for p in listing["problems"])
    out = [f"{listing['total']} session(s)"]
    for row in listing["sessions"]:
        counts = ", ".join(f"{k} {v}" for k, v in sorted(row["counts"].items()))
        out.append(f"  {_safe(row['session'])}  last {row['last'] or '-'}  [{counts}]")
    _more(out, listing["not_shown"])
    out += [f"! {_safe(p)}" for p in listing["problems"]]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _unexpected(args: argparse.Namespace, command: str) -> bool:
    extra = [token for token in getattr(args, "rest", []) or [] if token]
    if extra:
        print(f"context-layer session {command}: unrecognised arguments: {' '.join(extra)}",
              file=sys.stderr)
        return True
    return False


def cmd_show(args: argparse.Namespace) -> int:
    if _unexpected(args, "show"):
        return 2
    try:
        session = args.session_id
        chosen = ""
        if session is None:
            session, count = latest_session(args.vault, args.max_bytes)
            if session is None:
                print("context-layer session show: no sessions found in this vault",
                      file=sys.stderr)
                return 1
            chosen = f"no session id given: showing the most recent of {count} session(s)"
        elif not str(session).strip():
            print("context-layer session show: the session id is empty", file=sys.stderr)
            return 2
        report = build(args.vault, session, args.limit, args.max_bytes)
    except ShowError as exc:
        print(f"context-layer session show: {exc}", file=sys.stderr)
        return 1
    if chosen:
        print(f"context-layer session show: {chosen}", file=sys.stderr)
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(render(report))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    if _unexpected(args, "list"):
        return 2
    try:
        listing = list_sessions(args.vault, args.limit, args.max_bytes)
    except ShowError as exc:
        print(f"context-layer session list: {exc}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(listing, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(render_list(listing))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    """Attach `session show|list` to the CLI."""
    parser = sub.add_parser(
        "session", help="Join what one agent session delivered, recorded and ran (read-only).",
        description="Reads the evidence ledger, memory records, session record, task ledger "
                    "and packets that already exist under .context and joins them on the "
                    "session id. Every row cites its file and line; a missing or torn source "
                    "is reported, never guessed; nothing is written.")
    inner = parser.add_subparsers(dest="session_command", required=True)

    def bounds(p):
        p.add_argument("--limit", type=_positive, default=DEFAULT_LIMIT, metavar="N",
                       help=f"Rows per section in the output (default {DEFAULT_LIMIT}).")
        p.add_argument("--max-bytes", type=_positive, default=DEFAULT_MAX_BYTES, metavar="N",
                       help="Bytes read from each source file "
                            f"(default {DEFAULT_MAX_BYTES}); a larger file is reported "
                            "as partial.")
        p.add_argument("--json", action="store_true", dest="as_json")

    show = inner.add_parser("show", help="One session's joined report.")
    show.add_argument("vault")
    show.add_argument("session_id", nargs="?", default=None, metavar="SESSION_ID",
                      help="The session id (default: the session with the newest record).")
    bounds(show)
    show.set_defaults(func=cmd_show, forward_to=None)
    listing = inner.add_parser("list", help="The session ids found in the vault.")
    listing.add_argument("vault")
    bounds(listing)
    listing.set_defaults(func=cmd_list, forward_to=None)
