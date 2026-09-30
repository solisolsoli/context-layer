# Context-layer evaluation harness

A standard-library harness for retrieval diagnostics and exact evidence delivery.
It counts output characters and can verify that frozen source passages reached
the consumer. It does not establish semantic usefulness or answer correctness.

The default `evaluate.py` mode measures filename/path mentions only. A list of
names can score perfectly without delivering evidence. Use
[`--evidence-contract`](EVIDENCE_CONTRACT.md) to check exact source versions and
required spans instead. The bundled labels are invented regression fixtures,
not an independent benchmark for a real vault.

`score_packet.py` remains a heuristic rubric with manual axes. Its aggregate
must not be presented as an independent semantic score. `--gate` prints numeric
checks but always fails the unmeasured independent semantic acceptance condition;
no result from this harness automatically authorises live deployment.

For a real comparison, freeze the evaluation corpus, versions, questions and
labels independently before tuning. Use identical evidence output interfaces and
budgets for grep, FTS and the router. Report delivery, correct abstention, runtime
failures and cost separately; then perform the independent answer-quality review
and apply the project-specific live-promotion conditions.

## Common-budget comparison

Run `python3 compare.py --out /path/to/new-results` from this directory. This
uses the same frozen source/index snapshot, JSON evidence format and character
bounds for grep, FTS, FTS plus canonical routes, and the experimental router.
The comparison retains actual stdout/stderr for every case. See
[comparison/README.md](comparison/README.md) and [results](comparison/results.json).
The older adapters below remain source-name diagnostics with differing formats.

## Files

| File | What it is |
|---|---|
| `evaluate.py` | Source-name diagnostics or exact evidence delivery + cost; cannot authorise promotion. |
| `evidence_contract.py` | Frozen source/version/span validation and separate abstention scoring. |
| `EVIDENCE_CONTRACT.md` | Delivery JSON contract, label schema and runnable command. |
| `score_packet.py` | The 7-axis intelligence rubric. |
| `stimulus-set.example.jsonl` | 12 invented example rows in the real schema. |
| `cases.example.json` | 6 invented rubric cases with their required patterns. |
| `gate.example.json` | A filled-in gate, with the measured baseline it came from. |
| `packet-format.example.json` | Regexes describing *your* packet's structure. |
| `live_compare.py` | Runs a held-out case set through a headless host twice (host alone vs host + context-layer MCP) and records tokens, cost, time, delivery and abstention per case. |
| `LIVE_COMPARE.md` | What each arm may do, which host fields are measured, how judgements are merged, limits. |
| `LIVE_PILOT_0.3.md` | The 0.3 live host pilot on the fictional benchmark vault (24 sealed questions x 3 arms, blind-judged): setup, results, limits. |
| `live-pilot-0.3/` | That pilot's answers, blind-judging files and case selection. In the repository only, not in the distributions. |
| `orchestration_cost.py` | Estimated sub-agent payload tokens on a generated synthetic vault ([docs/subagents.md](../docs/subagents.md) section 5). |
| `heldout-cases.example.jsonl` | 3 invented rows in the held-out case schema over the fixture docs; a real set stays private. |
| `fixtures/demo_router.py` | A fake 150-line router, so everything here runs. |
| `fixtures/docs/` | A seven-file fictional documentation vault. |
| `fixtures/gen_packets.py` | Renders one packet per rubric case for batch scoring. |
| `PROMOTION_GATE.md` | How to set thresholds and why each condition is there. |
| `ABLATION.md` | How to attribute a capacity loss to one layer. |
| `DIAGNOSE.md` | **Start here if you have a vault and no router.** How to find out whether your own setup retrieves the right things. |
| `adapters/` | Ready-made adapters — grep, SQLite FTS5, and a template for an embedding setup — so any system can be measured. |
| `BENCHMARK_FIXTURE.md` | grep vs BM25 vs the fixture router, measured on `fixtures/`, including the result that does not flatter the router. |
| `bench_fixture.sh` | Reproduces that comparison in one command. |

## Quickstart

Everything below runs as written, from this directory.

```sh
# 1. hit + cost against the fixture router
python3 evaluate.py \
  --command "python3 fixtures/demo_router.py" \
  --stimuli stimulus-set.example.jsonl \
  --out baseline.json

# 2. rubric: render a packet per case, then score them
python3 fixtures/gen_packets.py --cases cases.example.json \
  --command "python3 fixtures/demo_router.py" --out-dir packets
python3 score_packet.py --cases cases.example.json \
  --batch-dir packets --out rubric-baseline.json

# 3. score a candidate the same way, then check it against the gate
python3 fixtures/gen_packets.py --cases cases.example.json \
  --command "python3 fixtures/demo_router.py --no-fast-path" --out-dir packets-cand
python3 score_packet.py --cases cases.example.json \
  --batch-dir packets-cand --out rubric-candidate.json

# exit code 3 = gate failed = the candidate does not go live
python3 evaluate.py \
  --command "python3 fixtures/demo_router.py --no-fast-path" \
  --stimuli stimulus-set.example.jsonl \
  --gate gate.example.json --rubric-results rubric-candidate.json
```

Real output tail from step 1 (the numbers are the same as on 2026-09-19, when
this section was first written; the labels have changed since, and
`tests/test_doc_claims.py` reruns step 1 and compares every number below):

```
=== SUMMARY ===
measurement: source_name_diagnostic (semantic quality not measured)
prompts evaluated:            12
router non-zero-exit/timeout: 0
aggregate requirement hit-rate:    15/17 (88.2%)
prompts with ALL sources hit: 10/12
prompts with ZERO sources hit:0/12
total packet cost (chars):    26127
mean packet cost (chars):     2177
cost per hit (chars):         1742
```

## Flags of `context-layer search` and `context-layer eval`

`context-layer search` defines `--prompt` and `--method`; every other flag is
forwarded verbatim to [retrieve.py](retrieve.py) (`python3 eval/retrieve.py
--help`). A flag that only one method applies is marked; `docs/cli.md` has the
exit codes.

| Flag | Default | Applies to | What it does |
| --- | --- | --- | --- |
| `--prompt TEXT` | required | all | the question |
| `--method M` | `fts` | all | `grep`, `fts`, `fts-canonical`, `router` or `synaptic` |
| `--top-k K` | 3 | all | at most K sources in the packet |
| `--budget CHARS` | 6000 | all | total evidence characters (JSON framing not counted); for synaptic, the budget of its fts part |
| `--per-source CHARS` | 2000 | all | at most this many characters from one source |
| `--delivery window\|prefix` | `window` | fts, fts-canonical, default synaptic (its fts part) | `window`: a note whole when it fits `--per-source`, else the match-anchored verbatim windows that hold query terms; `prefix`: the note's first `--per-source` characters (the 0.3 behaviour) |
| `--name-fields` | off | fts, fts-canonical, synaptic (its fts part) | needs an index built with `context-layer index --name-fields`: fuses the content ranking with the notes whose file name, frontmatter aliases or headings match a query term (reciprocal rank, k = 60); an index without the fields is an error that names the command; see [source-lifecycle.md](../docs/source-lifecycle.md#note-names-aliases-and-headings-opt-in) |
| `--extra-tokens N` | 600 | synaptic | estimated-token budget, ceil(chars/4), for link-graph extras added after the unchanged fts packet |
| `--compact` | off | synaptic | the compact packer: passages instead of the fts packet, one `--budget-tokens` budget; smaller, may drop fts evidence |
| `--budget-tokens N` | 1200 | synaptic with `--compact` | packet budget in estimated tokens |
| `--max-hops 1\|2` | 1 | synaptic | link hops activation may spread |
| `--record-query` | off | synaptic | store the query text in `.context/activation.json` (not stored by default) |
| `--jev-candidates N` | 0 | fts and default synaptic | add a `jev_candidates` side channel with up to N undelivered notes for the optional advisor ([docs/jev.md](../docs/jev.md)); the evidence is unchanged |
| `--prompt-file PATH` | none | `eval/retrieve.py` only (`context-layer search` requires `--prompt`) | read the prompt from a UTF-8 file (`-` reads standard input) instead of the last argument |

`bench/run_offline.py` refuses to forward a flag to a method that would not
apply it, and maps its own `--budget-tokens N` onto these flags per method (see
[bench/README.md](../bench/README.md)).

`context-layer eval` defines `--cwd DIR` (the directory to run from; default
this `eval/` directory, so the example paths resolve) and forwards everything
else to [evaluate.py](evaluate.py):

| Flag | Default | What it does |
| --- | --- | --- |
| `--command CMD` | required | retrieval command; the prompt is appended as the last argv token |
| `--stimuli FILE` | `stimulus-set.example.jsonl` | labelled prompts, one JSON object per line |
| `--evidence-contract FILE` | none | score the evidence-delivery-v1 JSON against frozen sources and spans ([EVIDENCE_CONTRACT.md](EVIDENCE_CONTRACT.md)) |
| `--match-mode basename\|path` | `basename` | how a name-mention hit is matched (diagnostic mode only) |
| `--facets F1,F2` | `intent_type,confidence,length_bucket` | stimulus fields to break results down by |
| `--filter FIELD=V1,V2` | none | keep only rows whose FIELD is one of the values; repeatable |
| `--limit N` | all | only the first N rows |
| `--timeout SECONDS` | 90 | per-prompt limit; a timeout is a miss and is reported |
| `--packets-dir DIR` | none | keep each case's stdout and stderr |
| `--out FILE` | none | full JSON results |
| `--gate FILE` | none | check the run against a promotion gate (exit 3 when it fails; it always fails independent semantic acceptance) |
| `--rubric-results FILE` | none | `score_packet.py` output for the gate's rubric condition |
| `--quiet` | off | no per-case progress lines |

## Measuring a setup that is not this router

`--command` runs any program that turns a prompt into retrieved context, so this
is a general instrument, not a test suite for one router. If you have notes and
some way of searching them, you can find out whether that search returns the
right things — and whether it beats plain grep, which is a question most people
cannot answer about their own second brain.

```sh
# your vault, the control everyone should beat
python3 evaluate.py \
  --command "python3 adapters/grep_baseline.py --vault ~/my-vault --top-k 5" \
  --stimuli my-stimuli.jsonl

# your vault, a real ranked full-text index, still stdlib-only
python3 evaluate.py \
  --command "python3 adapters/fts_sqlite.py --vault ~/my-vault --top-k 5" \
  --stimuli my-stimuli.jsonl
```

The full walkthrough — building a labelled stimulus set from your own questions,
labelling it without cheating, reading the result, and what to do about each
failure mode — is [DIAGNOSE.md](DIAGNOSE.md). How to wrap your own setup in
about twenty lines is [adapters/README.md](adapters/README.md). A worked
comparison on the fixture vault, with its inconvenient result published, is
[BENCHMARK_FIXTURE.md](BENCHMARK_FIXTURE.md).

## Adapting it to your system

1. **Wire up your router.** `--command` is run per prompt with the prompt
   appended as the final argv token. Include whatever flag keeps your router
   read-only; this harness adds none.
2. **Build your stimulus set.** Same JSONL schema as the example: `id`,
   `prompt`, `expected_sources`, plus any facets you want breakdowns by
   (`--facets`). Verify every path in `expected_sources` exists before you
   score anything; a label pointing at a file that is not there is a silent
   permanent miss. Mark the confidence of each label and keep the low-confidence
   rows rather than dropping them — record why they are low.
3. **Describe your packet format.** Copy `packet-format.example.json` and change
   the regexes to match the block and provenance shapes your router emits.
4. **Write rubric cases.** One per representative prompt, with the patterns that
   would prove the packet carried what the answer depended on. Keep the
   `source_path` of every pattern: a required fact you cannot point at in an
   original is not a required fact.
5. **Measure your baseline, then set the gate.** See
   [PROMOTION_GATE.md](PROMOTION_GATE.md).

## Honesty rules this harness enforces on itself

- Nothing is scored that cannot be checked. Un-automatable axes come back
  `MANUAL`, inapplicable ones `N/A`, and neither is folded into the total.
- A fabrication zeroes the **entire** case, not one axis.
- The waste axis is a labelled proxy, and it is scored last. "Less" counts only
  after capability has been counted.
- Characters are measured; tokens, latency and cost in money are not claimed.
- A hit means a filename appeared in the output — not that its body was
  included, and not that the answer was right.
- The example numbers in this repository were produced by running this code
  against `fixtures/`. They describe a seven-file toy corpus. They are not
  benchmarks and they say nothing about your system or anyone else's.

## Provenance

This is a de-personalized extraction of a private measurement harness built to
decide whether a change to one person's context-retrieval layer could go live.
The method is preserved; every project-specific identifier, path, corpus and
real user prompt is not. Example rows, fixture documents and rubric cases here
were invented for this repository. The released examples and comparison contain synthetic sources only.
