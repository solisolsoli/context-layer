"""context_layer.doctor — offline, read-only checks of a vault and the host entries wired to it.

    context-layer doctor [--host claude-code|codex|generic] [--project DIR] [--json] VAULT

Every check only reads: no file is written, no host, model or network is contacted, and
no hook or server is started. The hook and MCP lines found in host config are parsed with
this build's own command-line parsers, so a line this binary would reject (an unknown
flag or value, a value above a cap, a `rules hook` event it does not know) is reported
before a host runs it. Exit 0 when no check failed (warnings allowed), 1 when one did.

Python 3.10+; standard library only (tomllib, when present, on 3.11+).
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import sys

from . import install, mcp_server

SCHEMA = "context-layer-doctor/v1"
OK, WARN, FAIL = "ok", "warn", "fail"
ENTRY_COMMANDS = ("hook", "rules", "mcp")


class FlagError(Exception):
    """A command line this build's parser rejects (it would exit 1 or 2)."""


class Report:
    def __init__(self):
        self.checks: list[dict] = []

    def add(self, name: str, status: str, detail: str) -> None:
        self.checks.append({"name": name, "status": status, "detail": detail})

    def count(self, status: str) -> int:
        return sum(1 for check in self.checks if check["status"] == status)


# ---------------------------------------------------------------------------
# This build's own parsers
# ---------------------------------------------------------------------------

def _raise(message):
    raise FlagError(message)


def _no_exit(parser: argparse.ArgumentParser) -> None:
    """Make a parser and every nested subparser raise FlagError instead of exiting."""
    parser.error = _raise
    parser.allow_abbrev = False
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                _no_exit(child)


def cli_parser() -> argparse.ArgumentParser:
    """`mcp`, `hook` and `rules` as this build defines them (cli.py wires the same)."""
    from . import rules
    parser = argparse.ArgumentParser(prog="context-layer", add_help=False, allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    mcp_server.register(sub)
    rules.register(sub)
    _no_exit(parser)
    return parser


def split_command(command: str) -> tuple[dict, list[str]]:
    """(leading VAR=value assignments, argv) of a shell-form hook command."""
    if os.name == "nt" or command.lower().startswith("powershell.exe "):
        windows = _split_powershell_hook(command)
        if windows is not None:
            return windows
    tokens = shlex.split(command)
    env = {}
    while tokens and "=" in tokens[0] and not tokens[0].startswith("-") \
            and tokens[0].split("=", 1)[0].replace("_", "").isalnum():
        key, value = tokens.pop(0).split("=", 1)
        env[key] = value
    return env, tokens


def _split_powershell_hook(command: str):
    """Parse only install.py's deterministic EncodedCommand grammar; run no shell."""
    try:
        tokens = shlex.split(command, posix=False)
        if (len(tokens) != 6 or Path(tokens[0].strip('"')).name.lower() != "powershell.exe"
                or tokens[1:4] != ["-NoLogo", "-NoProfile", "-NonInteractive"]
                or tokens[4] != "-EncodedCommand"):
            return None
        script = base64.b64decode(tokens[5], validate=True).decode("utf-16le")
    except (ValueError, IndexError):
        return None
    env = {}
    index = 0

    def space():
        nonlocal index
        while index < len(script) and script[index].isspace():
            index += 1

    def literal():
        nonlocal index
        space()
        if index >= len(script) or script[index] != "'":
            raise ValueError("expected a PowerShell single-quoted literal")
        index += 1
        result = []
        while index < len(script):
            char = script[index]
            index += 1
            if char == "'":
                if index < len(script) and script[index] == "'":
                    result.append("'")
                    index += 1
                    continue
                return "".join(result)
            result.append(char)
        raise ValueError("unterminated PowerShell literal")

    try:
        while script.startswith("$env:", index):
            end = script.find("=", index + 5)
            if end < 0:
                return None
            name = script[index + 5:end]
            if not name or any(not (ch.isalnum() or ch == "_") for ch in name):
                return None
            index = end + 1
            value = literal()
            env[name] = value
            space()
            if not script.startswith(";", index):
                return None
            index += 1
            space()
        if not script.startswith("&", index):
            return None
        index += 1
        argv = []
        while True:
            space()
            if script.startswith("; exit $LASTEXITCODE", index):
                index += len("; exit $LASTEXITCODE")
                space()
                if index != len(script):
                    return None
                break
            argv.append(literal())
        return env, argv
    except ValueError:
        return None


def launcher(argv: list[str]) -> tuple[list[str], list[str]]:
    """(launcher tokens, context-layer arguments): `context-layer ...` or
    `<python> -m context_layer.cli ...`."""
    for position, token in enumerate(argv):
        if token in ENTRY_COMMANDS and position:
            head = argv[:position]
            if len(head) == 1 or head[1:] == ["-m", "context_layer.cli"]:
                return head, argv[position:]
    return argv, []


def executable_problem(token: str) -> str | None:
    if os.sep in token or token.startswith("."):
        path = Path(token).expanduser()
        if not path.is_file():
            return f"{token} does not exist"
        if os.name != "nt" and not os.access(path, os.X_OK):
            return f"{token} is not executable"
        if os.name == "nt" and path.suffix.lower() not in (".exe", ".com", ".cmd", ".bat"):
            return f"{token} is not a Windows executable or command script"
        return None
    return None if shutil.which(token) else f"{token} is not on PATH"


def pythonpath_problem(env: dict) -> str | None:
    value = env.get("PYTHONPATH")
    if not value:
        return None
    roots = [Path(part) for part in value.split(os.pathsep) if part]
    if not any((root / "context_layer" / "__init__.py").is_file() for root in roots):
        return f"PYTHONPATH={value} holds no context_layer package"
    return None


def check_line(argv: list[str], env: dict) -> tuple[str, str]:
    """(status, detail) for one launch line, parsed as this build would parse it."""
    head, arguments = launcher(argv)
    if not arguments:
        return FAIL, "not a context-layer hook, rules or mcp command line"
    hard = [problem for problem in (executable_problem(head[0]), pythonpath_problem(env))
            if problem]
    soft = []
    try:
        args, extra = cli_parser().parse_known_args(arguments)
    except FlagError as exc:
        return FAIL, f"this build rejects the line: {mcp_server.one_line(exc)}"
    extra = [token for token in extra if token not in ("--", "--verbose")]
    if args.command in ("hook", "mcp"):
        args.rest = extra
        label = args.command
        messages = io.StringIO()
        with contextlib.redirect_stderr(messages):
            usable = mcp_server.open_vault(args, label)
        if usable is None:
            return FAIL, f"this build rejects the line: {mcp_server.one_line(messages.getvalue())}"
        if args.command == "hook" and args.method not in mcp_server.HOOK_METHODS:
            soft.append(f"--method {args.method!r} is unknown here; the hook would run fts")
    else:
        if extra:
            return FAIL, f"this build rejects the line: unrecognised arguments: {' '.join(extra)}"
        if args.command == "rules" and getattr(args, "rules_command", None) == "hook":
            from . import rules
            if getattr(args, "event", None) not in rules.HOOK_EVENTS.values():
                return FAIL, (f"this build rejects the line: unknown rules hook event "
                              f"{getattr(args, 'event', None)!r}; known: "
                              + ", ".join(rules.HOOK_EVENTS.values()))
        vault = getattr(args, "vault", None)
        if vault and not Path(vault).expanduser().is_dir():
            return FAIL, f"vault not found: {vault}"
    if hard or soft:
        return (FAIL if hard else WARN), "; ".join(hard + soft)
    return OK, f"{' '.join(arguments[:2])} parses; launcher and vault exist"


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_runtime(report: Report) -> None:
    version = ".".join(str(part) for part in sys.version_info[:3])
    report.add("python", OK if sys.version_info >= (3, 10) else FAIL,
               f"{version} ({sys.executable})")
    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE VIRTUAL TABLE probe USING fts5(text)")
        finally:
            connection.close()
        report.add("sqlite fts5", OK, f"SQLite {sqlite3.sqlite_version}")
    except sqlite3.Error as exc:
        report.add("sqlite fts5", FAIL, f"SQLite {sqlite3.sqlite_version} has no FTS5 ({exc}); "
                                        "index and search need it")


def check_vault(report: Report, vault: Path) -> bool:
    if not vault.is_dir():
        report.add("vault", FAIL, f"not a directory: {vault}")
        return False
    report.add("vault", OK, str(vault))
    source_policy = mcp_server.policy()
    formats = mcp_server.router_module("index_format")
    config = source_policy.config_path(vault)
    if not config.is_file():
        report.add("routes.json", FAIL, "missing; run `context-layer init <vault>`")
    else:
        try:
            loaded = source_policy.load_config(config, required=True)
            report.add("routes.json", OK, f"schema_version {loaded.get('schema_version', 1)}, "
                                          f"{len(loaded.get('routes') or {})} route(s), "
                                          f"{len(source_policy.config_exclusions(loaded))} "
                                          "exclusion(s)")
        except ValueError as exc:
            report.add("routes.json", FAIL, mcp_server.one_line(exc))
    index = vault / ".context" / "index.sqlite"
    try:
        connection = formats.open_checked(index)
        try:
            records = connection.execute("SELECT count(*) FROM records").fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()
        report.add("index.sqlite", OK, f"format {version or 1}, {records} record(s)")
    except (ValueError, sqlite3.Error) as exc:
        report.add("index.sqlite", FAIL, mcp_server.one_line(exc))
    graph = vault / ".context" / "graph.sqlite"
    if not graph.is_file():
        report.add("graph.sqlite", WARN, "missing; `--method synaptic` needs it (run "
                                         "`context-layer index <vault>`)")
    else:
        try:
            formats.check_graph_file(graph)
            report.add("graph.sqlite", OK, f"format up to {formats.GRAPH_FORMAT_VERSION}")
        except (ValueError, sqlite3.Error) as exc:
            report.add("graph.sqlite", FAIL, mcp_server.one_line(exc))
    return True


def check_rules(report: Report, vault: Path) -> None:
    from . import rules
    result = rules.parity(vault)
    files = result.get("files") or {}
    present = [name for name, info in files.items() if info.get("exists")]
    if result.get("ok"):
        report.add("rule files", OK, "CLAUDE.md and AGENTS.md are byte-identical")
    elif not present:
        report.add("rule files", OK, "none (optional; `context-layer rules init` adds them)")
    else:
        claude = vault / "CLAUDE.md"
        text = claude.read_text(encoding="utf-8", errors="replace") if claude.is_file() else ""
        if (vault / "AGENTS.md").is_file() and text.strip() == "@AGENTS.md":
            report.add("rule files", OK, "single source: CLAUDE.md imports AGENTS.md")
        else:
            report.add("rule files", FAIL, "; ".join(result.get("problems") or ["differ"]))


def entries_in_settings(data: dict) -> list[tuple[str, dict]]:
    """(event, handler) for every hook handler this tool may have written."""
    found = []
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return found
    for event, groups in hooks.items():
        for group in groups if isinstance(groups, list) else []:
            for handler in (group.get("hooks") or []) if isinstance(group, dict) else []:
                if isinstance(handler, dict) and install.mine(str(handler.get("command", ""))):
                    found.append((event, handler))
    return found


def check_hook_file(report: Report, path: Path) -> None:
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report.add(path.name, FAIL, f"{path} is not readable JSON: {mcp_server.one_line(exc)}")
        return
    entries = entries_in_settings(data)
    if not entries:
        report.add(path.name, OK, f"{path}: no context-layer hooks")
        return
    for event, handler in entries:
        name = f"{path.name} {event}"
        try:
            env, argv = split_command(str(handler.get("command", "")))
        except ValueError as exc:
            report.add(name, FAIL, f"the command does not parse as a shell line: {exc}")
            continue
        status, detail = check_line(argv, env)
        timeout = handler.get("timeout")
        if status == OK and event == install.HOOK_EVENT and (
                not isinstance(timeout, int) or timeout <= mcp_server.HOOK_TIMEOUT):
            status, detail = WARN, (f"{detail}; timeout {timeout!r} does not exceed the hook's "
                                    f"own {mcp_server.HOOK_TIMEOUT} s limit")
        report.add(name, status, detail)


def check_mcp_entry(report: Report, name: str, entry) -> None:
    if not isinstance(entry, dict):
        report.add(name, FAIL, "the entry is not an object")
        return
    command = entry.get("command")
    args = entry.get("args") or []
    if not isinstance(command, str) or not isinstance(args, list):
        report.add(name, FAIL, "command must be a string and args a list")
        return
    env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    status, detail = check_line([command, *[str(a) for a in args]], env)
    report.add(name, status, detail)


def check_claude_code(report: Report, project: Path) -> None:
    config = project / ".mcp.json"
    if config.is_file():
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
            entry = (data.get("mcpServers") or {}).get(install.SERVER_NAME)
        except (OSError, ValueError, AttributeError) as exc:
            report.add(".mcp.json", FAIL, f"{config} is not readable JSON: {mcp_server.one_line(exc)}")
            entry = None
        else:
            if entry is None:
                report.add(".mcp.json", WARN, f"{config}: no context-layer server in mcpServers")
            else:
                check_mcp_entry(report, ".mcp.json context-layer", entry)
    else:
        report.add(".mcp.json", WARN, f"none in {project}; a user- or local-scope server lives "
                                      "in ~/.claude.json (see `claude mcp list`)")
    for settings in ("settings.json", "settings.local.json"):
        check_hook_file(report, project / ".claude" / settings)


def check_codex(report: Report, project: Path) -> None:
    try:
        home = install.codex_home()
    except ValueError as exc:
        report.add("CODEX_HOME", FAIL, mcp_server.one_line(exc))
        return
    config = home / "config.toml"
    if not config.is_file():
        report.add("config.toml", WARN, f"none at {config}")
    else:
        text = config.read_text(encoding="utf-8", errors="replace")
        problem = install.toml_problem(text)
        if problem:
            report.add("config.toml", FAIL, f"{config} is not valid TOML: {problem}")
        elif install.tomllib is None:
            ours = install.START in text
            report.add("config.toml", OK if ours else WARN,
                       ("context-layer block present (Python 3.10: TOML not parsed)" if ours
                        else "no context-layer block"))
        else:
            data = install.tomllib.loads(text)
            entry = (data.get("mcp_servers") or {}).get("context_layer")
            if entry is None:
                report.add("config.toml", WARN, f"{config}: no [mcp_servers.context_layer] table")
            else:
                check_mcp_entry(report, f"config.toml [{install.CODEX_TABLE}]", entry)
                timeout = entry.get("tool_timeout_sec") if isinstance(entry, dict) else None
                if not isinstance(timeout, (int, float)) or timeout <= mcp_server.SEARCH_TIMEOUT:
                    report.add("codex tool_timeout_sec", WARN,
                               f"{timeout!r}: a search may take {mcp_server.SEARCH_TIMEOUT} s "
                               "and Codex's default is 60 s")
    check_hook_file(report, project / ".codex" / "hooks.json")


def diagnose(vault: Path, host: str, project: Path) -> Report:
    report = Report()
    check_runtime(report)
    if check_vault(report, vault):
        check_rules(report, vault)
    if host == "claude-code":
        check_claude_code(report, project)
    elif host == "codex":
        check_codex(report, project)
    return report


def render(report: Report) -> str:
    width = max([len(check["name"]) for check in report.checks] + [5])
    lines = [f"{'check'.ljust(width)}  status  detail"]
    for check in report.checks:
        lines.append(f"{check['name'].ljust(width)}  {check['status'].ljust(6)}  "
                     f"{check['detail']}")
    lines.append(f"doctor: {report.count(FAIL)} failed, {report.count(WARN)} warning(s), "
                 f"{report.count(OK)} ok")
    return "\n".join(lines) + "\n"


def cmd_doctor(args: argparse.Namespace) -> int:
    extra = [token for token in getattr(args, "rest", []) if token]
    if extra:
        print(f"context-layer doctor: unrecognised arguments: {' '.join(extra)}", file=sys.stderr)
        return 2
    vault = Path(args.vault).expanduser().resolve()
    project = Path(args.project).expanduser().resolve() if args.project else vault
    report = diagnose(vault, args.host, project)
    if args.as_json:
        print(json.dumps({"schema": SCHEMA, "host": args.host, "checks": report.checks,
                          "failed": report.count(FAIL), "warnings": report.count(WARN)},
                         indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(render(report))
    return 1 if report.count(FAIL) else 0


def register(sub: argparse._SubParsersAction) -> None:
    """Attach `doctor` to the CLI."""
    parser = sub.add_parser(
        "doctor",
        help="Check a vault and its host wiring, offline and read-only.",
        description="Reports Python and SQLite FTS5, routes.json, the index and link graph "
                    "format, rule-file parity, and, per host, the MCP entry and hook lines: "
                    "each is parsed with this build's own flags, and its launcher, PYTHONPATH "
                    "and vault must exist. Writes nothing and runs no host. Exit 0 when no "
                    "check failed, 1 when one did.")
    parser.add_argument("vault")
    parser.add_argument("--host", choices=list(install.HOSTS), default="generic",
                        help="Whose config to check (default: generic, the vault only).")
    parser.add_argument("--project", default=None,
                        help="Project directory holding .mcp.json, .claude/ and .codex/ "
                             "(default: the vault).")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="Print the checks as JSON.")
    parser.set_defaults(func=cmd_doctor, forward_to=None)


def main(argv: list[str] | None = None) -> int:
    """`python -m context_layer.doctor ...`, the same command without the CLI wrapper."""
    parser = argparse.ArgumentParser(prog="context-layer", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    register(sub)
    args, extra = parser.parse_known_args(["doctor", *(sys.argv[1:] if argv is None else argv)])
    args.rest = extra
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
