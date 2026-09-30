"""One folding contract for query terms and the full-text index.

The index tokenizes note text with SQLite FTS5 `unicode61 remove_diacritics 2`
(`TOKENIZER`, used by router/build_index.py). A query term reaches that same
tokenizer through MATCH, so the two sides fold alike only when the term is sent
as the user wrote it. This module therefore splits a prompt into verbatim terms
and never sends a folded form to MATCH. An earlier version sent NFKC-casefolded
terms, and FTS5 folds differently (simple case folding, no compatibility
decomposition): `Straße` became `strasse`, which the index does not hold.

`fold()` is the comparison form, used only where no index is involved: stopword
checks, de-duplication, and comparing prompt words with text already read.

Term boundaries follow the index tokenizer, not Python's idea of a word. A
character ends a term only when FTS5 treats it as a separator; FTS5 keeps some
characters inside a token that Python's `\\w` does not (combining marks,
private-use characters, bidi isolates, symbols newer than its Unicode tables,
such as the `₺` in `100₺`). Characters outside ASCII are classified by asking
SQLite itself, once per character per process, so the rule holds for the
SQLite build in use. A term may still contain characters FTS5 splits on (for
example `e-mail`): FTS5 then reads the quoted term as a phrase, which matches
the same adjacent tokens in a note.

Python 3.10+; standard library only; no network access.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import unicodedata

# The FTS5 tokenizer of `.context/index.sqlite`. build_index.py writes it into
# the schema; the probe below classifies characters with the same one.
TOKENIZER = "unicode61 remove_diacritics 2"

# Two runs of term characters joined by one ASCII hyphen or apostrophe stay one
# term ("e-mail", "don't"); FTS5 reads such a quoted term as a phrase.
JOINERS = "-'"

_ASCII_WORD = re.compile(r"[A-Za-z0-9]")
_PY_WORD = re.compile(r"[^\W_]")


def fold(text: str) -> str:
    """The comparison form: NFKC, full case folding, and U+0307 removed.

    Casefolding a dotted capital I (U+0130, "İzmir") yields "i" plus a combining
    dot above (U+0307); dropping it keeps "İzmir", "İZMİR" and "izmir" equal, as
    they are to FTS5. Never sent to MATCH.
    """
    return unicodedata.normalize("NFKC", text).casefold().replace("\u0307", "")


# ---------------------------------------------------------------------------
# Which characters the index tokenizer keeps inside a token
# ---------------------------------------------------------------------------

_probe_connection: "sqlite3.Connection | None" = None
_probe_lock = threading.Lock()
_token_char: "dict[str, bool]" = {}


def _probe() -> sqlite3.Connection:
    global _probe_connection
    if _probe_connection is None:
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.execute(f"CREATE VIRTUAL TABLE probe USING fts5(x, tokenize='{TOKENIZER}')")
        connection.execute("CREATE VIRTUAL TABLE probe_terms USING fts5vocab(probe, 'instance')")
        _probe_connection = connection
    return _probe_connection


def _classify(chars: "set[str]") -> None:
    """Record, for each character, whether FTS5 keeps it inside a token.

    "a<c>a" is one token when c is a token character (or a diacritic FTS5
    removes from the token) and two tokens when c is a separator.
    """
    unknown = sorted(c for c in chars if c not in _token_char)
    if not unknown:
        return
    probed = [c for c in unknown if not 0xD800 <= ord(c) <= 0xDFFF]
    for c in unknown:
        if c not in probed:
            _token_char[c] = False          # a lone surrogate cannot be stored as UTF-8
    if not probed:
        return
    with _probe_lock:
        connection = _probe()
        connection.execute("DELETE FROM probe")
        connection.executemany("INSERT INTO probe(rowid, x) VALUES (?, ?)",
                               [(number, "a" + c + "a") for number, c in enumerate(probed, 1)])
        counts = dict(connection.execute("SELECT doc, count(*) FROM probe_terms GROUP BY doc"))
    for number, c in enumerate(probed, 1):
        _token_char[c] = counts.get(number, 0) == 1


def is_term_char(c: str) -> bool:
    """True when `c` belongs inside a query term: a letter or digit, or any
    character the index tokenizer keeps inside a token."""
    if c < "\x80":
        return bool(_ASCII_WORD.match(c))   # every other ASCII character separates tokens
    if _PY_WORD.match(c):
        return True
    if c not in _token_char:
        _classify({c})
    return _token_char[c]


# ---------------------------------------------------------------------------
# Terms
# ---------------------------------------------------------------------------

def words(text: str) -> "list[str]":
    """Verbatim terms of `text`, in order, repeats kept.

    A term is a run of term characters (`is_term_char`), and runs joined by one
    ASCII hyphen or apostrophe stay one term. Every term is an exact substring
    of `text` whose neighbours are separators for the index tokenizer.
    """
    _classify({c for c in text if c >= "\x80" and not _PY_WORD.match(c)})
    out: "list[str]" = []
    start = None
    index = 0
    length = len(text)
    while index < length:
        c = text[index]
        if is_term_char(c):
            if start is None:
                start = index
        elif (start is not None and c in JOINERS and index + 1 < length
              and is_term_char(text[index + 1])):
            pass                                  # "e-mail": the joiner stays inside
        elif start is not None:
            out.append(text[start:index])
            start = None
        index += 1
    if start is not None:
        out.append(text[start:])
    return out


def terms(text: str, stopwords: "frozenset[str] | set[str]" = frozenset(), *,
          min_chars: int = 1, skip_decimal: bool = False) -> "list[str]":
    """Verbatim query terms: `words(text)` without stopwords and repeats.

    Stopwords, the length floor and the decimal filter are applied to the folded
    form; repeats are recognised by it, and the first spelling is kept.
    """
    out: "list[str]" = []
    seen: "set[str]" = set()
    for word in words(text):
        key = fold(word)
        if key in seen or key in stopwords or len(key) < min_chars \
                or (skip_decimal and key.isdecimal()):
            continue
        seen.add(key)
        out.append(word)
    return out


def quote(term: str) -> str:
    """A term as an FTS5 string literal: FTS5 tokenizes it as it tokenizes notes."""
    return '"' + term.replace('"', '""') + '"'


def match_expression(query_terms: "list[str]") -> "str | None":
    """The MATCH expression for a list of terms: any term matches. None when empty."""
    return " OR ".join(quote(term) for term in query_terms) or None


def term_matches(texts: "list[str]", terms: "list[str]") -> "list[set[str]]":
    """For each text, the set of `terms` FTS5 (same tokenizer) finds in it."""
    found: "list[set[str]]" = [set() for _ in texts]
    if not texts or not terms:
        return found
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(f"CREATE VIRTUAL TABLE passage USING fts5(x, tokenize='{TOKENIZER}')")
        connection.executemany("INSERT INTO passage(rowid, x) VALUES (?, ?)",
                               list(enumerate(texts, 1)))
        for term in terms:
            for (number,) in connection.execute(
                    "SELECT rowid FROM passage WHERE passage MATCH ?", (quote(term),)):
                found[number - 1].add(term)
    finally:
        connection.close()
    return found


def matching(texts: "list[str]", expression: "str | None") -> "list[bool]":
    """For each text, whether FTS5 (same tokenizer) finds `expression` in it."""
    if not texts or not expression:
        return [False] * len(texts)
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(f"CREATE VIRTUAL TABLE passage USING fts5(x, tokenize='{TOKENIZER}')")
        connection.executemany("INSERT INTO passage(rowid, x) VALUES (?, ?)",
                               list(enumerate(texts, 1)))
        found = {row[0] for row in connection.execute(
            "SELECT rowid FROM passage WHERE passage MATCH ?", (expression,))}
    finally:
        connection.close()
    return [number in found for number in range(1, len(texts) + 1)]


# ---------------------------------------------------------------------------
# Markdown headings
# ---------------------------------------------------------------------------

_HEADING = re.compile(r" {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*\r?$")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)$")


def headings(text: str) -> "list[tuple[int, int, str]]":
    """ATX headings outside fenced code: (character offset of the line, level, title).

    A `#` line inside a ``` or ~~~ fence (a shell comment, say) is code, not a
    heading. Closing `#`s and surrounding blanks are not part of the title.
    Headings with an empty title are skipped.
    """
    found: "list[tuple[int, int, str]]" = []
    fence = ""
    offset = 0
    for line in text.split("\n"):
        opening = _FENCE.match(line)
        if fence:
            if opening and opening.group(1)[0] == fence[0] \
                    and len(opening.group(1)) >= len(fence) and not opening.group(2).strip():
                fence = ""
        elif opening and not (opening.group(1)[0] == "`" and "`" in opening.group(2)):
            fence = opening.group(1)
        else:
            heading = _HEADING.match(line)
            if heading and (heading.group(2) or "").strip():
                found.append((offset, len(heading.group(1)), heading.group(2).strip()))
        offset += len(line) + 1
    return found
