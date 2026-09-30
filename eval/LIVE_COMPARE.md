# Live comparison — one host, three arms

`live_compare.py` runs a held-out question set against the same AI host three
ways: with the host's own file tools on the vault (**baseline**), with no file
tools and only the context-layer MCP tools (**candidate**), and with baseline's
file tools plus the context-layer `UserPromptSubmit` hook (**hook**). It records
what the host reported for each call and writes a report a person can check.

The third arm exists because the first two do not separate *having* the evidence
from *searching* for it: a tools-only arm can spend extra turns finding what a
hook delivers with the prompt, and turns are tokens. Which way wins is a
question for a run, not for this file.

This is phase A6 of the 0.2 scope. It measures delivery, abstention, tokens,
host-estimated cost and time. It does **not** measure whether an answer is
correct; a coordinator adds that separately as judgements.

The runner makes no model API call of its own. The host binary named by
`--claude` is the only thing that talks to a model. Python 3.10+, standard
library only.

## The three arms

Every arm gets the same prompt, model, turn cap and budget, is launched headless
from the vault directory, and has its stdin connected to `/dev/null` so the host
never waits on a terminal.

| | baseline | candidate | hook |
|---|---|---|---|
| Vault access | `--add-dir <vault>` | none | `--add-dir <vault>` |
| Allowed tools | `Read,Grep,Glob` | `mcp__context-layer__search_vault,mcp__context-layer__read_source` | `Read,Grep,Glob` |
| Denied tools | `Bash,WebSearch,WebFetch,Edit,Write,Task` | `Read,Grep,Glob,Bash,WebSearch,WebFetch,Edit,Write,Task` | same as baseline |
| MCP | `--strict-mcp-config` with no config: no servers | `--mcp-config <out>/mcp.json`, from `install.mcp_snippet(vault)` | none, same as baseline |
| Context layer | not connected | called as tools, when the host chooses to | runs on every prompt, before the host answers |
| Told to cite | the vault-relative path of every source used | `source_path` and `sha256` of every source used | the vault-relative path of every source used |
| Told to abstain | exactly `NOT_FOUND` when the vault does not contain the answer | exactly `NOT_FOUND` when the tools return no evidence | exactly `NOT_FOUND` when the vault does not contain the answer |

Every arm also gets `-p`, `--output-format json`, `--model`, `--max-turns`,
`--max-budget-usd`, `--no-session-persistence` and `--strict-mcp-config`. No arm
can reach the network or run a command, and every arm denies `Task`, the
host's sub-agent tool. The hook arm's system prompt adds one
instruction the others do not have: prefer the evidence lines injected with the
prompt, and use the file tools only for what they do not cover.

### What the hook arm writes, and puts back

A `UserPromptSubmit` hook is project state, so the hook arm installs one in the
vault copy for the duration of its own runs. Before each hook call the runner
writes `<vault>/.claude/settings.json`:

```json
{"hooks": {"UserPromptSubmit": [{"hooks": [<context_layer.install.hook_entry(vault)>]}]}}
```

If that file already exists, the group is appended to whatever
`UserPromptSubmit` entries are there and every other key is left alone. When the
call ends — including when the host fails — the file is restored byte for byte,
or deleted if it did not exist, and a `.claude` directory the runner created is
removed again. Each result row records what happened in `hook_settings`:

```json
{"settings": ".claude/settings.json", "action": "created", "restored": "removed"}
```

The person's own host configuration is never touched: the hook lives in the
vault copy the host runs from, not in `~/.claude`. Use a copy of the vault, not
the vault you work in.

## The case file

One JSON object per line. `id`, `prompt` and `answerable` are required; the rest
are carried through to the report and to whoever judges the answers.

```json
{"id": "H01", "prompt": "…", "answerable": true,
 "expected_sources": ["notes/alpha.md"],
 "required_passages": [{"source_path": "notes/alpha.md", "text": "…"}],
 "answer_gist": "…", "category": "policy", "split": "heldout"}
```

Per A6, these cases are written before any candidate run and are not the 24
synthetic development cases used by `compare.py`.

## What is measured, and where it comes from

Every number is either a field of the host's `--output-format json` payload or
this runner's own clock; the runner itself only sums and averages them. One of
those fields is an estimate made by the host: `total_cost_usd` is a client-side
estimate, not billing data, so the reports call it **host-estimated** cost
(`summary.json` states it in `cost_basis`).

| Recorded field | Source |
|---|---|
| `answer` | host JSON `result` |
| `num_turns` | host JSON `num_turns` |
| `cost_usd` | host JSON `total_cost_usd` (host-estimated) |
| `host_duration_ms` | host JSON `duration_ms` |
| `duration_ms` | this runner's wall clock around the subprocess |
| `tokens.input` / `.cache_creation` / `.cache_read` / `.output` | host JSON `usage.input_tokens`, `usage.cache_creation_input_tokens`, `usage.cache_read_input_tokens`, `usage.output_tokens` |
| `tokens.total` | the sum of those four |
| `is_error`, `error` | non-zero exit, unparsable JSON, host JSON `is_error`, or a call stopped at `--timeout-s` |
| `timed_out` | the call outlived `--timeout-s`; the runner stopped the host and every process it started |
| `hook_settings` | what the hook arm wrote into the vault copy and put back; `null` for the other arms |

Both durations are kept because they answer different questions: the host's
number is its own view of the turn, the runner's number is what the person
waited for, process start included.

Derived per case and arm:

- **`delivered`** — every path in `expected_sources` appears in the answer as a
  whole path token, either in full or as its file name on its own: `notes/a.md`
  does not count inside `notes/delta.md`, and `plan.md` does not count inside
  `archive/old-plan.md` or `archive/plan.md`. Brackets, backticks, a leading
  `./` and a trailing sentence period around the path are fine. A case with no
  expected source is never counted as delivered.
- **`abstained`** — the first word of the answer (a run of letters, digits and
  underscores) is exactly `NOT_FOUND`; `NOT_FOUNDATION …` is not an abstention.
- **`correct_abstention`** — abstained on a case marked `answerable: false`.
- **`false_abstention`** — abstained on a case marked `answerable: true`.
- **`judge`** — `null` until a coordinator supplies it.

### What `delivered` does and does not prove

`delivered` is a mention check. It proves the answer named the source the case
expects. It does **not** prove the source was read, that its text reached the
model, that the citation belongs to the sentence it sits next to, or that the
answer is right. A host that lists plausible filenames scores here without
answering anything — the same limit the rest of this harness states about
filename hits.

Whether an answer is correct is the `judge` column, and only a person puts it
there. `required_passages` and `answer_gist` are carried in the case file so
that person has something to check against.

## Running it

```sh
python3 eval/live_compare.py \
  --vault /path/to/vault \
  --cases /path/to/heldout.jsonl \
  --out /path/to/run \
  --model sonnet --max-turns 8 --budget-usd 0.60 --timeout-s 600
```

All three arms run by default. Other flags: `--arms baseline,hook` to run a
subset, `--only H01,H02` to restrict case ids, `--claude PATH` to name a
different host executable. `--timeout-s` (default 600) bounds each host call:
a call that outlives it is stopped, together with every process it started,
and recorded as an error row with `timed_out: true`.

Output in `--out`:

| Path | What it is |
|---|---|
| `results.jsonl` | append-only, one row per (case, arm), ASCII JSON |
| `raw/<id>-<arm>.json` | the host's stdout exactly as received |
| `raw/<id>-<arm>.err` | the host's stderr |
| `mcp.json` | the candidate arm's MCP config |
| `summary.json` | per-arm aggregates and the per-case table |
| `REPORT.md` | the same, rendered |

The run is resumable: a (case, arm) already in `results.jsonl` is skipped, so an
interrupted or budget-stopped run continues where it stopped. A host failure —
non-zero exit, unparsable JSON, or a call stopped at `--timeout-s` — is written
as a row with `is_error` true and the error text, and the run continues to the
next case. Nothing is dropped. The case file and `results.jsonl` are read one
line per line feed and nothing else, and rows are written as ASCII JSON, so an
answer holding U+2028 or U+0085 stays one row for any reader.

Token, cost, turn and duration means exclude error rows; the error and timeout
counts are reported separately.

## Adding judgements

Write a JSON object keyed `"<id>|<arm>"`, with one of `correct`, `partial`,
`wrong`, `abstained`:

```json
{"H01|baseline": "partial", "H01|candidate": "correct", "H01|hook": "correct"}
```

```sh
python3 eval/live_compare.py --vault … --cases … --out /path/to/run \
  --judgements /path/to/judgements.json
```

This merges the verdicts into `results.jsonl` and rewrites `summary.json` and
`REPORT.md`. It launches no host call and re-runs nothing. A key that matches no
row is reported on stderr and ignored. Judge the answers in `results.jsonl`
against the case file's `answer_gist` and `required_passages` before looking at
the token and cost columns, not after.

## Limits

- **N is small.** The report says so itself: a small N is a signal, not proof.
  Nothing here supports a claim about a population of questions, vaults or users.
- **One host, one model, one vault, one machine.** Results are not transferable
  to another host, another model, or someone else's notes. The support matrix in
  `SCOPE.md` applies.
- **The vault is not shipped.** A real run uses a private vault, so the numbers
  cannot be reproduced from this repository alone. The case file and the code
  are the reproducible parts; publishing a result means publishing its case file
  and its `summary.json`, never the vault.
- **Arms are not identical in every respect.** The candidate carries an MCP
  server the baseline does not, and the hook arm carries a hook and a differently
  worded system prompt; some of the difference is the mechanism, not the
  retrieval. Different allowed-tool sets are the point of the comparison, not a
  controlled variable.
- **The hook arm edits the vault copy while it runs.** The edit is one file and
  it is restored when the call ends, including on host failure, but a runner
  killed outright (`SIGKILL`, power loss) can leave `<vault>/.claude/settings.json`
  behind. Check it before trusting a later run, and never point the hook arm at a
  vault you are working in.
- **Cost and token figures are the host's own accounting**, including its cache
  behaviour, which varies between runs and is not controlled here. The cost is
  the host's client-side estimate, not a bill.
- **The 0.2 judging was not blinded.** The coordinator judged each answer once with
  the arm named beside it; nothing was hidden and nothing was re-judged. The 0.3
  pilot on the fictional vault (`eval/LIVE_PILOT_0.3.md`) hid the arm labels from
  its judge; this recorded run did not.
- **Abstention is string matching.** Only an answer whose first word is exactly
  `NOT_FOUND` counts; `NOT_FOUND` later in a sentence, or a refusal phrased any
  other way, does not.
- `--max-turns` is documented in the public Claude Code CLI reference ("Limit the
  number of agentic turns (print mode only). Exits with an error when the limit
  is reached."; checked 2026-09-28); the 2026-09-21 run used Claude Code
  2.1.278, whose `claude --help` did not list it. Confirm it on the host version
  you use before trusting the cap.

## Recorded run — 2026-09-21

One run on one private Markdown vault, Claude Code 2.1.278
with `sonnet`, 12 held-out cases (8 answerable, 4 unanswerable), 8 turn cap,
$0.60 per case. The case set was written before any arm ran and was not edited
afterwards. Correctness was judged once, by the coordinator, reading the full
answers with the arm labels visible: the judging was **not blinded**, so a
preference for or against an arm could have moved a verdict. The vault, the
cases and the answers are private and are not distributed here.

| Measure | baseline | candidate (MCP) | hook |
| --- | ---: | ---: | ---: |
| judged correct / 8 answerable | 8 | 7 (+1 partial) | 8 |
| correct abstentions / 4 | 4 | 4 | 4 |
| false abstentions | 0 | 0 | 0 |
| errors | 0 | 0 | 0 |
| mean total tokens | 206,260 | 231,598 | 111,716 |
| median total tokens | 173,739 | 224,454 | 58,401 |
| mean host-estimated cost (USD) | 0.222 | 0.243 | 0.198 |
| mean wall time (s) | 17.0 | 16.1 | 14.9 |
| mean turns | 4.58 | 5.08 | 2.33 |

Read with care:

- **The MCP arm cost more, not less.** Given only the two tools, the host spent
  extra turns searching for what the baseline found with `Grep`. It bought
  verifiability, not savings: every candidate answer carried a source hash, and
  no baseline answer did.
- **The hook arm is where the saving is.** Injecting evidence with the prompt
  removed the search loop: half the turns and about 54% of the baseline's mean
  total tokens (111,716 against 206,260, i.e. about 46% fewer; the median was
  about 34% of the baseline's), with the same judged correctness on this set.
- The runner's automatic `delivered` and `abstained` columns disagree with the
  judgement in both directions on this set: `delivered` counts a path string, so
  an answer sourced from a file that mirrors the fact scores 0, and `abstained`
  only matches a leading `NOT_FOUND`, so a correct refusal that explains itself
  first scores 0. Both are diagnostics. `judge` is the correctness column. This
  run predates the whole-token rule for `delivered` and the first-word rule for
  `abstained` (both 2026-09-28); its judged columns do not depend on either.
- One run, one host, one model, one vault, N=12. It is a signal, not proof, and
  it says nothing about another vault or another question set.
- The hook arm ran the 0.2 hook, which joined items as
  `path (sha256 first 12) — content`. From 0.3.0 each item sits between
  nonce-delimited `<<evidence N nonce ...>>` / `<<end N nonce>>` markers, a few
  dozen characters more per item. The run was not repeated with that format.
