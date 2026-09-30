#!/usr/bin/env python3
"""Adapter: naive full-text grep over a vault. The control.

This is the system everyone already has. It has no index, no embeddings, no
ranking model and no configuration. Before you believe that your semantic setup
is earning its complexity, it has to beat this.

It is written to be a *fair* control, not a straw man:

  - the prompt is split into terms and stop words are dropped, because nobody
    greps for "the",
  - a document is ranked by how many distinct query terms it contains (term
    coverage), then by total occurrences, then by name for determinism,
  - the top --top-k documents are returned with their bodies, not just names.

What it cannot do, and this is the point of having it as the control: it cannot
match a synonym, it cannot match a paraphrase, and it cannot tell a passing
mention from the document that owns the answer. If your fancier system does not
beat it, those three abilities are not being delivered.

Usage (the last argv token is the prompt, which is what evaluate.py expects):

    python3 adapters/grep_baseline.py --vault fixtures/docs "release steps?"

    python3 evaluate.py \
      --command "python3 adapters/grep_baseline.py --vault fixtures/docs" \
      --stimuli stimulus-set.example.jsonl

Stdlib only. Read-only. Uses `rg` or `grep` if present, otherwise scans in
Python; the ranking is identical either way, so results do not depend on which
binary you happen to have.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (  # noqa: E402
    configure_stdout, add_common_args, clip, query_terms, read_text, rel, render_packet,
    resolve_vault, walk_vault,
)


def count_with_binary(binary: str, term: str, vault: str, globs: list[str]) -> dict[str, int]:
    """Occurrences of `term` per file, using ripgrep or grep. {} if the tool fails."""
    if binary == "rg":
        argv = ["rg", "--count-matches", "--fixed-strings", "--ignore-case", "--no-messages"]
        for g in globs:
            argv += ["--glob", g]
        argv += ["--", term, vault]
    else:
        argv = ["grep", "-r", "-o", "-i", "-c", "-F", "--", term, vault]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    # exit 1 just means "no match"; anything above that is a real failure.
    if proc.returncode not in (0, 1):
        return {}
    counts: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        path, _, num = line.rpartition(":")
        if not path or not num.isdigit():
            continue
        n = int(num)
        if n == 0:
            continue  # `grep -c` prints a 0 line for every file it did not match
        counts[path] = counts.get(path, 0) + n
    return counts


def count_in_python(term: str, files: list[str], cache: dict[str, str]) -> dict[str, int]:
    counts = {}
    needle = term.lower()
    for path in files:
        if path not in cache:
            cache[path] = read_text(path).lower()
        n = cache[path].count(needle)
        if n:
            counts[path] = n
    return counts


def main() -> int:
    configure_stdout()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(ap)
    ap.add_argument("--engine", choices=["auto", "rg", "grep", "python"], default="auto",
                    help="Which matcher to use (default: auto -> rg, grep, then python).")
    args = ap.parse_args()

    vault = resolve_vault(args.vault)
    prompt = " ".join(args.prompt)
    exts = tuple(e.strip().lower() for e in args.ext.split(",") if e.strip())
    files = walk_vault(vault, exts)
    if not files:
        print(f"adapter error: no files with extensions {args.ext} under {args.vault!r}. "
              f"Check --vault and --ext.", file=sys.stderr)
        return 2

    engine = args.engine
    if engine == "auto":
        engine = "rg" if shutil.which("rg") else ("grep" if shutil.which("grep") else "python")

    terms = query_terms(prompt)
    globs = [f"*{e}" for e in exts]
    allowed = set(files)
    cache: dict[str, str] = {}

    coverage: dict[str, int] = {}
    occurrences: dict[str, int] = {}
    fell_back = False
    for term in terms:
        if engine in ("rg", "grep"):
            counts = count_with_binary(engine, term, vault, globs)
            counts = {p: n for p, n in counts.items() if os.path.abspath(p) in allowed}
            if not counts:
                # The tool is missing or errored, or the term genuinely matches
                # nothing. Re-check in Python rather than silently scoring the
                # term as "matches nothing": a missing binary must not look like
                # a retrieval result.
                py_counts = count_in_python(term, files, cache)
                if py_counts:
                    fell_back = True
                counts = py_counts
        else:
            counts = count_in_python(term, files, cache)
        for path, n in counts.items():
            path = os.path.abspath(path)
            coverage[path] = coverage.get(path, 0) + 1
            occurrences[path] = occurrences.get(path, 0) + n

    ranked = sorted(coverage, key=lambda p: (-coverage[p], -occurrences[p], p))
    chosen = []
    for path in ranked[: args.top_k]:
        chosen.append((
            rel(path, vault),
            f"matched {coverage[path]}/{len(terms)} query terms, "
            f"{occurrences[path]} occurrences",
            clip(read_text(path), args.max_chars),
        ))

    engine_label = engine + (" (fell back to the python scanner for at least one "
                             "term; is the binary on PATH?)" if fell_back else "")
    note = (f"Engine: {engine_label}. Terms searched: {', '.join(terms) if terms else '(none)'}. "
            f"{len(ranked)} of {len(files)} documents matched at least one term.")
    print(render_packet("grep_baseline", prompt, chosen, note))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
