"""Native, adversarial checks for shared Windows/POSIX runtime primitives."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from context_layer.platform_support import (
    LockTimeout,
    atomic_write,
    file_lock,
    managed_process_tree,
    private_tempdir,
    private_tempfile,
    is_link_or_reparse,
    set_private_path,
    verify_private_path,
)
from context_layer import backends, install, platform_support


def _python_env():
    root = str(Path(__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


class PlatformSupport(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_file_lock_serializes_processes_and_initializes_once(self):
        lock = self.root / "shared.lock"
        ready = self.root / "ready"
        script = (
            "import pathlib,sys,time; from context_layer.platform_support import file_lock; "
            "exec('with file_lock(sys.argv[1]):\\n "
            "pathlib.Path(sys.argv[2]).write_text(\"held\")\\n time.sleep(.35)')"
        )
        process = subprocess.Popen([sys.executable, "-c", script, str(lock), str(ready)],
                                   env=_python_env())
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(ready.exists(), "lock holder did not start")
        with self.assertRaises(LockTimeout):
            with file_lock(lock, timeout=.05, poll_interval=.005):
                self.fail("second process acquired a held lock")
        self.assertEqual(process.wait(timeout=5), 0)
        with file_lock(lock, timeout=1):
            self.assertTrue(lock.exists())
        # Windows byte-range locking can deny an independent read handle while
        # the lock is held; inspect the initialized byte after releasing it.
        self.assertIn(lock.read_bytes(), (b"", b"\0"))
        verify_private_path(lock)

    def test_file_lock_recovers_when_process_dies_while_holding_lock(self):
        lock = self.root / "crash.lock"
        ready = self.root / "ready"
        script = (
            "import os,pathlib,sys; from context_layer.platform_support import file_lock; "
            "exec('with file_lock(sys.argv[1]):\\n "
            "pathlib.Path(sys.argv[2]).write_text(\"held\")\\n os._exit(0)')"
        )
        process = subprocess.Popen([sys.executable, "-c", script, str(lock), str(ready)],
                                   env=_python_env())
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(ready.exists(), "lock holder did not start")
        self.assertEqual(process.wait(timeout=5), 0)
        with file_lock(lock, timeout=1):
            self.assertTrue(lock.exists())

    def test_file_lock_rejects_symlink_path(self):
        original, alias = self.root / "real.lock", self.root / "alias.lock"
        original.touch()
        try:
            alias.symlink_to(original)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        self.assertTrue(is_link_or_reparse(alias))
        self.assertFalse(is_link_or_reparse(self.root / "missing"))
        with self.assertRaises(OSError):
            with file_lock(alias):
                pass

    def test_private_path_rejects_windows_junction_leaf(self):
        if os.name != "nt":
            self.skipTest("Windows junction regression")
        target = self.root / "target-dir"
        junction = self.root / "junction-dir"
        target.mkdir()
        command = f'cmd.exe /d /c mklink /J "{junction}" "{target}"'
        result = subprocess.run(command,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(junction.is_dir())
        self.assertTrue(is_link_or_reparse(junction))
        with self.assertRaises(OSError):
            set_private_path(junction, directory=True)

    def test_encoded_hook_ownership_requires_the_context_layer_launcher(self):
        def command(script):
            encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
            return ("powershell.exe -NoLogo -NoProfile -NonInteractive "
                    f"-EncodedCommand {encoded}")

        ours = command("& 'C:\\Python\\python.exe' '-m' 'context_layer.cli' "
                       "'hook' 'claude-code' '--vault' 'C:\\vault'; exit $LASTEXITCODE")
        embedded_text_only = command("& 'echo' 'context_layer' 'hook' 'claude-code'; "
                                     "exit $LASTEXITCODE")
        nested_hook_words = command("& 'C:\\Program Files\\context-layer.exe' "
                                   "'github-sources' 'hook' 'claude-code'; "
                                   "exit $LASTEXITCODE")
        self.assertTrue(install.is_ours(ours))
        self.assertFalse(install.is_ours(embedded_text_only))
        self.assertFalse(install.is_ours(nested_hook_words))

    def test_windows_host_plan_resolves_cmd_shim_and_preserves_json_arguments(self):
        if os.name != "nt":
            self.skipTest("Windows host command-script regression")
        program = self.root / "claude shim.py"
        command = self.root / "claude.cmd"
        program.write_text(
            "import json,sys; print(json.dumps(sys.argv[1:], ensure_ascii=True))\n",
            encoding="utf-8", newline="\n")
        command.write_text(f'@"{sys.executable}" "{program}" %*\r\n',
                           encoding="utf-8", newline="")
        plan = backends.plan("claude", prompt_file=self.root / "prompt.txt",
                             out_dir=self.root / "out", env={"PATH": str(self.root)})
        self.assertEqual(Path(plan.argv[0]), command)
        with managed_process_tree(plan.argv, env=_python_env(),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) as child:
            stdout, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, stderr)
        argv = json.loads(stdout)
        self.assertEqual(argv[argv.index("-p") + 1], backends.CLAUDE_INSTRUCTION)
        self.assertEqual(json.loads(argv[argv.index("--settings") + 1]),
                         json.loads(backends.CLAUDE_SETTINGS))

    def test_private_staging_is_secured_before_content_and_atomic_write_keeps_lf(self):
        fd, path = private_tempfile(self.root, prefix="private-")
        try:
            verify_private_path(path)
            os.write(fd, b"secret\n")
        finally:
            os.close(fd)
        self.assertEqual(path.read_bytes(), b"secret\n")
        target = self.root / "packet.json"
        atomic_write(target, "one\ntwo\n", private=True)
        self.assertEqual(target.read_bytes(), b"one\ntwo\n")
        verify_private_path(target)

    def test_atomic_write_retries_only_transient_windows_replace_errors(self):
        target = self.root / "state.json"
        target.write_bytes(b"old")
        replace = os.replace
        errors = []
        for code in (5, 32, 33):
            error = PermissionError("simulated Windows sharing conflict")
            error.winerror = code
            errors.append(error)

        def busy_then_replace(source, destination):
            self.assertEqual(target.read_bytes(), b"old")
            if errors:
                raise errors.pop(0)
            replace(source, destination)

        with patch.object(platform_support, "IS_WINDOWS", True), \
                patch.object(platform_support.os, "replace", side_effect=busy_then_replace) as call:
            atomic_write(target, b"new")
        self.assertGreaterEqual(call.call_count, 4)  # the native rename may also be briefly busy
        self.assertEqual(target.read_bytes(), b"new")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["state.json"])

    def test_atomic_write_replace_failure_is_bounded_and_keeps_original(self):
        target = self.root / "state.json"
        for windows, winerror in ((True, 5), (True, 87), (False, 5)):
            with self.subTest(windows=windows, winerror=winerror):
                target.write_bytes(b"old")
                error = PermissionError("simulated permanent failure")
                error.winerror = winerror
                with patch.object(platform_support, "IS_WINDOWS", windows), \
                        patch.object(platform_support.os, "replace", side_effect=error) as call, \
                        patch.object(platform_support.time, "monotonic", side_effect=[0.0, 2.0]), \
                        patch.object(platform_support.time, "sleep") as sleep, \
                        self.assertRaises(PermissionError):
                    atomic_write(target, b"new")
                self.assertEqual(call.call_count, 1)
                sleep.assert_not_called()
                self.assertEqual(target.read_bytes(), b"old")
                self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["state.json"])

    def test_private_directory_child_has_only_private_access(self):
        directory = private_tempdir(self.root, prefix="secure-")
        verify_private_path(directory, directory=True)
        child = directory / "inherited.txt"
        fd = os.open(child, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, b"private")
        finally:
            os.close(fd)
        # On Windows this reads the child's effective inherited DACL, not just its mode.
        verify_private_path(child)

    def test_managed_process_tree_stops_descendant_after_root_exits(self):
        self._assert_descendant_stopped(parent_exits_first=True)

    def test_managed_process_tree_kill_cancels_descendant(self):
        self._assert_descendant_stopped(parent_exits_first=False)

    def _assert_descendant_stopped(self, *, parent_exits_first):
        marker = self.root / "escaped-child"
        ready = self.root / "grandchild-started"
        child_code = ("import pathlib,sys,time; time.sleep(.45); "
                      "pathlib.Path(sys.argv[1]).write_text('escaped')")
        parent_code = (
            "import subprocess,sys,time; "
            "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); "
            "open(sys.argv[3], 'w').close(); "
            + ("raise SystemExit(0)" if parent_exits_first else "time.sleep(30)")
        )
        with managed_process_tree([sys.executable, "-c", parent_code,
                                   child_code, str(marker), str(ready)], env=_python_env(),
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL) as child:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists(), "parent did not start the grandchild")
            if parent_exits_first:
                self.assertEqual(child.wait(timeout=5), 0)
            else:
                child.kill()
                self.assertIsNotNone(child.poll())
        time.sleep(.65)
        self.assertFalse(marker.exists(), "a descendant escaped the managed process tree")


if __name__ == "__main__":
    unittest.main()
