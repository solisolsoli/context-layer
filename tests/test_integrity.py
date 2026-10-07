"""Black-box regressions; every test owns a disposable synthetic vault."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(os.environ.get("TEST_REPO_HOME", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO / "router"))
sys.path.insert(0, str(REPO / "eval"))
import build_index  # noqa: E402
import context_router  # noqa: E402
import retrieve  # noqa: E402
import source_policy  # noqa: E402
import textfold  # noqa: E402
from _portable_helpers import deny_path_access, sqlite_connection  # noqa: E402

ROUTES = {"routes": {}, "record_type_allowlist": ["verbatim_text_file"]}


def write(root, relative, data):
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    return path


def index_quietly(vault):
    """Build the index in-process; return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = build_index.main(["--vault", str(vault)])
    return code, out.getvalue(), err.getvalue()


def run_packet(vault, prompt, method="fts", *flags):
    """eval/retrieve.py in-process: (exit code, packet)."""
    out = io.StringIO()
    with redirect_stdout(out):
        code = retrieve.main(["--method", method, "--vault", str(vault), *flags, prompt])
    return code, json.loads(out.getvalue())


def paths(packet, origin=None):
    return [item["source_path"] for item in packet["evidence"]
            if origin is None or item.get("origin") == origin]


class IntegrityCLI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name)
        self.raw = "# Canonical\r\nalpha marker naïve café\r\n".encode()
        (self.vault / "canonical.md").write_bytes(self.raw)
        self.ctx = self.vault / ".context"
        self.ctx.mkdir()
        self.config = {"record_type_allowlist": ["verbatim_text_file"], "routes": {
            "canonical": {"priority": 10, "triggers": ["alpha"],
                          "canonical_sources": ["canonical.md"], "path_hints": []}},
            "fallback_routes": [], "aliases": {}}
        self.save_config()
        self.assertEqual(self.build().returncode, 0)

    def save_config(self):
        (self.ctx / "routes.json").write_text(json.dumps(self.config))

    def build(self):
        return subprocess.run([sys.executable, str(REPO / "router/build_index.py"),
                               "--vault", str(self.vault)], capture_output=True)

    def route(self, *flags, cli=False, save=False, prompt="alpha marker"):
        base = [sys.executable, str(REPO / "router/context_router.py"), "--vault", str(self.vault)]
        if cli:
            base = [sys.executable, "-m", "context_layer.cli", "route", str(self.vault)]
        return subprocess.run(base + ["--prompt", prompt] + ([] if save else ["--no-save"])
                              + list(flags), cwd=REPO, capture_output=True)

    def assert_error(self, result):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["operation_status"], "error")
        self.assertEqual(payload["evidence"], [])
        self.assertEqual(payload["status"], "ERROR")
        return payload

    def test_exact_crlf_and_utf8_and_index_version(self):
        result = self.route("--stdout", cli=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(self.raw, result.stdout)
        digest = hashlib.sha256(self.raw).hexdigest().encode()
        self.assertIn(b"Stored source SHA-256: `" + digest + b"`", result.stdout)
        self.assertIn(b"Current source SHA-256: `" + digest + b"`", result.stdout)
        self.assertIn(b"Source hash state: `verified`", result.stdout)
        self.assertFalse((self.vault / ".context-runs").exists())

    def test_saved_run_files_are_utf8_under_a_non_utf8_locale(self):
        """E-17: the run artifacts and --prompt-file use UTF-8, not the locale encoding."""
        prompt_file = self.vault / "prompt.txt"
        prompt_file.write_text("alpha marker \u2014 na\u00efve", encoding="utf-8")
        env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0")
        result = subprocess.run(
            [sys.executable, str(REPO / "router/context_router.py"), "--vault", str(self.vault),
             "--prompt-file", str(prompt_file), "--json"],
            cwd=REPO, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        packets = list((self.vault / ".context-runs").rglob("context.md"))
        self.assertEqual(len(packets), 1)
        self.assertIn("naïve café", packets[0].read_bytes().decode("utf-8"))
        request = json.loads((packets[0].parent / "request.json").read_bytes().decode("utf-8"))
        self.assertEqual(request["prompt"], "alpha marker \u2014 na\u00efve")

    def test_append_after_index_fails_closed(self):
        (self.vault / "canonical.md").write_bytes(self.raw + b"TAMPERED\n")
        self.assert_error(self.route("--json"))
        human = self.route("--stdout")
        self.assertEqual(human.returncode, 1)
        self.assertNotIn(b"TAMPERED", human.stdout)
        self.assertNotIn(b"## Verbatim evidence", human.stdout)

    def test_deleted_canonical_fails_closed(self):
        (self.vault / "canonical.md").unlink()
        self.assert_error(self.route("--json"))

    def test_new_unindexed_canonical_fails_closed(self):
        (self.vault / "new.md").write_bytes(b"alpha new\n")
        self.config["routes"]["canonical"]["canonical_sources"].append("new.md")
        self.save_config()
        self.assertIn("indexed version", self.assert_error(self.route("--json"))["error"])

    def test_broken_fts_all_entrypoints_and_output_modes(self):
        with sqlite_connection(self.ctx / "index.sqlite") as db:
            db.execute("DROP TABLE records_fts")
        for cli in [False, True]:
            for flags in [("--json",), ("--stdout",), ()]:
                with self.subTest(cli=cli, flags=flags):
                    result = self.route(*flags, cli=cli)
                    if flags == ("--json",):
                        self.assert_error(result)
                    else:
                        self.assertEqual(result.returncode, 1)
                        self.assertIn(b"operational error", result.stdout)
                        self.assertNotIn(b"## Verbatim evidence", result.stdout)

    def test_missing_and_corrupt_index(self):
        index = self.ctx / "index.sqlite"
        index.unlink()
        self.assert_error(self.route("--json"))
        index.write_bytes(b"not SQLite")
        self.assert_error(self.route("--json", cli=True))

    def test_vague_prompt_does_not_bypass_broken_fts(self):
        with sqlite_connection(self.ctx / "index.sqlite") as db:
            db.execute("DROP TABLE records_fts")
        self.assert_error(self.route("--json", prompt="it"))

    def test_tampered_index_chunk_fails_closed(self):
        self.config["routes"]["canonical"]["canonical_sources"] = []
        self.save_config()
        with sqlite_connection(self.ctx / "index.sqlite") as db:
            db.execute("UPDATE records SET content = 'alpha changed'")
        self.assert_error(self.route("--json"))

    def test_fact_card_cannot_bypass_missing_index_version(self):
        (self.vault / "card.md").write_text("alpha quote")
        (self.ctx / "facts.json").write_text(json.dumps({"cards": [{
            "id": "card", "path": "card.md", "quote": "alpha quote", "answer": "test",
            "terms": ["alpha", "marker"], "evidence_grade": "SUPPORTED"}]}))
        self.assertIn("fact_card", self.assert_error(self.route("--json"))["error"])

    def test_failed_rebuild_preserves_previous_index(self):
        # A build that fails after staging started (here: the staged index fails the
        # FTS5 integrity check) leaves the live index and manifest exactly as they were.
        sys.path.insert(0, str(REPO / "router"))
        import build_index
        index, manifest = self.ctx / "index.sqlite", self.ctx / "index-manifest.json"
        before, before_manifest = index.read_bytes(), manifest.read_bytes()
        (self.vault / "new.md").write_bytes(b"alpha new note\n")
        failure = build_index.index_format.IndexFormatError("staged index is damaged")
        with patch.object(build_index.index_format, "integrity_check", side_effect=failure), \
                redirect_stderr(io.StringIO()) as err:
            self.assertEqual(build_index.main(["--vault", str(self.vault)]), 1)
        self.assertIn("staged index is damaged", err.getvalue())
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual(manifest.read_bytes(), before_manifest)
        self.assertEqual(list(self.ctx.glob(".index-*")), [])

    def test_saved_packet_has_exact_source_and_ledger(self):
        result = self.route("--json", save=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        summary = json.loads(result.stdout)
        self.assertIn(self.raw, Path(summary["context_packet"]).read_bytes())
        records = [json.loads(s) for s in Path(summary["evidence_ledger"]).read_text().splitlines()]
        self.assertEqual(records[0]["source_sha256"], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(records[0]["source_hash_state"], "verified")

    def test_evidence_json_delivers_content(self):
        result = self.route("--evidence-json")
        self.assertEqual(result.returncode, 0)
        packet = json.loads(result.stdout)
        self.assertEqual(packet["schema"], "evidence-delivery-v1")
        self.assertEqual(packet["evidence"][0]["content"].encode(), self.raw)

    def test_zero_budget_does_not_claim_support_from_omitted_sources(self):
        self.config["routes"]["canonical"]["canonical_sources"] = []
        self.save_config()
        result = self.route("--evidence-json", "--max-context-chars", "0")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["evidence"], [])
        self.assertNotIn(payload["status"], ["SUPPORTED", "USER_STATED"])

    # Historical defects, pinned for every `search` method (the path the MCP
    # server, the hook and tasks use), not only the router's own entry point.

    def search(self, method, prompt="alpha marker"):
        return subprocess.run([sys.executable, "-m", "context_layer.cli", "search",
                               str(self.vault), "--method", method, "--prompt", prompt],
                              cwd=REPO, capture_output=True)

    def assert_search_fails_closed(self, result, forbidden=None):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["operation_status"], "error")
        self.assertEqual(payload["evidence"], [])
        self.assertNotIn(payload["status"], ["SUPPORTED", "USER_STATED", "OK"])
        self.assertTrue(payload.get("error"))
        if forbidden:
            self.assertNotIn(forbidden, result.stdout)

    def test_text_appended_after_indexing_is_never_emitted_by_any_search_method(self):
        healthy = json.loads(self.search("fts").stdout)
        self.assertEqual(healthy["evidence"][0]["content"].encode(), self.raw)
        (self.vault / "canonical.md").write_bytes(self.raw + b"APPENDED AFTER INDEX\n")
        for method in ["fts", "grep", "fts-canonical"]:
            with self.subTest(method=method):
                # Per-source drift: the source is withheld with its reason, never emitted.
                result = self.search(method)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["status"], "NOT_FOUND")
                self.assertEqual(payload["evidence"], [])
                self.assertEqual([w["source_path"] for w in payload["withheld"]],
                                 ["canonical.md"])
                self.assertNotIn(b"APPENDED AFTER INDEX", result.stdout)
        # The experimental router still withholds its whole packet.
        self.assert_search_fails_closed(self.search("router"), b"APPENDED AFTER INDEX")

    def test_destroyed_index_is_an_error_for_every_search_method(self):
        index = self.ctx / "index.sqlite"
        for damage in ("drop records_fts", "drop records", "delete", "garbage"):
            self.assertEqual(self.build().returncode, 0)
            self.assertEqual(self.search("fts").returncode, 0)   # healthy control
            if damage.startswith("drop"):
                with sqlite_connection(index) as db:
                    db.execute("DROP TABLE " + damage.split()[1])
            elif damage == "delete":
                index.unlink()
            else:
                index.write_bytes(b"not SQLite at all")
            for method in ["fts", "grep", "fts-canonical", "router"]:
                with self.subTest(damage=damage, method=method):
                    self.assert_search_fails_closed(self.search(method))


class FoldingContract(unittest.TestCase):
    """A-01: a verbatim word is found. Query terms reach FTS5 as written, so the index
    tokenizer folds both sides the same way (router/textfold.py)."""

    SAMPLE = ["ß", "ẞ", "ﬁ", "Ａ", "ｶ", "ㄱ", "ᾀ", "İ"]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / "vault"
        write(self.vault, ".context/routes.json", json.dumps(ROUTES))

    def test_fixed_sample_is_found_by_fts_synaptic_and_router(self):
        write(self.vault, "de.md", "# Traffic\n\nThe Old Straße by the harbor closes in May.\n")
        write(self.vault, "trip.md", "# Trip\n\nThe ferry to İzmir leaves at noon.\n")
        for number, letter in enumerate(self.SAMPLE):
            write(self.vault, f"letters/l{number}.md", f"# L{number}\n\nword qz{letter}qz here\n")
        self.assertEqual(index_quietly(self.vault)[0], 0)
        cases = [("Straße", "de.md"), ("İzmir", "trip.md"), ("İZMİR", "trip.md"),
                 ("izmir", "trip.md")]
        cases += [(f"qz{letter}qz", f"letters/l{n}.md") for n, letter in enumerate(self.SAMPLE)]
        for prompt, expected in cases:
            with self.subTest(prompt=prompt):
                code, fts = run_packet(self.vault, prompt)
                self.assertEqual(code, 0)
                self.assertIn(expected, paths(fts))
                code, synaptic = run_packet(self.vault, prompt, "synaptic")
                self.assertEqual(code, 0)
                self.assertIn(expected, paths(synaptic, origin="fts"))
                code, router = run_packet(self.vault, prompt, "router")
                self.assertEqual(code, 0, router)
                self.assertIn(expected, paths(router))

    def test_every_unicode_letter_round_trips_through_the_index(self):
        # Each letter L of this Python's Unicode database sits in a word qzLqz in a real
        # index. fts (and so the fts part of default synaptic, which is built from the
        # same ranked list) must find every word; the router's own query path a sample.
        letters = [chr(cp) for cp in range(0x110000) if not 0xD800 <= cp <= 0xDFFF
                   and textfold.unicodedata.category(chr(cp)).startswith("L")]
        per = 1000
        for start in range(0, len(letters), per):
            write(self.vault, f"l{start // per:04d}.md",
                  "\n".join(f"qz{c}qz" for c in letters[start:start + per]) + "\n")
        self.assertEqual(index_quietly(self.vault)[0], 0)
        connection = sqlite3.connect((self.vault / ".context/index.sqlite").as_uri() + "?mode=ro",
                                     uri=True)
        self.addCleanup(connection.close)
        missed = []
        for number, letter in enumerate(letters):
            expression = textfold.match_expression(retrieve.terms(f"qz{letter}qz"))
            found = {row[0] for row in connection.execute(retrieve.RANKED_SQL, (expression,))}
            if f"l{number // per:04d}.md" not in found:
                missed.append(f"U+{ord(letter):04X}")
        self.assertEqual(missed, [], f"{len(missed)} of {len(letters)} letters not found")
        sample = sorted(set(range(0, len(letters), 23))
                        | {letters.index(c) for c in self.SAMPLE})
        router_missed = []
        for number in sample:
            parsed = context_router.parse_prompt(f"qz{letters[number]}qz", ROUTES)
            ranked, _, errors, _ = context_router.retrieve(self.vault, connection, ROUTES, parsed)
            self.assertEqual(errors, [])
            if f"l{number // per:04d}.md" not in {c.source_path for c in ranked}:
                router_missed.append(f"U+{ord(letters[number]):04X}")
        self.assertGreater(len(sample), 5000)
        self.assertEqual(router_missed, [])

    def test_folded_forms_never_reach_match(self):
        self.assertEqual(retrieve.terms("Old Straße"), ["Old", "Straße"])
        parsed = context_router.parse_prompt("Straße", ROUTES)
        self.assertEqual(parsed["tokens"], ["strasse"])            # comparison form
        self.assertEqual(parsed["match_tokens"], ["Straße"])       # what MATCH gets
        expressions = [e for _, e, _ in context_router.query_variants(parsed)]
        self.assertTrue(expressions)
        self.assertTrue(all("strasse" not in e and "Straße" in e for e in expressions))
        write(self.vault, "de.md", "The Old Straße by the harbor\n")
        index_quietly(self.vault)
        _, packet = run_packet(self.vault, "Straße")
        self.assertEqual(packet["coverage"]["query_terms"], ["Straße"])
        self.assertEqual(packet["coverage"]["match_expression"], '"Straße"')

    def test_term_boundaries_follow_the_index_tokenizer(self):
        # FTS5 keeps these characters inside a token; a Python \\w split would cut the
        # word and miss it. Hyphen and apostrophe join (FTS5 reads a phrase).
        for text, expected in [("price 100\u20bf today", ["price", "100\u20bf", "today"]),
                               ("robot\U0001F916 here", ["robot\U0001F916", "here"]),
                               ("\u2068Name\u2069 isolate", ["\u2068Name\u2069", "isolate"]),
                               ("x\ue000y", ["x\ue000y"]),
                               ("cafe\u0301 au lait", ["cafe\u0301", "au", "lait"]),
                               ("e-mail don't --x", ["e-mail", "don't", "x"]),
                               ("2026/09/28 x_y", ["2026", "09", "28", "x", "y"])]:
            with self.subTest(text=text):
                self.assertEqual(textfold.words(text), expected)
        self.assertEqual(textfold.terms("İzmir İZMİR izmir"), ["İzmir"])
        self.assertEqual(retrieve.terms("What does it do?"), [])
        write(self.vault, "a.md", "price 100\u20bf today, robot\U0001F916 here\n")
        index_quietly(self.vault)
        for prompt in ("100\u20bf", "robot\U0001F916"):
            self.assertEqual(paths(run_packet(self.vault, prompt)[1]), ["a.md"])

    def test_ascii_shortcut_agrees_with_the_tokenizer(self):
        # is_term_char() answers ASCII without asking SQLite; ask it anyway.
        saved = dict(textfold._token_char)
        self.addCleanup(lambda: (textfold._token_char.clear(),
                                 textfold._token_char.update(saved)))
        ascii_chars = {chr(c) for c in range(1, 128)}
        textfold._classify(ascii_chars)
        for c in sorted(ascii_chars):
            with self.subTest(char=repr(c)):
                self.assertEqual(textfold._token_char[c], textfold.is_term_char(c))

    def test_one_fold_for_retrieve_router_and_scanner(self):
        from context_layer import vault_scan
        tricky = ["Straße STRASSE", "İzmir", "ﬁle Ｆｕｌｌ", "cafe\u0301", "e-mail don't",
                  "Λόγος ΛΌΓΟΣ", "100\u20bf", "a  b\tc"]
        for text in tricky:
            with self.subTest(text=text):
                self.assertEqual(vault_scan.normalize(text), textfold.fold(text))
                self.assertEqual(context_router.normalize(text),
                                 " ".join(textfold.fold(text).split()))
                router_terms = context_router.match_tokens(text)
                self.assertEqual(router_terms,
                                 [t for t in retrieve.terms(text)
                                  if len(textfold.fold(t)) >= 2
                                  and not textfold.fold(t).isdecimal()
                                  and textfold.fold(t) not in context_router.STOPWORDS])
                self.assertEqual(context_router.tokens(text),
                                 [textfold.fold(t) for t in router_terms])

    def test_headings_skip_fenced_code(self):
        text = ("# One\n```sh\n# not a heading\n```\n## Two ##\n   ### Three\n#nope\n"
                "~~~\n# no\n~~~\n#### Four\r\n")
        self.assertEqual([(level, title) for _, level, title in textfold.headings(text)],
                         [(1, "One"), (2, "Two"), (3, "Three"), (4, "Four")])
        self.assertEqual(text[textfold.headings(text)[1][0]:].split("\n")[0], "## Two ##")


class RelevanceFloor(unittest.TestCase):
    """--relevance-floor R (opt-in): weak top-k notes are left out and listed; off by default."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / "vault"
        write(self.vault, ".context/routes.json", json.dumps(ROUTES))
        write(self.vault, "notes/strong.md", "# Kestrel harbor\n\nkestrel harbor lantern "
                                             "kestrel harbor lantern schedule\n")
        write(self.vault, "notes/weak.md", "# Other\n\n" + "filler words here. " * 40
              + "a lantern once.\n")
        code, _, err = index_quietly(self.vault)
        self.assertEqual(code, 0, err)

    def test_off_by_default_and_the_floor_leaves_out_weak_notes(self):
        _, plain = run_packet(self.vault, "kestrel harbor lantern")
        self.assertEqual(paths(plain), ["notes/strong.md", "notes/weak.md"])
        self.assertNotIn("relevance_floor", plain)
        _, zero = run_packet(self.vault, "kestrel harbor lantern", "fts", "--relevance-floor", "0")
        self.assertEqual(zero, plain)
        code, floored = run_packet(self.vault, "kestrel harbor lantern", "fts",
                                   "--relevance-floor", "0.5")
        self.assertEqual(code, 0)
        self.assertEqual(paths(floored), ["notes/strong.md"])
        self.assertEqual(floored["relevance_floor"],
                         {"ratio": 0.5, "below_floor": ["notes/weak.md"]})
        self.assertEqual(floored["evidence"][0], plain["evidence"][0])

    def test_synaptic_keeps_the_floored_fts_packet_whole(self):
        _, fts = run_packet(self.vault, "kestrel harbor lantern", "fts", "--relevance-floor", "0.5")
        _, synaptic = run_packet(self.vault, "kestrel harbor lantern", "synaptic",
                                 "--relevance-floor", "0.5")
        for item in fts["evidence"]:
            self.assertTrue(any(other["source_path"] == item["source_path"]
                                and other["content"] == item["content"]
                                for other in synaptic["evidence"]), item["source_path"])
        self.assertEqual(synaptic["relevance_floor"], fts["relevance_floor"])

    def test_out_of_range_and_inapplicable_values_are_refused(self):
        code, packet = run_packet(self.vault, "kestrel", "fts", "--relevance-floor", "1")
        self.assertEqual((code, packet["status"]), (1, "ERROR"))
        for method, extra in (("grep", []), ("synaptic", ["--compact"])):
            with self.subTest(method=method), redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                run_packet(self.vault, "kestrel", method, *extra, "--relevance-floor", "0.3")
            self.assertEqual(raised.exception.code, 2)


class DeliveredEvidence(unittest.TestCase):
    """Packet integrity: coverage receipt, per-source withholding, duplicates, flags."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / "vault"
        self.ctx = self.vault / ".context"
        write(self.vault, ".context/routes.json", json.dumps(ROUTES))

    def build(self):
        code, out, err = index_quietly(self.vault)
        self.assertEqual(code, 0, err)
        return out

    def test_coverage_receipt_names_what_was_searched_and_never_a_path(self):
        write(self.vault, "notes/ferry.md", "# Ferry\n\nferry timetable\n")
        write(self.vault, "notes/bad.md", b"ferry \xff not utf-8\n")
        write(self.vault, "private/secret.md", "# S\n\nferry SECRET-SENTINEL\n")
        write(self.vault, ".context/routes.json",
              json.dumps({**ROUTES, "exclude_prefixes": ["private/"], "max_file_bytes": 100}))
        write(self.vault, "notes/big.md", "ferry " * 40)
        self.build()
        for method in ("fts", "synaptic", "grep", "fts-canonical"):
            with self.subTest(method=method):
                code, packet = run_packet(self.vault, "ferry timetable", method)
                self.assertEqual(code, 0, packet)
                coverage = packet["coverage"]
                self.assertEqual(sorted(coverage), ["index_sha256", "indexed_notes",
                                                    "match_expression", "query_terms",
                                                    "skipped_by_reason"])
                self.assertEqual(coverage["indexed_notes"], 1)
                self.assertEqual(coverage["index_sha256"], hashlib.sha256(
                    (self.ctx / "index.sqlite").read_bytes()).hexdigest())
                self.assertEqual(coverage["query_terms"], ["ferry", "timetable"])
                self.assertEqual(coverage["match_expression"],
                                 None if method == "grep" else '"ferry" OR "timetable"')
                self.assertEqual(coverage["skipped_by_reason"], {
                    "oversize": 1, "unreadable": 0, "unsupported_name": 0, "not_utf8": 1})
                text = json.dumps(coverage)
                for leaked in ("private", "secret", "big.md", "bad.md"):
                    self.assertNotIn(leaked, text)

    def test_coverage_receipt_stays_bounded(self):
        write(self.vault, "a.md", "alpha\n")
        self.build()
        prompt = " ".join(f"term{n:04d}" for n in range(600))
        _, packet = run_packet(self.vault, prompt)
        coverage = packet["coverage"]
        self.assertEqual(len(coverage["query_terms"]), retrieve.COVERAGE_TERMS)
        self.assertEqual(coverage["query_terms_omitted"], 600 - retrieve.COVERAGE_TERMS)
        self.assertEqual(len(coverage["match_expression"]), retrieve.COVERAGE_EXPRESSION_CHARS)
        self.assertTrue(coverage["match_expression_truncated"])

    def test_nothing_to_search_is_not_nothing_found(self):
        write(self.vault, "a.md", "# A\n\nThe harbor opens at dawn.\n")
        self.build()
        for method in ("fts", "synaptic", "grep"):
            with self.subTest(method=method):
                _, empty = run_packet(self.vault, "What does it do?", method)
                self.assertEqual((empty["status"], empty["reason"]),
                                 ("NOT_FOUND", "no searchable terms"))
                self.assertEqual(empty["coverage"]["query_terms"], [])
                self.assertIsNone(empty["coverage"]["match_expression"])
                _, missing = run_packet(self.vault, "zzqqxy", method)
                self.assertEqual((missing["status"], missing["reason"]),
                                 ("NOT_FOUND", "no indexed note matched"))
                self.assertEqual(missing["coverage"]["query_terms"], ["zzqqxy"])

    def test_symlink_since_indexing_is_withheld_on_its_own(self):
        write(self.vault, "notes/ferry.md", "# Ferry\n\nferry timetable\n")
        pier = write(self.vault, "notes/pier.md", "# Pier\n\npier schedule\n")
        self.build()
        real = write(self.vault, "pier-real.md", pier.read_bytes())
        pier.unlink()
        try:
            pier.symlink_to(real)
        except OSError:
            self.skipTest("symlinks unavailable")
        for method in ("fts", "synaptic"):
            with self.subTest(method=method):
                code, unrelated = run_packet(self.vault, "ferry timetable", method)
                self.assertEqual((code, unrelated["status"]), (0, "PARTIAL"))
                self.assertEqual(paths(unrelated)[:1], ["notes/ferry.md"])
                code, hit = run_packet(self.vault, "pier schedule", method)
                self.assertEqual((code, hit["status"]), (0, "NOT_FOUND"))
                self.assertEqual(hit["withheld"], [{
                    "source_path": "notes/pier.md", "reason": "symlink since indexing",
                    "next": "context-layer index <vault>"}])
                self.assertEqual(hit["reason"], "matching notes could not be delivered")
                self.assertNotIn("pier schedule", json.dumps(hit["evidence"]))

    def test_note_unreadable_since_indexing_is_withheld_on_its_own(self):
        note = write(self.vault, "notes/locked.md", "# Locked\n\nlantern wick\n")
        write(self.vault, "notes/open.md", "# Open\n\nlantern oil\n")
        self.build()
        deny_path_access(self, note)
        code, packet = run_packet(self.vault, "lantern")
        self.assertEqual((code, packet["status"]), (0, "PARTIAL"))
        self.assertEqual(paths(packet), ["notes/open.md"])
        self.assertEqual([(w["source_path"], w["reason"]) for w in packet["withheld"]],
                         [("notes/locked.md", "unreadable since indexing")])

    def test_identical_notes_are_delivered_once(self):
        body = "# Lease\n\nThe walnut clause of the lease ends in May.\n"
        write(self.vault, "copy-a.md", body)
        write(self.vault, "copy-b.md", body)
        write(self.vault, "other.md", "# Other\n\nA walnut table.\n")
        self.build()
        for method in ("fts", "synaptic"):
            with self.subTest(method=method):
                _, packet = run_packet(self.vault, "walnut clause lease", method)
                fts_items = [i for i in packet["evidence"] if i.get("origin", "fts") == "fts"]
                self.assertEqual([i["source_path"] for i in fts_items], ["copy-a.md", "other.md"])
                self.assertEqual(fts_items[0]["duplicates"], [{
                    "source_path": "copy-b.md",
                    "source_sha256": hashlib.sha256(body.encode()).hexdigest()}])
                self.assertNotIn("duplicates", fts_items[1])

    def test_items_say_whether_they_were_cut_and_hold_a_match(self):
        filler = "".join(f"Paragraph {n} about harbor fees and nothing else.\n\n"
                         for n in range(80))
        write(self.vault, "long.md", "# Long\n\n" + filler + "The quokka permit expires in May.\n")
        write(self.vault, "short.md", "# Short\n\nA quokka sighting.\n")
        self.build()
        # Default delivery (A-06): a note longer than --per-source is delivered as the
        # verbatim window that holds the match, with its byte and line span; a note that
        # fits is delivered whole.
        _, packet = run_packet(self.vault, "quokka permit")
        items = {item["source_path"]: item for item in packet["evidence"]}
        long_item, short_item = items["long.md"], items["short.md"]
        self.assertEqual((long_item["truncated"], long_item["match_in_content"]), (True, True))
        self.assertIn("The quokka permit expires in May.", long_item["content"])
        raw = (self.vault / "long.md").read_bytes()
        self.assertEqual(raw[long_item["start"]:long_item["end"]].decode("utf-8"),
                         long_item["content"])
        self.assertEqual(long_item["line_start"], raw.count(b"\n", 0, long_item["start"]) + 1)
        self.assertEqual(long_item["line_end"], raw.count(b"\n", 0, long_item["end"]) + 1)
        self.assertEqual((long_item["source_chars"], len(packet["evidence"])),
                         (len(raw.decode("utf-8")), 2))
        self.assertEqual((short_item["truncated"], short_item["match_in_content"]), (False, True))
        self.assertEqual((short_item["content"], short_item["start"], short_item["line_start"]),
                         ((self.vault / "short.md").read_text(encoding="utf-8"), 0, 1))
        # --delivery prefix: the 0.3 behaviour, the beginning of the note, match or not.
        _, prefix = run_packet(self.vault, "quokka permit", "fts", "--delivery", "prefix")
        items = {item["source_path"]: item for item in prefix["evidence"]}
        self.assertEqual((items["long.md"]["truncated"], items["long.md"]["match_in_content"],
                          items["long.md"]["start"], len(items["long.md"]["content"])),
                         (True, False, 0, 2000))
        self.assertEqual(items["long.md"]["content"], raw.decode("utf-8")[:2000])
        self.assertEqual((items["short.md"]["truncated"], items["short.md"]["match_in_content"]),
                         (False, True))
        # grep, the baseline, always delivers prefixes and refuses --delivery (usage, 2).
        _, grep = run_packet(self.vault, "quokka permit", "grep")
        self.assertEqual({i["source_path"]: i["match_in_content"] for i in grep["evidence"]},
                         {"long.md": False, "short.md": True})
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as refused:
                run_packet(self.vault, "quokka permit", "grep", "--delivery", "window")
        self.assertEqual(refused.exception.code, 2)

    def test_windows_take_the_best_blocks_within_the_per_source_limit(self):
        # Two matching paragraphs far apart: both delivered as separate windows, most
        # distinct query terms first, never more than --per-source characters in all,
        # each a verbatim span; blank-line neighbours merge into one window.
        filler = "".join(f"Paragraph {n} about harbor fees and nothing else.\n\n"
                         for n in range(40))
        text = ("# Long\n\n" + filler + "The quokka permit expires in May.\n\n"
                + filler + "Quokka counts are taken in June.\n\nThe permit office closes early.\n")
        write(self.vault, "long.md", text)
        self.build()
        _, packet = run_packet(self.vault, "quokka permit", "fts", "--per-source", "120")
        items = [i for i in packet["evidence"] if i["source_path"] == "long.md"]
        contents = [i["content"] for i in items]
        self.assertEqual(contents[0], "The quokka permit expires in May.")
        self.assertIn("Quokka counts are taken in June.\n\nThe permit office closes early.",
                      contents[1])
        self.assertLessEqual(sum(len(c) for c in contents), 120)
        raw = text.encode("utf-8")
        for item in items:
            self.assertEqual(raw[item["start"]:item["end"]].decode("utf-8"), item["content"])
            self.assertTrue(item["truncated"] and item["match_in_content"])
        self.assertLess(items[0]["start"], items[1]["start"])

    def test_boundary_checks_touch_only_the_notes_read(self):
        # A-10: source_path() (symlink and vault checks on disk) runs at most top-k
        # times, before each read; one symlinked note elsewhere changes nothing.
        for number in range(12):
            write(self.vault, f"notes/n{number:02d}.md", f"# N{number}\n\nlantern {number}\n")
        write(self.vault, "notes/unrelated.md", "# U\n\nharbor\n")
        self.build()
        calls = []
        real = retrieve.source_path

        def counted(vault, name, prefixes=()):
            calls.append(name)
            return real(vault, name, prefixes)
        for method, top_k in (("fts", 3), ("fts", 1), ("synaptic", 3)):
            calls.clear()
            with self.subTest(method=method, top_k=top_k), \
                    patch.object(retrieve, "source_path", counted):
                code, packet = run_packet(self.vault, "lantern", method, "--top-k", str(top_k))
                self.assertEqual(code, 0)
                fts_reads = calls[:top_k]
                self.assertEqual(fts_reads, paths(packet, None if method == "fts" else "fts"))
                if method == "fts":
                    self.assertLessEqual(len(calls), top_k)

    def test_unsupported_names_are_skipped_and_listed(self):
        write(self.vault, "notes/ferry.md", "# F\n\nferry\n")
        if os.name == "nt":
            # Windows cannot create a colon-containing component. Feed the name
            # returned by a filesystem walk at the walk boundary and verify the
            # builder still records it as unsupported without opening it.
            walk = [(str(self.vault), ["notes"], ["Meeting: 10.30.md"]),
                    (str(self.vault / "notes"), [], ["ferry.md"])]
            unsupported = []
            original_is_file = Path.is_file

            def is_file(path):
                if path.name == "Meeting: 10.30.md":
                    return True
                return original_is_file(path)

            with patch.object(build_index.os, "walk", return_value=walk), \
                    patch.object(Path, "is_file", is_file):
                files = build_index.iter_files(self.vault, {".md"}, set(), (), unsupported)
            self.assertEqual(len(files), 1)
            self.assertTrue(os.path.samefile(files[0], self.vault / "notes" / "ferry.md"))
            self.assertEqual(files[0].name, "ferry.md")
            self.assertEqual(files[0].parent.name, "notes")
            self.assertEqual(unsupported, ["Meeting: 10.30.md"])
            self.assertEqual(source_policy.walked_name_state("notes/a\\b.md"), "unsupported")
            return
        write(self.vault, "Meeting: 10.30.md", "# M\n\nagenda\n")
        write(self.vault, "notes/a\\b.md", "# B\n\nagenda\n")
        write(self.vault, "Archive: 2024/old.md", "# O\n\nagenda\n")
        write(self.vault, ".hidden/x\\y.md", "# H\n\nagenda\n")
        out = self.build()
        listed = ["Archive: 2024/old.md", "Meeting: 10.30.md", "notes/a\\b.md"]
        self.assertIn("skipped 3 files (3 unsupported_name)", out)
        for name in listed:
            self.assertIn(f"  {name}: unsupported_name", out)
        self.assertNotIn(".hidden", out)
        manifest = json.loads((self.ctx / "index-manifest.json").read_text())
        self.assertEqual(manifest["skipped"],
                         [{"path": name, "reason": "unsupported_name"} for name in listed])
        self.assertEqual(paths(run_packet(self.vault, "ferry")[1]), ["notes/ferry.md"])

    def test_oversize_unreadable_and_invalid_utf8_are_skipped_and_reported(self):
        write(self.vault, "notes/ferry.md", "# F\n\nferry zephyrine\n")
        write(self.vault, "big.md", ("zephyrine valve " * 125_001)[:2_000_001])
        write(self.vault, "latin1.csv", "caf\xe9 zephyrine".encode("latin-1"))
        locked = write(self.vault, "locked.md", "zephyrine locked\n")
        deny_path_access(self, locked)
        unreadable = True
        out = self.build()
        expected = [{"path": "big.md", "reason": "oversize", "size": 2_000_001},
                    {"path": "latin1.csv", "reason": "not_utf8"}]
        if unreadable:
            expected.append({"path": "locked.md", "reason": "unreadable"})   # sorted by path
        manifest = json.loads((self.ctx / "index-manifest.json").read_text())
        self.assertEqual(manifest["skipped"], expected)
        self.assertEqual(manifest["max_file_bytes"], 2_000_000)
        self.assertIn(f"skipped {len(expected)} files", out)
        self.assertIn("big.md: oversize (2000001 bytes; max_file_bytes is 2000000)", out)
        _, packet = run_packet(self.vault, "zephyrine")
        self.assertEqual(paths(packet), ["notes/ferry.md"])
        self.assertEqual(packet["coverage"]["skipped_by_reason"]["oversize"], 1)
        self.assertEqual(packet["coverage"]["skipped_by_reason"]["not_utf8"], 1)

    def test_max_file_bytes_is_configurable_and_checked(self):
        write(self.vault, "big.md", "zephyrine " * 30)
        write(self.vault, ".context/routes.json", json.dumps({**ROUTES, "max_file_bytes": 200}))
        self.build()
        self.assertEqual(paths(run_packet(self.vault, "zephyrine")[1]), [])
        write(self.vault, ".context/routes.json", json.dumps({**ROUTES, "max_file_bytes": 400}))
        self.build()
        self.assertEqual(paths(run_packet(self.vault, "zephyrine")[1]), ["big.md"])
        for bad in (0, 50_000_001, "2000000", True, 2.5):
            with self.subTest(value=bad):
                write(self.vault, ".context/routes.json",
                      json.dumps({**ROUTES, "max_file_bytes": bad}))
                code, _, err = index_quietly(self.vault)
                self.assertEqual(code, 1)
                self.assertIn("max_file_bytes must be a whole number of bytes from 1 to "
                              "50,000,000", err)


if __name__ == "__main__":
    unittest.main()
