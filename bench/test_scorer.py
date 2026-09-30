#!/usr/bin/env python3
"""Scorer tests with hand-built packets: python3 -m unittest bench/test_scorer.py

Nothing here runs a retrieval method; every packet is written by hand so the
expected score is known before the scorer computes it.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_offline as ro  # noqa: E402
import seal  # noqa: E402

BRIDGE = {
    "id": "T1", "type": "bridge_2hop", "question": "q",
    "gold": [{"path": "projects/a.md", "must_contain": "Lead: [[Ada Example]]"},
             {"path": "people/Ada Example.md", "must_contain": "on leave until 1 May"}],
}
SUPERSEDED = {
    "id": "T2", "type": "supersession", "question": "q",
    "gold": [{"path": "decisions/new.md", "must_contain": "width is 2.2 m"}],
    "distractors": [{"path": "decisions/old.md", "must_contain": "width is 1.8 m"}],
}
UNANSWERABLE = {"id": "T3", "type": "unanswerable", "question": "q", "gold": []}
FILES = {"projects/a.md": 400, "people/Ada Example.md": 101, "decisions/new.md": 8,
         "decisions/old.md": 12}


def packet(*items):
    return {"schema": "evidence-delivery-v1", "operation_status": "ok", "status": "PARTIAL",
            "evidence": [{"source_path": p, "source_sha256": "0" * 64, "content": c} for p, c in items]}


def passages(*items):
    return ro.extract_passages(packet(*items))


class ScoreCase(unittest.TestCase):
    def test_complete_bridge(self):
        row = ro.score_case(BRIDGE, passages(("projects/a.md", "x\nLead: [[Ada Example]]\ny"),
                                             ("people/Ada Example.md", "She is on leave until 1 May.")),
                            FILES)
        self.assertTrue(row["complete"])
        self.assertEqual(row["gold_recall"], 1.0)
        self.assertTrue(row["required_paths_present"])

    def test_half_bridge_is_not_complete(self):
        row = ro.score_case(BRIDGE, passages(("projects/a.md", "Lead: [[Ada Example]]")), FILES)
        self.assertFalse(row["complete"])
        self.assertEqual(row["gold_recall"], 0.5)
        self.assertFalse(row["required_paths_present"])

    def test_text_under_the_wrong_path_earns_nothing(self):
        row = ro.score_case(BRIDGE, passages(("hubs/Home.md",
                                              "Lead: [[Ada Example]] on leave until 1 May")), FILES)
        self.assertEqual(row["gold_found"], 0)
        self.assertFalse(row["complete"])

    def test_right_path_without_the_string_earns_nothing(self):
        # A truncated passage from the right note does not prove the answer.
        row = ro.score_case(BRIDGE, passages(("projects/a.md", "Lead: [[Ada Exa"),
                                             ("people/Ada Example.md", "Role: planner")), FILES)
        self.assertEqual(row["gold_found"], 0)
        self.assertTrue(row["required_paths_present"])

    def test_whitespace_is_collapsed_but_case_is_not(self):
        wrapped = passages(("projects/a.md", "Lead:\n  [[Ada Example]]"),
                           ("people/Ada Example.md", "ON LEAVE UNTIL 1 MAY"))
        row = ro.score_case(BRIDGE, wrapped, FILES)
        self.assertEqual(row["gold_found"], 1)

    def test_split_across_two_passages_does_not_count(self):
        split = passages(("people/Ada Example.md", "on leave "), ("people/Ada Example.md", "until 1 May"))
        self.assertFalse(ro.item_found(BRIDGE["gold"][1], split))

    def test_sizes_and_whole_file_cost(self):
        row = ro.score_case(BRIDGE, passages(("projects/a.md", "abcde"),
                                             ("projects/a.md", "fgh"),
                                             ("people/Ada Example.md", "i")), FILES)
        self.assertEqual(row["passages"], 3)
        self.assertEqual(row["distinct_files"], 2)
        self.assertEqual(row["packet_chars"], 9)
        self.assertEqual(row["est_tokens"], 3)                    # ceil(9 / 4)
        self.assertEqual(row["whole_file_tokens"], 100 + 26)      # ceil(400/4) + ceil(101/4)

    def test_unknown_paths_are_reported_not_costed(self):
        row = ro.score_case(BRIDGE, passages(("nowhere.md", "abcd")), FILES)
        self.assertEqual(row["unknown_paths"], ["nowhere.md"])
        self.assertEqual(row["whole_file_tokens"], 0)

    def test_supersession_misled_only_when_stale_wins(self):
        stale = ro.score_case(SUPERSEDED, passages(("decisions/old.md", "The width is 1.8 m.")), FILES)
        self.assertTrue(stale["misled"])
        self.assertEqual(stale["distractor_hits"], 1)
        both = ro.score_case(SUPERSEDED, passages(("decisions/old.md", "The width is 1.8 m."),
                                                  ("decisions/new.md", "The width is 2.2 m.")), FILES)
        self.assertTrue(both["complete"])
        self.assertFalse(both["misled"])
        self.assertEqual(both["distractor_hits"], 1)

    def test_unanswerable(self):
        empty = ro.score_case(UNANSWERABLE, [], FILES)
        self.assertIsNone(empty["complete"])
        self.assertIsNone(empty["gold_recall"])
        self.assertEqual(empty["false_evidence_on_unanswerable"], 0)
        some = ro.score_case(UNANSWERABLE, passages(("projects/a.md", "x"), ("decisions/old.md", "y")), FILES)
        self.assertEqual(some["false_evidence_on_unanswerable"], 2)


class Verbatim(unittest.TestCase):
    """A passage earns credit only if it is a span of the file it names (E-11)."""
    TEXTS = {"projects/a.md": "# A\n\nStatus: active\nLead: [[Ada Example]]\n",
             "people/Ada Example.md": "# Ada Example\n\nShe is on leave until 1 May.\n"}

    def test_fabricated_passage_under_the_gold_path_earns_nothing(self):
        row = ro.score_case(BRIDGE, passages(
            ("projects/a.md", "INVENTED TEXT NOT IN THE FILE Lead: [[Ada Example]]"),
            ("people/Ada Example.md", "made up: on leave until 1 May")), FILES, self.TEXTS)
        self.assertEqual(row["gold_found"], 0)
        self.assertFalse(row["complete"])
        self.assertEqual(row["non_verbatim"], 2)
        self.assertFalse(row["required_paths_present"])
        self.assertEqual(row["packet_chars"], len("INVENTED TEXT NOT IN THE FILE Lead: [[Ada Example]]")
                         + len("made up: on leave until 1 May"))

    def test_verbatim_spans_and_rewrapped_spans_are_credited(self):
        row = ro.score_case(BRIDGE, passages(
            ("projects/a.md", "Status: active\nLead: [[Ada Example]]"),
            ("people/Ada Example.md", "She is on leave\n  until 1 May.")), FILES, self.TEXTS)
        self.assertTrue(row["complete"])
        self.assertEqual(row["non_verbatim"], 0)

    def test_a_path_outside_the_vault_is_not_verbatim(self):
        row = ro.score_case(BRIDGE, passages(("elsewhere.md", "Lead: [[Ada Example]]")), FILES, self.TEXTS)
        self.assertEqual(row["non_verbatim"], 1)

    def test_without_file_texts_nothing_is_checked(self):
        row = ro.score_case(BRIDGE, passages(("projects/a.md", "anything")), FILES)
        self.assertIsNone(row["non_verbatim"])

    def test_aggregate_counts_non_verbatim_passages(self):
        rows = [ro.score_case(BRIDGE, passages(("projects/a.md", "not in it")), FILES, self.TEXTS)]
        self.assertEqual(ro.aggregate(rows)["non_verbatim_passages"], 1)


class Trec(unittest.TestCase):
    """qrels and run files parse as TREC (E-03)."""

    def test_qrels_lines(self):
        text = ro.qrels_text([BRIDGE, SUPERSEDED, UNANSWERABLE])
        lines = [line.split() for line in text.splitlines()]
        self.assertTrue(all(len(parts) == 4 for parts in lines), lines)
        self.assertIn(["T1", "0", "people/Ada%20Example.md", "1"], lines)
        self.assertIn(["T2", "0", "decisions/old.md", "0"], lines)
        self.assertIn(["T2", "0", "decisions/new.md", "1"], lines)
        self.assertFalse([parts for parts in lines if parts[0] == "T3"])

    def test_run_lines(self):
        row = ro.score_case(BRIDGE, passages(("people/Ada Example.md", "x"), ("projects/a.md", "y"),
                                             ("people/Ada Example.md", "z")), FILES)
        lines = [line.split() for line in ro.run_text([row], "synaptic-extra").splitlines()]
        self.assertEqual(lines, [["T1", "Q0", "people/Ada%20Example.md", "1", "2", "synaptic-extra"],
                                 ["T1", "Q0", "projects/a.md", "2", "1", "synaptic-extra"]])
        for parts in lines:
            int(parts[3])
            float(parts[4])


class Parsing(unittest.TestCase):
    def test_passages_shape_and_extra_fields_ignored(self):
        got = ro.extract_passages({"passages": [
            {"path": "./projects/a.md", "text": "Lead: [[Ada Example]]", "role": "seed", "score": 9},
            {"path": "people/Ada Example.md"},                         # no text: dropped
            "not a dict"]})
        self.assertEqual(got, [{"path": "projects/a.md", "text": "Lead: [[Ada Example]]"}])

    def test_absolute_paths_under_the_vault_are_relativised(self):
        vault = Path("/tmp/example-vault")
        got = ro.extract_passages({"evidence": [{"source_path": str(vault / "projects/a.md"),
                                                 "content": "x"}]}, vault)
        self.assertEqual(got[0]["path"], "projects/a.md")

    def test_parse_stdout_tolerates_log_lines(self):
        body = json.dumps(packet(("projects/a.md", "x")))
        self.assertEqual(ro.parse_stdout("+ some log line\n" + body)["status"], "PARTIAL")
        self.assertIsNone(ro.parse_stdout(""))
        self.assertIsNone(ro.parse_stdout("usage: error"))

    def test_error_packet_has_no_passages(self):
        self.assertEqual(ro.extract_passages({"operation_status": "error", "evidence": []}), [])
        self.assertEqual(ro.extract_passages(None), [])


class Statistics(unittest.TestCase):
    def test_wilson(self):
        low, high = ro.wilson(8, 10)
        self.assertAlmostEqual(low, 0.4902, places=3)
        self.assertAlmostEqual(high, 0.9433, places=3)
        self.assertEqual(ro.wilson(0, 0), (0.0, 0.0))

    def test_sign_test(self):
        self.assertEqual(ro.sign_test(0, 0), 1.0)
        self.assertAlmostEqual(ro.sign_test(6, 0), 0.03125)
        self.assertAlmostEqual(ro.sign_test(5, 5), 1.0)

    def test_aggregate_and_paired(self):
        full = passages(("projects/a.md", "Lead: [[Ada Example]]"),
                        ("people/Ada Example.md", "on leave until 1 May"))
        a = [ro.score_case(BRIDGE, full, FILES), ro.score_case(UNANSWERABLE, [], FILES)]
        b = [ro.score_case(BRIDGE, [], FILES), ro.score_case(UNANSWERABLE, full, FILES)]
        agg = ro.aggregate(a)
        self.assertEqual((agg["answerable"], agg["complete"], agg["unanswerable"]), (1, 1, 1))
        self.assertEqual(ro.aggregate(b)["unanswerable_with_passages"], 1)
        p = ro.paired(a, b)
        self.assertEqual((p["a_only"], p["b_only"]), (1, 0))

    def test_errors_stay_in_the_denominator(self):
        # An erroring search is an incomplete case, never a case that disappears: the
        # headline rate keeps it, and the error-free rate is reported beside it.
        row = ro.score_case(BRIDGE, [], FILES)
        row["error"] = "exit 1"
        agg = ro.aggregate([row])
        self.assertEqual((agg["errors"], agg["answerable"], agg["complete"]), (1, 1, 0))
        self.assertEqual(agg["complete_rate"], 0.0)
        self.assertEqual(agg["answerable_without_errors"], 0)
        self.assertIsNone(agg["complete_rate_without_errors"])
        self.assertEqual(agg["error_ids"], ["T1"])

    def test_an_error_cannot_raise_a_method_above_one_that_ran(self):
        full = passages(("projects/a.md", "Lead: [[Ada Example]]"),
                        ("people/Ada Example.md", "on leave until 1 May"))
        other = dict(BRIDGE, id="T4")
        a = [ro.score_case(BRIDGE, full, FILES), ro.score_case(other, [], FILES)]
        b = [ro.score_case(BRIDGE, full, FILES), ro.score_case(other, [], FILES)]
        b[1]["error"] = "exit 1"
        self.assertEqual(ro.aggregate(a)["complete_rate"], ro.aggregate(b)["complete_rate"])
        # A complete case the other method errored on is a win for the method that ran.
        c = [ro.score_case(BRIDGE, [], FILES), ro.score_case(other, [], FILES)]
        c[0]["error"] = "exit 1"
        p = ro.paired(a, c)
        self.assertEqual((p["a_only"], p["b_only"]), (1, 0))
        self.assertEqual(ro.paired(c, a)["b_only"], 1)


class SealedCases(unittest.TestCase):
    def test_every_gold_string_is_verbatim_in_its_note(self):
        self.assertEqual(seal.validate(seal.load_cases()), [])

    def test_every_real_gold_item_scores_when_its_note_is_returned_whole(self):
        # Oracle packet: the full text of every gold note. Every answerable case must be complete,
        # which checks the scorer and the case file against each other.
        for case in seal.load_cases():
            items = [(g["path"], (seal.VAULT / g["path"]).read_text(encoding="utf-8"))
                     for g in case["gold"]]
            row = ro.score_case(case, passages(*items), {})
            if case["gold"]:
                self.assertTrue(row["complete"], case["id"])


if __name__ == "__main__":
    unittest.main()
