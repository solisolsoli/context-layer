"""Incremental indexing: the updated index must equal a full rebuild.

The property test drives randomized edit sequences over a synthetic vault (add,
modify, frontmatter-only change, rename, delete, touch, empty and non-UTF-8 files,
oversize and excluded files by editing routes.json, unsupported names, same-size
edits that restore the old timestamp). After every step the index that
`build_index.py --incremental` updated in place is compared with the index a
`--full` build writes for the same vault.

What "equal" means here. SQLite page layout and FTS5 segment structure depend on
the history of writes, so file bytes are not compared. Everything a reader can
observe is:

- the `records` table, every column of every row, including the ids (equal-scoring
  hits are ordered by id, so the ids must be the ones a full build assigns);
- `index_meta` except `built_at` (a clock reading), `PRAGMA user_version`, and the
  schema text of every table;
- the full-text index itself, through `fts5vocab` (every term, document, column and
  offset), `records_fts_docsize` and `records_fts_config`, plus the ranked result
  (rowid and bm25 score) of a set of queries, which also exercises FTS5's corpus
  statistics;
- `index-manifest.json` except `built_at`.
"""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(os.environ.get("TEST_REPO_HOME", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO / "router"))
sys.path.insert(0, str(REPO / "eval"))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))
import build_index  # noqa: E402
import retrieve  # noqa: E402
from fixtures import dev_bridge  # noqa: E402
from _portable_helpers import sqlite_connection  # noqa: E402

# Sequences and steps per sequence; raise for a longer soak (for example
# INCREMENTAL_SEEDS=200 python3 tests/test_incremental_index.py).
SEEDS = int(os.environ.get("INCREMENTAL_SEEDS", "10"))
STEPS = int(os.environ.get("INCREMENTAL_STEPS", "22"))
VOCAB = ["alpha", "bravo", "cedar", "delta", "ember", "fjord", "garnet", "harbor", "indigo",
         "juniper", "kestrel", "lantern", "meadow", "nectar", "orchid", "pebble", "quartz",
         "russet", "saffron", "tundra", "umber", "velvet", "willow", "xenon", "yarrow", "zephyr"]
QUERIES = ["alpha", "bravo cedar", "kestrel", "lantern OR orchid", "zephyr willow", "umber",
           "harbor", "alpha bravo cedar delta", "straße", "café"]
NAMES = ["note", "Straße", "café menu", "log 2026-01-02", "a b", "ünï", "x"]
FOLDERS = ["", "", "alpha", "alpha/deep", "beta", "private", "café", "beta/n o"]
EXTENSIONS = [".md", ".md", ".md", ".txt", ".json", ".csv", ".png"]
ROUTES = {"routes": {}, "record_type_allowlist": ["verbatim_text_file"]}
BASE_TIME = 1_700_000_000


def run_build(vault: Path, *flags: str, out: Path = None) -> "tuple[int, str, str]":
    argv = ["--vault", str(vault), *flags] + (["--out", str(out)] if out else [])
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = build_index.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def logical(index: Path) -> dict:
    """Everything a reader can observe of an index and its manifest (module docstring)."""
    connection = sqlite3.connect(index.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        meta = dict(connection.execute("SELECT key, value FROM index_meta"))
        meta.pop("built_at")
        connection.execute("CREATE VIRTUAL TABLE temp.vocab USING "
                           "fts5vocab(main, records_fts, instance)")
        ranked = {}
        for query in QUERIES:
            try:
                ranked[query] = connection.execute(
                    "SELECT rowid, bm25(records_fts) FROM records_fts WHERE records_fts MATCH ?"
                    " ORDER BY bm25(records_fts), rowid", (query,)).fetchall()
            except sqlite3.OperationalError as exc:
                ranked[query] = str(exc)
        names = {}
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'names_fts'").fetchone():
            connection.execute("CREATE VIRTUAL TABLE temp.nvocab USING "
                               "fts5vocab(main, names_fts, instance)")
            names = {
                "rows": connection.execute(
                    "SELECT path, name, aliases, headings FROM names_fts ORDER BY path").fetchall(),
                "postings": connection.execute(
                    "SELECT v.term, n.path, v.col, v.offset FROM nvocab v JOIN names_fts n"
                    " ON n.rowid = v.doc ORDER BY v.term, n.path, v.col, v.offset").fetchall(),
                "ranked": {q: connection.execute(
                    "SELECT path, bm25(names_fts, 10.0, 8.0, 4.0) FROM names_fts"
                    " WHERE names_fts MATCH ? ORDER BY 2, path", (q,)).fetchall()
                    for q in QUERIES if '"' not in q and "OR" not in q}}
        manifest = json.loads(index.with_name(build_index.MANIFEST_NAME).read_text("utf-8"))
        manifest.pop("built_at")
        return {
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "schema": sorted(connection.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                " AND name NOT LIKE '%vocab'")),
            "meta": meta,
            "records": connection.execute("SELECT * FROM records ORDER BY id").fetchall(),
            "docsize": connection.execute(
                "SELECT id, sz FROM records_fts_docsize ORDER BY id").fetchall(),
            "config": connection.execute(
                "SELECT k, v FROM records_fts_config ORDER BY k").fetchall(),
            "postings": connection.execute(
                "SELECT term, doc, col, offset FROM vocab ORDER BY term, doc, col, offset"
            ).fetchall(),
            "ranked": ranked,
            "manifest": manifest,
            "names": names,
        }
    finally:
        connection.close()


def first_difference(left: dict, right: dict) -> str:
    for key in left:
        if left[key] != right[key]:
            a, b = left[key], right[key]
            if isinstance(a, list) and isinstance(b, list):
                for position, (x, y) in enumerate(zip(a, b)):
                    if x != y:
                        return f"{key}[{position}]: incremental {str(x)[:200]} != full {str(y)[:200]}"
                return f"{key}: {len(a)} rows incremental, {len(b)} rows full"
            return f"{key}: incremental {str(a)[:300]} != full {str(b)[:300]}"
    return "no difference"


class Vault:
    """A synthetic vault that a seeded generator edits one operation at a time."""

    def __init__(self, root: Path, rng: random.Random):
        self.root, self.rng, self.clock = root, rng, 0
        (root / ".context").mkdir(parents=True, exist_ok=True)
        self.routes: dict = dict(ROUTES)
        self.save_routes()

    def save_routes(self):
        (self.root / ".context" / "routes.json").write_text(json.dumps(self.routes))

    def stamp(self, path: Path, when: "int | None" = None):
        self.clock += 1
        moment = BASE_TIME + self.clock * 3 if when is None else when
        os.utime(path, (moment, moment))

    def files(self) -> "list[Path]":
        found = []
        for current, directories, names in os.walk(self.root):
            directories[:] = [d for d in directories if d != ".context"]
            found += [Path(current) / n for n in names if (Path(current) / n).is_file()
                      and not (Path(current) / n).is_symlink()]
        return sorted(found)

    def pick(self, suffixes=None) -> "Path | None":
        pool = [p for p in self.files() if suffixes is None or p.suffix in suffixes]
        return self.rng.choice(pool) if pool else None

    def body(self, long: bool = False) -> str:
        lines = []
        size = self.rng.choice([15000, 9000, 6100]) if long else self.rng.randint(1, 400)
        while sum(len(line) + 1 for line in lines) < size:
            lines.append(" ".join(self.rng.choice(VOCAB) for _ in range(self.rng.randint(1, 9))))
        return "\n".join(lines) + self.rng.choice(["\n", "", "\r\n"])

    def note(self) -> str:
        text = self.body(long=self.rng.random() < 0.18)
        if self.rng.random() < 0.5:
            title = self.rng.choice(VOCAB)
            text = f"---\ntitle: {title}\naliases: [{title} note]\n---\n{text}"
        if self.rng.random() < 0.4:
            text = f"# {self.rng.choice(VOCAB)} {self.rng.choice(VOCAB)}\n" + text \
                if not text.startswith("---") else text + f"\n## {self.rng.choice(VOCAB)} plan\n"
        if self.rng.random() < 0.3:
            text += f"\nSee [[{self.rng.choice(NAMES)}]]\n"
        return text

    def put(self, relative: str, data: "str | bytes"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        self.stamp(path)
        return path

    def random_relative(self) -> str:
        folder = self.rng.choice(FOLDERS)
        name = self.rng.choice(NAMES) + str(self.rng.randint(0, 40)) + self.rng.choice(EXTENSIONS)
        return f"{folder}/{name}" if folder else name

    # -- operations -------------------------------------------------------------------

    def op_add(self):
        self.put(self.random_relative(), self.note())

    def op_add_tie(self):
        """Identical content under two paths, so equal-scoring hits must be ordered by id."""
        text = "tie marker tundra tundra\n"
        first, second = sorted(self.rng.sample(["a", "b", "c", "d/x", "d/y", "z"], 2))
        self.put(f"ties/{second}.md", text)
        self.put(f"ties/{first}.md", text)

    def op_modify(self):
        path = self.pick({".md", ".txt", ".json", ".csv"})
        if path:
            text = path.read_bytes().decode("utf-8", "replace")
            if self.rng.random() < 0.5:
                text += "\n" + self.body()
            else:
                text = self.note() if self.rng.random() < 0.3 else text.replace("alpha", "omega")
            path.write_bytes(text.encode("utf-8"))
            self.stamp(path)

    def op_frontmatter_only(self):
        path = self.pick({".md"})
        if path:
            text = path.read_bytes().decode("utf-8", "replace")
            body = text.split("\n---\n", 1)[1] if text.startswith("---\n") and "\n---\n" in text else text
            new = f"---\ntitle: {self.rng.choice(VOCAB)}\ntags: [{self.rng.choice(VOCAB)}]\n---\n"
            path.write_bytes((new + body).encode("utf-8"))
            self.stamp(path)

    def op_rename(self):
        path = self.pick({".md", ".txt"})
        if path:
            target = self.root / self.random_relative()
            if not target.exists() and target.suffix in {".md", ".txt"}:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, target)

    def op_delete(self):
        path = self.pick()
        if path:
            path.unlink()

    def op_touch(self):
        path = self.pick()
        if path:
            self.stamp(path)      # same bytes, new timestamp

    def op_same_size_edit(self):
        """Different bytes, same size, old timestamp restored: only the hash sees it."""
        path = self.pick({".md", ".txt"})
        if path:
            data = path.read_bytes()
            before = path.stat().st_mtime
            if b"alpha" in data:
                path.write_bytes(data.replace(b"alpha", b"omega", 1))
                self.stamp(path, int(before))

    def op_empty(self):
        path = self.pick({".md"})
        if path and self.rng.random() < 0.5:
            path.write_bytes(b"")
            self.stamp(path)
        else:
            self.put(self.random_relative().replace(".png", ".md"), b"")

    def op_not_utf8(self):
        path = self.pick({".md", ".txt", ".csv"})
        blob = b"caf\xe9 alpha \xff\xfe broken\n"
        if path and self.rng.random() < 0.5:
            path.write_bytes(blob)
            self.stamp(path)
        else:
            self.put(self.random_relative(), blob)

    def op_restore_utf8(self):
        for path in self.files():
            if path.suffix in {".md", ".txt", ".csv"} and b"\xff\xfe" in path.read_bytes():
                path.write_bytes(b"repaired alpha bravo\n")
                self.stamp(path)
                return

    def op_unsupported_name(self):
        self.put("back\\slash" + str(self.rng.randint(0, 3)) + ".md", "alpha unsupported\n")

    def op_symlink(self):
        target = self.pick({".md"})
        if target:
            link = self.root / f"link{self.rng.randint(0, 2)}.md"
            if not link.exists() and not link.is_symlink():
                link.symlink_to(target)

    def op_limit(self):
        self.routes["max_file_bytes"] = self.rng.choice([1500, 6100, 20000, 2_000_000])
        self.save_routes()

    def op_exclude(self):
        self.routes["exclude_prefixes"] = self.rng.choice([[], ["private"], ["alpha/deep"],
                                                           ["private", "beta"]])
        self.save_routes()

    def step(self):
        operations = [self.op_add] * 4 + [self.op_modify] * 4 + [
            self.op_frontmatter_only, self.op_frontmatter_only, self.op_rename, self.op_rename,
            self.op_delete, self.op_delete, self.op_touch, self.op_same_size_edit,
            self.op_empty, self.op_not_utf8, self.op_restore_utf8, self.op_unsupported_name,
            self.op_symlink, self.op_limit, self.op_exclude, self.op_add_tie]
        for _ in range(self.rng.randint(1, 4)):
            self.rng.choice(operations)()


class IncrementalEqualsFull(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault_dir = self.root / "vault"
        self.vault_dir.mkdir()
        self.reference = self.root / "reference"
        self.reference.mkdir()

    def index(self) -> Path:
        return self.vault_dir / ".context" / build_index.INDEX_NAME

    extra: "tuple[str, ...]" = ()

    def assert_equal_to_full(self, label: str):
        code, _, err = run_build(self.vault_dir, "--full", *self.extra,
                                 out=self.reference / "index.sqlite")
        self.assertEqual(code, 0, err)
        live, full = logical(self.index()), logical(self.reference / "index.sqlite")
        self.assertEqual(live, full, f"{label}: {first_difference(live, full)}")

    def run_sequence(self, seed: int, share: float) -> "tuple[int, int]":
        vault = Vault(self.vault_dir, random.Random(seed))
        used = steps = 0
        with patch.object(build_index, "MAX_REWRITE_SHARE", share):
            for step in range(STEPS):
                vault.step()
                if step % 9 == 8:
                    flag = "--full"                 # a full build in between: the base changes hands
                else:
                    flag = "--incremental"
                code, out, err = run_build(self.vault_dir, flag, *self.extra)
                self.assertEqual(code, 0, err)
                used += "\nincremental: " in "\n" + out
                steps += 1
                self.assert_equal_to_full(f"seed {seed} step {step} ({flag})")
        return used, steps

    def test_random_edit_sequences_match_a_full_rebuild(self):
        used = steps = 0
        for seed in range(SEEDS):
            # Even seeds always update in place (the mechanism under test); odd seeds keep
            # the production threshold, so the fall-back to a full build is mixed in.
            share = 1.0 if seed % 2 == 0 else build_index.MAX_REWRITE_SHARE
            sequence_used, sequence_steps = self.run_sequence(seed, share)
            used += sequence_used if seed % 2 == 0 else 0
            steps += sequence_steps if seed % 2 == 0 else 0
            with self.subTest(seed=seed):
                self.assertGreater(sequence_used, 0, "the update path was never taken")
            self.temp.cleanup()
            self.setUp()
        # Not a silent comparison of a full build with itself: nearly every non-full step
        # of the always-incremental sequences took the in-place path.
        self.assertGreater(used, steps * 0.7, f"in-place updates: {used} of {steps} steps")

    def test_random_edit_sequences_match_a_full_rebuild_with_name_fields(self):
        self.extra = ("--name-fields",)
        used = steps = 0
        for seed in range(100, 100 + max(SEEDS // 2, 3)):
            sequence_used, sequence_steps = self.run_sequence(seed, 1.0)
            used, steps = used + sequence_used, steps + sequence_steps
            self.temp.cleanup()
            self.setUp()
        self.assertGreater(used, steps * 0.7, f"in-place updates: {used} of {steps} steps")

    def test_random_edit_sequences_match_a_full_rebuild_with_parallel_reads(self):
        """The threaded read-and-hash path (used on larger vaults) yields exactly what the
        serial one does: forced on here with a tiny read-ahead window."""
        used = steps = 0
        with patch.object(build_index, "READ_THREADS", 4), \
                patch.object(build_index, "READ_AHEAD", 2), \
                patch.object(build_index, "READ_AHEAD_BYTES", 4000):
            for seed in range(200, 200 + max(SEEDS // 2, 3)):
                sequence_used, sequence_steps = self.run_sequence(seed, 1.0)
                used, steps = used + sequence_used, steps + sequence_steps
                self.temp.cleanup()
                self.setUp()
        self.assertGreater(used, steps * 0.7, f"in-place updates: {used} of {steps} steps")

    def test_second_run_without_changes_is_stable(self):
        vault = Vault(self.vault_dir, random.Random(5))
        for _ in range(8):
            vault.step()
        run_build(self.vault_dir, "--full")
        for _ in range(3):
            code, out, err = run_build(self.vault_dir, "--incremental")
            self.assertEqual(code, 0, err)
            self.assertIn("0 changed, 0 added, 0 removed", out)
            self.assert_equal_to_full("no changes")

    def test_a_touched_file_only_updates_its_timestamp(self):
        (self.vault_dir / ".context").mkdir()
        note = self.vault_dir / "a.md"
        note.write_text("alpha bravo\n")
        (self.vault_dir / "b.md").write_text("cedar delta\n")
        run_build(self.vault_dir)
        os.utime(note, (BASE_TIME, BASE_TIME))
        code, out, _ = run_build(self.vault_dir, "--incremental")
        self.assertIn("0 changed, 0 added, 0 removed, 2 unchanged (0 records rewritten)", out)
        with sqlite_connection(self.index()) as connection:
            stamp = connection.execute(
                "SELECT timestamp FROM records WHERE source_path='a.md'").fetchone()[0]
        self.assertTrue(stamp.startswith("2023-11-1"), stamp)
        self.assert_equal_to_full("touch")

    def test_ids_match_a_full_build_when_a_note_is_inserted_early(self):
        (self.vault_dir / ".context").mkdir()
        for name in ("b.md", "c.md", "d.md"):
            (self.vault_dir / name).write_text(f"tie marker {name}\n" * 2)
        run_build(self.vault_dir)
        (self.vault_dir / "a.md").write_text("tie marker a\n")
        with patch.object(build_index, "MAX_REWRITE_SHARE", 1.0):
            code, out, _ = run_build(self.vault_dir, "--incremental")
        self.assertIn("1 added", out)
        with sqlite_connection(self.index()) as connection:
            records = connection.execute(
                "SELECT id, source_path FROM records ORDER BY id").fetchall()
        self.assertEqual(records, [(1, "a.md"), (2, "b.md"), (3, "c.md"), (4, "d.md")])
        self.assert_equal_to_full("early insert")

    def test_search_packets_do_not_depend_on_how_the_index_was_built(self):
        """Equal-scoring notes come back in the same order; only the index hash differs."""
        (self.vault_dir / ".context").mkdir()
        (self.vault_dir / ".context" / "routes.json").write_text(json.dumps(ROUTES))
        for name in ("m", "k", "d"):
            (self.vault_dir / f"{name}.md").write_text("shared marker tundra tundra\n")
        run_build(self.vault_dir)
        for name in ("z", "a", "f"):
            (self.vault_dir / f"{name}.md").write_text("shared marker tundra tundra\n")
        with patch.object(build_index, "MAX_REWRITE_SHARE", 1.0):
            self.assertIn("3 added", run_build(self.vault_dir, "--incremental")[1])
        packets = []
        for flag in ("--incremental", "--full"):
            if flag == "--full":
                run_build(self.vault_dir, "--full")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                retrieve.main(["--method", "fts", "--vault", str(self.vault_dir),
                               "shared marker tundra"])
            text = re.sub(r'"index_sha256":"[0-9a-f]{64}"', '"index_sha256":"-"', stdout.getvalue())
            packets.append(text)
        self.assertEqual(packets[0], packets[1])
        self.assertIn('"source_path":"a.md"', packets[0])


def graph_logical(path: Path) -> dict:
    """Everything a reader can observe of graph.sqlite: every table except the parse
    cache, and graph_meta except the clock reading and the cache keys."""
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        meta = dict(connection.execute("SELECT key, value FROM graph_meta"))
        for key in ("built_at", "builder", "inputs_sha256", "files_sha256"):
            meta.pop(key, None)
        tables = {"meta": meta}
        for table, order in (("notes", "path"), ("edges", "id"), ("unresolved", "rowid"),
                             ("skipped", "path"), ("frontmatter", "path")):
            tables[table] = connection.execute(
                f"SELECT * FROM {table} ORDER BY {order}").fetchall()
        return tables
    finally:
        connection.close()


class LinkVault(Vault):
    """The property-test vault plus links of every kind and attachments to point at."""

    def link(self) -> str:
        existing = self.pick({".md", ".png"}) if self.rng.random() < 0.7 else None
        if existing is not None:              # mostly links that resolve (or collide)
            relative = existing.relative_to(self.root).as_posix()
            name, folder = existing.stem, relative.rsplit("/", 1)[0] if "/" in relative else ""
            if existing.suffix == ".png":
                return self.rng.choice([f"![[{existing.name}]]", f"![pic]({relative})"])
        else:
            name = self.rng.choice(NAMES) + str(self.rng.randint(0, 40))
            folder = self.rng.choice(FOLDERS)
        return self.rng.choice([
            f"[[{name}]]", f"[[{folder}/{name}|alias]]", f"![[{name}.png]]",
            f"[text]({name}.md)", f"[up](../{name}.md#part)", f"![pic]({name}.png)",
            f"[[{name}#Heading]]", f"`[[{name}]]`", f"[[missing {name}]]"])

    def op_link(self):
        path = self.pick({".md"})
        if path:
            data = path.read_bytes()
            path.write_bytes(data + ("\nLinks " + " ".join(
                self.link() for _ in range(self.rng.randint(1, 4))) + "\n").encode("utf-8"))
            self.stamp(path)

    def op_related(self):
        name = self.rng.choice(NAMES) + str(self.rng.randint(0, 40))
        self.put(self.random_relative().rsplit(".", 1)[0] + ".md",
                 f"---\nrelated: [[{name}]]\naliases: [{name} alias]\n---\nalpha\n")

    def op_attachment(self):
        name = self.rng.choice(NAMES) + str(self.rng.randint(0, 40))
        folder = self.rng.choice(FOLDERS)
        path = self.root / (f"{folder}/{name}.png" if folder else f"{name}.png")
        if path.exists() and self.rng.random() < 0.5:
            path.unlink()
        else:
            self.put(path.relative_to(self.root).as_posix(), b"\x89PNG fake")

    def step(self):
        super().step()
        for _ in range(self.rng.randint(0, 3)):
            self.rng.choice([self.op_link, self.op_link, self.op_related,
                             self.op_attachment])()


class GraphFromCacheEqualsFresh(unittest.TestCase):
    """`context-layer index` builds the link graph from what the index builder hashed in
    the same run, reuses unchanged notes' parses and leaves an unchanged graph alone.
    After every randomized step that graph must equal one built from scratch."""

    def test_random_edit_sequences(self):
        from context_layer import graph
        unchanged = steps = 0
        for seed in range(300, 300 + max(SEEDS // 2, 3)):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                vault_dir = root / "vault"
                vault_dir.mkdir()
                vault = LinkVault(vault_dir, random.Random(seed))
                for step in range(STEPS):
                    vault.step()
                    if step % 7 == 6:
                        vault.stamp(vault.pick() or vault.put("a.md", "alpha\n"))
                    result: dict = {}
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        self.assertEqual(build_index.main(["--vault", str(vault_dir)], result), 0)
                        live = graph.build(vault_dir, verified={
                            source.path: source for source in result["sources"]})
                        if step % 5 == 4:          # and again: nothing changed in between
                            again = graph.build(vault_dir, verified={
                                source.path: source for source in result["sources"]})
                            self.assertTrue(again.get("unchanged"), f"seed {seed} step {step}")
                        reference = root / f"fresh-{step}.sqlite"
                        fresh = graph.build(vault_dir, out=reference)
                    unchanged += bool(live.get("unchanged"))
                    steps += 1
                    self.assertFalse(fresh.get("unchanged"))
                    left = graph_logical(graph.graph_path(vault_dir))
                    right = graph_logical(reference)
                    self.assertEqual(left, right, f"seed {seed} step {step}: "
                                     + first_difference(left, right))
                    for key in ("notes", "edges", "unresolved", "skipped", "excluded_notes"):
                        self.assertEqual(live[key], fresh[key], f"{key} seed {seed} step {step}")
        self.assertGreater(steps, 0)


class CliIndexRuns(unittest.TestCase):
    """`context-layer index` end to end: the in-process builder, the graph left alone
    when nothing changed, `--full` rebuilding both, usage errors, rollback pairing."""

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "context_layer.cli", *args], cwd=REPO,
                              capture_output=True, text=True,
                              env=dict(os.environ, PYTHONUTF8="1"))

    def test_repeat_full_and_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp) / "v"
            (vault / "notes").mkdir(parents=True)
            (vault / "notes" / "a.md").write_text("alpha see [[b]]\n")
            (vault / "notes" / "b.md").write_text("bravo\n")
            self.assertEqual(self.cli("init", str(vault)).returncode, 0)
            first = self.cli("index", str(vault))
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("graph: 2 notes, 1 edges", first.stdout)
            self.assertNotIn("(unchanged)", first.stdout)
            again = self.cli("index", str(vault))
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertIn("; index unchanged", again.stdout)
            self.assertIn("graph: 2 notes, 1 edges", again.stdout)
            self.assertTrue(again.stdout.rstrip().endswith("(unchanged)"), again.stdout)
            self.assertEqual(self.cli("rollback", str(vault), "--dry-run").returncode, 0)
            full = self.cli("index", str(vault), "--full")
            self.assertEqual(full.returncode, 0, full.stderr)
            self.assertNotIn("(unchanged)", full.stdout)
            self.assertEqual(self.cli("index", str(vault), "--bogus").returncode, 2)
            self.assertEqual(self.cli("index", str(vault), "--help").returncode, 0)
            verbose = self.cli("--verbose", "index", str(vault), "--no-graph")
            self.assertEqual(verbose.returncode, 0, verbose.stderr)
            self.assertIn("+ build_index.py --vault", verbose.stderr)
            self.assertNotIn("graph:", verbose.stdout)


class NothingToWrite(unittest.TestCase):
    """A run that finds nothing to change leaves the index and manifest bytes (and
    files) as they were; `.prev` is refreshed as after any build, so running `index`
    twice still clears a deleted note from it (docs/privacy.md)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / "vault"
        (self.vault / ".context").mkdir(parents=True)
        (self.vault / ".context" / "routes.json").write_text(json.dumps(ROUTES))
        for name in ("a", "b", "c", "gone"):
            (self.vault / f"{name}.md").write_text(f"alpha {name}\n")
        self.ctx = self.vault / ".context"
        self.index = self.ctx / build_index.INDEX_NAME
        self.manifest = self.ctx / build_index.MANIFEST_NAME
        self.assertEqual(run_build(self.vault)[0], 0)
        (self.vault / "gone.md").unlink()
        self.assertEqual(run_build(self.vault)[0], 0)        # a real change

    def live(self):
        return {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in (self.index, self.manifest)}

    def test_a_run_without_changes_leaves_the_index_and_manifest_as_they_were(self):
        before = self.live()
        self.assertIn(b"gone.md", (self.ctx / (self.manifest.name + ".prev")).read_bytes())
        result: dict = {}
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = build_index.main(["--vault", str(self.vault)], result)
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertIn("0 changed, 0 added, 0 removed, 3 unchanged (0 records rewritten); "
                      "index unchanged", stdout.getvalue())
        self.assertEqual(self.live(), before)
        self.assertFalse(result["changed"])
        self.assertEqual([s.path for s in result["sources"]], ["a.md", "b.md", "c.md"])
        # .prev now holds the same generation: the deleted note's text is gone from it.
        for path in (self.index, self.manifest):
            self.assertEqual((self.ctx / (path.name + ".prev")).read_bytes(), path.read_bytes())
        self.assertEqual(sorted(p.name for p in self.ctx.iterdir()
                                if p.name.startswith((".index-", ".staging-"))), [])

    def test_a_same_size_edit_with_the_old_timestamp_is_not_a_no_op(self):
        note = self.vault / "a.md"
        stamp = note.stat().st_mtime_ns
        note.write_text("omega a\n")
        os.utime(note, ns=(stamp, stamp))
        code, out, err = run_build(self.vault)
        self.assertEqual(code, 0, err)
        self.assertIn("1 changed", out)
        self.assertNotIn("index unchanged", out)

    def test_a_changed_limit_or_a_lost_manifest_is_written(self):
        routes = dict(ROUTES, max_file_bytes=6100)
        (self.ctx / "routes.json").write_text(json.dumps(routes))
        self.assertNotIn("index unchanged", run_build(self.vault)[1])
        self.assertIn("index unchanged", run_build(self.vault)[1])
        (self.ctx / build_index.MANIFEST_NAME).unlink()
        self.assertNotIn("index unchanged", run_build(self.vault)[1])
        self.assertTrue((self.ctx / build_index.MANIFEST_NAME).is_file())


class Atomicity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / "vault"
        (self.vault / ".context").mkdir(parents=True)
        (self.vault / ".context" / "routes.json").write_text(json.dumps(ROUTES))
        for name in ("a", "b", "c"):
            (self.vault / f"{name}.md").write_text(f"alpha {name} " + "text " * 900 + "\n")
        self.index = self.vault / ".context" / build_index.INDEX_NAME
        self.manifest = self.vault / ".context" / build_index.MANIFEST_NAME
        self.assertEqual(run_build(self.vault)[0], 0)
        (self.vault / "b.md").write_text("beta changed\n")
        (self.vault / "d.md").write_text("delta added\n")
        self.before = (self.index.read_bytes(), self.manifest.read_bytes())

    def leftovers(self) -> "list[str]":
        return sorted(p.name for p in (self.vault / ".context").iterdir()
                      if p.name.startswith((".index-", ".staging-")))

    def assert_untouched(self):
        self.assertEqual((self.index.read_bytes(), self.manifest.read_bytes()), self.before)
        self.assertFalse(self.index.with_name(self.index.name + ".prev").exists())

    def test_an_error_while_updating_leaves_the_old_index(self):
        with patch.object(build_index, "MAX_REWRITE_SHARE", 1.0),                 patch.object(build_index, "write_meta", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                run_build(self.vault, "--incremental")
        self.assert_untouched()
        self.assertEqual(self.leftovers(), [])

    def test_an_interrupt_while_updating_leaves_the_old_index(self):
        with patch.object(build_index, "MAX_REWRITE_SHARE", 1.0),                 patch.object(build_index, "check_staged", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_build(self.vault, "--incremental")
        self.assert_untouched()
        self.assertEqual(self.leftovers(), [])

    def test_a_killed_process_leaves_the_old_index_and_the_next_run_recovers(self):
        code = ("import os, sys; sys.path.insert(0, %r); import build_index; "
                "build_index.write_meta = lambda *a, **k: os._exit(9); "
                "build_index.MAX_REWRITE_SHARE = 1.0; "
                "build_index.main(['--vault', %r, '--incremental'])"
                % (str(REPO / "router"), str(self.vault)))
        result = subprocess.run([sys.executable, "-c", code], capture_output=True)
        self.assertEqual(result.returncode, 9, result.stderr)
        self.assert_untouched()          # the staging file stays behind; the live index does not move
        with patch.object(build_index, "MAX_REWRITE_SHARE", 1.0):
            code, out, err = run_build(self.vault, "--incremental")
        self.assertEqual(code, 0, err)
        self.assertIn("incremental: 1 changed, 1 added, 0 removed", out)
        reference = Path(self.temp.name) / "reference"
        reference.mkdir()
        run_build(self.vault, "--full", out=reference / "index.sqlite")
        self.assertEqual(logical(self.index), logical(reference / "index.sqlite"))

    def test_an_interrupt_between_index_and_manifest_is_repaired_by_the_next_run(self):
        real = build_index.replace_atomically

        def fail_on_manifest(target, data):
            if target.name == build_index.MANIFEST_NAME:
                raise KeyboardInterrupt
            return real(target, data)
        with patch.object(build_index, "MAX_REWRITE_SHARE", 1.0),                 patch.object(build_index, "replace_atomically", fail_on_manifest):
            with self.assertRaises(KeyboardInterrupt):
                run_build(self.vault, "--incremental")
        # The new index is complete; only the manifest is the old generation's.
        self.assertNotEqual(self.index.read_bytes(), self.before[0])
        self.assertEqual(self.manifest.read_bytes(), self.before[1])
        index_format = build_index.index_format
        connection = sqlite3.connect(self.index)
        self.assertEqual(index_format.full_check(connection), 1)
        connection.close()
        code, out, err = run_build(self.vault, "--incremental")
        self.assertEqual(code, 0, err)
        reference = Path(self.temp.name) / "reference"
        reference.mkdir()
        run_build(self.vault, "--full", out=reference / "index.sqlite")
        self.assertEqual(logical(self.index), logical(reference / "index.sqlite"))


class Fallbacks(unittest.TestCase):
    """Anything doubtful about the previous index means a full rebuild, never a guess."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        (self.vault / ".context").mkdir(parents=True)
        (self.vault / ".context" / "routes.json").write_text(json.dumps(ROUTES))
        (self.vault / "big.md").write_text("\n".join(f"line {n} alpha" for n in range(1200)))
        (self.vault / "small.md").write_text("bravo\n")
        self.index = self.vault / ".context" / build_index.INDEX_NAME
        self.assertEqual(run_build(self.vault)[0], 0)
        (self.vault / "small.md").write_text("bravo changed\n")

    def damage(self, *statements: str):
        connection = sqlite3.connect(self.index)
        for statement in statements:
            connection.execute(statement)
        connection.commit()
        connection.close()

    def assert_matches_full(self):
        reference = self.root / "reference"
        reference.mkdir(exist_ok=True)
        run_build(self.vault, "--full", out=reference / "index.sqlite")
        self.assertEqual(logical(self.index), logical(reference / "index.sqlite"))

    def expect_full(self, fragment: str):
        code, out, err = run_build(self.vault, "--incremental")
        self.assertEqual(code, 0, err)
        self.assertIn("full rebuild: ", out)
        self.assertIn(fragment, out)
        self.assertNotIn("\nincremental:", out)
        self.assert_matches_full()

    def test_a_newer_format_version(self):
        self.damage("PRAGMA user_version = 2")
        self.expect_full("format version")

    def test_a_legacy_format_version(self):
        self.damage("PRAGMA user_version = 0")
        self.expect_full("format version")

    def test_an_index_written_before_incremental_updates_existed(self):
        self.damage("DELETE FROM index_meta WHERE key = 'chunk_size'")
        self.expect_full("before incremental updates existed")

    def test_another_chunk_size(self):
        self.damage("UPDATE index_meta SET value = '4000' WHERE key = 'chunk_size'")
        self.expect_full("chunk size")

    def test_another_tokenizer(self):
        other = build_index.SCHEMA.replace(build_index.textfold.TOKENIZER, "porter")
        self.assertNotEqual(other, build_index.SCHEMA)
        with patch.object(build_index, "SCHEMA", other):
            run_build(self.vault, "--full")
        (self.vault / "small.md").write_text("bravo changed again\n")
        self.expect_full("layout or tokenizer")

    def test_an_emptied_full_text_table(self):
        self.damage("INSERT INTO records_fts(records_fts) VALUES('delete-all')")
        self.expect_full("inconsistent")

    def test_a_missing_chunk_row(self):
        self.damage("DELETE FROM records WHERE id = 2", "DELETE FROM records_fts_docsize WHERE id = 2")
        self.expect_full("contiguous")

    def test_one_note_with_two_hashes(self):
        self.damage("UPDATE records SET source_sha256 = 'x' WHERE id = 1")
        self.expect_full("single-version")

    def test_a_file_that_is_not_an_index(self):
        self.index.write_bytes(b"this is not a database" * 300)
        self.expect_full("not usable")

    def test_too_many_records_to_rewrite_builds_in_full_from_the_stored_text(self):
        (self.vault / "a.md").write_text("first\n")       # sorts before both: every id moves
        with patch.object(build_index, "iter_sources", wraps=build_index.iter_sources) as scans:
            code, out, _ = run_build(self.vault, "--incremental")
        self.assertIn("full rebuild: the update would rewrite", out)
        self.assertEqual(scans.call_count, 1, "the vault was walked twice")
        self.assert_matches_full()

    def test_a_stored_text_that_does_not_match_its_hash_is_not_trusted(self):
        (self.vault / "a.md").write_text("first\n")
        self.damage("UPDATE records SET content = 'tampered' WHERE id = 1")
        with patch.object(build_index, "iter_sources", wraps=build_index.iter_sources) as scans:
            code, out, _ = run_build(self.vault, "--incremental")
        self.assertIn("full rebuild: the previous index text does not match its hash", out)
        self.assertEqual(scans.call_count, 2)
        self.assert_matches_full()

    def test_a_large_change_stops_the_scan_early(self):
        (self.vault / "small.md").write_text("bravo " * 2000)
        with patch.object(build_index, "MAX_REWRITE_SHARE", 0.01):
            code, out, _ = run_build(self.vault, "--incremental")
        self.assertIn("full rebuild: the changed notes are more than 1% of the index size", out)
        self.assert_matches_full()

    def test_switching_name_fields_on_or_off_rebuilds_in_full(self):
        self.assertNotIn("names_fts", logical(self.index)["schema"].__repr__())
        code, out, _ = run_build(self.vault, "--incremental", "--name-fields")
        self.assertIn("full rebuild: the previous index was built without --name-fields", out)
        self.assertIn("names_fts", logical(self.index)["schema"].__repr__())
        (self.vault / "small.md").write_text("bravo changed twice\n")
        code, out, _ = run_build(self.vault, "--incremental", "--name-fields")
        self.assertIn("\nincremental: ", out)
        code, out, _ = run_build(self.vault, "--incremental")
        self.assertIn("full rebuild: the previous index was built with --name-fields", out)
        self.assert_matches_full()

    def test_no_index_yet_builds_in_full_without_a_message(self):
        self.index.unlink()
        code, out, _ = run_build(self.vault, "--incremental")
        self.assertEqual(code, 0)
        self.assertNotIn("full rebuild", out)
        self.assertNotIn("incremental:", out)

    def test_the_flags_exclude_each_other(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            run_build(self.vault, "--full", "--incremental")


GOLDEN = Path(__file__).resolve().parent / "fixtures" / "default_search_golden.json"


def default_search_digests() -> "dict[str, str]":
    """sha256 of the default `fts` and `synaptic` packets for every fourth dev_bridge
    question, on a vault built by `context-layer init` and `index` (as a user would).

    Three things are normalised because they are not part of the packet's evidence: the
    temporary vault path, the coverage receipt's `index_sha256` (the hash of an index
    file, which carries a build timestamp), and the link graph's `built_at`."""
    digests = {}
    with tempfile.TemporaryDirectory() as temp:
        vault = Path(temp) / "v"
        cases = dev_bridge.build(vault)
        for step in (["init", str(vault)], ["index", str(vault)]):
            subprocess.run([sys.executable, "-m", "context_layer.cli", *step], cwd=REPO,
                           capture_output=True, check=True)
        for method in ("fts", "synaptic"):
            for case in cases[::4]:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    retrieve.main(["--method", method, "--vault", str(vault), case["question"]])
                text = stdout.getvalue().replace(str(vault), "<vault>")
                text = re.sub(r'"index_sha256": ?"[0-9a-f]{64}"', '"index_sha256":"<sha>"', text)
                text = re.sub(r'"built_at": ?"[^"]*"', '"built_at":"<t>"', text)
                digests[f"{method}|{case['id']}"] = hashlib.sha256(text.encode()).hexdigest()
    return digests


class DefaultSearchIsUnchanged(unittest.TestCase):
    def test_default_search_output_is_byte_identical_to_the_golden_digests(self):
        """The digests were computed on the commit before incremental indexing existed
        (7abe5b1); the default build and the default search must still produce them.
        If dev_bridge.py changes on purpose, regenerate with
        `python3 tests/test_incremental_index.py --write-golden` and say why."""
        golden = json.loads(GOLDEN.read_text("utf-8"))
        self.assertEqual(default_search_digests(), golden["digests"])

    def test_a_default_full_build_and_an_incremental_update_give_the_same_packets(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp) / "v"
            cases = dev_bridge.build(vault)
            subprocess.run([sys.executable, "-m", "context_layer.cli", "init", str(vault)],
                           cwd=REPO, capture_output=True, check=True)
            self.assertEqual(run_build(vault)[0], 0)
            (vault / "people" / "Zz New Person.md").write_text("# Zz New Person\n\nPlays the tuba.\n")
            (vault / "projects" / "lantern.md").write_text(
                (vault / "projects" / "lantern.md").read_text("utf-8") + "\nAppended line.\n")
            texts = {}
            for flag in ("--incremental", "--full"):
                code, out, err = run_build(vault, flag)
                self.assertEqual(code, 0, err)
                if flag == "--incremental":
                    self.assertIn("incremental: ", out)
                packets = []
                for case in cases[::3]:
                    stdout = io.StringIO()
                    with redirect_stdout(stdout):
                        retrieve.main(["--method", "fts", "--vault", str(vault), case["question"]])
                    packets.append(re.sub(r'"index_sha256": ?"[0-9a-f]{64}"', "-", stdout.getvalue()))
                texts[flag] = packets
            self.assertEqual(texts["--incremental"], texts["--full"])


if __name__ == "__main__":
    if "--write-golden" in sys.argv:
        GOLDEN.write_text(json.dumps({
            "computed_on": "7abe5b1 (before incremental indexing)",
            "digests": default_search_digests()}, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {GOLDEN.name}")
    else:
        unittest.main()
