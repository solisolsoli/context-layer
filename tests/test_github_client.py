"""Offline tests for the isolated anonymous GitHub transport."""
import base64
import hashlib
import json
import sys
import unittest
from unittest import mock
import urllib.error

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from context_layer import github_client  # noqa: E402

COMMIT = "a" * 40
BODY = b"# Project\nA fictional keeper checks every lamp at dusk.\n"


def blob_sha(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


class Response:
    status = 200

    def __init__(self, body):
        self.body = body

    def read(self, amount=-1):
        return self.body[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Opener:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.request, self.timeout = response, error, None, None

    def open(self, request, timeout):
        self.request, self.timeout = request, timeout
        if self.error:
            raise self.error
        return self.response


def payload(data=BODY, **changes):
    row = {"type": "file", "encoding": "base64", "path": "README.md",
           "size": len(data), "sha": blob_sha(data),
           "content": base64.b64encode(data).decode("ascii")}
    row.update(changes)
    return json.dumps(row).encode()


class GitHubClientTests(unittest.TestCase):
    def opener_for(self, raw):
        opener = Opener(Response(raw))
        self.build_opener_patch = mock.patch.object(
            github_client.urllib.request, "build_opener", return_value=opener)
        self.build_opener_mock = self.build_opener_patch.start()
        self.addCleanup(self.build_opener_patch.stop)
        return opener

    def fetch_with(self, raw):
        opener = self.opener_for(raw)
        return github_client.fetch_file("example/project", COMMIT, "README.md"), opener

    def test_valid_payload_verifies_blob_and_uses_fixed_anonymous_api_request(self):
        result, opener = self.fetch_with(payload())
        self.assertEqual(result, BODY)
        self.assertEqual(opener.request.full_url,
                         f"https://api.github.com/repos/example/project/contents/README.md?ref={COMMIT}")
        self.assertEqual(opener.request.get_method(), "GET")
        self.assertEqual(opener.request.get_header("X-github-api-version"), github_client.API_VERSION)
        self.assertNotIn("authorization", {k.lower() for k, _ in opener.request.header_items()})
        self.assertLessEqual(opener.timeout, 5)
        args = self.build_opener_mock.call_args.args
        self.assertTrue(any(isinstance(arg, github_client.urllib.request.ProxyHandler)
                            and arg.proxies == {} for arg in args))
        self.assertTrue(any(isinstance(arg, github_client._NoRedirect) for arg in args))

    def test_base64_newlines_are_accepted_but_corrupt_data_and_hash_are_refused(self):
        wrapped = base64.b64encode(BODY).decode()
        self.assertEqual(self.fetch_with(payload(content=wrapped[:12] + "\n" + wrapped[12:]))[0], BODY)
        for raw, code in ((payload(content="not base64!"), "invalid_base64"),
                          (payload(sha="0" * 40), "blob_hash_mismatch"),
                          (payload(path="elsewhere.md"), "invalid_response"),
                          (payload(size=len(BODY) + 1), "size_mismatch")):
            with self.subTest(code=code), self.assertRaises(github_client.GitHubFetchError) as ctx:
                self.fetch_with(raw)
            self.assertEqual(ctx.exception.code, code)

    def test_invalid_coordinates_are_refused_before_network(self):
        with mock.patch.object(github_client.urllib.request, "build_opener",
                               side_effect=AssertionError("network setup attempted")):
            cases = [("owner/repo?x", COMMIT, "README.md", "invalid_repo"),
                     ("../repo", COMMIT, "README.md", "invalid_repo"),
                     ("owner/repo", "../" + COMMIT, "README.md", "invalid_commit"),
                     ("owner/repo", COMMIT, "../secret", "invalid_path"),
                     ("owner/repo", COMMIT, "%2e%2e/secret", "invalid_path")]
            for repo, commit, path, code in cases:
                with self.subTest(code=code), self.assertRaises(github_client.GitHubFetchError) as ctx:
                    github_client.fetch_file(repo, commit, path)
                self.assertEqual(ctx.exception.code, code)

    def test_timeout_must_be_finite_positive_and_not_boolean(self):
        with mock.patch.object(github_client.urllib.request, "build_opener",
                               side_effect=AssertionError("network setup attempted")):
            for timeout in (True, False, float("nan"), float("inf"), 0, -1):
                with self.subTest(timeout=timeout), self.assertRaises(github_client.GitHubFetchError) as ctx:
                    github_client.fetch_file("example/project", COMMIT, "README.md", timeout=timeout)
                self.assertEqual(ctx.exception.code, "invalid_timeout")

    def test_redirect_and_response_caps_return_safe_codes(self):
        opener = Opener(error=urllib.error.HTTPError("https://api.github.com", 302, "secret", {}, None))
        with mock.patch.object(github_client.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(github_client.GitHubFetchError) as ctx:
                github_client.fetch_file("example/project", COMMIT, "README.md")
        self.assertEqual(ctx.exception.code, "redirect_denied")
        self.assertNotIn("secret", str(ctx.exception))
        with self.assertRaises(github_client.GitHubFetchError) as ctx:
            self.fetch_with(b"x" * (github_client.MAX_RESPONSE_BYTES + 1))
        self.assertEqual(ctx.exception.code, "response_too_large")

    def test_strict_utf8_and_binary_data_are_rejected(self):
        for data, code in ((b"\xff", "invalid_utf8"), (b"a\x00b", "binary_content")):
            with self.subTest(code=code), self.assertRaises(github_client.GitHubFetchError) as ctx:
                self.fetch_with(payload(data))
            self.assertEqual(ctx.exception.code, code)

    def test_parser_and_truncated_http_failures_are_sanitized(self):
        for raw in (b"[" * 2000, b'{"size":' + b"1" * 5000 + b"}", b"not-json"):
            with self.subTest(raw_size=len(raw)), self.assertRaises(github_client.GitHubFetchError) as ctx:
                self.fetch_with(raw)
            self.assertEqual(ctx.exception.code, "invalid_response")
        opener = Opener(error=github_client.http.client.IncompleteRead(b"private-body"))
        with mock.patch.object(github_client.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(github_client.GitHubFetchError) as ctx:
                github_client.fetch_file("example/project", COMMIT, "README.md")
        self.assertEqual(str(ctx.exception), "network_error")

    def test_missing_python_roots_can_use_os_bundle_without_disabling_tls(self):
        context = mock.Mock()
        context.get_ca_certs.return_value = []
        with mock.patch.object(github_client.ssl, "create_default_context", return_value=context), \
                mock.patch.object(github_client.Path, "is_file", return_value=True), \
                mock.patch.dict(github_client.os.environ, {}, clear=True):
            self.assertIs(github_client._https_context(), context)
        context.load_verify_locations.assert_called_once_with(cafile="/etc/ssl/cert.pem")
        context.reset_mock()
        with mock.patch.object(github_client.ssl, "create_default_context", return_value=context), \
                mock.patch.dict(github_client.os.environ, {"SSL_CERT_FILE": "owner-choice.pem"}):
            github_client._https_context()
        context.load_verify_locations.assert_not_called()
        actual = github_client._https_context()
        self.assertEqual(actual.verify_mode, github_client.ssl.CERT_REQUIRED)
        self.assertTrue(actual.check_hostname)

    def test_tls_failure_has_a_specific_safe_code(self):
        opener = Opener(error=urllib.error.URLError(github_client.ssl.SSLCertVerificationError()))
        with mock.patch.object(github_client.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(github_client.GitHubFetchError) as ctx:
                github_client.fetch_file("example/project", COMMIT, "README.md")
        self.assertEqual(str(ctx.exception), "tls_verification_failed")

    def test_branch_and_tag_ref_resolution_uses_fixed_anonymous_commit_endpoint(self):
        opener = self.opener_for(json.dumps({"sha": COMMIT}).encode())
        self.assertEqual(github_client.resolve_ref("example/project", "heads/main"), COMMIT)
        self.assertEqual(opener.request.full_url,
                         "https://api.github.com/repos/example/project/commits/heads/main?per_page=1")
        self.assertEqual(opener.request.get_method(), "GET")
        with mock.patch.object(github_client.urllib.request, "build_opener",
                               side_effect=AssertionError("network setup attempted")):
            for ref in ("../main", "heads/%2e%2e", "main?x=y", "https://example.invalid"):
                with self.subTest(ref=ref), self.assertRaises(github_client.GitHubFetchError) as ctx:
                    github_client.resolve_ref("example/project", ref)
                self.assertEqual(ctx.exception.code, "invalid_ref")


if __name__ == "__main__":
    unittest.main()
