# Sub-agents that cost little: delegate less, return evidence, check it mechanically

This page covers the orchestration commands (`packet`, `job`, `handback`,
`handoff`, in `context_layer/orchestrate.py`) and how they sit on top of
[`tasks`](tasks.md). Everything described here is deterministic and local. No
model is called and nothing touches the network.

The design rests on one rule. **Delegate only a bounded job that removes real
root work and returns evidence the root can check cheaply.** The root (the
coordinating agent) keeps judgement and authorship, and it reproduces every
critical claim itself. A worker's report is a claim until it has been checked
at the source. Worker completion is not approval, and a worker never grades its
own work.

## 1. Delegate or do it yourself

Check the job before dispatch, not after an attractive report comes back.

| Question | Delegate when | Keep it yourself when |
| --- | --- | --- |
| **Authorship** (mandatory) | The job is locating, enumerating, measuring, an exact transformation or an approved check | It needs judgement, interpretation of the user's intent, or the final argument |
| **Deterministic first** | A bounded search has to be adapted to the case | A command already does it (search, hash, diff, count, test): run the command |
| **Verification** (mandatory) | The root can check decisive results from compact anchors | The root would have to redo the investigation |
| **Avoided work** (mandatory) | The worker removes discovery or reading the root would really do | The root already knows the source or can get it with one command |
| **Scope** (mandatory) | Allowed sources, exclusions, unknowns, acceptance test and stop condition all fit the job | The worker would have to invent policy or the intended conclusion |
| **Independence** | Parallel branches have disjoint, frozen inputs and file ownership | Later work depends on earlier provisional conclusions |
| **Return** (mandatory) | A small receipt plus durable evidence records carries the whole result | The budget can only be met by dropping findings |
| **All-in estimate** (mandatory) | Startup, verification, retries and interruptions fit inside the avoided work, with a buffer | The saving only appears if the root's remaining work is ignored |

`context-layer job estimate` does the break-even arithmetic and prints the
checklist:

```
context-layer job estimate --root-read-tokens 40000 --reread-fraction 0.15 \
    --worker-price-ratio 0.2 --startup-tokens 2700 --verify-tokens 650
DELEGATE  (estimate; root-input-token equivalents)
  direct    W = 40000
  delegated = dispatch 0 + worker 8540 [(1+q)*r*(S+Wk+m*O) = (1+0)*0.2*(2700+40000+5*0)]
              + verify 650 + reread 6000 [f*W = 0.15*40000] + integration 0 = 15190
  saving    = 24810; required (buffer 0.2 x W) = 8000
  input-only break-even: re-reading more than 80% of W leaves no input saving ...
```

The formula, in root-input-token equivalents:

```
direct    = W
delegated = D + (1 + q) * r * (S + Wk + m * O) + V + f * W + I
delegate only when direct - delegated > buffer * W
```

- `W` is what the root would read doing the job itself, including its own checking.
- `f` is the fraction of that the root must re-read after delegating.
- `r` is the worker/root input price ratio.
- `S` is the worker's startup payload and `Wk` its reading (default `W`).
- `O` is worker output, priced at `m` times input. `m` defaults to 5, which is
  an assumption: set it from your own price sheet.
- `V` is the root's verification reading, `D` is dispatch, `I` is integration
  and `q` is the retry rate.
- Work that is identical in both arms is left out.

With the worker's reading equal to the root's, input alone breaks even when
`f = 1 - r`. At `r = 0.2`, a root that re-reads more than 80% of the material
saves nothing on input. This is a conditional calculation, not a rule of thumb.
The result is an **estimate**, and it can only be as good as the numbers you
give it.

## 2. The lean pipeline

```
packet build ──► job new / job validate ──► dispatch (tasks) ──► handback check
      │                                                               │
  one shared,                                              root reads: receipt
  hash-pinned                                              → check summary
  evidence id                                              → sampled records
                                                           → reproduces critical claims
```

1. **Build evidence once.**
   ```
   context-layer packet build VAULT --prompt "..." [--method fts] [--budget-tokens N]
   context-layer packet build VAULT --prompt "..." --method synaptic [--extra-tokens N]
   context-layer packet build VAULT --prompt "..." --method synaptic --compact [--budget-tokens N]
   ```
   This runs the same retrieval as `context-layer search` and pins every
   passage to its source SHA-256 and line range. The result is stored at
   `.context/packets/<id>.json`, where `id` is the SHA-256 of the packet's
   canonical JSON. That JSON covers:
   - the request;
   - the index identity (a hash of the indexed path/hash pairs);
   - for `synaptic`, the link-graph identity;
   - every passage.

   Building the same request against the same vault gives the same id and
   reuses the file. For `fts`, `--budget-tokens` (default 1200) caps the
   evidence at 4 × N characters. The default `synaptic` packet is the fts
   packet plus link-graph extras sized by `--extra-tokens` (default 600);
   `--budget-tokens` sizes only the `--compact` synaptic packet and is refused
   without `--compact` (exit 2), the same rule as `install --hook`. If the
   search withheld a source because it changed since indexing, no packet is
   built: run `context-layer index <vault>` first.

2. **Serve it only if it is still true.** `context-layer packet show VAULT ID`
   (or the MCP tool `read_packet {id}`) re-checks the packet before serving it:
   - the file still matches its id, and every entry is well formed (a crafted
     entry, such as a line number that is not an integer, is withheld with its
     reason, never a crash);
   - every source is still allowed by the `routes.json` exclusions (no symlink,
     no escape), exists and has the recorded SHA-256;
   - every passage is still verbatim at its recorded lines (a line ends at
     `\n`; see step 6).

   If any check fails, the **whole packet is withheld** (`status: WITHHELD`,
   with reasons). It is never served stale. Rebuilding it gives a new id.

3. **Write the job.**
   ```
   context-layer job new VAULT --objective "..." --packet ID --root-goal "..." \
       --allowed-root REL [--allowed-root REL]... --acceptance "..." --stop-when "..." \
       --max-total-tokens N --max-seconds N [--known-unknown "..."]... [--exclude "..."]... \
       [--out REL] [--task-id ID] [--attempt N] [--allow-over]
   ```
   This writes `.context/jobs/<task>/attempt-NNN/`:
   - `job.md`: Markdown plus one `support-job/v1` JSON block;
   - `input-manifest.json`: rule core path/hash, packet id/path, index identity,
     allowed roots and the `routes.json` hash;
   - `payload.json`: the budget accounting.

   On first use, the lean worker rule core is copied to
   `.context/jobs/worker_core.md`. The job is validated before anything is
   written. If it is BLOCKED or over budget, nothing is written. A job is
   immutable: a changed job is a new `--attempt`, and `job.md` is created
   exclusively, so two `job new` runs racing for one attempt cannot both write.

   The worker's output directory defaults to the job's own
   `.context/jobs/<task>/attempt-NNN/out`, which retrieval never serves. A custom
   `--out` is BLOCKED when it contains an indexed source, overlaps an allowed
   root (`.` overlaps every directory retrieval can serve), or sits in another
   hidden or tool-state folder; one outside `routes.json` exclusions is accepted
   with a warning that the next `context-layer index` will index the worker's
   files. `--max-seconds` becomes the task timeout when `tasks new --job`
   dispatches it (the smaller wins) and bounds all attempts together.
   `--max-total-tokens` is recorded and checked between attempts; no backend
   enforces a token cap.

4. **Validate at any time.** `context-layer job validate FILE` prints OK or
   BLOCKED with every reason. BLOCKED covers:
   - a missing or empty mandatory value (a missing value is never inferred);
   - `may_modify_source` not `false`, or a changed authority line;
   - an `owned_output_dir` that holds an indexed source, overlaps an allowed
     root, or sits in a hidden folder other than the job's own;
   - the rule core or input manifest no longer matching the hash the job pins;
   - a different packet id in the manifest, or a packet that is now withheld;
   - an allowed root that escapes the vault, does not exist or is excluded;
   - a `routes.json` that changed since the job was written;
   - a payload over budget.

5. **Dispatch** the job with [`tasks`](tasks.md) (see §4 for the integration
   status), or hand the job path to any agent. The worker reads
   `worker_core.md`, the job and the packet, and nothing else is required.

6. **Check the return mechanically.**
   ```
   context-layer handback check OUT_DIR --job JOB.md [--sample K|auto] [--seed S] [--out FILE] [--json]
   ```
   `receipt.json` must parse, have the schema, stay at or under 160 estimated
   tokens, name a blocker when PARTIAL or BLOCKED, match the job's task and
   attempt, and count the records correctly. `evidence.jsonl` and `receipt.json`
   are sized before they are read: a file over its cap (the job's
   `max_evidence_bytes`), a symlink or a special file is reported, not read.
   Records are split on `\n` only, so a record whose text holds a raw U+2028 is
   still one record. Each record gets `mechanically_checked: true` only if every
   one of these holds:
   - all fields are present and `root_verified` is absent;
   - the path passes the source policy and lies inside `allowed_source_roots`,
     not inside the worker's own output directory;
   - the file exists and its SHA-256 is the current one;
   - `1 <= line_start <= line_end`, at most 21 lines, and the verbatim `span`
     occurs inside those lines, beginning on `line_start` and ending on
     `line_end`. **Line numbers:** a line ends at `\n` (a `\r` before it belongs
     to the ending); form feed, U+2028, U+2029 and U+0085 never end a line. That
     is what `grep -n` counts, and the same model numbers packet passages;
   - the span has at least 3 words or 12 non-space characters;
   - every hard token of the `observation` — a number, a date, a time, quoted
     text, a URL, a code identifier, a name of two or more capitalised words —
     also appears in the span, compared after Unicode (NFC), case, whitespace
     and thousands-separator normalisation. A failure reads
     `observation asserts "480,000"; not in span`;
   - no other record uses the same id.

   A fabricated or paraphrased quote fails, and so does a real quote cited at
   the wrong lines, a one-word span under an invented claim, or a real span
   under an observation that changes its number, date or name. A quote that
   matches only after normalising line endings, Unicode or whitespace still
   fails, but the result names that difference (`normalized_match`) so the
   worker can fix it. A checked record whose observation holds no hard token is
   counted as unanchored: nothing in it could be compared with the span, and
   the summary says so.

   `--sample K` draws K of the checked records for the root to read in full,
   ordered by SHA-256(seed:id). Without `--seed`, the seed is fresh randomness
   drawn at check time, so nothing the worker writes (blank lines, record order,
   ids) can steer which records are drawn; the report prints the seed and the
   SHA-256 of `evidence.jsonl`, and `--seed <that seed>` replays the draw.
   `--sample auto` uses the zero-defect sample size for a 10% defect rate at
   alpha 0.05 (the hypergeometric `C(N-D, n) / C(N, n) <= 0.05`): 9 of 12, 16 of
   20, 25 of 100. `--record` appends the check (evidence hash, seed, sampled ids,
   verdict and a digest of the result) to the verification ledger,
   `.context/tasks/LEDGER.jsonl` (see [tasks](tasks.md)).

7. **The root reads in this order:** receipt → check summary → sampled records
   → the source lines of every claim it will rely on. It then records
   acceptance or rejection in its own file. `mechanically_checked` means the
   quote is really at the cited place in the current file, long enough to
   anchor a claim, and carries every hard token the observation asserts. It
   does not mean the observation is correct (a claim that differs from the span
   only in ordinary words, such as "north" for "south", passes) or that coverage
   is complete.

8. **Hand off.**
   ```
   context-layer handoff write --job JOB.md --state READY|PARTIAL|BLOCKED \
       --done "..." --next "..." --not-established "..." [--check CHECK.json]
   ```
   This writes the minimal control file (`support-handoff/v1`, at most 800
   estimated tokens, otherwise refused). It holds:
   - identity: task, attempt, producer, consumer and predecessors;
   - root goal, worker objective and authority;
   - input hashes, plus whether they are still unchanged, re-validated at write
     time;
   - execution state and blocker;
   - output file hashes;
   - receipt counts and coverage counts;
   - what the work does **not** establish;
   - the handback check result, with the fixed line "Worker completion is not
     root approval";
   - the verification ledger's head line and hash, also printed on the terminal,
     so a copy kept elsewhere anchors the ledger as it stood;
   - the next permitted action.

### Payload budgets

`job new` and `job validate` count each part of the worker's initial payload.
The counts are **estimated tokens, ceil(characters / 4)**, not a tokenizer count.

| Part | Budget | Shipped / typical |
| --- | ---: | ---: |
| worker rule core (`context_layer/data/worker_core.md`) | 1,000 | 856 |
| job (`job.md`) | 750 | about 380–430 |
| input manifest | 250 | about 200–210 |
| evidence packet | 3,000 | depends on `--budget-tokens` (fts, compact) or `--extra-tokens` (synaptic) |
| **total** | **5,000** | |

A payload over budget is refused. `--allow-over` writes the job anyway and
records `payload_over_budget_allowed: true` in it, and validation then shows a
warning instead of BLOCKED. These are initial test budgets, not measured
optima.

### Worker return format

The worker writes only inside `owned_output_dir`:

- `evidence.jsonl`: one record per line with `id`, `observation`,
  `source_path`, `source_sha256`, `line_start`, `line_end`, `span` (verbatim),
  `method` and `uncertainty`. `interpretation` and `relates_to` are optional.
  The worker never sets `root_verified`.
- `coverage.json`: planned, scanned, excluded, failed and unprocessed paths. An
  empty evidence file means "nothing found under this procedure and coverage",
  not "nothing exists".
- `receipt.json` (`support-receipt/v1`, at most 160 estimated tokens), written
  last: task, attempt, state, counts, handoff path and blocker.

## 3. Parallel workers, models and self-grading

- **Disjoint ownership.** Give every parallel worker its own output directory,
  and for code changes its own git worktree and branch. Never let two workers
  edit the same file. The root merges.
- **Share evidence only when workers need the same evidence.** A packet id
  lets N workers read one pinned snapshot instead of each searching again. On
  the synthetic setup below, though, one packet per sub-question was *smaller*
  per worker than one shared packet covering all four. So share a packet when
  the workers genuinely need the same passages, for example a falsification
  check against the passages another worker used. Do not share one by default.
- **Cheap model for mechanical jobs, root for judgement.** Location,
  enumeration and extraction with an exact acceptance test suit a cheaper
  model, because `handback check` catches the typical failures: a quote that is
  not in the source, and an observation asserting a number, date or name its
  quote does not carry. Interpretation, conflict resolution and the final
  answer stay with the root.
- **Never self-grade.** A worker's own "verified" means nothing. So does
  agreement between workers: they can share a model, a prompt and a wrong
  premise. A claim is usable once its evidence and derivation have been
  checked at the source.
- **Do not ask a model whether a file exists.** Read the receipt.

## 4. Relation to `tasks`

`tasks` already gives each sub-agent a bounded packet, fenced output
directories, retries, cost accounting and the `pending_review → verify` state
machine. The orchestration layer adds the lean contract around it:

- shared packets;
- the job file;
- budget accounting;
- evidence-record returns and the mechanical handback check.

The two are wired together by a small patch to `tasks.py`, with the hooks
living in `orchestrate.py`:

- `tasks new VAULT --job JOB.md` dispatches a validated job. A BLOCKED job is
  refused. The goal, the shared packet, the source roots (a root that names a
  file is used as it is; a directory root becomes `root/**/*`), the owned output
  directory, the time limit and the usage cap all come from the job, and the
  prompt carries the worker core and the job file.
- `tasks new VAULT --goal ... --packet ID` delivers a shared packet instead of
  running a fresh search. A withheld packet is refused.
- `tasks verify` rejects a job task if its job file changed, any handback
  record fails the mechanical check, or the run modified an indexed source
  (the job says `may_modify_source: false`).
- The shared packets and the activation trace are left out of the
  unauthorized-write manifest. Both are derived, and packets are re-verified on
  every read.

`tests/test_orchestrate.py` has integration tests for this path
(`TasksIntegration`); they always run.

## 5. Measured: estimated payload tokens on a synthetic setup

`eval/orchestration_cost.py` generates a linked fictional vault with 45 notes
(a hub note, 4 notes that each hold one gold fact, and 40 distractors sharing
the vocabulary), then indexes it. It simulates a coordinator with four workers,
each needing one related fact. The worker returns are **synthesised** from the
evidence each worker received: one record per passage, the gold fact where
present, and one planted fabricated quote. No model runs. The lean packets are
compact synaptic packets (`packet build --method synaptic --compact
--budget-tokens N`), sized by the budgets in the tables; `--packet default`
measures the default synaptic packet (the fts packet plus link-graph extras)
instead.

The block below is the output of `python3 eval/orchestration_cost.py --docs`,
last regenerated on 28 September, after the compact-packet change; `tests/test_live_compare.py` fails when it no
longer matches the eval's `--json` output.

<!-- BEGIN generated by `python3 eval/orchestration_cost.py --docs` -->

```
python3 eval/orchestration_cost.py --standin
```

The rule file a naive worker loads is a synthetic 250-line stand-in: 250 lines, 2,924 estimated tokens.

| Arm (4 workers) | Initial worker payload, est. tokens | vs naive | Gold fact in payload |
| --- | ---: | ---: | ---: |
| naive: full rule file + objective + full text of the 3 files its own FTS search surfaced | 16,827 | 100% | 4/4 |
| lean-own: core + job + manifest + a compact synaptic packet for its own sub-question (budget 1,200) | 9,441 | 56% | 4/4 |
| lean-shared: core + job + manifest + one shared compact synaptic packet (budget 2,000; 10 passages, 1,419 est. tokens), counted once per worker | 11,626 | 69% | 4/4 |

| Root verification reading | est. tokens |
| --- | ---: |
| naive: 4 full reports + full text of every cited source | 12,452 |
| lean: receipts + `handback check` summary + 2 sampled records per worker with their cited lines + 1 critical record per worker with its lines | 2,580 |

`handback check` caught 1 of the 1 planted fabricated record(s).

```
python3 eval/orchestration_cost.py --rules templates/vault/CLAUDE.md
```

This run uses the vault rule template: 173 lines, 2,063 estimated tokens.

| Arm | Initial worker payload, est. tokens | vs naive |
| --- | ---: | ---: |
| naive | 13,383 | 100% |
| lean-own | 9,441 | 71% |
| lean-shared | 11,626 | 87% |

Verification reading is the same as above (12,452 vs 2,580).

The fixed part of every lean payload is the worker core (856 est. tokens), the job (about 424) and the input manifest (about 207): about 1,487 est. tokens per worker.

```
python3 eval/orchestration_cost.py --standin --packet default
```

With the default synaptic packet (the fts packet plus link-graph extras, not sized to a token budget) the stand-in run gives lean-own 11,658 and lean-shared 14,614 est. tokens against naive 16,827; the shared packet then holds 11 passages, 2,166 est. tokens, with 4 of the 4 gold facts.

<!-- END generated -->

How to read these tables:

- They are **estimated payload tokens (ceil(chars/4)) on a synthetic setup**.
  They are not billed host tokens, which also include system prompts, tool
  schemas, history and output.
- They are **not evidence about answer quality**. "Gold fact in payload" only
  says the needed sentence was present. It says nothing about whether a worker
  would use it.
- The output is deterministic: the test runs `--json` twice and requires
  byte-identical output.
- The lean saving on startup comes mostly from replacing the full rule file
  with the worker core, and whole files with budgeted passages. The fixed core
  + job + manifest stated above is now the largest share of a lean payload.
- The compact packets are the smaller ones on this setup; the default synaptic
  packet (last paragraph of the block) keeps every fts passage and is not sized
  to a token budget, so it costs more per worker here.
- The verification saving assumes a root that would otherwise open every cited
  source in full to confirm quotes. A root that already trusts quotes spends
  less in the naive arm, and loses the fabrication check that comes with it.
- The naive report is the same records rendered as Markdown with no extra
  prose. Real prose reports are usually longer, so this baseline is
  conservative.

## 6. Limits

- `est_tokens` is `ceil(characters / 4)`. It is not a tokenizer count, and
  code, CJK text and hashes can differ a lot from it.
- A hash proves equality with a recorded artifact. It does not prove truth,
  authenticity or complete coverage. A packet id is a content address, not a
  signature. Forged packet content is caught only because `read_packet`
  re-checks every passage verbatim against the current source.
- The job, the manifest and the packet list sources, but they cannot show that
  the source universe is complete. When coverage matters, the root has to
  establish it.
- A mechanically checked record can still be irrelevant, or wrong in its
  observation. The claim-span gate compares only hard tokens: on the synthetic
  100-record set in `tests/test_orchestrate.py` (50 genuine records, 50 with one
  detail swapped) it caught all 45 swaps of a number, date, time, quote, URL,
  identifier or name and none of the 5 swaps of ordinary words, and it rejected
  5 of the 50 genuine records, all of them paraphrases that restate a number,
  date or name in another form ("48 thousand" for "48,000", "11 weeks" for
  "eleven weeks", an ISO date for "12 March", a title for a first name, 0.87 for
  87%). Those rates describe that set, which was written to include both kinds
  of case, not real worker output. Print them with
  `python3 -c "import sys; sys.path.insert(0, 'tests'); import test_orchestrate
  as t; print(t.claim_gate_rates())"`.
- Sampling is process control for low-risk candidates, never a release decision
  for critical claims.
- Job files and returns are plain files. Nothing here enforces that a worker
  writes only its own directory. `tasks` detects such writes by hash diff but
  does not prevent them, and neither does this layer.
- The verification ledger is a hash chain in the same account the worker runs
  in: an edit shows as a broken chain, but a process that rewrites the whole
  file consistently is caught only by a head hash kept elsewhere.
- Budgets are the initial test values from the design notes, not tuned optima.
