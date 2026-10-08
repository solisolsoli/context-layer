"""`context-layer doctor`: offline, read-only checks of a vault and its host wiring.

Every test owns a disposable, fictional vault, a project directory and a HOME inside
its temp directory. The doctor is run as `python -m context_layer.doctor` (the same
command `context-layer doctor` runs once cli.py registers it); no host is started.
"""
import hashlib
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from _portable_helpers import isolated_home_env

REPO = Path(os.environ.get("TEST_REPO_HOME", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO))


def append_hook_args(command, *arguments):
    """Append argv using the installed hook's shell grammar on this platform."""
    if os.name == "nt":
        rendered = " ".join("'" + argument.replace("'", "''") + "'"
                            for argument in arguments)
    else:
        rendered = " ".join(arguments)
    return command + " " + rendered


class Doctor(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / "vault"
        self.project = self.root / "project"
        self.home = self.root / "home"
        for path in (self.vault / "notes", self.project, self.home, self.root / "bin"):
            path.mkdir(parents=True)
        (self.vault / "notes" / "lamp.md").write_text("# Lamp\nThe lamp wick is trimmed weekly.\n")
        # No `context-layer` script and no `claude` on PATH: the installers write this
        # interpreter plus PYTHONPATH, and nothing can reach a real host.
        self.env = dict(isolated_home_env(os.environ, self.home), PATH=str(self.root / "bin"))
        for name in ("CODEX_HOME", "CLAUDE_CODE_SESSION_ID", "CONTEXT_LAYER_SESSION_EVIDENCE"):
            self.env.pop(name, None)
        (self.vault / ".context").mkdir()
        (self.vault / ".context" / "routes.json").write_text(json.dumps({
            "schema_version": 1, "record_type_allowlist": ["verbatim_text_file"],
            "routes": {"lamp": {"priority": 10, "triggers": ["lamp"],
                                "canonical_sources": [], "path_hints": []}},
            "fallback_routes": [], "aliases": {}, "exclude_prefixes": ["private"]}))
        built = self.cli("index", str(self.vault))
        self.assertEqual(built.returncode, 0, built.stderr)

    def cli(self, *argv):
        return subprocess.run([sys.executable, "-m", "context_layer.cli", *argv], cwd=REPO,
                              capture_output=True, text=True, env=self.env)

    def doctor(self, *flags, host="claude-code", env=None):
        done = subprocess.run([sys.executable, "-m", "context_layer.doctor", str(self.vault),
                               "--host", host, "--project", str(self.project), "--json",
                               *flags], cwd=REPO, capture_output=True, text=True,
                              env=env or self.env)
        report = json.loads(done.stdout)
        return done.returncode, {check["name"]: check for check in report["checks"]}, report

    def install(self, *flags, host="claude-code", env=None):
        done = subprocess.run([sys.executable, "-m", "context_layer.cli", "install", host,
                               "--vault", str(self.vault), "--project", str(self.project),
                               *flags, "--apply"], cwd=REPO, capture_output=True, text=True,
                              env=env or self.env)
        self.assertEqual(done.returncode, 0, done.stderr)

    def settings(self):
        return self.project / ".claude" / "settings.json"

    def edit_hook(self, event, replace):
        data = json.loads(self.settings().read_text())
        handler = data["hooks"][event][0]["hooks"][0]
        command = handler["command"]
        if os.name == "nt":
            tokens = command.split()
            script = base64.b64decode(tokens[-1]).decode("utf-16le")
            terminator = "; exit $LASTEXITCODE"
            self.assertTrue(script.endswith(terminator), script)
            script = replace(script[:-len(terminator)]) + terminator
            tokens[-1] = base64.b64encode(script.encode("utf-16le")).decode("ascii")
            handler["command"] = " ".join(tokens)
        else:
            handler["command"] = replace(command)
        self.settings().write_text(json.dumps(data, indent=2) + "\n")

    def snapshot(self):
        state = {}
        for base in (self.vault, self.project, self.home):
            for path in sorted(base.rglob("*")):
                if path.is_file():
                    stat = path.stat()
                    state[str(path)] = (stat.st_mtime_ns, hashlib.sha256(path.read_bytes())
                                        .hexdigest())
                else:
                    state[str(path)] = "dir"
        return state

    def test_a_healthy_install_passes_and_nothing_is_written(self):
        self.install("--hook", "--rules")
        before = self.snapshot()
        code, checks, report = self.doctor()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["schema"], "context-layer-doctor/v1")
        self.assertEqual(report["failed"], 0, report)
        for name in ("python", "sqlite fts5", "vault", "routes.json", "index.sqlite",
                     "graph.sqlite", "rule files", ".mcp.json context-layer",
                     "settings.json UserPromptSubmit", "settings.json SessionStart",
                     "settings.json Stop"):
            self.assertEqual(checks[name]["status"], "ok", (name, checks.get(name)))
        self.assertEqual(self.snapshot(), before)               # read-only
        table = subprocess.run([sys.executable, "-m", "context_layer.doctor", str(self.vault),
                                "--host", "claude-code", "--project", str(self.project)],
                               cwd=REPO, capture_output=True, text=True, env=self.env)
        self.assertEqual(table.returncode, 0)
        self.assertTrue(table.stdout.startswith("check"))
        self.assertIn("doctor: 0 failed", table.stdout)

    def test_hook_lines_are_parsed_with_this_builds_flags(self):
        self.install("--hook", "--rules")
        for edit, expect, fragment in (
                (lambda c: append_hook_args(c, "--top-k", "99"), "fail", "above the cap"),
                (lambda c: append_hook_args(c, "--extra-tokens", "lots"), "fail", "invalid int"),
                (lambda c: append_hook_args(c, "--future-flag", "1"), "fail", "unrecognised"),
                (lambda c: append_hook_args(c, "--max-context-chars", "50"),
                 "fail", "max-context-chars"),
                (lambda c: append_hook_args(c, "--method", "unknown"), "warn", "would run fts")):
            with self.subTest(fragment=fragment):
                self.install("--hook", "--rules")
                self.edit_hook("UserPromptSubmit", edit)
                code, checks, _ = self.doctor()
                check = checks["settings.json UserPromptSubmit"]
                self.assertEqual(check["status"], expect, check)
                self.assertIn(fragment, check["detail"])
                self.assertEqual(code, 1 if expect == "fail" else 0)
        self.install("--hook", "--rules")
        data = json.loads(self.settings().read_text())
        data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["timeout"] = 10
        self.settings().write_text(json.dumps(data))
        code, checks, _ = self.doctor()
        self.assertEqual(checks["settings.json UserPromptSubmit"]["status"], "warn")
        self.assertIn("timeout", checks["settings.json UserPromptSubmit"]["detail"])
        self.assertEqual(code, 0)
        self.install("--hook", "--rules")
        if os.name == "nt":
            self.edit_hook("Stop", lambda c: c.replace(
                "'rules' 'hook' 'stop'", "'rules' 'hook' 'stopp'"))
        else:
            self.edit_hook("Stop", lambda c: c.replace("rules hook stop", "rules hook stopp"))
        code, checks, _ = self.doctor()
        self.assertEqual(checks["settings.json Stop"]["status"], "fail")
        self.assertEqual(code, 1)

    def test_machine_specific_paths_must_exist(self):
        self.install("--hook")
        missing_launcher = self.root / "gone" / ("python.exe" if os.name == "nt" else "python3")
        self.edit_hook("UserPromptSubmit",
                       lambda c: c.replace(sys.executable, str(missing_launcher)))
        code, checks, _ = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("does not exist", checks["settings.json UserPromptSubmit"]["detail"])
        self.install("--hook")
        if os.name == "nt":
            self.edit_hook("UserPromptSubmit", lambda c: re.sub(
                r"\$env:PYTHONPATH='[^']*'",
                lambda _match: f"$env:PYTHONPATH='{self.root / 'nowhere'}'", c, count=1))
        else:
            self.edit_hook("UserPromptSubmit", lambda c: re.sub(
                r"PYTHONPATH=\S+", lambda _match: f"PYTHONPATH={self.root / 'nowhere'}", c, count=1))
        code, checks, _ = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("PYTHONPATH", checks["settings.json UserPromptSubmit"]["detail"])
        mcp = self.project / ".mcp.json"
        data = json.loads(mcp.read_text())
        args = data["mcpServers"]["context-layer"]["args"]
        args[args.index("--vault") + 1] = str(self.root / "moved-vault")
        mcp.write_text(json.dumps(data))
        self.install("--hook")                                   # restores the hook only
        mcp.write_text(json.dumps(data))
        code, checks, _ = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("vault not found", checks[".mcp.json context-layer"]["detail"])

    def test_vault_state_and_rule_parity(self):
        (self.vault / "CLAUDE.md").write_text("# Rules\nA\n")
        (self.vault / "AGENTS.md").write_text("# Rules\nB\n")
        code, checks, _ = self.doctor(host="generic")
        self.assertEqual(code, 1)
        self.assertEqual(checks["rule files"]["status"], "fail")
        (self.vault / "CLAUDE.md").write_text("@AGENTS.md\n")         # single source: fine
        code, checks, _ = self.doctor(host="generic")
        self.assertEqual(checks["rule files"]["status"], "ok")
        self.assertEqual(code, 0)
        (self.vault / ".context" / "index.sqlite").unlink()
        code, checks, _ = self.doctor(host="generic")
        self.assertEqual(code, 1)
        self.assertEqual(checks["index.sqlite"]["status"], "fail")
        self.assertIn("context-layer index", checks["index.sqlite"]["detail"])
        (self.vault / ".context" / "routes.json").write_text('{"routes": {} "x": 1}')
        code, checks, _ = self.doctor(host="generic")
        self.assertEqual(checks["routes.json"]["status"], "fail")

    def test_codex_config_and_hooks(self):
        codex_home = self.root / "codex"
        codex_home.mkdir()
        env = dict(self.env, CODEX_HOME=str(codex_home))
        self.install("--hook", "--rules", host="codex", env=env)
        code, checks, report = self.doctor(host="codex", env=env)
        self.assertEqual(code, 0, report)
        self.assertEqual(checks["hooks.json UserPromptSubmit"]["status"], "ok")
        if sys.version_info >= (3, 11):
            self.assertEqual(checks["config.toml [mcp_servers.context_layer]"]["status"], "ok")
            config = codex_home / "config.toml"
            config.write_text(config.read_text().replace("tool_timeout_sec = 180",
                                                         "tool_timeout_sec = 30"))
            code, checks, _ = self.doctor(host="codex", env=env)
            self.assertEqual(checks["codex tool_timeout_sec"]["status"], "warn")
            self.assertEqual(code, 0)
            config.write_text("[broken\n")
            code, checks, _ = self.doctor(host="codex", env=env)
            self.assertEqual(code, 1)
            self.assertEqual(checks["config.toml"]["status"], "fail")
        code, checks, _ = self.doctor(host="codex",
                                      env=dict(self.env, CODEX_HOME=str(self.root / "none")))
        self.assertEqual(checks["CODEX_HOME"]["status"], "fail")
        self.assertEqual(code, 1)

    def test_the_cli_registers_doctor(self):
        from context_layer import cli
        if "doctor" not in cli.build_parser()._subparsers._group_actions[0].choices:
            self.skipTest("`context-layer doctor` needs the cli.py registration requested in "
                          "the W2a report; cli.py is not W2a's file")
        done = self.cli("doctor", str(self.vault))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("doctor: 0 failed", done.stdout)


if __name__ == "__main__":
    unittest.main()
