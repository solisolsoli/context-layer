# Build your own AI brain

A practical guide to running an AI "second brain" on a plain Markdown or
Obsidian vault with context-layer: your notes stay the source of truth, agents
retrieve verbatim evidence from them, and every agent works from the same rules
and leaves a record of what it did.

- [Who this is for](#who-this-is-for)
- [Setup in ten minutes](#setup-in-ten-minutes)
- [Create your brain](#create-your-brain)
- [The rule files](#the-rule-files)
- [The session brief](#the-session-brief)
- [The citation check](#the-citation-check)
- [The daily loop](#the-daily-loop)
- [Write notes that retrieval can use](#write-notes-that-retrieval-can-use)
- [Working with sub-agents](#working-with-sub-agents)
- [What this will and will not do](#what-this-will-and-will-not-do)
- [Use with Avenox Beyin](#use-with-avenox-beyin)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Credits](#credits)

## Who this is for

You keep notes in Markdown (Obsidian or any editor), you work with an AI coding
agent such as Claude Code or Codex, and you want the agent to:

- answer from your notes, with the file and hash it quoted, instead of from its
  own recall;
- follow one rule set, whichever agent you open;
- plan before it edits, ask before anything risky, and write down what it did.

You need Python 3.10 or newer (with SQLite FTS5, which standard CPython builds
include). Nothing else is installed: the package has no runtime dependencies
and makes no network calls by default. The [advisor](jev.md) and
[GitHub context](github-context.md) are separate opt-ins.

## Setup in ten minutes

### 1. Install the package

From a checkout of this repository:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
context-layer --version
```

### 2. Get a vault

Either create a new starter vault (next section):

```sh
context-layer brain init ~/Brain            # shows what it would create
context-layer brain init ~/Brain --apply    # creates it
```

or use the vault you already have, and let `init` propose a configuration:

```sh
context-layer init ~/Notes --print-only     # see the guesses, write nothing
context-layer init ~/Notes                  # write .context/routes.json
```

Open `.context/routes.json` and check `exclude_prefixes`: every folder listed
there is left out of search. Archives, generated files and anything private
belong in it. An entry matches a folder or file by whole path components from
the vault root, ignoring case (`400-Records/` covers everything under that
folder, `CLAUDE.md` only the root file of that name, and neither matches
`400-Records-old/`).

### 3. Build the index

```sh
context-layer index ~/Brain
```

This builds the full-text index under `.context/`. Re-run it after notes change;
a note that changed since the last index is withheld from results (the packet
lists it under `withheld`, with `context-layer index <vault>` as the next step)
rather than served with a hash that no longer matches. The other results are
still delivered.

### 4. Install the rule files

`brain init` already did this. For an existing vault, run it after `init`:

```sh
context-layer rules init ~/Notes            # dry run: what would be written
context-layer rules init ~/Notes --apply
context-layer rules check ~/Notes           # exit 0 = CLAUDE.md and AGENTS.md hold one rule text
```

`rules init` never overwrites a file you already have. It shows a diff as a merge
suggestion and leaves the file alone; `--force` replaces it after writing a
`<name>.bak-<UTC stamp>` backup. If you already have a `CLAUDE.md` but no
`AGENTS.md`, the new `AGENTS.md` is created as a copy of yours, so the two stay
identical, and the template rules are shown as a diff for you to merge.

It also keeps the two rule files out of search: it adds `CLAUDE.md` and
`AGENTS.md` to `exclude_prefixes` and `retrieval_exclude_prefixes` in
`.context/routes.json` (shown as a diff in the dry run; `--apply` writes it
after a `routes.json.bak-<UTC stamp>` backup), and drops a route whose only
canonical sources were those files. Claude Code loads `CLAUDE.md` and Codex
loads `AGENTS.md` at the start of every session, so search would only repeat
them, and identical twins would fill two result slots. `LOG.md` and
`BACKLOG.md` stay searchable. Without a `.context/routes.json` it prints a note
instead: run `context-layer init` first, then `rules init --apply` again.

### 5. Connect Claude Code

```sh
context-layer install claude-code --vault ~/Brain            # diff only
context-layer install claude-code --vault ~/Brain --apply    # write .mcp.json
```

This gives the agent the ten MCP tools listed in
[host-integration.md](host-integration.md#tools) (search, read a source, index
status, shared memory, link neighbours, shared packets, claim checks and the
optional advisor's status). Add `--hook` to also inject
evidence on every prompt. [host-integration.md](host-integration.md) covers
Codex (config written, host not verified here), other MCP clients, and
`uninstall`.

### 6. Turn on the record hooks (optional, Claude Code)

Three hooks make the recording rule visible instead of only written down:
SessionStart takes a snapshot, PostToolUse notes which vault files the agent
itself wrote, and Stop asks for a record of those. See them first:

```sh
context-layer rules settings --vault ~/Brain                  # prints the entries
context-layer rules settings --vault ~/Brain --plan-default   # ... plus plan mode by default
context-layer rules settings --vault ~/Brain --brief --check-citations   # ... plus the opt-ins
```

The command writes nothing. `context-layer install claude-code --vault ~/Brain
--rules` writes the same entries into `<project>/.claude/settings.json` (dry run
until `--apply`, with a backup and an `uninstall`); or merge the printed JSON by
hand. The project is the folder you start Claude Code in; usually the vault.
What the hooks do is described under [The hooks](#the-hooks); `--brief` and
`--check-citations` under [The session brief](#the-session-brief) and
[The citation check](#the-citation-check).

With `--plan-default`, the snippet also sets `"permissions": {"defaultMode": "plan"}`,
so terminal sessions started in that project begin in plan mode. Per the Claude
Code documentation, the VS Code extension does not read the starting mode from
project settings.

### 7. Optional extras

- **Synaptic search.** `context-layer search ~/Brain --prompt "..." --method synaptic`
  starts from the full-text matches and follows the vault's own explicit links
  (wikilinks, embeds, Markdown links, frontmatter relations) to pull in connected
  notes within a token budget. It is opt-in; the default search stays plain
  full-text. See [synapse.md](synapse.md).
- **Brain View in Obsidian.** The plugin in `obsidian-plugin/` (Context Layer
  Brain View) draws the vault's link graph in 3D and lights up the notes and
  links the last synaptic search used. The bundle is prebuilt (no build or
  Node needed; `dist/main.js` is committed): copy
  `obsidian-plugin/dist/main.js` (as `main.js`), `obsidian-plugin/manifest.json`
  and `obsidian-plugin/styles.css` into
  `<vault>/.obsidian/plugins/context-layer-brain/`, and enable it under
  Settings > Community plugins (details in `obsidian-plugin/README.md`).
  Nothing enables it for you.

## Create your brain

```sh
context-layer brain init <path> [--layout standard|minimal] [--avenox-compat]
                                [--into-existing] [--examples | --no-examples]
                                [--apply] [--json]
```

It creates:

- a folder layout, with one short `Index.md` hub per folder explaining what goes
  there and linking back to the Dashboard;
- a `Dashboard.md` home note linking every hub and the working files;
- note templates (Daily, Project, Decision, Person, Meeting) with the same
  frontmatter keys: `aliases`, `related`, `supersedes`, `status`, `tags`;
- nine fictional example notes, all named `Example - ...` and safe to delete,
  showing a project linked to a decision, a meeting, a person and a knowledge
  note, and a schedule note that replaced an older one kept in the archive
  (`--no-examples` leaves them out);
- the rule files `CLAUDE.md` = `AGENTS.md`, `LOG.md`, `BACKLOG.md`;
- `.context/routes.json`, from the same scan `context-layer init` runs, with
  the templates folder, the private records folder, the archive and the two
  rule files `CLAUDE.md` and `AGENTS.md` added to the search exclusions. A
  route whose canonical sources all fall under an exclusion is dropped, since
  those sources can never be delivered;
- `.obsidian/app.json` with three link settings (update links on rename, shortest
  link format, wikilinks). No plugin is installed or enabled.

The shipped notes link only to notes that stay searchable. Folders kept out of
search (records, archive, templates) are named in the Dashboard rather than
linked, the rule files are named in code spans, and the new schedule names the
archived one instead of linking it: a link into an excluded folder can never
be followed, and `context-layer index` would count it as unresolved. A fresh
brain's link graph has no unresolved link in any layout (a test checks this).

Layouts:

| Layout | Folders |
| --- | --- |
| `standard` (default) | `000-Inbox/` (with `Dump/`), `100-Command-Center/`, `200-Goals/`, `300-Projects/`, `400-Records/`, `500-Knowledge/`, `600-Arsenal/`, `700-Body/`, `800-Mind/`, `850-Companion/`, `900-Archive/`, `Templates/`, `daily/`, `knowledge/` |
| `minimal` | `Inbox/`, `Projects/`, `Knowledge/`, `Archive/`, `Daily/`, `Templates/`, with `Dashboard.md` at the root |
| `standard --avenox-compat` | The same folders under Avenox Beyin's original emoji-prefixed names (see [Use with Avenox Beyin](#use-with-avenox-beyin)) |

`400-Records` holds private reference records and is excluded from search. It is
called Records rather than Vault so it is not confused with the Obsidian vault
itself. `700-Body` and `800-Mind` are meant for personal notes, but only
`400-Records`, `900-Archive`, `Templates` and the rule files are excluded from
search on a fresh brain: `700-Body` and `800-Mind` are searchable until you add
them to `exclude_prefixes` (their `Index.md` notes say so). `850-Companion` holds the agent's working memory about you: `Core.md`
(what you told it about yourself), `Rules.md` (your dated corrections),
`Last-Session.md` (a handover note), `Threads.md` and `Journal.md`. The lower-case
`knowledge/` folder is for notes a tool writes, kept apart from `500-Knowledge/`,
which is for notes you write.

Safety: it is a dry run until `--apply`. A folder that is not empty is refused;
with `--into-existing` only missing files are added and no existing file is ever
overwritten, and an existing `.context/routes.json` is kept (a note says so when
it does not yet exclude the rule files; `rules init --apply` adds them). With
`--into-existing` the example notes are not written unless `--examples` asks
for them, so examples you deleted do not come back. A second
`brain init --apply` on the same folder is refused as not empty; with
`--into-existing` it changes nothing.

## The rule files

`CLAUDE.md` is what Claude Code reads; `AGENTS.md` is what Codex and other agents
read. They hold **one** rule text. By default they are byte-identical twins.
(Per the Claude Code memory documentation, Claude Code reads `AGENTS.md` only
when no `CLAUDE.md` exists, so with both files present a rule that is only in
`AGENTS.md` never reaches Claude, and the reverse for Codex. Under the
`claude-md-and-agents-md` setting it loads both, and it skips an `AGENTS.md`
only when `CLAUDE.md` imports or symlinks to it, so identical twins are then
loaded twice.)

### One source instead of twins (option)

```sh
context-layer rules init ~/Notes --single-source --apply
```

keeps the rules and the work records in `AGENTS.md` and writes `CLAUDE.md` as
the import `@AGENTS.md` on its first line (plus an HTML comment, which Claude
Code strips before loading). Claude Code expands the import, as its memory
documentation describes for sharing one file with other tools; Codex and other
agents read `AGENTS.md` directly. Parity can then not drift, and nothing is
loaded twice. `rules check` accepts this form (it reports "CLAUDE.md imports
AGENTS.md"); `rules record` writes only to `AGENTS.md` and `LOG.md`; the hooks
work the same way. Caveats: a tool that reads `CLAUDE.md` literally without
expanding imports sees only the import line; the Claude Code documentation
suggests running `/context` in the next session to confirm that `CLAUDE.md`
appears under Memory files. An existing `CLAUDE.md` with its own rules is kept
unless you add `--force`, which, as always, replaces every existing template
file after a backup: `CLAUDE.md` becomes the import and `AGENTS.md` the
template rules, so merge any rule of yours into `AGENTS.md` afterwards from
the backups. Twins remain the default.

The template ([templates/vault/CLAUDE.md](../templates/vault/CLAUDE.md)) has nine
sections: purpose, evidence and labels, plan first, stop and ask, recording,
sub-agents, privacy, the context-layer tools, and a record format. Sections
marked `CUSTOMIZE` in an HTML comment are yours to rewrite. Claude Code strips
block-level HTML comments before loading the file, so those notes cost no
context. Anthropic's guidance is to keep a `CLAUDE.md` under about 200 lines; the
template is 166. Its examples are placeholders in angle brackets
(`<what was done, one line>`), so no realistic example text can come back as an
answer.

### The recording rule

Every meaningful work step is recorded in the same run it happens:

- a short dated entry in the "Work records" section of **both** rule files
  (what was done and why, files changed, verification result, next step, link
  to the detail);
- a longer entry at the end of `LOG.md`, which is append-only;
- open or blocked work in `BACKLOG.md`.

One command does the first two and keeps the rule text one:

```sh
context-layer rules record ~/Brain \
  --summary "Chose the battery timer for the drip line" \
  --why "the schedule needs a second watering time on hot days" \
  --files "300-Projects/Example - Decision - Timer Controller.md" \
  --verified "re-read the decision and the schedule note" \
  --next "buy 30 m of drip line"
```

It takes a lock, re-reads both rule files, refuses to write if parity is
already broken, appends the entry to the rule text (both twins, or only
`AGENTS.md` when `CLAUDE.md` imports it) and to `LOG.md`, and checks parity
again. Each changed file is listed with the first 12 characters of its SHA-256.
New lines use the file's own line ending (CRLF files get CRLF lines). The text
cannot open, close or forge the records markers: `<!--` and `-->` in any field
are written as `< !--` and `-- >`, and the command says so. A rule file that is
not UTF-8 is refused with a one-line message. The rule files keep the ten most
recent entries (`--keep N`, `0` keeps all) so they stay short enough to load
every session; `LOG.md` keeps every entry, and each short entry links to its
long one (`[[LOG#^r-...]]`). Use `--json` for machine output. Run
`context-layer index <vault>` afterwards: `LOG.md` changed and is indexed (see
[the daily loop](#the-daily-loop), step 5).

The lock is `fcntl` on POSIX. Where `fcntl` is missing (Windows), an
exclusive-create marker `.context/rules.excl` holding the process id and time
is used instead; a marker older than two minutes is taken as left behind by a
process that died and is removed, and a timeout message names the file.

### The hooks

Three Claude Code hooks work together. Their state is plain files under
`.context/`; a vault without rule files gets no state at all.

`rules hook session-start` runs when a session starts. It records, per session
id in `.context/session-rules.json`, the SHA-256 of `CLAUDE.md`, `AGENTS.md`
and `LOG.md` and the state of the records section, and writes a snapshot of the
vault to `.context/session-rules/<session>.json`: every file in scope with its
size, modification time and SHA-256. Hidden folders (`.obsidian`, `.context`,
...), folders and files excluded in `.context/routes.json`, symlinks and the
four rule/record files are not in it; files over 4 MiB, and files beyond 256 MiB
hashed in one run, keep size and modification time only. A resumed, compacted
or forked session keeps its snapshot. Hashes of unchanged files are taken over
from the newest earlier snapshot rather than read again. State is kept for the
20 most recently used sessions and snapshots for the 8 most recent of them (a
snapshot takes about 100 bytes plus the path per file); a session that lost its snapshot
starts a new one at its next Stop and asks nothing until then. The hook adds
context only when parity is already broken (and the brief, with `--brief`).

`rules hook post-tool-use` runs after each successful Write, Edit, MultiEdit or
NotebookEdit tool call (the entry's matcher) and remembers the vault-relative
path the tool wrote, from the call's `tool_input.file_path` (or
`notebook_path`). Paths outside the vault, hidden or excluded paths and the
rule/record files are not stored. It prints nothing.

`rules hook stop` runs each time Claude finishes a response. It compares the
vault with the snapshot. "Changed" means the content changed: a file whose size
and modification time are unchanged is not read again; a different size is a
change; otherwise the file is hashed and compared, so rewriting identical bytes
is not a change. A modification time in the future is never trusted: such a
file is always hashed. (A file too large to hash is compared by size and
modification time.) It then:

- **asks** for a record, with Claude Code's block decision
  (`{"decision": "block", "reason": "..."}`, which makes Claude continue with
  the reason as its next instruction), only about changed paths the agent wrote
  with its file tools, and only when no new work record was appended. Each path
  is asked about at most once per session;
- **reports** other changed paths (your own edits in Obsidian, a sync, a shell
  command the agent ran) once, in a `systemMessage`, which Claude Code shows to
  you, without asking for anything. After a new record they are not listed;
- asks once per set of problems when parity is broken.

It never blocks in plan mode (`permission_mode` is `"plan"` in the hook input;
a broken parity is then reported in a `systemMessage`), and never while Claude
Code is already continuing because of a stop hook (`stop_hook_active` is true).
After a record the snapshot moves forward. Paths excluded in
`.context/routes.json` are never walked, stored or named; if that file cannot
be read, change tracking pauses and one message says so.

Every failure (unreadable hook input, a rule file that is not UTF-8, an unknown
hook event or flag, a missing `--vault`) prints one line on stderr and exits 1,
which Claude Code treats as a non-blocking error; the hooks never exit 2, which
would block.

Limits: edits the agent makes through a shell command are not attributed to it
(Claude Code runs no Write/Edit hook for them), so they are reported, not asked
about. A rewrite that keeps both size and modification time is not seen. The
hook cannot judge whether a change was meaningful; an agent can satisfy it with
a poor record. It makes a skipped record visible; it does not make the record
good.

## The session brief

```sh
context-layer brief ~/Brain [--json] [--max-chars N]
```

prints where the vault stands, for the start of a session, built from bytes
only. Each line names a file and the first 8 hex characters of its SHA-256
(`absent` when the file does not exist): the file the line was read from, or,
for a stale memory source, that source as it is now. Then it quotes the text:

```text
- log `LOG.md` 5e6f7a8b: 2026-01-15 - Chose the battery timer [r-20260115t143000z-a1b2c3]
```

In this order: the index status (`context-layer status`: overall, and how many
sources changed, were added, deleted or moved); sources of memory records in
force whose bytes changed since they were recorded; the last three `LOG.md`
entries; open work (`BACKLOG.md` table rows in state open, blocked or in
progress, open memory tasks, and sub-agent tasks that are queued, running,
blocked or waiting for review); the newest memory records in force; the time of
the last synaptic retrieval (`.context/activation.json`). Text is copied onto
one line and cut at 160 characters with a visible `[+N chars]`; nothing is
summarised and no model is called. Paths excluded in `.context/routes.json` are
never quoted or named (an excluded `LOG.md` or `BACKLOG.md` is skipped; if that
file cannot be read, nothing from the notes is quoted). The same bytes give the
same brief; it writes nothing. `--max-chars N` drops whole lines from the end and adds one
`(N line(s) omitted ...)` line.

`rules hook session-start --brief` (see `rules settings --brief`) adds the
brief to Claude Code's session context under a 3,000-character cap. Building it
runs `status`, which reads and hashes every file in scope, so it costs time on
a large vault. If a section cannot be read, the others are still added and a
`systemMessage` names the section.

## The citation check

`rules hook stop --check-citations` (see `rules settings --check-citations`)
compares the answer Claude just gave with what context-layer delivered in the
session, and adds one non-blocking line when the answer cites something that
was never delivered:

```text
context-layer: cited in the last answer but not delivered by context-layer in this session: `notes/beans.md`, hash 0badc0ff. Check them at the source before relying on them.
```

A citation is a vault path the answer names as a whole path, or a hex run of 8
to 64 characters that the answer presents as a hash: after `sha256`, `sha`,
`hash` or `digest`, right after a cited path (`notes/a.md (3f2a9c1b)`), or 64
characters long. Other hex runs, such as a commit id, are not citations. Files
the agent wrote in the session and the rule and record files do not count, and
excluded paths are never named. The answer is the Stop input's
`last_assistant_message`; without it, the last assistant message of the
transcript at `transcript_path` is read as a fallback (Claude Code documents
that file format as internal, so that reader is best-effort).

What was delivered comes from the session ledger,
`.context/session-evidence/<session>.jsonl`: one line per delivered item,
`{"path", "sha256", "packet_id", "at"}` plus the schema, session and channel
(and the line span of a passage), with ids, paths and hashes only. The ledger is
opt-in: nothing is written unless `install --session-evidence` (or the hook's
`--session-evidence` flag and `CONTEXT_LAYER_SESSION_EVIDENCE=1` for the MCP
server) is set and the `.context/session-evidence/` folder exists; without it
the check reports every vault path the answer cites as undelivered, with the
note that no delivery was recorded. The prompt hook and the MCP server append
to it (through `mcp_server.record_delivery`); the MCP server finds the session
through `CLAUDE_CODE_SESSION_ID`, which Claude Code sets for its subprocesses.
Appending stops at 4 MiB per session file (one overflow line marks it) and only
the 50 most recently written session files are kept (see
[host-integration.md](host-integration.md#session-evidence-ledger)). When a file
is longer than its cap or has an overflow marker, the record counts as
incomplete and the check stays silent, since a dropped line cannot be told from
a missing one.

The line never blocks and never judges whether the answer is right. It flags
what to check. Files the agent opened with its own Read tool were not delivered
by context-layer, so citing them is reported too.

## The daily loop

1. **Plan.** In Claude Code, start in plan mode: Shift+Tab cycles to it,
   prefixing a prompt with `/plan` enters it, `claude --permission-mode plan`
   starts in it; it stays on until you approve a plan or press Shift+Tab again.
   Other agents write the plan as their first message. The plan names the goal,
   the sources to read, the steps, the risks and what will be verified.
2. **Retrieve evidence.** `search_vault` / `context-layer search` returns
   verbatim passages with their path and SHA-256. `NOT_FOUND` means this search
   found nothing; it is not evidence that the answer is "no".
3. **Act.** Edit notes, draft, research. Stop and ask before anything
   destructive, outward-facing or ambiguous (the checklist is in the rules).
4. **Verify.** Re-read what you changed; re-run the search; count what was
   claimed to be counted. A measurement beats an estimate.
5. **Record.** `context-layer rules record ...` in the same run, then
   `context-layer index <vault>`. Recording rewrites the rule files and
   `LOG.md`. `rules init` and `brain init` keep `CLAUDE.md` and `AGENTS.md` out
   of search (the agents load them at session start), while `LOG.md` stays
   searchable on purpose: the work records are part of what the brain
   remembers. Until the next `index`, a search that matches `LOG.md` lists it
   under `withheld` instead of quoting it (the other results are still
   delivered). To keep `LOG.md` out of search too, add it to `exclude_prefixes`
   in `.context/routes.json` and re-index.
6. **Check parity.** `context-layer rules check .` exits 0 when the two rule
   files hold one rule text. `rules record` already checks it; run it yourself
   after you edit the rules by hand.

## Write notes that retrieval can use

Retrieval can only find what the notes make findable.

- **Link explicitly.** Write `[[Other note]]` wherever one note depends on
  another. Synaptic search and Brain View follow only links that are actually
  written; they never guess a connection.
- **One subject per note.** A note about three things is found for all three
  and useful for none. Split it and link the parts.
- **Stable, specific names.** `Watering Schedule` beats `notes 3`. Rename rarely;
  Obsidian updates links on rename with the setting `brain init` writes.
- **Say what replaced what.** Put `supersedes: "[[Old note]]"` in the new note's
  frontmatter and `related:` for close neighbours. Move the superseded note to
  the archive.
- **Keep duplicates out of scope.** Archives, exports, backups and copies of the
  same note must be in `exclude_prefixes`; otherwise an old version can be
  quoted back as current evidence.
- **Write the date and the source** into notes that record decisions or facts.

## Working with sub-agents

- Give each sub-agent a bounded task: the question, the sources it may read,
  what it may write (default: nothing), and the output format.
  `context-layer tasks` runs such bounded tasks against the vault; see
  [tasks.md](tasks.md).
- Ask for sources, not conclusions: "file X, line Y says Z" can be checked in
  seconds.
- A sub-agent's report is a claim until you, or the coordinating agent, open at
  least one critical claim at its source. Check "all", "none" and "every" first.
- Never let an agent grade its own work.

The reasoning behind these rules is in
[discipline/AGENT_REPORTS.md](../discipline/AGENT_REPORTS.md),
[discipline/EVIDENCE_GATE.md](../discipline/EVIDENCE_GATE.md),
[discipline/PLAN_AND_ASK.md](../discipline/PLAN_AND_ASK.md) and
[discipline/RECORD_DISCIPLINE.md](../discipline/RECORD_DISCIPLINE.md);
[discipline/WORKED_EXAMPLE.md](../discipline/WORKED_EXAMPLE.md) walks one
fictional step from plan to record.

## What this will and will not do

It will:

- deliver verbatim passages with path and SHA-256, and withhold stale ones;
- keep one rule text in the two rule files and tell you when it splits;
- give agents one command to record their work, and ask once per path when
  they skip recording their own edits;
- keep everything in plain files under the vault that you can read, diff and
  delete.

It will not:

- **eliminate hallucination.** No rule file or tool can. The rules make an
  unsupported claim harder to pass off (every claim needs a source and a label)
  and easier to detect (the source path and hash can be checked). A model can
  still misread a correct passage, or ignore the rules.
- judge whether an answer is correct. Evidence found is not an answer verified.
- sandbox an agent. The hooks and rules are not an operating-system security
  boundary; an agent with shell access can still edit any file it can reach.
- replace backups. Use version control or your usual backup for the vault.

## Use with Avenox Beyin

[Avenox Beyin](https://github.com/avenoxai/avenoxbeyin) is a separate local
second-brain engine for Claude Code, Codex and other clients, and the source of
the starter layout. context-layer is designed to run alongside it on the same
vault: it adds verbatim evidence search, synaptic retrieval, the Brain View and
the rule files, and it does not replace or modify the Avenox engine. The two
together have not been tested end to end here.

Folder names matter if you plan to install Avenox Beyin:

- The default `standard` layout uses English names without emoji.
- `brain init --avenox-compat` creates Avenox Beyin's original emoji-prefixed
  folder names instead. Use it if you will install the Avenox engine, or rename
  the folders by hand later.
- Why: Avenox Beyin's V2 documentation states that the companion memory folder
  name is fixed because its hooks read that exact path. On its current main
  branch (checked 2026-09-24, commit `3868d11`), the V3 companion module also
  accepts another top-level folder whose name ends in "Companion", but the
  session hook scripts and the graph checker in its `template/` folder still
  name the emoji path.
  Neither combination has been tested with this layout, so treat the English
  layout as not read by Avenox's hooks unless you use `--avenox-compat`.
- Avenox Beyin's companion rules file is its own; `brain init` writes
  `850-Companion/Rules.md`, which the Avenox engine does not read as its rules
  file.
- The optional retrieval patches in `retrieval-patches/` are verified only
  against Avenox Beyin v3.0.1. Nothing here has been tested with later
  releases.

## Troubleshooting

**`rules check` fails with "differ (first difference at line N)".** One agent
edited only one rule file. Decide which version is right, copy it over the other,
and run `rules check` again. If the difference is a record, re-run
`rules record` after restoring parity; it refuses to write while the files
differ.

**`rules check` fails with "differ only in line endings (X uses CRLF, Y uses
LF)".** The two files hold the same text; one was saved with Windows line
endings. Re-save the named CRLF file with LF (or copy the other file over it)
and run `rules check` again.

**The Stop hook keeps asking.** It asks at most once per path in a session, and
only about files the agent wrote with its file tools. If it fires on every
response, the agent keeps writing new files without recording; record the work,
or tell the agent the step was not meaningful. `.context/session-rules.json`
shows, per session, the paths written (`attributed`), asked about (`asked`) and
reported (`reported`).

**The Stop hook never asks.** Check that the PostToolUse entry is installed
(`rules settings` prints it, with the matcher `Write|Edit|MultiEdit|NotebookEdit`):
without it no change is attributed to the agent, so every change is only
reported. Plan mode never blocks, and a session whose hooks were installed
mid-session starts its snapshot at the first Stop.

**The hook never fires.** Run `context-layer rules settings --vault <vault>` and
compare with `.claude/settings.json` in the folder you start Claude Code from.
Start a new session after editing settings. Test the handlers by hand:
`echo '{"session_id":"t","source":"startup"}' | context-layer rules hook session-start --vault <vault>`,
then the same with `rules hook stop`.

**`brain init` says the folder is not empty.** Choose a new folder, or add
`--into-existing` to add only the missing files.

**Search returns `NOT_FOUND` for a note you can see.** Re-run
`context-layer index`, check that the note's folder is not in `exclude_prefixes`,
and try the words the note actually uses.

**More:** [host-integration.md](host-integration.md#troubleshooting) and
[source-lifecycle.md](source-lifecycle.md).

## FAQ

**Do I need Obsidian?** No. Any folder of Markdown files works; Obsidian adds the
editor, the graph and the Brain View plugin.

**Why two identical files instead of one?** Claude Code reads `CLAUDE.md`, Codex
reads `AGENTS.md`. Identical copies work for both without depending on import
features, and `rules check` makes a drift visible. If your tools expand
imports, `rules init --single-source` keeps one file instead (see
[One source instead of twins](#one-source-instead-of-twins-option)).

**Will the work records bloat `CLAUDE.md`?** The rule files keep only the ten
most recent entries by default; the full history is in `LOG.md`.

**Can I change the rules?** Yes. Edit both files the same way (or edit one and
copy it), then run `rules check`. The sections marked `CUSTOMIZE` are meant for
it.

**Does anything leave my machine?** context-layer makes no network calls by default. The [advisor](jev.md) and
[GitHub context](github-context.md) are separate opt-ins. Your
AI host sends what it reads to its model provider, as it always does; exclude
private folders in `.context/routes.json` to keep them out of search.

**Can I delete the example notes?** Yes: every file named `Example - ...` is
fictional. Remove the "Examples" section from the Dashboard and the "Example:"
lines from the hub notes too; `brain init --no-examples` creates a vault
without any of them, and `--into-existing` does not bring them back unless you
add `--examples`.

## Credits

The starter brain's folder layout is adapted from
[Avenox Beyin](https://github.com/avenoxai/avenoxbeyin) by Avenox (MIT; site
<https://avenox.lol>, setup video <https://www.youtube.com/watch?v=sRCTOxc4658>):
names translated to English and emoji removed, with `--avenox-compat` for the
original names. No Avenox code or text is included in the starter brain. Details
are in [CREDITS.md](../CREDITS.md).
