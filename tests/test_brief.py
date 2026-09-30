"""Tests for context_layer.brief - the evidence-pinned session brief.

Runnable as `python3 tests/test_brief.py`. Every test builds a disposable,
fictional vault in its own temporary folder (HOME points there too). The CLI is
driven through a small parser that registers `brief` the way cli.py would.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from _portable_helpers import isolated_home_env

REPO = Path(os.environ.get("TEST_REPO_HOME", Path(__file__).resolve().parents[1]))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from context_layer import brain, brief, memory, rules  # noqa: E402

DRIVER = (
    "import argparse, sys\n"
    "from context_layer import brief\n"
    "parser = argparse.ArgumentParser(prog='context-layer')\n"
    "sub = parser.add_subparsers(dest='command', required=True)\n"
    "brief.register(sub)\n"
    "args, extra = parser.parse_known_args(sys.argv[1:])\n"
    "args.rest = [t for t in extra if t != '--']\n"
    "raise SystemExit(args.func(args))\n"
)
LINE = re.compile(r"^- (status|stale|log|open|memory|activation) `([^`]+)` "
                  r"([0-9a-f]{8}|absent): (.+)$")


def sha8(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8] if path.is_file() else "absent"


class BriefBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        home = self.root / "home"
        home.mkdir()
        patcher = mock.patch.dict(os.environ, isolated_home_env(os.environ, str(home)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.vault = self.root / "vault"

    def env(self):
        env = dict(os.environ)
        env.pop("CONTEXT_LAYER_HOME", None)
        env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
        return env

    def run_cli(self, *argv, module=False):
        command = ([sys.executable, "-m", "context_layer.brief", *argv] if module
                   else [sys.executable, "-c", DRIVER, "brief", *argv])
        return subprocess.run(command, cwd=REPO, capture_output=True, text=True, env=self.env())

    def index(self):
        result = subprocess.run([sys.executable, "-m", "context_layer.cli", "index",
                                 str(self.vault)], cwd=REPO, capture_output=True, text=True,
                                env=self.env())
        self.assertEqual(result.returncode, 0, result.stderr)

    def full_vault(self):
        """A starter brain with an index, four records, memory, a stale source, two
        sub-agent tasks and one activation trace."""
        brain.init(self.vault, apply_now=True)
        self.index()
        for index in range(1, 5):
            rules.record(self.vault, summary=f"Garden step {index}", files=["none"],
                         verified="ok", next_step="none")
        decision = "300-Projects/Example - Decision - Timer Controller.md"
        memory.record(self.vault, kind="decision", text="Use the battery timer",
                      sources=[{"path": decision}])
        memory.record(self.vault, kind="task", text="Buy 30 m of drip line")
        with open(self.vault / decision, "a", encoding="utf-8") as handle:
            handle.write("\nRevisited after the first week.\n")
        tasks = self.vault / ".context" / "tasks"
        for task_id, state in (("t-open", "pending_review"), ("t-done", "verified")):
            (tasks / task_id).mkdir(parents=True)
            (tasks / task_id / "task.json").write_text(json.dumps(
                {"id": task_id, "goal": f"Check the {state} schedule"}), encoding="utf-8")
            (tasks / task_id / "result.json").write_text(json.dumps({"state": state}),
                                                         encoding="utf-8")
        (self.vault / ".context" / "activation.json").write_text(json.dumps(
            {"version": 1, "generated_at": "2026-01-15T14:31:00Z", "method": "synaptic",
             "run_id": "0" * 32}), encoding="utf-8")
        return decision


class Build(BriefBase):
    def test_empty_vault_reports_the_missing_index(self):
        (self.vault / "notes").mkdir(parents=True)
        (self.vault / "notes" / "idea.md").write_text("# Idea\n", encoding="utf-8")
        result = brief.build(self.vault)
        self.assertEqual([item["kind"] for item in result["items"]], ["status"])
        status = result["items"][0]
        self.assertEqual((status["path"], status["sha8"]), (".context/index-manifest.json",
                                                            "absent"))
        self.assertTrue(status["text"].startswith("overall missing;"))
        self.assertFalse((self.vault / ".context").exists())      # writes nothing

    def test_sections_in_order_and_every_line_pinned(self):
        decision = self.full_vault()
        result = brief.build(self.vault)
        self.assertEqual(result["problems"], [])
        kinds = [item["kind"] for item in result["items"]]
        order = ["status", "stale", "log", "open", "memory", "activation"]
        self.assertEqual(kinds, sorted(kinds, key=order.index))
        text = brief.render(result)
        lines = text.splitlines()
        self.assertEqual(lines[0], brief.HEADER)
        for line in lines[1:]:
            match = LINE.match(line)
            self.assertIsNotNone(match, line)
            kind, path, prefix, _ = match.groups()
            self.assertEqual(prefix, sha8(self.vault / path), line)
        logs = [item["text"] for item in result["items"] if item["kind"] == "log"]
        self.assertEqual(len(logs), 3)
        self.assertTrue(logs[0].split(" - ", 1)[1].startswith("Garden step 2"))
        self.assertIn("Garden step 4", logs[-1])
        self.assertRegex(logs[-1], r"\[r-\d{8}t\d{6}z-[0-9a-f]{6}\]$")
        stale = [item for item in result["items"] if item["kind"] == "stale"]
        self.assertEqual([item["path"] for item in stale], [decision])
        self.assertIn("recorded as", stale[0]["text"])
        opened = [item["text"] for item in result["items"] if item["kind"] == "open"]
        self.assertTrue(any(t.startswith("| B-1 | setup | open |") for t in opened))
        self.assertTrue(any("task: Buy 30 m of drip line" in t for t in opened))
        self.assertTrue(any("t-open pending_review" in t for t in opened))
        self.assertFalse(any("t-done" in t for t in opened))
        heads = [item["text"] for item in result["items"] if item["kind"] == "memory"]
        self.assertTrue(any("decision (draft): Use the battery timer" in t for t in heads))
        self.assertFalse(any("Buy 30 m" in t for t in heads))      # listed once, as open work
        self.assertIn("2026-01-15T14:31:00Z", result["items"][-1]["text"])
        status = result["items"][0]["text"]
        self.assertRegex(status, r"^overall degraded; \d+ changed, 0 added, 0 deleted, 0 moved")

    def test_deterministic(self):
        self.full_vault()
        first, second = brief.build(self.vault), brief.build(self.vault)
        self.assertEqual(first, second)
        self.assertEqual(brief.render(first, 900), brief.render(second, 900))

    def test_builds_write_nothing(self):
        self.full_vault()
        before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.vault.rglob("*")
                  if p.is_file()}
        brief.build(self.vault)
        after = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.vault.rglob("*")
                 if p.is_file()}
        self.assertEqual(before, after)

    def test_cap_drops_whole_lines_only(self):
        self.full_vault()
        result = brief.build(self.vault)
        full = brief.render(result).splitlines()
        text = brief.render(result, 700)
        self.assertLessEqual(len(text), 700)
        lines = text.splitlines()
        self.assertRegex(lines[-1], r"^\(\d+ line\(s\) omitted to fit 700 characters\)$")
        omitted = int(re.match(r"^\((\d+)", lines[-1]).group(1))
        self.assertEqual(lines[:-1], full[:len(lines) - 1])       # whole lines, in order
        self.assertEqual(len(lines) - 2 + omitted, len(full) - 1)

    def test_long_text_is_cut_visibly(self):
        self.vault.mkdir()
        (self.vault / "LOG.md").write_text("# Log\n\n## 2026-01-15 - " + "word " * 80 + "\n",
                                           encoding="utf-8")
        item = [i for i in brief.build(self.vault)["items"] if i["kind"] == "log"][0]
        self.assertRegex(item["text"], r" \[\+\d+ chars\]$")
        self.assertLessEqual(len(item["text"]), brief.QUOTE_CHARS + 20)

    def test_backlog_lists_open_states_only(self):
        self.vault.mkdir()
        rows = [("B-1", "open"), ("B-2", "blocked"), ("B-3", "in progress"), ("B-4", "done"),
                ("B-5", "dropped")]
        table = "| id | area | state | next step |\n| --- | --- | --- | --- |\n" + "".join(
            f"| {rid} | garden | {state} | water |\n" for rid, state in rows)
        closed = "\n## Closed\n\n| id | area | closed | evidence |\n| --- | --- | --- | --- |\n" \
                 "| B-9 | garden | 2026-01-15 | open question answered |\n"
        (self.vault / "BACKLOG.md").write_text("# Backlog\n\n" + table + closed,
                                               encoding="utf-8")
        opened = [i["text"] for i in brief.build(self.vault)["items"] if i["kind"] == "open"]
        self.assertEqual([t.split("|")[1].strip() for t in opened], ["B-1", "B-2", "B-3"])

    def test_unreadable_memory_is_a_problem_not_a_failure(self):
        self.full_vault()
        store = self.vault / ".context" / "memory" / "records.jsonl"
        with open(store, "a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        result = brief.build(self.vault)
        self.assertTrue(any(p.startswith("memory:") for p in result["problems"]))
        self.assertTrue(any(item["kind"] == "log" for item in result["items"]))
        text, problem = brief.hook_text(self.vault, 3000)
        self.assertIn("- log `LOG.md`", text)
        self.assertIn("memory:", problem)

    def test_exclusions_are_honoured(self):
        decision = self.full_vault()
        routes = self.vault / ".context" / "routes.json"
        config = json.loads(routes.read_text(encoding="utf-8"))
        config["exclude_prefixes"] += ["LOG.md", "300-Projects/"]
        routes.write_text(json.dumps(config), encoding="utf-8")
        result = brief.build(self.vault)
        kinds = {item["kind"] for item in result["items"]}
        self.assertNotIn("log", kinds)
        self.assertNotIn(decision, [item["path"] for item in result["items"]])
        self.assertNotIn("300-Projects", brief.render(result))
        routes.write_text("{broken", encoding="utf-8")
        result = brief.build(self.vault)
        self.assertTrue(any(p.startswith("exclusions:") for p in result["problems"]))
        self.assertFalse({"log", "stale"} & {item["kind"] for item in result["items"]})
        self.assertFalse(any(item["path"] == "BACKLOG.md" for item in result["items"]))

    def test_no_network_module_is_loaded(self):
        self.full_vault()
        probe = ("import sys\nfrom context_layer import brief\n"
                 f"brief.build({str(self.vault)!r})\n"
                 "print(sorted(m for m in ('socket', 'ssl', 'http.client', 'urllib.request') "
                 "if m in sys.modules))\n")
        result = subprocess.run([sys.executable, "-c", probe], cwd=REPO, capture_output=True,
                                text=True, env=self.env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[]")


class Cli(BriefBase):
    def test_text_json_and_module(self):
        self.full_vault()
        text = self.run_cli(str(self.vault))
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertTrue(text.stdout.startswith(brief.HEADER))
        data = json.loads(self.run_cli(str(self.vault), "--json", "--max-chars", "700").stdout)
        self.assertEqual(data["schema"], brief.SCHEMA)
        self.assertGreater(data["omitted"], 0)
        self.assertEqual(len(data["items"]) + data["omitted"],
                         len(brief.build(self.vault)["items"]))
        module = self.run_cli(str(self.vault), module=True)
        self.assertEqual((module.returncode, module.stdout), (0, text.stdout))

    def test_errors(self):
        missing = self.run_cli(str(self.root / "nope"))
        self.assertEqual(missing.returncode, 1)
        self.assertIn("vault not found", missing.stderr)
        self.vault.mkdir()
        self.assertEqual(self.run_cli(str(self.vault), "--bogus").returncode, 2)
        self.assertEqual(self.run_cli(str(self.vault), "--max-chars", "10").returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
