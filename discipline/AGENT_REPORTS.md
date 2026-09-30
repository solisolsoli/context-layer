# Agent Reports

**A subagent's report is a claim until it is verified at the source.**

Not a lie, not usually even careless — a claim. It was produced by a model that read
some of the material, summarized under pressure, and wrote a confident paragraph. The
confidence is free; the verification is not.

## Failure patterns to check

Verify counts, source paths, universal assertions and whether a worker stayed
within its assigned write scope. A plausible report can still contain material
errors. Preserve its raw output so the coordinator can reproduce the finding.

## The rule that follows

**The orchestrator personally opens at least one critical claim at its source before
recording it.** Not a sample of the report — the source behind the claim the decision
rests on. If the report says a count, count. If the report says "all", open the ones
that would falsify it.

**Delegate the grunt work; never delegate the verification of the result.**

Delegate: mechanical passes, measurement runs, scanning and grepping, extraction,
formatting, repetitive gate runs. These are the tasks where an error is visible when you
check the output, and where cheap models are genuinely good.

Do not delegate: the judgment that the result is correct, and the writing of anything
that will become the record. No agent grades its own work: an agent verifying its own work confirms its own
assumptions. An agent verifying a sibling's work adds a second unverified claim.

## Supporting rules

**Bound the task.** State the question, the sources the agent may read, what it may
write (default: nothing) and the output format. An unbounded brief produces an
unverifiable report.

**Scope the agent's authority explicitly.** "Read and report" means read and report; an
agent that can edit will sometimes edit, and helpful unrequested edits are still
unrequested. Give write access only for tasks whose whole point is writing.

**Universal claims get verified first.** "All", "none", "every", "no remaining" — these
are cheap for an agent to produce from a partial pass and expensive for anyone to undo.
Treat them as the highest-priority verification target.

**Ask for sources, not conclusions.** A report that says "file X, line Y says Z" can be
checked in seconds. A report that says "this is consistent across the project" cannot be
checked at all without redoing the work.

**Record the claim with its verification status.** When a verified claim and an
unverified one are written into the log the same way, the distinction is gone the moment
the session ends — and next week the unverified one is being cited as established.
