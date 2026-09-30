#!/usr/bin/env python3
"""Shared helpers for the adapters in this directory.

Nothing clever lives here. It is the tokenising, file-walking and packet-
printing that all three adapters would otherwise duplicate, kept in one place so
that a comparison between them is a comparison of *retrieval*, not of packet
formatting.

Stdlib only. Adapters never write to the vault.
"""
from __future__ import annotations

import os
import re
import sys

# A deliberately short stop list. It is not linguistics; it is the handful of
# words that otherwise match every document in any vault and make a lexical
# baseline look worse than it is. Keeping it short keeps the baseline honest:
# the control should be the best naive system you can write in an hour, not a
# straw man.
STOPWORDS = {
    "the", "and", "for", "are", "but", "not", "you", "our", "was", "were",
    "with", "that", "this", "have", "has", "had", "from", "what", "when",
    "how", "why", "who", "does", "did", "do", "is", "it", "its", "of", "to",
    "in", "on", "at", "as", "by", "or", "be", "we", "us", "my", "me", "i",
    "a", "an", "if", "so", "can", "will", "would", "should", "could", "about",
    "there", "their", "them", "they", "he", "she", "his", "her", "all", "any",
    "get", "got", "need", "needs", "want", "please", "tell", "give", "let",
    "still", "also", "just", "than", "then", "too", "very", "some", "one",
    "two", "yet", "out", "up", "off", "over", "into", "before", "after",
}

WORD_RE = re.compile(r"[a-z0-9][a-z0-9_\-]*")


def query_terms(prompt: str, min_len: int = 3) -> list[str]:
    """Prompt -> ordered, de-duplicated search terms.

    Case-folded, stop-worded, short tokens dropped. If that leaves nothing (a
    prompt made entirely of stop words), fall back to every token of length >= 2
    so the adapter still searches for something instead of silently returning an
    empty packet. An empty packet is the failure mode this whole harness exists
    to catch, so no adapter here is allowed to produce one quietly.
    """
    raw = WORD_RE.findall(prompt.lower())
    terms = [t for t in raw if len(t) >= min_len and t not in STOPWORDS]
    if not terms:
        terms = [t for t in raw if len(t) >= 2]
    seen, out = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def walk_vault(vault: str, exts: tuple[str, ...], skip_hidden: bool = True) -> list[str]:
    """Every file under `vault` with one of `exts`, as absolute paths, sorted."""
    found = []
    for root, dirs, files in os.walk(vault):
        if skip_hidden:
            dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if skip_hidden and name.startswith("."):
                continue
            if exts and not name.lower().endswith(exts):
                continue
            found.append(os.path.join(root, name))
    return sorted(found)


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def rel(path: str, vault: str) -> str:
    try:
        # Evidence paths use one spelling on every host; filesystem access still
        # uses native paths. Do not replace literal backslashes in POSIX names.
        return os.path.relpath(path, vault).replace(os.sep, "/")
    except ValueError:
        return path


def render_packet(adapter: str, prompt: str, chosen: list[tuple[str, str, str]],
                  note: str = "") -> str:
    """Render the retrieved context.

    `chosen` is [(relative_path, score_line, body), ...] already ranked.

    The shape matters for one reason only: `evaluate.py` scores a hit when the
    expected source's *name* appears in stdout, so the relative path is printed
    verbatim on its own line. The body is printed too, because a packet that
    names a file without carrying its text is exactly the cheat eval/README.md
    warns about, and a baseline that cheats is not a baseline.
    """
    out = [
        f"# Retrieved context ({adapter})",
        "",
        "## Query",
        "```text",
        prompt,
        "```",
        "",
    ]
    if note:
        out += [note, ""]
    if not chosen:
        out += ["No source matched this query.", ""]
    for i, (path, score_line, body) in enumerate(chosen, 1):
        out += [f"### R{i:03d} — `{path}`", f"- {score_line}", "", body.strip(), ""]
    return "\n".join(out)


def add_common_args(ap, default_vault: str = "fixtures/docs") -> None:
    ap.add_argument("--vault", default=default_vault,
                    help="Directory to search (default: %(default)s).")
    ap.add_argument("--top-k", type=int, default=3,
                    help="How many documents to put in the packet (default: %(default)s).")
    ap.add_argument("--ext", default=".md,.txt,.markdown",
                    help="Comma-separated file extensions to consider (default: %(default)s).")
    ap.add_argument("--max-chars", type=int, default=0,
                    help="Truncate each document body to N chars; 0 = whole document.")
    ap.add_argument("prompt", nargs="+",
                    help="The user prompt. evaluate.py appends it as the final argv token.")


def resolve_vault(vault: str) -> str:
    path = os.path.abspath(os.path.expanduser(vault))
    if not os.path.isdir(path):
        sys.exit(f"adapter error: --vault {vault!r} is not a directory "
                 f"(resolved to {path}). Point it at the folder your notes live in.")
    return path


def clip(body: str, max_chars: int) -> str:
    if max_chars and len(body) > max_chars:
        return body[:max_chars].rstrip() + "\n\n[...truncated by --max-chars]"
    return body
