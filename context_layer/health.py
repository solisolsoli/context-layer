"""context_layer.health — source lifecycle: what the index covers, and what drifted.

`status` compares the index against the vault as it is now. Every in-scope file
is read and hashed on every run: size and mtime are not evidence. A source can
be rewritten with both preserved, and a stat-keyed hash cache then certifies the
old bytes (see docs/source-lifecycle.md). The cost of not having that bug is
reading the in-scope bytes each time.

It also names what the index cannot cover (`now_skipped`: empty, over the size
limit, unreadable, not UTF-8, or a name the router refuses) and whether the link
graph belongs to the same build as the index.

`rollback` restores the index that the last successful build replaced, and the
link graph with it, so a rebuild against a half-synced vault is reversible.

Python 3.10+; standard library only; no network access; no writes to vault
content — only `<vault>/.context/` is touched, and only by `rollback`.
"""

from __future__ import annotations

import argparse
import codecs
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat as stat_module
import sys

SCHEMA = "source-health-v1"
CONTEXT_DIR = ".context"
ROUTES_NAME = "routes.json"
GRAPH_NAME = "graph.sqlite"     # written by context_layer.graph; kept with a .prev by `index`
READ_BLOCK = 1 << 20
MAX_FILE_BYTES_CAP = 50_000_000  # the most routes.json `max_file_bytes` may ask for

# overall -> process exit code. Missing is 2 because there is nothing to report
# against; stale and degraded are both 1 because both withhold or omit evidence.
# error (routes.json cannot be trusted, so nothing was read) is 2 as well: there
# is nothing trustworthy to compare against. See docs/cli.md for every command.
EXIT_CODES = {"ok": 0, "stale": 1, "degraded": 1, "missing": 2, "error": 2}

# Reasons the index builder records in index-manifest.json `skipped`, in the
# words `now_skipped` uses. An unknown reason is shown as the builder wrote it.
BUILDER_REASONS = {"empty": "empty", "oversize": "over size limit",
                   "over_size_limit": "over size limit", "unreadable": "unreadable",
                   "not_utf8": "not UTF-8", "unsupported_name": "unsupported name"}


def _router_modules():
    """router/source_policy and router/build_index through the package's one import route.

    `mcp_server.router_module` is the route every component uses, so the
    exclusion rules and ConfigError come from one module object. Imported on
    call, not at module level: mcp_server imports this module.
    """
    from .mcp_server import router_module
    return router_module("source_policy"), router_module("build_index")


def _index_format():
    """router/index_format, imported the same way as the other router modules."""
    from .mcp_server import router_module
    return router_module("index_format")


def _paths(vault: Path):
    """Return (index, manifest, routes) inside <vault>/.context."""
    _, builder = _router_modules()
    context = vault / CONTEXT_DIR
    return (context / builder.INDEX_NAME, context / builder.MANIFEST_NAME,
            context / ROUTES_NAME)


def _previous(path: Path) -> Path:
    _, builder = _router_modules()
    return path.with_name(path.name + builder.PREV_SUFFIX)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(READ_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash_checked(path: str) -> "tuple[str, bool]":
    """(SHA-256, whether the bytes are valid UTF-8), in one streamed pass."""
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")()
    utf8 = True
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(READ_BLOCK), b""):
            digest.update(block)
            if utf8:
                try:
                    decoder.decode(block)
                except UnicodeDecodeError:
                    utf8 = False
    if utf8:
        try:
            decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            utf8 = False
    return digest.hexdigest(), utf8


def _config(routes: Path):
    """(config, exclusion prefixes, problem) through the shared strict loader.

    A problem means the config cannot be trusted. The caller then reads no
    source at all: scanning with no exclusions would hash (and report on)
    exactly the files the config meant to keep out.
    """
    policy, _ = _router_modules()
    try:
        config = policy.load_config(routes)
        return config, policy.config_exclusions(config), ""
    except ValueError as exc:
        return {}, (), str(exc)


def _max_file_bytes(config: dict, builder) -> int:
    """The builder's per-file limit: routes.json `max_file_bytes` when it is a whole
    number from 1 to 50,000,000, else the builder's default."""
    value = config.get("max_file_bytes")
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MAX_FILE_BYTES_CAP:
        return value
    return builder.MAX_FILE_BYTES


def _scan(vault: Path, prefixes: "tuple[str, ...]", extensions: "set[str]", limit: int,
          indexed: "dict[str, str]") -> dict:
    """Walk the vault the way the builder does and hash every in-scope file.

    Returns a dict with
      current         {path: sha256} of the files the builder indexes (or indexed)
      skipped         {path: reason} of in-scope files it cannot index: empty, over
                      size limit, unreadable, not UTF-8, or an unreadable folder
      invalid         paths whose name the router refuses; counted as excluded
      symlinks, excluded_count, newest (mtime of the newest current file)
    Symlinks are never followed and never opened: they are unsupported sources.
    A file over the size limit that the index holds with the same bytes counts as
    covered: the build that indexed it had a higher limit.
    """
    policy, builder = _router_modules()
    skip_parts = set(builder.DEFAULT_SKIP_PARTS)
    root = str(vault)
    current: "dict[str, str]" = {}
    skipped: "dict[str, str]" = {}
    invalid: "set[str]" = set()
    symlinks: "list[str]" = []
    unreadable_folders: "list[str]" = []
    excluded_count = 0
    newest = 0.0

    def excluded(name: str):
        try:
            return policy.excluded(name, prefixes)
        except ValueError:
            return None  # Not a name the router accepts as a source.

    def on_error(error: OSError) -> None:
        name = str(getattr(error, "filename", "") or "")
        if name.startswith(root + os.sep):
            unreadable_folders.append(name[len(root) + 1:].replace(os.sep, "/"))

    for parent, directories, names in os.walk(root, followlinks=False, onerror=on_error):
        base = "" if parent == root else parent[len(root) + 1:].replace(os.sep, "/")
        keep = []
        for name in sorted(directories):
            if name in skip_parts or name.startswith("."):
                continue  # Tool/app noise: pruned like the builder, never counted.
            relative = f"{base}/{name}" if base else name
            if os.path.islink(os.path.join(parent, name)):
                if excluded(relative) is False:  # never name an excluded path
                    symlinks.append(relative + "/")
                continue
            keep.append(name)  # Excluded directories are still walked, to count them.
        directories[:] = keep
        for name in sorted(names):
            if os.path.splitext(name)[1].lower() not in extensions:
                continue
            relative = f"{base}/{name}" if base else name
            verdict = excluded(relative)
            if verdict is None:
                invalid.add(relative)
                excluded_count += 1
                continue
            if verdict:
                excluded_count += 1
                continue
            full = os.path.join(parent, name)
            try:
                info = os.lstat(full)
            except OSError:
                skipped[relative] = "unreadable"
                continue
            if stat_module.S_ISLNK(info.st_mode):
                symlinks.append(relative)
                continue
            if not stat_module.S_ISREG(info.st_mode):
                continue  # A pipe or device named like a note is never opened.
            if info.st_size == 0:
                skipped[relative] = "empty"
                continue
            if info.st_size > limit and relative not in indexed:
                skipped[relative] = "over size limit"
                continue
            try:
                digest, utf8 = _hash_checked(full)
            except OSError:
                skipped[relative] = "unreadable"
                continue
            if info.st_size > limit and indexed.get(relative) != digest:
                skipped[relative] = "over size limit"  # changed, and too big to index again
                continue
            if not utf8:
                skipped[relative] = "not UTF-8"
                continue
            current[relative] = digest
            newest = max(newest, info.st_mtime)
    for folder in sorted(set(unreadable_folders)):
        if excluded(folder) is False:
            skipped[folder + "/"] = "unreadable folder"
    return {"current": current, "skipped": skipped, "invalid": invalid,
            "symlinks": sorted(set(symlinks)), "excluded_count": excluded_count,
            "newest": newest}


def _excluded_name(policy, name: str, prefixes: "tuple[str, ...]") -> bool:
    """Whether a path the router refuses as a name still lies in excluded scope.

    Such a path cannot go through `policy.excluded` itself, so its valid leading
    folders do: a prefix can only ever match those. Dot and tool parts anywhere
    exclude it as well. Errs toward excluding, so an excluded path is never named.
    """
    parts = name.split("/")
    if any(part.startswith(".") or part in policy.BLOCKED_PARTS for part in parts):
        return True
    for end in range(1, len(parts)):
        try:
            if policy.excluded("/".join(parts[:end]), prefixes):
                return True
        except ValueError:
            return False  # an unusable folder name: no prefix reaches below it
    return False


def _from_manifest(manifest: Path):
    """Return (indexed {path: sha}, built_at, source_count, skipped {path: reason}) or None."""
    if not manifest.is_file():
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        sources = payload["sources"]
    except (json.JSONDecodeError, OSError, UnicodeError, KeyError, TypeError):
        return None
    if not isinstance(sources, list):
        return None
    indexed = {entry["path"]: entry["sha256"] for entry in sources
               if isinstance(entry, dict) and "path" in entry and "sha256" in entry}
    built_at = payload.get("built_at")
    count = payload.get("source_count", len(indexed))
    if isinstance(count, bool) or not isinstance(count, int):
        count = len(indexed)
    skipped = {}
    for entry in payload.get("skipped") or []:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str) \
                and isinstance(entry.get("reason"), str):
            skipped[entry["path"]] = BUILDER_REASONS.get(entry["reason"], entry["reason"])
    return indexed, built_at if isinstance(built_at, str) else None, count, skipped


def _readable(index: Path) -> "tuple[bool, str]":
    """Probe the index the way retrieval opens it: a manifest is not a substitute.

    Beyond opening it, the format version must be one this code reads and the
    full-text table must agree with the stored records (FTS5 integrity check on
    a private in-memory copy): an emptied full-text table is not a healthy index.
    Returns (usable, problem).
    """
    formats = _index_format()
    try:
        connection = sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)
    except (sqlite3.Error, OSError, ValueError):
        return False, ""
    try:
        formats.full_check(connection)
    except formats.IndexFormatError as exc:
        return False, str(exc)
    except (sqlite3.Error, OSError):
        return False, ""
    finally:
        connection.close()
    return True, ""


def _from_index(index: Path):
    """Fall back to the SQLite rows for a vault indexed before manifests existed."""
    connection = sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)
    try:
        hashes: "dict[str, set[str]]" = {}
        for path, sha in connection.execute(
                "SELECT DISTINCT source_path, source_sha256 FROM records"):
            hashes.setdefault(path, set()).add(sha)
        row = connection.execute(
            "SELECT value FROM index_meta WHERE key='built_at'").fetchone()
    finally:
        connection.close()
    # One path carrying two hashes is an inconsistent index; "" never matches a
    # real digest, so the source is reported as changed rather than trusted.
    indexed = {path: (shas.pop() if len(shas) == 1 else "")
               for path, shas in hashes.items()}
    return indexed, (row[0] if row else None), len(indexed)


def _markdown(indexed: "dict[str, str]") -> "dict[str, str]":
    """The notes a link graph is built from: the index's Markdown sources."""
    return {path: sha for path, sha in indexed.items() if path.lower().endswith(".md")}


def _graph_notes(path: Path):
    """({note path: sha256}, meta, edge count, problem) of a graph.sqlite, read-only.

    The notes are None, with a problem, when the file is not a link graph this
    version reads.
    """
    formats = _index_format()
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except (sqlite3.Error, OSError, ValueError):
        return None, {}, None, "it cannot be opened"
    try:
        meta = dict(connection.execute("SELECT key, value FROM graph_meta"))
        formats.check_graph_meta(meta)
        notes = dict(connection.execute("SELECT path, sha256 FROM notes"))
        edges = connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    except formats.IndexFormatError as exc:
        return None, {}, None, str(exc)
    except (sqlite3.Error, ValueError, TypeError):
        return None, {}, None, "it cannot be read as a link graph"
    finally:
        connection.close()
    return notes, meta, int(edges), ""


def _graph_state(context: Path, indexed) -> "tuple[dict, str]":
    """Presence and generation of the link graph; `indexed` is None without a usable index."""
    graph = context / GRAPH_NAME
    state = {"present": graph.is_file(), "readable": False, "built_at": None, "notes": None,
             "edges": None, "matches_index": None,
             "rollback_available": _previous(graph).is_file()}
    if not state["present"]:
        return state, ""
    notes, meta, edges, problem = _graph_notes(graph)
    if notes is None:
        return state, problem
    state.update(readable=True, built_at=meta.get("built_at"), notes=len(notes), edges=edges)
    if indexed is not None:
        state["matches_index"] = notes == _markdown(indexed)
    return state, ""


def _age_seconds(built_at: "str | None") -> "int | None":
    if not built_at:
        return None
    try:
        stamp = datetime.fromisoformat(built_at)
    except ValueError:
        return None
    now = datetime.now(stamp.tzinfo) if stamp.tzinfo else datetime.now()
    return max(0, int((now - stamp).total_seconds()))


def _built_before(built_at: "str | None", newest_mtime: float) -> bool:
    """True when a source was modified after the index was built."""
    if not built_at or not newest_mtime:
        return False
    try:
        stamp = datetime.fromisoformat(built_at)
        touched = datetime.fromtimestamp(newest_mtime)
        if stamp.tzinfo:
            touched = touched.astimezone()
        return touched > stamp
    except (ValueError, OSError, OverflowError, TypeError):
        return False


def _human_age(seconds: "int | None") -> str:
    if seconds is None:
        return "unknown age"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit} old"
    return f"{seconds}s old"


def _pair_moves(indexed: dict, current: dict, skipped: dict):
    """Same bytes at a new path: report a move, not a delete plus an add.

    An indexed path that still exists but can no longer be indexed is in
    `skipped`, never in `deleted`.
    """
    deleted = sorted(set(indexed) - set(current) - set(skipped))
    added = sorted(set(current) - set(indexed))
    by_hash: "dict[str, list[str]]" = {}
    for name in added:
        by_hash.setdefault(current[name], []).append(name)
    moved = []
    for name in list(deleted):
        candidates = by_hash.get(indexed.get(name, ""), [])
        if not candidates:
            continue
        destination = candidates.pop(0)
        moved.append({"from": name, "to": destination, "sha256": current[destination]})
        deleted.remove(name)
        added.remove(destination)
    return deleted, added, moved


def _by_reason(entries: "list[dict]") -> str:
    counts: "dict[str, int]" = {}
    for entry in entries:
        counts[entry["reason"]] = counts.get(entry["reason"], 0) + 1
    return ", ".join(f"{count} {reason}" for reason, count in sorted(counts.items()))


def status_summary(vault: "Path | str") -> dict:
    """Index health for one vault, as a plain dict. Importable; no subprocess.

    Raises ValueError if the vault is not a directory. Every path in the result
    is vault-relative, so the output can be shown or logged anywhere.
    """
    vault = Path(vault).expanduser().resolve()
    if not vault.is_dir():
        raise ValueError(f"vault not found: {vault.name}")
    _, builder = _router_modules()
    index, manifest, routes = _paths(vault)
    config, prefixes, config_problem = _config(routes)

    indexed: "dict[str, str]" = {}
    builder_skipped: "dict[str, str]" = {}
    built_at: "str | None" = None
    source_count = 0
    from_manifest = False
    readable = False
    present = index.is_file()
    index_problem = ""
    if present:
        readable, index_problem = _readable(index)
        loaded = _from_manifest(manifest) if readable else None
        if loaded is not None:
            indexed, built_at, source_count, builder_skipped = loaded
            from_manifest = True
        elif readable:
            try:
                indexed, built_at, source_count = _from_index(index)
            except (sqlite3.Error, OSError, ValueError):
                readable = False

    # The extension set the builder used is not recorded, so assume its default
    # plus whatever the index actually holds. A build with a narrower
    # --extensions set therefore reports the omitted extensions as added.
    extensions = {e.lower() for e in builder.DEFAULT_EXTENSIONS}
    extensions |= {Path(name).suffix.lower() for name in indexed if Path(name).suffix}
    symlinks: "list[str]" = []
    excluded_count = 0
    newest = 0.0
    now_skipped: "list[dict]" = []
    if config_problem:
        # Fail closed: without trustworthy exclusions no source is read at all.
        changed, deleted, added, moved = [], [], [], []
        current: "dict[str, str]" = {}
    else:
        scan = _scan(vault, prefixes, extensions, _max_file_bytes(config, builder), indexed)
        current, skipped = scan["current"], dict(scan["skipped"])
        symlinks, excluded_count, newest = scan["symlinks"], scan["excluded_count"], scan["newest"]
        # What the builder recorded as skipped, for names this scan cannot judge:
        # only a path the scan saw, outside every exclusion, is ever named.
        policy, _ = _router_modules()
        for name, reason in builder_skipped.items():
            if name in scan["invalid"] and not _excluded_name(policy, name, prefixes):
                skipped[name] = reason
                excluded_count -= 1
        # An indexed file inside a folder that cannot be listed is unreadable, not deleted.
        folders = [name for name, reason in skipped.items() if reason == "unreadable folder"]
        for name in indexed:
            if name not in current and name not in skipped \
                    and any(name.startswith(folder) for folder in folders):
                skipped[name] = "unreadable"
        changed = sorted(name for name in set(indexed) & set(current)
                         if indexed[name] != current[name])
        deleted, added, moved = _pair_moves(indexed, current, skipped)
        now_skipped = [{"path": name, "reason": skipped[name], "indexed": name in indexed}
                       for name in sorted(skipped)]
    graph, graph_problem = _graph_state(index.parent, indexed if present and readable else None)
    skipped_indexed = [entry for entry in now_skipped if entry["indexed"]]
    blocking = [entry for entry in now_skipped
                if not entry["indexed"] and entry["reason"] != "empty"]
    empty = [entry for entry in now_skipped
             if not entry["indexed"] and entry["reason"] == "empty"]
    graph_off = present and readable and graph["present"] and (
        not graph["readable"] or graph["matches_index"] is False)
    age = _age_seconds(built_at)
    drifted = bool(changed or deleted or moved or added or skipped_indexed)
    older = _built_before(built_at, newest)

    reasons: "list[str]" = []
    if config_problem:
        overall = "error"
        reasons.append(config_problem + ". No source was read, because exclusions that "
                       "cannot be trusted would widen scope; fix the file and run status again.")
    elif not present:
        overall = "missing"
        reasons.append(f"No index at {CONTEXT_DIR}/{builder.INDEX_NAME}; "
                       "run `context-layer index <vault>` before asking for evidence.")
    elif not readable:
        overall = "missing"
        if index_problem:
            reasons.append(f"{CONTEXT_DIR}/{builder.INDEX_NAME}: {index_problem}. Treat it "
                           "as absent until then.")
        else:
            reasons.append(f"{CONTEXT_DIR}/{builder.INDEX_NAME} cannot be opened as a "
                           "SQLite index; treat it as absent and rebuild.")
    elif changed or deleted or moved or skipped_indexed:
        overall = "degraded"
    elif added or symlinks or blocking or graph_off:
        overall = "stale"
    else:
        overall = "ok"

    if present and readable and drifted:
        reasons.append("The index is older than the sources in scope; rebuild with "
                       "`context-layer index <vault>`.")
    if changed:
        reasons.append(f"{len(changed)} indexed source(s) changed on disk; their evidence "
                       "is withheld until a successful rebuild.")
    if deleted:
        reasons.append(f"{len(deleted)} indexed source(s) are gone from scope; requests "
                       "that need them fail closed.")
    if moved:
        reasons.append(f"{len(moved)} indexed source(s) moved to a new path with identical "
                       "bytes; the index still points at the old path.")
    if skipped_indexed:
        reasons.append(f"{len(skipped_indexed)} indexed source(s) can no longer be indexed "
                       f"({_by_reason(skipped_indexed)}): the index still holds their earlier "
                       "bytes, which retrieval re-checks and does not serve; a rebuild drops "
                       "them from the index.")
    if added:
        reasons.append(f"{len(added)} in-scope source(s) are not in the index and cannot "
                       "be retrieved until the next build.")
    if blocking:
        reasons.append(f"{len(blocking)} in-scope file(s) cannot be indexed "
                       f"({_by_reason(blocking)}); no search sees them, and a rebuild does "
                       "not include them until they are fixed.")
    if empty:
        reasons.append(f"{len(empty)} empty in-scope file(s) have nothing to index; they "
                       "are listed under now_skipped and do not change overall.")
    if symlinks:
        reasons.append(f"{len(symlinks)} symlink(s) in scope are unsupported and are never "
                       "indexed; rebuilding will not include them.")
    if present and readable and graph["present"] and not graph["readable"]:
        reasons.append(f"The link graph ({CONTEXT_DIR}/{GRAPH_NAME}) cannot be used: "
                       f"{graph_problem}; `context-layer index <vault>` rebuilds it.")
    elif graph_off:
        reasons.append(f"The link graph ({CONTEXT_DIR}/{GRAPH_NAME}) was built from another "
                       "index generation: its notes or their hashes differ from the index's. "
                       "`context-layer index <vault>` rebuilds both.")
    if older and not drifted:
        reasons.append("A source was touched after the index was built, but every hash "
                       "still matches.")
    if overall == "ok":
        reasons.append(f"The index covers all {len(current)} in-scope source(s) and every "
                       "hash matches.")

    return {
        "schema": SCHEMA,
        "overall": overall,
        "exit_code": EXIT_CODES[overall],
        "reasons": reasons,
        "index": {
            "present": present,
            "readable": readable,
            "manifest": from_manifest,
            "built_at": built_at,
            "age_seconds": age,
            "source_count": source_count,
            "older_than_sources": older,
            "rollback_available": _previous(index).is_file(),
        },
        "graph": graph,
        "changed": changed,
        "deleted": deleted,
        "added": added,
        "moved": moved,
        "now_skipped": now_skipped,
        "excluded_count": excluded_count,
        "symlinks": symlinks,
    }


def render(summary: dict) -> str:
    """One screen of the same facts the JSON carries, vault-relative throughout."""
    index = summary["index"]
    graph = summary["graph"]
    lines = [f"overall: {summary['overall']}"]
    if index["present"]:
        source = "manifest" if index["manifest"] else "index rows (no manifest)"
        lines.append(f"index: present, {index['source_count']} source(s), built "
                     f"{index['built_at'] or 'unknown'} "
                     f"({_human_age(index['age_seconds'])}), read from {source}")
    else:
        lines.append("index: missing")
    if not graph["present"]:
        lines.append("graph: absent")
    elif not graph["readable"]:
        lines.append("graph: present, cannot be read")
    else:
        generation = {True: "same generation as the index",
                      False: "built from another index generation",
                      None: "not compared (no usable index)"}[graph["matches_index"]]
        lines.append(f"graph: present, {graph['notes']} note(s), {graph['edges']} edge(s), "
                     f"built {graph['built_at'] or 'unknown'}, {generation}")
    for reason in summary["reasons"]:
        lines.append(f"- {reason}")
    for key in ("changed", "deleted", "added", "symlinks"):
        for name in summary[key]:
            lines.append(f"{key}: {name}")
    for entry in summary["moved"]:
        lines.append(f"moved: {entry['from']} -> {entry['to']}")
    for entry in summary["now_skipped"]:
        indexed = "; the index holds earlier bytes" if entry["indexed"] else ""
        lines.append(f"now_skipped: {entry['path']} ({entry['reason']}{indexed})")
    lines.append(f"excluded_count: {summary['excluded_count']}")
    lines.append(f"rollback_available: {'yes' if index['rollback_available'] else 'no'}"
                 f" (graph: {'yes' if graph['rollback_available'] else 'no'})")
    return "\n".join(lines)


def _pair_problem(index: Path, graph: Path) -> str:
    """'' when `graph` was built from exactly the notes, and hashes, of `index`."""
    try:
        indexed, _, _ = _from_index(index)
    except (sqlite3.Error, OSError, ValueError):
        return f"cannot be compared with {CONTEXT_DIR}/{index.name}, which does not open"
    notes, _, _, problem = _graph_notes(graph)
    if notes is None:
        return f"cannot be read ({problem})"
    if notes != _markdown(indexed):
        return (f"was not built from {CONTEXT_DIR}/{index.name} (its notes or their "
                "hashes differ)")
    return ""


def rollback(vault: "Path | str", *, dry_run: bool = False, index_only: bool = False) -> dict:
    """Swap the live index (and manifest, and link graph) with the ones the last build replaced.

    The swap is its own undo: the replaced files become the new `.prev`. Each
    rename is atomic on its own; there is no atomic two-file rename, so the
    window between them leaves the restored index in place and a duplicate
    `.prev`, which is the safe direction to fail.

    The link graph must move with the index. When it cannot — a graph without
    a `graph.sqlite.prev`, or a `.prev` graph built from another generation than
    the `.prev` index — the rollback is refused (ValueError), unless
    `index_only` asks to restore the index alone and leave the graph as it is.
    """
    vault = Path(vault).expanduser().resolve()
    if not vault.is_dir():
        raise ValueError(f"vault not found: {vault.name}")
    _, builder = _router_modules()
    index, manifest, _ = _paths(vault)
    previous = _previous(index)
    if not previous.is_file():
        raise ValueError(
            f"no previous index to restore: {CONTEXT_DIR}/{previous.name} does not "
            "exist. It is written by the first rebuild that replaces an existing "
            "index, so a vault indexed only once has nothing to roll back to.")
    graph = index.with_name(GRAPH_NAME)
    previous_graph = _previous(graph)
    alone = ("Run `context-layer rollback <vault> --index-only` to restore the index "
             "alone (status then reports the graph as another generation), or "
             "`context-layer index <vault>` to rebuild both.")
    if not index_only:
        if graph.is_file() and not previous_graph.is_file():
            raise ValueError(
                f"the link graph cannot move with the index: {CONTEXT_DIR}/"
                f"{previous_graph.name} does not exist, so the restored index would be "
                f"paired with a graph of the newer build. {alone}")
        if previous_graph.is_file():
            problem = _pair_problem(previous, previous_graph)
            if problem:
                raise ValueError(f"the link graph cannot move with the index: "
                                 f"{CONTEXT_DIR}/{previous_graph.name} {problem}. {alone}")
    plan = {"restored": f"{CONTEXT_DIR}/{index.name}",
            "from": f"{CONTEXT_DIR}/{previous.name}",
            "manifest_restored": _previous(manifest).is_file(),
            "manifest_dropped": False,
            "graph_restored": not index_only and previous_graph.is_file(),
            "graph_left": graph.is_file() and (index_only or not previous_graph.is_file()),
            "index_only": index_only,
            "dry_run": dry_run}
    if not dry_run:
        live = index.read_bytes() if index.is_file() else None
        builder.replace_atomically(index, previous.read_bytes())
        if live is None:
            previous.unlink(missing_ok=True)
        else:
            builder.replace_atomically(previous, live)
        previous_manifest = _previous(manifest)
        live_manifest = manifest.read_bytes() if manifest.is_file() else None
        if previous_manifest.is_file():
            builder.replace_atomically(manifest, previous_manifest.read_bytes())
            if live_manifest is None:
                previous_manifest.unlink(missing_ok=True)
            else:
                builder.replace_atomically(previous_manifest, live_manifest)
        elif live_manifest is not None:
            # The restored index predates manifests. Keeping the newer manifest
            # would describe bytes the index does not hold, so it is set aside
            # and `status` falls back to reading the index rows.
            builder.replace_atomically(previous_manifest, live_manifest)
            manifest.unlink(missing_ok=True)
            plan["manifest_dropped"] = True
        if plan["graph_restored"]:
            # The link graph is swapped with the index, so the two stay one generation.
            live_graph = graph.read_bytes() if graph.is_file() else None
            builder.replace_atomically(graph, previous_graph.read_bytes())
            if live_graph is None:
                previous_graph.unlink(missing_ok=True)
            else:
                builder.replace_atomically(previous_graph, live_graph)
    return plan


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    if args.rest:
        print(f"context-layer status: unrecognised arguments: {' '.join(args.rest)}",
              file=sys.stderr)
        return 2
    try:
        summary = status_summary(Path(args.vault))
    except ValueError as exc:
        print(f"context-layer status: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(render(summary))
        from . import jev      # the optional advisor: one line, from its config file only
        print(jev.status_line(Path(args.vault)))
    return summary["exit_code"]


def cmd_rollback(args: argparse.Namespace) -> int:
    if args.rest:
        print(f"context-layer rollback: unrecognised arguments: {' '.join(args.rest)}",
              file=sys.stderr)
        return 2
    try:
        plan = rollback(Path(args.vault), dry_run=args.dry_run, index_only=args.index_only)
    except ValueError as exc:
        print(f"context-layer rollback: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"context-layer rollback: {exc.strerror or exc}", file=sys.stderr)
        return 1
    verb = "would restore" if plan["dry_run"] else "restored"
    print(f"{verb} {plan['restored']} from {plan['from']}")
    if plan["manifest_dropped"]:
        print("the newer index manifest was set aside; status falls back to index rows")
    if plan["graph_restored"]:
        print(f"{verb} {CONTEXT_DIR}/{GRAPH_NAME} from {CONTEXT_DIR}/{GRAPH_NAME}.prev")
    elif plan["graph_left"]:
        print(f"{CONTEXT_DIR}/{GRAPH_NAME} was left as it is (--index-only): `status` reports "
              "it as another generation until `context-layer index` rebuilds both")
    if not plan["dry_run"]:
        print("the replaced index is kept as the new .prev; run rollback again to undo")
        print("the restored index is older than the sources; run `status` before trusting it")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    """Attach `status` and `rollback` to the CLI."""
    p_status = sub.add_parser(
        "status",
        help="Report index age, drift, skipped files, exclusions, symlinks and the link graph.",
        description="Compare the index against the vault as it is now. Every "
                    "in-scope file is read and hashed; size and mtime are not "
                    "trusted, because a source can be rewritten with both "
                    "preserved. Files the builder cannot index are listed under "
                    "now_skipped. Exit code: 0 ok, 1 stale or degraded, 2 no "
                    "usable index (missing, unreadable, newer format or an "
                    "inconsistent full-text table) or an untrustworthy "
                    "routes.json (error; nothing is read). Output is "
                    "vault-relative. See docs/cli.md.",
    )
    p_status.add_argument("vault")
    p_status.add_argument("--json", action="store_true",
                          help="Print the machine-readable summary instead of the report.")
    p_status.set_defaults(func=cmd_status, forward_to=None)

    p_rollback = sub.add_parser(
        "rollback",
        help="Restore the index that the last successful rebuild replaced.",
        description="Swap <vault>/.context/index.sqlite with the .prev copy the "
                    "last rebuild kept, and the manifest and link graph (graph.sqlite) "
                    "with it. Refuses when there is no .prev, and when the link graph "
                    "cannot move with the index (no graph.sqlite.prev, or one built "
                    "from another generation) unless --index-only is given. The swap "
                    "is its own undo. The restored index is older than the sources; "
                    "check `status` after it.",
    )
    p_rollback.add_argument("vault")
    p_rollback.add_argument("--dry-run", action="store_true",
                            help="Report what would be restored; write nothing.")
    p_rollback.add_argument("--index-only", action="store_true",
                            help="Restore the index and its manifest only; leave the link "
                                 "graph as it is.")
    p_rollback.set_defaults(func=cmd_rollback, forward_to=None)
