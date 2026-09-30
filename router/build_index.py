#!/usr/bin/env python3
"""Build a read-only lexical (SQLite FTS5) index over a Markdown vault.

This builder never writes to, moves or alters any vault content. It reads files
and writes one SQLite file. The originals stay exactly as they are; the index
only records where their text is and what its hash was at build time.

Python 3.10+; standard library only; no network access.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import shutil
import tempfile
import os
import re
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Iterator, NamedTuple

try:
    from . import index_format, textfold
    from .source_policy import (DEFAULT_MAX_FILE_BYTES, ConfigError, config_exclusions,
                                config_max_file_bytes, load_config, source_path,
                                walked_name_state)
except ImportError:
    import index_format
    import textfold
    from source_policy import (DEFAULT_MAX_FILE_BYTES, ConfigError, config_exclusions,
                               config_max_file_bytes, load_config, source_path,
                               walked_name_state)

DEFAULT_EXTENSIONS = [".md", ".txt", ".json", ".jsonl", ".csv"]
DEFAULT_SKIP_PARTS = [".git", "node_modules", "__pycache__", ".obsidian",
                      ".context", ".context-runs", ".trash"]
# The default per-file limit; routes.json `max_file_bytes` overrides it (up to
# source_policy.MAX_FILE_BYTES_CAP). A larger file is skipped and reported.
MAX_FILE_BYTES = DEFAULT_MAX_FILE_BYTES
CHUNK = 6000
# index_meta key holding the chunk size a build used. An incremental update reuses the
# rows of notes whose bytes did not change, so it needs the same chunking.
CHUNK_META = "chunk_size"
# An incremental update that would rewrite more than this share of the records is
# slower than the bulk full build, so it falls back to that.
MAX_REWRITE_SHARE = 0.4
# Why a file in scope was not indexed. Every skip is printed and listed in the
# manifest; the counts are also stored in the index (index_meta skipped_by_reason),
# where each search's coverage receipt reads them.
SKIP_REASONS = ("oversize", "unreadable", "unsupported_name", "not_utf8")
SKIPS_SHOWN = 20
# The index this build replaces is kept next to it, and the plain-text manifest
# records what the index covers. context_layer.health reads both.
INDEX_NAME = "index.sqlite"
MANIFEST_NAME = "index-manifest.json"
PREV_SUFFIX = ".prev"

SCHEMA = """
CREATE TABLE records (
    id INTEGER PRIMARY KEY,
    source_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    source_format TEXT NOT NULL,
    record_type TEXT NOT NULL,
    locator TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    role TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    content TEXT NOT NULL
);
CREATE INDEX records_source_path ON records(source_path);
CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE VIRTUAL TABLE records_fts USING fts5(
    content, source_path UNINDEXED, content='records', content_rowid='id',
    tokenize='%s'
);
""" % textfold.TOKENIZER

# Opt-in second full-text table (`--name-fields`): one row per covered file with its name
# (file stem), the aliases of its frontmatter and its Markdown headings, as three
# columns, so a query can be weighted toward them. Off by default; the records and the
# default search never read it. index_meta `name_fields` is "1" when it exists.
NAME_TABLE = "names_fts"
NAME_META = "name_fields"
NAME_SCHEMA = ("CREATE VIRTUAL TABLE names_fts USING fts5(\n"
               "    path UNINDEXED, name, aliases, headings, tokenize='%s'\n);\n"
               % textfold.TOKENIZER)
# Bounds for the headings column of one note.
MAX_HEADINGS = 100
MAX_HEADING_CHARS = 4000


def chunks(text: str) -> "list[str]":
    """Split on a line boundary near the chunk size, so records stay readable."""
    if len(text) <= CHUNK:
        return [text]
    out = []
    start = 0
    while start < len(text):
        end = min(len(text), start + CHUNK)
        if end < len(text):
            window = text.rfind("\n", start + CHUNK // 2, end)
            if window > start:
                end = window
        out.append(text[start:end])
        start = end
    return out


def iter_files(vault: Path, extensions: "set[str]", skip_parts: "set[str]",
               skip_prefixes: "tuple[str, ...]",
               unsupported: "list[str] | None" = None) -> "list[Path]":
    """In-scope files, sorted. A file whose name the source policy cannot accept
    (a backslash, or ':' in the first path component; both legal on POSIX) is
    appended to `unsupported` instead: it could never be read back as a source."""
    found = []
    for current, directories, names in os.walk(vault, followlinks=False):
        base = Path(current)
        # A directory with an unsupported name is still walked, so its files are
        # listed; tool, dot and excluded directories are pruned unread.
        directories[:] = [d for d in directories if d not in skip_parts
            and not (base / d).is_symlink()
            and walked_name_state((base / d).relative_to(vault).as_posix(),
                                  skip_prefixes) != "excluded"]
        for name in sorted(names):
            path = base / name
            relative = path.relative_to(vault).as_posix()
            if path.suffix.lower() not in extensions or path.is_symlink():
                continue
            state = walked_name_state(relative, skip_prefixes)
            if state == "excluded":
                continue
            if state == "unsupported":
                if unsupported is not None and path.is_file():
                    unsupported.append(relative)
                continue
            try:
                path = source_path(vault, relative, skip_prefixes)
            except ValueError:
                continue    # a parent became a symlink during the walk: never followed
            if path.is_file():
                found.append(path)
    return sorted(found)


def skip_report(skipped: "list[dict[str, str]]", counts: "dict[str, int]",
                manifest_name: str, max_bytes: int) -> "list[str]":
    """Lines naming what was not indexed: a count by reason, then the first paths."""
    total = len(skipped)
    lines = [f"skipped {total} file{'s' if total != 1 else ''} ("
             + ", ".join(f"{count} {reason}" for reason, count in counts.items() if count)
             + f"), listed in {manifest_name}:"]
    for entry in skipped[:SKIPS_SHOWN]:
        detail = f" ({entry['size']} bytes; max_file_bytes is {max_bytes})" \
            if entry["reason"] == "oversize" else ""
        lines.append(f"  {entry['path']}: {entry['reason']}{detail}")
    if total > SKIPS_SHOWN:
        lines.append(f"  ... and {total - SKIPS_SHOWN} more")
    return lines



def display(path: Path, vault: Path) -> str:
    """Vault-relative name when inside the vault, else the bare file name: output
    and logs never carry an absolute home path."""
    try:
        return path.resolve().relative_to(vault).as_posix()
    except ValueError:
        return path.name


def replace_atomically(target: Path, data: bytes) -> None:
    """Write data to target through a staging file in the same directory."""
    handle = tempfile.NamedTemporaryFile(prefix=".staging-", dir=target.parent, delete=False)
    staging = Path(handle.name)
    try:
        handle.write(data)
        handle.close()
        staging.replace(target)
    finally:
        staging.unlink(missing_ok=True)


def keep_previous(path: Path) -> None:
    """Copy path to path + .prev, so `context-layer rollback` has a restore point."""
    if not path.is_file():
        return
    replace_atomically(path.with_name(path.name + PREV_SUFFIX), path.read_bytes())


class Source(NamedTuple):
    """One file the index covers. `text` is None when the previous index already
    holds the rows for these exact bytes (an incremental update, sha256 unchanged)."""
    path: str
    suffix: str
    sha: str
    size: int
    mtime: str
    text: "str | None"


def iter_sources(vault: Path, extensions: "set[str]", skip_prefixes: "tuple[str, ...]",
                 max_bytes: int, skipped: "list[dict[str, object]]",
                 known: "dict[str, str] | None" = None) -> "Iterator[Source]":
    """Read every in-scope file once and yield the covered sources in index order.
    Files it cannot cover are appended to `skipped`, which is complete and sorted once
    the generator is exhausted. This is the only place that decides what an index
    covers, so a full build and an incremental update cannot disagree.

    `known` maps a path to the sha256 the previous index holds. A file whose bytes
    still hash to that value is not decoded again (its text was valid UTF-8 then).
    """
    unsupported: "list[str]" = []
    for path in iter_files(vault, extensions, set(DEFAULT_SKIP_PARTS), skip_prefixes,
                           unsupported):
        rel = str(path.relative_to(vault)).replace("\\", "/")
        try:
            stat = path.stat()
        except OSError:
            skipped.append({"path": rel, "reason": "unreadable"})
            continue
        size = stat.st_size
        if size == 0:
            continue                      # nothing to find in it
        if size > max_bytes:
            skipped.append({"path": rel, "reason": "oversize", "size": size})
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            skipped.append({"path": rel, "reason": "unreadable"})
            continue
        digest = hashlib.sha256(raw).hexdigest()
        text = None
        if known is None or known.get(rel) != digest:
            try:
                text = raw.decode("utf-8")
            except UnicodeError:
                skipped.append({"path": rel, "reason": "not_utf8"})
                continue
        mtime = datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat()
        yield Source(rel, path.suffix.lower(), digest, size, mtime, text)
    skipped += [{"path": name, "reason": "unsupported_name"} for name in unsupported]
    skipped.sort(key=lambda entry: (str(entry["path"]), str(entry["reason"])))


def skip_counts(skipped: "list[dict[str, object]]") -> "dict[str, int]":
    return {reason: sum(1 for entry in skipped if entry["reason"] == reason)
            for reason in SKIP_REASONS}


RECORD_COLUMNS = ("source_path, source_sha256, source_format, record_type, locator, "
                  "timestamp, role, content_sha256, content")
TIMESTAMP_AT = 5          # position of `timestamp` in RECORD_COLUMNS


def frontmatter_aliases(text: str) -> "list[str]":
    """Values of `aliases:`/`alias:` in a leading `---` block: an inline list, a scalar or
    a `- item` list. Only that subset of YAML is read; anything else is ignored."""
    lines = text.lstrip("\ufeff").split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        return []
    body = []
    for line in lines[1:]:
        if line.rstrip("\r") in ("---", "..."):
            break
        body.append(line.rstrip("\r"))
    else:
        return []                                 # never closed: not a frontmatter block

    def clean(value: str) -> str:
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value.startswith("[[") and value.endswith("]]"):
            value = value[2:-2].split("|")[-1].split("#")[0]
        return value.strip()

    found: "list[str]" = []
    position = 0
    while position < len(body):
        head = re.match(r"(aliases|alias)[ \t]*:[ \t]*(.*)$", body[position], re.IGNORECASE)
        position += 1
        if head is None:
            continue
        rest = head.group(2).strip()
        if rest.startswith("["):
            close = rest.rfind("]")
            found += [clean(part) for part in
                      rest[1:close if close > 0 else len(rest)].split(",")]
        elif rest:
            found.append(clean(rest))
        else:
            while position < len(body) and re.match(r"[ \t]*-[ \t]+", body[position]):
                found.append(clean(re.sub(r"^[ \t]*-[ \t]+", "", body[position])))
                position += 1
    return [alias for alias in found if alias]


def name_row(source: Source) -> "tuple[str, str, str, str]":
    """(path, name, aliases, headings) of one source, for the names table."""
    assert source.text is not None
    aliases = headings = ""
    if source.suffix == ".md":
        aliases = "\n".join(frontmatter_aliases(source.text))
        titles = [title for _, _, title in textfold.headings(source.text)[:MAX_HEADINGS]]
        headings = "\n".join(titles)[:MAX_HEADING_CHARS]
    return (source.path, Path(source.path).stem, aliases, headings)


def source_rows(source: Source) -> "list[tuple]":
    """The records of one source, in chunk order (the columns of RECORD_COLUMNS)."""
    assert source.text is not None
    return [(source.path, source.sha, source.suffix, "verbatim_text_file",
             f"chunk {index}", source.mtime, "",
             hashlib.sha256(chunk.encode("utf-8")).hexdigest(), chunk)
            for index, chunk in enumerate(chunks(source.text))]


def write_meta(connection: sqlite3.Connection, built_at: str, vault: Path, files: int,
               rows: int, max_bytes: int, counts: "dict[str, int]",
               name_fields: bool = False) -> None:
    """index_meta and the format stamp: the same rows for either kind of build."""
    for key, value in (
        ("built_at", built_at),
        ("vault", str(vault)),
        ("files", str(files)),
        ("records", str(rows)),
        ("max_file_bytes", str(max_bytes)),
        ("skipped_by_reason", json.dumps(counts)),
        (CHUNK_META, str(CHUNK)),
    ) + (((NAME_META, "1"),) if name_fields else ()):
        connection.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES (?,?)",
                           (key, value))
    index_format.stamp(connection)


def manifest_bytes(built_at: str, sources: "list[Source]", max_bytes: int,
                   skipped: "list[dict[str, object]]") -> bytes:
    entries = [{"path": s.path, "sha256": s.sha, "size": s.size, "mtime": s.mtime}
               for s in sources]
    return (json.dumps(
        {"built_at": built_at, "source_count": len(sources), "sources": entries,
         "max_file_bytes": max_bytes, "skipped": skipped},
        indent=2, ensure_ascii=False) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Incremental update
# ---------------------------------------------------------------------------

class NeedsFullRebuild(Exception):
    """The previous index cannot be updated in place; the message says why."""


class TooManyRewrites(NeedsFullRebuild):
    """The update would rewrite so much that a full build is faster. The previous index
    itself is sound, so its stored text can feed that full build."""


class BaseNote(NamedTuple):
    sha: str
    count: int
    first: int
    last: int
    stamps: "tuple[str, str]"


def read_base(out: Path, name_fields: bool = False) -> "dict[str, BaseNote]":
    """The notes the previous index holds, by path, after the checks that make
    reusing its rows safe. Anything doubtful raises NeedsFullRebuild: an update in
    place starts only from an index this builder would have written itself."""
    if not out.is_file():
        raise NeedsFullRebuild("")
    connection = None
    try:
        connection = sqlite3.connect(out.resolve().as_uri() + "?mode=ro", uri=True)
        if connection.execute("PRAGMA user_version").fetchone()[0] \
                != index_format.INDEX_FORMAT_VERSION:
            raise NeedsFullRebuild("the previous index has another format version")
        index_format.check_index(connection, "previous index")
        meta = dict(connection.execute("SELECT key, value FROM index_meta"))
        if meta.get("format_version") != str(index_format.INDEX_FORMAT_VERSION):
            raise NeedsFullRebuild("the previous index has another format version")
        if meta.get(CHUNK_META) != str(CHUNK):
            raise NeedsFullRebuild("the previous index was written with another chunk "
                                   "size, or before incremental updates existed")
        fresh = sqlite3.connect(":memory:")
        try:
            fresh.executescript(SCHEMA + (NAME_SCHEMA if name_fields else ""))
            wanted = dict(fresh.execute(
                "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"))
        finally:
            fresh.close()
        found = dict(connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"))
        if (NAME_TABLE in found) != name_fields or (meta.get(NAME_META) == "1") != name_fields:
            raise NeedsFullRebuild("the previous index was built "
                                   + ("without" if name_fields else "with") + " --name-fields")
        if any(found.get(name) != sql for name, sql in wanted.items()):
            raise NeedsFullRebuild("the previous index has another layout or tokenizer")
        notes: "dict[str, BaseNote]" = {}
        for path, shas, sha, count, first, last, low, high in connection.execute(
                "SELECT source_path, count(DISTINCT source_sha256), min(source_sha256),"
                " count(*), min(id), max(id), min(timestamp), max(timestamp)"
                " FROM records GROUP BY source_path"):
            if shas != 1 or last - first + 1 != count:
                raise NeedsFullRebuild("the previous index rows of one note are not one "
                                       "contiguous, single-version block")
            notes[path] = BaseNote(sha, count, first, last, (low, high))
        return notes
    except (sqlite3.Error, index_format.IndexFormatError) as exc:
        raise NeedsFullRebuild(f"the previous index is not usable ({exc})") from None
    finally:
        if connection is not None:
            connection.close()


class Update(NamedTuple):
    changed: int
    added: int
    removed: int
    unchanged: int
    rewritten: int          # records deleted and written again (changed, added, moved ids)


def apply_update(staging: Path, out: Path, base: "dict[str, BaseNote]",
                 sources: "list[Source]", built_at: str, vault: Path, max_bytes: int,
                 counts: "dict[str, int]", max_share: float,
                 name_fields: bool = False) -> Update:
    """Turn a copy of the previous index into the index a full build would write.

    Record ids stay those of a full build (1..N in index order, chunks in order),
    because equal-scoring hits are ordered by id: a note whose id range moves is
    written again at its new range (its stored rows are reused; its file is not
    decoded again), and a note whose bytes changed is written from its new text.
    Untouched notes cost nothing, so an append to the last note or an edit that
    keeps its chunk count rewrites one note; an insert near the start of a big
    vault moves every later note, and past `max_share` of the records that is
    slower than a full build, which then runs instead (TooManyRewrites).
    """
    next_id = 1
    stamp_only: "list[Source]" = []
    moved: "list[tuple[Source, BaseNote, int]]" = []
    written: "list[tuple[Source, BaseNote | None, int, list[tuple]]]" = []
    for source in sources:
        old = base.get(source.path)
        if old is not None and old.sha == source.sha:
            if old.first != next_id:
                moved.append((source, old, next_id))
            elif old.stamps != (source.mtime, source.mtime):
                stamp_only.append(source)
            next_id += old.count
        else:
            rows = source_rows(source)
            written.append((source, old, next_id, rows))
            next_id += len(rows)
    total = next_id - 1
    live = {source.path for source in sources}
    removed = [old for path, old in base.items() if path not in live]
    rewritten = sum(old.count for _, old, _ in moved) + sum(len(w[3]) for w in written)
    if total and rewritten > max_share * total:
        raise TooManyRewrites(f"the update would rewrite {rewritten} of {total} records "
                              "(a full build is faster)")

    shutil.copyfile(out, staging)
    connection = sqlite3.connect(staging)
    try:
        connection.execute("BEGIN")
        dropped = [(old.first, old.last) for old in removed]
        dropped += [(old.first, old.last) for _, old, _ in moved]
        dropped += [(old.first, old.last) for _, old, _, _ in written if old is not None]
        held: "dict[str, list[tuple]]" = {}
        for source, old, _ in moved:
            held[source.path] = [
                row[:TIMESTAMP_AT] + (source.mtime,) + row[TIMESTAMP_AT + 1:]
                for row in connection.execute(
                    f"SELECT {RECORD_COLUMNS} FROM records WHERE id BETWEEN ? AND ?"
                    " ORDER BY id", (old.first, old.last))]
        for low, high in dropped:
            connection.executemany(
                "INSERT INTO records_fts(records_fts, rowid, content, source_path)"
                " VALUES('delete', ?, ?, ?)",
                connection.execute("SELECT id, content, source_path FROM records"
                                   " WHERE id BETWEEN ? AND ?", (low, high)).fetchall())
            connection.execute("DELETE FROM records WHERE id BETWEEN ? AND ?", (low, high))
        blocks = [(next_first, held[source.path]) for source, _, next_first in moved]
        blocks += [(first, rows) for _, _, first, rows in written]
        for first, rows in blocks:
            connection.executemany(
                f"INSERT INTO records (id, {RECORD_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(first + offset,) + row for offset, row in enumerate(rows)])
            connection.executemany(
                "INSERT INTO records_fts(rowid, content, source_path) VALUES (?,?,?)",
                [(first + offset, row[8], row[0]) for offset, row in enumerate(rows)])
        for source in stamp_only:
            connection.execute("UPDATE records SET timestamp = ? WHERE source_path = ?",
                               (source.mtime, source.path))
        if name_fields:
            gone = [path for path in base if path not in live]
            gone += [source.path for source, old, _, _ in written if old is not None]
            connection.executemany(f"DELETE FROM {NAME_TABLE} WHERE path = ?",
                                   [(path,) for path in gone])
            connection.executemany(
                f"INSERT INTO {NAME_TABLE} (path, name, aliases, headings) VALUES (?,?,?,?)",
                [name_row(source) for source, _, _, _ in written])
        write_meta(connection, built_at, vault, len(sources), total, max_bytes, counts,
                   name_fields)
        connection.commit()
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
        if connection.execute("PRAGMA freelist_count").fetchone()[0] > pages // 5:
            connection.execute("VACUUM")          # give back the pages the deletes freed
    finally:
        connection.close()
    unchanged = len(sources) - len(written)
    return Update(changed=sum(1 for w in written if w[1] is not None),
                  added=sum(1 for w in written if w[1] is None), removed=len(removed),
                  unchanged=unchanged, rewritten=rewritten)


def collect_sources(vault: Path, extensions: "set[str]", skip_prefixes: "tuple[str, ...]",
                    max_bytes: int, skipped: "list[dict[str, object]]",
                    known: "dict[str, str]", text_budget: "int | None") -> "list[Source]":
    """The sources for an update: text is held only for notes that changed. A scan that
    has already met more changed text than `text_budget` bytes stops (TooManyRewrites),
    so a rewrite of the whole vault does not also hold the whole vault in memory."""
    sources = []
    held = 0
    for source in iter_sources(vault, extensions, skip_prefixes, max_bytes, skipped, known):
        if source.text is not None:
            held += source.size
            if text_budget is not None and held > text_budget:
                raise TooManyRewrites(f"the changed notes are more than "
                                      f"{MAX_REWRITE_SHARE:.0%} of the index size")
        sources.append(source)
    return sources


def stored_text(out: Path, base: "dict[str, BaseNote]",
                sources: "list[Source]") -> "Iterator[Source]":
    """`sources` with the text of each unchanged note read back from the previous index,
    one note at a time, and checked against the note's sha256: a full build that starts
    from these rows must still equal one that starts from the files."""
    connection = sqlite3.connect(out.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        for source in sources:
            if source.text is None:
                note = base[source.path]
                text = "".join(row[0] for row in connection.execute(
                    "SELECT content FROM records WHERE id BETWEEN ? AND ? ORDER BY id",
                    (note.first, note.last)))
                if hashlib.sha256(text.encode("utf-8")).hexdigest() != source.sha:
                    raise NeedsFullRebuild("the previous index text does not match its hash")
                source = source._replace(text=text)
            yield source
    finally:
        connection.close()


def stage_full(staging: Path, sources: "Iterator[Source]", skipped: "list[dict[str, object]]",
               vault: Path, max_bytes: int, built_at: str,
               name_fields: bool = False) -> "tuple[list[Source], int]":
    """Write a complete new index into `staging`, one source at a time. Returns the
    sources (without their text) and the record count; `skipped` is complete after this."""
    connection = sqlite3.connect(staging)
    kept: "list[Source]" = []
    try:
        connection.executescript(SCHEMA + (NAME_SCHEMA if name_fields else ""))
        rows = 0
        for source in sources:
            if name_fields:
                connection.execute(
                    f"INSERT INTO {NAME_TABLE} (path, name, aliases, headings) VALUES (?,?,?,?)",
                    name_row(source))
            batch = source_rows(source)
            connection.executemany(
                f"INSERT INTO records ({RECORD_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?)", batch)
            rows += len(batch)
            kept.append(source._replace(text=None))
        connection.execute("INSERT INTO records_fts(records_fts) VALUES('rebuild')")
        write_meta(connection, built_at, vault, len(kept), rows, max_bytes,
                   skip_counts(skipped), name_fields)
        connection.commit()
    finally:
        connection.close()
    return kept, rows


def check_staged(staging: Path) -> None:
    """The staged index must pass FTS5's own integrity check and the reader's
    consistency check before it may replace the live one."""
    connection = sqlite3.connect(staging)
    try:
        index_format.integrity_check(connection, "new index")
        index_format.check_index(connection, "new index")
    finally:
        connection.close()


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_index.py",
        description="Build the SQLite/FTS5 lexical index that context_router.py reads.",
    )
    parser.add_argument("--vault", type=Path, required=True, help="Root of the Markdown vault.")
    parser.add_argument("--out", type=Path, default=None,
                        help="Index path (default: <vault>/.context/index.sqlite).")
    parser.add_argument("--config", type=Path, default=None,
                        help="Routing config JSON; its `exclude_prefixes` are honoured "
                             "(default: <vault>/.context/routes.json).")
    parser.add_argument("--extensions", nargs="*", default=DEFAULT_EXTENSIONS,
                        help=f"File extensions to index (default: {' '.join(DEFAULT_EXTENSIONS)}).")
    parser.add_argument("--name-fields", action="store_true",
                        help="Also index each file's name, frontmatter aliases and headings "
                             "as separate full-text fields, for `search --name-fields`. Off "
                             "by default; changing it rebuilds in full.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--incremental", action="store_true",
                      help="Update the existing index for the notes whose bytes changed, "
                           "were added or removed, when that is possible (the default; a "
                           "full build runs otherwise, and says why).")
    mode.add_argument("--full", action="store_true",
                      help="Always rebuild the whole index from the sources.")
    args = parser.parse_args(argv)

    vault = args.vault.resolve()
    if not vault.is_dir():
        print(f"build_index error: vault not found: {args.vault}", file=sys.stderr)
        return 1
    out = args.out or (vault / ".context" / INDEX_NAME)
    config_path = args.config or (vault / ".context" / "routes.json")

    # A missing config means no exclusions; a config that is present but
    # unusable (bad JSON, duplicate keys, a string where a list belongs) stops
    # the build rather than indexing what it meant to exclude.
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"build_index error: {exc}", file=sys.stderr)
        return 1
    skip_prefixes: "tuple[str, ...]" = config_exclusions(config)
    max_bytes = config_max_file_bytes(config)
    extensions = {e if e.startswith(".") else "." + e for e in (x.lower() for x in args.extensions)}

    out.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as resources:
        handle = resources.enter_context(tempfile.NamedTemporaryFile(
            prefix=".index-", suffix=".sqlite", dir=out.parent, delete=False))
        staging = Path(handle.name)
        handle.close()
        resources.callback(lambda: staging.unlink(missing_ok=True))
        built_at = datetime.now().astimezone().isoformat()
        update: "Update | None" = None
        fallback = ""
        sources: "list[Source]" = []
        skipped: "list[dict[str, object]]" = []
        stream: "Iterator[Source] | None" = None
        rows = 0
        if not args.full:
            scanned = False
            try:
                base = read_base(out, args.name_fields)
                budget = None if MAX_REWRITE_SHARE >= 1 else int(MAX_REWRITE_SHARE * out.stat().st_size)
                sources = collect_sources(vault, extensions, skip_prefixes, max_bytes, skipped,
                                          {path: note.sha for path, note in base.items()}, budget)
                scanned = True
                update = apply_update(staging, out, base, sources, built_at, vault,
                                      max_bytes, skip_counts(skipped), MAX_REWRITE_SHARE,
                                      args.name_fields)
                check_staged(staging)
            except TooManyRewrites as exc:
                update, fallback = None, str(exc)
                if scanned:                 # the scan is done: the stored text saves a second one
                    stream = stored_text(out, base, sources)
            except (NeedsFullRebuild, index_format.IndexFormatError, sqlite3.Error) as exc:
                update, fallback = None, str(exc)
        if update is None:
            if stream is None:
                skipped = []
                stream = iter_sources(vault, extensions, skip_prefixes, max_bytes, skipped)
            staging.write_bytes(b"")
            try:
                sources, rows = stage_full(staging, stream, skipped, vault, max_bytes, built_at,
                                           args.name_fields)
            except NeedsFullRebuild as exc:
                fallback = str(exc)
                skipped = []
                staging.write_bytes(b"")
                sources, rows = stage_full(
                    staging, iter_sources(vault, extensions, skip_prefixes, max_bytes, skipped),
                    skipped, vault, max_bytes, built_at, args.name_fields)
            try:
                check_staged(staging)
            except index_format.IndexFormatError as exc:
                print(f"build_index error: {exc}", file=sys.stderr)
                return 1
        else:
            connection = sqlite3.connect(staging)
            try:
                rows = connection.execute("SELECT count(*) FROM records").fetchone()[0]
            finally:
                connection.close()
        counts = skip_counts(skipped)
        # Nothing above this line touches the live index, so a failed build
        # leaves the previous index and its manifest exactly as they were.
        manifest = out.with_name(MANIFEST_NAME)
        keep_previous(out)
        staging.replace(out)
        keep_previous(manifest)
        replace_atomically(manifest, manifest_bytes(built_at, sources, max_bytes, skipped))
        files = len(sources)
        print(f"indexed {files} files, {rows} records -> {display(out, vault)}")
        if update is not None:
            print(f"incremental: {update.changed} changed, {update.added} added, "
                  f"{update.removed} removed, {update.unchanged} unchanged "
                  f"({update.rewritten} records rewritten)")
        elif fallback:
            print(f"full rebuild: {fallback}")
        if skipped:
            print("\n".join(skip_report(skipped, counts, display(manifest, vault), max_bytes)))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
