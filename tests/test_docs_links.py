#!/usr/bin/env python3
"""Every relative link in every tracked Markdown file resolves.

For each link, image and reference definition outside code: the target exists
inside the repository, every path component has the exact letter case on disk
(GitHub is case-sensitive, macOS usually is not), and an `#anchor` into a
Markdown file names a heading there (GitHub's slug rules). Links with a scheme
(https:, mailto:) are not fetched.
"""
import os
from pathlib import Path
import re
import subprocess
import unittest
from urllib.parse import unquote

REPO = Path(os.environ.get("TEST_REPO_HOME", Path(__file__).resolve().parents[1]))
LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)"      # [text](target "title")
                  r"|\!\[[^\]]*\]\(([^)\s]+)\)"                             # ![alt](target)
                  r"|<img[^>]*\ssrc=\"([^\"]+)\""                           # <img src="...">
                  r"|<a[^>]*\shref=\"([^\"]+)\""                            # <a href="...">
                  r"|^\s{0,3}\[[^\]]+\]:\s*(\S+)", re.M)                    # [ref]: target
FENCE = re.compile(r"^\s*(```|~~~)")


def strip_code(text):
    """Blank out fenced blocks and inline code, keeping line numbers."""
    out, fence = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            fence = not fence
            out.append("")
            continue
        out.append("" if fence else re.sub(r"`[^`]*`", "", line))
    return "\n".join(out)


def slug(heading):
    heading = re.sub(r"`([^`]*)`", r"\1", heading)
    heading = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    heading = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return heading.replace(" ", "-")


def anchors(path, cache={}):
    if path not in cache:
        seen, found, fence = {}, set(), False
        for line in path.read_text(encoding="utf-8").splitlines():
            if FENCE.match(line):
                fence = not fence
                continue
            match = None if fence else re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
            if match:
                base = slug(match.group(2))
                count = seen.get(base, 0)
                seen[base] = count + 1
                found.add(base if count == 0 else f"{base}-{count}")
        cache[path] = found
    return cache[path]


def tracked_markdown():
    done = subprocess.run(["git", "ls-files", "-z", "*.md"], cwd=REPO, capture_output=True)
    if done.returncode != 0:
        return None
    return [REPO / name for name in done.stdout.decode("utf-8").split("\0") if name]


def problems_in(path):
    text = strip_code(path.read_text(encoding="utf-8"))
    problems = []
    for match in LINK.finditer(text):
        target = next(group for group in match.groups() if group)
        if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):
            continue
        line = text.count("\n", 0, match.start()) + 1
        where = f"{path.relative_to(REPO)}:{line}"
        part, _, fragment = target.partition("#")
        dest = path if not part else (path.parent / unquote(part)).resolve()
        if not dest.exists():
            problems.append(f"{where}: missing target {target}")
            continue
        try:
            dest.relative_to(REPO.resolve())
        except ValueError:
            problems.append(f"{where}: target outside the repository {target}")
            continue
        if part:
            current = path.parent
            for piece in Path(unquote(part)).parts:
                if piece in (".", ".."):
                    current = (current / piece).resolve()
                    continue
                if piece not in {p.name for p in current.iterdir()}:
                    problems.append(f"{where}: letter case of '{piece}' differs on disk in {target}")
                    break
                current = current / piece
        if fragment and dest.suffix == ".md" and fragment.lower() not in anchors(dest):
            problems.append(f"{where}: no heading for #{fragment} in {dest.relative_to(REPO.resolve())}")
    return problems


class RelativeLinks(unittest.TestCase):
    def test_every_relative_link_resolves(self):
        files = tracked_markdown()
        if files is None:
            self.skipTest("not a git checkout")
        self.assertGreater(len(files), 50)
        problems = [p for path in files if path.is_file() for p in problems_in(path)]
        self.assertEqual(problems, [])

    def test_the_checker_finds_a_broken_link(self):
        import tempfile
        with tempfile.TemporaryDirectory(dir=REPO) as tmp:
            doc = Path(tmp) / "doc.md"
            doc.write_text("# Title\n\n[ok](doc.md#title) [gone](missing.md) [bad](doc.md#nope)\n"
                           "```\n[ignored](missing.md)\n```\n", encoding="utf-8")
            found = problems_in(doc)
        self.assertEqual(len(found), 2, found)
        self.assertIn("missing target missing.md", found[0])
        self.assertIn("no heading for #nope", found[1])


class BannedPhrases(unittest.TestCase):
    """ACTIONS E3: the docs describe the mechanism literally (explicit link graph,
    bounded spreading activation), not as a mind or a neural network, and do not
    say the trace shows the model's reasoning (it records which notes were selected)."""

    PHRASES = ("watch your ai think", "neural network")

    def test_readme_and_docs_avoid_the_banned_phrases(self):
        files = [REPO / "README.md", *sorted((REPO / "docs").rglob("*.md"))]
        self.assertGreater(len(files), 5)
        hits = []
        for path in files:
            # Fold line wraps and case so a phrase split over two lines is still found.
            text = " ".join(path.read_text(encoding="utf-8").lower().split())
            hits += [f"{path.relative_to(REPO).as_posix()}: {phrase!r}"
                     for phrase in self.PHRASES if phrase in text]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
