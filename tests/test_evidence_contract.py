"""Synthetic delivery-contract and command integration regressions, not a benchmark."""
import copy
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "eval"))
from evidence_contract import score_delivery, validate_contract
from evaluate import check_gate

FIXTURES = REPO / "tests/fixtures"


class EvidenceDelivery(unittest.TestCase):
    def setUp(self):
        self.contract = validate_contract(json.loads((FIXTURES / "evidence-contract.json").read_text()))
        self.cases = json.loads((FIXTURES / "evidence-cases.json").read_text())["cases"]

    def test_frozen_negative_and_positive_controls(self):
        for case in self.cases:
            with self.subTest(case=case["id"]):
                result = score_delivery(self.contract, case["id"], json.dumps(case["packet"]))
                for key, expected in case["expected"].items():
                    self.assertEqual(result[key], expected, (key, result))

    def test_missing_frozen_alternative_rejected(self):
        del self.contract["sources"]["mirror/policy-current.md"]
        with self.assertRaises((KeyError, ValueError)):
            validate_contract(self.contract)

    def test_malformed_contract_rejected(self):
        for contract in [None, [], {}, {"schema": "evidence-contract-v1", "sources": []}]:
            with self.subTest(contract=contract), self.assertRaises(ValueError):
                validate_contract(contract)

    def test_changed_frozen_digest_rejected(self):
        self.contract["sources"]["docs/policy-current.md"]["text"] += "changed"
        with self.assertRaises(ValueError):
            validate_contract(self.contract)

    def test_empty_requirement_rejected(self):
        self.contract["queries"]["filename_only"]["required_groups"] = [[]]
        with self.assertRaises(ValueError):
            validate_contract(self.contract)

    def test_malformed_and_metadata_only_packets_do_not_pass(self):
        for packet in ["docs/policy-current.md", "[]", "null", "{}", '{"status":"SUPPORTED"}']:
            with self.subTest(packet=packet):
                result = score_delivery(self.contract, "filename_only", packet)
                self.assertEqual(result["hit_count"], 0)
                self.assertFalse(result["case_pass"])

    def test_operational_failure_and_extra_fabrication_zero_all_hits(self):
        valid = next(c for c in self.cases if c["id"] == "valid_alternative_sources")
        stdout = json.dumps(valid["packet"])
        self.assertEqual(score_delivery(self.contract, valid["id"], stdout, command_ok=False)["hit_count"], 0)
        packet = copy.deepcopy(valid["packet"])
        packet["evidence"].append({"source_path": "unknown.md", "source_sha256": "0", "content": "fake"})
        self.assertEqual(score_delivery(self.contract, valid["id"], json.dumps(packet))["hit_count"], 0)

    def test_diagnostic_scores_cannot_promote(self):
        gate = check_gate({"hit_rate": 1, "mean_cost_chars": 1, "router_failures": 0}, {},
                          {"min_hit_rate": 0.5, "max_mean_cost_chars": 100})
        self.assertFalse(gate["pass"])
        self.assertTrue(any(c["condition"] == "independent semantic acceptance" and not c["pass"]
                            for c in gate["checks"]))

    def test_runner_never_scores_failed_process_stdout(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            command = td / "broken.py"
            command.write_text('print("policy-current.md")\nraise SystemExit(1)\n')
            stimuli = td / "stimuli.jsonl"
            stimuli.write_text(json.dumps({"id": "q", "prompt": "test", "expected_sources": ["docs/policy-current.md"]})+'\n')
            result_path = td / "result.json"
            result = subprocess.run([sys.executable, str(REPO / "eval/evaluate.py"), "--command",
                shlex.join([sys.executable, str(command)]), "--stimuli", str(stimuli), "--out", str(result_path), "--quiet"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result_path.read_text())["summary"]
            self.assertEqual(summary["total_hit"], 0)
            self.assertEqual(summary["router_failures"], 1)
            self.assertGreater(summary["total_cost_chars"], 0)

    def test_actual_router_output_and_abstention_through_runner(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            vault = td / "vault"
            vault.mkdir()
            body = self.contract["sources"]["docs/policy-current.md"]
            (vault / "docs").mkdir()
            (vault / "docs/policy-current.md").write_bytes(body["text"].encode())
            (vault / ".context").mkdir()
            (vault / ".context/routes.json").write_text(json.dumps({"routes": {}, "fallback_routes": [],
                                                                   "record_type_allowlist": ["verbatim_text_file"]}))
            built = subprocess.run([sys.executable, str(REPO / "router/build_index.py"), "--vault", str(vault)], capture_output=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            stimuli = td / "stimuli.jsonl"
            stimuli.write_text('\n'.join(json.dumps(row) for row in [
                {"id": "filename_only", "prompt": "releases reviewers", "expected_sources": ["docs/policy-current.md"]},
                {"id": "valid_abstention_unanswerable", "prompt": "zzunmatchedword", "expected_sources": []}])+'\n')
            result_path = td / "result.json"
            command = [sys.executable, str(REPO / "router/context_router.py"), "--vault", str(vault),
                       "--evidence-json", "--no-save", "--prompt"]
            result = subprocess.run([sys.executable, str(REPO / "eval/evaluate.py"), "--command", shlex.join(command),
                "--stimuli", str(stimuli), "--evidence-contract", str(FIXTURES / "evidence-contract.json"),
                "--out", str(result_path), "--quiet"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result_path.read_text())
            self.assertEqual(report["summary"]["total_hit"], 1)
            self.assertEqual(report["summary"]["correct_abstentions"], 1)
            self.assertEqual(report["summary"]["router_failures"], 0)
            self.assertFalse(report["summary"]["promotion_eligible"])
            self.assertEqual(report["results"][0]["hit_rate"], 1)


class RubricScorer(unittest.TestCase):
    """A-21: the rubric reads evidence, and a missing packet is not a skipped case."""

    CASES = {"cases": [
        {"id": "C1", "category": "demo", "prompt": "what is the quokka permit expiry",
         "decisive_facts": [{"pattern": "quokka"}]},
        {"id": "C2", "category": "demo", "prompt": "when does the ferry leave",
         "decisive_facts": [{"pattern": "07:45"}]}]}
    PACKET = ("# Evidence-bound context packet\n\n- Evidence status: **PARTIAL**\n\n"
              "## Exact user prompt\n\n```text\nwhat is the quokka permit expiry\n```\n\n"
              "## Routing metadata\n\n```json\n{\"tokens\": [\"quokka\", \"permit\", "
              "\"expiry\"], \"exact_phrases\": []}\n```\n\n## Verbatim evidence\n\n"
              "### E001 \u2014 `notes/quokka-permit.md`\n\n- Locator: `chunk 0`\n"
              "- Timestamp: `2026-01-01`\n- Authority class: `verbatim_project_or_archive_text`\n"
              "- Source hash state: `verified`\n\n```text\nUnrelated text about harbor fees.\n"
              "```\n")

    def score(self, packets):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "cases.json").write_text(json.dumps(self.CASES))
            (td / "packets").mkdir()
            for case_id, text in packets.items():
                (td / "packets" / f"packet-{case_id}.md").write_text(text)
            done = subprocess.run([sys.executable, str(REPO / "eval/score_packet.py"), "--cases",
                                   str(td / "cases.json"), "--batch-dir", str(td / "packets"),
                                   "--out", str(td / "out.json")], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            return json.loads((td / "out.json").read_text()), done.stdout.strip().splitlines()[-1]

    def test_words_outside_evidence_blocks_earn_nothing(self):
        # "quokka" is in the echoed prompt, the routing metadata and the block heading's
        # path, never in the evidence text itself.
        result, _ = self.score({"C1": self.PACKET, "C2": self.PACKET})
        c1 = next(r for r in result["results"] if r["case_id"] == "C1")
        self.assertEqual(c1["decisive_fact"]["score"], 0)
        self.assertEqual(c1["sourcing_grade"]["score"], 2)
        found = self.PACKET.replace("Unrelated text about harbor fees.",
                                    "The quokka permit expires in May.")
        result, _ = self.score({"C1": found, "C2": self.PACKET})
        c1 = next(r for r in result["results"] if r["case_id"] == "C1")
        self.assertEqual(c1["decisive_fact"]["score"], 2)

    def test_missing_packet_counts_as_zero_and_is_reported(self):
        result, headline = self.score({"C1": self.PACKET})
        aggregate = result["aggregate"]
        c2 = next(r for r in result["results"] if r["case_id"] == "C2")
        self.assertTrue(c2["missing_packet"])
        self.assertEqual([c2[ax]["score"] for ax in ("decisive_fact", "sourcing_grade", "waste")],
                         [0, 0, 0])
        self.assertEqual(c2["timeliness"]["score"], "N/A")        # the case defines none
        self.assertEqual((aggregate["cases_scored"], aggregate["missing_packets"]), (1, 1))
        self.assertEqual((aggregate["points_earned"], aggregate["points_possible"]), (2, 12))
        self.assertIn("rubric score: 2/12 (16.7%)", headline)
        self.assertIn("1 missing packet(s) counted as 0", headline)


if __name__ == "__main__":
    unittest.main()
