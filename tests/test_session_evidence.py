"""Tests for context_layer.session_evidence - the delivery ledger and the citation check.

Runnable as `python3 tests/test_session_evidence.py`. Every test owns a
disposable, fictional vault; HOME points into its temporary folder and no host
session id leaks in from the environment.

The transcripts below are synthetic. Their shape follows what the Claude Code
documentation states and no more: a JSONL file, one JSON object per line for a
message, a tool use or a metadata entry (code.claude.com/docs/en/sessions, which
also says the entry format is internal and changes between versions); entries
typed by a `type` string and identified by `uuid`
(code.claude.com/docs/en/agent-sdk/session-storage); assistant text in
`message.content` blocks of type `text`, as the Messages API returns it. The
Stop hook input carries `transcript_path` and `last_assistant_message`
(code.claude.com/docs/en/hooks). None of this was checked against a real
transcript here.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from _portable_helpers import isolated_home_env
from unittest import mock

REPO = Path(os.environ.get("TEST_REPO_HOME", Path(__file__).resolve().parents[1]))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from context_layer import rules, session_evidence as se  # noqa: E402

DRIVER = (
    "import argparse, sys\n"
    "from context_layer import rules\n"
    "parser = argparse.ArgumentParser(prog='context-layer')\n"
    "sub = parser.add_subparsers(dest='command', required=True)\n"
    "rules.register(sub)\n"
    "args, extra = parser.parse_known_args(sys.argv[1:])\n"
    "args.rest = [t for t in extra if t != '--']\n"
    "raise SystemExit(args.func(args))\n"
)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def entry(kind, text=None, message_id=None, blocks=None, number=0):
    """One synthetic transcript line."""
    if kind == "assistant":
        content = blocks if blocks is not None else [{"type": "text", "text": text}]
        body = {"id": message_id, "type": "message", "role": "assistant", "content": content}
        return {"type": "assistant", "uuid": f"u-{number}", "message": body}
    if kind == "user":
        return {"type": "user", "uuid": f"u-{number}",
                "message": {"role": "user", "content": text}}
    return {"type": kind, "uuid": f"u-{number}", "summary": text}


class EvidenceBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        home = self.root / "home"
        home.mkdir()
        patcher = mock.patch.dict(os.environ, isolated_home_env(os.environ, str(home)))
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.vault = self.root / "vault"
        for name, text in {"notes/garden.md": "# Garden\nTomatoes in bed two.\n",
                           "notes/beans.md": "# Beans\nPole beans by the fence.\n",
                           "a.md": "# A\nRoot note.\n",
                           "private/secret.md": "# Secret\nAccount numbers.\n"}.items():
            path = self.vault / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        (self.vault / ".context").mkdir()
        (self.vault / ".context" / "routes.json").write_text(json.dumps(
            {"routes": {}, "exclude_prefixes": ["private/"]}), encoding="utf-8")
        self.garden = sha(self.vault / "notes" / "garden.md")
        self.beans = sha(self.vault / "notes" / "beans.md")

    def transcript(self, entries, name="t.jsonl"):
        path = self.root / name
        path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        return path

    def ledger_lines(self, session="s1"):
        path = se.ledger_path(self.vault, session)
        return [json.loads(line) for line in path.read_text(encoding="ascii").splitlines()]


class Ledger(EvidenceBase):
    def test_record_and_read_back(self):
        items = [{"source_path": "notes/garden.md", "source_sha256": self.garden,
                  "content": "Tomatoes in bed two."},
                 ("notes/beans.md", self.beans.upper()),
                 {"path": "private/secret.md", "sha256": "0" * 64},       # excluded
                 {"source_path": "notes/garden.md", "source_sha256": "abc"},  # not a hash
                 {"source_path": "../escape.md", "source_sha256": "1" * 64},  # outside
                 "not an item"]
        result = se.record_delivery(self.vault, "s1", items, packet_id="a1b2c3d4e5f6",
                                    now=None)
        self.assertEqual((result["written"], result["skipped"]), (2, 4))
        self.assertEqual(result["ledger"], ".context/session-evidence/s1.jsonl")
        lines = self.ledger_lines()
        self.assertEqual([sorted(line) for line in lines],
                         [["at", "packet_id", "path", "sha256"]] * 2)
        self.assertEqual([line["path"] for line in lines], ["notes/garden.md", "notes/beans.md"])
        self.assertEqual(lines[1]["sha256"], self.beans)                  # stored lower case
        raw = se.ledger_path(self.vault, "s1").read_bytes()
        self.assertNotIn(b"Tomatoes", raw)                                # ids and hashes only
        self.assertNotIn(b"private", raw)
        seen = se.delivered(self.vault, "s1")
        self.assertEqual(seen["paths"], {"notes/garden.md", "notes/beans.md"})
        self.assertEqual(seen["sha256"], {self.garden, self.beans})
        self.assertEqual(seen["packet_ids"], {"a1b2c3d4e5f6"})
        self.assertTrue(seen["present"] and seen["complete"])

    def test_the_one_writer_serves_the_hook_and_the_server(self):
        # F2-08: `channel` adds the documented line shape, `require_folder` is the opt-in,
        # a symlinked ledger file is never followed.
        item = {"source_path": "notes/garden.md", "source_sha256": self.garden,
                "line_start": 3, "line_end": 9, "content": "Tomatoes in bed two."}
        folder = self.vault / ".context" / "session-evidence"
        if folder.exists():
            for path in folder.iterdir():
                path.unlink()
            folder.rmdir()
        refused = se.record_delivery(self.vault, "s9", [item], channel="mcp", require_folder=True)
        self.assertEqual(refused["written"], 0)
        self.assertIn("opt-in", refused["reason"])
        self.assertFalse(folder.exists())
        folder.mkdir(parents=True)
        done = se.record_delivery(self.vault, "s9", [item], packet_id="a" * 64, channel="mcp",
                                  require_folder=True)
        self.assertEqual(done["written"], 1)
        record = json.loads((folder / "s9.jsonl").read_text(encoding="ascii"))
        self.assertEqual((record["schema"], record["session"], record["channel"]),
                         ("session-evidence/v1", "s9", "mcp"))
        self.assertEqual((record["path"], record["lines"], record["packet_id"]),
                         ("notes/garden.md", [3, 9], "a" * 64))
        self.assertNotIn(b"Tomatoes", (folder / "s9.jsonl").read_bytes())
        outside = self.root / "outside.jsonl"
        outside.write_text("", encoding="ascii")
        (folder / "s8.jsonl").symlink_to(outside)
        linked = se.record_delivery(self.vault, "s8", [item], channel="hook", require_folder=True)
        self.assertEqual(linked["written"], 0)
        self.assertEqual(outside.read_text(encoding="ascii"), "")

    def test_session_id_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "env-session"}):
            result = se.record_delivery(self.vault, None, [("notes/garden.md", self.garden)])
        self.assertEqual(result["ledger"], ".context/session-evidence/env-session.jsonl")
        nothing = se.record_delivery(self.vault, None, [("notes/garden.md", self.garden)])
        self.assertEqual(nothing["written"], 0)
        self.assertIn("no session id", nothing["reason"])

    def test_unreadable_routes_writes_nothing(self):
        (self.vault / ".context" / "routes.json").write_text("{broken", encoding="utf-8")
        result = se.record_delivery(self.vault, "s1", [("notes/garden.md", self.garden)])
        self.assertEqual(result["written"], 0)
        self.assertIn("exclusions unknown", result["reason"])
        self.assertFalse(se.ledger_path(self.vault, "s1").exists())

    def test_session_ids_never_become_paths(self):
        for value in ("../../etc/passwd", "..", "a/b", ".hidden", "x" * 300, "café"):
            with self.subTest(value=value):
                stem = se.safe_stem(value)
                self.assertRegex(stem, r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")
                self.assertNotIn("/", stem)
                self.assertEqual(se.ledger_path(self.vault, value).parent,
                                 self.vault / ".context" / "session-evidence")
        uuid = "550e8400-e29b-41d4-a716-446655440000"
        self.assertEqual(se.safe_stem(uuid), uuid)

    def test_overflow_marks_the_ledger_incomplete(self):
        with mock.patch.object(se, "MAX_LEDGER_BYTES", 300):
            first = se.record_delivery(self.vault, "s1", [("notes/garden.md", self.garden)])
            second = se.record_delivery(self.vault, "s1", [("notes/beans.md", self.beans)] * 3)
            third = se.record_delivery(self.vault, "s1", [("notes/beans.md", self.beans)])
            seen = se.delivered(self.vault, "s1")
        self.assertEqual((first["written"], second["written"], third["written"]), (1, 0, 0))
        raw = se.ledger_path(self.vault, "s1").read_text(encoding="ascii")
        self.assertEqual(raw.count('"overflow":true'), 1)
        self.assertFalse(seen["complete"])
        result = se.check(self.vault, "s1", "See `notes/beans.md`.", ["notes/beans.md"])
        self.assertIsNone(se.message_line(result))      # a dropped line is not a missing one

    def test_only_the_newest_session_files_are_kept(self):
        with mock.patch.object(se, "MAX_LEDGER_FILES", 3):
            for index in range(5):
                se.record_delivery(self.vault, f"s{index}", [("notes/garden.md", self.garden)])
                path = se.ledger_path(self.vault, f"s{index}")
                stamp = time.time() - 100 + index
                os.utime(path, (stamp, stamp))
        names = sorted(p.name for p in (self.vault / ".context" / "session-evidence").iterdir())
        self.assertEqual(names, [".session-evidence.lock", "s2.jsonl", "s3.jsonl", "s4.jsonl"])

    def test_concurrent_writers_never_interleave(self):
        script = ("import sys\nfrom context_layer import session_evidence as se\n"
                  "for i in range(25):\n"
                  f"    se.record_delivery({str(self.vault)!r}, 'shared', "
                  f"[('notes/garden.md', {self.garden!r}), ('notes/beans.md', {self.beans!r})])\n")
        env = dict(os.environ, PYTHONPATH=str(REPO))
        workers = [subprocess.Popen([sys.executable, "-c", script], cwd=REPO, env=env)
                   for _ in range(6)]
        for worker in workers:
            self.assertEqual(worker.wait(timeout=60), 0)
        lines = se.ledger_path(self.vault, "shared").read_text(encoding="ascii").splitlines()
        self.assertEqual(len(lines), 6 * 25 * 2)
        self.assertTrue(all(json.loads(line)["sha256"] in (self.garden, self.beans)
                            for line in lines))


class Transcript(EvidenceBase):
    def test_last_message_is_joined_from_its_entries(self):
        path = self.transcript([
            entry("user", "Where are the tomatoes?", number=1),
            entry("assistant", "An earlier answer citing notes/beans.md.", "msg-1", number=2),
            entry("user", [{"type": "tool_result", "content": "ok"}], number=3),
            entry("assistant", "Tomatoes are in bed two", "msg-2", number=4),
            entry("assistant", None, "msg-2", number=5,
                  blocks=[{"type": "tool_use", "name": "Read", "input": {}}]),
            entry("assistant", "(`notes/garden.md`).", "msg-2", number=6),
            entry("summary", "metadata written after the reply", number=7),
        ])
        text = se.last_assistant_text(path)
        self.assertEqual(text, "Tomatoes are in bed two\n(`notes/garden.md`).")
        self.assertNotIn("beans", text)

    def test_tool_use_only_final_message_has_no_text(self):
        path = self.transcript([
            entry("assistant", "Some text.", "msg-1", number=1),
            entry("user", [{"type": "tool_result", "content": "ok"}], number=2),
            entry("assistant", None, "msg-2", number=3,
                  blocks=[{"type": "tool_use", "name": "Bash", "input": {}}]),
        ])
        self.assertIsNone(se.last_assistant_text(path))

    def test_broken_lines_are_skipped_and_the_tail_is_bounded(self):
        path = self.root / "t.jsonl"
        lines = ["{not json", json.dumps(["a list"]),
                 json.dumps(entry("assistant", "x" * 500, "msg-0", number=0)),
                 json.dumps(entry("assistant", "Final: `notes/garden.md`.", "msg-1", number=1))]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertEqual(se.last_assistant_text(path), "Final: `notes/garden.md`.")
        # A tail that starts inside the 500-character line drops that partial line.
        last = len(lines[-1].encode("utf-8")) + 1
        self.assertEqual(se.last_assistant_text(path, max_bytes=last + 40),
                         "Final: `notes/garden.md`.")
        self.assertIsNone(se.last_assistant_text(path, max_bytes=last - 10))
        self.assertIsNone(se.last_assistant_text(self.root / "missing.jsonl"))
        self.assertIsNone(se.last_assistant_text(self.root))              # a directory

    def test_final_message_prefers_the_documented_field(self):
        path = self.transcript([entry("assistant", "From the transcript.", "m", number=1)])
        self.assertEqual(se.final_message({"last_assistant_message": "From the field.",
                                           "transcript_path": str(path)}),
                         ("From the field.", "last_assistant_message"))
        self.assertEqual(se.final_message({"last_assistant_message": "  ",
                                           "transcript_path": str(path)}),
                         ("From the transcript.", "transcript_path"))
        self.assertEqual(se.final_message({}), (None, "none"))


class Citations(EvidenceBase):
    PATHS = ["notes/garden.md", "notes/beans.md", "a.md"]

    def test_paths_and_hashes_are_extracted_conservatively(self):
        text = (f"Bed two: `notes/garden.md` ({self.garden[:12]}), see also ./notes/beans.md.\n"
                "Commit ebb88ba8 and session 550e8400-e29b-41d4-a716-446655440000 are no "
                "citations; neither is my-a.md or notes/a.md.\n"
                "The marker said path=notes/garden.md sha256=5d41402abc4b.\n"
                f'"source_sha256": "{"f" * 64}"\n'
                "Its SHA-256 prefix 12345678 and hash is 9abcdef0.\n")
        found = se.extract_citations(text, self.PATHS)
        self.assertEqual(found["paths"], ["notes/beans.md", "notes/garden.md"])
        self.assertEqual(found["hashes"], [self.garden[:12], "5d41402abc4b", "f" * 64,
                                           "12345678", "9abcdef0"])

    def test_root_path_inside_a_longer_path_is_not_a_second_citation(self):
        found = se.extract_citations("Read `notes/a.md` and `a.md`.", ["a.md", "notes/a.md"])
        self.assertEqual(found["paths"], ["a.md", "notes/a.md"])
        found = se.extract_citations("Read `notes/a.md` only.", ["a.md", "notes/a.md"])
        self.assertEqual(found["paths"], ["notes/a.md"])

    def test_forged_citations_are_reported_and_delivered_ones_are_not(self):
        se.record_delivery(self.vault, "s1", [("notes/garden.md", self.garden)],
                           packet_id="0a1b2c3d4e5f")
        rule_hash = "c" * 64
        answer = (f"Tomatoes: `notes/garden.md` (sha256 {self.garden[:12]}). "
                  f"Beans: `notes/beans.md` (sha256 {self.beans[:12]}). "
                  "A note that does not exist: `notes/forged.md` (sha256 0badc0ffee12). "
                  f"Packet 0a1b2c3d4e5f. Rules: sha256 {rule_hash[:16]}. "
                  "I also wrote `a.md`.")
        result = se.check(self.vault, "s1", answer, self.PATHS, known_hashes=[rule_hash],
                          written_paths=["a.md"])
        self.assertEqual(result["undelivered_paths"], ["notes/beans.md"])
        self.assertEqual(result["undelivered_hashes"], [self.beans[:12], "0badc0ffee12"])
        line = se.message_line(result)
        self.assertIn("`notes/beans.md`", line)
        self.assertIn("hash 0badc0ffee12", line)
        self.assertNotIn("garden", line)
        self.assertNotIn("forged.md", line)            # not a vault path; its hash is named
        self.assertNotIn("No delivery was recorded", line)

    def test_no_ledger_is_said_plainly(self):
        result = se.check(self.vault, "s9", "See `notes/garden.md`.", self.PATHS)
        line = se.message_line(result)
        self.assertIn("`notes/garden.md`", line)
        self.assertIn("No delivery was recorded for this session.", line)

    def test_nothing_to_say_is_none(self):
        se.record_delivery(self.vault, "s1", [("notes/garden.md", self.garden)])
        result = se.check(self.vault, "s1", "Only `notes/garden.md`.", self.PATHS)
        self.assertIsNone(se.message_line(result))

    def test_many_citations_are_listed_with_a_count(self):
        paths = [f"notes/n{i}.md" for i in range(12)]
        text = " ".join(f"`{p}`" for p in paths)
        line = se.message_line(se.check(self.vault, "s1", text, paths))
        self.assertIn("and 4 more", line)


class StopHook(EvidenceBase):
    def env(self):
        env = dict(os.environ)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
        return env

    def stop(self, payload):
        result = subprocess.run([sys.executable, "-c", DRIVER, "rules", "hook", "stop",
                                 "--vault", str(self.vault), "--check-citations"], cwd=REPO,
                                input=json.dumps(payload), capture_output=True, text=True,
                                env=self.env())
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def test_forged_path_in_a_synthetic_transcript(self):
        rules.init(self.vault, apply_now=True)
        se.record_delivery(self.vault, "s1", [("notes/garden.md", self.garden)])
        path = self.transcript([
            entry("user", "Where are the beans and tomatoes?", number=1),
            entry("assistant", "Tomatoes: `notes/garden.md`. Beans: `notes/beans.md` "
                               "(read it myself, sha256 0badc0ffee12).", "msg-1", number=2)])
        output = self.stop({"session_id": "s1", "transcript_path": str(path),
                            "stop_hook_active": False})
        self.assertNotIn("decision", output)
        self.assertIn("`notes/beans.md`", output["systemMessage"])
        self.assertIn("hash 0badc0ffee12", output["systemMessage"])
        self.assertNotIn("garden", output["systemMessage"])

    def test_excluded_paths_are_never_named(self):
        output = self.stop({"session_id": "s1", "last_assistant_message":
                            "The account is in `private/secret.md` and `notes/garden.md`."})
        self.assertIn("`notes/garden.md`", output["systemMessage"])
        self.assertNotIn("private", output["systemMessage"])

    def test_works_without_rule_files_and_writes_no_state(self):
        output = self.stop({"session_id": "s1", "last_assistant_message": "Nothing cited."})
        self.assertIsNone(output)
        self.assertFalse((self.vault / ".context" / rules.STATE_NAME).exists())

    def test_never_blocks_even_with_stop_hook_active(self):
        output = self.stop({"session_id": "s1", "stop_hook_active": True,
                            "last_assistant_message": "See `notes/beans.md`."})
        self.assertNotIn("decision", output)
        self.assertIn("`notes/beans.md`", output["systemMessage"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
