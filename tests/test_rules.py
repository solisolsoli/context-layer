"""Tests for context_layer.rules - every test owns a disposable, fictional vault.

Runnable as `python3 tests/test_rules.py`. The CLI is exercised through a small
driver that registers `rules` exactly as cli.py does, so the tests do not depend
on cli.py having been wired yet. HOME points into each test's temporary folder
and no host session id leaks in from the environment.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO = Path(os.environ.get("TEST_REPO_HOME", Path(__file__).resolve().parents[1]))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from context_layer import rules  # noqa: E402

DRIVER = (
    "import argparse, sys\n"
    "from context_layer import rules\n"
    "parser = argparse.ArgumentParser(prog='context-layer')\n"
    "sub = parser.add_subparsers(dest='command', required=True)\n"
    "rules.register(sub)\n"
    "args, extra = parser.parse_known_args(sys.argv[1:])\n"
    "args.rest = [t for t in extra if t != '--']\n"
    "raise SystemExit(args.func(args))\n"
)
TEMPLATES = REPO / "templates" / "vault"
DAY_NS = 86_400 * 1_000_000_000


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RulesBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        (self.vault / "notes").mkdir()
        (self.vault / "notes" / "garden.md").write_text("# Garden\nTomatoes in bed two.\n",
                                                       encoding="utf-8")

    def env(self):
        env = dict(os.environ)
        for name in ("CONTEXT_LAYER_TOOL", "CLAUDE_CODE_SESSION_ID", "CONTEXT_LAYER_HOME"):
            env.pop(name, None)
        env["HOME"] = str(self.home)
        env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
        return env

    def cli(self, *argv, stdin=None):
        return subprocess.run([sys.executable, "-c", DRIVER, "rules", *argv], cwd=REPO,
                              input=stdin, capture_output=True, text=True, env=self.env())

    def install(self):
        result = rules.init(self.vault, apply_now=True)
        self.assertTrue(result["parity"]["ok"], result["parity"])
        return result

    def write(self, relative, text="changed\n", future_days=0):
        """Write a note; with future_days its mtime is pushed that many days ahead."""
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if future_days:
            stamp = time.time_ns() + future_days * DAY_NS
            os.utime(path, ns=(stamp, stamp))
        return path


class Templates(unittest.TestCase):
    def test_rule_templates_are_byte_identical(self):
        self.assertEqual((TEMPLATES / "CLAUDE.md").read_bytes(),
                         (TEMPLATES / "AGENTS.md").read_bytes())

    def test_template_has_required_sections_and_markers(self):
        text = (TEMPLATES / "CLAUDE.md").read_text(encoding="utf-8")
        for needle in ("Rules cannot eliminate hallucination", "Plan first",
                       "Stop and ask", "Record every meaningful step", "Sub-agents",
                       "Privacy", "NOT_FOUND", "USER_STATED", "CONFLICT",
                       rules.START_MARK, rules.END_MARK, "CUSTOMIZE"):
            self.assertIn(needle, text)
        self.assertLessEqual(len(text.splitlines()), 250)

    def test_templates_are_ascii(self):
        for name in (*rules.TEMPLATE_FILES, rules.SINGLE_SOURCE_TEMPLATE):
            self.assertTrue((TEMPLATES / name).read_bytes().isascii(), name)

    def test_template_examples_are_placeholders(self):
        # B-05: realistic example text in the rules answered real queries.
        text = (TEMPLATES / "CLAUDE.md").read_text(encoding="utf-8").lower()
        for word in ("budget", "merge", "dashboard link"):
            self.assertNotIn(word, text)
        self.assertIn("<what was done, one line>", text)

    def test_single_source_template_imports_agents(self):
        text = (TEMPLATES / rules.SINGLE_SOURCE_TEMPLATE).read_text(encoding="utf-8")
        self.assertTrue(rules.imports_agents(text))
        self.assertEqual(text.splitlines()[0], "@AGENTS.md")


class Init(RulesBase):
    def test_dry_run_writes_nothing(self):
        result = self.cli("init", str(self.vault))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dry run", result.stderr)
        for name in rules.TEMPLATE_FILES:
            self.assertFalse((self.vault / name).exists(), name)

    def test_apply_creates_identical_files(self):
        result = self.cli("init", str(self.vault), "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in rules.TEMPLATE_FILES:
            self.assertEqual((self.vault / name).read_bytes(), (TEMPLATES / name).read_bytes())
        self.assertEqual(sha(self.vault / "CLAUDE.md"), sha(self.vault / "AGENTS.md"))

    def test_existing_file_is_not_overwritten(self):
        mine = "# My backlog\n- keep me\n"
        (self.vault / "BACKLOG.md").write_text(mine, encoding="utf-8")
        result = self.cli("init", str(self.vault), "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.vault / "BACKLOG.md").read_text(encoding="utf-8"), mine)
        self.assertIn("kept: BACKLOG.md", result.stdout)
        self.assertIn("+++ BACKLOG.md (template)", result.stdout)   # merge suggestion
        self.assertEqual(list(self.vault.glob("BACKLOG.md.bak-*")), [])

    def test_existing_rule_file_is_mirrored_not_split(self):
        mine = "# My rules\nBe brief.\n"
        (self.vault / "CLAUDE.md").write_text(mine, encoding="utf-8")
        result = rules.init(self.vault, apply_now=True)
        self.assertEqual((self.vault / "CLAUDE.md").read_text(encoding="utf-8"), mine)
        self.assertEqual((self.vault / "AGENTS.md").read_text(encoding="utf-8"), mine)
        self.assertTrue(result["parity"]["ok"])
        kept = [s for s in result["plan"] if s["file"] == "CLAUDE.md"][0]
        self.assertEqual(kept["action"], "keep")
        self.assertIn("Plan first", kept["diff"])

    def test_force_replaces_after_backup(self):
        mine = "# My rules\nBe brief.\n"
        (self.vault / "CLAUDE.md").write_text(mine, encoding="utf-8")
        (self.vault / "AGENTS.md").write_text(mine, encoding="utf-8")
        result = self.cli("init", str(self.vault), "--apply", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        backups = sorted(self.vault.glob("CLAUDE.md.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), mine)
        self.assertEqual((self.vault / "CLAUDE.md").read_bytes(),
                         (TEMPLATES / "CLAUDE.md").read_bytes())
        self.assertEqual(len(list(self.vault.glob("AGENTS.md.bak-*"))), 1)

    def test_init_json(self):
        result = self.cli("init", str(self.vault), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertFalse(data["applied"])
        self.assertEqual({s["file"] for s in data["plan"]},
                         set(rules.TEMPLATE_FILES) | {rules.ROUTES_FILE})
        self.assertTrue(all(s["action"] == "create" for s in data["plan"]
                            if s["file"] in rules.TEMPLATE_FILES))
        self.assertEqual([s["action"] for s in data["plan"] if s["file"] == rules.ROUTES_FILE],
                         ["absent"])
        self.assertNotIn(str(self.vault.parent), result.stdout)   # no absolute paths

    def test_missing_vault(self):
        result = self.cli("init", str(self.vault / "nope"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("vault not found", result.stderr)


class RuleFilesLeaveSearch(RulesBase):
    """B-05: the hosts load CLAUDE.md and AGENTS.md; search must not serve them again."""

    ROUTES = {"schema_version": 1,
              "routes": {"agents": {"triggers": ["agents"],
                                    "canonical_sources": [{"path": "AGENTS.md"}]},
                         "notes": {"triggers": ["garden"],
                                   "canonical_sources": [{"path": "notes/garden.md"},
                                                         {"path": "CLAUDE.md"}]}},
              "exclude_prefixes": ["archive/"],
              "retrieval_exclude_prefixes": ["archive/", ".context"]}

    def routes(self):
        return json.loads((self.vault / ".context" / "routes.json").read_text(encoding="utf-8"))

    def test_init_adds_rule_files_to_routes_exclusions(self):
        (self.vault / ".context").mkdir()
        original = json.dumps(self.ROUTES, indent=2) + "\n"
        (self.vault / ".context" / "routes.json").write_text(original, encoding="utf-8")
        dry = self.cli("init", str(self.vault))
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn(f"would write: {rules.ROUTES_FILE} (update)", dry.stdout)
        self.assertIn('+    "CLAUDE.md"', dry.stdout)
        self.assertEqual((self.vault / ".context" / "routes.json").read_text(encoding="utf-8"),
                         original)
        done = self.cli("init", str(self.vault), "--apply")
        self.assertEqual(done.returncode, 0, done.stderr)
        config = self.routes()
        for key in rules.EXCLUSION_KEYS:
            self.assertIn("CLAUDE.md", config[key])
            self.assertIn("AGENTS.md", config[key])
            self.assertNotIn("LOG.md", config[key])
            self.assertNotIn("BACKLOG.md", config[key])
        self.assertNotIn("agents", config["routes"])            # only source was AGENTS.md
        self.assertEqual(config["routes"]["notes"]["canonical_sources"],
                         [{"path": "notes/garden.md"}])
        backups = list((self.vault / ".context").glob("routes.json.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), original)
        self.assertIn(".context/routes.json.bak-", done.stdout)
        again = rules.init(self.vault, apply_now=True)
        self.assertEqual([s["action"] for s in again["plan"] if s["file"] == rules.ROUTES_FILE],
                         ["unchanged"])

    def test_init_without_routes_says_how(self):
        result = self.cli("init", str(self.vault), "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no .context/routes.json yet", result.stderr)
        self.assertFalse((self.vault / ".context").exists())

    def test_invalid_routes_is_left_alone(self):
        (self.vault / ".context").mkdir()
        bad = '{"routes": {} "exclude_prefixes": []}'
        (self.vault / ".context" / "routes.json").write_text(bad, encoding="utf-8")
        result = self.cli("init", str(self.vault), "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not added to its exclusions", result.stderr)
        self.assertEqual((self.vault / ".context" / "routes.json").read_text(encoding="utf-8"),
                         bad)
        self.assertTrue((self.vault / "CLAUDE.md").is_file())

    def test_exclude_rule_files_is_pure(self):
        before = json.loads(json.dumps(self.ROUTES))
        after = rules.exclude_rule_files(self.ROUTES)
        self.assertEqual(self.ROUTES, before)
        self.assertEqual(rules.exclude_rule_files(after), after)


class Check(RulesBase):
    def test_identical_files_pass(self):
        self.install()
        result = self.cli("check", str(self.vault))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(sha(self.vault / "CLAUDE.md"), result.stdout)

    def test_divergence_fails_with_line(self):
        self.install()
        path = self.vault / "AGENTS.md"
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        lines[2] = "An extra rule only Codex sees.\n"
        path.write_text("".join(lines), encoding="utf-8")
        result = self.cli("check", str(self.vault))
        self.assertEqual(result.returncode, 1)
        self.assertIn("differ (first difference at line 3)", result.stderr)

    def test_line_ending_only_divergence_names_the_line_endings(self):
        # F2-46: CRLF against LF used to be reported as a difference at the last line.
        self.install()
        agents = self.vault / "AGENTS.md"
        agents.write_bytes(agents.read_bytes().replace(b"\n", b"\r\n"))
        result = self.cli("check", str(self.vault))
        self.assertEqual(result.returncode, 1)
        self.assertIn("differ only in line endings (AGENTS.md uses CRLF, CLAUDE.md uses LF)",
                      result.stderr)
        self.assertNotIn("first difference", result.stderr)
        as_json = json.loads(self.cli("check", str(self.vault), "--json").stdout)
        self.assertIsNone(as_json["first_difference_line"])

    def test_missing_file_fails(self):
        self.install()
        (self.vault / "AGENTS.md").unlink()
        result = self.cli("check", str(self.vault))
        self.assertEqual(result.returncode, 1)
        self.assertIn("AGENTS.md is missing", result.stderr)

    def test_check_json(self):
        self.install()
        ok = json.loads(self.cli("check", str(self.vault), "--json").stdout)
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["mode"], "twins")
        self.assertEqual(ok["files"]["CLAUDE.md"]["sha256"], ok["files"]["AGENTS.md"]["sha256"])
        (self.vault / "CLAUDE.md").write_text("drift\n", encoding="utf-8")
        result = self.cli("check", str(self.vault), "--json")
        self.assertEqual(result.returncode, 1)
        bad = json.loads(result.stdout)
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["first_difference_line"], 1)


class SingleSource(RulesBase):
    """N10: `rules init --single-source` - CLAUDE.md imports AGENTS.md; twins stay default."""

    def test_init_check_and_record(self):
        result = self.cli("init", str(self.vault), "--single-source", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.vault / "CLAUDE.md").read_bytes(),
                         (TEMPLATES / rules.SINGLE_SOURCE_TEMPLATE).read_bytes())
        self.assertEqual((self.vault / "AGENTS.md").read_bytes(),
                         (TEMPLATES / "AGENTS.md").read_bytes())
        check = self.cli("check", str(self.vault))
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn("CLAUDE.md imports AGENTS.md", check.stdout)
        claude_before = (self.vault / "CLAUDE.md").read_bytes()
        stored = rules.record(self.vault, summary="Planted tomatoes", files=["notes/garden.md"],
                              verified="re-read", next_step="water")
        self.assertEqual(stored["mode"], "single-source")
        self.assertEqual((self.vault / "CLAUDE.md").read_bytes(), claude_before)
        agents = (self.vault / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Planted tomatoes", rules._records_region(agents))
        self.assertIn("Planted tomatoes", (self.vault / "LOG.md").read_text(encoding="utf-8"))
        self.assertEqual(rules.records_file(self.vault), "AGENTS.md")

    def test_missing_agents_is_reported(self):
        (self.vault / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        result = self.cli("check", str(self.vault))
        self.assertEqual(result.returncode, 1)
        self.assertIn("AGENTS.md is missing (CLAUDE.md imports it)", result.stderr)

    def test_existing_twins_are_kept_without_force(self):
        self.install()
        before = (self.vault / "CLAUDE.md").read_bytes()
        result = rules.init(self.vault, apply_now=True, single_source=True)
        self.assertEqual((self.vault / "CLAUDE.md").read_bytes(), before)
        step = [s for s in result["plan"] if s["file"] == "CLAUDE.md"][0]
        self.assertEqual(step["action"], "keep")
        self.assertIn("--force replaces it", step["note"])
        forced = rules.init(self.vault, apply_now=True, single_source=True, force=True)
        self.assertTrue(rules.imports_agents((self.vault / "CLAUDE.md").read_bytes()))
        self.assertIn("CLAUDE.md.bak-", " ".join(forced["backups"]))
        self.assertEqual(rules.parity(self.vault)["mode"], "single-source")

    def test_twins_remain_the_default(self):
        self.install()
        self.assertEqual(rules.parity(self.vault)["mode"], "twins")
        self.assertFalse(rules.imports_agents((self.vault / "CLAUDE.md").read_bytes()))


class Record(RulesBase):
    def rec(self, *extra):
        return self.cli("record", str(self.vault), "--summary", "Planted tomatoes",
                        "--files", "notes/garden.md", "--verified", "note re-read",
                        "--next", "water on Friday", *extra)

    def test_record_appends_identical_entries_and_keeps_parity(self):
        self.install()
        log_before = (self.vault / "LOG.md").read_text(encoding="utf-8")
        result = self.rec("--why", "spring plan", "--link", "[[garden]]", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data["ok"] and data["parity"])
        claude = (self.vault / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertEqual(claude, (self.vault / "AGENTS.md").read_text(encoding="utf-8"))
        region = rules._records_region(claude)
        self.assertIn("UTC - Planted tomatoes", region)
        self.assertIn("- Why: spring plan", region)
        self.assertIn(f"`notes/garden.md` ({sha(self.vault / 'notes/garden.md')[:12]})", region)
        self.assertIn("- Verified: note re-read", region)
        self.assertIn("- Next: water on Friday", region)
        self.assertIn(f"[[LOG#^{data['id']}]], [[garden]]", region)
        log = (self.vault / "LOG.md").read_text(encoding="utf-8")
        self.assertTrue(log.startswith(log_before))                 # append-only
        self.assertIn(f"^{data['id']}", log)
        self.assertIn("- Remaining / next step: water on Friday", log)
        self.assertEqual(self.cli("check", str(self.vault)).returncode, 0)

    def test_second_record_goes_after_the_first(self):
        self.install()
        self.assertEqual(self.rec().returncode, 0)
        result = self.cli("record", str(self.vault), "--summary", "Second step",
                          "--files", "none", "--verified", "n/a", "--next", "none")
        self.assertEqual(result.returncode, 0, result.stderr)
        region = rules._records_region((self.vault / "CLAUDE.md").read_text(encoding="utf-8"))
        self.assertLess(region.index("Planted tomatoes"), region.index("Second step"))
        self.assertIn("- Files: none", region)

    def test_keep_trims_rule_files_but_not_log(self):
        self.install()
        for index in range(4):
            rules.record(self.vault, summary=f"step {index}", files=["none"], verified="ok",
                         next_step="more", keep=2)
        region = rules._records_region((self.vault / "CLAUDE.md").read_text(encoding="utf-8"))
        self.assertEqual(region.count("### "), 2)
        self.assertNotIn("step 0", region)
        self.assertIn("step 3", region)
        log = (self.vault / "LOG.md").read_text(encoding="utf-8")
        self.assertEqual(sum(f"- step {i}" in log for i in range(4)), 4)

    def test_record_refuses_broken_parity(self):
        self.install()
        (self.vault / "AGENTS.md").write_text("other\n", encoding="utf-8")
        before = (self.vault / "CLAUDE.md").read_bytes()
        result = self.rec()
        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing to record while parity is broken", result.stderr)
        self.assertEqual((self.vault / "CLAUDE.md").read_bytes(), before)

    def test_record_adds_section_when_markers_missing(self):
        text = "# House rules\nBe brief.\n"
        (self.vault / "CLAUDE.md").write_text(text, encoding="utf-8")
        (self.vault / "AGENTS.md").write_text(text, encoding="utf-8")
        self.assertEqual(self.rec().returncode, 0)
        claude = (self.vault / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertTrue(claude.startswith(text))
        self.assertIn(rules.RECORDS_HEADING, claude)
        self.assertIn("Planted tomatoes", rules._records_region(claude))
        self.assertTrue((self.vault / "LOG.md").is_file())

    def test_record_refuses_paths_outside_the_vault(self):
        self.install()
        for bad in ("../secret.md", "/etc/hosts"):
            result = self.cli("record", str(self.vault), "--summary", "x", "--files", bad,
                              "--verified", "x", "--next", "x")
            self.assertEqual(result.returncode, 1)
            self.assertIn("vault-relative", result.stderr)

    def test_record_requires_text(self):
        self.install()
        result = self.cli("record", str(self.vault), "--summary", "  ", "--files", "none",
                          "--verified", "x", "--next", "x", "--json")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)["ok"])

    def test_missing_rule_files(self):
        result = self.rec()
        self.assertEqual(result.returncode, 1)
        self.assertIn("rules init", result.stderr)


class RecordIntegrity(RulesBase):
    """B-14 to B-17: the records section, encodings, line endings and the lock."""

    def test_end_marker_in_text_cannot_break_the_section(self):
        self.install()
        result = self.cli("record", str(self.vault), "--summary",
                          f"First step {rules.END_MARK} trailing <!-- x",
                          "--files", "none", "--verified", "ok -->", "--next", "none")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("'< !--'", result.stderr)
        for index in (2, 3):
            rules.record(self.vault, summary=f"step {index}", files=["none"], verified="ok",
                         next_step="none")
        text = (self.vault / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertEqual(text.count(rules.END_MARK), 1)
        self.assertEqual(text.count(rules.START_MARK), 1)
        region = rules._records_region(text)
        self.assertEqual(region.count("### "), 3)
        self.assertLess(region.index("First step"), region.index("step 2"))
        self.assertLess(region.index("step 2"), region.index("step 3"))
        self.assertIn("< !-- context-layer:records:end -- >", region)
        log = (self.vault / "LOG.md").read_text(encoding="utf-8")
        self.assertNotIn(rules.END_MARK, log)

    def test_non_utf8_rule_files_fail_in_one_line(self):
        latin = "# Regeln f\u00fcr Agenten\n".encode("latin-1")
        for name in rules.RULE_FILES:
            (self.vault / name).write_bytes(latin)
        runs = {
            "record": self.cli("record", str(self.vault), "--summary", "x", "--files", "none",
                               "--verified", "x", "--next", "x"),
            "init": self.cli("init", str(self.vault)),
            "session-start": self.cli("hook", "session-start", "--vault", str(self.vault),
                                      stdin=json.dumps({"session_id": "s1"})),
            "stop": self.cli("hook", "stop", "--vault", str(self.vault),
                             stdin=json.dumps({"session_id": "s1"})),
        }
        for label, result in runs.items():
            with self.subTest(label):
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertIn("is not UTF-8", result.stderr)
                self.assertEqual(len(result.stderr.strip().splitlines()), 1, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_crlf_files_get_crlf_entries(self):
        self.install()
        for name in (*rules.RULE_FILES, rules.LOG_FILE):
            path = self.vault / name
            path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        rules.record(self.vault, summary="CRLF step", files=["notes/garden.md"],
                     verified="ok", next_step="none", why="line endings")
        for name in (*rules.RULE_FILES, rules.LOG_FILE):
            data = (self.vault / name).read_bytes()
            self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), name)
            self.assertIn(b"CRLF step", data)
        self.assertTrue(rules.parity(self.vault)["ok"])

    def test_lf_files_stay_lf(self):
        self.install()
        rules.record(self.vault, summary="LF step", files=["none"], verified="ok",
                     next_step="none")
        for name in (*rules.RULE_FILES, rules.LOG_FILE):
            self.assertNotIn(b"\r", (self.vault / name).read_bytes(), name)

    def test_stale_lock_marker_is_recovered(self):
        """B-17: without fcntl, a marker left by a holder that died is broken by age."""
        self.install()
        marker = self.vault / ".context" / "rules.excl"
        marker.parent.mkdir(exist_ok=True)
        marker.write_text(f"999999 {time.time() - 10 * rules.STALE_LOCK_S:.3f}\n",
                          encoding="ascii")
        with mock.patch.object(rules, "fcntl", None), \
                mock.patch.object(rules, "LOCK_TIMEOUT", 1.0):
            stored = rules.record(self.vault, summary="after a crash", files=["none"],
                                  verified="ok", next_step="none")
        self.assertTrue(stored["parity"])
        self.assertFalse(marker.exists())

    def test_live_lock_marker_times_out_with_the_file_named(self):
        self.install()
        marker = self.vault / ".context" / "rules.excl"
        marker.parent.mkdir(exist_ok=True)
        marker.write_text(f"{os.getpid()} {time.time():.3f}\n", encoding="ascii")
        with mock.patch.object(rules, "fcntl", None), \
                mock.patch.object(rules, "LOCK_TIMEOUT", 0.2):
            with self.assertRaises(rules.RulesError) as caught:
                rules.record(self.vault, summary="blocked", files=["none"], verified="ok",
                             next_step="none")
        self.assertIn(".context/rules.excl", str(caught.exception))
        self.assertTrue(marker.exists())


class HookBase(RulesBase):
    def hook(self, event, payload, *flags):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        return self.cli("hook", event, "--vault", str(self.vault), *flags, stdin=raw)

    def start(self, sid="s1", **extra):
        result = self.hook("session-start", {"session_id": sid, "source": "startup", **extra})
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def agent_wrote(self, relative, sid="s1", tool="Write", **write):
        path = self.write(relative, **write)
        result = self.hook("post-tool-use", {
            "session_id": sid, "hook_event_name": "PostToolUse", "tool_name": tool,
            "tool_input": {"file_path": str(path), "content": "..."},
            "tool_response": {"filePath": str(path), "type": "update"}})
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        return path

    def stop(self, sid="s1", **extra):
        result = self.hook("stop", {"session_id": sid, "stop_hook_active": False, **extra})
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def state(self):
        return json.loads((self.vault / ".context" / rules.STATE_NAME).read_text(encoding="utf-8"))

    def state_bytes(self):
        found = b""
        for path in sorted((self.vault / ".context").rglob("*")):
            if path.is_file() and (path.name == rules.STATE_NAME
                                   or path.parent.name == rules.SNAPSHOT_DIR):
                found += path.read_bytes()
        return found


class Hooks(HookBase):
    def test_session_start_snapshot(self):
        self.install()
        result = self.start()
        self.assertEqual(result.stdout, "")          # parity fine: no context injected
        session = self.state()["sessions"]["s1"]
        self.assertEqual(session["sha256"]["CLAUDE.md"], sha(self.vault / "CLAUDE.md"))
        self.assertEqual(session["sha256"]["AGENTS.md"], sha(self.vault / "AGENTS.md"))
        self.assertEqual(session["source"], "startup")
        self.assertRegex(session["started_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        snapshot = json.loads((self.vault / ".context" / rules.SNAPSHOT_DIR
                               / session["snapshot"]).read_text(encoding="utf-8"))
        size, _, digest = snapshot["files"]["notes/garden.md"]
        self.assertEqual(size, (self.vault / "notes" / "garden.md").stat().st_size)
        self.assertEqual(digest, sha(self.vault / "notes" / "garden.md"))
        self.assertNotIn("CLAUDE.md", snapshot["files"])     # record files are not work

    def test_session_start_reports_broken_parity(self):
        self.install()
        (self.vault / "AGENTS.md").write_text("x\n", encoding="utf-8")
        result = self.hook("session-start", {"session_id": "s1", "source": "startup"})
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "SessionStart")
        self.assertIn("byte-identical", output["additionalContext"])

    def test_session_start_without_rules_writes_nothing(self):
        """B-18: a folder without rule files gets no .context at all."""
        result = self.hook("session-start", {"session_id": "s1", "source": "startup"})
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.agent_wrote("notes/new.md")
        self.assertIsNone(self.stop())
        self.assertFalse((self.vault / ".context").exists())

    def test_compact_keeps_the_existing_snapshot(self):
        self.install()
        self.start()
        self.agent_wrote("notes/garden.md", text="# Garden\nPeppers too.\n")
        self.hook("session-start", {"session_id": "s1", "source": "compact"})
        decision = self.stop()
        self.assertEqual(decision["decision"], "block")          # the change survived compaction
        self.assertIn("notes/garden.md", decision["reason"])

    def test_stop_allows_when_nothing_changed(self):
        self.install()
        self.start()
        self.assertIsNone(self.stop())

    def test_agent_write_blocks_exactly_once(self):
        """B-03: an agent Write gives one block; the same path is never asked again."""
        self.install()
        self.start()
        self.agent_wrote("notes/garden.md", text="# Garden\nPeppers too.\n")
        first = self.stop()
        self.assertEqual(first["decision"], "block")
        self.assertIn("notes/garden.md", first["reason"])
        self.assertIn("context-layer rules record", first["reason"])
        # Claude Code continues; its next stop carries stop_hook_active=true.
        self.assertIsNone(self.stop(stop_hook_active=True))
        # Even without the flag, the same unrecorded path is not asked about twice.
        self.assertIsNone(self.stop())
        self.agent_wrote("notes/garden.md", text="# Garden\nPeppers and beans.\n")
        self.assertIsNone(self.stop())
        # A new agent-written path is asked about once more, and only that path.
        self.agent_wrote("notes/new.md", tool="Edit")
        fourth = self.stop()
        self.assertEqual(fourth["decision"], "block")
        self.assertIn("notes/new.md", fourth["reason"])
        self.assertNotIn("notes/garden.md", fourth["reason"])

    def test_user_edit_alone_never_blocks(self):
        """B-03: the user's own edit (no PostToolUse) is reported once, never asked about."""
        self.install()
        self.start()
        self.write("notes/garden.md", "# Garden\nEdited in Obsidian.\n")
        (self.vault / "notes" / "old.md").write_text("gone soon\n", encoding="utf-8")
        output = self.stop()
        self.assertNotIn("decision", output)
        self.assertIn("did not write", output["systemMessage"])
        self.assertIn("notes/garden.md", output["systemMessage"])
        self.assertIsNone(self.stop())                           # reported once
        (self.vault / "notes" / "old.md").unlink()
        self.assertIsNone(self.stop())                           # created and deleted: nothing new

    def test_deleted_note_is_reported(self):
        self.install()
        self.start()
        (self.vault / "notes" / "garden.md").unlink()
        output = self.stop()
        self.assertNotIn("decision", output)
        self.assertIn("notes/garden.md", output["systemMessage"])

    def test_plan_mode_never_blocks(self):
        self.install()
        self.start(permission_mode="plan")
        self.agent_wrote("notes/garden.md", text="# Garden\nPlanned.\n")
        self.assertIsNone(self.stop(permission_mode="plan"))
        later = self.stop(permission_mode="default")
        self.assertEqual(later["decision"], "block")             # asked once plan mode ends

    def test_plan_mode_reports_broken_parity_without_blocking(self):
        self.install()
        self.start()
        with open(self.vault / "CLAUDE.md", "a", encoding="utf-8") as handle:
            handle.write("\nA rule only Claude sees.\n")
        output = self.stop(permission_mode="plan")
        self.assertNotIn("decision", output)
        self.assertIn("differ", output["systemMessage"])
        self.assertIsNone(self.stop(permission_mode="plan"))    # reported once
        self.assertEqual(self.stop()["decision"], "block")      # asked once plan mode ends

    def test_future_mtime_asks_at_most_once(self):
        """B-04: a future-dated file cannot trigger a request after every record."""
        self.install()
        self.start()
        self.agent_wrote("notes/synced.md", text="synced\n", future_days=1)
        self.write("notes/other.md", "from a fast clock\n", future_days=1)
        results = [self.stop()]
        rules.record(self.vault, summary="Synced note", files=["notes/synced.md"],
                     verified="re-read", next_step="none")
        for _ in range(4):
            results.append(self.stop())
        blocks = [r for r in results if r and r.get("decision") == "block"]
        self.assertEqual(len(blocks), 1)
        self.assertIn("notes/synced.md", blocks[0]["reason"])
        self.assertNotIn("notes/other.md", blocks[0]["reason"])
        self.assertTrue(all(r is None for r in results[1:]), results[1:])

    def test_same_bytes_with_a_new_mtime_is_not_a_change(self):
        self.install()
        self.start()
        path = self.vault / "notes" / "garden.md"
        data = path.read_bytes()
        self.agent_wrote("notes/garden.md", text=data.decode("utf-8"))
        later = time.time_ns() + 5_000_000_000
        os.utime(path, ns=(later, later))
        self.assertIsNone(self.stop())

    def test_excluded_paths_are_never_walked_stored_or_named(self):
        """B-13: exclusions from routes.json are honoured by every rules hook."""
        self.install()
        (self.vault / ".context").mkdir(exist_ok=True)
        (self.vault / ".context" / "routes.json").write_text(json.dumps(
            {"routes": {}, "exclude_prefixes": ["Records/"]}), encoding="utf-8")
        self.start()
        self.agent_wrote("Records/Tax 2026 - account numbers.md", text="private\n")
        self.write("Records/bank.md", "private\n")
        self.write("node_modules/pkg/index.js", "x\n")
        self.assertIsNone(self.stop())
        stored = self.state_bytes()
        self.assertNotIn(b"Records", stored)
        self.assertNotIn(b"node_modules", stored)

    def test_bad_routes_json_pauses_tracking_without_naming_paths(self):
        self.install()
        (self.vault / ".context").mkdir(exist_ok=True)
        (self.vault / ".context" / "routes.json").write_text("{broken", encoding="utf-8")
        started = self.start()
        self.assertIn("not tracked", json.loads(started.stdout)["systemMessage"])
        self.agent_wrote("notes/garden.md", text="changed\n")
        output = self.stop()
        self.assertNotIn("decision", output or {})
        self.assertNotIn(b"notes/garden.md", self.state_bytes())
        # Parity is still checked, and still asked about only once.
        with open(self.vault / "CLAUDE.md", "a", encoding="utf-8") as handle:
            handle.write("\nA rule only Claude sees.\n")
        self.assertEqual(self.stop()["decision"], "block")
        self.assertIsNone(self.stop())

    def test_stop_allows_after_record(self):
        self.install()
        self.start()
        self.agent_wrote("notes/garden.md", text="# Garden\nPeppers too.\n")
        rules.record(self.vault, summary="Garden update", files=["notes/garden.md"],
                     verified="re-read", next_step="none")
        self.assertIsNone(self.stop())
        self.assertIsNone(self.state()["sessions"]["s1"]["blocked"])
        self.assertEqual(self.state()["sessions"]["s1"]["attributed"], {})

    def test_stop_blocks_on_broken_parity(self):
        self.install()
        self.start()
        with open(self.vault / "CLAUDE.md", "a", encoding="utf-8") as handle:
            handle.write("\nA rule only Claude sees.\n")
        decision = self.stop()
        self.assertEqual(decision["decision"], "block")
        self.assertIn("Rule parity is broken", decision["reason"])
        self.assertIsNone(self.stop())                           # asked once per problem

    def test_stop_without_snapshot_starts_one(self):
        self.install()
        self.agent_wrote("notes/garden.md", sid="late", text="before the snapshot\n")
        self.assertIsNone(self.stop(sid="late"))
        self.assertTrue(self.state()["sessions"]["late"]["snapshot"])

    def test_stop_ignores_vault_without_rules(self):
        self.assertIsNone(self.stop())
        self.assertFalse((self.vault / ".context").exists())

    def test_post_tool_use_records_only_vault_file_tools(self):
        self.install()
        self.start()
        outside = self.root / "outside.md"
        outside.write_text("x\n", encoding="utf-8")
        payloads = [
            {"tool_name": "Read", "tool_input": {"file_path": str(self.vault / "notes/garden.md")}},
            {"tool_name": "Write", "tool_input": {"file_path": str(outside)}},
            {"tool_name": "Write", "tool_input": {"file_path": str(self.vault / "CLAUDE.md")}},
            {"tool_name": "Write", "tool_input": {"file_path": str(self.vault / ".obsidian/a.json")}},
            {"tool_name": "NotebookEdit", "tool_input": {"notebook_path": "notes/lab.ipynb"},
             "cwd": str(self.vault)},
            {"tool_name": "MultiEdit", "tool_input": {},
             "tool_response": {"filePath": str(self.vault / "notes/multi.md")}},
        ]
        for payload in payloads:
            result = self.hook("post-tool-use", {"session_id": "s1", **payload})
            self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        attributed = self.state()["sessions"]["s1"]["attributed"]
        self.assertEqual(sorted(attributed), ["notes/lab.ipynb", "notes/multi.md"])
        self.assertEqual(attributed["notes/lab.ipynb"]["tool"], "NotebookEdit")

    def test_malformed_input(self):
        self.install()
        for raw in ("not json", "[1, 2]", ""):
            for event in ("session-start", "post-tool-use", "stop"):
                result = self.hook(event, raw)
                self.assertEqual(result.returncode, 1, (event, raw))
                self.assertEqual(result.stdout, "")
                self.assertIn("hook JSON", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_usage_errors_exit_1_never_2(self):
        """A broken hook line must not exit 2 (Stop: forced continuation)."""
        self.install()
        cases = {
            "unknown event": ("hook", "stopp", "--vault", str(self.vault)),
            "no event": ("hook", "--vault", str(self.vault)),
            "no vault": ("hook", "stop"),
            "vault without value": ("hook", "stop", "--vault"),
            "unknown flag": ("hook", "stop", "--vault", str(self.vault), "--bogus"),
            "brief on stop": ("hook", "stop", "--vault", str(self.vault), "--brief"),
            "citations on start": ("hook", "session-start", "--vault", str(self.vault),
                                   "--check-citations"),
        }
        for label, argv in cases.items():
            with self.subTest(label):
                result = self.cli(*argv, stdin="{}")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(len(result.stderr.strip().splitlines()), 1, result.stderr)

    def test_corrupt_state_file_is_replaced(self):
        self.install()
        (self.vault / ".context").mkdir(exist_ok=True)
        (self.vault / ".context" / rules.STATE_NAME).write_text("{broken", encoding="utf-8")
        self.start()
        self.assertIn("s1", self.state()["sessions"])

    def test_hidden_and_record_files_do_not_count_as_work(self):
        self.install()
        self.start()
        self.write(".obsidian/workspace.json", "{}")
        self.write("BACKLOG.md", "# Backlog\n")
        self.assertIsNone(self.stop())

    def test_sessions_and_snapshots_are_bounded(self):
        self.install()
        from datetime import datetime, timedelta, timezone
        clock = [datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)]

        def tick():
            clock[0] += timedelta(minutes=1)
            return clock[0]

        with mock.patch.object(rules, "MAX_SESSIONS", 4), \
                mock.patch.object(rules, "MAX_SNAPSHOTS", 2), \
                mock.patch.object(rules, "_now", tick):
            for index in range(5):
                rules.hook_session_start(self.vault, {"session_id": f"s{index}",
                                                      "source": "startup"})
            state = self.state()
            self.assertEqual(sorted(state["sessions"]), ["s1", "s2", "s3", "s4"])
            snapshots = sorted(p.name for p in
                               (self.vault / ".context" / rules.SNAPSHOT_DIR).glob("*.json"))
            self.assertEqual(snapshots, ["s3.json", "s4.json"])
            self.assertIsNone(state["sessions"]["s1"]["snapshot"])
            # A session that lost its snapshot starts a new one and asks nothing yet.
            self.agent_wrote("notes/garden.md", sid="s1", text="changed\n")
            self.assertIsNone(self.stop(sid="s1"))


class BriefAndCitationHooks(HookBase):
    def test_session_start_brief_is_added_within_the_cap(self):
        self.install()
        rows = "".join(f"| B-{i} | area | open | [[LOG]] | {'long step ' * 30} | none |\n"
                       for i in range(2, 30))
        with open(self.vault / "BACKLOG.md", "a", encoding="utf-8") as handle:
            handle.write("\n| id | area | state | source | next step | blocker |\n"
                         "| --- | --- | --- | --- | --- | --- |\n" + rows)
        for index in range(6):
            rules.record(self.vault, summary=f"Step {index} {'detail ' * 20}", files=["none"],
                         verified="ok", next_step="none")
        from context_layer import memory
        for index in range(6):
            memory.record(self.vault, kind="task", text=f"Task {index} {'to do ' * 40}")
            memory.record(self.vault, kind="note", text=f"Note {index} {'noted ' * 40}")
        result = self.hook("session-start", {"session_id": "s1", "source": "startup"},
                           "--brief")
        self.assertEqual(result.returncode, 0, result.stderr)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(context), rules.BRIEF_HOOK_CHARS)
        self.assertIn("Vault brief", context)
        self.assertRegex(context, r"\(\d+ line\(s\) omitted to fit 3000 characters\)")
        self.assertIn("- log `LOG.md` ", context)

    def test_brief_and_parity_text_share_the_cap(self):
        self.install()
        for index in range(6):
            rules.record(self.vault, summary=f"Step {index} {'detail ' * 20}", files=["none"],
                         verified="ok", next_step="none")
        with open(self.vault / "AGENTS.md", "a", encoding="utf-8") as handle:
            handle.write("\nA rule only Codex sees.\n")
        result = self.hook("session-start", {"session_id": "s1", "source": "startup"},
                           "--brief")
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(context.startswith("Vault rules check failed"))
        self.assertIn("Vault brief", context)
        self.assertLessEqual(len(context), rules.BRIEF_HOOK_CHARS)

    def test_brief_in_a_vault_without_rules_writes_no_state(self):
        self.write("notes/idea.md", "# Idea\n")
        result = self.hook("session-start", {"session_id": "s1", "source": "startup"},
                           "--brief")
        self.assertEqual(result.returncode, 0, result.stderr)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("- status `.context/index-manifest.json` absent: overall missing", context)
        self.assertFalse((self.vault / ".context").exists())

    def test_stop_citation_line_names_undelivered_citations_only(self):
        from context_layer import session_evidence
        self.install()
        self.write("notes/other.md", "# Other\n")
        (self.vault / ".context").mkdir(exist_ok=True)
        (self.vault / ".context" / "routes.json").write_text('{"routes": {}}', encoding="utf-8")
        self.start()
        garden = sha(self.vault / "notes" / "garden.md")
        session_evidence.record_delivery(self.vault, "s1", [
            {"source_path": "notes/garden.md", "source_sha256": garden}], packet_id="a1b2c3d4e5f6")
        answer = (f"Tomatoes are in bed two (`notes/garden.md`, sha256 {garden[:12]}). "
                  "Peppers are in `notes/other.md` (sha256 0badc0ffee12).")
        output = self.stop(last_assistant_message=answer, permission_mode="default")
        self.assertIsNone(output)                         # opt-in: nothing without the flag
        result = self.hook("stop", {"session_id": "s1", "last_assistant_message": answer},
                           "--check-citations")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertNotIn("decision", output)
        line = output["systemMessage"]
        self.assertIn("`notes/other.md`", line)
        self.assertIn("hash 0badc0ffee12", line)
        self.assertNotIn("notes/garden.md", line)


class Settings(RulesBase):
    def test_settings_snippet(self):
        result = self.cli("settings", "--vault", str(self.vault), "--plan-default")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        start = data["hooks"]["SessionStart"][0]["hooks"][0]
        post = data["hooks"]["PostToolUse"][0]
        stop = data["hooks"]["Stop"][0]["hooks"][0]
        self.assertEqual(start["type"], "command")
        self.assertIn("rules hook session-start --vault", start["command"])
        self.assertIn("rules hook stop --vault", stop["command"])
        self.assertEqual(post["matcher"], "Write|Edit|MultiEdit|NotebookEdit")
        self.assertIn("rules hook post-tool-use --vault", post["hooks"][0]["command"])
        self.assertEqual({start["timeout"], stop["timeout"]}, {30})
        self.assertEqual(data["permissions"], {"defaultMode": "plan"})
        self.assertFalse((self.vault / ".claude").exists())       # writes nothing

    def test_settings_brief_and_citations(self):
        result = self.cli("settings", "--vault", str(self.vault), "--brief", "--check-citations")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertIn("rules hook session-start --brief --vault",
                      data["hooks"]["SessionStart"][0]["hooks"][0]["command"])
        self.assertIn("rules hook stop --check-citations --vault",
                      data["hooks"]["Stop"][0]["hooks"][0]["command"])
        self.assertNotIn("permissions", data)

    def test_hook_groups_match_settings(self):
        groups = rules.hook_groups(self.vault, brief=True)
        snippet = rules.settings_snippet(self.vault, brief=True)
        self.assertEqual({event: [group] for event, group in groups.items()}, snippet["hooks"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
