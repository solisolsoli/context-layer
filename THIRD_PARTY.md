# Third-party components and notices

Everything this package distributes, where it came from, under what licence,
what that licence asks a redistributor to do, and where in the distribution the
notice actually sits. Checked by building the wheel and the source
distribution with setuptools 77+ and listing their contents; the commands that
reproduce the check are at the bottom. `<version>` below is the package version
being built.

**MIT is a licence, not an absence of copyright.** Every MIT component named
below is someone's copyrighted work, offered on terms that require its copyright
line and permission text to travel with every copy and substantial portion.
Dropping that notice does not make the code public domain — it makes the copy
unlicensed. That is why the upstream Avenox notice is kept verbatim next to the
patches instead of being folded into this project's own licence.

## Inventory

| Component | Origin | Licence | Notice obligation | Where the notice is |
| --- | --- | --- | --- | --- |
| `context_layer/`, `router/`, `eval/`, `scripts/`, `tests/`, `docs/`, `discipline/`, `.github/`, the benchmark's code and records in `bench/` (`*.py`, `cases.jsonl`, `judge_template.jsonl`, `SEAL.md`, `vault.sha256`, `results/`; its fictional vault is in the fixtures row below), `Makefile`, `pyproject.toml`, `MANIFEST.in`, `CITATION.cff`, the Markdown files at the repository root | Written for this project | MIT, copyright 2026 solisolsoli | Keep the copyright line and the permission text with every copy | [`LICENSE`](LICENSE); in the wheel at `context_layer-<version>.dist-info/licenses/LICENSE`; `License-Expression: MIT` in the package metadata (PEP 639) |
| `retrieval-patches/01-ranking.patch`, `02-cache.patch`, `03-strict-source-hash.patch` | Unified diffs against `template/.claude/scripts/beyin_v3.py` in [avenoxai/avenoxbeyin](https://github.com/avenoxai/avenoxbeyin), tag `v3.0.1`, commit `61a88467d748fbf94ebc340cb932da7c7e9a78b7`. Diff hunks carry verbatim context lines, so these files are partial copies of upstream source, not only this project's edits. | MIT, copyright 2026 Avenox | Keep Avenox's copyright line and permission text with the excerpted material | [`retrieval-patches/LICENSE.upstream.txt`](retrieval-patches/LICENSE.upstream.txt), verbatim, beside the patches and named from [`retrieval-patches/README.md`](retrieval-patches/README.md) and `retrieval-patches/codex-chain.md`. The **patches ship in the source distribution only**; the wheel contains no patch and no Avenox code. The notice file itself is listed in `license-files`, so it is also copied into the wheel at `context_layer-<version>.dist-info/licenses/retrieval-patches/LICENSE.upstream.txt` and named by a `License-File:` line in the wheel's `METADATA`. That copy is harmless (it covers nothing the wheel carries) and keeps the notice with every artefact built from this tree. |
| `retrieval-patches/hook-visible-error.example.sh` | Written for this project. A lab example of a host wrapper that reports a helper failure instead of injecting nothing; installing this package neither registers nor runs it. | MIT, copyright 2026 solisolsoli | Covered by this project's notice | [`LICENSE`](LICENSE). Source distribution only. |
| `router/example-vault/**`, `eval/fixtures/docs/*.md`, `eval/comparison/*`, `bench/vault/**`, `templates/**` | Synthetic fixture documents, the fictional benchmark vault and the starter-brain templates, written for this project. The content is fictional: no real organisation, person, note or vault. | MIT, copyright 2026 solisolsoli | Covered by this project's notice | [`LICENSE`](LICENSE) |
| `obsidian-plugin/` (Context Layer Brain View) | Written for this project. Plain JavaScript with no npm dependencies, built by a script in the same directory. Small, well-known techniques are credited in its source and in [`obsidian-plugin/THIRD_PARTY.md`](obsidian-plugin/THIRD_PARTY.md): perspective and look-at matrices written from the textbook definitions (the gl-matrix conventions, credited as the common reference), the mulberry32 PRNG (CC0), the FNV-1a hash (public domain), and Gaussian blur weights after Rakos (2010). | MIT, copyright 2026 solisolsoli | Covered by this project's notice; the credited snippets are CC0 or public domain, or re-implemented rather than copied | [`LICENSE`](LICENSE); the repository also keeps the same text at [`obsidian-plugin/LICENSE`](obsidian-plugin/LICENSE) for anyone who copies the plugin folder on its own. The plugin is not part of the Python wheel; the source distribution carries its sources and tests but not the built `dist/main.js`. |
| Python standard library — `sqlite3` (FTS5), `argparse`, `hashlib`, `json`, `difflib`, `fcntl`, `subprocess`, `venv` and the rest | CPython, supplied by whoever installs Python | PSF Licence Agreement for the standard library; SQLite itself is public domain and is built into CPython | None falls on this package | **Not distributed here.** It is a runtime requirement (`requires-python = ">=3.10"`), never vendored or copied. Its notices ship with the reader's own Python. |
| `setuptools>=77` | Named in `[build-system].requires`; the builder fetches it. 77 is the first release that accepts the PEP 639 `license = "MIT"` string and `license-files` this project uses. | MIT | None falls on this package | **Not distributed here.** No setuptools code is inside the wheel or the sdist; only the files it generated (`PKG-INFO`, `*.egg-info/`, `dist-info/RECORD`, `WHEEL`, `METADATA`, `entry_points.txt`, `dist-info/licenses/`). |
| `claude`, `codex`, or any `--cmd` binary used as a sub-agent backend | Already on the user's machine | Their own | None falls on this package | **Not distributed here.** `context-layer tasks` spawns a program the user already installed; see [`docs/tasks.md`](docs/tasks.md). |
| Model providers the optional advisor can call: TypeSafe and OpenRouter (hosted services), Laya (an Apache-2.0 server the user runs), Ollama, llama.cpp, LM Studio and vLLM (servers the user runs), Claude Code (the user's own CLI) | Services and programs the user chooses and configures; nothing is contacted unless the user enables the advisor ([`docs/jev.md`](docs/jev.md)) | Their own terms and licences; a hosted service's terms and retention apply to what is sent to it, under the user's own key or login | None falls on this package | **Not distributed here.** No client library, model, weights or key of any of them is included. "Jev" and "TypeSafe" are named only to identify a compatible service; no affiliation is claimed. The design credit for the advisor is in [`CREDITS.md`](CREDITS.md). |

## Runtime dependencies: none

`dependencies = []` in `pyproject.toml`, deliberately. Installing this package
downloads nothing beyond the package itself — `pip install --no-index --no-deps`
is enough, and that is how `scripts/check_distribution.py` installs it. Nothing
in the package makes a model API call or opens a network connection unless a
person enables the optional advisor ([`docs/jev.md`](docs/jev.md)); its provider
client, `context_layer/jev_client.py`, is the only module that can.

## What is deliberately not distributed

- **No vault.** No personal notes, no real Markdown corpus. Every document in
  the wheel and the sdist is a synthetic fixture written for the tests.
- **No prompts, packets, transcripts or evaluation runs** from a real session
  of anyone's own notes. The recorded comparison in `eval/comparison/` is the
  synthetic 24-case corpus. The one set of live-host run data in the
  repository, `eval/live-pilot-0.3/`, is the maintainer's own 0.3 pilot run
  against the fictional benchmark vault (`bench/vault`): the question text,
  the host's answers, blind-judging verdicts and token and cost counts, with
  fictional names only and no personal content. It lives in the git
  repository only; `MANIFEST.in` prunes it, so neither the wheel nor the sdist
  carries it.
- **No host settings.** The wheel carries no `.mcp.json`, no
  `.claude/settings.json`, no `~/.codex/config.toml` and no hook registration.
  `context-layer install` writes such a file only when a person runs it with
  `--apply`, and `uninstall` removes it again.
- **No derived state.** `MANIFEST.in` excludes `*.sqlite`, `*.sqlite.prev`,
  `index-manifest.json` and run output, so no index, memory record or task
  directory can travel with a release.
- **No personal paths, accounts, machine names or e-mail addresses.** The only
  personal identifier in the tracked tree is `solisolsoli` as the copyright
  holder and `authors` entry. A grep over every tracked file for home-directory
  prefixes, the maintainer's account name, `Desktop` and e-mail addresses (the
  commands below) finds no real address or account. It does list deliberate,
  harmless lines: the string `"/Users/"` inside a test that asserts `status`
  never prints one (`tests/test_health.py`), the audit commands and patterns
  themselves (this file, `CONTRIBUTING.md`, `scripts/audit_history.py`), the
  placeholder path `/home/me/vault` in `tests/test_release_tooling.py` and
  `eval/adapters/README.md` (the made-up user path that test also needs is
  assembled at run time, so no file spells it), and
  `isDesktopOnly` in the Obsidian plugin. The e-mail-shaped hits are fixtures
  with reserved domains (`example.com`, `example.org`, `*.test`,
  URL userinfo cases) in `tests/test_release_tooling.py`, `tests/test_jev.py`
  and `tests/test_jev_client.py`. The installer names the Codex config only as
  the `$HOME/.codex/` placeholder.
- **No upstream engine.** The patches are diffs. Nobody gets a runnable copy of
  `beyin_v3.py` from this package; applying them means fetching the pinned
  upstream file yourself, as [`retrieval-patches/README.md`](retrieval-patches/README.md)
  describes.

## Checking this yourself

```sh
python3 -m build
python3 -c "import zipfile,sys; print('\n'.join(sorted(zipfile.ZipFile(sys.argv[1]).namelist())))" \
  dist/context_layer-*-py3-none-any.whl | grep -E 'licenses/|patch'
python3 -c "import tarfile,sys; print('\n'.join(sorted(tarfile.open(sys.argv[1]).getnames())))" \
  dist/context_layer-*.tar.gz
git ls-files -z | xargs -0 grep -nE "/Users/|/home/[a-z]|Desktop|$(whoami)"
git ls-files -z | xargs -0 grep -nE "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
python3 scripts/check_distribution.py dist/context_layer-*-py3-none-any.whl
```

`check_distribution.py` asserts that the notice files above are present in the
source distribution, so a release that dropped one fails the check instead of
shipping quietly.
