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

    def test_all_tasks_cross_real_transport_parser_and_command_boundary(self):
        cases = [
            ("relevance", {"query": "Timer color?", "passage": "The fictional timer is red."},
             {"name": "directly_relevant", "type": "predicate", "probability": 0.8}),
            ("claim_support", {"claim": "The fictional timer is red.",
                               "passage": "The fictional timer is red."},
             {"name": "claim_relation", "type": "choice", "choice": "supports", "confidence": 0.8,
              "probabilities": [{"value": "supports", "probability": 0.8},
                                {"value": "contradicts", "probability": 0.1},
                                {"value": "unresolved", "probability": 0.1}]}),
            ("review_priority", {"item": "Fictional timer stops.", "rubric": "Review failures soon."},
             {"name": "review_priority", "type": "score", "score": 1.3, "confidence": 0.5,
              "probabilities": [{"value": 0, "label": "Later", "probability": 0.2},
                                {"value": 1, "label": "Soon", "probability": 0.3},
                                {"value": 2, "label": "Now", "probability": 0.5}]}),
        ]
        for task, data, answer in cases:
            for refused in (False, True):
                with self.subTest(task=task, refused=refused), tempfile.TemporaryDirectory() as temp:
                    wire_answer = {"name": answer["name"], "type": "refusal"} if refused else answer
                    raw = json.dumps({"model": "gpt-6-luna", "answers": [wire_answer]}).encode()
                    reply = io.BytesIO(raw)
                    reply.status = 200
                    path = Path(temp) / "fictional.json"
                    path.write_text(json.dumps(data), encoding="utf-8")
                    args = decisions.argparse.Namespace(task=task, data_scope="synthetic",
                                                         input=str(path), send=True)
                    with patch.dict(os.environ, {"OPENAI_API_KEY": "test-token"}), \
                         patch.object(decisions.decisions_client.urllib.request, "build_opener") as opener, \
                         patch("sys.stdout", new_callable=io.StringIO) as output:
                        opener.return_value.open.return_value = reply
                        self.assertEqual(decisions.cmd_assess(args), 1 if refused else 0)
                    result = json.loads(output.getvalue())
                    self.assertEqual(result["status"], "refused" if refused else "answered")
                    self.assertEqual(result["answer"], wire_answer)
                    self.assertTrue(result["advisory_only"])
                    self.assertEqual(opener.return_value.open.call_count, 1)

    def test_bad_json_errors_never_echo_input_bytes(self):
        for raw in (b'\xffFICTIONAL_SECRET', b'{"query":"q","query":"r","passage":"p"}',
                    b'[' * 2000 + b']' * 2000):
            with self.subTest(raw=raw[:40]), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "fictional.json"
                path.write_bytes(raw)
                args = decisions.argparse.Namespace(task="relevance", data_scope="synthetic",
                                                     input=str(path), send=True)
                with patch("sys.stderr", new_callable=io.StringIO) as error, \
                     patch.object(decisions.decisions_client, "create_decision") as call:
                    self.assertEqual(decisions.cmd_assess(args), 2)
                self.assertIn(error.getvalue(), (
                    "context-layer decisions: input_json_invalid\n",
                    "context-layer decisions: unknown task or invalid JSON object\n"))
                call.assert_not_called()

    def test_invalid_fields_secrets_unicode_and_send_type_stay_local(self):
        for data in ({"query": "q", "passage": "p", "extra": "ignored"},
                     {"query": "q", "passage": "sk-" + "F" * 25},
                     {"query": "q", "passage": "\ud800"}):
            with patch.object(decisions.decisions_client, "create_decision") as call:
                with self.assertRaises(decisions.InvalidDecisionInput):
                    decisions.assess("relevance", data, data_scope="synthetic", send=True)
                call.assert_not_called()
        with patch.object(decisions.decisions_client, "create_decision") as call:
            with self.assertRaises(decisions.InvalidDecisionInput):
                decisions.assess("relevance", {"query": "q", "passage": "p"},
                                 data_scope="synthetic", send="false")
            call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
