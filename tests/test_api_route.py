"""Offline route planning is bounded and cannot contact a provider."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from context_layer import api_route


class ApiRouteTests(unittest.TestCase):
    def assert_unverified(self, result):
        self.assertEqual(result["source_status"], "NOT_CHECKED")
        self.assertTrue(result["source_verification_required"])
        self.assertTrue(result["advisory"])
        self.assertFalse(result["network_call"])
        self.assertTrue(result["preview_only"])
        self.assertNotIn("probability", result)
        self.assertNotIn("truth", result)
        self.assertNotIn("approved", result)

    def test_default_local_is_offline_and_does_not_load_api_clients(self):
        code = """
import builtins, socket, sys
real_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.endswith((\"decisions_client\", \"responses_client\")):
        raise AssertionError(\"API client import attempted\")
    return real_import(name, *args, **kwargs)
def deny(*args, **kwargs):
    raise AssertionError(\"network access attempted\")
builtins.__import__ = guarded_import
socket.create_connection = deny
socket.socket.connect = deny
from context_layer import api_route
result = api_route.plan(\"local\", \"private\")
assert result[\"route\"] == \"local\"
assert result[\"endpoint_kind\"] == \"local_retrieval\"
assert result[\"source_status\"] == \"NOT_CHECKED\"
assert \"context_layer.decisions_client\" not in sys.modules
assert \"context_layer.responses_client\" not in sys.modules
"""
        result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_each_evaluation_maps_to_its_fixed_decisions_task(self):
        for need, task in api_route.DECISIONS_TASKS.items():
            with self.subTest(need=need):
                result = api_route.plan(need, "synthetic")
                self.assertEqual(result["route"], "decisions")
                self.assertEqual(result["endpoint_kind"], "decisions_api")
                argv = result["next_cli_argv_template"]
                self.assertEqual(argv[argv.index("--task") + 1], task)
                self.assertNotIn("--send", argv)
                self.assertTrue(result["send_requires_explicit_flag"])
                self.assert_unverified(result)

    def test_nonlocal_private_requests_are_always_blocked(self):
        for need in api_route.NEEDS:
            if need == "local":
                continue
            with self.subTest(need=need):
                result = api_route.plan(need, "private", model="model-x" if need == "generate" else None)
                self.assertEqual(result["route"], "blocked")
                self.assertEqual(result["endpoint_kind"], None)
                self.assertIsNone(result["next_cli_argv_template"])
                self.assertFalse(result["send_requires_explicit_flag"])
                self.assert_unverified(result)

    def test_generation_requires_an_explicit_model_and_routes_to_responses(self):
        from context_layer import responses

        missing = api_route.plan("generate", "public")
        empty = api_route.plan("generate", "synthetic", "")
        whitespace = api_route.plan("generate", "public", "   ")
        self.assertEqual(missing["route"], "needs_configuration")
        self.assertEqual(empty["reason"], "explicit_model_required")
        self.assertEqual(whitespace["route"], "needs_configuration")
        result = api_route.plan("generate", "public", "gpt-example-1")
        self.assertEqual(result["route"], "responses")
        self.assertEqual(result["endpoint_kind"], "responses_api")
        self.assertEqual(result["model"], "gpt-example-1")
        self.assertEqual(result["next_cli_argv_template"], [
            "context-layer", "responses", "run", "--input-file", "<INPUT_FILE>",
            "--data-scope", "public", "--model", "gpt-example-1",
        ])
        self.assertTrue(result["send_requires_explicit_flag"])
        self.assert_unverified(result)
        self.assertEqual(api_route.plan("generate", "public", "m" * 100)["route"], "responses")
        responses.build_payload("synthetic fixture", "m" * 100)
        with self.assertRaises(api_route.RouteInputError):
            api_route.plan("generate", "public", "m" * 101)
        with self.assertRaises(responses.InvalidResponseInput):
            responses.build_payload("synthetic fixture", "m" * 101)

    def test_invalid_need_scope_and_model_fail_closed(self):
        for args in (("guess", "public", None), ("local", "unknown", None),
                     ("generate", "public", "has whitespace"),
                     ("generate", "public", "x" * 101),
                     ("generate", "public", "x;whoami"),
                     ("generate", "public", "x\x00y"),
                     ("claim_support", "public", "gpt-demo"),
                     ("generate", "public", 42)):
            with self.subTest(args=args), self.assertRaises(api_route.RouteInputError):
                api_route.plan(*args)

    def test_cli_output_is_compact_bounded_json(self):
        args = api_route.argparse.Namespace(need="generate", data_scope="public", model="gpt-demo")
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(api_route.cmd_plan(args), 0)
        text = output.getvalue().strip()
        self.assertLess(len(text), 2048)
        self.assertEqual(json.loads(text)["route"], "responses")
        self.assertNotIn("\n", text)

    def test_registered_cli_defaults_to_local_private(self):
        root = api_route.argparse.ArgumentParser()
        commands = root.add_subparsers(dest="command", required=True)
        api_route.register(commands)
        args = root.parse_args(["api", "plan"])
        self.assertEqual((args.need, args.data_scope), ("local", "private"))
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(args.func(args), 0)
        self.assertEqual(json.loads(output.getvalue())["route"], "local")

    def test_all_provider_templates_parse_without_reading_or_sending(self):
        from context_layer import decisions, responses

        root = api_route.argparse.ArgumentParser()
        commands = root.add_subparsers(dest="command", required=True)
        decisions.register(commands)
        responses.register(commands)
        requests = [
            api_route.plan(need, "public")
            for need in api_route.DECISIONS_TASKS
        ] + [api_route.plan("generate", "synthetic", "gpt-demo")]
        with patch.object(decisions.decisions_client, "create_decision") as send_decisions, \
                patch.object(responses.responses_client, "create_response") as send_responses, \
                patch.object(decisions, "_read_input", side_effect=AssertionError), \
                patch.object(responses, "_read_input", side_effect=AssertionError):
            for request in requests:
                args = root.parse_args(request["next_cli_argv_template"][1:])
                self.assertFalse(args.send)
                self.assertEqual(args.func, decisions.cmd_assess if args.command == "decisions"
                                 else responses.cmd_run)
        send_decisions.assert_not_called()
        send_responses.assert_not_called()

    def test_invalid_cli_model_returns_exit_two_without_traceback(self):
        args = api_route.argparse.Namespace(need="generate", data_scope="public", model="bad model")
        with patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(api_route.cmd_plan(args), 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("context-layer api plan:", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
