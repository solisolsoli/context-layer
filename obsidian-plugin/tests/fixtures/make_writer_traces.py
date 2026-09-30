#!/usr/bin/env python3
"""Regenerate the writer-produced activation traces used by tests/contract.test.js.

    python3 obsidian-plugin/tests/fixtures/make_writer_traces.py          # rewrite the fixtures
    python3 obsidian-plugin/tests/fixtures/make_writer_traces.py --check  # fail on contract drift

The traces come from the real writer, not from hand-written JSON: the script
builds the fictional development vault (tests/fixtures/dev_bridge.py) in a
temporary directory, runs `context-layer init`, `index` and two synaptic
searches through `python3 -m context_layer.cli`, and copies each
`.context/activation.json` byte for byte. HOME points at a temporary directory,
no model is involved and nothing outside the temporary directory is read or
written except the fixture files next to this script.

--check regenerates into a temporary directory and compares everything except
the per-run fields `generated_at` and `run_id` with the committed fixtures, so
a change in the writer's trace format shows up as a failing check.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PER_RUN = ("generated_at", "run_id")
# (fixture file, search flags, prompt). The first is a two-hop bridge question
# over the dev vault; the second matches nothing.
CASES = [
    ("writer-trace-depth2.json", ["--method", "synaptic", "--max-hops=2"],
     "In which town does the lead of Project Lantern reside?"),
    ("writer-trace-not-found.json", ["--method", "synaptic"],
     "zzqx flurbl quintessence"),
]


def run_cli(args, env):
    done = subprocess.run([sys.executable, "-m", "context_layer.cli", *args], cwd=REPO, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if done.returncode != 0:
        sys.exit(f"context-layer {args[0]} failed ({done.returncode}): {done.stderr.strip()[:400]}")
    return done.stdout


def generate(out_dir: Path) -> None:
    sys.path.insert(0, str(REPO / "tests" / "fixtures"))
    import dev_bridge  # noqa: E402  (repository dev set, fictional)

    with tempfile.TemporaryDirectory(prefix="brain-view-trace-") as tmp:
        tmp = Path(tmp)
        vault, home = tmp / "vault", tmp / "home"
        home.mkdir()
        dev_bridge.build(vault)
        env = dict(os.environ, HOME=str(home), PYTHONPATH=str(REPO))
        run_cli(["init", str(vault)], env)
        run_cli(["index", str(vault)], env)
        for name, flags, prompt in CASES:
            run_cli(["search", str(vault), *flags, "--prompt", prompt], env)
            shutil.copyfile(vault / ".context" / "activation.json", out_dir / name)


def comparable(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in PER_RUN:
        data.pop(key, None)
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="compare a fresh run with the committed fixtures")
    args = parser.parse_args()
    if not args.check:
        generate(HERE)
        for name, _, _ in CASES:
            print(f"wrote {name}")
        return 0
    with tempfile.TemporaryDirectory(prefix="brain-view-check-") as tmp:
        tmp = Path(tmp)
        generate(tmp)
        drift = [name for name, _, _ in CASES if comparable(tmp / name) != comparable(HERE / name)]
    if drift:
        print("writer trace drift: " + ", ".join(drift) + "; review the change, then rerun without --check",
              file=sys.stderr)
        return 1
    print("writer traces match the committed fixtures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
