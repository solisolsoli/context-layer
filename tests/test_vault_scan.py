#!/usr/bin/env python3
"""Tests for the `context-layer init` vault scanner.

Run directly: `python3 tests/test_vault_scan.py`, or via `make test`.
Standard library only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from _portable_helpers import isolated_home_env

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from context_layer.vault_scan import (print_report, render_config, scan_vault,
                                      writable_exclusions)  # noqa: E402
from router import source_policy  # noqa: E402


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class ScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- noise -------------------------------------------------------------

    def test_text_files_skip_tooling_dot_excluded_empty_and_linked_paths(self) -> None:
        from context_layer.vault_scan import collect_text_files
        for name in ("notes/a.md", "notes/deep/b.TXT", "Index.md", ".dot.md", "arch.md",
                     "node_modules/x.md", ".git/y.md", "notes/.hidden/z.md", "archive/old.md",
                     "venv/v.md", "notes/pic.png"):
            write(self.root, name, "alpha\n")
        write(self.root, "notes/empty.md", "")
        linked = []
        if os.name != "nt":
            os.symlink(self.root / "notes", self.root / "linked")
            os.symlink(self.root / "notes" / "a.md", self.root / "alias.md")
            linked = ["linked/a.md", "alias.md"]
        found = [p.as_posix() for p in collect_text_files(self.root, ["arch"])]
        self.assertEqual(found, [".dot.md", "Index.md", "notes/a.md", "notes/deep/b.TXT"])
        self.assertFalse(set(found) & set(linked))


    def test_obvious_noise_is_excluded(self) -> None:
        write(self.root, ".obsidian/app.json", "{}")
        write(self.root, ".trash/gone.md", "# gone\n")
        write(self.root, "node_modules/x/index.js", "1")
        write(self.root, "Generated/report.md", "# generated\n")
        write(self.root, "Archive/old.md", "# old\n")
        write(self.root, "Notes/one.md", "# One\n\n## Scope\n\nbody\n")
        write(self.root, "Notes/two.md", "# Two\n\n## Scope\n\nbody\n")
        for name in ("a.png", "b.jpg", "c.pdf", "d.gif"):
            (self.root / "Attachments").mkdir(exist_ok=True)
            (self.root / "Attachments" / name).write_bytes(b"stub")

        result = scan_vault(self.root)
        prefixes = {prefix for prefix, _ in result.excluded}
        for expected in (".obsidian/", ".trash/", "node_modules/",
                         "Generated/", "Archive/", "Attachments/"):
            self.assertIn(expected, prefixes)
        indexed = {p.as_posix() for p in result.text_files}
        self.assertEqual(indexed, {"Notes/one.md", "Notes/two.md"})
        # An archive folder is also offered as a mirror prefix.
        self.assertIn("Archive/", result.mirror_prefixes)

    def test_binary_heavy_folder_is_treated_as_attachments(self) -> None:
        folder = self.root / "Pasted"
        folder.mkdir()
        for name in ("a.png", "b.png", "c.png", "d.png", "e.md"):
            (folder / name).write_bytes(b"stub")
        write(self.root, "Notes/one.md", "# One\n")
        write(self.root, "Notes/two.md", "# Two\n")
        result = scan_vault(self.root)
        self.assertIn("Pasted/", {prefix for prefix, _ in result.excluded})

    # -- route shapes ------------------------------------------------------

    def test_generated_config_names_this_version_and_a_utc_time(self) -> None:
        # F2-15: not a stale 0.1.0 label and not a local-offset timestamp.
        import re
        from context_layer import __version__
        write(self.root, "Notes/one.md", "# One\n\nbody\n")
        config = render_config(scan_vault(self.root))
        self.assertEqual(config["engine_version"], f"{__version__}-generated")
        self.assertRegex(config["_generated"]["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIsNotNone(re.match(r"^\d+\.\d+\.\d+-generated$", config["engine_version"]))

    def test_folders_become_routes(self) -> None:
        write(self.root, "Reference/glossary.md", "# Glossary\n\n## Terms\n\nx\n")
        write(self.root, "Reference/style-guide.md", "# Style guide\n\n## Voice\n\ny\n")
        result = scan_vault(self.root)
        names = {route.name for route in result.routes}
        self.assertIn("reference", names)
        route = next(r for r in result.routes if r.name == "reference")
        self.assertIn("reference", route.triggers)
        self.assertEqual(route.path_hints, ["Reference/"])
        self.assertTrue(route.canonical_sources)

    def test_flat_distinct_files_become_one_route_each(self) -> None:
        write(self.root, "release-checklist.md", "# Release checklist\n\nfreeze\n")
        write(self.root, "incident-runbook.md", "# Incident runbook\n\npage on-call\n")
        result = scan_vault(self.root)
        self.assertEqual(result.shape, "flat")
        names = {route.name for route in result.routes}
        self.assertEqual(names, {"release-checklist", "incident-runbook"})
        route = next(r for r in result.routes if r.name == "release-checklist")
        self.assertEqual(route.canonical_sources, ["release-checklist.md"])

    def test_daily_notes_get_a_route_with_no_canonical_source(self) -> None:
        for day in range(2, 12):
            write(self.root, f"2026-01-{day:02d}.md",
                  f"# 2026-01-{day:02d}\n\n## Standup\n\nblocked on the migration\n")
        result = scan_vault(self.root)
        route = next(r for r in result.routes if r.name == "daily-notes")
        # A date is useless as a trigger, so triggers must come from headings.
        self.assertIn("standup", route.triggers)
        # No single day is the authority: the route is deliberately lexical-only.
        self.assertEqual(route.canonical_sources, [])
        self.assertIsNotNone(route.canonical_note)

    def test_a_trigger_never_fires_two_routes(self) -> None:
        write(self.root, "Alpha/policy-alpha.md", "# Alpha\n\n## Policy\n\nx\n")
        write(self.root, "Alpha/alpha-two.md", "# Alpha two\n\n## Policy\n\nx\n")
        write(self.root, "Beta/policy-beta.md", "# Beta\n\n## Policy\n\nx\n")
        write(self.root, "Beta/beta-two.md", "# Beta two\n\n## Policy\n\nx\n")
        result = scan_vault(self.root)
        seen: set[str] = set()
        for route in result.routes:
            for trigger in route.triggers:
                self.assertNotIn(trigger, seen, f"trigger {trigger!r} fires two routes")
                seen.add(trigger)

    def test_legacy_named_file_is_not_pinned_as_current_canonical(self) -> None:
        write(self.root, "Rules/style-guide.md", "# Style guide\n\n## Voice\n\ncurrent\n")
        write(self.root, "Rules/legacy-style-guide.md", "# Legacy style guide\n\n" + "old\n" * 200)
        result = scan_vault(self.root)
        route = next(r for r in result.routes if r.name == "rules")
        self.assertNotIn("Rules/legacy-style-guide.md", route.canonical_sources)
        self.assertTrue(any("looks superseded" in reason for reason in route.why))

    def test_empty_vault_reports_instead_of_guessing(self) -> None:
        result = scan_vault(self.root)
        self.assertEqual(result.routes, [])
        self.assertTrue(result.notes)

    # -- generated config --------------------------------------------------

    def test_generated_config_is_loadable_and_says_it_is_a_guess(self) -> None:
        write(self.root, "Reference/glossary.md", "# Glossary\n\n## Terms\n\nx\n")
        write(self.root, "Reference/style-guide.md", "# Style guide\n\n## Voice\n\ny\n")
        config = render_config(scan_vault(self.root))
        self.assertEqual(config["schema_version"], 1)
        self.assertIn("routes", config)
        self.assertIn("GUESS", config["_generated"]["warning"])
        for route in config["routes"].values():
            self.assertIn("_inferred_from", route)
            for source in route["canonical_sources"]:
                self.assertTrue((self.root / source["path"]).is_file())
        for prefix in (".obsidian/", ".trash/", "node_modules/"):
            self.assertIn(prefix, config["exclude_prefixes"])

    # -- configurable stopwords -------------------------------------------

    def test_configured_stopwords_extend_the_english_defaults(self) -> None:
        for day in range(2, 12):
            write(self.root, f"2026-01-{day:02d}.md",
                  f"# 2026-01-{day:02d}\n\n## Standup\n\n## Kanban\n\nblocked\n")
        plain = scan_vault(self.root)
        route = next(r for r in plain.routes if r.name == "daily-notes")
        self.assertIn("kanban", route.triggers)
        self.assertEqual(plain.stopwords, [])
        # An existing routes.json names a vault-specific stopword: init honours it
        # and writes it back so the router tokenizes prompts the same way.
        write(self.root, ".context/routes.json", '{"routes": {}, "stopwords": ["Kanban"]}')
        configured = scan_vault(self.root)
        route = next(r for r in configured.routes if r.name == "daily-notes")
        self.assertNotIn("kanban", route.triggers)
        self.assertIn("standup", route.triggers)
        self.assertEqual(render_config(configured)["stopwords"], ["kanban"])
        # An explicit argument wins over the file; English defaults always apply.
        explicit = scan_vault(self.root, stopwords=[])
        route = next(r for r in explicit.routes if r.name == "daily-notes")
        self.assertIn("kanban", route.triggers)
        self.assertNotIn("the", route.triggers)

    def test_malformed_stopwords_in_an_existing_config_are_ignored(self) -> None:
        write(self.root, "Reference/glossary.md", "# Glossary\n\n## Terms\n\nx\n")
        write(self.root, "Reference/style-guide.md", "# Style guide\n\n## Voice\n\ny\n")
        write(self.root, ".context/routes.json", '{"stopwords": "glossary"}')
        result = scan_vault(self.root)
        self.assertEqual(result.stopwords, [])
        self.assertTrue(result.routes)


    def test_note_folders_are_not_excluded_by_a_substring(self) -> None:
        # "dist" in "Distributed", "temp" in "Contemporary"/"Temperature": whole
        # tokens only. A "Resources" folder of notes is notes, not attachments.
        for folder in ("Distributed Systems", "Contemporary Art", "Temperature Logs",
                       "Resources", "Buildings"):
            write(self.root, f"{folder}/one.md", "# One\n\n## Scope\n\nbody\n")
            write(self.root, f"{folder}/two.md", "# Two\n\n## Scope\n\nbody\n")
        write(self.root, "dist/bundle.md", "# built\n")
        write(self.root, "backup/copy.md", "# copy\n")
        write(self.root, "Old Backups/copy.md", "# copy\n")
        (self.root / "images").mkdir()
        for name in ("a.png", "b.jpg", "c.pdf"):
            (self.root / "images" / name).write_bytes(b"stub")
        prefixes = {prefix for prefix, _ in scan_vault(self.root).excluded}
        for kept in ("Distributed Systems/", "Contemporary Art/", "Temperature Logs/",
                     "Resources/", "Buildings/"):
            self.assertNotIn(kept, prefixes)
        for dropped in ("dist/", "backup/", "Old Backups/", "images/"):
            self.assertIn(dropped, prefixes)

    # -- the generated config must load (A-04) and the report stays relative (A-20)

    def test_init_never_writes_a_prefix_the_loader_refuses(self) -> None:
        if os.name == "nt":
            # Windows forbids these two POSIX fixture names. Exercise the same
            # route-writer boundary with the names returned by a scan instead.
            written, omitted = writable_exclusions([
                ("Archive: 2024/", "generated archive folder"),
                ("Old backup /", "generated backup folder"),
            ])
            self.assertEqual(written, [])
            omitted = dict(omitted)
            self.assertIn("skips its files as unsupported names", omitted["Archive: 2024/"])
            self.assertIn("its notes are indexed unless you rename the folder",
                          omitted["Old backup /"])
            return
        for number in (1, 2, 3):
            write(self.root, f"Archive: 2024/old-{number}.md", "# Old\n\nold text\n")
            write(self.root, f"Old backup /copy-{number}.md", "# Copy\n\ncopy\n")
        write(self.root, "Notes/one.md", "# One\n\n## Scope\n\nbody\n")
        write(self.root, "Notes/two.md", "# Two\n\n## Scope\n\nbody\n")
        result = scan_vault(self.root)
        config = render_config(result)
        source_policy.parse_config(json.dumps(config))          # loads: no ConfigError
        self.assertNotIn("Archive: 2024/", config["exclude_prefixes"])
        self.assertNotIn("Old backup /", config["exclude_prefixes"])
        omitted = dict(result.omitted_exclusions)
        self.assertIn("skips its files as unsupported names", omitted["Archive: 2024/"])
        self.assertIn("its notes are indexed unless you rename the folder",
                      omitted["Old backup /"])
        self.assertEqual(len(config["_exclusions_not_written"]), 2)
        lines: list[str] = []
        print_report(result, self.root / ".context" / "routes.json", True, emit=lines.append)
        report = "\n".join(lines)
        self.assertIn("Detected as noise but not written", report)
        self.assertIn("Archive: 2024/", report)

    def test_init_output_names_no_absolute_path(self) -> None:
        write(self.root, "Reference/glossary.md", "# Glossary\n\n## Terms\n\nx\n")
        write(self.root, "Reference/style-guide.md", "# Style guide\n\n## Voice\n\ny\n")
        done = subprocess.run([sys.executable, "-m", "context_layer.cli", "init", str(self.root)],
                              cwd=REPO, capture_output=True, text=True,
                              env=isolated_home_env(os.environ, str(self.root)))
        self.assertEqual(done.returncode, 0, done.stderr)
        output = done.stdout + done.stderr
        self.assertIn("Scanned .\n", output)
        self.assertIn("Wrote .context/routes.json\n", output)
        for absolute in {str(self.root), str(self.root.resolve())}:
            self.assertNotIn(absolute, output)
        self.assertFalse([word for word in output.split() if word.startswith(os.sep)])

    def test_heading_triggers_ignore_fenced_code(self) -> None:
        fence = "```sh\n# quokkainstall\n```\n"
        for day in range(2, 12):
            write(self.root, f"2026-01-{day:02d}.md",
                  f"# 2026-01-{day:02d}\n\n## Standup\n\n{fence}blocked\n")
        route = next(r for r in scan_vault(self.root).routes if r.name == "daily-notes")
        self.assertIn("standup", route.triggers)
        self.assertNotIn("quokkainstall", route.triggers)

    def test_note_headings_are_read_as_utf8_whatever_the_locale(self) -> None:
        """E-17: a non-ASCII heading must survive a non-UTF-8 locale unchanged."""
        write(self.root, "Notes/one.md", "# Caf\u00e9 No\u00ebl\n\n## Overview\n\nbody\n")
        probe = ("import json, locale, sys\n"
                 "from pathlib import Path\n"
                 "sys.path.insert(0, sys.argv[1])\n"
                 "from context_layer.vault_scan import heading_tokens\n"
                 "counter = heading_tokens(Path(sys.argv[2]), [Path('Notes/one.md')])\n"
                 "print(json.dumps([locale.getpreferredencoding(False), sorted(counter)]))\n")
        env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0")
        env.pop("PYTHONIOENCODING", None)
        done = subprocess.run([sys.executable, "-c", probe, str(REPO), str(self.root)],
                              env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(done.returncode, 0, done.stderr)
        encoding, tokens = json.loads(done.stdout)
        if encoding.lower().replace("-", "").replace("_", "") in ("utf8", "utf8sig"):
            self.skipTest("this Python cannot be made to use a non-UTF-8 locale encoding")
        self.assertTrue(any("\u00eb" in token for token in tokens), tokens)
        self.assertTrue(any("\u00e9" in token for token in tokens), tokens)
        self.assertFalse(any("\ufffd" in token for token in tokens), tokens)


if __name__ == "__main__":
    unittest.main(verbosity=2)
