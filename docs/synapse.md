# Synaptic retrieval (experimental)

"Synaptic" is the name of a retrieval method, `search --method
synaptic`, that uses the vault's explicit links. It is opt-in for `search` and
MCP and the default of the prompt hook (since 0.5, with focused delivery). It has two modes.

- **Default mode: the fts packet plus graph extras.** The packet starts with
  exactly the packet `--method fts` returns for the same flags: the same
  passages, in the same order, with the same bounds, built by the same
  function. After that come passages the link graph reaches, within a separate
  budget of `--extra-tokens` estimated tokens (default 600). They are
  deduplicated against the fts passages.
  - Extras are added only when there is a reason: a link near the question's
    words, a note the prompt names, or text past the fts prefix that holds
    query terms the packet does not cover yet. Otherwise the packet's evidence
    *is* the fts evidence.
  - So completeness can never be lower than fts, and the cost is the fts
    packet plus at most the extra budget. Both hold by construction, and a
    property test checks them over many generated queries on three vaults.
- **Compact mode (`--compact`).** The earlier packer: passages selected from
  seed and linked notes within one `--budget-tokens` budget (default 1200).
  It usually sends far fewer tokens than fts, but it **can drop evidence the
  fts packet would have carried**. The sealed benchmark measured exactly that
  on single-note questions. Use it when tokens matter more than
  never-worse-than-fts.

Other things to know:
- **Status.** It is **experimental and opt-in**. FTS stays the default
  everywhere (CLI, MCP, hook), and only the root maintainers change defaults,
  based on the independent benchmark.
- **Claims.** Nothing here claims a quality or token gain on real questions.
  The numbers below are cost measurements, plus results on a development set
  written together with this code.
- **Prior art.** Obsidian's own graph view and local graph already draw the
  same explicit-link graph. What this layer adds is the retrieval over it and
  the record of which notes one retrieval activated.

Default mode:

```
 prompt ──► --method fts, unchanged ──► fts packet (seeds = its notes)
                                              │
      notes named in the prompt ──┐           ▼
                                  ├──► spread along fresh edges (depth 1 default, 2 max)
     seed ──"Lead: [[Ada]]"──► people/Ada.md    each edge pinned to its source line + SHA-256
         links to (wikilink, line 5)
                                              │
   a link whose line/paragraph holds the question's words, a named note, or
   seed text past the fts prefix with query terms the packet lacks?
          no ──► packet evidence == fts evidence
          yes ─► + extras within --extra-tokens, never repeating fts bytes:
                   the linking line, the linked note (whole if short, else targeted
                   section / link paragraph / lead), named notes, then windows that
                   add query terms the packet does not cover yet
                                              │
            evidence-delivery-v1 packet  +  .context/activation.json (trace)
```

## 1. The link graph (`context-layer index`)

`context-layer index <vault>` builds the lexical index as before
(`router/build_index.py`). It then extracts links from every Markdown note
that index covers into `<vault>/.context/graph.sqlite`. `--no-graph` skips the
link extraction.

| Written as | Edge kind |
| --- | --- |
| `[[Note]]`, `[[Note\|alias]]`, `[[Note#Heading]]`, `[[Note#^block]]` | `wikilink` |
| `![[Note]]`, `![alt](Note.md)` | `embed` |
| `[text](relative/Note.md)` or `[text](relative/Note)`, `%20` escapes, `#fragment`, `<...>` | `mdlink` |
| frontmatter `related`, `up`, `parent`, `see_also`/`see-also`, `supersedes` (string or list, wikilink or plain name) | `frontmatter` |

- **Code is not linked.** Links inside fenced code blocks (backtick or tilde,
  also a fence inside a blockquote or callout), inside indented code blocks
  and inside inline code spans do not count. A link in a blockquote or
  callout outside code does.
- **Comments are not special.** A link inside a `%%…%%` or `<!-- … -->`
  comment still counts (see "Obsidian's rules" below).
- **Line numbers** count `\n` only (a `\r` before it is dropped), as editors
  and `grep -n` do: U+2028, a form feed or NEL inside a line does not start a
  new line. Packet line spans use the same count.
- **Byte-order mark.** One leading U+FEFF is ignored when the frontmatter is
  detected; byte and character offsets still count it.

**Resolution.** Every link is classified against the files of the vault. The
file walk skips excluded, dot and symlinked folders before listing them, as
the index builder does. Paths and targets are compared NFC-normalised and
case-insensitively, so a link typed in one Unicode form finds a file named in
the other; the graph stores the on-disk name. The order is:
1. the exact vault path (a leading `/` means the vault root);
2. the path relative to the linking note (a Markdown link tries it first, a
   wikilink with a folder after the vault path);
3. a unique path suffix (`folder/Note` finds `a/b/folder/Note.md`);
4. a unique file name, case-insensitive.

`.md` is optional in both link forms. A link that finds exactly one note makes
an edge. Any other link makes **no edge**; it is recorded with one reason and
counted, never guessed:

| Reason | Meaning | Stop counted in a packet |
| --- | --- | --- |
| `ambiguous` | two or more notes match; the candidates are stored | `ambiguous_target` |
| `missing` | no file matches | `target_unavailable` |
| `not_indexed` | a Markdown file matches that the index skipped (empty, over the size limit) | `target_unavailable` |
| `attachment` | a file that is not a note matches (`[[table.csv]]`, `![[plan.png]]`) | `attachment_target` |
| `excluded` | the target path lies under an exclusion; its path is never stored | `scope_excluded` |

A link that names only a file name inside an excluded folder (`[[secret]]` for
`private/secret.md`) counts as `missing`: excluded folders are never listed,
so the builder cannot know the file is there.

**Obsidian's rules** (checked on 2026-09-28; the pages below are the Obsidian
Help pages, `help.obsidian.md` now redirects to `obsidian.md/help`):
- **Aliases are not link destinations.** "Rather than just using the alias as
  the link destination (`[[AI]]`), Obsidian uses the `[[Artificial
  Intelligence|AI]]` link format"
  (<https://help.obsidian.md/aliases>). So a bare `[[alias]]` is `missing`
  here, as it is an unresolved link there; `graph health` names the note that
  carries the alias. Aliases still make a note a named seed (§3).
- **`.md` is optional.** "`[Three laws of motion](Three%20laws%20of%20motion)`
  or `[Three laws of motion](Three%20laws%20of%20motion.md)` … The examples
  above are equivalent" (<https://obsidian.md/help/links>).
- **Other files need their extension.** "links to file formats other than
  Markdown needs to include a file extension, such as `[[Figure 1.png]]`"
  (same page).
- **Code.** Obsidian documents fenced blocks and indented ones: "You can also
  create a code block by indenting the text using `Tab` or 4 blank spaces"
  (<https://obsidian.md/help/syntax>). The links Obsidian itself recorded for
  its published help vault (the metadata cache Obsidian Publish serves for
  <https://obsidian.md/help>: 176 notes, read on 2026-09-28, an external check
  that the tests do not repeat) agree with the rule above: 1,725 plain and 145
  blockquote or callout wikilinks are links; 20 in fenced code, 2 in a fence
  inside a callout and 54 in inline code are not. That vault has no link in
  an indented code block or in a comment, so those two cases rest on the
  documentation alone.
- **Comments.** Obsidian documents `%%…%%` ("Comments are only visible in
  Editing view", <https://obsidian.md/help/syntax>) and `<!-- … -->`
  (<https://obsidian.md/help/html>) as hidden comments, but not whether a
  link inside one counts as a link. That is unverified, so links inside
  comments still count here.

**Frontmatter.** A deliberately small YAML subset: `key: value`,
`key: [a, b]`, and `key:` followed by `- item` lines, with plain scalars (a
` #` comment is dropped), single-quoted scalars (`''` is a quote) and
double-quoted scalars (YAML backslash escapes). Anything richer is left out
rather than guessed, and counted per note: block scalars (`|`, `>`), values
that go on over more-indented lines (a flow list split over lines too),
nested lists and mappings, anchors, aliases, tags, and unknown escapes. An
unquoted `[[Note]]` value is read as the link it looks like, although Obsidian
asks for quotes ("Internal links in text properties must be surrounded with
quotes", <https://obsidian.md/help/properties>). Aliases come from `aliases`
(or the deprecated `alias`).

- **Excluded paths.** Excluded paths are never nodes.
- **What an edge stores.** Kind, source, target, 1-based line, the source
  SHA-256 at extraction, and any heading, block id or frontmatter key.
- **Skipped notes.** A note the builder cannot use (changed, deleted or
  unreadable since the index was built, a symlink, not UTF-8) is counted,
  printed on stderr with its reason, and recorded in the table `skipped`. It
  stays a link target. Its own links are left out and its recorded SHA-256 is
  empty, so it is never fresh and shows under `stale_sources` until the next
  `context-layer index`.
- **Atomic rebuild.** The graph is written to a staging file and moved into
  place with `os.replace`.
- **Reuse.** A note whose bytes did not change reuses its stored parse (table
  `parse_cache`). When only note contents changed (the same notes, exclusions and
  file list), only the changed notes' links are resolved again, in a copy that
  replaces the graph atomically; any other change rebuilds it. When none of the
  graph's inputs changed, `graph.sqlite` is not rewritten
  ([source-lifecycle.md](source-lifecycle.md#incremental-update-the-default-and---full)).
- **Damaged graph.** When `graph.sqlite` exists but cannot be read (a missing
  table, a damaged file, a newer layout), synaptic retrieval goes on without
  it: default mode delivers exactly the fts packet and compact mode the
  lexical packet, with `synapse.decision: "graph_unreadable"` and a note
  naming `context-layer index <vault>`. No raw SQLite message is shown.
- **Health.** `context-layer graph health <vault> [--json]` is read-only. It
  reports orphans, broken links by reason (a `missing` link that matches an
  alias names that note), ambiguous links with their candidates, links into
  excluded paths (by source line only), notes changed since the build, notes
  the builder skipped, frontmatter blocks parsed or partly left out, and
  `supersedes` cycles. Excluded notes are never listed.

**Staleness.** A note counts as fresh when its size and mtime still equal the
values recorded at build time. Otherwise its SHA-256 is compared with the
recorded one.
- Links **from** a changed note are not used until the next `context-layer
  index`.
- Passages whose bytes no longer match the lexical index are withheld.
- The packet names both, under `synapse.graph.stale_sources` and
  `withheld_passages_from`.
- The trace never lists these notes.

## 2. Default mode in detail

1. **The fts part.** `eval/retrieve.py` builds the fts evidence with the
   function `--method fts` uses (`lexical_evidence`), with the same `--top-k`,
   `--budget` and `--per-source`. Each item is then annotated with extra
   fields (`origin: "fts"`, `hop: 0`, `start`/`end`, line span,
   `source_chars`, `truncated`, `est_tokens`). Its `source_path`,
   `source_sha256` and `content` are never changed. If fts fails, for example
   on a broken index, synaptic fails the same way; an fts hit that changed
   since indexing is withheld the same way too (the packet's top-level
   `withheld` list, see [cli.md](cli.md#withheld-sources)) and is not a seed.
2. **Seeds.** The notes of the fts passages, plus up to 3 notes whose basename
   or alias occurs in the prompt (`named_seeds`).
3. **Spread.** The spreading activation below, over fresh edges only.
4. **Reason to add linked notes** (`synapse.decision`):
   - `relevant_links`: some linked note's connecting link has link relevance
     above 0. Notes reach the reserved set when their link relevance is at
     least 60% of the best one's (up to 4 notes).
   - `named_notes`: the prompt names a note the fts packet does not hold.
   - `no_relevant_link`: no linked note is reserved; the windows of step 5
     may still be added.
   - `node_cap_hit_lexical_fallback`: the first hop would take activation
     past `NODE_CAP` notes, so no linked note is used (step 5 still looks at
     the seeds). When a later hop would pass the cap, only that hop is left
     out and `stops.node_cap` counts the notes it would have added.
   - `graph_missing`, `graph_unreadable`, `no_seeds`: nothing is added.
5. **Extras**, within `--extra-tokens` and at most 3 per source, in this
   order:
   - the linking line and the reserved linked notes (whole when short,
     otherwise targeted section, link paragraph, lead);
   - named notes;
   - windows in activated notes that hold query terms the packet does not
     cover yet, a seed's text beyond its fts windows included. This step runs
     whenever there are seeds and a readable graph, with or without links, so
     a vault without links still gets such a passage.

   Extras never overlap the fts spans (a window straddling the end of an fts
   span is trimmed to start after it; one inside a span is dropped), and never
   repeat fts text.
6. **Order.** The fts items, then the extras (hop, score, path).

## 3. Shared machinery, and compact mode (`--compact`)

1. **Seeds.** The top `--top-k` notes (default 3) from the same FTS5 bm25
   query as `--method fts`. The FTS order breaks ties by path. Up to 3 notes
   whose basename or frontmatter alias occurs as a phrase in the prompt are
   added. FTS does not index names, so a query by note name would otherwise
   find nothing. A seed with no matching body block, because it was named or
   matched only in frontmatter, is represented by its lead, or by its whole
   body if that is short.
2. **Link relevance.** For each link: the share of query terms on the link's
   line, link text excluded, or half of that share when the terms appear only
   in its paragraph and enclosing section headings.
3. **Decision in compact mode** (`synapse.decision`):
   - `seed_link_matches_query`: a seed line that holds a link also holds
     query words. The answer may be behind that link, so expand.
   - `seeds_cover_query`: every query term is covered by the seed passages and
     the top seed dominates (bm25 ≥ 1.5x the next), or the seeds do not link
     to each other. No expansion.
   - `coverage_incomplete` / `seeds_linked`: expand.
   - `graph_missing`, `graph_unreadable`, `no_seeds`: the lexical packet.
   - `node_cap_hit_lexical_fallback`: the first hop would touch more than
     `NODE_CAP` notes, so the lexical packet is returned **unchanged**. A
     second hop that would pass the cap is left out and the first is kept.
4. **Spreading activation.** Seeds are always query hits, so activation stays
   conditioned on the query.

   ```
   contribution(u→v) = activation(u) x 0.5 x kind weight x (1 + 2 x link relevance)
                       / degree(u) / sqrt(degree(v))
   ```

   - `1/degree(u)` is the out-degree normalisation. Degree counts distinct
     linked notes.
   - `1/sqrt(degree(v))` keeps a hub target from collecting activation.
   - A note with more than `ADJACENCY_CAP` neighbours is never expanded from
     and is listed in `hubs_not_expanded`.
   - Repeated links between two notes count once: the strongest one, never a
     sum.
   - Links are followed in both directions. Against the link direction the
     kind is `backlink` ("linked from"), weighted x0.8.
   - Contributions below 0.005 are dropped and counted
     (`stops.below_threshold`). Neighbours are visited in path order.
   - Exclusions are checked before any read, and the index hash before any
     text is used, on every hop.
5. **Compact passages and packing.** Every limit holds at every step: `--budget-tokens`
   (default 1200), `--budget` and `--per-source` characters, 3 passages per
   source, no overlapping spans. The steps run in this order:
   1. The best lexical seed passage goes first.
   2. **Reserved linked notes.** Up to 4 hop notes whose link relevance is at
      least 60% of the best get a guaranteed share (at most 60% of the budget,
      200 estimated tokens each):
      - the linking line itself (the "synapse");
      - the linked note's whole body when it fits;
      - otherwise its targeted section (`[[note#heading]]`, `[[note#^id]]`),
        the paragraph holding the link, its lead paragraph, then paragraphs
        sharing words with the question or the link line.

      This is what reaches an answer that shares **no** word with the question.
   3. Everything else, greedy by utility per estimated token. Utility is the
      score plus a bonus for query terms no packed passage covers yet.
      Passages below 20% of the best score are skipped, or below 60% once
      every term is covered.
   4. Windows of one file separated only by whitespace are merged when the
      merged window still fits.
   5. Byte-identical passage text is delivered once, with the other copies
      listed under `duplicates`. Nothing is merged by similarity.

**Token estimate.** `est_tokens = ceil(characters / 4)`. It is named in every
packet (`est_tokens_estimator`, `est_tokens_scope`). It counts evidence text
only: not the JSON framing, and not the hook wrapper. It is not a model
tokenizer count.

### Packet fields

The shape stays `evidence-delivery-v1`. Every item keeps `source_path`,
`source_sha256` and verbatim `content`, and synaptic adds:

| Field | Meaning |
| --- | --- |
| `origin` | default mode: `fts` for the unchanged fts items, `graph` for extras |
| `start`, `end` | byte offsets into the hashed file: `raw[start:end]` decodes to `content` |
| `line_start`, `line_end` | 1-based line span |
| `source_chars`, `truncated` | the whole file's length; whether `content` is less than the whole file |
| `hop`, `activation` | 0 for a seed; activation in [0, 1] (a ranking signal, not a confidence) |
| `via` | edge chain that reached the note: `from`, `to`, `kind`, `label` ("links to" / "linked from"), `edge` (e.g. `wikilink`, `frontmatter:supersedes`), `weight`, `anchor` {path, line}, `text` |
| `reason` | why this passage: query terms, linked note (whole), link target section, holds the link, lead paragraph, the link line, note named in the prompt, ... |
| `anchors`, `duplicates` | traversed links inside the passage; byte-identical copies elsewhere |
| `est_tokens`, `score` | as above |

The top-level `synapse` object has these fields:
- `mode` (`superset` for default mode, `compact`), `experimental`, and
  `est_tokens` with its estimator fields;
- default mode: `extra_tokens`, `fts_passages`, `fts_est_tokens`,
  `extra_est_tokens`, `named_seeds`;
- compact mode: `budget_tokens`;
- `expanded`, `decision`, `max_hops`, `seeds`, `activated_notes`,
  `hubs_not_expanded`;
- `stops`, with counts for: `hop_cap` (links one hop past the limit),
  `budget_limited` (passages that did not fit), `ambiguous_target`,
  `scope_excluded` (links to paths outside the allowed set, including links
  the graph recorded as `excluded`), `target_unavailable` (missing and
  not-indexed targets, and withheld sources), `attachment_target` (links to
  files that are not notes), `below_threshold` (contributions under 0.005
  that were dropped) and `node_cap` (notes a hop past `NODE_CAP` would have
  added);
- `graph` (`present`, `readable`, `built_at`, `edges`, `unresolved_links`,
  `stale_sources`, `withheld_passages_from`), `trace`, and `notes` when there
  is something to fix.

## 4. Activation trace

After every synaptic retrieval (CLI, MCP `search_vault` or the hook),
`<vault>/.context/activation.json` is written through a uniquely named
`tempfile.mkstemp` file in the same directory plus `os.replace`, so concurrent
writers never collide. The payload has these fields:
- `version` (1), `generated_at`, `run_id` (random, 128 bits);
- `query`: `null` unless the user opted in;
- `method` (`synaptic`), `mode` (`superset` for default mode, `compact`),
  `max_hops` (1 or 2), `budget_tokens`, `nodes` (≤ 200), `edges` (≤ 400),
  `packet`.
  - `budget_tokens` is the extra budget in default mode and the packet budget
    in compact mode.
  - A node has `path`, `activation`, `hop`, `role` and `selected`. In default
    mode, the notes of the fts passages (and named notes) are the `seed`
    nodes, `hop` 0. Link-reached notes are `hop` nodes, `selected` when an
    extra came from them.
  - An edge has `from`, `to`, `kind` (`wikilink`, `embed`, `mdlink`,
    `frontmatter`, or `backlink` when it was followed against the link
    direction), `hop`, `weight` and `anchor` {path, line}. `hop` is the hop
    at which the spread followed the edge: an edge found on hop 2 is hop 2
    even when it ends at a seed or at a hop-1 note.
  - `packet` has `passages`, `est_tokens` and `status`. `status` is `PARTIAL`
    when the packet holds evidence and `NOT_FOUND` when it holds none. There
    is no `OK`: a packet never states that the question was answered
    ([design-rationale §1](design-rationale.md)). A retrieval that fails
    (`ERROR`) writes no trace.
  - Paths are NFC-normalised, the form Obsidian lists. The packet keeps the
    on-disk names, which reads and hashes use.

**Privacy.**
- No hash of the query is written: an unsalted hash of a short prompt can be
  recovered by guessing.
- The query text appears only with `--record-query` or
  `"record_query_text": true` in `.context/routes.json`.
- `"write_activation": false` turns the file off.
- Excluded, stale and withheld notes are never listed.
- The file is never read back into ranking, and it is never indexed:
  `.context/` is skipped. The Obsidian plugin reads it only to display it.
- It records which notes a retrieval activated and selected. It does not
  record the model's reasoning, and a lit-up graph does not mean the
  retrieval was good.

## 5. Hosts

- **CLI:** `context-layer search <vault> --prompt "..." --method synaptic
  [--extra-tokens N] [--max-hops 1|2] [--record-query]`, plus
  `[--compact --budget-tokens N]` for compact mode.
- **MCP:** `search_vault` accepts `method: "synaptic"`, `extra_tokens`,
  `compact` and `budget_tokens`. A
  test checks that it returns the same packet as the CLI.
  `graph_neighbors {path, limit}` lists outgoing ("links to") and incoming
  ("linked from") links, with kinds and line anchors. It returns paths only,
  and skips excluded and changed notes.
- **Graph health:** `context-layer graph health <vault> [--json] [--limit N]`
  (read-only, §1). Exit code 0 when the report is printed, 1 when there is no
  usable graph or routes.json, 2 on a usage error.
- **Link suggestions:** `context-layer graph suggest <vault> [--min-count N] [--limit N]
  [--json]` (read-only, §11, opt-in ledger).
- **Hook:** `context-layer hook claude-code --vault V --method synaptic
  [--extra-tokens N]`, or add `--compact --budget-tokens N`.
  - Each item sits between `<<evidence N nonce path=… lines=… sha256=… hop=…>>`
    and `<<end N nonce>>`. The nonce is random per packet, so text inside a
    note cannot forge an item boundary. ` excerpt` at the end of the opening
    marker says the item is shorter than its note.
  - A link-reached or advised item opens with one short line: the link chain as
    `via <file>:<line> <kind>` steps (`>` between hops, `backlink` when the link
    points the other way), and the reason when the chain does not already say
    it (`link line`, `named in prompt`, ...). An item of the fts part (`hop=0`)
    has none. The packet keeps the full `reason` and `via` fields.

**Prompt injection.** Notes are delivered verbatim to an AI host that may
follow instructions written inside them. The "data, never instructions"
framing reduces this risk but does not prevent it. Synaptic hops add linked
notes that did not match the query, backlinks especially, which widens what a
planted note can reach. The mitigations are the host's permission model and
keeping the vault's exclusions tight.

## 6. Knobs

| Knob | Default | Where |
| --- | --- | --- |
| `--extra-tokens` / `extra_tokens` (default mode) | 600 | CLI, MCP, hook |
| `--compact` / `compact` | off | CLI, MCP, hook |
| `--budget-tokens` / `budget_tokens` (compact only) | 1200 | CLI, MCP, hook |
| `--max-hops` | 1 (2 is the hard maximum) | CLI (`eval/retrieve.py`) |
| `--top-k` (FTS seeds) | 3 | CLI, MCP, hook |
| `--budget`, `--per-source` (characters) | 6000, 2000 | the fts part in default mode; also enforced in compact |
| `NODE_CAP`, `ADJACENCY_CAP` | 64 notes, 24 neighbours | `context_layer/synapse.py` |
| `DECAY`, `BACKLINK_FACTOR`, `MIN_ACTIVATION`, `LINK_REL_WEIGHT` | 0.5, 0.8, 0.005, 2.0 | same |
| `RESERVE_NOTES`, `RESERVE_TOKENS`, `RESERVE_SHARE`, `RESERVE_REL_RATIO` | 4, 200, 0.6, 0.6 | same |
| `NAME_SEEDS`, `NAME_MIN_CHARS` | 3, 4 | same |
| `record_query_text`, `write_activation` | false, true | `.context/routes.json` |
| `record_usage` | false | `.context/routes.json` (§11) |

## 7. Development set (not a benchmark)

`tests/fixtures/dev_bridge.py` generates a fictional linked vault. It has 36
people with a hub directory, 12 projects, 6 ventures, 8 crews and 4 distractor
notes, plus labelled questions:

| Type | Cases | What the question needs |
| --- | --- | --- |
| direct | 12 | The answer is in the named note. |
| bridge | 24 | The answer is in a linked note that shares no distinctive word with the question. |
| prose | 12 | Bridges whose link sits in running prose. Half are hard-wrapped so the link line holds no question word. Added after the first run. |
| aggregate | 8 | The answer is spread over three linked notes. |

A case counts as complete when every required span is delivered verbatim from
its source. Run it with `python3 tests/dev_bridge_eval.py`.

| Method / build | direct | bridge | prose | aggregate | mean est. tokens |
| --- | --- | --- | --- | --- | --- |
| fts | 12/12 (~194) | 0/24 (~154) | 0/12 (~119) | 0/8 (~120) | ~150 |
| synaptic default mode (fts + extras) | 12/12 (~194) | 24/24 (~310) | 12/12 (~211) | 8/8 (~436) | ~282 |
| synaptic `--compact` | 12/12 (~104) | 24/24 (~214) | 12/12 (~166) | 8/8 (~391) | ~206 |
| synaptic before the bridge work (`9f522a7`, compact-style) | 12/12 | 0/24 | n/a | 0/8 | ~78 |

In default mode the direct questions cost exactly what fts costs: no link near
their words, so nothing is added. The bridge, prose and aggregate questions
cost the fts packet plus the extras. Re-run on 2026-09-28 after that day's
link-graph changes (§1, and the windows past the fts prefix in §2): every case
gave the same completeness and token count as before.

- **Ablations** (compact mode, same set):
  - without the reserved share, bridge, prose and aggregate all fall to 0;
  - with paragraph-level link relevance off, prose falls to 6/12;
  - with the per-note reservation cut to 40 tokens, so that no note fits
    whole, bridge falls to 0/24 and aggregate to 0/8;
  - the `LINK_REL_WEIGHT` activation boost changed nothing measurable here.
- **Caveat.** This set was written by the same author as the code and shares
  its assumptions. For example, linked notes are short enough to be delivered
  whole. Only the independent benchmark says whether any of this carries over.

## 8. Cost (measured on 2026-09-28)

Measured on 2026-09-28 on one development laptop (arm64, 8 cores, macOS,
Python 3.12.4, SQLite 3.45.3) as wall-clock times: 9 runs of each step,
interleaved with the same runs of the previous commit (`ebb88ba`). Other work
kept the load average between 8 and 13 during the runs, so the times are high
and spread widely. Read them as upper bounds; the previous commit measured
within the same noise.

| Vault | Notes | Edges | `graph.sqlite` | `index.sqlite` |
| --- | --- | --- | --- | --- |
| demo (`eval/fixtures/docs`) | 7 | 0 | 57,344 bytes | 45,056 bytes |
| synthetic, generated (below) | 2,000 | 9,996 | 2,183,168 bytes | 2,043,904 bytes |

| Step, synthetic vault | Median | Fastest |
| --- | --- | --- |
| `graph.build` (in-process, no start-up) | 0.80 s | 0.63 s |
| `context-layer index` | 2.10 s | 1.26 s |
| `context-layer index --no-graph` | 0.98 s | 0.70 s |
| `eval/retrieve.py --method fts` | 0.53 s | 0.39 s |
| `--method synaptic` (default mode) | 0.56 s | 0.44 s |
| `--method synaptic --max-hops 2` | 0.62 s | 0.46 s |
| `--method synaptic --compact` | 0.63 s | 0.42 s |
| `--method synaptic --compact --max-hops 2` | 0.56 s | 0.41 s |

- The command rows include Python start-up; the query prompt was
  `harbor lantern keel`. On the demo vault `graph.build` took 0.033 s
  (median) and each query about 0.1 s.
- **Depth 2.** It adds nothing in that dense vault: with `--max-hops 2` the
  default mode activates the same 30 notes as with one hop, and
  `stops.below_threshold` counts 285 contributions two hops out that fell
  under `MIN_ACTIVATION`.
- **Size.** The graph file holds the `skipped` and `frontmatter` tables and
  the link targets `graph health` reports: 20,480 bytes more than the previous
  commit's on the synthetic vault, 20,480 bytes more on the nearly empty demo
  graph.
- **Earlier figures.** This section used to give 9,994 edges and 2,170,880
  bytes. The previous commit gives 9,996 edges and 2,162,688 bytes with the
  same generator (audit A measured the same), so those figures came from an
  earlier revision.

The synthetic vault has 2,000 notes in 20 folders. Each note has a 60-word
paragraph, three wikilinks, one relative Markdown link, one embed and one
fenced `[[not-a-link]]`:

```python
import random; from pathlib import Path
root, n = Path("synthetic-vault"), 2000; random.seed(7)
words = "anchor beacon canal delta ember fjord gable harbor inlet jetty keel lantern".split()
for i in range(n):
    folder = root / f"area{i % 20}"; folder.mkdir(parents=True, exist_ok=True)
    links = random.sample(range(n), 5)
    body = [f"# Note {i}", "", " ".join(random.choice(words) for _ in range(60)) + ".", ""]
    body += [f"Related: [[note-{j}]]." for j in links[:3]]
    body += ["", f"[more](../area{links[3] % 20}/note-{links[3]}.md)", f"![[note-{links[4]}]]",
             "", "```", "[[not-a-link]]", "```", ""]
    (folder / f"note-{i}.md").write_text("\n".join(body), encoding="utf-8")
```

Then run `context-layer init synthetic-vault && context-layer index synthetic-vault`.

## 9. What is and is not claimed

- **Tested** (`tests/test_synapse.py`):
  - **the superset property**: for every generated query (the dev questions
    plus random word and note-name queries, on the dev vault,
    `eval/fixtures/docs` and `router/example-vault`, under three flag sets),
    the fts evidence is the first part of the default-mode synaptic packet,
    unchanged and in order. The extras stay within `--extra-tokens`, never
    repeat fts bytes, and are byte-exact. Where there is no relevant link the
    packet equals fts, and `--extra-tokens 0` is the fts packet;
  - link parsing and resolution: a BOM + CRLF note keeps its frontmatter,
    NFC and NFD names meet, line numbers count `\n` only, the frontmatter
    subset leaves out what it does not cover, the code and comment rules,
    Markdown link forms, and the five link classes landing in their own
    counters;
  - notes the builder skips are counted, printed and never fresh;
  - a damaged graph (missing table, garbage file, newer layout) gives
    `graph_unreadable` and exactly the fts packet, never raw SQLite text;
  - a vault without links gets the window past the fts prefix;
  - stale sources withheld and reported;
  - hop, node and hub caps with lexical fallback, a node cap on hop 2 keeping
    hop 1, and `below_threshold` counts;
  - `graph health`: orphans, broken, ambiguous and excluded links, stale
    notes, frontmatter, `supersedes` cycles, and exclusions added later;
  - the budget is never exceeded;
  - no expansion when the seeds cover the query;
  - bridge answers with no shared word are delivered;
  - frontmatter `via` keys;
  - name and alias seeds;
  - byte-exact windows past 2,000 characters;
  - adjacent merge and exact-duplicate listing;
  - identical output under three `PYTHONHASHSEED` values;
  - an excluded archive cycle changes nothing;
  - stop reasons;
  - the trace contract (run_id, no hash, opt-in text, `mode`, `max_hops`,
    the hop of each edge, NFC paths, status `PARTIAL`/`NOT_FOUND`, concurrent
    writers, a planted file ignored, never indexed, opt-out);
  - MCP/CLI parity;
  - unforgeable hook markers.
- **Guaranteed, and only in default mode:** completeness is never below fts
  for the same flags, because the fts evidence is included unchanged. The cost
  is at most the fts packet plus `--extra-tokens` estimated tokens of
  evidence text. Compact mode gives no such guarantee.
- **Not claimed:**
  - that synaptic retrieval finds better evidence than FTS on real questions;
  - that it saves tokens. Default mode never sends fewer tokens than fts.
    Compact mode usually sends fewer, and can miss evidence fts would have
    delivered.

## 10. Limitations

- **The default mode inherits fts delivery.** The fts part is exactly what
  `--method fts` delivers: a note whole when it fits `--per-source`, else its
  match-anchored windows (`--delivery prefix` restores the 0.3 whole-file
  prefixes; then a match past the prefix reaches the packet only as an extra
  window, within `--extra-tokens`, while the graph is readable). Compact mode
  delivers its own match-anchored passages.
- **Frontmatter links need the question's words.** A frontmatter link is
  relevant only when its key line holds query words, because there is no
  paragraph around it. A `supersedes:` link does not fire for a question
  that does not use that word.
- **Explicit links only.** There are no semantic, embedding, tag or folder
  edges. A vault without links gets the fts packet plus windows in the seed
  notes that hold query terms the fts part does not cover yet.
- **Long linked notes.** A linked note too long to deliver whole is
  represented by its targeted section, link paragraph and lead. An answer
  elsewhere in it, sharing no word with the question, is missed (see the
  ablation).
- **Link relevance is lexical.** It is exact-token matching with no stemming
  and no synonyms. A link in a paragraph that shares no word with the question
  gets no reserved share.
- **Token estimate.** `est_tokens` is `ceil(chars / 4)`. Code, CJK text and
  URLs can differ a lot from it.
- **Frontmatter parsing.** The parser handles a small YAML subset (§1).
  Anything richer is left out rather than guessed, and `graph health` counts
  it. Obsidian documents links in any text or list property
  (<https://obsidian.md/help/properties>); here only the relation keys of §1
  make edges.
- **Ambiguous basenames.** These stay unresolved, and `graph health` lists
  their candidates. Obsidian may resolve some of them by a tie-break its
  documentation does not state. Link with a path to disambiguate.
- **Comments.** Links inside `%%…%%` and `<!-- … -->` count, because whether
  Obsidian counts them is not documented.
- **Excluded folders are not listed.** A link that gives only a file name
  inside an excluded folder counts as `missing`, not `excluded`; a link with
  the folder path counts as `excluded`.
- **Freshness shortcut.** Freshness uses size and mtime, then SHA-256. An edit
  that keeps both size and mtime is still caught when passage bytes are hashed
  for delivery, but its links are used.
- **Hand-set constants.** The constants were set by hand on small synthetic
  vaults and the development set. They were not tuned against any benchmark.

## 11. Link suggestions from usage (opt-in)

`context-layer graph suggest <vault>` lists pairs of notes that retrievals
delivered together but that no explicit link joins, so that a person can decide
to write a link. It is a report for a human; it changes nothing.

**Off by default.** Nothing is written until `"record_usage": true` is set in
`.context/routes.json` (exactly `true`; any other value, or a `routes.json`
that cannot be read, records nothing). With it on, every `fts` or `synaptic`
retrieval that delivers two or more notes appends one line to
`.context/usage-ledger.jsonl`, whichever host asked (CLI, MCP, hook):

```
{"v":1,"at":"2026-03-01T10:00:00Z","m":"fts","s":"3f2a9c1b7d4e","p":["alpha.md","gamma.md"]}
```

- **What a line holds.** The format version `v`, the UTC time, the method,
  a 12-hex SHA-256 prefix of the host session id (`null` when the host gave
  none: the CLI has none), and the sorted vault-relative paths of the delivered
  notes (at most 16). No note text, no prompt, no hash or count of the prompt,
  and no note under an exclusion. A retrieval that delivered fewer than two
  notes holds no pair and writes nothing.
- **Bounded.** At 1 MiB the file rotates to `usage-ledger.jsonl.1`, replacing
  the older rotation, so at most two files (2 MiB) exist. Appends are single
  `O_APPEND` writes under an advisory lock; the files are mode `0600`.
- **Deleting it.** Delete `.context/usage-ledger.jsonl*` (and the empty
  `.usage-ledger.lock`), or set `record_usage` back to `false`.
- **Never read by retrieval.** Ranking, packing, the trace and the
  host tools do not open the ledger; only `graph suggest` does. The retriever
  writes it after the packet is printed, and a write failure is ignored.
  Tests check that packets are byte-identical with the ledger absent, present
  or 6 MiB of lines, in both fts and synaptic, that a default-off vault gets no
  ledger file, and that no other module names the ledger
  (`tests/test_coactivation.py`). A link reaches retrieval only after a person
  writes it and `index` runs, through the explicit graph (§1).

**The report.** It counts the notes each ledger line delivered together and
skips pairs with a link in either direction (any link kind), pairs a current
exclusion hides, and notes the graph no longer knows. Pairs seen at least
`--min-count` times (default 2) are ranked by retrievals, then days, then
sessions. Each shows how many retrievals, sessions and distinct days it spans,
the first and last day, and how many times each note was delivered in all.
Sessions are only counted for hosts that give a session id.

**What it does not mean.** This is usage, not relatedness. Two notes can be
delivered together because one prompt was broad, because one retrieval was
repeated, or because one note is a hub delivered with everything; the
per-note totals and the day count are there so that a person can see this
before adding a link. Nothing here measures whether a suggestion is right. No
acceptance rate has been measured, and the ledger is not used to improve
ranking.

**Prior art.** Related-note suggestion from co-citation exists (Small, 1973;
the Obsidian Graph Analysis plugin's co-citation view), and search engines
learn from click logs. This is the same idea kept outside the ranker: usage
counts go to a person, not into scoring, so ranking stays a function of the
files, the configuration and the query.
