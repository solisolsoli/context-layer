#!/usr/bin/env python3
"""`context-layer` — one command group around the router, indexer and eval harness.

This is a wrapper, not a replacement. `router/build_index.py`,
`router/context_router.py` and `eval/evaluate.py` stay runnable directly with
`python3`; this module just finds them and forwards argv, so there is exactly
one entry point to learn and no second copy of the logic to drift.

Python 3.10+; standard library only.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import sys

from . import __version__
try:
    from .router.textio import configure_stdout
except ImportError:
    from router.textio import configure_stdout


class _LazyModule:
    """A component module imported on first attribute access, so a command pays only
    for the modules it uses (`--version` and `index` never import the MCP server's
    advisor, the task runner or the installers)."""

    def __init__(self, name: str, qualified: "str | None" = None):
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_qualified", qualified or f"{__package__}.{name}")

    def _load(self):
        module = importlib.import_module(self._qualified)
        globals()[self._name] = module          # later lookups skip the proxy
        return module

    def __getattr__(self, attribute: str):
        return getattr(self._load(), attribute)

    # A reference taken before the first use (`patch.object(cli.subprocess, ...)`)
    # still reaches the real module.
    def __setattr__(self, attribute: str, value) -> None:
        setattr(self._load(), attribute, value)

    def __delattr__(self, attribute: str) -> None:
        delattr(self._load(), attribute)


# 0.2+ components own one module each; each module's register() adds these commands.
# tests/test_harden.py checks this table against what every register() really adds.
COMPONENT_COMMANDS = {
    "mcp_server": ("hook", "mcp"), "install": ("install", "uninstall"), "memory": ("memory",),
    "tasks": ("tasks",), "health": ("rollback", "status"), "rules": ("rules",),
    "brain": ("brain",), "orchestrate": ("handback", "handoff", "job", "packet"),
    "jev": ("jev",), "graph": ("graph",), "doctor": ("doctor",), "brief": ("brief",),
    "session_show": ("session",),
}
COMPONENTS = tuple(COMPONENT_COMMANDS)
brain = brief = doctor = graph = health = install = jev = mcp_server = memory = None
orchestrate = rules = session_show = tasks = None
for _name in COMPONENTS:
    globals()[_name] = _LazyModule(_name)
del _name
sqlite3 = _LazyModule("sqlite3", "sqlite3")             # only a few commands need these
subprocess = _LazyModule("subprocess", "subprocess")


def repo_home() -> Path:
    """Locate the checkout that holds router/ and eval/.

    A wheel bundles router/eval beneath context_layer; an editable checkout
    keeps them at the repository root. CONTEXT_LAYER_HOME is an optional override.
    """
    override = os.environ.get("CONTEXT_LAYER_HOME")
    if override:
        candidate = Path(override).expanduser().resolve()
        if (candidate / "router" / "context_router.py").is_file():
            return candidate
        raise SystemExit(
            f"context-layer: CONTEXT_LAYER_HOME={candidate} does not contain "
            "router/context_router.py"
        )
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "router" / "context_router.py").is_file():
            return parent
    raise SystemExit(
        "context-layer: could not find the router/ and eval/ directories.\n"
        "This wrapper resolves them relative to the installed package, which "
        "works for an editable install (pip install -e .). Set "
        "CONTEXT_LAYER_HOME=/path/to/the/checkout to point it somewhere else."
    )


VERBOSE = False  # set by --verbose; the command trace carries absolute paths


def trace(command: list[str]) -> None:
    """Echo a forwarded command to stderr, only with --verbose: it names the absolute
    interpreter and script paths, which do not belong in logs or screenshots."""
    if VERBOSE:
        print("+ " + " ".join(command), file=sys.stderr)


def run_script(relative: str, argv: list[str], cwd: Path | None = None) -> int:
    script = repo_home() / relative
    command = [sys.executable, str(script), *argv]
    trace(command)
    return subprocess.call(command, cwd=str(cwd) if cwd else None)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    # init has nothing underneath it to forward to, so an unknown flag is a typo.
    if args.rest:
        print(f"context-layer init: unrecognised arguments: {' '.join(args.rest)}",
              file=sys.stderr)
        return 2
    vault = Path(args.vault).expanduser()
    if not vault.is_dir():
        print(f"context-layer init: vault not found: {args.vault}", file=sys.stderr)
        return 1
    out = Path(args.out).expanduser() if args.out else vault.resolve() / ".context" / "routes.json"
    if out.exists() and not args.force and not args.print_only:
        shown = ".context/routes.json" if not args.out else args.out
        print(f"context-layer init: {shown} already exists. Use --force to overwrite "
              f"it, or --print-only to see what would be generated.", file=sys.stderr)
        return 1

    from .vault_scan import print_report, render_config, scan_vault
    result = scan_vault(vault, max_routes=args.max_routes)
    config = render_config(result)
    written = False
    if not args.print_only:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        written = True
    print_report(result, out, written)
    if args.print_only:
        print("")
        print(json.dumps(config, indent=2, ensure_ascii=False))
    if not result.routes:
        return 1
    return 0


def index_out_path(rest: list[str]) -> Path | None:
    """The index file the builder was asked to write (`--out PATH` or `--out=PATH`).

    Both spellings are argparse's in router/build_index.py; the last one wins there,
    so it wins here, and the link graph is built from the file the builder wrote.
    """
    found = None
    for position, token in enumerate(rest):
        if token == "--out" and position + 1 < len(rest):
            found = rest[position + 1]
        elif token.startswith("--out="):
            found = token[len("--out="):]
    return Path(found).expanduser() if found else None


def cmd_index(args: argparse.Namespace) -> int:
    vault = Path(args.vault).expanduser()
    custom_config = any(a == "--config" or a.startswith("--config=") for a in args.rest)
    if vault.is_dir() and not custom_config \
            and not (vault / ".context" / "routes.json").is_file():
        print("context-layer index: no .context/routes.json, so this index has no exclusions; "
              "run `context-layer init <vault>` first to write one", file=sys.stderr)
    # The builder runs in this process (no second interpreter start); router/build_index.py
    # stays runnable on its own. What it read and hashed in this run is handed to the graph.
    builder = graph.router_module("build_index")
    argv = ["--vault", str(vault)] + args.rest
    trace(["build_index.py", *argv])
    result: dict = {}
    try:
        code = builder.main(argv, result)
    except SystemExit as exc:              # argparse: --help (0) or a usage error (2)
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    if code != 0:
        return code
    # The build kept the replaced index as index.sqlite.prev; keep the link graph that
    # belongs with it, so `rollback` restores the pair. With --no-graph the graph is
    # left as it is, and its .prev is a copy of the same bytes.
    graph_file = graph.graph_path(vault)
    if graph_file.is_file():
        builder.keep_previous(graph_file)
    if args.no_graph or not result:
        return code
    # The link graph is derived from exactly the notes the index just covered.
    index = index_out_path(args.rest)
    try:
        summary = graph.build(vault, index=index,
                              verified={source.path: source for source in result["sources"]},
                              reuse="--full" not in args.rest)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"context-layer index: link graph not built: {exc}", file=sys.stderr)
        return 1
    try:
        shown = Path(summary["graph"]).resolve().relative_to(vault.resolve()).as_posix()
    except ValueError:
        shown = Path(summary["graph"]).name
    print(f"graph: {summary['notes']} notes, {summary['edges']} edges, "
          f"{summary['unresolved']} unresolved links ({summary['unresolved_ambiguous']} "
          f"ambiguous) -> {shown} in {summary['seconds']:.3f}s"
          + (" (unchanged)" if summary.get("unchanged") else ""))
    return 0


def cmd_route(args: argparse.Namespace) -> int:
    argv = ["--vault", str(Path(args.vault).expanduser())]
    if args.prompt is not None:
        argv += ["--prompt", args.prompt]
    return run_script("router/context_router.py", argv + args.rest)


def search_error(message: str) -> int:
    """First-run and integrity failures: an ERROR packet on stdout (callers parse it),
    one plain line on stderr (people read it). Exit 1 = operational error."""
    print(json.dumps(mcp_server.failed_packet(message), ensure_ascii=False))
    print(f"context-layer search: {message}", file=sys.stderr)
    return 1


def cmd_search(args: argparse.Namespace) -> int:
    vault = Path(args.vault).expanduser()
    if not vault.is_dir():
        return search_error(f"vault not found: {args.vault}")
    problem = mcp_server.preflight(vault.resolve(), args.method)
    if problem:
        return search_error(problem)
    # --jev asks the optional advisor (docs/jev.md). Without a usable configuration, in
    # mode off or with a kill switch, the plan is None and this is the plain search below,
    # byte for byte; the provider client is never imported on that path.
    advisor = jev.search_plan(vault.resolve(), args.method, args.rest) \
        if args.jev and not args.no_jev else None
    command = [sys.executable, str(repo_home() / "eval/retrieve.py"),
               "--vault", str(vault), "--method", args.method, *args.rest,
               *(advisor.retrieve_args() if advisor else []), args.prompt]
    trace(command)
    done = subprocess.run(command, stdout=subprocess.PIPE)
    text = done.stdout.decode("utf-8", "replace")
    try:
        packet = json.loads(text)
    except ValueError:
        packet = None
    if isinstance(packet, dict) and packet.get("operation_status") == "error":
        # retrieve.py labels its error packet PARTIAL; PARTIAL means evidence was found.
        packet = mcp_server.as_error(packet)
        print(json.dumps(packet, ensure_ascii=False))
        print(f"context-layer search: {packet.get('error')}", file=sys.stderr)
        return done.returncode or 1
    if advisor is not None and isinstance(packet, dict):
        packet = jev.advise_search(vault.resolve(), args.prompt, args.method, packet, advisor)
        text = json.dumps(packet, ensure_ascii=False, separators=(",", ":")) + "\n"
    if args.github and not args.no_github and done.returncode == 0 and isinstance(packet, dict):
        augmented = mcp_server.github_fallback(vault.resolve(), args.prompt, packet)
        if augmented is not packet:
            packet = augmented
            text = json.dumps(packet, ensure_ascii=False, separators=(",", ":")) + "\n"
    sys.stdout.write(text)
    note = mcp_server.withheld_note(packet)
    if note:
        print(f"context-layer search: {note}", file=sys.stderr)
    return done.returncode


def cmd_github_context(args: argparse.Namespace) -> int:
    """Explicit external evidence lookup when the host identifies a knowledge gap."""
    if args.rest:
        print("context-layer github-context: unrecognised arguments", file=sys.stderr)
        return 2
    from . import github_context
    packet = github_context.fetch(Path(args.vault).expanduser(), args.prompt, args.source,
                                  offline=args.offline, force_refresh=args.refresh)
    print(json.dumps(packet, ensure_ascii=False))
    return 1 if packet.get("status") == "ERROR" else 0


def cmd_github_sources(args: argparse.Namespace) -> int:
    """Owner-facing source controls; writes require the explicit --apply flag."""
    if args.rest:
        print("context-layer github-sources: unrecognised arguments", file=sys.stderr)
        return 2
    from . import github_sources
    vault = Path(args.vault).expanduser()
    action = args.source_action
    try:
        if action == "list":
            result = github_sources.list_sources(vault)
        elif action == "add":
            result = github_sources.add_source(vault, args.id, args.repo, args.ref,
                                               args.path, args.keyword, apply=args.apply)
        elif action == "remove":
            result = github_sources.remove_source(vault, args.id, apply=args.apply)
        elif action in ("enable", "disable"):
            result = github_sources.set_enabled(vault, action == "enable", apply=args.apply)
        elif action == "check":
            result = github_sources.check_source(vault, args.id, upstream_ref=args.ref)
        else:
            result = github_sources.update_source(vault, args.id, args.commit,
                                                  args.expected_commit, apply=args.apply)
    except (ValueError, OSError):
        result = {"status": "ERROR", "error": "source_operation_failed"}
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result.get("status") == "ERROR" else 0


def cmd_github_cache(args: argparse.Namespace) -> int:
    """Explicit, bounded local cache controls, separate from source pin updates."""
    if args.rest:
        print("context-layer github-cache: unrecognised arguments", file=sys.stderr)
        return 2
    from . import github_sources
    vault = Path(args.vault).expanduser()
    action = args.cache_action
    try:
        if action == "status":
            result = github_sources.cache_status(vault)
        elif action == "purge":
            result = github_sources.purge_cache(vault, apply=args.apply)
        else:
            result = github_sources.set_cache_enabled(vault, action == "enable", apply=args.apply)
    except (ValueError, OSError):
        result = {"status": "ERROR", "error": "cache_operation_failed"}
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result.get("status") == "ERROR" else 0


def cmd_eval(args: argparse.Namespace) -> int:
    # The harness's own examples use paths relative to eval/ (fixtures/,
    # stimulus-set.example.jsonl), so it runs from there unless told otherwise.
    cwd = Path(args.cwd).expanduser() if args.cwd else repo_home() / "eval"
    return run_script("eval/evaluate.py", args.rest, cwd=cwd)


# ---------------------------------------------------------------------------

def build_parser(command: "str | None" = None) -> argparse.ArgumentParser:
    """The CLI parser. With `command`, only the component that owns it registers its
    sub-commands (the others are not imported); `--help`, no command or an unknown one
    register every component, so help and error messages list them all."""
    parser = argparse.ArgumentParser(
        prog="context-layer",
        allow_abbrev=False,
        description="Verbatim, hash-pinned evidence from a Markdown vault for an AI host: "
                    "index the vault, search it, serve it over MCP or a prompt hook, and "
                    "check that the index still matches the notes. Exit codes: docs/cli.md.",
        epilog="Flags this wrapper does not define are forwarded verbatim to the script "
               "underneath (router/build_index.py, router/context_router.py, "
               "eval/evaluate.py), which are all still runnable directly with python3.",
    )
    parser.add_argument("--version", action="version", version=f"context-layer {__version__}")
    parser.add_argument("--verbose", action="store_true",
                        help="Echo forwarded commands (with absolute paths) to stderr.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser(
        "init",
        help="Scan an existing vault and write a starting routes.json.",
        description="Walks the vault, infers routes from folders, filename tokens, "
                    "headings and file sizes, pre-fills noise exclusions, prints what "
                    "it inferred and why, and writes <vault>/.context/routes.json. "
                    "Every inferred route is a guess you are expected to correct.",
    )
    p_init.add_argument("vault")
    p_init.add_argument("--out", default=None,
                        help="Config path (default: <vault>/.context/routes.json).")
    p_init.add_argument("--force", action="store_true", help="Overwrite an existing config.")
    p_init.add_argument("--print-only", action="store_true",
                        help="Print the report and the JSON; write nothing.")
    p_init.add_argument("--max-routes", type=int, default=8,
                        help="Maximum number of routes to propose (default: 8).")
    p_init.set_defaults(func=cmd_init, forward_to=None)

    p_index = sub.add_parser(
        "index", help="Build the SQLite/FTS5 index and the link graph over the vault.",
        description="Builds <vault>/.context/index.sqlite (router/build_index.py), then "
                    "extracts the notes' explicit links into <vault>/.context/graph.sqlite "
                    "for `search --method synaptic`. Both are replaced atomically. Only changed notes "
                    "are written again unless --full asks for a complete rebuild.")
    p_index.add_argument("vault")
    p_index.add_argument("--no-graph", action="store_true",
                         help="Build the lexical index only; skip the link graph.")
    p_index.set_defaults(func=cmd_index, forward_to="router/build_index.py")

    p_route = sub.add_parser("route", help="Build a context packet for one prompt.")
    p_route.add_argument("vault")
    p_route.add_argument("--prompt", default=None, help="The exact user prompt.")
    p_route.set_defaults(func=cmd_route, forward_to="router/context_router.py")

    p_search = sub.add_parser(
        "search", help="Return source evidence with the FTS baseline (default).",
        description="Prints an evidence-delivery-v1 packet. --method synaptic (opt-in, "
                    "experimental) returns the unchanged fts packet plus link-graph extras "
                    "within --extra-tokens N (default 600, estimated as ceil(chars/4)); "
                    "--compact switches to the smaller packer sized by --budget-tokens N "
                    "(default 1200), which may drop fts evidence. It also accepts "
                    "--max-hops 1|2 (default 1) and --record-query.")
    p_search.add_argument("vault")
    p_search.add_argument("--prompt", required=True)
    p_search.add_argument("--method", choices=["grep", "fts", "fts-canonical", "router", "synaptic"],
                          default="fts")
    p_search.add_argument("--jev", action="store_true",
                          help="Ask the optional advisor about this packet (docs/jev.md; fts and "
                               "default synaptic). Unconfigured, off or killed: the plain search.")
    p_search.add_argument("--no-jev", action="store_true",
                          help="Never ask the advisor for this call (wins over --jev).")
    p_search.add_argument("--github", action="store_true",
                          help="On a clean local NOT_FOUND, fetch configured public GitHub "
                               "sources as external_context (docs/github-context.md).")
    p_search.add_argument("--no-github", action="store_true",
                          help="Never fetch GitHub context for this call (wins over --github).")
    p_search.set_defaults(func=cmd_search, forward_to="eval/retrieve.py")

    p_github = sub.add_parser("github-context", help="Fetch allowlisted GitHub evidence for a gap.")
    p_github.add_argument("vault")
    p_github.add_argument("--prompt", required=True, help="Matched locally; never sent to GitHub.")
    p_github.add_argument("--source", action="append",
                          help="Configured source id (repeatable); otherwise match local keywords.")
    github_mode = p_github.add_mutually_exclusive_group()
    github_mode.add_argument("--offline", action="store_true",
                             help="Use verified cached files only; never open a network connection.")
    github_mode.add_argument("--refresh", action="store_true",
                             help="Fetch the pinned version again instead of reusing the cache.")
    p_github.set_defaults(func=cmd_github_context)

    p_sources = sub.add_parser("github-sources", help="Configure and review pinned GitHub sources.")
    source_actions = p_sources.add_subparsers(dest="source_action", required=True)
    for action in ("list", "add", "remove", "enable", "disable", "check", "update"):
        child = source_actions.add_parser(action, allow_abbrev=False)
        child.add_argument("vault")
        child.set_defaults(func=cmd_github_sources)
        if action in ("add", "remove", "check", "update"):
            child.add_argument("--id", required=True, help="Configured source id.")
        if action == "add":
            child.add_argument("--repo", required=True, help="Public owner/repository.")
            child.add_argument("--ref", required=True, help="Commit SHA, branch or tag to pin.")
            child.add_argument("--path", action="append", required=True, help="Allowed file; repeatable.")
            child.add_argument("--keyword", action="append", required=True,
                               help="Locally matched topic phrase; repeatable.")
        if action == "check":
            child.add_argument("--ref", help="Upstream branch/tag; defaults to the recorded ref.")
        if action == "update":
            child.add_argument("--commit", required=True, help="Reviewed new full commit SHA.")
            child.add_argument("--expected-commit", required=True,
                               help="Current full SHA from the preview; refuses a changed pin.")
        if action not in ("list", "check"):
            child.add_argument("--apply", action="store_true",
                               help="Write the reviewed change; otherwise only preview it.")

    p_cache = sub.add_parser("github-cache", help="Control the optional verified local GitHub cache.")
    cache_actions = p_cache.add_subparsers(dest="cache_action", required=True)
    for action in ("status", "enable", "disable", "purge"):
        child = cache_actions.add_parser(action, allow_abbrev=False)
        child.add_argument("vault")
        child.set_defaults(func=cmd_github_cache)
        if action != "status":
            child.add_argument("--apply", action="store_true",
                               help="Apply this cache change; otherwise only preview it.")

    p_eval = sub.add_parser("eval", help="Run the hit/cost evaluation harness.")
    p_eval.add_argument("--cwd", default=None,
                        help="Directory to run from (default: the repo's eval/).")
    p_eval.set_defaults(func=cmd_eval, forward_to="eval/evaluate.py")

    # 0.2 phases own one module each so parallel work never edits this block.
    owners = [name for name, commands in COMPONENT_COMMANDS.items() if command in commands]
    if command is None or command not in sub.choices:
        owners = owners or list(COMPONENTS)
    for name in owners:
        globals()[name].register(sub)
    # A prefix of a flag (--pro for --prompt) is not silently accepted anywhere.
    for child in sub.choices.values():
        child.allow_abbrev = False
    return parser


def _command(tokens: "list[str]") -> "str | None":
    """The sub-command a command line names, or None when it is not plain (help,
    version, an option before the command): then the full parser is built."""
    for token in tokens:
        if token == "--verbose":
            continue
        return None if token.startswith("-") else token
    return None


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    # Anything this wrapper does not define is forwarded verbatim to the script
    # underneath, so every flag documented in router/README.md and eval/README.md
    # keeps working through the CLI. A bare `--` separator is accepted and
    # dropped. The trade-off is deliberate and worth stating: a mistyped flag is
    # forwarded rather than rejected here, and the underlying script reports it.
    global VERBOSE
    tokens = sys.argv[1:] if argv is None else list(argv)
    if tokens == ["--version"]:                  # answered before any parser is built
        print(f"context-layer {__version__}")
        return 0
    args, extra = build_parser(_command(tokens)).parse_known_args(tokens)
    # --verbose is accepted before or after the subcommand; it is never forwarded.
    VERBOSE = bool(args.verbose or "--verbose" in extra)
    args.rest = [token for token in extra if token not in ("--", "--verbose")]
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
