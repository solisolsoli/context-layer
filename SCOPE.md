# Scope, support matrix and acceptance

## Unreleased addition

Optional GitHub context adds commit-pinned, allowlisted public evidence through
the CLI and a tenth MCP tool. It is off by default and preserves local search
status and evidence; [docs/github-context.md](docs/github-context.md) defines
its bounds. The [hosted run for `db9b8ac`](https://github.com/solisolsoli/context-layer/actions/runs/36732194741)
passed all 11 required jobs. The exploratory Windows job failed on pre-existing
platform assumptions; Windows remains out of scope. This run does not establish
live host integration or model answer quality.

## 0.4 scope (2026-09-29)

0.4 keeps the 0.3 scope below and adds: an optional, default-off advisor (Jev,
[docs/jev.md](docs/jev.md)) that removes no evidence unless the named lossy
lever is set;
incremental indexing and opt-in note-name fields; usage-derived link
suggestions (`graph suggest`); the session brief and an opt-in delivery ledger
with a citation check; `check_claims` as the ninth MCP tool; and the fixes the
0.3 audit and a later independent audit found (CHANGELOG.md lists them). The
same definition of done applies. The support matrix below is the current one;
a live host and a live model have not been run for this release candidate.
The hosted CI result is recorded above and in README.md, Support.

## 0.3 scope (2026-09-24)

0.3 adds: opt-in synaptic retrieval over explicit links (FTS superset by
construction), the activation trace and the Obsidian Brain View, the starter
brain and enforceable rule files (`CLAUDE.md` = `AGENTS.md`, plan-first,
stop-and-ask, record every step), lean sub-agent jobs with mechanical
hand-back checks, a sealed offline benchmark, and the hardening listed in
CHANGELOG.md. Done means: the component's tests pass in `make test` or
`make plugin-test`, its limits are written in its doc, and any number in the
README comes from a command in this repository. The 0.2 text below is kept
as written; its principles still apply.

## 0.2 target (as written)

Written 2026-09-21 before any 0.2 code. The 0.1.0 alpha delivers local
`init/index/search/route/eval`. 0.2 adds what a person needs after installing:
a connected AI host that receives verbatim source evidence without pasting,
shared memory that survives sessions and tools, bounded sub-agent tasks that a
coordinator verifies, visible source health, and a measured before/after.
Nothing below is a claim of achieved results; each phase lists what counts as done.

## Design principles (user direction, 2026-09-21)

The stated purpose is an original design that gives every user of the brain
freedom. Concretely, every 0.2 component must satisfy:

1. **Local and plain.** State lives in the vault as JSONL/JSON/Markdown a person
   can open, diff, edit and delete without this tool. No database a person cannot
   read, no cloud, no model API call in the core. The optional advisor
   ([docs/jev.md](docs/jev.md)) is the only component that can call a model; it is
   off by default, sends only what its gates allow, and without a configuration
   its provider client is never loaded.
2. **Host-agnostic.** Any MCP stdio client can connect; nothing depends on one
   vendor's private feature. Host-specific installers are conveniences over a
   generic printed config, never the only path.
3. **Reversible.** Every write has a dry-run, a backup and an `uninstall`/
   `rollback`. Removing the package leaves the vault usable and readable.
4. **Verifiable.** Evidence carries path + SHA-256; memory carries source hashes;
   sub-agent results stay unverified until a coordinator checks them; measurements
   ship with their inputs so anyone can rerun them.
5. **Bounded.** Agents receive the passages a task needs, not the vault; budgets,
   allowed paths and retries are explicit and enforced.
6. **Readable inside the vault.** Memory and task state are plain JSON Lines a person
   can read, with Markdown views: `MEMORY.md` and `TASKS.md` in `.context/`, regenerated
   on every write (Obsidian does not show dot folders), and, opt-in,
   `context-layer memory mirror --notes <folder>`, which writes decisions and tasks as
   ordinary notes and keeps that folder out of retrieval by default, so text an agent
   wrote is never served as evidence.
7. **Honest.** Unverified platforms, small samples and heuristics are labelled as
   such in the interface and docs.

## Support matrix

| Area | Supported (tested) | Expected but unverified | Out of scope |
| --- | --- | --- | --- |
| OS | macOS 14+; Ubuntu (hosted CI) | other Linux distributions | Windows |
| Python | 3.10–3.13 on Ubuntu; 3.12 on macOS | other OS/Python combinations | <3.10 |
| Vault | Folder of UTF-8 `.md` files (Obsidian or plain) | — | symlinked sources, binary notes |
| AI host | Claude Code 2.1+ via MCP stdio and `UserPromptSubmit` hook | Codex CLI via MCP stdio config (CLI absent on the reference machine) | any host without MCP or hooks |
| Obsidian | 1.13.7 desktop (live render checked on the fictional vault) | other 1.x desktop | mobile |
| Sub-agent backend | the fake backend, driven by the unit suite. `claude -p` was run live once by the coordinator on 2026-09-21 (haiku, verified task), with the command line of that release; the 0.4 command line is unverified here ([docs/tasks.md](docs/tasks.md)) | `claude -p` as 0.4 builds it, `codex exec` | hosted agent APIs |

"Expected but unverified" is stated as such in user-facing docs. No automatic
compatibility is assumed for any host not listed.

## End-to-end scenario (the acceptance walk)

1. Install the package in a clean environment; `init`, `index` a vault.
2. Connect one supported host with one command; the host lists the tools.
3. Ask a question in the host; the answer cites source path + SHA-256 from the
   vault without the person pasting anything. An access or index error is visible.
4. Record a decision with its sources from that host (memory).
5. In a new session or the other tool, `resume` returns that decision with its
   sources, flagging any source whose hash has changed since.
6. Dispatch a bounded sub-agent task (allowed sources, allowed output dir,
   model, budget, retries). The task receives an evidence packet, not the vault.
7. The coordinator verifies the result; unauthorized file changes are detected;
   a failed task is never shown as done; cost of coordinator + agents + retries is
   summed.
8. `status` shows index age, changed/deleted/moved sources, exclusions, warnings.
9. A held-out question set written before step 3 compares baseline (host alone)
   and candidate (host + context layer) on quality, total tokens and wall time.

## Acceptance criteria

- **A2 host integration.** In a fresh temp HOME/vault a headless host call
  returns evidence with correct path and hash from the MCP tool or hook. Install
  is dry-run by default, backs up before writing, is reversible with `uninstall`,
  and never edits unrelated settings. Any helper error surfaces as a non-zero
  exit or an explicit error message, never as empty success.
- **A3 shared memory.** Append-only records with id, timestamp, tool, session,
  kind, state (draft/approved/published), text and source hashes. Re-running
  the same record does not duplicate it. Concurrent appends do not lose data
  (lock). `resume` marks records whose sources changed as STALE.
- **A4 sub-agent tasks.** Task spec limits sources, output directory, model,
  budget, retries and concurrency. Result state is `pending_review` until
  `verify` runs; `verify` fails on unauthorized writes or missing outputs.
  A backend that is absent yields `blocked`, not `done`. Cost of every attempt
  is recorded and summed.
- **A5 source lifecycle.** `status` reports stale index, changed, deleted and
  moved sources, exclusions and symlinks; stale evidence is withheld with a
  visible reason; previous index can be restored. Upstream cache/hook fixes stay
  lab patches with counter-example tests; no live promotion here.
- **A6 measurement.** Held-out cases are written before candidate runs and are
  not the 24 synthetic development cases. Report shows per-case source
  delivery, coordinator-judged correctness, total tokens (coordinator + agents +
  retries, from host usage JSON), and wall time for both arms. Small N is
  reported as a signal, not proof.
- **A7 sharing.** Inventory of every distributed component with license and
  notice; no personal paths, accounts or vault content; install, uninstall and
  upgrade verified in a clean vault; only tested platforms listed as supported.

## Measurement definitions

- **Quality**: required source delivered (path + hash) and coordinator-judged
  correctness of the final answer against the held-out expectation.
- **Total tokens**: input + output (+ cache read/write) summed over coordinator,
  every sub-agent attempt and every retry, taken from the host's usage output.
- **Time**: wall-clock per case from dispatch to verified result.
- **Baseline**: same host, same model, same question, no context layer (the
  host uses its own file tools on the same vault copy).
