# Bounded sub-agent tasks (`context-layer tasks`)

A task sends one goal and a bounded evidence packet to a local agent command,
records what every attempt cost, and keeps the result **unverified** until a
coordinator checks it. This tool hands the agent only the packet; the agent
process can still read the vault, because it runs as your user (see "What the
agent sees" and Limits).

Everything a task owns is a plain file inside the vault, so it can be read,
diffed and deleted without this tool:

```
<vault>/.context/tasks/<task-id>/task.json     the spec, with the output directory's identity
                                  packet.json   the evidence handed to the agent
                                  prompt.txt    exactly what the agent was sent
                                  runner.json   the claim: which runner ran it, its child, heartbeat
                                  attempts/1.json 1.stdout 1.stderr
                                  result.json   the state machine
                                  out/          default allowed output directory
                                  cancel        present once cancel was asked for
<vault>/.context/tasks/TASKS.md                 derived Markdown mirror
<vault>/.context/tasks/LEDGER.jsonl             hash-chained verification ledger
<vault>/.context/tasks/.leases/                 output-directory leases held during `run`
<vault>/.context/task-pins/<task-id>.json       dispatch pin: the definition and its hashes
```

`TASKS.md` is regenerated on every state change and starts with a "do not edit"
header. It is a view of `result.json`, never a second source of truth, and it
does not check the verification ledger: `tasks list` and `tasks show` do.

## Lifecycle

```
new  ->  queued  --run-->  running  -->  pending_review  --verify-->  verified
                                    |                            \-->  rejected
                                    +-->  failed      (every attempt failed, or the runner died)
                                    +-->  blocked     (the backend never started)
                                    +-->  cancelled   (cancel flag, or cancel after the runner died)
```

- Only `tasks verify` can produce `verified`. A finished run stops at
  `pending_review`; nothing in `run` claims success.
- `verify` refuses any state but `pending_review`, and `verified`/`rejected` are
  terminal. To re-run work after a rejection, create a new task.
- Retries happen **only on failure** — a non-zero exit, a timeout, or
  `is_error: true` in the backend's JSON — up to `--max-attempts`. Each attempt
  is its own costed record; none of them is discarded. A claude run that stopped
  on its own turn or spend limit (`error_max_turns`, `error_max_budget_usd`) is
  not retried: the same limit would stop it again.
- An unauthorized write does not stop the run and does not trigger a retry. It
  is recorded, and `verify` rejects on it.

Exit codes: `run` returns 1 if any task it ran ended anywhere but
`pending_review`, or if it ran none; `verify` returns 0 for verified, 1 for
rejected, 2 when the task is not pending.

## Spec

```
context-layer tasks new VAULT --goal TEXT [--source GLOB]... [--output-dir REL]
    [--backend fake|claude|codex|cmd] [--model M] [--max-turns N] [--max-cost-usd USD]
    [--host-context inherit|safe-mode|bare]
    [--max-attempts N] [--timeout S] [--cmd TEMPLATE]
    [--top-k K] [--budget CHARS] [--per-source CHARS] [--json]
```

| Field | Meaning |
| --- | --- |
| `goal` | What the agent must produce. It is the search prompt as well. |
| `sources` | Vault-relative globs, repeatable. Each must resolve inside the vault, is filtered through `.context/routes.json` exclusions, and is never followed through a symlink. Omitting them means "any indexed source", still minus the exclusions. |
| `output_dir` | Vault-relative directory the agent may write to. Defaults to the task's own `out/`. See "The output directory" below. |
| `backend`, `model`, `cmd` | Which command runs. |
| `max_turns` | `claude` only: passed as `--max-turns`. |
| `max_cost_usd` | A spend cap for the whole task, in host-estimated USD. `claude` receives what is left of it as `--max-budget-usd` on each attempt; for every backend the runner stops retrying once the recorded cost reaches it. |
| `host_context` | `claude` backend only: `inherit` (default), `safe-mode` or `bare`. See "What the agent sees" below. |
| `max_attempts`, `timeout_s` | Retries and the per-attempt wall-clock limit. |
| `bounds` | `top_k`, `budget` (evidence characters) and `per_source` for the packet. |

The task id is a short UTC timestamp plus six hex characters, e.g.
`20260921T202841Z-c43b23`.

### The output directory

- It may not sit in a hidden folder or in the tool's own state, in any letter
  case: `new` applies the retrieval exclusion rules (any path part starting with
  a dot, and `.git`, `.obsidian`, `node_modules` and the like), so
  `.context/...`, `.CONTEXT/...` and `.context-RUNS/...` are all refused. The one
  exception is a support job's own `.context/jobs/<task>/attempt-NNN/out`.
- It may not escape the vault, pass through a symlink, or name an existing file.
- It may not equal, contain or sit inside the output directory of another task
  that is still `queued`, `running` or `pending_review` (compared case- and
  NFC-folded, as a case-insensitive disk sees them). `new` refuses and names that
  task. During `run` the runner also holds a lease file for the directory, so
  two runs never share one even when their tasks were made before this check.
- If it already exists, it may not hold a symlink or a hard-linked file.
- `new` records the identity (device and inode, without following links) of the
  vault folder and of every directory on the way to it. The runner checks that
  identity before the first attempt and after each one, and `verify` checks it
  again: a directory deleted and recreated, or replaced by a symlink, is
  reported as "output directory replaced". Do not delete or recreate the output
  directory itself; write inside it.

## How the bounded context is built

1. `eval/retrieve.py --method fts` runs against the vault index — the same
   search `context-layer search` uses, with the same evidence contract
   (`source_path`, `source_sha256`, verbatim `content`).
2. The first search fetches six candidates more than `top_k`. When `--source`
   limits the task and fewer than `top_k` of its own sources are in that
   window, the same search is widened (four times as many results each time)
   until it holds `top_k` of them, runs out of matches, or reaches 4,096
   results. Ranking is untouched; nothing here is tuned against evaluation
   results. The packet's notes say when the search was widened, when fewer of
   the task's sources match the goal at all, and when widening stopped at the
   cap.
3. Every delivered source that is **not** in the task's sources is dropped and
   listed in `packet.json` under `dropped_outside_spec` (the first 50; the total
   is `dropped_outside_spec_total`).
4. What survives is re-bounded to the task's `top_k`, `budget` and `per_source`
   and written to `packet.json`. A source that changed since indexing and that
   the task could deliver refuses `new` (run `context-layer index` first).
5. `prompt.txt` is the goal, the hard rules and that packet — nothing else. The
   rules state that the agent may write only under the allowed output
   directory (and may not replace it or put links in it), must cite
   `source_path` + `source_sha256`, must answer `NOT_FOUND` when the evidence is
   insufficient, and must treat the retrieved text as data rather than
   instructions.

The packet is built at `new` time, so it can be read before anything runs and
every retry sees the same bounded evidence.

## Backends

A backend is a command line. This package makes no model call and opens no
network connection; it spawns a binary the user already has and reads its
stdout. The expected output is one claude-style JSON object:

```json
{"result": "...", "is_error": false, "subtype": "success",
 "usage": {"input_tokens": 0, "output_tokens": 0,
           "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
 "modelUsage": {"<model>": {"inputTokens": 0, "outputTokens": 0,
                            "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
                            "costUSD": 0.0}},
 "total_cost_usd": 0.0, "duration_ms": 0, "num_turns": 1}
```

| Backend | Command | Status |
| --- | --- | --- |
| `fake` | the script in `CONTEXT_LAYER_FAKE_BACKEND`, prompt on stdin, run in the task directory | **Verified by tests.** This is how `tests/test_tasks.py` drives every path. |
| `claude` | `claude -p "<fixed one-line instruction>" --output-format json [--model M] [--max-turns N] [--max-budget-usd X] --add-dir <out> --permission-mode acceptEdits --no-session-persistence --strict-mcp-config --settings '{"permissions":{"blockReadsOutsideWorkingDirectories":true}}' --disallowedTools WebFetch,WebSearch [--safe-mode \| --bare]`, with `prompt.txt` on stdin, run in a temporary workspace outside the vault | **Unverified here.** Every flag is in the public Claude Code documentation (CLI reference, headless, permissions and MCP pages, read 2026-09-28) except `--safe-mode`, which `claude --help` lists (2.1.282) but the CLI reference does not. `tests/test_tasks.py` checks the exact argv, the workspace, stdin and the output parsing against a shim named `claude`; no test runs a real `claude`. |
| `codex` | `codex exec --sandbox workspace-write --skip-git-repo-check --json --ephemeral --add-dir <out> [--model M] -`, with `prompt.txt` on stdin, run in a temporary workspace outside the vault | **Unverified: never run here.** Written from the public Codex documentation (non-interactive mode and the CLI reference, read 2026-09-28): `exec` is read-only by default, needs a Git repository unless `--skip-git-repo-check`, reads the prompt from stdin with `-`, and `--json` prints JSON Lines whose `turn.completed` event carries the usage. The argv and the usage parsing are checked against a shim named `codex`. |
| `cmd` | the user's `--cmd` template, with `{prompt_file}` and `{out_dir}` substituted, run in the task directory | Template handling and the missing-binary path are tested; the agent itself is whatever the user supplies. |

### What the agent sees

The packet is the only vault evidence this tool hands the agent, and the
prompt says so. It is **not** everything the agent sees, and it is not a fence
around the vault:

- The `claude` and `codex` children start in a fresh temporary directory
  outside the vault that holds only copies of `prompt.txt` and `packet.json`,
  and is deleted after the attempt. So the vault's own `CLAUDE.md` is not a
  parent of the working directory. The output directory is added with
  `--add-dir`, which also makes it readable.
- For `claude`, the settings flag turns on
  `permissions.blockReadsOutsideWorkingDirectories`, which the permissions page
  describes as making the file tools (and the built-in read-only Bash commands
  such as `cat`, `grep` and `find`) refuse paths outside the working directories.
  `WebFetch` and `WebSearch` are removed. These are Claude Code's behaviours as
  documented, not something this package can check.
- With the default `--host-context inherit`, Claude Code still loads the user's
  `~/.claude` settings, `CLAUDE.md` and hooks.
  - `--host-context bare` adds `claude --bare` (documented): hooks, skills,
    plugins, MCP servers and CLAUDE.md discovery are skipped, but authentication
    must come from `ANTHROPIC_API_KEY` or an `apiKeyHelper` in `--settings`;
    OAuth/keychain login is not read.
  - `--host-context safe-mode` adds `claude --safe-mode`, which `claude --help`
    lists but the CLI reference does not document; its effect is unverified.
- `fake` and `cmd` children run in the task directory inside the vault.
- Every child gets `CONTEXT_LAYER_OUT_DIR` (the output directory) and
  `CONTEXT_LAYER_TASK_DIR` (its working directory) in its environment. The vault
  path is not exported by name, but it can be derived from those paths; hiding
  it is not a protection.
- None of this is a sandbox (see Limits). An agent that runs shell commands as
  your user can read and write what your user can.

Notes worth knowing before trusting a backend:

- `--max-turns` is documented in the Claude Code CLI reference: the run exits
  with an error at the limit (`error_max_turns`). Such an attempt is not
  retried. The same holds for `--max-budget-usd` (`error_max_budget_usd`).
- No backend takes the prompt as an argument any more: `claude` and `codex`
  read it on stdin (the `claude` argument is a fixed one-line instruction), so
  a large packet cannot hit the operating system's argument limit and the
  evidence does not show in the process list. Claude Code caps piped stdin at
  10 MB.
- Output that is not the JSON object above is kept as raw text, `usage` is
  `null`, and the attempt carries a visible `usage unknown` note. Those attempts
  are counted separately in `tasks cost` rather than silently treated as free.
- A missing binary means the attempt never starts: the task becomes `blocked`
  with the reason, never `done` and never `pending_review`.

## Verification

`context-layer tasks verify VAULT ID [--record]` (or `ID` alone with `--vault`
or `CONTEXT_LAYER_VAULT`) rejects when:

- the task definition changed after dispatch: `task.json`, `packet.json` or
  `prompt.txt` no longer matches the SHA-256 recorded in its dispatch pin, or
  the pin is missing, malformed or inconsistent with itself; or
- the output directory is not the one recorded at `new` (replaced, turned into
  a symlink, removed), or holds a symlink (to a file or a directory), a file
  with more than one hard link, or anything that is not a regular file; or
- any attempt created, changed or deleted a file outside the allowed output
  directory — every path is listed, and that includes the pin itself; or
- no file in the allowed output directory was **created or modified during the
  run** (files that were already there and left untouched never count); or
- a file the run produced was changed or removed after the run; or
- for a support job: the job file changed, a handback record fails its check,
  or the run modified a file that is an indexed source (or would be indexed:
  outside the tool's state), since a job says `may_modify_source: false`.

Otherwise it records `verified` with each produced file's path, SHA-256 and
whether it was `created` or `modified`. Files that were in the output directory
before the run and are gone after it are listed as removed. Rejection
**reports**; it never reverts or deletes what the agent wrote. Every verdict is
also a line in the verification ledger (below).

### Dispatch pin

`tasks new` writes `.context/task-pins/<id>.json`: a copy of the task
definition (including the output directory's identity), the SHA-256 of
`task.json`, `packet.json` and `prompt.txt`, and a SHA-256 over the canonical
definition plus those three hashes. `run` and `verify` read the output
directory, the attempts, the timeout and the backend from the **pin**, never
from a re-read of `task.json`, and refuse (`blocked` before a run, `rejected` at
verify) when the files no longer match it.

The pin lives outside the task workspace on purpose. The task directory is
excluded from the change manifest; the pin directory is not, so an attempt that
edits a pin is recorded as an unauthorized change. A pin *created* while an
attempt runs is another `tasks new`, not the attempt's doing, and is not
reported. Tasks created before 0.3.0 have no pin, and tasks created before 0.4
have no output-directory identity; neither can be verified; re-create them.

### What counts as output

Before the first attempt the runner hashes every file under the allowed output
directory, walking it without following any link. After each attempt it
records, in `result.json` under `produced`, the files that are new or whose hash
changed against that baseline, and under `removed_from_output` the baseline
files that are gone. `verify` counts only the `produced` entries, and only while
each still has the hash recorded at the end of the run and the directory is
still the one recorded at `new`. Pointing `--output-dir` at a directory that
already holds notes is allowed, but those notes never count as the agent's
work: an untouched note is not in `produced`, a symlink or hard link cannot
smuggle one in, and no other unfinished task may write into that directory.

### Unauthorized-write detection

Before and after every attempt the runner takes a SHA-256 manifest of the
vault, without following links (a symlink, to a file or a directory, is
recorded as its target), and diffs it. Excluded from the manifest is the tool's
own derived state, which other commands rewrite while a task runs: the index
and the link graph (`.context/index*`, `.context/graph.sqlite*`, rewritten by
`context-layer index`), `.context-runs`, the activation trace, shared packets,
the memory store's lock, mirror and staging files, and the whole
`.context/tasks/` area — the last as a whole, so tasks running side by side do
not report each other's bookkeeping.

Three kinds of change in the window are recorded rather than reported:

- a pure append to `.context/memory/records.jsonl` (for example the coordinator
  running `memory add`): the appended records are listed with their `tool` and
  `session` under `memory_appends`; a rewrite or truncation of that file is
  still an unauthorized change;
- a write inside the output directory of another task run by the same
  `tasks run --jobs N`: listed under `concurrent_task_writes` with that task's
  id, and named in the verification notes;
- a newly *created* dispatch pin or job file (`tasks new`, `job new`).

This costs one full hash of the watched files per attempt, twice, so a very
large vault makes attempts slower.

### `--record`

`verify --record` appends the outcome to shared memory through `memory.record`
with `kind="result"`, `tool="tasks"`, `state="draft"`, the packet's evidence
sources and the output files' hashes, and a text that starts with the task id
(`task <id> verified: ...`) and cites the verdict's ledger line. A `record`
ledger line then cites the memory record. If memory refuses, the failure
becomes a visible note on the verification record; it never crashes `verify`.

## Verification ledger

`.context/tasks/LEDGER.jsonl` is append-only: one ASCII JSON object per line,
written under a lock, each carrying `n` (its line number) and `prev` (the
SHA-256 of the previous line's bytes). `verify` appends a `verify` line with the
task id, the pin's definition hash, the packet id and the SHA-256 of
`packet.json`, the produced files and their hashes, the SHA-256 of the handback
check (job tasks) and of the problem list, the verdict, the tool and session
(`CONTEXT_LAYER_TOOL`, `CONTEXT_LAYER_SESSION`) and the time.
`handback check --record` appends a `handback` line (evidence hash, seed,
sampled ids, verdict); `verify --record` a `record` line.

- `tasks list` and `tasks show` mark a `verified` or `rejected` state as
  **UNATTESTED** when no ledger line records that verdict for the task, when the
  latest line says otherwise, or when the chain is broken. A `result.json`
  rewritten by some other process (for example an agent's leftover child) shows
  up this way.
- `context-layer tasks ledger VAULT [--replay] [--json]` checks the chain and
  prints the head. `--replay` recomputes each task's latest verdict from the
  current files and compares it (verdict, produced files, definition) with its
  ledger line; exit 1 when the chain is broken or a verdict no longer
  reproduces. It also lists every `verified` or `rejected` task that the chain
  does not back (no ledger line for its verdict, a different last verdict, or a
  `result.json` that cites another ledger line; the `unattested` list in
  `--json`) and exits 1 for it, which catches an edited head line or a cut tail
  that the hash chain alone cannot see.
- `handoff write` prints the head line and writes it into the handoff file.

The same user can rewrite the ledger too: the chain makes an edit visible, not
impossible. To anchor it, keep the head hash somewhere the agent cannot rewrite
(commit it, or keep the handoff file elsewhere).

## Concurrency, claims and dead runners

- `run` takes each task by creating `runner.json` exclusively (runner pid, a
  hash of the host name, start time, heartbeat, the attempt and its child's
  process group). Exactly one process ever runs a task: a repeated id runs once,
  and two `tasks run` processes over the same vault never run the same task
  twice (the second skips it and says who holds it). `attempts/<n>.json` is
  created exclusively as well.
- `--jobs N` runs up to N tasks at once in one process; their output
  directories cannot overlap (see "The output directory").
- A runner updates its heartbeat about once a second during an attempt.
- `context-layer tasks cancel VAULT ID` writes a `cancel` flag. A queued task is
  claimed and becomes `cancelled` at once. For a running task, a live runner
  stops the child and records `cancelled`; if the runner is gone (killed), cancel
  stops the recorded child process group itself, counts the interrupted attempt
  and ends the task `cancelled` with the reason.
- `context-layer tasks recover VAULT [ID...] [--json]` checks every queued or
  running task that a runner claimed: a runner on this host that is no longer
  alive (`kill(pid, 0)`) has its child process group stopped (SIGTERM, then
  SIGKILL), its interrupted attempt counted from whatever its stdout holds, and
  the task ends `failed` (or `cancelled` if cancel was asked) with the reason. A
  live runner, or one on another host, is left alone.
- To run the work again, create a new task.

## Cost accounting

`context-layer tasks cost VAULT [ID] [--coordinator-usage FILE] [--json]` sums,
across **every attempt including retries**: input, output, cache-creation and
cache-read tokens, the cost, and wall-clock seconds, per task and in total.

- The cost is **host-estimated USD**: `total_cost_usd` and `costUSD` are the
  host's client-side estimates, not billing data.
- The token columns come from `usage`, which for `claude` covers the top-level
  agent loop only. When the host reports `modelUsage` (every model call it
  counted, subagents included), `cost` prints those per-model totals as well.
- A counter that is missing, negative, non-finite or not a whole number makes
  that attempt's usage unknown, never zero; a cost that is not a finite,
  non-negative number is not counted. Both are stated
  (`N attempts reported no usage`), not quietly dropped. `is_error` counts only
  when it is literally `true`.
- A task whose files cannot be read is named on stderr, listed as `unreadable`
  by `list`, and makes `cost` exit 1, because its total would be incomplete.

`--coordinator-usage FILE` takes the host's own usage JSON — one object or a
list of them, with either a nested `usage` object or the token keys at the top
level, plus optional `total_cost_usd`, `wall_s` or `duration_ms`, and
`modelUsage` — and adds it as a separate `coordinator` line so the total covers
coordinator + agents + retries. An entry with no token counts is counted as
usage unknown; a number that is not a finite, non-negative value is an error
(exit 1, no traceback).

## Timeouts and limits a task carries

A timeout kills the child's process group (SIGTERM, then SIGKILL) and records
the attempt as a failure with `timed_out: true`. A support job's
`max_elapsed_seconds` becomes the task timeout when it is the smaller, and also
bounds the time of all attempts together. Its `max_total_usage_tokens` is
recorded and checked between attempts (no retry once reached); no backend
enforces a token cap, so one attempt can go past it.

## Limits

- **Detection, not prevention. This is not an operating-system sandbox.** A
  backend runs as the same user as this tool and can read and write anything
  that user can. The allowed output directory is stated in the prompt and
  checked afterwards by hashing; an agent that ignores the rule is reported, not
  stopped. Writes outside the vault are not seen at all, and neither is a change
  undone before the attempt ends (a file written and deleted, or edited and
  restored, inside one attempt). Run an untrusted agent inside a real sandbox (a
  container, a separate user) if it must not be able to read or write.
- What is enforced rather than detected: the packet's bounds, the source
  filter, the output-directory rules at `new`, one run per task, the attempt
  count, the per-attempt timeout, the job time limit, and (for `claude`) the
  flags passed to the host. Spend and token caps other than claude's
  `--max-budget-usd` are checked between attempts only.
- The threat boundary of `verify` is **the attempt window**. The runner holds
  the pre-attempt manifest, the output baseline and the result record in memory
  and writes them after each attempt, so edits the backend makes during its run
  to `task.json`, `packet.json`, `prompt.txt`, the pin or `result.json` are
  detected or overwritten. A process left behind after the attempt that
  rewrites `result.json` shows as UNATTESTED; one that also forges a consistent
  ledger line is caught only by a head hash kept outside the vault.
- Writes inside the excluded task area (`.context/tasks/`) are not reported as
  unauthorized changes; the pinned files in it are checked by hash instead.
- A change made by something else on the machine during the attempt window is
  attributed to the attempt, except the derived tool state, memory appends and
  sibling tasks' output directories listed above. A sibling task in the same
  run that writes into *this* task's directory is indistinguishable from this
  task's own writes; run tasks one at a time when that matters.
- The output directory's identity is a device and inode number: a vault copied
  or restored to another disk reports every unfinished task's directory as
  replaced.
- What a task leaves on disk (prompts with verbatim evidence, the agent's full
  output) and how to delete it is listed in [privacy.md](privacy.md).
- The evidence packet uses the FTS baseline. A keyword search cannot prove that
  an answer is absent, which is why the rules ask for `NOT_FOUND` rather than
  treating an empty packet as an answer.
- `verified` means the coordinator's mechanical checks passed: the definition
  is the one dispatched, the run produced output in the allowed directory, and
  nothing was written where it should not have been. It is not a judgement that
  the answer is correct.
