## What and why


## How it was checked

- [ ] `make test`
- [ ] `make lint` (with `pyflakes` installed)
- [ ] `make demo`
- [ ] Plugin changes: `make plugin-test`
- [ ] Packaging changes: `python3 -m build` and `python3 scripts/check_distribution.py dist/*.whl`

Checks not run, and why:

## Hygiene

- [ ] Tests use disposable synthetic vaults only; no real notes, prompts, paths or credentials
- [ ] Negative cases are covered (abstention, operational failure, exclusion), not only the happy path
- [ ] Every new claim in the docs is backed by a test or a command in the repository
- [ ] Everything is in English
- [ ] `AGENTS.md` and `CLAUDE.md` are still byte-identical
- [ ] `CHANGELOG.md` updated for user-visible changes

By submitting this pull request I agree that my contribution is licensed under
the project's MIT licence, and that I understand and have tested any code in it,
including code written with an AI assistant.
