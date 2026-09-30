#!/usr/bin/env python3
"""The evaluation adapters in eval/adapters: index and cache identity.

Every test builds its own tiny fictional vaults in a temporary directory; HOME
points there too. No real vault, model or network is used.
"""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from _portable_helpers import isolated_home_env

REPO = Path(os.environ.get("TEST_REPO_HOME", Path(__file__).resolve().parents[1]))
ADAPTERS = REPO / "eval" / "adapters"
sys.path.insert(0, str(ADAPTERS))

import embedding_stub  # noqa: E402


class FtsAdapterIndex(unittest.TestCase):
    """E-23: an index built for one vault never answers for another."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = isolated_home_env(os.environ, str(self.root))
        self.a = self.vault("vault-a", {"alpha.md": "# Alpha\n\nThe alpha crossing budget is 10 credits.\n"})
        self.b = self.vault("vault-b", {"zebra.md": "# Zebra\n\nThe zebra crossing budget is 40 credits.\n"})
        # vault B's note is older than anything in vault A, the case an mtime check misses
        os.utime(self.b / "zebra.md", (946684800, 946684800))

    def vault(self, name, files):
        path = self.root / name
        path.mkdir()
        for rel, text in files.items():
            (path / rel).write_text(text, encoding="utf-8")
        return path

    def query(self, vault, *extra):
        done = subprocess.run([sys.executable, str(ADAPTERS / "fts_sqlite.py"), "--vault", str(vault),
                               *extra, "crossing budget"], capture_output=True, text=True,
                              cwd=self.root, env=self.env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_one_index_path_for_two_vaults_answers_from_the_right_one(self):
        index = str(self.root / "shared.sqlite3")
        first = self.query(self.a, "--index", index)
        self.assertIn("alpha.md", first)
        second = self.query(self.b, "--index", index)
        self.assertIn("zebra.md", second)
        self.assertNotIn("alpha.md", second)
        self.assertIn("Index: built", second)
        self.assertIn("Index: reused", self.query(self.b, "--index", index))

    def test_default_index_is_one_file_per_vault(self):
        self.query(self.a)
        self.query(self.b)
        self.assertEqual(len(list(self.root.glob(".eval-fts-index-*.sqlite3"))), 2)

    def test_a_deleted_note_forces_a_rebuild(self):
        (self.a / "beta.md").write_text("# Beta\n\nThe beta crossing budget is 7 credits.\n",
                                        encoding="utf-8")
        index = str(self.root / "a.sqlite3")
        self.assertIn("beta.md", self.query(self.a, "--index", index))
        (self.a / "beta.md").unlink()
        after = self.query(self.a, "--index", index)
        self.assertIn("Index: built", after)
        self.assertNotIn("beta.md", after)


class EmbeddingCache(unittest.TestCase):
    """E-24: cached vectors are keyed by the model, not by the text alone."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        (self.vault / "note.md").write_text("# Note\n\nThe harbour lantern is green.\n", encoding="utf-8")
        self.cache = self.root / "cache.json"
        self.calls = []

        def embed(texts, kind="document"):
            self.calls.append((kind, len(texts)))
            return embedding_stub.smoke_embed(texts, kind)

        for name, value in (("CONFIGURED", True), ("embed_texts", embed), ("MODEL_ID", "model-a")):
            original = getattr(embedding_stub, name)
            setattr(embedding_stub, name, value)
            self.addCleanup(setattr, embedding_stub, name, original)

    def run_adapter(self):
        argv = ["embedding_stub.py", "--vault", str(self.vault), "--cache", str(self.cache), "lantern"]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            original = sys.argv
            sys.argv = argv
            try:
                return embedding_stub.main()
            finally:
                sys.argv = original

    def document_calls(self):
        return [c for c in self.calls if c[0] == "document"]

    def test_the_same_model_hits_the_cache(self):
        self.assertEqual(self.run_adapter(), 0)
        self.assertEqual(len(self.document_calls()), 1)
        self.assertEqual(self.run_adapter(), 0)
        self.assertEqual(len(self.document_calls()), 1)

    def test_another_model_misses_the_cache(self):
        self.assertEqual(self.run_adapter(), 0)
        embedding_stub.MODEL_ID = "model-b"
        self.assertEqual(self.run_adapter(), 0)
        self.assertEqual(len(self.document_calls()), 2)

    def test_keys_differ_by_model_and_kind(self):
        key = embedding_stub.cache_key
        self.assertNotEqual(key("t", "document", "a"), key("t", "document", "b"))
        self.assertNotEqual(key("t", "document", "a"), key("t", "query", "a"))
        self.assertEqual(key("t", "document", "a"), key("t", "document", "a"))

    def test_a_configured_adapter_without_model_id_refuses_to_run(self):
        embedding_stub.MODEL_ID = ""
        self.assertEqual(self.run_adapter(), 2)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
