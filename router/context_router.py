#!/usr/bin/env python3
"""Build an evidence-bound, verbatim context packet for a user prompt.

The program routes a prompt to the relevant parts of a Markdown vault, opens the
canonical sources a route declares, retrieves full indexed records from a local
SQLite/FTS5 index, verifies hashes where possible, and records evidence limits.

It never generates an answer to the user's question and it never converts a
retrieval rank or a model confidence into a fact.

Everything vault-specific lives in a JSON config file (see `routes.example.json`).
Python 3.10+; standard library only; no network access.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Iterable

try:
    from . import index_format, textfold
    from .source_policy import excluded, load_config, source_path
    from .textio import configure_stdout
except ImportError:
    import index_format
    import textfold
    from source_policy import excluded, load_config, source_path
    from textio import configure_stdout


# ---------------------------------------------------------------------------
# Lexicon
# ---------------------------------------------------------------------------

# English default stopwords. A vault written in another language adds its own
# through the optional `stopwords` list in routes.json (see router/README.md);
# they extend these defaults, they never replace them.
STOPWORDS = frozenset({
    "a", "about", "an", "and", "are", "as", "at", "be", "can", "do", "for",
    "from", "how", "i", "in", "is", "it", "me", "my", "of", "on", "or", "that",
    "the", "this", "to", "we", "what", "when", "where", "which", "who", "why",
    "with", "would", "you", "your", "please", "write", "find", "make",
})

# The set tokens(), match_tokens() and exact_phrases() consult: the defaults plus
# the loaded config's `stopwords`. parse_prompt() sets it from the config it is given.
_active_stopwords = set(STOPWORDS)


def stopwords_for(config: "dict[str, Any] | None") -> "set[str]":
    """English defaults plus the config's optional `stopwords` list."""
    extra = (config or {}).get("stopwords", [])
    if not isinstance(extra, list) or not all(isinstance(item, str) for item in extra):
        raise ValueError("routes.json `stopwords` must be a list of strings")
    return set(STOPWORDS) | {normalize(item) for item in extra if item.strip()}


def use_config_stopwords(config: "dict[str, Any] | None") -> "set[str]":
    global _active_stopwords
    _active_stopwords = stopwords_for(config)
    return _active_stopwords


TEXT_EXTENSIONS = {
    ".md", ".txt", ".csv", ".tsv", ".json", ".jsonl", ".html", ".htm",
    ".xml", ".svg", ".css", ".scss", ".py", ".js", ".mjs", ".cjs", ".ts",
    ".tsx", ".jsx", ".rs", ".toml", ".yaml", ".yml", ".sql", ".sh", ".zsh",
    ".bash", ".swift", ".c", ".h", ".cpp", ".hpp", ".java",
    ".go", ".r", ".ini", ".cfg", ".conf", ".log", ".ndjson",
}

# Explicit-correction markers. Used only to ask the root agent to compare
# chronology; never to decide by itself that two records conflict.
EXPLICIT_CORRECTION_PATTERNS = (
    r"\bi do not want (?:this|that)\b",
    r"\bi don't want (?:this|that)\b",
    r"\bthat is not what i (?:meant|asked)\b",
    r"\bcorrection\s*:",
    r"\bnot what i want\b",
    r"\byou misunderstood\b",
    r"\bthat is (?:not|incorrect|wrong)\b",
)


# ---------------------------------------------------------------------------
# Tunables that are policy, not configuration
# ---------------------------------------------------------------------------

# Canonical operating rules are the part of the packet a narrowed budget must not
# be allowed to delete. The floor keeps the mandatory quota from reaching zero.
CANONICAL_SOURCE_FLOOR = 3

# A superseded document may enter a packet only as *labelled* evidence, never as
# a bare source, and never in a packet that does not also carry the rule that
# replaced it. These two limits enforce that in code rather than leaving it to
# configuration discipline.
MAX_SUPERSEDED_SOURCES = 2
HISTORICAL_TAIL_SLOTS = 2

ACTIVATION_TIERS = [
    # (name, max prompt chars, max_sources, max_context_chars, max_per_source)
    # A short stimulus should not cost a long packet. "Smaller" means fewer
    # redundant chunks, not fewer distinct sources: source breadth is what a
    # packet's usefulness rests on.
    ("brief", 160, 18, 40000, 6000),
    ("standard", 600, 22, 60000, 9000),
    ("full", None, 28, 86000, 24000),
]


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

class EvidenceError(RuntimeError):
    """The run cannot safely release an evidence packet."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize(text: str) -> str:
    """textfold.fold() with runs of whitespace collapsed: the form every in-Python
    comparison uses, on the prompt and on retrieved text alike. Never sent to MATCH."""
    return re.sub(r"\s+", " ", textfold.fold(text)).strip()


def semantic_hash(text: str) -> str:
    cleaned = re.sub(r"[^\w]+", " ", normalize(text), flags=re.UNICODE)
    return sha256_bytes(re.sub(r"\s+", " ", cleaned).strip().encode("utf-8"))


def contains_phrase(normalized_haystack: str, phrase: str,
                    stem: "dict[str, Any] | None" = None) -> bool:
    needle = normalize(phrase)
    if not needle:
        return False
    pattern = re.escape(needle).replace(r"\ ", r"\s+")
    if re.search(r"(?<!\w)" + pattern + r"(?!\w)", normalized_haystack, re.UNICODE) is not None:
        return True
    if not stem:
        return False
    # Inflection attaches suffixes to the end of the word, so a prompt writes
    # "standardised" where the route trigger is "standard" (and a suffixing
    # language does this far more often). Tolerate a bounded suffix, but only on
    # stems long enough that an everyday short word cannot reach a heavy route.
    if needle in {normalize(item) for item in stem.get("blocklist", [])}:
        return False
    last = needle.split(" ")[-1]
    if len(last) < int(stem.get("min_stem_chars", 5)):
        return False
    suffix = r"\w{1," + str(int(stem.get("max_suffix_chars", 12))) + r"}"
    return re.search(r"(?<!\w)" + pattern + suffix + r"(?!\w)",
                     normalized_haystack, re.UNICODE) is not None


def match_tokens(text: str) -> "list[str]":
    """Verbatim terms for FTS5 MATCH (router/textfold.py): no stopword, nothing
    shorter than two characters or purely decimal once folded, no repeat."""
    return textfold.terms(text, _active_stopwords, min_chars=2, skip_decimal=True)


def tokens(text: str) -> "list[str]":
    """The folded form of match_tokens(text), item for item: for comparing prompt
    words with normalized text, never for MATCH."""
    return [textfold.fold(term) for term in match_tokens(text)]


_WORD = r"[^\W\d_][\w'’-]+"


def _title_runs(prompt: str) -> "list[str]":
    """Runs of 2-5 whitespace-separated words that each start upper-case."""
    runs: "list[list[str]]" = []
    current: "list[str]" = []
    previous_end = None
    for match in re.finditer(r"(?<!\w)" + _WORD, prompt):
        word = match.group(0)
        adjacent = previous_end is not None and not prompt[previous_end:match.start()].strip()
        if word[0].isupper():
            if current and adjacent and len(current) < 5:
                current.append(word)
            else:
                if len(current) >= 2:
                    runs.append(current)
                current = [word]
        else:
            if len(current) >= 2:
                runs.append(current)
            current = []
        previous_end = match.end()
    if len(current) >= 2:
        runs.append(current)
    return [" ".join(run) for run in runs]


def exact_phrases(prompt: str, non_identity: "set[str] | None" = None) -> "list[str]":
    """Pull quoted strings, slash commands, file names and proper-noun runs."""
    out = []
    out.extend(match.strip() for match in re.findall(r"[\"“”]([^\"“”]{2,120})[\"“”]", prompt))
    out.extend(match.strip() for match in re.findall(r"/[A-Za-z]+(?:\s+[A-Za-z]+){0,2}", prompt))
    out.extend(match.strip() for match in re.findall(
        r"\b[^\s/]+\.(?:md|txt|csv|jsonl?|pdf|docx|pptx|xlsx|py|js|rs)\b", prompt, re.I))
    # Consecutive title-cased words are often person, product or document names.
    # Upper case is decided by str.isupper(), so any script with case works.
    out.extend(_title_runs(prompt))
    # A single title-cased word often opens a request and can be a real name.
    # An everyday sentence-opening word is not an identity, so the continuation
    # lexicon is folded in and plain adverbs can never open a heavy route.
    opening = re.match(r"^\s*(" + _WORD + r")", prompt)
    opening_noise = {"only", "today", "tomorrow", "before", "after", "all", "every"}
    opening_noise |= {normalize(item) for item in (non_identity or set())}
    if (opening and len(opening.group(1)) >= 3 and opening.group(1)[0].isupper()
            and normalize(opening.group(1)) not in opening_noise):
        out.append(opening.group(1))
    leading_noise = {
        "but", "here", "well", "now", "okay", "ok", "so", "the", "this",
        "please", "write", "find", "make",
    }
    cleaned = []
    seen = set()
    for item in out:
        item = re.sub(r"\s+", " ", item).strip(" ,.;:!?()[]{}")
        words = item.split()
        while len(words) > 1 and normalize(words[0]) in leading_noise:
            words.pop(0)
        item = " ".join(words)
        key = normalize(item)
        if len(key) >= 2 and key not in seen and key not in _active_stopwords \
                and key not in leading_noise:
            seen.add(key)
            cleaned.append(item)
    return cleaned


def fts_quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


# ---------------------------------------------------------------------------
# Configurable exclusions
# ---------------------------------------------------------------------------

def _normalized_path(source_path: str) -> str:
    return source_path.replace("\\", "/")


def is_excluded_prefix(config: "dict[str, Any]", source_path: str) -> bool:
    """Lab / generated-artifact exclusion, generalized to a configured list.

    A tool's own measurement output must never be quotable back as vault
    evidence, so those directories are named here and dropped after retrieval
    as well as skipped at index time.
    """
    prefixes = config.get("exclude_prefixes", []) + config.get("retrieval_exclude_prefixes", [])
    return excluded(source_path, prefixes)


def is_derived_text_mirror(config: "dict[str, Any]", source_path: str) -> bool:
    """A rendered/derived copy of text that exists authoritatively elsewhere."""
    normalized = _normalized_path(source_path).casefold()
    name = Path(normalized).name
    if name in {item.casefold() for item in config.get("derived_file_names", [])}:
        return True
    for fragment in config.get("derived_path_fragments", []):
        if fragment.casefold() in normalized:
            return True
    for fragment in config.get("derived_name_fragments", []):
        if fragment.casefold() in name:
            return True
    return False


def is_operational_metadata(config: "dict[str, Any]", source_path: str) -> bool:
    """Generated job/request envelopes, excluded when primary records exist."""
    normalized = _normalized_path(source_path).casefold()
    for keep in config.get("operational_metadata_exceptions", []):
        if normalized.endswith(keep.casefold()):
            return False
    for fragment in config.get("operational_metadata_fragments", []):
        if fragment.casefold() in normalized or normalized.startswith(fragment.casefold().lstrip("/")):
            return True
    return False


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    row_id: int
    source_path: str
    source_sha256: str
    source_format: str
    record_type: str
    locator: str
    timestamp: str
    role: str
    content_sha256: str
    content: str
    score: float = 0.0
    query_hits: "list[str]" = field(default_factory=list)
    mandatory: bool = False
    rule_state: str = ""
    superseded_by: str = ""
    # An opportunistic historical attachment: it may only take one of the
    # reserved tail slots of a packet, never a slot belonging to a rule in force
    # or to the highest-ranked direct user record.
    historical: bool = False


@dataclass
class Evidence:
    evidence_id: str
    source_path: str
    absolute_path: str
    locator: str
    timestamp: str
    role: str
    record_type: str
    source_sha256: str
    current_source_sha256: str
    source_hash_state: str
    content_sha256: str
    emitted_content_sha256: str
    content_scope: str
    authority_class: str
    route_reasons: "list[str]"
    query_hits: "list[str]"
    integrity_notes: "list[str]"
    mandatory: bool
    relation: str
    inlined: bool
    overflow_path: str
    overflow_sha256: str
    content: str
    rule_state: str = ""
    superseded_by: str = ""


def rule_state_line(item: Evidence) -> str:
    """Label a rule document inside its own evidence block."""
    tail = f", superseded by `{item.superseded_by}`" if item.superseded_by else ""
    return f"- Rule state: `{item.rule_state}`{tail}"


def load_candidate(row: Iterable[Any]) -> Candidate:
    return Candidate(*row)


def candidate_absolute(vault: Path, candidate: Candidate) -> Path:
    return source_path(vault, candidate.source_path)


# ---------------------------------------------------------------------------
# Prompt parsing and routing
# ---------------------------------------------------------------------------

def parse_prompt(prompt: str, config: "dict[str, Any]") -> "dict[str, Any]":
    use_config_stopwords(config)
    normal = normalize(prompt)
    stem = config.get("stem_suffix_tolerance")
    route_scores: "dict[str, int]" = {}
    route_matches: "dict[str, list[str]]" = {}
    for name, route in config["routes"].items():
        matched = []
        score = 0
        for trigger in route.get("triggers", []):
            if contains_phrase(normal, trigger, stem):
                matched.append(trigger)
                score += 4 if trigger.startswith("/") else max(1, len(tokens(trigger)))
        if score:
            route_scores[name] = score * int(route.get("priority", 1))
            route_matches[name] = matched

    # A broad topic term named without a narrower match still needs that topic's
    # active rules. Configured, not hardcoded.
    for fallback in config.get("fallback_routes", []):
        term = fallback.get("term", "")
        target = fallback.get("route", "")
        unless = set(fallback.get("unless_routes", []))
        if not term or target not in config["routes"]:
            continue
        if contains_phrase(normal, term, stem) and not (unless & set(route_scores)):
            route_scores.setdefault(target, 1)
            route_matches.setdefault(target, [term])

    max_routes = int(config.get("max_routes", 4))
    selected_routes = [
        name for name, _ in
        sorted(route_scores.items(), key=lambda item: (-item[1], item[0]))[:max_routes]
    ]

    phrases = exact_phrases(
        prompt,
        {term for term in config.get("continuation_terms", []) if " " not in term},
    )
    identity_phrases = [phrase for phrase in phrases if not phrase.startswith("/")]
    route_anchors = []
    for route_name in selected_routes:
        for match in route_matches.get(route_name, []):
            if match.startswith("/") or len(tokens(match)) >= 2:
                if normalize(match) not in {normalize(item) for item in route_anchors}:
                    route_anchors.append(match)
    prompt_match_tokens = match_tokens(prompt)
    prompt_tokens = [textfold.fold(term) for term in prompt_match_tokens]
    alias_terms = []
    alias_evidence = []
    for key, values in config.get("aliases", {}).items():
        if contains_phrase(normal, key):
            for value in values:
                if normalize(value) not in {normalize(item) for item in alias_terms}:
                    alias_terms.append(value)
                    alias_evidence.append({"matched": key, "expanded_to": value})

    freshness = [term for term in config.get("freshness_terms", []) if contains_phrase(normal, term)]
    external_terms = [term for term in config.get("external_fact_terms", []) if contains_phrase(normal, term)]
    web_revalidation = bool(freshness and external_terms)

    vague_terms = {normalize(term) for term in config.get("vague_memory_terms", [])}
    ambient = {normalize(term) for term in config.get("ambient_terms", [])}
    meaningful = [
        token for token in prompt_tokens
        if token not in ambient and token not in vague_terms
    ]
    vague_routes = config.get("vague_request_routes", [])
    vague_memory_request = (
        bool(vague_routes)
        and selected_routes == list(vague_routes)
        and not phrases
        and not meaningful
    )
    # A recorded continuation such as "keep going", "shorter" or "ok" carries no
    # retrievable anchor of its own. Sweeping the index for its everyday words
    # produces the largest and least useful packets. Abstain instead, and say so,
    # rather than guessing a topic.
    continuation_tokens: "set[str]" = set()
    for term in config.get("continuation_terms", []):
        continuation_tokens.update(tokens(term))
    distinctive = [
        token for token in prompt_tokens
        if token not in continuation_tokens and token not in vague_terms and len(token) > 2
    ]
    contextless_continuation = bool(
        not selected_routes and not phrases and not distinctive and prompt.strip()
    )
    return {
        "routes": selected_routes,
        "distinctive_tokens": distinctive,
        "contextless_continuation": contextless_continuation,
        "route_scores": route_scores,
        "route_matches": route_matches,
        "exact_phrases": phrases,
        "identity_phrases": identity_phrases,
        "route_anchors": route_anchors,
        "tokens": prompt_tokens,
        "match_tokens": prompt_match_tokens,
        "aliases": alias_terms,
        "alias_evidence": alias_evidence,
        "freshness_terms": freshness,
        "external_fact_terms": external_terms,
        "web_revalidation_required": web_revalidation,
        "needs_clarification": (
            (not meaningful and not selected_routes)
            or vague_memory_request
            or contextless_continuation
        ),
    }


def query_variants(parsed: "dict[str, Any]") -> "list[tuple[str, str, float]]":
    # A deliberately vague request has no trustworthy lexical anchor. Keep only
    # route-level canonical sources and close the answer gate instead of filling
    # the packet with coincidental generic matches.
    if parsed.get("needs_clarification"):
        return []

    variants: "list[tuple[str, str, float]]" = []
    seen = set()

    def add(label: str, expression: str, weight: float) -> None:
        if expression not in seen:
            seen.add(expression)
            variants.append((label, expression, weight))

    route_anchors = parsed.get("route_anchors", [])
    if route_anchors:
        longest = max(len(tokens(anchor)) for anchor in route_anchors)
        if longest >= 3:
            route_anchors = [a for a in route_anchors if len(tokens(a)) == longest]
    search_phrases = parsed["exact_phrases"] + route_anchors
    # MATCH gets verbatim terms, so FTS5 folds them the way it folded the notes.
    for index, phrase in enumerate(search_phrases[:10], 1):
        phrase_tokens = match_tokens(phrase)
        if phrase_tokens:
            add(f"exact_phrase_{index}", "content:" + fts_quote(" ".join(phrase_tokens)), 8.0)
            if len(phrase_tokens) > 1:
                add(f"phrase_terms_{index}",
                    " AND ".join("content:" + fts_quote(t) for t in phrase_tokens[:5]), 5.0)
    for index, alias in enumerate(parsed["aliases"][:8], 1):
        alias_tokens = match_tokens(alias)
        if alias_tokens:
            add(f"alias_{index}",
                " AND ".join("content:" + fts_quote(t) for t in alias_tokens[:5]), 5.0)

    # Broad token queries are a fallback for prompts without a quoted / name /
    # route anchor. Mixing them into an anchored request lets generic words pull
    # unrelated records into the packet.
    strongest = parsed["match_tokens"][:12]
    if strongest and not search_phrases and not parsed["aliases"]:
        add("anchor_and", " AND ".join("content:" + fts_quote(t) for t in strongest[:4]), 4.0)
        if len(strongest) <= 3:
            add("anchor_or", " OR ".join("content:" + fts_quote(t) for t in strongest[:3]), 1.5)
    return variants


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

COLUMNS = (
    "r.id,r.source_path,r.source_sha256,r.source_format,r.record_type,r.locator,"
    "r.timestamp,r.role,r.content_sha256,r.content"
)


def retrieve(
    vault: Path,
    connection: sqlite3.Connection,
    config: "dict[str, Any]",
    parsed: "dict[str, Any]",
    *,
    per_query_limit: int = 80,
) -> "tuple[list[Candidate], list[dict[str, str]], list[str], list[str]]":
    allowlist = tuple(config["record_type_allowlist"])
    placeholders = ",".join("?" for _ in allowlist)
    retrieval_excludes = config.get("exclude_prefixes", []) + config.get("retrieval_exclude_prefixes", [])
    exclude_sql = ""
    exclude_params = []
    for prefix in retrieval_excludes:
        # Bind literal paths; percent/underscore are not wildcard permissions.
        prefix = prefix.rstrip("/")
        escaped = prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        exclude_sql += " AND r.source_path != ? AND r.source_path NOT LIKE ? ESCAPE '!'"
        exclude_params.extend([prefix, escaped + "/%"])

    candidates: "dict[int, Candidate]" = {}
    query_log = []
    errors = []
    canonical_issues = []
    synthetic_id = -1

    for label, expression, weight in query_variants(parsed):
        sql = f"""
            SELECT {COLUMNS}, bm25(records_fts) AS bm
            FROM records_fts
            JOIN records r ON r.id = records_fts.rowid
            WHERE records_fts MATCH ? AND r.record_type IN ({placeholders}){exclude_sql}
            ORDER BY bm
            LIMIT ?
        """
        try:
            rows = connection.execute(sql, (expression, *allowlist, *exclude_params, per_query_limit)).fetchall()
        except sqlite3.OperationalError as exc:
            errors.append(f"{label}: {exc}")
            continue
        accepted = 0
        strong_terms = tokens(" ".join(
            parsed["exact_phrases"] + parsed.get("route_anchors", []) + parsed["aliases"]))
        identity_phrases = parsed.get("identity_phrases", [])
        for rank, row in enumerate(rows, 1):
            candidate = load_candidate(row[:10])
            if (is_derived_text_mirror(config, candidate.source_path)
                    or is_operational_metadata(config, candidate.source_path)
                    or is_excluded_prefix(config, candidate.source_path)):
                continue
            body = normalize(candidate.content)
            # FTS has multiple columns; require a true content match.
            relevant_terms = [
                token for token in parsed["tokens"] + tokens(" ".join(parsed["aliases"]))
                if token in body
            ]
            phrase_match = any(
                normalize(phrase) in body
                for phrase in parsed["exact_phrases"] + parsed.get("route_anchors", [])
            )
            identity_match = any(
                normalize(phrase) in body
                for phrase in identity_phrases + parsed["aliases"]
            )
            if identity_phrases and not identity_match:
                continue
            strong_match = any(token in body for token in strong_terms)
            if strong_terms and not (strong_match or phrase_match):
                continue
            if not relevant_terms and not phrase_match:
                continue
            current = candidates.get(candidate.row_id)
            if current is None:
                current = candidate
                candidates[candidate.row_id] = current
            current.score += weight / (40.0 + rank)
            current.query_hits.append(label)
            accepted += 1
        query_log.append({
            "label": label,
            "expression": expression,
            "raw_hits": str(len(rows)),
            "content_hits": str(accepted),
        })

    # Canonical sources are not discovered through probabilistic ranking. They
    # are read from the vault so the packet carries the version actually on disk,
    # with any superseded predecessor labelled rather than hidden.
    for route_name in parsed["routes"]:
        route = config["routes"][route_name]
        for entry in route.get("canonical_sources", []):
            source = entry["path"] if isinstance(entry, dict) else entry
            try:
                direct = source_path(vault, source, retrieval_excludes)
            except ValueError as exc:
                canonical_issues.append(str(exc))
                continue
            rows = connection.execute(
                f"SELECT {COLUMNS} FROM records r WHERE r.source_path = ? ORDER BY r.id",
                (source,),
            ).fetchall()
            if direct.is_file() and direct.suffix.lower() in TEXT_EXTENSIONS:
                stored_hashes = {row[2] for row in rows}
                if len(stored_hashes) != 1:
                    canonical_issues.append(f"Canonical source has no unique indexed version: {source}")
                    continue
                content = direct.read_bytes().decode("utf-8")
                candidate = Candidate(
                    row_id=synthetic_id, source_path=source,
                    source_sha256=next(iter(stored_hashes)),
                    source_format=direct.suffix.lower(),
                    record_type="verbatim_text_file",
                    locator="complete file",
                    timestamp=datetime.fromtimestamp(direct.stat().st_mtime).astimezone().isoformat(),
                    role="",
                    content_sha256=sha256_bytes(content.encode("utf-8")),
                    content=content,
                )
                synthetic_id -= 1
                if isinstance(entry, dict):
                    candidate.rule_state = entry.get("rule_state", "")
                    candidate.superseded_by = entry.get("superseded_by", "")
                # Opportunistic attachment: a superseded historical document is
                # offered to the ranker, never mandated. Historical breadth may
                # fill spare capacity; it may not displace material that changes
                # today's decision.
                if isinstance(entry, dict) and entry.get("attachment") == "opportunistic":
                    candidate.historical = True
                    candidate.score += 12.0
                else:
                    candidate.mandatory = True
                    candidate.score += 100.0
                candidate.query_hits.append(f"canonical:{route_name}")
                # The whole file read from disk replaces any lexically retrieved
                # chunk of the same file, so a rule document can never appear
                # twice — once labelled and once bare.
                for row_id in [rid for rid, existing in candidates.items()
                               if existing.source_path == source]:
                    candidate.query_hits.extend(candidates[row_id].query_hits)
                    candidate.score += candidates[row_id].score
                    del candidates[row_id]
                candidate.query_hits = sorted(set(candidate.query_hits))
                candidates[candidate.row_id] = candidate
                continue
            if not direct.is_file():
                canonical_issues.append(
                    f"Missing canonical source for {route_name}: {source}")
                continue
            if not rows:
                canonical_issues.append(
                    f"Canonical source is not directly readable text for {route_name}: {source}")
            for row in rows:
                candidate = load_candidate(row)
                current = candidates.get(candidate.row_id)
                if current is None:
                    current = candidate
                    candidates[candidate.row_id] = current
                current.mandatory = True
                current.score += 100.0
                current.query_hits.append(f"canonical:{route_name}")

    normal_prompt = normalize(" ".join(parsed["tokens"] + parsed["aliases"]))
    prompt_terms = tokens(normal_prompt)
    boost_prefixes = config.get("boost_path_prefixes", {})
    demote_fragments = config.get("demote_path_fragments", [])
    for candidate in candidates.values():
        path = candidate.source_path
        body = normalize(candidate.content)
        candidate.score += sum(1 for token in prompt_terms if token in body) * 0.8
        if candidate.record_type == "conversation_message":
            candidate.score += 2.0
        if candidate.role == "user":
            candidate.score += 4.0
        elif candidate.role == "assistant":
            candidate.score += 0.5
        for prefix, bonus in boost_prefixes.items():
            if path.startswith(prefix):
                candidate.score += float(bonus)
        for fragment in demote_fragments:
            if fragment.casefold() in path.casefold():
                candidate.score -= 3.0
        for route_name in parsed["routes"]:
            for hint in config["routes"][route_name].get("path_hints", []):
                if path.startswith(hint) or hint in path:
                    candidate.score += 2.5

    ranked = sorted(
        candidates.values(),
        key=lambda candidate: (
            not candidate.mandatory,
            -candidate.score,
            candidate.timestamp or "",
            candidate.source_path,
            candidate.locator,
        ),
    )
    return ranked, query_log, errors, canonical_issues


def authority_class(candidate: Candidate) -> str:
    path = candidate.source_path
    if candidate.mandatory:
        return "current_canonical_project_source"
    if candidate.record_type == "conversation_message" and candidate.role == "user":
        return "direct_user_statement"
    if candidate.record_type == "conversation_message" and candidate.role == "assistant":
        return "assistant_output_or_proposal"
    if "/evidence/" in path or "/data/" in path:
        return "stored_evidence_or_data"
    if candidate.record_type == "complete_document_text_extraction":
        return "textual_document_extraction"
    return "verbatim_project_or_archive_text"


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------

def best_markdown_section(text: str, anchors: "list[str]") -> "tuple[str, str]":
    """Return a whole Markdown section around the densest anchor match."""
    if not anchors:
        return text, "complete_file"
    # Shared with the scanner: a `#` line inside a code fence is code, not a heading.
    headings = textfold.headings(text)
    if not headings:
        return text, "complete_file"
    normalized_anchors = [normalize(a) for a in anchors if normalize(a)]
    best = None
    for index, (start, current_level, _title) in enumerate(headings):
        end = len(text)
        for later_start, later_level, _later_title in headings[index + 1:]:
            if later_level <= current_level:
                end = later_start
                break
        section = text[start:end].rstrip()
        normalized_section = normalize(section)
        score = sum(
            max(1, len(tokens(anchor))) ** 2
            for anchor in normalized_anchors
            if anchor in normalized_section
        )
        if score and (best is None or (score, -start) > (best[0], best[1])):
            best = (score, -start, section)
    if best is None:
        return text, "complete_file"
    return best[2], "complete_markdown_section"


def anchored_window(text: str, anchors: "list[str]", limit: int) -> "tuple[str, bool]":
    """Return the verbatim `limit`-sized window of `text` densest in anchors.

    Inlining the file head is where a rule file keeps its front matter, not where
    a narrow prompt's rule lives. A window is still one contiguous verbatim
    passage, so nothing is paraphrased; only the offset changes.
    """
    if limit <= 0 or len(text) <= limit:
        return text, False
    normalized_anchors = [normalize(a) for a in anchors if len(normalize(a)) > 2]
    if not normalized_anchors:
        return text[:limit], False
    stride = max(1, limit // 4)
    best_start = 0
    best_score = -1
    for start in range(0, max(1, len(text) - limit) + stride, stride):
        window = normalize(text[start:start + limit])
        score = sum(
            max(1, len(tokens(anchor))) ** 2
            for anchor in normalized_anchors
            if anchor in window
        )
        if score > best_score:
            best_score = score
            best_start = min(start, max(0, len(text) - limit))
    if best_score <= 0:
        return text[:limit], False
    return text[best_start:best_start + limit], best_start > 0


def best_jsonl_line(path: Path, anchors: "list[str]", text: str | None = None) -> str:
    """Return one complete physical JSONL record selected by strong anchors."""
    normalized_anchors = [normalize(a) for a in anchors if normalize(a)]
    best_line = ""
    best_score = 0
    if text is None:
        text = path.read_bytes().decode("utf-8")
    for line in text.splitlines(keepends=True):
        raw = line.rstrip("\n")
        if not raw:
            continue
        body = normalize(raw)
        matched = [a for a in normalized_anchors if a in body]
        score = sum(max(1, len(tokens(a))) for a in matched)
        if score > best_score:
            best_line = raw
            best_score = score
    return best_line


def materialize_candidate(
    vault: Path,
    candidate: Candidate,
    anchors: "list[str]",
    *,
    max_per_source: int,
    overflow_dir: Path,
    canonical_sections: bool = False,
) -> "tuple[str, str, list[str], str, str]":
    """Return materialized content and integrity data."""
    absolute = candidate_absolute(vault, candidate)
    notes: "list[str]" = []
    # Hash and materialize one snapshot. A cached digest plus a later read can
    # accidentally certify different bytes. The expected hash comes from SQLite.
    raw = absolute.read_bytes()
    current_sha = sha256_bytes(raw)
    if not candidate.source_sha256 or current_sha != candidate.source_sha256:
        raise EvidenceError(f"index_source_hash_mismatch: {candidate.source_path}; run `context-layer index <vault>`")
    if sha256_bytes(candidate.content.encode("utf-8")) != candidate.content_sha256:
        raise EvidenceError(f"indexed_content_hash_mismatch: {candidate.source_path}")
    source_state = "verified"
    text = raw.decode("utf-8") if absolute.suffix.lower() in TEXT_EXTENSIONS else ""
    if candidate.record_type == "verbatim_text_file" and candidate.content not in text:
        raise EvidenceError(f"indexed_content_not_in_source: {candidate.source_path}; run `context-layer index <vault>`")

    if candidate.record_type == "conversation_message":
        # Already the full visible message body.
        content = candidate.content
        scope = "complete_indexed_conversation_message"
    elif absolute.is_file() and absolute.suffix.lower() == ".jsonl":
        line = best_jsonl_line(absolute, anchors, text)
        if line:
            content = line
            scope = "complete_jsonl_line"
        else:
            content = candidate.content
            scope = "exact_index_chunk_from_jsonl"
            notes.append("No anchor-matching logical JSONL line was found; the exact indexed chunk is emitted.")
    elif absolute.is_file() and absolute.suffix.lower() == ".json" and not candidate.mandatory:
        # A large JSON file often combines the matched record with unrelated
        # sibling metadata. The FTS chunk is the exact source text that passed
        # the entity gate; expanding to the whole file would reintroduce the rest.
        content = candidate.content
        scope = "exact_index_chunk_from_json"
        notes.append("A prompt-specific exact JSON chunk is emitted to avoid unrelated sibling metadata.")
    elif (
        absolute.is_file()
        and absolute.suffix.lower() == ".md"
        and candidate.mandatory
        and canonical_sections
        and len(raw) > max_per_source
    ):
        # A canonical rule file answers a short stimulus through the section the
        # prompt actually names. The whole file stays one open away: the path,
        # the hash and the duty to read the original are still in the packet, so
        # this narrows cost, not authority.
        section, section_scope = best_markdown_section(text, anchors)
        if section_scope != "complete_file" and len(section) <= max_per_source:
            content = section
            scope = "canonical_" + section_scope
            notes.append("Canonical source inlined as the prompt-matching section only; "
                         "open the complete file before relying on the rule.")
        else:
            base = section if section_scope != "complete_file" else text
            window, shifted = anchored_window(base, anchors, max_per_source)
            content = window
            scope = "canonical_anchored_excerpt"
            notes.append(
                "Canonical source exceeded the activation budget; the prompt-matching passage is inlined verbatim"
                + (" from inside the file, not its head" if shifted else "")
                + ". Open the complete file before relying on the rule."
            )
    elif (absolute.is_file() and absolute.suffix.lower() == ".md"
          and not candidate.mandatory and len(raw) > 6000):
        section, section_scope = best_markdown_section(text, anchors)
        if len(section) <= max_per_source:
            content = section
            scope = section_scope
        else:
            content = candidate.content
            scope = "exact_index_chunk; complete_section_exceeds_limit"
            notes.append("The complete selected Markdown section exceeds the per-source inline limit; "
                         "the exact indexed chunk is emitted.")
    elif absolute.is_file() and absolute.suffix.lower() in TEXT_EXTENSIONS:
        if len(text) <= max_per_source:
            content = text
            scope = "complete_text_file"
        elif absolute.suffix.lower() == ".md":
            section, section_scope = best_markdown_section(text, anchors)
            if len(section) <= max_per_source:
                content = section
                scope = section_scope
            else:
                overflow = overflow_dir / f"{sha256_bytes(candidate.source_path.encode())[:12]}__section.txt"
                overflow.write_text(section, encoding="utf-8", newline="")
                content = candidate.content
                scope = "exact_index_chunk; complete_section_in_overflow"
                notes.append(f"Complete selected section exceeds the inline limit and is stored at {overflow}.")
        else:
            content = candidate.content
            scope = "exact_index_chunk"
            notes.append("The complete source is larger than the per-source inline limit; "
                         "the exact indexed chunk is emitted.")
    else:
        content = candidate.content
        scope = "exact_index_record"
        if candidate.record_type == "complete_document_text_extraction":
            notes.append("This is exact extracted text, not proof that visual layout, embedded objects, "
                         "OCR, or reading order are complete.")

    if candidate.record_type == "verbatim_text_file" and content.encode("utf-8") not in raw:
        raise EvidenceError(f"emitted_content_not_in_source: {candidate.source_path}")
    return content, scope, notes, current_sha, source_state


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def logical_source_key(config: "dict[str, Any]", candidate: Candidate) -> str:
    """Identify one logical file across a vault and its mirror copies.

    `standards/house-style.md` and `snapshots/2026-01/standards/house-style.md`
    are the same document at two revisions. Paying for both spends a source slot
    and a share of the character budget on text the packet already carries, and
    leaves the reader to work out which revision is in force.
    """
    path = candidate.source_path
    for prefix in config.get("mirror_prefixes", []):
        if path.startswith(prefix):
            return path[len(prefix):].casefold()
    return path.casefold()


def choose_records(ranked: "list[Candidate]", parsed: "dict[str, Any]", limit: int,
                   config: "dict[str, Any] | None" = None) -> "list[Candidate]":
    config = config or {}
    selected: "list[Candidate]" = []
    seen_semantic = set()
    canonical_sources = set()
    per_source_noncanonical: "dict[str, int]" = defaultdict(int)

    historical_ranked = [c for c in ranked if c.historical]
    ranked = [c for c in ranked if not c.historical]
    # Historical breadth is paid for out of the weakest lexical slots at the end
    # of the packet, never out of a canonical rule or the top-ranked direct user
    # record.
    reserved = min(HISTORICAL_TAIL_SLOTS, len(historical_ranked))
    limit = max(1, limit - reserved)

    mandatory_ranked = []
    mandatory_seen_paths = set()
    for candidate in ranked:
        if candidate.mandatory and candidate.source_path not in mandatory_seen_paths:
            mandatory_ranked.append(candidate)
            mandatory_seen_paths.add(candidate.source_path)
    # A superseded document never competes with a rule in force for a slot.
    mandatory_ranked.sort(key=lambda c: c.rule_state == "superseded")
    lexical_ranked = [c for c in ranked if not c.mandatory]

    # Keep room for prompt-specific history: multiple routes can otherwise fill
    # the packet with operating manuals alone. The quota must never reach zero —
    # a smaller budget may shorten the operating rules; it may not delete them.
    lexical_reserve = int(config.get("lexical_reserve", 6))
    canonical_floor = min(
        int(config.get("canonical_source_floor", CANONICAL_SOURCE_FLOOR)),
        len(mandatory_ranked),
        max(1, limit - 1),
    )
    mandatory_quota = min(
        len(mandatory_ranked),
        max(canonical_floor, limit - min(lexical_reserve, len(lexical_ranked))),
    )
    ordered = mandatory_ranked[:mandatory_quota] + lexical_ranked

    mirror_superseded: "dict[str, str]" = {}
    logical_seen: "dict[str, Candidate]" = {}
    for candidate in ordered:
        digest = semantic_hash(candidate.content)
        if digest in seen_semantic:
            continue
        key = logical_source_key(config, candidate)
        if key in logical_seen:
            # The selected revision is already present; note the mirror copy
            # rather than spending a second slot on the same document.
            if candidate.source_path != logical_seen[key].source_path:
                mirror_superseded[candidate.source_path] = logical_seen[key].source_path
            continue
        if candidate.mandatory:
            if candidate.source_path in canonical_sources:
                continue
            canonical_sources.add(candidate.source_path)
        elif per_source_noncanonical[candidate.source_path] >= 4:
            continue
        selected.append(candidate)
        seen_semantic.add(digest)
        logical_seen[key] = candidate
        if not candidate.mandatory:
            per_source_noncanonical[candidate.source_path] += 1
        if len(selected) >= limit:
            break
    for candidate in historical_ranked[:reserved]:
        digest = semantic_hash(candidate.content)
        if digest in seen_semantic:
            continue
        selected.append(candidate)
        seen_semantic.add(digest)
    choose_records.last_mirror_superseded = mirror_superseded
    return selected


def superseded_guard(selected: "list[Candidate]") -> "tuple[list[Candidate], list[dict[str, str]]]":
    """Drop any superseded source whose replacement is not in the same packet.

    Returns the kept candidates and a dropped-record list for the packet's own
    omission section, so the removal is reported rather than silent.
    """
    present = {Path(c.source_path).name for c in selected}
    kept: "list[Candidate]" = []
    dropped: "list[dict[str, str]]" = []
    superseded_used = 0
    for candidate in selected:
        if candidate.rule_state != "superseded":
            kept.append(candidate)
            continue
        replacement = Path(candidate.superseded_by).name if candidate.superseded_by else ""
        if not replacement or replacement not in present:
            dropped.append({
                "source_path": candidate.source_path,
                "reason": "superseded source dropped: the rule that replaced it "
                          f"(`{candidate.superseded_by or 'unrecorded'}`) is not in this packet",
            })
            continue
        if superseded_used >= MAX_SUPERSEDED_SOURCES:
            dropped.append({
                "source_path": candidate.source_path,
                "reason": f"superseded source dropped: more than {MAX_SUPERSEDED_SOURCES} "
                          "superseded documents would crowd out the rules in force",
            })
            continue
        superseded_used += 1
        kept.append(candidate)
    return kept, dropped


def known_limits(config: "dict[str, Any]", candidate: Candidate) -> "list[str]":
    notes = []
    for limit in config.get("known_integrity_limits", []):
        if (
            normalize(limit.get("source_path_contains", "")) in normalize(candidate.source_path)
            and (not limit.get("locator") or limit["locator"] == candidate.locator)
        ):
            notes.append(limit["reason"])
    return notes


# ---------------------------------------------------------------------------
# Evidence gate
# ---------------------------------------------------------------------------

def determine_status(
    selected: "list[Candidate]",
    evidence: "list[Evidence]",
    parsed: "dict[str, Any]",
    integrity_limits: "list[str]",
) -> "tuple[str, list[str], bool]":
    # Canonical-source problems never reach this gate: run() refuses the whole
    # packet first (EvidenceError), so no status is computed for them.
    reasons = []
    lexical = [c for c in selected if not c.mandatory and not c.historical]
    direct_user = [c for c in lexical if c.role == "user"]
    verified = [e for e in evidence if e.inlined and e.content and e.source_hash_state == "verified"]
    prompt_terms = parsed["tokens"] + tokens(" ".join(parsed["aliases"]))
    minimum_overlap = 2 if len(prompt_terms) >= 4 else 1
    relevant_direct_user = [
        c for c in direct_user
        if sum(1 for token in prompt_terms if token in normalize(c.content)) >= minimum_overlap
    ]
    # Intentionally conservative: everyday negations often express the current
    # requirement and do not prove that two records conflict. A root-agent
    # comparison is asked for only when a later record uses explicit correction
    # language.
    correction_review = False
    ordered_user = sorted(relevant_direct_user, key=lambda item: item.timestamp or "")
    if len(ordered_user) > 1:
        for candidate in ordered_user[1:]:
            body = normalize(candidate.content)
            if any(re.search(p, body, re.UNICODE) for p in EXPLICIT_CORRECTION_PATTERNS):
                correction_review = True
                break

    if parsed["web_revalidation_required"]:
        reasons.append("The prompt asks for a time-sensitive external fact; stored notes are dated context only.")
        return "EXTERNAL_RECHECK", reasons, correction_review
    if parsed["needs_clarification"]:
        reasons.append("The prompt has no sufficiently specific entity, phrase, file, date, or route anchor.")
        return "NOT_FOUND", reasons, correction_review
    if not lexical:
        if any(c.mandatory for c in selected):
            reasons.append("Canonical operating sources were found, but no prompt-specific record was retrieved.")
            return "PARTIAL", reasons, correction_review
        reasons.append("No prompt-specific record was retrieved from the vault.")
        return "NOT_FOUND", reasons, correction_review
    if integrity_limits:
        reasons.extend(integrity_limits)
        return "PARTIAL", reasons, correction_review
    if any(e.source_hash_state != "verified" for e in evidence):
        reasons.append("At least one selected index record does not match the current source-file hash.")
        return "PARTIAL", reasons, correction_review
    if any(e.role == "user" and e.source_path in {c.source_path for c in direct_user} for e in verified):
        reasons.append("A direct user statement and its exact indexed source are present.")
        return "USER_STATED", reasons, correction_review
    conversation = [c for c in lexical if c.record_type == "conversation_message"]
    # Deviation from the original, which assumed conversation records always
    # exist: an empty list must not vacuously satisfy "all assistant" and force
    # every file-only vault to PARTIAL.
    if conversation and all(c.role == "assistant" for c in conversation):
        reasons.append("The prompt-specific conversation evidence consists only of assistant output or proposals.")
        return "PARTIAL", reasons, correction_review
    if verified:
        reasons.append("Relevant exact records were retrieved and their current source hashes were verified.")
        return "SUPPORTED", reasons, correction_review
    reasons.append("Relevant records were retrieved, but source integrity could not be fully verified.")
    return "PARTIAL", reasons, correction_review


# ---------------------------------------------------------------------------
# Tiered activation
# ---------------------------------------------------------------------------

def activation_budget(parsed: "dict[str, Any]", prompt: str = "") -> "dict[str, Any]":
    """Pick a retrieval budget from the stimulus, not from a fixed maximum.

    The budget narrows what is retrieved and how much of each source is inlined;
    it never changes the evidence gate, the provenance requirement, or the duty
    to open the original.
    """
    if parsed.get("needs_clarification"):
        return {
            "tier": "abstain",
            "max_sources": 0,
            "max_context_chars": 0,
            "max_per_source": 0,
            "canonical_sections": True,
            "reason": "No specific route or anchor; the packet abstains instead of sweeping the index.",
        }
    size = len(prompt or "")
    for name, limit, sources, chars, per_source in ACTIVATION_TIERS:
        if limit is None or size <= limit:
            return {
                "tier": name,
                "max_sources": sources,
                "max_context_chars": chars,
                "max_per_source": per_source,
                "canonical_sections": name != "full",
                "reason": f"prompt is {size} characters; tier {name}",
            }
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# Fast-path answer cards
# ---------------------------------------------------------------------------

def load_fact_cards(vault: Path, facts_path: Path, config: dict | None = None) -> "list[dict[str, Any]]":
    """Load short answer cards and verify each quote against its source file.

    A card whose quote is not present verbatim is dropped, so the fast path can
    never answer from a stale or invented statement.
    """
    if not facts_path.is_file():
        return []
    try:
        raw = json.loads(facts_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    verified: "list[dict[str, Any]]" = []
    for card in raw.get("cards", []):
        rel = card.get("path")
        if not rel:
            continue
        prefixes = (config or {}).get("exclude_prefixes", []) + (config or {}).get("retrieval_exclude_prefixes", [])
        candidate = source_path(vault, rel, prefixes)
        if not candidate.is_file():
            continue
        raw_bytes = candidate.read_bytes()
        body = raw_bytes.decode("utf-8")
        if not card.get("quote") or card["quote"] not in body:
            continue
        item = dict(card)
        item["resolved_path"] = str(candidate)
        item["resolved_relative"] = rel
        item["source_sha256"] = sha256_bytes(raw_bytes)
        verified.append(item)
    return verified


def fast_activation(prompt: str, parsed: "dict[str, Any]", config: "dict[str, Any]",
                    vault: Path, facts_path: Path) -> "dict[str, Any] | None":
    """Answer a short, rule-shaped stimulus from verified cards, or return None.

    The fast path fires only when the prompt names a card's own terms. It never
    fires for a contextless prompt, and it never replaces the requirement to open
    the cited original.
    """
    if parsed.get("needs_clarification") or not parsed.get("routes"):
        return None
    cards = load_fact_cards(vault, facts_path, config)
    if not cards:
        return None
    normal = normalize(prompt)
    stem = config.get("stem_suffix_tolerance")
    hits = []
    for card in cards:
        matched = [term for term in card.get("terms", []) if contains_phrase(normal, term, stem)]
        # Require two independent term matches so an everyday word cannot pull a rule card.
        if len(matched) >= int(card.get("min_matches", 2)):
            hits.append((len(matched), card, matched))
    if not hits:
        return None
    hits.sort(key=lambda item: (-item[0], item[1]["id"]))
    chosen = [
        {
            "id": card["id"],
            "answer": card["answer"],
            "evidence_grade": card["evidence_grade"],
            "rule_state": card.get("rule_state", "current"),
            "supersedes": card.get("supersedes"),
            "superseded_note": card.get("superseded_note"),
            "source_path": card["resolved_relative"],
            "absolute_path": card["resolved_path"],
            "source_sha256": card["source_sha256"],
            "quote": card["quote"],
            "matched_terms": matched,
        }
        for _score, card, matched in hits[:3]
    ]
    grades = {card["evidence_grade"] for card in chosen}
    status = "USER_STATED" if "USER_STATED" in grades else (
        "SUPPORTED" if "SUPPORTED" in grades else sorted(grades)[0])
    return {
        "cards": chosen,
        "status": status,
        "characters": sum(len(card["quote"]) for card in chosen),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

SUBAGENT_BRIEF = (
    "# Retrieval subagent brief\n\n"
    "1. Read `context.md` and `evidence.jsonl`.\n"
    "2. Open the original `absolute_path` for every passage you propose to use.\n"
    "3. Compare timestamps where user corrections or conflicts may exist.\n"
    "4. Return only: route result, evidence IDs, exact source paths, gaps, conflicts, "
    "and the evidence status.\n"
    "5. Do not answer the user's underlying request, do not summarize source content as a "
    "replacement for it, do not infer approval, and do not fill gaps.\n"
    "6. If status is EXTERNAL_RECHECK, identify what must be checked against current "
    "authoritative sources.\n"
)


def brief_packet(prompt: str, parsed: "dict[str, Any]", fast: "dict[str, Any]") -> str:
    """Render the smallest packet that still carries provenance and the gate."""
    lines = [
        "# Fast activation packet",
        "",
        f"- Evidence status: **{fast['status']}**",
        f"- Routes: {', '.join(parsed['routes']) or 'none'}",
        "- Path: verified answer card; the full retrieval tier was not needed.",
        "",
        "## Exact user prompt",
        "",
        "```text",
        prompt,
        "```",
        "",
        "## Answer cards",
        "",
    ]
    for card in fast["cards"]:
        lines.extend([
            f"### {card['id']} — {card['answer']}",
            "",
            f"- Evidence grade: `{card['evidence_grade']}`",
            f"- Rule state: `{card['rule_state']}`",
            f"- Source: `{card['source_path']}`",
            f"- Source SHA-256: `{card['source_sha256']}`",
        ])
        if card.get("supersedes"):
            lines.append(f"- Replaces: `{card['supersedes']}` — "
                         f"{card.get('superseded_note', 'earlier version; do not apply it')}")
        lines.extend(["", "```text", card["quote"], "```", ""])
    lines.extend([
        "## Required root-agent check",
        "",
        "Open the cited original before using a card in an answer. A card is a pointer with a "
        "verified quote, not permission to skip the source, and not user approval.",
        "",
    ])
    return "\n".join(lines)


def markdown_packet(
    prompt: str,
    run_id: str,
    parsed: "dict[str, Any]",
    status: str,
    status_reasons: "list[str]",
    conflict_review: bool,
    evidence: "list[Evidence]",
    omitted: "list[dict[str, str]]",
    metadata: "dict[str, Any]",
    fast: "dict[str, Any] | None" = None,
    resolved_rules: "list[dict[str, str]] | None" = None,
) -> str:
    lines = [
        "# Evidence-bound context packet",
        "",
        f"- Run: `{run_id}`",
        f"- Evidence status: **{status}**",
        f"- Routes: {', '.join(parsed['routes']) or 'none'}",
        f"- External revalidation required: `{str(parsed['web_revalidation_required']).lower()}`",
        f"- Conflict review required: `{str(conflict_review).lower()}`",
        f"- Vault: `{metadata['vault']}`",
        f"- Index SHA-256: `{metadata['index_sha256']}`",
        f"- Index built at: `{metadata['index_built_at']}`",
        "",
        "## Exact user prompt",
        "",
        "```text",
        prompt,
        "```",
        "",
        "## Evidence gate",
        "",
    ]
    lines.extend(f"- {reason}" for reason in status_reasons)
    if conflict_review:
        lines.append("- Multiple direct user records include correction language. "
                     "Compare chronology before applying a preference.")
    if resolved_rules:
        lines.extend([
            "",
            "## Resolved rule state",
            "",
            "Both versions are in this packet on purpose. Apply the one marked `in force`; the other",
            "is kept so a superseded instruction can be recognised rather than silently re-applied.",
            "",
        ])
        for item in resolved_rules:
            lines.append(
                f"- `{item['path']}` — **{item['state']}**"
                + (f", superseded by `{item['superseded_by']}`" if item.get("superseded_by") else "")
            )
    if fast:
        lines.extend([
            "",
            "## Verified answer cards",
            "",
            "Each card names one source file and one quote checked byte-for-byte against it at run",
            "time; a card whose quote no longer matched was dropped. A card is a pointer with",
            "provenance, never user approval. Open the cited original before using it.",
            "",
        ])
        for card in fast["cards"]:
            lines.append(f"- **{card['answer']}**")
            lines.append(f"  - Source: `{card['source_path']}` · SHA-256 "
                         f"`{card['source_sha256'][:16]}…` · grade `{card['evidence_grade']}`")
            if card.get("supersedes"):
                lines.append(f"  - Replaces `{card['supersedes']}` — "
                             f"{card.get('superseded_note', 'earlier version; do not apply it')}")
            lines.append(f"  - Quote: `{card['quote']}`")
    lines.extend([
        "",
        "- A retrieved passage is evidence of what the source says. It is not automatic proof of "
        "an external fact.",
        "- Historical tool output and assistant prose are reference material, not current "
        "authorization.",
        "- Do not answer beyond the evidence status. State missing or conflicting evidence plainly.",
        "",
        "## Routing metadata",
        "",
        "```json",
        json.dumps({
            "route_matches": parsed["route_matches"],
            "exact_phrases": parsed["exact_phrases"],
            "identity_phrases": parsed.get("identity_phrases", []),
            "route_anchors": parsed.get("route_anchors", []),
            "tokens": parsed["tokens"],
            "alias_evidence": parsed["alias_evidence"],
            "freshness_terms": parsed["freshness_terms"],
            "external_fact_terms": parsed["external_fact_terms"],
        }, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Verbatim evidence",
        "",
    ])
    for item in (entry for entry in evidence if entry.inlined):
        lines.extend([
            f"### {item.evidence_id} — `{item.source_path}`",
            "",
            f"- Locator: `{item.locator}`",
            f"- Timestamp: `{item.timestamp or 'not recorded'}`",
            f"- Role: `{item.role or 'not applicable'}`",
            f"- Record type: `{item.record_type}`",
            f"- Authority class: `{item.authority_class}`",
            *([rule_state_line(item)] if item.rule_state else []),
            f"- Content scope: `{item.content_scope}`",
            f"- Stored source SHA-256: `{item.source_sha256}`",
            f"- Current source SHA-256: `{item.current_source_sha256 or 'unavailable'}`",
            f"- Source hash state: `{item.source_hash_state}`",
            f"- Indexed content SHA-256: `{item.content_sha256}`",
            f"- Emitted content SHA-256: `{item.emitted_content_sha256}`",
        ])
        if item.integrity_notes:
            lines.append("- Integrity notes:")
            lines.extend(f"  - {note}" for note in item.integrity_notes)
        lines.extend(["", "```text", item.content, "```", ""])

    lines.extend(["## Retrieved but not inlined", ""])
    if omitted:
        for item in omitted:
            lines.append(
                f"- `{item['evidence_id']}` — `{item['source_path']}` — "
                f"`{item['locator']}` — {item['reason']}"
            )
    else:
        lines.append("- None.")
    lines.extend([
        "",
        "## Required root-agent check",
        "",
        "The root agent must open the cited original source for every claim used in the final "
        "answer. The retrieval subagent's prose and this routing metadata are not evidence by "
        "themselves.",
        "",
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def index_metadata(connection: sqlite3.Connection) -> "dict[str, str]":
    try:
        rows = dict(connection.execute("SELECT key, value FROM index_meta").fetchall())
    except sqlite3.OperationalError:
        rows = {}
    return rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="context_router.py",
        description=(
            "Route a prompt through a Markdown vault and emit a verbatim, "
            "evidence-bound context packet. It never answers the prompt."
        ),
    )
    prompt_group = parser.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt", help="The exact user prompt; it is preserved verbatim.")
    prompt_group.add_argument("--prompt-file", type=Path,
                              help="UTF-8 file containing the exact user prompt.")
    parser.add_argument("--vault", type=Path, required=True,
                        help="Root directory of the Markdown vault.")
    parser.add_argument("--config", type=Path, default=None,
                        help="Routing config JSON (default: <vault>/.context/routes.json).")
    parser.add_argument("--index", type=Path, default=None,
                        help="SQLite index built by build_index.py "
                             "(default: <vault>/.context/index.sqlite).")
    parser.add_argument("--facts", type=Path, default=None,
                        help="Answer-card JSON (default: <vault>/.context/facts.json). "
                             "Missing file simply disables the fast path.")
    parser.add_argument("--runs-dir", type=Path, default=None,
                        help="Where run artifacts are written "
                             "(default: <vault>/.context-runs).")
    parser.add_argument("--max-sources", type=int, default=None)
    parser.add_argument("--max-context-chars", type=int, default=None)
    parser.add_argument("--max-per-source", type=int, default=None)
    parser.add_argument("--full", action="store_true",
                        help="Force the widest retrieval tier instead of sizing the packet "
                             "to the prompt.")
    parser.add_argument("--no-fast-path", action="store_true",
                        help="Skip verified answer cards and always run retrieval.")
    parser.add_argument("--no-save", action="store_true", help="Write no run artifacts.")
    parser.add_argument("--stdout", action="store_true", help="Also print the full Markdown packet.")
    parser.add_argument("--evidence-json", action="store_true", help="Print actual evidence as versioned JSON for delivery evaluation.")
    parser.add_argument("--json", action="store_true",
                        help="Print only the machine-readable run summary.")
    return parser


def run(args: argparse.Namespace, resources: ExitStack) -> int:
    prompt = args.prompt if args.prompt is not None else args.prompt_file.read_text(encoding="utf-8")
    vault = args.vault.resolve()
    config_path = args.config or (vault / ".context" / "routes.json")
    database = args.index or (vault / ".context" / "index.sqlite")
    facts_path = args.facts or (vault / ".context" / "facts.json")
    if not vault.is_dir():
        raise FileNotFoundError(f"Vault directory not found: {vault}")
    config = load_config(config_path, required=True)
    if not database.is_file():
        raise FileNotFoundError(f"Index not found ({database.name}); run "
                                "`context-layer index <vault>` first")
    if not isinstance(config.get("routes"), dict):
        raise ValueError("Config requires a routes object")
    for name in ("max_sources", "max_per_source"):
        if getattr(args, name) is not None and getattr(args, name) <= 0:
            raise ValueError(f"{name} must be positive")
    if args.max_context_chars is not None and args.max_context_chars < 0:
        raise ValueError("max_context_chars cannot be negative")
    parsed = parse_prompt(prompt, config)

    # Tier the activation: the budget follows the stimulus. Explicit flags win.
    budget = activation_budget(parsed, prompt)
    if args.full:
        name, _limit, sources, chars, per_source = ACTIVATION_TIERS[-1]
        budget = {"tier": name, "max_sources": sources, "max_context_chars": chars,
                  "max_per_source": per_source, "canonical_sections": False,
                  "reason": "--full requested"}
    if args.max_sources is not None:
        budget["max_sources"] = args.max_sources
    if args.max_context_chars is not None:
        budget["max_context_chars"] = args.max_context_chars
    if args.max_per_source is not None:
        budget["max_per_source"] = args.max_per_source

    # Answer cards are ADDITIVE, never a replacement for retrieval: a verified
    # quote is a useful pointer, not the whole context a real request needs.
    fast = None
    if not args.no_fast_path:
        fast = fast_activation(prompt, parsed, config, vault, facts_path)

    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    resources.callback(connection.close)
    # Schema, format version and FTS consistency are checked even when a vague
    # prompt produces no query: an emptied full-text table must not read as NOT_FOUND.
    index_format.check_index(connection)
    ranked, query_log, query_errors, canonical_issues = retrieve(vault, connection, config, parsed)
    if query_errors or canonical_issues:
        raise EvidenceError("; ".join(query_errors + canonical_issues))
    if fast:
        # Cards cannot bypass index freshness: their quote and digest come from
        # one read, and their expected version must independently exist in SQLite.
        for card in fast["cards"]:
            expected = {r[0] for r in connection.execute(
                "SELECT DISTINCT source_sha256 FROM records WHERE source_path = ?",
                (card["source_path"],))}
            if expected != {card["source_sha256"]}:
                raise EvidenceError(f"fact_card_index_version_mismatch: {card['source_path']}")
    selected = choose_records(ranked, parsed, budget["max_sources"], config)
    selected, superseded_dropped = superseded_guard(selected)

    now = datetime.now().astimezone()
    run_id = now.strftime("%Y%m%dT%H%M%S") + "-" + sha256_bytes(prompt.encode("utf-8"))[:10]
    if args.no_save:
        run_dir = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix="context-router-")))
    else:
        runs_dir = args.runs_dir or (vault / ".context-runs")
        run_dir = runs_dir / now.strftime("%Y-%m-%d") / run_id
        base_run_dir = run_dir
        collision = 1
        while run_dir.exists():
            run_dir = base_run_dir.with_name(f"{base_run_dir.name}-{collision:02d}")
            collision += 1
        run_id = run_dir.name
    overflow_dir = run_dir / "overflow"
    overflow_dir.mkdir(parents=True, exist_ok=True)

    anchors = (parsed["exact_phrases"] + parsed.get("route_anchors", [])
               + parsed["aliases"] + parsed["tokens"])
    evidence: "list[Evidence]" = []
    final_selected: "list[Candidate]" = []
    integrity_limits: "list[str]" = []
    omitted: "list[dict[str, str]]" = []
    used_chars = 0
    duplicate_materializations_skipped = 0
    emitted_semantic_seen: "set[str]" = set()
    for candidate in selected:
        content, scope, notes, current_sha, source_state = materialize_candidate(
            vault, candidate, anchors,
            max_per_source=budget["max_per_source"],
            overflow_dir=overflow_dir,
            canonical_sections=budget["canonical_sections"],
        )
        limit_notes = known_limits(config, candidate)
        notes.extend(limit_notes)
        integrity_limits.extend(limit_notes)
        emitted_semantic = semantic_hash(content)
        if emitted_semantic in emitted_semantic_seen:
            duplicate_materializations_skipped += 1
            continue
        emitted_semantic_seen.add(emitted_semantic)
        final_selected.append(candidate)
        evidence_id = f"E{len(final_selected):03d}"
        emitted_sha = sha256_bytes(content.encode("utf-8"))
        inlined = used_chars + len(content) <= budget["max_context_chars"]
        overflow_path = ""
        overflow_sha = ""
        if not inlined:
            overflow = overflow_dir / f"{evidence_id}__{sha256_bytes(candidate.source_path.encode())[:12]}.txt"
            overflow.write_text(content, encoding="utf-8", newline="")
            overflow_path = str(overflow)
            overflow_sha = sha256_file(overflow)
            omitted.append({
                "evidence_id": evidence_id,
                "source_path": candidate.source_path,
                "locator": candidate.locator,
                "overflow_path": overflow_path,
                "overflow_sha256": overflow_sha,
                "reason": f"context budget reached; exact content stored in {overflow_path}",
            })
        else:
            used_chars += len(content)
        relation = "canonical_source" if candidate.mandatory else "lexical_match"
        evidence.append(Evidence(
            evidence_id=evidence_id,
            source_path=candidate.source_path,
            absolute_path=str(candidate_absolute(vault, candidate)),
            locator=candidate.locator,
            timestamp=candidate.timestamp,
            role=candidate.role,
            record_type=candidate.record_type,
            source_sha256=candidate.source_sha256,
            current_source_sha256=current_sha,
            source_hash_state=source_state,
            content_sha256=candidate.content_sha256,
            emitted_content_sha256=emitted_sha,
            content_scope=scope,
            authority_class=authority_class(candidate),
            route_reasons=[
                route for route in parsed["routes"]
                if any(
                    candidate.source_path.startswith(hint) or hint in candidate.source_path
                    for hint in config["routes"][route].get("path_hints", [])
                ) or candidate.source_path in [
                    e["path"] if isinstance(e, dict) else e
                    for e in config["routes"][route].get("canonical_sources", [])
                ]
            ],
            query_hits=sorted(set(candidate.query_hits)),
            integrity_notes=notes,
            mandatory=candidate.mandatory,
            rule_state=candidate.rule_state,
            superseded_by=candidate.superseded_by,
            relation=relation,
            inlined=inlined,
            overflow_path=overflow_path,
            overflow_sha256=overflow_sha,
            content=content if inlined else "",
        ))

    status, status_reasons, conflict_review = determine_status(
        final_selected, evidence, parsed, integrity_limits
    )
    meta_rows = index_metadata(connection)
    metadata = {
        "run_id": run_id,
        "created_at": now.isoformat(),
        "engine_version": config.get("engine_version"),
        "vault": str(vault),
        "index_path": str(database),
        "index_sha256": sha256_file(database),
        "index_built_at": meta_rows.get("built_at", "unknown"),
        "selected_records": len(final_selected),
        "evidence_ledger_records": len(evidence),
        "inlined_evidence": sum(item.inlined for item in evidence),
        "inlined_characters": used_chars,
        "omitted_to_overflow": len(omitted),
        "duplicate_materializations_skipped": duplicate_materializations_skipped,
        "mirror_copies_suppressed": getattr(choose_records, "last_mirror_superseded", {}),
        "artifacts_saved": not args.no_save,
    }
    connection.close()

    for entry in superseded_dropped:
        omitted.append({
            "evidence_id": "-",
            "source_path": entry["source_path"],
            "locator": "whole file",
            "reason": entry["reason"],
        })
    for mirror, kept in metadata["mirror_copies_suppressed"].items():
        omitted.append({
            "evidence_id": "-",
            "source_path": mirror,
            "locator": "whole file",
            "reason": f"mirror copy of `{kept}`, already in this packet",
        })

    resolved_rules = [
        {
            "path": candidate.source_path,
            "state": "in force" if candidate.rule_state == "current" else candidate.rule_state,
            "superseded_by": candidate.superseded_by,
        }
        for candidate in final_selected
        if candidate.rule_state
    ]
    packet = markdown_packet(
        prompt, run_id, parsed, status, status_reasons, conflict_review,
        evidence, omitted, metadata, fast=fast, resolved_rules=resolved_rules,
    )
    summary = {
        **metadata,
        # Activation cost is a first-class measurement: a packet that answers a
        # short prompt with a large body is a failure, not a success.
        "packet_characters": len(packet),
        "activation_tier": budget["tier"],
        "activation_reason": budget["reason"],
        "operation_status": "ok",
        "status": status,
        "status_reasons": status_reasons,
        "conflict_review_required": conflict_review,
        "web_revalidation_required": parsed["web_revalidation_required"],
        "needs_clarification": parsed["needs_clarification"],
        "routes": parsed["routes"],
        "context_packet": None if args.no_save else str(run_dir / "context.md"),
        "evidence_ledger": None if args.no_save else str(run_dir / "evidence.jsonl"),
        "query_errors": query_errors,
        "canonical_issues": canonical_issues,
    }

    if not args.no_save:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "request.json").write_text(json.dumps({
            "prompt": prompt,
            "prompt_sha256": sha256_bytes(prompt.encode("utf-8")),
            "created_at": now.isoformat(),
        }, ensure_ascii=False, indent=2), encoding="utf-8", newline="")
        (run_dir / "routing.json").write_text(json.dumps({
            "parsed": parsed,
            "query_log": query_log,
            "query_errors": query_errors,
            "canonical_issues": canonical_issues,
        }, ensure_ascii=False, indent=2), encoding="utf-8", newline="")
        with (run_dir / "evidence.jsonl").open("w", encoding="utf-8", newline="") as handle:
            for item in evidence:
                record = asdict(item)
                record.pop("content")
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        (run_dir / "omitted.json").write_text(json.dumps(omitted, ensure_ascii=False, indent=2), encoding="utf-8", newline="")
        (run_dir / "context.md").write_text(packet, encoding="utf-8", newline="")
        (run_dir / "run.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="")
        (run_dir / "SUBAGENT_BRIEF.md").write_text(SUBAGENT_BRIEF, encoding="utf-8", newline="")

    if args.evidence_json:
        # This is the actual delivered evidence, not a metadata-only ledger.
        print(json.dumps({
            "schema": "evidence-delivery-v1", "operation_status": "ok", "status": status,
            "evidence": [{"source_path": e.source_path, "source_sha256": e.source_sha256,
                          "content": e.content} for e in evidence if e.inlined and e.content],
        }, ensure_ascii=False))
    elif args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif args.stdout:
        print(packet)
    else:
        print(f"status: {status}")
        print(f"routes: {', '.join(parsed['routes']) or 'none'}")
        print(f"activation_tier: {budget['tier']}")
        print(f"packet_characters: {len(packet)}")
        print(f"external_revalidation_required: {str(parsed['web_revalidation_required']).lower()}")
        print(f"conflict_review_required: {str(conflict_review).lower()}")
        if args.no_save:
            print("context_packet: not saved (--no-save)")
            print("evidence_ledger: not saved (--no-save)")
        else:
            print(f"context_packet: {run_dir / 'context.md'}")
            print(f"evidence_ledger: {run_dir / 'evidence.jsonl'}")
        if query_errors:
            print("query_errors:")
            for error in query_errors:
                print(f"- {error}")
        if canonical_issues:
            print("canonical_issues:")
            for issue in canonical_issues:
                print(f"- {issue}")
    return 0 if status != "NOT_FOUND" else 2


def main(argv: "list[str] | None" = None) -> int:
    configure_stdout()
    args = build_parser().parse_args(argv)
    try:
        with ExitStack() as resources:
            return run(args, resources)
    except (EvidenceError, sqlite3.Error, OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # ERROR, not PARTIAL: PARTIAL means some evidence was found, and here none is given.
        failure = {"schema": "evidence-delivery-v1", "operation_status": "error",
                   "status": "ERROR", "evidence": [], "error": str(exc)}
        if args.json or args.evidence_json:
            print(json.dumps(failure, ensure_ascii=False))
        else:
            print(f"context_router operational error: {exc}\nEvidence packet withheld; status: ERROR")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
