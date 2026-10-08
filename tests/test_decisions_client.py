"""Offline contract tests for the Decisions transport, using fictional data."""

import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from context_layer import decisions_client as client


PREDICATE = {"type": "predicate", "name": "useful", "instructions": "Would this public guide help?"}
CHOICE = {"type": "choice", "name": "route", "instructions": "Choose a queue.", "choices": [
    {"value": "guide", "description": "A how-to document."},
    {"value": "other", "description": "None of the listed types."},
]}
SCORE = {"type": "score", "name": "priority", "instructions": "Rate urgency.", "levels": [
    {"label": "Low", "description": "Can wait."},
    {"label": "High", "description": "Needs attention."},
]}


class _Reply:
    status = 200

    def __init__(self, payload):
        self.stream = io.BytesIO(json.dumps(payload).encode())

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, size):
        return self.stream.read(size)


class _Opener:
    def __init__(self, reply):
        self.reply = reply
        self.request = None
        self.timeout = None

    def open(self, request, timeout):
        self.request, self.timeout = request, timeout
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class DecisionsClientTests(unittest.TestCase):
    def payload(self, questions=None):
        return {"model": client.MODEL, "input": "A fictional public setup guide.",
                "questions": questions or [PREDICATE]}

    def test_import_and_invalid_scope_make_no_request(self):
        with patch.object(client.urllib.request, "build_opener") as network:
            with self.assertRaisesRegex(client.DecisionsError, "data_scope_refused"):
                client.create_decision(self.payload(), api_key="test-key", data_scope="private")
            network.assert_not_called()

    def test_missing_key_makes_no_request(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(client.urllib.request, "build_opener") as network:
            with self.assertRaisesRegex(client.DecisionsError, "key_missing"):
                client.create_decision(self.payload())
            network.assert_not_called()

    def test_fixed_endpoint_model_and_typed_predicate(self):
        opener = _Opener(_Reply({"model": client.MODEL, "answers": [{"type": "predicate", "name": "useful", "probability": 0.72}]}))
        with patch.object(client.urllib.request, "build_opener", return_value=opener):
            result = client.create_decision(self.payload(), api_key="test-key", timeout=2, data_scope="synthetic")
        self.assertEqual(result["answers"], [{"name": "useful", "type": "predicate", "probability": 0.72}])
        self.assertEqual(opener.request.full_url, client.ENDPOINT)
        self.assertEqual(opener.request.get_method(), "POST")
        self.assertEqual(opener.timeout, 2)
        self.assertEqual(json.loads(opener.request.data), self.payload())
        self.assertEqual(opener.request.get_header("Authorization"), "Bearer test-key")

    def test_choice_score_and_refusal(self):
        questions = [CHOICE, SCORE, PREDICATE]
        response = {"answers": [
            {"type": "score", "name": "priority", "score": 0.8, "confidence": 0.7,
             "probabilities": [{"value": 0, "label": "Low", "probability": 0.2},
                               {"value": 1, "label": "High", "probability": 0.8}]},
            {"type": "refusal", "name": "useful"},
            {"type": "choice", "name": "route", "choice": "guide", "confidence": 0.9,
             "probabilities": [{"value": "guide", "probability": 0.9},
                               {"value": "other", "probability": 0.1}]},
        ]}
        self.assertEqual([a["type"] for a in client.validate_response(response, questions)],
                         ["choice", "score", "refusal"])

    def test_malformed_distributions_are_rejected(self):
        answer = {"type": "choice", "name": "route", "choice": "guide", "confidence": 0.9,
                  "probabilities": [{"value": "guide", "probability": 0.9},
                                    {"value": "guide", "probability": 0.1}]}
        with self.assertRaisesRegex(client.DecisionsError, "response_invalid"):
            client.validate_response({"answers": [answer]}, [CHOICE])
        answer["probabilities"][1]["value"] = "other"
        answer["confidence"] = float("nan")
        with self.assertRaisesRegex(client.DecisionsError, "response_invalid"):
            client.validate_response({"answers": [answer]}, [CHOICE])

    def test_invalid_request_cannot_open_network(self):
        with patch.object(client.urllib.request, "build_opener") as network:
            for payload in [self.payload([{**PREDICATE, "name": "bad name"}]),
                            self.payload([PREDICATE, PREDICATE]),
                            self.payload([{**CHOICE, "choices": None}]),
                            self.payload([{**SCORE, "levels": None}]),
                            {**self.payload(), "model": "other"}]:
                with self.assertRaises(client.DecisionsError):
                    client.create_decision(payload, api_key="test-key")
            network.assert_not_called()

    def test_redirect_and_rate_limit_have_fixed_codes(self):
        for status, code in [(302, "redirect_refused"), (429, "rate_limited")]:
            error = urllib.error.HTTPError(client.ENDPOINT, status, "secret-response", {}, io.BytesIO(b"secret"))
            with patch.object(client.urllib.request, "build_opener", return_value=_Opener(error)):
                with self.assertRaises(client.DecisionsError) as captured:
                    client.create_decision(self.payload(), api_key="test-key")
            self.assertEqual(captured.exception.code, code)
            self.assertNotIn("secret", str(captured.exception))

    def test_unexpected_answer_name_rejected(self):
        with self.assertRaisesRegex(client.DecisionsError, "response_invalid"):
            client.validate_response({"answers": [{"type": "predicate", "name": "other", "probability": 0.8}]},
                                     [PREDICATE])

    def test_usage_is_bounded_and_model_must_match(self):
        answer = {"type": "predicate", "name": "useful", "probability": 0.5}
        reply = {"model": client.MODEL, "answers": [answer],
                 "usage": {"input_tokens": 42, "output_tokens": 0, "private_text": "ignored"}}
        with patch.object(client.urllib.request, "build_opener", return_value=_Opener(_Reply(reply))):
            result = client.create_decision(self.payload(), api_key="test-key")
        self.assertEqual(result["usage"], {"input_tokens": 42, "output_tokens": 0})
        for broken in [{**reply, "model": "other"},
                       {**reply, "usage": {"input_tokens": -1}},
                       {**reply, "usage": {"input_tokens": True}}]:
            with patch.object(client.urllib.request, "build_opener", return_value=_Opener(_Reply(broken))):
                with self.assertRaisesRegex(client.DecisionsError, "response_invalid"):
                    client.create_decision(self.payload(), api_key="test-key")

    def test_nonobject_http_json_is_a_fixed_invalid_response(self):
        for body in ([], None, "text", 42):
            with patch.object(client.urllib.request, "build_opener",
                              return_value=_Opener(_Reply(body))):
                with self.assertRaises(client.DecisionsError) as caught:
                    client.create_decision(self.payload(), api_key="test-key")
            self.assertEqual(caught.exception.code, "response_invalid")

    def test_boolean_choices_keep_types_and_stable_array_shape(self):
        question = {**CHOICE, "choices": [
            {"value": True, "description": "Boolean true."},
            {"value": "true", "description": "Text true."},
        ]}
        client._request("Fictional type check.", [question])
        answer = {"name": "route", "type": "choice", "choice": True, "confidence": 0.8,
                  "probabilities": [{"value": True, "probability": 0.8},
                                    {"value": "true", "probability": 0.2}]}
        validated = client.validate_response({"answers": [answer]}, [question])
        self.assertEqual(validated, [answer])
        self.assertEqual(client.validate_response({"answers": validated}, [question]), validated)
        for wrong in (1, [], {"value": True}):
            with self.subTest(wrong=wrong), self.assertRaises(client.DecisionsError):
                client.validate_response({"answers": [{**answer, "choice": wrong}]}, [question])

    def test_secret_unicode_huge_numbers_and_invalid_keys_have_fixed_errors(self):
        with patch.object(client.urllib.request, "build_opener") as network:
            for payload in ({**self.payload(), "input": "sk-" + "F" * 25},
                            {**self.payload(), "input": "\ud800"}):
                with self.assertRaises(client.DecisionsError):
                    client.create_decision(payload, api_key="test-token")
            for key in ("test\x00token", "t\u00e9st-token", "x" * 1025):
                with self.assertRaisesRegex(client.DecisionsError, "key_missing"):
                    client.create_decision(self.payload(), api_key=key)
            with self.assertRaisesRegex(client.DecisionsError, "timeout_invalid"):
                client.create_decision(self.payload(), api_key="test-token", timeout=10 ** 400)
            network.assert_not_called()
        for probability in (10 ** 400, float("nan"), float("inf"), True):
            with self.assertRaisesRegex(client.DecisionsError, "response_invalid"):
                client.validate_response({"answers": [{"type": "predicate", "name": "useful",
                                                       "probability": probability}]}, [PREDICATE])

    def test_missing_model_duplicate_json_and_deep_nesting_fail_closed(self):
        for raw in (b'{"answers":[{"type":"predicate","name":"useful","probability":0.8}]}',
                    b'{"model":"other","model":"gpt-6-luna","answers":[]}',
                    b'[' * 2000 + b']' * 2000):
            reply = io.BytesIO(raw)
            reply.status = 200
            with patch.object(client.urllib.request, "build_opener", return_value=_Opener(reply)):
                with self.assertRaisesRegex(client.DecisionsError, "response_invalid"):
                    client.create_decision(self.payload(), api_key="test-token")

    def test_utf8_request_and_response_byte_limits_are_enforced(self):
        with patch.object(client.urllib.request, "build_opener") as network:
            with self.assertRaisesRegex(client.DecisionsError, "request_too_large"):
                client.create_decision({**self.payload(), "input": "\U0001f680" * 10000},
                                       api_key="test-token")
            network.assert_not_called()
        reply = io.BytesIO(b" " * (client.MAX_RESPONSE_BYTES + 1))
        reply.status = 200
        with patch.object(client.urllib.request, "build_opener", return_value=_Opener(reply)):
            with self.assertRaisesRegex(client.DecisionsError, "response_too_large"):
                client.create_decision(self.payload(), api_key="test-token")


if __name__ == "__main__":
    unittest.main()
