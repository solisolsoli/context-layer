# Component documentation

One file per component. The top-level [README.md](../README.md) is the
overview; [QUICKSTART.md](../QUICKSTART.md) is the same ground as commands, and
[SCOPE.md](../SCOPE.md) states what each phase had to deliver to count as done.

| Document | Component | Commands it covers |
| --- | --- | --- |
| [host-integration.md](host-integration.md) | A2 — connecting an AI host | `mcp`, `hook`, `install`, `install print`, `uninstall`, `doctor` |
| [memory.md](memory.md) | A3 — shared memory across sessions and tools | `memory add`, `list`, `resume`, `verify`, `repair`, `rebind`, `mirror`, `session` |
| [tasks.md](tasks.md) | A4 — bounded sub-agent tasks | `tasks new`, `run`, `list`, `show`, `verify`, `cancel`, `cost`, `recover`, `ledger` |
| [source-lifecycle.md](source-lifecycle.md) | A5 — source health and reversibility | `status`, `index`, `rollback` |
| [synapse.md](synapse.md) | 0.3 — synaptic retrieval over explicit links, activation trace | `search --method synaptic`, `graph health`, `graph suggest`, `graph_neighbors` |
| [subagents.md](subagents.md) | 0.3 — lean sub-agent jobs and hand-back checks | `packet`, `job`, `handback`, `handoff` |
| [brain-guide.md](brain-guide.md) | 0.3 — building your own brain; rules and hooks | `brain init`, `rules init/check/record`, `brief` |
| [jev.md](jev.md) | 0.4 — the optional advisor, off by default | `jev status`, `off`, `shadow`, `on`, `report`, `purge`, `record`, `calibrate`, `answer`, `review-memory`, `search --jev` |
| [github-context.md](github-context.md) | Optional public GitHub evidence, source management, cache and version checks | `github-context`, `github-sources`, `github-cache`, `search --github`, MCP `github_context` |
| [cli.md](cli.md) | Every command: exit codes, packet statuses, format versions | all |
| [privacy.md](privacy.md) | Every artifact the tool writes; retention, deletion, threat model | — |
| [design-rationale.md](design-rationale.md) | Why it is built this way, with public sources | — |
| [publishing-checklist.md](publishing-checklist.md) | Maintainers: updating the existing repository, required checks, publication privacy, tags and reproducible artifacts | — |

Search and indexing themselves are covered in the top-level README and
[QUICKSTART.md](../QUICKSTART.md); the experimental router has its own
[router/README.md](../router/README.md), the measurement harness its own
[eval/README.md](../eval/README.md) (with a table of every `search` flag), and
the sealed benchmark its own [bench/README.md](../bench/README.md), with every
run of the sealed cases listed in [bench/INSPECTIONS.md](../bench/INSPECTIONS.md).

Three things hold across all of them:

- **State stays in inspectable files in the vault.** Configuration, memory
  and task records use JSON, JSONL and Markdown under `.context/`; the SQLite
  search and graph indexes can be inspected and rebuilt from the source notes.
- **Retrieved text is data, never instructions.** Evidence carries
  `source_path` and `source_sha256` so a reader can check what it got.
- **Nothing is written to a host by surprise.** Every install is a dry run until
  `--apply`, backs the file up first, and is reversible with `uninstall` or by
  restoring that backup.

Licences and notices for everything the package distributes:
[THIRD_PARTY.md](../THIRD_PARTY.md).
