# Credits

## Authorship

Context Layer is maintained by solisolsoli, who set its direction and made its
product decisions. The code, tests and documentation were written and reviewed
with AI coding assistants (Anthropic's Claude models in Claude Code) working
under that direction; this note replaces per-commit co-author lines.

## Avenox Beyin

The starter brain's folder layout (`context-layer brain init`) is adapted from
**Avenox Beyin** by **Avenox**, released under the MIT licence
(Copyright (c) 2026 Avenox).

- Repository: <https://github.com/avenoxai/avenoxbeyin>
- Site: <https://avenox.lol>
- Setup video: <https://www.youtube.com/watch?v=sRCTOxc4658>

What was adapted, and how:

- **The folder layout only.** The numbered structure (Inbox, Command Center,
  Goals, Projects, a private records folder, Knowledge, Arsenal, Body, Mind,
  Companion, Archive, Templates, plus `daily/` and `knowledge/`) follows the vault
  skeleton documented in the Avenox Beyin repository (`docs/beyin-v2.md`, the
  vault skeleton step, and the `template/` folder of its current main branch).
- **Translated and simplified.** The default `standard` layout uses English names
  with the emoji removed, and names the private records folder `400-Records`
  (upstream: `400-Vault`) so it is not confused with the Obsidian vault itself.
  The companion notes use English file names (`Core.md`, `Rules.md`,
  `Last-Session.md`, `Threads.md`, `Journal.md`).
- **Optional original names.** `brain init --avenox-compat` creates the upstream
  emoji-prefixed folder names, for people who will also run the Avenox Beyin
  engine on the same vault.

What was not taken: **no Avenox code, hooks, skills, scripts or text is included
in the starter brain.** Every note, hub, template and example in
`templates/starter-brain/` was written for this project. context-layer is a
separate tool with a compatible layout, not a fork of Avenox Beyin.

## Historical Jev advisor credit

The former optional advisor (`context-layer jev`, [migration note](docs/jev.md)) followed
the design of the optional Jev advisor in **Avenox Beyin** (v3.1.0-v3.5.1, MIT
licence, Copyright (c) 2026 Avenox), whose Jev integration was contributed by
**Forn** and adapts Forn's hafiza-os (MIT licence).

The former design used an advisor that was off by default and optional to
install; the `off` / `shadow` / `on` modes; per-feature switches; a kill switch;
a call log that holds counters only; privacy gates with a secret scan before
anything is sent; a freshness re-check of the judged sources after every call;
a cache bound to source versions; and a bounded transport that never retries.
The provider protocol shape follows TypeSafe's public API documentation.

The name "Jev" follows that advisor. It is also the name of TypeSafe's model;
no affiliation with TypeSafe or Avenox is claimed.

The code was written independently for this project: no Avenox file was copied
or adapted. A line-level comparison of `context_layer/jev*.py` and
`tests/test_jev*.py` against Avenox Beyin's Jev, client, contracts and Laya
modules and their tests (difflib over lines longer than 20 characters, run on
2026-09-29) found at most seven identical lines in one file pair, all generic
Python (`for item in evidence:`, returns and guards), and no shared run of more
than three lines.

## Retrieval patches

The optional patches in [`retrieval-patches/`](retrieval-patches/) modify
Avenox Beyin **v3.0.1** (commit `61a88467d748fbf94ebc340cb932da7c7e9a78b7`).
Their diff hunks contain upstream context lines, so they carry Avenox's MIT
notice verbatim in
[`retrieval-patches/LICENSE.upstream.txt`](retrieval-patches/LICENSE.upstream.txt).
Compatibility with any other Avenox Beyin release has not been tested. The full
licence inventory is in [THIRD_PARTY.md](THIRD_PARTY.md).
