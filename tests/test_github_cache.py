"""Offline and adversarial tests for the explicit GitHub file cache."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from context_layer import github_cache as cache, github_client, github_context  # noqa: E402

COMMIT = "a" * 40
BODY = b"The fictional harbor keeper checks every lamp.\n"


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


def make_junction(link: Path, target: Path):
    completed = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
                               capture_output=True, text=True, timeout=10)
    if completed.returncode != 0:
        raise AssertionError("Windows CI could not create a temporary directory junction")


class GitHubCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name)
        self.context = self.vault / ".context"
        self.context.mkdir()

    def enable(self):
        self.assertEqual(cache.set_enabled(self.vault, True, apply=True)["status"], "OK")

    def fetch(self, **opts):
        return cache.fetch_file(self.vault, "example/project", COMMIT, "README.md", **opts)

    def test_disabled_default_online_passes_through_without_writing_and_offline_misses(self):
        with mock.patch.object(github_client, "fetch_file", return_value=BODY) as remote:
            data, provenance = self.fetch()
        self.assertEqual((data, provenance), (BODY, None))
        self.assertEqual(remote.call_count, 1)
        self.assertFalse((self.context / "github-cache").exists())
        with mock.patch.object(github_client, "fetch_file", side_effect=AssertionError("network")) as remote:
            with self.assertRaises(cache.GitHubCacheError) as ctx:
                self.fetch(offline=True)
        self.assertEqual(ctx.exception.code, "cache_disabled")
        remote.assert_not_called()

    def test_opt_in_write_and_offline_read_report_provenance(self):
        self.enable()
        with mock.patch.object(github_client, "fetch_file", return_value=BODY) as remote:
            self.assertEqual(self.fetch(), (BODY, "network-cached"))
            self.assertEqual(self.fetch(offline=True), (BODY, "disk-cache"))
        self.assertEqual(remote.call_count, 1)
        report = cache.status(self.vault)
        self.assertEqual((report["status"], report["enabled"], report["entries"]), ("OK", True, 1))
        self.assertEqual(cache.purge(self.vault)["status"], "DRY_RUN")
        self.assertEqual(cache.purge(self.vault, apply=True)["purged"], 1)

    def test_force_refresh_bypasses_existing_cache_and_replaces_record(self):
        self.enable()
        newer = b"A new fictional harbor note.\n"
        with mock.patch.object(github_client, "fetch_file", side_effect=[BODY, newer]) as remote:
            self.fetch()
            self.assertEqual(self.fetch(force_refresh=True), (newer, "network-refresh"))
            self.assertEqual(self.fetch(offline=True), (newer, "disk-cache"))
        self.assertEqual(remote.call_count, 2)

    def test_record_corruption_and_coordinate_swap_fail_closed(self):
        self.enable()
        with mock.patch.object(github_client, "fetch_file", return_value=BODY):
            self.fetch()
        entry = next((self.context / "github-cache").glob("*.json"))
        doc = json.loads(entry.read_text())
        doc["path"] = "other.md"
        entry.write_text(json.dumps(doc))
        with mock.patch.object(github_client, "fetch_file", side_effect=AssertionError("network")):
            with self.assertRaises(cache.GitHubCacheError) as ctx:
                self.fetch(offline=True)
        self.assertEqual(ctx.exception.code, "cache_coordinates_mismatch")
        doc["path"] = "README.md"
        doc["sha256"] = "0" * 64
        entry.write_text(json.dumps(doc))
        with self.assertRaises(cache.GitHubCacheError) as ctx:
            self.fetch(offline=True)
        self.assertEqual(ctx.exception.code, "cache_hash_mismatch")

    def test_symlink_cache_entry_and_aggregate_limit_are_rejected(self):
        self.enable()
        directory = self.context / "github-cache"
        directory.mkdir()
        target = self.vault / "target"
        target.write_text("{}")
        (directory / ("c" * 64 + ".json")).symlink_to(target)
        self.assertEqual(cache.status(self.vault)["errors"], ["invalid_cache_entry"])
        with mock.patch.object(github_client, "fetch_file", side_effect=AssertionError("network")) as remote:
            with self.assertRaises(cache.GitHubCacheError):
                self.fetch(force_refresh=True)
        remote.assert_not_called()
        (directory / ("c" * 64 + ".json")).unlink()
        for index in range(cache.MAX_CACHE_ENTRIES + 1):
            (directory / (f"{index:064x}.json")).write_bytes(b"")
        self.assertEqual(cache.status(self.vault)["errors"], ["cache_entry_limit"])

    def test_cache_config_writes_are_opt_in_dry_run_by_default_and_backup_existing(self):
        preview = cache.set_enabled(self.vault, True)
        self.assertEqual(preview["status"], "DRY_RUN")
        self.assertFalse((self.context / "github-cache.json").exists())
        self.enable()
        self.assertTrue((self.context / "github-cache.json").exists())
        result = cache.set_enabled(self.vault, False, apply=True)
        self.assertEqual(result["status"], "OK")
        self.assertTrue((self.context / "github-cache.json.bak").exists())
        self.assertFalse(cache.status(self.vault)["enabled"])

    def test_cache_config_removal_race_does_not_recreate_stale_state(self):
        self.enable()
        config = self.context / "github-cache.json"
        original_open = cache.os.open

        def remove_during_lock(path, flags, mode=0o777, *, dir_fd=None):
            config.unlink()
            if dir_fd is None:
                return original_open(path, flags, mode)
            return original_open(path, flags, mode, dir_fd=dir_fd)

        with mock.patch.object(cache.os, "open", side_effect=remove_during_lock):
            result = cache.set_enabled(self.vault, False, apply=True)
        self.assertEqual(result["errors"], ["concurrent_write"])
        self.assertFalse(config.exists())

    def test_cache_operations_fail_closed_when_shared_lock_exists(self):
        self.enable()
        directory = self.context / "github-cache"
        directory.mkdir()
        lock = directory / ".github-cache.lock"
        ready, release = self.vault / "cache-locked", self.vault / "cache-release"
        process = hold_lock_process(lock, ready, release)
        self.assertTrue(wait_for(ready, process))
        with mock.patch.object(github_client, "fetch_file", side_effect=AssertionError("network")) as remote:
            with self.assertRaises(cache.GitHubCacheError) as ctx:
                self.fetch(offline=True)
        self.assertEqual(ctx.exception.code, "concurrent_cache_operation")
        remote.assert_not_called()
        process.terminate()
        process.wait(timeout=5)
        with mock.patch.object(github_client, "fetch_file", return_value=BODY):
            self.assertEqual(self.fetch(), (BODY, "network-cached"))
            self.assertEqual(self.fetch(offline=True), (BODY, "disk-cache"))

    @unittest.skipUnless(os.name == "nt", "Windows junction boundary regression")
    def test_cache_directory_junction_is_rejected_without_touching_target_or_network(self):
        self.enable()
        (self.context / "github.json").write_text(json.dumps({
            "version": 1, "enabled": True,
            "sources": [{"id": "docs", "repo": "example/project", "commit": COMMIT,
                         "paths": ["README.md"], "keywords": ["harbor"]}]}), encoding="utf-8")
        outside = self.vault / "external-cache"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("fictional external sentinel", encoding="utf-8")
        before = sentinel.read_bytes()
        make_junction(self.context / "github-cache", outside)
        with mock.patch.object(github_client, "fetch_file", side_effect=AssertionError("network")) as remote:
            packet = github_context.fetch(self.vault, "harbor", ["docs"])
        self.assertEqual(packet["status"], "ERROR")
        self.assertEqual(packet["errors"], ["unsafe_cache_path"])
        self.assertEqual(cache.status(self.vault)["errors"], ["unsafe_cache_path"])
        self.assertEqual(cache.purge(self.vault, apply=True)["errors"], ["unsafe_cache_path"])
        self.assertEqual(sentinel.read_bytes(), before)
        remote.assert_not_called()

    def test_context_integration_adds_provenance_only_when_opted_in_and_forbids_conflicting_options(self):
        (self.context / "github.json").write_text(json.dumps({
            "version": 1, "enabled": True,
            "sources": [{"id": "docs", "repo": "example/project", "commit": COMMIT,
                         "paths": ["README.md"], "keywords": ["harbor"]}]}))
        self.assertEqual(github_context.fetch(self.vault, "harbor", ["docs"], offline=True)["errors"],
                         ["cache_disabled"])
        self.enable()
        with mock.patch.object(github_client, "fetch_file", return_value=BODY):
            packet = github_context.fetch(self.vault, "harbor", ["docs"])
            local = github_context.fetch(self.vault, "harbor", ["docs"], offline=True)
        self.assertEqual(packet["status"], "FOUND")
        self.assertEqual(packet["evidence"][0]["cache_provenance"], "network-cached")
        self.assertEqual(local["evidence"][0]["cache_provenance"], "disk-cache")
        conflict = github_context.fetch(self.vault, "harbor", ["docs"], offline=True, force_refresh=True)
        self.assertEqual(conflict["errors"], ["invalid_cache_options"])


if __name__ == "__main__":
    unittest.main()
