# Security policy

## Supported versions

Only the latest release and current `main` are maintained. There are no
backports. The package version is currently 0.4.0; checkout changes are listed
under [Unreleased](CHANGELOG.md#unreleased). Include the commit when reporting
from a checkout, since its behavior may differ from the 0.4.0 tag.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/solisolsoli/context-layer/security/advisories/new),
or open this repository's **Security** tab and choose **Report a vulnerability**.
Sign in to GitHub if prompted. Reports stay private to the reporting and
repository security participants until disclosure; they are not public issues.

The repository is maintained only by its owner. Public issues, pull requests
and discussions are closed, but private vulnerability reporting remains open.
If GitHub's private form is unavailable, keep the details private and retry
that channel later; do not put them in commit comments, forks or public posts.
There is no alternate project email address or public support queue.

Include:

- the context-layer version (`context-layer --version` or the commit),
  Python version and operating system;
- a **minimal synthetic reproducer**: a few invented notes, the
  `.context/routes.json` involved and the exact commands;
- for anything involving a host or a task backend, which host, which backend
  and which permissions the agent had.

Do **not** send a real vault, real notes, prompts from real sessions,
credentials, or anyone else's data. Start with a synthetic reproducer and
describe any missing detail without including private content.

## What happens next

This is an owner-maintained project. Reports are acknowledged and
investigated on a best-effort basis; there is no guaranteed response time. A
confirmed issue gets a GitHub security advisory, published once a fix is
released — or published without a fix, with the workaround, if no fix is in
sight. Credit is given in the advisory unless you ask otherwise.

## In scope

The claims this project makes about itself. A report that shows any of them to
be false is a vulnerability:

- **Evidence integrity**: a packet delivers bytes that are not the indexed bytes
  of the named source, or marks a changed, deleted, symlinked or unindexed
  source as verified; an operational failure (missing, corrupt or damaged index,
  including a full-text table that no longer matches the stored records, or an
  unusable `routes.json`) produces anything but an error with no evidence.
- **Exclusions and boundaries**: content under an excluded prefix (in any
  letter case), outside the vault, or behind a symlink reaches an index, a
  packet, `read_source`, a synaptic hop, `activation.json` or a task packet.
- **Task verification**: `tasks verify` reports `verified` for a run that wrote
  outside its output directory, whose definition was changed after dispatch, or
  that produced no output of its own during the attempt window
  (see [docs/tasks.md](docs/tasks.md) for the exact boundary).
- **Installers**: `install` / `uninstall` writing a file or key other than the
  ones its dry run shows, or losing a backup.
- **Memory**: a change to `records.jsonl` that `memory verify` does not report.
- **GitHub evidence**: fetching outside owner-configured public coordinates,
  accepting bytes that fail the documented hash checks, sending a prompt or
  credentials to GitHub, bypassing an explicit offline request, or treating
  external content as locally verified evidence.
- A new way around a mitigation listed in the threat model below.

## Known limitations (not vulnerabilities, but disclosed)

These are documented design limits. Reports that merely restate them are out of
scope; a practical way to make them worse than described is in scope.

- **Prompt injection through vault content.** Evidence is delivered verbatim to
  an AI host that may follow instructions inside it. A note can contain
  instruction-shaped text, and the tool cannot stop a model from following it.
  The "data, not instructions" framing and the nonce-delimited evidence markers
  reduce this; they do not prevent it. The prompt hook adds evidence to host
  sessions that usually have file, shell and network tools, and sub-agent tasks
  start a host with tool permissions. The synaptic method adds linked notes that
  did not match the query (backlinks especially), which widens what a planted
  note can reach. The mitigations are the host's permission model and narrow
  `--source` globs. Public background: Greshake et al. 2023, "Not what you've
  signed up for" (arXiv:2302.12173); UK NCSC, 8 December 2025, "Prompt injection is
  not SQL injection (it may be worse)". See the threat model in
  [docs/privacy.md](docs/privacy.md#threat-model-vault-content-is-data-not-instructions).
- **A sub-agent's host loads its own context.** A `claude -p` task backend loads
  `CLAUDE.md` files from its working directory and parents and the user's
  `~/.claude` settings and hooks unless the task uses `--host-context
  safe-mode` or `bare` (see [docs/tasks.md](docs/tasks.md)). The packet is the
  only evidence this tool supplies, not the agent's only context.
- **Tasks are not sandboxed.** A backend runs as your user, with your
  environment, and can write anywhere that user can. `tasks verify` detects
  writes and definition tampering within the attempt window; a process the
  backend leaves running after the attempt can rewrite state undetected.
- **Memory records written through MCP** are always drafts (the MCP tool
  refuses `approved` and `published`), but a draft is still read back by later
  sessions through `memory_resume`, so a note-steered agent can plant one.
- **Full-text copies and other artifacts**: the index (and its `.prev`) is a
  full-text copy of the vault; `route` saves prompts and evidence under
  `.context-runs/` unless `--no-save` is given; shared packets and task
  directories hold verbatim evidence; `graph.sqlite` holds the link structure
  and `activation.json` the last synaptic retrieval's notes. The full list, with
  how to delete each, is in [docs/privacy.md](docs/privacy.md).
- The optional `retrieval-patches/` are experimental diffs against an upstream
  project, not part of the installed package, and have not had the adversarial
  testing the package has.

## Optional GitHub evidence

The owner must enable and allowlist public files in `.context/github.json`
before a caller can fetch them. The separate `github_client.py` transport
uses anonymous HTTPS GET to GitHub's contents API, validates pinned commit and
path inputs, refuses redirects, bounds responses and verifies blob hashes.
It never reads credentials, sends prompts or vault notes, executes source
text, follows source links or installs an MCP server. Fetched text remains
untrusted data and cannot override host instructions. Commit pinning binds a
version; it does not certify correctness or currency. Optional disk caching
stores only public file bytes and their provenance. Hash checks detect
accidental corruption, not an attacker who can rewrite both bytes and hashes.
Source updates are explicit owner actions; there is no automatic pin upgrade.
Details, cache limits, offline behavior and removal:
[GitHub context](docs/github-context.md).
