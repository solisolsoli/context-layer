# GitHub context for missing knowledge

This optional reader fetches **candidate evidence** from public GitHub files
the vault owner chooses. It is off by default, needs no token or model API,
and never uploads a question or a vault note. It reads documentation, including
prompt files or MCP setup guides; it does not install or execute their contents.

## Configure the sources once

Use the source commands to preview a configuration, then apply it. All examples
below use fictional coordinates: replace them with a public repository and
documentation relevant to your project.

```sh
context-layer github-sources add ~/MyVault --id project-docs \
  --repo example/project --ref main --path README.md --path docs/setup.md \
  --keyword "project setup" --keyword "project protocol"
# Review the result, then apply using its full resolved SHA as --ref.
# Keep the same --id, --repo, --path and --keyword arguments.
context-layer github-sources list ~/MyVault
```

`add` resolves a branch, tag or commit through GitHub's public API and records a
full commit SHA. Even a dry run needs this anonymous request. Only `--apply`
writes the configuration, with an atomic replacement and one previous copy at
`.context/github.json.bak` (replaced on the next applied change).
A new configuration enables its explicitly added source; adding
to an existing disabled configuration leaves it disabled. Concurrent config
changes are refused rather than overwritten. No command saves a credential.
To add exactly the previewed revision, use its full SHA as `--ref` when applying;
pass the branch or tag explicitly to later `check --ref` commands.

You can also create `.context/github.json` manually. Retrieval always uses
`commit`, never a moving branch or tag. The optional `ref` is used only for
explicit version checks. Arbitrary URLs are refused.

```json
{
  "version": 1,
  "enabled": true,
  "sources": [
    {
      "id": "project-docs",
      "repo": "example/project",
      "commit": "1111111111111111111111111111111111111111",
      "ref": "main",
      "paths": ["README.md", "docs/setup.md"],
      "keywords": ["project setup", "project protocol"]
    }
  ]
}
```

Only the owner-configured files can be fetched. Do not add secrets, tokens or
personal information to this file; unknown configuration fields are refused.
Set `enabled` to `false`, or remove the file, to disable fetching. Existing
vaults and host settings are not changed by installing this release.

```sh
context-layer github-sources disable ~/MyVault --apply
context-layer github-sources enable ~/MyVault --apply
context-layer github-sources remove ~/MyVault --id project-docs --apply
```

Omit `--apply` to preview any of these changes. `list` is read-only.
The examples above use a POSIX shell. In PowerShell, use `$env:USERPROFILE`
instead of `~`, quote paths, and keep each command on one line; see the
[PowerShell workflow](#powershell-workflow) below.

## Use it when evidence is missing

```sh
# Local retrieval first. Only a clean, empty NOT_FOUND can trigger GitHub.
context-layer search ~/MyVault --prompt "project setup" --github

# The host can also identify a semantic gap in otherwise nonempty local results.
context-layer github-context ~/MyVault --prompt "project setup" --source project-docs
```

`--no-github` overrides `--github`. Without `--github`, local search remains
unchanged. Index errors, stale or withheld sources never trigger a remote
substitute. Automatic fallback routes by configured keywords matched **locally**;
keyword matching requires the keyword's non-stopword tokens to be present in the prompt;
it is not phrase matching, semantic similarity or a model confidence detector.
An explicit `--source` selects a
configured source and permits a prefix excerpt when no prompt term matches
its text; omit it to use keyword routing. Such a prefix is still only a
candidate and may not answer the question.

The MCP server exposes the same paths:

- `search_vault` with `github: true` adds `external_context` on a clean miss.
- `github_context` takes `prompt` and optional `source_ids`, for example
  `{"prompt":"project setup","source_ids":["project-docs"]}`. Optional booleans
  `offline` and `force_refresh` select the cache behavior below; they cannot
  both be true. Configuration management is a CLI operation for the owner.

Search fallback (`search --github` or `search_vault`) has no per-call offline
or refresh option. When caching is enabled it uses cached bytes first, then
the network on a cache miss. Use `github-context --offline` or MCP
`github_context` with `offline: true` when network access must be avoided.

The prompt hook, brief, ordinary local search and manual Decisions command never implicitly enable
this reader. The default agent rules describe when to use the MCP tool. A host
still needs the tool connected and permitted; a rule file cannot force it to
call a tool.

## Optional cache and offline use

The cache is a separate opt-in. Enabling it stores verified public file bytes
under `.context/github-cache/`; it never stores the prompt. Cache configuration
lives in `.context/github-cache.json`; applied changes keep one previous copy
at `.context/github-cache.json.bak`. Files are keyed by repository, commit
and path, so changing a pin cannot reuse an older version's evidence.

```sh
context-layer github-cache enable ~/MyVault           # preview
context-layer github-cache enable ~/MyVault --apply
context-layer github-cache status ~/MyVault
context-layer github-context ~/MyVault --prompt "project setup" --source project-docs
context-layer github-context ~/MyVault --prompt "project setup" --source project-docs --offline
context-layer github-context ~/MyVault --prompt "project setup" --source project-docs --refresh
context-layer github-cache disable ~/MyVault --apply
context-layer github-cache purge ~/MyVault           # preview
context-layer github-cache purge ~/MyVault --apply
```

Normal retrieval uses an existing cache entry or fetches and caches a missing
file. `--offline` performs no network request: a disabled cache, missing entry
or corrupt record is an explicit error. `--refresh` bypasses a cached entry
and fetches the same pinned commit again. It does not update the source pin.
Disabling the cache leaves its files in place; `purge --apply` removes cache
records, not the configuration or coordination lock. Status and purge previews
may create a lock file in an existing cache directory, without changing records.

Every read rechecks the record coordinates, SHA-256 and Git blob hash. Evidence
labels its origin as `disk-cache`, `network-cached` or `network-refresh` when
the cache is enabled. Hash checks detect accidental corruption; they do not
authenticate a record against someone who can rewrite the cache and its hashes.
Cached content remains external, untrusted evidence. Storage is bounded at
8 MiB and 256 entries, with no silent eviction; inspect errors and purge when needed. Cache
state is excluded from version control and ordinary local evidence indexing.

## Review a newer source version

```sh
context-layer github-sources check ~/MyVault --id project-docs --ref main
# Use the branch/tag you intend to follow, especially if add used a pinned SHA.
context-layer github-sources update ~/MyVault --id project-docs \
  --expected-commit 1111111111111111111111111111111111111111 \
  --commit 2222222222222222222222222222222222222222
# Repeat the update with --apply only after reviewing the actual SHA and diff.
```

`check` reports the pinned and upstream commits plus bounded file diffs, without
changing configuration. Inspect its `errors` and `omissions`: `PARTIAL` means
the preview is incomplete. `update` requires both the exact old pin and a full
new SHA; a stale old pin is refused. `update` validates the SHA syntax and old
pin locally; it does not verify that the new commit or its paths exist. Use
the SHA returned by `check`, review the diffs, then test retrieval after applying.
There is no background update or automatic
promotion to a moving branch. Management commands report `DRY_RUN`, `OK` or
`ERROR`; a check can also report `PARTIAL`. `ERROR` exits 1.
Checks inspect at most four paths, accept at most 128 KiB across the old/new
file bytes and emit at most 6,000 diff characters. A 20-second budget is checked
between network operations; each socket operation has a maximum five-second
timeout. A check can make up to nine GETs: one ref resolution and old/new file
reads for four paths. These bounds are not a hard interrupt of a response
already being read.

## PowerShell workflow

Run from a checkout installed with `py -3 -m pip install .`. Use a disposable
vault while learning the commands. The coordinates and SHA values below are
placeholders; replace them with a real public source and the reviewed results.
All commands are single lines, so no POSIX line-continuation syntax is needed.

```powershell
$contextVault = Join-Path $env:USERPROFILE "MyVault"
context-layer github-sources add "$contextVault" --id project-docs --repo example/project --ref main --path README.md --keyword "project setup"
# Copy the resolved full SHA from the preview before applying.
$reviewedCommit = "1111111111111111111111111111111111111111"
context-layer github-sources add "$contextVault" --id project-docs --repo example/project --ref $reviewedCommit --path README.md --keyword "project setup" --apply
context-layer github-sources list "$contextVault"
context-layer github-cache enable "$contextVault"
context-layer github-cache enable "$contextVault" --apply
context-layer github-context "$contextVault" --prompt "project setup" --source project-docs
context-layer github-context "$contextVault" --prompt "project setup" --source project-docs --offline
context-layer github-sources check "$contextVault" --id project-docs --ref main
# Use the reviewed new SHA from check; preview before adding --apply.
$newCommit = "2222222222222222222222222222222222222222"
context-layer github-sources update "$contextVault" --id project-docs --expected-commit $reviewedCommit --commit $newCommit
context-layer github-sources update "$contextVault" --id project-docs --expected-commit $reviewedCommit --commit $newCommit --apply
```

The [CLI host setup](host-integration.md) is needed only for automatic host
integration; these standalone commands do not require a model API key.

## Evidence and failure semantics

The local packet's `status` and `evidence` remain unchanged. External passages
live in a separate `github-context-v1` result, under `external_context` for
fallback, with their repository, full commit, path, immutable URL, line span,
SHA-256 of the original file bytes and SHA-256 of the delivered excerpt. Text
is verbatim. The Git blob SHA from the API is also checked against the bytes.

| External status | Meaning |
| --- | --- |
| `OFF` | No enabled configuration; no network request. |
| `FOUND` | Candidate passages delivered; judge whether they answer the question. |
| `NOT_FOUND` | No matching configured source or usable passage. |
| `PARTIAL` | Evidence delivered with failed reads or bounded omissions, reported in `errors`. |
| `ERROR` | Configuration or fetch failed and no evidence was delivered. |

The standalone command exits 1 on `ERROR`, 0 for the other statuses; the MCP
tool marks `ERROR` with `isError`. Fallback retains the local search exit and
records remote failures under `external_context`; callers must inspect that
status. `FOUND` does not certify correctness, currency or safety. A pinned
commit stays at that version until the owner deliberately updates it.

External evidence cannot satisfy local `read_source`, `check_claims`, memory
source bindings, handback checks or the session delivery ledger. Cite its
immutable URL and hash instead. A README or prompt saying to override rules,
send credentials or run a command remains untrusted source text. If evidence
still does not settle a claim, keep the missing information explicit.

## Bounds and privacy

- Configuration: at most 32 KiB and eight sources; up to two selected sources
  and four files per request.
- File: at most 128 KiB, with at most 128 KiB of file bytes accepted across
  one call; strict UTF-8 text, with binary/NUL content refused.
- Delivery: at most 2,000 characters per file and 6,000 across the result.
- Retrieval transport responses are capped at 256 KiB each, across at most four GETs.
  The 128 KiB aggregate above limits accepted file bytes, not wire overhead.
  Source add/check have the separate request pattern described above.
- Transport: anonymous HTTPS GET to `api.github.com` only. Retrieval sends the
  pinned commit and path; source add/check also send the requested ref.
  Redirects, environment proxies and returned download URLs are
  not used. No credential lookup, Git clone, subprocess or model call.
- GitHub sees the requested public repository, commit, path and the network
  connection. It does not receive the prompt or local notes. Normal anonymous
  API limits apply; a rate limit is a visible error, with no retry storm.
- TLS certificate and hostname verification stay enabled. If Python has no
  default CA roots, the reader can use the OS CA bundle. Explicit
  `SSL_CERT_FILE`/`SSL_CERT_DIR` settings take precedence;
  `tls_verification_failed` means the local trust configuration needs repair.
- With caching disabled, retrieval writes no downloaded files, prompts or logs.
  Explicit source/cache management writes the selected configuration and backups;
  enabled caching stores bounded public file bytes. Your host may retain the
  returned evidence under its own policies.

The source API contract is documented by
[GitHub's repository contents API](https://docs.github.com/en/rest/repos/contents)
and [commit API](https://docs.github.com/en/rest/commits/commits#get-a-commit).
Reading external evidence can reduce unsupported guesses; it cannot eliminate
hallucinations. No improvement in model answer accuracy or billed token use
is claimed without an independent measurement.
