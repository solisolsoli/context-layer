"""Offline tests for safe, explicit GitHub source management."""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from context_layer import github_client, github_sources as sources  # noqa: E402

OLD = "a" * 40
NEW = "b" * 40


def hold_lock_process(lock_path, ready_path, release_path):
    script = ("from context_layer.platform_support import file_lock; "
              "from pathlib import Path; import sys,time; "
              "p,r,x=map(Path,sys.argv[1:]); "
              "ctx=file_lock(p,timeout=5); ctx.__enter__(); r.touch(); "
              "\nwhile not x.exists(): time.sleep(.01)\nctx.__exit__(None,None,None)")
    return subprocess.Popen([sys.executable, "-c", script, str(lock_path),
                             str(ready_path), str(release_path)],
                            cwd=Path(__file__).resolve().parents[1])


def wait_for(path, process):
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline and process.poll() is None:
        time.sleep(.01)
    return path.exists()


def row(**changes):
    value = {"id": "docs", "repo": "example/project", "commit": OLD,
             "paths": ["README.md"], "keywords": ["harbor"]}
    value.update(changes)
    return value


class GitHubSourcesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name)
        self.context = self.vault / ".context"
        self.context.mkdir()

    def configure(self, rows=None):
        data = {"version": 1, "enabled": True, "sources": [row()] if rows is None else rows}
        (self.context / "github.json").write_text(json.dumps(data), encoding="utf-8")
        return data

    def test_add_is_dry_run_by_default_then_atomic_with_backup_and_ref_metadata(self):
        self.configure(rows=[])
        with mock.patch.object(github_client, "resolve_ref", return_value=NEW) as resolve:
            preview = sources.add_source(self.vault, "guide", "example/project", "heads/main",
                                         ["README.md"], ["harbor"], apply=False)
            self.assertEqual(preview["status"], "DRY_RUN")
            self.assertFalse((self.context / "github.json").read_bytes() != json.dumps(
                {"version": 1, "enabled": True, "sources": []}).encode())
            applied = sources.add_source(self.vault, "guide", "example/project", "heads/main",
                                         ["README.md"], ["harbor"], apply=True)
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(applied["status"], "OK")
        config = json.loads((self.context / "github.json").read_text())
        self.assertEqual(config["sources"][0]["commit"], NEW)
        self.assertEqual(config["sources"][0]["ref"], "heads/main")
        self.assertTrue((self.context / "github.json.bak").exists())

    def test_add_rejects_unsafe_input_before_ref_resolution(self):
        with mock.patch.object(github_client, "resolve_ref", side_effect=AssertionError("network")):
            for args in (("bad/id", "example/project", "main", ["README.md"], ["x"]),
                         ("ok", "../project", "main", ["README.md"], ["x"]),
                         ("ok", "example/project", "main", ["../secret"], ["x"])):
                result = sources.add_source(self.vault, *args)
                self.assertEqual(result["status"], "ERROR")

    def test_old_version_one_config_lists_and_mutates_without_ref(self):
        original = self.configure()
        self.assertEqual(sources.list_sources(self.vault)["sources"], original["sources"])
        preview = sources.set_enabled(self.vault, False)
        self.assertEqual(preview["status"], "DRY_RUN")
        result = sources.set_enabled(self.vault, False, apply=True)
        self.assertEqual(result["status"], "OK")
        reread = json.loads((self.context / "github.json").read_text())
        self.assertFalse(reread["enabled"])
        self.assertNotIn("ref", reread["sources"][0])

    def test_pin_update_requires_expected_old_sha_and_explicit_apply(self):
        self.configure([row(ref="main")])
        mismatch = sources.update_source(self.vault, "docs", NEW, "c" * 40, apply=True)
        self.assertEqual(mismatch["errors"], ["expected_commit_mismatch"])
        preview = sources.update_source(self.vault, "docs", NEW, OLD)
        self.assertEqual(preview["status"], "DRY_RUN")
        self.assertEqual(json.loads((self.context / "github.json").read_text())["sources"][0]["commit"], OLD)
        applied = sources.update_source(self.vault, "docs", NEW, OLD, apply=True)
        self.assertEqual(applied["status"], "OK")
        self.assertEqual(json.loads((self.context / "github.json").read_text())["sources"][0]["commit"], NEW)
        self.assertTrue((self.context / "github.json.bak").exists())

    def test_remove_and_atomic_write_refuse_symlinked_config_and_lock(self):
        self.configure()
        self.assertEqual(sources.remove_source(self.vault, "docs")["status"], "DRY_RUN")
        self.assertEqual(sources.remove_source(self.vault, "missing")["errors"], ["source_not_found"])
        (self.context / "github.json").unlink()
        outside = self.vault / "outside.json"
        outside.write_text("{}")
        (self.context / "github.json").symlink_to(outside)
        self.assertEqual(sources.list_sources(self.vault)["errors"], ["invalid_config"])
        (self.context / "github.json").unlink()
        self.configure()

    def test_real_config_lock_times_out_then_recovers_after_release_or_owner_death(self):
        self.configure()
        lock = self.context / ".github-config.lock"
        ready, release = self.vault / "locked", self.vault / "release"
        process = hold_lock_process(lock, ready, release)
        self.assertTrue(wait_for(ready, process))
        blocked = sources.set_enabled(self.vault, False, apply=True)
        self.assertEqual(blocked["errors"], ["concurrent_write"])
        release.touch()
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertEqual(sources.set_enabled(self.vault, False, apply=True)["status"], "OK")
        self.configure()
        ready.unlink(); release.unlink()
        process = hold_lock_process(lock, ready, release)
        self.assertTrue(wait_for(ready, process))
        process.terminate()
        process.wait(timeout=5)
        recovered = sources.set_enabled(self.vault, False, apply=True)
        self.assertEqual(recovered["status"], "OK")
        self.assertTrue(lock.is_file())  # Persistent inode; OS released owner-death lock.

    def test_snapshot_detects_config_change_during_read_before_write(self):
        self.configure()
        original_load = sources._load

        def racing_load(vault):
            parsed = original_load(vault)
            (self.context / "github.json").write_text(
                json.dumps({"version": 1, "enabled": False, "sources": [row()]}))
            return parsed

        with mock.patch.object(sources, "_load", side_effect=racing_load):
            result = sources.set_enabled(self.vault, True, apply=True)
        self.assertEqual(result["errors"], ["concurrent_write"])
        self.assertFalse(json.loads((self.context / "github.json").read_text())["enabled"])

    def test_check_previews_bounded_per_file_diff_without_pin_change(self):
        self.configure([row(ref="main")])
        old = b"harbor lamp one\nunchanged\n"
        fresh = b"harbor lamp two\nunchanged\n"
        with mock.patch.object(github_client, "resolve_ref", return_value=NEW), \
                mock.patch.object(github_client, "fetch_file", side_effect=[old, fresh]) as fetch:
            result = sources.check_source(self.vault, "docs")
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["upstream_commit"], NEW)
        self.assertTrue(result["files"][0]["changed"])
        self.assertIn("-harbor lamp one", result["files"][0]["diff"])
        self.assertIn("+harbor lamp two", result["files"][0]["diff"])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(json.loads((self.context / "github.json").read_text())["sources"][0]["commit"], OLD)

    def test_check_exposes_file_omission_and_updates_never_resolve_implicit_ref(self):
        self.configure([row(paths=[f"docs/{i}.md" for i in range(5)], ref="main")])
        with mock.patch.object(github_client, "resolve_ref", return_value=NEW), \
                mock.patch.object(github_client, "fetch_file", return_value=b"same\n") as fetch:
            result = sources.check_source(self.vault, "docs")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["omissions"], ["file_limit_reached"])
        self.assertEqual(fetch.call_count, 8)
        with mock.patch.object(github_client, "resolve_ref", side_effect=AssertionError("implicit ref")):
            update = sources.update_source(self.vault, "docs", NEW, OLD)
        self.assertEqual(update["status"], "DRY_RUN")


if __name__ == "__main__":
    unittest.main()
