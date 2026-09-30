# Privacy: what context-layer writes, and how to remove it

context-layer makes no network request of its own unless you explicitly use
configured [GitHub context](github-context.md) or enable the
optional advisor ([jev.md](jev.md)). Both are off by default. GitHub context
sends only configured public repository paths and pinned commits, never a
prompt, local note or credential; it writes no fetched content or cache.
Without these opt-ins, indexing, search, memory, status and the task runner
read and write local files only. That is narrower
than "your notes never leave your machine". Evidence it hands to an AI host
(through the MCP server, the prompt hook or a task backend such as `claude`)
goes wherever that host sends it, and is kept under that host's retention.

Everything below is a plain file. There is no hidden store, no background
process and no automatic pruning: nothing is deleted unless you delete it.

## Artifacts inside the vault

| Path | Written by | Contains | Kept until |
| --- | --- | --- | --- |
| `.context/routes.json` | `context-layer init` (refuses to overwrite without `--force`) | Route names, trigger terms guessed from folder names, file names and headings, vault-relative paths, exclusion prefixes, optional `stopwords`. No note text. | You delete or edit it. |
| `.context/github.json` | You, by hand | Public repository names, pinned commit SHAs, file paths and local routing keywords. No tokens, prompts or note text. Read only; disabled unless enabled explicitly. | You delete or edit it. |
| `.context/facts.json` | You, by hand (the router only reads it) | Answer cards: short quotes and the paths they come from. | You delete it. |
| `.context/index.sqlite` | `context-layer index` | **A full-text copy of every indexed file**, plus each file's path, SHA-256 and modification time, and the absolute vault path in its metadata. | The next `index` replaces it. |
| `.context/index.sqlite.prev` | `context-layer index` (keeps the previous build for `rollback`) | The previous full-text copy. Text from a note you deleted or newly excluded survives here for one more build. | The build after next. |
| `.context/index-manifest.json` and `.prev` | `context-layer index` | Paths, SHA-256, sizes and times. No note text. | As the index. |
| `.context/graph.sqlite` | `context-layer index` (skipped with `--no-graph`) | The explicit link graph: note paths, SHA-256, sizes, modification times, frontmatter aliases, and each link's kind, target, line number, heading/block anchor and frontmatter key. No note text beyond those names. | The next `index` replaces it. |
| `.context/graph.sqlite.prev` | `context-layer index` (the graph that belongs with `index.sqlite.prev`, for `rollback`) | The previous link graph, including links from notes you since deleted or excluded. | The build after next. |
| `.context/activation.json` (and a transient `.activation-*.json`) | Every synaptic retrieval: `search --method synaptic`, MCP `search_vault` with `method: synaptic`, the synaptic hook (unless `"write_activation": false` in `routes.json`) | The last synaptic retrieval's activated notes and edges: vault-relative paths, scores, hops, link anchors, a random `run_id`, the time. The query text only if you opt in (`"record_query_text": true` or `--record-query`); no hash of it. Excluded, stale and withheld notes are never listed. | Overwritten by the next synaptic retrieval. |
| `.context/usage-ledger.jsonl`, `.context/usage-ledger.jsonl.1`, `.context/.usage-ledger.lock` | Every fts or synaptic retrieval that delivers two or more notes, **only when `"record_usage": true` is set in `routes.json`** (off by default) | One line per retrieval: format version, UTC time, method, a 12-hex hash of the host session id (or `null`) and the vault-relative paths of the notes delivered together. No note text, no prompt, no hash of the prompt; excluded notes are never listed. `0600`. Read only by `context-layer graph suggest`, never by retrieval ([synapse.md §11](synapse.md#11-link-suggestions-from-usage-opt-in)). It shows which notes you and your agents use together, so keep it out of anything you share. | Rotates at 1 MiB (two files at most); you delete it. |
| `.context/packets/<id>.json` | `context-layer packet build` | A shared evidence packet: **verbatim evidence**, paths, hashes, line spans, and the **query text** it was built for. | You delete it. |
| `.context/jobs/<task>/attempt-NNN/` and `.context/jobs/worker_core.md` | `context-layer job new`, `handoff write`, and the worker the job is given to | `job.md` (goal, objective, allowed roots, budgets), `input-manifest.json`, `payload.json`, whatever the worker returned there (`evidence.jsonl`, `coverage.json`, `receipt.json`, `handoff.md`), and a copy of the shipped worker rule core. | You delete it. |
| `.context/session-rules.json`, `.context/rules.lock` | The rules hooks (`install claude-code --rules`) and `context-layer rules` | Host session ids, start times and the SHA-256 of `CLAUDE.md`, `AGENTS.md` and `LOG.md` at session start. No note text. At most 20 sessions. | Pruned to the newest 20 sessions; you delete it. |
| `CLAUDE.md`, `AGENTS.md`, `LOG.md`, `BACKLOG.md` at the vault root | `context-layer brain init --apply`, `context-layer rules init` / `rules record` | Rule text and the dated work records you or an agent recorded. Visible notes, indexed like any other. | You edit or delete them. |
| `.context/.index-*.sqlite`, `.context/.staging-*` | `context-layer index`, while it runs | A build in progress (full text). Removed on success and on failure; a hard kill can leave one behind. | You delete it. |
| `.context-runs/<date>/<time>-<prompt hash>/` | `context-layer route` **by default** (`--no-save` writes nothing; `--runs-dir` moves it) | `request.json` with the **raw prompt**, `context.md` with the prompt and **verbatim evidence**, `overflow/*.txt` with full note sections, ledgers of paths and hashes. | You delete it. |
| `.context/memory/records.jsonl` | `memory add`, the MCP tool `memory_record`, `tasks verify --record` | Append-only free text written by a person **or an agent**, the source paths and hashes it rests on, time, tool name and session id. | You delete it (see below). |
| `.context/memory/MEMORY.md`, `.lock` | Every memory write | A derived view of the records; a lock file. | Regenerated on every write. |
| `.context/memory/records.jsonl.torn-<utc>` | `memory repair` | The torn last line moved aside, byte for byte. | You delete it. |
| `.context/sessions/<id>.jsonl` | memory calls while `CONTEXT_LAYER_SESSION` is set | Record ids, source paths and hashes, times; never text. | You delete it. |
| `.context/session-evidence/<session>.jsonl` | The prompt hook and the MCP server, **only when** `install --session-evidence` (or the hook's `--session-evidence` flag and `CONTEXT_LAYER_SESSION_EVIDENCE=1` for the server) is set and the folder exists | One line per delivered item: session id, channel, packet id, source path, SHA-256, line span, time; never note text. Appending stops at 4 MiB per file; only the 50 most recently written session files are kept. | You delete it. |
| `<folder>/*.md`, `<folder>/.memory-mirror.json` | `memory mirror --notes <folder>` | Copies of decision and task text as notes (excluded from retrieval by default), and the list of files written. | `context-layer memory mirror VAULT --notes <folder> --remove`. |
| `.context/tasks/<task-id>/` | `context-layer tasks` | `task.json` (goal, sources, backend command), `packet.json` and `prompt.txt` (**verbatim evidence**), `attempts/<n>.json` (the command line — for the `claude` and `codex` backends that includes the whole prompt — usage, up to 2000 characters of output), `attempts/<n>.stdout` / `.stderr` (the agent's full output), `result.json`, and `out/` (what the agent wrote). | You delete it. There is no task delete command. |
| `.context/tasks/TASKS.md` | Every task state change | A derived table with the first 60 characters of each goal. | Regenerated. |
| `.context/task-pins/<task-id>.json` | `tasks new` | The task definition (including its goal) and the SHA-256 of its files, used by `tasks verify`. | You delete it, together with the task directory. |
| A custom `tasks --output-dir` | The agent | Whatever the agent wrote. A directory that is not hidden is indexed by the next `index`, so agent output becomes searchable evidence. | You delete it. |
| `.mcp.json`, `.claude/settings.json` | `context-layer install claude-code --apply` (`--project` defaults to the vault) | The launch command, the absolute vault path, `PYTHONUTF8=1`, possibly a `PYTHONPATH`. | `context-layer uninstall ... --apply`. |
| `<file>.bak-<UTC time>` | Every `install --apply` / `uninstall --apply` | The previous bytes of the file it changed. | You delete it; backups are never pruned. |
| `.context/jev.json` | `context-layer jev off/shadow/on` (the optional advisor) | Mode, features, provider kind, endpoint, model id and the **name** of the variable holding its key, limits, thresholds, local-only prefixes. Never a key, never note text. `0600`. | You delete it. |
| `.context/jev.disabled` | You (the advisor's kill switch) | Nothing needed: its presence switches the advisor off. | You delete it. |
| `.context/jev-calls.jsonl` | Every advisor call in a configured vault (`search --jev`, the prompt hook with `auto_context` enabled, `jev record`) | One row of counters per call (mode, codes, latency, token and request counts, how many passages were judged, flagged, rescued). No query, no note text, no path. `0600`. | At 512 KiB the older half is dropped; `jev purge --apply --all`. |
| `.context/jev-cache/`, `.context/jev.salt` | Advisor calls in `shadow` or `on` | Validated yes/no answers with probabilities and counters, in files named by a keyed hash; the salt is 32 random bytes. No question, excerpt or key. | Expire after `cache_ttl_s`; `jev purge --apply`. |
| `.context/jev-calibration/<kind>-<model>.json` | `context-layer jev calibrate --apply` | The calibration receipt: provider kind and model, thresholds, template revision, `passed` per purpose with its counts and rates on the fictional development set, hashes of the dev set, report and recording. No path, no text. `0600`. | `jev purge --apply --receipts`. |
| `.context/jev-recordings/<kind>-<model>-<utc>.jsonl` | `context-layer jev record --run` (default `--out`) | One row per recorded question: hashes, template, provider identity, the validated answer or a failure code, counters. Never the question, an excerpt or a key. `0600`. | `jev purge --apply --receipts`, or delete the file. |

The graph and the activation trace are described in [synapse.md](synapse.md).
`activation.json` records which notes a retrieval selected, not the model's
reasoning, and holds a random `run_id` rather than any hash of the query (0.3.0
development builds stored an unsalted SHA-256 of the query; a short query can be
recovered from such a hash by guessing, so it was removed).

With the advisor enabled, a synaptic `search --jev` also adds labels to
`activation.json`: an additive `jev` object and one word per note (`rescued`,
`on_topic`, `off_topic`, `local_only`, `not_judged`). No text.

`context-layer status` and the MCP `vault_status` tool only read and print.
`context-layer rollback` swaps `index.sqlite`, the manifest and `graph.sqlite`
with their `.prev` copies, which is its own undo.

## Artifacts outside the vault

- **Host configuration**: `~/.codex/config.toml` (a marked block) with
  `install codex --apply`, and whatever `claude mcp add --scope user` writes
  when you run the command `install --scope user` prints. Each has an
  `uninstall`.
- **Host transcripts**: the prompt hook adds evidence to the host's context,
  so it lands in the host's own session history.
- **Shell history and process lists**: a prompt passed as `--prompt "..."` is
  in your shell history and visible in the process list while it runs. With
  `--verbose` the CLI wrapper also echoes the command it forwards (including
  the prompt and absolute paths) to stderr; without it, it does not. `route` accepts
  `--prompt-file` for anything sensitive; `search` takes the prompt only as an
  argument today. The MCP server and the hook receive prompts over stdin and
  put nothing on a command line of their own, but they pass the prompt to the
  retrieval subprocess as an argument.
- **Evaluation output**: `eval/evaluate.py`, `eval/compare.py` and
  `eval/live_compare.py` write only to the `--out` / `--packets-dir` you name;
  `live_compare.py` records prompts and answers there, and edits
  `<vault>/.claude/settings.json` for the length of one hook-arm run, restoring
  the original bytes afterwards.

## What the advisor sends

Nothing, unless you configure it: the advisor is off by default, and without
`.context/jev.json` its provider client is never loaded. In mode `shadow` or `on`,
`context-layer search --jev` sends to the provider you named: the question (at
most 2,000 characters) and, for each judged passage and each note it considers
adding, the note's file name without its folder (at most 160 characters), the
line that links to it (at most 300 characters) and the first `excerpt_chars`
(default 800) characters of the passage that would be delivered. Never folder
paths, hashes, the vault name or a key. `context-layer jev status` prints the
same notice for your configuration.

Before anything is sent: notes under `local_only_prefixes`, and notes whose
frontmatter says `remote_allowed` or `jev` other than exactly `true`,
`sensitivity` other than `public`/`internal`/`normal`, or `visibility: private`
(or whose frontmatter cannot be read with certainty), are never sent; a secret
scan stops the whole call on a match; a question shorter than 12 characters is
not sent. What you send is then under the provider's terms and retention; the
key and the optional `env_file`/`blocklist_file` stay outside the vault and are
never copied into it. Details: [jev.md](jev.md).

## Sync, backups and version control

All state lives inside the vault by default, so anything that copies the vault
copies it too: a cloud-synced folder (iCloud, Dropbox, OneDrive, Syncthing),
a backup tool, and a Git repository. A leading dot hides a folder from Obsidian's file list; it does not
exclude it from any of those. Obsidian Sync is the exception: its settings page
(obsidian.md/help/sync/settings) says files and folders beginning with a `.` are
excluded from sync, except `.obsidian`, so `.context/` and `.context-runs/` stay
local under Obsidian Sync. The visible files this tool can write (`CLAUDE.md`,
`AGENTS.md`, `LOG.md`, `BACKLOG.md`, a custom task output directory) do sync.

- Set up the sync provider's ignore rules **before** the files exist; most
  providers ignore a rule for files they already track.
- For a vault kept in Git, ignore at least:

  ```gitignore
  .context/
  .context-runs/
  .mcp.json
  .claude/settings.json
  *.bak-*
  ```

  Check with `git status --short --ignored` and `git ls-files .context`. An
  ignore rule does not untrack a file that is already committed and does not
  remove it from history.

## Deleting

| To remove | Do |
| --- | --- |
| Everything this tool keeps in a vault | Uninstall any host configuration first (`context-layer uninstall ... --apply`), then delete `.context/` and `.context-runs/`. |
| The full-text index | Delete `.context/index.sqlite*` and `.context/index-manifest.json*`; `index` rebuilds them. |
| The link graph and the last activation | Delete `.context/graph.sqlite*` and `.context/activation.json`. Set `"write_activation": false` in `routes.json` to stop writing the trace. |
| The usage ledger | Delete `.context/usage-ledger.jsonl*` and `.context/.usage-ledger.lock`; set `"record_usage": false` (or remove it) in `routes.json` to stop writing it. |
| Shared packets and support jobs | Delete `.context/packets/` and `.context/jobs/`. |
| A deleted note's text from the index | Run `index` twice: once to rebuild without it, once more so `.prev` no longer holds it. |
| Saved route runs | Delete `.context-runs/` (or the dated sub-folders you no longer want). Use `route --no-save` from now on. |
| One task | Delete `.context/tasks/<id>/` and `.context/task-pins/<id>.json`. |
| Memory | Delete `.context/memory/`, or remove lines from `records.jsonl` and run `context-layer memory verify` to see which records the hash chain no longer covers; delete `.context/sessions/` and `.context/session-evidence/`; remove visible notes with `context-layer memory mirror VAULT --notes <folder> --remove`. |
| Install backups | Delete the `*.bak-*` files next to the configuration they back up. |
| The advisor's files | `context-layer jev purge <vault> --apply --all --receipts` (without `--apply` it only lists), then delete `.context/jev.json` and `.context/jev.disabled`. What was already sent stays under the provider's retention. |

Deleting a file removes it from this vault on this machine. It is not
forensic erasure (SQLite and file systems can keep freed pages until they are
overwritten), and it does not reach sync-provider version history, backups,
Git history or clones. A general `privacy inspect` / `purge` command does not
exist; `context-layer jev purge` removes only the advisor's own files.

## What not to put in the vault or in memory

- Credentials, tokens and keys: anything indexed can be retrieved verbatim and
  handed to a host.
- Content you may not pass to a model provider: an excluded prefix
  (`exclude_prefixes` in `routes.json`) keeps a folder out of the index and out
  of every packet, but excluding it after it was indexed takes a rebuild (and a
  second one for `.prev`).
- Secrets in memory records: an agent can write memory through MCP, and a record
  is kept verbatim with no expiry.

## Threat model: vault content is data, not instructions

A vault holds text you did not all write yourself: clipped web pages, e-mail,
meeting transcripts, notes an agent wrote. Any of it can contain text shaped
like an instruction ("ignore previous instructions and ..."). context-layer
delivers source text **verbatim** — that is the point of the hash — so it also
delivers such text unchanged. It does not rewrite, strip or classify content,
and it cannot tell a note you wrote from one you imported.

What exists today:

- Every MCP tool description and the hook's preamble state that the evidence
  is data, not instructions. In the hook (fts and synaptic alike) each item sits
  between markers carrying a random per-packet nonce, with its path and hash in
  the opening marker, so a note cannot forge an item boundary or a header.
- A task prompt tells the agent the evidence is retrieved data, to ignore any
  instruction, request or link inside it, to write only in its output
  directory, and to answer `NOT_FOUND` rather than guess. The evidence sits
  inside a code fence longer than any backtick run it contains, with its path
  and hash outside the fence.
- A task agent receives a bounded packet, never the vault; `tasks verify`
  detects writes outside the output directory and edits to the task definition
  (see [tasks.md](tasks.md)).
- Retrieval refuses excluded prefixes, symlinks, `..` and absolute paths before
  reading a file; `read_source` is capped at 6000 characters.

What is **not** mitigated, stated plainly:

- **The prompt hook** adds evidence to every prompt of a host session that
  usually has file, shell and network tools. The notice that it is data is a
  request to the model, not an enforcement; this project has not measured how
  well any model honours it. The host's own permission settings are the only
  control over what the host then does.
- **Task backends** run as your user with your environment (including any
  credentials in environment variables), with no network restriction and no
  sandbox. Detection happens after the fact.
- **Memory persistence**: through the MCP tool an agent — possibly steered by a
  note — can append memory records (always as drafts over MCP), and `memory_resume` feeds them into later
  sessions. The hash chain shows that records changed, not that they were
  trustworthy when written.
- **No content flagging**: invisible or bidirectional Unicode, spoofed role
  markers and remote images in Markdown are passed through as they are.
- A SHA-256 proves that the bytes are the ones indexed. It says nothing about
  whether those bytes are safe to follow.

Never let a host act on vault evidence without the permissions you would give
it for untrusted text. Reporting a way around the protections that do exist is
covered by [SECURITY.md](../SECURITY.md).
