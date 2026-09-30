#!/usr/bin/env python3
"""Recreate the case selection of the 0.3 live pilot (eval/live-pilot-0.3/selection.txt).

    python3 bench/select_pilot.py            # print the selection
    python3 bench/select_pilot.py --check    # compare with the committed selection.txt

The pilot asked 24 of the 72 sealed questions. They were drawn with one seeded
generator, `random.Random(20260924)`, one `sample(pool, quota)` call per case
type in this order, each pool holding that type's cases in file order:

    bridge 7, aggregation 5, single-hop 4, supersession 3, distractor 2, unanswerable 3

selection.txt lists each type's drawn ids sorted, types in the same order.

This script was written after the pilot, from the committed ids and the seed
line in selection.txt; `--check` shows that it reproduces them exactly. It reads
only case ids and types, never questions or gold passages.

Python 3.10+; standard library only.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import random
import sys

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parent
SELECTION = REPO / "eval" / "live-pilot-0.3" / "selection.txt"
SEED = 20260924
QUOTA = (("bridge_2hop", 7), ("multi_note_aggregation", 5), ("single_hop", 4),
         ("supersession", 3), ("distractor", 2), ("unanswerable", 3))
HEADER = "seed=20260924 quota bridge7 agg5 single4 super3 distr2 unans3"

sys.path.insert(0, str(BENCH))
import seal  # noqa: E402


def select(cases: list[dict]) -> list[str]:
    rng = random.Random(SEED)
    chosen: list[str] = []
    for case_type, quota in QUOTA:
        pool = [c["id"] for c in cases if c["type"] == case_type]
        chosen += sorted(rng.sample(pool, quota))
    return chosen


def selection_text(ids: list[str]) -> str:
    return HEADER + "\n" + ",".join(ids) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="Exit 1 unless the committed selection.txt matches.")
    args = parser.parse_args(argv)
    text = selection_text(select(seal.load_cases()))
    if args.check:
        if not SELECTION.is_file():
            print("select_pilot: eval/live-pilot-0.3/selection.txt is not here (the pilot's "
                  "files are in the repository, not in the distributions)", file=sys.stderr)
            return 2
        committed = SELECTION.read_text(encoding="utf-8")
        if committed.strip() != text.strip():
            print("select_pilot: selection.txt differs from the seeded draw", file=sys.stderr)
            return 1
        print("select_pilot: selection.txt matches the seeded draw")
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
