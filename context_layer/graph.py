"""context_layer.graph — the vault's explicit link graph, source-anchored.

Built at `context-layer index` time, after the lexical index, from the notes
that index covers. Every edge records its kind, its source and target note, the
1-based line it sits on and the SHA-256 of the source file at extraction, so a
retrieval can tell whether the edge still describes the bytes on disk.

What counts as a link (and nothing else does):

- `[[Target]]`, `[[Target|alias]]`, `[[Target#Heading]]`, `[[Target#^block]]`
  -> kind `wikilink`; the same with a leading `!` -> kind `embed`;
- `[text](relative/path.md)` or `[text](relative/path)` (optionally
  `#fragment`, `%20` escapes, `<...>`) -> kind `mdlink`; `![alt](path)` -> kind
  `embed`; URLs with a scheme are not links to notes;
- frontmatter `related`, `up`, `parent`, `see_also`/`see-also`, `supersedes`
  (a string or a list; wikilink or plain name) -> kind `frontmatter`.

Links inside code are ignored: fenced blocks (also inside a blockquote or
callout), indented code blocks and inline code spans. Every link is classified
against the files of the vault: a unique note makes an edge; anything else is
recorded with its reason (`missing`, `ambiguous`, `not_indexed`, `attachment`,
`excluded`), never guessed. Resolution follows Obsidian's documented rules;
docs/synapse.md §1 lists them with their sources.

Line numbers count "\\n" only, as editors and `grep -n` do. The graph lives in
`<vault>/.context/graph.sqlite` and is rebuilt atomically (staging file +
os.replace). `context-layer graph health` reports on it, read-only.

Python 3.10+; standard library only.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import string
import sys
import tempfile
import time
import unicodedata
from urllib.parse import unquote

GRAPH_NAME = "graph.sqlite"
GRAPH_SCHEMA_VERSION = "1"
KINDS = ("wikilink", "embed", "mdlink", "frontmatter")
FRONTMATTER_LINK_KEYS = ("related", "up", "parent", "see_also", "see-also", "supersedes")
ALIAS_KEYS = ("aliases", "alias")
NOTE_SUFFIX = ".md"
# Why a link makes no edge (table `unresolved`). The first three leave a note the
# link names out of reach; an `attachment` is a file that is not a note, and an
# `excluded` target lies under an exclusion (its path is never stored).
LINK_REASONS = ("missing", "ambiguous", "not_indexed", "attachment", "excluded")
UNREACHED = ("missing", "ambiguous", "not_indexed")
UNREADABLE = ("the link graph (.context/graph.sqlite) cannot be read; rebuild it with "
              "`context-layer index <vault>`")
REBUILD = "run `context-layer index <vault>`"

SCHEMA = """
CREATE TABLE notes (
    path TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    aliases TEXT NOT NULL,
    out_degree INTEGER NOT NULL DEFAULT 0,
    in_degree INTEGER NOT NULL DEFAULT 0,
    degree INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE edges (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    source_path TEXT NOT NULL,
    target_path TEXT NOT NULL,
    line INTEGER NOT NULL,
    source_sha256 TEXT NOT NULL,
    heading TEXT,
    block TEXT,
    field TEXT
);
CREATE INDEX edges_source ON edges(source_path);
CREATE INDEX edges_target ON edges(target_path);
CREATE TABLE unresolved (
    source_path TEXT NOT NULL,
    line INTEGER NOT NULL,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    target TEXT,
    candidates TEXT
);
CREATE INDEX unresolved_source ON unresolved(source_path);
CREATE TABLE skipped (path TEXT PRIMARY KEY, reason TEXT NOT NULL);
CREATE TABLE frontmatter (path TEXT PRIMARY KEY, status TEXT NOT NULL, ignored INTEGER NOT NULL);
CREATE TABLE graph_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@dataclass
class RawLink:
    """One link as written, before resolution."""

    kind: str
    target: str            # link path as written (no alias, no fragment)
    line: int              # 1-based line in the source file
    heading: str | None = None
    block: str | None = None
    field: str | None = None   # frontmatter key, for kind == frontmatter
    markdown: bool = False     # written as [text](path): relative to the note first


@dataclass
class ParsedNote:
    links: list[RawLink] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    body_start: int = 1     # first 1-based line after the frontmatter
    frontmatter: str = "none"      # none | parsed | unclosed
    frontmatter_ignored: int = 0   # alias/link values outside the subset, left out


FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
QUOTE = re.compile(r"^ {0,3}> ?")
LIST_ITEM = re.compile(r"^ {0,3}(?:[-+*]|\d{1,9}[.)])(?:[ \t]|$)")
ATX_HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
WIKILINK = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")
MDLINK = re.compile(r"(!?)\[(?:[^\[\]\n]|\[[^\[\]\n]*\])*\]\(\s*(<[^>\n]+>|[^\s()]+(?:\([^\s()]*\)[^\s()]*)*)"
                    r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'))?\s*\)")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
BOM = "\ufeff"


def split_lines(text: str) -> list[str]:
    """Lines without their terminators; line i (1-based) is result[i - 1].

    Only "\\n" ends a line (a "\\r" before it is dropped), the way editors and
    `grep -n` count: U+2028, a form feed or NEL inside a line does not start a
    new one, so line numbers agree with `text.count("\\n", 0, offset) + 1`.
    """
    return [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]


def _opening(line: str) -> str:
    """A first line without one leading byte-order mark (U+FEFF)."""
    return line[1:] if line.startswith(BOM) else line


def frontmatter_span(lines: list[str]) -> int:
    """Number of lines the leading `---` frontmatter block occupies (0 if none).

    One leading byte-order mark is ignored, so a note saved with a BOM keeps its
    frontmatter; character and byte offsets still count the BOM.
    """
    if not lines or _opening(lines[0]).strip() != "---":
        return 0
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            return index + 1
    return 0


# The frontmatter subset: `key: value`, `key: [a, b]`, `key:` + `- item`, with plain,
# single-quoted ('' escapes) and double-quoted (backslash escapes) scalars and
# `#` comments. Block scalars (`|`, `>`), multi-line values, nested collections,
# anchors, aliases and tags are left out, never guessed.
KEY = re.compile(r"^([A-Za-z0-9_-]+)[ \t]*:(?:[ \t]+(.*))?$")
ITEM = re.compile(r"^([ \t]*)-(?:[ \t]+(.*))?$")
BLOCK_SCALAR = re.compile(r"^[|>][0-9+-]*(?:[ \t]+#.*)?$")
ESCAPES = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v",
           "f": "\f", "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\",
           "N": "\x85", "_": "\xa0", "L": "\u2028", "P": "\u2029"}
HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}
PLAIN_BAD_START = set("&*!%@`|>{}[],#'\"")
NULLS = {"~", "null", "Null", "NULL"}
IGNORED = None   # a value outside the subset


class _OutsideSubset(Exception):
    """A value the small YAML subset does not cover."""


def _indent(line: str) -> int:
    """Leading columns, tabs expanded to the next multiple of 4."""
    width = 0
    for char in line:
        if char == " ":
            width += 1
        elif char == "\t":
            width += 4 - width % 4
        else:
            break
    return width


def _quoted(value: str, start: int) -> tuple[str, int]:
    """The quoted scalar at value[start] and the index after its closing quote."""
    quote, out, index = value[start], [], start + 1
    while index < len(value):
        char = value[index]
        if quote == "'":
            if char == "'":
                if value[index + 1:index + 2] == "'":
                    out.append("'")
                    index += 2
                    continue
                return "".join(out), index + 1
            out.append(char)
            index += 1
            continue
        if char == '"':
            return "".join(out), index + 1
        if char == "\\":
            code = value[index + 1:index + 2]
            if code in ESCAPES:
                out.append(ESCAPES[code])
                index += 2
                continue
            width = HEX_ESCAPES.get(code)
            digits = value[index + 2:index + 2 + width] if width else ""
            if width and len(digits) == width and all(c in string.hexdigits for c in digits):
                try:
                    out.append(chr(int(digits, 16)))
                except ValueError:
                    raise _OutsideSubset from None
                index += 2 + width
                continue
            raise _OutsideSubset
        out.append(char)
        index += 1
    raise _OutsideSubset          # unterminated: a multi-line quoted scalar


def _blank_or_comment(value: str, index: int) -> bool:
    rest = value[index:]
    return not rest.strip() or (rest[:1] in (" ", "\t") and rest.lstrip().startswith("#"))


def _plain(text: str) -> str:
    """A one-line plain scalar, comment removed; raises _OutsideSubset otherwise."""
    comment = re.search(r"[ \t]#", text)
    if comment:
        text = text[:comment.start()]
    text = text.strip()
    if not text or text in NULLS:
        return ""
    if text[0] in PLAIN_BAD_START or text[:2] in ("- ", "? ", ": ") or text in ("-", "?", ":"):
        raise _OutsideSubset
    if ": " in text or ":\t" in text or text.endswith(":"):
        raise _OutsideSubset      # a mapping, not a string
    return text


def _wikilink_value(text: str) -> str:
    """An unquoted `[[Link]]` value (YAML would read a nested list): kept as written,
    up to a `#` comment outside the brackets."""
    depth = 0
    for index, char in enumerate(text):
        if char == "[":
            depth += 1
        elif char == "]":
            depth = max(0, depth - 1)
        elif char == "#" and depth == 0 and index and text[index - 1] in " \t":
            return text[:index].strip()
    return text.strip()


def _flow_list(text: str) -> list[str]:
    """A complete one-line flow list `[a, "b", 'c', [[d]]]`; raises _OutsideSubset
    for an unbalanced (multi-line) or nested list."""
    items, index = [], 1
    while True:
        while index < len(text) and text[index] in " \t":
            index += 1
        if index >= len(text):
            raise _OutsideSubset
        char = text[index]
        if char == "]":
            index += 1
            break
        if char in "'\"":
            item, index = _quoted(text, index)
            items.append(item)
        elif text.startswith("[[", index):
            end = text.find("]]", index)
            if end < 0:
                raise _OutsideSubset
            items.append(text[index:end + 2])
            index = end + 2
        elif char in "[{":
            raise _OutsideSubset
        else:
            end = index
            while end < len(text) and text[end] not in ",]":
                end += 1
            if end >= len(text):
                raise _OutsideSubset
            raw = text[index:end]
            if re.search(r"[ \t]#", raw):
                raise _OutsideSubset      # a comment inside the list: it goes on below
            items.append(_plain(raw))
            index = end
        while index < len(text) and text[index] in " \t":
            index += 1
        if index < len(text) and text[index] == ",":
            index += 1
            continue
        if index < len(text) and text[index] == "]":
            index += 1
            break
        raise _OutsideSubset
    if not _blank_or_comment(text, index):
        raise _OutsideSubset
    return [item for item in items if item.strip()]


def _values(value: str) -> list[str] | None:
    """The strings one value holds ([] for none); IGNORED when outside the subset."""
    text = value.strip()
    if not text or text.startswith("#"):
        return []
    try:
        if BLOCK_SCALAR.match(text):
            raise _OutsideSubset
        if text.startswith("[["):
            found = _wikilink_value(text)
            return [found] if found else []
        if text.startswith("["):
            return _flow_list(text)
        if text[0] in "'\"":
            scalar, end = _quoted(text, 0)
            if not _blank_or_comment(text, end):
                raise _OutsideSubset
            return [scalar] if scalar.strip() else []
        plain = _plain(text)
        return [plain] if plain else []
    except _OutsideSubset:
        return IGNORED


def _continues(lines: list[str], index: int, end: int, depth: int) -> bool:
    """Does the value on line `index` go on over more-indented lines?"""
    for following in range(index + 1, end):
        raw = lines[following]
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        return _indent(raw) > depth
    return False


def _parse_frontmatter(lines: list[str], span: int,
                       counted: tuple[str, ...] = ALIAS_KEYS + FRONTMATTER_LINK_KEYS
                       ) -> tuple[dict[str, list[tuple[str, int]]], int]:
    """(key -> [(value, 1-based line)], number of values of `counted` keys left out)."""
    result: dict[str, list[tuple[str, int]]] = {}
    ignored = 0
    key = None
    skip_deeper = None           # lines of a left-out multi-line value
    end = max(span - 1, 1)
    for index in range(1, end):
        raw = lines[index]
        if skip_deeper is not None:
            if not raw.strip() or _indent(raw) > skip_deeper:
                continue
            skip_deeper = None
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        item = ITEM.match(raw)
        if item is not None:
            if key is None:
                continue          # an item under no usable key
            depth = _indent(raw)
            values = _values(item.group(2) or "")
            if values is IGNORED or _continues(lines, index, end, depth):
                ignored += key in counted
                skip_deeper = depth
                continue
            result.setdefault(key, []).extend((v, index + 1) for v in values)
            continue
        match = KEY.match(raw)
        if match is None:
            key = None            # an indented mapping, a quoted key: outside the subset
            continue
        key = match.group(1).casefold()
        value = match.group(2) or ""
        if not value.strip() or value.strip().startswith("#"):
            continue              # `key:` -> a list may follow
        values = _values(value)
        if values is IGNORED or _continues(lines, index, end, 0):
            ignored += key in counted
            skip_deeper = 0
        else:
            result.setdefault(key, []).extend((v, index + 1) for v in values)
        key = None                # a scalar or flow value takes no `- item` lines
    return result, ignored


def parse_frontmatter(lines: list[str], span: int) -> dict[str, list[tuple[str, int]]]:
    """A deliberately small YAML subset: `key: value`, `key: [a, b]`, `key:` + `- item`.

    Returns key (casefolded) -> [(value, 1-based line)]. Anything richer than the
    subset (block scalars, multi-line values, nested collections, anchors, tags)
    is ignored rather than guessed; `#` comments are dropped and quoted scalars
    unescaped.
    """
    return _parse_frontmatter(lines, span)[0]


def strip_inline_code(line: str) -> str:
    """Blank out inline code spans (backtick runs of equal length), keeping offsets."""
    runs = [(m.start(), m.end()) for m in re.finditer(r"`+", line)]
    out = list(line)
    index = 0
    while index < len(runs):
        start, end = runs[index]
        width = end - start
        close = next((k for k in range(index + 1, len(runs))
                      if runs[k][1] - runs[k][0] == width), None)
        if close is None:          # an unmatched run is literal text
            index += 1
            continue
        for position in range(start, runs[close][1]):
            out[position] = " "
        index = close + 1
    return "".join(out)


def code_lines(lines: list[str], start: int = 0) -> set[int]:
    """0-based indexes of lines inside (or delimiting) fenced code blocks."""
    inside: set[int] = set()
    fence = None
    for index in range(start, len(lines)):
        match = FENCE.match(lines[index])
        if fence is None:
            if match:
                fence = match.group(1)
                inside.add(index)
            continue
        inside.add(index)
        if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) \
                and not lines[index].strip()[len(match.group(1)):].strip():
            fence = None
    return inside


def _unquote_line(line: str) -> str | None:
    """A blockquote line without its `>` markers; None for a line outside a quote."""
    match = QUOTE.match(line)
    if match is None:
        return None
    while match is not None:
        line = line[match.end():]
        match = QUOTE.match(line)
    return line


def quoted_code_lines(lines: list[str], start: int = 0,
                      fenced: set[int] = frozenset()) -> set[int]:
    """0-based indexes of fenced code inside blockquotes and callouts (`> ````)."""
    inside: set[int] = set()
    fence = None
    for index in range(start, len(lines)):
        content = None if index in fenced else _unquote_line(lines[index])
        if content is None:
            fence = None          # the quote (and any fence in it) ended
            continue
        match = FENCE.match(content)
        if fence is None:
            if match:
                fence = match.group(1)
                inside.add(index)
            continue
        inside.add(index)
        if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) \
                and not content.strip()[len(match.group(1)):].strip():
            fence = None
    return inside


def indented_code_lines(lines: list[str], start: int = 0,
                        fenced: set[int] = frozenset()) -> set[int]:
    """0-based indexes of CommonMark indented code blocks: lines indented four or
    more columns that start after a blank line or a heading, outside a list (an
    indented line under a list item continues the item and is not code)."""
    inside: set[int] = set()
    in_code = in_list = False
    can_start = True              # the start of the body counts as after a blank line
    for index in range(start, len(lines)):
        line = lines[index]
        if index in fenced:
            in_code = in_list = False
            can_start = FENCE.match(line) is not None
            continue
        if not line.strip():
            can_start = True
            continue
        if _indent(line) >= 4 and (in_code or (can_start and not in_list)):
            inside.add(index)
            in_code = True
            can_start = False
            continue
        in_code = False
        if LIST_ITEM.match(line):
            in_list = True
        elif _indent(line) == 0 and can_start:
            in_list = False       # a new top-level block after a blank line
        can_start = ATX_HEADING.match(line) is not None and not in_list
    return inside


def split_target(inner: str) -> tuple[str, str | None, str | None]:
    """`Target#Heading|alias` -> (target, heading, block)."""
    inner = inner.replace("\\|", "|")
    target = inner.split("|", 1)[0]
    heading = block = None
    if "#" in target:
        target, fragment = target.split("#", 1)
        fragment = fragment.strip()
        if fragment.startswith("^"):
            block = fragment[1:].strip() or None
        else:
            heading = fragment.split("#")[-1].strip() or None
    return target.strip(), heading, block


def frontmatter_value_links(value: str) -> list[tuple[str, str | None, str | None]]:
    """A frontmatter value may hold wikilinks, or be a plain note name/path."""
    found = [split_target(m.group(2)) for m in WIKILINK.finditer(value)]
    if found:
        return found
    value = value.strip()
    if not value or SCHEME.match(value):
        return []
    return [split_target(value)]


def parse_note(text: str) -> ParsedNote:
    """Every link in one note, with 1-based lines; code is skipped."""
    lines = split_lines(text)
    span = frontmatter_span(lines)
    note = ParsedNote(body_start=span + 1)
    if span:
        front, ignored = _parse_frontmatter(lines, span)
        note.frontmatter, note.frontmatter_ignored = "parsed", ignored
        for key in ALIAS_KEYS:
            note.aliases.extend(value for value, _ in front.get(key, []))
        for key in FRONTMATTER_LINK_KEYS:
            for value, line in front.get(key, []):
                for target, heading, block in frontmatter_value_links(value):
                    if target:
                        note.links.append(RawLink("frontmatter", target, line, heading, block,
                                                  key.replace("-", "_")))
    elif lines and _opening(lines[0]).strip() == "---":
        note.frontmatter = "unclosed"
    fenced = code_lines(lines, span)
    skipped = fenced | quoted_code_lines(lines, span, fenced) \
        | indented_code_lines(lines, span, fenced)
    for index in range(span, len(lines)):
        if index in skipped:
            continue
        line = strip_inline_code(lines[index])
        for match in WIKILINK.finditer(line):
            target, heading, block = split_target(match.group(2))
            if target:            # [[#Heading]] points inside the same note
                note.links.append(RawLink("embed" if match.group(1) else "wikilink",
                                          target, index + 1, heading, block))
        for match in MDLINK.finditer(line):
            href = match.group(2)
            if href.startswith("<") and href.endswith(">"):
                href = href[1:-1]
            if SCHEME.match(href) or href.startswith(("#", "//")):
                continue
            href, _, fragment = href.partition("#")
            href = unquote(href)
            if not href.strip() or href.endswith("/"):
                continue
            heading = block = None
            if fragment.startswith("^"):
                block = fragment[1:] or None
            elif fragment:
                heading = unquote(fragment).strip() or None
            note.links.append(RawLink("embed" if match.group(1) else "mdlink", href, index + 1,
                                      heading, block, markdown=True))
    return note


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _fold(text: str) -> str:
    """Case- and Unicode-form-insensitive key: an NFC link finds an NFD file name."""
    return _nfc(_nfc(text).casefold())


def _normalize(candidate: str) -> str | None:
    parts = []
    for part in PurePosixPath(candidate).parts:
        if part in ("", ".", "/"):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts) if parts else None


class _Lookup:
    """File paths by NFC form, by folded form, by folded path tail and by folded name."""

    def __init__(self, paths):
        self.exact: dict[str, list[str]] = {}
        self.folded: dict[str, list[str]] = {}
        self.tails: dict[str, list[str]] = {}
        self.names: dict[str, list[str]] = {}
        for path in sorted(set(paths)):
            nfc = _nfc(path)
            folded = _fold(nfc)
            self.exact.setdefault(nfc, []).append(path)
            self.folded.setdefault(folded, []).append(path)
            parts = folded.split("/")
            for index in range(1, len(parts)):
                self.tails.setdefault("/".join(parts[index:]), []).append(path)
            self.names.setdefault(parts[-1], []).append(path)

    def path(self, candidate: str) -> list[str]:
        """The exact vault path, else every case-insensitive match."""
        normalized = _normalize(candidate)
        if normalized is None:
            return []
        nfc = _nfc(normalized)
        return self.exact.get(nfc) or self.folded.get(_fold(nfc), [])

    def tail(self, candidate: str) -> list[str]:
        """Paths that end with `/candidate` (a unique path suffix in Obsidian's terms)."""
        return self.tails.get(_fold(candidate.lstrip("/")), [])

    def name(self, candidate: str) -> list[str]:
        return self.names.get(_fold(candidate), [])


@dataclass
class Resolution:
    path: str | None               # the on-disk note path, when the link makes an edge
    reason: str = ""               # "" or one of LINK_REASONS
    candidates: list[str] = field(default_factory=list)


class Resolver:
    """Obsidian-style target resolution over the vault's files.

    `notes` are the notes the graph covers (edge targets); `files` is every file
    the vault walk found outside the exclusions (attachments, notes the index
    skipped), or a function that lists them: it is called only when a link does
    not resolve to a note. `excluded(path)` answers the exclusion rules for a
    path, so a link into an excluded folder is counted as such without that
    folder being listed. Keys are NFC-normalised and case-folded; results are
    on-disk paths.
    """

    def __init__(self, notes, files=(), excluded=None):
        notes = list(notes)
        self.notes = _Lookup(notes)
        self._known = set(notes)
        self._files = files
        self._others: _Lookup | None = None
        self.excluded = excluded or (lambda name: False)

    @property
    def others(self) -> _Lookup:
        if self._others is None:
            files = self._files() if callable(self._files) else self._files
            self._others = _Lookup(path for path in files if path not in self._known)
        return self._others

    @staticmethod
    def _rules(lookup: _Lookup, target: str, folder: str, rooted: bool, markdown: bool,
               suffix: str) -> list[str]:
        """The matches of the first rule that matches, in resolution order."""
        name = target if not suffix or target.lower().endswith(suffix) else target + suffix
        relative = bool(folder) and not rooted
        steps = []
        if markdown and relative:
            steps.append(lambda: lookup.path(f"{folder}/{name}"))      # relative to the note
        steps.append(lambda: lookup.path(name))                        # vault path
        if "/" in name:
            if relative and not markdown:
                steps.append(lambda: lookup.path(f"{folder}/{name}"))
            steps.append(lambda: lookup.tail(name))                    # unique path suffix
        else:
            steps.append(lambda: lookup.name(name))                    # unique file name
        for step in steps:
            found = step()
            if found:
                return found
        return []

    def resolve_link(self, source: str, target: str, kind: str,
                     markdown: bool | None = None) -> Resolution:
        """Classify one link: a note (an edge), or the reason it makes none."""
        markdown = kind == "mdlink" if markdown is None else markdown
        target = _nfc(target.strip().replace("\\", "/"))
        rooted = target.startswith("/")
        target = target.lstrip("/")
        if not target:
            return Resolution(None, "missing")
        folder = PurePosixPath(source).parent.as_posix()
        folder = "" if folder == "." else folder
        found = self._rules(self.notes, target, folder, rooted, markdown, NOTE_SUFFIX)
        if len(found) == 1:
            return Resolution(found[0])
        if found:
            return Resolution(None, "ambiguous", found)
        other = self._rules(self.others, target, folder, rooted, markdown, NOTE_SUFFIX) \
            or self._rules(self.others, target, folder, rooted, markdown, "")
        if other:
            notes_only = all(p.lower().endswith(NOTE_SUFFIX) for p in other)
            return Resolution(None, "not_indexed" if notes_only else "attachment")
        stem = target if target.lower().endswith(NOTE_SUFFIX) else target + NOTE_SUFFIX
        places = [target, stem]
        if folder and not rooted:
            places += [f"{folder}/{target}", f"{folder}/{stem}"]
        for place in places:
            normalized = _normalize(place)
            if normalized is not None and self.excluded(normalized):
                return Resolution(None, "excluded")
        return Resolution(None, "missing")

    def resolve(self, source: str, target: str, kind: str) -> tuple[str | None, str]:
        """(target path, "") or (None, reason); reason is one of LINK_REASONS."""
        found = self.resolve_link(source, target, kind)
        return found.path, found.reason


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def graph_path(vault: Path) -> Path:
    return Path(vault) / ".context" / GRAPH_NAME


def _policy():
    from .mcp_server import policy
    return policy()


def indexed_notes(index: Path) -> dict[str, str]:
    """Markdown sources the lexical index covers: path -> indexed SHA-256."""
    connection = sqlite3.connect(index.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT DISTINCT source_path, source_sha256 FROM records ORDER BY source_path").fetchall()
    finally:
        connection.close()
    return {path: sha for path, sha in rows if path.lower().endswith(NOTE_SUFFIX)}


def vault_files(vault: Path, prefixes, policy) -> list[str]:
    """Vault-relative names of every file outside the exclusions. Excluded folders,
    dot folders and symlinked folders are pruned before they are listed, as the
    index builder does; file names are only compared, never opened."""
    vault = Path(vault).resolve()

    def outside(relative: str) -> bool:
        try:
            return policy.excluded(relative, prefixes)
        except ValueError:
            return True

    found = []
    for current, directories, names in os.walk(vault, followlinks=False):
        base = Path(current)
        here = base.relative_to(vault).as_posix()
        prefix = "" if here == "." else here + "/"
        directories[:] = sorted(d for d in directories
                                if not (base / d).is_symlink() and not outside(prefix + d))
        found.extend(prefix + name for name in names if not outside(prefix + name))
    return sorted(found)


def _read_note(vault: Path, name: str, prefixes, policy, indexed_sha: str):
    """(text, raw, stat) for a note the graph can use, or (None, reason, None);
    the reason `excluded` for a note routes.json now excludes."""
    try:
        path = policy.source_path(vault, name, prefixes)
    except ValueError:
        try:
            if policy.excluded(name, prefixes):
                return None, "excluded", None
        except ValueError:
            return None, "not a vault path", None
        current = Path(vault)
        for part in PurePosixPath(name).parts:
            current = current / part
            if current.is_symlink():
                return None, "symlink", None
        return None, "not a vault path", None
    try:
        raw = path.read_bytes()
        stat = path.stat()
    except FileNotFoundError:
        return None, "deleted since indexing", None
    except OSError:
        return None, "unreadable", None
    if hashlib.sha256(raw).hexdigest() != indexed_sha:
        return None, "changed since indexing", None
    try:
        return raw.decode("utf-8"), raw, stat
    except UnicodeError:
        return None, "not UTF-8", None


def _skipped_line(skipped: dict[str, str], excluded_notes: int) -> str | None:
    if not skipped and not excluded_notes:
        return None
    parts = []
    if skipped:
        reasons: dict[str, int] = {}
        for reason in skipped.values():
            reasons[reason] = reasons.get(reason, 0) + 1
        names = sorted(skipped)
        shown = ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
        parts.append(f"skipped {len(skipped)} note(s) ("
                     + ", ".join(f"{k}: {v}" for k, v in sorted(reasons.items()))
                     + f"): {shown}; their links are left out until {REBUILD} indexes "
                       "them as they are")
    if excluded_notes:
        parts.append(f"left out {excluded_notes} indexed note(s) that routes.json now excludes")
    return "context-layer index: link graph " + "; ".join(parts)


def build(vault: Path, index: Path | None = None, out: Path | None = None) -> dict:
    """Extract every note's links and replace graph.sqlite atomically. Returns a summary.

    A note the builder cannot use (changed, deleted or unreadable since the index
    was built, a symlink, not UTF-8) is counted, printed (stderr) and recorded in
    table `skipped`; it stays a link target, and its sha256 is empty so it is never
    fresh: its own links are left out until the next index.
    """
    from .mcp_server import exclude_prefixes
    started = time.perf_counter()
    vault = Path(vault).resolve()
    index = index or (vault / ".context" / "index.sqlite")
    out = out or graph_path(vault)
    policy = _policy()
    prefixes = exclude_prefixes(vault)

    def excluded(name: str) -> bool:
        try:
            return policy.excluded(name, prefixes)
        except ValueError:
            return False

    notes: dict[str, dict] = {}
    parsed: dict[str, ParsedNote] = {}
    skipped: dict[str, str] = {}
    excluded_notes = 0
    for name, indexed_sha in indexed_notes(index).items():
        text, raw, stat = _read_note(vault, name, prefixes, policy, indexed_sha)
        if raw == "excluded":
            excluded_notes += 1          # counted, never named
            continue
        if text is None:
            skipped[name] = raw
            notes[name] = {"sha256": "", "size": -1, "mtime_ns": -1, "aliases": []}
            continue
        parsed[name] = parse_note(text)
        notes[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "size": stat.st_size,
                       "mtime_ns": stat.st_mtime_ns, "aliases": parsed[name].aliases}
    resolver = Resolver(list(notes), lambda: vault_files(vault, prefixes, policy), excluded)
    edges = []
    unresolved = []
    for name, note in parsed.items():
        for link in note.links:
            found = resolver.resolve_link(name, link.target, link.kind, link.markdown)
            if found.path is None:
                unresolved.append((name, link.line, link.kind, found.reason,
                                   None if found.reason == "excluded" else link.target,
                                   json.dumps(found.candidates, ensure_ascii=False)
                                   if found.candidates else None))
            elif found.path != name:
                edges.append((link.kind, name, found.path, link.line, notes[name]["sha256"],
                              link.heading, link.block, link.field))
    frontmatter = [(name, "unclosed" if note.frontmatter == "unclosed"
                    else ("partial" if note.frontmatter_ignored else "parsed"),
                    note.frontmatter_ignored)
                   for name, note in sorted(parsed.items()) if note.frontmatter != "none"]
    neighbours: dict[str, set[str]] = {n: set() for n in notes}
    out_deg = {n: 0 for n in notes}
    in_deg = {n: 0 for n in notes}
    for _, source, target, *_ in edges:
        neighbours[source].add(target)
        neighbours[target].add(source)
    for source, targets in _distinct_pairs(edges).items():
        out_deg[source] = len(targets)
        for target in targets:
            in_deg[target] += 1
    reasons = dict.fromkeys(LINK_REASONS, 0)
    for row in unresolved:
        reasons[row[3]] += 1
    built_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(prefix=".graph-", suffix=".sqlite", dir=out.parent,
                                         delete=False)
    staging = Path(handle.name)
    handle.close()
    try:
        connection = sqlite3.connect(staging)
        try:
            connection.executescript(SCHEMA)
            connection.executemany(
                "INSERT INTO notes (path, sha256, size, mtime_ns, aliases, out_degree, in_degree,"
                " degree) VALUES (?,?,?,?,?,?,?,?)",
                [(n, v["sha256"], v["size"], v["mtime_ns"], json.dumps(v["aliases"]),
                  out_deg[n], in_deg[n], len(neighbours[n])) for n, v in sorted(notes.items())])
            connection.executemany(
                "INSERT INTO edges (kind, source_path, target_path, line, source_sha256, heading,"
                " block, field) VALUES (?,?,?,?,?,?,?,?)", edges)
            connection.executemany(
                "INSERT INTO unresolved (source_path, line, kind, reason, target, candidates)"
                " VALUES (?,?,?,?,?,?)", unresolved)
            connection.executemany("INSERT INTO skipped (path, reason) VALUES (?,?)",
                                   sorted(skipped.items()))
            connection.executemany("INSERT INTO frontmatter (path, status, ignored) VALUES (?,?,?)",
                                   frontmatter)
            meta = {"schema_version": GRAPH_SCHEMA_VERSION, "built_at": built_at,
                    "notes": str(len(notes)), "edges": str(len(edges)),
                    "unresolved": str(sum(reasons[r] for r in UNREACHED)),
                    "unresolved_missing": str(reasons["missing"]),
                    "unresolved_ambiguous": str(reasons["ambiguous"]),
                    "unresolved_not_indexed": str(reasons["not_indexed"]),
                    "attachment_links": str(reasons["attachment"]),
                    "excluded_links": str(reasons["excluded"]),
                    "skipped_notes": str(len(skipped)), "excluded_notes": str(excluded_notes),
                    "frontmatter_blocks": str(len(frontmatter)),
                    "frontmatter_ignored_values": str(sum(row[2] for row in frontmatter))}
            connection.executemany("INSERT INTO graph_meta (key, value) VALUES (?,?)",
                                   sorted(meta.items()))
            connection.commit()
        finally:
            connection.close()
        os.replace(staging, out)
    finally:
        staging.unlink(missing_ok=True)
    line = _skipped_line(skipped, excluded_notes)
    if line:
        print(line, file=sys.stderr)
    by_reason: dict[str, int] = {}
    for reason in skipped.values():
        by_reason[reason] = by_reason.get(reason, 0) + 1
    elapsed = time.perf_counter() - started
    return {"graph": str(out), "notes": len(notes), "edges": len(edges),
            "unresolved": sum(reasons[r] for r in UNREACHED),
            "unresolved_missing": reasons["missing"],
            "unresolved_ambiguous": reasons["ambiguous"],
            "unresolved_not_indexed": reasons["not_indexed"],
            "attachment_links": reasons["attachment"], "excluded_links": reasons["excluded"],
            "skipped": len(skipped), "skipped_by_reason": dict(sorted(by_reason.items())),
            "excluded_notes": excluded_notes, "frontmatter_blocks": len(frontmatter),
            "frontmatter_ignored_values": sum(row[2] for row in frontmatter),
            "built_at": built_at, "seconds": round(elapsed, 4), "bytes": out.stat().st_size}


def _distinct_pairs(edges) -> dict[str, set[str]]:
    pairs: dict[str, set[str]] = {}
    for _, source, target, *_ in edges:
        pairs.setdefault(source, set()).add(target)
    return pairs


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------

class GraphUnreadable(ValueError):
    """graph.sqlite exists but cannot be read (damaged, or a layout this code does not
    read). The message is one line naming the command that rebuilds it, never raw
    SQLite text."""


@dataclass
class Edge:
    kind: str
    source: str
    target: str
    line: int
    source_sha256: str
    heading: str | None
    block: str | None
    field: str | None


class Graph:
    """Read-only view over graph.sqlite with per-query freshness checks.

    Every read goes through `_rows`: a SQLite failure at any point (a missing
    table, a damaged page) raises GraphUnreadable, so callers can degrade.
    """

    def __init__(self, vault: Path, path: Path | None = None):
        self.vault = Path(vault).resolve()
        self.path = path or graph_path(self.vault)
        self.connection = None
        try:
            self.connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
            self.meta = dict(self.connection.execute("SELECT key, value FROM graph_meta"))
        except sqlite3.Error:
            self.close()
            raise GraphUnreadable(UNREADABLE) from None
        from .mcp_server import router_module
        try:
            router_module("index_format").check_graph_meta(self.meta)
        except ValueError as exc:       # a newer layout: the message names the fix
            self.close()
            raise GraphUnreadable(str(exc)) from None
        self._notes: dict[str, tuple] = {}
        self._fresh: dict[str, bool] = {}
        self._tables: set[str] | None = None

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _rows(self, sql: str, args: tuple = ()) -> list[tuple]:
        try:
            return self.connection.execute(sql, args).fetchall()
        except sqlite3.Error:
            raise GraphUnreadable(UNREADABLE) from None

    def has_table(self, name: str) -> bool:
        """Tables added after 0.3 (skipped, frontmatter) are absent from older graphs."""
        if self._tables is None:
            self._tables = {row[0] for row in self._rows(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        return name in self._tables

    def columns(self, table: str) -> set[str]:
        return {row[1] for row in self._rows(f"PRAGMA table_info({table})")}

    def note(self, path: str):
        """(sha256, size, mtime_ns, degree) or None for a note the graph does not know."""
        if path not in self._notes:
            rows = self._rows("SELECT sha256, size, mtime_ns, degree FROM notes WHERE path=?",
                              (path,))
            self._notes[path] = rows[0] if rows else None
        return self._notes[path]

    def degree(self, path: str) -> int:
        row = self.note(path)
        return max(1, row[3]) if row else 1

    def fresh(self, path: str) -> bool:
        """Does the file still hold the bytes its edges were extracted from?

        Size and mtime equal to the build's -> fresh without hashing (the same
        shortcut git uses); otherwise the SHA-256 decides. A note the builder
        skipped has an empty sha256 and is never fresh.
        """
        if path in self._fresh:
            return self._fresh[path]
        row = self.note(path)
        result = False
        if row is not None and row[0]:
            file = self.vault / path
            try:
                if not file.is_symlink():
                    stat = file.stat()
                    if stat.st_size == row[1] and stat.st_mtime_ns == row[2]:
                        result = True
                    else:
                        result = hashlib.sha256(file.read_bytes()).hexdigest() == row[0]
            except OSError:
                result = False
        self._fresh[path] = result
        return result

    def outgoing(self, path: str) -> list[Edge]:
        return [Edge(*row) for row in self._rows(
            "SELECT kind, source_path, target_path, line, source_sha256, heading, block, field"
            " FROM edges WHERE source_path=? ORDER BY line, id", (path,))]

    def incoming(self, path: str) -> list[Edge]:
        return [Edge(*row) for row in self._rows(
            "SELECT kind, source_path, target_path, line, source_sha256, heading, block, field"
            " FROM edges WHERE target_path=? ORDER BY source_path, line, id", (path,))]

    def aliases(self) -> list[tuple[str, str]]:
        """(path, aliases as stored JSON) for every note."""
        return self._rows("SELECT path, aliases FROM notes ORDER BY path")

    def link_reasons(self, path: str) -> list[str]:
        """Why each link of `path` that makes no edge makes none (LINK_REASONS)."""
        return [row[0] for row in self._rows(
            "SELECT reason FROM unresolved WHERE source_path=? ORDER BY line", (path,))]

    def unresolved_count(self, path: str | None = None) -> int:
        """Links that leave a named note out of reach (missing, ambiguous, not indexed)."""
        if path is None:
            return int(self.meta.get("unresolved", "0"))
        return sum(1 for reason in self.link_reasons(path) if reason not in
                   ("attachment", "excluded"))


def open_graph(vault: Path) -> Graph | None:
    """The link graph, or None when there is none; GraphUnreadable when it is damaged."""
    path = graph_path(vault)
    if not path.is_file():
        return None
    return Graph(vault, path)


# ---------------------------------------------------------------------------
# Health (read-only report; `context-layer graph health`)
# ---------------------------------------------------------------------------

HEALTH_LIMIT = 50


def _supersedes_cycles(pairs: list[tuple[str, str]]) -> list[list[str]]:
    """Groups of notes that supersede each other in a cycle (strongly connected
    components with more than one note), each sorted, in path order."""
    graph: dict[str, list[str]] = {}
    for source, target in pairs:
        graph.setdefault(source, []).append(target)
        graph.setdefault(target, [])
    order, low, stack, on_stack, groups = {}, {}, [], set(), []
    for root in sorted(graph):
        if root in order:
            continue
        work = [(root, iter(sorted(graph[root])))]
        order[root] = low[root] = len(order)
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            child = next(children, None)
            if child is not None:
                if child not in order:
                    order[child] = low[child] = len(order)
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(graph[child]))))
                elif child in on_stack:
                    low[node] = min(low[node], order[child])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == order[node]:
                group = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    group.append(member)
                    if member == node:
                        break
                if len(group) > 1:
                    groups.append(sorted(group))
    return sorted(groups)


def health(vault: Path, limit: int = HEALTH_LIMIT) -> dict:
    """A read-only report on the link graph: orphans, broken links by reason,
    ambiguous links with their candidates, links into excluded paths (the target is
    never named), notes changed since the build, notes the builder skipped,
    frontmatter blocks parsed or partly ignored, and `supersedes` cycles.

    Notes the current routes.json excludes are left out entirely. Raises ValueError
    (one line naming the fix) when there is no graph or it cannot be read.
    """
    from .mcp_server import exclude_prefixes
    vault = Path(vault).resolve()
    policy = _policy()
    prefixes = exclude_prefixes(vault)
    graph = open_graph(vault)
    if graph is None:
        raise ValueError("no link graph (.context/graph.sqlite); run `context-layer index "
                         "<vault>` to build it")
    limit = max(1, limit)
    truncated = False

    def hidden(name: str) -> bool:
        try:
            return policy.excluded(name, prefixes)
        except ValueError:
            return True

    def capped(rows: list) -> list:
        nonlocal truncated
        if len(rows) > limit:
            truncated = True
        return rows[:limit]

    try:
        notes = {path: (sha, degree) for path, sha, degree in graph._rows(
            "SELECT path, sha256, degree FROM notes ORDER BY path") if not hidden(path)}
        aliases: dict[str, list[str]] = {}
        for path, raw in graph.aliases():
            if path in notes:
                try:
                    aliases[path] = [a for a in json.loads(raw) if isinstance(a, str)]
                except ValueError:
                    aliases[path] = []
        by_alias: dict[str, set[str]] = {}
        for path, names in aliases.items():
            for name in names:
                by_alias.setdefault(_fold(name.strip()), set()).add(path)
        extra = graph.columns("unresolved") >= {"target", "candidates"}
        select = ("SELECT source_path, line, kind, reason"
                  + (", target, candidates" if extra else ", NULL, NULL")
                  + " FROM unresolved ORDER BY source_path, line")
        broken: dict[str, list[dict]] = {"missing": [], "not_indexed": []}
        ambiguous, boundary = [], []
        attachments = 0
        for source, line, kind, reason, target, candidates in graph._rows(select):
            if source not in notes:
                continue
            entry = {"source": source, "line": line, "kind": kind}
            if reason == "excluded":
                boundary.append(entry)
                continue
            if target is not None:
                place = _normalize(target.replace("\\", "/"))
                if place is not None and (hidden(place) or hidden(place + NOTE_SUFFIX)):
                    boundary.append(entry)    # excluded since the build: never named
                    continue
                entry["target"] = target
            if reason == "attachment":
                attachments += 1
            elif reason == "ambiguous":
                try:
                    listed = json.loads(candidates) if candidates else []
                except ValueError:
                    listed = []
                entry["candidates"] = [c for c in listed if isinstance(c, str) and c in notes]
                ambiguous.append(entry)
            elif reason in broken:
                if reason == "missing" and target:
                    hint = sorted(by_alias.get(_fold(target.strip()), ()))
                    if len(hint) == 1:
                        entry["alias_of"] = hint[0]
                broken[reason].append(entry)
        # Every note is checked: size and mtime first, the hash only when they moved.
        stale = {path: 0 for path in notes if not graph.fresh(path)}
        for source, target, line, kind in graph._rows(
                "SELECT source_path, target_path, line, kind FROM edges"
                " ORDER BY source_path, line, id"):
            if source not in notes:
                continue
            if hidden(target):          # excluded since the build: never named
                boundary.append({"source": source, "line": line, "kind": kind})
            elif source in stale:
                stale[source] += 1
        skipped = []
        if graph.has_table("skipped"):
            skipped = [{"path": p, "reason": r} for p, r in graph._rows(
                "SELECT path, reason FROM skipped ORDER BY path") if p in notes]
        frontmatter = {"blocks": 0, "parsed": 0, "partial": 0, "unclosed": 0,
                       "ignored_values": 0, "notes": []}
        recorded = graph.has_table("frontmatter")
        if recorded:
            for path, status, ignored in graph._rows(
                    "SELECT path, status, ignored FROM frontmatter ORDER BY path"):
                if path not in notes:
                    continue
                frontmatter["blocks"] += 1
                frontmatter[status if status in ("parsed", "partial", "unclosed")
                            else "partial"] += 1
                frontmatter["ignored_values"] += int(ignored)
                if status != "parsed":
                    frontmatter["notes"].append({"path": path, "status": status,
                                                 "ignored_values": int(ignored)})
        pairs = [(s, t) for s, t in graph._rows(
            "SELECT DISTINCT source_path, target_path FROM edges WHERE field='supersedes'")
            if s in notes and t in notes]
        cycles = _supersedes_cycles(pairs)
        orphans = [path for path, (_, degree) in notes.items() if degree == 0]
        boundary.sort(key=lambda e: (e["source"], e["line"]))
        report = {
            "schema": "graph-health-v1",
            "graph": {"built_at": graph.meta.get("built_at"), "notes": len(notes),
                      "edges": int(graph.meta.get("edges", "0"))},
            "orphans": {"count": len(orphans), "paths": capped(orphans)},
            "broken_links": {
                "missing": {"count": len(broken["missing"]), "links": capped(broken["missing"])},
                "not_indexed": {"count": len(broken["not_indexed"]),
                                "links": capped(broken["not_indexed"])}},
            "ambiguous_links": {"count": len(ambiguous), "links": capped(ambiguous)},
            "attachment_links": {"count": attachments},
            "excluded_links": {"count": len(boundary), "links": capped(boundary)},
            "stale": {"count": len(stale), "edges": sum(stale.values()),
                      "paths": capped(sorted(stale))},
            "skipped": {"count": len(skipped), "notes": capped(skipped)},
            "frontmatter": {**{k: v for k, v in frontmatter.items() if k != "notes"},
                            "recorded": recorded, "notes": capped(frontmatter["notes"])},
            "supersedes_cycles": {"count": len(cycles), "groups": capped(cycles)},
        }
    finally:
        graph.close()
    report["truncated"] = truncated
    return report


def _link_text(entry: dict) -> str:
    where = f"{entry['source']}:{entry['line']}"
    target = entry.get("target")
    return f"{where} {entry['kind']}" + (f" {target!r}" if target is not None else "")


def render_health(report: dict) -> str:
    """The human-readable form of health()."""
    out = []
    g = report["graph"]
    out.append(f"link graph: {g['notes']} notes, {g['edges']} edges (built {g['built_at']})")
    orphans = report["orphans"]
    out.append(f"orphans (no link in or out): {orphans['count']}")
    out += [f"  {p}" for p in orphans["paths"]]
    broken = report["broken_links"]
    out.append(f"broken links: {broken['missing']['count']} missing, "
               f"{broken['not_indexed']['count']} to notes the index skipped")
    for entry in broken["missing"]["links"]:
        hint = (f" (an alias of {entry['alias_of']}; Obsidian links aliases as "
                f"[[{PurePosixPath(entry['alias_of']).stem}|{entry['target']}]])"
                if entry.get("alias_of") else "")
        out.append(f"  {_link_text(entry)} missing{hint}")
    out += [f"  {_link_text(e)} not indexed" for e in broken["not_indexed"]["links"]]
    amb = report["ambiguous_links"]
    out.append(f"ambiguous links: {amb['count']}")
    out += [f"  {_link_text(e)} -> {' | '.join(e['candidates'])}" for e in amb["links"]]
    out.append(f"attachment links: {report['attachment_links']['count']}")
    excl = report["excluded_links"]
    out.append(f"links into excluded paths: {excl['count']} (targets are never shown)")
    out += [f"  {_link_text(e)}" for e in excl["links"]]
    stale = report["stale"]
    out.append(f"changed since the graph was built: {stale['count']} note(s), "
               f"{stale['edges']} edge(s) not used" + (f"; {REBUILD}" if stale["count"] else ""))
    out += [f"  {p}" for p in stale["paths"]]
    skipped = report["skipped"]
    out.append(f"skipped by the builder: {skipped['count']}")
    out += [f"  {e['path']} ({e['reason']})" for e in skipped["notes"]]
    fm = report["frontmatter"]
    if fm["recorded"]:
        out.append(f"frontmatter: {fm['blocks']} block(s): {fm['parsed']} parsed, "
                   f"{fm['partial']} with values left out ({fm['ignored_values']}), "
                   f"{fm['unclosed']} unclosed")
        out += [f"  {e['path']} ({e['status']}, {e['ignored_values']} left out)"
                for e in fm["notes"]]
    else:
        out.append(f"frontmatter: not recorded by this graph; {REBUILD}")
    cycles = report["supersedes_cycles"]
    out.append(f"supersedes cycles: {cycles['count']}")
    out += ["  " + " <-> ".join(group) for group in cycles["groups"]]
    if report.get("truncated"):
        out.append("(lists truncated; --limit N shows more)")
    return "\n".join(out)


def cmd_health(args: argparse.Namespace) -> int:
    if getattr(args, "rest", None):
        print(f"context-layer graph health: unrecognised arguments: {' '.join(args.rest)}",
              file=sys.stderr)
        return 2
    vault = Path(args.vault).expanduser()
    if not vault.is_dir():
        print(f"context-layer graph health: vault not found: {args.vault}", file=sys.stderr)
        return 1
    try:
        report = health(vault, args.limit)
    except ValueError as exc:
        print(f"context-layer graph health: {exc}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(render_health(report))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    """Attach `graph health` to the CLI."""
    parser = sub.add_parser(
        "graph", help="Inspect the vault's link graph (read-only).",
        description="Reports on <vault>/.context/graph.sqlite, the explicit-link graph "
                    "`context-layer index` builds. Nothing is written.")
    inner = parser.add_subparsers(dest="graph_command", required=True)
    p_health = inner.add_parser(
        "health", help="Orphans, broken and ambiguous links, links into excluded paths, "
                       "stale notes, frontmatter and supersedes cycles.",
        description="Read-only. Paths are vault-relative; excluded notes are never "
                    "listed, and a link into an excluded path is shown by its source line "
                    "only. Exit code: 0 report printed, 1 no usable graph or routes.json, "
                    "2 usage error.")
    p_health.add_argument("vault")
    p_health.add_argument("--json", action="store_true", dest="as_json",
                          help="Print the machine-readable report (schema graph-health-v1).")
    p_health.add_argument("--limit", type=int, default=HEALTH_LIMIT,
                          help=f"Entries per list (default: {HEALTH_LIMIT}).")
    p_health.set_defaults(func=cmd_health, forward_to=None)
    from . import coactivation
    coactivation.register(inner)
