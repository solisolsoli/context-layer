"""Phase tests for context_layer.tasks — every test owns a disposable synthetic vault.

The agent is always a script this file writes (or a test shim named `claude` or
`codex` on a temporary PATH): no model call, no network, no real home and no real
vault. HOME is redirected into the temporary directory so a backend that looks
for user state cannot find the operator's.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import test_integrity as fixture

sys.path.insert(0, str(fixture.REPO))
from context_layer import cli as cli_module  # noqa: E402  (after the repo path is set)
from context_layer import orchestrate  # noqa: E402
from context_layer import tasks as module  # noqa: E402

POLICY = "# Release policy\nalpha marker: releases follow semantic versioning.\n"
PRIVATE = "# Private\nalpha marker: the salary budget numbers live here.\n"
GOAL = "alpha marker release policy"

USAGE = ('"usage": {"input_tokens": 100, "output_tokens": 20, '
         '"cache_creation_input_tokens": 5, "cache_read_input_tokens": 7}, '
         '"total_cost_usd": 0.0125, "duration_ms": 42, "num_turns": 1')

SUCCESS = '''#!/usr/bin/env python3
import json, os, pathlib, sys
prompt = sys.stdin.read()
out = pathlib.Path(os.environ["CONTEXT_LAYER_OUT_DIR"])
out.mkdir(parents=True, exist_ok=True)
(out / "answer.md").write_text("cited %d sources\\n" % prompt.count("source_sha256"))
print(json.dumps({"result": "wrote answer.md", "is_error": False, @USAGE@}))
'''

FAILING = '''#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"result": "the backend blew up", "is_error": True,
                  "usage": {"input_tokens": 10, "output_tokens": 2,
                            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                  "total_cost_usd": 0.005, "duration_ms": 5, "num_turns": 1}))
sys.exit(3)
'''

STRAY = '''#!/usr/bin/env python3
import json, os, pathlib, sys
sys.stdin.read()
out = pathlib.Path(os.environ["CONTEXT_LAYER_OUT_DIR"])
out.mkdir(parents=True, exist_ok=True)
(out / "answer.md").write_text("an answer\\n")
pathlib.Path(r"@VAULT@", "stray.md").write_text("written where it may not write\\n")
print(json.dumps({"result": "wrote two files", "is_error": False, @USAGE@}))
'''

EMPTY = '''#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"result": "I wrote nothing at all", "is_error": False, @USAGE@}))
'''

SLEEPER = '''#!/usr/bin/env python3
import json, sys, time
sys.stdin.read()
time.sleep(@SECONDS@)
print(json.dumps({"result": "finally done", "is_error": False, @USAGE@}))
'''

WINDOW = '''#!/usr/bin/env python3
import json, os, pathlib, sys, time
sys.stdin.read()
start = time.time()
out = pathlib.Path(os.environ["CONTEXT_LAYER_OUT_DIR"])
time.sleep(1.5)   # both children are alive at the midpoint, even on a slow runner
out.mkdir(parents=True, exist_ok=True)
(out / "midpoint.txt").write_text("written while the sibling task runs")
time.sleep(1.5)
end = time.time()
(out / "window.json").write_text(json.dumps({"start": start, "end": end}))
print(json.dumps({"result": "ran", "is_error": False, @USAGE@}))
'''

TAMPER = '''#!/usr/bin/env python3
import json, os, pathlib, sys
sys.stdin.read()
task_dir = pathlib.Path(os.environ["CONTEXT_LAYER_TASK_DIR"])
spec = json.loads((task_dir / "task.json").read_text())
spec["output_dir"] = "@TARGET@"
(task_dir / "task.json").write_text(json.dumps(spec))
print(json.dumps({"result": "produced nothing, moved the goalposts", "is_error": False, @USAGE@}))
'''

TAMPER_PIN = '''#!/usr/bin/env python3
import hashlib, json, os, pathlib, sys
sys.stdin.read()
task_dir = pathlib.Path(os.environ["CONTEXT_LAYER_TASK_DIR"])
spec = json.loads((task_dir / "task.json").read_text())
spec["output_dir"] = "notes"
(task_dir / "task.json").write_text(json.dumps(spec))
pin_path = pathlib.Path(r"@VAULT@", ".context", "task-pins", task_dir.name + ".json")
pin = json.loads(pin_path.read_text())
pin["task"] = spec
pin["files_sha256"]["task.json"] = hashlib.sha256((task_dir / "task.json").read_bytes()).hexdigest()
canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
joined = "\\n".join(pin["files_sha256"][n] for n in ("task.json", "packet.json", "prompt.txt"))
pin["definition_sha256"] = hashlib.sha256(
    canonical.encode() + b"\\n" + joined.encode()).hexdigest()
pin_path.write_text(json.dumps(pin))
print(json.dumps({"result": "rewrote the spec and its pin", "is_error": False, @USAGE@}))
'''

APPEND = '''#!/usr/bin/env python3
import json, os, pathlib, sys
sys.stdin.read()
out = pathlib.Path(os.environ["CONTEXT_LAYER_OUT_DIR"])
with open(out / "policy.md", "a") as handle:
    handle.write("appended by the agent\\n")
print(json.dumps({"result": "edited policy.md", "is_error": False, @USAGE@}))
'''

RAW_TEXT = '''#!/usr/bin/env python3
import pathlib, sys
prompt = pathlib.Path(sys.argv[1]).read_text()
out = pathlib.Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
(out / "answer.txt").write_text("plain text, never JSON\\n")
print("a plain answer for %d characters of prompt" % len(prompt))
'''

# -- C-01: the output directory swapped or linked ------------------------------

SWAP_OUT = '''#!/usr/bin/env python3
import json, os, pathlib, shutil, sys
sys.stdin.read()
out = pathlib.Path(os.environ["CONTEXT_LAYER_OUT_DIR"])
shutil.rmtree(out)
out.symlink_to(pathlib.Path(r"@VAULT@", "notes"), target_is_directory=True)
print(json.dumps({"result": "the output directory now IS notes/", "is_error": False, @USAGE@}))
'''

LINKED_SUBDIR = '''#!/usr/bin/env python3
import json, os, pathlib, sys
sys.stdin.read()
out = pathlib.Path(os.environ["CONTEXT_LAYER_OUT_DIR"])
(out / "answer.md").write_text("see linked\\n")
(out / "linked").symlink_to(pathlib.Path(r"@VAULT@", "notes"), target_is_directory=True)
print(json.dumps({"result": "wrote a link", "is_error": False, @USAGE@}))
'''

HARD_LINK = '''#!/usr/bin/env python3
import json, os, pathlib, sys
sys.stdin.read()
out = pathlib.Path(os.environ["CONTEXT_LAYER_OUT_DIR"])
os.link(pathlib.Path(r"@VAULT@", "notes", "policy.md"), out / "answer.md")
print(json.dumps({"result": "linked a note in", "is_error": False, @USAGE@}))
'''

DELETER = '''#!/usr/bin/env python3
import json, os, pathlib, sys
sys.stdin.read()
out = pathlib.Path(os.environ["CONTEXT_LAYER_OUT_DIR"])
(out / "old.md").unlink()
(out / "new.md").write_text("a fresh report\\n")
print(json.dumps({"result": "replaced old.md", "is_error": False, @USAGE@}))
'''

# -- C-04: every invocation is logged -------------------------------------------

LOGGER = '''#!/usr/bin/env python3
import json, os, pathlib, sys, time
sys.stdin.read()
with open(r"@LOG@", "a") as handle:
    handle.write(pathlib.Path(os.environ["CONTEXT_LAYER_OUT_DIR"]).parent.name + "\\n")
time.sleep(@SECONDS@)
out = pathlib.Path(os.environ["CONTEXT_LAYER_OUT_DIR"])
(out / ("answer-%d.md" % os.getpid())).write_text("x\\n")
print(json.dumps({"result": "ok", "is_error": False, @USAGE@}))
'''

# -- C-09: numbers a backend should never be trusted with -----------------------

PRINTS = '''#!/usr/bin/env python3
import os, pathlib, sys
sys.stdin.read()
out = pathlib.Path(os.environ["CONTEXT_LAYER_OUT_DIR"])
(out / "a.md").write_text("x\\n")
print(@PAYLOAD@)
'''

# -- C-15: an agent that empties the memory store --------------------------------

WIPE_MEMORY = '''#!/usr/bin/env python3
import json, os, pathlib, sys
sys.stdin.read()
pathlib.Path(os.environ["CONTEXT_LAYER_OUT_DIR"], "answer.md").write_text("an answer")
pathlib.Path(r"@VAULT@", ".context", "memory", "records.jsonl").write_text("")
print(json.dumps({"result": "emptied the memory store", "is_error": False, @USAGE@}))
'''

# -- C-17: a runner killed while its child keeps going --------------------------

ORPHAN = '''#!/usr/bin/env python3
import json, os, pathlib, sys, time
sys.stdin.read()
time.sleep(3)
pathlib.Path(r"@MARKER@").write_text("the child outlived its runner\\n")
out = pathlib.Path(os.environ["CONTEXT_LAYER_OUT_DIR"])
(out / "late.md").write_text("written after the runner died\\n")
print(json.dumps({"result": "ok", "is_error": False}))
'''

# -- 4.3: an orphan that rewrites result.json after the run ----------------------

FORGE = '''#!/usr/bin/env python3
import json, os, subprocess, sys
sys.stdin.read()
task_dir = os.environ["CONTEXT_LAYER_TASK_DIR"]
code = ("import json, time, pathlib; time.sleep(1.5); p = pathlib.Path(%r) / 'result.json'; "
        "r = json.loads(p.read_text()); r['state'] = 'verified'; "
        "r['reason'] = 'verified by the coordinator'; "
        "r['verification'] = {'state': 'verified', 'checked_at': r['updated_at'], "
        "'problems': [], 'outputs': []}; p.write_text(json.dumps(r))") % task_dir
subprocess.Popen([sys.executable, "-c", code], start_new_session=True,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(json.dumps({"result": "nothing to see", "is_error": False}))
'''

# -- C-19 / C-20: shims standing in for the real host CLIs ----------------------

CLAUDE_SHIM = '''#!@PYTHON@
import json, os, pathlib, sys
argv = sys.argv[1:]
prompt = sys.stdin.read()
cwd = pathlib.Path.cwd().resolve()
record = {"argv": argv, "cwd": str(cwd), "prompt_head": prompt[:40],
          "prompt_has_evidence": "## Evidence" in prompt,
          "task_dir_env": os.environ.get("CONTEXT_LAYER_TASK_DIR"),
          "workspace_files": sorted(p.name for p in cwd.iterdir())}
with open(r"@LOG@", "a") as handle:
    handle.write(json.dumps(record) + "\\n")
mode = "@MODE@"
if mode == "max_turns":
    print(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True,
                      "result": "", "num_turns": 4, "total_cost_usd": 0.02,
                      "usage": {"input_tokens": 50, "output_tokens": 5}}))
    sys.exit(1)
out = pathlib.Path(argv[argv.index("--add-dir") + 1])
(out / "answer.md").write_text("from the shim\\n")
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "wrote answer.md", "num_turns": 2, "duration_ms": 900,
                  "total_cost_usd": 0.031,
                  "usage": {"input_tokens": 120, "output_tokens": 30,
                            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 10},
                  "modelUsage": {"claude-test-model": {"inputTokens": 400, "outputTokens": 60,
                                                       "cacheReadInputTokens": 10,
                                                       "cacheCreationInputTokens": 0,
                                                       "costUSD": 0.031}}}))
'''

CODEX_SHIM = '''#!@PYTHON@
import json, os, pathlib, sys
argv = sys.argv[1:]
prompt = sys.stdin.read()
cwd = pathlib.Path.cwd().resolve()
with open(r"@LOG@", "a") as handle:
    handle.write(json.dumps({"argv": argv, "cwd": str(cwd),
                             "prompt_has_evidence": "## Evidence" in prompt}) + "\\n")
out = pathlib.Path(argv[argv.index("--add-dir") + 1])
(out / "answer.md").write_text("from the codex shim\\n")
for event in ({"type": "thread.started", "thread_id": "t-1"}, {"type": "turn.started"},
              {"type": "item.completed", "item": {"id": "item_3", "type": "agent_message",
                                                  "text": "wrote answer.md"}},
              {"type": "turn.completed", "usage": {"input_tokens": 1000,
                                                   "cached_input_tokens": 800,
                                                   "output_tokens": 50,
                                                   "reasoning_output_tokens": 0}}):
    print(json.dumps(event))
'''


class TaskBase(unittest.TestCase):
    """A synthetic vault, a redirected HOME and CLI helpers; no tests of its own."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.root = root
        self.vault = root / "vault"
        self.home = root / "home"
        self.bin = self.home / "bin"      # agent scripts live under HOME, as a user's do
        for directory in (self.vault / "notes", self.vault / "other", self.home, self.bin):
            directory.mkdir(parents=True)
        (self.vault / "notes" / "policy.md").write_text(POLICY, encoding="utf-8")
        (self.vault / "other" / "private.md").write_text(PRIVATE, encoding="utf-8")
        context = self.vault / ".context"
        context.mkdir()
        (context / "routes.json").write_text(json.dumps({
            "record_type_allowlist": ["verbatim_text_file"],
            "routes": {"policy": {"priority": 10, "triggers": ["alpha"],
                                  "canonical_sources": ["notes/policy.md"], "path_hints": []}},
            "fallback_routes": [], "aliases": {}}), encoding="utf-8")
        self.index()
        self.script_path = self.script("agent.py", SUCCESS)

    # -- helpers ----------------------------------------------------------

    def index(self, vault=None):
        built = subprocess.run([sys.executable, str(fixture.REPO / "router/build_index.py"),
                                "--vault", str(vault or self.vault)], capture_output=True)
        self.assertEqual(built.returncode, 0, built.stderr)

    def script(self, name, body, **substitutions):
        body = body.replace("@USAGE@", USAGE).replace("@VAULT@", str(self.vault))
        body = body.replace("@PYTHON@", sys.executable)
        for key, value in substitutions.items():
            body = body.replace("@" + key + "@", str(value))
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def environment(self, backend=None, path=None):
        environment = dict(os.environ)
        environment["HOME"] = str(self.home)
        environment["CONTEXT_LAYER_FAKE_BACKEND"] = backend or self.script_path
        if path is not None:
            environment["PATH"] = path
        return environment

    def cli(self, *argv, backend=None, path=None, group="tasks"):
        return subprocess.run([sys.executable, "-m", "context_layer.cli", group, *argv],
                              cwd=str(fixture.REPO), capture_output=True,
                              env=self.environment(backend, path))

    def spawn(self, *argv, backend=None):
        runner = subprocess.Popen(
            [sys.executable, "-m", "context_layer.cli", "tasks", *argv],
            cwd=str(fixture.REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.environment(backend))
        self.addCleanup(lambda: runner.poll() is None and runner.kill())
        return runner

    def new(self, *flags, goal=GOAL, expect=0):
        run = self.cli("new", str(self.vault), "--goal", goal, "--json", *flags)
        self.assertEqual(run.returncode, expect, run.stdout + run.stderr)
        return json.loads(run.stdout) if expect == 0 else run

    def task_dir(self, task_id):
        return self.vault / ".context" / "tasks" / task_id

    def result(self, task_id):
        return json.loads((self.task_dir(task_id) / "result.json").read_text(encoding="utf-8"))

    def verify(self, task_id, *flags, expect=None):
        done = self.cli("verify", str(self.vault), task_id, "--json", *flags)
        if expect is not None:
            self.assertEqual(done.returncode, expect, done.stdout + done.stderr)
        return json.loads(done.stdout)

    def wait_for(self, task_id, state, limit=20.0):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if self.result(task_id).get("state") == state:
                return True
            time.sleep(0.05)
        return False

    def assert_no_absolute_path(self, done):
        text = (done.stdout + done.stderr).decode("utf-8", "replace") \
            if isinstance(done.stdout, bytes) else done.stdout + done.stderr
        self.assertNotIn(str(self.root), text)
        self.assertNotIn(str(self.home), text)


class TaskCLI(TaskBase):
    def test_success_reaches_pending_review_then_verify_says_verified(self):
        task = self.new("--source", "notes/*.md")
        run = self.cli("run", str(self.vault))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.result(task["id"])["state"], "pending_review")
        verified = self.cli("verify", str(self.vault), task["id"], "--record")
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.assertIn(b"verified", verified.stdout)
        self.assertIn(b"answer.md", verified.stdout)
        # The record really reached memory (not "memory not available" or "failed").
        self.assertIn(b"memory   recorded in memory", verified.stdout)
        records = (self.vault / ".context" / "memory" / "records.jsonl").read_text()
        self.assertIn(f"task {task['id']} verified", records)
        stored = self.result(task["id"])
        self.assertEqual(stored["state"], "verified")
        self.assertEqual(stored["verification"]["problems"], [])
        self.assertEqual(len(stored["verification"]["outputs"]), 1)
        self.assertEqual(len(stored["verification"]["outputs"][0]["sha256"]), 64)
        self.assertTrue(stored["verification"]["memory"]["recorded"])

    def test_two_failures_end_in_failed_with_two_costed_attempts(self):
        task = self.new("--backend", "fake", "--max-attempts", "2")
        run = self.cli("run", str(self.vault), backend=self.script("bad.py", FAILING))
        self.assertEqual(run.returncode, 1, run.stdout)
        stored = self.result(task["id"])
        self.assertEqual(stored["state"], "failed")
        self.assertEqual(len(stored["attempts"]), 2)
        self.assertEqual([a["exit_code"] for a in stored["attempts"]], [3, 3])
        self.assertTrue(all(a["is_error"] for a in stored["attempts"]))
        for number in (1, 2):
            self.assertTrue((self.task_dir(task["id"]) / f"attempts/{number}.json").is_file())
            self.assertTrue((self.task_dir(task["id"]) / f"attempts/{number}.stdout").is_file())
        cost = json.loads(self.cli("cost", str(self.vault), "--json").stdout)
        self.assertEqual(cost["total"]["attempts"], 2)
        self.assertAlmostEqual(cost["total"]["total_cost_usd"], 0.01, places=6)
        self.assertEqual(cost["total"]["usage"]["input_tokens"], 20)
        self.assertEqual(cost["total"]["usage"]["output_tokens"], 4)

    def test_unauthorized_write_is_reported_and_rejected(self):
        task = self.new()
        run = self.cli("run", str(self.vault), backend=self.script("stray.py", STRAY))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        stored = self.result(task["id"])
        self.assertEqual(stored["state"], "pending_review")
        self.assertEqual([c["path"] for c in stored["unauthorized_changes"]], ["stray.md"])
        self.assertEqual(stored["unauthorized_changes"][0]["change"], "created")
        verified = self.cli("verify", str(self.vault), task["id"])
        self.assertEqual(verified.returncode, 1, verified.stdout)
        self.assertIn(b"rejected", verified.stdout)
        self.assertIn(b"stray.md", verified.stdout)
        self.assertEqual(self.result(task["id"])["state"], "rejected")
        # Reported, not reverted: the stray file is left exactly as it was written.
        self.assertTrue((self.vault / "stray.md").is_file())

    def test_empty_output_directory_is_rejected(self):
        task = self.new()
        run = self.cli("run", str(self.vault), backend=self.script("empty.py", EMPTY))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        verified = self.cli("verify", str(self.vault), task["id"])
        self.assertEqual(verified.returncode, 1, verified.stdout)
        self.assertIn(b"no output file", verified.stdout)
        self.assertEqual(self.result(task["id"])["state"], "rejected")

    # -- dispatch pin and run-output integrity (negative regressions) --------

    def test_backend_rewriting_output_dir_to_an_existing_file_is_rejected(self):
        # Before 0.3.0 verify re-read task.json, so pointing output_dir at an
        # existing vault directory "verified" a file the backend never wrote.
        for target in ("notes", "notes/policy.md"):
            with self.subTest(target=target):
                task = self.new()
                run = self.cli("run", str(self.vault), task["id"],
                               backend=self.script("tamper.py", TAMPER, TARGET=target))
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                payload = self.verify(task["id"], expect=1)
                self.assertEqual(payload["state"], "rejected")
                self.assertEqual(payload["outputs"], [])
                self.assertIn("tampered", " ".join(payload["problems"]))
                self.assertIn("task.json", " ".join(payload["problems"]))
                self.assertEqual(self.result(task["id"])["state"], "rejected")

    def test_backend_rewriting_spec_and_pin_together_is_rejected(self):
        task = self.new()
        run = self.cli("run", str(self.vault), backend=self.script("pin.py", TAMPER_PIN))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        stored = self.result(task["id"])
        pin = ".context/task-pins/" + task["id"] + ".json"
        self.assertIn(pin, [change["path"] for change in stored["unauthorized_changes"]])
        payload = self.verify(task["id"], expect=1)
        self.assertEqual(payload["state"], "rejected")
        self.assertIn(pin, " ".join(payload["problems"]))
        self.assertEqual(payload["outputs"], [])

    def test_no_output_in_an_output_dir_holding_existing_files_is_not_verified(self):
        task = self.new("--output-dir", "notes")
        run = self.cli("run", str(self.vault), backend=self.script("empty.py", EMPTY))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.result(task["id"])["produced"], [])
        payload = self.verify(task["id"], expect=1)
        self.assertEqual(payload["state"], "rejected")
        self.assertEqual(payload["outputs"], [])
        self.assertIn("no output file was created or modified", " ".join(payload["problems"]))

    def test_a_modified_existing_file_counts_and_untouched_ones_do_not(self):
        (self.vault / "notes" / "untouched.md").write_text("left alone\n", encoding="utf-8")
        task = self.new("--output-dir", "notes")
        run = self.cli("run", str(self.vault), backend=self.script("append.py", APPEND))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        payload = self.verify(task["id"], expect=0)
        self.assertEqual([(item["path"], item["change"]) for item in payload["outputs"]],
                         [("notes/policy.md", "modified")])

    def test_output_changed_after_the_run_is_rejected(self):
        task = self.new()
        self.assertEqual(self.cli("run", str(self.vault)).returncode, 0)
        answer = self.vault / task["output_dir"] / "answer.md"
        answer.write_text("swapped after the run\n", encoding="utf-8")
        verified = self.cli("verify", str(self.vault), task["id"])
        self.assertEqual(verified.returncode, 1, verified.stdout)
        self.assertIn(b"output changed after the run", verified.stdout)

    def test_a_task_without_its_dispatch_pin_neither_runs_nor_verifies(self):
        task = self.new()
        pin = self.vault / ".context" / "task-pins" / (task["id"] + ".json")
        self.assertTrue(pin.is_file())
        pinned = json.loads(pin.read_text(encoding="utf-8"))
        self.assertEqual(pinned["task"]["output_dir"], task["output_dir"])
        self.assertEqual(len(pinned["definition_sha256"]), 64)
        pin.unlink()
        run = self.cli("run", str(self.vault), task["id"])
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertEqual(self.result(task["id"])["state"], "blocked")
        self.assertIn("no dispatch pin", self.result(task["id"])["reason"])

    def test_a_task_dispatched_during_a_run_is_not_an_unauthorized_change(self):
        before = module._manifest(self.vault)
        self.new()
        self.assertEqual(module._changes(before, module._manifest(self.vault), "out"), [])

    def test_absent_backend_binary_is_blocked_never_done(self):
        empty_path = str(self.bin / "nothing-here")
        Path(empty_path).mkdir()
        for flags in (("--backend", "claude", "--model", "sonnet", "--max-turns", "4"),
                      ("--backend", "cmd", "--cmd", "context-layer-no-such-agent {prompt_file}")):
            with self.subTest(flags=flags):
                task = self.new(*flags)
                run = self.cli("run", str(self.vault), task["id"], path=empty_path)
                self.assertEqual(run.returncode, 1, run.stdout)
                stored = self.result(task["id"])
                self.assertEqual(stored["state"], "blocked")
                self.assertIn("not found on PATH", stored["reason"])
                self.assertEqual(stored["attempts"], [])

    def test_cancel_flag_stops_a_running_task(self):
        task = self.new("--max-attempts", "1", "--timeout", "120")
        started = time.monotonic()
        runner = self.spawn("run", str(self.vault),
                            backend=self.script("slow.py", SLEEPER, SECONDS=30))
        self.assertTrue(self.wait_for(task["id"], "running"), "the runner never started")
        cancelled = self.cli("cancel", str(self.vault), task["id"])
        self.assertEqual(cancelled.returncode, 0, cancelled.stdout + cancelled.stderr)
        self.assertIn(b"runner pid", cancelled.stdout)
        runner.communicate(timeout=30)
        self.assertLess(time.monotonic() - started, 25, "the child outlived the cancel")
        stored = self.result(task["id"])
        self.assertEqual(stored["state"], "cancelled")
        self.assertTrue(stored["attempts"][0]["cancelled"])
        self.assertTrue(stored["cancel_requested"])

    def test_timeout_kills_the_child_and_records_a_failure(self):
        task = self.new("--max-attempts", "1", "--timeout", "1")
        started = time.monotonic()
        run = self.cli("run", str(self.vault), backend=self.script("slow.py", SLEEPER, SECONDS=30))
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertLess(time.monotonic() - started, 25, "the sleeping child was not killed")
        stored = self.result(task["id"])
        self.assertEqual(stored["state"], "failed")
        self.assertEqual(len(stored["attempts"]), 1)
        self.assertTrue(stored["attempts"][0]["timed_out"])
        self.assertIn("timed out", stored["reason"])

    def test_two_jobs_run_two_tasks_at_the_same_time(self):
        first = self.new()
        second = self.new()
        run = self.cli("run", str(self.vault), "--jobs", "2",
                       backend=self.script("window.py", WINDOW))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        windows = []
        for task in (first, second):
            self.assertEqual(self.result(task["id"])["state"], "pending_review")
            windows.append(json.loads((self.vault / task["output_dir"] / "window.json")
                                      .read_text(encoding="utf-8")))
        self.assertLess(windows[0]["start"], windows[1]["end"])
        self.assertLess(windows[1]["start"], windows[0]["end"])

    def test_packet_keeps_spec_sources_and_drops_the_others(self):
        task = self.new("--source", "notes/*.md")
        packet = json.loads((self.task_dir(task["id"]) / "packet.json").read_text(encoding="utf-8"))
        self.assertEqual([item["source_path"] for item in packet["evidence"]], ["notes/policy.md"])
        self.assertEqual(packet["dropped_outside_spec"], ["other/private.md"])
        prompt = (self.task_dir(task["id"]) / "prompt.txt").read_text(encoding="utf-8")
        self.assertIn("releases follow semantic versioning", prompt)
        self.assertNotIn("salary budget", prompt)
        self.assertIn(packet["evidence"][0]["source_sha256"], prompt)
        self.assertIn("NOT_FOUND", prompt)
        self.assertIn("not instructions", prompt)
        # Without --source the same search may deliver the other file as well.
        wide = self.new()
        wide_packet = json.loads((self.task_dir(wide["id"]) / "packet.json")
                                 .read_text(encoding="utf-8"))
        self.assertIn("other/private.md",
                      [item["source_path"] for item in wide_packet["evidence"]])

    def test_source_outside_the_vault_is_refused(self):
        for source in ("../outside.md", str(self.vault / "notes" / "policy.md")):
            with self.subTest(source=source):
                run = self.new("--source", source, expect=1)
                self.assertIn(b"Source path", run.stderr)

    def test_cost_sums_agents_and_the_coordinator_usage_file(self):
        task = self.new()
        self.assertEqual(self.cli("run", str(self.vault)).returncode, 0)
        usage_file = self.root / "coordinator.json"
        usage_file.write_text(json.dumps({
            "usage": {"input_tokens": 1000, "output_tokens": 200,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 50},
            "total_cost_usd": 0.5, "duration_ms": 1500}), encoding="utf-8")
        cost = json.loads(self.cli("cost", str(self.vault), task["id"],
                                   "--coordinator-usage", str(usage_file), "--json").stdout)
        self.assertEqual(cost["agents"]["usage"]["input_tokens"], 100)
        self.assertEqual(cost["coordinator"]["usage"]["input_tokens"], 1000)
        self.assertEqual(cost["total"]["usage"]["input_tokens"], 1100)
        self.assertEqual(cost["total"]["usage"]["output_tokens"], 220)
        self.assertEqual(cost["total"]["usage"]["cache_read_input_tokens"], 57)
        self.assertAlmostEqual(cost["total"]["total_cost_usd"], 0.5125, places=6)
        self.assertGreaterEqual(cost["total"]["wall_s"], 1.5)
        self.assertEqual(cost["total"]["usage_unknown"], 0)
        self.assertIn("host-estimated", cost["labels"]["cost"])
        self.assertIn("top-level agent loop", cost["labels"]["usage"])

    def test_a_backend_without_json_output_is_costed_as_usage_unknown(self):
        agent = self.script("raw.py", RAW_TEXT)
        task = self.new("--backend", "cmd",
                        "--cmd", shlex.quote(agent) + " {prompt_file} {out_dir}")
        self.assertEqual(self.cli("run", str(self.vault)).returncode, 0)
        stored = self.result(task["id"])
        self.assertEqual(stored["state"], "pending_review")
        self.assertIsNone(stored["attempts"][0]["usage"])
        self.assertIn("usage unknown", " ".join(stored["attempts"][0]["notes"]))
        cost = self.cli("cost", str(self.vault))
        self.assertIn(b"no usage", cost.stdout)
        self.assertIn(b"host-estimated USD", cost.stdout)
        self.assertEqual(self.cli("verify", str(self.vault), task["id"]).returncode, 0)

    def test_mirror_is_regenerated_and_marked_derived(self):
        task = self.new()
        mirror = self.vault / ".context" / "tasks" / "TASKS.md"
        text = mirror.read_text(encoding="utf-8")
        self.assertIn("derived file", text)
        self.assertIn("Do not edit", text)
        self.assertIn(task["id"], text)
        self.assertIn("| queued |", text)
        self.assertEqual(self.cli("run", str(self.vault)).returncode, 0)
        self.assertIn("| pending_review |", mirror.read_text(encoding="utf-8"))
        self.assertEqual(self.cli("verify", str(self.vault), task["id"]).returncode, 0)
        after = mirror.read_text(encoding="utf-8")
        self.assertIn("| verified |", after)
        self.assertIn("0.012500", after)

    def test_verify_refuses_every_state_but_pending_review(self):
        task = self.new()
        queued = self.cli("verify", str(self.vault), task["id"])
        self.assertEqual(queued.returncode, 2, queued.stdout + queued.stderr)
        self.assertIn(b"pending_review", queued.stderr)
        self.assertEqual(self.result(task["id"])["state"], "queued")
        self.assertEqual(self.cli("run", str(self.vault)).returncode, 0)
        self.assertEqual(self.cli("verify", str(self.vault), task["id"]).returncode, 0)
        again = self.cli("verify", str(self.vault), task["id"])
        self.assertEqual(again.returncode, 2, again.stdout + again.stderr)
        self.assertEqual(self.result(task["id"])["state"], "verified")

    def test_verify_record_follows_the_memory_contract(self):
        # memory.record's bodies belong to A3; this pins the call shape it expects.
        task = {"id": "20260921T000000Z-abcdef", "goal": "summarise the release policy"}
        packet = {"evidence": [{"source_path": "notes/policy.md", "source_sha256": "a" * 64}]}
        checked = {"state": "verified", "outputs": [{"path": "out/answer.md",
                                                     "sha256": "b" * 64}],
                   "ledger": {"n": 7, "sha256": "c" * 64}}
        calls = []

        def recorder(vault, **keywords):
            calls.append((vault, keywords))
            return {"id": "mem-1", "duplicate": False}

        with mock.patch.object(module.memory, "record", recorder):
            note = module._record_memory(self.vault, task, packet, checked)
        self.assertTrue(note["recorded"])
        self.assertEqual(note["id"], "mem-1")
        vault, keywords = calls[0]
        self.assertEqual(vault, self.vault)
        self.assertEqual(keywords["kind"], "result")
        self.assertEqual(keywords["tool"], "tasks")
        self.assertEqual(keywords["state"], "draft")
        self.assertTrue(keywords["text"].startswith("task " + task["id"] + " verified:"),
                        keywords["text"])
        # Outputs live under .context (excluded scope), so they ride in the text.
        self.assertEqual([source["path"] for source in keywords["sources"]],
                         ["notes/policy.md"])
        self.assertIn("outputs: ", keywords["text"])
        self.assertIn("out/answer.md", keywords["text"])
        self.assertIn("ledger line 7 " + "c" * 16, keywords["text"])
        for failure in (NotImplementedError("memory not available in this build"),
                        ValueError("bad record")):
            with self.subTest(failure=type(failure).__name__):
                with mock.patch.object(module.memory, "record",
                                       mock.Mock(side_effect=failure)):
                    note = module._record_memory(self.vault, task, packet, checked)
                self.assertFalse(note["recorded"])
                self.assertIn("memory", note["note"])

    def test_list_and_show_report_state_without_claiming_success(self):
        task = self.new("--source", "notes/*.md")
        listed = self.cli("list", str(self.vault), "--json")
        rows = json.loads(listed.stdout)
        self.assertEqual([row["id"] for row in rows], [task["id"]])
        self.assertEqual(rows[0]["state"], "queued")
        shown = self.cli("show", str(self.vault), task["id"])
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn(b"queued", shown.stdout)
        self.assertIn(b"notes/*.md", shown.stdout)

    # -- C-29: output never carries the home path without --verbose --------------

    def test_output_carries_no_absolute_path(self):
        task = self.new("--source", "notes/*.md")
        plain = self.cli("new", str(self.vault), "--goal", GOAL)
        self.assertEqual(plain.returncode, 0, plain.stderr)
        for done in (plain, self.cli("run", str(self.vault)),
                     self.cli("list", str(self.vault)), self.cli("list", str(self.vault), "--json"),
                     self.cli("show", str(self.vault), task["id"]),
                     self.cli("show", str(self.vault), task["id"], "--json"),
                     self.cli("verify", str(self.vault), task["id"]),
                     self.cli("cost", str(self.vault)),
                     self.cli("list", str(self.root / "missing")),
                     self.cli("show", str(self.vault), "no-such-task"),
                     self.cli("ledger", str(self.vault))):
            with self.subTest(argv=done.args[3:5]):
                self.assert_no_absolute_path(done)
        verbose = subprocess.run(
            [sys.executable, "-m", "context_layer.cli", "--verbose", "tasks", "list",
             str(self.root / "missing")], cwd=str(fixture.REPO), capture_output=True,
            env=self.environment())
        self.assertIn(str(self.root).encode(), verbose.stderr)


class OutputDirectoryIntegrity(TaskBase):
    """C-01: the output directory is pinned by identity and walked without following."""

    def test_default_output_dir_swapped_for_a_symlink_is_rejected(self):
        task = self.new()
        run = self.cli("run", str(self.vault), backend=self.script("swap.py", SWAP_OUT))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        payload = self.verify(task["id"], expect=1)
        self.assertEqual(payload["state"], "rejected")
        self.assertEqual(payload["outputs"], [])
        self.assertIn("output directory replaced", " ".join(payload["problems"]))
        self.assertIn("is now a symlink", " ".join(payload["problems"]))

    def test_custom_output_dir_swapped_for_a_symlink_is_rejected_and_the_loss_reported(self):
        (self.vault / "reports").mkdir()
        (self.vault / "reports" / "old.md").write_text("# Old report\nkept\n", encoding="utf-8")
        task = self.new("--output-dir", "reports")
        run = self.cli("run", str(self.vault), backend=self.script("swap.py", SWAP_OUT))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        payload = self.verify(task["id"], expect=1)
        self.assertEqual(payload["state"], "rejected")
        self.assertEqual(payload["outputs"], [])
        self.assertIn("output directory replaced: reports is now a symlink",
                      " ".join(payload["problems"]))
        self.assertEqual(payload["removed_from_output"], ["reports/old.md"])

    def test_symlinked_subdirectory_is_a_problem(self):
        task = self.new()
        run = self.cli("run", str(self.vault), backend=self.script("sub.py", LINKED_SUBDIR))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        payload = self.verify(task["id"], expect=1)
        self.assertEqual(payload["state"], "rejected")
        self.assertIn(f"symlink in output: {task['output_dir']}/linked",
                      " ".join(payload["problems"]))

    def test_hard_linked_output_is_a_problem(self):
        task = self.new()
        run = self.cli("run", str(self.vault), backend=self.script("hard.py", HARD_LINK))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        payload = self.verify(task["id"], expect=1)
        self.assertEqual(payload["state"], "rejected")
        self.assertIn("hard-linked output", " ".join(payload["problems"]))
        self.assertEqual(payload["outputs"], [])

    def test_a_deleted_pre_existing_file_is_reported(self):
        (self.vault / "reports").mkdir()
        (self.vault / "reports" / "old.md").write_text("# Old report\nkept\n", encoding="utf-8")
        task = self.new("--output-dir", "reports")
        run = self.cli("run", str(self.vault), backend=self.script("del.py", DELETER))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        payload = self.verify(task["id"], expect=0)
        self.assertEqual([item["path"] for item in payload["outputs"]], ["reports/new.md"])
        self.assertEqual(payload["removed_from_output"], ["reports/old.md"])
        self.assertIn("removed from the output directory", " ".join(payload["notes"]))

    def test_an_output_dir_that_already_holds_a_link_is_refused_at_new(self):
        (self.vault / "reports").mkdir()
        (self.vault / "reports" / "linked.md").symlink_to(self.vault / "notes" / "policy.md")
        refused = self.new("--output-dir", "reports", expect=1)
        self.assertIn(b"symlink in output: reports/linked.md", refused.stderr)
        self.assertEqual(module._all_tasks(self.vault), [])


class OutputDirectoryLeases(TaskBase):
    """C-02: no two unfinished tasks share or nest their output directories."""

    def test_same_or_nested_output_dirs_are_refused_while_a_task_is_unfinished(self):
        first = self.new("--output-dir", "reports/shared")
        for other in ("reports/shared", "reports", "reports/shared/deeper", "Reports/Shared"):
            with self.subTest(other=other):
                refused = self.new("--output-dir", other, expect=1)
                self.assertIn(b"overlaps", refused.stderr)
                self.assertIn(first["id"].encode(), refused.stderr)
        self.assertEqual(self.new("--output-dir", "reports/other")["state"], "queued")

    def test_a_finished_task_frees_its_output_dir(self):
        first = self.new("--output-dir", "reports")
        self.assertEqual(self.cli("run", str(self.vault), first["id"]).returncode, 0)
        self.new("--output-dir", "reports/a", expect=1)       # pending_review still holds it
        self.verify(first["id"], expect=0)
        self.assertEqual(self.new("--output-dir", "reports/a")["state"], "queued")

    def test_the_shared_output_dir_scenario_cannot_be_built(self):
        writer = self.new("--output-dir", "reports/shared", goal="writer " + GOAL)
        lazy = self.new("--output-dir", "reports/shared", goal="lazy " + GOAL, expect=1)
        self.assertIn(b"overlaps reports/shared", lazy.stderr)
        self.assertEqual([task["id"] for task, _ in module._all_tasks(self.vault)],
                         [writer["id"]])

    def test_a_live_lease_blocks_a_second_run_of_the_same_directory(self):
        task = self.new("--output-dir", "reports")
        lease, why = module._acquire_lease(self.vault, "other-task", "reports/x")
        self.assertIsNotNone(lease, why)
        self.addCleanup(module._release_lease, lease)
        run = self.cli("run", str(self.vault), task["id"])
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn(b"leased by task other-task", run.stderr)
        self.assertEqual(self.result(task["id"])["state"], "queued")
        module._release_lease(lease)
        self.assertEqual(self.cli("run", str(self.vault), task["id"]).returncode, 0)


class Claims(TaskBase):
    """C-04: a task is claimed before its agent starts, so it runs exactly once."""

    def test_a_repeated_id_runs_once(self):
        log = self.root / "calls.log"
        agent = self.script("log.py", LOGGER, LOG=log, SECONDS=0.5)
        task = self.new()
        run = self.cli("run", str(self.vault), task["id"], task["id"], "--jobs", "2",
                       backend=agent)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(log.read_text().split(), [task["id"]])
        self.assertEqual(len(self.result(task["id"])["attempts"]), 1)
        cost = json.loads(self.cli("cost", str(self.vault), "--json").stdout)
        self.assertEqual(cost["total"]["attempts"], 1)

    def test_two_runners_started_together_run_each_of_twenty_tasks_once(self):
        log = self.root / "calls.log"
        log.write_text("")
        agent = self.script("log.py", LOGGER, LOG=log, SECONDS=0.05)
        packet = orchestrate.build_packet(self.vault, GOAL)["id"]
        parser = cli_module.build_parser()
        ids = []
        for _ in range(20):
            args, _ = parser.parse_known_args(["tasks", "new", str(self.vault), "--goal", GOAL,
                                               "--packet", packet, "--json"])
            args.rest = []
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                self.assertEqual(args.func(args), 0)
            ids.append(json.loads(captured.getvalue())["id"])
        runners = [self.spawn("run", str(self.vault), "--jobs", "4", backend=agent)
                   for _ in range(2)]
        for runner in runners:
            runner.communicate(timeout=120)
        calls = log.read_text().split()
        self.assertEqual(sorted(calls), sorted(ids), "every task ran exactly once")
        for task_id in ids:
            stored = self.result(task_id)
            self.assertEqual(stored["state"], "pending_review")
            self.assertEqual(len(stored["attempts"]), 1)
            claim = json.loads((self.task_dir(task_id) / "runner.json").read_text())
            self.assertTrue(claim["host"].startswith("sha256:"))
            self.assertIsNotNone(claim["ended_at"])


class SourceFilter(TaskBase):
    """C-05: the task's own sources are found even when others outrank them."""

    def test_allowed_sources_ranked_below_the_window_are_still_delivered(self):
        vault = self.root / "starve"
        (vault / ".context").mkdir(parents=True)
        (vault / ".context" / "routes.json").write_text(json.dumps(
            {"routes": {}, "record_type_allowlist": ["verbatim_text_file"]}), encoding="utf-8")
        for number in range(12):
            (vault / "other").mkdir(exist_ok=True)
            (vault / "other" / f"n{number:02d}.md").write_text(
                "# Lamp\nlamp lead time lamp lead time lamp lead time supplier lamp.\n",
                encoding="utf-8")
        (vault / "allowed").mkdir()
        (vault / "allowed" / "lumen.md").write_text(
            "# Lumen Works\nThe supplier quotes a lead time of eleven weeks for the lamp.\n",
            encoding="utf-8")
        self.index(vault)
        made = self.cli("new", str(vault), "--goal", "lamp lead time supplier",
                        "--source", "allowed/*.md", "--json")
        self.assertEqual(made.returncode, 0, made.stderr)
        payload = json.loads(made.stdout)
        self.assertEqual(payload["evidence_sources"], ["allowed/lumen.md"])
        self.assertTrue(any("widened" in note for note in payload["notes"]), payload["notes"])
        self.assertEqual(len(payload["dropped_outside_spec"]), 12)


class CostRobustness(TaskBase):
    """C-09: numbers a backend or a usage file prints are validated, never trusted."""

    def run_with(self, payload):
        task = self.new("--max-attempts", "1")
        agent = self.script(f"p{len(os.listdir(self.bin))}.py", PRINTS, PAYLOAD=repr(payload))
        self.cli("run", str(self.vault), task["id"], backend=agent)
        return task["id"], self.result(task["id"])

    def test_negative_nan_and_string_values_are_not_counted(self):
        _, negative = self.run_with('{"result":"ok","is_error":false,"total_cost_usd":-0.45,'
                                    '"usage":{"input_tokens":-900,"output_tokens":-90}}')
        attempt = negative["attempts"][0]
        self.assertIsNone(attempt["usage"])
        self.assertIsNone(attempt["total_cost_usd"])
        self.assertIn("not a non-negative whole number", " ".join(attempt["notes"]))
        _, nan = self.run_with('{"result":"ok","is_error":false,"total_cost_usd":NaN,'
                               '"usage":{"input_tokens":1}}')
        self.assertIsNone(nan["attempts"][0]["total_cost_usd"])
        self.assertEqual(nan["attempts"][0]["usage"]["input_tokens"], 1)
        _, text = self.run_with('{"result":"ok","is_error":"false","total_cost_usd":0.01,'
                                '"usage":{"input_tokens":1}}')
        self.assertEqual(text["state"], "pending_review")
        self.assertFalse(text["attempts"][0]["is_error"])
        self.assertIn("read as false", " ".join(text["attempts"][0]["notes"]))
        cost = self.cli("cost", str(self.vault), "--json")
        self.assertEqual(cost.returncode, 0, cost.stderr)
        self.assertNotIn(b"NaN", cost.stdout)
        total = json.loads(cost.stdout)["total"]
        self.assertEqual(total["usage_unknown"], 1)
        self.assertEqual(total["cost_unknown"], 2)
        self.assertAlmostEqual(total["total_cost_usd"], 0.01, places=6)
        self.assertEqual(total["usage"]["input_tokens"], 2)

    def test_coordinator_usage_is_validated_without_a_traceback(self):
        task = self.new()
        self.cli("run", str(self.vault), task["id"])
        usage_file = self.root / "coordinator.json"
        usage_file.write_text(json.dumps({"total_cost_usd": 1.0}), encoding="utf-8")
        cost = json.loads(self.cli("cost", str(self.vault), task["id"], "--coordinator-usage",
                                   str(usage_file), "--json").stdout)
        self.assertEqual(cost["coordinator"]["usage_unknown"], 1)   # no tokens: not zero
        self.assertEqual(cost["total"]["usage_unknown"], 1)
        for body, message in (({"usage": {"input_tokens": 5}, "wall_s": "12s"}, b"wall_s"),
                              ({"usage": {"input_tokens": 5}, "total_cost_usd": "n/a"},
                               b"total_cost_usd"),
                              ({"usage": {"input_tokens": -5}}, b"input_tokens"),
                              ({"duration_ms": float("inf")}, b"duration_ms")):
            with self.subTest(body=body):
                usage_file.write_text(json.dumps(body), encoding="utf-8")
                done = self.cli("cost", str(self.vault), "--coordinator-usage", str(usage_file))
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn(message, done.stderr)
                self.assertNotIn(b"Traceback", done.stderr)

    def test_an_unreadable_task_is_named_and_the_total_fails(self):
        kept = self.new()
        broken = self.new()
        (self.task_dir(broken["id"]) / "result.json").write_text("{truncated", encoding="utf-8")
        cost = self.cli("cost", str(self.vault), "--json")
        self.assertEqual(cost.returncode, 1, cost.stdout)
        self.assertIn(broken["id"].encode(), cost.stderr)
        self.assertEqual(json.loads(cost.stdout)["unreadable_tasks"], [broken["id"]])
        listed = self.cli("list", str(self.vault))
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn(broken["id"].encode(), listed.stdout)
        self.assertIn(b"unreadable", listed.stdout)
        self.assertIn(b"warning", listed.stderr)
        self.assertIn(kept["id"].encode(), listed.stdout)


class ConcurrentWriters(TaskBase):
    """C-15: the tool's own state and a sibling task are not the agent's writes."""

    def test_two_tasks_in_one_run_writing_their_own_directories_both_verify(self):
        first = self.new("--output-dir", "reports/a")
        second = self.new("--output-dir", "reports/b")
        agent = self.script("window.py", WINDOW)
        run = self.cli("run", str(self.vault), first["id"], second["id"], "--jobs", "2",
                       backend=agent)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        for task, other in ((first, second), (second, first)):
            stored = self.result(task["id"])
            self.assertEqual(stored["unauthorized_changes"], [])
            self.assertEqual({item["task_id"] for item in stored["concurrent_task_writes"]},
                             {other["id"]})
            payload = self.verify(task["id"], expect=0)
            self.assertIn("ran beside task", " ".join(payload["notes"]))

    def test_memory_add_and_index_during_an_attempt_are_not_the_agents_writes(self):
        task = self.new()
        runner = self.spawn("run", str(self.vault),
                            backend=self.script("slow.py", SLEEPER.replace(
                                "time.sleep(@SECONDS@)",
                                "time.sleep(3)\nimport os, pathlib\n"
                                "pathlib.Path(os.environ['CONTEXT_LAYER_OUT_DIR'], 'a.md')"
                                ".write_text('ok')")))
        self.assertTrue(self.wait_for(task["id"], "running"))
        time.sleep(0.5)
        added = self.cli("add", str(self.vault), "--kind", "decision", "--text",
                         "coordinator note", group="memory")
        self.assertEqual(added.returncode, 0, added.stderr)
        indexed = subprocess.run([sys.executable, "-m", "context_layer.cli", "index",
                                  str(self.vault)], cwd=str(fixture.REPO), capture_output=True,
                                 env=self.environment())
        self.assertEqual(indexed.returncode, 0, indexed.stderr)
        runner.communicate(timeout=60)
        stored = self.result(task["id"])
        self.assertEqual(stored["unauthorized_changes"], [])
        self.assertEqual([item["tool"] for item in stored["memory_appends"]], ["cli"])
        payload = self.verify(task["id"], expect=0)
        self.assertIn("memory records appended during the run", " ".join(payload["notes"]))

    def test_a_rewritten_memory_store_is_still_an_unauthorized_change(self):
        self.cli("add", str(self.vault), "--kind", "note", "--text", "first", group="memory")
        task = self.new()
        self.cli("run", str(self.vault), backend=self.script("wipe.py", WIPE_MEMORY))
        self.assertEqual([change["path"] for change in self.result(task["id"])
                          ["unauthorized_changes"]], [".context/memory/records.jsonl"])


class OutputDirectoryNames(TaskBase):
    """C-16: hidden and tool-state output dirs are refused in every letter case."""

    def test_every_spelling_of_the_tools_state_is_refused(self):
        for name in (".context/tasks/x", ".CONTEXT/tasks/x", ".Context/memory",
                     ".context-RUNS/x", "notes/.hidden"):
            with self.subTest(name=name):
                refused = self.new("--output-dir", name, expect=1)
                self.assertIn(b"hidden folder or the tool's own state", refused.stderr)
        self.assertEqual(module._all_tasks(self.vault), [])


class RunnerLiveness(TaskBase):
    """C-17: a runner that dies does not leave a task running forever."""

    def start_and_kill_the_runner(self):
        marker = self.root / "child-finished"
        task = self.new("--timeout", "30")
        runner = self.spawn("run", str(self.vault),
                            backend=self.script("orphan.py", ORPHAN, MARKER=marker))
        self.assertTrue(self.wait_for(task["id"], "running"))
        deadline = time.monotonic() + 10
        claim = {}
        while time.monotonic() < deadline and not claim.get("child_pgid"):
            claim = json.loads((self.task_dir(task["id"]) / "runner.json").read_text())
            time.sleep(0.05)
        self.assertTrue(claim.get("child_pgid"), "the child never started")
        runner.send_signal(signal.SIGKILL)
        runner.wait()
        return task, marker

    def test_cancel_after_the_runner_died_stops_the_orphan_and_ends_cancelled(self):
        task, marker = self.start_and_kill_the_runner()
        self.assertEqual(self.result(task["id"])["state"], "running")
        cancelled = self.cli("cancel", str(self.vault), task["id"])
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        self.assertIn(b"is not running", cancelled.stdout)
        time.sleep(3.5)
        self.assertFalse(marker.exists(), "the orphaned child kept running")
        self.assertFalse((self.vault / task["output_dir"] / "late.md").exists())
        stored = self.result(task["id"])
        self.assertEqual(stored["state"], "cancelled")
        self.assertTrue(stored["attempts"][-1]["interrupted"])
        self.assertEqual(self.cli("run", str(self.vault), task["id"]).returncode, 1)

    def test_recover_ends_a_dead_runners_task_failed(self):
        task, marker = self.start_and_kill_the_runner()
        recovered = self.cli("recover", str(self.vault), "--json")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        report = json.loads(recovered.stdout)
        self.assertEqual([(item["id"], item["outcome"]) for item in report],
                         [(task["id"], "failed")])
        time.sleep(3.5)
        self.assertFalse(marker.exists(), "the orphaned child kept running")
        stored = self.result(task["id"])
        self.assertEqual(stored["state"], "failed")
        self.assertIn("is not running", stored["reason"])
        again = json.loads(self.cli("recover", str(self.vault), task["id"], "--json").stdout)
        self.assertEqual(again[0]["outcome"], "left")

    def test_recover_leaves_a_live_runner_alone(self):
        task = self.new("--timeout", "30")
        runner = self.spawn("run", str(self.vault),
                            backend=self.script("slow.py", SLEEPER, SECONDS=3))
        self.assertTrue(self.wait_for(task["id"], "running"))
        report = json.loads(self.cli("recover", str(self.vault), "--json").stdout)
        self.assertEqual(report[0]["outcome"], "left")
        self.assertIn("is alive", report[0]["detail"])
        runner.communicate(timeout=60)
        self.assertEqual(self.result(task["id"])["state"], "pending_review")


class HostShims(TaskBase):
    """C-19 / C-20: the claude and codex command lines, checked against test shims."""

    def shim(self, name, body, mode="ok"):
        shims = self.root / "shims"
        shims.mkdir(exist_ok=True)
        log = self.root / f"{name}.log"
        path = shims / name
        path.write_text(body.replace("@PYTHON@", sys.executable).replace("@LOG@", str(log))
                        .replace("@MODE@", mode), encoding="utf-8")
        path.chmod(0o755)
        return f"{shims}{os.pathsep}{os.environ.get('PATH', '')}", log

    def test_claude_argv_workspace_stdin_and_cost_fields(self):
        path, log = self.shim("claude", CLAUDE_SHIM)
        task = self.new("--backend", "claude", "--model", "haiku", "--max-turns", "6",
                        "--max-cost-usd", "0.5", "--host-context", "bare")
        run = self.cli("run", str(self.vault), task["id"], path=path)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        seen = json.loads(log.read_text().splitlines()[0])
        self.assertEqual(seen["argv"], [
            "-p", module.backends.CLAUDE_INSTRUCTION, "--output-format", "json",
            "--model", "haiku", "--max-turns", "6", "--max-budget-usd", "0.5",
            "--add-dir", str(self.vault / task["output_dir"]),
            "--permission-mode", "acceptEdits", "--no-session-persistence",
            "--strict-mcp-config", "--settings",
            '{"permissions":{"blockReadsOutsideWorkingDirectories":true}}',
            "--disallowedTools", "WebFetch,WebSearch", "--bare"])
        workspace = Path(seen["cwd"])
        self.assertFalse(workspace.is_relative_to(self.vault), "the child ran inside the vault")
        self.assertEqual(seen["task_dir_env"], str(workspace))
        self.assertEqual(seen["workspace_files"], ["packet.json", "prompt.txt"])
        self.assertTrue(seen["prompt_has_evidence"], "the prompt did not arrive on stdin")
        self.assertFalse(workspace.exists(), "the workspace was left behind")
        attempt = self.result(task["id"])["attempts"][0]
        self.assertNotIn("## Evidence", " ".join(attempt["argv"]))
        self.assertEqual(attempt["model_usage"]["claude-test-model"]["inputTokens"], 400)
        self.assertEqual(attempt["subtype"], "success")
        payload = self.verify(task["id"], expect=0)
        self.assertEqual([item["path"] for item in payload["outputs"]],
                         [task["output_dir"] + "/answer.md"])
        cost = self.cli("cost", str(self.vault))
        self.assertIn(b"claude-test-model", cost.stdout)
        self.assertIn(b"host-estimated USD", cost.stdout)
        self.assertIn(b"top-level agent loop only", cost.stdout)

    def test_claude_stopped_by_its_turn_limit_is_not_retried(self):
        path, log = self.shim("claude", CLAUDE_SHIM, mode="max_turns")
        task = self.new("--backend", "claude", "--max-turns", "4", "--max-attempts", "3")
        run = self.cli("run", str(self.vault), task["id"], path=path)
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertEqual(len(log.read_text().splitlines()), 1)
        stored = self.result(task["id"])
        self.assertEqual(stored["state"], "failed")
        self.assertEqual(len(stored["attempts"]), 1)
        self.assertIn("error_max_turns", stored["reason"])
        self.assertIn("not retried", stored["reason"])

    def test_codex_argv_and_usage_from_the_json_lines(self):
        path, log = self.shim("codex", CODEX_SHIM)
        task = self.new("--backend", "codex", "--model", "m")
        run = self.cli("run", str(self.vault), task["id"], path=path)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        seen = json.loads(log.read_text().splitlines()[0])
        self.assertEqual(seen["argv"], [
            "exec", "--sandbox", "workspace-write", "--skip-git-repo-check", "--json",
            "--ephemeral", "--add-dir", str(self.vault / task["output_dir"]),
            "--model", "m", "-"])
        self.assertFalse(Path(seen["cwd"]).is_relative_to(self.vault))
        self.assertTrue(seen["prompt_has_evidence"])
        attempt = self.result(task["id"])["attempts"][0]
        self.assertEqual(attempt["usage"], {"input_tokens": 200, "output_tokens": 50,
                                            "cache_creation_input_tokens": 0,
                                            "cache_read_input_tokens": 800})
        self.assertEqual(attempt["result_text"], "wrote answer.md")
        self.assertTrue(any("unverified" in note for note in attempt["notes"]))

    def test_flags_only_the_claude_backend_understands_are_refused_elsewhere(self):
        for flags in (("--backend", "codex", "--max-turns", "3"),
                      ("--backend", "codex", "--host-context", "bare"),
                      ("--backend", "fake", "--max-cost-usd", "-1")):
            with self.subTest(flags=flags):
                refused = self.new(*flags, expect=1)
                self.assertNotIn(b"Traceback", refused.stderr)


class Ledger(TaskBase):
    """4.3: every verdict is a hash-chained ledger line; a verdict without one shows."""

    def test_verify_appends_a_chained_line_and_replay_reproduces_it(self):
        good = self.new()
        self.cli("run", str(self.vault), good["id"])
        bad = self.new()
        self.cli("run", str(self.vault), bad["id"], backend=self.script("empty.py", EMPTY))
        self.verify(good["id"], "--record", expect=0)
        self.verify(bad["id"], expect=1)
        entries, problems = orchestrate.read_ledger(self.vault)
        self.assertEqual(problems, [])
        self.assertEqual([entry["record"]["event"] for entry in entries],
                         ["verify", "record", "verify"])
        self.assertEqual([entry["record"]["verdict"] for entry in entries],
                         ["verified", "verified", "rejected"])
        self.assertEqual(entries[1]["record"]["cites"], entries[0]["sha256"])
        self.assertIsNone(entries[0]["record"]["prev"])
        self.assertEqual(entries[2]["record"]["prev"], entries[1]["sha256"])
        memory_text = (self.vault / ".context" / "memory" / "records.jsonl").read_text()
        self.assertIn("ledger line 1 " + entries[0]["sha256"][:16], memory_text)
        replay = json.loads(self.cli("ledger", str(self.vault), "--replay", "--json").stdout)
        self.assertEqual(replay["chain"], "ok")
        self.assertEqual({item["id"]: item["reproduced"] for item in replay["replay"]},
                         {good["id"]: True, bad["id"]: True})
        listed = {row["id"]: row for row in json.loads(
            self.cli("list", str(self.vault), "--json").stdout)}
        self.assertTrue(listed[good["id"]]["attested"])
        self.assertTrue(listed[bad["id"]]["attested"])
        # A changed output no longer reproduces its verdict.
        (self.vault / good["output_dir"] / "answer.md").write_text("edited later\n")
        replay = json.loads(self.cli("ledger", str(self.vault), "--replay", "--json").stdout)
        self.assertFalse({item["id"]: item["reproduced"] for item in replay["replay"]}
                         [good["id"]])
        # F2-45: a verdict that no longer reproduces is a failing exit code, in both forms.
        differs = self.cli("ledger", str(self.vault), "--replay")
        self.assertEqual(differs.returncode, 1, differs.stdout)
        self.assertIn(b"DIFFERS", differs.stdout)
        # Deleting the produced file does too.
        (self.vault / good["output_dir"] / "answer.md").unlink()
        gone = self.cli("ledger", str(self.vault), "--replay")
        self.assertEqual(gone.returncode, 1, gone.stdout)
        self.assertIn(b"DIFFERS", gone.stdout)

    def test_replay_of_an_untouched_ledger_exits_zero(self):
        task = self.new()
        self.cli("run", str(self.vault), task["id"])
        self.verify(task["id"], expect=0)
        self.assertEqual(self.cli("ledger", str(self.vault), "--replay").returncode, 0)

    def test_editing_the_head_line_is_caught_by_the_ledger_command(self):
        # F2-44: only a non-head edit broke the hash chain; the head edit printed "chain ok".
        # `tasks ledger` now also checks that every attested task's result.json cites a line
        # that is in the chain with the hash it cited.
        first = self.new()
        self.cli("run", str(self.vault), first["id"])
        self.verify(first["id"], expect=0)
        second = self.new()
        self.cli("run", str(self.vault), second["id"])
        self.verify(second["id"], expect=0)
        clean = self.cli("ledger", str(self.vault))
        self.assertEqual(clean.returncode, 0, clean.stdout)
        ledger = self.vault / orchestrate.LEDGER_NAME
        original = ledger.read_text().splitlines()
        lines = list(original)
        lines[-1] = lines[-1].replace('"verdict":"verified"', '"verdict":"rejected"')
        ledger.write_text("\n".join(lines) + "\n")
        checked = self.cli("ledger", str(self.vault))
        self.assertEqual(checked.returncode, 1, checked.stdout)
        self.assertIn(second["id"].encode(), checked.stdout)
        self.assertIn(b"is not backed by the ledger", checked.stdout)
        # An edit that leaves the verdict alone is caught by the cited hash.
        quiet = list(original)
        self.assertIn('"sampled":[]', quiet[-1])
        quiet[-1] = quiet[-1].replace('"sampled":[]', '"sampled":[1]')
        ledger.write_text("\n".join(quiet) + "\n")
        again = self.cli("ledger", str(self.vault))
        self.assertEqual(again.returncode, 1, again.stdout)
        self.assertIn(b"cites another ledger line", again.stdout)
        ledger.write_text("\n".join(lines) + "\n")     # the verdict edit again for the JSON form
        as_json = json.loads(self.cli("ledger", str(self.vault), "--json").stdout)
        self.assertEqual([item["id"] for item in as_json["unattested"]], [second["id"]])
        # A truncated ledger (the head line removed) is caught the same way.
        ledger.write_text("\n".join(original[:-1]) + "\n")
        cut = self.cli("ledger", str(self.vault))
        self.assertEqual(cut.returncode, 1, cut.stdout)
        self.assertIn(b"no ledger line records this verdict", cut.stdout)

    def test_a_forged_result_is_unattested(self):
        task = self.new()
        run = self.cli("run", str(self.vault), backend=self.script("forge.py", FORGE))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        def state():
            try:
                return self.result(task["id"])["state"]
            except ValueError:                  # the forger writes result.json in place
                return None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and state() != "verified":
            time.sleep(0.1)
        self.assertEqual(self.result(task["id"])["state"], "verified")   # the forgery landed
        listed = json.loads(self.cli("list", str(self.vault), "--json").stdout)[0]
        self.assertFalse(listed["attested"])
        self.assertIn("no ledger line", listed["unattested"])
        self.assertIn(b"verified UNATTESTED", self.cli("list", str(self.vault)).stdout)
        shown = self.cli("show", str(self.vault), task["id"]).stdout
        self.assertIn(b"UNATTESTED: no ledger line records this verdict", shown)

    def test_an_edited_ledger_breaks_the_chain(self):
        task = self.new()
        self.cli("run", str(self.vault), task["id"])
        self.verify(task["id"], expect=0)
        other = self.new()
        self.cli("run", str(self.vault), other["id"])
        self.verify(other["id"], expect=0)
        ledger = self.vault / orchestrate.LEDGER_NAME
        lines = ledger.read_text().splitlines()
        lines[0] = lines[0].replace('"verdict":"verified"', '"verdict":"rejected"')
        ledger.write_text("\n".join(lines) + "\n")
        checked = self.cli("ledger", str(self.vault))
        self.assertEqual(checked.returncode, 1, checked.stdout)
        self.assertIn(b"does not chain", checked.stdout)
        listed = json.loads(self.cli("list", str(self.vault), "--json").stdout)
        self.assertTrue(all(row["attested"] is False for row in listed))

    def test_ledger_append_overhead_is_small(self):
        timings = []
        for number in range(40):
            started = time.perf_counter()
            orchestrate.ledger_append(self.vault, {"event": "verify", "task_id": f"t{number}",
                                                   "verdict": "verified"})
            timings.append(time.perf_counter() - started)
        timings.sort()
        median = timings[len(timings) // 2]
        # Measured well under 5 ms here; the bound is loose so a busy CI host passes.
        self.assertLess(median, 0.05, f"median ledger append {median * 1000:.2f} ms")
        self.assertEqual(orchestrate.read_ledger(self.vault)[1], [])


if __name__ == "__main__":
    unittest.main()
