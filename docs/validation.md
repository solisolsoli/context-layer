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

