"""Opt-in, bounded disk cache for immutable public GitHub file bytes.

Cache hashes detect accidental corruption, not a same-user attacker who can
rewrite both a record and its hashes. Cached content remains external evidence.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from contextlib import contextmanager

from . import github_client, platform_support

MAX_CACHE_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 200 * 1024
MAX_CACHE_ENTRIES = 256
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class GitHubCacheError(ValueError):
    """Expected, sanitized cache failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _result(status: str, **fields):
    return {"schema": "github-cache-v1", "status": status, **fields}


def _paths(vault: Path):
    vault = Path(vault)
    context = vault / ".context"
    cfg = context / "github-cache.json"
    directory = context / "github-cache"
    try:
        if platform_support.is_link_or_reparse(vault) or not vault.exists() or not vault.is_dir():
            raise GitHubCacheError("invalid_vault")
        if platform_support.is_link_or_reparse(context) or (context.exists() and not context.is_dir()):
            raise GitHubCacheError("unsafe_cache_path")
        if platform_support.is_link_or_reparse(cfg) or (cfg.exists() and not cfg.is_file()):
            raise GitHubCacheError("unsafe_cache_path")
        if platform_support.is_link_or_reparse(directory) or (directory.exists() and not directory.is_dir()):
            raise GitHubCacheError("unsafe_cache_path")
    except OSError:
        raise GitHubCacheError("cache_read_failed") from None
    return context, cfg, directory


def _load_enabled(cfg: Path) -> bool:
    if not cfg.exists():
        return False
    try:
        with cfg.open("rb") as stream:
            raw = stream.read(1025)
        if len(raw) > 1024:
            raise GitHubCacheError("invalid_cache_config")
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except GitHubCacheError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise GitHubCacheError("invalid_cache_config") from None
    if not isinstance(value, dict) or set(value) != {"version", "enabled"} \
            or type(value.get("version")) is not int or value["version"] != 1 \
            or type(value.get("enabled")) is not bool:
        raise GitHubCacheError("invalid_cache_config")
    return value["enabled"]


def _read_config_bytes(path: Path) -> bytes | None:
    if not path.exists():
        return None
    try:
        with path.open("rb") as stream:
            raw = stream.read(1025)
    except OSError:
        raise GitHubCacheError("cache_config_read_failed") from None
    if len(raw) > 1024:
        raise GitHubCacheError("invalid_cache_config")
    return raw


def _atomic(path: Path, data: bytes):
    fd, name = tempfile.mkstemp(prefix=".github-cache-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


@contextmanager
def _locked(directory: Path):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if platform_support.is_link_or_reparse(directory) or not directory.is_dir():
        raise GitHubCacheError("unsafe_cache_path")
    lock = directory / ".github-cache.lock"
    if platform_support.is_link_or_reparse(lock) or (lock.exists() and not lock.is_file()):
        raise GitHubCacheError("invalid_cache_entry")
    try:
        with platform_support.file_lock(lock, timeout=0):
            yield
    except platform_support.LockTimeout:
        raise GitHubCacheError("concurrent_cache_operation") from None


def set_enabled(vault: Path, enabled: bool, *, apply: bool = False) -> dict:
    try:
        if type(enabled) is not bool or type(apply) is not bool:
            raise GitHubCacheError("invalid_option")
        context, cfg, _ = _paths(vault)
        _load_enabled(cfg)
        previous = _read_config_bytes(cfg)
        doc = {"version": 1, "enabled": enabled}
        raw = json.dumps(doc, sort_keys=True, indent=2).encode() + b"\n"
        if not apply:
            return _result("DRY_RUN", enabled=enabled)
        context.mkdir(mode=0o700, exist_ok=True)
        lock = context / ".github-cache-config.lock"
        if platform_support.is_link_or_reparse(lock) or (lock.exists() and not lock.is_file()):
            raise GitHubCacheError("unsafe_cache_path")
        try:
            with platform_support.file_lock(lock, timeout=0):
                if platform_support.is_link_or_reparse(cfg):
                    raise GitHubCacheError("concurrent_write")
                current = _read_config_bytes(cfg)
                if current != previous:
                    raise GitHubCacheError("concurrent_write")
                backup = context / "github-cache.json.bak"
                if platform_support.is_link_or_reparse(backup) or (backup.exists() and not backup.is_file()):
                    raise GitHubCacheError("unsafe_cache_path")
                if previous is not None:
                    _atomic(backup, previous)
                _atomic(cfg, raw)
        except platform_support.LockTimeout:
            raise GitHubCacheError("concurrent_write") from None
        return _result("OK", enabled=enabled, backup=".context/github-cache.json.bak"
                       if previous else None)
    except GitHubCacheError as exc:
        return _result("ERROR", errors=[exc.code])
    except OSError:
        return _result("ERROR", errors=["write_failed"])


def _key(repo: str, commit: str, path: str) -> str:
    packed = json.dumps([repo, commit.lower(), path], ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(packed).hexdigest()


def _scan(directory: Path):
    if not directory.exists():
        return [], 0
    rows, total = [], 0
    try:
        for item in directory.iterdir():
            if item.name == ".github-cache.lock":
                if platform_support.is_link_or_reparse(item) or not item.is_file():
                    raise GitHubCacheError("invalid_cache_entry")
                continue
            if platform_support.is_link_or_reparse(item) or not item.is_file() or not re.fullmatch(r"[0-9a-f]{64}\.json", item.name):
                raise GitHubCacheError("invalid_cache_entry")
            size = item.stat().st_size
            if size > MAX_RECORD_BYTES:
                raise GitHubCacheError("invalid_cache_entry")
            total += size
            if total > MAX_CACHE_BYTES:
                raise GitHubCacheError("cache_size_limit")
            rows.append(item)
            if len(rows) > MAX_CACHE_ENTRIES:
                raise GitHubCacheError("cache_entry_limit")
    except OSError:
        raise GitHubCacheError("cache_read_failed") from None
    return rows, total


def _record(path: Path, repo: str, commit: str, source_path: str) -> bytes:
    try:
        if platform_support.is_link_or_reparse(path) or not path.is_file():
            raise GitHubCacheError("invalid_cache_entry")
        with path.open("rb") as stream:
            raw = stream.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES:
            raise GitHubCacheError("invalid_cache_entry")
        doc = json.loads(raw.decode("utf-8", errors="strict"))
        exact = {"version", "repo", "commit", "path", "data_b64", "sha256", "git_blob_sha"}
        if not isinstance(doc, dict) or set(doc) != exact or type(doc["version"]) is not int \
                or doc["version"] != 1 or doc["repo"] != repo or doc["commit"] != commit.lower() \
                or doc["path"] != source_path or not isinstance(doc["data_b64"], str):
            raise GitHubCacheError("cache_coordinates_mismatch")
        data = base64.b64decode(doc["data_b64"], validate=True)
        if len(data) > github_client.MAX_FILE_BYTES or hashlib.sha256(data).hexdigest() != doc["sha256"]:
            raise GitHubCacheError("cache_hash_mismatch")
        blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        if blob != doc["git_blob_sha"]:
            raise GitHubCacheError("cache_hash_mismatch")
        text = data.decode("utf-8", errors="strict")
        if "\0" in text:
            raise GitHubCacheError("cache_invalid_content")
        return data
    except GitHubCacheError:
        raise
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, RecursionError):
        raise GitHubCacheError("invalid_cache_entry") from None


def fetch_file(vault: Path, repo: str, commit: str, path: str, *, offline: bool = False,
               force_refresh: bool = False, timeout: float = 5.0):
    """Return verified bytes and explicit disk/network provenance (or None if cache is off)."""
    if type(offline) is not bool or type(force_refresh) is not bool or (offline and force_refresh):
        raise GitHubCacheError("invalid_cache_options")
    context, cfg, directory = _paths(vault)
    enabled = _load_enabled(cfg)
    github_client._validate(repo, commit, path)
    if not enabled:
        if offline:
            raise GitHubCacheError("cache_disabled")
        return github_client.fetch_file(repo, commit, path, timeout=timeout), None
    if offline or not force_refresh:
        if directory.exists():
            with _locked(directory):
                _scan(directory)
                entry = directory / (_key(repo, commit, path) + ".json")
                if entry.exists():
                    data = _record(entry, repo, commit, path)
                    return data, "disk-cache"
        if offline:
            raise GitHubCacheError("cache_miss")
    elif directory.exists():
        # Refresh bypasses byte reuse, but unsafe entries and symlinks are still
        # rejected before opening the network connection.
        with _locked(directory):
            _scan(directory)
    data = github_client.fetch_file(repo, commit, path, timeout=timeout)
    doc = {"version": 1, "repo": repo, "commit": commit.lower(), "path": path,
           "data_b64": base64.b64encode(data).decode("ascii"),
           "sha256": hashlib.sha256(data).hexdigest(),
           "git_blob_sha": hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()}
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > MAX_RECORD_BYTES:
        raise GitHubCacheError("cache_record_too_large")
    with _locked(directory):
        _scan(directory)
        entry = directory / (_key(repo, commit, path) + ".json")
        existing = entry.stat().st_size if entry.exists() and not platform_support.is_link_or_reparse(entry) else 0
        files, total = _scan(directory)
        if entry not in files and len(files) >= MAX_CACHE_ENTRIES:
            raise GitHubCacheError("cache_entry_limit")
        if total - existing + len(raw) > MAX_CACHE_BYTES:
            raise GitHubCacheError("cache_size_limit")
        _atomic(entry, raw)
    return data, "network-refresh" if force_refresh else "network-cached"


def status(vault: Path) -> dict:
    try:
        _, cfg, directory = _paths(vault)
        enabled = _load_enabled(cfg)
        if directory.exists():
            with _locked(directory):
                files, size = _scan(directory)
        else:
            files, size = [], 0
        return _result("OK", enabled=enabled, entries=len(files), bytes=size,
                       max_bytes=MAX_CACHE_BYTES, max_entries=MAX_CACHE_ENTRIES,
                       directory=".context/github-cache")
    except GitHubCacheError as exc:
        return _result("ERROR", errors=[exc.code])
    except OSError:
        return _result("ERROR", errors=["cache_read_failed"])


def purge(vault: Path, *, apply: bool = False) -> dict:
    try:
        if type(apply) is not bool:
            raise GitHubCacheError("invalid_apply")
        _, _, directory = _paths(vault)
        if directory.exists():
            with _locked(directory):
                entries, size = _scan(directory)
                if apply:
                    for entry in entries:
                        if platform_support.is_link_or_reparse(entry) or not entry.is_file():
                            raise GitHubCacheError("invalid_cache_entry")
                    for entry in entries:
                        entry.unlink()
        else:
            entries, size = [], 0
        if not apply:
            return _result("DRY_RUN", entries=len(entries), bytes=size)
        return _result("OK", purged=len(entries), bytes=size)
    except GitHubCacheError as exc:
        return _result("ERROR", errors=[exc.code])
    except OSError:
        return _result("ERROR", errors=["cache_write_failed"])
