# Measurements and limits

This guide collects the results behind Context Layer. Start with the
[README](../README.md) for usage and the [support matrix](../SCOPE.md#support-matrix)
for tested platforms. Retrieval checks, live-host studies and answer quality
are different kinds of evidence; the limits below are part of each result.

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
tokens. Every run of the sealed set is listed in [bench/INSPECTIONS.md](../bench/INSPECTIONS.md): five design-time looks at aggregate level, the rest verification reruns; no case-level tuning was done. [bench/README.md](../bench/README.md)

**Live host runs** (Claude Code headless, read-only tools):

- *0.2, one private vault, 12 held-out questions:* the FTS prompt-hook arm
  matched the host's own judged correctness using about **54% of the baseline's
  mean total tokens (about 46% fewer)** and half the turns. One run, N = 12.
  [eval/LIVE_COMPARE.md](../eval/LIVE_COMPARE.md)
- *0.3 pilot, fictional 130-note vault, 24 questions × 3 arms, blind-judged:*
  all three arms answered 24/24 correctly; mean total tokens per question were
  77,160 (host alone), 43,729 with the FTS hook (57%) and 41,685 with the
  synaptic hook (54%), with about half the turns. On two-hop bridge questions
  the synaptic hook used 31% fewer tokens than the FTS hook; on aggregation and
  unanswerable questions it used more. One run, N = 24, correctness at ceiling.
  [eval/LIVE_PILOT_0.3.md](../eval/LIVE_PILOT_0.3.md)

### Focused hook delivery

`--delivery focus` (the prompt hook's default since this version) delivers the
blocks that hold query terms with their same-section neighbours, and in synaptic
reserves only strongly activated linked notes. Development measurements only:
`python3 tests/dev_bridge_eval.py --methods fts synaptic --hook --delivery D
[--extra-paragraph]`, on the synaptic development set (56 labelled questions,
written alongside the code); not measured on the sealed benchmark. "Complete" =
every required span verbatim in the hook's `additionalContext`; characters = its
mean length.

| packet in the hook | dev set | dev set, `--extra-paragraph` |
| --- | ---: | ---: |
| fts, `window` (the 0.4 hook default) | 12/56, 1,188 chars | 12/56, 1,990 chars |
| synaptic, `window` | 56/56, 2,041 chars | 56/56, 3,299 chars |
| fts, `focus` (the hook default now) | 12/56, 1,033 chars | 12/56, 1,041 chars |
| synaptic, `focus` (`hook --method synaptic`) | 56/56, 1,602 chars | 56/56, 1,898 chars |

`--extra-paragraph` appends one paragraph of invented filler words under a new
heading to every note, so notes are about twice as long. Where notes are as short
as in the plain set, the focused synaptic hook costs more than the 0.4 fts hook
(1,602 vs 1,188 characters) and answers 56 questions instead of 12; it is opt-in (`install ... --hook --method synaptic`). Focus can
miss an answer that shares no word with the question and is not next to a block
that does, in the same section.

### Opt-in relevance floor

`--relevance-floor R` (search and hook; off by default) leaves out a top-k note
whose bm25 is weaker than R times the strongest one. On the synaptic development
set (`python3 tests/dev_bridge_eval.py --methods synaptic -- --relevance-floor R`,
written alongside the code, not a benchmark) the default synaptic packet stays
56/56 complete at R = 0.3 and R = 0.5 while its mean falls from ~282 to ~219
(R = 0.3) and ~169 (R = 0.5) est. tokens. It can drop a note that held the
answer when a stronger-scoring note outranks it; it is not measured on the
sealed benchmark, so it is not a default.

**Sub-agent payloads** (`python3 eval/orchestration_cost.py --standin`,
synthetic setup, estimates): four workers' initial payloads 16,827 → 9,441
est. tokens with per-worker compact packets; the coordinator's verification reading
12,452 → 2,580; the one planted fabricated quote was caught (re-measured after the compact-packet change). Not billed tokens
and not quality evidence. [docs/subagents.md](subagents.md)

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

