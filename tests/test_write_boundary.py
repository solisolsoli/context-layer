#!/usr/bin/env python3
"""Canary write-boundary test (audit E-22 / ACTIONS D2).

Reading is read-only: `search` (fts and synaptic), the read-only MCP tools and the
prompt hook, run on a disposable vault with HOME redirected into the temp directory,
leave the vault's file set (paths and bytes) and the temporary HOME unchanged, except
the declared `.context/activation.json` (docs/synapse.md: the last activation trace).
Anything else that appears, changes or disappears fails the test and is named.
"""
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_mcp_install as host  # noqa: E402

DECLARED = {".context/activation.json"}
LINKED = {"notes/alpha.md": "# Alpha\nThe zephyr valve rule is written in [[beta]].\n",
          "notes/beta.md": "# Beta\nThe gasket torque for the zephyr valve is 40 Nm.\n"}


def snapshot(root: Path) -> dict:
    """Relative POSIX path -> sha256 for every file, directories as their own key."""
    found = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        found[rel] = "<dir>" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def difference(before: dict, after: dict) -> set:
    return {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}


class ReadingLeavesNoTrace(host.HostFixture):
    def setUp(self):
        super().setUp()
        for rel, text in LINKED.items():
            (self.vault / rel).write_text(text, encoding="utf-8")
        self.index()      # the graph is built here, before the snapshot

    def test_search_mcp_reads_and_hook_change_no_file_but_the_activation_trace(self):
        before_vault, before_home = snapshot(self.vault), snapshot(self.home)
        self.assertNotIn(".context/activation.json", before_vault)

        for method in ("fts", "synaptic"):
            done = self.cli("search", str(self.vault), "--method", method,
                            "--prompt", "zephyr valve gasket torque")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("notes/beta.md", done.stdout)

        client = host.McpClient(self, self.env)
        try:
            client.initialize("2025-03-26")
            client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            calls = [("search_vault", {"prompt": "zephyr valve gasket torque"}),
                     ("search_vault", {"prompt": "zephyr valve gasket torque",
                                       "method": "synaptic"}),
                     ("read_source", {"path": "notes/beta.md"}),
                     ("vault_status", {}),
                     ("graph_neighbors", {"path": "notes/alpha.md"})]
            for ident, (name, arguments) in enumerate(calls, start=10):
                reply = client.request(ident, "tools/call", {"name": name, "arguments": arguments})
                self.assertFalse(reply["result"]["isError"], (name, reply))
        finally:
            client.close()
            client.proc.stdout.close()

        hook = self.cli("hook", "claude-code", "--vault", str(self.vault), stdin=json.dumps(
            {"hook_event_name": "UserPromptSubmit", "prompt": "zephyr valve gasket torque"}))
        self.assertEqual(hook.returncode, 0, hook.stderr)
        self.assertIn("notes/beta.md", hook.stdout)

        changed_vault = difference(before_vault, snapshot(self.vault))
        self.assertEqual(changed_vault, DECLARED, "the declared trace was not the only change")
        self.assertLessEqual(changed_vault, DECLARED, sorted(changed_vault - DECLARED))
        self.assertEqual(difference(before_home, snapshot(self.home)), set(),
                         "reading wrote into HOME")

    def test_the_canary_notices_a_stray_write(self):
        before = snapshot(self.vault)
        (self.vault / "stray.txt").write_text("x", encoding="utf-8")
        (self.vault / "notes" / "beta.md").write_text("changed\n", encoding="utf-8")
        self.assertEqual(difference(before, snapshot(self.vault)),
                         {"stray.txt", "notes/beta.md"})


if __name__ == "__main__":
    unittest.main()
