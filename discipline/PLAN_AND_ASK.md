# Plan and Ask

Two habits that prevent most expensive mistakes: plan before editing, and stop to ask
when a wrong guess would be costly. The rule template
([templates/vault/CLAUDE.md](../templates/vault/CLAUDE.md), sections 3 and 4) carries
the same text in short form.

## Plan first

Every task starts with a plan, before any edit:

1. **Goal** — what "done" means.
2. **Sources to read** — notes, commands or URLs, and why each one.
3. **Steps** — short and ordered.
4. **Risks** — what could go wrong, and which stop-and-ask condition applies.
5. **Verification** — what will be checked, and how, before the work is called done.

In Claude Code, plan mode keeps the agent read-only until the plan is approved: press
Shift+Tab to cycle to it, prefix a prompt with `/plan`, or start with
`claude --permission-mode plan`. A project can make plan mode the starting mode by
setting `"permissions": {"defaultMode": "plan"}` in its `.claude/settings.json`. Agents
without a plan mode write the plan as their first message, before any edit.

A trivial request gets a one-line plan. The point is that the goal and the check are
stated before the work, not that every lookup gets a document.

## Stop and ask when risk is probable

Stop and ask the user before acting when any of these holds:

- The action is **destructive, irreversible or outward-facing**: delete, overwrite,
  bulk move, publish, send, pay, push, schedule.
- The instruction is **ambiguous** and two reasonable readings lead to different actions.
- Required evidence is **missing, partial, stale** (its hash changed since it was read)
  or **conflicting**.
- The agent would have to **guess** a fact, a user preference, which file is meant, or
  what was decided before.
- A load-bearing claim rests on **confidence rather than a source**.
- A **sub-agent's claim** has not been verified at its source.
- The plan would change **scope, time or cost** materially.

**How to ask.** One concrete question, the options, and a recommended option with its
reason. "Two watering schedules disagree about the evening slot. Keep
`Watering Schedule v2` as current and archive v1 (recommended: v2 is newer and the
timer decision links it), or keep both searchable?" is answerable in one word. "How
should I proceed?" is not.

## Do not over-ask

Routine research, reading, drafting, critique and local, reversible edits inside the
task's scope need no approval. An agent that asks about everything gets approvals
granted by reflex, and a reflexive approval protects nothing — including the one
question that mattered.

Two limits hold in both directions: **silence is not approval**, and a past approval
covers the action it was given for, not the next outward-facing one.
