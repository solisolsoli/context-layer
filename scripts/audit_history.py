#!/usr/bin/env python3
"""Audit the whole git history, not only the tracked files, before it becomes public.

    python3 scripts/audit_history.py                  # every ref (like git log --all)
    python3 scripts/audit_history.py --rev main       # only what main reaches
    python3 scripts/audit_history.py --accept BLOB    # a blob the owner decided to publish as is

A first push publishes every commit, and every file version in them. The leak and
English audits in CONTRIBUTING.md look at the tracked files of one checkout; this
looks at every blob of every commit reachable from the chosen refs, and at every
commit message, for:

- letters outside ASCII (prose in another language), except the letters
  ALLOWED_LETTERS permits in named files (fixtures, author names);
- home-directory paths (`/Users/<name>`, `/home/<name>`, `C:\\Users\\<name>`), except the
  placeholder names in HOME_PLACEHOLDERS;
- e-mail addresses, except no-reply addresses and reserved placeholder domains (example.com,
  example.org, example.net and their subdomains; the `.test`, `.example`, `.invalid` and
  `.localhost` top-level domains, which cannot belong to a real mailbox);
- private-vault traces: the salted word and word-pair digests that
  `tests/test_harden.py` (PRIVATE_TRACE_DIGESTS) guards the tracked files
  against. The strings themselves are stored nowhere.

Output: one line per finding (kind, path, the first commit that has it, the
blob id, the line, a masked excerpt), then the commit-message trailers
(informational: they become public too), then a summary. Exit 1 when a finding
was not accepted, 0 when there is none, 2 on a usage error.

Local branches that will not be pushed count too; delete them first, or pass
`--rev` for each ref you will push. Hosted CI runs this in the release workflow
(`.github/workflows/release-audit.yml`), not on every push.

Python 3.10+; standard library only; it only reads the repository.
"""
from __future__ import annotations

import argparse
from collections import Counter
import fnmatch
import hashlib
from pathlib import Path
import re
import subprocess
import unicodedata

REPO = Path(__file__).resolve().parents[1]
TRACE_SALT = "context-layer-trace:"
PER_BLOB_LIMIT = 5

# The letters specific to Turkish (dotless and dotted I, g and s with breve or cedilla) are
# never allowed anywhere except the dotted capital I fixtures named below.
# The character class two tests use to prove the templates contain no Turkish letters.
TURKISH_CLASS = "\u00e7\u011f\u0131\u00f6\u015f\u00fc\u00c7\u011e\u0130\u00d6\u015e\u00dc"

# (path pattern, letters allowed there, why). Written as escapes so this file stays ASCII.
# The list matches the tracked tree: tests/test_release_tooling.py fails when an entry names
# a file that no longer needs it, and when the tree carries a letter that is not listed.
# Extend it in the same change that adds a deliberate non-ASCII fixture; prose in another
# language is never allowed.
ALLOWED_LETTERS = (
    ("docs/design-rationale.md", "\u00e9\u00e4\u00f6", "author names in the references"),
    ("CHANGELOG.md", "\u00df\u0130", "the case-folding fixes name their example words"),
    ("docs/cli.md", "\u00df\u0130", "the case-folding examples"),
    ("router/textfold.py", "\u00df\u0130", "comments naming the folded example words"),
    ("context_layer/synapse.py", "\u0130", "a comment on the dotted capital I"),
    ("tests/test_harden.py", "\u0130", "the dotted capital I fixture"),
    ("tests/test_synapse.py", "\u00e9\u00ef\u0130", "byte-preservation and case-folding fixtures"),
    ("tests/test_boundaries.py", "\u00ef\u00e9", "a byte-preservation fixture"),
    ("tests/test_memory.py", "\u00ef\u00e9", "a byte-preservation fixture"),
    ("tests/test_session_evidence.py", "\u00e9", "a file-name fixture"),
    ("tests/test_incremental_index.py", "\u00df\u00e9\u00ef\u00fc", "file-name fixtures (Unicode names)"),
    ("tests/test_integrity.py",
     "\u00df\u00e9\u00ef\u0130\u038c\u0393\u039b\u039f\u03a3\u03b3\u03bf\u03c2\u03cc\u1e9e\u1f80"
     "\u3131\ufb01\uff21\uff26\uff4c\uff55\uff76",
     "tokenizer fixtures: ligatures, full-width forms, Greek, Hangul, dotted capital I"),
)

HOME_RE = re.compile(r"(?<![\w/])(?:/Users/|/home/|[A-Za-z]:\\+Users\\+)([A-Za-z0-9._-]+)")
HOME_PLACEHOLDERS = {"me", "you", "user", "username", "example", "name", "shared"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
EMAIL_DOMAINS = {"example.com", "example.org", "example.net"}
# RFC 2606 / RFC 6761 reserved names cannot belong to a real mailbox: fixtures may use them.
RESERVED_TLDS = {"test", "example", "invalid", "localhost"}
WORD_RE = re.compile(r"[^\W_]+")


def git(*args: str, stdin: bytes | None = None) -> bytes:
    done = subprocess.run(["git", *args], cwd=REPO, input=stdin, capture_output=True)
    if done.returncode != 0:
        raise SystemExit(f"audit_history: git {' '.join(args)} failed: "
                         f"{done.stderr.decode('utf-8', 'replace').strip()}")
    return done.stdout


def private_digests() -> set[str]:
    """PRIVATE_TRACE_DIGESTS as tests/test_harden.py defines it at this checkout."""
    try:
        source = (REPO / "tests" / "test_harden.py").read_text(encoding="utf-8")
    except OSError:
        return set()
    block = re.search(r"PRIVATE_TRACE_DIGESTS\s*=\s*frozenset\(\{(.*?)\}\)", source, re.S)
    return set(re.findall(r"[0-9a-f]{64}", block.group(1))) if block else set()


def trace_digests(text: str) -> dict[str, int]:
    """Digest -> first line number, for every word and adjacent word pair (as test_harden)."""
    found: dict[str, int] = {}
    previous = None
    for number, line in enumerate(text.splitlines(), 1):
        for word in WORD_RE.findall(line.casefold()):
            for gram in (word, f"{previous} {word}" if previous else None):
                if gram:
                    digest = hashlib.sha256((TRACE_SALT + gram).encode("utf-8")).hexdigest()
                    found.setdefault(digest, number)
            previous = word
    return found


def placeholder_domain(domain: str) -> bool:
    domain = domain.lower().rstrip(".")
    return (domain in EMAIL_DOMAINS or domain.rsplit(".", 1)[-1] in RESERVED_TLDS
            or any(domain.endswith("." + name) for name in EMAIL_DOMAINS))


def allowed_letters(path: str) -> str:
    return "".join(letters for pattern, letters, _ in ALLOWED_LETTERS if fnmatch.fnmatch(path, pattern))


def mask(text: str) -> str:
    return text[:2] + "..." if len(text) > 2 else text


def excerpt(line: str, at: int) -> str:
    start = max(0, at - 30)
    return line[start:at + 30].strip().replace("\t", " ")


def scan_text(text: str, path: str, digests: set[str]) -> list[tuple[str, int, str]]:
    """(kind, line number, detail) for one text."""
    found = []
    allowed = allowed_letters(path)
    for number, line in enumerate(text.splitlines(), 1):
        bad = [(i, ch) for i, ch in enumerate(line)
               if ord(ch) > 127 and unicodedata.category(ch).startswith("L") and ch not in allowed]
        if bad:
            letters = "".join(sorted({ch for _, ch in bad}))
            found.append(("non-ascii", number, f"letters {letters}: {excerpt(line, bad[0][0])}"))
        for match in HOME_RE.finditer(line):
            if match.group(1).lower() not in HOME_PLACEHOLDERS:
                found.append(("home-path", number, match.group(0)[:-len(match.group(1))]
                              + mask(match.group(1))))
        for match in EMAIL_RE.finditer(line):
            address = match.group(0).lower()
            if "noreply" in address or placeholder_domain(match.group(1)):
                continue
            found.append(("e-mail", number, mask(match.group(0)) + "@" + match.group(1)))
    for digest, number in sorted(trace_digests(text).items(), key=lambda item: item[1]):
        if digest in digests:
            found.append(("private-trace", number, f"digest {digest[:12]}"))
    return found


def scan_bytes(data: bytes) -> list[tuple[str, int, str]]:
    """Binary blobs: only ASCII home paths and e-mail addresses."""
    text = data.decode("latin-1")
    found = []
    for match in HOME_RE.finditer(text):
        if match.group(1).lower() not in HOME_PLACEHOLDERS:
            found.append(("home-path", 0, "binary: " + match.group(0)[:-len(match.group(1))]
                          + mask(match.group(1))))
    for match in EMAIL_RE.finditer(text):
        if "noreply" not in match.group(0).lower() and not placeholder_domain(match.group(1)):
            found.append(("e-mail", 0, "binary: " + mask(match.group(0)) + "@" + match.group(1)))
    return found


def read_objects(shas: list[str]) -> dict[str, bytes]:
    if not shas:
        return {}
    out = git("cat-file", "--batch", stdin=("\n".join(shas) + "\n").encode("ascii"))
    objects, at = {}, 0
    for sha in shas:
        end = out.index(b"\n", at)
        header = out[at:end].split()
        if len(header) < 3:
            raise SystemExit(f"audit_history: cannot read object {sha}")
        size = int(header[2])
        objects[sha] = out[end + 1:end + 1 + size]
        at = end + 1 + size + 1
    return objects


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--rev", action="append", default=None,
                        help="A ref to audit (repeatable; default: every ref, like --all).")
    parser.add_argument("--repo", type=Path, default=None,
                        help="Repository to audit (default: the checkout this script is in).")
    parser.add_argument("--accept", action="append", default=[], metavar="BLOB",
                        help="A blob id (or a unique prefix of 7+ characters) the owner decided "
                             "to publish as is; its findings are listed but do not fail.")
    args = parser.parse_args(argv)
    global REPO
    if args.repo is not None:
        REPO = args.repo.resolve()
    if any(len(prefix) < 7 for prefix in args.accept):
        parser.error("--accept needs at least 7 characters of a blob id")
    refs = args.rev or ["--all"]
    commits = git("rev-list", "--reverse", *refs).decode("ascii").split()
    if not commits:
        parser.error("no commits reachable from " + " ".join(refs))
    first: dict[tuple[str, str], str] = {}
    for commit in commits:
        listing = git("ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0")
        for entry in listing:
            if not entry:
                continue
            meta, _, path = entry.partition(b"\t")
            mode, kind, sha = meta.decode("ascii").split()
            if kind == "blob" and mode != "160000":
                first.setdefault((sha, path.decode("utf-8", "surrogateescape")), commit)
    blobs = read_objects(sorted({sha for sha, _ in first}))
    messages = read_objects(commits)
    digests = private_digests()

    findings = []                                  # (kind, path, commit, blob, line, detail)
    for (sha, path), commit in sorted(first.items(), key=lambda item: (item[0][1], item[1])):
        data = blobs[sha]
        try:
            hits = scan_text(data.decode("utf-8"), path, digests)
        except UnicodeDecodeError:
            hits = scan_bytes(data)
        for kind, number, detail in hits[:PER_BLOB_LIMIT]:
            findings.append((kind, path, commit, sha, number, detail))
        if len(hits) > PER_BLOB_LIMIT:
            findings.append(("more", path, commit, sha, 0, f"{len(hits) - PER_BLOB_LIMIT} more in this blob"))
    trailers: Counter[str] = Counter()
    for commit in commits:
        raw = messages[commit].decode("utf-8", "replace")
        message = raw.split("\n\n", 1)[1] if "\n\n" in raw else ""
        for kind, number, detail in scan_text(message, "(commit message)", digests):
            findings.append((kind, "(commit message)", commit, commit, number, detail))
        for line in message.splitlines():
            if re.match(r"^[A-Za-z][A-Za-z-]*: \S", line) and not line.startswith("http"):
                key = line.split(":", 1)[0]
                if key.lower() in {"co-authored-by", "signed-off-by", "reviewed-by", "reported-by",
                                   "acked-by", "tested-by", "helped-by"}:
                    trailers[line.strip()] += 1

    accepted = [f for f in findings if any(f[3].startswith(prefix) for prefix in args.accept)]
    failing = [f for f in findings if f not in accepted]
    for kind, path, commit, sha, number, detail in findings:
        mark = "accepted " if (kind, path, commit, sha, number, detail) in accepted else ""
        where = f"{path}:{number}" if number else path
        print(f"{mark}{kind}  {where}  first in {commit[:9]}  blob {sha[:9]}  {detail}")
    if trailers:
        print("\ntrailers in commit messages (they become public with the history):")
        for line, count in trailers.most_common():
            print(f"  {count:4d}  {line}")
    kinds = Counter(f[0] for f in failing if f[0] != "more")
    print(f"\naudited {len(commits)} commits and {len({sha for sha, _ in first})} blobs "
          f"({' '.join(refs)}); private-trace digests: {len(digests)}")
    if failing:
        print("FAILED: " + ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
              + ". Decide per docs/publishing-checklist.md step 1b (rewrite the history, or accept "
              "the listed blobs with --accept).")
        return 1
    print("ok: no finding" + (f" ({len(accepted)} accepted)" if accepted else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
