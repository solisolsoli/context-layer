"""Phase tests for context_layer.memory — every test owns a disposable vault.

HOME is redirected to a temporary folder for every command this file runs, and
nothing here reads a real vault or a host configuration.
"""
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(os.environ.get("TEST_REPO_HOME", Path(__file__).resolve().parents[1]))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from context_layer import memory  # noqa: E402

WORKERS = 8
PER_WORKER = 25
FALLBACK_WORKERS = 6
FALLBACK_PER_WORKER = 20
BOM_KEYS = {"schema", "ts", "session", "op", "event", "id", "path", "sha256", "current_sha256"}


def write_batch(payload):
    """One child process: PER_WORKER records into the shared store."""
    vault, tag, count = payload
    from context_layer import memory as child_memory
    for index in range(count):
        child_memory.record(Path(vault), kind="note", text=f"{tag} line {index}",
                            tool="worker", session=tag)
    return tag


def write_batch_without_fcntl(payload):
    """One child process forced onto the O_EXCL fallback lock."""
    vault, tag, count = payload
    from context_layer import memory as child_memory
    child_memory.fcntl = None
    for index in range(count):
        child_memory.record(Path(vault), kind="note", text=f"{tag} fallback {index}",
                            tool="worker", session=tag)
    return tag


def legacy_record(prev, kind, text, sources=(), supersedes=None, ts="2026-01-01T00:00:00.000000Z"):
    """A format 1 record as 0.3 wrote it: no format_version, no closes, sources as given."""
    sources = [dict(source) for source in sources]
    return {"id": memory._record_id(kind, text, sources, supersedes, version=1),
            "prev": prev, "ts": ts, "tool": "cli", "session": None, "kind": kind,
            "state": "draft", "text": text, "sources": sources, "supersedes": supersedes}


def frontmatter(path: Path) -> dict:
    """The small YAML subset `memory mirror --notes` writes: scalars and string lists."""
    lines = path.read_text(encoding="utf-8").split("\n")
    assert lines[0] == "---", path
    end = lines.index("---", 1)
    data, key = {}, None
    for line in lines[1:end]:
        if line.startswith("  - "):
            data[key].append(json.loads(line[4:]))
        elif line.endswith(":"):
            key = line[:-1]
            data[key] = []
        else:
            key, _, value = line.partition(": ")
            data[key] = json.loads(value) if value[:1] == '"' or value in ("true", "false") \
                else value
    return data


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.home = root / "home"
        self.home.mkdir()
        self.vault = root / "vault"
        self.vault.mkdir()
        environment = patch.dict(os.environ, {"HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("CONTEXT_LAYER_TOOL", "CONTEXT_LAYER_SESSION"):
            os.environ.pop(name, None)
        self.alpha = self.vault / "alpha.md"
        self.alpha.write_text("# Alpha\nalpha body naïve café\n", encoding="utf-8")
        (self.vault / "notes").mkdir()
        self.beta = self.vault / "notes" / "beta.md"
        self.beta.write_text("# Beta\nbeta body\n", encoding="utf-8")
        (self.vault / "private").mkdir()
        (self.vault / "private" / "secret.md").write_text("secret\n", encoding="utf-8")
        context = self.vault / ".context"
        context.mkdir()
        self.routes = context / "routes.json"
        self.routes.write_text(json.dumps({
            "routes": {}, "exclude_prefixes": ["private"],
            "retrieval_exclude_prefixes": ["private", ".context"]}), encoding="utf-8")
        self.records = context / "memory" / "records.jsonl"
        self.mirror = context / "memory" / "MEMORY.md"

    # -- helpers ----------------------------------------------------------
    def digest(self, path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def lines(self):
        text = self.records.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.split("\n") if line.strip()]

    def add(self, **kwargs):
        kwargs.setdefault("kind", "note")
        return memory.record(self.vault, **kwargs)

    def write_lines(self, items, ensure_ascii=False):
        self.records.parent.mkdir(parents=True, exist_ok=True)
        self.records.write_text("".join(json.dumps(item, ensure_ascii=ensure_ascii) + "\n"
                                        for item in items), encoding="utf-8")

    def run_cli(self, *argv, env=None):
        environment = dict(os.environ)
        environment.pop("CONTEXT_LAYER_TOOL", None)
        environment.pop("CONTEXT_LAYER_SESSION", None)
        environment["HOME"] = str(self.home)
        environment.update(env or {})
        return subprocess.run([sys.executable, "-m", "context_layer.cli", *argv],
                              cwd=REPO, capture_output=True, text=True, env=environment)

    def cli(self, *argv, env=None):
        return self.run_cli("memory", *argv, env=env)

    def build_index(self):
        done = subprocess.run([sys.executable, str(REPO / "router" / "build_index.py"),
                               "--vault", str(self.vault)], capture_output=True, text=True,
                              env=dict(os.environ, HOME=str(self.home)))
        self.assertEqual(done.returncode, 0, done.stderr)

    def open_ids(self):
        return [r["id"] for r in memory.resume(self.vault)["open_tasks"]]


class MemoryStore(Base):
    # -- records ----------------------------------------------------------
    def test_add_stores_the_documented_fields(self):
        stored = self.add(kind="decision", text="  Use FTS by default  ",
                          sources=[{"path": "alpha.md"}], state="approved",
                          tool="claude-code", session="s-1")
        self.assertEqual(set(stored), {"id", "prev", "ts", "tool", "session", "kind", "state",
                                       "text", "sources", "supersedes", "closes",
                                       "format_version", "duplicate", "in_force",
                                       "superseded_by"})
        self.assertTrue(stored["id"].startswith("m-"))
        self.assertEqual(len(stored["id"]), 18)
        self.assertIsNone(stored["prev"])
        self.assertTrue(stored["ts"].endswith("Z"))
        self.assertEqual(stored["tool"], "claude-code")
        self.assertEqual(stored["session"], "s-1")
        self.assertEqual(stored["state"], "approved")
        self.assertEqual(stored["text"], "Use FTS by default")
        self.assertEqual(stored["sources"],
                         [{"path": "alpha.md", "sha256": self.digest(self.alpha)}])
        self.assertIsNone(stored["supersedes"])
        self.assertEqual(stored["closes"], [])
        self.assertEqual(stored["format_version"], 2)
        self.assertEqual((stored["duplicate"], stored["in_force"], stored["superseded_by"]),
                         (False, True, []))
        on_disk = self.lines()
        self.assertEqual(len(on_disk), 1)
        for derived in ("duplicate", "in_force", "superseded_by"):
            self.assertNotIn(derived, on_disk[0])  # derived flags, never stored
        self.assertEqual(on_disk[0]["id"], stored["id"])

    def test_duplicate_returns_the_stored_record_and_appends_nothing(self):
        first = self.add(kind="decision", text="same", sources=["alpha.md"])
        second = self.add(kind="decision", text="same", sources=["alpha.md"],
                          tool="other-tool")
        self.assertTrue(second["duplicate"])
        self.assertTrue(second["in_force"])
        self.assertEqual(second["superseded_by"], [])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["ts"], first["ts"])
        self.assertEqual(second["tool"], first["tool"])  # the stored one, not the caller's
        self.assertEqual(len(self.lines()), 1)

    def test_supersede_chain_resolves_in_resume(self):
        first = self.add(kind="decision", text="draft wording", state="draft")
        second = self.add(kind="decision", text="final wording", state="approved",
                          supersedes=first["id"])
        third = self.add(kind="decision", text="final wording", state="published",
                         supersedes=second["id"])
        packet = memory.resume(self.vault)
        self.assertEqual([r["id"] for r in packet["records"]], [third["id"]])
        self.assertEqual(packet["records"][0]["state"], "published")
        self.assertEqual(packet["vault"], self.vault.name)
        self.assertTrue(packet["generated_at"].endswith("Z"))
        self.assertEqual([r["prev"] for r in self.lines()],
                         [None, first["id"], second["id"]])
        self.assertEqual(memory.verify(self.vault), [])

    def test_resume_filters_kinds_and_lists_open_tasks(self):
        open_task = self.add(kind="task", text="write the docs")
        done_task = self.add(kind="task", text="ship the index")
        self.add(kind="result", text="index shipped", closes=[done_task["id"]])
        self.add(kind="note", text="unrelated note")
        packet = memory.resume(self.vault, kinds=["task"])
        self.assertEqual({r["kind"] for r in packet["records"]}, {"task"})
        self.assertEqual([r["id"] for r in packet["open_tasks"]], [open_task["id"]])
        self.assertEqual(packet["conflicts"], [])
        self.assertEqual(packet["closed_by_mention"], [])

    def test_stale_marks_changed_and_deleted_sources(self):
        recorded_alpha = self.digest(self.alpha)
        changed = self.add(kind="decision", text="rests on alpha", sources=["alpha.md"])
        deleted = self.add(kind="note", text="rests on beta", sources=["notes/beta.md"])
        self.alpha.write_text("# Alpha\nrewritten\n", encoding="utf-8")
        self.beta.unlink()
        packet = memory.resume(self.vault)
        by_id = {entry["id"]: entry for entry in packet["stale"]}
        self.assertEqual(set(by_id), {changed["id"], deleted["id"]})
        self.assertEqual(by_id[changed["id"]]["path"], "alpha.md")
        self.assertEqual(by_id[changed["id"]]["recorded_sha256"], recorded_alpha)
        self.assertEqual(by_id[changed["id"]]["current_sha256"], self.digest(self.alpha))
        self.assertIsNone(by_id[deleted["id"]]["current_sha256"])
        self.assertEqual(by_id[deleted["id"]]["moved_to"], [])  # no index manifest here
        self.assertTrue(any("stale source" in p for p in memory.verify(self.vault)))

    def test_given_hash_mismatch_is_refused_and_allow_stale_records_it(self):
        wrong = "0" * 64
        with self.assertRaises(memory.MemoryStoreError) as caught:
            self.add(kind="decision", text="drifted", sources=[{"path": "alpha.md",
                                                               "sha256": wrong}])
        self.assertIn("Source drift", str(caught.exception))
        self.assertFalse(self.records.exists())
        stored = self.add(kind="decision", text="drifted",
                          sources=[{"path": "alpha.md", "sha256": wrong}], allow_stale=True)
        self.assertEqual(stored["sources"][0]["sha256"], wrong)
        self.assertEqual(len(self.lines()), 1)
        packet = memory.resume(self.vault)
        self.assertEqual(packet["stale"][0]["current_sha256"], self.digest(self.alpha))

    def test_sources_outside_excluded_or_symlinked_are_refused(self):
        link = self.vault / "link.md"
        try:
            link.symlink_to(self.alpha)
        except OSError:  # pragma: no cover - platform without symlinks
            self.skipTest("symlinks unavailable")
        cases = {
            "../outside.md": "escapes the vault",
            str(self.alpha): "Source path",
            "private/secret.md": "Excluded source",
            "link.md": "Symlink",
            "notes/missing.md": "not a file",
        }
        for name, expected in cases.items():
            with self.subTest(source=name):
                with self.assertRaises(memory.MemoryStoreError) as caught:
                    self.add(kind="note", text=f"about {name}", sources=[name])
                self.assertIn(expected, str(caught.exception))
        self.assertFalse(self.records.exists())  # nothing was appended

    def test_bad_kind_state_text_and_supersedes_are_refused(self):
        with self.assertRaises(memory.MemoryStoreError):
            self.add(kind="rumour", text="nope")
        with self.assertRaises(memory.MemoryStoreError):
            self.add(kind="note", text="nope", state="final")
        with self.assertRaises(memory.MemoryStoreError):
            self.add(kind="note", text="   ")
        with self.assertRaises(memory.MemoryStoreError):
            self.add(kind="note", text="orphan", supersedes="m-0000000000000000")
        with self.assertRaises(memory.MemoryStoreError):
            self.add(kind="note", text="bad id", supersedes="not-an-id")
        self.assertFalse(self.records.exists())

    def test_environment_supplies_tool_and_session(self):
        with patch.dict(os.environ, {"CONTEXT_LAYER_TOOL": "codex",
                                     "CONTEXT_LAYER_SESSION": "env-session"}):
            stored = self.add(kind="note", text="from the environment")
            explicit = self.add(kind="note", text="explicit wins", tool="claude-code",
                                session="arg-session")
        self.assertEqual((stored["tool"], stored["session"]), ("codex", "env-session"))
        self.assertEqual((explicit["tool"], explicit["session"]),
                         ("claude-code", "arg-session"))
        with patch.dict(os.environ, {}, clear=True):
            fallback = self.add(kind="note", text="no environment, no argument")
        self.assertEqual((fallback["tool"], fallback["session"]), ("cli", None))

    # -- mirror -----------------------------------------------------------
    def test_mirror_is_regenerated_on_every_write(self):
        first = self.add(kind="decision", text="mirror me", sources=["alpha.md"])
        text = self.mirror.read_text(encoding="utf-8")
        self.assertIn("Derived file", text)
        self.assertIn("edits made here are lost", text)
        self.assertIn(first["id"], text)
        self.assertIn("mirror me", text)
        self.assertIn("## Decisions", text)
        self.assertIn("[alpha.md](../../alpha.md)", text)
        self.assertIn(self.digest(self.alpha)[:12], text)
        second = self.add(kind="task", text="and me", state="approved")
        text = self.mirror.read_text(encoding="utf-8")
        self.assertIn(second["id"], text)
        self.assertIn("## Tasks", text)
        self.assertIn("Open.", text)
        self.assertIn(first["id"], text)

    # -- verify -----------------------------------------------------------
    def test_verify_passes_then_fails_on_a_corrupt_line(self):
        self.add(kind="note", text="healthy one", sources=["alpha.md"])
        self.add(kind="note", text="healthy two")
        self.assertEqual(memory.verify(self.vault), [])
        with self.records.open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        problems = memory.verify(self.vault)
        self.assertTrue(any("invalid JSON" in problem for problem in problems), problems)
        with self.assertRaises(memory.MemoryStoreError):
            memory.load(self.vault)  # reading refuses rather than dropping the line

    def test_verify_fails_on_a_broken_chain_and_an_edited_line(self):
        self.add(kind="note", text="one")
        self.add(kind="note", text="two")
        self.add(kind="note", text="three")
        kept = self.lines()
        del kept[1]
        self.write_lines(kept)
        self.assertTrue(any("prev is" in problem for problem in memory.verify(self.vault)))
        edited = self.lines()
        edited[0]["text"] = "rewritten in place"
        self.write_lines(edited)
        self.assertTrue(any("does not match its id" in problem
                            for problem in memory.verify(self.vault)))

    def test_verify_reports_duplicate_ids(self):
        self.add(kind="note", text="only one")
        line = self.records.read_text(encoding="utf-8")
        self.records.write_text(line + line, encoding="utf-8")
        self.assertTrue(any("duplicate id" in problem for problem in memory.verify(self.vault)))

    # -- concurrency ------------------------------------------------------
    def test_concurrent_writers_lose_no_line_and_keep_the_chain(self):
        context = multiprocessing.get_context("spawn")
        payloads = [(str(self.vault), f"w{index}", PER_WORKER) for index in range(WORKERS)]
        with context.Pool(WORKERS) as pool:
            self.assertEqual(len(pool.map(write_batch, payloads)), WORKERS)
        records = self.lines()
        self.assertEqual(len(records), WORKERS * PER_WORKER)
        self.assertEqual(len({r["id"] for r in records}), WORKERS * PER_WORKER)
        self.assertIsNone(records[0]["prev"])
        for previous, current in zip(records, records[1:]):
            self.assertEqual(current["prev"], previous["id"])
        self.assertEqual(memory.verify(self.vault), [])
        self.assertIn(records[-1]["id"], self.mirror.read_text(encoding="utf-8"))

    def test_fallback_lock_serialises_concurrent_processes(self):
        # Every child replaces fcntl with None, so only the O_EXCL lock file
        # stands between the writers.
        context = multiprocessing.get_context("spawn")
        payloads = [(str(self.vault), f"f{index}", FALLBACK_PER_WORKER)
                    for index in range(FALLBACK_WORKERS)]
        with context.Pool(FALLBACK_WORKERS) as pool:
            self.assertEqual(len(pool.map(write_batch_without_fcntl, payloads)),
                             FALLBACK_WORKERS)
        records = self.lines()
        self.assertEqual(len(records), FALLBACK_WORKERS * FALLBACK_PER_WORKER)
        self.assertEqual(len({r["id"] for r in records}), len(records))
        for previous, current in zip(records, records[1:]):
            self.assertEqual(current["prev"], previous["id"])
        self.assertEqual(memory.verify(self.vault), [])
        self.assertFalse((self.records.parent / memory.LOCK_NAME).exists())

    def test_fallback_lock_times_out_on_a_stale_lock_file(self):
        self.add(kind="note", text="before the crash")
        (self.records.parent / memory.LOCK_NAME).write_text("")
        with patch.object(memory, "fcntl", None), patch.object(memory, "LOCK_TIMEOUT", 0.2):
            with self.assertRaises(memory.MemoryStoreError) as caught:
                self.add(kind="note", text="blocked")
        self.assertIn("delete that file", str(caught.exception))
        self.assertEqual(len(self.lines()), 1)

    # -- CLI --------------------------------------------------------------
    def test_cli_json_outputs_parse(self):
        added = self.cli("add", str(self.vault), "--kind", "decision", "--text",
                         "cli decision", "--source", "alpha.md", "--state", "approved",
                         "--json")
        self.assertEqual(added.returncode, 0, added.stderr)
        stored = json.loads(added.stdout)
        self.assertFalse(stored["duplicate"])
        self.assertEqual(stored["sources"][0]["sha256"], self.digest(self.alpha))

        again = self.cli("add", str(self.vault), "--kind", "decision", "--text",
                         "cli decision", "--source", "alpha.md", "--state", "approved",
                         "--json")
        self.assertTrue(json.loads(again.stdout)["duplicate"])
        self.assertEqual(len(self.lines()), 1)

        listed = self.cli("list", str(self.vault), "--kind", "decision", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual([r["id"] for r in json.loads(listed.stdout)["records"]],
                         [stored["id"]])

        packet = self.cli("resume", str(self.vault), "--json")
        self.assertEqual(packet.returncode, 0, packet.stderr)
        parsed = json.loads(packet.stdout)
        self.assertEqual(set(parsed), {"generated_at", "vault", "records", "stale",
                                       "open_tasks", "conflicts", "closed_by_mention"})
        self.assertEqual(parsed["vault"], self.vault.name)
        self.assertEqual(parsed["stale"], [])

        plain = self.cli("resume", str(self.vault))
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertIn(stored["id"], plain.stdout)
        self.assertNotIn(str(self.vault), plain.stdout)  # no absolute paths in output

        healthy = self.cli("verify", str(self.vault))
        self.assertEqual(healthy.returncode, 0, healthy.stderr)
        self.assertIn("chain intact", healthy.stdout)
        self.assertIn("no fork", healthy.stdout)

    def test_cli_source_hash_suffix_and_visible_failures(self):
        good = self.cli("add", str(self.vault), "--kind", "note", "--text", "pinned",
                        "--source", f"alpha.md@{self.digest(self.alpha)}", "--json")
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual(json.loads(good.stdout)["sources"][0]["path"], "alpha.md")

        drifted = self.cli("add", str(self.vault), "--kind", "note", "--text", "drifted",
                           "--source", f"alpha.md@{'0' * 64}")
        self.assertEqual(drifted.returncode, 1)
        self.assertEqual(drifted.stdout, "")
        self.assertIn("Source drift", drifted.stderr)

        allowed = self.cli("add", str(self.vault), "--kind", "note", "--text", "drifted",
                           "--source", f"alpha.md@{'0' * 64}", "--allow-stale", "--json")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(json.loads(allowed.stdout)["sources"][0]["sha256"], "0" * 64)

        missing = self.cli("add", str(self.vault / "nope"), "--kind", "note", "--text", "x")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("Vault not found", missing.stderr)

        broken = self.cli("verify", str(self.vault))
        self.assertEqual(broken.returncode, 1)
        self.assertIn("stale source", broken.stderr)

    def test_cli_reads_tool_and_session_from_the_environment(self):
        result = self.cli("add", str(self.vault), "--kind", "note", "--text", "env test",
                          "--json", env={"CONTEXT_LAYER_TOOL": "codex",
                                         "CONTEXT_LAYER_SESSION": "cli-session"})
        self.assertEqual(result.returncode, 0, result.stderr)
        stored = json.loads(result.stdout)
        self.assertEqual((stored["tool"], stored["session"]), ("codex", "cli-session"))

    def test_cli_verify_without_a_store_is_not_an_error(self):
        result = self.cli("verify", str(self.vault))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nothing to verify", result.stdout)

    def test_cli_closes_supersedes_merge_and_allow_fork(self):
        task = self.add(kind="task", text="write the guide")
        closed = self.cli("add", str(self.vault), "--kind", "result", "--text", "guide done",
                          "--closes", task["id"], "--json")
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertEqual(json.loads(closed.stdout)["closes"], [task["id"]])
        self.assertNotIn(task["id"], self.open_ids())
        wrong = self.cli("add", str(self.vault), "--kind", "note", "--text", "n",
                         "--closes", task["id"])
        self.assertEqual(wrong.returncode, 1)
        self.assertIn("Only a result closes tasks", wrong.stderr)

        base = self.add(kind="decision", text="cap 10")
        left = self.add(kind="decision", text="cap 12", supersedes=base["id"])
        refused = self.cli("add", str(self.vault), "--kind", "decision", "--text", "cap 15",
                           "--supersedes", base["id"])
        self.assertEqual(refused.returncode, 1)
        self.assertIn(f"--supersedes {left['id']}", refused.stderr)
        forked = self.cli("add", str(self.vault), "--kind", "decision", "--text", "cap 15",
                          "--supersedes", base["id"], "--allow-fork", "--json")
        self.assertEqual(forked.returncode, 0, forked.stderr)
        right = json.loads(forked.stdout)
        self.assertEqual(self.cli("verify", str(self.vault)).returncode, 1)
        resumed = self.cli("resume", str(self.vault))
        self.assertIn("Conflicts (1)", resumed.stdout)
        merged = self.cli("add", str(self.vault), "--kind", "decision", "--text", "cap 15",
                          "--supersedes", left["id"], "--supersedes", right["id"], "--json")
        self.assertEqual(merged.returncode, 0, merged.stderr)
        self.assertEqual(json.loads(merged.stdout)["supersedes"],
                         sorted([left["id"], right["id"]]))
        self.assertEqual(self.cli("verify", str(self.vault)).returncode, 0)

    def test_cli_duplicate_with_another_state_prints_the_supersedes_hint(self):
        draft = self.add(kind="decision", text="Ship 0.4 in October.")
        again = self.cli("add", str(self.vault), "--kind", "decision", "--text",
                         "Ship 0.4 in October.", "--state", "approved")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("duplicate, nothing appended", again.stdout)
        self.assertIn(f"--supersedes {draft['id']}", again.stdout)


class LineModel(Base):
    """C-06: a line ends at "\\n" only; no character inside a record ends it."""

    SPECIALS = [" ", " ", "\u0085", "\x1c", "\x1d", "\x1e", "\x0c", "\x0b", "\r"]

    def test_line_separators_inside_text_never_split_a_record(self):
        texts = [f"left{ch}right" for ch in self.SPECIALS]
        for text in texts:
            self.add(kind="note", text=text)
        raw = self.records.read_bytes()
        self.assertTrue(raw.isascii())
        self.assertEqual(raw.count(b"\n"), len(texts))
        # Even a reader that splits on every Unicode line break sees one record per line.
        self.assertEqual(len(raw.decode("ascii").splitlines()), len(texts))
        packet = memory.resume(self.vault, limit=50)
        self.assertEqual(sorted(r["text"] for r in packet["records"]), sorted(texts))
        self.assertEqual(memory.verify(self.vault), [])
        after = self.add(kind="note", text="after the separators")
        self.assertEqual(after["prev"], self.lines()[-2]["id"])
        self.assertEqual(memory.verify(self.vault), [])
        self.assertIn("after the separators", self.mirror.read_text(encoding="utf-8"))

    def test_a_store_written_with_raw_separators_is_read_and_extended(self):
        # 0.3 wrote JSON with ensure_ascii=False, so U+2028/U+0085 sit raw in the file.
        text = "pasted from a PDF\u0085page end"
        item = legacy_record(None, "decision", text)
        self.write_lines([item], ensure_ascii=False)
        self.assertIn(" ".encode("utf-8"), self.records.read_bytes())
        self.assertEqual(memory.verify(self.vault), [])
        self.assertEqual(memory.resume(self.vault)["records"][0]["text"], text)
        after = self.add(kind="note", text="next record")
        self.assertEqual(after["prev"], item["id"])
        self.assertEqual(memory.verify(self.vault), [])

    def test_crlf_line_endings_are_read_and_extended(self):
        self.add(kind="note", text="one")
        self.add(kind="note", text="two")
        self.records.write_bytes(self.records.read_bytes().replace(b"\n", b"\r\n"))
        self.assertEqual(memory.verify(self.vault), [])
        self.assertEqual(len(memory.load(self.vault)), 2)
        self.add(kind="note", text="three")
        self.assertEqual(len(memory.load(self.vault)), 3)
        self.assertEqual(memory.verify(self.vault), [])

    def test_line_numbers_follow_line_feeds(self):
        self.add(kind="note", text="one")
        with self.records.open("a", encoding="utf-8") as handle:
            handle.write("{broken\n")
        problems = memory.verify(self.vault)
        self.assertTrue(any(p.startswith("line 2: invalid JSON") for p in problems), problems)


class FormatTwo(Base):
    """C-08, C-21, C-22 and 4.5: supersession-aware memory, format 2."""

    def test_new_records_are_format_2_with_sorted_sources(self):
        stored = self.add(kind="note", text="both", sources=["notes/beta.md", "alpha.md"])
        self.assertEqual(stored["format_version"], 2)
        self.assertEqual([s["path"] for s in stored["sources"]], ["alpha.md", "notes/beta.md"])
        self.assertEqual(stored["id"], memory.record_id("note", " both ",
                                                        list(reversed(stored["sources"])), None))
        self.assertEqual(memory.verify(self.vault), [])

    def test_reordered_sources_are_the_same_record(self):  # audit M4
        first = self.add(kind="note", text="rests on both", sources=["alpha.md", "notes/beta.md"])
        second = self.add(kind="note", text="rests on both",
                          sources=["notes/beta.md", "alpha.md"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(self.lines()), 1)

    def test_a_format_1_record_with_unsorted_sources_is_still_a_duplicate(self):
        sources = [{"path": "notes/beta.md", "sha256": self.digest(self.beta)},
                   {"path": "alpha.md", "sha256": self.digest(self.alpha)}]
        item = legacy_record(None, "note", "legacy order", sources)
        self.write_lines([item])
        self.assertEqual(memory.verify(self.vault), [])  # recomputed with format 1 rules
        again = self.add(kind="note", text="legacy order", sources=["alpha.md", "notes/beta.md"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["id"], item["id"])
        self.assertEqual(len(self.lines()), 1)

    def test_readopting_superseded_content_is_refused_with_the_exact_command(self):  # M2
        first = self.add(kind="decision", text="Use FTS by default.", sources=["alpha.md"])
        second = self.add(kind="decision", text="Use synaptic by default.",
                          supersedes=first["id"])
        with self.assertRaises(memory.MemoryStoreError) as caught:
            self.add(kind="decision", text="Use FTS by default.", sources=["alpha.md"])
        message = str(caught.exception)
        self.assertIn(first["id"], message)
        self.assertIn(f"--supersedes {second['id']}", message)
        self.assertEqual(len(self.lines()), 2)
        command = shlex.split(message.rsplit("in force: ", 1)[1])
        self.assertEqual(command[:4], ["context-layer", "memory", "add", "<vault>"])
        done = self.run_cli(*[str(self.vault) if arg == "<vault>" else arg
                              for arg in command[1:]])
        self.assertEqual(done.returncode, 0, done.stderr)
        records = memory.resume(self.vault)["records"]
        self.assertEqual([r["text"] for r in records], ["Use FTS by default."])
        self.assertEqual(records[0]["supersedes"], second["id"])

    def test_a_state_change_without_supersedes_returns_the_stored_draft(self):  # M3
        self.add(kind="decision", text="Ship 0.4 in October.", state="draft")
        again = self.add(kind="decision", text="Ship 0.4 in October.", state="approved")
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["state"], "draft")
        self.assertTrue(again["in_force"])

    def test_superseding_a_superseded_record_is_refused_without_allow_fork(self):
        base = self.add(kind="decision", text="Budget cap 10k.")
        head = self.add(kind="decision", text="Budget cap 12k.", supersedes=base["id"])
        with self.assertRaises(memory.MemoryStoreError) as caught:
            self.add(kind="decision", text="Budget cap 15k.", supersedes=base["id"])
        self.assertIn(f"--supersedes {head['id']}", str(caught.exception))
        self.assertIn("--allow-fork", str(caught.exception))
        with self.assertRaises(memory.MemoryStoreError):  # a merge needs heads in force
            self.add(kind="decision", text="merge", supersedes=[base["id"], head["id"]])
        self.assertEqual(len(self.lines()), 2)

    def test_a_fork_is_one_verify_problem_and_one_conflict_until_merged(self):  # M10
        base = self.add(kind="decision", text="Budget cap 10k.")
        left = self.add(kind="decision", text="Budget cap 12k.", supersedes=base["id"])
        right = self.add(kind="decision", text="Budget cap 15k.", supersedes=base["id"],
                         allow_fork=True)
        problems = memory.verify(self.vault)
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith(f"fork: {base['id']}"))
        packet = memory.resume(self.vault)
        self.assertEqual(packet["conflicts"],
                         [{"target": base["id"], "heads": [left["id"], right["id"]]}])
        mirror = self.mirror.read_text(encoding="utf-8")
        self.assertIn("## Conflicts", mirror)
        self.assertIn(f"Superseded by `{left['id']}`, `{right['id']}`.", mirror)
        merged = self.add(kind="decision", text="Budget cap 15k, agreed.",
                          supersedes=[right["id"], left["id"]])
        self.assertEqual(merged["supersedes"], sorted([left["id"], right["id"]]))
        self.assertEqual(memory.resume(self.vault)["conflicts"], [])
        self.assertEqual(memory.verify(self.vault), [])

    def test_a_retracted_result_no_longer_closes_its_task(self):  # M12
        task = self.add(kind="task", text="Write the migration guide.")
        result = self.add(kind="result", text="Guide finished.", closes=[task["id"]])
        self.assertNotIn(task["id"], self.open_ids())
        self.add(kind="note", text="Retraction: the guide is not finished.",
                 supersedes=result["id"])
        packet = memory.resume(self.vault)
        self.assertIn(task["id"], [r["id"] for r in packet["open_tasks"]])
        self.assertNotIn(result["id"], [r["id"] for r in packet["records"]])

    def test_a_result_that_only_mentions_a_task_leaves_it_open(self):  # M12b
        task = self.add(kind="task", text="Review the index format.")
        self.add(kind="result", text=f"Could not do {task['id']}: blocked on access.")
        packet = memory.resume(self.vault)
        self.assertIn(task["id"], [r["id"] for r in packet["open_tasks"]])
        self.assertEqual(packet["closed_by_mention"], [])

    def test_a_format_1_result_that_quotes_a_task_id_closes_it_by_mention(self):
        task = legacy_record(None, "task", "Ship the index.")
        result = legacy_record(task["id"], "result", f"finished {task['id']}")
        self.write_lines([task, result])
        packet = memory.resume(self.vault)
        self.assertEqual(packet["open_tasks"], [])
        self.assertEqual(packet["closed_by_mention"],
                         [{"task": task["id"], "result": result["id"]}])
        self.assertEqual(memory.verify(self.vault), [])
        memory.rebuild_mirror(self.vault)
        self.assertIn("Closed by mention", self.mirror.read_text(encoding="utf-8"))

    def test_closes_names_tasks_in_force_on_results_only(self):
        task = self.add(kind="task", text="t")
        decision = self.add(kind="decision", text="d")
        cases = (({"kind": "note", "text": "n", "closes": [task["id"]]}, "Only a result"),
                 ({"kind": "result", "text": "r1", "closes": [decision["id"]]}, "not a task"),
                 ({"kind": "result", "text": "r2", "closes": ["m-" + "0" * 16]},
                  "does not hold"))
        for kwargs, fragment in cases:
            with self.subTest(fragment=fragment):
                with self.assertRaises(memory.MemoryStoreError) as caught:
                    self.add(**kwargs)
                self.assertIn(fragment, str(caught.exception))
        newer = self.add(kind="task", text="t, rescoped", supersedes=task["id"])
        with self.assertRaises(memory.MemoryStoreError) as caught:
            self.add(kind="result", text="r3", closes=[task["id"]])
        self.assertIn("no longer in force", str(caught.exception))
        self.assertIn(newer["id"], str(caught.exception))

    def test_verify_recomputes_each_line_with_its_own_format(self):
        sources = [{"path": "notes/beta.md", "sha256": self.digest(self.beta)},
                   {"path": "alpha.md", "sha256": self.digest(self.alpha)}]
        legacy = legacy_record(None, "decision", "legacy, unsorted", sources)
        self.write_lines([legacy])
        self.add(kind="note", text="format two", sources=["notes/beta.md", "alpha.md"])
        self.assertEqual(memory.verify(self.vault), [])
        lines = self.lines()
        lines[1]["text"] = "format two, edited"
        self.write_lines(lines)
        self.assertTrue(any(p.startswith("line 2:") and "does not match its id" in p
                            for p in memory.verify(self.vault)))
        lines = self.lines()
        lines[0]["closes"] = []
        self.write_lines(lines)
        self.assertTrue(any("closes is a format 2 field" in p for p in memory.verify(self.vault)))

    def test_a_record_from_a_newer_format_is_refused(self):
        self.add(kind="note", text="first")
        with self.records.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"format_version": memory.FORMAT_VERSION + 1,
                                     "id": "m-" + "0" * 16}) + "\n")
        with self.assertRaisesRegex(memory.MemoryStoreError,
                                    f"format_version {memory.FORMAT_VERSION + 1}"):
            memory.load(self.vault)


class MovedSources(Base):
    """C-23 and 4.6: a renamed source is found by its hash and rebound, never edited."""

    def test_a_renamed_source_is_reported_with_its_new_path_and_rebound(self):  # M11
        decision = self.add(kind="decision", text="Alpha is canonical.", sources=["alpha.md"])
        recorded = decision["sources"][0]["sha256"]
        self.alpha.rename(self.vault / "notes" / "alpha-renamed.md")
        stale = memory.resume(self.vault)["stale"]
        self.assertEqual(stale[0]["moved_to"], [])  # no index knows the new path yet
        self.build_index()                           # the manifest now lists it
        stale = memory.resume(self.vault)["stale"]
        self.assertEqual(stale, [{"id": decision["id"], "path": "alpha.md",
                                  "recorded_sha256": recorded, "current_sha256": None,
                                  "moved_to": ["notes/alpha-renamed.md"]}])
        problems = memory.verify(self.vault)
        self.assertEqual(len(problems), 1)
        self.assertIn("notes/alpha-renamed.md", problems[0])
        self.assertIn(f"memory rebind <vault> {decision['id']}", problems[0])
        before = self.records.read_bytes()
        done = self.cli("rebind", str(self.vault), decision["id"], "--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        report = json.loads(done.stdout)
        self.assertTrue(self.records.read_bytes().startswith(before))  # nothing was edited
        self.assertEqual(len(self.lines()), 2)
        rebound = report["record"]
        self.assertEqual(rebound["supersedes"], decision["id"])
        self.assertEqual(rebound["sources"], [{"path": "notes/alpha-renamed.md",
                                               "sha256": recorded}])
        self.assertEqual(report["changes"], [{"from": "alpha.md",
                                              "to": "notes/alpha-renamed.md"}])
        packet = memory.resume(self.vault)
        self.assertEqual([r["id"] for r in packet["records"]], [rebound["id"]])
        self.assertEqual(packet["stale"], [])
        self.assertEqual(memory.verify(self.vault), [])

    def test_rebind_checks_bytes_and_asks_when_copies_are_ambiguous(self):
        decision = self.add(kind="decision", text="Alpha holds.", sources=["alpha.md"])
        data = self.alpha.read_bytes()
        (self.vault / "notes" / "copy-one.md").write_bytes(data)
        (self.vault / "notes" / "copy-two.md").write_bytes(data)
        self.alpha.unlink()
        self.build_index()
        with self.assertRaises(memory.MemoryStoreError) as caught:
            memory.rebind(self.vault, decision["id"])
        self.assertIn("2 copies", str(caught.exception))
        with self.assertRaises(memory.MemoryStoreError) as caught:
            memory.rebind(self.vault, decision["id"], moves={"alpha.md": "notes/beta.md"})
        self.assertIn("does not hold the bytes", str(caught.exception))
        report = memory.rebind(self.vault, decision["id"],
                               moves={"alpha.md": "notes/copy-two.md"})
        self.assertEqual(report["changes"], [{"from": "alpha.md", "to": "notes/copy-two.md"}])
        with self.assertRaises(memory.MemoryStoreError) as caught:
            memory.rebind(self.vault, decision["id"])
        self.assertIn(report["record"]["id"], str(caught.exception))  # rebind the head
        with self.assertRaises(memory.MemoryStoreError) as caught:
            memory.rebind(self.vault, report["record"]["id"])
        self.assertIn("has moved", str(caught.exception))


class Repair(Base):
    """C-24: a torn last line is moved aside; nothing else is ever changed."""

    def test_a_torn_last_line_blocks_writes_until_repair_moves_it_aside(self):
        self.add(kind="note", text="one")
        self.add(kind="note", text="two")
        before = self.records.read_bytes()
        fragment = b'{"id": "m-0123456789abcdef", "prev": "m-\xe2\x80'  # half a UTF-8 character
        with self.records.open("ab") as handle:
            handle.write(fragment)
        with self.assertRaises(memory.MemoryStoreError) as caught:
            self.add(kind="note", text="after the crash")
        self.assertIn("memory repair", str(caught.exception))

        dry = self.cli("repair", str(self.vault), "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("would move the torn last line", dry.stdout)
        self.assertEqual(self.records.read_bytes(), before + fragment)
        self.assertEqual(list(self.records.parent.glob("records.jsonl.torn-*")), [])

        done = self.cli("repair", str(self.vault))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.records.read_bytes(), before)
        torn = list(self.records.parent.glob("records.jsonl.torn-*"))
        self.assertEqual(len(torn), 1)
        self.assertEqual(torn[0].read_bytes(), fragment)
        self.assertEqual(memory.verify(self.vault), [])
        self.assertFalse(self.add(kind="note", text="after the crash")["duplicate"])

    def test_repair_touches_nothing_but_a_torn_last_line(self):
        self.add(kind="note", text="one")
        self.add(kind="note", text="two")
        lines = self.records.read_text(encoding="utf-8").split("\n")
        broken = "\n".join([lines[0], "{not json", *lines[1:]])
        self.records.write_text(broken, encoding="utf-8")
        report = memory.repair(self.vault)
        self.assertFalse(report["torn"])
        self.assertTrue(any("invalid JSON" in p for p in report["problems"]))
        self.assertEqual(self.records.read_text(encoding="utf-8"), broken)
        done = self.cli("repair", str(self.vault))
        self.assertEqual(done.returncode, 1)
        self.assertIn("fix the line(s) above by hand", done.stderr)

    def test_a_complete_last_line_without_a_newline_is_not_torn(self):
        self.add(kind="note", text="one")
        self.records.write_bytes(self.records.read_bytes().rstrip(b"\n"))
        report = memory.repair(self.vault)
        self.assertEqual((report["torn"], report["problems"]), (False, []))
        self.add(kind="note", text="two")
        self.assertEqual(len(self.lines()), 2)
        self.assertEqual(memory.verify(self.vault), [])

    def test_mirror_rebuilds_memory_md_after_a_hand_edit(self):
        stored = self.add(kind="decision", text="keep me visible")
        self.mirror.unlink()
        done = self.cli("mirror", str(self.vault))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(stored["id"], self.mirror.read_text(encoding="utf-8"))


class NotesMirror(Base):
    """4.4 and C-11: an opt-in visible mirror that never becomes evidence."""

    def notes(self, folder="Memory"):
        root = self.vault / folder
        top = {path.stem: frontmatter(path) for path in root.glob("*.md")}
        old = {path.stem: frontmatter(path) for path in (root / "superseded").glob("*.md")}
        return top, old

    def expected(self):
        records = memory.load(self.vault)
        chain = memory._Chain(records)
        wanted = [r for r in records if r["kind"] in ("decision", "task")]
        return ({r["id"] for r in wanted if chain.in_force(r["id"])},
                {r["id"] for r in wanted if not chain.in_force(r["id"])})

    def test_notes_mirror_matches_the_store(self):
        first = self.add(kind="decision", text="Use FTS by default.", sources=["alpha.md"])
        second = self.add(kind="decision", text="Use synaptic by default.",
                          supersedes=first["id"])
        open_task = self.add(kind="task", text="Write the docs.")
        done_task = self.add(kind="task", text="Ship the index.")
        self.add(kind="result", text="Index shipped.", closes=[done_task["id"]])
        self.add(kind="note", text="A note is not mirrored.")
        run = self.cli("mirror", str(self.vault), "--notes", "Memory")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("notice: add `Memory` to exclude_prefixes", run.stdout)
        top, old = self.notes()
        in_force, superseded = self.expected()
        self.assertEqual(set(top), in_force)
        self.assertEqual(set(old), superseded)
        self.assertEqual(set(top), {second["id"], open_task["id"], done_task["id"]})
        self.assertEqual(top[second["id"]]["supersedes"], f"[[{first['id']}]]")
        self.assertEqual(old[first["id"]]["superseded_by"], [f"[[{second['id']}]]"])
        self.assertEqual(top[open_task["id"]]["status"], "open")
        self.assertEqual(top[done_task["id"]]["status"], "closed")
        self.assertTrue(all(note["derived"] is True for note in [*top.values(), *old.values()]))
        self.assertFalse(old[first["id"]]["stale"])
        body = (self.vault / "Memory" / "superseded" / f"{first['id']}.md").read_text()
        self.assertIn(f"[[alpha.md]] `{self.digest(self.alpha)[:12]}` current", body)
        self.assertIn("Memory/", json.loads(self.routes.read_text())["exclude_prefixes"])

        self.alpha.write_text("# Alpha\nrewritten\n", encoding="utf-8")
        third = self.add(kind="decision", text="Back to FTS.", supersedes=second["id"])
        self.assertEqual(self.cli("mirror", str(self.vault), "--notes", "Memory").returncode, 0)
        top, old = self.notes()
        in_force, superseded = self.expected()
        self.assertEqual((set(top), set(old)), (in_force, superseded))
        self.assertIn(third["id"], top)
        self.assertIn(second["id"], old)
        self.assertFalse((self.vault / "Memory" / f"{second['id']}.md").exists())
        self.assertTrue(old[first["id"]]["stale"])

    def test_notes_mirror_never_touches_files_it_did_not_write_and_keeps_edits(self):
        task = self.add(kind="task", text="Keep this task visible.")
        other = self.add(kind="task", text="Another task.")
        user = self.vault / "Taken"
        user.mkdir()
        (user / "mine.md").write_text("# Mine\n")
        refused = self.cli("mirror", str(self.vault), "--notes", "Taken")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("did not write", refused.stderr)
        hidden = self.cli("mirror", str(self.vault), "--notes", ".memory")
        self.assertEqual(hidden.returncode, 1)
        self.assertIn("visible folder", hidden.stderr)

        self.assertEqual(self.cli("mirror", str(self.vault), "--notes", "Memory").returncode, 0)
        edited = self.vault / "Memory" / f"{task['id']}.md"
        edited.write_text(edited.read_text() + "\nA person's own line.\n")
        kept = edited.read_bytes()
        again = self.cli("mirror", str(self.vault), "--notes", "Memory")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn(f"kept Memory/{task['id']}.md", again.stdout)
        self.assertEqual(edited.read_bytes(), kept)

        removed = self.cli("mirror", str(self.vault), "--notes", "Memory", "--remove")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(edited.read_bytes(), kept)
        self.assertFalse((self.vault / "Memory" / f"{other['id']}.md").exists())
        self.assertIn("Memory/", json.loads(self.routes.read_text())["exclude_prefixes"])
        edited.unlink()
        self.assertEqual(self.cli("mirror", str(self.vault), "--notes", "Memory",
                                  "--remove").returncode, 0)
        self.assertFalse((self.vault / "Memory").exists())
        self.assertNotIn("Memory/", json.loads(self.routes.read_text())["exclude_prefixes"])

    def test_remove_restores_routes_json_and_the_folder(self):
        self.add(kind="decision", text="Visible for a while.")
        before = json.loads(self.routes.read_text())
        dry = self.cli("mirror", str(self.vault), "--notes", "Memory", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertFalse((self.vault / "Memory").exists())
        self.assertEqual(json.loads(self.routes.read_text()), before)
        self.assertEqual(self.cli("mirror", str(self.vault), "--notes", "Memory").returncode, 0)
        self.assertNotEqual(json.loads(self.routes.read_text()), before)
        done = self.cli("mirror", str(self.vault), "--notes", "Memory", "--remove")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(self.routes.read_text()), before)
        self.assertFalse((self.vault / "Memory").exists())

    def test_an_exclusion_removed_by_hand_is_not_added_again(self):
        stored = self.add(kind="decision", text="Searchable on purpose.")
        self.assertEqual(self.cli("mirror", str(self.vault), "--notes", "Memory").returncode, 0)
        config = json.loads(self.routes.read_text())
        config["exclude_prefixes"].remove("Memory/")
        self.routes.write_text(json.dumps(config))
        again = self.cli("mirror", str(self.vault), "--notes", "Memory")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("removed by hand", again.stdout)
        self.assertNotIn("Memory/", json.loads(self.routes.read_text())["exclude_prefixes"])
        note = (self.vault / "Memory" / f"{stored['id']}.md").read_text()
        self.assertIn("is not excluded from retrieval", note)

    def test_notes_are_never_evidence_unless_the_exclusion_is_removed(self):
        self.add(kind="decision", text="Zanzibar quorum is seven members.")
        self.assertEqual(self.cli("mirror", str(self.vault), "--notes", "Memory").returncode, 0)
        self.assertEqual(self.run_cli("index", str(self.vault)).returncode, 0)
        hidden = self.run_cli("search", str(self.vault), "--prompt", "zanzibar quorum")
        self.assertEqual(hidden.returncode, 0, hidden.stderr)
        packet = json.loads(hidden.stdout)
        self.assertEqual(packet["status"], "NOT_FOUND")
        self.assertNotIn("Memory/", hidden.stdout)

        config = json.loads(self.routes.read_text())
        config["exclude_prefixes"].remove("Memory/")
        self.routes.write_text(json.dumps(config))
        self.assertEqual(self.run_cli("index", str(self.vault)).returncode, 0)
        shown = self.run_cli("search", str(self.vault), "--prompt", "zanzibar quorum")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        paths = [item["source_path"] for item in json.loads(shown.stdout)["evidence"]]
        self.assertTrue(paths and all(path.startswith("Memory/") for path in paths), paths)

    def test_notes_mirror_needs_routes_json(self):
        self.add(kind="task", text="t")
        self.routes.unlink()
        done = self.cli("mirror", str(self.vault), "--notes", "Memory")
        self.assertEqual(done.returncode, 1)
        self.assertIn("context-layer init", done.stderr)
        self.assertFalse((self.vault / "Memory").exists())


class SessionRecord(Base):
    """4.7: the opt-in session bill of materials holds ids, paths and hashes only."""

    def session_file(self, name="s-bom"):
        return self.vault / ".context" / "sessions" / f"{name}.jsonl"

    def rows(self, name="s-bom"):
        path = self.session_file(name)
        return [json.loads(line) for line in path.read_text().split("\n") if line.strip()]

    def events(self, rows):
        return sorted((row["event"], row.get("id") or row.get("path")) for row in rows)

    def test_every_event_is_written_once_and_no_text_is_stored(self):
        secret_text = "Quarterly plan: tell nobody."
        with patch.dict(os.environ, {"CONTEXT_LAYER_SESSION": "s-bom"}):
            task = self.add(kind="task", text=secret_text, sources=["alpha.md"])
            first = self.rows()
            self.assertEqual(self.events(first), [("memory.write", task["id"]),
                                                  ("source.check", "alpha.md")])
            self.add(kind="task", text=secret_text, sources=["alpha.md"])
            second = self.rows()[len(first):]
            self.assertEqual(self.events(second), [("memory.duplicate", task["id"]),
                                                   ("source.check", "alpha.md")])
            memory.resume(self.vault)  # the task is both a record and an open task
            third = self.rows()[len(first) + len(second):]
            self.assertEqual(self.events(third), [("memory.read", task["id"]),
                                                  ("source.check", "alpha.md")])
        data = self.session_file().read_bytes()
        self.assertNotIn(b"Quarterly", data)
        self.assertNotIn(str(self.vault).encode(), data)
        for row in self.rows():
            self.assertLessEqual(set(row), BOM_KEYS)
            self.assertEqual((row["schema"], row["session"]), ("session-bom/v1", "s-bom"))
        check = [row for row in self.rows() if row["event"] == "source.check"][0]
        self.assertEqual(check["sha256"], self.digest(self.alpha))
        self.assertEqual(check["current_sha256"], self.digest(self.alpha))

    def test_nothing_is_written_without_the_environment_variable(self):
        self.add(kind="note", text="no session")
        memory.resume(self.vault)
        self.assertFalse((self.vault / ".context" / "sessions").exists())

    def test_an_unusual_session_id_is_hashed_into_a_file_name(self):
        with patch.dict(os.environ, {"CONTEXT_LAYER_SESSION": "../../outside session"}):
            self.add(kind="note", text="odd id")
        files = list((self.vault / ".context" / "sessions").glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].name.startswith("sha256-"))
        self.assertEqual(memory.session_path(self.vault, "../../outside session"), files[0])
        self.assertFalse((self.vault.parent / "outside session.jsonl").exists())

    def test_session_show_renders_stale_flags(self):
        env = {"CONTEXT_LAYER_SESSION": "s-show"}
        added = self.cli("add", str(self.vault), "--kind", "decision", "--text", "rests on alpha",
                         "--source", "alpha.md", "--json", env=env)
        self.assertEqual(added.returncode, 0, added.stderr)
        stored = json.loads(added.stdout)
        self.assertEqual(self.cli("resume", str(self.vault), env=env).returncode, 0)
        self.alpha.write_text("# Alpha\nchanged after the session\n", encoding="utf-8")
        self.add(kind="decision", text="replaced", supersedes=stored["id"])
        shown = self.cli("session", "show", str(self.vault), "s-show")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("STALE now: changed", shown.stdout)
        self.assertIn("superseded by", shown.stdout)
        view = json.loads(self.cli("session", "show", str(self.vault), "s-show",
                                   "--json").stdout)
        self.assertEqual(view["sources"][0]["now"], "changed")
        self.assertFalse(view["sources"][0]["stale_when_checked"])
        self.assertEqual(view["records"][0]["events"], ["memory.write", "memory.read"])
        self.assertFalse(view["records"][0]["in_force"])
        listed = self.cli("session", "list", str(self.vault))
        self.assertIn("s-show", listed.stdout)
        missing = self.cli("session", "show", str(self.vault), "s-none")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("CONTEXT_LAYER_SESSION", missing.stderr)

    def test_a_session_record_that_cannot_be_written_never_fails_the_record(self):
        sessions = self.vault / ".context" / "sessions"
        sessions.write_text("not a folder")
        done = self.cli("add", str(self.vault), "--kind", "note", "--text", "still recorded",
                        env={"CONTEXT_LAYER_SESSION": "s-broken"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("session record", done.stderr)
        self.assertEqual(len(self.lines()), 1)


class Durability(Base):
    """C-30: atomic mirror writes, a durable new store, one import route."""

    def test_memory_md_is_replaced_atomically_with_normal_permissions(self):
        self.add(kind="note", text="one")
        self.add(kind="note", text="two")
        self.assertEqual(list(self.mirror.parent.glob(".MEMORY.md.*")), [])
        mask = os.umask(0)
        os.umask(mask)
        self.assertEqual(stat.S_IMODE(self.mirror.stat().st_mode), 0o666 & ~mask)

    def test_the_folder_is_synced_when_the_store_is_created(self):
        self.records.parent.mkdir(parents=True)
        with patch.object(memory, "_fsync_dir") as synced:
            memory._append(self.records, {"id": "first"})
            memory._append(self.records, {"id": "second"})
        self.assertEqual([call.args[0] for call in synced.call_args_list],
                         [self.records.parent])
        self.assertEqual(self.records.read_text().count("\n"), 2)

    def test_one_import_route_to_the_source_policy(self):
        from context_layer import health, mcp_server
        self.assertIs(memory._policy(), mcp_server.policy())
        self.assertIs(health._router_modules()[0], mcp_server.policy())


if __name__ == "__main__":
    unittest.main()
