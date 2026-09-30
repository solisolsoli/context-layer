"""Tests for context_layer.session_show - `context-layer session show|list`.

Runnable as `python3 tests/test_session_show.py`. The vault is synthetic and
fictional: two sessions, built with the components' own writers (the delivery
ledger, memory records, the task ledger), plus hand-written task folders. HOME
points into a temporary folder, no host session id leaks in, and nothing calls
a model or the network.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(os.environ.get("TEST_REPO_HOME", Path(__file__).resolve().parents[1]))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from context_layer import jev, memory, orchestrate, session_evidence as se  # noqa: E402
from context_layer import session_show as ss  # noqa: E402

PACKET = "ab" * 32                       # a shared packet id (64 hex)
NOW_A = datetime(2026, 1, 15, 14, 30, 0, tzinfo=timezone.utc)
NOW_B = datetime(2099, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
TASK_1 = "20260115T143000Z-a1b2c3"      # dispatched and verified in session A
TASK_2 = "20260115T150000Z-d4e5f6"      # dispatched from A's packet, never attested
NOTES = {"notes/garden.md": "# Garden\nTomatoes in bed two.\n",
         "notes/beans.md": "# Beans\nPole beans by the fence.\n",
         "notes/harbor.md": "# Harbor\nThe lamp is on the north pier.\n"}


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot(root: Path) -> dict:
    """Every file (bytes hash, size, mtime) and every folder under root."""
    state = {}
    for base, dirs, files in os.walk(root):
        for name in dirs:
            state[os.path.relpath(os.path.join(base, name), root)] = "dir"
        for name in files:
            path = os.path.join(base, name)
            info = os.lstat(path)
            state[os.path.relpath(path, root)] = (
                hashlib.sha256(Path(path).read_bytes()).hexdigest(), info.st_size,
                info.st_mtime_ns)
    return state


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("CLAUDE_CODE_SESSION_ID", "CONTEXT_LAYER_SESSION", "CONTEXT_LAYER_TOOL"):
            os.environ.pop(name, None)
        self.vault = self.root / "vault"
        for name, text in NOTES.items():
            path = self.vault / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        (self.vault / ".context").mkdir(exist_ok=True)
        (self.vault / ".context" / "routes.json").write_text(
            json.dumps({"routes": {}, "exclude_prefixes": []}), encoding="utf-8")
        self.build_sessions()

    # -- fixture -------------------------------------------------------------

    def in_session(self, session):
        return mock.patch.dict(os.environ, {"CONTEXT_LAYER_SESSION": session})

    def task_dir(self, task_id, state, packet=None, ledger=None, job=None):
        folder = self.vault / ".context" / "tasks" / task_id
        folder.mkdir(parents=True, exist_ok=True)
        task = {"schema": "context-layer-task-v1", "id": task_id, "created_at": "2026-01-15T14:31:00Z",
                "goal": "Summarise the garden note.", "backend": "fake", "output_dir": "out"}
        if packet:
            task["shared_packet_id"] = packet
        if job:
            task["job"] = job
        result = {"schema": "context-layer-task-result-v1", "id": task_id, "state": state,
                  "attempts": [{"attempt": 1, "ok": True}]}
        if state in ("verified", "rejected"):
            result["verification"] = {"state": state, "checked_at": "2026-01-15T14:40:00Z",
                                      "problems": [], "ledger": ledger}
        (folder / "task.json").write_text(json.dumps(task), encoding="utf-8")
        (folder / "result.json").write_text(json.dumps(result), encoding="utf-8")

    def build_sessions(self):
        garden, beans, harbor = (sha(NOTES[n]) for n in NOTES)
        # Session A: two deliveries in two packets, memory, a verified task, an advisor row.
        se.record_delivery(self.vault, "sess-a", [("notes/garden.md", garden)],
                           packet_id=PACKET, now=NOW_A)
        se.record_delivery(self.vault, "sess-a", [("notes/beans.md", beans)],
                           packet_id="hookid1", now=NOW_A)
        # Session B: one delivery, nothing else.
        se.record_delivery(self.vault, "sess-b", [("notes/harbor.md", harbor)],
                           packet_id=None, now=NOW_B)
        packet_dir = self.vault / ".context" / "packets"
        packet_dir.mkdir()
        (packet_dir / f"{PACKET}.json").write_text("{}", encoding="utf-8")
        with self.in_session("sess-a"):
            self.decision = memory.record(
                self.vault, kind="decision", text="Plant tomatoes in bed two.",
                sources=[{"path": "notes/garden.md"}], tool="test")
            self.task_rec = memory.record(
                self.vault, kind="task", text="Stake the beans.",
                sources=[{"path": "notes/harbor.md"}], tool="test")
            self.line = orchestrate.ledger_append(self.vault, {
                "event": "verify", "task_id": TASK_1, "verdict": "verified",
                "packet_id": PACKET, "produced": [{"path": "out/a.md", "sha256": "0" * 64}]})
            orchestrate.ledger_append(self.vault, {
                "event": "record", "task_id": TASK_1, "memory_id": self.decision["id"],
                "cites": self.line["sha256"], "verdict": "verified"})
        # A ledger line of another session, and one of no session.
        with mock.patch.dict(os.environ, {"CONTEXT_LAYER_SESSION": "sess-b"}):
            orchestrate.ledger_append(self.vault, {"event": "verify", "task_id": "other-task",
                                                   "verdict": "rejected"})
        self.task_dir(TASK_1, "verified", packet=PACKET,
                      ledger={"n": self.line["n"], "sha256": self.line["sha256"]},
                      job={"path": ".context/jobs/j1/job.md", "sha256": "1" * 64})
        self.task_dir(TASK_2, "verified", packet=PACKET,
                      ledger={"n": 99, "sha256": "2" * 64})
        # Advisor rows: one inside session A's window (the session spans 2026-01-15 to
        # today), one long before it.
        (self.vault / ".context").mkdir(exist_ok=True)
        jev.append_log(self.vault, {"at": int(NOW_A.timestamp()), "feature": "search",
                                    "mode": "shadow", "applied": False, "requests": 1,
                                    "input_tokens": 40, "output_tokens": 5})
        jev.append_log(self.vault, {"at": 1_500_000_000, "feature": "search", "mode": "on",
                                    "applied": True, "requests": 9})

    def run_cli(self, *argv):
        environment = dict(os.environ)
        environment["HOME"] = str(self.home)
        return subprocess.run([sys.executable, "-m", "context_layer.cli", "session", *argv],
                              cwd=REPO, capture_output=True, text=True, env=environment)


class Joins(Base):
    def test_session_a_is_joined_from_every_source(self):
        report = ss.build(self.vault, "sess-a")
        self.assertEqual(report["schema"], "session-show/v1")
        evidence = report["evidence"]
        self.assertEqual(evidence["total"], 2)
        self.assertEqual([i["path"] for i in evidence["items"]],
                         ["notes/garden.md", "notes/beans.md"])
        self.assertEqual(evidence["items"][0]["cite"], ".context/session-evidence/sess-a.jsonl:1")
        self.assertEqual(evidence["items"][1]["cite"], ".context/session-evidence/sess-a.jsonl:2")
        packets = {p["packet_id"]: p for p in evidence["packets"]}
        self.assertEqual(packets[PACKET]["kind"], "shared_packet_id")
        self.assertEqual(packets[PACKET]["packet_file_status"], "present")
        self.assertEqual(packets["hookid1"]["kind"], "other_id")

        records = {r["id"]: r for r in report["memory"]["records"]}
        self.assertEqual(set(records), {self.decision["id"], self.task_rec["id"]})
        decision = records[self.decision["id"]]
        self.assertEqual(decision["cite"], ".context/memory/records.jsonl:1")
        self.assertTrue(decision["in_force"])
        self.assertNotIn("text", decision)
        # garden.md was delivered in this session, harbor.md was not.
        self.assertTrue(decision["sources"][0]["delivered_in_session"])
        self.assertFalse(records[self.task_rec["id"]]["sources"][0]["delivered_in_session"])

        events = report["memory"]["session_record_events"]
        self.assertTrue(all(e["cite"].startswith(".context/sessions/sess-a.jsonl:")
                            for e in events))
        self.assertIn(("memory.write", self.decision["id"]),
                      [(e["event"], e.get("id")) for e in events])

        tasks_ = {t["task_id"]: t for t in report["tasks"]["items"]}
        self.assertEqual(set(tasks_), {TASK_1, TASK_2})
        first = tasks_[TASK_1]
        self.assertEqual(first["joined_by"], "ledger session")
        self.assertEqual(first["state"], "verified")
        self.assertEqual(first["attestation"], "attested")
        self.assertEqual(first["job"]["path"], ".context/jobs/j1/job.md")
        self.assertEqual([(l["event"], l["cite"]) for l in first["ledger_lines"]],
                         [("verify", ".context/tasks/LEDGER.jsonl:1"),
                          ("record", ".context/tasks/LEDGER.jsonl:2")])
        self.assertEqual(first["ledger_lines"][1]["memory_id"], self.decision["id"])
        # Session B's ledger line is not A's.
        self.assertNotIn("other-task", tasks_)
        second = tasks_[TASK_2]
        self.assertEqual(second["joined_by"], "delivered packet id")
        self.assertEqual(second["attestation"], "UNATTESTED")
        self.assertIn("no ledger line", second["attestation_reason"])

    def test_advisor_rows_are_matched_by_window_and_labelled(self):
        advisor = ss.build(self.vault, "sess-a")["advisor"]
        self.assertFalse(advisor["attributed"])
        self.assertEqual(advisor["rows"], 1)
        self.assertEqual(advisor["features"]["search"]["input_tokens"], 40)
        self.assertEqual(advisor["window"]["from"], "2026-01-15T14:30:00Z")

    def test_session_b_only_has_its_own_rows(self):
        report = ss.build(self.vault, "sess-b")
        self.assertEqual(report["evidence"]["total"], 1)
        self.assertEqual(report["evidence"]["items"][0]["path"], "notes/harbor.md")
        self.assertEqual(report["memory"]["records_total"], 0)
        self.assertEqual(report["memory"]["session_record_events_total"], 0)
        # Its one ledger line names a task that has no folder: reported, not guessed.
        self.assertEqual([t["task_id"] for t in report["tasks"]["items"]], ["other-task"])
        self.assertEqual(len(report["tasks"]["items"][0]["problems"]), 2)
        by_name = {s["name"]: s for s in report["sources"]}
        self.assertEqual(by_name["session_record"]["status"], "missing")
        self.assertEqual(by_name["evidence_ledger"]["status"], "ok")
        # Missing sources are reported as missing, not as a problem, and not invented.

    def test_superseded_record_is_not_in_force(self):
        with self.in_session("sess-a"):
            newer = memory.record(self.vault, kind="decision", text="Plant peppers instead.",
                                  sources=[{"path": "notes/garden.md"}], tool="test",
                                  supersedes=self.decision["id"])
        records = {r["id"]: r for r in ss.build(self.vault, "sess-a")["memory"]["records"]}
        self.assertFalse(records[self.decision["id"]]["in_force"])
        self.assertEqual(records[self.decision["id"]]["superseded_by"], [newer["id"]])
        self.assertTrue(records[newer["id"]]["in_force"])

    def test_unsafe_session_id_joins_through_its_hashed_file_names(self):
        odd = "team a/1"
        se.record_delivery(self.vault, odd, [("notes/garden.md", sha(NOTES["notes/garden.md"]))],
                           now=NOW_A)
        with self.in_session(odd):
            memory.record(self.vault, kind="note", text="Odd id note.", tool="test")
        report = ss.build(self.vault, odd)
        self.assertEqual(report["evidence"]["total"], 1)
        self.assertEqual(report["memory"]["records_total"], 1)
        self.assertEqual(report["memory"]["session_record_events_total"], 1)
        rows = [r for r in ss.list_sessions(self.vault)["sessions"] if r["session"] == odd]
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]["sources"]),
                         {"evidence_ledger", "memory_records", "session_record"})

    def test_a_task_id_that_is_not_a_folder_name_is_not_opened(self):
        with self.in_session("sess-a"):
            orchestrate.ledger_append(self.vault, {"event": "verify",
                                                   "task_id": "../../escape",
                                                   "verdict": "verified"})
        views = {t["task_id"]: t for t in ss.build(self.vault, "sess-a")["tasks"]["items"]}
        self.assertIn("not opened", views["../../escape"]["problems"][0])
        self.assertEqual(views["../../escape"]["attestation"], "unchecked")

    def test_text_output_cites_files_and_lines(self):
        done = self.run_cli("show", str(self.vault), "sess-a")
        self.assertEqual(done.returncode, 0, done.stderr)
        for expected in (".context/session-evidence/sess-a.jsonl:1", "notes/garden.md",
                         ".context/memory/records.jsonl:1", ".context/tasks/LEDGER.jsonl:1",
                         "UNATTESTED", "attested", "Complete: yes"):
            self.assertIn(expected, done.stdout)
        self.assertNotIn(str(self.root), done.stdout)


class ReadOnly(Base):
    def test_no_command_changes_the_tree(self):
        before = snapshot(self.vault)
        for argv in (("show", str(self.vault), "sess-a"), ("show", str(self.vault), "sess-b"),
                     ("show", str(self.vault)), ("show", str(self.vault), "sess-a", "--json"),
                     ("show", str(self.vault), "nope"), ("list", str(self.vault)),
                     ("list", str(self.vault), "--json")):
            self.run_cli(*argv)
        ss.build(self.vault, "sess-a")
        self.assertEqual(snapshot(self.vault), before)

    def test_home_and_missing_context_folder_are_left_alone(self):
        bare = self.root / "bare"
        bare.mkdir()
        done = self.run_cli("show", str(bare), "sess-a")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(list(bare.iterdir()), [])
        self.assertEqual(list(self.home.iterdir()), [])


class Failures(Base):
    def test_torn_evidence_line_is_reported_not_guessed(self):
        ledger = se.ledger_path(self.vault, "sess-a")
        with open(ledger, "ab") as handle:
            handle.write(b'{"path": "notes/x.md", "sha')          # a torn append
        report = ss.build(self.vault, "sess-a")
        by_name = {s["name"]: s for s in report["sources"]}
        self.assertEqual(by_name["evidence_ledger"]["status"], "torn")
        self.assertEqual(report["evidence"]["total"], 2)           # nothing invented
        self.assertFalse(report["complete"])
        self.assertTrue(any("line 3" in p and "torn" in p for p in report["problems"]))
        done = self.run_cli("show", str(self.vault), "sess-a")
        self.assertEqual(done.returncode, 0)
        self.assertIn("! line 3: not a JSON object", done.stdout)
        self.assertIn("Complete: no", done.stdout)

    def test_bad_line_in_the_middle_is_damage_and_the_rest_is_still_read(self):
        path = memory.session_path(self.vault, "sess-a")
        lines = path.read_bytes().split(b"\n")
        lines.insert(1, b"not json at all")
        path.write_bytes(b"\n".join(lines))
        report = ss.build(self.vault, "sess-a")
        by_name = {s["name"]: s for s in report["sources"]}
        self.assertEqual(by_name["session_record"]["status"], "damaged")
        self.assertGreater(report["memory"]["session_record_events_total"], 1)
        self.assertTrue(any(p.endswith("line 2: not a JSON object") for p in report["problems"]))

    def test_torn_memory_line_makes_in_force_unknown(self):
        with open(memory.records_path(self.vault), "ab") as handle:
            handle.write(b'{"id": "m-0000')
        records = ss.build(self.vault, "sess-a")["memory"]["records"]
        self.assertTrue(records)
        self.assertTrue(all(r["in_force"] is None for r in records))

    def test_broken_ledger_chain_makes_verdicts_unattested(self):
        path = orchestrate.ledger_path(self.vault)
        with open(path, "ab") as handle:
            handle.write(b"junk\n")
        report = ss.build(self.vault, "sess-a")
        first = {t["task_id"]: t for t in report["tasks"]["items"]}[TASK_1]
        self.assertEqual(first["attestation"], "UNATTESTED")
        self.assertIn("chain is broken", first["attestation_reason"])
        self.assertFalse(report["complete"])

    def test_missing_task_files_are_reported(self):
        for name in ("task.json", "result.json"):
            (self.vault / ".context" / "tasks" / TASK_1 / name).unlink()
        first = {t["task_id"]: t for t in ss.build(self.vault, "sess-a")["tasks"]["items"]}[TASK_1]
        self.assertIsNone(first["state"])
        self.assertEqual(len(first["problems"]), 2)
        self.assertEqual(first["attestation"], "not_applicable")

    def test_missing_packet_file_is_absent_not_present(self):
        (self.vault / ".context" / "packets" / f"{PACKET}.json").unlink()
        packets = {p["packet_id"]: p for p in ss.build(self.vault, "sess-a")["evidence"]["packets"]}
        self.assertEqual(packets[PACKET]["packet_file_status"], "absent")
        self.assertIsNone(packets[PACKET]["packet_file"])

    def test_unknown_session_id_exits_1(self):
        done = self.run_cli("show", str(self.vault), "no-such-session")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, "")
        self.assertIn("unknown session id", done.stderr)
        with self.assertRaises(ss.ShowError):
            ss.build(self.vault, "no-such-session")

    def test_no_sessions_and_bad_input(self):
        empty = self.root / "empty"
        (empty / ".context").mkdir(parents=True)
        done = self.run_cli("show", str(empty))
        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.assertIn("no sessions", done.stderr)
        listing = self.run_cli("list", str(empty))
        self.assertEqual(listing.returncode, 0)
        self.assertIn("No sessions found", listing.stdout)
        self.assertEqual(self.run_cli("show", str(self.root / "missing"), "s").returncode, 1)
        self.assertEqual(self.run_cli("show", str(self.vault), "  ").returncode, 2)
        self.assertEqual(self.run_cli("show", str(self.vault), "sess-a", "--limit", "0").returncode, 2)
        self.assertEqual(self.run_cli("show", str(self.vault), "sess-a", "--bogus").returncode, 2)

    def test_a_symlinked_source_is_not_followed(self):
        outside = self.root / "outside.jsonl"
        outside.write_text('{"path": "notes/secret.md", "sha256": "' + "3" * 64 + '"}\n',
                           encoding="utf-8")
        link = se.ledger_path(self.vault, "sess-link")
        link.symlink_to(outside)
        report = ss.build(self.vault, "sess-link")
        self.assertEqual(report["evidence"]["total"], 0)
        by_name = {s["name"]: s for s in report["sources"]}
        self.assertEqual(by_name["evidence_ledger"]["status"], "unreadable")


class Bounds(Base):
    def test_a_large_ledger_is_bounded_and_the_bound_can_be_raised(self):
        items = [(f"notes/n{i}.md", sha(str(i))) for i in range(300)]
        ledger = se.ledger_path(self.vault, "sess-big")
        ledger.parent.mkdir(parents=True, exist_ok=True)
        # record_delivery checks exclusions only, not that the file exists.
        for start in range(0, 300, 100):
            se.record_delivery(self.vault, "sess-big", items[start:start + 100], now=NOW_A)
        report = ss.build(self.vault, "sess-big")
        self.assertEqual(report["evidence"]["total"], 300)
        self.assertEqual(len(report["evidence"]["items"]), ss.DEFAULT_LIMIT)
        self.assertEqual(report["evidence"]["not_shown"], 300 - ss.DEFAULT_LIMIT)
        raised = ss.build(self.vault, "sess-big", limit=400)
        self.assertEqual(len(raised["evidence"]["items"]), 300)
        self.assertEqual(raised["evidence"]["not_shown"], 0)
        text = self.run_cli("show", str(self.vault), "sess-big", "--limit", "5")
        self.assertIn("295 more not shown (raise --limit)", text.stdout)

    def test_read_bound_marks_the_source_partial(self):
        size = se.ledger_path(self.vault, "sess-a").stat().st_size
        report = ss.build(self.vault, "sess-a", max_bytes=size // 2)
        by_name = {s["name"]: s for s in report["sources"]}
        self.assertEqual(by_name["evidence_ledger"]["status"], "partial")
        self.assertTrue(any("--max-bytes" in p for p in report["problems"]))
        self.assertFalse(report["complete"])
        self.assertLess(report["evidence"]["total"], 2)
        full = ss.build(self.vault, "sess-a", max_bytes=size)
        self.assertEqual(full["evidence"]["total"], 2)

    def test_an_oversize_task_ledger_is_not_chain_checked(self):
        size = orchestrate.ledger_path(self.vault).stat().st_size
        report = ss.build(self.vault, "sess-a", max_bytes=size - 1)
        first = {t["task_id"]: t for t in report["tasks"]["items"]}
        self.assertTrue(all(t["attestation"] in ("unchecked", "not_applicable")
                            for t in first.values()))
        self.assertTrue(any("--max-bytes" in p for p in report["problems"]))

    def test_an_overlong_line_is_skipped_and_reported(self):
        ledger = se.ledger_path(self.vault, "sess-a")
        with open(ledger, "ab") as handle:
            handle.write(b'{"path": "' + b"x" * (ss.MAX_LINE_BYTES + 10) + b'"}\n')
            handle.write(json.dumps({"path": "notes/harbor.md", "sha256": "4" * 64,
                                     "packet_id": None, "at": "2026-01-16T00:00:00Z"}
                                    ).encode() + b"\n")
        report = ss.build(self.vault, "sess-a", max_bytes=64 * 1024 * 1024)
        self.assertEqual(report["evidence"]["total"], 3)          # the line after it is read
        self.assertTrue(any("longer than" in p for p in report["problems"]))


class Listing(Base):
    def test_list_names_both_sessions_newest_first(self):
        listing = ss.list_sessions(self.vault)
        self.assertEqual(listing["schema"], "session-list/v1")
        self.assertEqual([r["session"] for r in listing["sessions"]], ["sess-b", "sess-a"])
        by_id = {r["session"]: r for r in listing["sessions"]}
        self.assertEqual(by_id["sess-a"]["counts"]["evidence_ledger"], 2)
        self.assertEqual(set(by_id["sess-a"]["sources"]),
                         {"evidence_ledger", "memory_records", "session_record", "task_ledger"})
        self.assertEqual(by_id["sess-b"]["sources"], ["evidence_ledger", "task_ledger"])
        self.assertEqual(listing["sessions"][0]["last"], "2099-01-01T00:00:00Z")
        limited = ss.list_sessions(self.vault, limit=1)
        self.assertEqual((len(limited["sessions"]), limited["not_shown"]), (1, 1))

    def test_show_without_an_id_takes_the_newest_session(self):
        done = self.run_cli("show", str(self.vault), "--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["session"], "sess-b")
        self.assertIn("most recent of 2", done.stderr)


class JsonSchema(Base):
    def test_json_keys_are_stable(self):
        done = self.run_cli("show", str(self.vault), "sess-a", "--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        report = json.loads(done.stdout)
        self.assertEqual(set(report), {"schema", "session", "read_only", "bounds", "complete",
                                       "problems", "sources", "evidence", "memory", "tasks",
                                       "advisor"})
        self.assertEqual(report["schema"], "session-show/v1")
        self.assertIs(report["read_only"], True)
        self.assertEqual(report["bounds"], {"limit": 50, "max_bytes": 8 * 1024 * 1024})
        self.assertEqual([s["name"] for s in report["sources"]],
                         ["evidence_ledger", "memory_records", "session_record", "task_ledger",
                          "tasks", "advisor"])
        for source in report["sources"]:
            self.assertEqual(set(source), {"name", "file", "status", "size", "lines", "problems"})
        self.assertEqual(set(report["evidence"]),
                         {"total", "not_shown", "items", "packets_total", "packets_not_shown",
                          "packets"})
        self.assertEqual(set(report["evidence"]["items"][0]),
                         {"cite", "path", "sha256", "packet_id", "at"})
        self.assertEqual(set(report["evidence"]["packets"][0]),
                         {"packet_id", "items", "first_at", "last_at", "first_cite", "kind",
                          "packet_file", "packet_file_status"})
        self.assertEqual(set(report["memory"]),
                         {"records_total", "records_not_shown", "records",
                          "session_record_events_total", "session_record_events_not_shown",
                          "session_record_events"})
        self.assertEqual(set(report["memory"]["records"][0]),
                         {"cite", "id", "kind", "state", "ts", "tool", "sources", "supersedes",
                          "closes", "superseded_by", "closed_by", "in_force"})
        self.assertEqual(set(report["memory"]["records"][0]["sources"][0]),
                         {"path", "sha256", "delivered_in_session"})
        self.assertEqual(set(report["tasks"]), {"total", "not_shown", "items"})
        attested = [t for t in report["tasks"]["items"] if t["attestation"] == "attested"][0]
        self.assertEqual(set(attested),
                         {"task_id", "joined_by", "files", "problems", "ledger_lines",
                          "created_at", "backend", "shared_packet_id", "job", "state",
                          "attempts", "verification", "attestation", "attestation_reason",
                          "ledger_verdict"})
        self.assertEqual(set(attested["ledger_lines"][0]),
                         {"cite", "n", "line_sha256", "event", "task_id", "verdict",
                          "memory_id", "packet_id", "ts", "produced"})
        self.assertEqual(set(report["advisor"]),
                         {"file", "status", "attributed", "basis", "window", "rows", "features"})

    def test_list_json_keys_are_stable(self):
        listing = json.loads(self.run_cli("list", str(self.vault), "--json").stdout)
        self.assertEqual(set(listing), {"schema", "read_only", "total", "not_shown", "bounds",
                                        "sessions", "problems"})
        self.assertEqual(set(listing["sessions"][0]), {"session", "sources", "counts", "last"})

    def test_no_text_or_prompt_reaches_the_report(self):
        raw = self.run_cli("show", str(self.vault), "sess-a", "--json").stdout
        for text in ("Plant tomatoes", "Stake the beans", "Summarise the garden"):
            self.assertNotIn(text, raw)


if __name__ == "__main__":
    unittest.main()
