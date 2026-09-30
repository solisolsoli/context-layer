# Publishing checklist (first GitHub release)

The steps the maintainer follows to publish this repository on GitHub for the
first time and release 0.4.0. They are written without a repository address,
because the address is chosen in step 2. Two placeholders stand for it:

- `<REMOTE>`: the clone address GitHub shows for the new repository (SSH or
  HTTPS), used by `git remote add`;
- `<REPO_URL>`: the repository's web address, without a trailing slash, used in
  the files filled in at step 7.

Nothing in this checklist publishes a Python package; see step 9.

## 1. Pre-flight, on the local `main`

Run these from a clean checkout of `main` and do not continue past a failure:

```sh
git status --short                      # must print nothing
make test
pip install pyflakes==4.0.0 && make lint
make demo
python3 obsidian-plugin/build.py --check && make plugin-test   # needs Node 24
make bench-check                        # seal, scorer tests, runner tests
python3 bench/run_offline.py --out /tmp/bench-check
diff <(grep -v '^Checkout: ' bench/results/SUMMARY.md) <(grep -v '^Checkout: ' /tmp/bench-check/SUMMARY.md)
python3 -m pip install build==1.6.1 && make dist   # wheel + reproducible sdist
python3 scripts/check_distribution.py dist/context_layer-*.whl
diff AGENTS.md CLAUDE.md                # must print nothing
```

Then the English and leak audits from
[CONTRIBUTING.md](../CONTRIBUTING.md#before-proposing-a-release) over the
tracked files.

## 1b. Audit the history, and record the decision about it

The first push publishes every commit, and every file version in them, not
only the files at the tip. Before the push, delete the local branches and
worktrees that will not be published (`git worktree list`, `git branch`), then
run, from the checkout:

```sh
python3 scripts/audit_history.py        # every blob of every commit and every commit message
git log --all --format='%(trailers:only,unfold)' | sort | uniq -c   # trailers that become public
git log --all --format='%an <%ae>%n%cn <%ce>' | sort -u              # identities
```

- `audit_history.py` reports non-English letters outside the fixtures its
  allowlist names (`ALLOWED_LETTERS`, matched to the tracked tree), home-directory
  paths other than the documented placeholder names, e-mail addresses other
  than no-reply ones and the reserved placeholder domains (`example.com`,
  `example.org`, `example.net` and their subdomains, and the `.test`,
  `.example`, `.invalid` and `.localhost` top-level domains), and the
  private-trace digests `tests/test_harden.py` guards the tracked files
  against, in any historical blob or commit message. It exits 1 on any finding
  not accepted with `--accept <blob>`. The release workflow
  (`.github/workflows/release-audit.yml`) runs it again on every `v*` tag, over
  the commit the tag points at (`--rev HEAD`).
- The identities must be only the project identity (`solisolsoli` with its
  GitHub `noreply` address).
- The trailers (`Co-Authored-By: ...`) name the assistants that co-wrote
  commits; they become public with the history.

The tracked files of this tree pass the audit: `tests/test_release_tooling.py`
copies them into a scratch repository as one commit and runs
`audit_history.py --rev HEAD` over it (a commit message of the owner's own is
audited when the real commit exists: run the script once more after
committing). The history of the development branch does not pass: on
2026-09-29 `audit_history.py --rev HEAD` over its 260 commits reported 2
home-path, 58 non-ascii and 5 private-trace findings in blobs and commit
messages of earlier commits, and `git log` counted 181 `Co-Authored-By`
trailers. A pushed
history cannot be taken back, so the owner decides before step 3 and
records the decision here, in the same commit that carries it out:

```text
History decision (owner): [ ] publish one squashed commit and keep the full
                              history in a local bundle
                              (git bundle create ../context-layer-full.bundle --all)
                          [ ] publish the history as it is; every remaining
                              finding accepted with --accept, listed below
Trailers:                 [ ] keep   [ ] drop (only with a rewrite)
Accepted blobs, if any:   ...
Decided on:               ....-..-..
```

Until this block is filled in, do not continue past this step.

## 2. Create the repository

On GitHub, create a new **public** repository under the owner that will hold
the project.

- **Do not** let GitHub add a README, a `.gitignore` or a licence. The repository
  must start empty; any of those creates a first commit that the local history
  does not have, and the push in step 3 is then rejected.
- **Description** (the "About" box; at most 350 characters). Reuse the package
  description from `pyproject.toml`:

  > Verbatim, hash-pinned evidence from a Markdown or Obsidian vault for AI
  > agents: FTS search plus explicit-link (synaptic) retrieval, MCP server and
  > Claude Code hook, an Obsidian Brain View of each retrieval, a starter
  > brain with enforceable rule files, append-only memory, bounded sub-agent
  > tasks and a sealed benchmark. Standard library only.

- **Topics:** `obsidian`, `markdown`, `retrieval`, `mcp`, `claude-code`,
  `evidence`, `knowledge-base`.
- Website: leave empty.

## 3. Push `main`

```sh
git remote add origin <REMOTE>
git push -u origin main
```

Check on GitHub that the default branch is `main` and that the file list
matches `git ls-files`.

## 4. Repository settings

Do these right after the first push, before telling anyone about the
repository.

- **Private vulnerability reporting: enable it.** Settings → the code security
  page (GitHub has named it "Code security and analysis", "Code security" and
  "Advanced Security") → Private vulnerability reporting → Enable. [SECURITY.md](../SECURITY.md) and
  [CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md) both send reporters to the
  **Security** tab → **Report a vulnerability**; that button exists only while
  this setting is on. Check it by opening the Security tab while signed out, or
  from an account without write access.
- **Actions permissions: read-only token.** Settings → Actions → General →
  Workflow permissions → "Read repository contents and packages permissions",
  and leave "Allow GitHub Actions to create and approve pull requests" off. The
  workflow already declares `permissions: contents: read`; the repository
  default keeps any future workflow that forgets to declare it at the same
  level. Keep the default approval requirement for workflow runs from
  first-time contributors' forks.
- **Features:** Issues on. The two issue templates in `.github/ISSUE_TEMPLATE/`
  and the pull request template apply automatically. The wiki is not used (the
  documentation lives in the repository); turning it off avoids a second place for docs
  that pull requests and CI do not cover.
- **Branch protection for `main`** (suggested; Settings → Rules → Rulesets, or
  Branches → Add rule): require a pull request before merging, require the
  status checks below to pass, block force pushes and deletion. The checks can
  only be selected after they have run once (step 5):
  - `python 3.10 on ubuntu-latest`, `python 3.11 on ubuntu-latest`,
    `python 3.12 on ubuntu-latest`, `python 3.13 on ubuntu-latest`,
    `python 3.12 on macos-latest`;
  - `obsidian plugin (node 24)`;
  - `sealed benchmark (offline)`;
  - `bench-reproduce` (reruns the benchmark and the README's numbers);
  - `doc-claims` (documented numbers and relative links);
  - `reproducible build` (two builds, one sha256).

  Do not select `windows smoke (non-blocking)`: it runs with
  `continue-on-error` to learn what breaks on Windows, which stays out of
  scope until it has passed twice.

## 5. Hosted CI must go green before anything is announced

The push in step 3 starts the `tests` workflow (`.github/workflows/tests.yml`).
Open the Actions tab and wait for every job listed above to pass. The actions
it uses are pinned by commit; `.github/dependabot.yml` proposes updates.

- If a job fails, fix it with a normal commit on `main` and push again. Do not
  tag a commit whose run is red.
- A local pass is not a hosted pass. Until this run is green, the sentence
  "Hosted CI has not run for this release candidate" in the README stays true
  and stays in.
- Once it is green, update what the run changes, in one commit: the README
  sentence above, the "(CI matrix, not yet run)" labels in the README support
  table, and the matching sentence in
  [CONTRIBUTING.md](../CONTRIBUTING.md#the-suites) (say that hosted CI passed and
  on which commit). The support matrix in the README and in
  [SCOPE.md](../SCOPE.md) moves a platform to "tested" only if you decide that a
  green test-suite run on it is enough; otherwise it stays "expected but
  unverified". A CI run is not a live host run.

## 6. Tag `v0.4.0` and push the tag

Tag the commit whose run is green (normally the tip of `main`), as an annotated
tag:

```sh
git tag -a v0.4.0 -m "context-layer 0.4.0"
git push origin v0.4.0
```

The tag push starts the `release audit` workflow (the history audit of step
1b); it must be green before a GitHub release is created from the tag.

The version must already read 0.4.0 in `pyproject.toml`,
`context_layer/__init__.py` (`context-layer --version`),
`obsidian-plugin/manifest.json` and the top entry of `CHANGELOG.md`; the
pre-flight checks do not compare them, so look.

Optionally, create a GitHub release from the tag with the `CHANGELOG.md` 0.4.0
entry as its text. If you attach `obsidian-plugin/dist/main.js` (as
`main.js`), `obsidian-plugin/manifest.json` and `obsidian-plugin/styles.css`,
the manual and BRAT-style installs described in
[obsidian-plugin/README.md](../obsidian-plugin/README.md) can use the release
assets.

Never attach a wheel or sdist built with a plain `python -m build`: setuptools
writes the builder's local user and group names into every tar header of the
sdist, and its bytes change with every build. Build with `make dist`
(`SOURCE_DATE_EPOCH` from the tagged commit, then `scripts/normalize_sdist.py`:
root/root owners, one timestamp, sorted members), and check with
`tar -tvzf dist/context_layer-*.tar.gz | head` that the owner column reads
`root/root`. The `reproducible build` job shows that two such builds match.

## 7. Fill in what needs the repository address

These are left out of the repository on purpose until the address exists. Add
them in one commit after step 5, replacing `<REPO_URL>`:

- **`pyproject.toml`**, a new table after `[project.scripts]`:

  ```toml
  [project.urls]
  Homepage = "<REPO_URL>"
  Repository = "<REPO_URL>"
  Issues = "<REPO_URL>/issues"
  Changelog = "<REPO_URL>/blob/main/CHANGELOG.md"
  ```

- **`README.md`**, a CI badge line directly under the `# Context Layer` heading:

  ```markdown
  [![tests](<REPO_URL>/actions/workflows/tests.yml/badge.svg)](<REPO_URL>/actions/workflows/tests.yml)
  ```

- **`.github/ISSUE_TEMPLATE/config.yml`** (optional), so the "New issue" page
  offers the private channel next to the templates:

  ```yaml
  blank_issues_enabled: true
  contact_links:
    - name: Report a security vulnerability
      url: <REPO_URL>/security/advisories/new
      about: Private reporting, as described in SECURITY.md. Please do not open a public issue.
  ```

- **"Report a vulnerability" link check:** open `<REPO_URL>/security` while
  signed out and confirm the button that SECURITY.md describes is there. If the
  config above was added, follow its link from the "New issue" page too.

After this commit, rerun `make test` and let the hosted run pass again.

## 8. Before announcing

- The Actions tab shows a green run for the current tip of `main`.
- The Security tab offers **Report a vulnerability**.
- The README renders: the Brain View image loads, the tables render, and the
  relative links work on GitHub (GitHub is case-sensitive about paths).

## 9. PyPI: decision pending, not required for GitHub

Publishing the package on PyPI is a separate decision that has not been made,
and it is not planned for 0.4. It is not needed for the GitHub release: the
README installs from a checkout (`pip install -e .`), and hosted CI builds the
wheel and sdist and runs the distribution check without uploading anything.

If PyPI is chosen later, settle these first:

- **The name.** `context-layer` is probably not available. On 2026-09-28
  `https://pypi.org/pypi/context-layer/json` answered 404, but
  `https://pypi.org/pypi/contextlayer/json` answered 200: a different project,
  `contextlayer` 0.0.1 ("Context Layer - Personal context management for AI
  assistants"). PyPI compares names with the separators removed and refuses a
  name "too similar to an existing project", so `context-layer` would most
  likely be refused. Candidate names whose JSON URL answered 404 on the same
  day (not a promise that PyPI accepts them, and no choice is made here):
  `context-layer-vault`, `vault-context-layer`, `obsidian-context-layer`,
  `vault-evidence`, `verbatim-evidence`, `evidence-layer`. Only the
  distribution name would change; the import package `context_layer` and the
  `context-layer` command can stay as they are.
- **The long description.** `pyproject.toml` uses `README.md` as the long
  description. PyPI renders it outside the repository, so its relative links
  (`docs/cli.md`, `SECURITY.md`, ...) and the relative image
  `docs/images/brain-view.png` would not resolve there. Give PyPI a README
  with absolute links to `<REPO_URL>` (or a short description that links to
  the repository), and put the `[project.urls]` block from step 7 in place
  first.
- **The classifiers.** `Operating System :: POSIX :: Linux` should stay only
  once a Linux CI run has passed (the README's support table still says
  "expected but unverified").
- **Publishing itself.** Trusted publishing from the CI workflow, with the
  artifacts `make dist` produces there, never an upload from a local build.
