#!/usr/bin/env python3
"""AST guard: only explicit GitHub, Decisions and Responses clients may open a network.

The API transports must never start a process or invoke a task backend.
This check imports no project modules and performs no network I/O.
"""
from __future__ import annotations

import argparse
import ast
from pathlib import Path
import sys

SCANNED = ("context_layer", "router", "eval")
NETWORK_ALLOWED = frozenset({"context_layer/github_client.py",
                             "context_layer/decisions_client.py",
                             "context_layer/responses_client.py"})
DECISIONS_CLIENT = "context_layer/decisions_client.py"
RESPONSES_CLIENT = "context_layer/responses_client.py"
NETWORK_MODULES = (
    "urllib.request", "http.client", "http.server", "http.cookiejar", "ssl", "socket",
    "socketserver", "asyncio", "ftplib", "smtplib", "poplib", "imaplib", "nntplib",
    "telnetlib", "xmlrpc", "webbrowser", "requests", "urllib3", "httpx", "aiohttp",
)
SPAWN_MODULES = ("subprocess", "pty", "multiprocessing")
OS_SPAWN_PREFIXES = ("system", "popen", "exec", "spawn", "posix_spawn", "fork")


def is_network(name: str) -> bool:
    return any(name == module or name.startswith(module + ".") for module in NETWORK_MODULES)


def dotted(node: ast.AST) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


class Scanner(ast.NodeVisitor):
    def __init__(self, relative: str):
        self.relative = relative
        self.network_allowed = relative in NETWORK_ALLOWED
        self.api_client = relative in (DECISIONS_CLIENT, RESPONSES_CLIENT)
        self.violations = []
        self.spawn_names = set()

    def flag(self, node: ast.AST, rule: str, detail: str) -> None:
        self.violations.append((self.relative, getattr(node, "lineno", 0), rule, detail))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if is_network(alias.name) and not self.network_allowed:
                self.flag(node, "network-import", f"imports {alias.name}")
            if self.api_client and alias.name.split(".")[0] in SPAWN_MODULES:
                self.flag(node, "api-process", f"imports {alias.name}")
            if self.api_client and alias.name == "context_layer.backends":
                self.flag(node, "api-process", "imports task backends")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            full = f"{module}.{alias.name}" if module else alias.name
            if node.level == 0 and (is_network(module) or is_network(full)) and not self.network_allowed:
                self.flag(node, "network-import", f"imports {full}")
            if self.api_client and (module.split(".")[0] in SPAWN_MODULES or
                                    module.endswith("backends") or alias.name == "backends"):
                self.flag(node, "api-process", f"imports {full}")
            if module.split(".")[0] in SPAWN_MODULES:
                self.spawn_names.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        name = dotted(node)
        if name and not self.network_allowed and name.split(".")[0] in ("urllib", "http", "xmlrpc"):
            head = ".".join(name.split(".")[:2])
            if is_network(head):
                self.flag(node, "network-import", f"uses {head}")
                return
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = dotted(node.func)
        if name in ("__import__", "importlib.import_module") and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                if is_network(first.value) and not self.network_allowed:
                    self.flag(node, "network-import", f"imports {first.value} dynamically")
                if self.api_client and first.value.split(".")[0] in SPAWN_MODULES:
                    self.flag(node, "api-process", f"imports {first.value} dynamically")
        if self.api_client and name:
            if (name in self.spawn_names or name.startswith("subprocess.") or
                name.startswith("pty.") or name.startswith("multiprocessing.") or
                name.startswith("os.") and name[3:].startswith(OS_SPAWN_PREFIXES) or
                name.startswith("asyncio.create_subprocess") or name.endswith("backends.plan")):
                self.flag(node, "api-process", f"starts a process ({name})")
        self.generic_visit(node)


def scan(root: Path) -> tuple[list[tuple], int]:
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
            except (SyntaxError, UnicodeDecodeError, ValueError):
                violations.append((relative, 0, "parse-error", "cannot be parsed"))
                continue
            scanner = Scanner(relative)
            scanner.visit(tree)
            violations.extend(scanner.violations)
    return sorted(set(violations)), files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
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
          f"{', '.join(sorted(NETWORK_ALLOWED))}; API transports cannot spawn processes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
