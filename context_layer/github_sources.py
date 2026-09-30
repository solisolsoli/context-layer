"""Safe management for the small, vault-local GitHub source allowlist."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from contextlib import contextmanager

from . import github_client, github_context, platform_support

_ID = github_context._ID
_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
MAX_CHECK_FILES = 4
MAX_CHECK_FILE_BYTES = 128 * 1024
MAX_DIFF_CHARS = 6000


class GitHubSourceError(ValueError):
    """Expected, sanitized source-management failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _result(status: str, **fields) -> dict:
    return {"schema": "github-sources-v1", "status": status, **fields}


def _load(vault: Path):
    try:
        return github_context._load_config(Path(vault))
    except github_context._ConfigError:
        raise GitHubSourceError("invalid_config") from None


def _paths(vault: Path):
    vault = Path(vault)
    context = vault / ".context"
    config = context / "github.json"
    try:
        if platform_support.is_link_or_reparse(vault) or not vault.exists() or not vault.is_dir():
            raise GitHubSourceError("invalid_vault")
        if platform_support.is_link_or_reparse(context) or (context.exists() and not context.is_dir()):
            raise GitHubSourceError("unsafe_config_path")
        if platform_support.is_link_or_reparse(config) or (config.exists() and not config.is_file()):
            raise GitHubSourceError("unsafe_config_path")
    except OSError:
        raise GitHubSourceError("config_read_failed") from None
    return context, config


def _atomic_bytes(path: Path, data: bytes):
    fd, temp_name = tempfile.mkstemp(prefix=".github-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _read_config(path: Path) -> bytes | None:
    if not path.exists():
        return None
    with path.open("rb") as stream:
        raw = stream.read(github_context.MAX_CONFIG_BYTES + 1)
    if len(raw) > github_context.MAX_CONFIG_BYTES:
        raise GitHubSourceError("invalid_config")
    return raw


def _write(vault: Path, config: dict, expected: bytes | None) -> str:
    context, path = _paths(vault)
    context.mkdir(mode=0o700, exist_ok=True)
    lock = context / ".github-config.lock"
    try:
        with _config_lock(lock):
            if platform_support.is_link_or_reparse(path) or (path.exists() and not path.is_file()):
                raise GitHubSourceError("unsafe_config_path")
            current = _read_config(path)
            if current != expected:
                raise GitHubSourceError("concurrent_write")
            payload = json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
            if len(payload) > github_context.MAX_CONFIG_BYTES:
                raise GitHubSourceError("config_too_large")
            backup = context / "github.json.bak"
            if platform_support.is_link_or_reparse(backup) or (backup.exists() and not backup.is_file()):
                raise GitHubSourceError("unsafe_config_path")
            if current is not None:
                _atomic_bytes(backup, current)
            _atomic_bytes(path, payload)
            return hashlib.sha256(payload).hexdigest()
    except platform_support.LockTimeout:
        raise GitHubSourceError("concurrent_write") from None
    except OSError:
        raise GitHubSourceError("write_failed") from None


@contextmanager
def _config_lock(lock: Path):
    if platform_support.is_link_or_reparse(lock) or (lock.exists() and not lock.is_file()):
        raise GitHubSourceError("unsafe_config_path")
    try:
        with platform_support.file_lock(lock, timeout=0):
            yield
    except platform_support.LockTimeout:
        raise


def _save(vault: Path, config: dict, original: bytes | None, apply: bool) -> dict:
    if type(apply) is not bool:
        raise GitHubSourceError("invalid_apply")
    content = json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    if len(content) > github_context.MAX_CONFIG_BYTES:
        raise GitHubSourceError("config_too_large")
    if not apply:
        return _result("DRY_RUN", config=config, backup="would-back-up-existing-config" if original else None)
    digest = _write(vault, config, original)
    return _result("OK", config=config, config_sha256=digest,
                   backup=".context/github.json.bak" if original else None)


def _load_with_bytes(vault: Path):
    try:
        _, path = _paths(vault)
        before = _read_config(path)
        config = _load(vault)
        after = _read_config(path)
    except OSError:
        raise GitHubSourceError("config_read_failed") from None
    if before != after:
        raise GitHubSourceError("concurrent_write")
    return config, before


def list_sources(vault: Path) -> dict:
    try:
        config = _load(vault)
        return _result("OK", enabled=False if config is None else config["enabled"],
                       sources=[] if config is None else config["sources"])
    except GitHubSourceError as exc:
        return _result("ERROR", errors=[exc.code])


def add_source(vault: Path, source_id: str, repo: str, ref: str, paths: list[str],
               keywords: list[str], *, apply: bool = False) -> dict:
    try:
        if type(apply) is not bool:
            raise GitHubSourceError("invalid_apply")
        if not isinstance(source_id, str) or not _ID.fullmatch(source_id):
            raise GitHubSourceError("invalid_source_id")
        github_client.validate_ref(repo, ref)
        # Reuse the config validator to enforce exactly the runtime schema.
        candidate = {"id": source_id, "repo": repo, "commit": "0" * 40,
                     "paths": paths, "keywords": keywords, "ref": ref}
        current, original = _load_with_bytes(vault)
        cfg = current or {"version": 1, "enabled": True, "sources": []}
        if source_id in {s["id"] for s in cfg["sources"]}:
            raise GitHubSourceError("source_exists")
        if len(cfg["sources"]) >= github_context.MAX_SOURCES:
            raise GitHubSourceError("source_limit_reached")
        # Validate untrusted paths/keywords by validating the candidate config.
        github_context._validate_source(candidate)
        sha = github_client.resolve_ref(repo, ref)
        candidate["commit"] = sha
        cfg = {**cfg, "sources": [*cfg["sources"], candidate]}
        result = _save(vault, cfg, original, apply)
        result.update(source_id=source_id, resolved_commit=sha, ref=ref)
        return result
    except github_client.GitHubFetchError as exc:
        return _result("ERROR", errors=[exc.code])
    except github_context._ConfigError:
        return _result("ERROR", errors=["invalid_source"])
    except GitHubSourceError as exc:
        return _result("ERROR", errors=[exc.code])


def _mutate_source(vault: Path, source_id: str, mutate, apply: bool) -> dict:
    try:
        current, original = _load_with_bytes(vault)
        if current is None:
            raise GitHubSourceError("config_not_found")
        matches = [s for s in current["sources"] if s["id"] == source_id]
        if not matches:
            raise GitHubSourceError("source_not_found")
        cfg = {**current, "sources": mutate(current["sources"], matches[0])}
        result = _save(vault, cfg, original, apply)
        result["source_id"] = source_id
        return result
    except GitHubSourceError as exc:
        return _result("ERROR", errors=[exc.code])


def remove_source(vault: Path, source_id: str, *, apply: bool = False) -> dict:
    return _mutate_source(vault, source_id,
                          lambda rows, target: [s for s in rows if s["id"] != target["id"]], apply)


def set_enabled(vault: Path, enabled: bool, *, apply: bool = False) -> dict:
    try:
        if type(enabled) is not bool:
            raise GitHubSourceError("invalid_enabled")
        current, original = _load_with_bytes(vault)
        if current is None:
            raise GitHubSourceError("config_not_found")
        cfg = {**current, "enabled": enabled}
        return _save(vault, cfg, original, apply)
    except GitHubSourceError as exc:
        return _result("ERROR", errors=[exc.code])


def check_source(vault: Path, source_id: str, *, upstream_ref: str | None = None) -> dict:
    """Preview bounded verbatim line diffs between the configured pin and ref head."""
    try:
        config = _load(vault)
        if config is None:
            raise GitHubSourceError("config_not_found")
        source = next((s for s in config["sources"] if s["id"] == source_id), None)
        if source is None:
            raise GitHubSourceError("source_not_found")
        ref = upstream_ref if upstream_ref is not None else source.get("ref")
        if ref is None:
            raise GitHubSourceError("source_ref_missing")
        github_client.validate_ref(source["repo"], ref)
        deadline = time.monotonic() + github_context.GLOBAL_TIMEOUT_S
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _result("ERROR", errors=["deadline_exceeded"])
        latest = github_client.resolve_ref(source["repo"], ref, timeout=min(5.0, remaining))
        requested = source["paths"]
        omissions = []
        if len(requested) > MAX_CHECK_FILES:
            omissions.append("file_limit_reached")
        diffs, total_bytes, total_chars = [], 0, 0
        errors = []
        for path in requested[:MAX_CHECK_FILES]:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    omissions.append("deadline_exceeded")
                    break
                before = github_client.fetch_file(source["repo"], source["commit"], path,
                                                  timeout=min(5.0, remaining))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    omissions.append("deadline_exceeded")
                    break
                after = github_client.fetch_file(source["repo"], latest, path,
                                                 timeout=min(5.0, remaining))
                if total_bytes + len(before) + len(after) > MAX_CHECK_FILE_BYTES:
                    omissions.append("byte_limit_reached")
                    break
                total_bytes += len(before) + len(after)
                old_text = before.decode("utf-8", errors="strict")
                new_text = after.decode("utf-8", errors="strict")
                if "\0" in old_text or "\0" in new_text:
                    errors.append("binary_content")
                    break
                lines = list(difflib.unified_diff(old_text.splitlines(keepends=True),
                                                   new_text.splitlines(keepends=True),
                                                   fromfile=f"{path}@{source['commit'][:12]}",
                                                   tofile=f"{path}@{latest[:12]}", n=2))
                diff = "".join(lines)
                room = MAX_DIFF_CHARS - total_chars
                if diff and room > 0:
                    piece = diff[:room]
                    diffs.append({"path": path, "changed": before != after, "diff": piece})
                    total_chars += len(piece)
                    if len(piece) < len(diff):
                        omissions.append("diff_limit_reached")
                        break
                elif diff:
                    omissions.append("diff_limit_reached")
                    break
                else:
                    diffs.append({"path": path, "changed": False, "diff": ""})
            except github_client.GitHubFetchError as exc:
                errors.append(exc.code)
                break
            except UnicodeDecodeError:
                errors.append("invalid_utf8")
                break
        status = "ERROR" if errors and not diffs else ("PARTIAL" if errors or omissions else "OK")
        return _result(status, source_id=source_id, pinned_commit=source["commit"].lower(),
                       upstream_ref=ref, upstream_commit=latest, current=(latest == source["commit"].lower()),
                       files=diffs, errors=errors, omissions=omissions)
    except github_client.GitHubFetchError as exc:
        return _result("ERROR", errors=[exc.code])
    except GitHubSourceError as exc:
        return _result("ERROR", errors=[exc.code])


def update_source(vault: Path, source_id: str, new_commit: str,
                  expected_commit: str, *, apply: bool = False) -> dict:
    try:
        if not isinstance(new_commit, str) or not _SHA.fullmatch(new_commit):
            raise GitHubSourceError("invalid_commit")
        if not isinstance(expected_commit, str) or not _SHA.fullmatch(expected_commit):
            raise GitHubSourceError("invalid_expected_commit")
        current, original = _load_with_bytes(vault)
        if current is None:
            raise GitHubSourceError("config_not_found")
        source = next((s for s in current["sources"] if s["id"] == source_id), None)
        if source is None:
            raise GitHubSourceError("source_not_found")
        if source["commit"].lower() != expected_commit.lower():
            raise GitHubSourceError("expected_commit_mismatch")
        rows = [{**s, "commit": new_commit.lower()} if s["id"] == source_id else s
                for s in current["sources"]]
        result = _save(vault, {**current, "sources": rows}, original, apply)
        result.update(source_id=source_id, previous_commit=expected_commit.lower(),
                      commit=new_commit.lower())
        return result
    except GitHubSourceError as exc:
        return _result("ERROR", errors=[exc.code])


def set_cache_enabled(vault: Path, enabled: bool, *, apply: bool = False) -> dict:
    from . import github_cache
    return github_cache.set_enabled(vault, enabled, apply=apply)


def cache_status(vault: Path) -> dict:
    from . import github_cache
    return github_cache.status(vault)


def purge_cache(vault: Path, *, apply: bool = False) -> dict:
    from . import github_cache
    return github_cache.purge(vault, apply=apply)
