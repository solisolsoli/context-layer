"""context_layer.synapse — synaptic retrieval (experimental, opt-in).

"Synaptic" is a name for a literal mechanism: the vault's explicit link graph
(context_layer.graph) is used for bounded spreading activation. FTS hits (plus
notes the prompt names by title or alias) are the seeds; activation spreads
along source-anchored edges for a bounded number of hops; passages are chosen
inside the activated notes and packed into a token budget.

The packet keeps the evidence-delivery-v1 shape (source_path, source_sha256,
verbatim content) and adds, per passage, the byte and line span, the hop, the
activation, the reason it was chosen and the `via` edge chain that reached it.

Opt-in (`--method synaptic`); FTS stays the default. Hard limits: at most
MAX_HOPS_LIMIT hops (default 1); a note with more distinct neighbours than
ADJACENCY_CAP is a hub and is never expanded from; if a hop would take
activation past NODE_CAP notes, that hop is left out (the hops before it stay;
at the first hop that is the lexical, seed-only packet, unchanged). A damaged
graph.sqlite degrades to the packet without the graph (`graph_unreadable`).
Token counts are estimates: ceil(chars / 4).

After every synaptic retrieval `<vault>/.context/activation.json` is written
atomically (unless `write_activation: false`). It carries a random run id, not
a hash of the query, and the query text only when the user opted in. It is
never read back into ranking (the Obsidian plugin displays it; the advisor's side channel
reads only its run id); its paths
are NFC-normalised, as Obsidian lists them.

Python 3.10+; standard library only. No network, no model call.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import tempfile
import unicodedata

from . import graph as graphs

BUDGET_TOKENS = 1200      # --compact: the whole packet's budget
EXTRA_TOKENS = 600        # default mode: budget for graph extras added after the fts packet
MAX_HOPS_DEFAULT = 1
MAX_HOPS_LIMIT = 2
NODE_CAP = 64             # more activated notes than this -> lexical fallback
ADJACENCY_CAP = 24        # a note with more distinct neighbours is a hub: not expanded from
DECAY = 0.5               # activation kept per hop
BACKLINK_FACTOR = 0.8     # traversing a link against its direction is slightly weaker
KIND_WEIGHT = {"wikilink": 1.0, "embed": 1.0, "mdlink": 1.0, "frontmatter": 0.9}
MIN_ACTIVATION = 0.005    # contributions below this are dropped
LINK_REL_WEIGHT = 2.0     # an edge whose link line matches the query carries x(1 + 2 x rel)
DOMINANCE = 1.5           # top seed "dominates" when its bm25 is this many times the next
NAME_SEEDS = 3            # notes named in the prompt (title/basename/alias) added as seeds
NAME_MIN_CHARS = 4
ANCHOR_BONUS = 0.5        # the paragraph holding a link that was traversed
TARGET_BONUS = 0.5        # the section/block a [[note#heading]] or [[note#^block]] points at
HEADING_WEIGHT = 0.5      # a query term found only in the enclosing headings
TITLE_INHERIT = 0.5       # share of the title block's relevance other blocks inherit when
                          # the query matched the note only by its title
FLOOR_RATIO = 0.2         # passages below this share of the best score are never packed
COVERED_FLOOR_RATIO = 0.6 # once every query term is covered, only strong passages are added
COMPLEMENT_BONUS = 0.5    # x best score x share of still-uncovered query terms a passage adds
CANDIDATE_CAP = 400       # passages considered by the packer, highest score first
PASSAGE_MAX_CHARS = 1200
RESERVE_NOTES = 4         # linked notes guaranteed a passage before the budget is filled by score
RESERVE_TOKENS = 200      # per reserved note: a note body this short is delivered whole
RESERVE_SHARE = 0.6       # reserved passages use at most this share of the token budget
RESERVE_REL_RATIO = 0.6   # reserve linked notes whose link relevance is >= this x the best one
PARAGRAPH_WEIGHT = 0.5    # link relevance: query terms in the link's paragraph/section count half
PER_SOURCE_PASSAGES = 3
TRACE_NODES = 200
TRACE_EDGES = 400
TRACE_NAME = "activation.json"
TOKEN = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*")
LINK_SPAN = re.compile(r"!?\[\[[^\[\]\n]*\]\]|!?\[[^\]\n]*\]\([^)\n]*\)")
ESTIMATOR = {"est_tokens_estimator": "ceil(characters / 4)",
             "est_tokens_scope": "evidence content only; JSON framing and the hook wrapper "
                                 "are not counted; not a model tokenizer count"}
STOP_REASONS = ("hop_cap", "budget_limited", "ambiguous_target", "scope_excluded",
                "target_unavailable", "attachment_target", "below_threshold", "node_cap")


def est_tokens(text: str) -> int:
    """An estimate, not a model tokenizer: ceil(characters / 4)."""
    return math.ceil(len(text) / 4)


def fold(text: str) -> str:
    """NFKC, casefold and strip combining marks (incl. U+0307 from a casefolded 'İ') —
    close to FTS5 unicode61 remove_diacritics 2."""
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c))


def tokens(text: str) -> set[str]:
    return set(TOKEN.findall(fold(text)))


def name_key(text: str) -> str:
    """A note name or prompt as space-separated folded tokens, for phrase matching."""
    return " ".join(TOKEN.findall(fold(text.replace("_", " ").replace("-", " "))))


# ---------------------------------------------------------------------------
# Blocks and passages
# ---------------------------------------------------------------------------

@dataclass
class Block:
    start: int                 # 1-based first line
    end: int                   # 1-based last line (inclusive)
    lo: int                    # character offset of the first character
    hi: int                    # character offset after the last character
    headings: list[str]        # enclosing section headings (level 2+), outermost first


HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")


def _kept_lines(text: str) -> list[str]:
    """Lines with their terminators, split on "\\n" only: the numbering of
    graph.split_lines, `grep -n` and editors (U+2028, form feed, NEL stay inside)."""
    parts = text.split("\n")
    kept = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        kept.append(parts[-1])
    return kept


def _bare(line: str) -> str:
    """A kept line without its "\\n" and the "\\r" of a CRLF ending."""
    if line.endswith("\n"):
        line = line[:-1]
    return line[:-1] if line.endswith("\r") else line


def _line_offsets(kept: list[str]) -> list[int]:
    offsets, total = [], 0
    for line in kept:
        offsets.append(total)
        total += len(line)
    return offsets


def split_blocks(text: str) -> list[Block]:
    """Paragraph-sized blocks outside the frontmatter; a lone heading joins the next block."""
    kept = _kept_lines(text)
    bare = [_bare(line) for line in kept]
    offsets = _line_offsets(kept)
    span = graphs.frontmatter_span(bare)
    code = graphs.code_lines(bare, span)
    chain: dict[int, str] = {}
    raw: list[tuple[int, int, list[str], bool]] = []   # (first, last, headings, heading_only)
    state = {"start": None, "only": False, "heads": []}

    def close(end: int) -> None:
        if state["start"] is not None:
            raw.append((state["start"], end, state["heads"], state["only"]))
        state["start"] = None

    for index in range(span, len(bare)):
        line = bare[index]
        match = HEADING.match(line) if index not in code else None
        if not line.strip() and index not in code:
            close(index - 1)
            continue
        if match:
            close(index - 1)
            level = len(match.group(1))
            for deeper in [k for k in chain if k >= level]:
                del chain[deeper]
            chain[level] = match.group(2)
            state.update(start=index, only=True, heads=_section_chain(chain))
            continue
        if state["start"] is None:
            state.update(start=index, only=False, heads=_section_chain(chain))
        else:
            state["only"] = False
    close(len(bare) - 1)
    merged: list[tuple[int, int, list[str]]] = []
    pending = None
    for first, last, heads, only in raw:
        if pending is not None:
            first, pending = pending[0], None
        if only:
            pending = (first, last, heads)
            continue
        merged.append((first, last, heads))
    if pending is not None:
        merged.append(pending)
    blocks: list[Block] = []
    for first, last, heads in merged:
        blocks.extend(_windows(kept, bare, offsets, first, last, heads))
    return blocks


def _section_chain(chain: dict[int, str]) -> list[str]:
    """Enclosing section headings. The level-1 title is left out: it covers every block of
    the note equally, so it says nothing about which block answers the query."""
    return [chain[level] for level in sorted(chain) if level > 1]


def _windows(kept, bare, offsets, first: int, last: int, heads: list[str]) -> list[Block]:
    """Split a block longer than PASSAGE_MAX_CHARS on line boundaries (or inside one line)."""
    spans: list[tuple[int, int]] = []
    begin, size = first, 0
    for index in range(first, last + 1):
        length = len(kept[index])
        if size and size + length > PASSAGE_MAX_CHARS:
            spans.append((begin, index - 1))
            begin, size = index, 0
        size += length
    spans.append((begin, last))
    out: list[Block] = []
    for a, b in spans:
        lo, hi = offsets[a], offsets[b] + len(bare[b])
        if hi - lo <= PASSAGE_MAX_CHARS:
            out.append(Block(a + 1, b + 1, lo, hi, list(heads)))
            continue
        for cut in range(lo, hi, PASSAGE_MAX_CHARS):
            out.append(Block(a + 1, b + 1, cut, min(hi, cut + PASSAGE_MAX_CHARS), list(heads)))
    return out


@dataclass
class Passage:
    path: str
    sha: str
    text: str                  # the whole verified source text
    block: Block
    rel: float = 0.0
    matched: set[str] = field(default_factory=set)
    score: float = 0.0
    reason: str = "query terms"
    anchors: list[dict] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)

    @property
    def content(self) -> str:
        return self.text[self.block.lo:self.block.hi]

    @property
    def tokens(self) -> int:
        return est_tokens(self.content)

    def key(self) -> tuple:
        return (self.path, self.block.lo, self.block.hi)


def relevance(passage_text: str, headings: list[str], query: list[str]) -> tuple[float, set[str]]:
    if not query:
        return 0.0, set()
    body = tokens(passage_text)
    heads = tokens(" ".join(headings))
    matched = {term for term in query if term in body}
    head_only = {term for term in query if term not in body and term in heads}
    return (len(matched) + HEADING_WEIGHT * len(head_only)) / len(query), matched | head_only


def line_block(text: str, line: int) -> Block | None:
    """The single source line `line` (1-based) as a block."""
    kept = _kept_lines(text)
    if not 1 <= line <= len(kept):
        return None
    lo = sum(len(x) for x in kept[:line - 1])
    hi = lo + len(_bare(kept[line - 1]))
    return Block(line, line, lo, hi, []) if kept[line - 1].strip() else None


def body_block(text: str) -> Block | None:
    """Everything after the frontmatter, leading/trailing blank space trimmed."""
    kept = _kept_lines(text)
    bare = [_bare(line) for line in kept]
    span = graphs.frontmatter_span(bare)
    first = next((i for i in range(span, len(bare)) if bare[i].strip()), None)
    if first is None:
        return None
    last = max(i for i in range(first, len(bare)) if bare[i].strip())
    offsets = _line_offsets(kept)
    return Block(first + 1, last + 1, offsets[first], offsets[last] + len(bare[last]), [])


# ---------------------------------------------------------------------------
# Options, sources
# ---------------------------------------------------------------------------

@dataclass
class Options:
    top_k: int = 3
    budget_chars: int = 6000
    per_source_chars: int = 2000
    budget_tokens: int = BUDGET_TOKENS
    max_hops: int = MAX_HOPS_DEFAULT
    node_cap: int = NODE_CAP
    adjacency_cap: int = ADJACENCY_CAP
    record_query: bool = False
    extra_tokens: int = EXTRA_TOKENS
    compact: bool = False          # set by retrieve() (compact) and extend() (superset)
    jev_candidates: int = 0   # side channel for the optional advisor; see "Jev candidates"


class Sources:
    """Reads each allowed source once, after the exclusion check; bytes that no longer match
    the index are withheld."""

    def __init__(self, reader, indexed: dict[str, str], allowed: set[str]):
        self.reader = reader
        self.indexed = indexed
        self.allowed = allowed
        self.cache: dict[str, tuple[str, str] | None] = {}
        self.withheld: list[str] = []
        self.lines: dict[str, list[str]] = {}

    def get(self, path: str) -> tuple[str, str] | None:
        if path not in self.allowed:
            return None                       # excluded or not indexed: never read
        if path in self.cache:
            return self.cache[path]
        result = None
        try:
            raw = self.reader(path)
            sha = hashlib.sha256(raw).hexdigest()
            if sha == self.indexed.get(path):
                result = (raw.decode("utf-8"), sha)
            else:
                self.withheld.append(path)
        except (OSError, UnicodeError, ValueError):
            self.withheld.append(path)
        self.cache[path] = result
        return result

    def line(self, path: str, number: int) -> str:
        if path not in self.lines:
            loaded = self.get(path)
            self.lines[path] = graphs.split_lines(loaded[0]) if loaded else []
        lines = self.lines[path]
        return lines[number - 1] if 1 <= number <= len(lines) else ""


def seed_scores(rows, allowed: set[str], top_k: int) -> dict[str, float]:
    """Best bm25 per source (FTS5 bm25 is lower-is-better), as a positive strength.

    Rows arrive ordered by (bm25, source_path, id), so ties end in the path."""
    best: dict[str, float] = {}
    for name, score in rows:
        if name in allowed and name not in best:
            best[name] = -float(score)
        if len(best) >= top_k:
            break
    return best


def named_notes(prompt: str, allowed: set[str], graph) -> list[str]:
    """Notes the prompt names by basename or frontmatter alias (FTS does not index names)."""
    key = f" {name_key(prompt)} "
    aliases: dict[str, list[str]] = {}
    if graph is not None:
        for path, raw in graph.aliases():
            try:
                aliases[path] = [a for a in json.loads(raw) if isinstance(a, str)]
            except ValueError:
                aliases[path] = []
    found: list[tuple[int, str]] = []
    for path in sorted(allowed):
        if not path.lower().endswith(graphs.NOTE_SUFFIX):
            continue
        names = [PurePosixPath(path).name[:-len(graphs.NOTE_SUFFIX)]] + aliases.get(path, [])
        best = 0
        for name in names:
            folded = name_key(name)
            if len(folded) >= NAME_MIN_CHARS and f" {folded} " in key:
                best = max(best, len(folded))
        if best:
            found.append((best, path))
    found.sort(key=lambda item: (-item[0], item[1]))
    return [path for _, path in found[:NAME_SEEDS]]


# ---------------------------------------------------------------------------
# Spreading activation
# ---------------------------------------------------------------------------

def _edge_view(source: str, target: str, kind: str, weight: float, edge, link_rel: float) -> dict:
    view = {"from": source, "to": target, "kind": kind, "weight": round(min(weight, 1.0), 4),
            "anchor": {"path": edge.source, "line": edge.line},
            "_link_rel": link_rel, "_heading": edge.heading, "_block": edge.block,
            "_link_kind": edge.kind}
    if edge.field:
        view["_field"] = edge.field
    return view


@dataclass
class Spread:
    activation: dict[str, float]
    hop: dict[str, int]
    via: dict[str, list[dict]]
    used: list[dict] = field(default_factory=list)
    hubs: list[str] = field(default_factory=list)
    stale: set[str] = field(default_factory=set)
    capped: bool = False
    scope_excluded: int = 0
    hop_cap: int = 0
    below_threshold: int = 0      # contributions dropped under MIN_ACTIVATION
    capped_at: int = 0            # the hop at which NODE_CAP stopped the spread (0: never)
    left_out: int = 0             # notes that hop would have added


def spread(graph, seeds: dict[str, float], allowed: set[str], options: Options,
           link_rel=lambda path, line: 0.0) -> Spread:
    """Bounded spreading activation over fresh edges, query-conditioned by the seeds.

    contribution = activation(u) x DECAY x kind weight x (1 + LINK_REL_WEIGHT x link_rel)
                   / degree(u) / sqrt(degree(v))
    degree = distinct linked notes. 1/degree(u) is the out-degree normalisation;
    1/sqrt(degree(v)) keeps a hub target from collecting activation. Repeated links
    between the same two notes count once (the strongest), never summed. Every edge
    used carries the hop it was traversed at. When a hop would pass NODE_CAP notes,
    it is undone and the spread stops there: the hops before it are kept.
    """
    state = Spread(dict(seeds), {name: 0 for name in seeds}, {name: [] for name in seeds})
    frontier = sorted(seeds)
    depth_limit = min(options.max_hops, MAX_HOPS_LIMIT)
    for depth in range(1, depth_limit + 1):
        gained: dict[str, float] = {}
        parents: dict[str, tuple[float, str, dict]] = {}
        used_before = len(state.used)
        for node in frontier:
            degree = graph.degree(node)
            if degree > options.adjacency_cap:
                state.hubs.append(node)
                continue
            best: dict[str, tuple[float, str, object, float]] = {}
            if graph.fresh(node):
                for edge in graph.outgoing(node):
                    if edge.target == node:
                        continue
                    if edge.target not in allowed:
                        state.scope_excluded += 1
                        continue
                    rel = link_rel(edge.source, edge.line)
                    weight = KIND_WEIGHT.get(edge.kind, 1.0) * (1 + LINK_REL_WEIGHT * rel)
                    if weight > best.get(edge.target, (0.0,))[0]:
                        best[edge.target] = (weight, edge.kind, edge, rel)
            elif graph.note(node) is not None:
                state.stale.add(node)
            for edge in graph.incoming(node):
                other = edge.source
                if other == node:
                    continue
                if other not in allowed:
                    state.scope_excluded += 1
                    continue
                if not graph.fresh(other):
                    state.stale.add(other)
                    continue
                rel = link_rel(edge.source, edge.line)
                weight = KIND_WEIGHT.get(edge.kind, 1.0) * BACKLINK_FACTOR * (
                    1 + LINK_REL_WEIGHT * rel)
                if weight > best.get(other, (0.0,))[0]:
                    best[other] = (weight, "backlink", edge, rel)
            for other in sorted(best):
                weight, kind, edge, rel = best[other]
                contribution = state.activation[node] * DECAY * weight / degree / math.sqrt(
                    graph.degree(other))
                if contribution < MIN_ACTIVATION:
                    state.below_threshold += 1
                    continue
                view = _edge_view(node, other, kind, contribution, edge, rel)
                view["hop"] = depth
                state.used.append(view)
                gained[other] = gained.get(other, 0.0) + contribution
                if contribution > parents.get(other, (0.0,))[0]:
                    parents[other] = (contribution, node, view)
        new = sorted(name for name in gained if name not in state.activation)
        if len(state.activation) + len(new) > options.node_cap:
            del state.used[used_before:]          # this hop is not used; the ones before stay
            state.capped, state.capped_at, state.left_out = True, depth, len(new)
            return state
        for name in sorted(gained):
            state.activation[name] = min(1.0, state.activation.get(name, 0.0) + gained[name])
        for name in new:
            _, parent, view = parents[name]
            state.hop[name] = depth
            state.via[name] = state.via[parent] + [view]
        frontier = new
        if not frontier:
            break
    else:
        # The loop ran to the hop limit: count links that lead further out.
        for node in frontier:
            if graph.degree(node) > options.adjacency_cap or not graph.fresh(node):
                continue
            further = {e.target for e in graph.outgoing(node)} | {
                e.source for e in graph.incoming(node)}
            state.hop_cap += sum(1 for n in further
                                 if n in allowed and n not in state.activation)
    return state


def seeds_link_each_other(graph, seeds: list[str]) -> bool:
    members = set(seeds)
    for seed in sorted(seeds):
        if not graph.fresh(seed):
            continue
        if any(edge.target in members and edge.target != seed for edge in graph.outgoing(seed)):
            return True
    return False


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

def match_candidates(path: str, text: str, sha: str, query: list[str], activation: float,
                     targets: list[dict], anchors: list[dict]) -> list[Passage]:
    """Score every block of one note: query terms, plus the link lines that connected it."""
    out = []
    blocks = split_blocks(text)
    scored = [relevance(text[b.lo:b.hi], b.headings, query) for b in blocks]
    title = 0 if blocks and text[blocks[0].lo:blocks[0].hi].lstrip().startswith("# ") else None
    title_only = title is not None and scored[title][0] > 0 and not any(
        rel > 0 for index, (rel, _) in enumerate(scored) if index != title)
    for index, block in enumerate(blocks):
        rel, matched = scored[index]
        reason = "query terms"
        if title_only and index != title:
            # The query matched this note only by its title: the note as a whole is the
            # match, so its other blocks inherit part of that (without claiming the terms).
            rel, reason = TITLE_INHERIT * scored[title][0], "note title matched"
        passage = Passage(path, sha, text, block, rel, set(matched), reason=reason)
        bonus = 0.0
        for anchor in anchors:
            if block.start <= anchor["line"] <= block.end:
                bonus = max(bonus, anchor["activation"] * ANCHOR_BONUS)
                _add_anchor(passage, anchor)
        if _targets_block(passage, targets):
            bonus = max(bonus, activation * TARGET_BONUS)
        passage.score = activation * rel + bonus
        if passage.score > 0 and (rel > 0 or bonus > 0):
            if rel == 0:
                passage.reason = "holds a traversed link" if passage.anchors else "link target"
            out.append(passage)
    return out


def _add_anchor(passage: Passage, anchor: dict) -> None:
    entry = {k: anchor[k] for k in ("to", "kind", "line")}
    if entry not in passage.anchors:
        passage.anchors.append(entry)


def _targets_block(passage: Passage, targets: list[dict]) -> bool:
    content = passage.content
    for target in targets:
        if target.get("block") and f"^{target['block']}" in content:
            return True
        if target.get("heading") and passage.block.headings and \
                fold(target["heading"]) == fold(passage.block.headings[-1]):
            return True
    return False


def reserved_passages(path: str, text: str, sha: str, activation: float, query: list[str],
                      targets: list[dict], anchors: list[dict], context: set[str],
                      budget: int) -> list[Passage]:
    """Passages that represent one linked note, chosen by what connects it, not by overlap
    with the question: the whole body when it is short; otherwise the section a link
    targets, the paragraph holding the link, the lead paragraph, then the paragraphs that
    share most words with the question and the linking line."""
    body = body_block(text)
    if body is None:
        return []
    whole = Passage(path, sha, text, body, 0.0, set(), reason="linked note (whole)")
    whole.rel, whole.matched = relevance(whole.content, [], query)
    whole.score = activation * (0.5 + whole.rel)
    if whole.tokens <= budget:
        for anchor in anchors:
            if body.start <= anchor["line"] <= body.end:
                _add_anchor(whole, anchor)
        return [whole]
    blocks = split_blocks(text)
    if not blocks:
        return []
    ranked: list[tuple[int, float, int, Passage]] = []
    for index, block in enumerate(blocks):
        passage = Passage(path, sha, text, block)
        passage.rel, passage.matched = relevance(passage.content, block.headings, query)
        overlap = len(tokens(passage.content) & context) / max(len(context), 1)
        tier, reason = 4, "shares words with the question or link line"
        if _targets_block(passage, targets):
            tier, reason = 0, "link target section"
        elif any(block.start <= a["line"] <= block.end for a in anchors):
            tier, reason = 1, "holds the link"
            for anchor in anchors:
                if block.start <= anchor["line"] <= block.end:
                    _add_anchor(passage, anchor)
        elif index == 0:
            tier, reason = 2, "lead paragraph of a linked note"
        passage.reason = reason
        passage.score = activation * (0.5 + passage.rel)
        ranked.append((tier, -(passage.rel + overlap), block.start, passage))
    ranked.sort(key=lambda item: item[:3])
    out, spent = [], 0
    for tier, value, _, passage in ranked:
        if tier == 4 and value == 0:
            continue
        if spent + passage.tokens > budget:
            continue
        out.append(passage)
        spent += passage.tokens
    return out


# ---------------------------------------------------------------------------
# Packing
# ---------------------------------------------------------------------------

class Packer:
    """Every limit holds at every step: tokens, characters, per-source, overlap, repeats."""

    def __init__(self, options: Options, query: list[str]):
        self.options = options
        self.terms = set(query)
        self.chosen: list[Passage] = []
        self.tokens = self.chars = 0
        self.per_chars: dict[str, int] = {}
        self.per_count: dict[str, int] = {}
        self.covered: set[str] = set()
        self.budget_limited = 0
        self.occupied: dict[str, list[tuple[int, int]]] = {}   # spans owned by the fts packet
        self.taken_text: set[str] = set()

    def occupy(self, path: str, lo: int, hi: int, content: str, matched: set[str]) -> None:
        """Mark a span as already delivered (by the fts packet): never repeated, never
        counted against this packer's budget."""
        self.occupied.setdefault(path, []).append((lo, hi))
        self.taken_text.add(content)
        self.covered |= matched

    def overlaps(self, p: Passage) -> bool:
        return any(o.path == p.path and o.block.lo < p.block.hi and p.block.lo < o.block.hi
                   for o in self.chosen) or any(
            lo < p.block.hi and p.block.lo < hi for lo, hi in self.occupied.get(p.path, []))

    def duplicate_of(self, p: Passage) -> Passage | None:
        content = p.content
        return next((o for o in self.chosen if o.path != p.path and o.content == content), None)

    def fits(self, p: Passage, token_cap: int | None = None) -> bool:
        size = len(p.content)
        cap = self.options.budget_tokens if token_cap is None else token_cap
        return not (self.tokens + p.tokens > cap
                    or self.chars + size > self.options.budget_chars
                    or self.per_chars.get(p.path, 0) + size > self.options.per_source_chars
                    or self.per_count.get(p.path, 0) >= PER_SOURCE_PASSAGES)

    def offer(self, p: Passage, token_cap: int | None = None) -> bool:
        if self.overlaps(p) or p.content in self.taken_text:
            return False
        original = self.duplicate_of(p)
        if original is not None:            # byte-identical text: list it, deliver it once
            entry = {"source_path": p.path, "source_sha256": p.sha,
                     "line_start": p.block.start, "line_end": p.block.end}
            if entry not in original.duplicates:
                original.duplicates.append(entry)
            return False
        if not self.fits(p, token_cap):
            self.budget_limited += 1
            return False
        self.chosen.append(p)
        self.covered |= p.matched
        self.tokens += p.tokens
        self.chars += len(p.content)
        self.per_chars[p.path] = self.per_chars.get(p.path, 0) + len(p.content)
        self.per_count[p.path] = self.per_count.get(p.path, 0) + 1
        return True

    def fill(self, candidates: list[Passage]) -> None:
        """Greedy by utility per estimated token (score + bonus for uncovered query terms)."""
        if not candidates:
            return
        pool = sorted(candidates, key=lambda c: (-c.score, c.path, c.block.lo))[:CANDIDATE_CAP]
        best = max(c.score for c in pool)
        while pool:
            floor = (COVERED_FLOOR_RATIO if self.terms <= self.covered else FLOOR_RATIO) * best
            scored = []
            for c in pool:
                new = len(c.matched - self.covered) / len(self.terms) if self.terms else 0.0
                utility = c.score + COMPLEMENT_BONUS * new * best
                if utility >= floor:
                    scored.append((-utility / max(c.tokens, 1), -utility, c.path, c.block.lo, c))
            if not scored:
                break
            scored.sort(key=lambda row: row[:4])
            placed = False
            for row in scored:
                c = row[4]
                pool.remove(c)
                if self.offer(c):
                    placed = True
                    break
            if not placed and not pool:
                break

    def merge_adjacent(self) -> None:
        """Windows of one file separated only by whitespace become one window, if it fits."""
        self.chosen.sort(key=lambda p: (p.path, p.block.lo))
        merged: list[Passage] = []
        for p in self.chosen:
            last = merged[-1] if merged else None
            if last is not None and last.path == p.path and \
                    not p.text[last.block.hi:p.block.lo].strip():
                block = Block(last.block.start, p.block.end, last.block.lo, p.block.hi,
                              last.block.headings)
                joined = Passage(p.path, p.sha, p.text, block, max(last.rel, p.rel),
                                 last.matched | p.matched, max(last.score, p.score),
                                 last.reason if last.score >= p.score else p.reason,
                                 last.anchors + [a for a in p.anchors if a not in last.anchors],
                                 last.duplicates + p.duplicates)
                extra = joined.tokens - last.tokens - p.tokens
                if self.tokens + extra <= self.options.budget_tokens and \
                        self.chars + (p.block.lo - last.block.hi) <= self.options.budget_chars:
                    self.tokens += extra
                    self.chars += p.block.lo - last.block.hi
                    merged[-1] = joined
                    continue
            merged.append(p)
        self.chosen = merged


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def _label(step: dict) -> dict:
    """A via step with literal labels: 'links to' / 'linked from', and the link kind."""
    kind = step["kind"]
    link_kind = step.get("_link_kind", kind)
    edge = f"frontmatter:{step['_field']}" if step.get("_field") else link_kind
    out = {"from": step["from"], "to": step["to"], "kind": kind,
           "label": "linked from" if kind == "backlink" else "links to",
           "edge": edge, "weight": step["weight"], "anchor": dict(step["anchor"])}
    out["text"] = (f"{out['from']} {out['label']} {out['to']} "
                   f"({edge}, {step['anchor']['path']}:{step['anchor']['line']})")
    return out


def _byte_span(text: str, lo: int, hi: int) -> tuple[int, int]:
    start = len(text[:lo].encode("utf-8"))
    return start, start + len(text[lo:hi].encode("utf-8"))


def retrieve(vault: Path, prompt: str, query: list[str], rows, allowed: set[str],
             indexed: dict[str, str], reader, options: Options) -> dict:
    """One synaptic packet. `rows` are FTS (source_path, bm25) rows in rank order."""
    vault = Path(vault).resolve()
    query = list(dict.fromkeys(fold(term) for term in query))
    allowed = set(allowed)
    options = replace(options, compact=True)
    try:
        graph = graphs.open_graph(vault)
    except graphs.GraphUnreadable as exc:
        graph, problem = None, str(exc)
    else:
        problem = None
    if graph is not None:
        try:
            return _retrieve(vault, prompt, query, rows, allowed,
                             Sources(reader, indexed, allowed), graph, options)
        except graphs.GraphUnreadable as exc:
            problem = str(exc)
        finally:
            graph.close()
    packet = _retrieve(vault, prompt, query, rows, allowed, Sources(reader, indexed, allowed),
                       None, options)
    return _graph_unreadable(packet, problem) if problem else packet


def _graph_unreadable(packet: dict, problem: str) -> dict:
    """Label a packet built without the link graph because graph.sqlite exists but
    could not be read: decision `graph_unreadable`, and the problem as the first
    note (one line naming `context-layer index`, never raw SQLite text)."""
    meta = packet["synapse"]
    meta["decision"] = "graph_unreadable"
    meta["graph"].update({"present": True, "readable": False})
    meta["notes"] = [problem] + [note for note in meta.get("notes", [])
                                 if not note.startswith("no graph.sqlite")]
    return packet


def _retrieve(vault, prompt, query, rows, allowed, sources, graph, options) -> dict:
    strengths = seed_scores(rows, allowed, options.top_k)
    top = max(strengths.values(), default=0.0)
    seeds = {name: max(0.05, min(1.0, value / top if top > 0 else 1.0))
             for name, value in strengths.items()}
    named = [n for n in named_notes(prompt, allowed, graph) if n not in seeds] if query else []
    for name in named:
        seeds[name] = 1.0

    link_rel = link_relevance(sources, query)

    def matches_for(activation, used) -> list[Passage]:
        anchors, targets = _anchor_maps(used)
        out = []
        for name in sorted(activation, key=lambda n: (-activation[n], n)):
            loaded = sources.get(name)
            if loaded is None:
                continue
            text, sha = loaded
            out.extend(match_candidates(name, text, sha, query, activation[name],
                                        targets.get(name, []), anchors.get(name, [])))
        return out

    seed_candidates = matches_for(seeds, [])
    # A seed with no matching body block (named in the prompt, or matched only in its
    # frontmatter) is represented by its lead, or its whole body when that is short.
    named_passages = []
    with_match = {c.path for c in seed_candidates}
    for name in sorted(seeds, key=lambda n: (-seeds[n], n)):
        loaded = sources.get(name)
        if name in with_match or loaded is None:
            continue
        for passage in reserved_passages(name, loaded[0], loaded[1], seeds[name], query, [], [],
                                         set(query), RESERVE_TOKENS):
            passage.reason = ("note named in the prompt" if name in named
                              else "note matched outside its body (frontmatter)")
            named_passages.append(passage)
    lexical = Packer(options, query)
    for passage in named_passages:
        lexical.offer(passage)
    lexical.fill(seed_candidates)
    lexical.merge_adjacent()
    covered = lexical.covered
    full = bool(query) and set(query) <= covered
    ranked = sorted(strengths.values(), reverse=True)
    dominant = len(ranked) == 1 or (len(ranked) > 1 and ranked[1] > 0
                                    and ranked[0] >= DOMINANCE * ranked[1])
    graph_info: dict = {"present": graph is not None, "readable": graph is not None}
    stops = dict.fromkeys(STOP_REASONS, 0)
    decision, expanded, packer = "no_seeds", False, lexical
    state = Spread(dict(seeds), {n: 0 for n in seeds}, {n: [] for n in seeds})
    if graph is not None:
        graph_info.update({"built_at": graph.meta.get("built_at"),
                           "edges": int(graph.meta.get("edges", "0")),
                           "unresolved_links": graph.unresolved_count()})
    if not seeds:
        decision = "no_seeds"
    elif graph is None:
        decision = "graph_missing"
    else:
        relational = _seed_links_match(graph, seeds, query, link_rel)
        if relational:
            decision = "seed_link_matches_query"
        elif full and dominant:
            decision = "seeds_cover_query"
        elif not full:
            decision = "coverage_incomplete"
        elif seeds_link_each_other(graph, list(seeds)):
            decision = "seeds_linked"
        else:
            decision = "seeds_cover_query"
        if decision != "seeds_cover_query":
            state = spread(graph, seeds, allowed, options, link_rel)
            stops["scope_excluded"] = state.scope_excluded
            stops["hop_cap"] = state.hop_cap
            stops["below_threshold"], stops["node_cap"] = state.below_threshold, state.left_out
            if state.capped and state.capped_at <= 1:
                decision = "node_cap_hit_lexical_fallback"
                state = Spread(dict(seeds), {n: 0 for n in seeds}, {n: [] for n in seeds},
                               stale=state.stale)
            else:
                expanded = len(state.activation) > len(seeds) or bool(state.used)
                packer = _pack_expanded(state, seeds, query, sources, graph, options,
                                        matches_for, seed_candidates, named_passages)
    stops["budget_limited"] = packer.budget_limited
    if graph is not None:
        for key, value in _unresolved_stops(graph, state, sources).items():
            stops[key] += value
    stops["target_unavailable"] += len(set(sources.withheld))
    return _packet(vault, prompt, query, options, seeds, state, packer, sources, graph_info,
                   stops, decision, expanded, graph is not None)


def link_relevance(sources: Sources, query: list[str]):
    """A function (path, line) -> how much the words around that link match the query, link
    text excluded: the line itself, or at PARAGRAPH_WEIGHT its paragraph and enclosing
    section headings."""
    blocks_by_path: dict[str, list[Block]] = {}
    cache: dict[tuple[str, int], float] = {}

    def link_rel(path: str, line: int) -> float:
        if not query:
            return 0.0
        if (path, line) in cache:
            return cache[(path, line)]
        found = tokens(LINK_SPAN.sub(" ", sources.line(path, line)))
        value = sum(1 for term in query if term in found) / len(query)
        loaded = sources.get(path)
        if loaded is not None:
            if path not in blocks_by_path:
                blocks_by_path[path] = split_blocks(loaded[0])
            for block in blocks_by_path[path]:
                if block.start <= line <= block.end:
                    rel, _ = relevance(LINK_SPAN.sub(" ", loaded[0][block.lo:block.hi]),
                                       block.headings, query)
                    value = max(value, PARAGRAPH_WEIGHT * rel)
                    break
        cache[(path, line)] = value
        return value
    return link_rel


def _anchor_maps(used: list[dict]) -> tuple[dict, dict]:
    anchors: dict[str, list[dict]] = {}
    targets: dict[str, list[dict]] = {}
    for view in used:
        anchor = view["anchor"]
        anchors.setdefault(anchor["path"], []).append(
            {"to": view["to"] if anchor["path"] == view["from"] else view["from"],
             "kind": view["kind"], "line": anchor["line"], "activation": view["weight"]})
        if view["kind"] != "backlink":
            targets.setdefault(view["to"], []).append(
                {"heading": view.get("_heading"), "block": view.get("_block")})
    return anchors, targets


def _seed_links_match(graph, seeds, query, link_rel) -> bool:
    """Does a seed line that holds a link also hold a query term (outside the link text)?"""
    for seed in sorted(seeds):
        if not graph.fresh(seed):
            continue
        for edge in graph.outgoing(seed):
            if link_rel(seed, edge.line) > 0:
                return True
    return False


def _pack_expanded(state: Spread, seeds, query, sources, graph, options, matches_for,
                   seed_candidates, named_passages=()) -> Packer:
    """Best lexical passage first; then a guaranteed share for the linked notes the query's
    own link lines point at; then everything else by utility per token."""
    packer = Packer(options, query)
    for passage in named_passages:
        packer.offer(passage)
    candidates = matches_for(state.activation, state.used)
    anchors, targets = _anchor_maps(state.used)
    if seed_candidates:
        first = sorted(seed_candidates, key=lambda c: (-c.score / max(c.tokens, 1), -c.score,
                                                       c.path, c.block.lo))[0]
        top = next((c for c in candidates if c.key() == first.key()), first)
        packer.offer(top)
    hop_notes = [n for n in state.activation if n not in seeds and n in state.via
                 and state.via[n]]
    best_rel = max((state.via[n][-1].get("_link_rel", 0.0) for n in hop_notes), default=0.0)
    linked = [n for n in hop_notes if best_rel > 0
              and state.via[n][-1].get("_link_rel", 0.0) >= RESERVE_REL_RATIO * best_rel
              and graph.degree(n) <= options.adjacency_cap]
    order = sorted(linked, key=lambda n: (-state.via[n][-1].get("_link_rel", 0.0),
                                          -state.activation[n], n))[:RESERVE_NOTES]
    if order:
        cap = int(options.budget_tokens * RESERVE_SHARE)
        cap = max(cap, packer.tokens)
        per_note = max(1, min(RESERVE_TOKENS, (cap - packer.tokens) // len(order)))
        for name in order:
            step = state.via[name][-1]
            context = tokens(LINK_SPAN.sub(" ", sources.line(step["anchor"]["path"],
                                                             step["anchor"]["line"])))
            context |= set(query)
            # The linking line itself (the "synapse"), so the host can follow the chain.
            synapse_source = sources.get(step["anchor"]["path"])
            if synapse_source is not None and step["anchor"]["path"] != name:
                block = line_block(synapse_source[0], step["anchor"]["line"])
                if block is not None:
                    line = Passage(step["anchor"]["path"], synapse_source[1], synapse_source[0],
                                   block, reason="the link line that connects a linked note")
                    line.rel, line.matched = relevance(line.content, [], query)
                    line.score = state.activation.get(step["anchor"]["path"], 0.0) * (
                        0.5 + line.rel)
                    _add_anchor(line, {"to": name, "kind": step["kind"],
                                       "line": step["anchor"]["line"]})
                    packer.offer(line, cap)
            loaded = sources.get(name)
            if loaded is None:
                continue
            for passage in reserved_passages(name, loaded[0], loaded[1], state.activation[name],
                                             query, targets.get(name, []),
                                             anchors.get(name, []), context, per_note):
                packer.offer(passage, cap)
    packer.fill(candidates)
    packer.merge_adjacent()
    return packer


STOP_FOR_REASON = {"ambiguous": "ambiguous_target", "excluded": "scope_excluded",
                   "attachment": "attachment_target"}     # missing, not_indexed: unavailable


def _unresolved_stops(graph, state: Spread, sources) -> dict:
    """Links out of the activated notes that make no edge, counted by why: an
    ambiguous target, a path under an exclusion, a file that is not a note, or a
    target that is missing or was not indexed."""
    counts = dict.fromkeys(("ambiguous_target", "scope_excluded", "attachment_target",
                            "target_unavailable"), 0)
    for name in sorted(state.activation):
        if name in state.stale or sources.cache.get(name, "") is None:
            continue
        for reason in graph.link_reasons(name):
            counts[STOP_FOR_REASON.get(reason, "target_unavailable")] += 1
    return counts


def _packet(vault, prompt, query, options, seeds, state: Spread, packer: Packer, sources,
            graph_info, stops, decision, expanded, graph_present) -> dict:
    chosen = sorted(packer.chosen, key=lambda p: (state.hop.get(p.path, 0), -p.score, p.path,
                                                  p.block.lo))
    evidence = [_item(p, state) for p in chosen]
    total = sum(item["est_tokens"] for item in evidence)
    status = "PARTIAL" if evidence else "NOT_FOUND"
    withheld = set(sources.withheld)
    graph_info["stale_sources"] = sorted(state.stale)
    graph_info["withheld_passages_from"] = sorted(withheld)
    notes = []
    if state.stale:
        notes.append("links from sources changed since the graph was built were not used; "
                     "run `context-layer index <vault>` to rebuild")
    if withheld:
        notes.append("sources changed since the index was built were withheld")
    if not graph_present:
        notes.append("no graph.sqlite: run `context-layer index` to build the link graph")
    selected = {item["source_path"] for item in evidence}
    hidden = set(state.stale) | withheld
    trace_path, trace_error = _emit_trace(vault, prompt, options, state, selected, len(evidence),
                                          total, status, seeds, hidden, options.budget_tokens)
    synapse = {"method": "synaptic", "mode": "compact", "experimental": True,
               "budget_tokens": options.budget_tokens,
               "est_tokens": total, **ESTIMATOR, "expanded": expanded, "decision": decision,
               "max_hops": options.max_hops, "seeds": sorted(seeds, key=lambda n: (-seeds[n], n)),
               "activated_notes": len(state.activation),
               "hubs_not_expanded": sorted(set(state.hubs)), "stops": stops,
               "graph": graph_info, "trace": trace_path}
    if trace_error:
        synapse["trace_error"] = trace_error
    if notes:
        synapse["notes"] = notes
    return {"schema": "evidence-delivery-v1", "operation_status": "ok", "status": status,
            "evidence": evidence, "synapse": synapse}


def _item(p: Passage, state: Spread) -> dict:
    start, end = _byte_span(p.text, p.block.lo, p.block.hi)
    content = p.content
    item = {"source_path": p.path, "source_sha256": p.sha, "content": content,
            "start": start, "end": end, "line_start": p.block.start,
            "line_end": p.block.end, "source_chars": len(p.text),
            "truncated": content != p.text, "hop": state.hop.get(p.path, 0),
            "activation": round(state.activation.get(p.path, 0.0), 4),
            "via": [_label(step) for step in state.via.get(p.path, [])],
            "reason": p.reason, "est_tokens": p.tokens, "score": round(p.score, 4)}
    if p.anchors:
        item["anchors"] = p.anchors
    if p.duplicates:
        item["duplicates"] = p.duplicates
    return item


def _emit_trace(vault, prompt, options, state, selected, passages, total, status, seeds,
                hidden, budget) -> tuple[str | None, str | None]:
    record, write = _trace_settings(vault, options)
    if not write:
        return None, None
    trace = trace_payload(prompt, options, state, selected, passages, total, status, seeds,
                          record, hidden)
    trace["budget_tokens"] = budget
    try:
        write_trace(vault, trace)
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return f".context/{TRACE_NAME}", None


# ---------------------------------------------------------------------------
# Default mode: the fts packet, unchanged, plus graph extras
# ---------------------------------------------------------------------------

def extend(vault: Path, prompt: str, query: list[str], rows, base: list[dict],
           allowed: set[str], indexed: dict[str, str], reader, options: Options) -> dict:
    """The fts packet `base` exactly as `--method fts` built it (same items, same order, same
    bounds), followed by passages the link graph adds, in their own budget of
    `options.extra_tokens` estimated tokens: linked notes when a link near the question's
    words gives a reason, notes the prompt names, and windows past the fts prefix that
    hold query terms the packet does not cover yet. Without a graph (missing, or
    unreadable: `graph_unreadable`) the packet's evidence is the fts evidence."""
    vault = Path(vault).resolve()
    query = list(dict.fromkeys(fold(term) for term in query))
    allowed = set(allowed)
    options = replace(options, compact=False)
    try:
        graph = graphs.open_graph(vault)
    except graphs.GraphUnreadable as exc:
        graph, problem = None, str(exc)
    else:
        problem = None
    if graph is not None:
        try:
            return _extend(vault, prompt, query, rows, base, allowed,
                           Sources(reader, indexed, allowed), graph, options)
        except graphs.GraphUnreadable as exc:
            problem = str(exc)
        finally:
            graph.close()
    if problem is None:
        return _extend(vault, prompt, query, rows, base, allowed,
                       Sources(reader, indexed, allowed), None, options)
    # graph.sqlite exists but cannot be read: exactly the fts packet (no terms, so no
    # named-note extras either), labelled, so synaptic never delivers less than fts.
    return _graph_unreadable(_extend(vault, prompt, [], rows, base, allowed,
                                     Sources(reader, indexed, allowed), None, options), problem)


def _locate(text: str, content: str, start) -> int | None:
    """The character offset of an fts item's content in its note: from the item's byte
    offset `start` when it is one and the bytes agree, else the first occurrence."""
    if isinstance(start, int) and not isinstance(start, bool) and start >= 0:
        try:
            lo = len(text.encode("utf-8")[:start].decode("utf-8"))
        except UnicodeDecodeError:
            lo = None
        if lo is not None and text.startswith(content, lo):
            return lo
    found = text.find(content)
    return found if found >= 0 else None


def _trim_after(p: Passage, lo: int, query: list[str]) -> Passage | None:
    """The part of a window that lies after character `lo` (where an fts prefix ends)."""
    text = p.text
    lo = max(lo, p.block.lo)
    while lo < p.block.hi and text[lo] in " \t\r\n":
        lo += 1
    if lo >= p.block.hi:
        return None
    block = Block(text.count("\n", 0, lo) + 1, p.block.end, lo, p.block.hi, p.block.headings)
    trimmed = Passage(p.path, p.sha, text, block, reason=p.reason, anchors=list(p.anchors))
    trimmed.rel, trimmed.matched = relevance(trimmed.content, block.headings, query)
    trimmed.score = p.score * (trimmed.rel / p.rel) if p.rel else p.score
    return trimmed


def _extend(vault, prompt, query, rows, base, allowed, sources, graph, options) -> dict:
    evidence = [dict(item) for item in base]
    base_paths = list(dict.fromkeys(item["source_path"] for item in evidence))
    strengths: dict[str, float] = {}
    for name, score in rows:
        if name in base_paths and name not in strengths:
            strengths[name] = -float(score)
    top = max(strengths.values(), default=0.0)
    seeds = {name: max(0.05, min(1.0, strengths[name] / top)) if top > 0 and name in strengths
             else 1.0 for name in base_paths}
    extra = Options(**{**options.__dict__, "budget_tokens": options.extra_tokens,
                       "budget_chars": options.extra_tokens * 4,
                       "per_source_chars": options.extra_tokens * 4})
    packer = Packer(extra, query)
    for item in evidence:                     # the fts part: annotated, never changed
        loaded = sources.get(item["source_path"])
        text = loaded[0] if loaded else None
        content = item["content"]
        lo = _locate(text, content, item.get("start")) if text is not None else None
        hi = (lo if lo is not None else 0) + len(content)
        packer.occupy(item["source_path"], lo or 0, hi, content, relevance(content, [], query)[1])
        item.update({"origin": "fts", "hop": 0, "reason": "fts"})
        if text is not None and lo is not None:
            start, end = _byte_span(text, lo, hi)
            for key, value in (("start", start), ("end", end),
                               ("line_start", text.count("\n", 0, lo) + 1),
                               ("line_end", text.count("\n", 0, max(hi - 1, lo)) + 1),
                               ("source_chars", len(text)), ("truncated", content != text)):
                item.setdefault(key, value)
        item["est_tokens"] = est_tokens(item["content"])
    named = [n for n in named_notes(prompt, allowed, graph) if n not in seeds] if query else []
    all_seeds = {**seeds, **{name: 1.0 for name in named}}
    stops = dict.fromkeys(STOP_REASONS, 0)
    state = Spread(dict(all_seeds), {n: 0 for n in all_seeds}, {n: [] for n in all_seeds})
    graph_info: dict = {"present": graph is not None, "readable": graph is not None}
    decision = "no_seeds" if not all_seeds else "graph_missing"
    order: list[str] = []
    if graph is not None:
        graph_info.update({"built_at": graph.meta.get("built_at"),
                           "edges": int(graph.meta.get("edges", "0")),
                           "unresolved_links": graph.unresolved_count()})
    if graph is not None and all_seeds:
        link_rel = link_relevance(sources, query)
        state = spread(graph, all_seeds, allowed, options, link_rel)
        stops["scope_excluded"], stops["hop_cap"] = state.scope_excluded, state.hop_cap
        stops["below_threshold"], stops["node_cap"] = state.below_threshold, state.left_out
        if state.capped and state.capped_at <= 1:
            decision = "node_cap_hit_lexical_fallback"
            state = Spread(dict(all_seeds), {n: 0 for n in all_seeds},
                           {n: [] for n in all_seeds}, stale=state.stale)
            named = []
        else:
            hop_notes = [n for n in state.activation if n not in all_seeds and state.via.get(n)]
            best = max((state.via[n][-1].get("_link_rel", 0.0) for n in hop_notes), default=0.0)
            order = sorted((n for n in hop_notes if best > 0
                            and state.via[n][-1].get("_link_rel", 0.0) >= RESERVE_REL_RATIO * best
                            and graph.degree(n) <= options.adjacency_cap),
                           key=lambda n: (-state.via[n][-1].get("_link_rel", 0.0),
                                          -state.activation[n], n))[:RESERVE_NOTES]
            decision = "relevant_links" if order else (
                "named_notes" if named else "no_relevant_link")
    elif named:
        decision = "named_notes"
    anchors, targets = _anchor_maps(state.used)
    if order or named:
        per_note = max(1, min(RESERVE_TOKENS, options.extra_tokens // max(1, len(order) + len(named))))
        for name in order:
            step = state.via[name][-1]
            source = sources.get(step["anchor"]["path"])
            if source is not None and step["anchor"]["path"] != name:
                block = line_block(source[0], step["anchor"]["line"])
                if block is not None:
                    line = Passage(step["anchor"]["path"], source[1], source[0], block,
                                   reason="the link line that connects a linked note")
                    line.rel, line.matched = relevance(line.content, [], query)
                    line.score = state.activation.get(step["anchor"]["path"], 0.0)
                    _add_anchor(line, {"to": name, "kind": step["kind"],
                                       "line": step["anchor"]["line"]})
                    packer.offer(line)
            loaded = sources.get(name)
            if loaded is None:
                continue
            context = tokens(LINK_SPAN.sub(" ", sources.line(step["anchor"]["path"],
                                                             step["anchor"]["line"])))
            for passage in reserved_passages(name, loaded[0], loaded[1], state.activation[name],
                                             query, targets.get(name, []),
                                             anchors.get(name, []), context | set(query),
                                             per_note):
                packer.offer(passage)
        for name in named:
            loaded = sources.get(name)
            if loaded is None:
                continue
            for passage in reserved_passages(name, loaded[0], loaded[1], 1.0, query, [], [],
                                             set(query), per_note):
                passage.reason = "note named in the prompt"
                packer.offer(passage)
    if order or named or (graph is not None and all_seeds):
        # Complement: windows in activated notes (seeds included, past the fts prefix) that
        # hold query terms the packet does not cover yet. It runs whenever there are seeds
        # and a graph, so a vault without links still gets the passage past the prefix.
        complements = []
        for name in sorted(state.activation, key=lambda n: (-state.activation[n], n)):
            loaded = sources.get(name)
            if loaded is None:
                continue
            spans = sorted(packer.occupied.get(name, []))
            for c in match_candidates(name, loaded[0], loaded[1], query,
                                      state.activation[name], targets.get(name, []),
                                      anchors.get(name, [])):
                for lo, hi in spans:          # never overlap an fts span: keep the tail
                    if c is not None and c.block.lo < hi and c.block.hi > lo:
                        c = _trim_after(c, hi, query) if c.block.hi > hi else None
                if c is not None and c.matched - packer.covered:
                    complements.append(c)
        packer.fill(complements)
        packer.merge_adjacent()
    stops["budget_limited"] = packer.budget_limited
    if graph is not None:
        for key, value in _unresolved_stops(graph, state, sources).items():
            stops[key] += value
    stops["target_unavailable"] += len(set(sources.withheld))
    extras = [_item(p, state) for p in sorted(
        packer.chosen, key=lambda p: (state.hop.get(p.path, 0), -p.score, p.path, p.block.lo))]
    for item in extras:
        item["origin"] = "graph"
    fts_tokens = sum(item["est_tokens"] for item in evidence)
    extra_tokens = sum(item["est_tokens"] for item in extras)
    evidence += extras
    status = "PARTIAL" if evidence else "NOT_FOUND"
    withheld = set(sources.withheld)
    graph_info["stale_sources"] = sorted(state.stale)
    graph_info["withheld_passages_from"] = sorted(withheld)
    notes = []
    if state.stale:
        notes.append("links from sources changed since the graph was built were not used; "
                     "run `context-layer index <vault>` to rebuild")
    if withheld:
        notes.append("sources changed since the index was built were withheld")
    if graph is None:
        notes.append("no graph.sqlite: run `context-layer index` to build the link graph")
    selected = {item["source_path"] for item in evidence}
    trace_path, trace_error = _emit_trace(vault, prompt, options, state, selected,
                                          len(evidence), fts_tokens + extra_tokens, status,
                                          all_seeds, set(state.stale) | withheld,
                                          options.extra_tokens)
    synapse = {"method": "synaptic", "mode": "superset", "experimental": True,
               "extra_tokens": options.extra_tokens, "fts_passages": len(base),
               "fts_est_tokens": fts_tokens, "extra_est_tokens": extra_tokens,
               "est_tokens": fts_tokens + extra_tokens, **ESTIMATOR,
               "expanded": bool(extras), "decision": decision, "max_hops": options.max_hops,
               "seeds": base_paths + named, "named_seeds": named,
               "activated_notes": len(state.activation),
               "hubs_not_expanded": sorted(set(state.hubs)), "stops": stops,
               "graph": graph_info, "trace": trace_path}
    if trace_error:
        synapse["trace_error"] = trace_error
    if notes:
        synapse["notes"] = notes
    if options.jev_candidates > 0:     # optional advisor's side channel; evidence is final here
        synapse["jev_candidates"] = jev_candidates(vault, state, all_seeds, evidence, sources,
                                                   graph, options, query, allowed, trace_path)
    return {"schema": "evidence-delivery-v1", "operation_status": "ok", "status": status,
            "evidence": evidence, "synapse": synapse}


# ---------------------------------------------------------------------------
# Jev candidates: a side channel for the optional advisor (context_layer/jev.py)
# ---------------------------------------------------------------------------
# Additive block. `_extend` calls it only when Options.jev_candidates > 0
# (eval/retrieve.py --jev-candidates N), after the evidence, the stops and the
# trace are final, so it cannot change them. Deterministic and model-free.

JEV_CANDIDATES_SCHEMA = "jev-candidates-v1"
RUN_ID = re.compile(r"[0-9a-f]{32}")


def jev_candidates(vault, state: Spread, seeds: dict, evidence: list[dict], sources: Sources,
                   graph, options: Options, query: list[str], allowed: set[str],
                   trace_path: str | None = None) -> dict:
    """Linked notes the activation reached that received no passage in the packet: the hop
    notes the lexical link rule left out (link relevance below RESERVE_REL_RATIO x best, or
    no best at all), or whose reserved share did not fit. Hubs, stale notes and notes whose
    bytes no longer match the index are left out, as retrieval leaves them out.

    Each candidate carries the passages it would be given if it were reserved (the linking
    line in the note that links to it, and `reserved_passages(..., RESERVE_TOKENS)`), in
    the packet's item format, plus its hop, activation and `via` chain. Nothing here is
    evidence: the caller decides, and the candidates never enter `evidence`."""
    delivered = {item["source_path"] for item in evidence}
    items: list[dict] = []
    limit = max(0, int(options.jev_candidates))
    if graph is not None and limit:
        anchors, targets = _anchor_maps(state.used)
        hop_notes = sorted((n for n in state.activation
                            if n not in seeds and state.via.get(n) and n not in delivered),
                           key=lambda n: (-state.activation[n], state.hop.get(n, 0), n))
        for name in hop_notes:
            if len(items) >= limit:
                break
            if name not in allowed or name in state.stale or not graph.fresh(name) \
                    or graph.degree(name) > options.adjacency_cap:
                continue
            loaded = sources.get(name)
            if loaded is None:
                continue
            step = state.via[name][-1]
            anchor = step["anchor"]
            link = None
            linking = sources.get(anchor["path"]) if anchor["path"] != name else None
            if linking is not None:
                block = line_block(linking[0], anchor["line"])
                if block is not None:
                    line = Passage(anchor["path"], linking[1], linking[0], block,
                                   reason="the link line that connects a linked note")
                    line.rel, line.matched = relevance(line.content, [], query)
                    line.score = state.activation.get(anchor["path"], 0.0)
                    _add_anchor(line, {"to": name, "kind": step["kind"], "line": anchor["line"]})
                    link = _item(line, state)
            context = tokens(LINK_SPAN.sub(" ", sources.line(anchor["path"], anchor["line"])))
            own = [_item(p, state) for p in reserved_passages(
                name, loaded[0], loaded[1], state.activation[name], query,
                targets.get(name, []), anchors.get(name, []), context | set(query),
                RESERVE_TOKENS)]
            if not own:
                continue
            items.append({"source_path": name, "source_sha256": loaded[1], "kind": "link",
                          "hop": state.hop.get(name, 0),
                          "activation": round(state.activation[name], 4),
                          "via": [_label(s) for s in state.via[name]],
                          "link_line": link, "passages": own})
    return {"schema": JEV_CANDIDATES_SCHEMA, "limit": limit, "items": items,
            "trace_run_id": _trace_run_id(vault, trace_path)}


def _trace_run_id(vault, trace_path: str | None) -> str | None:
    """The run id of the trace this retrieval just wrote, so the advisor can label that
    run and no other. Read only for that pairing; never used for ranking."""
    if not trace_path:
        return None
    try:
        data = json.loads((Path(vault) / trace_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    run = data.get("run_id") if isinstance(data, dict) else None
    return run if isinstance(run, str) and RUN_ID.fullmatch(run) else None


# ---------------------------------------------------------------------------
# Activation trace
# ---------------------------------------------------------------------------

def _trace_settings(vault: Path, options: Options) -> tuple[bool, bool]:
    """(record the query text?, write the trace at all?) from options and routes.json."""
    record, write = options.record_query, True
    from .mcp_server import policy
    try:
        data = policy().load_config(vault / ".context" / "routes.json")
    except ValueError:
        data = None  # preflight already refused an unusable config; stay private here
    if isinstance(data, dict):
        record = record or data.get("record_query_text") is True
        write = data.get("write_activation") is not False
    return record, write


def trace_payload(prompt: str, options: Options, state: Spread, selected: set[str],
                  passages: int, total: int, status: str, seeds: dict, record: bool,
                  hidden: set[str] = frozenset()) -> dict:
    """The contract payload. Notes that are stale or withheld are never listed.

    `mode` is `superset` (default mode) or `compact`; each edge carries the `hop` it
    was traversed at; paths are NFC-normalised (Obsidian lists NFC names), while the
    packet keeps on-disk names for reading and re-hashing.
    """
    activation, hop = state.activation, state.hop
    visible = [n for n in activation if n not in hidden]
    nodes = sorted(visible, key=lambda n: (-activation[n], hop[n], n))[:TRACE_NODES]
    kept = set(nodes)
    edges = [e for e in sorted(state.used, key=lambda e: (-e["weight"], e["from"], e["to"],
                                                          e["kind"], e["anchor"]["line"]))
             if e["from"] in kept and e["to"] in kept and e["anchor"]["path"] in kept]
    edges = edges[:TRACE_EDGES]
    return {
        "version": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": secrets.token_hex(16),
        "query": prompt if record else None,
        "method": "synaptic",
        "mode": "compact" if options.compact else "superset",
        "max_hops": options.max_hops,
        "budget_tokens": options.budget_tokens,
        "nodes": [{"path": _nfc(n), "activation": round(min(1.0, max(0.0, activation[n])), 4),
                   "hop": hop[n], "role": "seed" if n in seeds else "hop",
                   "selected": n in selected} for n in nodes],
        "edges": [{"from": _nfc(e["from"]), "to": _nfc(e["to"]), "kind": e["kind"],
                   "hop": e.get("hop", 1),
                   "weight": round(min(1.0, max(0.0, e["weight"])), 4),
                   "anchor": {"path": _nfc(e["anchor"]["path"]), "line": e["anchor"]["line"]}}
                  for e in edges],
        "packet": {"passages": passages, "est_tokens": total, "status": status},
    }


def _nfc(path: str) -> str:
    return unicodedata.normalize("NFC", path)


def write_trace(vault: Path, payload: dict) -> Path:
    """A uniquely named temp file in the same directory + os.replace: concurrent writers
    never share a staging name, and a reader never sees half a file."""
    target = Path(vault) / ".context" / TRACE_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=".activation-", suffix=".json", dir=target.parent)
    staging = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=1)
            stream.write("\n")
        os.replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)
    return target


# ---------------------------------------------------------------------------
# graph_neighbors
# ---------------------------------------------------------------------------

NEIGHBOR_DEFAULT = 25
NEIGHBOR_CAP = 100


def neighbors(vault: Path, path: str, prefixes: list[str], policy,
              limit: int = NEIGHBOR_DEFAULT) -> dict:
    """Linked notes of one note, both directions, with kinds and line anchors; bounded."""
    vault = Path(vault).resolve()
    name = policy.relative_name(path)
    policy.source_path(vault, name, prefixes)          # boundaries before anything else
    limit = max(1, min(limit, NEIGHBOR_CAP))
    graph = graphs.open_graph(vault)
    if graph is None:
        raise ValueError("No link graph: run `context-layer index` to build .context/graph.sqlite")
    try:
        row = graph.note(name)
        if row is None:
            raise ValueError(f"Note not in the link graph: {name}")

        def visible(other: str) -> bool:
            try:
                return not policy.excluded(other, prefixes)
            except ValueError:
                return False

        fresh = graph.fresh(name)
        outgoing = [] if not fresh else [
            {"path": e.target, "label": "links to", "kind": e.kind, "line": e.line,
             "heading": e.heading, "block": e.block, **({"field": e.field} if e.field else {})}
            for e in graph.outgoing(name) if visible(e.target)]
        incoming = []
        stale_sources = set()
        for e in graph.incoming(name):
            if not visible(e.source):
                continue
            if not graph.fresh(e.source):
                stale_sources.add(e.source)
                continue
            incoming.append({"path": e.source, "label": "linked from", "kind": "backlink",
                             "link_kind": e.kind, **({"field": e.field} if e.field else {}),
                             "anchor": {"path": e.source, "line": e.line}})
        total_out, total_in = len(outgoing), len(incoming)
        return {"schema": "graph-neighbors-v1", "path": name, "graph_sha256": row[0],
                "fresh": fresh, "degree": row[3],
                "outgoing": outgoing[:limit], "incoming": incoming[:limit],
                "outgoing_total": total_out, "incoming_total": total_in,
                "truncated": total_out > limit or total_in > limit,
                "unresolved_links": graph.unresolved_count(name),
                "stale_sources": sorted(stale_sources),
                "note": ("links out of this note are withheld: it changed since the graph was "
                         "built; run `context-layer index`") if not fresh else None}
    finally:
        graph.close()
