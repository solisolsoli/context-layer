# context-router

A prompt-routing and evidence-packaging layer for a Markdown vault (Obsidian or
any folder of `.md` files).

Given a user prompt, it decides which parts of the vault are relevant, opens the
canonical files those routes declare, retrieves matching indexed records, checks
their hashes against the files on disk, applies a categorical evidence gate, and
emits two artifacts:

- `context.md` — a verbatim, evidence-bound **context packet**
- `SUBAGENT_BRIEF.md` — instructions for a retrieval subagent

**It never answers the user's question.** Its whole job is to put the right
verbatim source text, with provenance, in front of whatever does answer.

Python 3.10+, standard library only, no network access.

> **Status: experimental, and a candidate for removal.** The routing and packet
> step (`context_router.py`, reached as `context-layer route`) is experimental and
> may be removed from the package in a later release; nothing is removed in this
> one. The maintained retrieval paths are `context-layer search`, the prompt hook
> and the MCP server, which read the same index and do not go through
> `context_router.py`. `build_index.py` and `source_policy.py` are shared with those paths and
> stay. If you depend on `route`, say so before it goes.

---

## Quick start

```bash
# 1. Index the vault (read-only; writes one SQLite file, changes nothing else)
python3 build_index.py --vault example-vault

# 2. Route a prompt
python3 context_router.py \
    --vault example-vault \
    --prompt "What is the summary line character limit in the house style?" \
    --stdout

# 3. See every flag
python3 context_router.py --help
```

Against the bundled `example-vault/` the first prompt returns status
`SUPPORTED`, routes to `writing-standards`, and inlines `writing-standards.md`,
the labelled superseded `reference-docs/legacy-style-guide.md`, and the
prompt-specific notes — while suppressing the archived mirror copy under
`snapshots/` and excluding `generated/`.

Run the tests:

```bash
python3 build_index.py --vault example-vault
python3 test_context_router.py
```

---

## Files

| File | What it is |
| --- | --- |
| `context_router.py` | The router and packet builder. |
| `build_index.py` | Builds the SQLite/FTS5 lexical index the router reads. |
| `textfold.py` | The folding contract shared by the index, `eval/retrieve.py`, the router and `context-layer init`: verbatim query terms for MATCH, one folded form for comparisons, Markdown headings outside code fences. |
| `routes.example.json` | Example routing config. Copy to `<vault>/.context/routes.json` and edit. |
| `facts.example.json` | Example fast-path answer cards. Optional. |
| `test_context_router.py` | Regression tests against `example-vault/`. |
| `example-vault/` | A tiny invented vault used for the smoke path and the tests. |

By default the router looks for `<vault>/.context/routes.json`,
`<vault>/.context/index.sqlite` and `<vault>/.context/facts.json`, and writes run
artifacts to `<vault>/.context-runs/<date>/<run-id>/`. All four are overridable
(`--config`, `--index`, `--facts`, `--runs-dir`).

---

## Configuring routes

A route is a named group of trigger terms plus the files that must be opened
whenever it fires. Everything vault-specific lives in the config; nothing is
hardcoded in the Python.

```json
"routes": {
  "writing-standards": {
    "priority": 2,
    "triggers": ["house style", "summary line", "style guide"],
    "canonical_sources": [
      { "path": "writing-standards.md", "rule_state": "current" },
      {
        "path": "reference-docs/legacy-style-guide.md",
        "rule_state": "superseded",
        "superseded_by": "writing-standards.md",
        "attachment": "opportunistic"
      }
    ],
    "path_hints": ["notes/"]
  }
}
```

- **`triggers`** — phrases matched against the normalized prompt. A trigger
  beginning with `/` is treated as a command and scores higher. Multi-word
  triggers also become retrieval anchors.
- **`priority`** — multiplies the route's trigger score when routes compete.
  At most `max_routes` routes fire per prompt.
- **`canonical_sources`** — files the route *always* opens, regardless of search
  rank. This is the point of the whole design: an operating rule must not depend
  on winning a BM25 contest. Entries can be a bare path or an object with:
  - `rule_state`: `"current"` or `"superseded"`
  - `superseded_by`: the file that replaced it (required when superseded)
  - `attachment: "opportunistic"` — offer it to the ranker for a tail slot
    instead of mandating it, so history cannot displace a rule in force.
- **`path_hints`** — path prefixes/fragments that get a ranking bonus while this
  route is active.

Other config keys, all optional:

| Key | Effect |
| --- | --- |
| `aliases` | Term → synonyms, expanded into extra queries and recorded as `alias_evidence`. |
| `freshness_terms` + `external_fact_terms` | Both present → status `EXTERNAL_RECHECK`. |
| `vague_memory_terms`, `vague_request_routes`, `ambient_terms` | Detect a deliberately vague request and abstain. |
| `continuation_terms` | A "go on / keep going / shorter" list. A prompt made only of these has no anchor, so the router abstains instead of sweeping the index. |
| `stopwords` | Extra stopwords for the vault's language, added to the built-in English list (never replacing it). Used by the router when it tokenizes a prompt, by `eval/retrieve.py`'s query terms once that reads the config, and by `context-layer init` when it picks trigger terms. Must be a list of strings; anything else is a config error. |
| `exclude_prefixes` | Path prefixes skipped at index time **and** dropped after retrieval — intended for the tool's own generated output, so a measurement file can never be quoted back as vault evidence. A prefix matches whole path components, literally (`%`, `_` and quotes are ordinary characters), but case-insensitively and after Unicode NFC normalisation, so `private` also excludes `Private/` and a decomposed file name; on a case-sensitive file system that errs toward excluding more. An entry with blanks around it or around a component, or with an empty component (`"private/ "`, `"a//b"`), is refused: it would exclude nothing. |
| `max_file_bytes` | The index builder's per-file limit in bytes (1 to 50,000,000; default 2,000,000). A larger file is skipped and listed, never indexed in part. |
| `retrieval_exclude_prefixes` | Extra `LIKE 'prefix%'` exclusions in the SQL. |
| `mirror_prefixes` | Strips a snapshot/mirror directory so an archived revision collapses onto the live file instead of spending a second source slot. |
| `derived_file_names`, `derived_path_fragments`, `derived_name_fragments` | Rendered/derived copies (transcripts, OCR dumps) excluded in favour of the authoritative original. |
| `operational_metadata_fragments` / `..._exceptions` | Generated job/request envelopes excluded, with named exceptions. |
| `boost_path_prefixes`, `demote_path_fragments` | Ranking nudges. |
| `known_integrity_limits` | Records known to be truncated/damaged; matching evidence forces `PARTIAL` and prints the reason. |
| `stem_suffix_tolerance` | Bounded suffix tolerance for inflected words (useful for suffixing languages), with a minimum stem length and a blocklist so an everyday short word cannot open a heavy route. |
| `record_type_allowlist` | Which indexed record types may be retrieved. |
| `canonical_source_floor`, `lexical_reserve`, `max_routes` | Quota tuning (see below). |

### Answer cards (`facts.json`, optional)

A card is one answer, one source file, one verbatim quote:

```json
{ "id": "summary-line-length",
  "answer": "A summary line is 40 to 80 visible characters.",
  "terms": ["summary line", "characters", "length", "limit"],
  "min_matches": 2,
  "evidence_grade": "SUPPORTED",
  "path": "writing-standards.md",
  "quote": "40 to 80 visible characters" }
```

At load time every quote is checked byte-for-byte against its file. A card whose
quote no longer matches is **dropped**, so the fast path cannot answer from a
stale or invented rule. Cards require `min_matches` independent term hits (2 by
default) so an everyday word cannot pull one. Cards are **additive**: they are
rendered as a header on top of the normal packet, never as a replacement for
retrieval. `--no-fast-path` disables them.

---

## How it works

1. **Parse.** Split the prompt into verbatim terms (`textfold.py`), drop the
   English default stopwords plus the config's optional `stopwords`, extract
   exact phrases (quoted strings, slash commands, file names, proper-noun runs),
   expand aliases, score routes. Stopwords, repeats and every in-Python
   comparison use the folded form (NFKC + casefold); FTS5 queries get the terms
   as written, so the index folds prompt and notes the same way.
2. **Abstain or budget.** If the prompt has no route, no anchor and no
   distinctive token — a bare continuation such as `continue` / `keep going` — the
   router abstains and says so, rather than sweeping the index for everyday
   words. Otherwise a **tiered activation budget** is picked from the prompt
   length (`brief` / `standard` / `full`): fewer redundant chunks and smaller
   per-source inlining for a short stimulus, never fewer *distinct* sources.
   `--full` and the `--max-*` flags override it.
3. **Retrieve.** Each query variant runs against FTS5/BM25, then every hit must
   survive a content gate: if the prompt named an identity, the body must
   contain it; if there are strong anchors, one must appear in the body. Derived
   mirrors, operational envelopes and excluded prefixes are dropped.
4. **Open canonical sources.** Independently of rank, the route's canonical
   files are read from disk whole, hashed, and marked mandatory (or
   opportunistic). A whole-file read replaces any lexical chunk of the same file
   so a rule document can never appear twice, once labelled and once bare.
5. **Select.** A **mandatory quota** guarantees canonical rules cannot be
   squeezed out by lexical hits, and a floor keeps that quota from reaching
   zero; a **lexical reserve** keeps room for prompt-specific history so
   operating manuals alone cannot fill a packet; `semantic_hash` deduplicates
   bodies; `mirror_prefixes` collapse archived revisions; superseded documents
   are capped and are dropped entirely unless the rule that replaced them is in
   the same packet (and the drop is reported, not silent).
6. **Materialize.** Verbatim only. Whole file if it fits; otherwise the whole
   Markdown section around the densest anchor (a `#` line inside a code fence
   is not a heading); otherwise a contiguous
   anchor-centred window (not the file head — a rule file's head is front
   matter); otherwise the exact indexed chunk. One complete logical line for
   JSONL. Everything over the budget is written to `overflow/` and listed.
7. **Verify.** Compare the indexed source hash with the same byte snapshot used
   to materialize the passage. Canonical documents need an existing, unique
   indexed version. Missing sources, mismatches and operational index errors
   withhold the packet and exit 1. Emitted passages carry stored/current source
   hashes, indexed/emitted content hashes and `verified`. Verbatim text records
   must also be literal spans of that snapshot; UTF-8 and CRLF bytes are preserved.
8. **Gate.** One categorical status, in this order:

   | Status | Meaning |
   | --- | --- |
   | `EXTERNAL_RECHECK` | The prompt asks for a time-sensitive external fact. Stored notes are dated context; check an authoritative source. |
   | `PARTIAL` | A known evidence limit, canonical-only context or only assistant-side evidence. Some evidence was found. |
   | `NOT_FOUND` | No anchor, or nothing prompt-specific retrieved. The router abstains. |
   | `USER_STATED` | A direct user statement plus its verified indexed source. |
   | `SUPPORTED` | Relevant exact records retrieved and their current source hashes verified. |
   | `ERROR` | Operational failure (source, index, configuration or I/O): `operation_status=error`, no evidence, exit 1. Not a gate outcome. |

   A separate `conflict_review_required` flag is raised only when a *later*
   direct user record uses explicit correction language (bilingual pattern list)
   — never merely because two records disagree lexically. There is deliberately
   no `CONFLICT` status: confirming a material contradiction requires opening
   the originals, which is the root agent's job.

Exit code is `0` for a completed evidence run, `2` for normal `NOT_FOUND`, and
`1` for source/index/configuration or I/O failures. All output modes report those
failures. `--json` returns an error envelope with `operation_status: "error"`,
`status: "ERROR"` (0.2 and earlier said `PARTIAL` here), an empty `evidence`
array and `error` text. No success packet
is printed; an interrupted saved run can leave diagnostic files, so consumers
must check the exit code and completed run metadata.

`--evidence-json` prints `evidence-delivery-v1`: the actual inlined source paths,
indexed hashes and exact content. The existing `--json` success output remains
metadata only. See [the delivery contract](../eval/EVIDENCE_CONTRACT.md). Omitted
sources and answer-card interpretations do not count as delivered evidence in
this format. Use the same format and budget for all systems in a comparison.

`--no-save` uses a temporary directory cleaned on success and failure. A source
mismatch requires an explicit index rebuild. Builds and incremental updates use a
temporary SQLite file and replace the previous index only after success; a failed
build preserves the previous index (`build_index.py --full` rebuilds everything,
the default updates only the notes whose bytes changed). A file that is not valid UTF-8, is too large, unreadable or has an
unsupported name is skipped and listed in `index-manifest.json` (see
[docs/source-lifecycle.md](../docs/source-lifecycle.md#what-the-index-skips)).

---

## What this is **not**

Read this part before trusting anything the packet says.

- **It is not a semantic judge.** Routing is trigger-phrase matching; retrieval
  is BM25 lexical search over an FTS5 index. There is no embedding model, no
  reranker, no notion of meaning. A paraphrase that shares no words with your
  vault will not be found.
- **It does not answer questions.** It emits source text and a status. Every
  claim you build on a packet still requires opening the cited original — that
  is why every evidence block carries an absolute path and a hash.
- **BM25 rank is not proof.** Retrieval order is a lexical statistic. It is not
  evidence of truth, recency, authority or user approval. Neither is the number
  of times a phrase occurs in the vault: an outdated rule can easily outnumber
  the current one on disk, which is exactly why `rule_state` /
  `superseded_by` labelling is enforced in code rather than left to ranking.
- **Hash verification proves file identity, not correctness.** `verified` means
  the byte snapshot read for this packet matches the hash recorded when indexed.
  It does not promise the file will remain unchanged after that read. It says
  nothing about whether the file's content is right, current, or approved.
- **A verified answer card is a pointer, not an authority.** The quote is
  checked byte-for-byte; the *interpretation* in the `answer` field is written
  by you and is not verified by anything.
- **No performance claims are made here.** This README states no benchmark, hit
  rate, latency or token figure, because none were measured for this generic
  version. If you need numbers, measure them on your own vault.
- **The evidence gate is heuristic.** `SUPPORTED` means "relevant exact records
  were retrieved and hashed", not "the answer is correct".
- **It is single-vault and offline.** No network, no remote sources, no
  cross-vault federation.

### Not carried over from the original

This is a generalized extraction from code written for a private vault that is
not distributed here. Three subsystems were
deliberately left out rather than faked, because they depended on data schemas
that do not exist in a generic Markdown vault:

- **Same-turn conversation recovery** (pulling every user steering and the final
  answer out of a long agent turn) — required a specific agent-transcript JSONL
  schema with turn IDs and message phases.
- **Active-turn archive state** (detecting whether the prompt's own turn is
  already in the index and complete) — same dependency.
- **A peer "which rule is in force" reasoning layer** — a separate program that
  is not part of this extraction.

The record types they produced (`conversation_message`, `role: user/assistant`)
are still first-class in the schema, the gate and the ranker, so an index built
from conversation data still works; only the automatic turn reconstruction is
absent.

One behavioural fix was made relative to the original: a status check that
vacuously succeeded when a packet contained no conversation records — which
would force every file-only vault to `PARTIAL` — now requires at least one
conversation record before it applies.
