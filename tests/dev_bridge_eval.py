#!/usr/bin/env python3
"""Run the synaptic DEV set (tests/fixtures/dev_bridge.py) and print completeness by type.

Development aid, not a benchmark: the dev set was written alongside the code it
measures. A case is complete when every required span occurs verbatim in an
evidence item from its source_path. est_tokens = ceil(evidence characters / 4).

    python3 tests/dev_bridge_eval.py [--methods fts synaptic compact] [--json]
        [--delivery window|prefix|focus] [--hook] [--extra-paragraph]

`--hook` scores the prompt hook's additionalContext instead of the packet (a case is
complete when every required span occurs in it; the size reported is its mean length in
characters). `--extra-paragraph` appends one paragraph of invented filler words under a
new heading to every note (notes about twice as long; the labels still hold).

`synaptic` is the default synaptic mode (the fts packet plus graph extras);
`compact` is `--method synaptic --compact`.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import random
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


def hook_text(packet: dict, method: str) -> str:
    sys.path.insert(0, str(REPO))
    from context_layer import mcp_server
    context, _, _ = mcp_server.hook_context(packet, "fts" if method == "fts" else "synaptic")
    return context or ""


def add_paragraph(vault: Path) -> None:
    rng = random.Random(3)
    syllables = ["vel", "mor", "qui", "stan", "ob", "rek", "tal", "zun", "phy", "dra", "kel"]
    for path in sorted(vault.rglob("*.md")):
        words = " ".join("".join(rng.choice(syllables) for _ in range(rng.randint(2, 3)))
                         for _ in range(30))
        text = path.read_text(encoding="utf-8").rstrip("\n")
        path.write_bytes((text + "\n\n## Notes\n\n" + words.capitalize() + ".\n").encode())


def complete(packet: dict, required: list[dict]) -> bool:
    return all(any(e["source_path"] == r["source_path"] and r["text"] in e["content"]
                   for e in packet.get("evidence", [])) for r in required)


def evaluate(methods: list[str], extra: list[str], delivery: str | None = None,
             hook: bool = False, paragraph: bool = False) -> dict:
    with tempfile.TemporaryDirectory() as temp:
        vault = Path(temp) / "dev-vault"
        cases = dev_bridge.build(vault)
        if paragraph:
            add_paragraph(vault)
        for step in (["init", str(vault)], ["index", str(vault)]):
            subprocess.run([sys.executable, "-m", "context_layer.cli", *step], cwd=REPO,
                           capture_output=True, check=True)
        results: dict = {}
        for method in methods:
            rows = []
            for case in cases:
                flags = (extra if method != "fts" else []) + (
                    ["--delivery", delivery] if delivery and method != "compact" else [])
                packet = run(vault, method, case["question"], flags)
                if hook:
                    text = hook_text(packet, method)
                    rows.append({"id": case["id"], "type": case["type"],
                                 "complete": all(r["text"] in text for r in case["required"]),
                                 "chars": len(text)})
                    continue
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
            unit, key = ("chars", "chars") if "chars" in rows[0] else ("tok", "est_tokens")
            mean = sum(r[key] for r in subset) / max(len(subset), 1)
            parts.append(f"{kind} {done}/{len(subset)} (~{mean:.0f} {unit})")
        total = sum(r[key] for r in rows) / max(len(rows), 1)
        done = sum(r["complete"] for r in rows)
        lines.append(f"{method:9s} " + " | ".join(parts)
                     + f" | all {done}/{len(rows)} | mean ~{total:.0f} {unit}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--methods", nargs="+", default=["fts", "synaptic", "compact"],
                        choices=sorted(MODES))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--delivery", choices=["window", "prefix", "focus"])
    parser.add_argument("--hook", action="store_true")
    parser.add_argument("--extra-paragraph", action="store_true")
    parser.add_argument("extra", nargs="*", help="extra flags for synaptic, after --")
    args = parser.parse_args()
    results = evaluate(args.methods, args.extra, args.delivery, args.hook,
                       args.extra_paragraph)
    if args.json:
        print(json.dumps(results, indent=1))
    print("\n".join(summary(results)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
