# Context Layer

[![tests](https://github.com/solisolsoli/context-layer/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/solisolsoli/context-layer/actions/workflows/tests.yml)

**A local, evidence-first layer between your Obsidian (or plain Markdown) vault and
your AI agents.** It hands the agent verbatim passages pinned to a file path and a
SHA-256, follows your own `[[links]]` when a question spans several notes, shows
you in Obsidian which notes and links the last retrieval used, and keeps the
agent's work recorded, planned and checkable.

Python 3.10+ with SQLite FTS5, standard library only. No network by default. GitHub
context and the model advisor are separate opt-ins. No database you cannot open. MIT.

The package version is **0.4.0**. The latest checkout also includes the GitHub
source/cache tools and Windows support documented under
[Unreleased](CHANGELOG.md#unreleased). Install from this repository; these
checkout additions are not a separate PyPI release.

**Maintenance:** this repository is maintained only by its owner. Outside pull
requests, issues and discussions are closed for now. Use and fork the code
under the [MIT license](LICENSE); report vulnerabilities privately through
[SECURITY.md](SECURITY.md). See [CONTRIBUTING.md](CONTRIBUTING.md) for the policy.

<p align="center">
  <img src="docs/images/brain-view.png" width="520"
       alt="Context Layer Brain View: a 3D sphere of a vault's explicit link graph; each point is a note, each line a resolved link, brighter where a note has more links">
</p>
<p align="center"><sub>The Brain View look: a large vault's link graph at rest, each point a note and
each line a resolved link; colour and size follow how many notes a note links to.</sub></p>

> **Read before connecting an agent.** Retrieved notes are data, but a note can
> contain text that looks like instructions (prompt injection). Evidence is
> fenced and labelled, which reduces the risk; it does not remove it. This tool
> is not an operating-system sandbox. See [SECURITY.md](SECURITY.md) and
> [docs/privacy.md](docs/privacy.md).

## What you get

| Piece | What it does | Docs |
| --- | --- | --- |
| **Evidence packets** | Search (FTS5 by default) returns verbatim passages with `source_path` and `source_sha256`. A file that changed after indexing is withheld (listed under `withheld` with the command that restores it), not served under a stale hash. A broken or emptied index is an `ERROR`, never a quiet empty answer. | [cli](docs/cli.md) · [lifecycle](docs/source-lifecycle.md) |
| **Synaptic retrieval** *(opt-in)* | `--method synaptic` = the unchanged FTS packet **plus** passages reached through your explicit links (`[[wikilinks]]`, embeds, Markdown links, frontmatter `related`/`supersedes`/…), within a separate extra budget. By construction it never delivers less than FTS. | [synapse](docs/synapse.md) |
| **Brain View** (Obsidian plugin) | A 3D view of the vault's explicit link graph that highlights the notes and links the last retrieval used: seeds, hops, delivered vs only reached. No network; it writes nothing to the vault except its own settings file, `.obsidian/plugins/context-layer-brain/data.json`. | [plugin](obsidian-plugin/README.md) |
| **Starter brain** | `brain init` creates an Obsidian vault with an English, emoji-free layout adapted from [Avenox Beyin](https://github.com/avenoxai/avenoxbeyin), templates, example notes and the rule files. | [brain guide](docs/brain-guide.md) |
| **Rules the agent must follow** | Byte-identical `CLAUDE.md` / `AGENTS.md`: plan first, stop and ask when a step is risky, no claim without a source, record every step in both files. Hooks make recording enforceable in Claude Code. | [brain guide](docs/brain-guide.md) · [discipline](discipline/) |
| **Lean sub-agents** | Shared evidence packets, a bounded `support-job/v1` contract, a ~5,000-token payload budget, evidence-record returns and `handback check`, which mechanically catches fabricated quotes. | [subagents](docs/subagents.md) · [tasks](docs/tasks.md) |
| **Memory and health** | Append-only JSONL memory whose records go stale when their sources change; `status` and `rollback` for the index and the link graph. | [memory](docs/memory.md) |
| **Optional advisor (Jev)** *(off by default)* | `search --jev` asks a model provider you configure whether delivered passages and link-reached notes help the question; `shadow` only counts, `on` (which needs a calibration receipt) appends byte-exact passages within their own budget. Design after Avenox Beyin's Jev; any provider, including a local server or the host's own CLI. | [jev](docs/jev.md) |
| **GitHub context** *(off by default)* | After a clean local miss, or an explicit agent tool call, read bounded passages from owner-selected public files pinned to a commit. CLI source setup, explicit version checks and an optional verified offline cache. The prompt stays local; citations carry immutable URLs and hashes. No token or model required. | [GitHub context](docs/github-context.md) |
| **Host integration** | MCP stdio server and a Claude Code prompt hook. Every install is a dry run until `--apply`, backs up first, and has an `uninstall`. | [host integration](docs/host-integration.md) |

## Quick start

Get the checkout first (Git is needed for this step):

```sh
git clone https://github.com/solisolsoli/context-layer.git
cd context-layer
```

On macOS or Linux:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e .                                   # from a checkout

context-layer brain init ~/MyBrain --apply         # new vault with rules and routes.json
# existing vault instead: context-layer init ~/MyVault && context-layer rules init ~/MyVault --apply
context-layer index ~/MyBrain                      # FTS index + link graph
context-layer search ~/MyBrain --prompt "which timer controller did we choose and why" --method synaptic
context-layer rules check ~/MyBrain                # CLAUDE.md and AGENTS.md identical?

# Connect Claude Code (dry run first, then --apply):
context-layer install claude-code --vault ~/MyBrain --project ~/MyBrain \
    --hook --method synaptic --rules --plan-default
# Review the diff, then repeat the install command with --apply.
```

`synapse.decision` in the packet says whether links were followed (on the
starter brain the question above gives `relevant_links`: three full-text
passages plus two reached through links).

On Windows, create and use the environment from PowerShell without an activation
script. The CLI flags are the same:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\context-layer.exe brain init "$env:USERPROFILE\MyBrain" --apply
.\.venv\Scripts\context-layer.exe index "$env:USERPROFILE\MyBrain"
.\.venv\Scripts\context-layer.exe search "$env:USERPROFILE\MyBrain" --prompt "timer controller"
.\.venv\Scripts\context-layer.exe install claude-code --vault "$env:USERPROFILE\MyBrain" --project "$env:USERPROFILE\MyBrain" --hook --method synaptic --rules --plan-default
# Review the diff, then repeat the install command with --apply.
```

From a new host session, confirm that the MCP server exposes its **ten tools**.
For other MCP clients, `context-layer install generic --vault <vault>` prints
a configuration snippet. See [host integration](docs/host-integration.md) for
the supported host paths and `doctor` checks.

The Brain View is optional. From a macOS/Linux checkout, install it as follows;
PowerShell instructions are in the [plugin guide](obsidian-plugin/README.md).
No build or Node is needed; `dist/main.js` is committed:

```sh
mkdir -p ~/MyBrain/.obsidian/plugins/context-layer-brain
cp obsidian-plugin/manifest.json obsidian-plugin/styles.css \
   ~/MyBrain/.obsidian/plugins/context-layer-brain/
cp obsidian-plugin/dist/main.js ~/MyBrain/.obsidian/plugins/context-layer-brain/main.js
```

Enable **Context Layer Brain View** under Settings > Community plugins, and run a
search: the view lights up what the retrieval used. The core walk (index, host,
memory, tasks, health) is in [QUICKSTART.md](QUICKSTART.md); the brain setup and
the everyday loop (plan → evidence → act → verify → record) are in
[docs/brain-guide.md](docs/brain-guide.md).

## Fill a knowledge gap from GitHub

The owner chooses public repositories, files and immutable commits once. The
agent can then fetch those documents when local evidence is insufficient.
This is useful for a project's documentation, a prompt file or an MCP setup
guide. Retrieved text remains evidence; it does not install tools or change
the agent's instructions.

1. Configure a source with `github-sources add`: preview its resolved commit
   and files, then reuse that full SHA as `--ref` with `--apply` to save it with
   a backup. This avoids resolving a moving branch to a different revision.
2. Use `search --github` for a fallback after a clean, empty local `NOT_FOUND`.
   When local results exist but do not settle the question, the agent calls
   the `github_context` MCP tool explicitly.
3. Inspect the external status and passages; cite the immutable URL, commit,
   line span and hash. Keep an unresolved question explicit.

After configuring a source named `project-docs`, the CLI equivalents are:

```sh
context-layer search ~/MyBrain --prompt "project setup" --github
context-layer github-context ~/MyBrain --prompt "project setup" --source project-docs
```

| Need | Command | Behavior |
| --- | --- | --- |
| Manage sources | `github-sources list/add/remove/enable/disable` | Preview mutations before `--apply`; configuration is local. |
| Check newer documentation | `github-sources check --id project-docs --ref main` | Reports upstream commits and bounded diffs; choose the branch/tag to follow. Does not change the pin. |
| Adopt a reviewed version | `github-sources update --id project-docs --expected-commit OLD_SHA --commit NEW_SHA` | Requires the expected old pin, a full new SHA and `--apply` to write. |
| Use a local cache | `github-cache enable/status/disable/purge` | Separate opt-in, bounded to 8 MiB and 256 entries; mutations need `--apply`. |
| Work offline or refetch | `github-context --offline` / `--refresh` | Offline reads require cached bytes; refresh fetches the same pinned version. |

The table abbreviates commands: each takes a vault path; retrieval also takes
`--prompt`. See the [complete GitHub guide](docs/github-context.md) for copyable
commands, configuration, cache controls and error handling. The same CLI flags
work on Windows with the executable and vault path shown above.

GitHub retrieval requires no token, model API or Git clone and uploads no
prompt or local note. It is not a model-confidence detector or a search of all
GitHub. It never substitutes remote text for index errors, stale or withheld
local sources. `FOUND` means candidate evidence was delivered, not that an
answer is correct or that the pinned documentation is the newest version.

## How the rules work

`context-layer rules init` installs one rule text as both `CLAUDE.md` and
`AGENTS.md`, because Claude Code reads the first and Codex and other agents read
the second. The rules ask the agent to:

- **start every task with a plan** (Claude Code: plan mode, `/plan`, or
  `--plan-default` to make it the project default);
- **stop and ask** before destructive, irreversible or outward-facing actions,
  on ambiguous instructions, and when evidence is missing, stale or
  conflicting; routine, reversible work needs no approval;
- **label every claim** `SUPPORTED` / `USER_STATED` / `PARTIAL` / `NOT_FOUND` /
  `CONFLICT` and cite a path + SHA-256, a command output or a URL;
- **record every step** in both files with `context-layer rules record`. The
  optional Stop hook blocks once when files changed and nothing was recorded.

**On hallucination, plainly:** no rule file or tool eliminates it. These rules
make an unsupported claim hard to pass off as a fact and easy to detect: every
claim needs a source, `NOT_FOUND` is an allowed answer, and a sub-agent's quote
is checked against the file's bytes before anyone relies on it.

## What was measured

All numbers below come from commands in this repository. Token counts marked
*est.* are `ceil(chars / 4)` estimates, not billed host tokens.

**Sealed offline benchmark** (`python3 bench/run_offline.py`): a fictional
130-note vault and 72 questions, sealed (hashes in `bench/SEAL.md`) before any
candidate ran. A case counts only when **every** required passage is delivered
verbatim from the right note. Default bounds: top-k 3, 6,000 characters.

| Method | Complete (64 answerable) | 2-hop bridge (20) | Mean packet (est. tokens) |
| --- | ---: | ---: | ---: |
| grep | 37 (58%) | 0 | 388 |
| FTS (default) | 33 (52%) | 0 | 333 |
| **synaptic** (FTS + link extras) | **46 (72%)** | **10** | 527 |
| synaptic `--compact` | 37 (58%) | 10 | 334 |

Synaptic vs FTS on the same cases: 13 cases only synaptic completes, 0 only FTS
completes (exact sign test p = 0.0002). The gain costs about 58% more packet
tokens. Every run of the sealed set is listed in [bench/INSPECTIONS.md](bench/INSPECTIONS.md): five design-time looks at aggregate level, the rest verification reruns; no case-level tuning was done. [bench/README.md](bench/README.md)

**Live host runs** (Claude Code headless, read-only tools):

- *0.2, one private vault, 12 held-out questions:* the FTS prompt-hook arm
  matched the host's own judged correctness using about **54% of the baseline's
  mean total tokens (about 46% fewer)** and half the turns. One run, N = 12.
  [eval/LIVE_COMPARE.md](eval/LIVE_COMPARE.md)
- *0.3 pilot, fictional 130-note vault, 24 questions × 3 arms, blind-judged:*
  all three arms answered 24/24 correctly; mean total tokens per question were
  77,160 (host alone), 43,729 with the FTS hook (57%) and 41,685 with the
  synaptic hook (54%), with about half the turns. On two-hop bridge questions
  the synaptic hook used 31% fewer tokens than the FTS hook; on aggregation and
  unanswerable questions it used more. One run, N = 24, correctness at ceiling.
  [eval/LIVE_PILOT_0.3.md](eval/LIVE_PILOT_0.3.md)

**Sub-agent payloads** (`python3 eval/orchestration_cost.py --standin`,
synthetic setup, estimates): four workers' initial payloads 16,827 → 9,441
est. tokens with per-worker compact packets; the coordinator's verification reading
12,452 → 2,580; the one planted fabricated quote was caught (re-measured after the compact-packet change). Not billed tokens
and not quality evidence. [docs/subagents.md](docs/subagents.md)

## What it does not claim

- It does not answer questions; it delivers evidence. `PARTIAL` means evidence
  was found, `NOT_FOUND` means this retrieval found nothing (not that the vault
  is silent), `ERROR` means retrieval failed.
- It does not eliminate hallucination or prompt injection, and it is not a
  sandbox: a sub-agent running as your user can still write anywhere; `verify`
  detects, it does not prevent.
- Synaptic retrieval uses explicit links only: no embeddings, no inferred
  relations. "Synaptic" and "Brain View" are product names for an explicit link
  graph and its retrieval trace; nothing here is a neural-network model.
- No general token-saving or speed claim beyond the runs listed above; both live runs used one vault each and a single model.

## Prior art and credits

Obsidian's own graph view and local graph came first; Brain View adds only the
retrieval-activation overlay. [Neural Vault](https://github.com/williansaez/obsidian-neural-vault)
lights Obsidian's 2D graph as Claude Code reads notes; this project instead
visualizes a retrieval's trace of hash-pinned evidence. Graph-based retrieval
research (HippoRAG, GraphRAG, spreading activation) informs the synaptic layer;
see [docs/design-rationale.md](docs/design-rationale.md). What we believe is
distinctive is the combination: hash-pinned verbatim evidence, explicit-link
activation anchored to source lines, a live view of that trace, enforced
record/plan rules, and fenced, checkable sub-agent work, all local and
standard-library only. We did not find another tool that combines them; that is
not proof that none exists.

The starter brain's folder layout is adapted from
**[Avenox Beyin](https://github.com/avenoxai/avenoxbeyin) by Avenox** (MIT,
[avenox.lol](https://avenox.lol)); no Avenox code or text is included. Optional
patches in [retrieval-patches/](retrieval-patches/README.md) modify Avenox Beyin
v3.0.1 and carry its MIT notice. Full list: [CREDITS.md](CREDITS.md),
[THIRD_PARTY.md](THIRD_PARTY.md).

## Support

| Area | Tested here | Expected but unverified | Out of scope |
| --- | --- | --- | --- |
| OS | macOS 14+; Ubuntu and Windows (hosted CI) | other Linux distributions | — |
| Python | 3.10–3.13 on Ubuntu; 3.12 on macOS and Windows | other OS/Python combinations | < 3.10 |
| Vault | UTF-8 `.md` folder (Obsidian or plain) | — | symlinked sources, binary notes |
| Text encoding | UTF-8 locale, or UTF-8 mode (`PYTHONUTF8=1`: `make` sets it, `install` writes it into the host commands) | — | a non-UTF-8 locale without UTF-8 mode: the package's own file reads and writes name UTF-8, but some test suites (`tests/test_integrity.py`, for one) fail under a Latin-1 locale |
| AI host | Claude Code 2.1+ (MCP stdio, `UserPromptSubmit` hook) | Codex CLI via MCP config | hosts without MCP or hooks |
| Obsidian | 1.13.7 desktop (live render checked on the fictional vault) | other 1.x desktop | mobile |

The [accepted runtime run for `4dc60a9`](https://github.com/solisolsoli/context-layer/actions/runs/36773269705)
passed all 12 required jobs, including the Ubuntu/macOS matrix, native Windows
full test suite and installed-distribution walk, packaging, benchmark
reproduction and plugin tests. Windows uses Python 3.12 on the hosted runner;
other OS/Python combinations remain unverified. CI does not verify a live AI
host session or model answer quality. See
[docs/publishing-checklist.md](docs/publishing-checklist.md).

## Documentation

| Document | What is in it |
| --- | --- |
| [QUICKSTART.md](QUICKSTART.md) | The core walk as commands: index, host, memory, tasks, health; optional GitHub context |
| [docs/github-context.md](docs/github-context.md) | Source setup, missing-evidence flow, cache/offline reads, version checks and troubleshooting |
| [docs/brain-guide.md](docs/brain-guide.md) | Building your own brain: setup, daily loop, note-writing, sub-agents, FAQ |
| [docs/cli.md](docs/cli.md) | Conventions for every command: exit codes, packet statuses, output, format versions (per-command flags: `--help`) |
| [docs/synapse.md](docs/synapse.md) | Synaptic retrieval: how it works, knobs, limits |
| [docs/subagents.md](docs/subagents.md) | When to delegate, lean jobs, `handback check`, measured payloads |
| [docs/privacy.md](docs/privacy.md) | Every file the tool writes, retention, deletion, threat model |
| [docs/README.md](docs/README.md) | Index of the component docs (host integration, memory, tasks, lifecycle) |
| [obsidian-plugin/README.md](obsidian-plugin/README.md) | Brain View install, settings, activation overlay |
| [bench/README.md](bench/README.md) | The sealed benchmark and how to rerun it |
| [SECURITY.md](SECURITY.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) · [CHANGELOG.md](CHANGELOG.md) | Private security reporting, owner maintenance, conduct and releases |

## Development and license

`make test` (Python suites), `make plugin-test` (Node, plugin), `make bench`,
`make lint`, `make demo`. `python3 scripts/check_distribution.py dist/*.whl`
installs a built wheel into a fresh environment and runs the walks listed in the
script's own header (`make test` covers the rest).

MIT, copyright **solisolsoli**: [LICENSE](LICENSE). The license permits use,
modification and redistribution subject to its terms; it does not grant write
access to this repository or require the owner to accept contributions.
Retained third-party notices are listed in [THIRD_PARTY.md](THIRD_PARTY.md).
No personal vault, prompts, packets, host settings or private research are
distributed.
