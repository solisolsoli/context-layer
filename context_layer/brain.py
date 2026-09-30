"""context_layer.brain - create a starter Obsidian/Markdown vault ("brain") in one command.

`context-layer brain init <path>` lays out folders, one hub note per folder, a
Dashboard, note templates, a few fictional example notes that show linking and
an archived older version, the vault rule files (CLAUDE.md = AGENTS.md, LOG.md,
BACKLOG.md) and a starting `.context/routes.json`. Dry run by default; `--apply`
writes.

The shipped notes link only to notes that stay searchable, so a fresh brain's
link graph has no unresolved link: folders kept out of search (records,
archive, templates) and the rule files, which the hosts load themselves, are
named in code spans rather than linked.

Layouts
-------
- `standard` (default): English folder names adapted from the Avenox Beyin
  layout (see CREDITS.md), no emoji.
- `minimal`: Inbox, Projects, Knowledge, Archive, Daily, Templates.
- `--avenox-compat` (standard only): the upstream emoji-prefixed folder names,
  for a vault that will also run the Avenox Beyin engine.

Safety: a non-empty target is refused unless `--into-existing`, and then no
existing file is ever overwritten (files are created with exclusive open).
Example notes are created in a new vault unless `--no-examples`; with
`--into-existing` they are created only when `--examples` asks for them, so
deleted examples do not come back.
No Obsidian community plugin is enabled; the command only prints how to
install the Brain View plugin.

Python 3.10+; standard library only; no network access.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from . import rules

ROLES = ("command", "inbox", "goals", "projects", "records", "knowledge", "arsenal", "body",
         "mind", "companion", "archive", "templates", "daily", "machine")

STANDARD = {
    "inbox": "000-Inbox", "command": "100-Command-Center", "goals": "200-Goals",
    "projects": "300-Projects", "records": "400-Records", "knowledge": "500-Knowledge",
    "arsenal": "600-Arsenal", "body": "700-Body", "mind": "800-Mind",
    "companion": "850-Companion", "archive": "900-Archive", "templates": "Templates",
    "daily": "daily", "machine": "knowledge",
}
# Upstream names, byte for byte (escaped so this source file stays ASCII).
AVENOX_COMPAT = {
    "inbox": "\U0001f4e5 000-Inbox", "command": "\U0001f3af 100-Command-Center",
    "goals": "\u2694\ufe0f 200-Goals", "projects": "\U0001f3f0 300-Projects",
    "records": "\U0001f510 400-Vault", "knowledge": "\U0001f9e0 500-Knowledge",
    "arsenal": "\U0001f6e0\ufe0f 600-Arsenal", "body": "\U0001f4aa 700-Body",
    "mind": "\U0001f9d8 800-Mind", "companion": "\U0001f52e 850-Companion",
    "archive": "\U0001f4e6 900-Archive", "templates": "\U0001f4cb Templates",
    "daily": "daily", "machine": "knowledge",
}
MINIMAL = {
    "command": "", "inbox": "Inbox", "projects": "Projects", "knowledge": "Knowledge",
    "archive": "Archive", "daily": "Daily", "templates": "Templates",
}
LAYOUTS = {"standard": STANDARD, "minimal": MINIMAL}
EXTRA_DIRS = {"standard": [("inbox", "Dump")], "minimal": []}

HUBS = {   # role -> (title, one line) for the Dashboard, in display order
    "inbox": ("Inbox", "quick capture; sort it later"),
    "goals": ("Goals", "longer-term outcomes"),
    "projects": ("Projects", "work with a goal and an end, plus its decisions and meetings"),
    "records": ("Records", "private reference records; excluded from search"),
    "knowledge": ("Knowledge", "notes you wrote about how things work"),
    "arsenal": ("Arsenal", "tools and resources"),
    "body": ("Body", "health and training notes"),
    "mind": ("Mind", "reflection and habits"),
    "companion": ("Companion", "the agent's memory about you and open threads"),
    "daily": ("Daily", "one note per day"),
    "machine": ("Machine knowledge", "notes written by tools, kept apart from yours"),
    "archive": ("Archive", "finished and superseded notes; excluded from search"),
    "templates": ("Templates", "note templates; excluded from search"),
}
# Kept out of search by `brain init` (the scan of `init` also recognises the archive).
SEARCH_EXCLUDED_ROLES = ("templates", "records", "archive")
EXAMPLE_PREFIX = "Example - "          # fictional notes; skipped with --no-examples
EXAMPLE_LINE = "{{example}}"          # a template line kept only when examples are written

CREDIT = ("Folder layout adapted from Avenox Beyin by Avenox "
          "(github.com/avenoxai/avenoxbeyin, MIT): names translated to English, emoji "
          "removed. No Avenox code or text is included.")
CREDIT_COMPAT = ("Folder layout follows Avenox Beyin by Avenox "
                 "(github.com/avenoxai/avenoxbeyin, MIT), using its original folder names "
                 "for compatibility. No Avenox code or text is included.")
PLUGIN_ID = "context-layer-brain"
IGNORED_IN_EMPTY = {".DS_Store", "Thumbs.db", "desktop.ini"}
TOKEN = re.compile(r"\{\{(folder:([a-z]+)|hubs|credit)\}\}")


class BrainError(ValueError):
    """A refusal a person is meant to read."""


def folders(layout: str, avenox_compat: bool = False) -> dict[str, str]:
    if layout not in LAYOUTS:
        raise BrainError(f"unknown layout {layout!r}; use one of: {', '.join(LAYOUTS)}")
    if avenox_compat and layout != "standard":
        raise BrainError("--avenox-compat applies to the standard layout only")
    return dict(AVENOX_COMPAT if avenox_compat else LAYOUTS[layout])


def _hub_line(mapping: dict[str, str], role: str) -> str:
    title, what = HUBS[role]
    if role in SEARCH_EXCLUDED_ROLES:
        # A link into an excluded folder could never be followed by search or the graph.
        return f"- {title} (`{mapping[role]}/`): {what}"
    return f"- [[{mapping[role]}/Index|{title}]]: {what}"


def _fill(text: str, mapping: dict[str, str], credit: str, source: str,
          examples: bool = True) -> str:
    def replace(match: re.Match) -> str:
        if match.group(1) == "hubs":
            return "\n".join(_hub_line(mapping, role) for role in HUBS if mapping.get(role))
        if match.group(1) == "credit":
            return credit
        role = match.group(2)
        if not mapping.get(role):
            raise BrainError(f"template {source} links role {role!r}, absent from this layout")
        return mapping[role]
    lines = []
    for line in text.splitlines(keepends=True):
        if line.startswith(EXAMPLE_LINE):
            if not examples:
                continue
            line = line[len(EXAMPLE_LINE):]
        lines.append(line)
    return TOKEN.sub(replace, "".join(lines))


def plan(layout: str = "standard", avenox_compat: bool = False,
         examples: bool = True) -> tuple[list[str], dict[str, str]]:
    """(directories, {relative path: text}) that make up the new vault. Pure."""
    mapping = folders(layout, avenox_compat)
    source = rules.template_dir("starter-brain")
    credit = CREDIT_COMPAT if avenox_compat else CREDIT
    directories: list[str] = []
    files: dict[str, str] = {}
    for role in ROLES:
        folder = mapping.get(role)
        if folder is None:
            continue
        if folder:
            directories.append(folder)
        role_dir = source / role
        for path in sorted(role_dir.glob("*.md")) if role_dir.is_dir() else []:
            if not folder and path.name == "Index.md":
                continue                      # a root-level layout has no hub for the root
            if not examples and path.name.startswith(EXAMPLE_PREFIX):
                continue
            relative = f"{folder}/{path.name}" if folder else path.name
            files[relative] = _fill(path.read_text(encoding="utf-8"), mapping, credit,
                                    f"{role}/{path.name}", examples)
    for role, sub in EXTRA_DIRS.get(layout, []):
        directories.append(f"{mapping[role]}/{sub}")
    vault_templates = rules.template_dir("vault")
    for name in rules.TEMPLATE_FILES:
        files[name] = (vault_templates / name).read_text(encoding="utf-8")
    files[".obsidian/app.json"] = (source / "obsidian" / "app.json").read_text(encoding="utf-8")
    return directories, files


def _is_empty(target: Path) -> bool:
    return all(entry.name in IGNORED_IN_EMPTY for entry in target.iterdir())


def _routes(target: Path, mapping: dict[str, str]) -> dict:
    """The regular `init` scan, plus the brain's own search exclusions.

    Templates, private records, the archive and the two rule files are excluded;
    a route loses the canonical sources that fall under an exclusion, and a
    route left with none is dropped (its sources could never be delivered).
    """
    from .vault_scan import render_config, scan_vault
    config = rules.exclude_rule_files(render_config(scan_vault(target)))
    extra = [mapping[role] + "/" for role in SEARCH_EXCLUDED_ROLES if mapping.get(role)]
    for key in rules.EXCLUSION_KEYS:
        values = list(config.get(key) or [])
        config[key] = sorted(set(values + extra))
    policy = rules.router_module("source_policy")
    prefixes = policy.config_exclusions(config)
    routes = config.get("routes") if isinstance(config.get("routes"), dict) else {}
    for name in list(routes):
        sources = routes[name].get("canonical_sources") or []
        kept = [item for item in sources if isinstance(item, dict)
                and not _excluded(policy, item.get("path"), prefixes)]
        if kept:
            routes[name]["canonical_sources"] = kept
        else:
            del routes[name]
    # The starter vault is English-only; keep only ASCII continuation terms.
    config["continuation_terms"] = [term for term in config.get("continuation_terms") or []
                                    if isinstance(term, str) and term.isascii()]
    config["_brain_exclusions"] = ("Added by `brain init`: templates, private records, the "
                                   "archive and the rule files CLAUDE.md and AGENTS.md (the "
                                   "agents load those at session start) stay out of search. "
                                   "Delete a prefix to make it searchable.")
    return config


def _excluded(policy, name, prefixes) -> bool:
    try:
        return policy.excluded(name, prefixes)
    except (TypeError, ValueError):
        return True


def init(path, layout: str = "standard", avenox_compat: bool = False,
         apply_now: bool = False, into_existing: bool = False,
         examples: bool | None = None) -> dict:
    """Create (or, with `into_existing`, complete) a starter vault.

    `examples`: None writes the example notes into a new vault and leaves them out
    with `into_existing`; True or False decides explicitly.
    """
    target = Path(path).expanduser()
    mapping = folders(layout, avenox_compat)
    if examples is None:
        examples = not into_existing
    directories, files = plan(layout, avenox_compat, examples)
    if target.exists() and not target.is_dir():
        raise BrainError(f"{target.name} exists and is not a directory")
    if target.is_dir() and not _is_empty(target) and not into_existing:
        raise BrainError(f"{target.name} is not empty; choose a new folder, or pass "
                         "--into-existing to add only the missing files (nothing is overwritten)")
    present = {name: (target / name).is_file() for name in rules.RULE_FILES}
    if sum(present.values()) == 1:
        # Keep the user's one rule file and mirror it, so the pair stays identical; the
        # template rules can be merged in later (`rules init` shows the diff).
        existing = next(name for name, found in present.items() if found)
        missing = next(name for name, found in present.items() if not found)
        with open(target / existing, encoding="utf-8", newline="") as handle:
            files[missing] = handle.read()
    create = [rel for rel in files if not (target / rel).exists()]
    new_directories = [d for d in directories if not (target / d).is_dir()]
    keep = [rel for rel in files if (target / rel).exists()]
    routes_rel = ".context/routes.json"
    routes_exists = (target / routes_rel).exists()
    written: list[str] = []
    if apply_now:
        target.mkdir(parents=True, exist_ok=True)
        for directory in directories:
            (target / directory).mkdir(parents=True, exist_ok=True)
        for rel in create:
            destination = target / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(destination, "x", encoding="utf-8", newline="") as handle:
                    handle.write(files[rel])
                written.append(rel)
            except FileExistsError:           # appeared since the plan: keep it
                keep.append(rel)
        if not routes_exists:
            config = _routes(target.resolve(), mapping)
            destination = target / routes_rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            with open(destination, "x", encoding="utf-8") as handle:
                handle.write(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
            written.append(routes_rel)
    routes_note = None
    if routes_exists:
        try:
            kept_config = rules.router_module("source_policy").load_config(target / routes_rel)
        except (ValueError, OSError):
            kept_config = None
        if kept_config is not None and rules.exclude_rule_files(kept_config) != kept_config:
            routes_note = ("kept .context/routes.json does not exclude CLAUDE.md and AGENTS.md "
                           "from search; `context-layer rules init <vault> --apply` adds them")
    return {"layout": layout, "avenox_compat": avenox_compat, "applied": apply_now,
            "examples": examples,
            "directories": directories, "new_directories": new_directories,
            "create": sorted(create), "keep": sorted(keep),
            "routes": "keep" if routes_exists else "create", "routes_note": routes_note,
            "written": sorted(written),
            "parity": rules.parity(target)["ok"] if apply_now else None}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _next_steps(path: str) -> str:
    quoted = path if re.fullmatch(r"[\w./~-]+", path) else json.dumps(path)
    return "\n".join([
        "Next steps:",
        f"  context-layer index {quoted}",
        f"  context-layer rules check {quoted}",
        f"  context-layer install claude-code --vault {quoted}          (dry run; add --apply)",
        f"  context-layer install claude-code --vault {quoted} --rules  (optional record hooks)",
        "Open the folder in Obsidian (Open folder as vault). To see the Brain View, copy",
        "obsidian-plugin/manifest.json, obsidian-plugin/styles.css and",
        "obsidian-plugin/dist/main.js (as main.js) from the context-layer repository into",
        f"<vault>/.obsidian/plugins/{PLUGIN_ID}/ (the bundle is prebuilt), then enable it",
        "under Settings > Community plugins. Nothing was enabled for you.",
    ])


def cmd_init(args: argparse.Namespace) -> int:
    extra = [token for token in getattr(args, "rest", []) or [] if token]
    if extra:
        print(f"context-layer brain init: unrecognised arguments: {' '.join(extra)}",
              file=sys.stderr)
        return 2
    try:
        result = init(args.path, layout=args.layout, avenox_compat=args.avenox_compat,
                      apply_now=args.apply, into_existing=args.into_existing,
                      examples=args.examples)
    except (BrainError, rules.RulesError, OSError) as exc:
        print(f"context-layer brain init: {exc}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        verb = "created" if result["applied"] else "would create"
        name = "standard (Avenox-compatible names)" if result["avenox_compat"] else result["layout"]
        print(f"Layout: {name}")
        print(f"{verb}: {len(result['new_directories'])} folders, {len(result['create'])} files, "
              f".context/routes.json ({result['routes']})")
        for rel in result["create"]:
            print(f"  + {rel}")
        for rel in result["keep"]:
            print(f"  = {rel} (exists, kept)")
        if not result["examples"]:
            print("  (example notes not written; --examples adds the missing ones)")
        if result["routes_note"]:
            print(f"note: {result['routes_note']}", file=sys.stderr)
        if not result["applied"]:
            print("dry run: nothing was written. Re-run with --apply.", file=sys.stderr)
        else:
            print(_next_steps(args.path))
    if result["applied"] and result["parity"] is False:
        print("context-layer brain init: CLAUDE.md and AGENTS.md differ in the target "
              "(an existing file was kept); run `context-layer rules check` and merge them.",
              file=sys.stderr)
        return 1
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    """Attach `brain init` to the CLI."""
    parser = sub.add_parser(
        "brain",
        help="Create a starter vault: folders, hubs, templates, examples and rules.",
        description="Creates a new Obsidian/Markdown vault with a folder layout, one hub note "
                    "per folder, a Dashboard, note templates, fictional example notes, the "
                    "rule files (CLAUDE.md = AGENTS.md, LOG.md, BACKLOG.md) and a starting "
                    ".context/routes.json. Dry run unless --apply.",
    )
    inner = parser.add_subparsers(dest="brain_command", required=True)
    p_init = inner.add_parser("init", help="Create the starter vault (dry run unless --apply).")
    p_init.add_argument("path", help="Folder for the new vault (created if missing).")
    p_init.add_argument("--layout", choices=list(LAYOUTS), default="standard",
                        help="standard (default) or minimal.")
    p_init.add_argument("--avenox-compat", action="store_true",
                        help="Use Avenox Beyin's original emoji-prefixed folder names "
                             "(standard layout only).")
    p_init.add_argument("--into-existing", action="store_true",
                        help="Allow a non-empty folder; only missing files are added. "
                             "Example notes are left out unless --examples.")
    examples = p_init.add_mutually_exclusive_group()
    examples.add_argument("--examples", dest="examples", action="store_true", default=None,
                          help="Write the fictional example notes (default for a new vault).")
    examples.add_argument("--no-examples", dest="examples", action="store_false",
                          help="Leave the fictional example notes out.")
    p_init.add_argument("--apply", action="store_true", help="Write the vault.")
    p_init.add_argument("--json", action="store_true", dest="as_json")
    p_init.set_defaults(func=cmd_init, forward_to=None)
