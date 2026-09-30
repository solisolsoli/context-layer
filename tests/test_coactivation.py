"""Usage co-activation ledger and `graph suggest`: opt-in, bounded, never read by retrieval.

Every test builds a disposable fictional vault in a temp directory and calls no model and no
network. Run: python3 tests/test_coactivation.py
"""
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(os.environ.get("TEST_REPO_HOME", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO))

from context_layer import coactivation  # noqa: E402

_spec = importlib.util.spec_from_file_location("retrieve_under_test", REPO / "eval" / "retrieve.py")
retrieve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(retrieve)

ROUTES = {"record_type_allowlist": ["verbatim_text_file"],
          "routes": {"notes": {"priority": 1, "triggers": ["note"], "canonical_sources": [],
                               "path_hints": []}},
          "fallback_routes": [], "aliases": {}, "exclude_prefixes": ["private"]}

NOTES = {
    "alpha.md": "# Alpha\n\nThe lantern rota is kept here. See [[beta]].\n",
    "beta.md": "# Beta\n\nThe lantern owner is named here.\n",
    "gamma.md": "# Gamma\n\nThe lantern pager schedule lives here.\n",
    "delta.md": "# Delta\n\nThe lantern vendor list.\n",
    "private/secret.md": "# Secret\n\nThe lantern vault code.\n",
}
PROMPT_TOKEN = "zzuniqueprompttoken"


class Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name).resolve() / "vault"
        (self.vault / ".context").mkdir(parents=True)
        self.routes(ROUTES)
        for name, text in NOTES.items():
            path = self.vault / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.env = dict(os.environ, HOME=str(self.vault.parent))
        self.env.pop("CLAUDE_CODE_SESSION_ID", None)
        done = self.cli("index", str(self.vault))
        self.assertEqual(done.returncode, 0, done.stderr)

    def routes(self, config):
        (self.vault / ".context" / "routes.json").write_text(json.dumps(config), encoding="utf-8")

    def usage_on(self):
        self.routes({**ROUTES, "record_usage": True})

    def cli(self, *argv):
        return subprocess.run([sys.executable, "-m", "context_layer.cli", *argv], cwd=REPO,
                              capture_output=True, text=True, env=self.env)

    def search(self, prompt, method="fts", *extra):
        """eval/retrieve.py in-process; returns the exact stdout text."""
        out = io.StringIO()
        argv = ["--method", method, "--vault", str(self.vault), *extra, prompt]
        with redirect_stdout(out), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            self.assertEqual(retrieve.main(argv), 0, out.getvalue())
        return out.getvalue()

    @property
    def ledger(self):
        return self.vault / ".context" / coactivation.LEDGER_NAME

    def context_files(self):
        return sorted(p.name for p in (self.vault / ".context").iterdir())

    def plant(self, rows):
        text = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)
        self.ledger.write_text(text, encoding="utf-8")

    def row(self, paths, at="2026-03-01T10:00:00Z", session="aaaaaaaaaaaa", method="fts"):
        return {"v": 1, "at": at, "m": method, "s": session, "p": paths}

    def note_bytes(self):
        found = {}
        for path in sorted(self.vault.rglob("*")):
            if path.is_file() and ".context" not in path.relative_to(self.vault).parts:
                found[path.relative_to(self.vault).as_posix()] = path.read_bytes()
        return found


class DefaultOff(Case):
    def test_default_off_writes_nothing(self):
        before = self.context_files()
        for method in ("fts", "synaptic"):
            packet = json.loads(self.search("lantern", method))
            self.assertGreaterEqual(len({e["source_path"] for e in packet["evidence"]}), 2)
        after = self.context_files()
        self.assertFalse([n for n in after if "usage" in n], after)
        self.assertEqual([n for n in after if n not in before], ["activation.json"])

    def test_setting_must_be_exactly_true(self):
        for value in ("true", 1, "yes", None, False):
            self.routes({**ROUTES, "record_usage": value})
            self.search("lantern")
            self.assertFalse(self.ledger.exists(), value)

    def test_unusable_config_records_nothing(self):
        result = coactivation.record(self.vault, {"operation_status": "ok", "evidence": []}, "fts")
        self.assertFalse(result["written"])
        (self.vault / ".context" / "routes.json").write_text("{not json", encoding="utf-8")
        self.assertFalse(coactivation.enabled(self.vault))

    def test_router_and_grep_methods_are_not_recorded(self):
        self.usage_on()
        packet = {"operation_status": "ok", "evidence": [{"source_path": "alpha.md"},
                                                          {"source_path": "beta.md"}]}
        for method in ("grep", "router", "fts-canonical"):
            self.assertFalse(coactivation.record(self.vault, packet, method)["written"])
        self.assertFalse(self.ledger.exists())


class Recording(Case):
    def test_line_holds_paths_only(self):
        self.usage_on()
        self.search(f"lantern {PROMPT_TOKEN}", "fts")
        self.search("lantern", "synaptic")
        text = self.ledger.read_text(encoding="utf-8")
        self.assertNotIn(PROMPT_TOKEN, text)
        self.assertNotIn("lantern", text)                # no note text either
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual([r["m"] for r in rows], ["fts", "synaptic"])
        for row in rows:
            self.assertEqual(set(row), {"v", "at", "m", "s", "p"})
            self.assertEqual(row["v"], 1)
            self.assertIsNone(row["s"])
            self.assertEqual(row["p"], sorted(row["p"]))
            self.assertGreaterEqual(len(row["p"]), 2)
            self.assertFalse([p for p in row["p"] if p.startswith("private")], row)
        self.assertEqual(oct(self.ledger.stat().st_mode & 0o777), "0o600")

    def test_session_is_stored_only_as_a_hash(self):
        self.usage_on()
        result = coactivation.record(
            self.vault, {"operation_status": "ok", "evidence": [
                {"source_path": "alpha.md"}, {"source_path": "gamma.md"}]},
            "fts", session_id="host-session-1234")
        self.assertTrue(result["written"], result)
        text = self.ledger.read_text(encoding="utf-8")
        self.assertNotIn("host-session-1234", text)
        self.assertEqual(json.loads(text)["s"],
                         hashlib.sha256(b"host-session-1234").hexdigest()[:12])

    def test_single_note_and_excluded_notes_write_nothing(self):
        self.usage_on()
        one = {"operation_status": "ok", "evidence": [{"source_path": "alpha.md"}]}
        self.assertFalse(coactivation.record(self.vault, one, "fts")["written"])
        hidden = {"operation_status": "ok", "evidence": [{"source_path": "alpha.md"},
                                                          {"source_path": "private/secret.md"}]}
        self.assertFalse(coactivation.record(self.vault, hidden, "fts")["written"])
        bad = {"operation_status": "ok", "evidence": [{"source_path": "../x.md"},
                                                       {"source_path": "/etc/passwd"}]}
        self.assertFalse(coactivation.record(self.vault, bad, "fts")["written"])
        error = {"operation_status": "error", "evidence": []}
        self.assertFalse(coactivation.record(self.vault, error, "fts")["written"])
        self.assertFalse(self.ledger.exists())

    def test_notes_per_line_are_capped(self):
        self.usage_on()
        evidence = [{"source_path": f"n{i:02d}.md"} for i in range(40)]
        coactivation.record(self.vault, {"operation_status": "ok", "evidence": evidence}, "fts")
        row = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(len(row["p"]), coactivation.MAX_NOTES_PER_LINE)

    def test_rotation_bounds_the_size(self):
        self.usage_on()
        packet = {"operation_status": "ok", "evidence": [{"source_path": "alpha.md"},
                                                          {"source_path": "beta.md"}]}
        with mock.patch.object(coactivation, "MAX_LEDGER_BYTES", 2000):
            for _ in range(200):
                self.assertTrue(coactivation.record(self.vault, packet, "fts")["written"])
        previous = self.ledger.with_name(self.ledger.name + ".1")
        self.assertTrue(previous.is_file())
        self.assertLessEqual(self.ledger.stat().st_size, 2000)
        self.assertLessEqual(previous.stat().st_size, 2000)
        self.assertEqual([n for n in self.context_files() if n.startswith(("usage", ".usage"))],
                         [".usage-ledger.lock", "usage-ledger.jsonl", "usage-ledger.jsonl.1"])

    def test_a_failure_never_breaks_retrieval(self):
        self.usage_on()
        (self.vault / ".context" / coactivation.LEDGER_NAME).mkdir()    # cannot be appended to
        packet = json.loads(self.search("lantern"))
        self.assertEqual(packet["operation_status"], "ok")


class NeverReadByRetrieval(Case):
    def test_output_is_byte_identical_with_ledger_absent_present_or_huge(self):
        for setting in (False, True):
            for method in ("fts", "synaptic"):
                with self.subTest(setting=setting, method=method):
                    self.routes({**ROUTES, "record_usage": setting})
                    self.ledger.unlink(missing_ok=True)
                    absent = self.search("lantern owner", method)
                    self.plant([self.row(["alpha.md", "gamma.md"]),
                                self.row(["gamma.md", "delta.md"])] * 3)
                    present = self.search("lantern owner", method)
                    with open(self.ledger, "a", encoding="utf-8") as handle:
                        line = json.dumps(self.row(["delta.md", "gamma.md", "alpha.md"])) + "\n"
                        handle.write(line * (6 * 1024 * 1024 // len(line)))
                        handle.write("not json\n" * 1000)
                    self.assertGreater(self.ledger.stat().st_size, 5 * 1024 * 1024)
                    huge = self.search("lantern owner", method)
                    self.assertEqual(absent, present)
                    self.assertEqual(absent, huge)

    def test_a_planted_ledger_never_changes_the_packet_or_the_trace(self):
        self.usage_on()
        first = self.search("lantern owner pager", "synaptic", "--max-hops", "2")
        self.plant([self.row(["delta.md", "gamma.md"])] * 50)
        second = self.search("lantern owner pager", "synaptic", "--max-hops", "2")
        self.assertEqual(first, second)

    def test_only_the_ledger_module_and_its_two_callers_name_the_ledger(self):
        allowed = {"context_layer/coactivation.py", "eval/retrieve.py", "context_layer/graph.py"}
        found = set()
        for folder in ("context_layer", "eval", "router"):
            for path in (REPO / folder).rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                if "usage-ledger" in text or "coactivation" in text or "usage_ledger" in text:
                    found.add(path.relative_to(REPO).as_posix())
        self.assertEqual(found, allowed)
        # retrieval-side code only ever calls `record`, never a reader.
        retrieve_text = (REPO / "eval" / "retrieve.py").read_text(encoding="utf-8")
        self.assertIn("coactivation.record(", retrieve_text)
        for reader in ("read_ledger", "suggest", "_parse_line"):
            self.assertNotIn(reader, retrieve_text)

    def test_the_ledger_is_never_indexed(self):
        self.usage_on()
        self.search("lantern")
        self.assertTrue(self.ledger.exists())
        done = self.cli("index", str(self.vault))
        self.assertEqual(done.returncode, 0, done.stderr)
        import sqlite3
        connection = sqlite3.connect(self.vault / ".context" / "index.sqlite")
        try:
            paths = [r[0] for r in connection.execute("SELECT DISTINCT source_path FROM records")]
        finally:
            connection.close()
        self.assertFalse([p for p in paths if "usage" in p or ".context" in p], paths)


class Suggest(Case):
    def test_lists_unlinked_pairs_with_evidence(self):
        rows = [self.row(["alpha.md", "beta.md", "gamma.md"], "2026-03-01T10:00:00Z", "s1s1s1s1s1s1"),
                self.row(["alpha.md", "gamma.md"], "2026-03-01T11:00:00Z", "s1s1s1s1s1s1"),
                self.row(["alpha.md", "beta.md"], "2026-03-02T11:00:00Z", "s1s1s1s1s1s1"),
                self.row(["alpha.md", "gamma.md"], "2026-03-04T09:00:00Z", "s2s2s2s2s2s2"),
                self.row(["beta.md", "gamma.md"], "2026-03-05T09:00:00Z", "s3s3s3s3s3s3"),
                self.row(["alpha.md", "delta.md"], "2026-03-05T09:30:00Z", "s3s3s3s3s3s3")]
        self.plant(rows)
        before = self.note_bytes()
        report = coactivation.suggest(self.vault)
        self.assertEqual(self.note_bytes(), before)
        found = {(s["a"], s["b"]): s for s in report["suggestions"]}
        # alpha-beta is linked ([[beta]]), alpha-delta was seen once only.
        self.assertEqual(sorted(found), [("alpha.md", "gamma.md"), ("beta.md", "gamma.md")])
        top = report["suggestions"][0]
        self.assertEqual((top["a"], top["b"]), ("alpha.md", "gamma.md"))
        self.assertEqual((top["retrievals"], top["sessions"], top["days"]), (3, 2, 2))
        self.assertEqual((top["a_delivered"], top["b_delivered"]), (5, 4))
        self.assertEqual(top["first_seen"], "2026-03-01T10:00:00Z")
        self.assertEqual(report["already_linked"], 1)
        self.assertEqual(report["retrievals"], 6)

    def test_link_in_either_direction_counts_as_linked(self):
        # beta.md links to gamma.md, so the pair is not suggested even though only the
        # reverse direction exists.
        (self.vault / "beta.md").write_text("# Beta\n\nSee [[gamma]].\n", encoding="utf-8")
        self.assertEqual(self.cli("index", str(self.vault)).returncode, 0)
        self.plant([self.row(["gamma.md", "beta.md"])] * 3)
        self.assertEqual(coactivation.suggest(self.vault)["suggestions"], [])

    def test_excluded_and_deleted_notes_are_left_out(self):
        self.plant([self.row(["alpha.md", "gamma.md", "private/secret.md"])] * 3
                   + [self.row(["gamma.md", "gone.md"])] * 3)
        report = coactivation.suggest(self.vault)
        text = json.dumps(report)
        self.assertNotIn("private", text)
        self.assertNotIn("gone.md", text)
        self.assertEqual([(s["a"], s["b"]) for s in report["suggestions"]],
                         [("alpha.md", "gamma.md")])

    def test_unusable_lines_are_skipped_and_counted(self):
        self.ledger.write_text(
            "not json\n" + json.dumps({"v": 2, "at": "2026-03-01T10:00:00Z",
                                       "p": ["alpha.md", "gamma.md"]}) + "\n"
            + json.dumps({"v": 1, "at": "2026-03-01T10:00:00Z", "p": "alpha.md"}) + "\n"
            + json.dumps(self.row(["alpha.md", "gamma.md", 7, None])) + "\n"
            + json.dumps(self.row(["alpha.md", "gamma.md"])) + "\n", encoding="utf-8")
        report = coactivation.suggest(self.vault)
        self.assertEqual(report["skipped_lines"], 3)
        self.assertEqual(report["suggestions"][0]["retrievals"], 2)

    def test_rotated_file_is_read_too(self):
        previous = self.ledger.with_name(self.ledger.name + ".1")
        previous.write_text(json.dumps(self.row(["alpha.md", "gamma.md"])) + "\n", encoding="utf-8")
        self.plant([self.row(["alpha.md", "gamma.md"], "2026-03-02T10:00:00Z")])
        self.assertEqual(coactivation.suggest(self.vault)["suggestions"][0]["retrievals"], 2)

    def test_end_to_end_from_recorded_retrievals(self):
        self.usage_on()
        for _ in range(3):
            self.search("lantern", "fts", "--top-k", "4")
        done = self.cli("graph", "suggest", str(self.vault), "--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        report = json.loads(done.stdout)
        pairs = {(s["a"], s["b"]) for s in report["suggestions"]}
        self.assertIn(("alpha.md", "gamma.md"), pairs)
        self.assertNotIn(("alpha.md", "beta.md"), pairs)
        self.assertTrue(all(s["retrievals"] == 3 for s in report["suggestions"]))
        self.assertNotIn("private", done.stdout)

    def test_cli_text_report_and_exit_codes(self):
        done = self.cli("graph", "suggest", str(self.vault))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("No usage ledger yet", done.stdout)
        self.assertIn("off by default", done.stdout)
        self.plant([self.row(["alpha.md", "gamma.md"])] * 2)
        done = self.cli("graph", "suggest", str(self.vault))
        self.assertIn("alpha.md  <->  gamma.md", done.stdout)
        self.assertIn("delivered together 2x", done.stdout)
        self.assertIn("usage, not relatedness", done.stdout)
        self.assertEqual(self.cli("graph", "suggest", str(self.vault / "nope")).returncode, 1)
        self.assertEqual(self.cli("graph", "suggest", str(self.vault), "--limit", "0").returncode, 2)
        (self.vault / ".context" / "graph.sqlite").unlink()
        done = self.cli("graph", "suggest", str(self.vault))
        self.assertEqual(done.returncode, 1)
        self.assertIn("context-layer index", done.stderr)

    def test_min_count_and_limit(self):
        self.plant([self.row(["alpha.md", "gamma.md"])] * 3 + [self.row(["gamma.md", "delta.md"])] * 2)
        self.assertEqual(len(coactivation.suggest(self.vault, min_count=3)["suggestions"]), 1)
        report = coactivation.suggest(self.vault, min_count=2, limit=1)
        self.assertEqual(len(report["suggestions"]), 1)
        self.assertTrue(report["truncated"])


if __name__ == "__main__":
    unittest.main()
