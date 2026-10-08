# Host integration

Two ways to give an AI host the vault's evidence without pasting it by hand:

- **MCP server** (`context-layer mcp`) — the host calls tools when it decides it
  needs them: `search_vault`, `read_source`, `vault_status`, `memory_record`
  (drafts only), `memory_resume`, `graph_neighbors`, `read_packet`,
  `check_claims`, `github_context`. Any MCP client
  that speaks stdio can use it. It speaks MCP revision 2026-07-28 and the
  `initialize`-era revisions 2025-11-25, 2025-06-18, 2025-03-26 and 2024-11-05
  (see [Protocol revisions](#protocol-revisions)).
- **Prompt hook** (`context-layer hook claude-code`) — runs on every prompt and
  prepends focused synaptic evidence (FTS hits plus notes reached through
  explicit links; `--method fts` for FTS only) before the model sees the
  question. Claude Code; the same hook in Codex's documented format is
  expected but unverified (no Codex CLI ran on the reference machine).

They are independent. The MCP server is on demand and precise; the hook is
automatic and unconditional, so it also spends tokens on prompts that need no
vault. Installing both is fine: the hook front-loads the obvious evidence and
the tools fetch the rest.

Everything a tool returns is framed as **data, not instructions**. That framing
reduces prompt injection; it does not prevent it. Notes are delivered verbatim
to a model that may still follow an instruction written inside one ("ignore
your instructions", a request to run a command, a link to fetch). The host's own
permission model is the real control: keep tool permissions narrow, review
what the model proposes to run, and, for sub-agent tasks, narrow `--source`
globs. The synaptic method also adds linked notes that did not match the query
(backlinks especially), which widens what a planted note can reach. See
SECURITY.md. Evidence carries `source_path` and `source_sha256` so a reader can
check what it got; `NOT_FOUND` means no evidence was found, not that the answer
is "no".

For an owner-enabled public documentation fallback, use `search_vault` with
`github: true`, or `github_context` when local evidence is insufficient.
External passages have immutable URL/commit/hash citations and stay outside
the local evidence ledger. See [GitHub context](github-context.md).

## Install, with nothing written by surprise

Every `install` and `uninstall` is a **dry run** by default: it prints a unified
diff of the file it would change and writes nothing.

```sh
context-layer install claude-code --vault /path/to/vault            # show the diff
context-layer install claude-code --vault /path/to/vault --apply    # write it
```

`--apply` copies the current file to `<name>.bak-<UTC timestamp>` beside it
before writing, and never overwrites an existing backup. A file that holds
nothing but this tool's own entries (its own `.mcp.json`, settings or marker)
is replaced without a backup, because there is nothing of anyone else's in it;
that is why an install followed by an uninstall in an empty project leaves the
project empty. `install print <host>` shows the config alone, for pasting
somewhere this tool does not write. Diffs and snippets go to stdout; notes and
warnings go to stderr.

The command a host is told to run is `context-layer` from `PATH`, or
`<this python> -m context_layer.cli` (with `PYTHONPATH` set to the checkout) when
the package is not on `PATH`, both with `PYTHONUTF8=1` (an `env` block for the MCP
server, a `PYTHONUTF8=1` prefix for POSIX hook commands). Windows hooks use a
PowerShell encoded command to carry literal paths and environment values safely;
the encoding is shell quoting, not encryption. Whichever launcher is resolved is written into the config
literally, so a host that does not share your shell's `PATH` still starts the
server. Those entries therefore name this machine's interpreter and vault paths;
every install says so on stderr. Keep them out of shared commits: for Claude Code,
`--scope local` writes the hooks to `.claude/settings.local.json`, which the
Claude Code settings docs describe as personal to one project
(code.claude.com/docs/en/settings, "Project local").

## Claude Code

### MCP, project scope (default)

```sh
context-layer install claude-code --vault /path/to/vault --project /path/to/repo --apply
```

Writes `<project>/.mcp.json` (default project: the vault directory itself):

```json
{
  "mcpServers": {
    "context-layer": {
      "command": "context-layer",
      "args": ["mcp", "--vault", "/path/to/vault"]
    }
  }
}
```

Other `mcpServers` keys and every other key in the file are kept. Claude Code
asks for approval the first time it sees a project MCP server. Confirm with
`/mcp` in the session; the ten tools appear as `mcp__context-layer__*`.

### MCP, local or user scope

```sh
context-layer install claude-code --scope user --vault /path/to/vault
context-layer install claude-code --scope local --vault /path/to/vault --project /path/to/repo
```

Claude Code keeps local- and user-scope servers in its own `~/.claude.json`
(code.claude.com/docs/en/mcp, "MCP installation scopes"), which this tool never
edits. It prints the exact command instead:

```sh
claude mcp add --scope user context-layer -- context-layer mcp --vault /path/to/vault
```

The command carries `--env PYTHONUTF8=1` (UTF-8 mode, so a host that starts it
under a non-UTF-8 locale still gets UTF-8 standard streams) and, when the server
needs `PYTHONPATH` (a checkout without an installed script), `--env PYTHONPATH=...`,
after the server name and before `--`, where the Claude Code MCP docs place `--env`.
The command shown above is the shape; the real one lists these `--env` pairs. With `--apply`, and
only if `claude` is on `PATH`, it runs that command for you (local scope from
the project directory, since Claude Code stores a local server per project);
otherwise it prints it and says it was not run. Undo with
`claude mcp remove --scope user|local context-layer` (what
`uninstall --scope user|local` prints or runs). `--apply` here runs a host CLI
that edits the host's own config, so this tool keeps no backup of that file.

### Prompt hook

```sh
context-layer install claude-code --vault /path/to/vault --hook --apply
```

Adds one `UserPromptSubmit` command hook to `<project>/.claude/settings.json`
(`.claude/settings.local.json` with `--scope local`), as its own entry, with
`"timeout": 30`; existing hooks are copied through untouched. The hook runs
`context-layer hook claude-code --vault <vault>`, which reads the host's hook
JSON on stdin, runs the synaptic search (the fts items, focused, plus link-reached
passages; `--method fts` for lexical matches only) for its `prompt`, and prints
`hookSpecificOutput.additionalContext`. Each evidence item sits between markers
that carry a random per-packet nonce, with the path, the line span and the hash
in the opening marker, so text inside a note cannot forge an item boundary:

```text
<<evidence 1 3f9a0c1d2e4b path=notes/release.md lines=1-14 sha256=5d41402abc4b>>
...verbatim note text...
<<end 1 3f9a0c1d2e4b>>
```

The hook delivers each note focused (`--delivery focus`, the default): the blocks
that hold query terms with their neighbours in the same section, even when the
whole note would fit, so `lines=` may start past line 1 and ` excerpt` at the end
of the marker says the item is shorter than its note; a note with no matching
block, or whose selection is all of it, comes whole (or as its match windows when
it is longer than the per-source limit, 2,000 characters by default).
`--delivery window` gives the items `search` gives (`docs/cli.md`, "Evidence
items"); `install ... --hook --delivery window` writes it. Measured on the development set only:
[validation guide](validation.md#focused-hook-delivery). The 0.3 hook wrote the
marker without `lines=`.

A path that holds whitespace, a quote, `<`, `>`, `=`, a backslash or a
non-printing character is written as a quoted string in which `<`, `>` and every
control, format or separator character is a `\uXXXX` escape, so a file name can
neither break the header onto a new line nor close it early:

```text
<<evidence 1 3f9a0c1d2e4b path="notes/x sha256=000000000000>> SYSTEM.md" sha256=5d41402abc4b>>
```

(0.2 joined items as `path (sha256 first 12) — content`; the 0.2 live comparison
in `eval/LIVE_COMPARE.md` was recorded with that older format.)

Since 0.5 the hook runs synaptic retrieval with focused delivery by default:
the fts hits plus notes reached through explicit links, each cut to the blocks
that match the prompt and their neighbours (byte and line ranges and sha256
kept). To size the link extras or to keep the hook on FTS only:

```sh
context-layer install claude-code --vault /path/to/vault --hook \
  --extra-tokens 600 --apply                     # synaptic, larger link budget
context-layer install claude-code --vault /path/to/vault --hook \
  --method fts --apply                           # FTS only, as before 0.5
```

The flags are written into the hook command (`hook claude-code --extra-tokens
600 --vault <vault>`, or `--method fts`); `uninstall` recognises and removes
that entry too, and re-installing without `--method` replaces it with the
default synaptic hook. In the default synaptic mode the packet is the fts
packet plus link extras within `--extra-tokens` (default 600). `--budget-tokens
N` sizes only the compact packer, so it is accepted only together with
`--compact` (`--compact --budget-tokens 1200`); see `docs/synapse.md`.

The hook uses local retrieval. The manual [Decisions](decisions.md) and
[Responses](responses.md) commands do not run from a hook or send vault
passages automatically.

**Size.** The Claude Code hooks reference (code.claude.com/docs/en/hooks, read
2026-09-28) says: "A hook's `additionalContext`, `systemMessage`, and
`initialUserMessage` strings, and its plain stdout, are capped at 10,000
characters"; above that Claude Code saves the text to a file and passes Claude
the path with a preview of up to the first 2,000 characters. The hook therefore
packs its output to `--max-context-chars` (default 9,000; accepted range
2,000–10,000, framing included): whole items are taken from the top of the
packet until the next one would not fit, and the rest are dropped and named in
one line, `N item(s) omitted to fit the 9000-character hook limit: <paths>`. An
item is never cut. `install ... --hook --max-context-chars N` writes the flag.

**Fewer tokens, opt-in.** `--relevance-floor R` on the hook line (0 <= R < 1, default
0 = off; `install ... --hook --relevance-floor R` writes it) leaves out a top-k note whose bm25 is weaker than
R times the strongest one, in fts and in the fts part of the default synaptic packet;
it may drop a note that held the answer. Measured only on the development set:
[validation guide](validation.md#opt-in-relevance-floor).

**Notices always travel.** Stderr from a hook that exits 0 reaches only Claude
Code's debug log (hooks reference, "Exit code 0"), so everything the model
must pass on is also in `additionalContext`:

- a source that changed or was deleted since the last index is withheld, and
  the context says `Not included: withheld N source(s): <path> (<reason>); run
  context-layer index <vault>. Tell the user that these notes were left out ...`
  — even when nothing else was found. For `hook claude-code` the same line
  (`context-layer: withheld N source(s): ...`) is also the hook JSON's
  `systemMessage`, which Claude Code shows to the user without the model's
  relay; `hook codex` sends only the context notice (whether Codex shows
  `systemMessage` is not verified);
- a prompt longer than 16,000 characters is searched by its first and last
  8,000 characters, and the context says so.

**Exit codes.** The hook never exits 2. The hooks reference ("Exit code 2
behavior per event") says exit 2 from `UserPromptSubmit` "Blocks prompt
processing and erases the prompt", and from `Stop` "Prevents Claude from
stopping, continues the conversation".

| Outcome | Exit | stdout |
| --- | --- | --- |
| evidence (possibly with omitted items or a withheld notice) | 0 | the hook JSON |
| no evidence and nothing withheld (`NOT_FOUND`) | 0 | nothing |
| an empty or blank prompt (nothing to search; one stderr line says so) | 0 | nothing |
| an unknown `--method` value | 0 | the fts result; one stderr line says fts ran |
| a malformed command line: unknown flag, a bad or missing value, a missing `--vault`, an unknown host, a value above a cap | 1 | nothing |
| unreadable or non-UTF-8 hook JSON, no `prompt` string | 1 | nothing |
| missing index, retrieval error, retrieval slower than 20 s | 1 | nothing |

Every exit-1 case prints exactly one line to stderr (Claude Code shows the first
stderr line as a non-blocking "hook error"), never a traceback. The 20 s limit
sits below the 30 s the host allows. Exit codes for every command:
`docs/cli.md`.

Retrieval runs inside the hook's own process (no second interpreter) and gets the
prompt as a string, never as an argument, so a prompt such as `--help` or `-x` is
searched, not read as an option; NUL becomes a space and a lone surrogate becomes
U+FFFD. A retrieval slower than 20 s is abandoned on its thread and the hook exits 1
at once with its own message.

The hook is installed per project even with `--scope user`; `--scope` only moves
the MCP entry (and, for `local`, the hooks' file).

### Rule-record hooks

`install claude-code --rules` adds the `SessionStart` and `Stop` hooks that run
`context-layer rules hook session-start|stop` (see `docs/brain-guide.md`), and a
`PostToolUse` hook for `Write|Edit|MultiEdit|NotebookEdit` that attributes vault
writes to the agent. `--plan-default` sets `permissions.defaultMode` to `"plan"`
(see [Uninstall and rollback](#uninstall-and-rollback)).

## Codex — expected but unverified

```sh
context-layer install codex --vault /path/to/vault --apply
context-layer install codex --vault /path/to/vault --project /path/to/repo --hook --rules --apply
```

No Codex CLI ran on the reference machine: every file below follows the Codex
docs (learn.chatgpt.com/docs/extend/mcp, learn.chatgpt.com/docs/hooks,
learn.chatgpt.com/docs/config-file/environment-variables, all read 2026-09-28),
and nobody here has watched Codex load it.

**MCP server.** A block in `$CODEX_HOME/config.toml` (`CODEX_HOME` defaults to
`~/.codex`; the Codex docs say that if you set it, "the directory must already
exist", so a missing one is refused), between the tool's own markers:

```toml
# >>> context-layer >>>
# Written by context-layer. Only the lines between these markers are touched.
[mcp_servers.context_layer]
command = "context-layer"
args = ["mcp", "--vault", "/path/to/vault"]
tool_timeout_sec = 180
# <<< context-layer <<<
```

`tool_timeout_sec` is written because Codex's default is 60 s and one search may
take up to 120 s here. Only text between the markers is read or rewritten; the
rest of the file is copied through byte for byte. Before writing, the installer
refuses, names the problem and writes nothing when:

- the file already declares `[mcp_servers.context_layer]` outside the markers,
  in any spelling (a table, a sub-table, a dotted key, or a key of
  `[mcp_servers]`) — for example after `codex mcp add context_layer ...`;
- the file, or the file with the block, is not valid TOML (checked with
  `tomllib`, Python 3.11+; on Python 3.10 only the table scan runs).

**Hooks.** `--hook` and `--rules` write `<project>/.codex/hooks.json` in the shape
the Codex hooks page documents (event → matcher group → handler, the same
`hookSpecificOutput.additionalContext` output): a `UserPromptSubmit` hook
running `context-layer hook codex`, and the `SessionStart`/`Stop` rule hooks.
The prompt hook's handler carries `"additionalContextLimit": 0`: by default Codex
limits a hook's context to roughly 2,500 tokens and saves longer text to disk,
and its docs reserve `0` for a hook that enforces its own strict cap, which this
one does (`--max-context-chars`). Codex loads a project's hooks only when the
project is trusted, and runs a new or changed hook only after you review it in
`/hooks`.

## Other hosts — expected but unverified

`install generic` prints the server entry and writes nothing; `--format <host>`
prints it in that host's own shape:

```sh
context-layer install generic --vault /path/to/vault --format cursor
```

The table records each host's documented config, read on 2026-09-28. **None of
these hosts ran on the reference machine**; every row is expected but
unverified. The printed snippets are tested only for their shape
(`tests/test_mcp_install.py`, `GenericFormats`).

| Host (`--format`) | Config file: project / user | Stdio server entry | Notes | Docs |
| --- | --- | --- | --- | --- |
| Claude Code (`claude-code`) | `<project>/.mcp.json` / `~/.claude.json` via `claude mcp add` | `mcpServers.<name>` with `command`, `args`, `env` | written by `install claude-code` | code.claude.com/docs/en/mcp |
| Codex | `<project>/.codex/config.toml` (trusted projects) / `$CODEX_HOME/config.toml` | `[mcp_servers.<name>]` with `command`, `args`, `env`, `cwd`, `startup_timeout_sec` (10), `tool_timeout_sec` (60), `enabled` | written by `install codex` | learn.chatgpt.com/docs/extend/mcp |
| Cursor (`cursor`) | `.cursor/mcp.json` / `~/.cursor/mcp.json` | `mcpServers.<name>` with `type: "stdio"`, `command`, `args`, `env`, `envFile` | asks for approval before using MCP tools by default | cursor.com/docs/mcp |
| Gemini CLI (`gemini`) | `.gemini/settings.json` / `~/.gemini/settings.json` | `mcpServers.<name>` with `command`, `args`, `env`, `cwd`, `timeout` (ms, 600,000), `trust` | confirms each tool call unless `trust`; with Trusted Folders on, an untrusted folder loads no project settings | geminicli.com/docs/tools/mcp-server/ |
| Antigravity (`antigravity`) | `.agents/mcp_config.json` / `~/.gemini/config/mcp_config.json` | `mcpServers.<name>` with `command`, `args`, `env`, `cwd`, `disabled`, `disabledTools` | an unconfigured MCP tool runs in Ask mode | antigravity.google/docs/mcp |
| OMP (`omp`) | `.omp/mcp.json` / `~/.omp/agent/mcp.json`; falls back to `mcp.json` or `.mcp.json` at the project root | `mcpServers.<name>` with `type` (default stdio), `command`, `args`, `env`, `cwd`, `enabled`, `timeout` (ms) | imports Claude Code, Codex, Gemini CLI, OpenCode and Cursor definitions, so `install claude-code` may already reach it | omp.sh/docs/mcp |
| OpenCode (`opencode`) | `opencode.json` / `~/.config/opencode/opencode.json` | `mcp.<name>` with `type: "local"`, `command` (one array: executable and arguments), `environment`, `enabled`, `timeout` (ms, 5,000) | JSON or JSONC | opencode.ai/docs/mcp-servers/ |
| Hermes Agent (`hermes`) | — / `~/.hermes/config.yaml` | `mcp_servers.<name>` with `command`, `args`, `env`, `enabled`, `timeout` (s, 300) (YAML) | with `trust: untrusted`, a tool without `readOnlyHint: true` needs approval | hermes-agent.nousresearch.com/docs/reference/mcp-config-reference |

The tools' annotations matter to two of these hosts: Codex's
`default_tools_approval_mode = "writes"` prompts for tools that are not marked
read-only (learn.chatgpt.com/docs/extend/mcp), and so does Hermes for an
untrusted server. `search_vault` is not marked read-only (see [Tools](#tools)),
so those hosts ask before each search unless you approve the tool.

## The MCP server

The contract is plain stdio MCP: JSON-RPC 2.0, one object per line, nothing but
JSON-RPC on stdout (written as ASCII, every other character as a JSON escape),
logs on stderr. On start the server prints
`context-layer mcp: serving vault '<vault folder name>'` to stderr and waits.

### Protocol revisions

The MCP specification index (modelcontextprotocol.io/specification, with the
list at modelcontextprotocol.io/llms.txt), read on 2026-09-28, lists the
revisions 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25 and 2026-07-28, plus a
draft; its versioning page (modelcontextprotocol.io/specification/versioning)
names **2026-07-28** the current revision. This server supports the current
revision and the two before it, and keeps the two older ones:

- **2026-07-28** has no `initialize`: every request carries
  `params._meta["io.modelcontextprotocol/protocolVersion"]` and
  `["io.modelcontextprotocol/clientCapabilities"]`, and is served on its own.
  `server/discover` returns the supported versions, capabilities and
  `instructions`; results carry `resultType: "complete"` and the server's
  identity in `_meta`; `tools/list` carries `ttlMs` and `cacheScope`. A version
  it does not support gets error `-32022` with `data.supported` and
  `data.requested`; a request missing `clientCapabilities` gets `-32602`. A
  client that opens with `initialize` is served under the initialize-era rules
  ("dual-era", /specification/2026-07-28/basic/versioning).
- **2025-11-25, 2025-06-18, 2025-03-26, 2024-11-05**: `initialize` echoes the
  requested revision when it is one of these, and answers `2025-11-25`
  otherwise (including a request for 2026-07-28, which has no `initialize`).

Per revision: an argument the tool rejects (wrong type, below its minimum,
missing) is a protocol error `-32602` before `initialize` and up to 2025-06-18,
and a tool error (`isError: true`) from 2025-11-25 on (its changelog, SEP-1303).
A value above a [limit](#limits) is a tool error in every revision.

### Limits

| Argument | At most | Over the limit |
| --- | --- | --- |
| `search_vault` `top_k` | 20 sources | refused |
| `search_vault` `budget` | 24,000 characters | refused |
| `search_vault` `per_source` | 6,000 characters | refused |
| `search_vault` `budget_tokens`, `extra_tokens` | 20,000 estimated tokens | refused |
| `search_vault` `prompt` | 16,000 characters | refused |
| `read_source` `max_chars` | 6,000 characters | refused |
| `read_source` file size | 64 MiB | refused |
| `memory_resume` `limit` | 200 records | refused |
| `graph_neighbors` `limit` | 100 per direction | refused |
| `check_claims` `claims` | 20 claims | refused |
| `check_claims` citations per claim | 8 | refused |
| `check_claims` claim `text`, citation `span` | 2,000 and 20,000 characters | refused |

"Refused" means a tool result with `isError: true` whose text names each value
over its limit and the limit (for example `top_k 1000000 is above the cap of 20;
ask for at most 20`), so the model can correct itself; nothing is clamped
silently. Every limit is declared as `maximum` (or `maxLength`) in the tool's
`inputSchema`. The server's own defaults (`mcp --top-k/--budget/--per-source`)
are checked against the same limits at start. `read_source` reads its window by
streaming the file once (hash, length, the requested characters), so memory does
not grow with the file: on the reference machine a 50 MB file peaked at
24.1–24.9 MiB RSS against 23.8–24.1 MiB for a 2 KB file (0.3.0 read the file
whole: 118.2–118.7 MiB).

### Concurrency and cancellation

One reader answers `initialize`, `ping`, `tools/list`, `server/discover` and
`notifications/cancelled` as they arrive; `tools/call` runs on one worker
thread, in arrival order. So a ping never waits behind a search, but **tool
calls are still serial**: a second search waits until the first finishes. At
most 32 calls wait behind the running one; one more is answered at once with a
"busy" tool error.
Searches run on one warm retrieval worker (`eval/retrieve.py --serve`, started on
the first search and reused), so a call pays no interpreter start or imports. The
worker re-reads the configuration, the index, the graph and every source per call;
it keeps only the index SHA-256 (keyed by the index file's device, inode, size and
modification time, plus its status-change time on POSIX) and per-name exclusion
verdicts (keyed by the exclusion list). `notifications/cancelled` for a running
search kills the worker's whole process tree, and so does a search slower than
120 s; the next search starts a new worker. A cancelled request (running or still
queued) gets no response. Other tools run
in-process and cannot be interrupted: a cancelled one runs to the end and its
result is discarded. When the client closes stdin, the server finishes the calls
it already accepted and exits 0.

### Malformed input

One bad message never ends the session. A line that is not UTF-8, not JSON or
nested too deeply is answered with `-32700` and `id: null`; a line over 8 MiB is
answered with `-32600`. A request with a `null`, fractional or boolean id, a
missing or wrong `"jsonrpc"`, or no string method gets `-32600` (MCP requests
never use a null id). An id that is a lone surrogate is echoed back as a JSON
escape. A JSON-RPC response sent by the client is never answered. Batches are
answered (one array) only when `2025-03-26` was negotiated — the only revision
whose base protocol has them; elsewhere a batch gets one `-32600`, and an empty
batch always does.

## Tools

| Tool | What it returns |
| --- | --- |
| `search_vault` | The `evidence-delivery-v1` packet from `eval/retrieve.py`: verbatim passages with `source_path` and `source_sha256`. `method` defaults to `fts`; `top_k`, `budget` and `per_source` override the server's defaults up to the [limits](#limits). Annotated not read-only: `method: synaptic` writes `.context/activation.json`, and the opt-in [session evidence ledger](#session-evidence-ledger) records paths and hashes. Decisions assessment and Responses generation are separate manual CLI commands. |
| `read_source` | One vault file verbatim, with its current SHA-256 and total length in characters. `start` offsets, `max_chars` defaults to 2000 and is at most 6000. Pass `sha256` to assert a version. Files over 64 MiB and non-UTF-8 files are refused. |
| `vault_status` | The `source-health-v1` report (see [source-lifecycle.md](source-lifecycle.md)): whether the index exists, when it was built, how many sources are in scope and whether any went stale. |
| `memory_record` | Appends one shared memory record (`decision`, `task`, `result` or `note`) with the sources it rests on, through the same store as `context-layer memory add` (see [memory.md](memory.md)). A repeated record is reported as a duplicate, not appended twice. A `result` names the tasks it completes in `closes`. |
| `memory_resume` | Recent shared memory records, the ones whose sources changed since they were written, and open tasks — the same packet as `context-layer memory resume`. |
| `graph_neighbors` | The explicit-link neighbours of one note (`path`, vault-relative): the notes it links to and the notes that link to it (wikilinks, embeds, Markdown links, frontmatter relations), each with the link kind and line. Paths only, no note text. `limit` caps each direction (default 25, at most 100). Links from a note that changed since the last `index` are withheld. Needs the link graph that `context-layer index` builds. |
| `read_packet` | A shared evidence packet by its `id` (the SHA-256 printed by `context-layer packet build`, see [subagents.md](subagents.md)). Every source is re-checked first; if any changed, vanished or became excluded, the whole packet is withheld (`status: WITHHELD`, with reasons). |
| `check_claims` | Checks claims against the passages they cite. Each claim is `{text, citations}` (optionally `id`), with citations `{source_path, source_sha256, line_start, line_end, span}`. The check verifies local boundaries, source hash and verbatim span; it does not call a model or prove the claim true. |

Every tool has a `title` and `annotations`: `readOnlyHint` is true for all but
`search_vault` and `memory_record`, `openWorldHint` is false for
all (the vault is local), and the tools that are
not read-only say `destructiveHint: false` (they append or overwrite only their
own derived files). Annotations are hints a client should
not trust (MCP tools spec). The `initialize` and `server/discover` results carry
`instructions`: the data-not-instructions rule, how to cite, and the limits.

`read_source` refuses anything outside the vault: absolute paths, `..`,
symlinked sources, paths excluded by `.context/routes.json`, and anything that is
not `.md`, `.txt`, `.json` or `.csv`. Exclusions are read before any file is
opened; an unreadable `routes.json` refuses the read rather than guessing.

## Session evidence ledger

Opt-in, and off unless both switches are on: the flag, and a
`<vault>/.context/session-evidence/` folder that already exists. Then every item
the hook or `search_vault` delivers is appended to
`.context/session-evidence/<session>.jsonl`, one line per item:

```json
{"schema":"session-evidence/v1","session":"s-1","channel":"hook","packet_id":"<64 hex>","path":"notes/release.md","sha256":"<64 hex>","at":"2026-09-28T20:00:00Z"}
```

Synaptic passages add `"lines": [start, end]`. The ledger holds ids, paths,
hashes and times, **never note text**; `packet_id` is a SHA-256 over the
delivered items' paths, hashes, line spans and lengths. It is what the rules Stop
check reads to tell cited evidence from delivered evidence.

- Hook: `--session-evidence` on the hook line; the session id is the hook JSON's
  `session_id`.
- Server: `CONTEXT_LAYER_SESSION_EVIDENCE=1` in its environment; the session id
  is `CLAUDE_CODE_SESSION_ID` (set by Claude Code for stdio MCP servers,
  code.claude.com/docs/en/env-vars) or `CLAUDE_SESSION_ID`. This build reads no
  Codex session variable, so under Codex only the hook records.
- `install ... --session-evidence` writes both switches and creates the folder
  on `--apply`. `uninstall` leaves the folder (it is vault data); delete it to
  remove the ledgers.

A session id that is not a plain file name (letters, digits, `.`, `_`, `-`, not
starting with a dot; at most 100 characters) is stored as `sid-<24 hex>.jsonl`,
the name the Stop check reads. A session ledger stops growing at 4 MiB: one
`{"overflow":true}` line marks it, and the Stop check then stays silent because a
missing citation cannot be told from a dropped line. When a new session file is
created, only the 50 most recently written session files are kept. A failed
write never fails the search.

## doctor

```sh
context-layer doctor --host claude-code --project /path/to/repo /path/to/vault
context-layer doctor --host codex /path/to/vault --json
```

Offline and read-only: it writes nothing, starts no host, hook or server, and
makes no network request. It reports, one row each, Python and SQLite FTS5,
`routes.json`, the index and link-graph format, CLAUDE.md/AGENTS.md parity (or
a single-source `CLAUDE.md` that is just `@AGENTS.md`), and per host:

- `claude-code`: the `.mcp.json` entry and every context-layer hook in
  `.claude/settings.json` and `.claude/settings.local.json`;
- `codex`: the `[mcp_servers.context_layer]` table in `$CODEX_HOME/config.toml`
  (TOML parsed on 3.11+; `tool_timeout_sec` above 120) and
  `.codex/hooks.json`;
- `generic`: the vault only.

Each MCP and hook line is parsed with this build's own parsers, so a flag or
value this binary rejects (a value above a cap, an unknown flag, a `rules hook`
event it does not know) fails; an unknown `--method` warns. The launcher, its
`PYTHONPATH` and the `--vault` it names must exist; a prompt hook whose timeout
does not exceed the hook's own 20 s warns. Exit 0 when nothing failed (warnings
allowed), 1 when a check failed. `python -m context_layer.doctor ...` runs the
same command.

## Uninstall and rollback

```sh
context-layer uninstall claude-code --vault /path/to/vault --project /path/to/repo   # diff
context-layer uninstall claude-code --vault /path/to/vault --project /path/to/repo --apply
context-layer uninstall codex --vault /path/to/vault --project /path/to/repo --apply
```

`uninstall` removes only what this tool added: the `context-layer` MCP entry,
its own `UserPromptSubmit`, `SessionStart`, `Stop` and `PostToolUse` hooks from
both `.claude/settings.json` and `.claude/settings.local.json` (Claude Code),
the marker block and `.codex/hooks.json` entries (Codex) — always all of them,
whatever flags the install used. A file it created and nothing else uses is
deleted without a backup, and a `.claude/` or `.codex/` folder that this removal
empties is removed; a file with other content keeps that content, loses only our
keys and is backed up first, so a rollback is `cp <name>.bak-<stamp> <name>`.

`install --plan-default` sets `permissions.defaultMode` to `"plan"` only when
no mode is set. An existing different mode (for example `"acceptEdits"`) is
refused with a one-line message and nothing is written; an existing `"plan"` is
left as the user's. When it does set the mode, it records that (and which
settings file it wrote) in `.claude/context-layer.plan-default.json`, and
`uninstall` removes a `"plan"` mode only when that marker is present.

Two limits of "byte-identical", both visible in the dry-run diff before you
apply: JSON configs are rewritten in the standard two-space form (a file already
in that form returns to its exact bytes), and a `config.toml` that did not end
with a newline gains one.

## Troubleshooting

**The host lists no tools.** Run `context-layer doctor --host <host>`, then the
configured command by hand — it should print
`context-layer mcp: serving vault '<vault folder name>'` on stderr and then
wait. A wrong `command`/`args` or a `PATH` the host does not share is the usual
cause; rerun `install … --apply` from the environment the host will use.

**Every search comes back `NOT_FOUND`.** Call `vault_status`. `index_present:
false` means the index was never built (`context-layer index /path/to/vault`),
and `markdown_files_in_scope: 0` means exclusions or the path are wrong.

**A packet has a `withheld` list, or the hook says "Not included: withheld N
source(s)".** A note changed or was deleted after the index was built. This is
the design: that note's text is left out rather than delivered with a hash that
no longer matches, and the rest of the packet is served. Re-run
`context-layer index /path/to/vault`.

**The hook says "N item(s) omitted".** The packet was larger than
`--max-context-chars`. Lower `--top-k`/`--per-source`, or read the named notes
with `read_source`.

**`read_source` returns "source changed".** The `sha256` you asked for is not the
file's current hash; the error carries the current one. Re-read without `sha256`,
or search again to get fresh evidence.

**"Excluded source" / "Source path escapes the vault" / "Symlink source is not
supported".** Boundary refusals, not bugs. Widen `exclude_prefixes` in
`.context/routes.json` if the exclusion was wrong; symlinked sources stay
unsupported, so copy the file into the vault instead.

**`install codex` refuses: the table is declared outside the markers.** An
earlier `codex mcp add context_layer ...` (or a hand edit) already defines the
server. Remove that table, or keep it and skip `install codex`.

**Permissions.** `install --apply` needs write access to the config file's
directory (`.mcp.json`, `.claude/`, `.codex/`, `$CODEX_HOME`) and creates a
missing `.claude/` or `.codex/`; the failure is an OS error on stderr and exit 1.
The server itself only reads the vault and reads and writes `.context/`. If the
host cannot read the vault, `search_vault` returns an error packet, never empty
success.

## Verified on

macOS (Darwin 25.6.0), CPython 3.12.4, by `tests/test_mcp_install.py`,
`tests/test_doctor.py` and the hook and first-run classes of
`tests/test_harden.py`, all against synthetic vaults and a temporary `HOME`
(tests that apply `--scope user|local` put a fake `claude` first on `PATH`):

- the server as a subprocess over real pipes: `initialize` for every supported
  revision and for unknown ones, 2026-07-28 requests without `initialize`
  (`server/discover`, `tools/list`, `tools/call`, `-32022`, `-32602`), `ping`,
  `tools/list` (ten tools, titles, annotations, every `maximum`),
  `search_vault` evidence with a path and a 64-hex hash, every limit refused as a
  tool error naming it, `read_source` (happy path, offset, the 6000 limit, hash
  mismatch, `..`, excluded prefix, dotted path, absolute path, missing file,
  directory, a 50 MB file within 8 MiB of the RSS of a 2 KB one, a file over 64
  MiB, non-UTF-8), `vault_status`, `memory_record` and `memory_resume`, unknown
  method `-32601`, unknown tool `-32602`, bad arguments `-32602` (2025-06-18) or
  a tool error (2025-11-25), a non-UTF-8 line, a lone-surrogate id, a null id, a
  missing `jsonrpc`, deep nesting, an 8 MiB line, batches under 2025-03-26 and
  2025-06-18, a client response left unanswered;
- the reader and the worker in-process over OS pipes: a ping answered while a
  search runs, a running and a queued search cancelled with no response and the
  retrieval worker's process tree gone, a full queue answered with a "busy" tool
  error; the warm worker reused across calls with the `search` packet, replaced
  after a timeout or after it died;
- installs: dry run writing nothing, `--apply` merging into an existing
  `.mcp.json` with a backup, the hook preserving an existing hook, `uninstall`
  restoring both files byte for byte, an empty project left empty after install
  and uninstall, `--scope local` writing `settings.local.json` and running a fake
  `claude mcp add --scope local` from the project, `--scope user` carrying
  `--env PYTHONPATH=` (and the printed server starting from another directory),
  Codex markers added, replaced idempotently and removed, a Codex table outside
  the markers refused in four spellings through the CLI and five with `tomllib`
  disabled (the Python 3.10 path), invalid
  TOML refused, `CODEX_HOME`, `tool_timeout_sec`, `.codex/hooks.json` written
  and its hook command run, `--format` snippets for seven hosts, a malformed
  config refused rather than overwritten;
- the hook: evidence with path and hash, `NOT_FOUND` printing nothing at exit 0,
  every command-line error and every runtime failure exiting 1 with an empty
  stdout and one stderr line, prompts
  `--help` and `-x` searched, an empty prompt answered with exit 0 and no output, a 2.1 MB prompt bounded,
  whole-item packing under three limits, escaped file-name headers, the withheld
  notice in the context, the session evidence ledger (hook and server, an unsafe
  session id, the 4 MiB limit and file pruning), `install --session-evidence`, the PostToolUse
  attribution hook with a rules module that offers it;
- `doctor`: a healthy install, bad hook flags, a short hook timeout, a missing
  interpreter, a wrong `PYTHONPATH`, a moved vault, rule-file parity, a missing
  index, a bad `routes.json`, Codex config and hooks — and that it wrote
  nothing.

Not verified here: a live Claude Code, Codex or any other host loading these
configs (every non-Claude-Code row and the Codex hooks are expected but
unverified), and `claude mcp add` actually executing. The
[accepted runtime run for `4dc60a9`](https://github.com/solisolsoli/context-layer/actions/runs/36773269705)
passed all 12 required jobs, including Ubuntu with Python 3.10–3.13, macOS with
Python 3.12 and native Windows with Python 3.12. These jobs check the CLI, local
fixtures and installed packages; they do not establish that every live host
loads the generated configuration. The acceptance walk in
[SCOPE.md](../SCOPE.md) covers the live part separately.
