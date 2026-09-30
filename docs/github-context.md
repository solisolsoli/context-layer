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
# Repeat the same command with --apply after reviewing the resolved commit.
context-layer github-sources list ~/MyVault
```

`add` resolves a branch, tag or commit through GitHub's public API and records a
full commit SHA. Even a dry run needs this anonymous request. Only `--apply`
writes the configuration, with an atomic replacement and a backup of the
previous file. A new configuration enables its explicitly added source; adding
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
it is not a model confidence detector. An explicit `--source` selects a
configured source and permits a prefix excerpt when no prompt term matches
its text; omit it to use keyword routing. Such a prefix is still only a
candidate and may not answer the question.

The MCP server exposes the same paths:

- `search_vault` with `github: true` adds `external_context` on a clean miss.
- `github_context` takes `prompt` and optional `source_ids`, for example
  `{"prompt":"project setup","source_ids":["project-docs"]}`. Optional booleans
  `offline` and `force_refresh` select the cache behavior below; they cannot
  both be true. Configuration management is a CLI operation for the owner.

The prompt hook, brief, ordinary local search and Jev never implicitly enable
this reader. The default agent rules describe when to use the MCP tool. A host
still needs the tool connected and permitted; a rule file cannot force it to
call a tool.

## Optional cache and offline use

The cache is a separate opt-in. Enabling it stores verified public file bytes
under `.context/github-cache/`; it never stores the prompt. Cache configuration
lives in `.context/github-cache.json`. Files are keyed by repository, commit
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
Disabling the cache leaves its files in place; `purge --apply` removes them.

Every read rechecks the record coordinates, SHA-256 and Git blob hash. Evidence
labels its origin as `disk-cache`, `network-cached` or `network-refresh` when
the cache is enabled. Hash checks detect accidental corruption; they do not
authenticate a record against someone who can rewrite the cache and its hashes.
Cached content remains external, untrusted evidence. Storage is bounded at
8 MiB and 256 entries, with no silent eviction; inspect errors and purge when needed. Cache
state is excluded from version control and ordinary local evidence indexing.

## Review a newer source version

```sh
context-layer github-sources check ~/MyVault --id project-docs
# For a manually configured source without ref, add --ref main to the check.
context-layer github-sources update ~/MyVault --id project-docs \
  --expected-commit 1111111111111111111111111111111111111111 \
  --commit 2222222222222222222222222222222222222222
# Repeat the update with --apply only after reviewing the actual SHA and diff.
```

`check` reports the pinned and upstream commits plus bounded file diffs, without
changing configuration. Inspect its `errors` and `omissions`: `PARTIAL` means
the preview is incomplete. `update` requires both the exact old pin and a full
new SHA; a stale old pin is refused. There is no background update or automatic
promotion to a moving branch. Management commands report `DRY_RUN`, `OK` or
`ERROR`; a check can also report `PARTIAL`. `ERROR` exits 1.
Checks inspect at most four paths, accept at most 128 KiB across the old/new
file bytes and emit at most 6,000 diff characters. A 20-second budget is checked
between network operations; each socket operation has a maximum five-second
timeout. These bounds are not a hard interrupt of a response already being read.

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
| `PARTIAL` | Some evidence delivered, with failed reads reported separately. |
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
- Transport responses are capped at 256 KiB each, across at most four GETs.
  The 128 KiB aggregate above limits accepted file bytes, not wire overhead.
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
