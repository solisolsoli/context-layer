# Live host pilot, 0.3

One run of Claude Code (headless, model `sonnet`, `claude` 2.1.282) answering 24
questions from the fictional benchmark vault in three arms. Token, cost and turn
figures are the host's own counts from its JSON output. Code state:
`b4f969372f76c144988dc7895a70c5ef15a02f88`. Later commits also changed code on
the retrieval and hook path (for example `a278506`, the synaptic hook flags, and
`a6a72fc`, which withholds a source changed since indexing instead of failing
the packet), so this describes that commit, not the current checkout; the run
was not repeated.

The run's raw files are in the repository under
[live-pilot-0.3/](live-pilot-0.3/). They are not part of the source or wheel
distributions.

## Setup

- **Questions:** 24 of the 72 sealed cases in `bench/cases.jsonl`, drawn with a
  fixed seed (`20260924`) under a per-type quota: 7 two-hop bridge,
  5 aggregation, 4 single-hop, 3 supersession, 2 distractor, 3 unanswerable.
  The ids are in [live-pilot-0.3/selection.txt](live-pilot-0.3/selection.txt).
  No selection script was committed with the run; `python3 bench/select_pilot.py
  --check`, written afterwards, recreates the same ids from the seed
  (`random.Random(20260924)`, one `sample` per type in the quota order).
- **Arms**, run one after another on **one** fresh copy of the vault, in this
  fixed order, each over all 24 questions: the hook was written into the copy's
  `.claude/settings.json` before an arm and removed after it.
  - `baseline`: the host with read-only tools (Read, Grep, Glob) only.
  - `hook-fts`: the same, plus the `UserPromptSubmit` hook injecting the FTS packet.
  - `hook-synaptic`: the same, plus the hook injecting the synaptic packet
    (FTS packet + link extras, `--extra-tokens 600`).
- **Host limits:** at most 8 turns, project settings only (no user-level hooks),
  no Bash, web or edit tools, the same system prompt ("answer from the notes, name
  the note path, reply NOT_FOUND if the notes do not answer").
- **Command:** `python3 bench/run_live.py --host-cmd claude --model sonnet --run
  --max-budget-usd 1.5 --only <ids>`.
- **Judging:** the 72 answers were shuffled and stripped of their arm
  ([blind-judge.jsonl](live-pilot-0.3/blind-judge.jsonl)); a separate model
  judged each against the gold facts without access to the arm mapping
  ([blind-judged.jsonl](live-pilot-0.3/blind-judged.jsonl),
  [blind-key.json](live-pilot-0.3/blind-key.json)). Which model judged, and
  with which prompt, was not recorded; each judged row keeps the verdict and a
  one-line reason. A maintainer spot-checked the unanswerable, bridge and
  aggregation verdicts against the answers and agreed.

## Result

| Arm | Correct | Mean total tokens | vs baseline | Median total tokens | Mean turns | Cost (24 questions) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | 24/24 | 77,160 | 100% | 65,496 | 4.4 | $0.94 |
| hook-fts | 24/24 | 43,729 | 57% | 22,268 | 2.5 | $0.69 |
| hook-synaptic | 24/24 | 41,685 | 54% | 23,717 | 2.2 | $0.76 |

Means and medians are over the 24 host calls of an arm, rounded to the nearest token; `tests/test_doc_claims.py` recomputes every number in these two tables from [answers.jsonl](live-pilot-0.3/answers.jsonl) and the blind-judging files. (The baseline mean was first published as 77,161, a double rounding of 77,160.46.)

Mean total tokens by question type (input + cache creation + cache read + output):

| Type | n | baseline | hook-fts | hook-synaptic |
| --- | ---: | ---: | ---: | ---: |
| single-hop | 4 | 63,788 | 21,772 | 22,331 |
| two-hop bridge | 7 | 109,446 | 67,279 | **46,682** |
| aggregation | 5 | 66,562 | 53,922 | 65,894 |
| supersession | 3 | 58,387 | 21,915 | 22,683 |
| distractor | 2 | 64,856 | 44,894 | 34,845 |
| unanswerable | 3 | 64,298 | 22,103 | 39,040 |

## What this shows, and what it does not

- **Same correctness, fewer tokens.** Every arm answered all 24 correctly, so
  this pilot cannot separate the arms on quality: the question set is at the
  host's ceiling on a 130-note vault. What it does show is cost: both hooks
  reached the same answers with 43–46% fewer total tokens and about half the
  turns, consistent with the 0.2 run on a different vault.
- **Where synaptic helps:** on two-hop bridge questions the synaptic hook used
  31% fewer tokens than the FTS hook (57% fewer than baseline), because the
  linked note arrived in the first packet instead of being found by searching.
- **Where it costs more:** on aggregation and unanswerable questions the extra
  passages led the host to read more, and synaptic used more tokens than FTS.
  Its total dollar cost was also higher than FTS ($0.76 vs $0.69) because of
  more cache creation.
- **Limits:** one run, 24 questions, one fictional vault, one model, one judge.
  Treat these as signals, not general claims. A larger vault, where the host
  cannot cheaply grep everything, is the case that matters most and is not
  measured here.
- **Order and shared copy.** The arms always ran in the order baseline,
  hook-fts, hook-synaptic, so the host's prompt caching could favour later arms
  (the cache-read column is most of every arm's total), and the order was not
  varied to check. All arms used the same vault copy, so the baseline host
  could also open the copy's `.context/` directory (index and link graph);
  whether it did was not checked.

## Reproduce

`python3 bench/run_live.py --help`; it is a dry run unless `--run` is given, and
`--write-judge-template` / `--summarize-judged` handle judging. Expect a cost
of the order shown above per 24 questions × 3 arms with `sonnet`.
