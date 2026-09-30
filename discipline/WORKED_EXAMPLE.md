# Record discipline: one worked example

One fictional working step in a starter brain (`context-layer brain init`),
from plan to record, with what each tool prints or writes. The garden, the
dates, the record id and the hashes are example values; the formats are the
ones the tools produce. The rules behind it are in
[RECORD_DISCIPLINE.md](RECORD_DISCIPLINE.md), [PLAN_AND_ASK.md](PLAN_AND_ASK.md)
and [EVIDENCE_GATE.md](EVIDENCE_GATE.md).

## 1. Plan (before any edit)

The user asks: "Add an evening watering slot for very hot days." The agent,
in plan mode, answers with a plan first:

```text
Goal: the schedule note has a short evening slot for days above 32 C.
Sources: 500-Knowledge/Example - Watering Schedule v2.md (the current schedule).
Steps: read the note, add one line, re-read it.
Risks: none from the stop-and-ask list; a local, reversible edit.
Verification: re-read the note; rules check.
```

## 2. Evidence, labelled

`context-layer search . --prompt "watering schedule hot days"` returns the
schedule note verbatim with its path and SHA-256. The agent writes:
"SUPPORTED: the schedule waters at 19:00 on days above 28 C
(`500-Knowledge/Example - Watering Schedule v2.md`, sha256 3f2a9c1b7d4e).
USER_STATED: a second slot is wanted above 32 C."

## 3. The edit, then the Stop hook

After the plan is approved, the agent adds one line with its Edit tool. The
PostToolUse hook remembers that path as the agent's. The user meanwhile fixes a
typo in the daily note in Obsidian. When the answer ends, the Stop hook asks
once, about the agent's edit only:

```text
1 vault path(s) the agent wrote in this session changed and no work record was
appended: 500-Knowledge/Example - Watering Schedule v2.md. Run: context-layer
rules record <vault> --summary "<what was done and why>" --files <changed paths>
--verified "<how it was checked>" --next "<what remains>". If the change was not
meaningful, say so in one line and stop again.
```

and tells the user, without asking the agent anything:

```text
context-layer rules: 1 vault path(s) changed in this session that the agent's
file tools did not write (not asked about): daily/Example - 2026-01-15.md.
```

## 4. The record

```sh
context-layer rules record . --summary "Added a hot-day evening watering slot" \
  --why "the timer has a spare program and beds dry out above 32 C" \
  --files "500-Knowledge/Example - Watering Schedule v2.md" \
  --verified "re-read the schedule note; rules check OK" \
  --next "set the second timer program"
```

The short entry lands in the "Work records" section of the rule text:

```text
### 2026-01-16 09:12 UTC - Added a hot-day evening watering slot
- Why: the timer has a spare program and beds dry out above 32 C
- Files: `500-Knowledge/Example - Watering Schedule v2.md` (997fe8cf6883)
- Verified: re-read the schedule note; rules check OK
- Next: set the second timer program
- Detail: [[LOG#^r-20260116t091200z-4df846]]
```

and the long one at the end of `LOG.md`:

```text
## 2026-01-16 - Added a hot-day evening watering slot

Record `r-20260116t091200z-4df846` | 2026-01-16T09:12:00Z | tool `cli` ^r-20260116t091200z-4df846

- What was done: Added a hot-day evening watering slot
- Why: the timer has a spare program and beds dry out above 32 C
- Files changed: `500-Knowledge/Example - Watering Schedule v2.md` (997fe8cf6883)
- Verification: re-read the schedule note; rules check OK
- Remaining / next step: set the second timer program
```

The next Stop asks nothing: a new record closes the stretch of work.

## 5. What stays open

Setting the timer is physical work nobody can do from the vault, so it goes
into `BACKLOG.md` in the same run:

```text
| B-2 | garden | open | [[LOG]] | Set the second timer program | none |
```

## 6. The next session

`context-layer brief .` (or the SessionStart hook with `--brief`) quotes where
things stand, each line pinned to a file and its hash prefix:

```text
- log `LOG.md` dca2bf5e: 2026-01-16 - Added a hot-day evening watering slot [r-20260116t091200z-4df846]
- open `BACKLOG.md` 526ac678: | B-2 | garden | open | [[LOG]] | Set the second timer program | none |
```

## What the example does not show

The record says how the change was checked; it does not prove the change was
right. The hooks make a skipped record visible; they do not write a good one.
An edit made through a shell command would have been reported like the user's
typo fix, not asked about.
