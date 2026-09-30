# Contributing

Contributions are welcome under the project's MIT licence: by opening a pull
request you agree that your contribution is licensed under it. There is no CLA.
Fixes that come with a synthetic reproducer are the easiest to accept; new
features, new dependencies and architecture changes need agreement in an issue
before a pull request. The maintainers may decline a change that does not fit
the project's scope ([SCOPE.md](SCOPE.md)). If you use an AI assistant, you are
still responsible for understanding and testing every line you submit.

Security problems go through private reporting, not issues: see
[SECURITY.md](SECURITY.md). Behaviour in project spaces follows
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Issues are triaged in this order:
security, then false or unverified evidence, then regressions, then features.

Everything in the repository is written in English: code, comments, docs,
fixtures, test strings and commit messages.

Use synthetic fixtures or public sources. Preserve verbatim source bytes and record hashes. Add tests for both positive and negative evidence delivery, including abstention and operational failure. Keep labels independent of candidate output. Do not add private-vault content, private hooks, hosted services, or unmeasured performance claims. Run the relevant local checks and report any check not run.

## The suites

`make test` runs every regression in order; the same command is what hosted CI
runs. Each file is also runnable on its own with `python3 tests/<file>.py` (the
router suite: `python3 router/build_index.py --vault router/example-vault`, then
`python3 router/test_context_router.py`). The code and the suites read and
write UTF-8: run them in UTF-8 mode or under a UTF-8 locale. `make` and hosted
CI set `PYTHONUTF8=1`; under a Latin-1 locale without it, some suites fail (`tests/test_integrity.py`, for one).

| Suite | What it covers |
| --- | --- |
| `router/test_context_router.py` | The experimental router, against `router/example-vault` |
| `tests/test_vault_scan.py` | `init`: what the scanner infers from a vault |
| `tests/test_integrity.py` | Index/packet integrity and byte preservation |
| `tests/test_evidence_contract.py` | Frozen source identity and required passages |
| `tests/test_boundaries.py` | Exclusions, path escapes, symlink refusal |
| `tests/test_comparison.py` | The synthetic 24-case comparison |
| `tests/test_eval_adapters.py` | The evaluation adapters: an index never answers for another vault; vectors cached per model |
| `tests/test_release_tooling.py` | `check_distribution.py`'s content rules, the sdist normaliser, the history audit |
| `tests/test_doc_claims.py` | Every number the README and `docs/` quote from a result file or a measuring command, recomputed; `make demo`; `AGENTS.md` = `CLAUDE.md`; every `search` flag documented |
| `tests/test_docs_links.py` | Every relative link, its letter case and its anchor, in every tracked Markdown file |
| `tests/test_mcp_install.py` | The MCP server over real pipes, the installers, the hook |
| `tests/test_memory.py` | Records, ids, chain, drift, resume, concurrent appends |
| `tests/test_tasks.py` | Task spec, packet bounds, backends, verification, cost, cancel |
| `tests/test_health.py` | `status`, the index manifest, `rollback`, the hook example |
| `tests/test_rules.py` | `rules`: identical `CLAUDE.md`/`AGENTS.md`, `record`, the rule hooks and settings |
| `tests/test_brain.py` | `brain init`: the starter vault layouts |
| `tests/test_orchestrate.py` | Shared packets, `job` contracts, payload budgets, `handback check` |
| `tests/test_live_compare.py` | The live before/after harness |
| `tests/test_harden.py` | 0.3.0 hardening: config loading, index integrity, error packets, first-run messages, hook framing, format versions |
| `tests/test_doctor.py` | `doctor`: offline read-only host and vault checks |
| `tests/test_brief.py` | `brief`: the evidence-pinned session briefing |
| `tests/test_session_evidence.py` | The delivered-evidence ledger and the citation check |
| `tests/test_jev_contracts.py` | The advisor's templates, answer validation and confidence formula (no I/O) |
| `tests/test_jev_client.py` | The advisor's providers and transport bounds, against loopback fakes and a fake CLI |
| `tests/test_jev.py` | The advisor's invariants: inert when off, shadow changes nothing, `on` only appends, fail to local, privacy gates |
| `tests/test_jev_answer.py` | The advisor's answer checks: `jev answer`, `handback check --jev` (notes only, never a pass), MCP `check_claims` |
| `tests/test_jev_memory.py` | The advisor's memory review (store only read) and the loopback-only `jev status --check` probe |
| `tests/test_dev_jev.py` | The advisor's development set: deterministic, self-checked, pinned by hash; `JEV_DEV_E2E=1` also runs the oracle recording chain (record, replay, calibrate, `on`) |
| `tests/test_synapse.py` | Synaptic retrieval: link extraction, activation, packing, trace, host tools |
| `tests/test_incremental_index.py` | Incremental indexing equals a full rebuild after randomized edits; golden digests of the default packets |
| `tests/test_name_fields.py` | The opt-in name, alias and heading fields (`index --name-fields`, `search --name-fields`) |
| `tests/test_coactivation.py` | The opt-in usage ledger and `graph suggest`: bounded, never read by retrieval |
| `tests/test_session_show.py` | `session show` and `session list`: one session's joined report, read-only |
| `tests/test_encoding.py` | Runtime text I/O names its encoding (an `ast` scan) |
| `tests/test_write_boundary.py` | Read paths (`search`, the read-only MCP tools, the hook) write nothing but `activation.json` |

`tests/dev_bridge_eval.py` is a development aid (the synaptic dev set), not a
suite, and `make test` does not run it. Outside `make test`: `make plugin-test`
runs the plugin's Node tests (`obsidian-plugin/tests/`), and `make bench-check`
runs the sealed benchmark's own checks (`bench/seal.py check`, the scorer tests
in `bench/test_scorer.py`, and `bench/test_runner.py`, which runs two sealed
cases at a tiny budget to check the runner's plumbing, never completeness).

`tests/test_doc_claims.py` is the reason a number in the docs must come from a
command: change a result or a measuring script, and it names each number that
no longer matches. Nothing in it is skipped because a doc is known to be stale. With
`DOC_CLAIMS_BENCH_DIR` pointing at fresh benchmark runs it also checks the
README's benchmark table against them, as the `bench-reproduce` CI job does.

Never tune retrieval against the sealed cases (`bench/cases.jsonl`): develop on
a dev set, and add a row to [bench/INSPECTIONS.md](bench/INSPECTIONS.md) before
relying on any new run of the sealed set.

Every suite owns a disposable synthetic vault and redirects `HOME` into its own
temporary directory. No test may read the operator's vault, home or host
settings, and no test may spawn a real model backend: the sub-agent tests drive
the `fake` backend with a script they write themselves.

`make lint` byte-compiles every Python file under `context_layer/`,
`router/`, `eval/`, `tests/`, `scripts/` and `bench/`, and
`obsidian-plugin/build.py`, and runs `pyflakes` on them when it is installed
(`pip install pyflakes`); under CI (`CI` set) a missing `pyflakes` fails the
target instead of being skipped. `make demo` runs init + index + one packet on a
throwaway copy of the fixture vault. `make plugin` builds the Obsidian plugin,
`make plugin-test` runs its Node tests, `make bench` runs the offline retrieval
benchmark, and `make dist` builds the wheel and a reproducible sdist.
`PYTHON` may be a command on `PATH` or a path to an interpreter, relative or
absolute (`make test PYTHON=.venv/bin/python`).

Hosted CI (`.github/workflows/tests.yml`, actions pinned by commit) runs 12
required jobs. The Ubuntu matrix (Python 3.10–3.13) and macOS job (Python 3.12)
run `make test`, `make demo`, lint, the network boundary check, build and the
distribution check. The Windows job (Python 3.12) runs the fixture router checks,
build and installed-distribution walk before all unit suites, benchmark checks,
lint and the network boundary check. Ubuntu jobs also cover the plugin's build
and Node tests (Node 24), the sealed benchmark, `bench-reproduce` (fresh benchmark
and README numbers), `doc-claims`, the Jev recording-to-calibration chain, and
`reproducible build` (two builds, one SHA-256).
The [hosted run for `4abc25e`](https://github.com/solisolsoli/context-layer/actions/runs/36763000857)
passed all 12 jobs. This is package and platform verification; a live AI host
session and model answer quality require separate evidence.
`.github/workflows/release-audit.yml` runs the history audit on every `v*` tag.
See [docs/publishing-checklist.md](docs/publishing-checklist.md).

## The distribution check

```sh
python3 -m pip install build
make dist                                # or: python3 -m build
python3 scripts/check_distribution.py dist/context_layer-*.whl
```

The project declares its licence in PEP 639 form (`license = "MIT"` plus
`license-files`), which needs setuptools 77 or newer; `[build-system].requires`
pins `setuptools==84.0.0`, so builds on different days use the same backend,
and `python -m build` fetches it into an isolated environment. `make dist`
builds with `SOURCE_DATE_EPOCH` set to the last commit's time and rewrites the
sdist with `scripts/normalize_sdist.py` (sorted members, `root/root` owners, one
timestamp): two builds of one commit then give the same sha256, and no local
account name ends up in the tar headers.

`check_distribution.py` builds nothing itself; it takes the wheel and the sdist
of the same version beside it (a missing sdist fails the check, unless
`--no-sdist` says that only the wheel is being checked). In one throwaway
directory it (with both POSIX and Windows home variables isolated):

1. creates a fresh virtual environment **outside the checkout**, installs the
   wheel with `--no-index --no-deps`, and asserts the imported package really
   comes from that environment;
2. runs `init`, `index`, `search`, `route`, the bundled evaluation harness, the
   installed `eval` CLI and the comparison, checking the delivered evidence
   against the source bytes and their SHA-256;
3. checks that the sdist still carries `LICENSE`, the retained upstream notice,
   the component documentation and no derived index state;
4. walks the 0.2 surface: `status` going ok → stale → ok around an added source,
   `memory add/resume/verify` including the duplicate-record case, one bounded
   task on the `fake` backend from `new` through `run` to `verify`, `install
   generic` / `install claude-code --hook --apply` / `uninstall --apply` in a
   temporary project directory, and `rollback` with its dry run;
5. walks the 0.3 surface on fresh vaults: `brain init --apply` and `rules
   check`; `index` and `search --method synaptic` over an explicit link, with
   the shape of `.context/activation.json`; `packet build`, `job new` and
   `handback check`, which must catch a planted fabricated quote; the MCP
   server's `initialize` and `tools/list` over pipes; the Claude Code prompt
   hook on stdin;
6. reinstalls the same wheel with `--force-reinstall` and checks that the
   vault's `.context` state — index, manifest, memory records, tasks — is still
   readable by `status`, `memory verify`, `memory resume`, `tasks list` and
   `search`.

Every child process runs with `HOME` inside the temporary directory, and the
check fails if anything wrote there. It makes no network calls; installing the
build tools may require network access. Hosted CI runs this script after
`python -m build` and does not publish a package. A local pass is not a hosted
CI pass; say which one you ran.

## Before proposing a release

- Bump the version in `pyproject.toml` and `context_layer/__init__.py` together.
- Add the entry to `CHANGELOG.md`.
- Update `THIRD_PARTY.md` if the set of distributed components changed — a new
  vendored file, a new patch, or anything whose licence asks for a notice.
- Re-read the known limitations in `SECURITY.md` and `docs/privacy.md`; a
  release must not ship a limitation that is no longer disclosed, or a
  disclosure that is no longer true.
- Check that everything is in English. This lists every non-ASCII letter in
  every tracked text file (typography such as dashes and quotes is not a
  letter, so it is not listed):

  ```sh
  git grep -n -I -P '(?=[^\x00-\x7F])\p{L}' | less
  ```

  The expected hits are exactly the files and letters in `ALLOWED_LETTERS` of
  `scripts/audit_history.py` (`tests/test_release_tooling.py` fails when the list
  and the tree disagree): the case-folding examples (sharp s and dotted capital
  I, U+0130) in `CHANGELOG.md`, `docs/cli.md` and `router/textfold.py`; the
  dotted capital I in `context_layer/synapse.py`, `tests/test_harden.py` and
  `tests/test_synapse.py`; byte-preservation and Unicode file-name fixtures in
  `tests/test_boundaries.py`, `tests/test_memory.py`,
  `tests/test_session_evidence.py`, `tests/test_incremental_index.py` and
  `tests/test_synapse.py`; the tokenizer fixtures in `tests/test_integrity.py`
  (ligatures, full-width forms, Greek, Hangul); and author names in the
  references of `docs/design-rationale.md`. Non-ASCII is fine in deliberate
  fixtures and names; anything that is prose in another language is not.
- Audit the history, not only the tracked files: `python3
  scripts/audit_history.py` checks every blob of every commit and every commit
  message (letters, home paths, e-mail addresses, private-trace digests), with
  the same allowlist. See step 1b of
  [docs/publishing-checklist.md](docs/publishing-checklist.md).
- Re-run the leak audit over tracked files and fix anything it finds:

  ```sh
  git ls-files -z | xargs -0 grep -nE "/Users/|/home/[a-z]|C:\\\\Users|Desktop|$(whoami)"
  git ls-files -z | xargs -0 grep -nE "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
  ```

  The expected hits are the `"/Users/"` literal in `tests/test_health.py`, which
  asserts that `status` never prints an absolute path; the audit commands
  themselves in this file and `THIRD_PARTY.md`; the `/home/me/vault` placeholder
  in `eval/adapters/README.md`; and `isDesktopOnly` in the Obsidian plugin's
  manifest, README and tests; the placeholder paths quoted by `scripts/audit_history.py`
  and its fixtures in `tests/test_release_tooling.py`. The e-mail audit's expected hits are
  placeholder addresses in test fixtures only (`@example.com`, `@example.org`, `.test`
  domains, and URL userinfo cases in `tests/test_jev.py` and `tests/test_jev_client.py`). Anything
  else is a leak.

- Keep `AGENTS.md` and `CLAUDE.md` byte-identical: `diff AGENTS.md CLAUDE.md`
  must print nothing.
- Mark a platform as supported only after it has been run. Anything else is
  "expected but unverified", in exactly those words, as `SCOPE.md` uses them.

Publishing the repository itself for the first time follows
[docs/publishing-checklist.md](docs/publishing-checklist.md).
