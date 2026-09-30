"""Phase A5 regressions: source status, index rollback, the lab hook candidate.

Every test owns a disposable synthetic vault under a temporary directory, and
HOME points at a temporary folder for every command. Nothing here reads a real
vault, a host config or a hook setting.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(os.environ.get("TEST_REPO_HOME", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO))

from context_layer import health  # noqa: E402

HOOK = REPO / "retrieval-patches" / "hook-visible-error.example.sh"

STATUS_KEYS = ["added", "changed", "deleted", "excluded_count", "exit_code", "graph", "index",
               "moved", "now_skipped", "overall", "reasons", "schema", "symlinks"]
INDEX_KEYS = ["age_seconds", "built_at", "manifest", "older_than_sources", "present",
              "readable", "rollback_available", "source_count"]
GRAPH_KEYS = ["built_at", "edges", "matches_index", "notes", "present", "readable",
              "rollback_available"]
SKIPPED_KEYS = ["indexed", "path", "reason"]


class VaultFixture(unittest.TestCase):
    """A two-source vault with a routes config and one successful index build."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.home = root / "home"
        self.home.mkdir()
        self.vault = root / "vault"
        (self.vault / "notes").mkdir(parents=True)
        self.write("canonical.md", b"# Canonical\nalpha marker\n")
        self.write("notes/weekly.md", b"# Weekly\nbeta note\n")
        self.ctx = self.vault / ".context"
        self.ctx.mkdir()
        self.config = {"record_type_allowlist": ["verbatim_text_file"], "routes": {
            "canonical": {"priority": 10, "triggers": ["alpha"],
                          "canonical_sources": ["canonical.md"], "path_hints": []}},
            "fallback_routes": [], "aliases": {}, "exclude_prefixes": []}
        self.save_config()
        self.build()

    # -- helpers ---------------------------------------------------------
    def env(self):
        return dict(os.environ, HOME=str(self.home))

    def write(self, relative, data: bytes):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def save_config(self):
        (self.ctx / "routes.json").write_text(json.dumps(self.config))

    def build(self):
        result = subprocess.run(
            [sys.executable, str(REPO / "router/build_index.py"), "--vault", str(self.vault)],
            capture_output=True, text=True, env=self.env())
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def cli(self, *argv):
        return subprocess.run([sys.executable, "-m", "context_layer.cli", *argv],
                              cwd=REPO, capture_output=True, text=True, env=self.env())

    def index(self, *flags):
        done = self.cli("index", str(self.vault), *flags)
        self.assertEqual(done.returncode, 0, done.stderr)

    def script(self, relative, *argv):
        return subprocess.run([sys.executable, str(REPO / relative), *argv],
                              cwd=REPO, capture_output=True, env=self.env())

    def rewrite_preserving_stat(self, relative, old: bytes, new: bytes):
        """The lab's trap: identical byte length and identical mtime, new content."""
        path = self.vault / relative
        before = path.stat()
        raw = path.read_bytes()
        replaced = raw.replace(old, new)
        self.assertNotEqual(replaced, raw)
        self.assertEqual(len(replaced), len(raw))
        path.write_bytes(replaced)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        self.assertEqual(after.st_size, before.st_size)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        return path

    def unreadable(self, path: Path):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root reads files whatever their mode")
        mode = path.stat().st_mode
        path.chmod(0)
        self.addCleanup(path.chmod, mode)

    def skipped(self, summary):
        return {(e["path"], e["reason"], e["indexed"]) for e in summary["now_skipped"]}


class StatusReport(VaultFixture):
    def test_status_ok_after_index(self):
        summary = health.status_summary(self.vault)
        self.assertEqual(summary["overall"], "ok")
        self.assertEqual(summary["exit_code"], 0)
        self.assertEqual(summary["index"]["source_count"], 2)
        self.assertTrue(summary["index"]["present"])
        self.assertTrue(summary["index"]["manifest"])
        self.assertIsNotNone(summary["index"]["built_at"])
        self.assertIsNotNone(summary["index"]["age_seconds"])
        for key in ("changed", "deleted", "added", "moved", "symlinks", "now_skipped"):
            self.assertEqual(summary[key], [], key)
        self.assertFalse(summary["graph"]["present"])  # built without the CLI: no graph

    def test_changed_is_detected_with_size_and_mtime_preserved(self):
        self.rewrite_preserving_stat("canonical.md", b"alpha", b"gamma")
        summary = health.status_summary(self.vault)
        self.assertEqual(summary["changed"], ["canonical.md"])
        self.assertEqual(summary["overall"], "degraded")
        self.assertEqual(summary["exit_code"], 1)
        self.assertTrue(any("changed on disk" in r for r in summary["reasons"]))

    def test_changed_source_withholds_search_and_route_with_a_visible_reason(self):
        self.rewrite_preserving_stat("canonical.md", b"alpha", b"gamma")
        self.assertEqual(health.status_summary(self.vault)["changed"], ["canonical.md"])

        search = self.script("eval/retrieve.py", "--vault", str(self.vault),
                             "--method", "fts", "alpha marker")
        self.assertEqual(search.returncode, 0, search.stdout)
        packet = json.loads(search.stdout)
        self.assertEqual(packet["operation_status"], "ok")
        self.assertNotIn("canonical.md", [e["source_path"] for e in packet["evidence"]])
        self.assertEqual(packet["withheld"], [{"source_path": "canonical.md",
                                               "reason": "changed since indexing",
                                               "next": "context-layer index <vault>"}])
        self.assertNotIn(b"gamma", search.stdout)

        route = self.script("router/context_router.py", "--vault", str(self.vault),
                            "--prompt", "alpha marker", "--no-save", "--stdout")
        self.assertEqual(route.returncode, 1, route.stdout)
        self.assertIn(b"index_source_hash_mismatch", route.stdout)
        self.assertIn(b"context-layer index <vault>", route.stdout)
        self.assertIn(b"Evidence packet withheld", route.stdout)
        self.assertNotIn(b"## Verbatim evidence", route.stdout)
        self.assertNotIn(b"gamma", route.stdout)

    def test_deleted_source(self):
        (self.vault / "notes/weekly.md").unlink()
        summary = health.status_summary(self.vault)
        self.assertEqual(summary["deleted"], ["notes/weekly.md"])
        self.assertEqual(summary["added"], [])
        self.assertEqual(summary["overall"], "degraded")

    def test_added_source(self):
        self.write("notes/fresh.md", b"# Fresh\ndelta note\n")
        summary = health.status_summary(self.vault)
        self.assertEqual(summary["added"], ["notes/fresh.md"])
        self.assertEqual(summary["changed"], [])
        self.assertEqual(summary["overall"], "stale")
        self.assertEqual(summary["exit_code"], 1)

    def test_moved_source_is_not_reported_as_delete_plus_add(self):
        shutil.move(str(self.vault / "canonical.md"), str(self.vault / "notes/canonical.md"))
        summary = health.status_summary(self.vault)
        self.assertEqual(summary["moved"],
                         [{"from": "canonical.md", "to": "notes/canonical.md",
                           "sha256": summary["moved"][0]["sha256"]}])
        self.assertEqual(summary["deleted"], [])
        self.assertEqual(summary["added"], [])
        self.assertEqual(summary["overall"], "degraded")

    def test_symlink_is_reported_as_unsupported_and_never_indexed(self):
        (self.vault / "link.md").symlink_to(self.vault / "canonical.md")
        summary = health.status_summary(self.vault)
        self.assertEqual(summary["symlinks"], ["link.md"])
        self.assertEqual(summary["added"], [])
        self.assertEqual(summary["overall"], "stale")
        self.assertTrue(any("symlink" in r for r in summary["reasons"]))
        self.build()  # A rebuild does not adopt it; the report stays the same.
        self.assertEqual(health.status_summary(self.vault)["symlinks"], ["link.md"])

    def test_a_symlinked_folder_is_named_only_in_scope(self):
        (self.vault / "private").mkdir()
        (self.vault / "private" / "linked").symlink_to(self.vault / "notes")
        (self.vault / "shared").symlink_to(self.vault / "notes")
        self.config["exclude_prefixes"] = ["private"]
        self.save_config()
        summary = health.status_summary(self.vault)
        self.assertEqual(summary["symlinks"], ["shared/"])  # never an excluded path

    def test_excluded_source_is_counted_not_reported_as_added(self):
        self.write("private/secret.md", b"# Secret\nomega\n")
        self.config["exclude_prefixes"] = ["private"]
        self.save_config()
        summary = health.status_summary(self.vault)
        self.assertEqual(summary["added"], [])
        self.assertEqual(summary["excluded_count"], 1)
        self.assertEqual(summary["overall"], "ok")

    def test_falls_back_to_index_rows_when_the_manifest_is_absent(self):
        (self.ctx / "index-manifest.json").unlink()
        summary = health.status_summary(self.vault)
        self.assertFalse(summary["index"]["manifest"])
        self.assertEqual(summary["index"]["source_count"], 2)
        self.assertEqual(summary["overall"], "ok")
        self.rewrite_preserving_stat("canonical.md", b"alpha", b"gamma")
        self.assertEqual(health.status_summary(self.vault)["changed"], ["canonical.md"])

    def test_missing_vault_is_rejected_without_a_traceback(self):
        result = self.cli("status", str(self.vault / "absent"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("vault not found", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_exit_codes_zero_one_two(self):
        self.assertEqual(self.cli("status", str(self.vault)).returncode, 0)
        self.rewrite_preserving_stat("canonical.md", b"alpha", b"gamma")
        self.assertEqual(self.cli("status", str(self.vault)).returncode, 1)
        (self.ctx / "index.sqlite").unlink()
        missing = self.cli("status", str(self.vault))
        self.assertEqual(missing.returncode, 2)
        self.assertIn("overall: missing", missing.stdout)

    def test_unreadable_index_is_reported_as_missing(self):
        (self.ctx / "index.sqlite").write_bytes(b"not SQLite")
        summary = health.status_summary(self.vault)
        self.assertEqual(summary["overall"], "missing")
        self.assertEqual(summary["exit_code"], 2)
        self.assertFalse(summary["index"]["readable"])

    def test_a_present_manifest_does_not_mask_a_broken_index(self):
        # The manifest is plain text and survives anything; retrieval needs the
        # SQLite index, so status probes it the way retrieval opens it.
        with sqlite3.connect(self.ctx / "index.sqlite") as db:
            db.execute("DROP TABLE records_fts")
        self.assertTrue((self.ctx / "index-manifest.json").is_file())
        summary = health.status_summary(self.vault)
        self.assertEqual(summary["overall"], "missing")
        self.assertFalse(summary["index"]["readable"])
        self.assertFalse(summary["index"]["manifest"])

    def test_output_carries_no_absolute_path(self):
        self.write("notes/fresh.md", b"# Fresh\ndelta note\n")
        self.write("notes/empty.md", b"")
        for argv in (["status", str(self.vault)], ["status", str(self.vault), "--json"]):
            result = self.cli(*argv)
            self.assertNotIn(str(self.vault), result.stdout)
            self.assertNotIn(str(REPO), result.stdout)


class SkippedFiles(VaultFixture):
    """C-25 and A-05: files the builder cannot index are named, never called deleted."""

    def test_emptied_and_oversized_sources_are_skipped_not_deleted(self):
        _, builder = health._router_modules()
        self.write("canonical.md", b"")
        self.write("notes/weekly.md", b"x" * (builder.MAX_FILE_BYTES + 1))
        summary = health.status_summary(self.vault)
        self.assertEqual((summary["deleted"], summary["changed"]), ([], []))
        self.assertEqual(self.skipped(summary), {("canonical.md", "empty", True),
                                                 ("notes/weekly.md", "over size limit", True)})
        self.assertEqual(summary["overall"], "degraded")
        self.assertTrue(any("can no longer be indexed (1 empty, 1 over size limit)" in r
                            for r in summary["reasons"]), summary["reasons"])
        self.assertFalse(any("gone from scope" in r for r in summary["reasons"]))
        self.build()  # the builder skips both; the index no longer holds them
        summary = health.status_summary(self.vault)
        self.assertEqual(self.skipped(summary), {("canonical.md", "empty", False),
                                                 ("notes/weekly.md", "over size limit", False)})
        self.assertEqual(summary["overall"], "stale")  # the oversized one has content

    def test_an_oversized_note_is_named_and_not_ok(self):  # A-05 v-big
        _, builder = health._router_modules()
        self.write("notes/big.md", b"zephyrine valve " * (builder.MAX_FILE_BYTES // 16 + 1))
        summary = health.status_summary(self.vault)
        self.assertEqual(self.skipped(summary), {("notes/big.md", "over size limit", False)})
        self.assertEqual(summary["added"], [])
        self.assertEqual(summary["overall"], "stale")
        self.assertEqual(summary["excluded_count"], 0)

    def test_an_unreadable_note_is_named_and_not_ok(self):  # A-05 v-perm
        self.unreadable(self.write("notes/locked.md", b"# Locked\nrelease checklist\n"))
        summary = health.status_summary(self.vault)
        self.assertEqual(self.skipped(summary), {("notes/locked.md", "unreadable", False)})
        self.assertEqual(summary["overall"], "stale")
        self.assertNotEqual(self.cli("status", str(self.vault)).returncode, 0)

    def test_an_unreadable_indexed_note_is_not_reported_deleted(self):
        self.unreadable(self.vault / "notes" / "weekly.md")
        summary = health.status_summary(self.vault)
        self.assertEqual(summary["deleted"], [])
        self.assertEqual(self.skipped(summary), {("notes/weekly.md", "unreadable", True)})
        self.assertEqual(summary["overall"], "degraded")

    def test_an_unreadable_folder_is_named_and_its_notes_are_not_deleted(self):
        self.unreadable(self.vault / "notes")
        summary = health.status_summary(self.vault)
        self.assertEqual(summary["deleted"], [])
        self.assertEqual(self.skipped(summary), {("notes/", "unreadable folder", False),
                                                 ("notes/weekly.md", "unreadable", True)})
        self.assertEqual(summary["overall"], "degraded")

    def test_a_note_that_is_not_utf8_is_named_not_added(self):
        self.write("notes/latin.md", b"caf\xe9 au lait\n")
        summary = health.status_summary(self.vault)
        self.assertEqual(self.skipped(summary), {("notes/latin.md", "not UTF-8", False)})
        self.assertEqual(summary["added"], [])
        self.assertEqual(summary["overall"], "stale")

    def test_an_empty_note_is_listed_but_overall_stays_ok(self):
        self.write("notes/untitled.md", b"")
        summary = health.status_summary(self.vault)
        self.assertEqual(self.skipped(summary), {("notes/untitled.md", "empty", False)})
        self.assertEqual(summary["overall"], "ok")
        self.assertTrue(any("nothing to index" in r for r in summary["reasons"]))
        self.assertEqual(self.cli("status", str(self.vault)).returncode, 0)

    def test_max_file_bytes_in_routes_json_sets_the_limit(self):
        self.config["max_file_bytes"] = 10
        self.save_config()
        summary = health.status_summary(self.vault)
        self.assertEqual(summary["now_skipped"], [])  # indexed with the same bytes: covered
        self.assertEqual(summary["overall"], "ok")
        self.write("notes/new.md", b"# New\nmore than ten bytes\n")
        summary = health.status_summary(self.vault)
        self.assertEqual(self.skipped(summary), {("notes/new.md", "over size limit", False)})

    def test_the_builder_manifest_names_what_the_scan_cannot_judge(self):
        odd = "notes/odd\\name.md"
        try:
            self.write(odd, b"# Odd\nname the router refuses\n")
        except OSError:  # pragma: no cover - a file system without backslashes in names
            self.skipTest("backslash not allowed in file names here")
        self.write("private/hidden\\name.md", b"# Hidden\n")
        self.config["exclude_prefixes"] = ["private"]
        self.save_config()
        summary = health.status_summary(self.vault)
        self.assertEqual(summary["now_skipped"], [])
        self.assertEqual(summary["excluded_count"], 2)
        self.assertEqual(summary["overall"], "ok")
        manifest = self.ctx / "index-manifest.json"
        payload = json.loads(manifest.read_text())
        payload["skipped"] = [{"path": odd, "reason": "unsupported_name"},
                              {"path": "private/hidden\\name.md", "reason": "unsupported_name"},
                              {"path": "notes/gone.md", "reason": "oversize"}, "junk"]
        manifest.write_text(json.dumps(payload))
        summary = health.status_summary(self.vault)
        self.assertEqual(self.skipped(summary), {(odd, "unsupported name", False)})
        self.assertEqual(summary["excluded_count"], 1)
        self.assertEqual(summary["overall"], "stale")
        self.assertNotIn("private", json.dumps(summary))


class GraphGeneration(VaultFixture):
    """C-26: the link graph is reported, and rollback never mixes generations."""

    def change(self, text=b"# Canonical\nalpha marker, and [[weekly]]\n"):
        self.write("canonical.md", text)

    def test_status_reports_the_graph_and_its_generation(self):
        self.index()
        summary = health.status_summary(self.vault)
        graph = summary["graph"]
        self.assertEqual((graph["present"], graph["readable"], graph["matches_index"]),
                         (True, True, True))
        self.assertEqual(graph["notes"], 2)
        self.assertEqual(summary["overall"], "ok")
        self.change()
        self.index("--no-graph")
        summary = health.status_summary(self.vault)
        self.assertFalse(summary["graph"]["matches_index"])
        self.assertEqual(summary["overall"], "stale")
        self.assertTrue(any("another index generation" in r for r in summary["reasons"]))
        self.assertIn("built from another index generation",
                      self.cli("status", str(self.vault)).stdout)
        self.index()
        self.assertEqual(health.status_summary(self.vault)["overall"], "ok")

    def test_index_out_builds_the_graph_in_both_spellings(self):  # B-25
        for number, spelling in enumerate((lambda p: ["--out", p], lambda p: [f"--out={p}"])):
            target = self.vault / f"alt-{number}.sqlite"
            # Without the default index the graph can only come from the --out file.
            for leftover in self.ctx.glob("index.sqlite*"):
                leftover.unlink()
            done = self.cli("index", str(self.vault), *spelling(str(target)))
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertNotIn("link graph not built", done.stderr)
            self.assertTrue(target.is_file())
            self.assertIn("graph: 2 notes", done.stdout)

    def test_an_unreadable_graph_is_reported(self):
        self.index()
        (self.ctx / "graph.sqlite").write_bytes(b"not a graph")
        summary = health.status_summary(self.vault)
        self.assertFalse(summary["graph"]["readable"])
        self.assertEqual(summary["overall"], "stale")
        self.assertTrue(any("link graph" in r and "cannot be used" in r
                            for r in summary["reasons"]))

    def test_rollback_refuses_when_the_graph_cannot_move(self):  # audit h3b
        self.index()
        self.change()
        self.index()
        (self.ctx / "graph.sqlite.prev").unlink()
        index_before = (self.ctx / "index.sqlite").read_bytes()
        graph_before = (self.ctx / "graph.sqlite").read_bytes()
        for flags in ((), ("--dry-run",)):
            refused = self.cli("rollback", str(self.vault), *flags)
            self.assertEqual(refused.returncode, 1, refused.stdout)
            self.assertIn("--index-only", refused.stderr)
        self.assertEqual((self.ctx / "index.sqlite").read_bytes(), index_before)
        done = self.cli("rollback", str(self.vault), "--index-only")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("left as it is (--index-only)", done.stdout)
        self.assertNotEqual((self.ctx / "index.sqlite").read_bytes(), index_before)
        self.assertEqual((self.ctx / "graph.sqlite").read_bytes(), graph_before)
        summary = health.status_summary(self.vault)
        self.assertFalse(summary["graph"]["matches_index"])
        self.assertEqual(summary["overall"], "degraded")

    def test_rollback_refuses_a_previous_graph_of_another_generation(self):
        self.index()
        self.change()
        self.index("--no-graph")
        self.change(b"# Canonical\nalpha marker, third version\n")
        self.index("--no-graph")
        refused = self.cli("rollback", str(self.vault))
        self.assertEqual(refused.returncode, 1)
        self.assertIn("was not built from .context/index.sqlite.prev", refused.stderr)
        self.assertEqual(self.cli("rollback", str(self.vault), "--index-only").returncode, 0)

    def test_rollback_moves_a_matching_graph_with_the_index(self):
        self.index()
        self.change()
        self.index()
        done = self.cli("rollback", str(self.vault))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("graph.sqlite from .context/graph.sqlite.prev", done.stdout)
        summary = health.status_summary(self.vault)
        self.assertTrue(summary["graph"]["matches_index"])
        self.assertEqual(summary["changed"], ["canonical.md"])


class JsonShape(VaultFixture):
    """C-31: the same keys in every state, and exit codes that follow `overall`."""

    def check(self, expected_overall, expected_code):
        summary = health.status_summary(self.vault)
        self.assertEqual(summary["overall"], expected_overall)
        self.assertEqual(sorted(summary), STATUS_KEYS)
        self.assertEqual(sorted(summary["index"]), INDEX_KEYS)
        self.assertEqual(sorted(summary["graph"]), GRAPH_KEYS)
        for entry in summary["now_skipped"]:
            self.assertEqual(sorted(entry), SKIPPED_KEYS)
        self.assertEqual(summary["schema"], "source-health-v1")
        self.assertIsInstance(summary["reasons"], list)
        self.assertEqual(summary["exit_code"], expected_code)
        printed = self.cli("status", str(self.vault), "--json")
        self.assertEqual(printed.returncode, expected_code, printed.stderr)
        self.assertEqual(sorted(json.loads(printed.stdout)), STATUS_KEYS)

    def test_json_keys_are_stable_across_every_state(self):
        self.index()
        self.check("ok", 0)
        self.write("notes/fresh.md", b"# Fresh\n")
        self.write("notes/empty.md", b"")
        self.check("stale", 1)
        self.rewrite_preserving_stat("canonical.md", b"alpha", b"gamma")
        self.check("degraded", 1)
        (self.ctx / "routes.json").write_text('{"exclude_prefixes": ["a"], '
                                              '"exclude_prefixes": ["b"]}')
        self.check("error", 2)
        self.save_config()
        (self.ctx / "index.sqlite").unlink()
        self.check("missing", 2)


class ManifestAndRollback(VaultFixture):
    def test_build_index_writes_the_manifest(self):
        manifest = json.loads((self.ctx / "index-manifest.json").read_text())
        for key in ("built_at", "source_count", "sources"):
            self.assertIn(key, manifest)
        self.assertEqual(manifest["source_count"], 2)
        self.assertEqual(len(manifest["sources"]), 2)
        for entry in manifest["sources"]:
            self.assertEqual(sorted(entry), ["mtime", "path", "sha256", "size"])
            path = self.vault / entry["path"]
            self.assertEqual(entry["size"], path.stat().st_size)
            self.assertEqual(entry["sha256"], health._hash_file(path))
        self.assertNotIn(str(self.vault), json.dumps(manifest))

    def test_first_build_leaves_nothing_to_roll_back_to(self):
        self.assertFalse((self.ctx / "index.sqlite.prev").exists())
        self.assertFalse(health.status_summary(self.vault)["index"]["rollback_available"])
        result = self.cli("rollback", str(self.vault))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no previous index to restore", result.stderr)

    def test_rollback_restores_the_previous_index_byte_identical(self):
        first = (self.ctx / "index.sqlite").read_bytes()
        first_manifest = (self.ctx / "index-manifest.json").read_bytes()
        (self.vault / "canonical.md").write_bytes(b"# Canonical\nalpha marker extended\n")
        self.build()
        second = (self.ctx / "index.sqlite").read_bytes()
        self.assertNotEqual(first, second)
        self.assertEqual((self.ctx / "index.sqlite.prev").read_bytes(), first)
        self.assertEqual(health.status_summary(self.vault)["overall"], "ok")

        dry = self.cli("rollback", str(self.vault), "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual((self.ctx / "index.sqlite").read_bytes(), second)

        result = self.cli("rollback", str(self.vault))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.ctx / "index.sqlite").read_bytes(), first)
        self.assertEqual((self.ctx / "index-manifest.json").read_bytes(), first_manifest)
        self.assertEqual((self.ctx / "index.sqlite.prev").read_bytes(), second)

        summary = health.status_summary(self.vault)
        self.assertEqual(summary["changed"], ["canonical.md"])
        self.assertEqual(summary["overall"], "degraded")
        self.assertTrue(summary["index"]["older_than_sources"])
        self.assertTrue(any("older than the sources" in r for r in summary["reasons"]))

    def test_rollback_is_its_own_undo(self):
        first = (self.ctx / "index.sqlite").read_bytes()
        (self.vault / "canonical.md").write_bytes(b"# Canonical\nalpha marker extended\n")
        self.build()
        second = (self.ctx / "index.sqlite").read_bytes()
        self.assertEqual(self.cli("rollback", str(self.vault)).returncode, 0)
        self.assertEqual((self.ctx / "index.sqlite").read_bytes(), first)
        self.assertEqual(self.cli("rollback", str(self.vault)).returncode, 0)
        self.assertEqual((self.ctx / "index.sqlite").read_bytes(), second)
        self.assertEqual(health.status_summary(self.vault)["overall"], "ok")

    def test_rollback_sets_aside_a_manifest_the_restored_index_predates(self):
        (self.vault / "canonical.md").write_bytes(b"# Canonical\nalpha marker extended\n")
        self.build()
        (self.ctx / "index-manifest.json.prev").unlink()  # An index built before manifests.
        result = self.cli("rollback", str(self.vault))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.ctx / "index-manifest.json").exists())
        summary = health.status_summary(self.vault)
        self.assertFalse(summary["index"]["manifest"])
        self.assertEqual(summary["changed"], ["canonical.md"])

    def test_failed_rebuild_touches_neither_index_nor_manifest(self):
        # The build fails after it staged a complete index: the check that must
        # pass before the live index is replaced is forced to fail.
        index = (self.ctx / "index.sqlite").read_bytes()
        manifest = (self.ctx / "index-manifest.json").read_bytes()
        self.write("notes/extra.md", b"# Extra\nmore text\n")
        _, builder = health._router_modules()
        forced = builder.index_format.IndexFormatError("forced failure")
        with patch.object(builder.index_format, "check_index", side_effect=forced), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = builder.main(["--vault", str(self.vault)])
        self.assertEqual(code, 1)
        self.assertIn("forced failure", stderr.getvalue())
        self.assertEqual((self.ctx / "index.sqlite").read_bytes(), index)
        self.assertEqual((self.ctx / "index-manifest.json").read_bytes(), manifest)
        self.assertFalse((self.ctx / "index.sqlite.prev").exists())
        self.assertEqual(list(self.ctx.glob(".index-*")), [])
        self.assertEqual(list(self.ctx.glob(".staging-*")), [])


class HookVisibleError(unittest.TestCase):
    """The lab's 5 cases against the generic wrapper example. Nothing is installed."""

    CASES = {
        "valid": '{"hookSpecificOutput":{"additionalContext":"VALID-CONTEXT\\n\\n"}}',
        "malformed": "not json",
        "wrongtype": '{"hookSpecificOutput":{"additionalContext":123}}',
        "empty": "",
    }

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.helper = Path(self.temp.name) / "fake-helper.sh"
        self.helper.write_text(
            "#!/bin/sh\n"
            'case "$CASE" in\n'
            "  exit7) exit 7 ;;\n"
            '  empty) : ;;\n'
            '  *) printf %s "$PAYLOAD" ;;\n'
            "esac\n")
        self.helper.chmod(0o755)

    def run_case(self, case):
        environment = dict(os.environ, CASE=case, PAYLOAD=self.CASES.get(case, ""),
                           CONTEXT_HELPER=str(self.helper), CONTEXT_PYTHON=sys.executable,
                           HOME=self.temp.name)
        return subprocess.run(["/bin/sh", str(HOOK)], capture_output=True, env=environment)

    def test_valid_helper_response_reaches_stdout(self):
        result = self.run_case("valid")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"VALID-CONTEXT\n\n")

    def test_every_failure_is_visible_and_never_empty_success(self):
        expected = {
            "exit7": b"helper exited 7",
            "malformed": b"malformed helper response",
            "wrongtype": b"malformed helper response",
            "empty": b"empty helper response",
        }
        for case, fragment in expected.items():
            with self.subTest(case=case):
                result = self.run_case(case)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(b"[Memory unavailable:", result.stdout)
                self.assertIn(fragment, result.stdout)
                self.assertFalse(result.returncode == 0 and not result.stdout.strip())

    def test_missing_helper_is_reported_rather_than_swallowed(self):
        environment = dict(os.environ, CONTEXT_HELPER=str(self.helper) + "-absent",
                           CONTEXT_PYTHON=sys.executable, HOME=self.temp.name)
        result = subprocess.run(["/bin/sh", str(HOOK)], capture_output=True, env=environment)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"[Memory unavailable:", result.stdout)

    def test_example_carries_no_personal_path(self):
        text = HOOK.read_text()
        self.assertNotIn("/Users/", text)
        self.assertNotIn("/home/", text)
        self.assertTrue(text.isascii(), "the example should be plain ASCII English")
        self.assertIn("does not install", text)


if __name__ == "__main__":
    unittest.main()
