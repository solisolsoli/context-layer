#!/usr/bin/env python3
"""Adapter: SQLite FTS5 full-text search over a vault. The "good enough" baseline.

FTS5 ships inside the Python standard library's sqlite3 on most builds, so this
is a real ranked full-text index with no dependency, no service and no model.
Documents are ranked by BM25, which means a term that appears in every note
counts for little and a rare term counts for a lot -- the one thing plain grep
cannot do.

This is the baseline a vector setup actually has to beat. "Better than grep" is
not an achievement; "better than BM25" is a claim worth measuring.

Index lifecycle: the index is built on first run into --index (default:
`.eval-fts-index-<vault digest>.sqlite3` in the current directory, one file per
vault, which `.gitignore` already ignores). The index records which vault it was
built from and a digest of every indexed file's path, size and modification
time; it is rebuilt whenever that record differs from the vault on disk (a
different vault, an added, deleted, changed or restored file), or when you pass
--rebuild. So one index path shared by two vaults never answers for the wrong
one. The vault itself is never written to.

Usage:

    python3 adapters/fts_sqlite.py --vault fixtures/docs "release steps?"

    python3 evaluate.py \
      --command "python3 adapters/fts_sqlite.py --vault fixtures/docs" \
      --stimuli stimulus-set.example.jsonl

Stdlib only. No network.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (  # noqa: E402
    configure_stdout, add_common_args, clip, query_terms, read_text, rel, render_packet,
    resolve_vault, walk_vault,
)


def require_fts5() -> None:
    try:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        con.close()
    except sqlite3.OperationalError as exc:
        sys.exit(
            "adapter error: this Python's sqlite3 was built without FTS5, so this "
            f"adapter cannot run here ({exc}).\n"
            "Options: use a Python whose sqlite3 has FTS5 (most system and "
            "python.org builds do), or measure adapters/grep_baseline.py instead.\n"
            "Nothing was written and no packet was produced -- an empty packet "
            "would have looked like a retrieval failure rather than a setup problem."
        )


def vault_key(vault: str) -> str:
    """Short digest of the vault's real path, for the default per-vault index name."""
    return hashlib.sha256(os.path.realpath(vault).encode("utf-8")).hexdigest()[:12]


def files_digest(vault: str, files: list[str]) -> str:
    """Digest of (relative path, size, mtime in ns) for every indexed file."""
    digest = hashlib.sha256()
    for f in files:
        stat = os.stat(f)
        digest.update(f"{rel(f, vault)}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode("utf-8"))
    return digest.hexdigest()


def index_meta(index_path: str) -> dict[str, str]:
    """The vault and file digest an existing index was built from ({} if unreadable)."""
    try:
        con = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
        try:
            return dict(con.execute("SELECT key, value FROM meta").fetchall())
        finally:
            con.close()
    except sqlite3.Error:
        return {}


def build_index(index_path: str, vault: str, files: list[str], digest: str) -> None:
    tmp = index_path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.execute("CREATE VIRTUAL TABLE docs USING fts5(path, body, tokenize='porter unicode61')")
    con.executemany("INSERT INTO docs(path, body) VALUES (?, ?)",
                    ((rel(f, vault), read_text(f)) for f in files))
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.executemany("INSERT INTO meta(key, value) VALUES (?, ?)",
                    (("vault", os.path.realpath(vault)), ("files_sha256", digest)))
    con.commit()
    con.close()
    os.replace(tmp, index_path)


def fts_query(terms: list[str]) -> str:
    """Terms -> an FTS5 MATCH expression.

    Every term is double-quoted so that punctuation, hyphens and FTS5 operator
    words ("AND", "NEAR", "*") in a user's prompt are treated as text rather than
    as syntax. Terms are OR-ed: a document that contains more of them scores
    higher under BM25 anyway, and AND would return nothing for most real prompts.
    """
    quoted = ['"' + t.replace('"', '""') + '"' for t in terms]
    return " OR ".join(quoted)


def main() -> int:
    configure_stdout()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(ap)
    ap.add_argument("--index", default=None,
                    help="Where to keep the FTS index (default: "
                         ".eval-fts-index-<vault digest>.sqlite3 in the current directory).")
    ap.add_argument("--rebuild", action="store_true", help="Rebuild the index before querying.")
    args = ap.parse_args()

    require_fts5()
    vault = resolve_vault(args.vault)
    prompt = " ".join(args.prompt)
    exts = tuple(e.strip().lower() for e in args.ext.split(",") if e.strip())
    files = walk_vault(vault, exts)
    if not files:
        print(f"adapter error: no files with extensions {args.ext} under {args.vault!r}. "
              f"Check --vault and --ext.", file=sys.stderr)
        return 2

    index_path = os.path.abspath(args.index or f".eval-fts-index-{vault_key(vault)}.sqlite3")
    digest = files_digest(vault, files)
    meta = {} if args.rebuild or not os.path.exists(index_path) else index_meta(index_path)
    if meta.get("vault") != os.path.realpath(vault) or meta.get("files_sha256") != digest:
        build_index(index_path, vault, files, digest)
        built = "built"
    else:
        built = "reused"

    con = sqlite3.connect(index_path)
    terms = query_terms(prompt)
    rows = []
    if terms:
        try:
            rows = con.execute(
                "SELECT path, body, bm25(docs) AS score FROM docs "
                "WHERE docs MATCH ? ORDER BY score LIMIT ?",
                (fts_query(terms), args.top_k),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            con.close()
            sys.exit(f"adapter error: FTS query failed for prompt {prompt!r}: {exc}")
    total = con.execute("SELECT count(*) FROM docs").fetchone()[0]
    con.close()

    chosen = [(path, f"bm25 score {score:.3f} (lower is better)", clip(body, args.max_chars))
              for path, body, score in rows]
    note = (f"Index: {built}, {total} documents, tokenizer porter/unicode61. "
            f"Terms searched: {', '.join(terms) if terms else '(none)'}.")
    print(render_packet("fts_sqlite", prompt, chosen, note))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
