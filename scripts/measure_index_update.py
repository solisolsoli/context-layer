#!/usr/bin/env python3
"""Time a full index build against an incremental update on a synthetic vault.

    python3 scripts/measure_index_update.py --notes 3000 --words 2000 5000

The vault is generated (fictional words, deterministic seed) in a temporary
directory; nothing outside it is read or written. Each scenario edits the vault
and times `router/build_index.py` in a fresh process with `--full` and with the
default update, on copies of the same starting index, then checks that both
produce the same records (ids, paths, hashes; the edited copies differ in timestamp). Numbers are wall-clock seconds on this machine: they
show the shape (what the update saves), not a promise for another vault.
"""
from __future__ import annotations

import argparse
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BUILDER = REPO / "router" / "build_index.py"


def make_vault(root: Path, notes: int, low: int, high: int) -> None:
    rng = random.Random(7)
    words = [f"w{n}" for n in range(8000)]
    for number in range(notes):
        folder = root / f"folder{number % 40}"
        folder.mkdir(parents=True, exist_ok=True)
        body = " ".join(rng.choice(words) for _ in range(rng.randint(low, high)))
        (folder / f"note{number:05d}.md").write_text(f"# Note {number}\n\n{body}\n", encoding="utf-8")
    (root / ".context").mkdir()


def build(vault: Path, *flags: str) -> float:
    started = time.perf_counter()
    subprocess.run([sys.executable, str(BUILDER), "--vault", str(vault), *flags],
                   check=True, capture_output=True)
    return time.perf_counter() - started


def rows(vault: Path) -> list:
    connection = sqlite3.connect(vault / ".context" / "index.sqlite")
    try:
        return connection.execute(
            "SELECT id, source_path, source_sha256, locator, content_sha256 FROM records"
            " ORDER BY id").fetchall()
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--notes", type=int, default=3000)
    parser.add_argument("--words", type=int, nargs=2, default=[2000, 5000], metavar=("LOW", "HIGH"),
                        help="words per note, drawn uniformly (default 2000 5000)")
    args = parser.parse_args()
    scenarios = {
        "nothing changed": lambda v: None,
        "1 note edited (same chunk count)": lambda v: (v / "folder5" / "note00005.md").write_text(
            (v / "folder5" / "note00005.md").read_text(encoding="utf-8").replace("w1", "w2", 1), encoding="utf-8"),
        "50 notes edited (same chunk count)": lambda v: [
            (v / f"folder{n % 40}" / f"note{n:05d}.md").write_text(
                (v / f"folder{n % 40}" / f"note{n:05d}.md").read_text(encoding="utf-8").replace("w1", "w2", 1),
                encoding="utf-8") for n in range(100, 100 + 50 * 7, 7)],
        "1 note appended to (last note)": lambda v: (v / "folder39" / f"note{args.notes - 1:05d}.md")
        .open("a", encoding="utf-8").write("\nappended line\n"),
        "1 note added at the end": lambda v: (v / "zz-new.md").write_text("new note\n", encoding="utf-8"),
        "1 note added at the start": lambda v: (v / "aa-new.md").write_text("new note\n", encoding="utf-8"),
        "1 note deleted (middle)": lambda v: (v / "folder10" / "note00010.md").unlink(),
    }
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp) / "base"
        make_vault(base, args.notes, *args.words)
        size = sum(p.stat().st_size for p in base.rglob("*.md")) / 1e6
        print(f"{args.notes} notes, {size:.1f} MB of text; first full build "
              f"{build(base, '--full'):.2f}s\n")
        print(f"{'scenario':38s} {'--full':>8s} {'update':>8s}  same records")
        for name, edit in scenarios.items():
            timings = {}
            results = {}
            for label, flags in (("full", ("--full",)), ("update", ())):
                work = Path(temp) / label
                if work.exists():
                    shutil.rmtree(work)
                shutil.copytree(base, work, symlinks=True)
                edit(work)
                timings[label] = build(work, *flags)
                results[label] = rows(work)
            print(f"{name:38s} {timings['full']:7.2f}s {timings['update']:7.2f}s  "
                  f"{results['full'] == results['update']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
