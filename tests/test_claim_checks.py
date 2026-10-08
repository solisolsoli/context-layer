"""Fictional vault tests for provider-free claim citation checks."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from context_layer import claim_checks


ROUTES = {"record_type_allowlist": ["verbatim_text_file"],
          "routes": {}, "fallback_routes": [], "aliases": {},
          "exclude_prefixes": ["private"]}


class ClaimChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / "vault"
        (self.vault / ".context").mkdir(parents=True)
        (self.vault / ".context" / "routes.json").write_text(
            json.dumps(ROUTES), encoding="utf-8")
        (self.vault / "notes").mkdir()
        self.source = self.vault / "notes" / "lamp-plan.md"
        self.source.write_text("# Lamp plan\n\nThe red lamp switches off at dawn.\n"
                               "The backup lamp stays on until noon.\n", encoding="utf-8")

    def cite(self, span="The red lamp switches off at dawn.", **changes):
        citation = {"source_path": "notes/lamp-plan.md",
                    "source_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
                    "line_start": 3, "line_end": 3, "span": span}
        citation.update(changes)
        return citation

    def document(self, citation=None, claim="The red lamp switches off at dawn."):
        return {"schema": claim_checks.CLAIMS_SCHEMA,
                "claims": [{"id": "lamp-1", "text": claim,
                            "citations": [citation or self.cite()]}]}

    def test_exact_source_and_lines_check_without_approving_claim(self):
        result = claim_checks.check_claims(self.vault, self.document())
        self.assertEqual(result["schema"], claim_checks.REPORT_SCHEMA)
        self.assertEqual((result["citations"], result["mechanically_checked"]), (1, 1))
        self.assertEqual(result["claims"][0]["id"], "lamp-1")
        citation = result["claims"][0]["citations"][0]
        self.assertTrue(citation["mechanically_checked"])
        self.assertEqual(citation["reasons"], [])
        self.assertEqual(citation["anchor_notes"], [])
        self.assertFalse(result["approved"])
        self.assertFalse(result["memory_written"])

    def test_stale_hash_wrong_lines_and_nonverbatim_span_fail(self):
        cases = [self.cite(source_sha256="0" * 64),
                 self.cite(line_start=4, line_end=4),
                 self.cite(span="The blue lamp switches off at dawn.")]
        for citation in cases:
            with self.subTest(citation=citation):
                result = claim_checks.check_claims(self.vault, self.document(citation))
                entry = result["claims"][0]["citations"][0]
                self.assertFalse(entry["mechanically_checked"])
                self.assertTrue(entry["reasons"])
                self.assertEqual(entry["anchor_notes"], [])

    def test_excluded_path_and_parent_traversal_are_refused(self):
        private = self.vault / "private"
        private.mkdir()
        private_note = private / "memo.md"
        private_note.write_text("Private lamp schedule is held locally.\n", encoding="utf-8")
        for name, path, span in (("private/memo.md", private_note,
                                  "Private lamp schedule is held locally."),
                                 ("notes/../notes/lamp-plan.md", self.source,
                                  "The red lamp switches off at dawn.")):
            with self.subTest(name=name):
                cite = self.cite(span=span, source_path=name,
                                 source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                 line_start=1, line_end=1)
                entry = claim_checks.check_claims(
                    self.vault, self.document(cite))["claims"][0]["citations"][0]
                self.assertFalse(entry["mechanically_checked"])
                self.assertTrue(entry["reasons"])

    def test_anchor_notes_are_advisory_only(self):
        result = claim_checks.check_claims(
            self.vault, self.document(claim="The red lamp switches off at 22:00."))
        citation = result["claims"][0]["citations"][0]
        self.assertTrue(citation["mechanically_checked"])
        self.assertTrue(any("22" in note for note in citation["anchor_notes"]))
        self.assertFalse(result["approved"])

    def test_input_shape_is_bounded(self):
        good = self.document()
        bad = [[], {**good, "schema": "unknown/v1"}, {"claims": []},
               {"claims": [good["claims"][0]] * 21},
               {"claims": [{"text": " ", "citations": [self.cite()]}]},
               {"claims": [{"text": "valid claim", "citations": [self.cite(line_start=True)]}]},
               {"claims": [{"text": "valid claim", "citations": [self.cite(span=" ")]}]}]
        for document in bad:
            with self.subTest(document=document):
                with self.assertRaises(claim_checks.Refused):
                    claim_checks.parse_claims(document)


if __name__ == "__main__":
    unittest.main()
