"""context_layer.install — connect a host to the MCP server, and disconnect it again.

Dry run is the default: every command prints a unified diff of what it would do
and writes nothing. `--apply` writes, after copying the previous file to
`<name>.bak-<UTC stamp>` beside it. A file that holds nothing but this tool's own
entries gets no backup (there is nothing of anyone else's to keep), so an install
followed by an uninstall leaves no file of this tool behind. `uninstall` removes only
this tool's own keys and markers: a config nothing else touched returns to its
pre-install bytes, and a `.claude/` or `.codex/` folder it emptied is removed.

Diffs and config snippets go to stdout; notes and warnings go to stderr.

Python 3.10+; standard library only (tomllib, when present, on 3.11+).
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import datetime as dt
import difflib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

from . import mcp_server

if sys.version_info >= (3, 11):
    import tomllib
else:                                   # Python 3.10: a table-header scan instead
    tomllib = None

SERVER_NAME = "context-layer"
HOSTS = ("claude-code", "codex", "generic")
SCOPES = ("project", "local", "user")
HOOK_EVENT = "UserPromptSubmit"
# Seconds the host may give our prompt hook. Claude Code's default for a command
# hook on UserPromptSubmit is also 30 s (code.claude.com/docs/en/hooks); writing it
# keeps the hook's own 20 s retrieval limit (mcp_server.HOOK_TIMEOUT) below the
# host's even if that default changes.
HOOK_TIMEOUT_S = 30
HOOK_METHODS = ("fts", "synaptic")
START = "# >>> context-layer >>>"
END = "# <<< context-layer <<<"
CODEX_TABLE = "mcp_servers.context_layer"
# Codex's documented tool_timeout_sec default is 60 s (learn.chatgpt.com/docs/extend/mcp);
# one search may take up to mcp_server.SEARCH_TIMEOUT (120 s), so the block allows more.
CODEX_TOOL_TIMEOUT_S = 180
UNVERIFIED = ("codex: expected but unverified: the file follows the Codex docs, but no Codex "
              "CLI ran on the reference machine")
PLAN_MARKER = "context-layer.plan-default.json"
RULES_EVENTS = {"SessionStart": "session-start", "Stop": "stop"}
ATTRIBUTION_EVENT = "PostToolUse"
ATTRIBUTION_MATCHER = "Write|Edit|MultiEdit|NotebookEdit"
OUR_EVENTS = (HOOK_EVENT, "SessionStart", "Stop", ATTRIBUTION_EVENT)
MACHINE_NOTE = "note: these entries name this machine's interpreter and vault paths; "
MACHINE_ADVICE = {
    "claude-code": ("keep them out of shared commits (`--scope local` writes the hooks to "
                    ".claude/settings.local.json)"),
    "codex": "keep a project's .codex/hooks.json out of shared commits",
}
# `install generic --format <host>`: where each host reads a stdio server and the shape it
# expects (docs read 2026-09-28; see docs/host-integration.md). Expected but unverified.
FORMATS = {
    "claude-code": ("<project>/.mcp.json", "mcpServers"),
    "cursor": (".cursor/mcp.json (project) or ~/.cursor/mcp.json", "mcpServers"),
    "gemini": (".gemini/settings.json (project) or ~/.gemini/settings.json", "mcpServers"),
    "antigravity": (".agents/mcp_config.json (project) or ~/.gemini/config/mcp_config.json",
                    "mcpServers"),
    "omp": (".omp/mcp.json (project) or ~/.omp/agent/mcp.json", "mcpServers"),
    "opencode": ("opencode.json (project) or ~/.config/opencode/opencode.json", "mcp"),
    "hermes": ("~/.hermes/config.yaml", "mcp_servers (YAML)"),
}


@dataclass
class Change:
    """One file this tool would write (new) or delete (new is None). `own`: the old bytes
    hold nothing but this tool's own entries, so replacing them needs no backup."""

    path: Path
    old: str | None
    new: str | None
    own: bool = False


# ---------------------------------------------------------------------------
# What a host is told to run
# ---------------------------------------------------------------------------

def cli_argv(vault: Path, *tail: str) -> list[str]:
    """How a host launches us: the installed script if there is one, else this interpreter."""
    script = shutil.which(SERVER_NAME)
    base = [script] if script else [sys.executable, "-m", "context_layer.cli"]
    return [*base, *tail, "--vault", str(vault)]


def launch_env() -> dict:
    """Env a host needs to start us.

    PYTHONUTF8=1 always: a host may start the command under a non-UTF-8 locale, and
    UTF-8 mode makes the standard streams UTF-8 whatever the locale says (file I/O
    already names its encoding). Hosts also start the server from their own cwd, where
    `-m context_layer.cli` only imports if the checkout root is on PYTHONPATH; an
    installed console script needs no PYTHONPATH.
    """
    env = {}
    if not shutil.which(SERVER_NAME):
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    env["PYTHONUTF8"] = "1"
    return env


def server_env(session_evidence: bool = False) -> dict:
    env = launch_env()
    if session_evidence:
        env[mcp_server.LEDGER_ENV] = "1"
    return env


def mcp_entry(vault: Path, session_evidence: bool = False) -> dict:
    argv = cli_argv(vault, "mcp")
    entry = {"command": argv[0], "args": argv[1:]}
    env = server_env(session_evidence)
    if env:
        entry["env"] = env
    return entry


def mcp_snippet(vault: Path, session_evidence: bool = False) -> dict:
    return {"mcpServers": {SERVER_NAME: mcp_entry(vault, session_evidence)}}


def hook_argv(vault: Path, method: str = "fts", budget_tokens: "int | None" = None,
              extra_tokens: "int | None" = None, compact: bool = False,
              host: str = "claude-code", max_context_chars: "int | None" = None,
              session_evidence: bool = False, extra: "tuple[str, ...]" = ()) -> list[str]:
    """The hook command line. fts (the default) adds no flag, so an fts hook written by
    an earlier version and one written now are the same command. Synaptic flags are
    written only when given: --extra-tokens sizes the default synaptic packet,
    --budget-tokens only the --compact one. `extra` holds further hook flags, already
    checked (hook_flags())."""
    tail = ["hook", host]
    if method != "fts":
        tail += ["--method", method]
    if compact:
        tail += ["--compact"]
    if extra_tokens is not None:
        tail += ["--extra-tokens", str(extra_tokens)]
    if budget_tokens is not None:
        tail += ["--budget-tokens", str(budget_tokens)]
    if max_context_chars is not None:
        tail += ["--max-context-chars", str(max_context_chars)]
    if session_evidence:
        tail += ["--session-evidence"]
    tail += list(extra)
    return cli_argv(vault, *tail)


def hook_flags(args) -> "tuple[str, ...]":
    """The opt-in hook flags `install` passes through as written (see `hook --help`)."""
    floor = getattr(args, "relevance_floor", None)
    delivery = getattr(args, "delivery", None)
    return ((("--relevance-floor", repr(floor)) if floor else ())
            + (("--delivery", delivery) if delivery else ()))


def env_prefix() -> str:
    if os.name == "nt":
        # Retained for callers that display an env prefix; hook_entry uses the
        # full PowerShell form below so paths never pass through cmd assignment syntax.
        return ""
    return "".join(f"{key}={shlex.quote(value)} " for key, value in launch_env().items())


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def hook_command(argv: list[str]) -> str:
    """Serialize a hook command for the host shell without POSIX syntax on Windows."""
    if os.name != "nt":
        return env_prefix() + shlex.join(argv)
    script = "; ".join([*(f"$env:{key}={_powershell_quote(value)}"
                            for key, value in launch_env().items()),
                         "& " + " ".join(_powershell_quote(arg) for arg in argv),
                         "exit $LASTEXITCODE"])
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand " + encoded


def hook_entry(vault: Path, method: str = "fts", budget_tokens: "int | None" = None,
               extra_tokens: "int | None" = None, compact: bool = False,
               host: str = "claude-code", max_context_chars: "int | None" = None,
               session_evidence: bool = False, extra: "tuple[str, ...]" = ()) -> dict:
    argv = hook_argv(vault, method, budget_tokens, extra_tokens, compact, host,
                     max_context_chars, session_evidence, extra)
    entry = {"type": "command", "command": hook_command(argv),
             "timeout": HOOK_TIMEOUT_S}
    if host == "codex":
        # learn.chatgpt.com/docs/hooks: 0 passes the whole additionalContext to the model;
        # the docs advise it only for a hook that enforces its own strict output cap, which
        # this one does (--max-context-chars). Without it Codex may save text to disk.
        entry["additionalContextLimit"] = 0
    return entry


def is_ours(command: str) -> bool:
    """Recognise a prompt hook this tool installed, whatever vault or launcher it names."""
    parsed = parse_powershell_hook(command)
    if parsed is not None:
        _env, argv = parsed
        tail = _context_layer_tail(argv)
        return tail is not None and tail[:2] in (["hook", "claude-code"],
                                                 ["hook", "codex"])
    text = command
    return ("hook claude-code" in text or "hook codex" in text) and (
        "context-layer" in text or "context_layer" in text)


def is_ours_rules(command: str) -> bool:
    """Recognise a `rules hook <event>` entry this tool installed."""
    parsed = parse_powershell_hook(command)
    if parsed is not None:
        _env, argv = parsed
        tail = _context_layer_tail(argv)
        return tail is not None and tail[:2] == ["rules", "hook"]
    text = command
    return "rules hook " in text and ("context-layer" in text
                                      or "context_layer" in text)


def _context_layer_tail(argv: list[str]) -> list[str] | None:
    """Return hook arguments only for this package's console or Python launcher."""
    if not argv:
        return None
    executable = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if executable in ("context-layer", "context-layer.exe"):
        return argv[1:]
    if (executable.startswith("python") and executable.endswith(".exe")
            and argv[1:3] == ["-m", "context_layer.cli"]):
        return argv[3:]
    return None


def parse_powershell_hook(command: str):
    """Parse only hook_command's literal-only EncodedCommand form; never execute it."""
    try:
        tokens = shlex.split(command, posix=False)
        if (len(tokens) != 6 or Path(tokens[0].strip('"')).name.lower() != "powershell.exe"
                or tokens[1:4] != ["-NoLogo", "-NoProfile", "-NonInteractive"]
                or tokens[4] != "-EncodedCommand"):
            return None
        script = base64.b64decode(tokens[5], validate=True).decode("utf-16le")
    except (UnicodeDecodeError, ValueError, OSError):
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
        value = []
        while index < len(script):
            char = script[index]
            index += 1
            if char == "'":
                if index < len(script) and script[index] == "'":
                    value.append("'")
                    index += 1
                    continue
                return "".join(value)
            value.append(char)
        raise ValueError("unterminated PowerShell literal")

    try:
        while script.startswith("$env:", index):
            end = script.find("=", index + 5)
            if end < 0:
                return None
            name = script[index + 5:end]
            if not name or any(not (char.isalnum() or char == "_") for char in name):
                return None
            index = end + 1
            env[name] = literal()
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
        if not argv:
            return None
        return env, argv
    except ValueError:
        return None


def mine(command: str) -> bool:
    return is_ours(command) or is_ours_rules(command)


def rules_events(include_attribution: bool = True) -> dict:
    """Event -> `rules hook` argument. SessionStart and Stop always; PostToolUse (agent
    write attribution) once this build's rules module lists "post-tool-use" in HOOK_EVENTS."""
    events = dict(RULES_EVENTS)
    if include_attribution:
        from . import rules
        if "post-tool-use" in (getattr(rules, "HOOK_EVENTS", None) or ()):
            events[ATTRIBUTION_EVENT] = "post-tool-use"
    return events


def rules_hook_entry(vault: Path, event: str) -> dict:
    argument = rules_events()[event]
    return {"type": "command",
            "command": hook_command(cli_argv(vault, "rules", "hook", argument))}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def read_file(path: Path) -> str | None:
    # newline="" keeps the bytes as they are, so a diff shows every real change.
    if not path.is_file():
        return None
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def write_file(path: Path, text: str) -> None:
    """Replace `path` atomically: the new text goes to a temporary file in the same folder and
    is renamed over the old one, so a crash never leaves half a settings file and a reader
    never sees one. A symlinked `path` keeps its link and its target is the file replaced
    (`emit` says so first); the mode of an existing file is kept."""
    real = Path(os.path.realpath(path))
    try:
        mode = stat.S_IMODE(real.stat().st_mode)
    except OSError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    descriptor, temporary = tempfile.mkstemp(dir=real.parent, prefix=f".{real.name}.",
                                             suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, real)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def read_json(path: Path) -> tuple[str | None, dict]:
    old = read_file(path)
    if old is None or not old.strip():
        return old, {}
    data = json.loads(old)              # malformed config: an error, never an overwrite
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return old, data


def render_json(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def unused(path: Path) -> Path:
    """Never overwrite a backup: a second run in the same second gets -2, -3, ..."""
    candidate = path
    counter = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}-{counter}")
        counter += 1
    return candidate


def diff_text(path: Path, old: str | None, new: str | None) -> str:
    return "".join(difflib.unified_diff(
        (old or "").splitlines(keepends=True), (new or "").splitlines(keepends=True),
        fromfile=str(path) if old is not None else "/dev/null",
        tofile=str(path) if new is not None else "/dev/null"))


def emit(changes: list[Change], apply_now: bool, project: "Path | None" = None) -> int:
    """Print a diff, or write with a backup. Returns the number of files changed.

    A `.claude/` or `.codex/` folder directly under the project that this run emptied by
    removing a file is removed as well (only if empty)."""
    written = 0
    pending = False
    emptied: set[Path] = set()
    for change in changes:
        path, old, new = change.path, change.old, change.new
        if old == new:
            if old is not None:
                print(f"unchanged: {path}", file=sys.stderr)
            continue
        if not apply_now:
            sys.stdout.write(diff_text(path, old, new))
            pending = True
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if old is not None and not change.own:
            backup = unused(path.parent / f"{path.name}.bak-{stamp()}")
            backup.write_bytes(path.read_bytes())
            print(f"backup: {backup}")
        if new is None:
            path.unlink()
            print(f"removed: {path}")
            emptied.add(path.parent)
        else:
            if path.is_symlink():
                print(f"note: {path} is a symlink; its target {os.path.realpath(path)} is "
                      "updated and the link is kept", file=sys.stderr)
            write_file(path, new)
            print(f"wrote: {path}")
        written += 1
    for folder in sorted(emptied):
        if project is not None and folder.parent == project and folder.name in (".claude",
                                                                                ".codex"):
            try:
                folder.rmdir()                      # only succeeds when empty
                print(f"removed empty folder: {folder}")
            except OSError:
                pass
    if pending:
        print("dry run: nothing was written. Re-run with --apply.", file=sys.stderr)
    return written


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

def claude_mcp_change(project: Path, vault: Path, removing: bool,
                      session_evidence: bool = False) -> Change:
    """The project-scoped MCP entry in <project>/.mcp.json; every other key is kept."""
    path = project / ".mcp.json"
    old, data = read_json(path)
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"{path}: mcpServers must be an object")
    others = {name: entry for name, entry in servers.items() if name != SERVER_NAME}
    servers = dict(servers)
    if removing:
        servers.pop(SERVER_NAME, None)
    else:
        servers[SERVER_NAME] = mcp_entry(vault, session_evidence)
    if servers:
        data["mcpServers"] = servers
    else:
        data.pop("mcpServers", None)
    rest = {key: value for key, value in data.items() if key != "mcpServers"}
    own = old is not None and not others and not rest
    return Change(path, old, render_json(data) if data else None, own)


def _merge_hook(data: dict, path: Path, event: str, mine_test, entry: "dict | None",
                matcher: "str | None" = None) -> None:
    """Drop our entries for `event` (recognised by `mine_test`), then add `entry` if given."""
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path}: hooks must be an object")
    hooks = dict(hooks)
    groups = hooks.get(event) or []
    if not isinstance(groups, list):
        raise ValueError(f"{path}: hooks.{event} must be a list")
    kept = []
    for group in groups:
        if not isinstance(group, dict):
            kept.append(group)
            continue
        original = group.get("hooks") or []
        ours = [item for item in original
                if isinstance(item, dict) and mine_test(str(item.get("command", "")))]
        if not ours:
            kept.append(group)                     # never touched, preserved exactly
        elif len(ours) < len(original):
            kept.append({**group, "hooks": [item for item in original if item not in ours]})
    if entry is not None:
        kept.append({"matcher": matcher, "hooks": [entry]} if matcher else {"hooks": [entry]})
    if kept:
        hooks[event] = kept
    else:
        hooks.pop(event, None)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)


def strip_ours(data: dict, path: Path) -> dict:
    """A copy of a settings/hooks object with every entry this tool may have written removed."""
    copy = json.loads(json.dumps(data))
    for event in OUR_EVENTS:
        _merge_hook(copy, path, event, mine, None)
    return copy


def plan_marker(project: Path) -> Path:
    """Records that `install --plan-default` set defaultMode, so uninstall removes only that."""
    return project / ".claude" / PLAN_MARKER


def plan_marker_text(settings_name: str) -> str:
    return render_json({"set_by": "context-layer install --plan-default",
                        "key": "permissions.defaultMode", "value": "plan",
                        "file": settings_name})


def marker_file(text: "str | None") -> str:
    """The settings file a plan-default marker names (markers before 0.4 name none)."""
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        data = {}
    name = data.get("file") if isinstance(data, dict) else None
    return name if name in ("settings.json", "settings.local.json") else "settings.json"


def claude_settings_changes(project: Path, vault: Path, removing: bool, prompt_hook: bool,
                            rules_hooks: bool, plan_default: bool, method: str = "fts",
                            budget_tokens: "int | None" = None,
                            extra_tokens: "int | None" = None,
                            compact: bool = False, scope: str = "project",
                            max_context_chars: "int | None" = None,
                            session_evidence: bool = False,
                            hook_extra: "tuple[str, ...]" = ()) -> list[Change]:
    """Every settings key this tool owns, merged in one pass per file (one diff, one
    backup), plus the plan-default marker when this tool sets or removes defaultMode.

    The hooks go to .claude/settings.local.json with `--scope local`, else to
    .claude/settings.json; installing moves our hook entries out of the other file, and
    uninstall clears both."""
    folder = project / ".claude"
    target = folder / ("settings.local.json" if scope == "local" else "settings.json")
    marker = plan_marker(project)
    marker_old = marker.read_text(encoding="utf-8") if marker.is_file() else None
    marker_new = marker_old
    plan_file = folder / marker_file(marker_old)
    changes = []
    for path in (folder / "settings.json", folder / "settings.local.json"):
        old, data = read_json(path)
        owns_plan = marker_old is not None and path == plan_file
        stripped = strip_ours(data, path)
        if owns_plan:
            _without_plan(stripped, path)
        if removing:
            data = strip_ours(data, path)
        else:
            if prompt_hook:
                _merge_hook(data, path, HOOK_EVENT, is_ours,
                            hook_entry(vault, method, budget_tokens, extra_tokens, compact,
                                       "claude-code", max_context_chars, session_evidence,
                                       hook_extra)
                            if path == target else None)
            if rules_hooks:
                for event in (*RULES_EVENTS, ATTRIBUTION_EVENT):
                    wanted = path == target and event in rules_events()
                    _merge_hook(data, path, event, is_ours_rules,
                                rules_hook_entry(vault, event) if wanted else None,
                                ATTRIBUTION_MATCHER if event == ATTRIBUTION_EVENT else None)
        if plan_default and not removing and path == target:
            if marker_old is not None and plan_file != target:
                raise ValueError(f"{marker}: an earlier --plan-default set the mode in "
                                 f"{plan_file.name}; uninstall it first")
            permissions = data.get("permissions", {})
            if not isinstance(permissions, dict):
                raise ValueError(f"{path}: permissions must be an object")
            current = permissions.get("defaultMode")
            if current is not None and current != "plan":
                # Never replace the user's own mode: it would survive only in the backup.
                raise ValueError(f"{path}: permissions.defaultMode is already "
                                 f"{json.dumps(current)}; --plan-default does not replace it "
                                 "(change it by hand, or drop --plan-default)")
            if current is None:
                data["permissions"] = {**permissions, "defaultMode": "plan"}
                marker_new = plan_marker_text(path.name)
            # current == "plan" without our marker was the user's; it stays theirs.
        elif removing and owns_plan:
            # Only a plan default this tool recorded setting is removed; any other is the user's.
            _without_plan(data, path)
        own = old is not None and not stripped
        changes.append(Change(path, old, render_json(data) if data else None, own))
    if removing:
        marker_new = None
    if marker_new != marker_old:
        changes.append(Change(marker, marker_old, marker_new, own=True))
    return changes


def _without_plan(data: dict, path: Path) -> dict:
    """Remove a "plan" defaultMode (and an emptied permissions object) in place."""
    permissions = data.get("permissions")
    if isinstance(permissions, dict) and permissions.get("defaultMode") == "plan":
        permissions = {k: v for k, v in permissions.items() if k != "defaultMode"}
        if permissions:
            data["permissions"] = permissions
        else:
            data.pop("permissions")
    return data


def claude_cli_scope(vault: Path, removing: bool, apply_now: bool, scope: str, project: Path,
                     session_evidence: bool = False) -> int:
    """`claude mcp add/remove --scope user|local`: run it only on --apply with claude on PATH.
    Local scope is stored per project directory, so it runs from the project directory."""
    argv = cli_argv(vault, "mcp")
    env_flags = []
    for key, value in server_env(session_evidence).items():
        env_flags += ["--env", f"{key}={value}"]
    # code.claude.com/docs/en/mcp: --env goes after the server name and before `--`.
    command = (["claude", "mcp", "remove", "--scope", scope, SERVER_NAME] if removing
               else ["claude", "mcp", "add", "--scope", scope, SERVER_NAME, *env_flags,
                     "--", *argv])
    claude = shutil.which("claude")
    where = str(project) if scope == "local" else None
    if apply_now and claude:
        print("+ " + shlex.join(command), file=sys.stderr)
        return subprocess.call([claude, *command[1:]], cwd=where)
    print(shlex.join(command))
    reason = "claude is not on PATH" if not claude else "dry run"
    place = f" from {project}" if scope == "local" else ""
    print(f"{scope} scope: not run ({reason}); run the command above yourself{place}.",
          file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# Codex (expected but unverified: no Codex CLI on the reference machine)
# ---------------------------------------------------------------------------

def codex_home() -> Path:
    """$CODEX_HOME, else ~/.codex. learn.chatgpt.com/docs/config-file/environment-variables:
    CODEX_HOME "sets the root for Codex state, including config ...; if you set it, the
    directory must already exist"."""
    override = os.environ.get("CODEX_HOME")
    if override:
        home = Path(override).expanduser()
        if not home.is_dir():
            raise ValueError(f"CODEX_HOME={override} is not a directory; Codex requires it to "
                             "exist, so nothing was written")
        return home
    return Path.home() / ".codex"


def toml_string(value: str) -> str:
    """A TOML basic string (JSON string escapes are a subset of TOML's)."""
    return json.dumps(value, ensure_ascii=False)


def codex_block(vault: Path, session_evidence: bool = False) -> str:
    argv = cli_argv(vault, "mcp")
    args = ", ".join(toml_string(token) for token in argv[1:])
    lines = [START,
             "# Written by context-layer. Only the lines between these markers are touched.",
             f"[{CODEX_TABLE}]",
             f"command = {toml_string(argv[0])}",
             f"args = [{args}]",
             f"tool_timeout_sec = {CODEX_TOOL_TIMEOUT_S}"]
    env = server_env(session_evidence)
    if env:
        lines.append("env = { " + ", ".join(f"{key} = {toml_string(value)}"
                                            for key, value in env.items()) + " }")
    return "\n".join([*lines, END]) + "\n"


HEADER = re.compile(r"^\[\[?\s*(.+?)\s*\]\]?$")
KEY_LINE = re.compile(r"^([A-Za-z0-9_\-\"'. ]+?)\s*=")


def _dotted(key: str) -> str:
    return ".".join(part.strip().strip("\"'") for part in key.split("."))


def _declares_our_table(text: str) -> bool:
    """Header scan (Python 3.10, or a file tomllib cannot read): does the text declare
    mcp_servers.context_layer as a table, a dotted key or a key of [mcp_servers]?"""
    table = ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        header = HEADER.match(line)
        if header:
            table = _dotted(header.group(1))
            if table == CODEX_TABLE or table.startswith(CODEX_TABLE + "."):
                return True
            continue
        key = KEY_LINE.match(line)
        if key:
            full = (table + "." if table else "") + _dotted(key.group(1))
            if full == CODEX_TABLE or full.startswith(CODEX_TABLE + "."):
                return True
    return False


def toml_problem(text: str) -> "str | None":
    """Why `text` is not valid TOML, or None. Needs tomllib (3.11+); on 3.10 it cannot tell,
    and only the table-header scan guards the write."""
    if tomllib is None:
        return None
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return str(exc)
    return None


def codex_change(vault: Path, removing: bool, session_evidence: bool = False) -> Change:
    """Our marker block in $CODEX_HOME/config.toml; the rest of the file is copied through.

    Before writing, the result is checked: the file without our block must not already
    declare [mcp_servers.context_layer] (`codex mcp add` writes that table), and file and
    candidate must parse as TOML (3.11+). Either problem refuses, naming it; nothing is
    written."""
    path = codex_home() / "config.toml"
    old = read_file(path)
    body = old or ""
    start = body.find(START)
    if start != -1:
        end = body.find(END, start)
        if end == -1:
            raise ValueError(f"{path}: {START} has no matching {END}; fix the file by hand")
        line_end = body.find("\n", end)
        body = body[:start] + (body[line_end + 1:] if line_end != -1 else "")
    own = old is not None and not body.strip()
    if not removing:
        problem = toml_problem(body)
        if problem:
            raise ValueError(f"{path} is not valid TOML ({problem}); fix it first. "
                             "Nothing was written")
        if _declares_our_table(body) or _parsed_has_table(body):
            raise ValueError(f"{path} already declares [{CODEX_TABLE}] outside the "
                             "context-layer markers (for example from `codex mcp add`); remove "
                             "that table, then re-run. Nothing was written")
        if body and not body.endswith("\n"):
            body += "\n"
        body += codex_block(vault, session_evidence)
        problem = toml_problem(body)
        if problem:
            raise ValueError(f"{path} would not be valid TOML with the context-layer block "
                             f"({problem}). Nothing was written")
    return Change(path, old, body or None, own)


def _parsed_has_table(text: str) -> bool:
    if tomllib is None:
        return False
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    servers = data.get("mcp_servers")
    return isinstance(servers, dict) and "context_layer" in servers


def codex_hooks_change(project: Path, vault: Path, removing: bool, prompt_hook: bool,
                       rules_hooks: bool, method: str = "fts",
                       budget_tokens: "int | None" = None, extra_tokens: "int | None" = None,
                       compact: bool = False, max_context_chars: "int | None" = None,
                       session_evidence: bool = False,
                       hook_extra: "tuple[str, ...]" = ()) -> Change:
    """Our hooks in <project>/.codex/hooks.json (learn.chatgpt.com/docs/hooks: the same
    event -> matcher group -> handler shape as Claude Code). Codex loads project hooks only
    in a trusted project and runs a new or changed hook only after it is reviewed in /hooks."""
    path = project / ".codex" / "hooks.json"
    old, data = read_json(path)
    own = old is not None and not strip_ours(data, path)
    if removing:
        data = strip_ours(data, path)
    else:
        if prompt_hook:
            _merge_hook(data, path, HOOK_EVENT, is_ours,
                        hook_entry(vault, method, budget_tokens, extra_tokens, compact, "codex",
                                   max_context_chars, session_evidence, hook_extra))
        if rules_hooks:
            for event in RULES_EVENTS:
                _merge_hook(data, path, event, is_ours_rules, rules_hook_entry(vault, event))
    return Change(path, old, render_json(data) if data else None, own)


# ---------------------------------------------------------------------------
# Snippets for hosts this tool does not write
# ---------------------------------------------------------------------------

def format_snippet(fmt: str, vault: Path, session_evidence: bool = False) -> str:
    entry = mcp_entry(vault, session_evidence)
    if fmt == "opencode":
        server = {"type": "local", "command": [entry["command"], *entry["args"]],
                  "enabled": True}
        if "env" in entry:
            server["environment"] = entry["env"]
        return render_json({"$schema": "https://opencode.ai/config.json",
                            "mcp": {SERVER_NAME: server}})
    if fmt == "hermes":
        lines = ["mcp_servers:", f"  {SERVER_NAME}:",
                 f"    command: {json.dumps(entry['command'], ensure_ascii=False)}",
                 "    args: [" + ", ".join(json.dumps(a, ensure_ascii=False)
                                           for a in entry["args"]) + "]"]
        if "env" in entry:
            lines.append("    env:")
            lines += [f"      {key}: {json.dumps(value, ensure_ascii=False)}"
                      for key, value in entry["env"].items()]
        return "\n".join(lines) + "\n"
    if fmt == "cursor":
        entry = {"type": "stdio", **entry}
    return render_json({"mcpServers": {SERVER_NAME: entry}})


def print_config(host: str, vault: Path, project: Path, want_hook: bool, method: str = "fts",
                 budget_tokens: "int | None" = None, extra_tokens: "int | None" = None,
                 compact: bool = False, fmt: "str | None" = None,
                 max_context_chars: "int | None" = None, session_evidence: bool = False,
                 rules_hooks: bool = False, hook_extra: "tuple[str, ...]" = ()) -> int:
    """Show what would be written; touch nothing, whatever the other flags say."""
    if host == "codex":
        print(f"codex: {codex_home() / 'config.toml'}, between the markers below",
              file=sys.stderr)
        print(UNVERIFIED, file=sys.stderr)
        sys.stdout.write(codex_block(vault, session_evidence))
        if want_hook or rules_hooks:
            print(f"codex: {project / '.codex' / 'hooks.json'}", file=sys.stderr)
            change = codex_hooks_change(project, vault, False, want_hook, rules_hooks, method,
                                        budget_tokens, extra_tokens, compact,
                                        max_context_chars, session_evidence, hook_extra)
            print(change.new or "", end="")
        return 0
    if host == "generic" and fmt:
        where, key = FORMATS[fmt]
        print(f"{fmt}: add this under `{key}` in {where}; expected but unverified: the "
              "shape follows the host's docs, no such host ran on the reference machine",
              file=sys.stderr)
        sys.stdout.write(format_snippet(fmt, vault, session_evidence))
        return 0
    if host == "claude-code":
        print(f"claude-code: {project / '.mcp.json'}", file=sys.stderr)
    else:
        print("generic: any MCP stdio client can launch this command; it speaks JSON-RPC 2.0 "
              "over stdin/stdout. `--format <host>` prints a host's own shape.", file=sys.stderr)
    print(render_json(mcp_snippet(vault, session_evidence)), end="")
    if host == "claude-code" and want_hook:
        print(f"claude-code: {project / '.claude' / 'settings.json'}", file=sys.stderr)
        print(render_json({"hooks": {HOOK_EVENT: [{"hooks": [
            hook_entry(vault, method, budget_tokens, extra_tokens, compact, "claude-code",
                       max_context_chars, session_evidence, hook_extra)]}]}}), end="")
    return 0


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def usage(label: str, message: str) -> int:
    print(f"context-layer {label}: {message}", file=sys.stderr)
    return 2


def ledger_folder(vault: Path, apply_now: bool) -> None:
    """--session-evidence: the hook and the server write only into an existing folder."""
    folder = vault / ".context" / mcp_server.LEDGER_DIR
    if folder.is_dir():
        return
    if not apply_now:
        print(f"would create: {folder}/ (session evidence: paths and hashes, never text)",
              file=sys.stderr)
        return
    folder.mkdir(parents=True, exist_ok=True)
    print(f"created: {folder}/")


def run(args: argparse.Namespace, removing: bool) -> int:
    label = "uninstall" if removing else "install"
    extra = [token for token in getattr(args, "rest", []) if token]
    if extra:
        return usage(label, f"unrecognised arguments: {' '.join(extra)}")
    host = args.host
    printing = host == "print"
    if printing:
        host = getattr(args, "print_host", None)
        if host is None:
            return usage(label + " print", f"name a host: {' | '.join(HOSTS)}")
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        print(f"context-layer {label}: vault not found: {vault}", file=sys.stderr)
        return 1
    project = Path(args.project).expanduser().resolve() if args.project else vault
    method = getattr(args, "method", "fts")
    budget_tokens = getattr(args, "budget_tokens", None)
    extra_tokens = getattr(args, "extra_tokens", None)
    compact = getattr(args, "compact", False)
    max_context_chars = getattr(args, "max_context_chars", None)
    session_evidence = getattr(args, "session_evidence", False)
    fmt = getattr(args, "format", None)
    hook_extra = hook_flags(args)
    if not removing:
        hooked = host in ("claude-code", "codex") and args.hook
        if (method != "fts" or budget_tokens is not None or extra_tokens is not None
                or compact or max_context_chars is not None or hook_extra) and not hooked:
            return usage(label, "--method, --extra-tokens, --compact, --budget-tokens, "
                                "--max-context-chars, --relevance-floor and --delivery "
                                "configure the prompt hook; add --hook (claude-code or codex)")
        floor = getattr(args, "relevance_floor", None)
        if floor is not None and not 0 <= floor < 1:
            return usage(label, "--relevance-floor must be at least 0 and below 1")
        if getattr(args, "delivery", None) and compact:
            return usage(label, "--delivery applies to fts and the default synaptic packet, "
                                "not --compact")
        if floor and compact:
            return usage(label, "--relevance-floor applies to fts and the default synaptic "
                                "packet, not --compact")
        if (compact or extra_tokens is not None or budget_tokens is not None) \
                and method != "synaptic":
            return usage(label, "--extra-tokens, --compact and --budget-tokens need "
                                "--method synaptic")
        if extra_tokens is not None and (extra_tokens < 0 or compact):
            return usage(label, "--extra-tokens sizes the default synaptic packet; it needs a "
                                "value >= 0 and no --compact")
        if budget_tokens is not None and (budget_tokens <= 0 or not compact):
            return usage(label, "--budget-tokens sizes only the --compact synaptic packet and "
                                "needs a positive value; the default synaptic packet is sized "
                                "by --extra-tokens")
        if max_context_chars is not None and not (
                mcp_server.MAX_CONTEXT_MIN <= max_context_chars <= mcp_server.HOST_CONTEXT_LIMIT):
            return usage(label, f"--max-context-chars must be between "
                                f"{mcp_server.MAX_CONTEXT_MIN} and "
                                f"{mcp_server.HOST_CONTEXT_LIMIT}: Claude Code keeps at most "
                                f"{mcp_server.HOST_CONTEXT_LIMIT} characters of one hook "
                                "context string")
        if fmt is not None and host != "generic":
            return usage(label, "--format names a host whose snippet `install generic` "
                                "prints; it goes with generic")
        if host == "codex" and args.plan_default:
            return usage(label, "--plan-default is a Claude Code setting")
    try:
        if printing:
            return print_config(host, vault, project, args.hook, method, budget_tokens,
                                extra_tokens, compact, fmt, max_context_chars,
                                session_evidence, getattr(args, "rules", False), hook_extra)
        if host == "generic":
            # Nothing to write: a generic client is configured by hand from this snippet.
            return print_config("generic", vault, project, args.hook, fmt=fmt,
                                session_evidence=session_evidence)
        status = 0
        changes: list[Change] = []
        if host == "claude-code":
            if args.scope in ("user", "local"):
                status = claude_cli_scope(vault, removing, args.apply, args.scope, project,
                                          session_evidence)
            else:
                changes.append(claude_mcp_change(project, vault, removing, session_evidence))
            if args.hook or args.rules or args.plan_default or removing:
                # uninstall always clears every hook it may have written
                changes.extend(claude_settings_changes(
                    project, vault, removing, args.hook, args.rules, args.plan_default,
                    method, budget_tokens, extra_tokens, compact, args.scope,
                    max_context_chars, session_evidence, hook_extra))
        else:
            changes.append(codex_change(vault, removing, session_evidence))
            if args.hook or args.rules or removing:
                changes.append(codex_hooks_change(project, vault, removing, args.hook,
                                                  args.rules, method, budget_tokens,
                                                  extra_tokens, compact, max_context_chars,
                                                  session_evidence, hook_extra))
        if not removing:
            print(MACHINE_NOTE + MACHINE_ADVICE[host], file=sys.stderr)
        written = emit(changes, args.apply, project)
        if session_evidence and not removing:
            ledger_folder(vault, args.apply)
        if removing and (vault / ".context" / mcp_server.LEDGER_DIR).is_dir():
            print("kept: .context/session-evidence/ (session ledgers are vault data; delete "
                  "the folder yourself if you no longer want them)", file=sys.stderr)
        if host == "codex":
            state = ("not written" if not (args.apply and written)
                     else "block removed" if removing else "written")
            print(f"codex: config {state}; {UNVERIFIED.split(': ', 1)[1]}", file=sys.stderr)
            if args.hook or args.rules:
                print("codex: project hooks load only in a trusted project, and a new or "
                      "changed hook runs only after you review it in /hooks", file=sys.stderr)
        return status
    except (OSError, ValueError) as exc:  # includes json.JSONDecodeError
        print(f"context-layer {label}: {mcp_server.one_line(exc)}", file=sys.stderr)
        return 1


def cmd_install(args: argparse.Namespace) -> int:
    return run(args, removing=False)


def cmd_uninstall(args: argparse.Namespace) -> int:
    return run(args, removing=True)


def register(sub: argparse._SubParsersAction) -> None:
    """Attach `install` and `uninstall` to the CLI."""
    p_install = sub.add_parser(
        "install",
        help="Connect a host to this vault's MCP server (dry run unless --apply).",
        description="Writes the host config that launches `context-layer mcp` over stdio. "
                    "claude-code: <project>/.mcp.json, plus optional hooks in "
                    "<project>/.claude/settings.json (settings.local.json with --scope local). "
                    "codex: a marker block in $CODEX_HOME/config.toml (default ~/.codex), plus "
                    "optional hooks in <project>/.codex/hooks.json (expected but unverified). "
                    "generic: prints a snippet for any MCP stdio client (--format <host> for a "
                    "host's own shape). `install print <host>` shows the config only.",
    )
    p_install.add_argument("host", choices=[*HOSTS, "print"])
    p_install.add_argument("print_host", nargs="?", choices=list(HOSTS),
                           help="With `print`: the host whose config to show.")
    p_uninstall = sub.add_parser(
        "uninstall",
        help="Remove this tool's host config entries (dry run unless --apply).",
        description="Removes only the context-layer MCP entry, hooks and marker block, "
                    "backs up any file that also holds someone else's content, leaves a file "
                    "nothing else changed at its pre-install bytes, and removes a file or "
                    ".claude/ or .codex/ folder that held only this tool's entries.",
    )
    p_uninstall.add_argument("host", choices=list(HOSTS))
    p_install.add_argument("--method", choices=list(HOOK_METHODS), default="fts",
                           help="With --hook: retrieval method the prompt hook runs (default "
                                "fts; synaptic is opt-in and experimental).")
    p_install.add_argument("--extra-tokens", type=int, default=None, metavar="N",
                           help="With --hook --method synaptic: estimated-token budget for "
                                "link-graph extras added after the unchanged fts packet "
                                "(default: the hook's own, 600).")
    p_install.add_argument("--compact", action="store_true",
                           help="With --hook --method synaptic: the compact packer (passages "
                                "within --budget-tokens) instead of fts packet + extras.")
    p_install.add_argument("--budget-tokens", type=int, default=None, metavar="N",
                           help="With --hook --method synaptic --compact only: packet budget "
                                "in estimated tokens (default: the hook's own, 1200).")
    p_install.add_argument("--max-context-chars", type=int, default=None, metavar="N",
                           help="With --hook: most characters of context the hook prints "
                                f"(default: the hook's own, {mcp_server.MAX_CONTEXT_DEFAULT}; "
                                f"{mcp_server.MAX_CONTEXT_MIN}-{mcp_server.HOST_CONTEXT_LIMIT}).")
    p_install.add_argument("--relevance-floor", type=float, default=None, metavar="R",
                           help="With --hook: pass --relevance-floor R to the hook (drop a "
                                "top-k note weaker than R x the strongest; 0 <= R < 1; off "
                                "by default; may drop evidence).")
    p_install.add_argument("--delivery", choices=["focus", "window", "prefix"], default=None,
                           help="With --hook: pass --delivery to the hook (default: the "
                                "hook's own, focus; window gives the items `search` gives).")
    p_install.add_argument("--session-evidence", action="store_true",
                           help="Record delivered paths and hashes (never text) per host "
                                "session in <vault>/.context/session-evidence/: adds the hook "
                                f"flag and {mcp_server.LEDGER_ENV}=1 to the server, and creates "
                                "the folder on --apply.")
    p_install.add_argument("--format", choices=list(FORMATS), default=None,
                           help="With generic: print the snippet in this host's own shape "
                                "(expected but unverified).")
    for parser, func in ((p_install, cmd_install), (p_uninstall, cmd_uninstall)):
        parser.add_argument("--vault", required=True, help="Vault the server will serve.")
        parser.add_argument("--project", default=None,
                            help="Project directory holding .mcp.json, .claude/ and .codex/ "
                                 "(default: the vault).")
        parser.add_argument("--scope", choices=list(SCOPES), default="project",
                            help="claude-code: project (default: .mcp.json and "
                                 ".claude/settings.json), local (`claude mcp add --scope "
                                 "local` and .claude/settings.local.json) or user (`claude mcp "
                                 "add --scope user`; hooks stay per project).")
        parser.add_argument("--hook", action="store_true",
                            help="Also install the UserPromptSubmit prompt hook (claude-code, "
                                 "codex).")
        parser.add_argument("--rules", action="store_true",
                            help="Also install the rule-record hooks: SessionStart and Stop "
                                 "(claude-code, codex), and PostToolUse attribution when the "
                                 "rules module supports it (claude-code). See "
                                 "`context-layer rules`.")
        parser.add_argument("--plan-default", action="store_true",
                            help="Also set permissions.defaultMode to \"plan\" in the "
                                 "project's Claude Code settings (claude-code).")
        parser.add_argument("--apply", action="store_true",
                            help="Write the change, after backing the file up.")
        parser.set_defaults(func=func, forward_to=None)
