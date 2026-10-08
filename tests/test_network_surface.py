"""The static network/process boundary must catch new unauthorized access."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.check_network_surface import scan


class NetworkSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "context_layer").mkdir()
        (self.root / "router").mkdir()
        (self.root / "eval").mkdir()

    def write(self, name, source):
        path = self.root / name
        path.write_text(source, encoding="utf-8")

    def test_only_explicit_transports_can_import_network(self):
        self.write("context_layer/decisions_client.py", "import urllib.request\n")
        self.write("context_layer/responses_client.py", "import urllib.request\n")
        self.write("context_layer/github_client.py", "import socket\n")
        self.write("eval/retrieve.py", "import urllib.request\n")
        violations, files = scan(self.root)
        self.assertEqual(files, 4)
        self.assertEqual([(entry[0], entry[2]) for entry in violations],
                         [("eval/retrieve.py", "network-import")])

    def test_api_transports_cannot_spawn(self):
        for name in ("decisions_client.py", "responses_client.py"):
            with self.subTest(name=name):
                self.write(f"context_layer/{name}", "import subprocess\nsubprocess.run(['echo'])\n")
                violations, _ = scan(self.root)
                self.assertTrue(any(entry[0].endswith(name) and entry[2] == "api-process"
                                    for entry in violations))

    def test_dynamic_import_is_caught(self):
        self.write("router/probe.py", "importlib.import_module('ssl')\n")
        violations, _ = scan(self.root)
        self.assertEqual(violations[0][2], "network-import")


if __name__ == "__main__":
    unittest.main()
