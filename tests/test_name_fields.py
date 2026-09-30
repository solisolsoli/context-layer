"""The opt-in name, alias and heading fields (`index --name-fields`, `search --name-fields`).

Default behaviour is pinned elsewhere (tests/test_incremental_index.py: golden digests of the
default packets); this file covers what the option adds, and that it stays off unless asked.
"""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO = Path(os.environ.get("TEST_REPO_HOME", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO / "router"))
sys.path.insert(0, str(REPO / "eval"))
sys.path.insert(0, str(REPO / "tests"))
import build_index  # noqa: E402
import retrieve  # noqa: E402
from fixtures import dev_names  # noqa: E402


def build(vault, *flags):
    out = io.StringIO()
    with redirect_stdout(out):
        code = build_index.main(["--vault", str(vault), *flags])
    assert code == 0
    return out.getvalue()


def search(vault, question, *flags, method="fts"):
    out = io.StringIO()
    with redirect_stdout(out):
        code = retrieve.main(["--method", method, "--vault", str(vault), *flags, question])
    return code, json.loads(out.getvalue())


def sources(packet):
    return [item["source_path"] for item in packet["evidence"]]


class FieldParsing(unittest.TestCase):
    def test_aliases_in_the_three_yaml_spellings(self):
        self.assertEqual(build_index.frontmatter_aliases(
            "---\naliases: [Dockhands, \"Buoy Painters\", 'Rope Crew']\n---\nbody"),
            ["Dockhands", "Buoy Painters", "Rope Crew"])
        self.assertEqual(build_index.frontmatter_aliases("---\nalias: Solo\n---\n"), ["Solo"])
        self.assertEqual(build_index.frontmatter_aliases(
            "---\ntitle: T\naliases:\n  - A one\n  - \"B two\"\ntags: [x]\n---\n"),
            ["A one", "B two"])

    def test_no_aliases_without_a_closed_leading_block(self):
        self.assertEqual(build_index.frontmatter_aliases("aliases: [x]\n"), [])
        self.assertEqual(build_index.frontmatter_aliases("---\naliases: [x]\nno close\n"), [])
        self.assertEqual(build_index.frontmatter_aliases("text\n---\naliases: [x]\n---\n"), [])

    def test_headings_skip_fenced_code_and_are_bounded(self):
        text = "# Real\n\n```sh\n# a shell comment\n```\n\n## Second one\n"
        source = build_index.Source("dir/Note Name.md", ".md", "s", 1, "t", text)
        self.assertEqual(build_index.name_row(source),
                         ("dir/Note Name.md", "Note Name", "", "Real\nSecond one"))
        many = "\n".join(f"# h{n} " + "w" * 90 for n in range(500))
        row = build_index.name_row(build_index.Source("x.md", ".md", "s", 1, "t", many))
        self.assertLessEqual(len(row[3]), build_index.MAX_HEADING_CHARS)
        self.assertLessEqual(row[3].count("\n") + 1, build_index.MAX_HEADINGS)

    def test_a_non_markdown_file_has_only_a_name(self):
        source = build_index.Source("data.csv", ".csv", "s", 1, "t", "# not a heading\n")
        self.assertEqual(build_index.name_row(source), ("data.csv", "data", "", ""))


class NameFieldSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.vault = Path(cls.temp.name) / "v"
        cls.cases = dev_names.build(cls.vault)
        (cls.vault / ".context").mkdir()
        (cls.vault / ".context" / "routes.json").write_text(json.dumps(
            {"routes": {}, "record_type_allowlist": ["verbatim_text_file"],
             "exclude_prefixes": ["private"]}))
        (cls.vault / "private").mkdir()
        (cls.vault / "private" / "Vault Combination.md").write_text("Seven left, two right.\n")
        cls.plain = Path(cls.temp.name) / "plain"
        shutil.copytree(cls.vault, cls.plain)
        build(cls.plain)
        build(cls.vault, "--name-fields")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_the_default_index_has_no_name_table(self):
        connection = sqlite3.connect(self.plain / ".context" / "index.sqlite")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        keys = {row[0] for row in connection.execute("SELECT key FROM index_meta")}
        connection.close()
        self.assertNotIn("names_fts", tables)
        self.assertNotIn("name_fields", keys)

    def test_a_note_named_by_the_question_is_found_only_with_the_option(self):
        case = next(c for c in self.cases if c["id"] == "N01")
        wanted = case["required"][0]["source_path"]
        self.assertNotIn(wanted, sources(search(self.vault, case["question"])[1]))
        self.assertIn(wanted, sources(search(self.vault, case["question"], "--name-fields")[1]))

    def test_every_dev_name_question_is_answered_with_the_option(self):
        misses = []
        for case in self.cases:
            if case["type"] != "name":
                continue
            _, packet = search(self.vault, case["question"], "--name-fields")
            if case["required"][0]["source_path"] not in sources(packet):
                misses.append(case["id"])
        self.assertEqual(misses, [])

    def test_cases_the_default_search_answers_are_still_answered(self):
        for case in self.cases:
            if case["type"] not in ("alias", "heading", "both"):
                continue
            wanted = case["required"][0]
            for flags in ((), ("--name-fields",)):
                _, packet = search(self.vault, case["question"], *flags)
                self.assertTrue(any(e["source_path"] == wanted["source_path"]
                                    and wanted["text"] in e["content"]
                                    for e in packet["evidence"]), (case["id"], flags))

    def test_a_question_with_no_match_anywhere_stays_empty(self):
        for case in self.cases:
            if case["type"] == "absent":
                self.assertEqual(search(self.vault, case["question"], "--name-fields")[1]
                                 ["evidence"], [])

    def test_the_default_search_ignores_the_name_table(self):
        for case in self.cases[::5]:
            left = search(self.vault, case["question"])[1]
            right = search(self.plain, case["question"])[1]
            left.pop("coverage"), right.pop("coverage")
            self.assertEqual(left, right, case["id"])

    def test_an_excluded_note_is_never_returned_by_its_name(self):
        packet = search(self.vault, "Vault Combination", "--name-fields")[1]
        self.assertNotIn("private/Vault Combination.md", sources(packet))
        self.assertNotIn("Seven left", json.dumps(packet))

    def test_the_option_on_an_index_without_the_fields_is_an_error_that_names_the_fix(self):
        code, packet = search(self.plain, "Kestrel Yard", "--name-fields")
        self.assertEqual(code, 1)
        self.assertEqual(packet["operation_status"], "error")
        self.assertIn("index <vault> --name-fields", packet["error"])

    def test_the_option_is_refused_where_it_changes_nothing(self):
        for method in ("grep", "router"):
            done = subprocess.run([sys.executable, str(REPO / "eval" / "retrieve.py"),
                                   "--method", method, "--vault", str(self.vault),
                                   "--name-fields", "Kestrel Yard"], capture_output=True, text=True)
            self.assertEqual(done.returncode, 2)
            self.assertIn("--name-fields applies to", done.stderr)

    def test_synaptic_accepts_the_option(self):
        _, named = search(self.vault, "Tell me about Kestrel Yard", "--name-fields",
                          method="synaptic")
        self.assertIn("places/Kestrel Yard.md", sources(named))


if __name__ == "__main__":
    unittest.main()
