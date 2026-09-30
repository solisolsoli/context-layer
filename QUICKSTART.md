# Quickstart

The acceptance walk from [SCOPE.md](SCOPE.md), as commands. Steps 1–2 and 4–8
are things you can run here and now; steps 3 and 9 happen inside an AI host and
are marked as such. Python 3.10+ with SQLite FTS5 support.

Replace `/absolute/path/to/vault` with your vault and `/absolute/path/to/repo`
with the project you will talk to the host from.

## 0. See it work on a throwaway vault first

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
python3 -m context_layer.cli --version
make demo
```

The demo copies seven fictional documents to `.demo-vault`, infers configuration,
builds an index, and prints a source-evidence JSON packet. It leaves the copy for
inspection. `make clean` removes demo/build artifacts, not your own vault.

## 1. Install, configure, index

```sh
context-layer init /absolute/path/to/vault --print-only   # see the guesses, write nothing
context-layer init /absolute/path/to/vault
# Review .context/routes.json before indexing — especially exclude_prefixes.
context-layer index /absolute/path/to/vault
```

`init` reads files to extract headings and metadata; routes and canonical sources
are guesses. Review exclusions and authority yourself. It refuses to overwrite
configuration unless `--force` is given. Notes are not rewritten. Metadata/index
files live in `.context`; optional router run artifacts live in `.context-runs`.

Check the evidence path before connecting anything to it:

```sh
context-layer search /absolute/path/to/vault --prompt "release versioning policy"
```

Search defaults to FTS, top-k 3, 6,000 evidence characters, 2,000 per source.
Use `--top-k`, `--budget`, `--per-source` or `--method` to change these explicitly.
The JSON includes each source path, its indexed SHA-256 and verbatim content: a
note whole when it fits, else the windows that hold the match, each with its byte
and line span.
`PARTIAL` means evidence is available, not that an answer has been verified. `ERROR` means retrieval failed (exit 1, no evidence).
`NOT_FOUND` means this retrieval returned nothing, not proof of corpus-wide absence.
Search exits 0 on completed retrieval, including abstention, and 1 on operational
failure. Invalid arguments exit 2. A note that changed after indexing is left out
and listed under `withheld` with the next command (`context-layer index <vault>`);
a missing or broken index is an `ERROR`, and operational errors carry empty
evidence.

## 2. Connect a host

Look at the change before it happens; nothing is written without `--apply`.

```sh
context-layer install claude-code --vault /absolute/path/to/vault \
  --project /absolute/path/to/repo --hook              # unified diff, writes nothing
context-layer install claude-code --vault /absolute/path/to/vault \
  --project /absolute/path/to/repo --hook --apply      # backs up, then writes
```

That writes `<project>/.mcp.json` and one `UserPromptSubmit` hook in
`<project>/.claude/settings.json`. For any other MCP stdio client:

```sh
context-layer install generic --vault /absolute/path/to/vault   # prints a snippet, writes nothing
```

Undo at any time — `uninstall` removes this tool's MCP entry and its hook
together, whatever flags the install used, and keeps a timestamped backup:

```sh
context-layer uninstall claude-code --vault /absolute/path/to/vault \
  --project /absolute/path/to/repo --apply
```

Details, the Codex block and troubleshooting: [docs/host-integration.md](docs/host-integration.md).

## 3. Ask in the host *(inside the host)*

Start the host in that project and confirm it sees the server — in Claude Code,
`/mcp` lists `context-layer` and its nine tools. Ask a question the vault
answers. The reply should cite a `source_path` and a `source_sha256` you never
pasted. If the index is broken or a file is unreadable, you get a visible error,
not an empty success; a note edited since the last `index` is left out with a
visible "withheld" line until you re-index.

## 4. Record the decision

```sh
context-layer memory add /absolute/path/to/vault --kind decision --state approved \
  --text "Search defaults to FTS." --source notes/search.md
```

The source's current SHA-256 is stored with the record. Pin an exact version
with `--source path@<64 hex>`; a mismatch is refused rather than recorded.
Running the same command twice appends one line, not two.

## 5. Resume somewhere else

```sh
context-layer memory resume /absolute/path/to/vault
context-layer memory resume /absolute/path/to/vault --json
context-layer memory verify /absolute/path/to/vault    # exit 1 on any problem
```

`resume` returns the records still in force, the open tasks, and a `stale` list
naming every source whose hash no longer matches what was recorded. Read
`records.jsonl` and `MEMORY.md` directly in `.context/memory/` — they are plain
files. More: [docs/memory.md](docs/memory.md).

## 6. Dispatch a bounded sub-agent task

```sh
context-layer tasks new /absolute/path/to/vault \
  --goal "Summarise the release versioning policy" \
  --source "notes/release/*.md" --output-dir drafts \
  --backend claude --model <model> --max-attempts 2 --timeout 300
```

The packet is built now, at `new` time, so you can read
`.context/tasks/<id>/packet.json` and `prompt.txt` before anything runs. Only
the sources you allowed can appear in it; everything else is listed under
`dropped_outside_spec`. Then:

```sh
context-layer tasks run /absolute/path/to/vault <id>
context-layer tasks list /absolute/path/to/vault
```

A finished run stops at `pending_review`. A backend whose binary is missing
gives `blocked`, never `done`.

`--backend` defaults to `fake`, which runs no model: it runs the executable
script named in `CONTEXT_LAYER_FAKE_BACKEND` (set it for `tasks run`). The
script reads the prompt on stdin, writes its files under
`$CONTEXT_LAYER_OUT_DIR`, and prints one claude-style JSON object on stdout
(`{"result": "...", "is_error": false, "usage": {...}}`; the full shape is in
[docs/tasks.md](docs/tasks.md#backends)). That is enough to try the whole
new/run/verify/cost flow without a model:

```sh
CONTEXT_LAYER_FAKE_BACKEND=/absolute/path/to/agent.py \
  context-layer tasks run /absolute/path/to/vault <id>
```

`--output-dir` is a folder inside the vault (without it, output stays under
`.context/tasks/<id>/out`). Its files are ordinary vault files, so the next
`context-layer index` makes them searchable evidence; if drafts must not become
evidence, add that folder to `exclude_prefixes` in `.context/routes.json`.

## 7. Verify, and total the cost

```sh
context-layer tasks show /absolute/path/to/vault <id>
context-layer tasks verify /absolute/path/to/vault <id>            # 0 verified, 1 rejected
# instead of the line above, to also append the result to memory:
#   context-layer tasks verify /absolute/path/to/vault <id> --record
context-layer tasks cost /absolute/path/to/vault \
  --coordinator-usage /absolute/path/to/host-usage.json
```

A task can be verified once: a second `verify` exits 2 because the task is no
longer `pending_review`. `host-usage.json` is the host's own usage: one JSON
object or a list of them, each with a `usage` object (or the token keys at the
top level: `input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
`cache_read_input_tokens`) and optional `total_cost_usd` and `duration_ms`; see
[docs/tasks.md](docs/tasks.md#cost-accounting). Pass it as an absolute path.

`verify` rejects when any attempt touched a file outside the allowed output
directory, when the output directory is empty, or when an output cannot be read.
`cost` sums every attempt including retries, and `--coordinator-usage` adds the
host's own usage so the total covers coordinator + agents + retries. Limits and
the state machine: [docs/tasks.md](docs/tasks.md).

## 8. Watch the sources

```sh
context-layer status /absolute/path/to/vault          # 0 ok, 1 stale/degraded, 2 no usable index
context-layer status /absolute/path/to/vault --json
context-layer index /absolute/path/to/vault           # rebuild; keeps the replaced index
context-layer rollback /absolute/path/to/vault --dry-run
context-layer rollback /absolute/path/to/vault        # restore the previous index
```

`status` reads and hashes every in-scope file, so a source rewritten with its
size and mtime preserved is still reported as `changed`. Every path it prints is
vault-relative; the vault's own location never appears in the report.
More: [docs/source-lifecycle.md](docs/source-lifecycle.md).

## 9. Measure it *(held-out set, written before step 3)*

The synthetic delivery comparison runs here:

```sh
python3 eval/compare.py --out /tmp/context-comparison
```

It writes per-case stdout/stderr, scoring JSON and a summary. The test corpus is
disposable. See [the contract](eval/EVIDENCE_CONTRACT.md) and
[the evaluation limits](eval/comparison/README.md). The CLI also forwards custom
evaluation arguments through `context-layer eval`; use absolute paths for your
own stimuli and outputs because its default working directory is the bundled eval directory.

The live before/after — same host, same model, same questions, with and without
this layer — is a separate measurement run by the coordinator against a held-out
set written before any candidate run. Its definitions are in [SCOPE.md](SCOPE.md).

## The experimental router

Richer configuration and metadata, not the default:

```sh
context-layer route /absolute/path/to/vault \
  --prompt "writing standards" --evidence-json --no-save
```

Router abstention exits 2; operational failure exits 1. Its `--json` is metadata
only. Use `--evidence-json` when a consumer or evaluator needs the actual passages.
Read [router/README.md](router/README.md) before changing routes.
