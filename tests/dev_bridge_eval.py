#!/usr/bin/env python3
"""Run the synaptic DEV set (tests/fixtures/dev_bridge.py) and print completeness by type.

Development aid, not a benchmark: the dev set was written alongside the code it
measures. A case is complete when every required span occurs verbatim in an
evidence item from its source_path. est_tokens = ceil(evidence characters / 4).

    python3 tests/dev_bridge_eval.py [--methods fts synaptic compact] [--json]

`synaptic` is the default synaptic mode (the fts packet plus graph extras);
`compact` is `--method synaptic --compact`.
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

from fixtures import dev_bridge  # noqa: E402


MODES = {"fts": ["--method", "fts"], "synaptic": ["--method", "synaptic"],
         "compact": ["--method", "synaptic", "--compact"]}


def run(vault: Path, method: str, question: str, extra: list[str]) -> dict:
    done = subprocess.run([sys.executable, str(REPO / "eval" / "retrieve.py"), *MODES[method],
                           "--vault", str(vault), *extra, question],
                          capture_output=True, text=True, cwd=REPO)
    return json.loads(done.stdout)


def complete(packet: dict, required: list[dict]) -> bool:
    return all(any(e["source_path"] == r["source_path"] and r["text"] in e["content"]
                   for e in packet.get("evidence", [])) for r in required)


def evaluate(methods: list[str], extra: list[str]) -> dict:
    with tempfile.TemporaryDirectory() as temp:
        vault = Path(temp) / "dev-vault"
        cases = dev_bridge.build(vault)
        for step in (["init", str(vault)], ["index", str(vault)]):
            subprocess.run([sys.executable, "-m", "context_layer.cli", *step], cwd=REPO,
                           capture_output=True, check=True)
        results: dict = {}
        for method in methods:
            rows = []
            for case in cases:
                packet = run(vault, method, case["question"],
                             extra if method != "fts" else [])
                chars = sum(len(e["content"]) for e in packet.get("evidence", []))
                rows.append({"id": case["id"], "type": case["type"],
                             "complete": complete(packet, case["required"]),
                             "est_tokens": math.ceil(chars / 4)})
            results[method] = rows
        return results


def summary(results: dict) -> list[str]:
    lines = []
    for method, rows in results.items():
        parts = []
        for kind in ("direct", "bridge", "prose", "aggregate"):
            subset = [r for r in rows if r["type"] == kind]
            done = sum(r["complete"] for r in subset)
            mean = sum(r["est_tokens"] for r in subset) / max(len(subset), 1)
            parts.append(f"{kind} {done}/{len(subset)} (~{mean:.0f} tok)")
        total = sum(r["est_tokens"] for r in rows) / max(len(rows), 1)
        lines.append(f"{method:9s} " + " | ".join(parts) + f" | mean ~{total:.0f} tok")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--methods", nargs="+", default=["fts", "synaptic", "compact"],
                        choices=sorted(MODES))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("extra", nargs="*", help="extra flags for synaptic, after --")
    args = parser.parse_args()
    results = evaluate(args.methods, args.extra)
    if args.json:
        print(json.dumps(results, indent=1))
    print("\n".join(summary(results)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
