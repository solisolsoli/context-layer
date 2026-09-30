# Maintainer update and release checklist

This checklist is for the owner of the existing
[Context Layer repository](https://github.com/solisolsoli/context-layer).
Outside contributions are closed under [CONTRIBUTING.md](../CONTRIBUTING.md).
Use the existing public history and `main` branch. Updating documentation or
pushing a commit does not create a release, publish to PyPI, or authorize
installation into anyone's live vault.

The package version is 0.4.0. Changes after that tag remain under
[Unreleased](../CHANGELOG.md#unreleased) until a separate release is prepared.
Do not move or recreate an existing version tag.

## 1. Review the exact change

- Inspect the working tree, current branch and intended diff. Preserve any
  unrelated local work. Confirm that `origin` identifies the existing project.
- Keep `AGENTS.md` and `CLAUDE.md` byte-identical, and do the same for the vault
  templates when they change. Do not put private task records in this repo.
- Recheck user-facing claims against commands and source. Retain measured
  benchmark results and their limits; do not turn a package pass into a claim
  about a live host or model answer accuracy.
- Record user-visible changes under `Unreleased`. Keep the MIT license and
  retained [third-party notices](../THIRD_PARTY.md) in every distribution.

## 2. Validate the affected surface

For documentation changes, run the relevant documentation checks:

```sh
git diff --check
python3 tests/test_docs_links.py
python3 tests/test_doc_claims.py
```

For runtime changes, run `make test` and the relevant installed-package checks.
For packaging or metadata changes, build fresh artifacts and check both the
wheel and sdist. The full release gate also includes:

```sh
make test
make lint                              # pyflakes must be installed
make demo
python3 obsidian-plugin/build.py --check
make plugin-test                       # Node 24
make bench-check
make dist                              # requires the Python build frontend
python3 scripts/check_distribution.py dist/context_layer-*.whl
```

Pin the build tools to the versions in the CI workflow. `make dist` replaces
`dist/`; preserve any unrelated artifacts before using it. It sets
`SOURCE_DATE_EPOCH` from the commit and normalizes the sdist with
`scripts/normalize_sdist.py`: sorted members, `root/root` ownership and one
UTC timestamp. Never distribute an unnormalized sdist containing a local
account name. Inspect archive members and metadata for private paths or data.

When retrieval, measurement code or published benchmark claims change, rerun
the required benchmark reproduction. Before inspecting new sealed-set results,
record the inspection in [bench/INSPECTIONS.md](../bench/INSPECTIONS.md); do not
tune against those results. Keep real-model and live-host evaluations separate.
Report skipped checks and the conditions needed to run them.

## 3. Audit files and the history being published

A push exposes all reachable commits in the pushed ref, including old file
versions and commit metadata. Audit the intended public revision explicitly:

```sh
python3 scripts/audit_history.py --rev HEAD
```

Run it again after committing so the new commit message and blobs are included.
Audit any additional ref you intend to push separately. Do not use `--all` for
this release check when the checkout contains unrelated private refs, and do
not delete branches or worktrees just to make an audit pass.

The audit checks non-placeholder home paths and emails, private-trace digests
and the documented English/fixture rules. It is not a complete secret detector:
separately inspect credentials, private keys, local config, archives, logs and
build metadata. Distinguish deliberate synthetic secret fixtures from real
credentials without copying matched values into reports.

Review author/committer identities and trailers for the public ancestry. Use
the public project identity and a GitHub noreply email for owner commits; keep
private names, addresses and local timezone details out of commit metadata.
If a real secret has reached public history, stop that publication, revoke the
secret, and handle history cleanup as a separate explicit recovery operation.
Do not silently force-push or rewrite established public history.

## 4. Verify repository settings

These settings implement the current owner-maintenance policy; verify their
actual state rather than assuming a documentation edit applied them:

- Only the owner has repository write access. Review collaborators, pending
  invitations and deploy keys; retain no unintended write grant.
- Pull requests, issues, discussions and the unused wiki are disabled. Keep
  the repository public. MIT use and fork rights are unchanged.
- Private vulnerability reporting is enabled. The reporting form linked in
  [SECURITY.md](../SECURITY.md) requires a GitHub sign-in; it must remain
  separate from conduct reports or general support.
- Actions workflow tokens default to read access and cannot approve pull
  requests. Workflows declare `contents: read`. Keep CI running on owner pushes.
- Dependabot version-update PRs are paused (`open-pull-requests-limit: 0`).
  Security-update PRs are separately disabled in repository settings; alerts
  and secret scanning are not disabled by that policy. The owner reviews
  pinned action updates. Keep secret scanning and push protection enabled.
- The About description and topics describe current features without unmeasured
  accuracy claims. The website points to the repository's documentation.
- Do not impose a required-PR rule while PR creation is disabled. Any future
  ruleset must preserve the owner's intended update path.

GitHub documents the current controls in
[disabling pull requests](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/disabling-pull-requests)
and the [repository REST API](https://docs.github.com/en/rest/repos/repos).
A temporary interaction limit is not a permanent replacement for these controls.

## 5. Commit, push and verify hosted CI

Push only the intended branch with a normal fast-forward. For a reviewed
checkout whose HEAD is the intended next `main`:

```sh
git push origin HEAD:main
```

Do not use `--all`, `--mirror`, a force push, or push private local refs. Verify
that remote `main` resolves to the intended commit and its files match the
reviewed bytes. Wait for the `tests` workflow on that exact commit. It has 12
required jobs:

- `python 3.10 on ubuntu-latest`, `python 3.11 on ubuntu-latest`,
  `python 3.12 on ubuntu-latest`, `python 3.13 on ubuntu-latest`;
- `python 3.12 on macos-latest`, `python 3.12 on windows-latest`;
- `obsidian plugin (node 24)`, `sealed benchmark (offline)`, `bench-reproduce`;
- `doc-claims`, `jev recording chain`, `reproducible build`.

A failure is investigated and fixed, not hidden behind a platform-wide skip or
`continue-on-error`. Local checks do not replace hosted results. Keep static
support claims tied to an accepted runtime commit and run; the README badge
shows the current workflow state. A subsequent documentation commit does not
change which runtime was measured. CI does not prove live-host behavior.

## 6. A new version is a separate release step

Only after the owner chooses a new version and authorizes a release:

1. Align `pyproject.toml`, `context_layer/__init__.py`, the plugin manifest and
   the new changelog entry. Run the applicable full gate for that revision.
2. Create a new version tag on the accepted commit. Never reuse `v0.4.0` for
   later work. Push only that named tag.
3. Require the `release audit` workflow for the tag to pass. Inspect its
   history, license and metadata evidence before creating a GitHub release.
4. Attach only reviewed, normalized artifacts. Optional plugin release assets
   are `main.js`, `manifest.json` and `styles.css` as described in the
   [plugin guide](../obsidian-plugin/README.md).
5. Verify the final README rendering, image, relative links, package metadata,
   Security policy and release asset hashes on GitHub.

PyPI publication is a separate decision and is not performed by these checks.
If later authorized, verify the package name and publishing requirements at
that time, prepare a long description with links that resolve outside GitHub,
and use reviewed CI artifacts with trusted publishing. A package index lookup
alone does not reserve or prove availability of a name.
