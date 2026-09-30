"""Measurement harness regressions: the live comparison runner, driven by a fake host.

Every test owns a disposable synthetic vault and output directory under a
temporary directory. The real `claude` binary is never called: `--claude` points
at a fake script that reads its own argv and prints canned host JSON. HOME is
redirected to a throwaway directory so nothing touches a real host config.
"""
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(os.environ.get("TEST_REPO_HOME", Path(__file__).resolve().parents[1]))
RUNNER = REPO / "eval" / "live_compare.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("live_compare_under_test", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

CASES = [
    {"id": "H01", "prompt": "H01 what is the retention window?", "answerable": True,
     "expected_sources": ["notes/alpha.md"],
     "required_passages": [{"source_path": "notes/alpha.md", "text": "ninety days"}],
     "answer_gist": "ninety days", "category": "policy", "split": "heldout"},
    {"id": "H02", "prompt": "H02 what is the vacation policy?", "answerable": False,
     "expected_sources": [], "required_passages": [],
     "answer_gist": "not in the vault", "category": "absent", "split": "heldout"},
    {"id": "H03", "prompt": "H03 who is the escalation contact?", "answerable": True,
     "expected_sources": ["notes/beta.md"],
     "required_passages": [{"source_path": "notes/beta.md", "text": "escalation"}],
     "answer_gist": "the duty reviewer", "category": "process", "split": "heldout"},
    {"id": "H04", "prompt": "H04 which note names the index format?", "answerable": True,
     "expected_sources": ["notes/alpha.md"],
     "required_passages": [{"source_path": "notes/alpha.md", "text": "index"}],
     "answer_gist": "sqlite", "category": "policy", "split": "heldout"},
]

# Canned host answers: basename-only citation, exact path citation, and both
# abstention shapes. H04 never gets here -- the fake host fails on it instead.
# The arm is read off the call itself: the MCP config, or the project settings
# the hook arm must have written into the cwd before the host started.
FAKE_BODY = r'''
import json, os, sys

argv = sys.argv[1:]
prompt = argv[argv.index("-p") + 1] if "-p" in argv else ""
case = prompt.split(" ")[0] if prompt else "?"
settings = None
if os.path.isfile(".claude/settings.json"):
    settings = json.loads(open(".claude/settings.json", encoding="utf-8").read())
if "--mcp-config" in argv:
    arm = "candidate"
elif settings and "hook claude-code" in json.dumps(settings):
    arm = "hook"
else:
    arm = "baseline"
with open(os.environ["FAKE_HOST_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"case": case, "arm": arm, "argv": argv, "settings": settings,
                             "cwd": os.getcwd(), "stdin": sys.stdin.read()}) + "\n")

if case == "H04":
    sys.stderr.write("fake host crashed\n")
    sys.stdout.write("this is not JSON")
    raise SystemExit(2)

answers = {
    ("H01", "baseline"): "alpha.md says the retention window is ninety days.",
    ("H01", "candidate"): "notes/alpha.md (sha256 aaa111) : the retention window is ninety days.",
    ("H01", "hook"): "notes/alpha.md : the retention window is ninety days.",
    ("H02", "baseline"): "NOT_FOUND",
    ("H02", "candidate"): "NOT_FOUND",
    ("H02", "hook"): "NOT_FOUND",
    ("H03", "baseline"): "NOT_FOUND\nNothing in the vault names an escalation contact.",
    ("H03", "candidate"): "notes/beta.md (sha256 bbb222) names the duty reviewer.",
    ("H03", "hook"): "notes/beta.md names the duty reviewer.",
}
usage = {"baseline": {"input_tokens": 1000, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0, "output_tokens": 100},
         "candidate": {"input_tokens": 400, "cache_creation_input_tokens": 50,
                       "cache_read_input_tokens": 25, "output_tokens": 80},
         "hook": {"input_tokens": 300, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0, "output_tokens": 60}}
turns = {"baseline": 4, "candidate": 2, "hook": 1}
print(json.dumps({"result": answers[(case, arm)], "is_error": False,
                  "num_turns": turns[arm],
                  "duration_ms": {"baseline": 1500, "candidate": 800, "hook": 400}[arm],
                  "total_cost_usd": {"baseline": 0.02, "candidate": 0.01, "hook": 0.005}[arm],
                  "usage": usage[arm]}))
'''


# A host that hangs on T01 (with a child of its own) and answers T02 with line
# separators inside the text.
SLOW_BODY = r'''
import json, os, subprocess, sys, time
argv = sys.argv[1:]
prompt = argv[argv.index("-p") + 1]
case = prompt.split(" ")[0]
with open(os.environ["FAKE_HOST_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"case": case}) + "\n")
if case == "T01":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(os.environ["FAKE_CHILD_PID"], "w") as handle:
        handle.write(str(child.pid))
    time.sleep(60)
print(json.dumps({"result": "notes/alpha.md says ninety\u2028days\u0085later", "is_error": False,
                  "num_turns": 1, "duration_ms": 5, "total_cost_usd": 0.001,
                  "usage": {"input_tokens": 10, "output_tokens": 2}}))
'''


class Matching(unittest.TestCase):
    """C-28: `delivered` matches whole path tokens; `abstained` needs NOT_FOUND first."""

    @classmethod
    def setUpClass(cls):
        cls.runner = load_runner()

    def test_delivered_needs_a_whole_path_token(self):
        cases = [("notes/a.md", "The answer cites notes/delta.md", False),
                 ("notes/plan.md", "see archive/old-plan.md", False),
                 ("notes/plan.md", "see archive/plan.md", False),
                 ("notes/plan.md", "notes/plan.md.bak holds it", False),
                 ("notes/a.md", "data.md", False),
                 ("notes/plan.md", "plan.md says so", True),
                 ("notes/plan.md", "Source: notes/plan.md.", True),
                 ("notes/plan.md", "Source: `notes/plan.md` (sha256 abc)", True),
                 ("notes/plan.md", "[plan](notes/plan.md)", True),
                 ("notes/plan.md", "./notes/plan.md line 3", True),
                 ("notes/plan.md", "notes/plan.md#budget", True),
                 ("notes/My Plan.md", "from notes/My Plan.md, the rule", True)]
        for expected, answer, wanted in cases:
            with self.subTest(expected=expected, answer=answer):
                self.assertEqual(self.runner.is_delivered(answer, [expected]), wanted)
        self.assertFalse(self.runner.is_delivered("notes/a.md", []))
        self.assertFalse(self.runner.is_delivered("notes/a.md", ["notes/a.md", "notes/b.md"]))

    def test_abstention_is_exactly_the_first_word(self):
        cases = [("NOT_FOUND", True), ("NOT_FOUND.", True), ("**NOT_FOUND**", True),
                 ("NOT_FOUND\nNothing in the vault says so.", True),
                 ("NOT_FOUNDATION of the plan", False), ("The answer is NOT_FOUND", False),
                 ("not_found", False), ("", False)]
        for answer, wanted in cases:
            with self.subTest(answer=answer):
                self.assertEqual(self.runner.is_abstention(answer), wanted)


class LiveCompare(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.vault = self.root / "synthetic-vault"
        (self.vault / "notes").mkdir(parents=True)
        (self.vault / "notes" / "alpha.md").write_text("# Alpha\nretention window: ninety days\n")
        (self.vault / "notes" / "beta.md").write_text("# Beta\nescalation: the duty reviewer\n")
        self.cases = self.root / "cases.jsonl"
        self.cases.write_text("".join(json.dumps(case) + "\n" for case in CASES))
        self.out = self.root / "out"
        self.log = self.root / "host-calls.jsonl"
        self.fake = self.root / "fake-claude"
        self.fake.write_text("#!" + sys.executable + "\n" + FAKE_BODY)
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # -- helpers ----------------------------------------------------------

    def compare(self, *flags, out=None, expect=0):
        run = subprocess.run([sys.executable, str(RUNNER), "--vault", str(self.vault),
                              "--cases", str(self.cases), "--out", str(out or self.out),
                              "--claude", str(self.fake), *flags],
                             capture_output=True, text=True,
                             env=dict(os.environ, HOME=str(self.home),
                                      FAKE_HOST_LOG=str(self.log)))
        self.assertEqual(run.returncode, expect, run.stdout + run.stderr)
        return run

    def results(self, out=None):
        text = (out or self.out).joinpath("results.jsonl").read_text()
        return {(row["id"], row["arm"]): row
                for row in (json.loads(line) for line in text.splitlines() if line.strip())}

    def host_calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    # -- tests ------------------------------------------------------------

    def test_row_fields_and_delivery_logic(self):
        self.compare()
        rows = self.results()
        self.assertEqual(len(rows), 12)          # 4 cases x 3 arms, hook included by default
        row = rows[("H01", "candidate")]
        for key in ["id", "arm", "model", "started_at", "duration_ms", "host_duration_ms",
                    "num_turns", "is_error", "cost_usd", "tokens", "answer", "delivered",
                    "abstained", "correct_abstention", "false_abstention", "judge",
                    "hook_settings"]:
            self.assertIn(key, row)
        self.assertEqual(row["model"], "sonnet")
        self.assertIsNone(row["judge"])
        self.assertEqual(row["host_duration_ms"], 800)
        self.assertEqual(row["num_turns"], 2)
        self.assertEqual(row["cost_usd"], 0.01)
        self.assertEqual(row["tokens"], {"input": 400, "cache_creation": 50, "cache_read": 25,
                                         "output": 80, "total": 555})
        self.assertGreaterEqual(row["duration_ms"], 0)
        self.assertIn("ninety days", row["answer"])

        self.assertIsNone(row["hook_settings"])            # only the hook arm writes settings
        hooked = rows[("H01", "hook")]
        self.assertEqual(hooked["hook_settings"], {"settings": ".claude/settings.json",
                                                   "action": "created", "restored": "removed"})
        self.assertEqual(hooked["num_turns"], 1)
        self.assertEqual(hooked["tokens"]["total"], 360)
        self.assertTrue(hooked["delivered"])

        # a basename citation counts as delivered; an unanswerable case never does
        self.assertTrue(rows[("H01", "baseline")]["delivered"])
        self.assertFalse(rows[("H02", "baseline")]["delivered"])
        for arm in ["baseline", "candidate", "hook"]:
            self.assertTrue(rows[("H02", arm)]["abstained"])
            self.assertTrue(rows[("H02", arm)]["correct_abstention"])
            self.assertFalse(rows[("H02", arm)]["false_abstention"])
        # NOT_FOUND with trailing prose still abstains, and on an answerable case it is false
        beta = rows[("H03", "baseline")]
        self.assertTrue(beta["abstained"])
        self.assertTrue(beta["false_abstention"])
        self.assertFalse(beta["correct_abstention"])
        self.assertFalse(beta["delivered"])
        self.assertTrue(rows[("H03", "candidate")]["delivered"])
        self.assertFalse(rows[("H03", "candidate")]["abstained"])

        summary = json.loads((self.out / "summary.json").read_text())
        self.assertEqual(summary["per_arm"]["baseline"]["delivered"], 1)
        self.assertEqual(summary["per_arm"]["candidate"]["delivered"], 2)
        self.assertEqual(summary["per_arm"]["baseline"]["answerable_cases"], 3)
        self.assertEqual(summary["per_arm"]["baseline"]["false_abstentions"], 1)
        self.assertEqual(summary["per_arm"]["candidate"]["correct_abstentions"], 1)
        self.assertEqual(summary["per_arm"]["candidate"]["mean_total_tokens"], 555.0)
        self.assertEqual(summary["per_arm"]["hook"]["mean_total_tokens"], 360.0)
        self.assertEqual(summary["per_arm"]["baseline"]["mean_turns"], 4.0)
        self.assertEqual(summary["per_arm"]["hook"]["mean_turns"], 1.0)
        self.assertEqual(summary["per_arm"]["hook"]["delivered"], 2)
        self.assertEqual(summary["arms"], ["baseline", "candidate", "hook"])
        self.assertIsNone(summary["per_arm"]["baseline"]["judged"])

    def test_host_failure_is_recorded_and_the_run_continues(self):
        self.compare()
        rows = self.results()
        for arm in ["baseline", "candidate", "hook"]:
            failed = rows[("H04", arm)]
            self.assertTrue(failed["is_error"])
            self.assertIn("host exit 2", failed["error"])
            self.assertEqual(failed["answer"], "")
            self.assertEqual(failed["tokens"]["total"], 0)
            self.assertEqual((self.out / failed["raw"]).read_text(), "this is not JSON")
            self.assertIn("fake host crashed", (self.out / failed["stderr"]).read_text())
        self.assertEqual(len(rows), 12)         # the three other cases still ran
        # a failing hook run still puts the vault copy's settings back
        self.assertEqual(rows[("H04", "hook")]["hook_settings"]["restored"], "removed")
        self.assertFalse((self.vault / ".claude").exists())
        summary = json.loads((self.out / "summary.json").read_text())
        for arm in ["baseline", "candidate", "hook"]:
            self.assertEqual(summary["per_arm"][arm]["errors"], 1)
            self.assertEqual(summary["per_arm"][arm]["cases_run"], 4)

    def test_resume_skips_rows_already_present(self):
        self.compare()
        first = len(self.host_calls())
        self.assertEqual(first, 12)
        again = self.compare()
        self.assertEqual(len(self.host_calls()), first)      # no second host call
        self.assertIn("skip H01 baseline", again.stderr)
        self.assertIn("skip H01 hook", again.stderr)
        self.assertEqual(len(self.results()), 12)

    def test_only_restricts_ids(self):
        self.compare("--only", "H01,H02", "--arms", "baseline,candidate")
        self.assertEqual(sorted(self.results()), [("H01", "baseline"), ("H01", "candidate"),
                                                  ("H02", "baseline"), ("H02", "candidate")])
        self.assertEqual({call["case"] for call in self.host_calls()}, {"H01", "H02"})
        self.assertEqual({call["arm"] for call in self.host_calls()}, {"baseline", "candidate"})

    def test_judgements_merge_and_recount_without_running(self):
        self.compare()
        calls = len(self.host_calls())
        verdicts = self.root / "judgements.json"
        verdicts.write_text(json.dumps({"H01|baseline": "partial", "H01|candidate": "correct",
                                        "H02|baseline": "abstained", "H03|candidate": "correct",
                                        "H03|baseline": "wrong", "H99|baseline": "correct"}))
        run = self.compare("--judgements", str(verdicts))
        self.assertEqual(len(self.host_calls()), calls)      # nothing was re-run
        self.assertIn("H99|baseline matches no result row", run.stderr)
        rows = self.results()
        self.assertEqual(rows[("H01", "candidate")]["judge"], "correct")
        self.assertEqual(rows[("H01", "baseline")]["judge"], "partial")
        self.assertIsNone(rows[("H04", "baseline")]["judge"])
        summary = json.loads((self.out / "summary.json").read_text())
        self.assertEqual(summary["per_arm"]["baseline"]["judged"],
                         {"correct": 0, "partial": 1, "wrong": 1, "abstained": 1})
        self.assertEqual(summary["per_arm"]["candidate"]["judged"]["correct"], 2)
        self.assertIn("| judged correct |", (self.out / "REPORT.md").read_text())
        verdicts.write_text(json.dumps({"H01|baseline": "excellent"}))
        rejected = self.compare("--judgements", str(verdicts), expect=2)
        self.assertIn("correct/partial/wrong/abstained", rejected.stderr)
        self.assertEqual(self.results()[("H01", "baseline")]["judge"], "partial")

    def test_report_and_summary_carry_no_absolute_path(self):
        self.compare()
        summary_text = (self.out / "summary.json").read_text()
        report = (self.out / "REPORT.md").read_text()
        for text in [report, summary_text]:
            self.assertNotIn(str(self.root), text)
            self.assertNotIn(str(self.vault), text)
            self.assertNotIn(str(REPO), text)
        self.assertEqual(json.loads(summary_text)["vault"], "synthetic-vault")
        self.assertIn("sonnet", report)
        self.assertIn("signal, not proof", report)
        self.assertIn("cases.jsonl", report)
        self.assertIn("| H01 | policy | candidate |", report)

    def test_arms_get_their_own_tools_and_the_candidate_gets_mcp_config(self):
        self.compare("--only", "H01", "--max-turns", "5", "--budget-usd", "0.25")
        config = json.loads((self.out / "mcp.json").read_text())
        self.assertIn("context-layer", config["mcpServers"])
        self.assertIn("command", config["mcpServers"]["context-layer"])
        calls = {call["arm"]: call for call in self.host_calls()}
        self.assertEqual(set(calls), {"baseline", "candidate", "hook"})
        for arm, call in calls.items():
            argv = call["argv"]
            self.assertEqual(call["cwd"], str(self.vault))
            self.assertEqual(call["stdin"], "")           # stdin is /dev/null, never a terminal
            self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
            self.assertEqual(argv[argv.index("--max-turns") + 1], "5")
            self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "0.25")
            self.assertEqual(argv[argv.index("--output-format") + 1], "json")
            self.assertIn("--no-session-persistence", argv)
            self.assertIn("--strict-mcp-config", argv)
            self.assertIn("NOT_FOUND", argv[argv.index("--append-system-prompt") + 1])
        baseline, candidate = calls["baseline"]["argv"], calls["candidate"]["argv"]
        self.assertNotIn("--mcp-config", baseline)
        self.assertEqual(baseline[baseline.index("--add-dir") + 1], str(self.vault))
        self.assertEqual(baseline[baseline.index("--allowedTools") + 1], "Read,Grep,Glob")
        self.assertIn("WebFetch", baseline[baseline.index("--disallowedTools") + 1])
        self.assertNotIn("--add-dir", candidate)
        self.assertEqual(candidate[candidate.index("--mcp-config") + 1], str(self.out / "mcp.json"))
        self.assertEqual(candidate[candidate.index("--allowedTools") + 1],
                         "mcp__context-layer__search_vault,mcp__context-layer__read_source")
        self.assertIn("Read,Grep,Glob", candidate[candidate.index("--disallowedTools") + 1])
        # the hook arm is baseline's tool set plus the hook, never the MCP config
        hook = calls["hook"]["argv"]
        self.assertNotIn("--mcp-config", hook)
        self.assertEqual(hook[hook.index("--add-dir") + 1], str(self.vault))
        self.assertEqual(hook[hook.index("--allowedTools") + 1], "Read,Grep,Glob")
        self.assertEqual(hook[hook.index("--disallowedTools") + 1],
                         baseline[baseline.index("--disallowedTools") + 1])
        self.assertIn("injected with the prompt",
                      hook[hook.index("--append-system-prompt") + 1])

    def test_hook_arm_installs_project_settings_and_puts_them_back(self):
        self.compare("--only", "H01", "--arms", "hook")
        call = self.host_calls()[0]
        group = call["settings"]["hooks"]["UserPromptSubmit"]       # present while it ran
        self.assertEqual(len(group), 1)
        command = group[0]["hooks"][0]
        self.assertEqual(command["type"], "command")
        self.assertIn("hook claude-code", command["command"])
        self.assertIn(str(self.vault), command["command"])
        self.assertFalse((self.vault / ".claude").exists())         # and gone afterwards

    def test_hook_arm_merges_and_restores_existing_project_settings(self):
        settings = self.vault / ".claude" / "settings.json"
        settings.parent.mkdir()
        before = json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command",
                                                                        "command": "echo mine"}]}]},
                             "model": "opus"}, indent=4) + "\n"
        settings.write_text(before)
        self.compare("--only", "H01", "--arms", "hook")
        during = self.host_calls()[0]["settings"]
        self.assertEqual(during["model"], "opus")                   # unrelated keys survive
        commands = [entry["command"] for group in during["hooks"]["UserPromptSubmit"]
                    for entry in group["hooks"]]
        self.assertEqual(len(commands), 2)
        self.assertIn("echo mine", commands)
        self.assertEqual(settings.read_text(), before)              # byte-for-byte restore
        row = self.results()[("H01", "hook")]["hook_settings"]
        self.assertEqual(row, {"settings": ".claude/settings.json", "action": "merged",
                               "restored": "previous bytes"})


class TimeoutsAndLines(unittest.TestCase):
    """C-28: a hung host becomes an error row; rows survive any character in an answer."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.vault = self.root / "vault"
        (self.vault / "notes").mkdir(parents=True)
        (self.vault / "notes" / "alpha.md").write_text("# Alpha\nninety days\n")
        self.cases = self.root / "cases.jsonl"
        self.cases.write_text("".join(json.dumps(case) + "\n" for case in [
            {"id": "T01", "prompt": "T01 hangs", "answerable": True,
             "expected_sources": ["notes/alpha.md"]},
            {"id": "T02", "prompt": "T02 answers", "answerable": True,
             "expected_sources": ["notes/alpha.md"]}]))
        self.out = self.root / "out"
        self.log = self.root / "calls.jsonl"
        self.child = self.root / "child.pid"
        self.fake = self.root / "slow-claude"
        self.fake.write_text("#!" + sys.executable + "\n" + SLOW_BODY)
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC)

    def run_runner(self, *flags):
        return subprocess.run([sys.executable, str(RUNNER), "--vault", str(self.vault),
                               "--cases", str(self.cases), "--out", str(self.out),
                               "--claude", str(self.fake), "--arms", "baseline", *flags],
                              capture_output=True, text=True,
                              env=dict(os.environ, HOME=str(self.home),
                                       FAKE_HOST_LOG=str(self.log),
                                       FAKE_CHILD_PID=str(self.child)))

    def test_a_hung_host_is_stopped_and_recorded_as_an_error_row(self):
        started = time.monotonic()
        run = self.run_runner("--timeout-s", "1")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertLess(time.monotonic() - started, 30)
        rows = {row["id"]: row for row in map(json.loads, self.out.joinpath(
            "results.jsonl").read_text().split("\n")[:-1])}
        hung = rows["T01"]
        self.assertTrue(hung["is_error"])
        self.assertTrue(hung["timed_out"])
        self.assertIn("timed out after 1 s", hung["error"])
        self.assertEqual(hung["tokens"]["total"], 0)
        self.assertFalse(rows["T02"]["is_error"])          # the run went on
        pid = int(self.child.read_text())
        for _ in range(50):                                # the host's child went with it
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail("the hung host's child process is still running")
        summary = json.loads((self.out / "summary.json").read_text())
        self.assertEqual(summary["per_arm"]["baseline"]["timeouts"], 1)
        self.assertEqual(summary["bounds"]["timeout_s"], 1.0)
        self.assertIn("| timeouts | 1 |", (self.out / "REPORT.md").read_text())

    def test_rows_are_ascii_and_resume_across_line_separators(self):
        self.cases.write_text(json.dumps({"id": "T02", "prompt": "T02 answers",
                                          "answerable": True,
                                          "expected_sources": ["notes/alpha.md"]}) + "\n")
        self.assertEqual(self.run_runner().returncode, 0)
        raw = (self.out / "results.jsonl").read_bytes()
        self.assertTrue(raw.isascii())
        self.assertEqual(raw.count(b"\n"), 1)
        row = json.loads(raw)
        self.assertIn("\u2028", row["answer"])
        self.assertTrue(row["delivered"])
        again = self.run_runner()
        self.assertIn("skip T02 baseline", again.stderr)
        self.assertEqual(len(self.log.read_text().split("\n")[:-1]), 1)  # no second host call

    def test_cost_is_labelled_host_estimated(self):
        self.cases.write_text(json.dumps({"id": "T02", "prompt": "T02 answers",
                                          "answerable": True}) + "\n")
        self.assertEqual(self.run_runner().returncode, 0)
        summary = json.loads((self.out / "summary.json").read_text())
        self.assertTrue(summary["cost_basis"].startswith("host-estimated"))
        report = (self.out / "REPORT.md").read_text()
        self.assertIn("mean host-estimated cost (USD)", report)
        self.assertIn("Cost: host-estimated", report)

    def test_timeout_must_be_positive(self):
        run = self.run_runner("--timeout-s", "0")
        self.assertEqual(run.returncode, 2)
        self.assertIn("--timeout-s", run.stderr)


class OrchestrationCostTable(unittest.TestCase):
    """C-10: the §5 tables of docs/subagents.md are the eval's own `--json` output."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def run_eval(self, *flags):
        done = subprocess.run([sys.executable, str(REPO / "eval" / "orchestration_cost.py"),
                               *flags, "--json"], cwd=REPO, capture_output=True, text=True,
                              env=dict(os.environ, HOME=self.temp.name))
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout), done.stdout

    def test_docs_tables_are_generated_from_the_json_output(self):
        standin, first = self.run_eval("--standin")
        _, second = self.run_eval("--standin")
        self.assertEqual(first, second)                    # deterministic, byte for byte
        template, _ = self.run_eval("--rules", "templates/vault/CLAUDE.md")
        default, _ = self.run_eval("--standin", "--packet", "default")
        self.assertEqual((standin["packets"], standin["own_budget_tokens"],
                          standin["shared_budget_tokens"]), ("compact", 1200, 2000))
        self.assertEqual((standin["fabricated_records_planted"],
                          standin["records_failing_check"]), (1, 1))
        spec = importlib.util.spec_from_file_location(
            "orchestration_cost_under_test", REPO / "eval" / "orchestration_cost.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        block = module.docs_block(standin, template, default)
        docs = (REPO / "docs" / "subagents.md").read_text(encoding="utf-8")
        begin = docs.index(module.DOCS_BEGIN)
        end = docs.index(module.DOCS_END) + len(module.DOCS_END)
        self.assertEqual(docs[begin:end], block,
                         "docs/subagents.md is out of date: paste the output of "
                         "`python3 eval/orchestration_cost.py --docs` between its markers")


if __name__ == "__main__":
    unittest.main()
