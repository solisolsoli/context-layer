# Exact evidence delivery v1

This contract tests whether required source text reached the consumer. It does
not judge truth, relevance, authority, final answers or readiness to go live.
All fixtures in `tests/fixtures/evidence-*.json` are synthetic regression cases.

## Consumer output

The command must print exactly one JSON object:

```json
{
  "schema": "evidence-delivery-v1",
  "operation_status": "ok",
  "status": "PARTIAL",
  "evidence": [
    {"source_path": "docs/policy.md", "source_sha256": "<64 hex digits>",
     "content": "The exact delivered passage, including original line endings."}
  ]
}
```

Paths identify complete relative source identities; basename matching is never
used here. The hash identifies the full source version. Each content string
must be a contiguous, byte-exact UTF-8 span in that frozen version. CRLF and LF
are different bytes. Each delivered entry, including extras, is checked. An
unknown source, fabricated excerpt, wrong version, malformed JSON or operational
failure invalidates delivery for that run. A source name in metadata, an omitted
path, an empty excerpt and a producer's `SUPPORTED` claim earn no credit.

A frozen contract contains `schema: "evidence-contract-v1"`, a `sources` object
mapping each path to `{text, sha256}`, and a `queries` object mapping each stimulus
ID to `{answerable, required_groups}`. A required group is a nonempty list of
alternative witnesses, each with `{source_path, source_sha256, required_text}`.
All groups must be satisfied; any explicitly labelled alternative within a group
suffices. Required text must fit within one delivered excerpt. Combining separate
excerpts to satisfy a single span is not implemented in v1.

Missing label IDs, invalid source digests and impossible required spans reject
the contract before evaluation. Keep labels and source snapshots outside the
retriever's inputs, frozen independently before tuning. Hash identity alone does
not establish that a source is authoritative; that is a labelling responsibility.

## Abstention and failures

`NOT_FOUND` or `ABSTAINED` with empty evidence and `operation_status: "ok"` is an
explicit abstention. If the question is answerable, it is a retrieval miss. If
independently labelled unanswerable (`answerable: false`, no required groups),
it is a correct abstention, reported separately from evidence recall. The
bundled router's exit 2 is accepted only for this successful abstention envelope.
Other nonzero exits and timeouts earn zero hits even when stdout includes names
or apparently valid evidence. Output character cost is retained for failed runs.

## Run

From this directory, substitute your frozen files and absolute vault path:

```sh
python3 evaluate.py \
  --command 'python3 ../router/context_router.py --vault /path/to/vault --evidence-json --no-save --prompt' \
  --stimuli /path/to/stimuli.jsonl \
  --evidence-contract /path/to/frozen-contract.json \
  --out /path/to/delivery-results.json
```

Stimulus rows need `id` and `prompt`; `expected_sources` is optional in this mode.
The supplied command receives the prompt as its final argument. Use the same
output contract and budget for every system in a comparison; a Markdown packet
and JSON evidence output have different costs. This mode has no network or model
calls. It measures characters, not API tokens or money.

Results name `measurement_kind: "evidence_delivery_v1"`. Without the contract,
results name `source_name_diagnostic`. Both set `semantic_quality_measured: false`
and `promotion_eligible: false`. The legacy rubric and numeric gate cannot
supply independent semantic acceptance; `--gate` exits 3 until a separately
reviewed evaluation process exists. The tool has no bypass flag for that check.

`make test` from the repository root exercises negative controls and runs a real
router output through this evaluator. Passing those checks validates the tested
software contract, not an improvement in the live brain.
