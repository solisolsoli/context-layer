# Inspection ledger: every run of the sealed set

`AGENTS.md` asks the documentation to say how often the sealed cases were
looked at. This file lists every run of `bench/cases.jsonl` against the
fictional vault that the repository's history or its maintainers' records know
of, in order. A run is listed even when nobody changed anything because of it.

Kinds:

- **baseline**: only the lexical baselines (grep, fts) ran; no candidate method.
- **design look**: a candidate retrieval design was measured while it could
  still change. These are the runs that count against "no tuning on the sealed
  set", and the README states their number.
- **verification**: the same code was rerun to check that the committed
  numbers reproduce; no design change followed or could follow.
- **live**: sealed questions were sent to a live host.
- **planned**: announced, not yet run.

"Commit" is the checkout that was measured when the repository records it
(the `Checkout:` line of a committed `results/SUMMARY.md`), and the commit that
recorded the result. "Not recorded" means the run left no file in git; its
date and result come from the maintainers' run notes.

| # | Date | Kind | Checkout measured (recorded in) | What ran | Purpose and result seen | Could the result influence retrieval design? |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 2026-09-24 | baseline | `f62afe7` (recorded in `e2a3c24`) | `run_offline.py --methods grep,fts` | Record the two baselines after the seal (`d11c8b0`). grep 37/64, fts 33/64; both 0/20 on bridge cases, as the case construction guarantees. | Possible: per-case baseline results were visible while the synaptic method was still being built. No candidate was measured. |
| 2 | 2026-09-24 | design look | not recorded (the synaptic code before its bridge work) | default run incl. synaptic | Mechanism diagnosis, aggregate only: synaptic 34/64, bridge 0/20. The linked answer note was activated but not selected into the packet. | Yes: it prompted the bridge-packing work, which was developed and measured on the separate dev set (`tests/fixtures/dev_bridge.py`), not on sealed cases. |
| 3 | 2026-09-24 | design look | `3bedf0a` (recorded in `06a8621`) | default run | Interim run after the merge: synaptic 37/64, bridge 10/20, but single-hop 10/14 where fts had 14/14. | Yes: it led to the superset rule (the synaptic packet contains the unchanged fts packet plus extras), a principle-level change, not a case-level one. Today's `--compact` packer is the packer measured here; the README's `synaptic --compact` row equals this run. |
| 4 | 2026-09-24 | design look | `f0cce2b` (recorded in `19a3f3c`) | default run | Final run of the 0.3 design: synaptic 46/64, fts 33/64, grep 37/64 (the README table). | No retrieval design change followed it. |
| 5 | 2026-09-24 | live | `b4f9693` (recorded in `b3995e4`, `eval/live-pilot-0.3/`) | 24 of the 72 questions x 3 arms through a live host (`bench/run_live.py`) | Cost and correctness pilot after run 4; answers judged blind; see `eval/LIVE_PILOT_0.3.md`. | No: it came after the final design run and changed no retrieval code. |
| 6 | 2026-09-25 | verification | not recorded (the release candidate) | `make bench` during a from-scratch walk-through, and one maintainer rerun | Both reproduced run 4 exactly (46/33/37; 388.3/332.7/526.6 est. tokens; p = 0.0002). The rewritten SUMMARY differed only in its `Checkout:` line and was restored. | No. |
| 7 | 2026-09-28 | verification | `ebb88ba` | audit E: the default run, a hash-seed variant, `--methods synaptic --search-arg=--compact`, `--budget-tokens 400`, and the default run on CPython 3.10, 3.11, 3.13 and on a case-sensitive file system | Reproducibility audit. Every default run matched run 4 except the `Checkout:` line; `--compact` gave 37/64, bridge 10/20, 333.9 est. tokens; `--budget-tokens 400` changed nothing, which exposed that the flag was ignored (fixed afterwards in the runner). | No. |
| 8 | 2026-09-28 | verification | `ebb88ba` plus runner and scorer changes (branch `wt/bench-ci-release`) | the default run; `bench/test_runner.py` (cases S01 and B01 at a 50-token budget); `--methods synaptic --search-arg=--compact` for the README check; a dry run of `bench/run_live.py --only S01` (its hook preflight sends the first question through each hook; no host is called) | Check that the new scorer (errors kept in the denominator, verbatim-span check, provenance fields, TREC export) reproduces the committed numbers: the SUMMARY matched except the `Checkout:` line, and `--compact` matched the README row (37/64, bridge 10/20, 333.9 est. tokens). The 50-token runs checked packet sizes and flags only; the preflight reported only that each hook ran. | No: that branch changes no retrieval code. |
| 9 | 2026-09-29 | design look | the merged 0.4 tree (`integration`, retrieval folding contract, byte-identical duplicates collapsed, complement windows on every seeded synaptic search) | the default run, compared with the committed summary | Completeness unchanged for every method (grep 37, fts 33, synaptic 46/64; bridge 10/20); the synaptic mean est. tokens moved 526.6 → 527.1 (aggregation family 639 → 641). The summary was regenerated from this run. | Yes: the result of a design change was seen at aggregate level. |
| 10 | 2026-09-29 | design look | `989b439` (the 0.4 tree: fts window delivery, the advisor wired into search, the hook and MCP) | the default run (grep, fts, synaptic) into a fresh directory, plus `--methods fts --search-arg=--delivery=prefix` and `--methods synaptic --search-arg=--compact` | Every number of the default run equals run 9 (the Checkout line aside): grep 37/64, fts 33/64, synaptic 46/64, bridge 10/20, synaptic mean 527.1 est. tokens. The fts prefix arm equals the fts window arm on this vault: every note fits `--per-source`, so both deliver whole notes. Compact arm 37/64, bridge 10/20, 333.9 est. tokens (the README row). No advisor arm: no recording of a live provider exists, and the fake provider would measure nothing. The summary was regenerated from this run. | Yes: the fts delivery change was seen at aggregate level (no change on this vault). |
| 11 | 2026-10-07 | verification | `ba55323` (in-process retrieval, warm MCP worker, focused delivery for the hook; default search packets meant to be unchanged) | the default run (`python3 bench/run_offline.py`) | Check that default packets are unchanged after the speed and token work: the SUMMARY matched the committed one except the `Checkout:` line (grep 37, fts 33, synaptic 46/64; bridge 10/20). The summary was not regenerated. | No: default search packets did not change, and no design was chosen from it. |
| 12 | planned | planned | a later version | one run with an advisor arm (`--search-arg=--jev` with a recorded provider) once a recording of a live provider exists | To be added here, with its result, when it happens. | Yes, by definition; it will be counted as a design look. |

**Design-time looks at a candidate so far: 5** (runs 2, 3, 4, 9 and 10).

## Standing verification runs in CI

These run on every push and pull request once hosted CI is on. They compare
against committed numbers and fail on any difference; nobody reads per-case
results there, so they are verification, not design looks:

- the `sealed benchmark (offline)` job runs `bench/test_runner.py`: two sealed
  cases at a 50-token budget, checking packet sizes, flags and output formats,
  never completeness;
- the `bench-reproduce` job runs the default benchmark and the `--compact`
  arm, and fails unless `results/SUMMARY.md` and the README's benchmark
  numbers are reproduced (the `Checkout:` line is ignored).

## Rules for the next runs

- Add a row before relying on a new run: date, kind, checkout, command, what
  was seen, and whether it can change a design.
- A design look is spent the moment its result is seen. Changes to the cases or
  the vault are a new sealed version, never an edit of this one.
