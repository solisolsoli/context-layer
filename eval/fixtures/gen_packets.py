#!/usr/bin/env python3
"""Generate one packet file per rubric case, so score_packet.py --batch-dir has
something real to score.

    python3 fixtures/gen_packets.py --cases cases.example.json \
        --command "python3 fixtures/demo_router.py" --out-dir packets

The COMMAND contract is the same as evaluate.py's: the prompt is appended as
the final argv token. Files are written as <out-dir>/packet-<CASEID>.md, which
is the name score_packet.py looks for.
"""
import argparse
import json
import shlex
import subprocess
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", required=True)
    ap.add_argument("--command", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--timeout", type=float, default=90.0)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    parts = shlex.split(args.command)

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))["cases"]
    for case in cases:
        proc = subprocess.run(parts + [case["prompt"]], capture_output=True,
                              text=True, timeout=args.timeout)
        target = out_dir / f"packet-{case['id']}.md"
        target.write_text(proc.stdout, encoding="utf-8")
        print(f"{case['id']}: rc={proc.returncode} {len(proc.stdout):>7d} chars -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
