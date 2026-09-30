#!/usr/bin/env python3
"""Regenerate the advisor-annotated activation traces used by tests/advisor-layer.test.js.

    python3 obsidian-plugin/tests/fixtures/make_jev_writer_traces.py          # rewrite the fixtures
    python3 obsidian-plugin/tests/fixtures/make_jev_writer_traces.py --check  # fail on contract drift

The traces come from the real writer (context_layer/jev.py annotating the trace
that a synaptic retrieval wrote), not from hand-written JSON. The script reuses
the fictional "Harbor Lights" vault and the scripted provider of
tests/test_jev.py: no model, no network, HOME in a temporary directory. The
provider says "yes" to one linked note that shares no word with the question and
"no" to the other, so mode `on` rescues one note and mode `shadow` records the
same judgement without applying it.

--check regenerates into a temporary directory and compares everything except
the per-run fields `generated_at` and `run_id` with the committed fixtures.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PER_RUN = ("generated_at", "run_id")
CASES = [("writer-trace-jev-on.json", "on"), ("writer-trace-jev-shadow.json", "shadow")]


def generate(out_dir: Path) -> None:
    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(REPO / "tests"))
    import test_jev as tj  # noqa: E402  (repository test helpers, fictional vault)

    saved_home = os.environ.get("HOME")
    try:
        for name, mode in CASES:
            with tempfile.TemporaryDirectory(prefix="brain-view-jev-") as tmp:
                root = Path(tmp).resolve()
                home, vault = root / "home", root / "vault"
                home.mkdir()
                os.environ["HOME"] = str(home)
                tj.build_vault(vault, tj.HARBOR, home)
                tj.configure(vault, mode)
                provider = tj.Provider(lambda q: tj.answer(0.91 if tj.name_of(q) == "Mira Holt" else 0.08))
                tj.advise(vault, "synaptic", tj.HARBOR_PROMPT, provider)
                shutil.copyfile(vault / ".context" / "activation.json", out_dir / name)
    finally:
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home


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
        for name, _ in CASES:
            print(f"wrote {name}")
        return 0
    with tempfile.TemporaryDirectory(prefix="brain-view-jev-check-") as tmp:
        tmp = Path(tmp)
        generate(tmp)
        drift = [name for name, _ in CASES if comparable(tmp / name) != comparable(HERE / name)]
    if drift:
        print("advisor trace drift: " + ", ".join(drift) + "; review the change, then rerun without --check",
              file=sys.stderr)
        return 1
    print("advisor traces match the committed fixtures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
