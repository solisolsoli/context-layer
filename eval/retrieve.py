#!/usr/bin/env python3
"""Comparable retrieval adapters with one evidence format and content budget.

All methods use the same frozen router index, source rules and UTF-8 input.
No method sees evaluation labels. grep/fts/fts-canonical are baselines;
router is the experimental implementation, not a presumed improvement.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'router'))
from source_policy import (SymlinkSource, config_exclusions, exclusion_matcher, load_config,
                           source_path)
import context_router
import index_format
import textfold

METHODS = ['grep', 'fts', 'fts-canonical', 'router', 'synaptic']


def synapse_module():
    """context_layer.synapse from an install, or from this checkout's root."""
    try:
        from context_layer import synapse
    except ImportError:
        sys.path.insert(0, str(ROOT))
        from context_layer import synapse
    return synapse

def record_usage(args, packet):
    """Opt-in usage ledger (`"record_usage": true` in routes.json; docs/synapse.md, section 11).

    Runs after the packet is printed and cannot change it; a failure is swallowed. Nothing
    reads the ledger back into retrieval."""
    if args.method not in ('fts', 'synaptic'):
        return
    try:
        synapse_module()
        from context_layer import coactivation
        coactivation.record(args.vault, packet, args.method)
    except Exception:
        pass


REINDEX_HINT = 'run `context-layer index <vault>`'


def withheld_entry(name, reason):
    """One source left out of a packet, with the reason and the command that fixes it."""
    return {'source_path': name, 'reason': reason, 'next': 'context-layer index <vault>'}


STOP = context_router.STOPWORDS | {'what', 'does', 'should', 'please', 'could', 'would'}

# The coverage receipt stays small whatever the prompt: at most this many terms and
# this many characters of the MATCH expression are listed.
COVERAGE_TERMS = 64
COVERAGE_EXPRESSION_CHARS = 4096

# `reason` on a NOT_FOUND packet: nothing to search for, nothing matched, or notes
# matched but none could be delivered (see `withheld` for the notes left out).
NO_TERMS = 'no searchable terms'
NO_MATCH = 'no indexed note matched'
NOT_DELIVERED = 'matching notes could not be delivered'


def terms(prompt):
    """Verbatim query terms (router/textfold.py). Stopwords and repeats are recognised by
    their folded form, but each term is sent to MATCH as written, so FTS5 folds it the
    way it folded the notes."""
    return textfold.terms(prompt, STOP)


def bounded(evidence, top_k, budget, per_source):
    """At most top_k items and `budget` characters, `per_source` per item, one item per
    path. Byte-identical content is delivered once: later paths with the same text are
    listed under that item's `duplicates` and take no slot."""
    result = []
    seen = set()
    by_content = {}
    for item in evidence:
        path = item['source_path']
        if path in seen or len(result) >= top_k or budget <= 0:
            continue
        content = item['content'][:min(per_source, budget)]
        if not content:
            continue
        seen.add(path)
        original = by_content.get(content)
        if original is not None:
            original.setdefault('duplicates', []).append(
                {'source_path': path, 'source_sha256': item['source_sha256']})
            continue
        entry = {'source_path': path, 'source_sha256': item['source_sha256'], 'content': content}
        by_content[content] = entry
        result.append(entry)
        budget -= len(content)
    return result


DELIVERIES = ('window', 'prefix')
WINDOWS_PER_SOURCE = 3


def _cut(text, lo, hi, limit):
    """(lo, hi) pieces of text[lo:hi], each at most `limit` characters: cut where a line
    ends when whole lines fit, else inside the line. Trailing line breaks are left out
    of a piece, as block spans leave them out."""
    pieces = []
    begin = lo
    while begin < hi:
        end = begin
        while end < hi:
            newline = text.find('\n', end, hi)
            following = hi if newline < 0 else newline + 1
            if following - begin > limit:
                break
            end = following
        if end == begin:                       # one line longer than the limit
            end = min(hi, begin + limit)
        stop = end
        while stop > begin and text[stop - 1] in '\r\n':
            stop -= 1
        if stop > begin:
            pieces.append((begin, stop))
        begin = end
    return pieces


def windows(text, query_terms, limit, synapse):
    """Match-anchored (lo, hi) spans of `text`, at most `limit` characters in all and at
    most WINDOWS_PER_SOURCE: the paragraph-sized blocks (context_layer.synapse.split_blocks)
    in which FTS5, with the index tokenizer, finds query terms; most distinct terms first,
    then by position. A block longer than the limit is cut at line ends and its pieces
    re-matched. Spans separated only by blank lines merge when the gap fits. None when
    no block holds a term (the match sits in the frontmatter, or nowhere a block shows)."""
    blocks = synapse.split_blocks(text)
    if not blocks or not query_terms:
        return None
    spans = [(block.lo, block.hi) for block in blocks]
    found = textfold.term_matches([text[lo:hi] for lo, hi in spans], query_terms)
    candidates = [(span, terms) for span, terms in zip(spans, found) if terms]
    if not candidates:
        return None
    long = [span for span, _ in candidates if span[1] - span[0] > limit]
    if long:
        pieces = [piece for lo, hi in long for piece in _cut(text, lo, hi, limit)]
        found = textfold.term_matches([text[lo:hi] for lo, hi in pieces], query_terms)
        candidates = [c for c in candidates if c[0] not in set(long)] + \
            [(piece, terms) for piece, terms in zip(pieces, found) if terms]
    candidates.sort(key=lambda c: (-len(c[1]), c[0][0]))
    chosen, used = [], 0
    for (lo, hi), _ in candidates:
        if len(chosen) >= WINDOWS_PER_SOURCE:
            break
        if used + (hi - lo) > limit:
            continue
        chosen.append((lo, hi))
        used += hi - lo
    if not chosen:
        return None
    chosen.sort()
    merged = [chosen[0]]
    for lo, hi in chosen[1:]:
        previous_lo, previous_hi = merged[-1]
        gap = lo - previous_hi
        if not text[previous_hi:lo].strip() and used + gap <= limit:
            merged[-1] = (previous_lo, hi)
            used += gap
        else:
            merged.append((lo, hi))
    return merged


def window_item(name, text, sha, lo, hi):
    """One evidence item for text[lo:hi]: verbatim content with its byte span, lines,
    the note's length and whether the item is shorter than the note."""
    content = text[lo:hi]
    start = len(text[:lo].encode('utf-8'))
    return {'source_path': name, 'source_sha256': sha, 'content': content,
            'start': start, 'end': start + len(content.encode('utf-8')),
            'line_start': text.count('\n', 0, lo) + 1,
            'line_end': text.count('\n', 0, max(hi - 1, lo)) + 1,
            'source_chars': len(text), 'truncated': content != text}


def deliver(name, text, sha, query_terms, limit, mode, synapse):
    """The items `--delivery MODE` gives for one note within `limit` characters: the whole
    note when it fits (both modes); otherwise `window` delivers the match-anchored
    windows and falls back to the prefix when no block holds a query term, and `prefix`
    delivers the note's first `limit` characters."""
    if limit <= 0 or not text:
        return []
    if len(text) <= limit:
        return [window_item(name, text, sha, 0, len(text))]
    if mode == 'window':
        spans = windows(text, query_terms, limit, synapse)
        if spans:
            return [window_item(name, text, sha, lo, hi) for lo, hi in spans]
    return [window_item(name, text, sha, 0, limit)]


# One row per matching source, best (lowest) bm25 first: the order in which the
# per-chunk rows first name each source. LIMIT -1 keeps SQLite from flattening the
# subquery, since bm25() may not run inside an aggregate.
RANKED_SQL = '''SELECT source_path, MIN(score) AS best FROM (
    SELECT r.source_path AS source_path, bm25(records_fts) AS score
    FROM records_fts JOIN records r ON r.id = records_fts.rowid
    WHERE records_fts MATCH ? LIMIT -1)
    GROUP BY source_path ORDER BY best, source_path'''


class AllowedNames:
    """Indexed names outside every exclusion, as `allowed` always meant here, but lazy:
    `name in allowed` checks one name on demand (string rules and one indexed lookup,
    no file access); iterating it, or set(allowed), lists them all. fts never iterates
    it, so a search checks only the names it ranks."""

    def __init__(self, connection, is_excluded):
        self._connection = connection
        self._is_excluded = is_excluded
        self._all = None

    def __contains__(self, name):
        if self._all is not None:
            return name in self._all
        return not self._is_excluded(name) and self._connection.execute(
            'SELECT 1 FROM records WHERE source_path=? LIMIT 1', (name,)).fetchone() is not None

    def __iter__(self):
        if self._all is None:
            self._all = {name for (name,) in self._connection.execute(
                'SELECT DISTINCT source_path FROM records') if not self._is_excluded(name)}
        return iter(sorted(self._all))


def open_index(index):
    """Open the index read-only; return (connection, handle on the same file).

    The handle lets the coverage receipt hash exactly the file this connection reads,
    even when `context-layer index` replaces it meanwhile.
    """
    if not index.is_file():
        raise ValueError(f'no index at .context/{index.name}; {REINDEX_HINT} first')
    for _ in range(3):
        try:
            handle = open(index, 'rb')
        except OSError as exc:
            raise ValueError(f'.context/{index.name} cannot be read ({exc.strerror}); '
                             f'{REINDEX_HINT}') from None
        connection = sqlite3.connect(index.as_uri() + '?mode=ro', uri=True)
        try:
            index_format.check_index(connection)
            if os.path.samestat(os.fstat(handle.fileno()), os.stat(index)):
                return connection, handle
        except BaseException:
            connection.close()
            handle.close()
            raise
        connection.close()
        handle.close()
    raise ValueError('the index was replaced three times while it was opened; try again')


def coverage(connection, handle, query_terms, expression):
    """What this query searched: never a path, bounded in size."""
    digest = hashlib.sha256()
    handle.seek(0)
    for block in iter(lambda: handle.read(1 << 20), b''):
        digest.update(block)
    try:
        meta = dict(connection.execute('SELECT key, value FROM index_meta'))
    except sqlite3.Error:
        meta = {}
    try:
        skipped = json.loads(meta['skipped_by_reason'])
    except (KeyError, TypeError, ValueError):
        skipped = None            # an index built before skips were recorded
    receipt = {
        'indexed_notes': connection.execute(
            'SELECT COUNT(DISTINCT source_path) FROM records').fetchone()[0],
        'index_sha256': digest.hexdigest(),
        'query_terms': query_terms[:COVERAGE_TERMS],
        'match_expression': expression,
        'skipped_by_reason': skipped if isinstance(skipped, dict) else None,
    }
    if len(query_terms) > COVERAGE_TERMS:
        receipt['query_terms_omitted'] = len(query_terms) - COVERAGE_TERMS
    if expression and len(expression) > COVERAGE_EXPRESSION_CHARS:
        receipt['match_expression'] = expression[:COVERAGE_EXPRESSION_CHARS]
        receipt['match_expression_truncated'] = True
    return receipt


def retrieve(args):
    vault = args.vault.resolve()
    config_path = vault / '.context/routes.json'
    config = load_config(config_path)
    if args.method in ('router', 'fts-canonical') and not isinstance(config.get('routes'), dict):
        raise ValueError('Configuration must contain a routes object')
    prefixes = list(config_exclusions(config))
    if args.method == 'router':
        command = [sys.executable, str(ROOT / 'router/context_router.py'), '--vault', str(vault),
                   '--prompt', args.prompt, '--evidence-json', '--no-fast-path', '--no-save',
                   '--max-sources', str(args.top_k), '--max-context-chars', str(args.budget),
                   '--max-per-source', str(args.per_source)]
        completed = subprocess.run(command, capture_output=True, timeout=60)
        packet = json.loads(completed.stdout.decode('utf-8'))
        if completed.returncode not in (0, 2) or packet.get('operation_status') != 'ok':
            raise ValueError(packet.get('error', 'router failed'))
        packet['evidence'] = bounded(packet['evidence'], args.top_k, args.budget, args.per_source)
        if not packet['evidence'] and packet['status'] in {'SUPPORTED', 'USER_STATED'}:
            packet['status'] = 'PARTIAL'
        return packet
    connection, handle = open_index(vault / '.context/index.sqlite')
    try:
        # Exclusions are string rules, checked only on the names a method looks at; the
        # symlink and vault-boundary checks touch the file system, so source_path() runs
        # only on the names actually read, always before the read.
        is_excluded = exclusion_matcher(prefixes)
        compact = args.method == 'synaptic' and args.compact
        delivery = args.delivery or 'window'
        loaded_synapse = []

        def synapse_mod():
            if not loaded_synapse:
                loaded_synapse.append(synapse_module())
            return loaded_synapse[0]

        query_terms = terms(args.prompt)
        expression = textfold.match_expression(query_terms)
        folded_terms = [textfold.fold(term) for term in query_terms]
        ranked = []
        rows = []
        allowed = AllowedNames(connection, is_excluded)
        if args.method == 'grep':
            for name in allowed:
                text = '\n'.join(r[0] for r in connection.execute(
                    'SELECT content FROM records WHERE source_path=? ORDER BY id', (name,)))
                text = textfold.fold(text)
                counts = [text.count(term) for term in folded_terms]
                if any(counts):
                    ranked.append((name, -sum(c > 0 for c in counts), -sum(counts)))
            ranked.sort(key=lambda r: (r[1], r[2], r[0]))
            ranked = [r[0] for r in ranked]
        elif expression:
            rows = connection.execute(RANKED_SQL, (expression,)).fetchall()
            ranked = [name for name, _ in rows]
            if args.name_fields:    # opt-in: `rows` (the graph seeds) stays the content ranking
                if not index_format.has_name_fields(connection):
                    raise ValueError('this index has no name fields; run '
                                     '`context-layer index <vault> --name-fields`')
                ranked = index_format.merge_name_hits(connection, expression, ranked)
            ranked = [name for name in ranked if not is_excluded(name)]
        withheld = []

        def in_content(items):
            """Whether each delivered text holds a query match, by the method's own rule."""
            if args.method == 'grep':
                return [any(term in textfold.fold(item['content']) for term in folded_terms)
                        for item in items]
            return textfold.matching([item['content'] for item in items], expression)

        def lexical_evidence(names):
            # A source that changed, vanished, became unreadable or became a symlink since
            # indexing is withheld on its own, with a visible reason; the rest of the
            # packet is still delivered. Its slot is not back-filled, so after
            # `context-layer index` the packet is the one a fresh index gives. Index-wide
            # failures (format, full-text table) stay ERROR: open_index above.
            # Delivery: at most --top-k notes, --per-source characters per note and
            # --budget in all; a note is delivered whole when it fits, else as
            # match-anchored windows (--delivery window) or its prefix (prefix). Grep, the
            # baseline, always delivers prefixes. Byte-identical deliveries are listed once,
            # later notes under the first item's `duplicates` (they take no slot).
            evidence = []
            by_content = {}
            budget = args.budget
            mode = 'prefix' if args.method == 'grep' else delivery
            for name in names[:args.top_k]:
                if budget <= 0:
                    break
                expected = {r[0] for r in connection.execute('SELECT DISTINCT source_sha256 FROM records WHERE source_path=?', (name,))}
                if len(expected) != 1:
                    raise ValueError(f'Index records disagree for {name}; {REINDEX_HINT}')
                try:
                    raw = source_path(vault, name, prefixes).read_bytes()
                except FileNotFoundError:
                    withheld.append(withheld_entry(name, 'deleted since indexing'))
                    continue
                except SymlinkSource:
                    withheld.append(withheld_entry(name, 'symlink since indexing'))
                    continue
                except OSError:
                    withheld.append(withheld_entry(name, 'unreadable since indexing'))
                    continue
                sha = hashlib.sha256(raw).hexdigest()
                if expected != {sha}:
                    withheld.append(withheld_entry(name, 'changed since indexing'))
                    continue
                items = deliver(name, raw.decode('utf-8'), sha, query_terms,
                                min(args.per_source, budget), mode, synapse_mod())
                if not items:
                    continue
                key = tuple(item['content'] for item in items)
                original = by_content.get(key)
                if original is not None:
                    original.setdefault('duplicates', []).append(
                        {'source_path': name, 'source_sha256': sha})
                    continue
                by_content[key] = items[0]
                budget -= sum(len(item['content']) for item in items)
                evidence.extend(items)
            for item, hit in zip(evidence, in_content(evidence)):
                item['match_in_content'] = hit
            return evidence

        def with_withheld(packet):
            if withheld:
                packet['withheld'] = withheld
            if args.jev_candidates:  # the optional advisor's side channel (see below)
                attach_jev_candidates(packet, args, ranked, connection, vault, prefixes)
            return packet

        def with_coverage(packet):
            if packet.get('status') == 'NOT_FOUND':
                packet['reason'] = (NO_TERMS if not query_terms
                                    else NOT_DELIVERED if ranked else NO_MATCH)
            packet['coverage'] = coverage(connection, handle, query_terms,
                                          None if args.method == 'grep' else expression)
            return packet

        if args.method == 'synaptic':
            # The graph layer lives in context_layer.synapse so the MCP server and the hook
            # share one copy. Default mode: the fts packet, built by the very function fts
            # uses below, plus graph extras in a separate budget. --compact: the older packer.
            synapse = synapse_module()
            indexed = dict(connection.execute(
                'SELECT source_path, MIN(source_sha256) FROM records GROUP BY source_path'))
            options = synapse.Options(top_k=args.top_k, budget_chars=args.budget,
                                      per_source_chars=args.per_source,
                                      budget_tokens=args.budget_tokens, max_hops=args.max_hops,
                                      record_query=args.record_query,
                                      extra_tokens=args.extra_tokens)
            options.jev_candidates = args.jev_candidates  # advisor side channel; 0 = off
            reader = lambda name: source_path(vault, name, prefixes).read_bytes()  # noqa: E731
            if compact:
                return with_coverage(synapse.retrieve(vault, args.prompt, query_terms, rows,
                                                      set(allowed), indexed, reader, options))
            return with_coverage(with_withheld(synapse.extend(
                vault, args.prompt, query_terms, rows, lexical_evidence(ranked), set(allowed),
                indexed, reader, options)))
        if args.method == 'fts-canonical':
            parsed = context_router.parse_prompt(args.prompt, config)
            pinned = []
            for route in parsed['routes']:
                for entry in config['routes'][route].get('canonical_sources', []):
                    if isinstance(entry, dict) and entry.get('attachment') == 'opportunistic':
                        continue
                    name = entry['path'] if isinstance(entry, dict) else entry
                    source_path(vault, name, prefixes)
                    if name not in allowed:
                        raise ValueError(f'Canonical source not indexed: {name}')
                    pinned.append(name)
            ranked = list(dict.fromkeys(pinned + ranked))
        evidence = lexical_evidence(ranked)
        return with_coverage(with_withheld({
            'schema': 'evidence-delivery-v1', 'operation_status': 'ok',
            'status': 'PARTIAL' if evidence else 'NOT_FOUND', 'evidence': evidence}))
    finally:
        connection.close()
        handle.close()


# Defaults of the synaptic-only flags. The parser leaves them unset (None) so that a
# flag given where it has no effect is refused, as `packet build` and `install` do.
SYNAPTIC_DEFAULTS = {'extra_tokens': 600, 'compact': False, 'budget_tokens': 1200,
                     'max_hops': 1, 'record_query': False}


def inapplicable_flag(args):
    """The first flag given where it changes nothing, as one usage line; None if none."""
    given = [flag for flag, name in (('--compact', 'compact'), ('--extra-tokens', 'extra_tokens'),
                                     ('--budget-tokens', 'budget_tokens'),
                                     ('--max-hops', 'max_hops'),
                                     ('--record-query', 'record_query'))
             if getattr(args, name) is not None]
    if args.method != 'synaptic' and given:
        return f'{given[0]} applies only to --method synaptic'
    if args.budget_tokens is not None and not args.compact:
        return ('--budget-tokens sizes only the --compact synaptic packet; the default '
                'synaptic packet takes --extra-tokens')
    if args.extra_tokens is not None and args.compact:
        return '--extra-tokens sizes the default synaptic packet, not the --compact one'
    if args.delivery is not None and (args.method in ('grep', 'router')
                                      or (args.method == 'synaptic' and args.compact)):
        return ('--delivery applies to --method fts, fts-canonical and the default '
                'synaptic packet (its fts part)')
    if args.jev_candidates and (args.method in ('grep', 'fts-canonical', 'router')
                                or (args.method == 'synaptic' and args.compact)):
        return ('--jev-candidates applies to --method fts and the default synaptic '
                'packet only')
    if args.name_fields and args.method in ('grep', 'router'):
        return '--name-fields applies to --method fts, fts-canonical and synaptic'
    return None


def read_prompt(parser, args):
    """The prompt: the positional argument, or the text of --prompt-file (- is stdin)."""
    if (args.prompt is None) == (args.prompt_file is None):
        parser.error('give the prompt either as the last argument or with --prompt-file')
    if args.prompt_file is None:
        return args.prompt
    try:
        raw = sys.stdin.buffer.read() if args.prompt_file == '-' else Path(args.prompt_file).read_bytes()
        return raw.decode('utf-8')
    except (OSError, UnicodeError) as exc:
        detail = getattr(exc, 'strerror', None) or 'not UTF-8 text'
        parser.exit(2, f'{parser.prog}: error: --prompt-file cannot be read ({detail})\n')


# ---------------------------------------------------------------------------
# Jev candidates: a side channel for the optional advisor (context_layer/jev.py)
# ---------------------------------------------------------------------------
# Additive block. With --jev-candidates N the packet gains a top-level
# `jev_candidates` object; `evidence` and every other key stay exactly as they
# are without the flag. Deterministic, model-free, no network.

JEV_CANDIDATES_CAP = 32


def attach_jev_candidates(packet, args, ranked, connection, vault, prefixes):
    """Up to N notes the packet did not deliver, for an advisor to judge: in the default
    synaptic mode first the link-reached notes the synapse layer left out (it puts them in
    `synapse.jev_candidates`; they move to the top level here), then, for fts and
    synaptic alike, the bm25 tail: notes ranked after --top-k, each with the prefix fts
    would deliver for it. Notes that changed since indexing are skipped, as fts skips them."""
    if args.method not in ('fts', 'synaptic') or getattr(args, 'compact', False):
        return packet
    side = packet.get('synapse', {}).pop('jev_candidates', None) \
        if isinstance(packet.get('synapse'), dict) else None
    if not isinstance(side, dict):
        side = {'schema': 'jev-candidates-v1', 'limit': args.jev_candidates, 'items': [],
                'trace_run_id': None}
    taken = {item['source_path'] for item in packet.get('evidence', [])}
    taken |= {entry['source_path'] for entry in packet.get('withheld', [])}
    taken |= {item['source_path'] for item in side['items']}
    for rank, name in enumerate(ranked, start=1):
        if len(side['items']) >= args.jev_candidates:
            break
        if rank <= args.top_k or name in taken:
            continue
        passages = fts_tail_items(connection, vault, prefixes, name,
                                  min(args.per_source, args.budget), args)
        if passages:
            side['items'].append({'source_path': name, 'source_sha256': passages[0]['source_sha256'],
                                  'kind': 'bm25_tail', 'rank': rank, 'link_line': None,
                                  'passages': passages})
    packet['jev_candidates'] = side
    return packet


def fts_tail_items(connection, vault, prefixes, name, size, args):
    """What fts would deliver for `name` if it ranked inside --top-k (the same --delivery
    rule, within `size` characters), each item with its byte and line span; [] when the
    note changed, vanished or disagrees with the index."""
    expected = {r[0] for r in connection.execute(
        'SELECT DISTINCT source_sha256 FROM records WHERE source_path=?', (name,))}
    try:
        raw = source_path(vault, name, prefixes).read_bytes()
        text = raw.decode('utf-8')
    except (OSError, ValueError):
        return []
    sha = hashlib.sha256(raw).hexdigest()
    if expected != {sha}:
        return []
    mode = args.delivery or 'window'
    items = deliver(name, text, sha, terms(args.prompt), size, mode, synapse_module())
    for item in items:
        item['reason'] = f'fts {mode} of a note ranked after --top-k'
        item['est_tokens'] = -(-len(item['content']) // 4)
    return items


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--method', required=True, choices=METHODS)
    parser.add_argument('--vault', required=True, type=Path)
    parser.add_argument('--top-k', type=int, default=3)
    parser.add_argument('--budget', type=int, default=6000, help='Total evidence content characters, excluding JSON framing.')
    parser.add_argument('--per-source', type=int, default=2000)
    parser.add_argument('--extra-tokens', type=int, default=None,
                        help='synaptic only, not with --compact: estimated-token budget for graph '
                             'extras added after the unchanged fts packet (ceil(chars/4)); '
                             'default 600.')
    parser.add_argument('--compact', action='store_true', default=None,
                        help='synaptic only: the compact packer (passages instead of the fts '
                             'packet, one --budget-tokens budget); fewer tokens, may drop fts '
                             'evidence.')
    parser.add_argument('--budget-tokens', type=int, default=None,
                        help='synaptic --compact only: packet budget in estimated tokens, '
                             'ceil(chars/4); default 1200.')
    parser.add_argument('--max-hops', type=int, choices=[1, 2], default=None,
                        help='synaptic only: link hops to spread activation (default 1, max 2).')
    parser.add_argument('--record-query', action='store_true', default=None,
                        help='synaptic only: store the query text in .context/activation.json '
                             '(default: not stored).')
    parser.add_argument('--delivery', choices=DELIVERIES, default=None,
                        help='fts, fts-canonical and the fts part of the default synaptic '
                             'packet: `window` (default) delivers a note whole when it fits '
                             'the per-source limit, else match-anchored verbatim windows; '
                             '`prefix` delivers the first per-source characters (the 0.3 '
                             'behaviour).')
    parser.add_argument('--name-fields', action='store_true',
                        help='fts, fts-canonical and synaptic only: also search the file names, '
                             'frontmatter aliases and headings of an index built with '
                             '`index --name-fields`, and merge those hits into the ranking '
                             '(off by default; the default packet is unchanged).')
    parser.add_argument('--prompt-file', metavar='PATH',
                        help='read the prompt from this UTF-8 file (- reads standard input) '
                             'instead of the last argument; avoids command-line length limits.')
    parser.add_argument('--jev-candidates', type=int, default=0, metavar='N',
                        help='fts and default synaptic only: add a `jev_candidates` side '
                             'channel with up to N undelivered notes for the optional advisor '
                             f'(0-{JEV_CANDIDATES_CAP}; default 0 = none). Evidence is '
                             'unchanged.')
    parser.add_argument('prompt', nargs='?')
    args = parser.parse_args(argv)
    problem = inapplicable_flag(args)
    if problem:
        parser.exit(2, f'{parser.prog}: error: {problem}\n')
    for name, default in SYNAPTIC_DEFAULTS.items():
        if getattr(args, name) is None:
            setattr(args, name, default)
    args.prompt = read_prompt(parser, args)
    try:
        if min(args.top_k, args.budget, args.per_source, args.budget_tokens) <= 0 \
                or args.extra_tokens < 0:
            raise ValueError('Budgets and top-k must be positive')
        if not 0 <= args.jev_candidates <= JEV_CANDIDATES_CAP:
            raise ValueError(f'--jev-candidates must be between 0 and {JEV_CANDIDATES_CAP}')
        packet = retrieve(args)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        print(json.dumps({'schema':'evidence-delivery-v1','operation_status':'error',
                          'status':'ERROR','evidence':[],'error':str(exc)}))
        return 1
    print(json.dumps(packet, ensure_ascii=False, separators=(',', ':')))
    record_usage(args, packet)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
