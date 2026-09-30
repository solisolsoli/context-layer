# GitHub context for missing knowledge

This optional reader fetches **candidate evidence** from public GitHub files
the vault owner chooses. It is off by default, needs no token or model API,
and never uploads a question or a vault note. It reads documentation, including
prompt files or MCP setup guides; it does not install or execute their contents.

## Configure the sources once

Create `.context/github.json` in the vault. All values below are fictional;
replace the repository, paths, keywords and full 40-character commit SHA with
the sources and version relevant to your project. Copy the SHA from that
repository's commit page. Branch names, tags and arbitrary URLs are refused.

```json
{
  "version": 1,
  "enabled": true,
  "sources": [
    {
      "id": "project-docs",
      "repo": "example/project",
      "commit": "1111111111111111111111111111111111111111",
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
  `{"prompt":"project setup","source_ids":["project-docs"]}`.

The prompt hook, brief, ordinary local search and Jev never implicitly enable
this reader. The default agent rules describe when to use the MCP tool. A host
still needs the tool connected and permitted; a rule file cannot force it to
call a tool.

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
- Transport: anonymous HTTPS GET to `api.github.com` only, pinned commit and
  path only; redirects, environment proxies and returned download URLs are
  not used. No credential lookup, Git clone, subprocess or model call.
- GitHub sees the requested public repository, commit, path and the network
  connection. It does not receive the prompt or local notes. Normal anonymous
  API limits apply; a rate limit is a visible error, with no retry storm.
- TLS certificate and hostname verification stay enabled. If Python has no
  default CA roots, the reader can use the OS CA bundle. Explicit
  `SSL_CERT_FILE`/`SSL_CERT_DIR` settings take precedence;
  `tls_verification_failed` means the local trust configuration needs repair.
- No downloaded files, prompts, caches or logs are written by this reader.
  Your host may retain the returned evidence under its own policies.

The source API contract is documented by
[GitHub's repository contents API](https://docs.github.com/en/rest/repos/contents).
Reading external evidence can reduce unsupported guesses; it cannot eliminate
hallucinations. No improvement in model answer accuracy or billed token use
is claimed without an independent measurement.
