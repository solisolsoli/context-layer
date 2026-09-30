#!/usr/bin/env python3
"""Measure the opt-in name/alias/heading fields on DEV sets (never the sealed benchmark).

    python3 tests/dev_names_eval.py [--json]

Builds the fictional `tests/fixtures/dev_names.py` vault and the `dev_bridge.py` vault,
each indexed twice (default, and `--name-fields`), and runs every question through
`eval/retrieve.py --method fts` with and without `--name-fields`. A case is complete when
every required span occurs verbatim in an evidence item of its source note; an `absent`
case is correct when nothing is returned. est_tokens = ceil(evidence characters / 4).
Columns: default index and search; `--name-fields` search on the name-fields index.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))

from fixtures import dev_bridge, dev_names  # noqa: E402


def search(vault: Path, question: str, *flags: str) -> dict:
    done = subprocess.run([sys.executable, str(REPO / "eval" / "retrieve.py"), "--method", "fts",
                           "--vault", str(vault), *flags, question],
                          capture_output=True, text=True, cwd=REPO)
    return json.loads(done.stdout)


def complete(case: dict, packet: dict) -> bool:
    if case["type"] == "absent":
        return not packet.get("evidence")
    return all(any(e["source_path"] == r["source_path"] and r["text"] in e["content"]
                   for e in packet.get("evidence", [])) for r in case["required"])


def tokens(packet: dict) -> int:
    return math.ceil(sum(len(e["content"]) for e in packet.get("evidence", [])) / 4)


def index(vault: Path, *flags: str) -> None:
    for step in ([] if (vault / ".context").exists() else [["init", str(vault)]]) \
            + [["index", str(vault), *flags]]:
        subprocess.run([sys.executable, "-m", "context_layer.cli", *step], cwd=REPO,
                       capture_output=True, check=True)


def evaluate() -> dict:
    results = {}
    with tempfile.TemporaryDirectory() as temp:
        for label, module in (("dev_names", dev_names), ("dev_bridge", dev_bridge)):
            vault = Path(temp) / label
            cases = module.build(vault)
            if label == "dev_bridge":
                cases = [c for c in cases if c["required"]]
            index(vault)
            default = {c["id"]: search(vault, c["question"]) for c in cases}
            index(vault, "--name-fields")
            named = {c["id"]: search(vault, c["question"], "--name-fields") for c in cases}
            identical_flagless = {c["id"]: search(vault, c["question"]) for c in cases}
            results[label] = [{
                "id": c["id"], "type": c["type"],
                "default": complete(c, default[c["id"]]),
                "name_fields": complete(c, named[c["id"]]),
                "tokens_default": tokens(default[c["id"]]),
                "tokens_name_fields": tokens(named[c["id"]]),
                # the same default search on the name-fields index: must equal the default index's
                "flagless_same": json.dumps({k: v for k, v in default[c["id"]].items()
                                             if k != "coverage"}, sort_keys=True)
                == json.dumps({k: v for k, v in identical_flagless[c["id"]].items()
                               if k != "coverage"}, sort_keys=True),
            } for c in cases]
    return results


def summary(results: dict) -> "list[str]":
    lines = [f"{'set':11s} {'type':9s} {'n':>3s} {'default':>8s} {'--name-fields':>14s} "
             f"{'tok default':>12s} {'tok name-fields':>16s}"]
    for label, rows in results.items():
        for kind in sorted({r["type"] for r in rows}):
            subset = [r for r in rows if r["type"] == kind]
            lines.append(
                f"{label:11s} {kind:9s} {len(subset):3d} "
                f"{sum(r['default'] for r in subset):3d}/{len(subset):<4d} "
                f"{sum(r['name_fields'] for r in subset):5d}/{len(subset):<8d} "
                f"{sum(r['tokens_default'] for r in subset) / len(subset):12.0f} "
                f"{sum(r['tokens_name_fields'] for r in subset) / len(subset):16.0f}")
    same = all(r["flagless_same"] for rows in results.values() for r in rows)
    lines.append(f"default search identical on both index kinds (coverage receipt aside): {same}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    results = evaluate()
    if args.json:
        print(json.dumps(results, indent=1))
    print("\n".join(summary(results)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
