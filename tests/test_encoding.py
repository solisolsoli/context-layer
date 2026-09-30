#!/usr/bin/env python3
"""Runtime text I/O names its encoding (audit E-17).

Every `open(...)`, `Path.read_text` and `Path.write_text` call in the runtime code
(`context_layer/`, `router/` except its tests, `eval/`, `scripts/`) either opens the file
in binary mode or passes `encoding=`. A call that relies on the locale encoding fails
this test and is named, with its file and line. The scan is syntactic (`ast`), so it
needs no locale, no vault and no network.
"""
import ast
from pathlib import Path
import unittest

REPO = Path(__file__).resolve().parent.parent
ROOTS = ("context_layer", "router", "eval", "scripts")


def is_test_file(path: Path) -> bool:
    return path.name.startswith("test_") or path.parts[-2:-1] == ("tests",)


def text_calls_without_encoding(source: str) -> list[int]:
    """Line numbers of text-mode file calls that do not pass `encoding=`."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "open":
            mode_index = 1
        elif (isinstance(func, ast.Attribute) and func.attr == "fdopen"
              and isinstance(func.value, ast.Name) and func.value.id == "os"):
            mode_index = 1
        elif isinstance(func, ast.Attribute) and func.attr in ("read_text", "write_text"):
            mode_index = None
        else:
            continue
        if any(keyword.arg == "encoding" for keyword in node.keywords):
            continue
        mode = None
        if mode_index is not None and len(node.args) > mode_index:
            mode = node.args[mode_index]
        for keyword in node.keywords:
            if keyword.arg == "mode":
                mode = keyword.value
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value:
            continue
        found.append(node.lineno)
    return sorted(found)


class RuntimeTextIoNamesItsEncoding(unittest.TestCase):
    def test_no_runtime_call_relies_on_the_locale_encoding(self):
        offenders = []
        for root in ROOTS:
            for path in sorted((REPO / root).rglob("*.py")):
                if is_test_file(path):
                    continue
                for line in text_calls_without_encoding(path.read_text(encoding="utf-8")):
                    offenders.append(f"{path.relative_to(REPO).as_posix()}:{line}")
        self.assertEqual(offenders, [], "text I/O without encoding= (E-17)")

    def test_the_scan_notices_each_shape_it_claims_to_catch(self):
        source = "\n".join([
            "open('a')",                                   # 1
            "open('a', 'w')",                              # 2
            "open('a', mode='a+')",                        # 3
            "p.read_text()",                               # 4
            "p.write_text('x')",                           # 5
            "os.fdopen(3, 'w')",                           # 6
            "open('a', 'rb')",                             # 7 binary: fine
            "open('a', mode='wb')",                        # 8 binary: fine
            "open('a', encoding='utf-8')",                 # 9 fine
            "open('a', 'w', encoding='utf-8')",            # 10 fine
            "p.read_text(encoding='utf-8')",               # 11 fine
            "p.write_text('x', encoding='utf-8')",         # 12 fine
            "os.fdopen(3, 'wb')",                          # 13 fine
            "urlopen(request).open()",                     # 14 not a file call
        ])
        self.assertEqual(text_calls_without_encoding(source), [1, 2, 3, 4, 5, 6])


if __name__ == "__main__":
    unittest.main()
