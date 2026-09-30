"""Tests for context_layer.jev_client, the advisor's only network and model-CLI surface.

Offline by construction: every server is a loopback `http.server` fake started by
this file, every model CLI is a script this file writes (a fake `claude` on a
temporary PATH), HOME points into a temporary directory, and a guard installed
for the whole module refuses any socket connection that is not to 127.0.0.1 or
::1. No real provider, key, `claude` or `codex` is ever used.
"""
import http.server
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from _portable_helpers import process_is_gone
from unittest import mock
import urllib.request

from _portable_helpers import isolated_home_env

REPO = Path(os.environ.get("TEST_REPO_HOME", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO))
from context_layer import jev_client, jev_contracts as contracts  # noqa: E402

KEY = "fictional-key-7730-do-not-log"
VAULT_MARKER = "VAULT-TEXT-MARKER-5521"
BODY_MARKER = "PRIVATE-BODY-MARKER-3108"
REASON_MARKER = "PRIVATE-REASON-MARKER"
RESULT_KEYS = {"ok", "answer", "code", "latency_ms", "usage", "model_reported", "requests"}
SYSTEMONE_REPLY = {"answers": {"q": {"type": "noul", "noul": 0.83}},
                   "usage": {"input_tokens": 412, "output_tokens": 0}, "model": "jev-1.13.0"}


class OutsideNetwork(BaseException):
    """Raised when a test tries to connect anywhere but loopback (never caught by code)."""


class NetworkAttempted(BaseException):
    """Raised when a test that must not touch the network or start a process does."""


_REAL_CONNECT = socket.socket.connect


def _loopback_only(sock, address, *args):
    if sock.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in ("127.0.0.1", "::1"):
        raise OutsideNetwork("a test tried to leave loopback")
    return _REAL_CONNECT(sock, address, *args)


def setUpModule():
    global _HOME, _PATCH
    _HOME = tempfile.TemporaryDirectory()
    _PATCH = mock.patch.dict(os.environ, isolated_home_env(os.environ, _HOME.name))
    _PATCH.start()
    socket.socket.connect = _loopback_only


def tearDownModule():
    socket.socket.connect = _REAL_CONNECT
    _PATCH.stop()
    _HOME.cleanup()


def relevance(excerpt="The keeper checks every lamp at dusk and logs each repair."):
    return contracts.build_questionnaire("search", "relevance.v1", {
        "request": "who maintains the harbor lights after launch", "title": "Harbor Keeper",
        "link_line": "Ask [[Harbor Keeper]] about anything beyond the opening week.",
        "excerpt": excerpt})


def claim(marker=VAULT_MARKER):
    return contracts.build_questionnaire("answer", "claim_support.v1", {
        "claim": "The ferry leaves at nine.", "quote": f"The ferry leaves at nine. {marker}",
        "section": f"## Timetable\nThe ferry leaves at nine. {marker}"})


def systemone(url, **extra):
    provider = {"kind": "systemone", "base_url": url, "model": "jev-1.13.0"}
    provider.update(extra)
    return provider


def run(provider, questionnaires, deadline=5.0, parallel=1, key=None, capture=None):
    return jev_client.evaluate(provider, questionnaires, deadline_s=deadline,
                               max_parallel=parallel, key=key, capture=capture)


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


# ---------------------------------------------------------------------------
# Loopback fake servers
# ---------------------------------------------------------------------------

class _QuietServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False  # a dribbling handler must not hold up tearDown


class FakeServer:
    """A loopback HTTP server whose reply is a behaviour(handler, body) callable."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.lock = threading.Lock()
        self.requests = []
        self.active = 0
        self.peak = 0
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with fake.lock:
                    fake.requests.append({"method": self.command, "path": self.path,
                                          "headers": {k.lower(): v for k, v in
                                                      self.headers.items()},
                                          "body": body})
                    fake.active += 1
                    fake.peak = max(fake.peak, fake.active)
                try:
                    fake.behaviour(self, body)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with fake.lock:
                        fake.active -= 1

            do_POST = serve
            do_GET = serve

        self.httpd = _QuietServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    @property
    def hits(self):
        with self.lock:
            return len(self.requests)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def reply_json(document, status=200, delay=0.0):
    def behaviour(handler, body):
        if delay:
            time.sleep(delay)
        data = json.dumps(document).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
    return behaviour


def reply_status(status):
    def behaviour(handler, body):
        data = (BODY_MARKER + " " + KEY).encode("utf-8")
        handler.send_response(status, REASON_MARKER)
        handler.send_header("Content-Type", "text/plain")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
    return behaviour


def reply_bytes(data, with_length=True):
    def behaviour(handler, body):
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        if with_length:
            handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
    return behaviour


def dribble(handler, body):
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", "100000")
    handler.end_headers()
    stop = time.monotonic() + 4.0
    while time.monotonic() < stop:
        handler.wfile.write(b" ")
        time.sleep(0.1)


class ServerCase(unittest.TestCase):
    def serve(self, behaviour):
        server = FakeServer(behaviour)
        self.addCleanup(server.close)
        return server


# ---------------------------------------------------------------------------
# Endpoints and keys
# ---------------------------------------------------------------------------

class Endpoints(ServerCase):
    def test_accepted_endpoints_are_normalised(self):
        cases = {"https://api.typesafe.example": "https://api.typesafe.example",
                 "https://openrouter.example/api/": "https://openrouter.example/api",
                 "http://127.0.0.1:8765": "http://127.0.0.1:8765",
                 "http://[::1]:8080/v1/": "http://[::1]:8080/v1",
                 "HTTPS://API.Example.TEST:443": "https://api.example.test:443",
                 "https://10.0.0.5": "https://10.0.0.5"}
        for url, normalised in cases.items():
            self.assertEqual(jev_client.validate_endpoint(url), normalised)

    def test_refused_endpoints_carry_a_fixed_code_and_never_echo_the_url(self):
        cases = {
            "http://localhost:8765": "endpoint_localhost",
            "https://localhost": "endpoint_localhost",
            "https://lamps.localhost": "endpoint_localhost",
            "http://api.example.test": "endpoint_not_loopback",
            "http://127.0.0.2:8765": "endpoint_not_loopback",
            "http://10.0.0.1": "endpoint_not_loopback",
            "http://[::2]:8080": "endpoint_not_loopback",
            "https://user:SECRET-PASS@api.example.test": "endpoint_userinfo",
            "https://SECRET-TOKEN@api.example.test": "endpoint_userinfo",
            "https://api.example.test/v1?key=SECRET": "endpoint_query",
            "https://api.example.test?": "endpoint_query",
            "https://api.example.test/#part": "endpoint_fragment",
            "http://127.0.0.1:0": "endpoint_port",
            "http://127.0.0.1:65536": "endpoint_port",
            "http://127.0.0.1:abc": "endpoint_port",
            "http://127.0.0.1:": "endpoint_port",
            "http://127.0.0.1:+80": "endpoint_port",
            "ftp://api.example.test": "endpoint_scheme",
            "file:///etc/hosts": "endpoint_scheme",
            "api.example.test": "endpoint_scheme",
            "": "endpoint_not_text",
            "https://api.example.test/a b": "endpoint_not_text",
            "https://api.example.test/\n": "endpoint_not_text",
            "https://api.ex\u00e4mple.test": "endpoint_host",
            "https://[not-an-address]": "endpoint_host",
            "https://": "endpoint_host",
            "https://api.example.test/../v1": "endpoint_path",
            "https://api.example.test/a%2e": "endpoint_path",
        }
        for url, code in cases.items():
            with self.assertRaises(jev_client.EndpointInvalid) as caught:
                jev_client.validate_endpoint(url)
            self.assertEqual(caught.exception.code, code, url)
            self.assertEqual(str(caught.exception), code)
        for value in (None, 8080, b"https://api.example.test"):
            with self.assertRaises(jev_client.EndpointInvalid):
                jev_client.validate_endpoint(value)

    def test_refused_before_any_io(self):
        server = self.serve(reply_json(SYSTEMONE_REPLY))
        port = server.httpd.server_address[1]
        for url in (f"http://localhost:{port}", f"http://127.0.0.1:{port}/?x=1",
                    f"http://user:pw@127.0.0.1:{port}"):
            result = run(systemone(url), [relevance()])[0]
            self.assertEqual((result["code"], result["requests"]), ("endpoint_invalid", 0))
        self.assertEqual(server.hits, 0)

    def test_loopback_detection(self):
        self.assertTrue(jev_client.is_loopback("http://127.0.0.1:8765"))
        self.assertTrue(jev_client.is_loopback("http://[::1]:8765"))
        self.assertFalse(jev_client.is_loopback("https://api.example.test"))


class Keys(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / "vault"
        (self.vault / ".context").mkdir(parents=True)
        (self.vault / ".context" / "routes.json").write_text("{}", encoding="utf-8")
        self.outside = self.root / "keys"
        self.outside.mkdir()
        self.provider = {"kind": "systemone", "base_url": "https://api.example.test",
                         "model": "jev-1.13.0", "key_env": "JEV_TEST_KEY"}
        cleaned = {name: value for name, value in os.environ.items() if name != "JEV_TEST_KEY"}
        patcher = mock.patch.dict(os.environ, cleaned, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def env_file(self, text, where=None):
        path = (where or self.outside) / "jev.env"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def assertRefused(self, code, env_file, provider=None, **kwargs):
        with self.assertRaises(jev_client.KeyConfigError) as caught:
            jev_client.load_key(provider or self.provider, env_file, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)
        return caught.exception

    def test_key_from_the_environment(self):
        with mock.patch.dict(os.environ, {"JEV_TEST_KEY": KEY}):
            self.assertEqual(jev_client.load_key(self.provider, None), KEY)
            path = self.env_file("JEV_TEST_KEY=from-the-file\n")
            self.assertEqual(jev_client.load_key(self.provider, path), KEY)  # environment wins

    def test_key_from_literal_env_file_lines(self):
        path = self.env_file("# fictional\nOTHER=1\nexport JEV_TEST_KEY=\"first\"\n"
                             f"JEV_TEST_KEY='{KEY}'\n")
        self.assertEqual(jev_client.load_key(self.provider, path), KEY)
        self.assertIsNone(jev_client.load_key(self.provider, self.env_file("OTHER=1\n")))
        self.assertIsNone(jev_client.load_key(self.provider, None))

    def test_env_file_is_never_executed(self):
        marker = self.root / "executed-marker"
        text = f"JEV_TEST_KEY=$(touch {marker})\n"
        self.assertEqual(jev_client.load_key(self.provider, self.env_file(text)),
                         f"$(touch {marker})")
        text = f"JEV_TEST_KEY=`touch {marker}`\n"
        self.assertEqual(jev_client.load_key(self.provider, self.env_file(text)),
                         f"`touch {marker}`")
        self.assertFalse(marker.exists())

    def test_env_file_inside_the_vault_is_refused(self):
        inside = self.env_file(f"JEV_TEST_KEY={KEY}\n", where=self.vault)
        error = self.assertRefused("env_file_inside_vault", inside)
        self.assertNotIn(KEY, str(error))
        self.assertRefused("env_file_inside_vault", inside, vault=str(self.vault))
        hidden = self.env_file(f"JEV_TEST_KEY={KEY}\n", where=self.vault / ".context")
        self.assertRefused("env_file_inside_vault", hidden)
        bare = self.root / "unmarked-vault"
        bare.mkdir()
        self.assertRefused("env_file_inside_vault", self.env_file("JEV_TEST_KEY=x\n", bare),
                           vault=str(bare))

    def test_env_file_must_be_absolute_regular_and_not_a_symlink(self):
        self.assertRefused("env_file_not_absolute", "jev.env")
        real = self.env_file(f"JEV_TEST_KEY={KEY}\n")
        link = self.outside / "link.env"
        link.symlink_to(real)
        self.assertRefused("env_file_symlink", str(link))
        self.assertRefused("env_file_unreadable", str(self.outside / "missing.env"))
        self.assertRefused("env_file_not_regular", str(self.outside))
        big = self.outside / "big.env"
        big.write_text("#" * (64 * 1024 + 1), encoding="utf-8")
        self.assertRefused("env_file_too_large", str(big))

    def test_key_names_are_checked(self):
        for name in ("lower_case", "1ABC", "A", "A-B", "A" * 65, 7):
            self.assertRefused("key_env_invalid", None, dict(self.provider, key_env=name))
        compat = {"kind": "openai_compat", "api_key_env": "JEV_ONE", "key_env": "JEV_TWO"}
        self.assertRefused("key_env_conflict", None, compat)

    def test_only_key_reading_kinds_read_a_key(self):
        with mock.patch.dict(os.environ, {"JEV_TEST_KEY": KEY}):
            for kind in ("host_cli", "cmd", "recorded", "fake"):
                self.assertIsNone(jev_client.load_key({"kind": kind, "key_env": "JEV_TEST_KEY"},
                                                      None))
            compat = {"kind": "openai_compat", "api_key_env": "JEV_TEST_KEY"}
            self.assertEqual(jev_client.load_key(compat, None), KEY)
            self.assertIsNone(jev_client.load_key({"kind": "systemone"}, None))


# ---------------------------------------------------------------------------
# systemone over loopback: request shape, bounds, errors
# ---------------------------------------------------------------------------

class SystemOne(ServerCase):
    def test_request_shape_answer_and_usage(self):
        server = self.serve(reply_json(SYSTEMONE_REPLY))
        result = run(systemone(server.url, key_env="JEV_TEST_KEY"), [relevance()], key=KEY)[0]
        self.assertEqual(set(result), RESULT_KEYS)
        self.assertTrue(result["ok"], result)
        self.assertIsNone(result["code"])
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["model_reported"], "jev-1.13.0")
        self.assertEqual(result["usage"], {"input_tokens": 412, "output_tokens": 0,
                                           "cache_creation_input_tokens": 0,
                                           "cache_read_input_tokens": 0, "cost_usd": None})
        answer = result["answer"]
        self.assertEqual((answer["label"], answer["p_yes"]), ("yes", 0.83))
        self.assertAlmostEqual(answer["confidence"], 0.66)
        self.assertEqual(answer["provenance"]["provider_kind"], "systemone")
        self.assertEqual(answer["provenance"]["model_reported"], "jev-1.13.0")
        request = server.requests[0]
        self.assertEqual((request["method"], request["path"]), ("POST", "/v1/systemone"))
        headers = request["headers"]
        self.assertEqual(headers["authorization"], "Bearer " + KEY)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["user-agent"], "context-layer/" + jev_client.__version__)
        body = json.loads(request["body"])
        question = relevance()["question"]
        self.assertEqual(body, {"model": "jev-1.13.0", "state": relevance()["state"],
                                "questions": {"q": {"type": "noul",
                                                    "instructions": question["instructions"],
                                                    "criteria": question["criteria"]}}})
        self.assertNotIn(KEY, json.dumps(result))

    def test_path_follows_the_base(self):
        server = self.serve(reply_json(SYSTEMONE_REPLY))
        run(systemone(server.url + "/v1"), [relevance()])
        run(systemone(server.url + "/api"), [relevance()])
        self.assertEqual([request["path"] for request in server.requests],
                         ["/v1/systemone", "/api/v1/systemone"])

    def test_no_key_sends_no_authorization(self):
        server = self.serve(reply_json(SYSTEMONE_REPLY))
        self.assertTrue(run(systemone(server.url), [relevance()])[0]["ok"])
        self.assertNotIn("authorization", server.requests[0]["headers"])

    def test_missing_or_malformed_key_is_refused_before_io(self):
        server = self.serve(reply_json(SYSTEMONE_REPLY))
        missing = run(systemone(server.url, key_env="JEV_TEST_KEY"), [relevance()])[0]
        self.assertEqual((missing["code"], missing["requests"]), ("key_missing", 0))
        injected = run(systemone(server.url), [relevance()],
                       key="fictional\r\nX-Injected: yes")[0]
        self.assertEqual((injected["code"], injected["requests"]), ("key_invalid", 0))
        self.assertNotIn("Injected", json.dumps(injected))
        self.assertEqual(server.hits, 0)

    def test_redirects_are_refused(self):
        target = self.serve(reply_json(SYSTEMONE_REPLY))
        for status in (301, 302, 303, 307, 308):
            def redirect(handler, body, status=status):
                handler.send_response(status)
                handler.send_header("Location", target.url + "/v1/systemone")
                handler.send_header("Content-Length", "0")
                handler.end_headers()
            server = self.serve(redirect)
            result = run(systemone(server.url), [relevance()])[0]
            self.assertEqual((result["code"], result["requests"]), ("redirect_refused", 1))
            self.assertEqual(server.hits, 1)
        self.assertEqual(target.hits, 0)

    def test_body_over_one_million_bytes_is_refused(self):
        oversized = b"{" + b" " * 999_999 + b"}"
        self.assertEqual(len(oversized), 1_000_001)
        for with_length in (True, False):
            server = self.serve(reply_bytes(oversized, with_length=with_length))
            result = run(systemone(server.url), [relevance()])[0]
            self.assertEqual(result["code"], "response_too_large", with_length)
            self.assertFalse(result["ok"])

    def test_body_of_exactly_one_million_bytes_is_read(self):
        document = dict(SYSTEMONE_REPLY, pad="")
        size = len(json.dumps(document).encode("utf-8"))
        document["pad"] = "x" * (1_000_000 - size)
        data = json.dumps(document).encode("utf-8")
        self.assertEqual(len(data), 1_000_000)
        server = self.serve(reply_bytes(data, with_length=False))
        self.assertTrue(run(systemone(server.url), [relevance()])[0]["ok"])

    def test_a_dribbling_server_hits_the_deadline_without_retry(self):
        server = self.serve(dribble)
        started = time.monotonic()
        result = run(systemone(server.url), [relevance()], deadline=1.0)[0]
        elapsed = time.monotonic() - started
        self.assertEqual((result["code"], result["requests"]), ("deadline_exceeded", 1))
        self.assertLess(elapsed, 2.0)
        self.assertGreaterEqual(result["latency_ms"], 900)
        time.sleep(0.3)
        self.assertEqual(server.hits, 1)

    def test_http_errors_map_to_fixed_codes_without_body_or_retry(self):
        expected = {401: "http_unauthorized", 403: "http_forbidden", 404: "http_not_found",
                    422: "http_unprocessable", 429: "http_rate_limited", 529: "http_overloaded",
                    500: "http_server_error", 502: "http_server_error", 418: "http_error"}
        for status, code in expected.items():
            server = self.serve(reply_status(status))
            result = run(systemone(server.url, key_env="JEV_TEST_KEY"), [relevance()], key=KEY)[0]
            self.assertEqual((result["code"], result["requests"]), (code, 1), status)
            text = json.dumps(result)
            for marker in (BODY_MARKER, REASON_MARKER, KEY):
                self.assertNotIn(marker, text)
            self.assertEqual(server.hits, 1, "no retry")

    def test_unreachable_and_invalid_replies(self):
        refused = run(systemone(f"http://127.0.0.1:{free_port()}"), [relevance()])[0]
        self.assertEqual((refused["code"], refused["requests"]), ("provider_unreachable", 1))
        cases = [(reply_bytes(b"not json"), "response_invalid"),
                 (reply_bytes(b"[1, 2]"), "response_invalid"),
                 (reply_json({"usage": {}}), "answer_not_object"),
                 (reply_json({"answers": {"q": {"type": "noul", "noul": 1.7}}}),
                  "value_out_of_range"),
                 (reply_json({"answers": {"q": {"type": "noul", "noul": 0.9}, "q2": {}}}),
                  "answer_ids_mismatch"),
                 (reply_json({"answers": {"q": {"type": "noul", "label": "yes"}}}),
                  "probabilities_missing")]
        for behaviour, code in cases:
            server = self.serve(behaviour)
            result = run(systemone(server.url), [relevance()])[0]
            self.assertEqual((result["ok"], result["code"]), (False, code))
            self.assertIsNone(result["answer"])

    def test_counters_too_large_for_a_float_are_dropped_not_fatal(self):
        huge = "1" + "0" * 400
        data = ('{"answers": {"q": {"type": "noul", "noul": 0.9}}, '
                '"usage": {"input_tokens": %s, "output_tokens": 3, "cost": %s}}' % (huge, huge))
        server = self.serve(reply_bytes(data.encode("utf-8")))
        result = run(systemone(server.url), [relevance()])[0]
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["usage"]["input_tokens"], result["usage"]["output_tokens"]),
                         (0, 3))
        self.assertIsNone(result["usage"]["cost_usd"])
        provider = {"kind": "host_cli", "model": "haiku", "max_budget_usd": json.loads(huge)}
        self.assertEqual(run(provider, [relevance()])[0]["code"], "provider_invalid")

    def test_reported_model_must_be_a_bounded_identifier(self):
        server = self.serve(reply_json(dict(SYSTEMONE_REPLY, model="evil model\nname")))
        result = run(systemone(server.url), [relevance()])[0]
        self.assertTrue(result["ok"])
        self.assertIsNone(result["model_reported"])

    def test_parallel_requests_are_bounded_and_ordered(self):
        server = self.serve(reply_json(SYSTEMONE_REPLY, delay=0.3))
        questionnaires = [relevance(f"Lamp log entry {index}.") for index in range(6)]
        results = run(systemone(server.url), questionnaires, parallel=3)
        self.assertEqual(len(results), 6)
        self.assertTrue(all(result["ok"] for result in results))
        self.assertLessEqual(server.peak, 3)
        self.assertGreaterEqual(server.peak, 2)
        bodies = [json.loads(request["body"])["state"]["excerpt"] for request in server.requests]
        self.assertEqual(sorted(bodies), sorted(q["state"]["excerpt"] for q in questionnaires))

    def test_laya_profile_is_sequential(self):
        server = self.serve(reply_json(SYSTEMONE_REPLY, delay=0.2))
        results = run(systemone(server.url, profile="laya"), [relevance()] * 3, parallel=4)
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(server.peak, 1)

    def test_rounding_profile_reaches_the_validator(self):
        rounded = {"answers": {"q": {"type": "choice", "choice": "supports", "probabilities": {
            "supports": 0.33, "contradicts": 0.33, "silent": 0.33}}}}
        server = self.serve(reply_json(rounded))
        self.assertEqual(run(systemone(server.url), [claim()])[0]["code"], "probabilities_sum")
        self.assertTrue(run(systemone(server.url, rounding="2dp"), [claim()])[0]["ok"])


class Proxies(ServerCase):
    def test_loopback_skips_environment_proxies_that_plain_urllib_follows(self):
        proxy = self.serve(reply_bytes(b"proxied"))
        target = self.serve(reply_json(SYSTEMONE_REPLY))
        variables = {name: proxy.url for name in ("http_proxy", "https_proxy", "all_proxy",
                                                  "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
        with mock.patch.dict(os.environ, variables):
            for name in ("no_proxy", "NO_PROXY"):
                os.environ.pop(name, None)
            # Control: a default urllib opener sends 127.0.0.1 traffic to the proxy.
            urllib.request.build_opener().open(target.url + "/control", timeout=5).read()
            self.assertEqual((proxy.hits, target.hits), (1, 0))
            self.assertTrue(proxy.requests[0]["path"].startswith(target.url))
            result = run(systemone(target.url), [relevance()])[0]
            self.assertTrue(result["ok"], result)
            self.assertEqual((proxy.hits, target.hits), (1, 1))
            remote = jev_client._opener("https://api.example.test")
            local = jev_client._opener(target.url)
        proxies = [handler.proxies for handler in remote.handlers
                   if isinstance(handler, urllib.request.ProxyHandler)]
        self.assertEqual(proxies[0].get("https"), proxy.url)  # remote follows the environment
        # ProxyHandler({}) defines no *_open method, so the opener lists no proxy at all.
        local_proxies = [handler.proxies for handler in local.handlers
                         if isinstance(handler, urllib.request.ProxyHandler) and handler.proxies]
        self.assertEqual(local_proxies, [])


# ---------------------------------------------------------------------------
# openai_compat
# ---------------------------------------------------------------------------

def chat_reply(content, usage=None):
    document = {"model": "fictional-local-model",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": content}}]}
    if usage is not None:
        document["usage"] = usage
    return reply_json(document)


class OpenAICompat(ServerCase):
    def provider(self, url, **extra):
        provider = {"kind": "openai_compat", "base_url": url, "model": "fictional-local-model"}
        provider.update(extra)
        return provider

    def test_schema_constrained_label_only_answer(self):
        server = self.serve(chat_reply('{"answer": "yes"}',
                                       {"prompt_tokens": 50, "completion_tokens": 5}))
        result = run(self.provider(server.url), [relevance()])[0]
        self.assertTrue(result["ok"], result)
        answer = result["answer"]
        self.assertEqual((answer["label"], answer["p_yes"], answer["confidence"]),
                         ("yes", None, None))
        self.assertEqual((result["usage"]["input_tokens"], result["usage"]["output_tokens"]),
                         (50, 5))
        self.assertEqual(result["model_reported"], "fictional-local-model")
        request = server.requests[0]
        self.assertEqual(request["path"], "/v1/chat/completions")
        body = json.loads(request["body"])
        self.assertEqual(body["response_format"]["type"], "json_schema")
        schema_block = body["response_format"]["json_schema"]
        self.assertEqual((schema_block["name"], schema_block["strict"]), ("jev_answer", True))
        self.assertEqual(schema_block["schema"]["properties"]["answer"]["enum"], ["yes", "no"])
        self.assertEqual(schema_block["schema"]["required"], ["answer"])
        self.assertEqual((body["temperature"], body["seed"]), (0, 0))
        self.assertLessEqual(body["max_tokens"], 64)
        self.assertEqual(json.loads(body["messages"][1]["content"]), relevance())
        self.assertNotIn("authorization", request["headers"])

    def test_bad_output_degrades(self):
        for content in ("not json", '{"answer": "maybe"}', '{"answer": "yes", "why": "x"}', "[]"):
            server = self.serve(chat_reply(content))
            result = run(self.provider(server.url), [relevance()])[0]
            self.assertFalse(result["ok"])
            self.assertIn(result["code"], ("output_invalid", "label_unknown"), content)
        server = self.serve(reply_json({"choices": []}))
        self.assertEqual(run(self.provider(server.url), [relevance()])[0]["code"],
                         "response_invalid")

    def test_loopback_only_and_optional_key(self):
        remote = run(self.provider("https://api.example.test"), [relevance()])[0]
        self.assertEqual((remote["code"], remote["requests"]), ("endpoint_invalid", 0))
        server = self.serve(chat_reply('{"answer": "no"}'))
        result = run(self.provider(server.url, api_key_env="JEV_LOCAL_KEY"), [relevance()],
                     key=KEY)[0]
        self.assertTrue(result["ok"])
        self.assertEqual(server.requests[0]["headers"]["authorization"], "Bearer " + KEY)


# ---------------------------------------------------------------------------
# Local programs: host_cli, cmd, fake
# ---------------------------------------------------------------------------

FAKE_CLAUDE = r'''#!@PYTHON@
import json, os, subprocess, sys, time
stdin = sys.stdin.read()
record = {"argv": sys.argv, "cwd": os.getcwd(), "pid": os.getpid(),
          "child": os.environ.get("CONTEXT_LAYER_JEV_CHILD"), "stdin": stdin,
          "key_in_env": "@KEY@" in json.dumps(dict(os.environ))}
mode = os.environ.get("JEV_TEST_CLAUDE_MODE", "success")
if mode == "slow":
    record["grandchild"] = subprocess.Popen([sys.executable, "-c",
                                             "import time; time.sleep(60)"]).pid
with open(os.environ["JEV_TEST_CLAUDE_LOG"], "w") as handle:
    json.dump(record, handle)
if mode == "slow":
    time.sleep(60)
result = {"type": "result", "subtype": "success", "is_error": False, "result": "",
          "num_turns": 2, "total_cost_usd": 0.0161,
          "usage": {"input_tokens": 1200, "output_tokens": 40,
                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
          "modelUsage": {"claude-haiku-fictional": {"inputTokens": 1200}}}
answer = {"answer": os.environ.get("JEV_TEST_CLAUDE_ANSWER", "supports")}
if mode == "success":
    result["structured_output"] = answer
elif mode == "is_error":
    result["is_error"] = True
    result["structured_output"] = answer
elif mode == "retries":
    result["subtype"] = "error_max_structured_output_retries"
elif mode == "extra":
    result["structured_output"] = dict(answer, why="because")
elif mode == "garbage":
    print("this is not json")
    sys.exit(0)
elif mode == "crash":
    print("boom")
    sys.exit(3)
print(json.dumps(result))
'''

FAKE_ANSWER = r'''#!@PYTHON@
import json, os, sys, time
questionnaire = json.loads(sys.stdin.read())
marker = os.environ.get("JEV_TEST_FAKE_MARKER")
if marker:
    open(marker, "w").close()
mode = os.environ.get("JEV_TEST_FAKE_MODE", "probability")
if mode == "slow":
    time.sleep(60)
if mode == "garbage":
    print("no answer here")
    sys.exit(0)
if mode == "exit":
    sys.exit(2)
kind = questionnaire["question"]["type"]
if mode == "label":
    item = {"type": "noul", "label": "yes"} if kind == "noul" else {"type": "choice",
                                                                     "choice": "silent"}
    print(json.dumps({"q": item}))
elif kind == "noul":
    print(json.dumps({"answers": {"q": {"type": "noul", "noul": 0.9}},
                      "usage": {"input_tokens": 30, "output_tokens": 1},
                      "model": "fake-judge-1"}))
else:
    print(json.dumps({"q": {"type": "choice", "choice": "contradicts", "probabilities": {
        "supports": 0.05, "contradicts": 0.9, "silent": 0.05}}}))
'''

CMD_PROGRAM = r'''import json, sys
questionnaire = json.load(open(sys.argv[1], encoding="utf-8"))
assert questionnaire["contract"] == "jev-questionnaire/v1"
print(json.dumps({"q": {"type": "choice", "choice": "supports", "probabilities": {
    "supports": 0.8, "contradicts": 0.1, "silent": 0.1}}}))
'''


def jev_temp_dirs():
    base = Path(tempfile.gettempdir())
    return {path.name for path in base.glob("context-layer-jev-*")}


def gone(pid, within=5.0):
    stop = time.monotonic() + within
    while time.monotonic() < stop:
        if process_is_gone(pid):
            return True
        time.sleep(0.05)
    return False


class ProgramCase(unittest.TestCase):
    def setUp(self):
        # A private temp root: the leftover-directory checks must not see other
        # processes' advisor runs in the shared system temp directory.
        private = tempfile.mkdtemp(prefix="jev-client-test-")
        self.addCleanup(shutil.rmtree, private, True)
        patcher = mock.patch.object(tempfile, "tempdir", private)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.vault = self.root / "vault"
        (self.vault / ".context").mkdir(parents=True)
        (self.vault / ".context" / "routes.json").write_text("{}", encoding="utf-8")
        self.log = self.root / "claude-call.json"

    def script(self, name, body, directory=None):
        folder = directory or self.bin
        content = body.replace("@PYTHON@", sys.executable).replace("@KEY@", KEY)
        if os.name == "nt":
            stem = Path(name).stem
            program = folder / f"{stem}.py"
            program.write_text(content, encoding="utf-8", newline="\n")
            if stem == "claude":
                command = folder / "claude.cmd"
                command.write_text(f'@"{sys.executable}" "{program}" %*\r\n',
                                   encoding="utf-8", newline="")
                return command
            return program
        path = folder / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
        return path


class HostCli(ProgramCase):
    def setUp(self):
        super().setUp()
        self.script("claude", FAKE_CLAUDE)
        self.provider = {"kind": "host_cli", "model": "haiku", "max_budget_usd": 0.02}

    def call(self, mode, deadline=20.0, answer="supports", questionnaire=None, path=None):
        environment = {"PATH": str(path or self.bin), "JEV_TEST_CLAUDE_MODE": mode,
                       "JEV_TEST_CLAUDE_LOG": str(self.log), "JEV_TEST_CLAUDE_ANSWER": answer}
        with mock.patch.dict(os.environ, environment):
            return run(self.provider, [questionnaire or claim()], deadline=deadline, key=KEY)[0]

    def test_argv_stdin_cwd_and_environment(self):
        before = jev_temp_dirs()
        result = self.call("success")
        self.assertTrue(result["ok"], result)
        record = json.loads(self.log.read_text(encoding="utf-8"))
        argv = record["argv"]
        self.assertEqual(os.path.basename(argv[0]), "claude")
        self.assertEqual(argv, [argv[0], "-p", jev_client.HOST_CLI_INSTRUCTION,
                                "--model", "haiku", "--output-format", "json",
                                "--json-schema", argv[8], "--tools", "",
                                "--no-session-persistence", "--strict-mcp-config",
                                "--setting-sources", "project", "--max-budget-usd", "0.02"])
        schema = json.loads(argv[8])
        self.assertEqual(schema["properties"]["answer"]["enum"],
                         ["supports", "contradicts", "silent"])
        self.assertIs(schema["additionalProperties"], False)
        for banned in ("--add-dir", "acceptEdits", "--permission-mode", "--disallowedTools",
                       "--dangerously-skip-permissions"):
            self.assertNotIn(banned, argv)
        self.assertNotIn(VAULT_MARKER, json.dumps(argv))  # vault text never in argv
        self.assertIn(VAULT_MARKER, record["stdin"])      # it travels on stdin only
        self.assertEqual(json.loads(record["stdin"]), claim())
        cwd = os.path.realpath(record["cwd"])
        for root in (os.path.realpath(self.vault), os.path.realpath(REPO)):
            self.assertFalse(cwd == root or cwd.startswith(root + os.sep), cwd)
        self.assertFalse(os.path.exists(record["cwd"]))  # the private directory is removed
        self.assertEqual(jev_temp_dirs(), before)
        self.assertEqual(record["child"], "1")
        self.assertFalse(record["key_in_env"])  # the key is never handed to the child

    def test_structured_output_becomes_a_label_only_answer_with_usage(self):
        result = self.call("success", answer="contradicts")
        answer = result["answer"]
        self.assertEqual((answer["label"], answer["probabilities"], answer["confidence"]),
                         ("contradicts", None, None))
        self.assertEqual(answer["provenance"]["provider_kind"], "host_cli")
        self.assertEqual(result["usage"]["input_tokens"], 1200)
        self.assertEqual(result["usage"]["output_tokens"], 40)
        self.assertAlmostEqual(result["usage"]["cost_usd"], 0.0161)
        self.assertEqual(result["model_reported"], "claude-haiku-fictional")
        self.assertEqual(result["requests"], 1)
        yes = self.call("success", answer="yes", questionnaire=relevance())
        self.assertEqual((yes["answer"]["label"], yes["answer"]["p_yes"]), ("yes", None))

    def test_refusals_and_failures_degrade_with_fixed_codes(self):
        cases = {"is_error": "cli_error", "retries": "cli_not_success",
                 "missing": "structured_output_missing", "extra": "output_invalid",
                 "garbage": "output_invalid", "crash": "program_failed"}
        for mode, code in cases.items():
            result = self.call(mode)
            self.assertEqual((result["ok"], result["code"]), (False, code), mode)
            self.assertIsNone(result["answer"])
        unknown = self.call("success", answer="maybe")
        self.assertEqual(unknown["code"], "label_unknown")

    def test_deadline_kills_the_process_group(self):
        started = time.monotonic()
        result = self.call("slow", deadline=2.0)
        self.assertLess(time.monotonic() - started, 4.0)
        self.assertEqual((result["code"], result["requests"]), ("deadline_exceeded", 1))
        record = json.loads(self.log.read_text(encoding="utf-8"))
        self.assertTrue(gone(record["pid"]), "the CLI outlived its deadline")
        self.assertTrue(gone(record["grandchild"]), "its process group outlived the deadline")

    def test_missing_cli(self):
        empty = self.root / "empty-bin"
        empty.mkdir()
        result = self.call("success", path=empty)
        self.assertEqual((result["code"], result["requests"]), ("program_missing", 0))

    def test_budget_and_model_are_required_and_bounded(self):
        for provider in ({"kind": "host_cli"}, {"kind": "host_cli", "model": "-p"},
                         {"kind": "host_cli", "model": "haiku", "max_budget_usd": 5},
                         {"kind": "host_cli", "model": "haiku", "max_budget_usd": 0.00001},
                         {"kind": "host_cli", "model": "haiku", "max_budget_usd": True},
                         {"kind": "host_cli", "model": "haiku", "add_dir": "/"}):
            result = run(provider, [claim()])[0]
            self.assertEqual((result["code"], result["requests"]), ("provider_invalid", 0))


class CmdAndFake(ProgramCase):
    def test_cmd_program_reads_the_questionnaire_file(self):
        program = self.script("judge.py", CMD_PROGRAM)
        provider = {"kind": "cmd", "argv": [sys.executable, str(program), "{questionnaire_file}"]}
        result = run(provider, [claim()])[0]
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["answer"]["label"], "supports")
        self.assertAlmostEqual(result["answer"]["confidence"], (3 * 0.8 - 1) / 2)
        self.assertEqual(result["answer"]["provenance"]["provider_kind"], "cmd")

    def test_an_abandoned_call_leaves_no_private_directory_behind(self):
        program = self.script("slow-judge.py", "import time\ntime.sleep(60)\n")
        provider = {"kind": "cmd", "argv": [sys.executable, str(program), "{questionnaire_file}"]}
        before = jev_temp_dirs()
        started = time.monotonic()
        result = run(provider, [claim()], deadline=1.0)[0]
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(result["code"], "deadline_exceeded")
        # Checked at once: the questionnaire file (vault text) must not outlive the call.
        self.assertEqual(jev_temp_dirs(), before)

    def test_cmd_template_and_failures(self):
        no_placeholder = {"kind": "cmd", "argv": [sys.executable, "-c", "print(1)"]}
        self.assertEqual(run(no_placeholder, [claim()])[0]["code"], "provider_invalid")
        missing = {"kind": "cmd", "argv": [str(self.root / "no-such-judge"),
                                           "{questionnaire_file}"]}
        self.assertEqual(run(missing, [claim()])[0]["code"], "program_missing")
        failing = {"kind": "cmd", "argv": [sys.executable, "-c", "import sys; sys.exit(4)",
                                           "{questionnaire_file}"]}
        self.assertEqual(run(failing, [claim()])[0]["code"], "program_failed")

    def fake(self, mode="probability", provider=None, questionnaires=None, deadline=10.0,
             marker=None, capture=None):
        script = self.script("fake-judge", FAKE_ANSWER)
        environment = {jev_client.FAKE_ENV: str(script), "JEV_TEST_FAKE_MODE": mode,
                       "JEV_TEST_FAKE_MARKER": str(marker or "")}
        with mock.patch.dict(os.environ, environment):
            return run(provider or {"kind": "fake"}, questionnaires or [relevance(), claim()],
                       deadline=deadline, parallel=2, capture=capture)

    def test_fake_answers_like_a_provider(self):
        noul, choice = self.fake()
        self.assertTrue(noul["ok"] and choice["ok"], (noul, choice))
        self.assertEqual((noul["answer"]["label"], noul["answer"]["p_yes"]), ("yes", 0.9))
        self.assertEqual(noul["model_reported"], "fake-judge-1")
        self.assertEqual(noul["usage"]["input_tokens"], 30)
        self.assertEqual(choice["answer"]["label"], "contradicts")
        label_only = self.fake("label", provider={"kind": "fake", "label_only": True})
        self.assertEqual([r["answer"]["label"] for r in label_only], ["yes", "silent"])
        strict = self.fake(provider={"kind": "fake", "label_only": True})
        self.assertEqual({r["code"] for r in strict}, {"probabilities_unexpected"})

    def test_fake_is_honoured_only_for_the_fake_kind(self):
        marker = self.root / "fake-ran"
        results = self.fake(provider=systemone(f"http://127.0.0.1:{free_port()}"), marker=marker)
        self.assertEqual({r["code"] for r in results}, {"provider_unreachable"})
        self.assertFalse(marker.exists())
        with mock.patch.dict(os.environ, {jev_client.FAKE_ENV: ""}):
            result = run({"kind": "fake"}, [relevance()])[0]
        self.assertEqual((result["code"], result["requests"]), ("fake_not_configured", 0))

    def test_fake_failures(self):
        self.assertEqual({r["code"] for r in self.fake("garbage")}, {"output_invalid"})
        self.assertEqual({r["code"] for r in self.fake("exit")}, {"program_failed"})
        started = time.monotonic()
        slow = self.fake("slow", deadline=1.0)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual({r["code"] for r in slow}, {"deadline_exceeded"})


# ---------------------------------------------------------------------------
# recorded
# ---------------------------------------------------------------------------

class NoNetworkOrProcess:
    """Refuse every socket connection and child process for the duration."""

    def __init__(self):
        self.attempts = []

    def refuse(self, *args, **kwargs):
        self.attempts.append(args[:1])
        raise NetworkAttempted("network or process attempted")

    def __enter__(self):
        self.patches = [mock.patch.object(socket.socket, "connect", self.refuse),
                        mock.patch.object(socket, "create_connection", self.refuse),
                        mock.patch.object(subprocess, "Popen", self.refuse)]
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self.patches):
            patch.stop()
        return False


class Recorded(ProgramCase):
    def setUp(self):
        super().setUp()
        self.replays = systemone("https://api.example.test")
        self.recording = self.root / "dev-recording.jsonl"

    def write(self, rows):
        self.recording.write_text("".join(json.dumps(row) + "\n" for row in rows),
                                  encoding="utf-8")
        return {"kind": "recorded", "recording": str(self.recording), "replays": self.replays}

    def test_hit_and_miss_never_open_a_socket(self):
        hit = relevance()
        provider = self.write([jev_client.recording_row(
            hit, self.replays, raw={"q": {"type": "noul", "noul": 0.74}},
            usage={"input_tokens": 390, "output_tokens": 0}, model_reported="jev-1.13.0")])
        with NoNetworkOrProcess() as guard:
            results = run(provider, [hit, relevance("A different excerpt.")], parallel=2)
        self.assertEqual(guard.attempts, [])
        found, missed = results
        self.assertTrue(found["ok"], found)
        self.assertEqual((found["answer"]["p_yes"], found["requests"]), (0.74, 0))
        self.assertEqual(found["model_reported"], "jev-1.13.0")
        self.assertEqual(found["usage"]["input_tokens"], 390)
        self.assertEqual((missed["code"], missed["requests"]), ("recording_miss", 0))

    def test_recordings_are_bound_to_provider_identity_and_template(self):
        questionnaire = relevance()
        provider = self.write([jev_client.recording_row(
            questionnaire, self.replays, raw={"q": {"type": "noul", "noul": 0.74}})])
        other_model = dict(provider, replays=dict(self.replays, model="jev-2.0.0"))
        other_endpoint = dict(provider, replays=systemone("https://other.example.test"))
        with NoNetworkOrProcess():
            for variant in (other_model, other_endpoint):
                self.assertEqual(run(variant, [questionnaire])[0]["code"], "recording_miss")
        self.assertNotEqual(jev_client.recording_key(questionnaire, self.replays),
                            jev_client.recording_key(relevance("Other."), self.replays))

    def test_rows_keep_only_checked_fields(self):
        text = f"{VAULT_MARKER} free text a provider slipped in"
        raw = {"q": {"type": "choice", "choice": "supports", "label": text, "note": text,
                     "confidence": text,
                     "probabilities": {"supports": 0.9, "contradicts": 0.05, "silent": 0.05}}}
        row = jev_client.recording_row(claim(), self.replays, raw=raw)
        self.assertNotIn(VAULT_MARKER, json.dumps(row))
        self.assertEqual(row["raw"], {"q": {"type": "choice", "choice": "supports",
                                            "probabilities": {"supports": 0.9,
                                                              "contradicts": 0.05,
                                                              "silent": 0.05}}})
        self.assertEqual(set(row), {"contract", "key", "template", "provider", "raw", "code",
                                    "usage", "model_reported"})
        with self.assertRaises(ValueError):
            jev_client.recording_row(claim(), self.replays)
        with self.assertRaises(ValueError):
            jev_client.recording_row(claim(), self.replays, code="made_up_code")

    def test_recorded_failures_replay_as_their_code(self):
        questionnaire = relevance()
        provider = self.write([jev_client.recording_row(questionnaire, self.replays,
                                                        code="http_rate_limited")])
        with NoNetworkOrProcess():
            result = run(provider, [questionnaire])[0]
        self.assertEqual((result["code"], result["requests"]), ("http_rate_limited", 0))

    def test_replayed_answers_are_validated_again(self):
        questionnaire = relevance()
        row = jev_client.recording_row(questionnaire, self.replays,
                                       raw={"q": {"type": "noul", "noul": 0.74}})
        row["raw"] = {"q": {"type": "noul", "noul": 7.4}}  # edited by hand
        provider = self.write([row])
        with NoNetworkOrProcess():
            self.assertEqual(run(provider, [questionnaire])[0]["code"], "value_out_of_range")

    def test_malformed_or_symlinked_recordings_are_refused(self):
        provider = self.write([])
        self.recording.write_text("{not json\n", encoding="utf-8")
        with NoNetworkOrProcess():
            self.assertEqual(run(provider, [relevance()])[0]["code"], "recording_invalid")
        real = self.root / "real.jsonl"
        real.write_text("", encoding="utf-8")
        self.recording.unlink()
        self.recording.symlink_to(real)
        with NoNetworkOrProcess():
            self.assertEqual(run(provider, [relevance()])[0]["code"], "recording_invalid")
        relative = dict(provider, recording="dev-recording.jsonl")
        self.assertEqual(run(relative, [relevance()])[0]["code"], "provider_invalid")
        nested = dict(provider, replays=dict(provider))
        self.assertEqual(run(nested, [relevance()])[0]["code"], "provider_invalid")

    def test_capture_then_replay_round_trip_holds_no_text(self):
        script = self.script("fake-judge", FAKE_ANSWER)
        questionnaires = [relevance(f"{VAULT_MARKER} lamp log."), claim()]
        capture = []
        live_provider = {"kind": "fake", "model": "fake-judge-1"}
        with mock.patch.dict(os.environ, {jev_client.FAKE_ENV: str(script)}):
            live = run(live_provider, questionnaires, capture=capture)
        self.assertEqual(len(capture), 2)
        stored = json.dumps(capture)
        self.assertNotIn(VAULT_MARKER, stored)
        self.assertNotIn("lamp log", stored)
        self.recording.write_text("".join(json.dumps(row) + "\n" for row in capture),
                                  encoding="utf-8")
        provider = {"kind": "recorded", "recording": str(self.recording),
                    "replays": live_provider}
        with NoNetworkOrProcess():
            replay = run(provider, questionnaires)
        self.assertEqual([r["answer"] for r in replay], [r["answer"] for r in live])
        self.assertEqual([r["requests"] for r in replay], [0, 0])


# ---------------------------------------------------------------------------
# The evaluate() contract
# ---------------------------------------------------------------------------

class EvaluateContract(ServerCase):
    def test_provider_kinds(self):
        self.assertEqual(jev_client.PROVIDER_KINDS,
                         ("systemone", "openai_compat", "host_cli", "cmd", "recorded", "fake"))

    def test_one_result_per_questionnaire_in_order(self):
        self.assertEqual(run(systemone("https://api.example.test"), []), [])
        server = self.serve(reply_json(SYSTEMONE_REPLY))
        results = run(systemone(server.url), [relevance(), claim()], parallel=2)
        self.assertEqual([set(result) for result in results], [RESULT_KEYS, RESULT_KEYS])
        self.assertTrue(results[0]["ok"])
        self.assertEqual(results[1]["code"], "answer_type_mismatch")  # a noul reply to a choice

    def test_caller_mistakes_raise(self):
        with self.assertRaises(TypeError):
            run("systemone", [relevance()])
        with self.assertRaises(TypeError):
            run({"kind": "fake"}, relevance())
        with self.assertRaises(contracts.ContractError):
            run({"kind": "fake"}, [{"contract": "jev-questionnaire/v1"}])
        for deadline in (0, -1, float("nan"), float("inf"), 601, True):
            with self.assertRaises(ValueError):
                run({"kind": "fake"}, [relevance()], deadline=deadline)
        for parallel in (0, -2, 1.5, True):
            with self.assertRaises(ValueError):
                run({"kind": "fake"}, [relevance()], parallel=parallel)
        with self.assertRaises(TypeError):
            run({"kind": "fake"}, [relevance()], key=7)

    def test_unusable_providers_fail_to_local_without_raising(self):
        for provider in ({"kind": "unknown"}, {}, {"kind": "systemone"},
                         systemone("https://api.example.test", extra="x"),
                         systemone("https://api.example.test", model="bad model"),
                         systemone("https://api.example.test", profile="other"),
                         {"kind": "fake", "label_only": "yes"}):
            results = run(provider, [relevance(), claim()])
            self.assertEqual([r["code"] for r in results], ["provider_invalid"] * 2, provider)
            self.assertEqual([r["requests"] for r in results], [0, 0])

    def test_codes_are_fixed_identifiers(self):
        for code in jev_client.CODES:
            self.assertRegex(code, r"^[a-z][a-z0-9_]{1,63}$")
        self.assertIn("provider_kind_unavailable", jev_client.CODES)
        self.assertTrue(contracts.ANSWER_CODES <= jev_client.CODES)

    def test_profiles_and_identities(self):
        self.assertEqual(jev_client.profile_for(systemone("https://api.example.test"))
                         ["probabilities"], "required")
        self.assertEqual(jev_client.profile_for(systemone("https://api.example.test",
                                                          rounding="2dp"))["rounding"], "2dp")
        self.assertEqual(jev_client.profile_for({"kind": "host_cli", "model": "haiku"})
                         ["probabilities"], "none")
        self.assertEqual(jev_client.profile_for({"kind": "fake"})["probabilities"], "optional")
        recorded = {"kind": "recorded", "recording": "/fictional/rec.jsonl",
                    "replays": systemone("https://api.example.test/")}
        self.assertEqual(jev_client.provider_identity(recorded),
                         jev_client.provider_identity(systemone("https://api.example.test")))
        self.assertEqual(jev_client.provider_identity(recorded)["endpoint"],
                         "https://api.example.test")
        with self.assertRaises(jev_client.ProviderInvalid):
            jev_client.provider_identity({"kind": "cmd"})
        first = jev_client.provider_identity({"kind": "cmd", "argv": ["judge-a",
                                                                      "{questionnaire_file}"]})
        second = jev_client.provider_identity({"kind": "cmd", "argv": ["judge-b",
                                                                       "{questionnaire_file}"]})
        self.assertNotEqual(first, second)
        self.assertNotIn("judge-a", json.dumps(first))

    def test_key_never_appears_in_results_captures_or_home(self):
        ok = self.serve(reply_json(SYSTEMONE_REPLY))
        denied = self.serve(reply_status(401))
        capture = []
        results = []
        for server in (ok, denied):
            results += run(systemone(server.url, key_env="JEV_TEST_KEY"), [relevance()],
                           key=KEY, capture=capture)
        self.assertEqual([r["code"] for r in results], [None, "http_unauthorized"])
        self.assertNotIn(KEY, json.dumps(results))
        self.assertNotIn(KEY, json.dumps(capture))
        for path in Path(os.environ["HOME"]).rglob("*"):
            if path.is_file():
                self.assertNotIn(KEY, path.read_text(encoding="utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# The network-surface guard
# ---------------------------------------------------------------------------

class NetworkSurfaceGuard(unittest.TestCase):
    SCRIPT = REPO / "scripts" / "check_network_surface.py"

    def guard(self, root):
        return subprocess.run([sys.executable, str(self.SCRIPT), "--root", str(root)],
                              capture_output=True, text=True, timeout=60)

    def test_this_repository_passes(self):
        result = self.guard(REPO)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("network surface: ok", result.stdout)

    def test_violations_are_reported_with_file_and_line(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for folder in ("context_layer", "eval", "router"):
                (root / folder).mkdir()
            files = {
                "context_layer/graph.py": "from urllib.parse import quote\nimport socket\n",
                "eval/fetch.py": "from urllib import request\n",
                "router/probe.py": "import urllib\nurllib.request.urlopen('x')\n",
                "context_layer/dynamic.py": "import importlib\nimportlib.import_module('ssl')\n",
                "context_layer/jev.py": "import subprocess\n",
                "context_layer/cli.py": ("import subprocess\nFLAG = '--jev'\n"
                                         "subprocess.run(['claude', '-p', 'x'])\n"),
                "context_layer/hook.py": ("from . import backends\nJEV = True\n"
                                          "backends.plan('claude')\n"),
                "context_layer/other.py": "import subprocess\nsubprocess.run(['claude'])\n",
                "context_layer/tasks.py": ("import subprocess\nNOTE = 'jev'\n"
                                           "subprocess.run(['claude'])\n"),
                "context_layer/jev_client.py": "import socket, ssl, subprocess, asyncio\n"
                                               "import urllib.request, http.client\n",
            }
            for relative, text in files.items():
                (root / relative).write_text(text, encoding="utf-8")
            result = self.guard(root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        lines = set(result.stdout.splitlines())
        self.assertEqual(lines, {
            "context_layer/graph.py:2: network-import: imports socket",
            "eval/fetch.py:1: network-import: imports urllib.request",
            "router/probe.py:2: network-import: uses urllib.request",
            "context_layer/dynamic.py:2: network-import: imports ssl dynamically",
            "context_layer/jev.py:1: jev-module-spawn: imports subprocess",
            "context_layer/cli.py:3: jev-cli-spawn: starts a model CLI (subprocess.run)",
            "context_layer/hook.py:3: jev-cli-spawn: uses backends.plan (task backend argv)",
        })


if __name__ == "__main__":
    unittest.main()
