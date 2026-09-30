"""Anonymous, bounded transport for pinned public GitHub repository files.

This is the only GitHub network surface. It never follows redirects, uses
environment proxies, reads credentials, or exposes remote error text.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = "https://api.github.com/repos/"
API_VERSION = "2022-11-28"
MAX_FILE_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
_COMMIT = re.compile(r"[0-9a-fA-F]{40}\Z")
_PATH = re.compile(r"[A-Za-z0-9._~!$&'()+,;=@-]+(?:/[A-Za-z0-9._~!$&'()+,;=@-]+)*\Z")


class GitHubFetchError(Exception):
    """A transport failure represented by a fixed, safe code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _https_context() -> ssl.SSLContext:
    """Use normal verified TLS. Some Python.org macOS installs omit their CA
    bundle; use the OS bundle only when no default roots or explicit override
    exist. Never disable certificate or hostname verification."""
    context = ssl.create_default_context()
    if not context.get_ca_certs() and not any(os.environ.get(key)
                                             for key in ("SSL_CERT_FILE", "SSL_CERT_DIR")):
        for candidate in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
            if Path(candidate).is_file():
                context.load_verify_locations(cafile=candidate)
                break
    return context


def _validate(repo: str, commit: str, path: str) -> None:
    if (not isinstance(repo, str) or not _REPO.fullmatch(repo)
            or any(part in (".", "..") for part in repo.split("/"))):
        raise GitHubFetchError("invalid_repo")
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise GitHubFetchError("invalid_commit")
    if (not isinstance(path, str) or not _PATH.fullmatch(path)
            or any(part in ("", ".", "..") for part in path.split("/"))):
        raise GitHubFetchError("invalid_path")


def fetch_file(repo: str, commit: str, path: str, timeout: float = 5.0) -> bytes:
    """Fetch and verify one file from a public repository at an immutable commit."""
    _validate(repo, commit, path)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise GitHubFetchError("invalid_timeout")
    qrepo = "/".join(urllib.parse.quote(segment, safe="") for segment in repo.split("/"))
    qpath = "/".join(urllib.parse.quote(segment, safe="") for segment in path.split("/"))
    url = f"{API_ROOT}{qrepo}/contents/{qpath}?ref={urllib.parse.quote(commit, safe='')}"
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "context-layer-github-context/1",
    }, method="GET")
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                             urllib.request.HTTPSHandler(context=_https_context()))
        with opener.open(request, timeout=min(float(timeout), 5.0)) as response:
            if response.status != 200:
                raise GitHubFetchError("http_status")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise GitHubFetchError("response_too_large")
    except GitHubFetchError:
        raise
    except urllib.error.HTTPError as exc:
        # Do not retain or relay the server's body, headers, URL, or exception text.
        exc.close()
        raise GitHubFetchError("redirect_denied" if exc.code in (301, 302, 303, 307, 308)
                               else "http_status") from None
    except ssl.SSLCertVerificationError:
        raise GitHubFetchError("tls_verification_failed") from None
    except urllib.error.URLError as exc:
        code = "tls_verification_failed" if isinstance(exc.reason, ssl.SSLCertVerificationError) \
            else "network_error"
        raise GitHubFetchError(code) from None
    except (TimeoutError, OSError, http.client.HTTPException):
        raise GitHubFetchError("network_error") from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise GitHubFetchError("invalid_response") from None
    if not isinstance(payload, dict) or payload.get("type") != "file" \
            or payload.get("encoding") != "base64" or payload.get("path") != path:
        raise GitHubFetchError("invalid_response")
    size = payload.get("size")
    content = payload.get("content")
    blob_sha = payload.get("sha")
    if (isinstance(size, bool) or not isinstance(size, int) or size < 0
            or size > MAX_FILE_BYTES or not isinstance(content, str)
            or not isinstance(blob_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", blob_sha)):
        raise GitHubFetchError("invalid_response")
    # GitHub may line-wrap base64 with ASCII newlines. Other whitespace is invalid.
    encoded = content.replace("\n", "").replace("\r", "")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise GitHubFetchError("invalid_base64") from None
    if len(data) != size:
        raise GitHubFetchError("size_mismatch")
    git_blob = hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()
    if git_blob.lower() != blob_sha.lower():
        raise GitHubFetchError("blob_hash_mismatch")
    try:
        decoded = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise GitHubFetchError("invalid_utf8") from None
    if "\0" in decoded:
        raise GitHubFetchError("binary_content")
    return data
