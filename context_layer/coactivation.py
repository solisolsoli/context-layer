"""context_layer.coactivation - a usage ledger of notes delivered together, and the link
suggestions a person can read from it.

Opt-in and one-way. With `"record_usage": true` in `.context/routes.json`, every fts or
synaptic retrieval that delivers two or more notes appends one line to
`.context/usage-ledger.jsonl` (schema `usage-ledger/v1`):

    {"v":1,"at":"2026-01-15T14:30:00Z","m":"fts","s":"3f2a9c1b7d4e","p":["a.md","b/c.md"]}

`at` is the UTC time, `m` the method, `s` a 12-hex SHA-256 prefix of the host session id
(null when the host gave none), `p` the vault-relative paths of the notes the packet
delivered, sorted and at most `MAX_NOTES_PER_LINE`. Paths only: never note text, never the
prompt, never a hash or count of the prompt, never a note that fell under the vault's
exclusions. A retrieval that delivered fewer than two notes writes nothing, because it
holds no pair. Readers ignore unknown keys and skip lines they cannot use.

The ledger is bounded: the file rotates to `usage-ledger.jsonl.1` at `MAX_LEDGER_BYTES`
(replacing the older rotation), so at most twice that stays on disk.

`suggest()` reads the ledger and the link graph and lists pairs of notes that were
delivered together at least `min_count` times and have no explicit link between them in
either direction. It prints evidence (retrievals, sessions, days) and writes nothing: no
note and no link. A person decides, and a link reaches retrieval only when that person
writes it, through the explicit graph.

**The ledger is never read by retrieval, ranking, the advisor or any host tool.** Only
`suggest()` reads it. It is usage, not relatedness: two notes can be delivered together
because a prompt was broad, or because one is a hub that is delivered with everything.
The report says how often each note was delivered so that a person can see that.

Prior art: co-citation and bibliographic coupling (Small, 1973; Kessler, 1963), the
Obsidian Graph Analysis plugin's co-citation view, and click-log learning in search
engines. This module keeps the counts out of ranking and hands them to a person.

Python 3.10+; standard library only; no network access.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

try:  # POSIX advisory locking; appends are single O_APPEND writes either way.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

SCHEMA = "usage-ledger/v1"
VERSION = 1
LEDGER_NAME = "usage-ledger.jsonl"
LOCK_NAME = ".usage-ledger.lock"
SETTING = "record_usage"
METHODS = ("fts", "synaptic")
MAX_LEDGER_BYTES = 1024 * 1024      # per file; then it rotates to `.1`
MAX_NOTES_PER_LINE = 16
MAX_PATH_CHARS = 1024
MAX_LINE_BYTES = 16 * 1024          # a longer line is never read
SUGGEST_LIMIT = 20
MIN_COUNT = 2


def ledger_path(vault) -> Path:
    return Path(vault) / ".context" / LEDGER_NAME


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _policy():
    """router/source_policy, found the way graph.py finds it (checkout or wheel)."""
    from . import graph
    return graph._policy()


def session_key(session_id: str | None = None) -> str | None:
    """12 hex of the SHA-256 of the host session id, or None when there is none."""
    value = session_id if session_id is not None else os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    value = str(value).strip()
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def enabled(vault) -> bool:
    """Is `record_usage` exactly true in routes.json? False on any doubt (default off)."""
    try:
        policy = _policy()
        config = policy.load_config(policy.config_path(Path(vault)))
    except Exception:  # noqa: BLE001 - an unusable config means: record nothing
        return False
    return isinstance(config, dict) and config.get(SETTING) is True


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def delivered_paths(packet) -> list[str]:
    """The distinct `source_path`s of a packet's evidence items, in delivery order."""
    seen: dict[str, None] = {}
    if isinstance(packet, dict):
        for item in packet.get("evidence") or []:
            path = item.get("source_path") if isinstance(item, dict) else None
            if isinstance(path, str) and path:
                seen.setdefault(path, None)
    return list(seen)


def record(vault, packet, method: str, session_id: str | None = None,
           now: datetime | None = None) -> dict:
    """Append one ledger line for a delivered packet, if the setting is on.

    Returns {"written": bool, "reason": str or None}. It never raises: recording usage
    must not change or break a retrieval. Nothing is created when the setting is off.
    """
    try:
        return _record(Path(vault).expanduser().resolve(), packet, method, session_id, now)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        return {"written": False, "reason": f"not recorded ({type(exc).__name__})"}


def _record(root: Path, packet, method: str, session_id, now) -> dict:
    if method not in METHODS:
        return {"written": False, "reason": "method not recorded"}
    if not enabled(root):
        return {"written": False, "reason": "off"}
    if not isinstance(packet, dict) or packet.get("operation_status") != "ok":
        return {"written": False, "reason": "no delivered evidence"}
    policy = _policy()
    prefixes = policy.config_exclusions(policy.load_config(policy.config_path(root)))
    names: dict[str, None] = {}
    for path in delivered_paths(packet):
        try:
            name = policy.relative_name(path)
            if len(name) > MAX_PATH_CHARS or policy.excluded(name, prefixes):
                continue
        except ValueError:
            continue
        names.setdefault(name, None)
        if len(names) >= MAX_NOTES_PER_LINE:
            break
    if len(names) < 2:
        return {"written": False, "reason": "fewer than two notes"}
    line = json.dumps({"v": VERSION, "at": _now_iso(now), "m": method,
                       "s": session_key(session_id), "p": sorted(names)},
                      ensure_ascii=True, separators=(",", ":")) + "\n"
    _append(ledger_path(root), line.encode("ascii"))
    return {"written": True, "reason": None}


def _append(target: Path, data: bytes) -> None:
    """One O_APPEND write under an advisory lock; rotate to `.1` when the file is full."""
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = os.open(target.parent / LOCK_NAME, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            full = target.stat().st_size + len(data) > MAX_LEDGER_BYTES
        except OSError:
            full = False
        if full:
            os.replace(target, target.with_name(target.name + ".1"))
        fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    finally:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


# ---------------------------------------------------------------------------
# Reading (`graph suggest` only)
# ---------------------------------------------------------------------------

def read_ledger(vault) -> dict:
    """Usable ledger rows (oldest file first) and how many lines were skipped."""
    target = ledger_path(vault)
    rows, skipped = [], 0
    for path in (target.with_name(target.name + ".1"), target):
        try:
            handle = open(path, "rb")
        except OSError:
            continue
        with handle:
            for raw in handle:
                row = _parse_line(raw)
                if row is None:
                    skipped += 1
                else:
                    rows.append(row)
    return {"rows": rows, "skipped": skipped}


def _parse_line(raw: bytes):
    if len(raw) > MAX_LINE_BYTES or not raw.strip():
        return None
    try:
        row = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(row, dict) or row.get("v") != VERSION:
        return None
    paths, at = row.get("p"), row.get("at")
    if not isinstance(paths, list) or not isinstance(at, str) or len(at) < 10:
        return None
    paths = [p for p in paths if isinstance(p, str) and 0 < len(p) <= MAX_PATH_CHARS]
    session = row.get("s")
    return {"paths": sorted(set(paths))[:MAX_NOTES_PER_LINE], "day": at[:10], "at": at,
            "session": session if isinstance(session, str) and session else None}


def suggest(vault, min_count: int = MIN_COUNT, limit: int = SUGGEST_LIMIT) -> dict:
    """Pairs delivered together that no explicit link joins, ranked by retrievals.

    Read-only. Raises ValueError (one line naming the fix) when there is no usable link
    graph or routes.json. Notes the current routes.json excludes, and notes the graph no
    longer knows, are left out; their pairs are counted in `dropped_pairs` only.
    """
    from . import graph as graphs
    root = Path(vault).expanduser().resolve()
    policy = _policy()
    try:
        prefixes = policy.config_exclusions(policy.load_config(policy.config_path(root)))
    except ValueError as exc:
        raise ValueError(str(exc)) from None
    try:
        link_graph = graphs.open_graph(root)
    except graphs.GraphUnreadable as exc:
        raise ValueError(str(exc)) from None
    if link_graph is None:
        raise ValueError("no link graph (.context/graph.sqlite); run `context-layer index "
                         "<vault>` to build it")
    min_count, limit = max(1, int(min_count)), max(1, int(limit))
    ledger = read_ledger(root)
    seen: dict[str, dict] = {}       # path -> {"count": n}
    pairs: dict[tuple[str, str], dict] = {}
    known: dict[str, bool] = {}

    def visible(name: str) -> bool:
        if name not in known:
            try:
                ok = not policy.excluded(name, prefixes)
            except ValueError:
                ok = False
            known[name] = ok and link_graph.note(name) is not None
        return known[name]

    try:
        for row in ledger["rows"]:
            names = [n for n in row["paths"] if visible(n)]
            for name in names:
                seen.setdefault(name, {"count": 0})["count"] += 1
            for i, first in enumerate(names):
                for second in names[i + 1:]:
                    entry = pairs.setdefault((first, second), {
                        "count": 0, "sessions": set(), "days": set(),
                        "first": row["at"], "last": row["at"]})
                    entry["count"] += 1
                    entry["days"].add(row["day"])
                    if row["session"]:
                        entry["sessions"].add(row["session"])
                    entry["first"] = min(entry["first"], row["at"])
                    entry["last"] = max(entry["last"], row["at"])
        linked = _linked_pairs(link_graph, pairs, min_count)
    finally:
        link_graph.close()
    total_pairs = len(pairs)
    candidates = [(pair, entry) for pair, entry in pairs.items()
                  if entry["count"] >= min_count and pair not in linked]
    candidates.sort(key=lambda item: (-item[1]["count"], -len(item[1]["days"]),
                                      -len(item[1]["sessions"]), item[0]))
    suggestions = [{"a": a, "b": b, "retrievals": entry["count"],
                    "sessions": len(entry["sessions"]), "days": len(entry["days"]),
                    "first_seen": entry["first"], "last_seen": entry["last"],
                    "a_delivered": seen[a]["count"], "b_delivered": seen[b]["count"]}
                   for (a, b), entry in candidates[:limit]]
    return {"schema": "link-suggestions-v1",
            "basis": "usage, not relatedness: notes delivered together in fts or synaptic "
                     "retrievals, with no explicit link between them",
            "ledger_present": bool(ledger["rows"]) or ledger_path(root).exists(),
            "retrievals": len(ledger["rows"]), "skipped_lines": ledger["skipped"],
            "pairs_seen": total_pairs,
            "already_linked": sum(1 for pair, e in pairs.items()
                                  if e["count"] >= min_count and pair in linked),
            "min_count": min_count, "suggestions": suggestions,
            "truncated": len(candidates) > limit}


def _linked_pairs(link_graph, pairs, min_count: int) -> set[tuple[str, str]]:
    """The candidate pairs that have an edge in either direction (any link kind)."""
    linked: set[tuple[str, str]] = set()
    wanted = {name for pair, e in pairs.items() if e["count"] >= min_count for name in pair}
    for name in sorted(wanted):
        for edge in link_graph.outgoing(name):
            pair = tuple(sorted((edge.source, edge.target)))
            if pair in pairs:
                linked.add(pair)
    return linked


def render(report: dict) -> str:
    lines = ["Link suggestions from usage (read-only; no note and no link is written)",
             f"  {report['basis']}.",
             f"  ledger: {report['retrievals']} retrieval(s) with two or more notes, "
             f"{report['pairs_seen']} pair(s) seen, {report['already_linked']} already linked "
             f"(min {report['min_count']} retrievals)."]
    if report["skipped_lines"]:
        lines.append(f"  {report['skipped_lines']} ledger line(s) were unusable and skipped.")
    if not report["ledger_present"]:
        lines.append("  No usage ledger yet. Set \"record_usage\": true in .context/routes.json "
                     "to start one; it is off by default.")
        return "\n".join(lines)
    if not report["suggestions"]:
        lines.append("  Nothing to suggest.")
        return "\n".join(lines)
    lines.append("")
    for number, item in enumerate(report["suggestions"], 1):
        sessions = f"{item['sessions']} session(s), " if item["sessions"] else ""
        lines.append(f"{number:>3}. {item['a']}  <->  {item['b']}")
        lines.append(f"       delivered together {item['retrievals']}x ({sessions}"
                     f"{item['days']} day(s), {item['first_seen'][:10]} to "
                     f"{item['last_seen'][:10]}); each note was delivered "
                     f"{item['a_delivered']}x and {item['b_delivered']}x in all")
    if report["truncated"]:
        lines.append("  ... more pairs; raise --limit to list them.")
    lines.append("")
    lines.append("A frequently delivered note pairs with many others: check the two totals before "
                 "adding a link. Add links by hand; run `context-layer index` afterwards.")
    return "\n".join(lines)


def cmd_suggest(args) -> int:
    if getattr(args, "rest", None):
        print(f"context-layer graph suggest: unrecognised arguments: {' '.join(args.rest)}",
              file=sys.stderr)
        return 2
    vault = Path(args.vault).expanduser()
    if not vault.is_dir():
        print(f"context-layer graph suggest: vault not found: {args.vault}", file=sys.stderr)
        return 1
    if args.min_count < 1 or args.limit < 1:
        print("context-layer graph suggest: --min-count and --limit must be at least 1",
              file=sys.stderr)
        return 2
    try:
        report = suggest(vault, args.min_count, args.limit)
    except ValueError as exc:
        print(f"context-layer graph suggest: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1) if args.as_json else render(report))
    return 0


def register(inner) -> None:
    """Attach `graph suggest` to the `graph` sub-commands."""
    parser = inner.add_parser(
        "suggest", help="Notes delivered together that no link joins (from the opt-in usage "
                        "ledger); read-only.",
        description="Reads .context/usage-ledger.jsonl (written only when \"record_usage\": "
                    "true is set in routes.json) and the link graph, and lists pairs of notes "
                    "that retrievals delivered together but that have no explicit link between "
                    "them, with counts, sessions and days. It writes no note and no link, and "
                    "retrieval never reads the ledger. Exit code: 0 report printed, 1 no usable "
                    "graph or routes.json, 2 usage error.")
    parser.add_argument("vault")
    parser.add_argument("--min-count", type=int, default=MIN_COUNT,
                        help=f"Fewest retrievals a pair needs (default: {MIN_COUNT}).")
    parser.add_argument("--limit", type=int, default=SUGGEST_LIMIT,
                        help=f"Pairs to list (default: {SUGGEST_LIMIT}).")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="Print the machine-readable report (schema link-suggestions-v1).")
    parser.set_defaults(func=cmd_suggest, forward_to=None)
