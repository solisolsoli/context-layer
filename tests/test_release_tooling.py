#!/usr/bin/env python3
"""Release tooling in scripts/: the distribution check's content rules, the sdist
normaliser and the history audit.

Everything runs on archives, repositories and files built here in a temporary
directory (HOME points there too); nothing is built with the real backend and
nothing is installed.
"""
import gzip
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

REPO = Path(os.environ.get("TEST_REPO_HOME", Path(__file__).resolve().parents[1]))


def load(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_distribution = load("check_distribution")
normalize_sdist = load("normalize_sdist")
audit_history = load("audit_history")


class TempDir(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(os.environ, HOME=str(self.root))


class WheelContents(TempDir):
    """E-07: the content pass fails on a wheel that lost its starter-brain templates."""

    def wheel(self, with_brain=True):
        path = self.root / "context_layer-9.9.9-py3-none-any.whl"
        with zipfile.ZipFile(path, "w") as archive:
            for name in check_distribution.WHEEL_REQUIRED:
                archive.writestr(name, "x")
            if with_brain:
                archive.writestr("context_layer/templates/starter-brain/README.md", "x")
            for name in check_distribution.WHEEL_LICENSES:
                archive.writestr(f"context_layer-9.9.9.dist-info/licenses/{name}", "x")
        return path

    def test_a_complete_wheel_passes(self):
        self.assertEqual(check_distribution.check_wheel(self.wheel()),
                         sorted(check_distribution.WHEEL_LICENSES))

    def test_a_wheel_without_the_starter_brain_fails(self):
        with self.assertRaises(SystemExit) as caught:
            check_distribution.check_wheel(self.wheel(with_brain=False))
        self.assertIn("starter-brain", str(caught.exception))

    def test_a_missing_sdist_fails_unless_no_sdist(self):
        wheel = self.wheel()
        with self.assertRaises(SystemExit) as caught:
            check_distribution.check_sdist(wheel, "9.9.9")
        self.assertIn("--no-sdist", str(caught.exception))
        with open(os.devnull, "w") as quiet:
            original, sys.stderr = sys.stderr, quiet
            try:
                self.assertIn("not checked", check_distribution.check_sdist(wheel, "9.9.9", required=False))
            finally:
                sys.stderr = original

    def test_no_bare_assert_in_the_check(self):
        # `python -O` strips assert statements; every condition goes through check().
        source = (REPO / "scripts" / "check_distribution.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"(?m)^\s*assert\b")


class NormalizeSdist(TempDir):
    """E-18: two builds of the same files give one sha256, with no account names."""

    def build(self, name, mtime, owner):
        files = {"pkg-1.0/b.txt": b"second\n", "pkg-1.0/a.txt": b"first\n",
                 "pkg-1.0/run.sh": b"#!/bin/sh\n"}
        path = self.root / name / "pkg-1.0.tar.gz"
        path.parent.mkdir()
        with tarfile.open(path, "w:gz") as archive:
            for member, data in files.items():
                info = tarfile.TarInfo(member)
                info.size, info.mtime = len(data), mtime + 0.25
                info.uname = info.gname = owner
                info.mode = 0o755 if member.endswith(".sh") else 0o664
                archive.addfile(info, io.BytesIO(data))
        return path

    def test_two_builds_become_identical(self):
        first = self.build("one", 1_800_000_000, "builder-one")
        second = self.build("two", 1_900_000_000, "builder-two")
        self.assertNotEqual(first.read_bytes(), second.read_bytes())
        digests = {normalize_sdist.normalize(path, 1_700_000_000) for path in (first, second)}
        self.assertEqual(len(digests), 1)
        self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), digests.pop())
        with tarfile.open(first) as archive:
            members = archive.getmembers()
        self.assertEqual([m.name for m in members], sorted(m.name for m in members))
        for member in members:
            self.assertEqual((member.uname, member.gname, member.uid, member.gid, member.mtime),
                             ("root", "root", 0, 0, 1_700_000_000))
        modes = {m.name: m.mode for m in members}
        self.assertEqual(modes["pkg-1.0/run.sh"], 0o755)
        self.assertEqual(modes["pkg-1.0/a.txt"], 0o644)
        with gzip.open(first) as packed:
            self.assertTrue(packed.read())          # still a valid gzip stream
        self.assertEqual(int.from_bytes(first.read_bytes()[4:8], "little"), 1_700_000_000)

    def test_contents_are_unchanged(self):
        path = self.build("one", 1_800_000_000, "builder")
        normalize_sdist.normalize(path, 1_700_000_000)
        with tarfile.open(path) as archive:
            self.assertEqual(archive.extractfile("pkg-1.0/a.txt").read(), b"first\n")

    def test_needs_an_epoch(self):
        path = self.build("one", 1_800_000_000, "builder")
        env = dict(self.env)
        env.pop("SOURCE_DATE_EPOCH", None)
        done = subprocess.run([sys.executable, str(REPO / "scripts" / "normalize_sdist.py"), str(path)],
                              capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 2)
        self.assertIn("SOURCE_DATE_EPOCH", done.stderr)


class AuditHistory(TempDir):
    """E-05: every historical blob and commit message is audited, not only HEAD."""

    PLANTED = "lanternfish"          # stands in for a private word; only its digest is stored

    def git(self, *args):
        done = subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, env=self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def commit(self, files, message):
        for name, text in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if text is None:
                path.unlink()
            else:
                path.write_text(text, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def setUp(self):
        super().setUp()
        self.env.update(GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.com",
                        GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.com",
                        GIT_CONFIG_NOSYSTEM="1")
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        digest = audit_history.hashlib.sha256(
            (audit_history.TRACE_SALT + self.PLANTED).encode("utf-8")).hexdigest()
        self.guard = ("PRIVATE_TRACE_DIGESTS = frozenset({\n    \"%s\",\n})\n" % digest)
        self.commit({"tests/test_harden.py": self.guard, "README.md": "# Clean\n\nAll English.\n"},
                    "start")

    def audit(self, *extra):
        out = io.StringIO()
        original = sys.stdout
        sys.stdout = out
        try:
            code = audit_history.main(["--repo", str(self.repo), *extra])
        finally:
            sys.stdout = original
            audit_history.REPO = REPO
        return code, out.getvalue()

    def test_a_clean_history_passes(self):
        code, out = self.audit()
        self.assertEqual(code, 0, out)
        self.assertIn("ok: no finding", out)

    def test_a_removed_file_still_fails(self):
        self.commit({"notes.md": "A fixture line with \u00f0 and \u00fe.\n"}, "add")
        self.commit({"notes.md": None}, "remove it again")
        code, out = self.audit()
        self.assertEqual(code, 1)
        self.assertIn("non-ascii  notes.md:1", out)

    def test_home_paths_emails_and_private_traces(self):
        # Built at run time so this file itself carries no path or address the audit would flag.
        home = "/Users/" + "quokka" + "/vault"
        mail = "reviewer" + "@" + "widgets-corp.io"
        self.commit({"a.md": f"See {home} and /home/me/vault.\n",
                     "b.md": f"Mail {mail} or noreply@example.org.\n",
                     "c.md": "A Lanternfish swam by.\n"}, "add")
        code, out = self.audit()
        self.assertEqual(code, 1)
        self.assertIn("home-path  a.md:1", out)
        self.assertNotIn("/home/me", out)                 # a documented placeholder
        self.assertNotIn("quokka", out)                   # names are masked in the report
        self.assertIn("e-mail  b.md:1", out)
        self.assertNotIn("noreply", out.split("trailers")[0])
        self.assertIn("private-trace  c.md:1", out)
        self.assertNotIn(self.PLANTED, out.casefold())

    def test_reserved_placeholder_domains_are_not_findings(self):
        self.commit({"p.md": "Mail a@corp.test, b@judge.example, c@api.example.test, "
                             "d@mail.example.com, e@host.invalid.\n"}, "placeholders")
        code, out = self.audit()
        self.assertEqual(code, 0, out)
        self.commit({"q.md": "Mail f@" + "widgets-corp.io and g@" + "example.com.io.\n"}, "real")
        code, out = self.audit()
        self.assertEqual(code, 1)
        self.assertIn("e-mail  q.md:1", out)

    def test_commit_messages_and_trailers(self):
        self.commit({"d.md": "fine\n"}, "Fixture \u00f0\n\nCo-Authored-By: Helper <noreply@example.org>")
        code, out = self.audit()
        self.assertEqual(code, 1)
        self.assertIn("non-ascii  (commit message)", out)
        self.assertIn("1  Co-Authored-By: Helper <noreply@example.org>", out)

    def test_allowlisted_letters_and_accepted_blobs(self):
        self.commit({"docs/design-rationale.md": "Guti\u00e9rrez et al.\n"}, "reference")
        self.assertEqual(self.audit()[0], 0)
        self.commit({"docs/design-rationale.md": "Guti\u00e9rrez et al. \u00f0\n"}, "not a name")
        code, out = self.audit()
        self.assertEqual(code, 1)
        blob = self.git("rev-parse", "HEAD:docs/design-rationale.md").strip()
        code, out = self.audit("--accept", blob[:9])
        self.assertEqual(code, 0, out)
        self.assertIn("accepted non-ascii", out)

    def test_rev_limits_the_audit(self):
        self.git("checkout", "-q", "-b", "scratch")
        self.commit({"e.md": "\u00fe\n"}, "local only")
        self.git("checkout", "-q", "main")
        self.assertEqual(self.audit("--rev", "main")[0], 0)
        self.assertEqual(self.audit()[0], 1)


class PackagingMetadata(unittest.TestCase):
    """F2-32, F2-36: what pyproject.toml declares matches what the package ships and supports."""

    def setUp(self):
        self.text = (REPO / "pyproject.toml").read_text(encoding="utf-8")

    def test_package_data_names_the_example_configs_not_a_json_glob(self):
        # `example-vault/.context/*.json` also matched the index-manifest.json `make test`
        # writes there, and `pip wheel .` from a used checkout bundled it.
        self.assertNotIn("example-vault/.context/*.json", self.text)
        self.assertIn("example-vault/.context/routes.json", self.text)
        self.assertIn("example-vault/.context/facts.json", self.text)

    def test_only_verified_platforms_are_classified(self):
        # README and SCOPE label Linux as expected but unverified.
        self.assertNotIn("Operating System :: POSIX :: Linux", self.text)
        self.assertIn("Operating System :: MacOS", self.text)


class ReleaseTree(TempDir):
    """F2-01: the release is one commit holding only the current tree; the gate must pass on it."""

    def tracked_files(self):
        done = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True)
        if done.returncode != 0:
            self.skipTest("not a git checkout (an sdist carries no history to audit)")
        return [name for name in done.stdout.decode("utf-8").split("\0")
                if name and (REPO / name).is_file() and not (REPO / name).is_symlink()]

    def test_the_current_tree_passes_the_history_audit_as_one_commit(self):
        snapshot = self.root / "snapshot"
        for name in self.tracked_files():
            target = snapshot / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((REPO / name).read_bytes())
        env = dict(self.env, GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.com",
                   GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.com",
                   GIT_CONFIG_NOSYSTEM="1")
        for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                     ["commit", "-q", "-m", "Release snapshot"]):
            done = subprocess.run(["git", *args], cwd=snapshot, capture_output=True, text=True, env=env)
            self.assertEqual(done.returncode, 0, done.stderr)
        out = io.StringIO()
        original = sys.stdout
        sys.stdout = out
        try:
            code = audit_history.main(["--repo", str(snapshot), "--rev", "HEAD"])
        finally:
            sys.stdout = original
            audit_history.REPO = REPO
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("audited 1 commits", out.getvalue())

    def test_every_allow_list_entry_is_used_by_the_tree(self):
        import fnmatch
        import unicodedata
        files = self.tracked_files()
        for pattern, letters, why in audit_history.ALLOWED_LETTERS:
            with self.subTest(pattern=pattern):
                present = set()
                matched = [name for name in files if fnmatch.fnmatch(name, pattern)]
                self.assertTrue(matched, f"{pattern} ({why}) names no tracked file")
                for name in matched:
                    text = (REPO / name).read_text(encoding="utf-8")
                    present |= {ch for ch in text if ord(ch) > 127
                                and unicodedata.category(ch).startswith("L")}
                unused = set(letters) - present
                self.assertFalse(unused, f"{pattern}: {sorted(unused)} are allowed but not used")


if __name__ == "__main__":
    unittest.main()
