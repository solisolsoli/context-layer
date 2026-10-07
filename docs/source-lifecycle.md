# Source lifecycle: status, rollback and the lab candidates

An index is a photograph of the vault. The moment a source changes, the
photograph is evidence of something that no longer exists. This component makes
that gap visible (`status`), reversible (`rollback`), and refuses to paper over
it (search withholds changed notes; the router withholds its whole packet).

Nothing in this document installs, registers or promotes anything to a live
host. The hook example at the end is a lab candidate for review, not a feature
of this package.

## `context-layer status <vault>`

```sh
context-layer status /path/to/vault          # human report
context-layer status /path/to/vault --json   # same facts, machine-readable
```

`status_summary(vault) -> dict` is the same report as an importable function,
with no subprocess, so another component (for example an MCP tool) can call it
directly. It raises `ValueError` if the vault is not a directory.

### What it reports

| Field | Meaning |
| --- | --- |
| `index.present` / `index.readable` | The file exists, and it opens as the SQLite index retrieval actually reads: a format this version reads, a full-text table whose row count matches the stored records, and a clean FTS5 `integrity-check` (run on an in-memory copy). |
| `index.built_at` / `index.age_seconds` | When the build ran, and how long ago. Age alone never changes `overall`. |
| `index.source_count` | Sources the index covers. |
| `index.manifest` | Read from `index-manifest.json`, or `false` when it fell back to the index rows. |
| `index.older_than_sources` | A source was modified after the build timestamp. |
| `index.rollback_available` | A `.prev` index exists to restore. |
| `changed` | Indexed sources whose current bytes hash differently. |
| `deleted` | Indexed sources no longer in scope. |
| `added` | In-scope sources the index does not cover. |
| `moved` | Same bytes at a new path, reported as one move instead of a delete plus an add. |
| `now_skipped` | In-scope files the builder cannot index, each `{path, reason, indexed}`. `reason` is `empty`, `over size limit` (routes.json `max_file_bytes` when it is a whole number from 1 to 50,000,000, else the builder's 2,000,000 bytes), `unreadable`, `not UTF-8`, `unreadable folder` (a folder that cannot be listed, named with a trailing `/`), or the reason the builder wrote for that path in the manifest's `skipped` list (for example `unsupported name`). `indexed` is `true` when the index still holds earlier bytes of that path. |
| `excluded_count` | In-scope-by-extension files that are never read: configured exclusions, dot paths, and names the router refuses as a source path (unless the builder's manifest lists them as skipped). |
| `symlinks` | Symlinked paths found in scope. Unsupported as sources, never followed, never opened. |
| `graph.present` / `graph.readable` | `.context/graph.sqlite` exists, and opens as a link graph in a format this version reads. |
| `graph.built_at` / `graph.notes` / `graph.edges` | From the graph itself. |
| `graph.matches_index` | The graph belongs to this index's generation: its notes and their hashes equal the index's Markdown sources. `null` without a usable index. |
| `graph.rollback_available` | A `graph.sqlite.prev` exists. |
| `overall` + `reasons` | One of `ok`, `stale`, `degraded`, `missing`, `error`, with one line per reason. |

`overall` maps to the process exit code: **0** `ok`, **1** `stale` or `degraded`,
**2** `missing` or `error` (all commands: [cli.md](cli.md)).

- `error` — `.context/routes.json` cannot be trusted (invalid JSON, a repeated
  key, exclusions that are not a list of relative paths, a newer
  `schema_version`). No source is read: scanning without the exclusions would
  hash exactly the files they were meant to keep out. (0.2 reported this as
  `degraded` and scanned with no exclusions.)
- `missing` — no index, a file that does not open as one, a newer index
  format, or a full-text table that no longer agrees with the stored records
  (for example after `DELETE FROM records_fts`). There is nothing trustworthy
  to compare against; build before asking for evidence.
- `degraded` — `changed`, `deleted` or `moved` entries, or a `now_skipped`
  entry with `indexed: true` (an indexed file that became empty, too large,
  unreadable or not UTF-8). Retrieval re-checks each hit against its file, so
  the index's earlier bytes of those sources are not served; a rebuild drops or
  replaces them.
- `stale` — the index is internally consistent but does not cover everything:
  `added` sources, `symlinks`, a `now_skipped` file the index never held (other
  than an empty one), or a link graph that cannot be read or was built from
  another index generation. A rebuild fixes `added` and the graph. It does
  **not** fix `symlinks` or skipped files, and the reason lines say so.
- `ok` — every in-scope source is covered and every hash matches. An empty file
  has nothing to index: it is listed under `now_skipped` with a reason line,
  and it does not change `ok`.

Every path in the output is vault-relative; the vault's own location is not
printed. Relative note names may still reveal private information. Keep real
vault reports local, and use synthetic examples for any shared report under
the [development guide](../CONTRIBUTING.md).

### Why it hashes everything, every time

`status` reads and hashes every in-scope file on every run. It does not use size
or mtime as a shortcut, because both can be preserved across a rewrite — the
trap recorded in the lab evidence below, and the one the `changed`-detection
test reproduces with `os.utime` and an equal-length replacement. The cost is one
pass over the in-scope bytes; the alternative is a report that certifies content
that is no longer there.

Files the builder never reads are not reported as `added` either: configured
`exclude_prefixes` and `retrieval_exclude_prefixes` from `.context/routes.json`,
dot paths and tool directories. Tool and dot directories are pruned without
being counted; directories excluded by configuration are walked so their files
can be counted — their **contents are never read**. A path under an excluded
prefix is never named, whatever the manifest says.

Files the builder reads but cannot index are named under `now_skipped`,
never counted as excluded and never called deleted: empty files, files over the
size limit, files that cannot be read, files that are not valid UTF-8, and the
paths the builder recorded in the manifest's `skipped` list. `status` checks the
first four itself, so it names them even for an index built before the builder
recorded skips. A file over the limit that the index holds with the same bytes
counts as covered, because the build that indexed it allowed that size. An
indexed note inside a folder that cannot be listed is `unreadable`, not
`deleted`.

One limit worth stating: the extension set a build used is not recorded. `status`
assumes the builder's default set plus any extension present in the index, so a
build made with a narrower `--extensions` list will report the omitted
extensions as `added`.

## Rebuild and rollback

```sh
context-layer index /path/to/vault           # rebuild; keeps the replaced index
context-layer status /path/to/vault          # confirm it is ok again
context-layer rollback /path/to/vault --dry-run
context-layer rollback /path/to/vault        # restore the previous index and link graph
context-layer rollback /path/to/vault --index-only   # when the graph cannot move with it
```

A successful build writes two extra plain files next to the index in
`<vault>/.context/`:

- `index-manifest.json` — `{built_at, source_count, sources: [{path, sha256,
  size, mtime}], max_file_bytes, skipped: [{path, reason}]}` (see
  [What the index skips](#what-the-index-skips)). Readable, diffable and
  deletable without this tool; `status` falls back to the index rows when it is
  absent, so an older index still works.
- `index.sqlite.prev` and `index-manifest.json.prev` — the index and manifest
  that this build replaced; `context-layer index` also keeps `graph.sqlite.prev`,
  the link graph that belongs with them.

A build that fails writes neither: it stages into a temporary file and only
replaces the live index at the end, so the previous index and its manifest
survive a failed rebuild untouched.

### Incremental update (the default) and `--full`

`context-layer index` reads and hashes every in-scope file, as `status` does, and
then writes only what differs from the previous index: the notes whose bytes
changed, that were added, or that are gone. `context-layer index --full` always
rebuilds everything from the sources. The update works on a copy of the previous
index and publishes it exactly like a full build (FTS5 integrity check first,
then an atomic replace, `.prev` kept), so an interrupted update leaves the old
index. If the process stops between the index and the manifest being replaced,
the index is already the new one and the manifest the old one; the next `index`
run repairs that, because the update starts from the index rows, not from the
manifest.

The result is meant to be indistinguishable from a full build. The property test
`tests/test_incremental_index.py` compares, after every step of randomized edit
sequences, every row and column of `records` (ids included), `index_meta` (except
`built_at`), the schema, the full-text index (`fts5vocab`, `records_fts_docsize`,
ranked results with bm25 scores) and the manifest (except `built_at`). Record ids
are kept equal to a full build's, because equal-scoring notes are ordered by id: a
note whose position moves is written again at its new ids (its stored rows are
reused; its file is not decoded again).

An update is not attempted, and a full build runs instead (printing `full rebuild:`
and the reason), when there is no previous index; its format version, layout,
tokenizer or chunk size differ from this builder's; its rows or full-text table
are inconsistent; or the update would rewrite more than 40% of the records, where
a full build is faster. The link graph (`graph.sqlite`) is derived again after
`index`, from the notes the builder read and hashed in that same run (they are not
read a second time). A note whose bytes did not change reuses the parse the previous
graph stored for it (table `parse_cache`, keyed by its SHA-256 and invalidated by
any change to the graph builder). Every link is resolved again on every build, since
a new note can change how another note's links resolve. When nothing the graph
depends on changed (the notes, their SHA-256, size and mtime, the exclusions, and the
vault's file list when a link needed it), `graph.sqlite` is left as it is and the
summary line ends in `(unchanged)`; `graph.sqlite.prev` is refreshed as after any build.

What it does not save: the walk, the boundary checks, and reading and hashing every
file still run on every `index` (the reads overlap on a few threads); only decoding,
chunking, full-text insertion and the rebuild of the full-text index are limited to
the changed notes. There is no timestamp shortcut, because a file edited to the
same size with its old timestamp restored must still be found.

When nothing differs (the same notes with the same bytes, ids and timestamps, and
the same `index_meta` and manifest apart from `built_at`), the run prints
`incremental: 0 changed, ... ; index unchanged` and leaves `index.sqlite` and
`index-manifest.json` as they are: no staging copy, no second integrity check, no
replacement. Their `built_at` then stays the time of the build that wrote them.
The `.prev` copies are refreshed as after any build, so running `index` twice still
clears a deleted note from `.prev`. The live index passed the FTS5 integrity check
when it was written; `status` runs that check again, and `index --full` rebuilds. The `index_sha256` in a search's coverage receipt is
the hash of the index file, which differs between an updated and a rebuilt index
with the same content.

`rollback` swaps the live index with `.prev`, and the manifest and link graph
(`graph.sqlite`) with it, so the two stay one generation. It checks that before
it writes anything: when a `graph.sqlite` exists without a `graph.sqlite.prev`
(the previous build had no graph, or the index was rebuilt with
`router/build_index.py` directly), or when the `.prev` graph was
not built from the `.prev` index (its notes or their hashes differ, as after two
`index --no-graph` rebuilds in a row), it refuses rather than pair an index with
a graph of another build, and `--dry-run` refuses the same way.
`rollback --index-only` then restores the index and its manifest alone and
leaves the graph as it is; `status` reports it as another generation
(`graph.matches_index: false`) until `context-layer index` rebuilds both. It is
its own undo — the replaced index becomes the new `.prev` — and it refuses with
an explicit message when there is no `.prev` to restore, which is the case for a
vault that has only ever been indexed once. If the restored index predates
manifests, the newer manifest is set aside rather than left to describe bytes
the index does not hold, and `status` falls back to the index rows.

Each rename is atomic on its own. There is no atomic two-file rename, so an
interruption between them leaves the restored index in place with a duplicated
`.prev`, which is the safe direction to fail.

After a rollback the index is, by construction, older than the sources.
`status` says so in its reasons rather than reporting a healthy index.

## Note names, aliases and headings (opt-in)

By default a note is found only by its text (its frontmatter and headings are part of
that text; its file name is not). `context-layer index <vault> --name-fields` also
writes a second full-text table, `names_fts`, with one row per indexed file: the file
name without its folder and extension, the `aliases:`/`alias:` values of a leading
frontmatter block (an inline list, a scalar or a `- item` list; no other YAML is
read), and the Markdown headings outside fenced code (at most 100 and 4,000
characters per note). `context-layer search <vault> --prompt "..." --name-fields`
(or `eval/retrieve.py --name-fields`) then fuses two rankings by reciprocal rank
(k = 60): the usual content ranking and the notes whose name, aliases or headings
match any query term, ordered by bm25 with column weights 10 (name), 8 (aliases)
and 4 (headings). Both constants are untuned defaults. A note only the name ranking
finds is delivered like any other hit; its item has `match_in_content: false` when
its text holds no query term. The option applies to `--method fts`,
`fts-canonical` and `synaptic` (its fts part; the graph seeds stay the content
ranking) and is refused for `grep` and `router`.

Nothing changes without the option: the records, the `records_fts` table and the
default packet are the same bytes whether or not the table exists, and an index
built without `--name-fields` refuses `--name-fields` on search with the command
that fixes it. Building with or without the flag after the other one rebuilds in
full (an incremental update keeps `names_fts` in step for changed, added and
removed notes, and the property test covers it). Excluded notes are filtered from
the merged ranking like any other.

Measured with `python3 tests/dev_names_eval.py` on two fictional dev sets (not the
sealed benchmark): 32 of 32 questions that name a note (`tests/fixtures/dev_names.py`)
are answered with the option and 0 of 32 without; every question the default search
answers is still answered; the mean evidence per question grows from about 10 to 41
estimated tokens on the name questions and by 9 and 21 tokens on the `dev_bridge.py`
aggregate and bridge questions (extra notes the headings pull in), and is unchanged on
the others. Alias and heading questions are answered by default too (those words are
in the note's text), so they show no gain. A dev-set result, not a benchmark claim.

## What the index skips

`context-layer index` (router/build_index.py) never fails and never goes silent
because of one file. A file in scope that it cannot index is skipped, printed
and listed in `index-manifest.json` under `skipped`, with one reason:

| Reason | The file |
| --- | --- |
| `oversize` | is larger than `max_file_bytes` (the entry also records its `size`). |
| `unreadable` | could not be read (for example, permission denied). |
| `unsupported_name` | has a name the source policy cannot accept: a backslash anywhere, or a `:` in its top-level folder or file name. Both are legal on POSIX, but such a name could not be read back safely as a source. |
| `not_utf8` | is not valid UTF-8 (a Latin-1 CSV, say). Before 0.4 one such file stopped the whole build. |

The build prints the count by reason and the first 20 paths:

```text
indexed 5 files, 5 records -> .context/index.sqlite
skipped 2 files (1 oversize, 1 not_utf8), listed in .context/index-manifest.json:
  big.md: oversize (2708941 bytes; max_file_bytes is 2000000)
  export.csv: not_utf8
```

The counts are also stored in the index itself, so every search reports them in
its coverage receipt (`coverage.skipped_by_reason`, see
[cli.md](cli.md#evidence-items-and-the-coverage-receipt)): a `NOT_FOUND` then
says how many files were never searchable. Empty files, symlinks, dot paths, tool
folders and configured exclusions are not listed: they are out of scope, not
failures, and an excluded path is never named. A folder whose name is
unsupported is still walked, so each file in it is listed.

`max_file_bytes` in `.context/routes.json` sets the size limit: a whole number of
bytes from 1 to 50,000,000; the default is 2,000,000. The manifest records the
limit a build used.

A build that fails for any other reason (an untrusted `routes.json`, a staged
index that fails the FTS5 integrity check) exits 1 and leaves the previous index
and manifest untouched, as described above.

## Why stale evidence is withheld

The index stores each source's SHA-256 at build time. Before any content is
emitted the retriever re-reads the source and compares:

- `eval/retrieve.py` (the `search` path, the MCP `search_vault` tool and the
  hook) leaves that one source out and lists it under `withheld` with
  `reason: "changed since indexing"` (or `"deleted since indexing"`,
  `"symlink since indexing"`, `"unreadable since indexing"`) and
  `next: "context-layer index <vault>"`. The other hits are still delivered;
  the status comes from what remains, so a packet whose only hit changed is
  `NOT_FOUND` with the reason attached. The symlink and vault-boundary checks
  run on each note just before it is read, and only on the notes read (at most
  `--top-k` for fts), so a symlink elsewhere in the vault no longer fails
  unrelated searches. See [cli.md](cli.md#withheld-sources).
- `router/context_router.py` (experimental) raises
  ``index_source_hash_mismatch: <path>; run `context-layer index <vault>` `` and
  prints `Evidence packet withheld; status: ERROR`, exit 1.

Neither emits the new bytes. A packet that silently mixed a stale hash with
current content would be worse than no packet: the hash would certify text that
was never checked. Up to 0.3.0 release candidates the `search` path refused the
whole packet as well; since recording a work log (`rules record`) rewrites
indexed files, that turned every later search touching them into an `ERROR`
until a rebuild, so the search path now withholds per source, as this page and
the README always described. The regression in `tests/test_health.py` edits a
source with its byte length and mtime preserved, asserts `status` reports it as
`changed`, and asserts that search withholds it (and the router refuses) with
the reason on stdout and the new content absent from the output;
`tests/test_harden.py` covers the hook and MCP paths.

## Lab candidate 1: is patch 03 enough?

**Conclusion: yes. `retrieval-patches/03-strict-source-hash.patch` already covers
the same-size/same-mtime cache miss, and no fourth patch is warranted.**

Evidence, from the patch text and from the lab's isolated upstream run:

1. The patch text removes exactly the stat-keyed shortcut. It deletes the
   `sha_cache` lookup guarded by `hit[0] == st.st_mtime_ns and hit[1] ==
   st.st_size` and moves `hashlib.sha256(path.read_bytes())` above the `stat()`
   call, so the digest is recomputed from current bytes on every verification.
   The remaining cache metadata is still written, for compatibility with the
   surrounding patch set, and is no longer consulted as verification evidence.
2. The recorded run is the counter-example, not a description of one. It pins
   upstream `v3.0.1`, commit `61a88467d748fbf94ebc340cb932da7c7e9a78b7`, and
   rewrites the source with `same_size: true` and `same_mtime: true`. With
   `01+02` the query after the rewrite returns the pre-edit record
   (`text: "AAAA"`) with `stale_count: 0` — the stale answer. With `03` applied,
   the same query returns `records: []`, `citations: []`, `abstained: true`,
   `stale_count: 1`.
3. The three patches reverse in order back to the original engine:
   `9f92e03f…48cb` → `be4c3b4c…0f6a` → `9f92e03f…48cb`, with `compile: pass`.
   The same digests are pinned in `retrieval-patches/compatibility.json`.

That run checks this failure mode only. It is not a measurement of retrieval
quality, thread safety, arbitrary concurrent file replacement, or speed. The
patch remains optional; installing this package applies none of the three.

## Lab candidate 2: `retrieval-patches/hook-visible-error.example.sh`

A host wrapper that runs a context helper, discards its exit code and injects
whatever reached stdout cannot tell a helper crash from "there was nothing to
add". The recorded shell run of an existing wrapper shows exactly that: a helper
exiting 7, a malformed response and a wrong-typed field all produced
`returncode: 0` with empty stdout. Silent empty success.

The example here is the generic form of the reviewed candidate. It takes the
helper command from `CONTEXT_HELPER` (or the first argument) and the interpreter
from `CONTEXT_PYTHON`; it contains no personal path and no private hook name.
Its contract:

| Case | Result |
| --- | --- |
| Valid `{"hookSpecificOutput":{"additionalContext":"…"}}` | The context on stdout, exit 0. Trailing newlines inside the field survive. |
| Helper exits non-zero | `[Memory unavailable: helper exited N]`, exit 1 |
| Response is not JSON | `[Memory unavailable: malformed helper response]`, exit 1 |
| `additionalContext` is not a string | `[Memory unavailable: malformed helper response]`, exit 1 |
| Helper produced no output at all | `[Memory unavailable: empty helper response]`, exit 1 |

A well-formed response whose `additionalContext` is an empty string is a
legitimate "nothing to add": the wrapper stays silent and exits 0. That is the
one deliberate difference from the reviewed lab function, which folded a
zero-byte response into the same silent path; here a helper that produced
nothing at all is reported, because the failure being fixed is precisely an
empty stdout with exit 0. `tests/test_health.py` runs all five cases against a
fake helper and asserts the table above.

**Limits.** It checks the helper's exit code and the shape of the response. It
does not verify what the helper retrieved, whether that content is current, or
whether it should be injected at all — that stays the host's decision, and
retrieved text is data, never instructions. It is a shape to copy into a wrapper
you own, after your own review.

**It is not installed.** This package registers no hook, edits no host settings
and runs no wrapper. Neither lab candidate has been promoted to a live host, and
neither is evidence of host integration: that requires a separate reviewed
evaluation and the user's authority for that action.
