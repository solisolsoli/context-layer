"""The explicit Decisions command stays offline until --send is supplied."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from context_layer import decisions


class DecisionsCommandTests(unittest.TestCase):
    def test_preview_never_opens_transport(self):
        data = {"query": "Where is the style guide?", "passage": "The style guide is in docs."}
        with patch.object(decisions.decisions_client, "create_decision") as call:
            result = decisions.assess("relevance", data, data_scope="synthetic")
        call.assert_not_called()
        self.assertEqual(result["status"], "preview")
        self.assertFalse(result["network_call"])

    def test_private_scope_rejected_before_transport(self):
        with patch.object(decisions.decisions_client, "create_decision") as call:
            with self.assertRaises(decisions.InvalidDecisionInput):
                decisions.assess("relevance", {"query": "q", "passage": "p"},
                                 data_scope="private", send=True)
        call.assert_not_called()

    def test_claim_support_uses_supplied_choices(self):
        payload = decisions.build_payload("claim_support", {"claim": "The sky is blue.",
                                                            "passage": "The sky is blue."})
        question = payload["questions"][0]
        self.assertEqual(question["type"], "choice")
        self.assertIn("unresolved", [x["value"] for x in question["choices"]])

    def test_send_returns_advisory_only(self):
        response = {"model": "gpt-6-luna", "answers": [
            {"name": "directly_relevant", "type": "predicate", "probability": 0.8}],
            "usage": {"input_tokens": 20}}
        with patch.object(decisions.decisions_client, "create_decision", return_value=response):
            result = decisions.assess("relevance", {"query": "q", "passage": "p"},
                                      data_scope="public", send=True)
        self.assertTrue(result["advisory_only"])
        self.assertEqual(result["gate"], "OPEN_ORIGINAL_BEFORE_CLAIM")
        self.assertEqual(result["usage"], {"input_tokens": 20})

    def test_file_command_does_not_echo_input(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "input.json"
            path.write_text(json.dumps({"query": "fictional private marker",
                                        "passage": "synthetic passage"}), encoding="utf-8")
            args = decisions.argparse.Namespace(task="relevance", data_scope="synthetic",
                                                 input=str(path), send=False)
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(decisions.cmd_assess(args), 0)
            self.assertNotIn("fictional private marker", output.getvalue())

    def test_stdin_is_bounded_before_json_parse_or_transport(self):
        args = decisions.argparse.Namespace(task="relevance", data_scope="synthetic",
                                             input="-", send=True)
        with patch("sys.stdin", io.TextIOWrapper(io.BytesIO(b"x" * 12001))), \
             patch("sys.stderr", new_callable=io.StringIO) as error, \
             patch.object(decisions.decisions_client, "create_decision") as call:
            self.assertEqual(decisions.cmd_assess(args), 2)
        self.assertIn("too large", error.getvalue())
        call.assert_not_called()

    def test_file_reader_refuses_symlink_and_nonregular_input(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            target = root / "fictional.json"
            target.write_text('{"query":"q","passage":"p"}', encoding="utf-8")
            link = root / "alias.json"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            with self.assertRaisesRegex(decisions.InvalidDecisionInput, "symlink"):
                decisions._read_input(str(link))
            if hasattr(os, "mkfifo"):
                pipe = root / "pipe"
                os.mkfifo(pipe)
                with self.assertRaisesRegex(decisions.InvalidDecisionInput, "regular file"):
                    decisions._read_input(str(pipe))

    def test_send_sanitizes_unexpected_response_fields(self):
        response = {"model": "gpt-6-luna", "answers": [
            {"name": "directly_relevant", "type": "predicate", "probability": 0.8,
             "untrusted_extra": "must not appear"}], "usage": {"input_tokens": 2}}
        with patch.object(decisions.decisions_client, "create_decision", return_value=response):
            result = decisions.assess("relevance", {"query": "q", "passage": "p"},
                                      data_scope="synthetic", send=True)
        self.assertNotIn("untrusted_extra", result["answer"])


if __name__ == "__main__":
    unittest.main()
