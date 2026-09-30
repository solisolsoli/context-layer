# Shared memory

`context-layer memory` keeps one append-only file per vault so a decision, a
task or a result survives the end of a session and is readable by the next
tool. It records what was asserted and which sources it rested on, with the
SHA-256 those sources had at the time. It does not judge whether the text is
true, and it never rewrites a line.

## Where it lives

```
<vault>/.context/memory/
├── records.jsonl              the record: one JSON object per line, append-only
├── MEMORY.md                  derived view, regenerated after every write
├── .lock                      held during append + mirror regeneration
└── records.jsonl.torn-<utc>   only after `memory repair`: a torn last line, moved aside
<vault>/.context/sessions/<id>.jsonl   opt-in session record (see below)
<vault>/<folder>/                      opt-in visible notes (`memory mirror --notes`)
```

`records.jsonl` is the record of truth. It is plain JSON Lines: open it, diff
it in git, delete it, or delete a line with any editor. Nothing in this tool is
needed to read or remove it.

A line ends at a line feed; one trailing carriage return is tolerated, and
nothing else ends a line: a U+2028, U+2029, U+0085 or form feed inside a
record's text stays inside that record's line. New lines are written as ASCII
JSON (a non-ASCII character appears as a `\uXXXX` escape), so even a reader that
splits on other line breaks, as Python's `str.splitlines` does, sees one record
per line. Lines written by 0.3 with raw characters are still read.

`MEMORY.md` is a Markdown view of the whole store; anything typed into it is
lost on the next write. It sits in `.context/`, and Obsidian does not show dot
folders, so it is not an ordinary note there. For notes Obsidian shows, write
the opt-in visible mirror described below.

## The record

```json
{"id": "m-153ee1f8efbed8dc", "prev": "m-3fc5a6cdee12d44d",
 "ts": "2026-09-21T20:20:23.558708Z", "tool": "claude-code", "session": "s-1",
 "kind": "result", "state": "approved", "text": "Indexed 1,204 notes.",
 "sources": [{"path": "notes/search.md", "sha256": "744cbc47…"}],
 "supersedes": null, "closes": ["m-a14e86f4509693e1"], "format_version": 2}
```

| Field | Meaning |
| --- | --- |
| `id` | `m-` + the first 16 hex of SHA-256 over the canonical JSON of `kind`, `text`, `sources` (path + sha256), `supersedes` and, when it is not empty, `closes`. It addresses what the record asserts, not when or by whom. |
| `prev` | `id` of the previous line, or `null` on the first. A simple chain that makes a removed, reordered or inserted line visible. |
| `ts` | UTC ISO 8601, microsecond precision, `Z` suffix. |
| `tool` | Argument, else `$CONTEXT_LAYER_TOOL`, else `cli`. |
| `session` | Argument, else `$CONTEXT_LAYER_SESSION`, else `null`. |
| `kind` | `decision`, `task`, `result` or `note`. |
| `state` | `draft`, `approved` or `published`. |
| `text` | What is being recorded, stripped of surrounding whitespace. |
| `sources` | Vault-relative paths with the SHA-256 the file had when recorded, sorted by path. |
| `supersedes` | `null`, the `id` of the record this one replaces, or a list of two or more ids when it replaces several at once (a merge). |
| `closes` | Ids of the tasks a `result` completes; `[]` otherwise. |
| `format_version` | `2`. |

`record()` returns the stored object plus three derived flags that are never
written to the file: `duplicate`, `in_force` and `superseded_by`.

### Formats 1 and 2

Records written before 0.4 are format 1: no `format_version`, no `closes`, and
an id that hashes the sources in the order they were given. This version writes
format 2, where the sources are sorted by path before hashing, so the same
sources in another order are the same record. A format-2 record with no
`closes` and sources already in order gets the id format 1 gave the same
content.

Both formats are read, and a store may hold both: nothing is migrated and no
old line is rewritten. `verify` recomputes every line's id with that line's own
format. A line with a newer `format_version` than this version knows is
refused with "upgrade context-layer" rather than half understood.

### Same record twice

The id is a content address, so re-running the same call appends nothing and
returns the record already stored, with `duplicate: true`. A rerun after a
crash, or two tools recording the same decision from the same sources, costs
one line, not two. The match also finds a format-1 record that listed the same
sources in another order. Changing the text, the sources, `supersedes` or
`closes` makes a different record.

Re-recording content that a later record superseded is refused: the stored
record is no longer in force, and returning it as a duplicate would silently
adopt nothing. The error names the records in force and prints the exact
command that records the content again as their replacement
(`context-layer memory add <vault> … --supersedes <id in force>`).

A state is not part of the id. Recording an existing draft again with
`--state approved` returns the stored draft as a duplicate, and the CLI prints
the `--supersedes <id>` that approves it.

### States, corrections and forks

Nothing is edited in place. Promoting a draft to `approved`, or correcting the
wording, means appending a new record whose `supersedes` names the old id. Both
lines stay in the file; `resume` returns only the one in force, and `MEMORY.md`
marks the old entry `Superseded by`. `supersedes` must name a record the vault
already holds, so a typo is refused instead of orphaning the chain.

Only a record in force can be superseded. Superseding a record that another
record already replaced would give the chain two current versions (a fork), so
it is refused, with the id to supersede instead, unless `--allow-fork`
(`allow_fork=True`) asks for a competing version on purpose. A fork can also
appear when two copies of `records.jsonl` are merged by hand. Either way it is
reported: `verify` lists it as a `fork:` problem (exit 1), `resume` returns it
under `conflicts` as `{target, heads}`, and `MEMORY.md` shows a `Conflicts`
section and every successor under `Superseded by`. Resolve it with one record
that supersedes every head (`--supersedes A --supersedes B`); every record a
merge names must be in force, unless `--allow-fork` is given.

### Tasks and results

A task is open while it is in force and no result in force closes it. A result
closes a task by naming it in `closes` (`memory add --kind result --closes
TASK_ID`) or by superseding it. `closes` is refused on any other kind, for a
record that is not a task, and for a task that is no longer in force (the error
names the task that replaced it).

A result that is itself superseded, for example by a retraction, closes
nothing, and the task is open again. Text never closes a task: a format-2
result reading "Could not do m-…" leaves that task open. Format 1 had no
`closes` and treated a result that quotes a task id in its text as closing it;
a format-1 result in force still closes a task that way, and `resume` lists it
under `closed_by_mention` (and `MEMORY.md` marks it) so a reader can check that
the result really completes the task.

A task is a record like any other: superseding it (for example with `memory
rebind`) makes a new record, and a result that closed the old one names the old
id. Close the new record again if it is still done.

## Sources, drift, stale and moves

Source paths are vault-relative and are checked by `router/source_policy.py`
before any file is opened: absolute paths, `..`, paths that leave the vault,
symlinks and excluded prefixes from `.context/routes.json` are all refused, and
nothing is appended. The same exclusions the indexer honours apply here.

- Omit a hash and it is computed from the file at record time.
- Give a hash (`--source path@<64 hex>`) and it is compared with the file now.
  A mismatch is **source drift**: the record is refused with a visible error.
- `--allow-stale` (`allow_stale=True`) records the given hash anyway. The
  record then asserts a version that is not on disk, and `resume` and `verify`
  report it as stale from the start.

A source is **stale** when the file's current SHA-256 differs from the recorded
one, or when the file is gone, unreadable, or has since fallen outside the
vault boundary (deleted, symlinked, newly excluded). `resume` reports it as
`{id, path, recorded_sha256, current_sha256, moved_to}` with `current_sha256:
null` for the second group; it never guesses what changed. Stale means
"re-read the source before trusting this record", not "the record is wrong".
Only records in force are checked by `verify`: a superseded record no longer
asserts anything.

When a source is missing, its recorded SHA-256 is looked up in
`.context/index-manifest.json`, and every in-scope path listed there that still
holds exactly those bytes is reported in `moved_to`. The manifest describes the
last index build, so a rename is found after the next `context-layer index`.
`context-layer memory rebind VAULT ID` then appends a record that supersedes ID
with the same kind, state, text and hashes (and the `closes` of tasks still in
force), and the new path. A
missing source with exactly one `moved_to` copy is rebound on its own;
`--move OLD NEW` names the copy when there are several, and NEW must hold the
recorded bytes (a file with other bytes is a new source: record a new record).
No line is edited.

## Resuming in another session or tool

```python
from pathlib import Path
from context_layer import memory

packet = memory.resume(Path(vault), limit=20, kinds=["decision", "task"])
```

or `context-layer memory resume VAULT --json`. Either returns:

| Key | Contents |
| --- | --- |
| `generated_at` | When the packet was built (UTC). |
| `vault` | The vault's folder name only. No absolute path is ever printed. |
| `records` | Records in force, newest first, superseded ones removed, filtered by `kinds`, capped at `limit`. |
| `stale` | Stale sources of the records in this packet, each with `moved_to`. |
| `open_tasks` | `task` records in force that no `result` in force closes, newest first, capped at `limit`. |
| `conflicts` | Forks: `{target, heads}` for every record replaced by two or more records still in force. |
| `closed_by_mention` | `{task, result}` for tasks closed only because a format-1 result quotes their id. |

A tool continues work by reading `records` for what was decided, `open_tasks`
for what is unfinished, `stale` for which evidence must be re-read first, and
`conflicts` for which decisions disagree.

## Concurrency

Append and mirror regeneration happen under an exclusive `fcntl.flock` on
`.context/memory/.lock`, so parallel writers cannot lose a line or break the
chain; the regression test runs 8 processes × 25 records and expects 200 valid
lines with an intact chain. Where `fcntl` is unavailable the fallback is an
`O_EXCL` lock file with retry and a 30-second timeout; its test runs 6
processes × 20 records with `fcntl` removed in every process.

Limits, plainly: this covers processes on **one machine using one local
filesystem**. POSIX advisory locks are not reliable over NFS, SMB or a folder
synced by a cloud client, and two machines writing the same synced vault can
still produce a conflicted copy. `flock` is released by the kernel if a writer
dies; the `O_EXCL` fallback is not, so a crash there leaves `.lock` behind and
the next writer times out with a message naming the file to delete.

## Crash recovery

Each append is flushed and `fsync`ed, and the folder is synced when
`records.jsonl` is created. `MEMORY.md` is replaced atomically: a staging file
in the same folder, `fsync`, rename, and a sync of the folder.

A crash in the middle of an append can still leave a torn last line: bytes with
no final newline that are not a JSON object. Like any unusable line it makes
every other memory command refuse, `verify` reports it, and the refusal names
the fix:

```sh
context-layer memory repair VAULT --dry-run   # what would move
context-layer memory repair VAULT             # move it aside
```

`repair` moves only that last fragment, byte for byte, to
`.context/memory/records.jsonl.torn-<utc>`, cuts it from the store and rebuilds
`MEMORY.md`. Every other unusable line is reported and left as it is (exit 1):
those are fixed by hand, and `verify` shows the consequences. A complete last
record that only lost its newline is not torn; the next append adds the
newline first. `context-layer memory mirror VAULT` rebuilds `MEMORY.md` on its
own, for example after a hand edit of `records.jsonl`.

## Visible notes for Obsidian (opt-in)

```sh
context-layer memory mirror VAULT --notes Memory             # write or refresh
context-layer memory mirror VAULT --notes Memory --dry-run   # show the plan
context-layer memory mirror VAULT --notes Memory --remove    # take it away again
```

This writes one note per decision and task: records in force as
`Memory/<id>.md`, superseded ones as `Memory/superseded/<id>.md`. Results and
notes are not mirrored. Each note carries frontmatter

| Key | Meaning |
| --- | --- |
| `memory_id`, `kind`, `state`, `recorded` | The record's id, kind, state and `ts`. |
| `in_force` | Whether no record supersedes it. |
| `supersedes` | `"[[m-…]]"`, or a list of links for a merge. |
| `superseded_by` | A list of links to the records that replace it. |
| `status` | Tasks only: `open`, or `closed` by a result in force (or by mention in a format-1 result). |
| `stale` | `true` when a source is missing or no longer holds the recorded bytes, computed when the note was written. |
| `derived`, `generated` | Always `true`; when the mirror wrote it. |

and a body with the text as a quote, each source as a wikilink with its
recorded hash prefix and its state (`current`, `changed since it was
recorded`, `missing`), and the supersession links. Obsidian's graph and
backlinks show the chain; context-layer's own link graph reads only indexed
notes, so it sees them only if the folder is made searchable.

The notes are a snapshot: run the command again to refresh them, and a note
whose record was superseded since moves to `superseded/`.

They are never evidence by default. The first run adds `Memory/` to
`exclude_prefixes` in `.context/routes.json` and prints a notice, so text an
agent wrote into memory is never served by search, the hook or MCP as if it
were a source. If you delete that entry, later runs do not add it back, they
say the notes can now be served as evidence, and every note's footer says the
same.

Safety rules: a dot or tool folder is refused (Obsidian would not show it), so
is a folder that holds files the mirror did not write, and a vault without
`routes.json` (run `context-layer init` first). What the mirror wrote is listed
with hashes in `<folder>/.memory-mirror.json`; a note edited since it was
written is never overwritten or deleted, only reported as kept. `--remove`
deletes the unedited notes, the marker and the folders it leaves empty, and
removes the exclusion it added; while edited notes remain, the marker keeps
listing them and the exclusion stays. Changing `routes.json` is visible to other
components: a job written before the change is BLOCKED by `job validate`
([subagents.md](subagents.md)).

## Session record (opt-in)

With `CONTEXT_LAYER_SESSION` set in the environment, memory appends what each
call used to `.context/sessions/<id>.jsonl`, a bill of materials for the
session: which records it wrote, matched as duplicates or read, and which
sources it hashed. `--session` only sets a record's `session` field; the file is
written only while the environment variable is set.

Each line is one event, schema `session-bom/v1`, and holds ids, paths, hashes
and times, never text:

| Key | Meaning |
| --- | --- |
| `schema` | `session-bom/v1` |
| `ts` | When the call ran (UTC); every event of one call has the same time. |
| `session` | The session id. |
| `op` | The memory call: `record`, `resume` or `list`. |
| `event` | `memory.write` (appended), `memory.duplicate` (an existing record matched), `memory.read` (returned by `resume` or `memory list`), `source.check` (a source hashed at record time or re-checked by `resume`). |
| `id` | The record id, for the `memory.*` events. |
| `path`, `sha256`, `current_sha256` | For `source.check`: the source, the hash the record holds, and the hash of the file then (`null` when missing or unreadable). |

Identical events of one call are written once, so a task that `resume` returns
both as a record and as an open task is one `memory.read`. An id that is not a
plain file name (`[A-Za-z0-9][A-Za-z0-9._-]{0,127}`) is stored as
`sha256-<32 hex>.jsonl`. When the file cannot be written, a warning goes to
stderr and the memory call itself still succeeds. The schema is meant to be
shared: other components can append their own events with the same keys (none
does yet).

```sh
context-layer memory session list VAULT
context-layer memory session show VAULT ID          # records and sources, with stale flags
context-layer memory session show VAULT ID --json
```

`show` lists each record with the events that touched it and whether it is
still in force, and each source with whether it was stale when it was checked
and whether the file holds the recorded bytes now (`STALE now: changed` or
`missing`).

To see more than memory did in a session, `context-layer session show VAULT ID`
joins this record with the memory records written under the id, the evidence
delivered to the host, the tasks and their ledger status, and the advisor's
counters, read-only and with a file and line for every row
([cli.md](cli.md#session-show-one-sessions-joined-report)).

## CLI

```sh
# Record a decision with the source it rests on.
context-layer memory add VAULT --kind decision --state approved \
  --text "Search defaults to FTS." --source notes/search.md

# Pin an exact version; a mismatch is refused.
context-layer memory add VAULT --kind result --text "Indexed 1,204 notes." \
  --source notes/search.md@744cbc47ce31a443f1218e73192f05e0450ecd96b135b1fe7cf0a1acdfd98bc7

# Approve an earlier draft (a new record, not an edit).
context-layer memory add VAULT --kind decision --state approved \
  --text "Search defaults to FTS." --supersedes m-a14e86f4509693e1

# Close a task; merge a fork.
context-layer memory add VAULT --kind result --text "Guide written." --closes m-5c0d1e2f3a4b5c6d
context-layer memory add VAULT --kind decision --text "Cap at 15k." \
  --supersedes m-0f1e2d3c4b5a6978 --supersedes m-8a7b6c5d4e3f2a1b

# Read it back.
context-layer memory list VAULT --kind decision --limit 10
context-layer memory resume VAULT            # compact continuation packet
context-layer memory resume VAULT --json     # the same packet as JSON
context-layer memory verify VAULT            # exit 1 on any problem

# Maintenance.
context-layer memory rebind VAULT m-153ee1f8efbed8dc [--move OLD NEW]
context-layer memory repair VAULT [--dry-run]
context-layer memory mirror VAULT [--notes FOLDER [--remove]] [--dry-run]
```

`--tool` and `--session` default to `$CONTEXT_LAYER_TOOL` and
`$CONTEXT_LAYER_SESSION`, so a host can set them once per session. Every
refusal prints to stderr and exits non-zero; no failure is reported as success.

`verify` reports unusable lines (invalid JSON, invalid UTF-8, a
`format_version` that is not a positive integer), malformed or duplicate ids, unknown kinds or states, a line whose
content no longer matches its id, a broken `prev` chain, a `supersedes` or
`closes` naming a record the file does not hold, `closes` on a record that is
not a result or naming a record that is not a task, forks, and stale sources of
records in force (with the moved path when there is one). Any of these exits 1,
and so does a record in a newer format, which stops `verify` with an upgrade
message.
Hand-editing is allowed by design, and `verify` is how the consequences become
visible: deleting a line leaves a chain gap, and rewriting a line's text makes
its id stop matching.

## What this does not guarantee

- **Not truth.** A record is what a tool asserted, with hashes of the sources
  it named. Matching hashes prove the bytes are unchanged, not that the text is
  correct or that those sources support it.
- **Not tamper-proof.** Anyone who can write the vault can rewrite the whole
  file, chain included. The chain detects accident and drift, not an adversary;
  it is not signed and there is no external anchor. The same holds for the
  session record.
- **Not a sync or merge tool.** No remote, no conflict resolution. Two diverged
  copies of `records.jsonl` are merged by a person, and `verify` afterwards
  names any fork the merge created.
- **No retention or privacy handling.** Text and paths are stored verbatim, in
  the clear, until someone deletes them. Do not record a secret. How to remove
  records, and what else the tool keeps on disk, is in [privacy.md](privacy.md).
- **An agent can write memory.** The MCP `memory_record` tool appends records,
  always as drafts; a note that steers an agent can steer what it records. See the threat model in [privacy.md](privacy.md). The visible
  notes mirror is kept out of retrieval by default for this reason.
- **No ordering across clocks.** `ts` comes from the writing machine's clock;
  the chain records append order, which is the only order this tool asserts.
- **Reads are whole-file.** Every write and every `resume` parses the entire
  file. That is deliberate for a file a person must be able to read, and it is
  not sized for hundreds of thousands of records.

The optional advisor can review a proposal before it is recorded:
`context-layer jev review-memory VAULT --proposal FILE` (advisory only; it never
writes the store). See [jev.md](jev.md).
