# CLI conventions: exit codes, error packets, output and format versions

This page is the reference for how every `context-layer` command reports its
outcome. It describes the behaviour of 0.4.0; numbers were not renumbered from
0.2, so scripts written against 0.2 keep working.

## Exit codes

The rule for every command: **0** means the command did what it was asked (an
empty result included), **1** means an operational failure or a negative
verdict, **2** means the command line itself was wrong (usage). The exceptions
are listed in the table and are kept for compatibility.

| Command | 0 | 1 | 2 |
| --- | --- | --- | --- |
| `search` (and `eval/retrieve.py`) | a packet with status `PARTIAL` or `NOT_FOUND`, including one that withheld changed notes (router method: see below) | operational error: `status: "ERROR"` packet on stdout, one line on stderr | usage: an unknown flag, a missing prompt, or a flag that does nothing for the method, named in one stderr line (`--compact`, `--extra-tokens`, `--budget-tokens`, `--max-hops` or `--record-query` without `--method synaptic`; `--budget-tokens` without `--compact`; `--extra-tokens` with `--compact`) |
| `hook claude-code`, `hook codex` | evidence printed (whole items, possibly with an "item(s) omitted" line or a withheld notice), a withheld notice alone, or nothing to add (`NOT_FOUND`, or an empty or blank prompt); an unknown `--method` value runs fts | any failure, a malformed command line included (unknown flag, bad or missing value, missing `--vault`, unknown host, a value above a cap), a synaptic budget flag where it has no effect (`--budget-tokens` without `--compact`; `--extra-tokens` with `--compact`; `--compact`, `--extra-tokens` or `--budget-tokens` without `--method synaptic`), unreadable hook JSON, hook JSON with no `prompt` string, a retrieval slower than 20 s; one line on stderr, nothing on stdout | **never**: Claude Code reads exit 2 from a `UserPromptSubmit` hook as "block and erase the prompt" |
| `mcp` | the client closed stdin (calls already accepted finish first); a malformed message gets a JSON-RPC error and never ends the server | the vault or budgets are unusable at start (a default above a cap included), or an unknown flag | usage (argparse: a missing `--vault`, a non-integer value) |
| `index` | index (and link graph) built or updated (only changed notes are written again; `--full` rebuilds everything, see [source-lifecycle.md](source-lifecycle.md#incremental-update-the-default-and---full)); files it could not index are skipped and listed (see [source-lifecycle.md](source-lifecycle.md#what-the-index-skips)) | build failed; the previous index is untouched | usage |
| `init` | `routes.json` written (or printed) | vault missing, file exists without `--force`, or no route inferred | unknown argument |
| `status` | `ok` | `stale` or `degraded` | `missing` (no usable index: absent, unreadable, wrong format or an inconsistent full-text table) or `error` (`routes.json` cannot be trusted, so nothing was read) |
| `rollback` | restored (or `--dry-run` shown) | nothing to restore, the link graph cannot move with the index (use `--index-only`), or I/O error | unknown argument |
| `graph health` | report shown | the graph or index cannot be read | usage |
| `graph suggest` | report shown (also when there is no ledger yet) | no usable link graph or `routes.json` | usage |
| `doctor` | every check passed | a check failed (table shown) | usage |
| `brief` | briefing printed | the vault has no usable state | usage |
| `session show VAULT [ID] [--json]`, `session list VAULT [--json]` | report printed, including one that lists missing, torn or partial sources (`complete: false`) | unknown session id, no sessions in the vault (`show` without an id), or the vault is missing | usage: an empty id, `--limit` or `--max-bytes` below 1, an unknown flag |
| `jev status VAULT [--json] [--check]` | shown | the configuration is invalid (shown, nothing changed) | usage, or the vault directory does not exist |
| `jev off\|shadow\|on VAULT …` | written (or nothing to switch off) | refused: invalid file left untouched, no provider named, `calibration_required`, unknown feature | usage, a feature both enabled and disabled, or a vault directory that does not exist |
| `jev report VAULT [--days N] [--json]` | shown | the log cannot be read | usage, or the vault directory does not exist |
| `jev purge VAULT [--apply] [--all] [--receipts]` | shown or removed | I/O error | usage, or the vault directory does not exist |
| `jev answer VAULT --claims FILE [--json]` | report produced, whatever the verdicts (advisory) | the claims file is refused (schema, size, symlink) | usage, or the vault directory does not exist |
| `jev record VAULT --questions FILE …` | dry run shown, or every question answered | refused (no usable configuration, a kill switch, an invalid question, a credential in a question, an existing `--out` without `--append`), or some questions failed (their rows carry a code) | usage, or the vault directory does not exist |
| `jev calibrate VAULT --report FILE --recording FILE [--apply] [--json]` | every purpose the enabled features need is met | refused (nothing written), or a needed purpose is not met | usage, or the vault directory does not exist |
| `jev review-memory VAULT --proposal FILE [--json]` | a report was produced, whatever it says | the input was refused | usage, or the vault directory does not exist |
| `eval` (forwards to `eval/evaluate.py`) | the harness ran (a failed `--gate` is 3) | no stimulus matched the filters | usage, an invalid evidence contract, or a `--command` that cannot be started; **3**: `--gate` was given and the gate failed |
| `route` (experimental router) | evidence run completed | source, index, configuration or I/O failure | **`NOT_FOUND`** (the router's abstention; kept from 0.2) |
| `tasks run` | every task reached `pending_review` | any task did not | unknown argument |
| `tasks verify` | `verified` | `rejected` | the task is not `pending_review` |
| `tasks ledger VAULT [--replay] [--json]` | the chain holds and every `verified` or `rejected` task is backed by it (and, with `--replay`, every verdict reproduces) | the chain is broken, a `verified` or `rejected` task has no ledger line for its verdict, its last ledger verdict differs, or its `result.json` cites another ledger line (the `unattested` list in `--json`), or a replayed verdict differs | unknown argument |
| `tasks` (other), `memory`, `rules`, `packet`, `job`, `handback`, `handoff`, `brain` (`handback check --jev` never changes the exit code) | done | refused (bad input, a boundary, drift, a failed check) | unknown argument; for `packet build` also `--budget-tokens` with `--method synaptic` but without `--compact`, or `--extra-tokens`/`--compact` without `--method synaptic` |
| `install` / `uninstall` | diff shown or written | the host config cannot be read or written | usage, or `--method`/`--extra-tokens`/`--compact`/`--budget-tokens` without `--hook`, or `--budget-tokens` without `--compact` |

Argparse itself exits 2 for a malformed command line on every command except
`hook`, which exits 1 (see its row). Flag prefixes are not accepted (`--pro` is
not `--prompt`).

## `session show`: one session's joined report

`context-layer session show VAULT [SESSION_ID] [--json] [--limit N] [--max-bytes N]`
joins, for one agent session, files that other commands already wrote: the
evidence delivered (`.context/session-evidence/<id>.jsonl`), the memory records
whose `session` is the id (`.context/memory/records.jsonl`) and the opt-in session
record (`.context/sessions/<id>.jsonl`), the tasks whose verdicts the task ledger
recorded under that id (`.context/tasks/LEDGER.jsonl`, joined to each task's
`task.json` and `result.json`, with the same attested/UNATTESTED status as
`tasks list`), the packet ids that were delivered (does
`.context/packets/<id>.json` still exist, and which tasks were dispatched from it),
and the advisor's counters. It is read-only: nothing is written, no model or
network is used, and no note text or prompt is printed.

Every row names the file and line it came from (`file:LINE`) or the record or task
id. A source that is missing is reported as `missing`; one with a torn or
non-JSON line, a symbolic link, an over-long line or more bytes than `--max-bytes`
(default 8 MiB per file) is reported as `torn`, `damaged`, `unreadable` or
`partial`, the rows that could be read are kept, and `complete` is `false`.
Sections show at most `--limit` rows (default 50) and say how many were not
shown. Two things are joined by convention, not by proof: the evidence ledger's id
comes from the host (`CLAUDE_CODE_SESSION_ID` or the hook input) while memory
and the task ledger use `CONTEXT_LAYER_SESSION` or `--session`, so they meet only
when the same id was used; and the advisor log has no session field, so its rows
are those inside the time window of the session's other records
(`"attributed": false`). A task that was never verified under the id, and was not
dispatched from a delivered packet, cannot be found. Without an id, `show` takes
the session with the newest timestamp and says so on stderr; `session list` prints
the ids found, newest first. The JSON schema is `session-show/v1` (list:
`session-list/v1`).

## Less common flags

Flags that are easy to miss because no walkthrough needs them. Each row is read
from the flag's argparse definition and the code that uses it; `--help` on the
command lists them too.

| Command | Flag | What it does | Default |
| --- | --- | --- | --- |
| `init` | `--max-routes N` | Propose at most `N` routes. Routes are ranked with low-confidence guesses last, then by file count (largest first), then by name; those past the limit are dropped and listed in the report as skipped ("over the --max-routes limit"). Loose files at the vault root fill only the slots that are left. | 8 |
| `route` | `--max-sources N` | The most evidence records the packet may select. Without it the tier that fits the prompt applies (`brief` 18 for a prompt of up to 160 characters, `standard` 22 up to 600, `full` 28 above; `--full` forces `full`); an explicit value replaces the tier's. When the packet holds superseded documents, up to two of the slots are kept for them, labelled. Must be a positive integer. | tier value |
| `route` | `--max-context-chars N` | The characters the packet may inline. A record that would go past it is not dropped: its exact text is written to a file under the run directory and listed in the packet and in `omitted.json`. `0` is accepted and inlines nothing. | tier value (40,000 / 60,000 / 86,000) |
| `route` | `--max-per-source N` | The per-source inline limit. A source, or the section of it that matches, longer than this is not inlined whole: the packet inlines the passage around the prompt's matches (or the exact indexed chunk) and says so in its notes, and may store the complete section under the run directory's `overflow/`. Must be a positive integer. | tier value (6,000 / 9,000 / 24,000) |
| `job new` | `--max-evidence-bytes N` | The largest `evidence.jsonl` that `handback check` will read for this job. It is recorded in `job.md` as `max_evidence_bytes`; a larger file (or a symlink or special file) is reported and not read. Must be a positive integer. | 20 MiB (20,971,520) |
| `job estimate` | `--worker-read-tokens Wk` | What the worker reads. | `W` (`--root-read-tokens`) |
| `job estimate` | `--dispatch-tokens D` | The root's cost of dispatching. | 0 |
| `job estimate` | `--worker-output-tokens O` | What the worker writes back. | 0 |
| `job estimate` | `--output-multiplier m` | Output price divided by input price; an assumption, set it from your own price sheet. | 5 |
| `job estimate` | `--integration-tokens I` | The root's cost of integrating the result. | 0 |
| `job estimate` | `--retry-rate q` | Expected retries per worker run; the worker's cost is multiplied by `1 + q` (`0.5` is one retry in two). | 0 |
| `job estimate` | `--buffer B` | The saving that must be left over, as a share of `W`, before the verdict is `DELEGATE`. | 0.2 |
| `handoff write` | `--blocker TEXT` | Why the work stopped. Required when `--state` is `PARTIAL` or `BLOCKED` (the command refuses without it); printed on the execution line if given with `READY`. | none |
| `handoff write` | `--producer NAME` | Who wrote the handoff, recorded in its identity line. | `worker` |
| `handoff write` | `--consumer NAME` | Who is to read it, recorded there too. | `root` |
| `handoff write` | `--predecessor ID` | A handoff or task this one follows; repeat the flag for several. Recorded as text, not checked. | `none` |
| `rules record` | `--link TEXT` | A pointer to the detail (`[[note]]` or a URL), one line. The rule-file entry's `Detail:` line then reads `[[LOG#^<record id>]], <link>`, and the `LOG.md` entry gets its own `- Detail: <link>` line. Without it the rule-file `Detail:` line holds the `LOG.md` anchor only and `LOG.md` has no `Detail:` line. | none |

The `job estimate` symbols are the ones in the formula in
[subagents.md](subagents.md#1-delegate-or-do-it-yourself).

## Evidence packet status

`evidence-delivery-v1` packets carry one `status`:

| Status | Meaning |
| --- | --- |
| `PARTIAL` | **some evidence was found**; it may not answer the question. The `grep`, `fts` and `synaptic` methods report found evidence this way |
| `SUPPORTED`, `USER_STATED`, `EXTERNAL_RECHECK` | router method only (experimental); see router/README.md |
| `NOT_FOUND` | no evidence was found; not a "no". The packet's `reason` says which case: `no searchable terms` (every word of the prompt is a stopword), `no indexed note matched`, or `matching notes could not be delivered` (see `withheld`) |
| `ERROR` | operational failure: `operation_status: "error"`, `evidence: []`, an `error` message, exit 1 |

`activation.json` `packet.status` uses the same names for the synaptic packet it
describes.

### Withheld sources

A note that matched but changed (or was deleted) after the last `index` is
**withheld on its own**: its text is left out, the rest of the packet is
delivered, and the packet carries a `withheld` list (present only when
something was withheld):

```json
"withheld": [{"source_path": "notes/a.md", "reason": "changed since indexing",
              "next": "context-layer index <vault>"}]
```

`reason` is `changed since indexing`, `deleted since indexing`, `symlink since
indexing` (the note, or a folder on its path, is now a symlink; it is never
followed) or `unreadable since indexing` (for example its permissions changed).
`status` is
computed from the evidence that remains (`PARTIAL`, or `NOT_FOUND` when every
hit was withheld) and the exit code follows the status (0). The withheld slot is
not back-filled with a lower-ranked note, so after `context-layer index` the
packet is the one a fresh index gives. `search` and the hook also print one
stderr line naming the withheld paths and `context-layer index <vault>`; the
hook adds the same line to the context it injects. The default synaptic mode
carries the same `withheld` list for its fts part; passages the link graph
would have added from a changed note are reported separately under
`synapse.graph.withheld_passages_from` (see [synapse.md](synapse.md)).
`packet build` and `tasks new` refuse to pin a packet while a hit is withheld. Index-wide
problems (no index, wrong format, an inconsistent full-text table, index rows
that disagree about a note's hash) remain `ERROR`, and the experimental router
still withholds its whole packet.

### Evidence items and the coverage receipt

Each `grep`, `fts` and `fts-canonical` item (and the fts part of a default
synaptic packet) carries, besides `source_path`, `source_sha256` and `content`:

- `start`, `end`: the byte span of `content` in the note (`raw[start:end]` is
  exactly `content`), `line_start`, `line_end`: its 1-based lines, and
  `source_chars`: the note's length in characters.
- `truncated`: `true` when `content` is shorter than the note.
- `match_in_content`: whether `content` itself holds a query match, decided by
  the same FTS5 tokenizer and MATCH expression as the search (for `grep`: a
  folded substring). `false` means the note matched in a part that was not
  delivered.
- `duplicates` (only when present): other notes whose delivered text is
  byte-identical, as `{source_path, source_sha256}`. That text is delivered
  once; a duplicate takes no slot, and its slot is not back-filled.

What `content` is depends on `--delivery` (0.4): a note that fits `--per-source`
(and what is left of `--budget`) is delivered whole. A longer note is delivered
as **match-anchored windows** (`--delivery window`, the default): the
paragraph-sized blocks in which FTS5, with the index tokenizer, finds query
terms, most distinct terms first, at most 3 windows and `--per-source`
characters per note, blank-line neighbours merged; a note may therefore appear
as several items, each a verbatim span. When no block holds a term (the match
sits in the frontmatter) the note's beginning is delivered, as `--delivery
prefix` always does (the 0.3 behaviour, kept for comparison). `grep`, the
baseline, always delivers prefixes and refuses `--delivery` (exit 2), as do
`--compact` synaptic packets and the router.

Every `grep`, `fts`, `fts-canonical` and `synaptic` packet carries `coverage`,
which says what was searched. It never names a path and stays small:

| Field | Meaning |
| --- | --- |
| `indexed_notes` | Notes in the index this query read. A note excluded after the last `index` is counted here but never searched. |
| `index_sha256` | SHA-256 of the index file this query read, hashed through a handle opened on that same file (a replacement written meanwhile by `context-layer index` is not hashed instead). |
| `query_terms` | The terms sent to FTS5, as the prompt spelled them (at most 64; `query_terms_omitted` counts the rest). |
| `match_expression` | The exact FTS5 MATCH string (the terms, quoted, joined by `OR`), cut at 4,096 characters with `match_expression_truncated: true`; `null` when there are no terms and for `grep`, which matches substrings instead. |
| `skipped_by_reason` | Files the index build skipped, counted by reason (`oversize`, `unreadable`, `unsupported_name`, `not_utf8`); `null` for an index built before skips were recorded. |

Query terms keep the prompt's spelling because FTS5 then folds them exactly as it
folded the notes (`router/textfold.py`): `Straße` finds `Straße`, and
`İzmir`, `İZMİR` and `izmir` find each other. Stopwords and repeated words are
recognised by their folded form (NFKC and case folding), which is never sent to
FTS5. A word spelled two ways that FTS5 keeps apart (`Straße` and `STRASSE`) is
searched in its first spelling only.

`eval/retrieve.py` takes the prompt as its last argument or, to stay clear of
command-line length limits, from `--prompt-file PATH` (UTF-8; `-` reads standard
input). Both at once, or neither, is a usage error (exit 2).

0.2 labelled error packets `PARTIAL`. In 0.3.0 the CLI `search`, the MCP
`search_vault` tool, the hook, shared packets and `router/context_router.py`
say `ERROR`, and so does `eval/retrieve.py` run directly.

## First run

Every search path (CLI `search`, MCP `search_vault`, the hook, `packet build`)
checks, before it reads a note:

1. `.context/routes.json` exists → otherwise: run `context-layer init <vault>`.
2. It is a valid config (see below) → otherwise: the problem, and how to fix it.
3. `.context/index.sqlite` exists, has a format this version reads, and its
   full-text table agrees with the stored records → otherwise: run
   `context-layer index <vault>`.
4. For `--method synaptic`, a `graph.sqlite` that cannot be read (damaged, or a
   newer layout) is not an error: the packet is built without it, with
   `synapse.decision: "graph_unreadable"` and a note naming `context-layer index <vault>`.

Each failure is one line naming the next command, never a raw `Errno` or SQLite
message.

## Output and paths

- stdout carries the result (a packet, a report, JSON-RPC); stderr carries
  diagnostics.
- Messages name vault-relative paths (`.context/index.sqlite`, `notes/a.md`), not
  absolute ones; `init` reports `Scanned .` and `Wrote .context/routes.json`
  (a `--out` outside the vault is shown by its file name). The command a wrapper
  forwards (with the absolute interpreter,
  script and vault paths, and the prompt) is echoed to stderr only with
  `--verbose` (accepted before or after the subcommand).
- Host config diffs from `install` name the files they change, which are
  absolute paths by nature.

## `routes.json`: one strict loader

Every entry point that reads `.context/routes.json` (index, search, route, MCP,
hook, status, tasks, shared packets, memory) uses one loader
(`router/source_policy.load_config`). It refuses, with a message naming the
problem, instead of reading the file as "no exclusions":

- invalid JSON, or a top level that is not an object;
- a key repeated anywhere in the file (JSON parsers disagree on which one wins);
- `exclude_prefixes` / `retrieval_exclude_prefixes` that are not a list of
  non-empty, vault-relative strings (a bare string would otherwise be read one
  character at a time);
- an exclusion entry with blanks around it or around one of its components, or
  with an empty component (`"private/ "`, `" private"`, `"a//b"`): each would
  exclude nothing while looking like a rule, so the message names the entry and
  the spelling to write instead;
- `max_file_bytes` that is not a whole number from 1 to 50,000,000 (the default
  is 2,000,000);
- a `schema_version` newer than this version reads.

`init` never writes an entry this loader would refuse: a folder it would exclude
whose name cannot be written (a `:` in a top-level name, a backslash, blanks
around the name) is listed under `Detected as noise but not written` with the
reason, and in the file's `_exclusions_not_written` note.

A missing `routes.json` means "no exclusions" for `index` and `status`; search
needs one (run `init`). `index` says so in one stderr line naming
`context-layer init <vault>` and still exits 0.

## Format versions

| Artifact | Where the version is | Current | Missing means | Newer than current |
| --- | --- | --- | --- | --- |
| `.context/routes.json` | `schema_version` | 1 | 1 (legacy) | refused: upgrade context-layer |
| `.context/index.sqlite` | `PRAGMA user_version`, and `format_version` in `index_meta` | 1 | 1 (an index from 0.2) | refused: upgrade, or rebuild with `context-layer index` |
| `.context/graph.sqlite` | `schema_version` in `graph_meta` | 1 | 1 | not used: the synaptic packet degrades to the fts packet with `decision: "graph_unreadable"` |
| `.context/memory/records.jsonl` | `format_version` per record | 2 (format 1 records carry no key and stay readable) | 1 | refused: upgrade context-layer |
| `.context/jev.json` | `schema_version` | 1 | the advisor is off | the advisor behaves as off (`config_invalid`) |
| `.context/jev-calls.jsonl`, `.context/jev-cache/*.json` | `v` per row / entry | 1 | — | the row or entry is ignored |
| Jev recording (JSONL, written by `evaluate(..., capture=...)` and read by the `recorded` provider) | `contract` in every row | `jev-recording/v1` | the file is refused (`recording_invalid`) | the file is refused (`recording_invalid`) |
| `.context/task-pins/<id>.json` | `schema` (`context-layer-task-pin-v1`) | v1 | the pin is malformed | the task is blocked: upgrade context-layer |
| `.context/activation.json` | `version` (the additive `jev` object and per-node `jev` labels keep it at 1) | 1 | ignored by the Obsidian plugin | ignored by the plugin |

## Index integrity

`index` stamps the format version and runs SQLite FTS5's own
`integrity-check` on the staged index before it replaces the live one.
Readers check the format and compare the number of stored records with the
number of full-text rows on every open, so an emptied or dropped full-text table
is an error rather than a clean `NOT_FOUND`. `status` additionally runs the FTS5
integrity check on an in-memory copy, which also catches a full-text row that no
longer matches its record's text; the per-search check does not.

## GitHub context (opt-in)

`github-context VAULT --prompt TEXT [--source ID ...]` fetches configured public
files pinned to full commit SHAs. `search --github` uses the same reader only
after clean local NOT_FOUND; `--no-github` overrides it. Both require an enabled
`.context/github.json`. No prompt or note is sent to GitHub. The standalone
command exits 1 on ERROR, 0 otherwise; fallback keeps the local result and exit
status with external failures visible in `external_context`.
See [github-context.md](github-context.md) for configuration, bounds, evidence
fields and failure semantics.
