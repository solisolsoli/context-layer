"""Offline integration boundaries: remote evidence never becomes local support."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(os.environ.get("TEST_REPO_HOME", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO))
from context_layer import cli, mcp_server  # noqa: E402


def local_packet(status="NOT_FOUND", **overrides):
    return {"schema": "evidence-delivery-v1", "operation_status": "ok",
            "status": status, "evidence": [], **overrides}


EXTERNAL = {"schema": "github-context-v1", "status": "FOUND", "errors": [],
            "evidence": [{"source_type": "github", "content": "Candidate documentation."}]}


class Integration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name)
        self.state = mcp_server.Server(self.vault)

    def test_fallback_preserves_local_abstention_and_does_not_mutate_input(self):
        packet = local_packet()
        before = copy.deepcopy(packet)
        with patch("context_layer.github_context.fetch", return_value=EXTERNAL) as fetch:
            result = mcp_server.github_fallback(self.vault, "a gap", packet)
        fetch.assert_called_once_with(self.vault, "a gap")
        self.assertEqual(packet, before)
        self.assertEqual(result.pop("external_context"), EXTERNAL)
        self.assertEqual(result, before)

    def test_existing_evidence_errors_and_withheld_sources_never_fall_through(self):
        cases = [local_packet("SUPPORTED", evidence=[{"content": "local"}]),
                 local_packet("PARTIAL"), local_packet("ERROR", operation_status="error"),
                 local_packet(withheld=[{"source_path": "notes/stale.md"}]),
                 local_packet(evidence=[{"content": "local"}]), {}]
        with patch("context_layer.github_context.fetch") as fetch:
            for packet in cases:
                with self.subTest(packet=packet):
                    self.assertIs(mcp_server.github_fallback(self.vault, "gap", packet), packet)
            fetch.assert_not_called()

    def call_cli(self, packet, *extra, code=0):
        raw = json.dumps(packet, indent=2) + "\n"
        done = subprocess.CompletedProcess([], code, stdout=raw.encode())
        output = io.StringIO()
        with patch.object(mcp_server, "preflight", return_value=None), \
                patch.object(cli.subprocess, "run", return_value=done), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            result = cli.main(["search", str(self.vault), "--prompt", "gap", *extra])
        return result, output.getvalue(), raw

    def test_cli_off_and_no_github_are_byte_identical(self):
        with patch("context_layer.github_context.fetch") as fetch:
            for flags in ((), ("--github", "--no-github")):
                code, output, raw = self.call_cli(local_packet(), *flags)
                self.assertEqual((code, output), (0, raw))
            fetch.assert_not_called()

    def test_cli_only_clean_miss_gets_external_context(self):
        with patch("context_layer.github_context.fetch", return_value=EXTERNAL) as fetch:
            code, output, _ = self.call_cli(local_packet(), "--github")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)["external_context"], EXTERNAL)
            fetch.assert_called_once()
        with patch("context_layer.github_context.fetch") as fetch:
            self.call_cli(local_packet(), "--github", code=1)
            fetch.assert_not_called()

    def test_mcp_opt_in_and_external_evidence_never_enters_ledger(self):
        packet = local_packet()
        raw = json.dumps(packet)
        self.state.session_evidence = True
        with patch.object(mcp_server, "search", return_value=(raw, packet, 0)), \
                patch("context_layer.github_context.fetch", return_value=EXTERNAL) as fetch, \
                patch.object(mcp_server, "record_delivery", return_value=None) as record:
            result = mcp_server.tool_search_vault(self.state, {"prompt": "gap"})
            self.assertEqual(result["content"][0]["text"], raw)
            fetch.assert_not_called()
            result = mcp_server.tool_search_vault(self.state, {"prompt": "gap", "github": True})
            self.assertFalse(result["isError"])
            augmented = json.loads(result["content"][0]["text"])
            self.assertEqual(augmented["status"], "NOT_FOUND")
            self.assertEqual(augmented["evidence"], [])
            self.assertEqual(record.call_args.args[2], [])

    def test_mcp_search_failure_and_bad_flag_do_not_fetch(self):
        with patch("context_layer.github_context.fetch") as fetch:
            with self.assertRaises(mcp_server.InvalidParams):
                mcp_server.tool_search_vault(self.state, {"prompt": "gap", "github": "yes"})
            with patch.object(mcp_server, "search", return_value=("{}", None, 1)):
                result = mcp_server.tool_search_vault(self.state,
                                                      {"prompt": "gap", "github": True})
            self.assertTrue(result["isError"])
            fetch.assert_not_called()

    def test_explicit_gap_tool_uses_configured_ids_and_marks_errors(self):
        with patch("context_layer.github_context.fetch", return_value=EXTERNAL) as fetch:
            result = mcp_server.tool_github_context(self.state,
                            {"prompt": "semantic gap", "source_ids": ["project-docs"]})
            fetch.assert_called_once_with(self.vault, "semantic gap", ["project-docs"])
            self.assertFalse(result["isError"])
        with patch("context_layer.github_context.fetch", return_value={"status": "ERROR"}):
            self.assertTrue(mcp_server.tool_github_context(self.state,
                                                         {"prompt": "gap"})["isError"])

    def test_explicit_cli_works_without_a_local_index(self):
        with patch("context_layer.github_context.fetch", return_value=EXTERNAL) as fetch, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = cli.main(["github-context", str(self.vault), "--prompt", "gap",
                             "--source", "project-docs"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), EXTERNAL)
        fetch.assert_called_once_with(self.vault, "gap", ["project-docs"])

    def test_mcp_tools_describe_network_and_are_registered(self):
        described = {tool["name"]: tool for tool in mcp_server.TOOLS}
        for name in ("search_vault", "github_context"):
            self.assertTrue(described[name]["annotations"]["openWorldHint"])
            self.assertIn(name, mcp_server.HANDLERS)
        self.assertTrue(described["github_context"]["annotations"]["readOnlyHint"])

    def test_only_dedicated_transport_gets_network_exception(self):
        root = self.vault / "guard"
        package = root / "context_layer"
        package.mkdir(parents=True)
        (package / "github_client.py").write_text("import urllib.request\n", encoding="utf-8")
        (package / "github_context.py").write_text("import urllib.request\n", encoding="utf-8")
        run = subprocess.run([sys.executable, str(REPO / "scripts/check_network_surface.py"),
                              "--root", str(root)], capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 1)
        self.assertIn("github_context.py:1: network-import", run.stdout)
        self.assertNotIn("github_client.py:", run.stdout)


if __name__ == "__main__":
    unittest.main()
