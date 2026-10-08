# Upgrading from 0.4 to 0.5

0.5.0 remains alpha. It removes the Jev advisor and introduces separate,
manual API commands. Existing local retrieval, source-linked memory and
mechanical claim checks do not require an API key.

## What changes

| Old interface | 0.5 behavior |
| --- | --- |
| `context-layer jev ...` | Removed; there is no advisor mode to enable. |
| `search --jev`, `--no-jev`, `--jev-candidates` and hook advisor flags | Removed; update scripts and saved hook commands. |
| MCP `jev_status`, `search_vault` with `jev` | Removed; refresh the host's tool list and remove the old argument. |
| Obsidian Jev advisor settings and display | Removed; update the plugin alongside the Python package. |
| `jev-claim-report/v1` | Mechanical `check_claims` now reports `context-layer-claim-check/v1`; update schema consumers. |
| `.context/jev*` files | Legacy local artifacts; the new API commands do not read or migrate them. |

Back up managed host configuration, preview `context-layer install` again,
and use `--apply` only after reviewing the diff. Consult the
[host guide](host-integration.md) for existing manual hook settings and undo.
The package does not automatically delete legacy files. Inspect and remove
unwanted advisor state yourself using the [privacy guide](privacy.md).
Historical Jev changelog entries describe earlier releases, not available 0.5 commands.

## Selecting a manual assessment

Decisions is an explicit assessment of a file you prepare, rather than a
replacement for Jev shadow/on modes, calibration or automatic rescue. There is
no parameter-for-parameter migration and no automatic source selection.
For a fictional example, save this as `example.json`:

```json
{"claim":"The timer has two programs.","passage":"The fictional setup guide lists two timer programs."}
```

Preview without a key or network request:

```sh
context-layer api plan --need claim_support --data-scope synthetic
context-layer decisions assess --task claim_support --data-scope synthetic --input example.json
```

After checking every field, an operator may explicitly add `--send` with
`OPENAI_API_KEY` set in the environment. Scope flags are caller assertions,
not privacy detection. Public or synthetic text only; never pass private vault
notes. An assessment is advisory, not source truth, approval or calibrated
confidence. API account access is separate from a successful local preview.

Use [Responses](responses.md) for a selected public or synthetic drafting task
with an explicit model. Use [offline routing](api-routing.md) to preview a
route; it does not call either API. Neither API command is invoked by search,
hooks or MCP. Original source inspection remains necessary.

## Defaults to check

CLI `search` and MCP `search_vault` still default to FTS. The optional Claude
Code prompt hook defaults to synaptic retrieval with focused delivery. Set
`--method fts --delivery window` explicitly if an existing hook needs the
previous retrieval/delivery choice. GitHub retrieval and caching stay opt-in;
pinned versions are not advanced automatically.

## Existing index state

The first index after upgrading rebuilds a legacy index that lacks an index
receipt. Index and graph content digests detect corrupted reuse and cause a
rebuild; they are not authentication against a writer who can replace both data
and receipts. A healthy no-op index preserves existing bytes and timestamps.
Run `context-layer index <vault>` after upgrading; no source notes are changed.
