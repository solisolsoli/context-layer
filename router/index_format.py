"""Format version and consistency checks for `.context/index.sqlite`.

One place for the rules every reader of the lexical index applies before it
trusts a search result: `build_index.py` stamps the format and runs the FTS5
integrity check before the staged index replaces the live one; `status`, the
CLI/MCP/hook search path and `context_router.py` call `check_index()` on open.

Why a consistency check at all: the index uses an FTS5 *external content*
table (`records_fts` over `records`). SQLite leaves keeping the two in step to
the application, and an emptied full-text table (for example after
`INSERT INTO records_fts(records_fts) VALUES('delete-all')`) still answers every
query, with no rows: a search would report NOT_FOUND as a clean success. The
cheap check compares the number of stored records with the number of rows the
full-text index holds (`records_fts_docsize`, one row per indexed record); the
full check runs FTS5's own `integrity-check` against a private in-memory copy,
so the reader never opens the index for writing.

Limits, stated plainly: the cheap check catches a missing, emptied or
partially emptied full-text table; it does not prove that each full-text row
matches its record's text. The full check does, and `status` runs it.

Python 3.10+; standard library only.
"""
from __future__ import annotations

import hashlib
import sqlite3

# Version of the index layout this code writes and reads. Stored twice: as
# `PRAGMA user_version` (readable without knowing the schema) and as
# index_meta `format_version`. An index from 0.2 or earlier has user_version 0
# and is read as legacy version 1: its layout is identical.
INDEX_FORMAT_VERSION = 1
# graph.sqlite: graph_meta `schema_version` written by context_layer/graph.py.
GRAPH_FORMAT_VERSION = 1

REBUILD = "rebuild it with `context-layer index <vault>`"


class IndexFormatError(ValueError):
    """The index cannot be trusted as it is; the message says what to run."""


def file_digest(path) -> str:
    """Fresh SHA-256 of a generation file, streamed without holding it in memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def stamp(connection: sqlite3.Connection) -> None:
    """Record the format version in a staging index before it is committed."""
    connection.execute(f"PRAGMA user_version = {INDEX_FORMAT_VERSION}")
    connection.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                       ("format_version", str(INDEX_FORMAT_VERSION)))


def check_format(connection: sqlite3.Connection, label: str = "index") -> int:
    """Refuse an index written by a newer layout. Returns the version read."""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > INDEX_FORMAT_VERSION:
        raise IndexFormatError(
            f"the {label} has format version {version}, but this context-layer reads only "
            f"up to {INDEX_FORMAT_VERSION}; upgrade context-layer, or {REBUILD}")
    return version or INDEX_FORMAT_VERSION


def check_consistency(connection: sqlite3.Connection, label: str = "index") -> None:
    """Cheap per-open check: stored records and full-text rows must agree in number."""
    try:
        records = connection.execute("SELECT count(*) FROM records").fetchone()[0]
    except sqlite3.Error as exc:
        raise IndexFormatError(f"the {label} has no readable records table ({exc}); "
                               f"{REBUILD}") from None
    try:
        indexed = connection.execute("SELECT count(*) FROM records_fts_docsize").fetchone()[0]
        connection.execute("SELECT rowid FROM records_fts LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        raise IndexFormatError(f"the {label} has no usable full-text table ({exc}); "
                               f"{REBUILD}") from None
    if records != indexed:
        raise IndexFormatError(
            f"the {label} is inconsistent: {records} stored record(s) but {indexed} "
            f"full-text row(s), so a search could miss text silently; {REBUILD}")


def integrity_check(connection: sqlite3.Connection, label: str = "index") -> None:
    """FTS5 `integrity-check` (rank 1: also compare with the content table).

    Needs a writable connection: call it on the staging index at build time,
    or through `full_check()` on a private copy.
    """
    try:
        connection.execute(
            "INSERT INTO records_fts(records_fts, rank) VALUES('integrity-check', 1)")
    except sqlite3.Error as exc:
        raise IndexFormatError(f"the {label} failed the FTS5 integrity check ({exc}); "
                               f"{REBUILD}") from None


def check_index(connection: sqlite3.Connection, label: str = "index") -> int:
    """Format and cheap consistency check for a read-only reader. Returns the version."""
    version = check_format(connection, label)
    check_consistency(connection, label)
    return version


def full_check(connection: sqlite3.Connection, label: str = "index") -> int:
    """check_index() plus the FTS5 integrity check, run on an in-memory copy.

    Costs memory equal to the index size; `status` uses it because it already
    reads every in-scope source.
    """
    version = check_index(connection, label)
    copy = sqlite3.connect(":memory:")
    try:
        connection.backup(copy)
        integrity_check(copy, label)
    finally:
        copy.close()
    return version


def open_checked(path, label: str = "index") -> sqlite3.Connection:
    """Open an index read-only and run check_index(); the caller closes it."""
    from pathlib import Path
    path = Path(path)
    if not path.is_file():
        raise IndexFormatError(f"no {label} at .context/{path.name}; "
                               "run `context-layer index <vault>` first")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        check_index(connection, label)
    except BaseException:
        connection.close()
        raise
    return connection


# The opt-in name/alias/heading table written by `build_index.py --name-fields`.
NAME_TABLE = "names_fts"
# bm25 column weights: file name, frontmatter aliases, headings (lower bm25 is better,
# and FTS5 weights multiply the column's contribution).
NAME_WEIGHTS = (10.0, 8.0, 4.0)
# Reciprocal-rank-fusion constant (Cormack et al., 2009).
RRF_K = 60


def has_name_fields(connection: sqlite3.Connection) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_master WHERE name = ?",
                              (NAME_TABLE,)).fetchone() is not None


def merge_name_hits(connection: sqlite3.Connection, expression: str,
                    ranked: "list[str]") -> "list[str]":
    """`ranked` (paths, best first) fused with the note-name ranking by reciprocal rank.

    The name ranking is every note whose name, aliases or headings match any query term
    (`expression`, the same OR expression the content search uses), ordered by bm25 with
    NAME_WEIGHTS and then by path. Each list gives a note 1 / (RRF_K + position); the
    result is ordered by the sum, then by path, so it is deterministic. A note only one
    list finds is kept. The weights and RRF_K are untuned defaults, not measured optima.
    """
    weights = ", ".join(str(weight) for weight in NAME_WEIGHTS)
    named = [row[0] for row in connection.execute(
        f"SELECT path FROM {NAME_TABLE} WHERE {NAME_TABLE} MATCH ? "
        f"ORDER BY bm25({NAME_TABLE}, {weights}), path", (expression,))]
    score: "dict[str, float]" = {}
    for listing in (ranked, named):
        for position, name in enumerate(listing, 1):
            score[name] = score.get(name, 0.0) + 1.0 / (RRF_K + position)
    return sorted(score, key=lambda name: (-score[name], name))


def check_graph_meta(meta: dict, label: str = "link graph") -> None:
    """Refuse a graph.sqlite written by a newer layout (graph_meta schema_version)."""
    raw = meta.get("schema_version", "1")
    try:
        version = int(raw)
    except (TypeError, ValueError):
        raise IndexFormatError(f"the {label} has an unreadable schema_version {raw!r}; "
                               f"{REBUILD}") from None
    if version > GRAPH_FORMAT_VERSION:
        raise IndexFormatError(
            f"the {label} has format version {version}, but this context-layer reads only "
            f"up to {GRAPH_FORMAT_VERSION}; upgrade context-layer, or {REBUILD}")


def check_graph_file(path, label: str = "link graph") -> None:
    """Read-only format check of graph.sqlite; a missing file is not an error here."""
    from pathlib import Path
    path = Path(path)
    if not path.is_file():
        return
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        try:
            meta = dict(connection.execute("SELECT key, value FROM graph_meta"))
        except sqlite3.Error as exc:
            raise IndexFormatError(f"the {label} cannot be read ({exc}); {REBUILD}") from None
        check_graph_meta(meta, label)
    finally:
        connection.close()
