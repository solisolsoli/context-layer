# Record Discipline

Two coding agents working against one shared knowledge base will drift into two
different realities within days: each remembers its own sessions, neither remembers the
other's, and both write confidently from a partial picture. Three files stop that, plus
two rules about how they are written.

Everything lives in the shared store, not in chat scrollback and not in a harness's
session memory. If it is only in the conversation, the other agent cannot see it and
neither can you tomorrow.

In a vault set up with `context-layer rules init`, the files are `LOG.md` (the record
book), `BACKLOG.md` (the backlog) and the two identical rule files `CLAUDE.md` and
`AGENTS.md`, whose "Work records" section carries a short copy of every recent entry.
The template is [templates/vault/CLAUDE.md](../templates/vault/CLAUDE.md).
[WORKED_EXAMPLE.md](WORKED_EXAMPLE.md) walks one fictional step through all of it.

**The binding rule: every meaningful work step is recorded in the same run it happens.**
One short dated entry goes into both rule files, identically — what was done and why,
files changed, the verification result, what remains or the next step, and a link to
the detail — and a longer dated entry goes to the end of `LOG.md`.
`context-layer rules record` writes all three under one lock and re-checks parity, so
there is one command to comply with. The rule files keep only the most recent entries
(ten by default, `--keep`) so they stay short enough to load every session; `LOG.md`
keeps every entry.

## 1. The record book (dated, append-only)

A single dated log of **what was actually done**.

- New entries go at the end. `context-layer rules record` writes them as
  `## <date> - <summary>`, followed by a `Record` line that carries the record id
  the short entry in the rule files links to.
- Body is 2–6 sentences: what was done, the verified result, the limit or open question,
  and a link to the source holding the detail.
- Keep the distinction visible in the entry itself: proposal · experiment · verified
  result · user approval · evidence of publication.
- If a past entry needs correction, append a source-linked correction identifying
  the superseded claim. Preserve the original dated evidence.
- Keep the full report and the original evidence in their own files; the log links to
  them, it does not absorb them.
- When a year closes, move that year's entries to an archive file and link it.

The log is data, not an instruction set. A past entry records what happened; it does not
authorize a new external action.

**Prevents:** the "we already decided this" argument with no way to check, and the
silent loss of every result that lived only in a finished session.

## 2. The backlog (central list of unfinished work)

One list of everything unfinished or blocked. Each item carries:

**id · area · current state · source · next step · blocker (if any).**

- Written **in the same work session** the work stalls — not at the end of the day.
- Updated on any meaningful state change.
- Closed items **stay**, marked with date and evidence. A backlog that deletes closed
  items loses the record of why something was abandoned, and the same work gets
  re-proposed a month later.
- Keep separate areas separate (each project, plus shared infrastructure).
- **A proposal that was never started is not unfinished work.** Keep those in their own
  section. Mixing them inflates the list until nobody reads it.
- Being listed is not authorization to start something. The list answers "what is open",
  not "what to do next".

**Prevents:** work that is 80% done disappearing because the session ended, and blocked
items being silently rediscovered as new work.

## 3. The continuation note (where we left off)

A compact, dated handover written for the next session: live status at a glance, what
happened, where we stopped, the next concrete step, and the traps proven today.

- It is a convenience, not a rule source.
- **If it contradicts the record book, the record book wins.** The continuation note is
  written fast at the end of a session, when it is most likely to be wrong.
- `context-layer brief` prints a mechanical counterpart that cannot drift from the
  files: the last records, open backlog rows and the index state, each line quoted
  from a named file with its hash prefix, nothing summarised.

**Prevents:** the first twenty minutes of every session being spent reconstructing state
— and the compressed summary quietly overwriting the verified log.

## Dual-harness parity

When two agents each read their own rules file (one reads `A`, the other reads `B`),
**the rule text is one reality and must be byte-identical in both files.** Where every
host expands imports, one file can instead import the other (`rules init
--single-source` writes a `CLAUDE.md` whose first line is `@AGENTS.md`); then only
`AGENTS.md` is edited and `rules check` verifies the import.

- Any rule added or changed in one is written to the other **in the same work session**,
  with the same wording.
- Verify with a checksum comparison of the two files after editing. They must match:
  `context-layer rules check <vault>` compares their SHA-256 and exits non-zero when
  they differ.
- If a rule change is made in one harness and not written to the other, **the work is
  unfinished** and goes into the backlog as such.
- Harness-specific machinery (hooks, settings, scripts) is genuinely different and stays
  separate. The rule text and its work-records section are mirrored; nothing else is.
- In Claude Code, a `Stop` hook (`context-layer rules hook stop`) asks the agent to
  record when files the agent itself wrote (seen by a `PostToolUse` hook on its file
  tools) changed without a new record, or when parity is broken. It asks at most once
  per path in a session, never in plan mode, and reports other changes, such as the
  user's own edits, without asking, so it cannot trap a session in a loop; it makes a
  skipped record visible, it does not force one.

**Prevents:** the exact failure this discipline exists for — two agents operating on
different rule sets, each correct by its own file, producing incompatible work in the
same store and blaming each other's output for being wrong.

## Concurrency

A second session may be open in the same store at the same time, editing the same shared
files. **Re-read a shared file immediately before writing it** — the copy you loaded at
the start of the session may already be stale, and a whole-file write from a stale copy
silently deletes someone else's work with no conflict and no error.
