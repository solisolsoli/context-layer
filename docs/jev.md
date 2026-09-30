# The optional advisor (Jev)

context-layer's core never calls a model. The advisor is the one optional
component that can: it puts one narrow, fixed question to a model provider
about evidence the local retrieval already found, and records its judgement
beside that evidence. It is **off by default**, and a vault without
`.context/jev.json` never loads its provider client.

The name follows the optional Jev advisor of Avenox Beyin, whose design this
component follows (see [Credits](#credits)). "Jev" is also the name of
TypeSafe's model; no affiliation with TypeSafe or Avenox is claimed.

## What it does, and what it never does

**Does (in this version).** `context-layer search --jev` asks, for each passage
in the packet and for each note the retrieval reached but did not deliver:
*would reading this note directly help answer the request?* In mode `shadow`
the answers are only counted and shown. In mode `on`, notes judged relevant are
appended to the packet as the exact passages the deterministic packer would
have delivered for them, within their own budget. The prompt hook does the
same on every prompt when, and only when, you enable the `auto_context` feature
by name (see [The prompt hook](#the-prompt-hook-auto_context)): a topicality
question about the prompt first, then the same relevance questions, within a
deadline that keeps the hook inside the host's time limit. The `answer`
feature asks a different question of quoted evidence: *does this quote, read
within its section, support this claim?* (see [Answer checks](#answer-checks)).

**Never.**

- It never writes or rewrites evidence. Every passage it adds is a byte span of
  a source whose SHA-256 was re-checked after the provider answered.
- It never removes evidence, unless you set the named lossy lever `prune_fts`
  (see [Lossy lever](#lossy-lever)).
- It never writes memory, approves anything, or changes the exit code of an
  existing command.
- It never stores a key, and there is no `--key` flag.
- Without a configuration, in mode `off` or with a kill switch, it makes no
  call, reads no key, touches no cache and never imports the provider client.

`search`, `auto_context`, `answer` and `memory` are wired in this version, so every
feature can make a call once it is enabled.

## The prompt hook (`auto_context`)

`context-layer jev shadow|on <vault> --enable auto_context` switches the
feature on; `jev shadow` and `jev on` alone never do, and `jev status` then
says `automatic_model_calls: yes` with what is sent. On every prompt the hook
(`hook claude-code`, fts or the default synaptic method; not `--compact`):

1. plans from `.context/jev.json` only: no configuration, an invalid one, mode
   `off`, the feature not enabled, a kill switch or the child guard mean
   today's hook, byte for byte and silently (a kill switch or the child guard
   on an enabled feature writes one counter row);
2. runs the retrieval with the side channel (`--jev-candidates 8`);
3. gives the advisor `min(hook_timeout_s, 25 s − time spent)`; under 1 s left
   it asks nothing (`skipped_deadline`), so the hook stays inside the host's
   30 s;
4. asks one `topicality.v1` question about the prompt (*is this prompt about
   the vault's subjects at all?*) and one `relevance.v1` question per delivered
   passage and per candidate, through the same privacy gates as `search --jev`;
5. in `shadow`, prints exactly what it prints without the advisor and counts
   (`gate_passed`, `would_rescue`, `would_skip`); in `on` (a receipt covering
   `relevance` **and** `topicality`), appends the rescued passages as
   `<<evidence ...>>` blocks after the unchanged ones, each with a line
   `reason: advisor judged the linked note relevant: ...; advisor p_yes 0.95
   (kind); advisory, not a check of correctness`, only when the gate passed
   (`p_yes` at or above `gate`); a prompt judged off topic gets no rescue and
   nothing else changes;
6. the lossy `gate_skip` lever (off by default) is the one way to lose context:
   in `on`, a prompt judged off topic then gets no evidence at all (exit 0, one
   stderr line); in `shadow` it is only counted (`would_skip`).

Every failure (deadline, provider error, a source that changed, a receipt that
stopped matching) leaves the hook's output as it is without the advisor, with
exit 0 and one counter row (`feature: auto_context`). The advisor never changes
the hook's exit code and never blocks a prompt.

## Modes

| Mode | Calls | Packet |
| --- | --- | --- |
| `off` (default; also when the file is absent or invalid, or a kill switch is set) | none | exactly what it is without the advisor |
| `shadow` | yes | unchanged; `search --jev` adds a top-level `jev` block with the answers |
| `on` (refused without a calibration receipt) | yes | the unchanged packet, then rescued passages marked `origin: "jev"` |

```sh
context-layer jev status <vault>
context-layer jev shadow <vault> --provider-kind KIND [--base-url URL] [--model ID] [--key-env NAME]
context-layer search <vault> --prompt "..." --method synaptic --jev
context-layer jev report <vault>
context-layer jev review-memory <vault> --proposal proposal.json   # see "Memory review"
context-layer jev status <vault> --check                           # see "Status check"
context-layer jev record <vault> --questions q.jsonl --out r.jsonl --run   # a development recording
context-layer jev calibrate <vault> --report report.json --recording r.jsonl --apply
context-layer jev on <vault>          # refused until a calibration receipt exists
context-layer jev off <vault>
```

There is no default provider. The first `jev shadow` or `jev on` must name
`--provider-kind`; later calls reuse the saved provider. `jev on` and
`jev shadow` never switch on `auto_context`: only `--enable auto_context` does.
`--enable F` / `--disable F` (repeatable) switch features; `--no-jev` on a search
wins over `--jev`.

## Providers and where your text goes

The transport lives in `context_layer/jev_client.py`, the only module of the
package that may open a network connection and the only one that starts a model
command line for the advisor. Other modules start programs of their own (the
task backends run the host's CLI when you run a task); `make network-guard`
scans `context_layer/`, `router/` and `eval/` for the two rules above and does
not cover `bench/` or `scripts/`.

| Kind | Reached how | Where the question and excerpts go | Status |
| --- | --- | --- | --- |
| `systemone` | `POST {base_url}/v1/systemone` with the key from the variable named by `--key-env` | the host in `--base-url` (for example TypeSafe or OpenRouter over https, or a Laya server on `127.0.0.1`) | request shape follows TypeSafe's public API; no call to any provider was made while building this version: expected but unverified |
| `openai_compat` | an OpenAI-compatible server on `127.0.0.1` or `[::1]` with JSON-schema output | that local server (and wherever it forwards) | expected but unverified |
| `host_cli` | your own headless `claude -p` with a JSON schema (needs `--model`) | that CLI's model provider, under your login | expected but unverified |
| `cmd` | a local program named by `provider.argv` (set by hand) | wherever that program sends it | unverified |
| `recorded` (`--provider-kind recorded --recording FILE`), `fake` | replayed or scripted answers | nowhere | for tests and offline evaluation; a recording is made by `jev record` |

Endpoints must be `https`; plain `http` is accepted only on the literal
loopback addresses `127.0.0.1` and `[::1]` (not `localhost`, which a hosts file
can point elsewhere). User information, a query or a fragment in the URL is
refused. `jev status --check` checks the endpoint and, for a provider on a
loopback address, whether something answers there (see [Status check](#status-check)).

## What `search --jev` sends

Printed by `context-layer jev status` as well:

> the question (at most 2,000 characters) and, for each judged passage and
> each rescue candidate, the note's file name without its folder (at most 160
> characters), the line that links to it (at most 300 characters) and the
> first 800 characters (`excerpt_chars`) of the passage that would be
> delivered; never folder paths, hashes, the vault name, keys or local-only
> notes.

Each passage and each candidate is its own questionnaire, so no question mixes
two notes. Only this bounded *view* is sent; the passage that may be delivered
is never cut to fit.

**Candidates** come from `eval/retrieve.py --jev-candidates N` (N =
`max_candidates`, default 12, at most 32), a deterministic, model-free side
channel that never enters `evidence`:

- in the default synaptic mode, the notes the link graph reached that got no
  passage: the ones the lexical link rule left out (link relevance below 0.6 ×
  the best link, no best at all, or beyond the four reserved notes), or whose
  reserved share did not fit. Hubs,
  stale notes and notes whose bytes no longer match the index are left out.
  Each carries its linking line and the passages it would get if reserved;
- for fts and synaptic, the bm25 tail: notes ranked after `--top-k`, each with
  what fts would deliver for it (the note whole, or its match windows).

`search --jev` removes the side channel from what it prints. `--jev-candidates`
on its own leaves `evidence` and every other key of the packet unchanged
(tested).

## Answer checks

The `answer` feature (on by default with `jev shadow|on`, like `search`; switch
it off with `--disable answer`) puts one `claim_support.v1` question per claim
and citation to the provider. It is used in three places, through the same gates,
secret scan, cache, freshness check, kill switches and counters as `search`:

- `context-layer jev answer <vault> --claims FILE [--json]`, where FILE is a
  `jev-claims/v1` document: 1 to 20 claims, each `{"text", "citations", "id"?}`
  with 1 to 8 citations `{"source_path", "source_sha256", "line_start",
  "line_end", "span"}` (a claim's text is at most 2,000 characters, a span at
  most 20,000, the file at most 1 MiB);
- `context-layer handback check DIR ... --jev`, which asks about each evidence
  record that passed the deterministic check, the record's `observation` being
  the claim and its `span` the quote;
- the MCP tool `check_claims` (see [Over MCP](#over-mcp)).

**What is checked first, always, without a model.** Every citation must be a
verbatim span at the cited lines of a source that is inside the vault's
boundaries and still has the cited SHA-256, long enough to anchor a claim (the
mechanical rules of `handback check`). A citation that fails is reported with its
reasons and is **never sent**. In `jev answer` and `check_claims` the claim's own
numbers, dates and names are not required to appear in the quote (a claim the
quote contradicts differs on exactly those), so they are listed as
`anchor_notes` instead; `handback check` keeps its stricter rule and asks only
about records that carry every hard token of their observation.

**What is sent.** For each judged citation: the claim (at most 2,000
characters), the verbatim quote (at most 4,000) and its enclosing section (at
most 4,000): the text from the nearest heading above the cited lines to the next
heading of the same or a higher level, with subsections included and `#` lines in
code fences not counted; without a heading above, from the start of the file. A
quote can be exact while its section later cancels it, so the section decides.
Nothing is cut to fit: a section or quote over its limit means the citation is
not asked (`context_incomplete`), and so does a claim over 2,000 characters.
Never folder paths, hashes, the vault name, keys or local-only notes (the same
frontmatter convention as [Privacy gates](#privacy-gates); a local-only source is
`local_only`, never sent). A credential-shaped string in a claim, quote or section
drops only that question (`sensitive_input`); the others are still asked, and if
every question is held nothing is sent.

**Verdicts.** The provider picks `supports`, `contradicts` or `silent`, with
probabilities. At confidence (normalized-max-v1) of at least
`thresholds.confidence` (0.80, inherited from TypeSafe's citation recipe, not
measured here) they become `supported`, `contradicted` or `insufficient`; below
it, `uncertain` (`low_confidence`). An answer without probabilities is
`uncertain` (`uncalibrated`) unless a calibration receipt covers the provider
(mode `on`). `p_yes` in a result is the probability of `supports`. A claim's
own verdict comes from its judged citations: `supported` if any supports (and
none contradicts), `contradicted` if any contradicts, `insufficient` if any is
insufficient, otherwise `uncertain`; one citation that supports and another that
contradicts make it `uncertain` (`citations_disagree`); `not_judged` when no
citation was asked.

**Advice only.** The report always says `approved: false`, `memory_written:
false`, `rewrites: false`. A verdict is a model's judgement of support, never a
verification, and never a verdict on whether the claim is true. It can only
*add* a note beside the deterministic result: in `handback check` the notes are a
`jev` object per record that passed and a top-level `jev_summary`, and `ok`,
`problems`, `mechanically_checked`, `observation_unanchored`, the check digest,
the ledger line and the exit code are exactly those of the run without `--jev`
(a failed record cannot become a pass; it is never asked about).

| Mode | `jev answer` | `handback check --jev` | MCP `check_claims` with `jev: true` |
| --- | --- | --- | --- |
| `off`, no configuration, kill switch, child guard, `answer` disabled | mechanical report; `jev.why` names the reason; no call | the deterministic report (bytes as without `--jev`); one stderr line names the reason | mechanical report, no `jev` key |
| `shadow` | calls; verdicts shown, `applied: false` | calls and counts; stdout and `--out` bytes as without `--jev`; one stderr line | calls and counts; result as in `off` |
| `on` (needs a receipt covering `claim_support`, else it acts as `shadow` with `calibration_required`) | calls; verdicts shown, `applied: true` | notes added as above | verdicts added |

Counters: the call log row has `feature: "answer"`; `candidates` is the number of
citations asked, `flagged` the ones judged `contradicted`, and, for this feature
only, `would_rescue` and `rescued` count the advisory notes `on` would add and
added, so `jev report`'s shadow change rate is the share of shadow calls in
which `on` would have added at least one note.

Not built: `handback check --jev-priority` (a sample that draws advisor-flagged
records first) is a design idea that is not implemented, because it would let
model output steer which records the root reads.

## Over MCP

`check_claims` takes `claims` in the shape above (`maxItems` 20, 8 citations per
claim; over a limit is a tool error that names it) and `jev` (default `false`).
It always runs the mechanical check; with `jev: true`, if the vault owner
enabled the `answer` feature and its mode is `on`, each result also carries the
advisor's verdicts and the result has a `jev` block. In `shadow` the call is
counted and the result has no `jev` key, as when the advisor did not run. It
writes no note or memory record; it is annotated not read-only because, with
`jev: true`, it may add counters and cached answers under `.context`.

`search_vault` takes `jev: true` (default `false`): without a configuration,
in mode `off`, with a kill switch or with the `search` feature disabled the
tool returns the plain packet (no `jev` key: the advisor did not run); in
`shadow` the packet is unchanged plus the `jev` block; in `on` rescued
passages follow the unchanged items, `origin: "jev"`. The read-only
`jev_status` tool returns what `context-layer jev status --json` prints. The
MCP server never enables the advisor and never reads a key of its own: the
vault's `.context/jev.json` decides, as for the CLI.

## Memory review

`context-layer jev review-memory <vault> --proposal FILE [--json]` (feature
`memory`) reviews what someone wants to add to the shared memory
([memory.md](memory.md)) before it is added. This is the only entry point.
The advisor never writes, accepts or rejects a record: the memory store is only
read, and every report says `approved: false` and `memory_written: false`.

**Input**, a `jev-memory-proposal/v1` (the `schema` key may be left out): one
JSON object, a JSON array of up to 64 of them, or JSON lines.

```json
{"schema": "jev-memory-proposal/v1", "kind": "decision",
 "text": "The night watch rota now starts at 8 in the evening instead of 10.",
 "evidence": [{"path": "logbook/April.md", "sha256": "<64 hex>", "line_start": 9,
               "line_end": 9,
               "span": "The night watch rota now starts at 8 in the evening instead of 10."}],
 "prior": ["m-0123456789abcdef"]}
```

`kind` is one of `decision`, `task`, `result`, `note`; `text` at most 4,000
characters; 1 to 8 evidence spans, each with exactly those five keys (the same
span shape as a handback record). `prior` is optional: absent means the newest
at most four records in force that share an evidence path; a list means exactly
those ids (at most four, each in force, else the input is refused); `[]` asks
for no comparison.

**Always, in every mode, without a model:** each span is checked the way a
handback record is (`orchestrate.check_record_detail`: the path passes the
source boundaries and exclusions, `sha256` is the file's current hash, and the
span is verbatim at the cited lines, long enough to anchor a claim), and an
exact duplicate of a record in force (same content address as
`memory.record_id`, or the same kind, text and sources) is named. A stale
source shows as `stale: true`.

**With the advisor** (shadow and on), four fixed questions, each a separate
request through the same provider, privacy gates, secret scan, cache, freshness
re-check, kill switch and counters as `search --jev`:

| Question | Options | Sees |
| --- | --- | --- |
| `memory_support` | `supports`, `contradicts`, `silent` | the proposal text and each span with its heading-bounded section |
| `memory_commitment` | `asserted`, `tentative`, `not_stated` | the same |
| `memory_kind` | `decision`, `task`, `result`, `note`, `question`, `hypothesis`, `other` | the same (the kind the proposal claims is not sent) |
| `memory_relation`, one per prior | `duplicate`, `refines`, `replaces`, `contradicts`, `unrelated` | the proposal text and that prior record's text |

At most seven requests per proposal. Nothing is asked about a proposal that
fails a mechanical check or duplicates a record in force, whose source is local
only (the gates of [Privacy gates](#privacy-gates)), or whose view does not fit
(a span with its section over 4,000 characters is never cut; the advisor says
`context_incomplete` instead). A secret-shaped string in one prior record drops
only that relation question; one in an evidence span drops the three questions
that carry it. A prior record whose sources are local only, unreadable or out of
bounds is not compared.

| Mode | Output | Counters |
| --- | --- | --- |
| `off` (default; also not configured, a kill switch, the feature disabled) | the mechanical report; `advisor: null`; no call, no key read, provider client not loaded | none (one row for a kill switch or a disabled feature) |
| `shadow` | byte for byte the same as `off` | the four answers are asked and counted in `jev report` (`flagged`: the proposals `on` would send to inspection) |
| `on` (needs a calibration receipt covering the four `memory_*` purposes, else it runs as shadow and says so on stderr) | the report plus the `advisor` block: the labels and confidences, the route | as shadow |

**Route.** `inspect_sources` unless the advisor answered all four questions
(with a relation for every prior) and: every confidence is at least
`thresholds.confidence` (0.80; a label-only provider has no confidence, and its
answers count only because the report is shown under a receipt), support is
`supports`, commitment is `asserted`, the kind is not `question`, `hypothesis` or
`other`, and no relation is `contradicts`. Then the route is `candidate`: a
candidate for the agent's or the person's review, not an approval. `reasons`
lists fixed codes (`mechanical_failed`, `exact_duplicate`, `advisor_not_applied`,
`support_not_supports`, `commitment_not_asserted`, `kind_not_a_record`,
`relation_contradicts`, `<question>_low_confidence`, `<question>_not_judged`,
`prior_not_judged`, `local_only`, `context_incomplete`). Off and shadow have
route `inspect_sources` with `advisor_not_applied`: nothing judged the proposal.
A confident `replaces` fills `suggested_supersedes`, a confident `duplicate`
fills `semantic_duplicate_of`; for a `candidate` that is not a duplicate the
report carries `suggested_command`, the `context-layer memory add <vault> ...
--supersedes m-...` line a person could run. It is text: nothing is run. A
difference between the kind the proposal claims and the kind the advisor judges
is visible in `advisor.answers.kind` and does not change the route.

**Failure.** A provider error, a timeout, an invalid answer, a source, prior
record, `routes.json` or configuration that changed during the call, or a kill
switch set meanwhile leave the mechanical report (as `off`), one counter row,
and a line on stderr. Exit codes: 0 a report was produced (whatever it says), 1
the input was refused, 2 usage. Advice never changes the exit code of an
existing command.

The report holds labels, confidences and fixed codes, never text from the
provider; the log, the cache and any trace hold no proposal text either. What is
sent to the provider is printed by `context-layer jev status`: the proposal, its
spans with their sections and up to four prior record texts.

## Status check

`context-layer jev status <vault> --check` adds one probe line to the status.
Plain `jev status` sends nothing and starts nothing. The probe is
`jev_client.probe`, in the one module that may reach a network or start a
program. It is a reachability check, not an advisor call: no question, no note
text, no key.

| Provider | What `--check` does |
| --- | --- |
| `openai_compat`, or `systemone` on `127.0.0.1` / `[::1]` (a Laya, Ollama, llama.cpp, LM Studio or vLLM server) | `GET /health`; if that does not answer 2xx (or 401/403, which count as reachable: the probe sends no key), `GET /v1/models`. At most two bodyless requests, no redirects, no proxy, 1 second in all |
| `systemone` on any other host | nothing is sent: a remote provider is never probed, so a check never reaches the internet |
| `host_cli` | `claude --version` in a private temporary directory (1 second); no model is asked |
| `cmd` | nothing is started; the program named by `argv[0]` is looked up |
| `recorded`, `fake` | nothing |

Nothing is sent either when the advisor is off (mode `off`, a kill switch or the
child guard) or the endpoint is not acceptable. A probe that is late is
abandoned after its time limit (`deadline_exceeded`). The result is under
`check.probe` in `--json` (`checked`, `ok`, `code`, `detail`, `requests`,
`latency_ms`, and `version` for `host_cli`); it never holds a URL, a response
body or a key.

## Privacy gates

Applied in this order, before any byte could leave, for every provider:

1. **Boundaries and exclusions.** Each source is re-resolved inside the vault
   with the exclusions of `routes.json`; excluded, dot-path and symlinked
   sources are never read.
2. **`local_only_prefixes`** in `jev.json`: indexed and retrieved as usual,
   never sent.
3. **The frontmatter convention** (fail closed). A note is local only when its
   frontmatter has
   - `remote_allowed` or `jev` with a raw value other than exactly `true`
     (so `false`, `"false"`, `"true"`, `no`, `True`, a list or a comment all
     keep the note local);
   - `sensitivity` other than `public`, `internal` or `normal`;
   - `visibility: private`;
   - anything the small frontmatter parser cannot read with certainty: a
     byte-order mark at the start of the file, an opening `---` that is never
     closed, or one of these four keys in any form other than a top-level
     `key: value` line (a nested key, a list, a tag named `jev`), or whose value
     is not a plain scalar: a block scalar (`>`, `|`), an anchor, alias, tag or
     flow collection, or any `#` (so `visibility: private # note` stays local).
4. **Secret scan** of each serialised request and of every raw string in it:
   private-key headers, cloud access-key ids and `AIza` API keys, GitHub tokens,
   `sk-` keys, Slack tokens, bearer tokens, a bare three-segment JSON web token
   (`eyJ...` with two dots, no `Bearer` needed), `password`/`secret`/`token`/`api key` assignments with
   a value of 12 or more characters, URL user information, plus the literal
   strings of `blocklist_file` (one per line, at least 4 characters, at most
   100). A question whose view holds a hit is not asked, and `sensitive_input`
   is named in the codes; the prompt sits in every question, so a hit in the
   prompt drops them all and no call is made. A pattern scan cannot
   promise to find every secret.
5. **Prompt length.** A question shorter than 12 characters is not sent.
6. **Size.** A request longer than `max_input_chars` is not sent
   (`budget_exceeded`); at most `max_requests` requests per search.

A local-only passage stays in the packet; a local-only candidate is never
judged and never rescued.

## Freshness, failures and the kill switch

Before a call the advisor pins every source it will judge to its SHA-256, and
notes the SHA-256 of `routes.json` and a revision of `jev.json` (its bytes plus
modification time, size and inode). After the provider answers, and before
anything is used or cached, every pinned source is re-resolved inside the
boundaries and re-hashed and both files are compared again; any difference, or a
kill switch set meanwhile, discards all advice from that call. The kill switch and
the configuration revision are also checked just before sending, so a kill switch
set after the search started stops the call itself.

**Every failure falls back to the local packet**, plus one counter row:
timeout (`deadline_exceeded`), provider errors (their code), malformed answers
(`answers_invalid`, `answer_invalid`), a secret (`sensitive_input`), a changed
source (`source_changed`) or configuration (`config_changed`), a missing
advisor module (`advisor_unavailable`), a short prompt (`prompt_too_short`).
The provider call is abandoned after `timeout_s` plus a small grace and is never
retried. If only some questions fail, the others may still be used and the block
says `degraded: true`.

**Kill switches**: the environment variable `CONTEXT_LAYER_JEV_DISABLE` (any
value but empty or `0`), or a file `.context/jev.disabled`. Either makes the mode
in force `off` without changing the saved mode; removing it restores the saved
mode. `CONTEXT_LAYER_JEV_CHILD` does the same automatically inside an advisor
call (a recursion guard). When a person asks `--jev` in a configured vault while a
kill switch, the child guard or a disabled feature holds, the search is the
plain one and one counters-only row records why; without a configuration, with
an invalid one, or in mode `off`, nothing is written at all.

## Configuration: `.context/jev.json`

Written only by `context-layer jev off|shadow|on` (validated, a temp file made
`0600` before any content, then an atomic replace). A key a person adds by hand
is kept if it is a known key. At most 16 KiB. The file never holds a key: a
value that looks like a credential makes it invalid.

```json
{"schema_version": 1, "mode": "shadow", "features": ["search", "answer", "memory"],
 "provider": {"kind": "systemone", "base_url": "https://api.example", "model": "jev-1.13.0",
              "key_env": "TYPESAFE_API_KEY"},
 "timeout_s": 3.0, "hook_timeout_s": 2.0, "max_candidates": 12, "max_requests": 32,
 "max_parallel": 4, "max_input_chars": 24000, "excerpt_chars": 800,
 "jev_extra_tokens": 400, "cache_ttl_s": 3600,
 "thresholds": {"gate": 0.25, "keep": 0.4, "rescue": 0.6, "confidence": 0.8},
 "lossy": {"gate_skip": false, "prune_fts": false}, "local_only_prefixes": [],
 "trace": true, "env_file": null, "blocklist_file": null}
```

| Key | Default | Allowed | Provenance of the default |
| --- | --- | --- | --- |
| `timeout_s` | 3.0 | above 0, at most 10 | design choice of this project (upstream Jev also defaults to 3.0) |
| `hook_timeout_s` | 2.0 | above 0, at most 5 | design choice; the advisor's share of a hook run (see [The prompt hook](#the-prompt-hook-auto_context)) |
| `max_candidates` | 12 | 0-32 | design choice |
| `max_requests` | 32 | 1-64 | design choice |
| `max_parallel` | 4 | 1-8 | design choice |
| `max_input_chars` | 24,000 | 1-100,000 | upstream Jev's default |
| `excerpt_chars` | 800 | 1-4,000 | upstream Jev's manual excerpt length |
| `jev_extra_tokens` | 400 | 0-2,000 | coordinator decision: a budget separate from `--extra-tokens` |
| `cache_ttl_s` | 3,600 | 0-86,400 (0 = no cache) | upstream Jev's default |

Also: `schema_version` must be 1 (a newer one makes the file invalid);
`local_only_prefixes` is a list of at most 64 vault-relative paths;
`env_file` and `blocklist_file` must be absolute, regular, not symlinks, and
**outside the vault** (a file inside it could be indexed and delivered as
evidence); `key_env` must look like `TYPESAFE_API_KEY`. A file that is invalid
in any way (an unknown key, a value out of range, a symlink, a duplicate key,
invalid JSON) makes every advisor path behave as `off` (`config_invalid`); the
mode commands then refuse and leave it untouched, the same fail-closed stance as
`routes.json`.

## Thresholds and calibration receipts

| Name | Default | Used for | Provenance |
| --- | --- | --- | --- |
| `keep` | 0.40 | a delivered passage below it is *flagged* off topic (removed only with `prune_fts`) | inherited from upstream calibration (Avenox Beyin's synthetic calibration of its Jev hook), **not measured here** |
| `rescue` | 0.60 | a candidate at or above it may be added in `on` | inherited from upstream calibration, **not measured here** |
| `gate` | 0.25 | topicality of a prompt (the hook rescues only at or above it) | inherited from upstream calibration, **not measured here** |
| `confidence` | 0.80 | choice verdicts of the `answer` and `memory` features | TypeSafe's public citation-check recipe; **not measured here** |

A candidate needs `rescue` to count as on topic; a delivered passage needs only
`keep`. An answer without probabilities (a label only) counts as 1 or 0 and can
drive a rescue only through a calibration receipt; it never removes anything.

**`on` is refused without a calibration receipt**, for every provider:
`.context/jev-calibration/<kind>-<model>.json` (characters outside
`[A-Za-z0-9._-]` in the model id become `_`), with `schema:
"jev-calibration/v1"`, the same provider kind and model, `passed: true` for the
purposes of every enabled feature that can call in this version (`relevance`
for search), the same thresholds, and the same question-template revision as the
installed contracts. If the receipt stops matching, a saved `on` runs as
`shadow` (`calibration_required`). No receipt ships with this version:
`jev record` and `jev calibrate` (next section) make one from a recording of the
development set, and until you run them for your provider `on` stays refused
by the command. The receipt is a plain file the vault owner can write; it
protects against forgetting to calibrate, not against the owner (`jev status`
says the same: the file records a calibration run and is not proof).

## Recording and calibration

A receipt comes from four steps; only the second sends anything, and only to
the provider you configured:

```sh
python3 tests/jev_dev_eval.py --provider oracle --json --dump-questions questions.jsonl > oracle.json
context-layer jev shadow <vault> --provider-kind host_cli --model <model>    # or systemone, openai_compat
context-layer jev record <vault> --questions questions.jsonl --out recording.jsonl        # dry run
context-layer jev record <vault> --questions questions.jsonl --out recording.jsonl --run  # asks
python3 tests/jev_dev_eval.py --provider recorded:recording.jsonl --json > report.json
context-layer jev calibrate <vault> --report report.json --recording recording.jsonl --apply
context-layer jev on <vault>
```

1. The evaluator (`tests/jev_dev_eval.py`, a development aid, not a benchmark)
   builds the fictional development set (`tests/fixtures/dev_jev.py`, written
   and hashed before any provider ran on it) in a temporary directory, runs
   every advisor question through an oracle that answers from the labels,
   checks that no privacy trap reached a request, and with `--dump-questions`
   writes the distinct questionnaires it sent, one per line (372 in this
   version: 304 relevance, 16 claim support, 12 topicality and 40 memory
   questions; `--json` reports the count as `dumped_questions`).
2. `jev record` checks every question against the installed contracts and the
   secret scan, refuses a kill switch, and with `--run` asks the configured
   provider in batches (`--max-requests`, `--max-parallel`, `--deadline-s`;
   defaults from `jev.json` and 60 s). One `jev-recording/v1` row per question
   goes to `--out` (default `.context/jev-recordings/<kind>-<model>-<utc>.jsonl`,
   `0600`): the recording key, the template and its revision, the provider
   identity, the validated answer (type, label or number, probabilities) or a
   failure code, counters and the reported model id. Never the question, the
   excerpt or a key. A row with a code marks a question that failed; the
   command exits 1 when any did, and stops early on a kill switch (the rows so
   far are kept). A recording of the fictional set holds fictional data and
   may be committed.
3. `--provider recorded:FILE` replays the recording against the same questions,
   joined by key, so a changed template or provider gives `recording_miss`,
   never a wrong answer, and prints the report as JSON.
4. `jev calibrate` reads the report and the recording. It refuses anything that
   is not a recorded-provider evaluation with the privacy and integrity checks
   passed, with the configured thresholds, for the configured provider and made
   with the installed template revision. With `--apply` it writes
   `.context/jev-calibration/<kind>-<model>.json`: `passed` per question
   purpose against the bars below, the numbers each rests on, the dev set hash
   and version, the hashes of the report and the recording, the thresholds and
   the template revision; no path. It exits 1 when a purpose the enabled
   features need is not met (the receipt is still written, and `on` stays
   refused).

| Purpose (feature) | Bar | Provenance |
| --- | --- | --- |
| `relevance` (search, auto_context) | precision ≥ 0.85 at `rescue`; at least 12 answer notes and 20 non-answers judged; injection notes rescued no more often than neutral distractors | the design's proposal (audit F §8), with 12 in place of 20 because the set holds 16 answer notes |
| `topicality` (auto_context) | ≥ 0.90 of non-topical prompts below `gate` and ≥ 0.95 of topical prompts at or above it; every prompt judged | the design's proposal |
| `claim_support` (answer) | ≥ 0.90 accuracy on clear verdicts; no cancelled-plan claim `supported` or wrongly clear; every claim judged | the design's proposal |
| `memory_support`, `memory_commitment`, `memory_kind`, `memory_relation` (memory) | ≥ 0.80 accuracy each; every proposal judged | the design's proposal for the relation; the same bar for the other three, adopted before the first run |

The bars were written before the first recording and are the release rule, not
measurements. A receipt is necessary, not sufficient: a fictional English
development set is not your vault. Run `shadow` and `jev report` on your own
notes first.

`jev shadow|on <vault> --provider-kind recorded --recording FILE` configures
the replaying provider for tests and offline evaluation: the rows name the
provider they replay; the configured live provider is reused when it is that
one, else the block is rebuilt from the rows (`cmd` recordings cannot be
rebuilt: configure that provider first).

## Lossy lever

`lossy.prune_fts` (off by default): in `on`, fts passages judged below `keep`
are removed. The packet's `jev` block and `jev status` then say
`superset: false`. In `shadow` the would-be removals are only counted
(`would_prune`). `lossy.gate_skip` (off by default): in `on`, the prompt hook
adds no evidence at all for a prompt the advisor judged off topic; in `shadow`
the would-be skips are only counted (`would_skip`).

## What is written, and where

| Path | Written by | Holds |
| --- | --- | --- |
| `.context/jev.json` | `jev off/shadow/on` | the configuration; no key, no note text |
| `.context/jev.disabled` | you (a kill switch) | nothing needed; its presence is the switch |
| `.context/jev-calls.jsonl` | every advisor call (`search --jev`, the hook with `auto_context`, `jev answer`, `handback check --jev`, `check_claims`, `jev record`) | one row per call: counters only (`0600`, at most 512 KiB, older half dropped when full) |
| `.context/jev-cache/<hmac>.json` | shadow/on searches and claim checks | validated answers (`yes`/`no` and probability, or a claim label with probabilities and confidence), counters, the reported model id; never the question, excerpts or a key (`0700` folder, `0600` files) |
| `.context/jev.salt` | the first `jev shadow` or `jev on` | 32 random bytes keying the cache file names |
| `.context/jev-calibration/<kind>-<model>.json` | `jev calibrate --apply` | the receipt: `passed` per purpose, the numbers, hashes of the dev set, report and recording, thresholds, template revision; no path, no text |
| `.context/jev-recordings/<kind>-<model>-<utc>.jsonl` | `jev record --run` (default `--out`) | one row per question: key, template, provider identity, validated answer or failure code, counters; never text (`0600`) |
| `.context/activation.json` | synaptic searches, as before | with the advisor, additive `jev` fields: a block and one label per node |

Cache file names are HMAC-SHA256 values keyed by the salt, so a file listing
(a sync provider, a backup) does not let anyone confirm a guessed question. That
does not protect against someone who can read the vault, because the salt is
there too.

Log rows have exactly these keys: `v, at, feature, mode, applied, provider_kind,
model_id, cache_hit, degraded, code, latency_ms, requests, input_tokens,
output_tokens, cost_usd, candidates, judged, local_only, gate_passed, flagged,
would_prune, pruned, would_rescue, rescued, would_skip, skipped`; every value is
null, a boolean, a finite number from 0 to 10^12, or a short lowercase code. No
free text can enter.

`context-layer jev report <vault> [--days N] [--json]` summarises the log per
feature: calls, share applied, degraded calls by code, cache hits, p50/p95
latency, tokens and cost where a provider reported them, and the **shadow change
rate**: the share of successful shadow calls in which `on` would have rescued at
least one note (before the `jev_extra_tokens` budget).

`context-layer jev purge <vault>` lists the cache, the salt and stray temp
files; `--apply` removes them, `--all` adds the call log, `--receipts` adds
receipts and recordings. It never removes `jev.json` or `jev.disabled`. To
remove every advisor file: `jev purge <vault> --apply --all --receipts`, then
delete `.context/jev.json` and `.context/jev.disabled`.

## Formats

- **Packet.** A top-level `jev` block, `schema: "jev-advice/v1"`: `feature`,
  `mode`, `applied`, `superset`, `provider_kind`, `model_reported`, `counts`
  (`judged`, `local_only`, `flagged`, `candidates`, `would_rescue`, `rescued`,
  `pruned`, `cache_hits`; the hook adds `would_skip`), for the hook `gate`
  (`judged`, `p_yes`, `passed`, `threshold`) and `skip`, `items` (one verdict per
  delivered passage: `on_topic`,
  `off_topic`, `local_only`, `not_judged`, with `p_yes`), `candidates` (path,
  kind, verdict, `p_yes`, `rescued`), `degraded`, `codes`, `latency_ms`,
  `requests`, `input_tokens`, `extra_est_tokens`, `jev_extra_tokens`, `notice`.
  The passages the packet already held are left byte for byte as they were;
  rescued ones follow them with `origin: "jev"`, `start`/`end` (byte offsets),
  `line_start`/`line_end`, `via`, `reason`, `est_tokens` and a `jev` object
  (`p_yes`, candidate kind, the note it belongs to).
- **Trace.** `activation.json` keeps `version: 1`. With `trace: true` and a
  synaptic search, the advisor adds a top-level `jev` object (`mode`, `applied`,
  `superset`, `provider_kind`, `gate_passed`, `kept`, `flagged`, `rescued`, `would_rescue`,
  `degraded`) and per node `jev`: `rescued`, `on_topic`, `off_topic`,
  `local_only` or `not_judged`; rescued notes become `selected: true`. It
  writes only into the trace its own retrieval wrote (same run id and counts).
- **Side channel.** `jev_candidates`, `schema: "jev-candidates-v1"`: `limit`,
  `items` (`source_path`, `source_sha256`, `kind` `link` or `bm25_tail`, `hop`,
  `activation`, `via`, `rank`, `link_line`, `passages`), `trace_run_id`.
- **Claims.** Input `jev-claims/v1` (above). Output `jev-claim-report/v1`:
  `claims` (per claim: `claim` number, `id`, `citations` with `source_path`,
  lines, `mechanically_checked`, `reasons`, `anchor_notes` and, when shown, `jev`
  = `verdict`, `label`, `p_yes`, `confidence`, `code`, `cached`, `provider_kind`;
  and a claim-level `jev` with the aggregate `verdict`), `citations`,
  `mechanically_checked`, `approved`, `memory_written`, `rewrites` (all false),
  `meaning`, and `jev`, a `jev-advice/v1` block with `feature: "answer"` whose
  `counts` are `supported`, `contradicted`, `insufficient`, `uncertain`,
  `not_judged`, `local_only`, `asked`, `judged`, `cache_hits`. Verdict codes:
  `low_confidence`, `uncalibrated`, `context_incomplete`, `claim_too_long`,
  `local_only`, `sensitive_input`, `not_asked` (over `max_requests` or
  `max_input_chars`), `no_answer`, and the call-wide codes of
  [Freshness](#freshness-failures-and-the-kill-switch).
- Versions: `jev.json` `schema_version` 1, log rows `v` 1, cache entries `v` 1.

## Exit codes

| Command | 0 | 1 | 2 |
| --- | --- | --- | --- |
| `jev status VAULT [--json] [--check]` | shown | the configuration is invalid (shown, nothing changed) | usage |
| `jev off\|shadow\|on VAULT [--enable F]… [--disable F]… [--provider-kind K] [--base-url U] [--model M] [--key-env NAME] [--recording FILE]` | written (or nothing to switch off) | refused: invalid file left untouched, no provider named, `calibration_required`, unknown feature | usage, or the same feature both enabled and disabled |
| `jev report VAULT [--days N] [--json]` | shown | the log cannot be read | usage |
| `jev record VAULT --questions FILE [--out FILE] [--run] [--append] [--max-requests N] [--max-parallel N] [--deadline-s S]` | dry run shown, or every question answered | refused (no usable configuration, a kill switch, an invalid question, a credential in a question, an existing `--out` without `--append`), or some questions failed (their rows carry a code) | usage |
| `jev calibrate VAULT --report FILE --recording FILE [--apply] [--json]` | every purpose the enabled features need is met (shown, written with `--apply`) | refused (nothing written), or a needed purpose is not met (shown; written with `--apply`) | usage |
| `jev answer VAULT --claims FILE [--json]` | report produced, whatever the verdicts (an advisory report, also with the advisor off) | the claims file is refused (schema, size, a symlink, a missing vault file) | usage |
| `jev review-memory VAULT --proposal FILE [--json]` | a report was produced, whatever it says | the input was refused (schema, an evidence or prior id problem, unreadable file) | usage |
| `search ... --jev`, `handback check ... --jev` | unchanged from the command without `--jev` | unchanged | unchanged |

Advice never changes the exit code of an existing command.

## Measured results

No model's quality has been measured: no provider was called while building this
version, and the sealed benchmark has not been run with the advisor. The
pipeline was run with the oracle (the labels answering) on 2026-09-29: the 372
dump questions recorded through `jev record --run` (372 rows, none failed) and
replayed,
relevance precision 1.0 and recall 16 of 16 at `rescue`, the gate right on 12
of 12 prompts, no privacy trap in any request (the one question whose prompt
carries a fake secret is never asked), a receipt covering `relevance` and
`topicality` computed, `jev on --enable auto_context` accepted for that fake
provider, and the lexical-bridge case rescued by `search --jev` and by the
hook in `on`. That checks the plumbing, not any model. The plan: a recording of the
development set per provider you use, receipts computed from those recordings,
then a single sealed run by the coordinator, stated in the benchmark summary as
one more aggregate inspection of the sealed set.

## Limits

- **Prompt injection aimed at the judge.** A linked note can argue for its own
  rescue ("this note is highly relevant"). A rescue is bounded by
  `jev_extra_tokens`, labelled `origin: "jev"`, never approves anything, and is
  verbatim source text like every other passage. Use `shadow` and `jev report`
  on your own vault first.
- **Not deterministic.** Providers can answer the same question differently.
  The cache and receipts reduce, not remove, this.
- **Latency.** A search with `--jev` waits for the provider, up to `timeout_s`
  (default 3 s) plus a small grace.
- **Cost.** Provider-reported tokens and cost are recorded as counters; no cost
  was measured here.
- **Calibration.** A receipt is necessary, not sufficient: a fictional English
  development set is not your vault.
- **Budget.** A rescue that does not fit the remaining `jev_extra_tokens` is
  skipped, never cut: with the defaults, a bm25-tail note whose fts delivery (the
  note whole, or its match windows) is longer than 1,600 characters cannot be
  rescued.
- **Frontmatter.** The convention is deliberately lexical and fail closed; a
  frontmatter that merely mentions one of the four keys in another form keeps the
  note local. A note that starts with a byte-order mark is always local only.
- **Scope of this version.** `search` (CLI and MCP), the prompt hook's
  `auto_context`, the claim checks (`answer`: `jev answer`, `handback check
  --jev`, MCP `check_claims`) and `review-memory` are wired.
- **Claim checks.** A judge can be wrong in both directions, and the claim
  bars of the calibration receipt (`claim_support`) are proposals not yet met by
  any recording in this repository unless a receipt says so. A claim whose
  support spans several notes or several sections cannot be judged from one
  quote and section; each citation is asked alone. The advisor never reads the
  rest of the vault.
- **Memory review.** It reads the proposal's spans and the prior records the
  proposal names; it cannot know a fact that is in no source, and a `candidate`
  route is advice to look, not a check that the text is true. Only exact
  duplicates are found without a model; a reworded duplicate needs the advisor.
  The default prior selection is the newest four records that share an evidence
  path, not a search of the whole store.

## Tests

`tests/test_jev.py` (run by `make test`), offline: no socket, no `claude` or
`codex`, a temporary `HOME`. It checks, among others:

- **inert without a configuration**: init, index, fts and synaptic searches with
  and without `--jev`, `status`, `memory add`/`resume` and `jev status` under a
  guard that turns any network or model-CLI attempt into an error: the same exit
  codes and output with or without a key in the environment, no provider module
  loaded in any process, no advisor file, the key written nowhere
  (`Optionality`);
- **shadow changes nothing**: generated queries on the development vault,
  `eval/fixtures/docs` and `router/example-vault`, three flag sets, fts and
  synaptic, providers that answer yes, no, randomly, malformed, too slowly or
  by raising: every packet byte as without the advisor (`ShadowIdentity`);
- **`on` only appends**: the prefix is the packet without the advisor, extras
  are byte-exact, within the budget, never overlapping (`MonotoneEvidence`);
- **every failure falls back** with one counter row (`FailToLocal`), the privacy
  gates (`PrivacyGates`), no text at rest (`NoTextAtRest`), advice only and the
  lossy lever (`AdviceOnlyAndLossyNamed`), the configuration and commands
  (`ConfigurationAndCommands`), cache, log and report (`CacheLogReport`),
  recording, calibration and the replaying provider (`RecordAndCalibrate`: a
  recording holds no question text, a credential in a question stops the run,
  a receipt lets `on` through only when the needed bars are met), the prompt
  hook (`HookAutoContext`: off unless enabled by name, shadow byte for byte,
  `on` rescues only when the gate passed, `gate_skip`, no time left, a slow
  judge, a kill switch) and the MCP surface (`McpAdvisor`: `search_vault`
  with `jev: true`, `jev_status`);
- `tests/test_jev_answer.py`, offline like the above: the claim checks
  (`jev answer`, `handback check --jev`, MCP `check_claims`): off makes zero
  calls, shadow leaves the handback bytes as they were, `on` adds notes only and
  never changes `ok`, `problems`, the digest or the exit code, the kill switch,
  the child guard and the per-feature switch, a secret in one claim drops only
  that question, the cache and a changed source, a section over 4,000 characters
  is not sent, and the MCP tool is listed and bounded;
- **the lexical-bridge case** documented in [synapse.md](synapse.md): off
  delivers no answer note; `on` with a judge that says yes to it delivers
  exactly its reserved passage after the unchanged packet (`HarborLights`).

`tests/test_jev_memory.py` (run by `make test`), offline in the same way, covers
the memory review and the status check: `off` asks nothing, reads no key and
loads neither the client nor the contracts (`Modes`); `shadow` prints exactly
what `off` prints while the four questions are asked and counted; `on` adds the
advisor block and the route, and without a receipt runs as shadow; the route for
every answer combination, a label-only provider, the kill switch (before and
during the call), a secret in a prior record, an evidence span or the proposal
text dropping only the questions that carry it, local-only sources, a view that
does not fit, a source or a prior record changed during the call, provider
failures and a slow provider, and the memory store byte-identical after a review
in every mode (`KillSwitchGatesAndFreshness`, `MechanicalPriorsAndStore`);
`jev_client.probe` against loopback servers (health, models, 401, redirects,
nothing listening), refusing every non-loopback address before any I/O, bounded
in time even against a server that dribbles its status line, `claude --version`
through a fake `claude`, and `jev status --check` against `jev status`
(`Probe`, `StatusCheck`).

## Credits

The advisor follows the design of the optional Jev advisor in Avenox Beyin
(v3.1.0-v3.5.1, MIT, Avenox), whose Jev integration was contributed by Forn and
adapts Forn's hafiza-os (MIT): an optional, default-off advisor with
off/shadow/on modes, per-feature switches, a kill switch, a counters-only call
log, privacy gates and a freshness re-check. The provider protocol shape follows
TypeSafe's public API documentation. See [CREDITS.md](../CREDITS.md) and
[THIRD_PARTY.md](../THIRD_PARTY.md).
