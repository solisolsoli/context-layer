# context-layer benchmark: evidence completeness and packet cost

This directory is a public, reproducible benchmark for one question:

> Does a retrieval method that follows the vault's own links (the "synaptic"
> method) put **all** the evidence a question needs into the packet more often
> than the FTS baseline, at the same or a smaller packet size, and what does
> each packet really cost in tokens?

It is deliberately independent of the method it evaluates. The vault, the
cases and the scorer were written without reading any synaptic retrieval
code; the scorer only reads a returned passage's path and verbatim text.

Everything under `vault/` is fiction: a made-up city ("Kellbrook") and its
planning office. No real organisation, person, place or vault is described.

## Files

| Path | What it is |
| --- | --- |
| `vault/` | 130-note Obsidian-style vault (the fixture) |
| `cases.jsonl` | 72 sealed cases, one JSON object per line |
| `vault.sha256` | manifest: SHA-256 and path of every vault file |
| `SEAL.md` | SHA-256 of `cases.jsonl` and of `vault.sha256` |
| `seal.py` | `validate` / `write` / `check` for the seal |
| `run_offline.py` | offline runner and scorer (no network, no model) |
| `test_scorer.py` | scorer tests with hand-built packets |
| `test_runner.py` | runner tests: the equal-budget mapping on two cases, refused flags, output formats |
| `run_live.py` | optional live host arm (dry run by default) |
| `select_pilot.py` | recreates the 24-case selection of the 0.3 live pilot from its seed (`--check`) |
| `judge_template.jsonl` | one row per case for human or separate-judge verdicts |
| `INSPECTIONS.md` | ledger of every run of the sealed set, and which of them could influence design |
| `results/SUMMARY.md` | the committed offline result table (it ships in the sdist too); every other result file (`<method>.json`, `qrels.txt`, `<method>.run`) is ignored by git |

## The vault

Folders: `hubs/` (maps of content), `projects/`, `people/`, `teams/`,
`meetings/`, `decisions/`, `glossary/`, `daily/`, `partners/`, `places/`,
`funding/`, `office/`, `inbox/`. Notes use `[[wikilinks]]` with aliases and
headings (`[[Riverside Greenway#Budget|greenway budget]]`), `![[embeds]]`
(including a heading embed), relative Markdown links, and frontmatter with
`aliases`, `tags`, `related`, `supersedes` and `superseded_by`.

Deliberate traps:

- **Hubs** linked from almost everything (`hubs/Home.md` has 42 inbound links).
- **Near-duplicates**: an abandoned 2023 "Tanner Road" concept next to the live
  "Tanner Street" project, a 2019 riverside study next to the greenway, and an
  Obsidian-style stale copy `projects/Riverside Greenway 1.md` with old numbers.
- **Supersession**: four decision pairs where the newer decision replaces the
  older one (`status: superseded` / `supersedes:`).
- **Link-only facts**: the answer note for every bridge case shares no
  distinctive word with the question; it is reachable by following a link from
  the note the question names.
- **Backlink-only lists**: e.g. the funding note does not list the projects it
  pays for; only the projects link to it.
- **Orphans**: five `inbox/` notes nothing links to, two of which hold answers,
  plus daily notes with no inbound links and one dangling link.
- **Inconsistent naming**: title case, lower-case file names
  (`school-streets-pilot.md`), one subject under several words (bike lanes /
  cycle lanes / cycle track; BRT / rapid bus line).

## The cases

Each line of `cases.jsonl`:

```json
{"id": "B01", "type": "bridge_2hop", "question": "...",
 "gold": [{"path": "projects/...md", "must_contain": "verbatim substring"}, ...],
 "distractors": [{"path": "...", "must_contain": "..."}],
 "why": "optional note on the trap"}
```

| Type | n | What it checks |
| --- | --- | --- |
| `single_hop` | 14 | the answer is in one note that shares words with the question |
| `bridge_2hop` | 20 | two notes: the one the question names, and the linked note holding the answer |
| `multi_note_aggregation` | 12 | two to four notes must all be present |
| `supersession` | 8 | the current decision must be present; the superseded one is a listed distractor |
| `distractor` | 10 | a near-duplicate with different numbers exists; it is listed as a distractor |
| `unanswerable` | 8 | nothing in the vault answers it; `gold` is empty |

44% of cases are bridge or aggregation cases. A gold item's `must_contain`
is the verbatim text that proves that part of the answer; bridge cases list
both the link that identifies the target and the fact in the target.

### How the cases were sealed

1. The vault was written, then the cases, in one sitting.
2. `python3 bench/seal.py validate` checked that every `must_contain` (gold
   and distractor) occurs verbatim in the note it names, that only
   unanswerable cases have empty gold, and that no bridge question shares a
   distinctive term (one found in at most four notes) with its answer note.
3. `python3 bench/seal.py write` wrote `vault.sha256` and `SEAL.md`, and the
   vault, cases and seal were committed together as **"bench: seal cases"**
   before `run_offline.py` existed and before any search method was run on the
   vault. The only commands run on a copy of the vault before sealing were
   `context-layer init` and `index`, to confirm no folder is excluded as noise.
4. The runners refuse to score if `python3 bench/seal.py check` fails
   (`run_offline.py --allow-unsealed` overrides and marks the output).

Any later edit to a case or a note changes a hash. Changes to the benchmark
must be a new sealed version, not an edit of this one.

### How often the sealed set was looked at

[INSPECTIONS.md](INSPECTIONS.md) lists every run of these cases: one baseline
run of grep and fts, five design-time looks (a mechanism diagnosis, an interim
run and the final run of the 0.3 design, then two aggregate looks at the 0.4
tree), one live pilot on 24 of the questions, and verification reruns that
reproduced the committed numbers. Add a row there before relying on a new run.

## Running it offline

From a checkout, with no install:

```
python3 bench/run_offline.py                       # grep, fts, synaptic
python3 bench/run_offline.py --methods grep,fts
python3 bench/run_offline.py --budget-tokens 400   # one packet budget, mapped per method
python3 bench/run_offline.py --methods synaptic --search-arg=--compact
python3 -m unittest bench/test_scorer.py bench/test_runner.py
```

The runner copies the vault to a temporary directory, runs
`python3 -m context_layer.cli init` and `index` on the copy (the checkout is
put on `PYTHONPATH`), then for every case and method runs
`python3 -m context_layer.cli search <copy> --prompt <question> --method <m>`.

- A method the CLI rejects (`invalid choice`) is skipped with a note.
- `--top-k K` and `--search-arg=...` are forwarded to every method. A flag that
  a method would not apply is refused before anything runs (exit 2): the
  synaptic-only flags (`--extra-tokens`, `--compact`, `--budget-tokens`,
  `--max-hops`, `--record-query`) with any other method, and `--budget-tokens`
  without `--compact`. So the summary never lists a flag that had no effect,
  whether or not the installed `search` refuses such flags itself.
- `--budget-tokens N` gives every method one budget of N estimated tokens,
  translated into that method's own flags:

  | arm | flags | why |
  | --- | --- | --- |
  | `grep`, `fts` | `--budget 4N` | their budget is in characters (tokens x 4) |
  | `synaptic` | `--compact --budget-tokens N` | the compact packer is the synaptic packer with one token budget |
  | `synaptic-extra` | `--budget 4(N-E) --extra-tokens E` | the default superset packer inside the same total: an fts packet at the smaller budget plus E extra tokens of linked passages. E is `--extra-tokens E` or, by default, the CLI's own share 600/2,100 of N (for N = 400, E = 114) |

  After the run every error-free packet of every arm is checked against N; a
  larger one fails the run (exit 3). The summary gains an "Equal packet budget"
  table with the mapping, the largest packet per arm and the check.

It writes `results/<method>.json` (flags used, per-case rows, aggregates,
provenance), `results/SUMMARY.md`, and the same run in TREC form for other
scoring tools: `results/qrels.txt` (`qid 0 docno rel`; gold notes 1, listed
distractor notes 0) and `results/<method>.run` (`qid Q0 docno rank score tag`;
the distinct files in delivery order, score = files - rank + 1). A `docno` is
the vault-relative path, percent-encoded so it has no spaces. No output file has
a timestamp or a temporary path, so the same checkout produces the same bytes.

Provenance: every result file records the command, the checkout (`+dirty` when
`context_layer/`, `router/`, `eval/` or `bench/` has uncommitted changes, apart
from `bench/results/`), the SHA-256 of the cases and of the vault manifest, and
`python`, `sqlite`, `unicode` (the Unicode database version), `platform` and
`scorer_sha256` (one digest over `bench/run_offline.py` and `bench/seal.py`).
The `Checkout:` line of `SUMMARY.md` carries the same environment; it is the
only line that differs between machines, and the only one hosted CI ignores
when it reruns the benchmark and compares it with the committed file.

## Metrics

Per case and method:

| Metric | Definition |
| --- | --- |
| `gold_recall` | fraction of gold items whose `must_contain` occurs in a returned passage from the gold path (whitespace collapsed, Unicode NFC, case-sensitive; a string split across two passages does not count) |
| `non_verbatim` | passages that are not a verbatim span of the file they name (exactly, or after the same whitespace and NFC normalisation). They earn nothing, not even a path hit, but still count toward the packet size |
| `complete` | every gold item found. This is the headline: a bridge or aggregation case counts only if **all** required evidence is in the packet, not if one hit is in the top k |
| `required_paths_present` | looser check: every gold path appears in the packet at all (explains truncation losses) |
| `distractor_hits`, `misled` | distractor strings present; `misled` = a distractor is present and the case is not complete |
| `false_evidence_on_unanswerable` | number of passages returned for an unanswerable case (allowed; recorded) |
| `packet_chars` | characters of passage text (JSON framing excluded; `json_chars` records the raw output length) |
| `est_tokens` | ceil(`packet_chars` / 4). **An estimate**, not a tokenizer count |
| `whole_file_tokens` | the same estimate for reading every distinct surfaced file in full, the cost when an agent opens each hit |

Per method: completeness with a 95% Wilson interval, mean gold recall, the
same by case type, packet size (mean, median, max), whole-file cost,
`est_tokens` per complete case, and a paired exact sign test between methods
on the cases where exactly one of them is complete.

A case whose search failed (non-zero exit without a packet, or an error
packet) stays in the denominator and counts as incomplete, in the headline
rate and in the paired test ("A errored, B complete" is a win for B); the
summary names the failed cases and adds the rate over the error-free cases.
Packet sizes are averaged over error-free cases only, so an error never makes a
method look cheaper.

Only passage path and text are scored. Fields a method adds about itself
(scores, roles, activation, status) are never read, so no method is rewarded
for its own labels. The grep baseline is always available and is included by
default.

## Live host arm (optional, costs money)

`run_live.py` asks each question to a headless host
(`claude -p --output-format json --model sonnet`) from a fresh vault copy, in
three arms that all allow only the host's `Read`, `Grep` and `Glob` tools:

- `baseline`: no hook;
- `hook-fts`: the copy's `.claude/settings.json` has a `UserPromptSubmit` hook
  `PYTHONPATH=<checkout> python3 -m context_layer.cli hook claude-code --vault <copy>`
  (the checkout form of what `context_layer/install.py` writes);
- `hook-synaptic`: the same with `--method synaptic`.

```
python3 bench/run_live.py --host-cmd claude                # dry run: plan + hook preflight
python3 bench/run_live.py --host-cmd claude --run --max-budget-usd 0.25
python3 bench/run_live.py --summarize-judged bench/results/live/judge.jsonl
```

Dry run is the default. The preflight runs each hook command once locally;
an arm whose hook rejects its flags is skipped unless `--force-arms`.
`--setting-sources project` is passed by default so the person's own
user-level hooks do not fire inside the benchmark.

One run prepares one fresh vault copy and the arms take turns on it, in the
order given by `--arms` (default `baseline`, `hook-fts`, `hook-synaptic`), each
over all cases: the hook is written into the copy's `.claude/settings.json`
before an arm and removed after it. The baseline host can therefore also see
the copy's `.context/` directory (index and link graph), and the fixed arm order
is not balanced against the host's caching; both are limits of a pilot, not of
the scorer. `select_pilot.py` recreates the 0.3 pilot's case selection.

Each call records the answer text and, from the host's own JSON, input,
cache-creation, cache-read and output tokens, cost, turns and duration. The
script never grades an answer: it writes `judge.jsonl` (same shape as
`judge_template.jsonl`) with an empty `verdict` (`correct`, `partial`,
`wrong`, `abstained`) for a human or a separate judge. For an unanswerable
case the right behaviour is `NOT_FOUND`, judged as `abstained`.

## Limits

- **One fictional vault, author-written questions.** The same author wrote the
  vault and the questions, knowing how the lexical baselines work. Bridge
  cases were built to be lexically disjoint from their answer note, so a
  baseline failing them is by construction, not a discovery; the useful number
  is how often a link-following method completes them and at what size.
- **Small n per type** (8 to 20). Intervals are wide; read the counts and the
  paired test, not a single percentage.
- **Short notes.** Every note is at most about 1,000 bytes, so the default FTS
  packet (top 3, 2,000 characters per source; a note that fits is delivered
  whole under either `--delivery` mode) is always whole files and
  `whole_file_tokens` is close to `est_tokens` for the baselines. Token savings
  over "open the hits" show up only for methods that return sub-file passages
  or under `--budget-tokens`; this vault does not model very long notes.
- **Top-k caps.** At the baselines' default top 3, a case with four gold notes
  (A06) cannot be complete; that is part of what is measured, and `--top-k`
  changes it.
- **Lexical scoring.** A passage that paraphrases or re-wraps text across
  passages earns nothing. Evidence completeness is not answer correctness;
  the live arm with separate judging exists for that.
- **`est_tokens` is an estimate** (characters / 4); host token counts come only
  from the live arm.
- **Equal budgets are enforced, not equalised.** `--budget-tokens N` caps every
  packet at N, but a method that stops early (few matches, top-k 3) may use
  less; compare the reported sizes, not only the cap.
- The distinctive-term check for bridge questions is a heuristic (document
  frequency at most four), not proof that no lexical path exists.
