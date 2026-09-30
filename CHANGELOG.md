# Changelog

## Unreleased

- Brain View: the HUD title and counts line take a lighter typography
  (a light, widely spaced title and a
  monospace line; local font stacks, themeable through `--nb-hud-font` and
  `--nb-hud-mono`). The resting colours, point sizes, opacities and link width
  already followed a fixed degree scale; `tests/look.test.js` now pins
  them and checks that the retrieval overlay colours stay distinct from them.

## 0.4.0

Retrieval that says what it searched and what it left out, hosts that get
bounded answers and exit codes they can trust, verification a worker cannot
forge, a memory that survives torn lines and forks, a Brain View that keeps its
layout, and an optional advisor (Jev) that is off by default. Most entries close
a finding of the 0.3.0 audit; the ids in brackets (A retrieval, B hosts, rules
and brain, C memory, tasks and orchestration, D Obsidian plugin, E bench, CI,
packaging and docs) are the audit's, and CONTRIBUTING.md names the suite that
guards each area.

**New**

- **The optional advisor (Jev), off by default** ([docs/jev.md](docs/jev.md)).
  `context-layer jev status|off|shadow|on|report|purge` and
  `search --jev` / `--no-jev`. The advisor asks a provider whether each passage,
  and each note the links reached but the packet did not deliver, would help
  answer the question. `shadow` only counts and shows the answers; `on` (refused
  without a calibration receipt) appends the judged notes as byte-exact passages
  after the unchanged packet, within `jev_extra_tokens` (default 400, at most
  2,000). No default provider; no key in any file; privacy gates (local-only
  prefixes, a fail-closed frontmatter convention, a secret scan) before anything
  is sent; a freshness re-check after every call; every failure falls back to
  the local packet; a counters-only call log and a keyed-hash cache under
  `.context/`; kill switches `CONTEXT_LAYER_JEV_DISABLE` and
  `.context/jev.disabled`. Providers: `systemone`, `openai_compat`, `host_cli`
  (the `claude` CLI in print mode with a JSON schema and no tools), `cmd`,
  `recorded`, `fake`. Only `context_layer/jev_client.py` may open a network
  connection or start a model CLI for the advisor; `make network-guard` scans
  `context_layer/`, `router/` and `eval/` for that (other modules start programs
  of their own, such as the task backends, and `bench/` and `scripts/` are not
  scanned). `search`, the prompt hook's
  `auto_context`, `answer` and `memory` are wired (below). No quality number has
  been measured yet.
- **Recording and calibration.** `jev record` asks the configured provider a
  file of questions (dry run unless `--run`) and keeps one `jev-recording/v1`
  row per question: hashes, the validated answer or a failure code, counters,
  never text. `jev calibrate` turns the dev-set evaluation of a recording
  (`tests/jev_dev_eval.py --provider recorded:FILE --json`, with
  `--dump-questions` for the questions to record) into the
  `jev-calibration/v1` receipt `on` requires, `passed` per question purpose
  against bars written before the first recording. `jev shadow|on
  --provider-kind recorded --recording FILE` configures the replaying provider.
  The development set (`tests/fixtures/dev_jev.py`, hashed) and its evaluator
  are development aids, not a benchmark.
- **The advisor over MCP.** `search_vault` takes `jev: true` (default false):
  a plain packet unless the vault owner enabled the advisor, else the same
  shadow block or rescued passages as `search --jev`; the new read-only
  `jev_status` tool prints what `jev status --json` prints. That made eight
  tools; `check_claims` (below) is the ninth.
- **The advisor in the prompt hook** (`auto_context`, enabled only by
  `jev shadow|on --enable auto_context`). On every prompt the hook asks a
  topicality question and the relevance questions within `hook_timeout_s`
  (default 2 s, never past 25 s after the hook started; under 1 s left it asks
  nothing). `shadow` leaves the hook's output byte for byte and counts; `on`
  (a receipt covering relevance and topicality) appends rescued notes as
  marked blocks only when the gate passed. Every failure leaves the output
  unchanged with exit 0 and one counter row; the lossy `gate_skip` lever is the
  only way to lose context.
- `eval/retrieve.py --jev-candidates N`: a model-free side channel listing the
  notes the packet did not deliver (link-reached notes the lexical link rule left
  out, and the bm25 tail); evidence unchanged.
- **Match-anchored fts delivery** (`--delivery window`, the default). A note
  that fits `--per-source` is delivered whole, as before; a longer note is
  delivered as the paragraph-sized windows in which FTS5 finds query terms
  (most distinct terms first, at most 3 per note, blank-line neighbours
  merged), each a verbatim byte span with its lines. `--delivery prefix` keeps
  the 0.3 behaviour for comparison; `grep` always delivers prefixes. The fts
  part of a default synaptic packet follows the same rule, and its extras never
  overlap those spans. The prompt hook's fts marker gains `lines=a-b`.
  (`indexed_notes`, `index_sha256`, `query_terms`, `match_expression`,
  `skipped_by_reason`), so "nothing searched" can be told from "nothing found";
  a prompt made of stopwords says `reason: "no searchable terms"` (A-05, A-18).
- **`context-layer graph health`**: orphans, broken links by reason (with an
  alias hint), ambiguous links with their candidates, links into excluded
  paths, stale notes, frontmatter problems and `supersedes` cycles. Read-only.
- **`context-layer doctor [--host …]`**: offline, read-only checks of a host
  set-up (config present, paths exist, Python and SQLite FTS5, rule parity,
  hook command lines valid for this binary, hook event names known).
- **`context-layer brief VAULT [--json]`**: a session brief built from bytes
  only. Every line names its file and the first 8 hex of that file's SHA-256
  and quotes it (cut, never summarised): index status, stale memory sources,
  the last `LOG.md` records, open work, newest memory records, the last
  activation. `rules hook session-start --brief` adds it to Claude Code's
  session context under a 3,000-character cap.
- **Delivery ledger and citation check.** Opt-in (`install --session-evidence`,
  or `--session-evidence` on the hook line and `CONTEXT_LAYER_SESSION_EVIDENCE=1`
  for the server, plus an existing `.context/session-evidence/` folder), the
  prompt hook and the MCP server record `{path, sha256, packet_id, at}` of every
  delivered item in `.context/session-evidence/<session>.jsonl` (ids and hashes
  only; appending stops at 4 MiB per file, and only the 50 most recently written
  session files are kept). The Stop hook's model-free check reports vault paths and hash prefixes the answer
  cites but the session never delivered; it never blocks.
- **Verification ledger.** `.context/tasks/LEDGER.jsonl` is hash-chained;
  `tasks ledger [--replay]` checks the chain and recomputes verdicts; `tasks
  list` and `tasks show` mark a verdict `UNATTESTED` when no ledger line records
  it (a rewritten `result.json` shows as UNATTESTED). Output directories are
  pinned by identity (device and inode of every component) at `new` and
  re-checked at every step; leases stop two tasks from sharing a directory;
  runner claims stop double dispatch; `tasks recover` ends tasks whose runner is
  gone and stops the orphaned child (C-01, C-02, C-04, C-17).
- **Memory format 2.** Sorted sources in the id, explicit `closes`, only
  in-force results close tasks, forks detected (`verify` reports each fork
  point, `resume.conflicts`, a Conflicts section in MEMORY.md), `--allow-fork`,
  and a merge record whose `supersedes` lists every head. New commands:
  `memory repair` (moves only a torn final fragment to
  `records.jsonl.torn-<utc>`), `memory rebind` (a moved source's new path, as a
  superseding record; `resume` suggests `moved_to` from the index manifest),
  `memory mirror --notes FOLDER` (one visible note per decision and task,
  frontmatter with wikilinked `supersedes`, the folder excluded from retrieval
  on the first run), `memory session list|show` (opt-in bill of materials with
  `CONTEXT_LAYER_SESSION`). `memory.record_id` is public; MCP `memory_record`
  takes `closes`. Records written before 0.4 are still read (C-06, C-08, C-11,
  C-21 to C-24).
- **Handback and jobs.** A claim-span gate (a span anchors a claim only with
  at least 3 words or 12 visible characters), an audit sample drawn from a seed
  fixed at check time, `support-job/v1` limits enforced at dispatch
  (`max_elapsed_seconds` bounds the run), exclusive `job.md` creation, a 32 MiB
  packet size cap checked before reading, and one line model for every line
  number (C-03, C-07, C-12 to C-14, C-27).
- **Backends.** The `claude` backend runs the child in a temporary workspace
  outside the vault with `permissions.blockReadsOutsideWorkingDirectories` and
  a `--max-budget-usd` spend cap; the `codex` backend's command line follows
  the public Codex documentation (`codex exec --sandbox workspace-write
  --skip-git-repo-check --json --ephemeral …`, prompt on stdin); `tasks cost`
  labels its USD figure host-estimated and adds `modelUsage` token totals when
  the backend reports them (C-19, C-20, C-09).
- **Rules and hooks.** `install claude-code --rules` adds a PostToolUse hook
  that records the vault paths the agent wrote, so the Stop hook asks only
  about the agent's own edits; it honours `routes.json` exclusions, treats
  future timestamps as unknown, keeps vault snapshots for the eight most recent
  sessions and reports broken parity once in plan mode. `rules init
  --single-source` writes a `CLAUDE.md` that imports `@AGENTS.md` (documented
  with its host caveats; byte-identical twins stay the default). `rules init`
  and `brain init` exclude the root rule files from retrieval, since the host
  loads them (B-03, B-04, B-05, B-13).
- **Installer.** `--scope local` writes the hooks to `.claude/settings.local.json`
  and the installer says which entries carry machine-specific paths;
  `--max-context-chars` (default 9,000) packs the hook output under the host's
  context cap; `install codex --hook` and `--rules` write Codex's documented
  hooks file (labelled expected but unverified), `$CODEX_HOME` is honoured,
  `config.toml` is parsed before it is written (B-06, B-10, B-19 to B-22).
- **MCP server.** Caps: `search_vault` `top_k` ≤ 20, `budget` ≤ 24,000
  characters, `per_source` ≤ 6,000; `memory_resume` `limit` ≤ 200; one tool
  error names every over-cap argument. `ping` and `notifications/cancelled`
  are answered while a call runs; malformed bytes and odd request ids get a
  JSON-RPC error, never a crash; tool annotations; `initialize` negotiates
  2025-11-25, 2025-06-18, 2025-03-26 or 2024-11-05, and a request that carries a
  supported version in `_meta` works without a handshake; `read_source` streams
  in 64 KiB chunks; the prompt reaches `eval/retrieve.py` on stdin; marker
  header fields are escaped so a file name cannot forge them (B-01, B-07 to
  B-09, B-11, B-12, B-24).
- **Brain View** (`obsidian-plugin/`): a link-aware layout that stays stable
  across link edits and new notes (the remembered layout is a pinned cache, not
  a re-solve), a region palette chosen to stay apart under the common
  colour-vision deficiencies, keyboard focus and an ARIA role on the stage,
  `normalizePath()` for the trace-path setting (only an `activation*.json`
  directly inside a `.context` folder is accepted), deferred views and pop-out
  windows handled, per-file vault events applied incrementally, an unmatched
  trace reported as such instead of dimming the whole graph; 16 Node test
  files including static GLSL checks (no shader is compiled), a
  writer-to-plugin contract test and a bundle test (D-01 to D-14).
- **Bench.** Equal budgets are enforced per method and asserted; error rows
  stay in the denominator; a passage that is not a verbatim span of the file it
  names (exactly, or after whitespace and NFC normalisation) earns nothing; every result file records Python, SQLite, Unicode,
  platform, the scorer's hash and a `+dirty` mark; TREC qrels/run export;
  `bench/select_pilot.py --check` recreates the 0.3 live pilot's case
  selection; `bench/test_runner.py`; and [bench/INSPECTIONS.md](bench/INSPECTIONS.md)
  lists every run of the sealed set with its purpose (E-01, E-10 to E-13,
  E-21).
- **Release tooling and CI.** `scripts/audit_history.py` (an English and
  leak audit over the whole history, trailers listed) with a `release-audit`
  workflow; `bench-reproduce` and `doc-claims` jobs, backed by
  `tests/test_doc_claims.py` (every number in README and docs/subagents.md is
  regenerated from result files) and `tests/test_docs_links.py`; a reproducible
  sdist (`scripts/normalize_sdist.py`, pinned build backend, `make dist`);
  `scripts/check_distribution.py` walks the 0.3 surface; `make network-guard`;
  pinned node24 actions, Dependabot, timeouts and concurrency groups, a
  non-blocking Windows smoke job; the Makefile and the bench runner run Python
  in UTF-8 mode (E-05 to E-09, E-16 to E-18, E-22).
- **Docs.** [docs/jev.md](docs/jev.md); a multi-host table in
  [docs/host-integration.md](docs/host-integration.md) (Claude Code, Codex,
  Cursor, Gemini CLI, Antigravity, OMP, OpenCode, Hermes Agent) with caps, exit
  codes, packing and protocol revisions; [discipline/WORKED_EXAMPLE.md](discipline/WORKED_EXAMPLE.md);
  format-version and exit-code tables for every command in
  [docs/cli.md](docs/cli.md); memory format 2 in [docs/memory.md](docs/memory.md);
  skip reasons and the rollback guard in [docs/source-lifecycle.md](docs/source-lifecycle.md);
  the Obsidian link rules, each with its source page, in [docs/synapse.md](docs/synapse.md);
  flag tables for `eval/retrieve.py` in [eval/README.md](eval/README.md) (E-25).

- **Advisor answer checks.** `context-layer jev answer VAULT --claims FILE`
  checks each citation mechanically and asks one `claim_support` question per
  passing citation (claim, verbatim quote, enclosing section, never cut); a
  citation that fails is never asked. Verdicts are advisory: supported,
  contradicted, insufficient or uncertain. `handback check --jev` adds, in `on`
  mode only, an advisory `jev` note to each record that passed; `ok`, `problems`,
  `mechanically_checked`, the digest, the ledger line and the exit code never
  move, and shadow changes no output byte. MCP `check_claims` is the ninth tool
  (at most 20 claims, 8 citations each; `jev: true` is opt-in per call).
- **Advisor memory review.** `context-layer jev review-memory VAULT --proposal
  FILE` checks a memory proposal's spans without a model and, in shadow or on,
  asks the four `memory_*` questions (support, commitment, kind, relation to up
  to four prior records). The report is advisory; the memory store is only read.
- **`jev status --check`**: a loopback-only reachability probe
  (`jev_client.probe`; `claude --version` for `host_cli`; nothing for remote,
  recorded or fake providers). Plain `jev status` still sends nothing.
- `jev on` and `jev calibrate` require the bars of every enabled feature's
  purposes (`claim_support` for answer, the four `memory_*` for memory);
  `--disable answer|memory` turns a feature off instead.
- **Brain View advisor layer** (Obsidian plugin, hidden by default): what the
  advisor did in the last retrieval, read from the activation trace only; the
  trace's `jev` block now records `would_rescue`, and the shadow HUD shows
  "would rescue N". Empty states say why nothing is shown.
- **Incremental indexing.** `context-layer index` rewrites only the notes whose
  bytes changed, appeared or disappeared, keeps the record ids a full build would
  give, and falls back to a full build (saying why) when the old index cannot be
  updated in place; `index --full` always rebuilds. A property test compares the
  updated index with a `--full` build after every step of randomized edit
  sequences (rows, ids, meta, schema, full-text postings, ranked scores, the
  manifest), and an interrupted update leaves the old index. The link graph is
  still rebuilt in full. No speed-up is claimed: on 10,000 small notes the walk
  and hashing dominate and both paths took about the same time.
- **Name fields, opt-in.** `index --name-fields` adds note name, alias and heading
  columns; `search --name-fields` ranks with them. Off by default, and default
  output is pinned byte for byte by a golden test. On the fictional dev set of
  questions that name a note (`tests/dev_names_eval.py`), 0/32 become 32/32;
  alias and heading questions already worked without it.
- **Link suggestions from usage, opt-in** ([docs/synapse.md](docs/synapse.md)
  section 11). With `"record_usage": true` in `.context/routes.json`, each fts or
  synaptic retrieval appends the delivered note paths (no text, a hashed session
  id) to `.context/usage-ledger.jsonl` (rotated at 1 MiB). `context-layer graph
  suggest VAULT [--min-count N] [--limit N] [--json]` lists notes delivered
  together that no link joins, with counts, sessions and days; it writes
  nothing. Nothing reads the ledger back into retrieval: a test holds the packet
  byte-identical with the ledger absent, present or over 5 MiB. Prior art:
  co-citation analysis and search click logs.
- **`context-layer session show VAULT [ID] [--json]`** and `session list`: a
  read-only join, per agent session, of the delivered evidence, memory records,
  tasks and their ledger attestation, and advisor counters, each row citing the
  file and line it came from; torn or missing sources are reported, and output
  is bounded (`--limit`, `--max-bytes`). Advisor counters are matched by time
  window only and marked unattributed.
- `install` writes `PYTHONUTF8=1` into every host command; a test scans the
  runtime code for text I/O without `encoding=` (E-17). An empty or blank hook
  prompt exits 0 with nothing on stdout (B-12). For Claude Code, the hook also
  shows the withheld-source notice to the user as `systemMessage` (B-26).
- `CITATION.cff`; `docs/cli.md` lists the less common flags (E-25);
  `eval/LIVE_COMPARE.md` says the 0.2 judging was unblinded (E-14); a docs
  phrase-grep test and a canary write-boundary test (E-22).

**Fixed**

- `context-layer index VAULT --out=PATH` (the `=` spelling) builds the link graph
  from that index (B-25).
- `init` and the router read and write notes, prompt files and run artifacts as
  UTF-8 whatever the locale (E-17).
- Query terms were folded differently from the FTS5 tokenizer, so words with
  some Unicode letters (`Straße`) could not be found verbatim. One folding
  contract (`router/textfold.py`): terms go to MATCH as written, term
  boundaries follow the tokenizer (SQLite is asked once per character), and the
  five normalisers became one (A-01, A-22).
- An exclusion entry with stray whitespace passed the strict loader and
  excluded nothing; it is refused now, and `init` writes only entries its own
  loader accepts (A-02, A-04).
- A file whose name holds `:` in its first segment or `\` anywhere aborted
  `index` with a traceback; it is skipped as `unsupported_name` and counted
  (A-03).
- Notes over the size limit, unreadable notes and files that are not UTF-8
  were skipped silently. They are recorded in the manifest by reason, printed
  as one count, listed by `status` under `now_skipped` (overall `degraded` or
  `stale`, never `ok`) and named in the coverage receipt; `graph.build` counts
  them too. The limit is `max_file_bytes` in `routes.json` (default 2,000,000,
  at most 50,000,000). A rebuild that fails for another reason keeps the
  previous index and graph pair (A-05, A-23, C-25).
- Default fts delivered the beginning of each matched file, so on a long note
  whose only match lay further in, the passage held no query term and nothing
  said the file continued. A note that fits `--per-source` is still delivered
  whole; a longer one now arrives as match-anchored verbatim windows, each with
  `start`/`end`, `line_start`/`line_end`, `source_chars`, `truncated` and
  `match_in_content` (A-06, E-04; see New).
- A UTF-8 BOM hid the frontmatter from the link graph; NFC and NFD note names
  did not meet in link resolution; the trace wrote on-disk paths the plugin
  could not match. Fixed together: BOM ignored, resolver keys NFC and
  casefolded, trace paths NFC (A-07, A-08, D-07).
- Default synaptic added no passage beyond the fts prefix unless a link or a
  named note triggered it. The complement-window step now runs on every seeded
  search with a readable graph; the superset property still holds (A-09).
- One note swapped for a symlink after indexing turned every query into
  `ERROR`; it is withheld with `reason: "symlink since indexing"` and the other
  hits are delivered (A-11).
- A damaged `graph.sqlite` made synaptic `ERROR`, sometimes with raw SQLite
  text; it degrades to exactly the fts packet with
  `synapse.decision: "graph_unreadable"` and a note naming
  `context-layer index <vault>` (A-12).
- Line numbers used `str.splitlines()`, so U+2028, U+0085 and form feed shifted
  anchors away from what editors show. One line model everywhere (a line ends
  at `\n`; a trailing `\r` is dropped): graph, synaptic passages, packets,
  handback and memory; a memory record holding such a character no longer makes
  the store unreadable (A-13, C-06, C-07, C-12).
- The frontmatter subset guessed: block scalars, trailing comments and
  multi-line lists produced garbage aliases. What it cannot parse is left out
  and counted (A-14).
- Link accounting: links to existing non-note files and to excluded notes were
  `missing`; they are `attachment` and `excluded` now, ambiguous targets keep
  their candidates, and a bare `[[alias]]` no longer resolves (Obsidian's rule,
  checked against its help pages). Fences inside blockquotes and indented code
  are skipped; `%%` and HTML comments are kept because Obsidian records those
  links (A-15, A-16).
- fts spent top-k slots on byte-identical text; duplicates are listed under the
  first item's `duplicates` and take no slot. Task packets count them toward
  the fetch window, so allowed sources ranked past the first window are still
  fetched instead of leaving the packet empty (A-17, C-05).
- Depth 2 was inert in dense vaults with no stop reason, and a node-cap hit
  discarded the depth-1 expansion too. `stops.below_threshold` and
  `stops.node_cap` are counted, and a cap hit undoes only that hop (A-19).
- `score_packet.py` credited prompt words echoed in the routing metadata and
  dropped missing packets from the headline score (A-21).
- Hook command lines could exit 2 on an argparse error, which made Claude Code
  erase the prompt; every hook failure exits 1 and prints one line, and an
  unknown `--method` runs fts (B-02). `rules record` text could inject the
  records end marker; a non-UTF-8 rule file crashed the rules commands; CRLF
  files received LF entries; the lock fallback never recovered from a stale
  marker; SessionStart wrote state into folders without rules (B-14 to B-18).
- `install --scope user` omitted the `PYTHONPATH` the project entry carries;
  `uninstall` left backups of the tool's own marker; the starter brain wrote
  routes for excluded folders and "missing" links and had no `--no-examples`;
  `index --out=PATH` broke the graph step; the hook's withheld notices reached
  only the debug log and now reach the user through `additionalContext`
  ("Not included: …") (B-19, B-22, B-23, B-25, B-26).
- `resume` let a retracted result close a task and "Could not do <id>" close
  it too; re-adopting superseded content was a silent no-op; reordered sources
  defeated de-duplication; forks went undetected; a renamed source was only
  "missing"; a torn final line blocked every write (C-08, C-21 to C-24).
- Cost totals dropped an unreadable task silently and accepted negative or NaN
  amounts; `status` called emptied or oversized files deleted; `rollback`
  without `graph.sqlite.prev` mixed generations (it refuses now;
  `--index-only` restores the index alone); `live_compare.py` had no host
  timeout and credited substring deliveries; `tasks` and `packet` printed home
  paths without `--verbose`; writes were not atomic (C-09, C-25, C-26, C-28
  to C-30).
- Brain View: a fresh trace matching no note was reported as shown and dimmed
  the graph; the writer's `PARTIAL`/`NOT_FOUND` statuses did not match the
  plugin's `OK`; depth-2 back edges pulsed at the wrong hop; per-file events
  ran full rebuilds; legend listeners and `signalEvents` grew without bound;
  activated notes vanished when unlinked notes were hidden (D-01 to D-04,
  D-10, D-11, D-14).
- `run_offline.py --budget-tokens N` was a no-op; the published sub-agent
  payload numbers no longer reproduced; design-rationale §7 described a
  benchmark that did not exist; the 0.3 live-pilot description did not match
  the run; the bench README's hook sentence was stale; a relative `PYTHON`
  broke `make test`; 7 of 16 suites failed under a Latin-1 locale; the sdist
  embedded the builder's account name; `eval/adapters/fts_sqlite.py` reused an
  index across vaults and `embedding_stub.py` cached vectors by text alone
  (E-01 to E-03, E-13, E-15 to E-18, E-23, E-24).
- `doctor` accepted an unknown `rules hook` event name in an installed hook.
- `search --jev-candidates` is refused with exit 2 for grep, fts-canonical,
  router and compact synaptic, where it used to be dropped silently (F2-07).
- One session-evidence writer: the hook and the MCP server used two writers with
  different file-name schemes, so the ledger of a session id that is not a plain
  name was never the file the Stop check reads. Both call
  `session_evidence.record_delivery` now; a session ledger stops at 4 MiB (one
  overflow line) and the 50 newest session files are kept (F2-08).
- `hook` refuses a synaptic budget flag that changes nothing (exit 1, one line);
  `rules check` names a line-ending-only difference between CLAUDE.md and
  AGENTS.md; `tasks ledger` also cross-checks the chain against each verdict's
  cited line, so an edited head line or a cut tail is exit 1; installer writes to
  a host settings file are atomic and a symlinked file is kept and named; the
  Jev secret scan recognises a bare JSON web token; `init` writes the package
  version and a UTC time into `routes.json` (F2-30, F2-46, F2-44, F2-43, F2-42,
  F2-15).
- The Jev advisor's frontmatter gate keeps a note local when a privacy value is
  not a plain scalar: a block scalar, an anchor, an alias, a tag, a flow value
  or a comment (F2-38).
- CI: the release audit now passes on a one-commit snapshot of the tree (the
  allow-list matches the tree and a test checks it), and the synthetic
  comparison results are regenerated with a test that fails on drift (F2-01,
  F2-02).

**Changed (may affect callers)**

- `search` rejects flags that do not apply to its method with exit 2, as
  `packet build` and `install` already did: `--max-hops`, `--extra-tokens`,
  `--budget-tokens`, `--compact` and `--record-query` are synaptic only;
  `--jev-candidates` is for fts and default synaptic (E-08).
- MCP tool schemas carry `maximum`; an over-cap value is a tool error, not a
  clamp. `memory_record` gains `closes`; `search_vault` gains `jev`; the tool
  list has eight entries (`jev_status` is new); the rule templates name it.
- Hook command lines exit 1 on any failure, never 2.
- `rules init` and `brain init` exclude `CLAUDE.md` and `AGENTS.md` from
  retrieval; an existing `routes.json` is not changed (a notice is printed).
- New memory records are format 2 (`format_version: 2`, `closes`, sorted
  sources in the id); `verify` and `resume` still read format 1.
- The activation trace gains `mode` (`superset`/`compact`), `max_hops` and a
  per-edge `hop`; `packet.status` is `PARTIAL` or `NOT_FOUND`, never `OK`;
  paths are NFC. `version` stays 1.
- `graph.sqlite` gains the tables `skipped` and `frontmatter` and two nullable
  columns on `unresolved`; `schema_version` stays 1 and a 0.3 graph is still
  read. `status` reports a `graph` object and `matches_index`.
- `tasks cost` prints host-estimated USD and labels `usage` "top-level agent
  loop only".
- fts items of a note longer than `--per-source` are match-anchored windows
  (several items per note are possible); every fts item carries `start`,
  `end`, `line_start`, `line_end` and `source_chars`. `--delivery prefix`
  restores the 0.3 delivery. Task packets and shared packets count notes, not
  items, against `top_k`.
- `.context/tasks/` gains `LEDGER.jsonl` and `.leases/`; tasks created before
  0.4 have no pin and are marked as such.
- The default synaptic packet may now hold complement windows on every seeded
  search (see A-09); the sealed-set numbers did not change beyond the estimate
  noise stated in bench/INSPECTIONS.md.

**Measured**

- Sealed benchmark, run twice on this tree (runs 9 and 10 in
  bench/INSPECTIONS.md, the second after window delivery and the advisor):
  completeness unchanged from 0.3 (grep 37, fts 33, synaptic 46 of 64
  answerable; synaptic 10 of 20 two-hop bridges); synaptic mean packet 527
  estimated tokens; the fts prefix and window arms give the same numbers on
  this vault (every note fits `--per-source`); the compact arm 37 of 64 at 334.
  No advisor arm was run: no recording of a live provider exists.
- Sub-agent payloads (`eval/orchestration_cost.py --standin`, regenerated into
  docs/subagents.md and tested): four workers' initial payloads 16,827 → 9,441
  (56%) with own packets or 11,626 (69%) with one shared packet; verification
  reading 12,452 → 2,580; the one planted fabricated quote was caught.
- The advisor: no quality number. No provider was called while building this
  version. The pipeline was run with the oracle (the labels answering): 299
  questions recorded and replayed, precision 1.0 and recall 16 of 16 at
  `rescue`, the topicality gate right on 12 of 12 prompts, no privacy trap in
  any request, a receipt computed and `on` with `auto_context` accepted for
  that fake provider. That checks the plumbing, not any model; `jev on` stays
  refused until a recording of your provider is calibrated.

**Not in this version**

- `jev review-memory` reviews proposal files only: the store keeps no spans, so a
  draft already stored cannot be reviewed. `handback check` has no `--jev-priority`:
  model output does not steer which records are sampled.
- The routing/packet step (`context-layer route`, `router/context_router.py`) is
  experimental and a candidate for removal later; nothing is removed here.
- The wheel does not carry the Obsidian plugin bundle (D-17); PyPI publication
  is not planned (E-19). Windows stays out of scope while the smoke job is
  non-blocking.

**Credits**

- The advisor follows the design of the optional Jev advisor in Avenox Beyin
  (v3.1.0–v3.5.1, MIT, Avenox), whose Jev integration was contributed by Forn
  and adapts Forn's hafiza-os (MIT): off by default, off/shadow/on, per-feature
  switches, a kill switch, a counters-only log, privacy gates and a freshness
  re-check. No code was taken. The provider protocol shape follows TypeSafe's
  public API documentation; the services and programs the advisor can call are
  listed in THIRD_PARTY.md, none affiliated with this project.

## 0.3.0

Your own links become a retrieval signal, the agent's work becomes recorded and
checkable, and you can see in Obsidian what a retrieval used.

**New**

- **Synaptic retrieval (opt-in, `--method synaptic`).** `index` also extracts the
  vault's explicit link graph into `.context/graph.sqlite` (wikilinks with
  alias/heading/block, embeds, relative Markdown links, frontmatter
  `related`/`up`/`parent`/`see_also`/`supersedes`; code ignored; ambiguous
  targets recorded, never guessed). A synaptic packet is the unchanged FTS packet
  plus link-reached passages within `--extra-tokens` (default 600); `--compact`
  keeps the smaller packer that can drop FTS evidence. Hub damping, fan-out and
  node caps, 1 hop by default (2 behind `--max-hops`), deterministic output,
  recorded stop reasons. MCP `search_vault` gains `method`/`budget_tokens`/
  `extra_tokens`/`compact`; new MCP `graph_neighbors`.
- **Activation trace.** Each synaptic retrieval writes `.context/activation.json`
  (random `run_id`, no prompt hash, query text only on opt-in, never read back
  into ranking, excluded/stale notes never listed; `write_activation: false`
  turns it off).
- **Context Layer Brain View** (`obsidian-plugin/`): a 3D view of the explicit
  link graph with the last retrieval's seeds, hops and traversed links. Plain JS,
  no npm dependencies, no network, writes nothing to the vault except its own
  settings file; 12 Node test files.
- **Starter brain.** `context-layer brain init` creates an Obsidian vault with an
  English, emoji-free layout adapted from Avenox Beyin (`--avenox-compat` for
  the original names), templates, example notes and the rule files.
- **Rules.** `context-layer rules init|check|record|hook|settings`: byte-identical
  `CLAUDE.md`/`AGENTS.md`, `LOG.md`, `BACKLOG.md`; plan-first, stop-and-ask and
  evidence-label rules; `install claude-code --rules` adds SessionStart/Stop hooks
  that block once when work was not recorded; `--plan-default` sets plan mode.
- **Lean sub-agents.** `packet build/show` (content-addressed shared evidence,
  re-verified before serving), `job new/validate/estimate` (`support-job/v1`,
  payload budgets), `handback check` (mechanical verification of worker evidence
  records; catches fabricated quotes), `handoff write`; `tasks new --job/--packet`,
  `--host-context inherit|safe-mode|bare`.
- **Sealed benchmark** (`bench/`): fictional 130-note vault, 72 sealed cases,
  offline scorer and a live-host arm. `docs/design-rationale.md`,
  `docs/brain-guide.md`, `docs/cli.md`, `docs/privacy.md`, `docs/subagents.md`,
  `docs/synapse.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `CREDITS.md`.

**Fixed**

- `tasks verify` could mark a pre-existing vault file as verified output when a
  backend rewrote `task.json`; definitions are now pinned at dispatch and only
  run-produced outputs count.
- Exclusions now match case- and Unicode-normalisation-insensitively
  (`private` excluded `Private/secret.md` only on case-sensitive systems).
- One strict `routes.json` loader everywhere; malformed files, wrong types and
  duplicate keys fail closed.
- An emptied or inconsistent FTS index is an error (index `user_version`, FTS5
  integrity check at build, consistency check on read), not a quiet `NOT_FOUND`.
- `init` no longer excludes note folders whose names merely contain a marker
  word ("Distributed…", "Temperature…", "Resources").
- A dotted capital I (`İ`) no longer splits a query term.
- The MCP server negotiates `protocolVersion` instead of echoing any value.
- `install claude-code --hook --method synaptic --budget-tokens N` wrote a flag
  the default synaptic hook ignores (it only sizes `--compact`). `install` now
  takes `--extra-tokens N` and `--compact`, and refuses `--budget-tokens`
  without `--compact`; `search --help` names all three.
- `install claude-code --plan-default` replaced an existing
  `permissions.defaultMode` (such as `"acceptEdits"`) and `uninstall` then
  deleted it. Install now refuses a different existing mode, and uninstall
  removes only a `"plan"` that install recorded setting
  (`.claude/context-layer.plan-default.json`).
- Docs: the 0.2 live result is "about 54% of the baseline's mean total tokens
  (about 46% fewer)", not "46% of its tokens".
- A note that changed after indexing turned every search that matched it into
  an `ERROR` with no evidence (and `rules record` rewrites the indexed
  `CLAUDE.md`, `AGENTS.md` and `LOG.md`). `search`, the hook and MCP
  `search_vault` (fts and synaptic) now withhold only that note, as the docs
  said, and list it under `withheld` with its reason and
  `context-layer index <vault>`; index-wide failures stay `ERROR`, the router
  still withholds its whole packet, and `packet build` and `tasks new` refuse
  to pin evidence while a note is withheld.
- Stale-index messages name `context-layer index <vault>` instead of "rebuild
  the index".
- `index` on a vault without `.context/routes.json` now says, in one stderr
  line, that it indexes with no exclusions and that `context-layer init` should
  run first (still exit 0).
- `brain init --into-existing` counted every layout folder as "would create";
  it now counts only folders that do not exist (`new_directories` in `--json`).
- `packet build --method synaptic --budget-tokens N` had no effect on the
  default synaptic packet. `packet build` now takes `--extra-tokens` and
  `--compact`, and refuses `--budget-tokens` for synaptic without `--compact`
  (exit 2), like `install`; ids of packets built without these flags are
  unchanged.

**Changed (may affect callers)**

- Error packets say `status: "ERROR"` (was `PARTIAL`); `status` reports
  `overall: "error"` with exit 2.
- MCP `memory_record` accepts only drafts (`approved`/`published` are refused).
- Hook evidence items are nonce-delimited; the 0.2 live comparison used the old
  format. Installed hooks carry `timeout: 30`; retrieval stops at 20 s.
- First-run failures print one line naming the next command; absolute paths and
  script traces need `--verbose`; flag abbreviations are no longer accepted.
- English only: stopwords and route aliases are English, with an optional
  `stopwords` list in `routes.json`.
- Evidence packets can carry a top-level `withheld` list; a search that
  withheld a changed note exits 0 with the status of the remaining evidence
  (`PARTIAL` or `NOT_FOUND`), where it used to exit 1.
- The test guarding against private-vault traces compares SHA-256 digests of
  words and word pairs instead of carrying the strings it guards against.

## 0.2.0

What a person needs after installing 0.1: a connected host, memory that outlives
a session, bounded sub-agent work, visible source health, and a measurement.
Scope, the support matrix and the per-phase definition of done are in SCOPE.md.

- **A1 scope and scaffold.** SCOPE.md: design principles, support matrix,
  acceptance criteria per phase, and the end-to-end walk. Pre-registered the
  phase modules, test files and `docs/` so parallel work stayed disjoint, and
  fixed the memory interface contract the other phases build on.
- **A2 host integration.** `context-layer mcp`: an MCP stdio server
  (`search_vault`, `read_source`, `vault_status`, `memory_record`,
  `memory_resume`) for any JSON-RPC stdio client. `context-layer install` /
  `uninstall` for Claude Code (`.mcp.json`, optional `UserPromptSubmit` hook),
  Codex (marker block, host unverified) and a printed generic snippet — dry run
  by default, backup before every write, this tool's own keys only.
  `context-layer hook claude-code` prepends FTS evidence to a prompt and makes a
  helper failure visible instead of silently empty.
- **A3 shared memory.** Append-only `.context/memory/records.jsonl` with a
  content-addressed id, a `prev` chain, source paths and the SHA-256 they had.
  Re-recording the same content appends nothing. `resume` returns the records in
  force plus open tasks and flags stale sources; `verify` reports chain gaps,
  bad ids and drift. Concurrent appends are locked; `MEMORY.md` mirrors it for
  Obsidian.
- **A4 bounded sub-agent tasks.** `context-layer tasks new/run/list/show/verify/
  cancel/cost`. A task carries a bounded evidence packet, not the vault, with
  allowed sources, one allowed output directory, model, budget, retries and
  timeout. A finished run is `pending_review` until a coordinator's `verify`;
  unauthorized writes are detected by hashing the vault around each attempt and
  cause a rejection. A missing backend yields `blocked`, never `done`. Every
  attempt's tokens, cost and wall time are recorded and summed, with the host's
  own usage addable as a `coordinator` line.
- **A5 source lifecycle.** `context-layer status` hashes every in-scope file and
  reports index age, changed/deleted/added/moved sources, exclusions and
  symlinks, with `ok`/`stale`/`degraded`/`missing` mapped to the exit code.
  Builds now write `index-manifest.json` and keep the replaced index, so
  `context-layer rollback` can restore it. Confirmed that stale evidence is
  withheld rather than delivered with a hash that no longer matches. Added the
  reviewed `hook-visible-error.example.sh` lab candidate; nothing is installed.
- **A6 live measurement.** Held-out before/after of one host with and without
  this layer — source delivery, judged correctness, total tokens over
  coordinator + agents + retries, and wall time. See `eval/LIVE_COMPARE.md`,
  which also records the first measured run (12 held-out cases, one private
  vault, Claude Code + sonnet): the hook arm matched the baseline's judged
  correctness on about 46% of its tokens, while the MCP-only arm matched it
  at a higher token cost and added a source hash to every answer.
- **A7 sharing.** Version 0.2.0. Added THIRD_PARTY.md: every distributed
  component with its origin, licence, notice obligation and where that notice
  lives, including the retained Avenox notice for the optional upstream patches.
  Audited every tracked file for personal paths, accounts and e-mail addresses.
  Extended `scripts/check_distribution.py` from an install smoke test into the
  full release walk in a throwaway environment outside the checkout — status,
  memory, a bounded task on the `fake` backend, host install and uninstall in a
  temporary project with a temporary `HOME`, index rollback, and a
  `--force-reinstall` upgrade after which the vault's `.context` state is still
  readable. Rewrote README.md, QUICKSTART.md, CONTRIBUTING.md and docs/README.md
  around the 0.2 components, with only tested platforms marked as supported.

Unchanged from 0.1.0: FTS remains the default search method, the router remains
experimental, no runtime dependency is added, and no token or speed gain is
claimed anywhere in the package.

## 0.1.0

- Added local init/index/search commands, with FTS as the default search method.
- Preserved the configurable router as an experimental option.
- Added frozen source/passage contracts, common-budget comparisons and raw packet capture.
- Withheld evidence on stale sources or broken indexes; preserved byte-exact UTF-8/CRLF data.
- Applied literal exclusions before reads and rejected symlink/path escapes.
- Bundled CLI runtime modules and fixtures in the wheel; added installed-package smoke checks.
- Pinned optional upstream patches and added strict source-hash checking with rollback metadata.
- Recorded synthetic comparison limits; independent semantic promotion remains outside this runner.
