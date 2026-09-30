"""Offline tests for local selection and bounded GitHub evidence packets."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from context_layer import github_context as gh  # noqa: E402

COMMIT = "b" * 40
TEXT = "# Fictional project\nThe harbor keeper checks every lamp at dusk.\nA second line records the repair.\n"


def source(sid="project-docs", **changes):
    result = {"id": sid, "repo": "example/project", "commit": COMMIT,
              "paths": ["README.md"], "keywords": ["harbor", "project"]}
    result.update(changes)
    return result


class GitHubContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name)
        self.context = self.vault / ".context"
        self.context.mkdir()

    def configure(self, sources=None, **fields):
        config = {"version": 1, "enabled": True,
                  "sources": [source()] if sources is None else sources}
        config.update(fields)
        (self.context / "github.json").write_text(json.dumps(config), encoding="utf-8")

    def test_missing_and_disabled_config_are_off_without_network(self):
        with mock.patch.object(gh.github_client, "fetch_file", side_effect=AssertionError("network")):
            self.assertEqual(gh.fetch(self.vault, "harbor" )["status"], "OFF")
            self.configure(enabled=False)
            self.assertEqual(gh.fetch(self.vault, "harbor")["status"], "OFF")

    def test_keyword_match_fetches_verbatim_hash_pinned_evidence_without_sending_prompt(self):
        self.configure()
        prompt = "PRIVATE-PROMPT-MARKER harbor lamp"
        with mock.patch.object(gh.github_client, "fetch_file", return_value=TEXT.encode()) as fetch:
            result = gh.fetch(self.vault, prompt)
        self.assertEqual(result["status"], "FOUND")
        self.assertEqual(len(result["evidence"]), 1)
        item = result["evidence"][0]
        self.assertEqual(fetch.call_args.args, ("example/project", COMMIT, "README.md"))
        self.assertNotIn("PRIVATE-PROMPT-MARKER", repr(fetch.call_args))
        self.assertEqual(item["source_type"], "github")
        self.assertEqual(item["line_start"], 1)
        self.assertEqual(item["content"], TEXT)
        self.assertEqual(item["source_sha256"], hashlib.sha256(TEXT.encode()).hexdigest())
        self.assertEqual(item["content_sha256"], hashlib.sha256(item["content"].encode()).hexdigest())
        self.assertIn(f"/blob/{COMMIT}/README.md#L1-L3", item["source_url"])
        self.assertIn("untrusted data", result["notice"])

    def test_no_keyword_match_is_not_found_and_never_contacts_github(self):
        self.configure()
        with mock.patch.object(gh.github_client, "fetch_file", side_effect=AssertionError("network")):
            self.assertEqual(gh.fetch(self.vault, "PRIVATE-PROMPT-MARKER astronomy")["status"], "NOT_FOUND")

    def test_multiword_keywords_require_all_meaningful_terms_and_literal_lf_lines(self):
        self.configure(sources=[source(keywords=["project protocol"])])
        with mock.patch.object(gh.github_client, "fetch_file", side_effect=AssertionError("network")):
            self.assertEqual(gh.fetch(self.vault, "project astronomy")["status"], "NOT_FOUND")
        self.configure()
        data = "A heading\u2028the harbor beacon is bright.\nAnother line.\n".encode()
        with mock.patch.object(gh.github_client, "fetch_file", return_value=data):
            result = gh.fetch(self.vault, "harbor")
        item = result["evidence"][0]
        self.assertEqual((item["line_start"], item["line_end"]), (1, 2))
        self.assertEqual(item["content"], "A heading\u2028the harbor beacon is bright.\nAnother line.\n")

    def test_explicit_id_allows_prefix_fallback_and_unknown_ids_fail_before_network(self):
        self.configure()
        with mock.patch.object(gh.github_client, "fetch_file", return_value=b"# A fictional README\n") as fetch:
            result = gh.fetch(self.vault, "astronomy", ["project-docs"])
        self.assertEqual(result["status"], "FOUND")
        self.assertEqual(result["evidence"][0]["content"], "# A fictional README\n")
        self.assertEqual(fetch.call_count, 1)
        with mock.patch.object(gh.github_client, "fetch_file", side_effect=AssertionError("network")):
            bad = gh.fetch(self.vault, "astronomy", ["missing"])
        self.assertEqual((bad["status"], bad["errors"]), ("ERROR", ["unknown_source_id"]))

    def test_malformed_unknown_key_symlink_and_oversized_configs_fail_safely(self):
        for config in ({"version": 1, "enabled": True, "sources": [source()], "token": "x"},
                       {"version": 1, "enabled": True, "sources": [source(paths=["../README.md"]) ]},
                       {"version": 2, "enabled": True, "sources": []}):
            (self.context / "github.json").write_text(json.dumps(config), encoding="utf-8")
            self.assertEqual(gh.fetch(self.vault, "harbor")["errors"], ["invalid_config"])
        (self.context / "github.json").write_bytes(b" " * (gh.MAX_CONFIG_BYTES + 1))
        self.assertEqual(gh.fetch(self.vault, "harbor")["errors"], ["invalid_config"])
        (self.context / "github.json").unlink()
        (self.context / "github.json").symlink_to(self.vault / "missing")
        self.assertEqual(gh.fetch(self.vault, "harbor")["errors"], ["invalid_config"])

    def test_config_shape_caps_and_duplicate_json_members_are_enforced(self):
        self.configure(sources=[source(str(i)) for i in range(gh.MAX_SOURCES + 1)])
        self.assertEqual(gh.fetch(self.vault, "harbor")["errors"], ["invalid_config"])
        (self.context / "github.json").write_text(
            '{"version":1,"version":1,"enabled":true,"sources":[]}', encoding="utf-8")
        self.assertEqual(gh.fetch(self.vault, "harbor")["errors"], ["invalid_config"])
        self.configure()
        result = gh.fetch(self.vault, "x" * (gh.MAX_PROMPT_CHARS + 1))
        self.assertEqual(result["errors"], ["invalid_prompt"])
        self.assertEqual(gh.fetch(self.vault, "harbor", ["project-docs", "project-docs"])["errors"],
                         ["invalid_source_ids"])

    def test_fetch_failure_statuses_are_error_or_partial_and_codes_are_sanitized(self):
        self.configure()
        with mock.patch.object(gh.github_client, "fetch_file",
                               side_effect=gh.github_client.GitHubFetchError("network_error")):
            result = gh.fetch(self.vault, "harbor")
        self.assertEqual((result["status"], result["errors"]), ("ERROR", ["network_error"]))
        with mock.patch.object(gh.github_client, "fetch_file", side_effect=[TEXT.encode(),
                               gh.github_client.GitHubFetchError("http_status")]):
            self.configure(sources=[source(paths=["README.md", "docs/guide.md"])])
            result = gh.fetch(self.vault, "harbor")
        self.assertEqual((result["status"], result["errors"]), ("PARTIAL", ["http_status"]))

    def test_empty_content_and_prompt_term_miss_deliver_nothing(self):
        self.configure()
        for data in (b"", b"unrelated fictional text\n"):
            with mock.patch.object(gh.github_client, "fetch_file", return_value=data):
                result = gh.fetch(self.vault, "harbor")
            self.assertEqual((result["status"], result["evidence"]), ("NOT_FOUND", []))

    def test_total_file_byte_and_delivery_character_budgets_hold(self):
        long_lines = "harbor " + "x" * 1990 + "\n" + "harbor " + "y" * 1990 + "\n"
        self.configure(sources=[source(paths=[f"doc{i}.md" for i in range(8)])])
        with mock.patch.object(gh.github_client, "fetch_file", return_value=long_lines.encode()) as fetch:
            result = gh.fetch(self.vault, "harbor")
        self.assertLessEqual(fetch.call_count, gh.MAX_FILES)
        self.assertIn("file_limit_reached", result["errors"])
        self.assertEqual(result["status"], "PARTIAL")
        self.assertLessEqual(sum(len(item["content"].encode()) for item in result["evidence"]),
                             gh.MAX_DELIVERED_CHARS)
        self.assertTrue(all(len(item["content"]) <= gh.MAX_FILE_CHARS for item in result["evidence"]))
        too_much = b"harbor\n" + b"x" * gh.MAX_TOTAL_FILE_BYTES
        with mock.patch.object(gh.github_client, "fetch_file", return_value=too_much):
            result = gh.fetch(self.vault, "harbor")
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("total_bytes_exceeded", result["errors"])

    def test_symlinked_context_directory_is_rejected(self):
        self.context.rmdir()
        self.context.symlink_to(self.vault, target_is_directory=True)
        self.assertEqual(gh.fetch(self.vault, "harbor")["errors"], ["invalid_config"])

    def test_missing_or_final_symlink_vault_is_an_error(self):
        self.assertEqual(gh.fetch(self.vault / "missing", "harbor")["errors"], ["invalid_config"])
        target = self.vault / "real-vault"
        target.mkdir()
        alias = self.vault / "vault-link"
        alias.symlink_to(target, target_is_directory=True)
        self.assertEqual(gh.fetch(alias, "harbor")["errors"], ["invalid_config"])

    def test_more_than_two_keyword_selected_sources_is_reported_as_partial(self):
        rows = [source(f"docs-{i}", keywords=["harbor"]) for i in range(3)]
        self.configure(sources=rows)
        with mock.patch.object(gh.github_client, "fetch_file", return_value=b"harbor lights.\n") as fetch:
            result = gh.fetch(self.vault, "harbor")
        self.assertEqual(fetch.call_count, gh.MAX_SELECTED_SOURCES)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("source_limit_reached", result["errors"])

    def test_final_file_omitted_by_delivery_budget_is_visible_without_fetching_it(self):
        self.configure(sources=[source(paths=[f"docs/{i}.md" for i in range(4)])])
        data = ("harbor " + "x" * 1992 + "\n").encode()
        with mock.patch.object(gh.github_client, "fetch_file", return_value=data) as fetch:
            result = gh.fetch(self.vault, "harbor")
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("delivery_limit_reached", result["errors"])
        self.assertEqual(sum(len(row["content"]) for row in result["evidence"]), 6000)

    def test_whitespace_file_does_not_become_found_and_blank_prompt_is_rejected(self):
        self.configure()
        with mock.patch.object(gh.github_client, "fetch_file", return_value=b"\n \r\n") as fetch:
            result = gh.fetch(self.vault, "gap", ["project-docs"])
            self.assertEqual(result["status"], "NOT_FOUND")
            fetch.reset_mock()
            self.assertEqual(gh.fetch(self.vault, " ")["status"], "ERROR")
            fetch.assert_not_called()

    def test_extreme_json_numeric_literal_is_a_safe_config_error(self):
        (self.context / "github.json").write_text('{"version":' + "1" * 5000 + '}', encoding="utf-8")
        with mock.patch.object(gh.github_client, "fetch_file") as fetch:
            self.assertEqual(gh.fetch(self.vault, "harbor")["errors"], ["invalid_config"])
            fetch.assert_not_called()

    def test_word_fragment_is_not_a_passage_match(self):
        self.configure(sources=[source(keywords=["art"])])
        with mock.patch.object(gh.github_client, "fetch_file", return_value=b"A partial document.\n"):
            self.assertEqual(gh.fetch(self.vault, "art")["status"], "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
