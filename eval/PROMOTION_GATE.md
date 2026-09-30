# The promotion gate

**Implementation status (2026-09-20):** `evaluate.py --gate` always fails the
independent semantic acceptance condition. Its default source-name score and
optional exact-span delivery score are diagnostics, not semantic evaluation.
The commands below illustrate the numeric part of a gate; they cannot approve
live promotion. Frozen independent labels, answer-quality assessment and root
review must be established separately. The synthetic examples do not meet that
requirement. See [EVIDENCE_CONTRACT.md](EVIDENCE_CONTRACT.md).

A promotion gate is the rule that decides whether a change to your retrieval or
routing layer is allowed to go live. It exists because "it feels smarter" and
"the packet got smaller" are both things a change can produce while the system
gets worse at its job.

The gate is one sentence:

> **A candidate may not go live unless it beats the measured baseline on every
> condition at once, measured by someone other than the thing being evaluated.**

Everything below is how to make that sentence operational.

---

## 1. First measure the baseline. Do not inherit a number.

Before any candidate exists, run the harness against the system that is
currently live and write the numbers down with the date and the exact command.

```sh
python3 evaluate.py \
  --command "python3 fixtures/demo_router.py" \
  --stimuli stimulus-set.example.jsonl \
  --out baseline.json
```

Re-run the baseline yourself even if a number was handed to you. A baseline you
did not produce is a claim, not a measurement, and the whole gate is calibrated
against it.

Record at least:

- **aggregate hit rate** — expected sources found / expected sources total
- **sub-breakdown hit rates** — the same, split by intent, by confidence, by
  prompt length, by whatever facet distinguishes the work you actually do
- **mean packet cost** — characters per packet (see §5 on why characters)
- **rubric score** — from `score_packet.py`, over applicable axes only

## 2. Turn the baseline into thresholds

Each threshold answers one question.

| Condition | How to set it |
|---|---|
| Aggregate hit rate | At minimum the baseline's. Set it higher only if you are willing to reject a candidate that merely ties. |
| Sub-breakdown hit rates | One per bucket you care about, each at that bucket's baseline. This is the condition that catches an "improvement" that raises the average by getting better at the easy third of your prompts and worse at the part you depend on. |
| Packet cost ceiling | A number you are willing to pay. Set it from the baseline, not from a wish. |
| Rubric score | At minimum the baseline's. |

`gate.example.json` shows the shape. Its numbers came from running the fixture
baseline in this repository and are stated there as such. **Do not copy them.**
They describe a seven-file toy corpus and mean nothing about your system.

Then check a candidate against the gate in one command:

```sh
python3 evaluate.py \
  --command "python3 fixtures/demo_router.py --no-fast-path" \
  --stimuli stimulus-set.example.jsonl \
  --gate gate.example.json \
  --rubric-results rubric-candidate.json
```

`evaluate.py` exits `3` when the gate fails, so this drops straight into CI.

## 3. All conditions, at once

The conditions are joined with AND, never with "on balance". A candidate that
wins four conditions and loses one has lost. This is not strictness for its own
sake: each condition is there because it is the one a plausible-sounding change
tends to quietly trade away.

A worked example from this repository's own fixture, measured, not imagined: a
candidate that forces the largest budget tier reaches a perfect hit rate and a
better rubric score than the baseline, and is **rejected** — it costs 3,292
characters per packet against a 2,900 ceiling, and buys exactly the same hit
rate a cheaper candidate already reached. Paying more for capability you can
get for less is not a promotion.

## 4. The evaluated agent does not score itself

If the same party that wrote the change also defines what counts as a hit, the
gate measures nothing. The failure is rarely dishonesty; it is that whoever
tuned the router has already, unconsciously, tuned the label set to the router's
strengths.

In practice:

- The stimulus set is built **independently** of the change, from real usage,
  ideally from sources the implementer did not select.
- Whoever labels `expected_sources` decides from evidence of what was actually
  needed after each prompt, not from what the router happens to return.
- Whoever runs the final measurement is not the agent or person being evaluated.
- A subagent's report of its own score is a **claim until verified**. The
  verifying party re-runs at least one critical claim at the source or at the
  command line before accepting it.
- The eval's own files must be unreachable by the router. If the stimulus set
  is inside the corpus being indexed, the router can cite the answer key.

## 5. Cost reduction alone is not success

A retrieval layer can always be made cheaper by retrieving less. That is not an
optimization, it is a capability cut with a pleasant-looking metric attached.

So the cost condition is a **ceiling, not an objective**. Nothing in the gate
rewards going lower. Cost enters only as a constraint on candidates that have
already demonstrated they did not lose capability.

The same asymmetry is built into the rubric in `score_packet.py`: the waste axis
is scored last and on purpose. "Less" only counts after "did it carry what the
answer depended on" has been counted.

Two honesty notes about cost:

- This harness measures **characters**, because characters are what it can
  actually count. Characters are not tokens and are not money. If you need a
  token or billing number, measure it with your provider's own counter and
  report it separately. Do not convert with a ratio and call the result measured.
- Cost per hit is reported but is not a gate condition. It is a ratio that can
  be improved by getting worse at both halves at different rates.

## 6. A capability drop is rolled back

If a change ships and a measured capability drops, it is reverted — the same
day, not after a redesign. The candidate goes back to the lab. An unproven
improvement stays in the lab; a proven regression does not get a grace period
while someone investigates.

This is also why the baseline is kept runnable and the previous version is kept
on disk. A rollback you cannot perform in minutes is a rollback you will argue
about instead of doing.

## 7. What the gate does not prove

State these limits next to any result, every time:

- A hit means **the source name appeared in the output**. A router can satisfy
  that cheaply by listing filenames it never included the body of. If that risk
  is live for you, count body inclusion as a second, separate number and report
  both.
- Basename matching (the default) is only sound when basenames are distinctive.
  Use `--match-mode path` when they are not.
- The gate measures whether the **right sources were activated**, not whether
  the final answer was correct. These are different questions.
- Your stimulus set has limits — low-confidence labels, ambiguous versions,
  uneven coverage. Count them and publish the count. Do not change the ruling
  in your own favour after seeing the score.
- Passing the gate is not a delivery, a user approval, or evidence of anything
  being published.

---
