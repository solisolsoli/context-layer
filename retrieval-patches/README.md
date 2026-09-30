# Optional source-retrieval patches and lab candidates

These patches modify [avenoxai/avenoxbeyin](https://github.com/avenoxai/avenoxbeyin),
MIT copyright Avenox. The exact upstream notice is in [LICENSE.upstream.txt](LICENSE.upstream.txt).
MIT is a licence, not an absence of copyright: the diff hunks carry verbatim
upstream context lines, so that notice travels with them. The full component
inventory is in [THIRD_PARTY.md](../THIRD_PARTY.md).
They are optional reference patches; installing context-layer applies none of them.

## Pinned source

- Tag: `v3.0.1`
- Commit: `61a88467d748fbf94ebc340cb932da7c7e9a78b7`
- Engine: `template/.claude/scripts/beyin_v3.py`, class `MemoryStore`, method `_retrieve`.
- File hashes and the checked rollback: [compatibility.json](compatibility.json).

The similarly named `scripts/beyin_v3.py` is a CLI loader and is not the target.
Compatibility with other upstream revisions has not been established.

## Apply to an isolated copy

Copy that exact engine to a disposable directory as `beyin_v3.py`; retain the
upstream license. From that directory, with patch paths made absolute:

```sh
patch --dry-run -p1 < /path/to/context-layer/retrieval-patches/01-ranking.patch
patch -p1 < /path/to/context-layer/retrieval-patches/01-ranking.patch
patch -p1 < /path/to/context-layer/retrieval-patches/02-cache.patch
patch -p1 < /path/to/context-layer/retrieval-patches/03-strict-source-hash.patch
python3 -m py_compile beyin_v3.py
```

01 filters AppleDouble records, prefers primary locations and suppresses some
mirror duplicates. 02 introduces vocabulary and source-hash caching. **Do not
stop after 02**: its size/mtime-based source-hash reuse can return stale content
when both attributes are preserved. 03 recomputes SHA-256 from current source
bytes on every verification while retaining the vocabulary cache. The remaining
source-cache metadata is maintained for patch compatibility and is not reused
as verification evidence.

The isolated regression warms the cache, replaces `AAAA` with `BBBB`, preserves
size and mtime, then queries without syncing. 01+02 returns the stale record;
01+02+03 excludes it with stale_count 1. This test checks that failure mode only.
It does not measure semantic retrieval quality, thread safety, arbitrary concurrent
file replacement, or speed/token savings. Keep candidates isolated until your
own independent adoption gate passes. No host hooks are installed here.

## Roll back

```sh
patch -R -p1 < /path/to/context-layer/retrieval-patches/03-strict-source-hash.patch
patch -R -p1 < /path/to/context-layer/retrieval-patches/02-cache.patch
patch -R -p1 < /path/to/context-layer/retrieval-patches/01-ranking.patch
```

The final engine must match the original SHA-256 in compatibility.json. Test the
complete apply/reverse sequence on a copy before changing an actual installation.
Generic host integration considerations: [codex-chain.md](codex-chain.md).

## The hook example

[`hook-visible-error.example.sh`](hook-visible-error.example.sh) is the other
lab candidate kept here. It is a wrapper shape, written for this project under
the same MIT licence, for the failure where a host runs a context helper,
discards its exit code and injects whatever reached stdout — so a crashed helper
is indistinguishable from "there was nothing to add". The wrapper reports the
exit code and the shape of the response instead, and treats a well-formed empty
`additionalContext` as a legitimate silence.

It takes the helper from `CONTEXT_HELPER` (or the first argument) and the
interpreter from `CONTEXT_PYTHON`. It contains no personal path and no private
hook name. **Installing this package neither registers nor runs it**, and no
live host has been changed here. The five cases it must satisfy are tabulated
and tested in
[docs/source-lifecycle.md](../docs/source-lifecycle.md#lab-candidate-2-retrieval-patcheshook-visible-errorexamplesh)
(`tests/test_health.py`). Copy it into a wrapper you own, after your own review.

Note that `context-layer hook claude-code` is a different thing: a supported
command of this package, documented in
[docs/host-integration.md](../docs/host-integration.md). The file here is a
generic shell example for hosts this package does not implement.
