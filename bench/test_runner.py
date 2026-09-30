#!/usr/bin/env python3
"""Runner tests: python3 -m unittest bench/test_runner.py

The first class runs the real runner on two sealed cases (S01, B01) with a tiny
equal budget. It checks plumbing only (budget flags applied and bounded, flags
reported, provenance and TREC files written); it never looks at which method
completed a case. The other classes are pure unit tests with no search at all.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH))
import run_offline as ro  # noqa: E402

BUDGET = 50
PROVENANCE = ("python", "sqlite", "unicode", "platform", "scorer_sha256")


def runner(*argv, out=None):
    home = tempfile.mkdtemp(prefix="cl-bench-home-")
    env = dict(os.environ, HOME=home)
    env.pop("CONTEXT_LAYER_HOME", None)
    command = [sys.executable, str(BENCH / "run_offline.py"), *argv]
    if out is not None:
        command += ["--out", str(out)]
    return subprocess.run(command, capture_output=True, text=True, env=env, timeout=600)


def flags_section(summary: str) -> dict[str, list[str]]:
    """`- `arm`: `flags`` lines of the SUMMARY's "Flags passed to `search`" section."""
    section = summary.split("## Flags passed to `search`", 1)[1].split("\n## ", 1)[0]
    return {m.group(1): m.group(2).split() for m in re.finditer(r"^- `([^`]+)`: `([^`]*)`$",
                                                                section, re.M)}


class EqualBudgetRun(unittest.TestCase):
    """`--budget-tokens 50 --only S01,B01`: every packet fits 50 est. tokens (200 chars)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="cl-bench-runner-")
        cls.out = Path(cls.tmp.name)
        cls.done = runner("--only", "S01,B01", "--budget-tokens", str(BUDGET), out=cls.out)
        cls.summary = (cls.out / "SUMMARY.md").read_text(encoding="utf-8") \
            if (cls.out / "SUMMARY.md").is_file() else ""

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def arms(self):
        return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in self.out.glob("*.json")}

    def test_exit_zero(self):
        self.assertEqual(self.done.returncode, 0, self.done.stderr[-2000:])

    def test_every_row_fits_the_budget(self):
        arms = self.arms()
        self.assertEqual(sorted(arms), ["fts", "grep", "synaptic", "synaptic-extra"])
        for label, data in arms.items():
            self.assertEqual(len(data["rows"]), 2, label)
            for row in data["rows"]:
                self.assertIsNone(row["error"], (label, row["id"]))
                self.assertLessEqual(row["packet_chars"], BUDGET * 4, (label, row["id"]))
                self.assertLessEqual(row["est_tokens"], BUDGET, (label, row["id"]))

    def test_summary_lists_only_the_applied_flags(self):
        e = ro.default_extra_tokens(BUDGET)
        expected = {"grep": ["--budget", str(BUDGET * 4)],
                    "fts": ["--budget", str(BUDGET * 4)],
                    "synaptic": ["--compact", "--budget-tokens", str(BUDGET)],
                    "synaptic-extra": ["--budget", str((BUDGET - e) * 4), "--extra-tokens", str(e)]}
        listed = flags_section(self.summary)
        self.assertEqual(listed, expected)
        for label in ("grep", "fts"):
            self.assertFalse(set(listed[label]) & set(ro.SYNAPTIC_ONLY), label)
        self.assertIn("## Equal packet budget", self.summary)
        self.assertNotIn("FAILED", self.summary)
        rows = [line for line in self.summary.splitlines() if line.startswith("| `")
                and line.endswith("| ok |")]
        self.assertEqual(len(rows), 4, self.summary)

    def test_budget_mapping_is_recorded_in_the_results(self):
        for label, data in self.arms().items():
            budget = data["budget"]
            self.assertEqual(budget["tokens"], BUDGET)
            self.assertEqual(budget["check"], "ok")
            self.assertEqual(budget["arms"][label]["bound_est_tokens"], BUDGET)
            self.assertLessEqual(budget["arms"][label]["largest_est_tokens"], BUDGET)

    def test_results_carry_provenance(self):
        for label, data in self.arms().items():
            for key in PROVENANCE:
                self.assertTrue(data.get(key), (label, key))
            self.assertRegex(data["scorer_sha256"], r"^[0-9a-f]{64}$")
        line = next(l for l in self.summary.splitlines() if l.startswith("Checkout: "))
        self.assertIn("scorer `", line)
        self.assertNotIn(str(Path.home()), line)

    def test_trec_files_parse(self):
        qrels = (self.out / "qrels.txt").read_text(encoding="utf-8").splitlines()
        self.assertTrue(qrels)
        for line in qrels:
            qid, zero, doc, rel = line.split()
            self.assertIn(qid, ("S01", "B01"))
            self.assertEqual(zero, "0")
            self.assertIn(int(rel), (0, 1))
            self.assertNotIn(" ", doc)
        for label in ("grep", "fts", "synaptic", "synaptic-extra"):
            lines = (self.out / f"{label}.run").read_text(encoding="utf-8").splitlines()
            self.assertTrue(lines, label)
            last = {}
            for line in lines:
                qid, q0, doc, rank, score, tag = line.split()
                self.assertEqual(q0, "Q0")
                self.assertEqual(tag, label)
                self.assertEqual(int(rank), last.get(qid, 0) + 1)
                last[qid] = int(rank)
                float(score)


class Refusals(unittest.TestCase):
    """Flags a method would not apply never reach `search` (no search is run)."""

    def test_synaptic_only_flag_for_fts_is_refused(self):
        done = runner("--methods", "grep,fts,synaptic", "--search-arg=--compact")
        self.assertEqual(done.returncode, 2)
        self.assertIn("--compact applies only to --method synaptic", done.stderr)

    def test_budget_tokens_without_compact_is_refused(self):
        done = runner("--methods", "synaptic", "--search-arg=--budget-tokens=300")
        self.assertEqual(done.returncode, 2)
        self.assertIn("--budget-tokens sizes only the --compact", done.stderr)

    def test_forwarded_budget_flags_clash_with_the_equal_budget(self):
        done = runner("--budget-tokens", "50", "--search-arg=--budget=100")
        self.assertEqual(done.returncode, 2)
        self.assertIn("do not also forward --budget", done.stderr)

    def test_extra_tokens_needs_room_inside_the_budget(self):
        self.assertEqual(runner("--budget-tokens", "50", "--extra-tokens", "50").returncode, 2)
        self.assertEqual(runner("--extra-tokens", "10").returncode, 2)

    def test_unknown_case_id_is_refused(self):
        done = runner("--only", "S01,NOPE")
        self.assertEqual(done.returncode, 2)
        self.assertIn("NOPE", done.stderr)


class Planning(unittest.TestCase):
    class Args:
        top_k = None
        search_arg = None
        budget_tokens = None
        extra_tokens = None

    def test_default_run_passes_no_flags(self):
        arms = ro.plan_arms(["grep", "fts", "synaptic"], self.Args())
        self.assertEqual([(a["label"], a["flags"], a["bound"]) for a in arms],
                         [("grep", [], None), ("fts", [], None), ("synaptic", [], None)])

    def test_mapping_and_extra_share(self):
        args = self.Args()
        args.budget_tokens = 400
        arms = {a["label"]: a for a in ro.plan_arms(["grep", "synaptic"], args)}
        self.assertEqual(arms["grep"]["flags"], ["--budget", "1600"])
        self.assertEqual(arms["synaptic"]["flags"], ["--compact", "--budget-tokens", "400"])
        e = ro.default_extra_tokens(400)
        self.assertEqual(e, 114)            # round(400 x 600 / 2100)
        self.assertEqual(arms["synaptic-extra"]["flags"],
                         ["--budget", str((400 - e) * 4), "--extra-tokens", str(e)])
        self.assertTrue(all(a["bound"] == 400 for a in arms.values()))

    def test_check_flags_a_packet_over_its_bound(self):
        arms = [{"label": "fts", "bound": 50, "budget_flags": ["--budget", "200"]}]
        results = {"fts": {"aggregate": {"max_est_tokens": 51}}}
        self.assertEqual(len(ro.budget_check(arms, results)), 1)
        results["fts"]["aggregate"]["max_est_tokens"] = 50
        self.assertEqual(ro.budget_check(arms, results), [])


class PilotSelection(unittest.TestCase):
    def test_select_pilot_reproduces_the_committed_selection(self):
        import select_pilot
        if not select_pilot.SELECTION.is_file():
            self.skipTest("the pilot's files are not shipped in the distributions")
        self.assertEqual(select_pilot.main(["--check"]), 0)


class CliRobustness(unittest.TestCase):
    """Works with a CLI that ignores an inapplicable flag and with one that refuses it."""

    def fake(self, returncode, stdout="", stderr=""):
        original = ro.search
        ro.search = lambda *a, **k: subprocess.CompletedProcess([], returncode, stdout, stderr)
        self.addCleanup(setattr, ro, "search", original)

    def test_refused_flags_stop_the_run_loudly(self):
        self.fake(2, stderr="usage: retrieve.py\nretrieve.py: error: --compact needs --method synaptic")
        arm = {"method": "fts", "flags": ["--budget", "200"]}
        with self.assertRaises(SystemExit) as caught:
            ro.probe_arm(Path("."), arm, "q")
        self.assertIn("refused --budget 200", str(caught.exception))

    def test_unknown_method_is_skipped_with_a_note(self):
        self.fake(2, stderr="error: argument --method: invalid choice: 'nope'")
        note = ro.probe_arm(Path("."), {"method": "nope", "flags": []}, "q")
        self.assertIn("skipped", note)

    def test_a_packet_means_the_arm_runs(self):
        self.fake(0, stdout=json.dumps({"schema": "evidence-delivery-v1", "evidence": []}))
        self.assertIsNone(ro.probe_arm(Path("."), {"method": "fts", "flags": []}, "q"))


if __name__ == "__main__":
    unittest.main()
