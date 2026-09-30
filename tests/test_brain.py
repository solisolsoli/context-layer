"""Tests for context_layer.brain - the starter vault. Runnable as `python3 tests/test_brain.py`.

Every test writes into its own temporary directory, with HOME pointed there too.
The CLI is driven through a small parser that registers `brain` and `rules` the
way cli.py does; `index` and `search` run through `python -m context_layer.cli`.
"""
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from _portable_helpers import isolated_home_env

REPO = Path(os.environ.get("TEST_REPO_HOME", Path(__file__).resolve().parents[1]))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from context_layer import brain  # noqa: E402

DRIVER = (
    "import argparse, sys\n"
    "from context_layer import brain, rules\n"
    "parser = argparse.ArgumentParser(prog='context-layer')\n"
    "sub = parser.add_subparsers(dest='command', required=True)\n"
    "brain.register(sub)\n"
    "rules.register(sub)\n"
    "args, extra = parser.parse_known_args(sys.argv[1:])\n"
    "args.rest = [t for t in extra if t != '--']\n"
    "raise SystemExit(args.func(args))\n"
)
WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
FENCE = re.compile(r"^\s*(```|~~~)")
NON_ASCII = re.compile(r"[^\x00-\x7f]")
# The letters the repository must never contain, written as escapes.
LOCAL_LETTERS = re.compile("[\u00e7\u011f\u0131\u00f6\u015f\u00fc\u00c7\u011e\u0130\u00d6\u015e\u00dc]")
UPSTREAM_TOP = {
    "\U0001f4e5 000-Inbox", "\U0001f3af 100-Command-Center", "\u2694\ufe0f 200-Goals",
    "\U0001f3f0 300-Projects", "\U0001f510 400-Vault", "\U0001f9e0 500-Knowledge",
    "\U0001f6e0\ufe0f 600-Arsenal", "\U0001f4aa 700-Body", "\U0001f9d8 800-Mind",
    "\U0001f52e 850-Companion", "\U0001f4e6 900-Archive", "\U0001f4cb Templates",
    "daily", "knowledge",
}
STANDARD_TOP = {"000-Inbox", "100-Command-Center", "200-Goals", "300-Projects", "400-Records",
                "500-Knowledge", "600-Arsenal", "700-Body", "800-Mind", "850-Companion",
                "900-Archive", "Templates", "daily", "knowledge"}
COMPANION_FILES = {"Core.md", "Rules.md", "Last-Session.md", "Threads.md", "Journal.md",
                   "Index.md"}


def links_in(text):
    """Wikilink targets outside fenced code and inline code, frontmatter included."""
    found, inside = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            inside = not inside
            continue
        if inside:
            continue
        line = re.sub(r"`[^`]*`", "", line)
        for match in WIKILINK.finditer(line):
            target = match.group(1).split("|", 1)[0].split("#", 1)[0].strip()
            if target:
                found.append(target)
    return found


def unresolved_links(vault):
    notes = [p for p in vault.rglob("*.md") if ".context" not in p.parts]
    by_path = {p.relative_to(vault).with_suffix("").as_posix().casefold() for p in notes}
    names = {}
    for p in notes:
        names.setdefault(p.stem.casefold(), []).append(p)
    missing = []
    for note in notes:
        for target in links_in(note.read_text(encoding="utf-8")):
            key = target[:-3] if target.endswith(".md") else target
            if "/" in key:
                ok = key.casefold() in by_path
            else:
                ok = len(names.get(key.casefold(), [])) == 1
            if not ok:
                missing.append((note.relative_to(vault).as_posix(), target))
    return missing


class BrainBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / "my-brain"
        home = self.root / "home"
        home.mkdir()
        patcher = mock.patch.dict(os.environ, isolated_home_env(os.environ, str(home)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def env(self):
        env = dict(os.environ)
        env.pop("CONTEXT_LAYER_HOME", None)
        env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
        return env

    def cli(self, *argv):
        return subprocess.run([sys.executable, "-c", DRIVER, *argv], cwd=REPO,
                              capture_output=True, text=True, env=self.env())

    def context_layer(self, *argv):
        return subprocess.run([sys.executable, "-m", "context_layer.cli", *argv], cwd=REPO,
                              capture_output=True, text=True, env=self.env())

    def index(self):
        result = self.context_layer("index", str(self.vault))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def unresolved(self):
        """Unresolved links in the link graph `index` built (reason, source, line)."""
        connection = sqlite3.connect(self.vault / ".context" / "graph.sqlite")
        try:
            return connection.execute("SELECT reason, source_path, line FROM unresolved "
                                      "ORDER BY source_path, line").fetchall()
        finally:
            connection.close()

    def make(self, *flags):
        result = self.cli("brain", "init", str(self.vault), "--apply", *flags)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def top(self):
        return {p.name for p in self.vault.iterdir() if p.is_dir() and not p.name.startswith(".")}


class Create(BrainBase):
    def test_dry_run_writes_nothing(self):
        result = self.cli("brain", "init", str(self.vault))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dry run", result.stderr)
        self.assertIn("+ CLAUDE.md", result.stdout)
        self.assertFalse(self.vault.exists())

    def test_apply_creates_the_standard_layout(self):
        result = self.make()
        self.assertEqual(self.top(), STANDARD_TOP)
        self.assertTrue((self.vault / "000-Inbox" / "Dump").is_dir())
        self.assertTrue((self.vault / "100-Command-Center" / "Dashboard.md").is_file())
        self.assertEqual({p.name for p in (self.vault / "850-Companion").iterdir()},
                         COMPANION_FILES)
        for name in ("Daily", "Project", "Decision", "Person", "Meeting"):
            text = (self.vault / "Templates" / f"{name}.md").read_text(encoding="utf-8")
            for key in ("aliases:", "related:", "supersedes:", "status:", "tags:"):
                self.assertIn(key, text.split("---")[1], (name, key))
        for folder in brain.STANDARD.values():
            self.assertTrue((self.vault / folder / "Index.md").is_file(), folder)
        examples = [p for p in self.vault.rglob("Example - *.md")]
        self.assertTrue(6 <= len(examples) <= 10, len(examples))
        for path in examples:
            self.assertIn("Safe to delete", path.read_text(encoding="utf-8"), path.name)
        dashboard = (self.vault / "100-Command-Center" / "Dashboard.md").read_text(encoding="utf-8")
        self.assertIn("adapted from Avenox Beyin", dashboard)
        self.assertIn("github.com/avenoxai/avenoxbeyin", dashboard)
        self.assertIn("context-layer index", result.stdout)
        self.assertIn("rules check", result.stdout)
        self.assertIn("install claude-code", result.stdout)
        # The plugin bundle is committed: the message names the files to copy, no build.
        for name in ("obsidian-plugin/manifest.json", "obsidian-plugin/styles.css",
                     "obsidian-plugin/dist/main.js"):
            self.assertIn(name, result.stdout)
        self.assertNotIn("build the", result.stdout)
        self.assertIn(f".obsidian/plugins/{brain.PLUGIN_ID}/", result.stdout)
        self.assertFalse((self.vault / ".obsidian" / "plugins").exists())
        self.assertFalse((self.vault / ".obsidian" / "community-plugins.json").exists())

    def test_supersession_example(self):
        self.make()
        v2 = (self.vault / "500-Knowledge" / "Example - Watering Schedule v2.md").read_text(
            encoding="utf-8")
        # The archived version is named, not linked: the archive is outside search, so a
        # link into it could never be followed and would count as unresolved.
        self.assertIn("replaces `Example - Watering Schedule v1`", v2)
        self.assertIn("(`900-Archive/`)", v2)
        self.assertNotIn("[[Example - Watering Schedule v1]]", v2)
        v1 = (self.vault / "900-Archive" / "Example - Watering Schedule v1.md").read_text(
            encoding="utf-8")
        self.assertIn("Superseded by [[Example - Watering Schedule v2]]", v1)

    def test_routes_exclude_templates_records_archive_and_rule_files(self):
        self.make()
        config = json.loads((self.vault / ".context" / "routes.json").read_text(encoding="utf-8"))
        for prefix in ("Templates/", "400-Records/", "900-Archive/", "CLAUDE.md", "AGENTS.md"):
            self.assertIn(prefix, config["exclude_prefixes"])
            self.assertIn(prefix, config["retrieval_exclude_prefixes"])
        for prefix in ("LOG.md", "BACKLOG.md"):                 # records stay searchable
            self.assertNotIn(prefix, config["exclude_prefixes"])

    def test_no_route_points_at_an_excluded_source(self):
        """B-23: routes for excluded folders (templates, rule files) are pruned."""
        for flags in ((), ("--layout", "minimal"), ("--avenox-compat",)):
            with self.subTest(flags=flags):
                self.vault = self.root / ("r" + "".join(flags).replace("-", ""))
                self.make(*flags)
                config = json.loads((self.vault / ".context" / "routes.json").read_text(
                    encoding="utf-8"))
                prefixes = config["exclude_prefixes"]
                self.assertTrue(config["routes"])
                for name, route in config["routes"].items():
                    self.assertTrue(route["canonical_sources"], name)
                    for source in route["canonical_sources"]:
                        path = source["path"]
                        self.assertFalse(any(path == p or path.startswith(p) for p in prefixes),
                                         (name, path))

    def test_default_layout_is_pure_ascii(self):
        self.make()
        for path in self.vault.rglob("*"):
            relative = path.relative_to(self.vault).as_posix()
            self.assertIsNone(NON_ASCII.search(relative), relative)
            if path.is_file() and path.suffix in (".md", ".json"):
                text = path.read_text(encoding="utf-8")
                self.assertIsNone(NON_ASCII.search(text), relative)

    def test_minimal_layout(self):
        self.make("--layout", "minimal")
        self.assertEqual(self.top(), {"Inbox", "Projects", "Knowledge", "Archive", "Daily",
                                      "Templates"})
        self.assertTrue((self.vault / "Dashboard.md").is_file())
        self.assertFalse((self.vault / "Index.md").exists())
        self.assertEqual(unresolved_links(self.vault), [])

    def test_avenox_compat_names(self):
        self.make("--avenox-compat")
        self.assertEqual(self.top(), UPSTREAM_TOP)
        companion = self.vault / "\U0001f52e 850-Companion"
        self.assertEqual({p.name for p in companion.iterdir()}, COMPANION_FILES)
        self.assertTrue((self.vault / "\U0001f4e5 000-Inbox" / "Dump").is_dir())
        dashboard = (self.vault / "\U0001f3af 100-Command-Center" / "Dashboard.md").read_text(
            encoding="utf-8")
        self.assertIn("[[\U0001f3f0 300-Projects/Index|Projects]]", dashboard)
        self.assertIn("original folder names", dashboard)

    def test_compat_needs_standard_layout(self):
        result = self.cli("brain", "init", str(self.vault), "--layout", "minimal",
                          "--avenox-compat")
        self.assertEqual(result.returncode, 1)
        self.assertIn("standard layout only", result.stderr)

    def test_all_links_resolve_in_every_layout(self):
        for flags in ((), ("--layout", "minimal"), ("--avenox-compat",)):
            with self.subTest(flags=flags):
                self.vault = self.root / ("v" + "".join(flags).replace("-", ""))
                self.make(*flags)
                self.assertEqual(unresolved_links(self.vault), [])

    def test_no_local_letters_in_any_layout(self):
        for flags in ((), ("--layout", "minimal"), ("--avenox-compat",)):
            self.vault = self.root / ("w" + "".join(flags).replace("-", ""))
            self.make(*flags)
            for path in self.vault.rglob("*"):
                self.assertIsNone(LOCAL_LETTERS.search(path.name), path.name)
                if path.is_file() and path.suffix in (".md", ".json"):
                    self.assertIsNone(LOCAL_LETTERS.search(path.read_text(encoding="utf-8")),
                                      path.name)

    def test_json_output(self):
        result = self.cli("brain", "init", str(self.vault), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertFalse(data["applied"])
        self.assertIn("CLAUDE.md", data["create"])
        self.assertIn("850-Companion/Core.md", data["create"])


class Safety(BrainBase):
    def test_refuses_non_empty_directory(self):
        self.vault.mkdir()
        (self.vault / "notes.md").write_text("mine\n", encoding="utf-8")
        result = self.cli("brain", "init", str(self.vault), "--apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not empty", result.stderr)
        self.assertEqual([p.name for p in self.vault.iterdir()], ["notes.md"])

    def test_empty_directory_is_fine(self):
        self.vault.mkdir()
        (self.vault / ".DS_Store").write_bytes(b"")
        self.make()
        self.assertTrue((self.vault / "CLAUDE.md").is_file())

    def test_into_existing_never_overwrites(self):
        self.vault.mkdir()
        mine_rules = "# My rules\nKeep it short.\n"
        mine_hub = "# My projects\n"
        (self.vault / "CLAUDE.md").write_text(mine_rules, encoding="utf-8")
        (self.vault / "300-Projects").mkdir()
        (self.vault / "300-Projects" / "Index.md").write_text(mine_hub, encoding="utf-8")
        result = self.make("--into-existing")
        self.assertIn("300-Projects/Index.md (exists, kept)", result.stdout)
        self.assertEqual((self.vault / "CLAUDE.md").read_text(encoding="utf-8"), mine_rules)
        self.assertEqual((self.vault / "300-Projects" / "Index.md").read_text(encoding="utf-8"),
                         mine_hub)
        # The missing partner mirrors the user's rule file, so parity holds.
        self.assertEqual((self.vault / "AGENTS.md").read_text(encoding="utf-8"), mine_rules)
        self.assertTrue((self.vault / "500-Knowledge" / "Index.md").is_file())
        self.assertEqual(list(self.vault.rglob("*.bak-*")), [])

    def test_into_existing_keeps_routes(self):
        (self.vault / ".context").mkdir(parents=True)
        (self.vault / ".context" / "routes.json").write_text('{"routes": {}}\n', encoding="utf-8")
        self.make("--into-existing")
        self.assertEqual((self.vault / ".context" / "routes.json").read_text(encoding="utf-8"),
                         '{"routes": {}}\n')

    def test_into_existing_counts_only_what_it_would_create(self):
        first = self.make()
        total = len(brain.plan("standard")[0])
        self.assertIn(f"created: {total} folders,", first.stdout)
        again = self.cli("brain", "init", str(self.vault), "--into-existing")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("would create: 0 folders, 0 files,", again.stdout)
        shutil.rmtree(self.vault / "500-Knowledge")
        partial = self.cli("brain", "init", str(self.vault), "--into-existing", "--json")
        data = json.loads(partial.stdout)
        directories, files = brain.plan("standard", examples=False)
        self.assertEqual(data["new_directories"],
                         [d for d in directories if d.split("/")[0] == "500-Knowledge"])
        self.assertTrue(data["new_directories"])
        self.assertEqual(data["create"],
                         sorted(f for f in files if f.startswith("500-Knowledge/")))

    def test_second_run_changes_nothing(self):
        self.make()
        before = {p: p.read_bytes() for p in self.vault.rglob("*") if p.is_file()}
        self.make("--into-existing")
        after = {p: p.read_bytes() for p in self.vault.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_deleted_examples_are_not_recreated(self):
        """B-23: --into-existing leaves deleted examples deleted unless --examples."""
        self.make()
        examples = sorted(self.vault.rglob("Example - *.md"))
        self.assertTrue(examples)
        for path in examples:
            path.unlink()
        again = self.make("--into-existing")
        self.assertIn("example notes not written", again.stdout)
        self.assertEqual(list(self.vault.rglob("Example - *.md")), [])
        self.make("--into-existing", "--examples")
        self.assertEqual(sorted(self.vault.rglob("Example - *.md")), examples)

    def test_kept_routes_without_rule_exclusions_get_a_note(self):
        (self.vault / ".context").mkdir(parents=True)
        (self.vault / ".context" / "routes.json").write_text('{"routes": {}}\n', encoding="utf-8")
        result = self.make("--into-existing")
        self.assertIn("rules init", result.stderr)
        self.assertIn("CLAUDE.md and AGENTS.md", result.stderr)


class NoExamples(BrainBase):
    def test_no_examples_leaves_no_example_or_dangling_link(self):
        for flags in ((), ("--layout", "minimal")):
            with self.subTest(flags=flags):
                self.vault = self.root / ("n" + "".join(flags).replace("-", ""))
                result = self.make("--no-examples", *flags)
                self.assertIn("example notes not written", result.stdout)
                self.assertEqual(list(self.vault.rglob("Example - *.md")), [])
                for path in self.vault.rglob("*.md"):
                    text = path.read_text(encoding="utf-8")
                    self.assertNotIn("Example - ", text, path.name)
                    self.assertNotIn("## Examples", text, path.name)
                self.assertEqual(unresolved_links(self.vault), [])

    def test_placeholders_never_leak(self):
        for flags in ((), ("--no-examples",), ("--layout", "minimal"), ("--avenox-compat",)):
            with self.subTest(flags=flags):
                self.vault = self.root / ("p" + "".join(flags).replace("-", ""))
                self.make(*flags)
                for path in self.vault.rglob("*.md"):
                    text = path.read_text(encoding="utf-8")
                    for token in (brain.EXAMPLE_LINE, "{{folder:", "{{hubs}}", "{{credit}}"):
                        self.assertNotIn(token, text, (path.name, token))

    def test_json_reports_examples(self):
        result = self.cli("brain", "init", str(self.vault), "--no-examples", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertFalse(data["examples"])
        self.assertFalse([f for f in data["create"] if "Example - " in f])

    def test_examples_flags_exclude_each_other(self):
        result = self.cli("brain", "init", str(self.vault), "--examples", "--no-examples")
        self.assertEqual(result.returncode, 2)


class Integration(BrainBase):
    def test_indexes_and_rules_check_pass(self):
        self.make()
        check = self.cli("rules", "check", str(self.vault))
        self.assertEqual(check.returncode, 0, check.stderr)
        self.index()
        self.assertTrue((self.vault / ".context" / "index.sqlite").is_file())
        search = self.context_layer("search", str(self.vault), "--prompt",
                                    "drip irrigation emitters")
        self.assertEqual(search.returncode, 0, search.stderr)
        self.assertIn("Example - Drip Irrigation Basics.md", search.stdout)
        self.assertNotIn("900-Archive/", search.stdout)

    def test_fresh_brain_link_graph_has_no_unresolved_link(self):
        """N13: shipped notes never link into excluded folders or the rule files."""
        for flags in ((), ("--no-examples",), ("--layout", "minimal"),
                      ("--layout", "minimal", "--no-examples"), ("--avenox-compat",)):
            with self.subTest(flags=flags):
                self.vault = self.root / ("g" + "".join(flags).replace("-", ""))
                self.make(*flags)
                result = self.index()
                self.assertIn(" 0 unresolved links", result.stdout)
                self.assertEqual(self.unresolved(), [])

    def test_search_never_serves_the_rule_files(self):
        """B-05: the hosts load CLAUDE.md/AGENTS.md; search must not repeat them."""
        self.make()
        self.index()
        for prompt in ("when should the agent stop and ask", "record every meaningful step",
                       "what is our budget total", "merge two notes"):
            with self.subTest(prompt=prompt):
                search = self.context_layer("search", str(self.vault), "--prompt", prompt)
                self.assertEqual(search.returncode, 0, search.stderr)
                packet = json.loads(search.stdout)
                paths = [item["source_path"] for item in packet.get("evidence", [])]
                self.assertNotIn("CLAUDE.md", paths)
                self.assertNotIn("AGENTS.md", paths)
                for item in packet.get("evidence", []):
                    self.assertNotIn("<what was done, one line>", item.get("content", ""))

    def test_rule_records_stay_searchable(self):
        self.make()
        self.cli("rules", "record", str(self.vault), "--summary", "Planted the zucchini seedlings",
                 "--files", "none", "--verified", "ok", "--next", "none")
        self.index()
        search = self.context_layer("search", str(self.vault), "--prompt", "zucchini seedlings")
        self.assertEqual(search.returncode, 0, search.stderr)
        paths = [item["source_path"] for item in json.loads(search.stdout)["evidence"]]
        self.assertIn("LOG.md", paths)
        self.assertNotIn("CLAUDE.md", paths)


if __name__ == "__main__":
    unittest.main(verbosity=2)
