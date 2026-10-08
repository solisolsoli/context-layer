"""Offline Responses boundary checks with fictional inputs and no real API call."""

from contextlib import redirect_stderr, redirect_stdout
import argparse
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from context_layer import responses, responses_client


MODEL = "test-model"


class _Reply(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class _Opener:
    def __init__(self, reply):
        self.reply = reply
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.reply, Exception):
            raise self.reply
        return _Reply(json.dumps(self.reply).encode("utf-8"))


def _response(*, output=None, status="completed"):
    return {"status": status, "model": MODEL, "output": output if output is not None else [
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "A candidate answer."}]},
    ], "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}}


class ResponsesTests(unittest.TestCase):
    def test_preview_is_local_and_does_not_echo_input(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "task.txt"
            source.write_text("FICTIONAL_PUBLIC_SENTINEL", encoding="utf-8")
            args = argparse.Namespace(input_file=str(source), data_scope="synthetic", model=MODEL,
                                      max_output_tokens=128, timeout=10, web_search=False, send=False)
            output = io.StringIO()
            with patch.object(responses_client, "create_response", side_effect=AssertionError("network")):
                with redirect_stdout(output):
                    self.assertEqual(responses.cmd_run(args), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "preview")
        self.assertFalse(report["network_call"])
        self.assertFalse(report["store"])
        self.assertNotIn("FICTIONAL_PUBLIC_SENTINEL", output.getvalue())

    def test_payload_has_no_tools_by_default_and_bounds_web_search(self):
        plain = responses.build_payload("Public fictional task", MODEL, 128)
        self.assertEqual(set(plain), {"model", "input", "store", "max_output_tokens"})
        self.assertIs(plain["store"], False)
        searched = responses.build_payload("Public fictional task", MODEL, 128, True)
        self.assertEqual(searched["tools"], [{"type": "web_search"}])
        self.assertEqual(searched["max_tool_calls"], 1)
        self.assertIs(searched["parallel_tool_calls"], False)
        with self.assertRaises(responses_client.ResponsesError):
            responses_client._request_body({**searched, "max_tool_calls": 2})

    def test_scope_secret_symlink_and_large_input_refused_before_network(self):
        with patch.object(responses_client, "create_response", side_effect=AssertionError("network")):
            with self.assertRaises(responses.InvalidResponseInput):
                responses.run("Fictional", data_scope="private", model=MODEL, send=True)
            with self.assertRaises(responses.InvalidResponseInput):
                responses.run("sk-" + "X" * 25, data_scope="public", model=MODEL, send=True)
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "task.txt"
            source.write_text("Fictional", encoding="utf-8")
            link = Path(temp) / "link.txt"
            link.symlink_to(source)
            with self.assertRaises(responses.InvalidResponseInput):
                responses._read_input(str(link))
            source.write_text("x" * (responses.MAX_INPUT_BYTES + 1), encoding="utf-8")
            with self.assertRaises(responses.InvalidResponseInput):
                responses._read_input(str(source))

    def test_missing_key_never_opens_network(self):
        payload = responses.build_payload("Fictional public task", MODEL)
        with patch.dict(responses_client.os.environ, {}, clear=True):
            with patch.object(responses_client.urllib.request, "build_opener",
                              side_effect=AssertionError("network")):
                with self.assertRaisesRegex(responses_client.ResponsesError, "key_missing"):
                    responses_client.create_response(payload, data_scope="public")

    def test_explicit_send_uses_only_fixed_endpoint_once_and_returns_candidate(self):
        opener = _Opener(_response())
        with patch.dict(responses_client.os.environ, {"OPENAI_API_KEY": "test-token"}):
            with patch.object(responses_client.urllib.request, "build_opener", return_value=opener) as build:
                answer = responses.run("Fictional public task", data_scope="public", model=MODEL,
                                       max_output_tokens=128, send=True)
        self.assertEqual(build.call_count, 1)
        self.assertIs(build.call_args.args[0], responses_client._NoRedirect)
        self.assertEqual(len(opener.requests), 1)
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, responses_client.ENDPOINT)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(timeout, 10)
        self.assertEqual(json.loads(request.data), {
            "model": MODEL, "input": "Fictional public task", "store": False,
            "max_output_tokens": 128,
        })
        self.assertEqual(answer["status"], "answered")
        self.assertEqual(answer["text"], "A candidate answer.")
        self.assertEqual(answer["citations"], [])
        self.assertEqual(answer["gate"], "VERIFY_WITH_ORIGINAL_SOURCES")

    def test_redirect_and_http_body_are_not_exposed_or_retried(self):
        payload = responses.build_payload("Fictional public task", MODEL)
        for code, expected in ((302, "redirect_refused"), (429, "rate_limited"),
                               (500, "http_error")):
            with self.subTest(code):
                error = urllib.error.HTTPError(responses_client.ENDPOINT, code,
                                               "SENSITIVE_SERVER_MESSAGE", {},
                                               io.BytesIO(b"SENSITIVE_SERVER_BODY"))
                opener = _Opener(error)
                with patch.dict(responses_client.os.environ, {"OPENAI_API_KEY": "test-token"}):
                    with patch.object(responses_client.urllib.request, "build_opener",
                                      return_value=opener):
                        with self.assertRaises(responses_client.ResponsesError) as caught:
                            responses_client.create_response(payload, data_scope="public")
                self.assertEqual(str(caught.exception), expected)
                self.assertEqual(len(opener.requests), 1)
                self.assertNotIn("SENSITIVE", str(caught.exception))

    def test_incomplete_refusal_and_unexpected_tool_cannot_be_answers(self):
        payload = responses.build_payload("Fictional public task", MODEL)
        bad = [
            _response(status="incomplete"),
            _response(output=[{"type": "message", "role": "assistant",
                               "content": [{"type": "refusal", "refusal": "No"}]}]),
            _response(output=[{"type": "function_call", "name": "external"}]),
            _response(output=[{"type": "web_search_call", "status": "completed"}]),
        ]
        for item in bad:
            with self.subTest(item=item):
                opener = _Opener(item)
                with patch.dict(responses_client.os.environ, {"OPENAI_API_KEY": "test-token"}):
                    with patch.object(responses_client.urllib.request, "build_opener",
                                      return_value=opener):
                        with self.assertRaises(responses_client.ResponsesError):
                            responses_client.create_response(payload, data_scope="public")

    def test_malformed_response_shapes_and_unicode_fail_closed(self):
        payload = responses.build_payload("Fictional public task", MODEL)
        for malformed in ([], "text", None, {"status": "completed", "model": MODEL,
                                              "output": ["not an item"]}):
            with self.subTest(malformed=malformed):
                opener = _Opener(malformed)
                with patch.dict(responses_client.os.environ, {"OPENAI_API_KEY": "test-token"}):
                    with patch.object(responses_client.urllib.request, "build_opener",
                                      return_value=opener):
                        with self.assertRaises(responses_client.ResponsesError):
                            responses_client.create_response(payload, data_scope="public")
        with self.assertRaises(responses.InvalidResponseInput):
            responses.build_payload("\ud800", MODEL)
        with self.assertRaises(responses.InvalidResponseInput):
            responses.build_payload("Fictional", "\ud800")

    def test_one_web_search_call_is_accepted_only_when_requested(self):
        output = [{"type": "web_search_call", "status": "completed"},
                  {"type": "message", "role": "assistant",
                   "content": [{"type": "output_text", "text": "Candidate with sources."}]}]
        opener = _Opener(_response(output=output))
        with patch.dict(responses_client.os.environ, {"OPENAI_API_KEY": "test-token"}):
            with patch.object(responses_client.urllib.request, "build_opener", return_value=opener):
                answer = responses.run("Fictional public task", data_scope="public", model=MODEL,
                                       web_search=True, send=True)
        self.assertEqual(answer["tool_calls"], 1)

    def test_citations_are_bounded_and_offsets_follow_joined_text(self):
        output = [
            {"type": "web_search_call", "status": "completed"},
            {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "Alpha here", "annotations": [
                    {"type": "url_citation", "url": "https://example.org/a", "title": "Alpha",
                     "start_index": 0, "end_index": 5, "server_extra": "discard"}]},
                {"type": "output_text", "text": "Beta proof", "annotations": [
                    {"type": "url_citation", "url": "http://example.net/b", "title": "Beta",
                     "start_index": 5, "end_index": 10}]},
            ]},
        ]
        opener = _Opener(_response(output=output))
        with patch.dict(responses_client.os.environ, {"OPENAI_API_KEY": "test-token"}):
            with patch.object(responses_client.urllib.request, "build_opener", return_value=opener):
                answer = responses.run("Fictional public task", data_scope="public", model=MODEL,
                                       web_search=True, send=True)
        self.assertEqual(answer["text"], "Alpha here\nBeta proof")
        self.assertEqual(answer["citations"], [
            {"url": "https://example.org/a", "title": "Alpha",
             "start_index": 0, "end_index": 5},
            {"url": "http://example.net/b", "title": "Beta",
             "start_index": 16, "end_index": 21},
        ])
        self.assertNotIn("server_extra", json.dumps(answer))

    def test_malformed_citation_and_model_are_fixed_response_errors(self):
        good = {"type": "url_citation", "url": "https://example.org/a", "title": "Alpha",
                "start_index": 0, "end_index": 5}
        bad_annotations = [
            {**good, "url": "file:///tmp/private"},
            {**good, "url": "https://user@example.org/a"},
            {**good, "end_index": 99},
            {**good, "start_index": True},
            {**good, "title": "Bad\nTitle"},
            {"type": "file_citation", "file_id": "file_x"},
        ]
        for annotation in bad_annotations:
            with self.subTest(annotation=annotation):
                reply = _response(output=[
                    {"type": "message", "role": "assistant", "content": [
                        {"type": "output_text", "text": "Alpha here",
                         "annotations": [annotation]},
                    ]},
                ])
                opener = _Opener(reply)
                with patch.dict(responses_client.os.environ, {"OPENAI_API_KEY": "test-token"}):
                    with patch.object(responses_client.urllib.request, "build_opener",
                                      return_value=opener):
                        with self.assertRaisesRegex(responses_client.ResponsesError,
                                                    "response_invalid"):
                            responses.run("Fictional public task", data_scope="public",
                                          model=MODEL, web_search=True, send=True)
        for model in ("bad model", "bad\x00model", "/model", "\ud800"):
            with self.subTest(model=model):
                with self.assertRaises(responses.InvalidResponseInput):
                    responses.build_payload("Fictional", model)
        bad_model = _response()
        bad_model["model"] = "model\x00name"
        with self.assertRaisesRegex(responses_client.ResponsesError, "response_invalid"):
            responses_client._extract_result(bad_model, web_search=False)

    def test_bad_cli_argument_is_local_error_code_two(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "task.txt"
            source.write_text("Fictional", encoding="utf-8")
            args = argparse.Namespace(input_file=str(source), data_scope="public", model="bad model",
                                      max_output_tokens=128, timeout=10, web_search=False, send=True)
            error = io.StringIO()
            with redirect_stderr(error):
                self.assertEqual(responses.cmd_run(args), 2)
        self.assertIn("model_invalid", error.getvalue())

    def test_cli_error_does_not_echo_secret_or_path(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "task.txt"
            secret = "sk-" + "Q" * 25
            source.write_text(secret, encoding="utf-8")
            args = argparse.Namespace(input_file=str(source), data_scope="public", model=MODEL,
                                      max_output_tokens=128, timeout=10, web_search=False, send=True)
            error = io.StringIO()
            with redirect_stderr(error):
                self.assertEqual(responses.cmd_run(args), 2)
        self.assertIn("credential_pattern_refused", error.getvalue())
        self.assertNotIn(secret, error.getvalue())
        self.assertNotIn(temp, error.getvalue())

    def test_direct_transport_refuses_secret_before_network(self):
        payload = {"model": MODEL, "input": "OPENAI_API_KEY = fictional-token",
                   "store": False, "max_output_tokens": 128}
        with patch.object(responses_client.urllib.request, "build_opener") as network:
            with self.assertRaisesRegex(responses_client.ResponsesError, "credential_pattern_refused"):
                responses_client.create_response(payload, data_scope="synthetic")
            network.assert_not_called()

    def test_refusal_and_malformed_unicode_are_distinct_fixed_errors(self):
        for part, code in (({"type": "refusal", "refusal": "FICTIONAL_SENSITIVE_REASON"}, "response_refused"),
                           ({"type": "output_text", "text": "\ud800"}, "response_invalid")):
            reply = _response(output=[{"type": "message", "role": "assistant", "content": [part]}])
            with self.assertRaises(responses_client.ResponsesError) as caught:
                responses_client._extract_result(reply, web_search=False)
            self.assertEqual(str(caught.exception), code)

    def test_invalid_send_web_flag_timeout_and_key_never_call_network(self):
        with patch.object(responses_client.urllib.request, "build_opener") as network:
            for args in ({"send": "false"}, {"web_search": "false"}, {"timeout": 10 ** 400}):
                with self.assertRaises(responses.InvalidResponseInput):
                    responses.run("Fictional", data_scope="synthetic", model=MODEL, **args)
            for key in ("token\x00text", "t\u00e9st-token", "x" * 1025):
                with patch.object(responses_client.os, "environ", {"OPENAI_API_KEY": key}):
                    with self.assertRaisesRegex(responses_client.ResponsesError, "key_missing"):
                        responses_client.create_response(responses.build_payload("Fictional", MODEL),
                                                         data_scope="synthetic")
            network.assert_not_called()

    def test_duplicate_keys_and_deep_response_json_fail_closed(self):
        for raw in (b'{"status":"failed","status":"completed","output":[]}',
                    b'[' * 2000 + b']' * 2000):
            with patch.dict(responses_client.os.environ, {"OPENAI_API_KEY": "test-token"}), \
                 patch.object(responses_client.urllib.request, "build_opener") as opener:
                opener.return_value.open.return_value = _Reply(raw)
                with self.assertRaises(responses_client.ResponsesError):
                    responses_client.create_response(responses.build_payload("Fictional", MODEL),
                                                     data_scope="synthetic")

    def test_serialized_request_response_and_answer_limits_are_enforced(self):
        with patch.object(responses_client.urllib.request, "build_opener") as network:
            with self.assertRaisesRegex(responses.InvalidResponseInput, "request_too_large"):
                responses.run("\x00" * 7000, data_scope="synthetic", model=MODEL, send=True)
            network.assert_not_called()
        with patch.dict(responses_client.os.environ, {"OPENAI_API_KEY": "test-token"}), \
             patch.object(responses_client.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value = _Reply(b" " * (responses_client.MAX_RESPONSE_BYTES + 1))
            with self.assertRaisesRegex(responses_client.ResponsesError, "response_too_large"):
                responses_client.create_response(responses.build_payload("Fictional", MODEL),
                                                 data_scope="synthetic")
        with self.assertRaisesRegex(responses_client.ResponsesError, "response_invalid"):
            responses_client._extract_result(_response(output=[{
                "type": "message", "role": "assistant", "content": [{
                    "type": "output_text", "text": "x" * (responses_client.MAX_ANSWER_CHARACTERS + 1)
                }]}]), web_search=False)


if __name__ == "__main__":
    unittest.main()
