#!/usr/bin/env python3
"""Keep network access in the two explicit transports, and model CLI access
in the optional advisor transport (Jev).

An AST scan (nothing is imported or run) of every .py file under context_layer/,
router/ and eval/. Three rules:

  network-import    Only context_layer/jev_client.py and github_client.py may import urllib.request,
                    http.client, ssl, socket, asyncio or another network module
                    (plain imports, `from urllib import request`, attribute use
                    such as `urllib.request.urlopen`, `__import__("socket")` and
                    `importlib.import_module("ssl")` all count). urllib.parse
                    stays allowed.
  jev-cli-spawn     A module that references Jev (any identifier, import or
                    string containing "jev") must not start `claude` or `codex`
                    (a process call whose arguments name either) and must not
                    use the task backends' `backends.plan`, which grants a
                    writing agent's permissions. context_layer/tasks.py and
                    context_layer/backends.py are exempt: they run the existing
                    task backends, not the advisor.
  jev-module-spawn  The advisor's own modules (file names starting with "jev")
                    other than jev_client.py start no process at all: no
                    subprocess, pty or multiprocessing import, no backends
                    import, no os.system/popen/exec*/spawn*/fork call.

Exit 0 and one summary line when clean; exit 1 with `path:line: rule: detail`
for every violation (or a file that cannot be parsed); exit 2 on usage errors.
Python 3.10+; standard library only.
"""
from __future__ import annotations

import argparse
import ast
from pathlib import Path
import sys

SCANNED = ("context_layer", "router", "eval")
ALLOWED = "context_layer/jev_client.py"
NETWORK_ALLOWED = frozenset({ALLOWED, "context_layer/github_client.py"})
SPAWN_EXEMPT = ("context_layer/tasks.py", "context_layer/backends.py")
NETWORK_MODULES = (
    "urllib.request", "http.client", "http.server", "http.cookiejar", "ssl", "socket",
    "socketserver", "asyncio", "ftplib", "smtplib", "poplib", "imaplib", "nntplib",
    "telnetlib", "xmlrpc", "webbrowser", "requests", "urllib3", "httpx", "aiohttp",
)
MODEL_CLIS = ("claude", "codex")
SPAWN_MODULES = ("subprocess", "pty", "multiprocessing")
SUBPROCESS_CALLS = ("run", "Popen", "call", "check_call", "check_output", "getoutput",
                    "getstatusoutput")
OS_SPAWN_PREFIXES = ("system", "popen", "exec", "spawn", "posix_spawn", "fork")


def is_network(name: str) -> bool:
    return any(name == module or name.startswith(module + ".") for module in NETWORK_MODULES)


def dotted(node: ast.AST) -> str | None:
    """`a.b.c` for a chain of attributes on a name, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def strings(node: ast.AST):
    """Every string constant inside an expression (lists, tuples, calls included)."""
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            yield child.value


def names_model_cli(text: str) -> bool:
    words = text.split()
    if not words:
        return False
    first = words[0].replace("\\", "/").rsplit("/", 1)[-1]
    return first in MODEL_CLIS


def references_jev(tree: ast.AST, relative: str) -> bool:
    if "jev" in Path(relative).name.lower():
        return True
    for node in ast.walk(tree):
        texts = []
        if isinstance(node, ast.Name):
            texts.append(node.id)
        elif isinstance(node, ast.Attribute):
            texts.append(node.attr)
        elif isinstance(node, ast.alias):
            texts += [node.name, node.asname or ""]
        elif isinstance(node, ast.ImportFrom):
            texts.append(node.module or "")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            texts.append(node.value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            texts.append(node.name)
        elif isinstance(node, ast.arg):
            texts.append(node.arg)
        elif isinstance(node, ast.keyword):
            texts.append(node.arg or "")
        if any("jev" in text.lower() for text in texts):
            return True
    return False


class Scanner(ast.NodeVisitor):
    def __init__(self, relative: str, jev_reference: bool):
        self.relative = relative
        self.allowed = relative == ALLOWED
        self.network_allowed = relative in NETWORK_ALLOWED
        self.jev_reference = jev_reference and not self.allowed \
            and relative not in SPAWN_EXEMPT
        self.jev_module = Path(relative).name.startswith("jev") and not self.allowed
        self.violations = []
        self.spawn_names = {}    # local name -> "subprocess.run" etc.
        self.plan_names = set()  # local names bound to backends.plan
        self.backends_names = set()

    def flag(self, node: ast.AST, rule: str, detail: str) -> None:
        self.violations.append((self.relative, getattr(node, "lineno", 0), rule, detail))

    # -- imports -----------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if is_network(alias.name) and not self.network_allowed:
                self.flag(node, "network-import", f"imports {alias.name}")
            root = alias.name.split(".")[0]
            if self.jev_module and root in SPAWN_MODULES:
                self.flag(node, "jev-module-spawn", f"imports {alias.name}")
            if alias.name in ("context_layer.backends",):
                self.backends_names.add(alias.asname or alias.name)
                if self.jev_module:
                    self.flag(node, "jev-module-spawn", "imports the task backends")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            full = f"{module}.{alias.name}" if module else alias.name
            if node.level == 0 and (is_network(module) or is_network(full)) and not self.network_allowed:
                self.flag(node, "network-import", f"imports {full}")
            if node.level == 0 and module.split(".")[0] in SPAWN_MODULES:
                if self.jev_module:
                    self.flag(node, "jev-module-spawn", f"imports {full}")
                self.spawn_names[alias.asname or alias.name] = full
            backends_module = module.endswith("backends") and (node.level > 0 or module ==
                                                               "context_layer.backends")
            if alias.name == "backends" and (node.level > 0 or module == "context_layer"):
                self.backends_names.add(alias.asname or alias.name)
                if self.jev_module:
                    self.flag(node, "jev-module-spawn", "imports the task backends")
            if backends_module:
                if alias.name == "plan":
                    self.plan_names.add(alias.asname or alias.name)
                if self.jev_module:
                    self.flag(node, "jev-module-spawn", "imports from the task backends")
        self.generic_visit(node)

    # -- uses --------------------------------------------------------------

    def visit_Attribute(self, node: ast.Attribute) -> None:
        name = dotted(node)
        if name and not self.network_allowed and name.split(".")[0] in ("urllib", "http", "xmlrpc"):
            head = ".".join(name.split(".")[:2])
            if is_network(head):
                self.flag(node, "network-import", f"uses {head}")
                return  # one report per chain
        self.generic_visit(node)

    def spawn_target(self, func: ast.AST) -> str | None:
        name = dotted(func)
        if name is None:
            return None
        if name in self.spawn_names:
            return self.spawn_names[name]
        parts = name.split(".")
        if parts[0] == "subprocess" and len(parts) == 2 and parts[1] in SUBPROCESS_CALLS:
            return name
        if parts[0] == "os" and len(parts) == 2 and parts[1].startswith(OS_SPAWN_PREFIXES):
            return name
        if parts[0] == "asyncio" and len(parts) == 2 and parts[1].startswith("create_subprocess"):
            return name
        if name == "pty.spawn":
            return name
        return None

    def visit_Call(self, node: ast.Call) -> None:
        name = dotted(node.func)
        if name in ("__import__", "importlib.import_module") and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str) \
                    and is_network(first.value) and not self.network_allowed:
                self.flag(node, "network-import", f"imports {first.value} dynamically")
        target = self.spawn_target(node.func)
        if target is not None:
            if self.jev_module:
                self.flag(node, "jev-module-spawn", f"starts a process ({target})")
            if self.jev_reference:
                arguments = list(node.args) + [keyword.value for keyword in node.keywords]
                if any(names_model_cli(text) for argument in arguments
                       for text in strings(argument)):
                    self.flag(node, "jev-cli-spawn", f"starts a model CLI ({target})")
        if self.jev_reference and name is not None:
            parts = name.split(".")
            if name in self.plan_names or (len(parts) == 2 and parts[1] == "plan"
                                           and parts[0] in self.backends_names | {"backends"}):
                self.flag(node, "jev-cli-spawn", "uses backends.plan (task backend argv)")
        self.generic_visit(node)


def scan(root: Path) -> tuple:
    violations, files = [], 0
    for top in SCANNED:
        base = root / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(root).as_posix()
            files += 1
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            except (SyntaxError, UnicodeDecodeError, ValueError) as error:
                violations.append((relative, getattr(error, "lineno", 0) or 0, "parse-error",
                                   "cannot be parsed"))
                continue
            scanner = Scanner(relative, references_jev(tree, relative))
            scanner.visit(tree)
            violations += scanner.violations
    return sorted(set(violations)), files


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="repository root to scan (default: this checkout)")
    options = parser.parse_args(argv)
    root = options.root.resolve()
    if not root.is_dir():
        parser.error("--root must be a directory")
    violations, files = scan(root)
    for relative, line, rule, detail in violations:
        print(f"{relative}:{line}: {rule}: {detail}")
    if violations:
        print(f"network surface: {len(violations)} violation(s) in {files} files", file=sys.stderr)
        return 1
    print(f"network surface: ok ({files} files; network only in "
          f"{', '.join(sorted(NETWORK_ALLOWED))}; model CLI for Jev only in {ALLOWED})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
