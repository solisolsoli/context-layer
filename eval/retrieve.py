#!/usr/bin/env python3
"""Comparable retrieval adapters with one evidence format and content budget.

All methods use the same frozen router index, source rules and UTF-8 input.
No method sees evaluation labels. grep/fts/fts-canonical are baselines;
router is the experimental implementation, not a presumed improvement.
"""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import io
import json
import os
import re
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'router'))
from source_policy import (SymlinkSource, config_exclusions, exclusion_matcher, load_config,
                           source_path)
import context_router
import index_format
import textfold
from textio import configure_stdout

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
    reads the ledger back into retrieval. The setting is read here first, with the loader
    already imported, so a vault that does not record (the default) imports nothing more."""
    if args.method not in ('fts', 'synaptic'):
        return
    try:
        if load_config(args.vault.resolve() / '.context/routes.json').get('record_usage') is not True:
            return
    except Exception:
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


DELIVERIES = ('window', 'prefix', 'focus')
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


def focus_spans(text, query_terms, limit, synapse):
    """`--delivery focus`: the blocks in which FTS5 finds a query term, each with the block
    before and after it in the same section (the answer often sits next to the words that
    found the note), merged where only blank lines separate them; None when no block holds a term, or when
    the selection is the whole note's text anyway. Over `limit` characters in all, the
    plain match-anchored windows are used instead."""
    blocks = synapse.split_blocks(text)
    if not blocks or not query_terms:
        return None
    found = textfold.term_matches([text[block.lo:block.hi] for block in blocks], query_terms)
    matched = [index for index, terms in enumerate(found) if terms]
    if not matched:
        return None
    keep = sorted({j for i in matched for j in (i - 1, i, i + 1) if 0 <= j < len(blocks)
                   and blocks[j].headings == blocks[i].headings})
    spans = []
    for index in keep:
        lo, hi = blocks[index].lo, blocks[index].hi
        if spans and not text[spans[-1][1]:lo].strip():
            spans[-1] = (spans[-1][0], hi)
        else:
            spans.append((lo, hi))
    if len(spans) == 1 and not text[:spans[0][0]].strip() and not text[spans[0][1]:].strip():
        return None                               # all of it: deliver the note whole
    if sum(hi - lo for lo, hi in spans) > limit:
        return windows(text, query_terms, limit, synapse)
    return spans


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
    """The items `--delivery MODE` gives for one note within `limit` characters. `focus`:
    the matched blocks and their same-section neighbours (focus_spans) whenever they are
    less than the note. Otherwise the whole note when it fits; else `window` (and `focus`)
    delivers the match-anchored windows and falls back to the prefix when no block holds a
    query term, and `prefix` delivers the note's first `limit` characters."""
    if limit <= 0 or not text:
        return []
    if mode == 'focus':           # matched blocks and their neighbours, even when it all fits
        spans = focus_spans(text, query_terms, limit, synapse)
        if spans:
            return [window_item(name, text, sha, lo, hi) for lo, hi in spans]
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


class Kept:
    """`names` in order without the excluded ones, as the list comprehension gave, but
    checked lazily: a name is tested only when a position at or after it is read. The
    default fts packet reads --top-k names; the NOT_FOUND reason reads one; slicing,
    indexing, iteration and len() see exactly the eager list."""

    def __init__(self, names, is_excluded):
        self._names = names
        self._is_excluded = is_excluded
        self._kept = []
        self._next = 0

    def _fill(self, count=None):
        names, kept = self._names, self._kept
        while (count is None or len(kept) < count) and self._next < len(names):
            name = names[self._next]
            self._next += 1
            if not self._is_excluded(name):
                kept.append(name)

    def __getitem__(self, index):
        if isinstance(index, slice):
            if index.start in (None, 0) and index.step in (None, 1) \
                    and index.stop is not None and index.stop >= 0:
                self._fill(index.stop)
            else:
                self._fill()
            return self._kept[index]
        self._fill(None if index < 0 else index + 1)
        return self._kept[index]

    def __iter__(self):
        position = 0
        while True:
            self._fill(position + 1)
            if position >= len(self._kept):
                return
            yield self._kept[position]
            position += 1

    def __len__(self):
        self._fill()
        return len(self._kept)

    def __bool__(self):
        self._fill(1)
        return bool(self._kept)


# The exclusion verdict of a name is a pure function of the name and the configured
# exclusions, so a long-lived process (`--serve`, the MCP server's worker) keeps the
# verdicts per exclusion list instead of recomputing them on every call.
_MATCHERS = {}


def cached_matcher(prefixes):
    key = tuple(prefixes)
    matcher = _MATCHERS.get(key)
    if matcher is None:
        if len(_MATCHERS) >= 8:
            _MATCHERS.clear()
        matcher = _MATCHERS[key] = exclusion_matcher(list(key))
    return matcher


class IndexDigest:
    """SHA-256 of every byte of the open index file, computed on a thread while the query
    runs (reading and hashing release the GIL), so the coverage receipt costs no wall time
    it did not cost before. Never cached: every call hashes the bytes it reads."""

    def __init__(self, handle, expected=None):
        self._handle = handle
        self._expected = expected
        self._value = None
        self._error = None
        self._thread = threading.Thread(target=self._run, name='index-digest', daemon=True)
        self._thread.start()

    def _run(self):
        try:
            hasher = hashlib.sha256()
            self._handle.seek(0)
            for block in iter(lambda: self._handle.read(1 << 20), b''):
                hasher.update(block)
            self._value = hasher.hexdigest()
        except BaseException as exc:          # re-raised by result()
            self._error = exc

    def wait(self):
        self._thread.join()

    def result(self):
        self.wait()
        if self._error is not None:
            raise self._error
        if self._expected is not None and self._value != self._expected:
            raise ValueError(f'the index generation digest does not match; {REINDEX_HINT}')
        return self._value


def open_index(index):
    """Open the index read-only; return (connection, handle, generation digest).

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
            expected = None
            manifest_path = index.with_name('index-manifest.json')
            try:
                manifest = json.loads(manifest_path.read_bytes())
            except FileNotFoundError:
                manifest = {}             # legacy indexes did not have a manifest
            except (OSError, ValueError):
                raise ValueError(f'the index generation manifest is unreadable; {REINDEX_HINT}') from None
            if isinstance(manifest, dict) and 'index_sha256' in manifest:
                expected = manifest['index_sha256']
                if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
                    raise ValueError(f'the index generation digest is invalid; {REINDEX_HINT}')
                built = connection.execute("SELECT value FROM index_meta WHERE key='built_at'").fetchone()
                if built is None or built[0] != manifest.get('built_at'):
                    raise ValueError(f'the index and generation manifest disagree; {REINDEX_HINT}')
            if os.path.samestat(os.fstat(handle.fileno()), os.stat(index)):
                return connection, handle, expected
        except BaseException:
            connection.close()
            handle.close()
            raise
        connection.close()
        handle.close()
    raise ValueError('the index was replaced three times while it was opened; try again')


def coverage(connection, digest, query_terms, expression):
    """What this query searched: never a path, bounded in size."""
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
        'index_sha256': digest.result(),
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
    connection, handle, expected_digest = open_index(vault / '.context/index.sqlite')
    digest = IndexDigest(handle, expected_digest)
    try:
        # Exclusions are string rules, checked only on the names a method looks at; the
        # symlink and vault-boundary checks touch the file system, so source_path() runs
        # only on the names actually read, always before the read.
        is_excluded = cached_matcher(prefixes)
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
            ranked = Kept(ranked, is_excluded)
        withheld = []

        pinned = set()
        floor_dropped = []

        def below_floor(candidates):
            """--relevance-floor R (opt-in; 0 = off): of the top-k candidates, drop a note
            whose best bm25 is weaker than R x the strongest candidate's. A canonical pin and
            a note ranked only by its name fields are always kept. Nothing is back-filled; the
            dropped paths are listed in the packet's `relevance_floor` block."""
            if not args.relevance_floor or not rows:
                return candidates
            scores = dict(rows)
            known = [scores[name] for name in candidates if name in scores]
            if not known or min(known) >= 0:
                return candidates
            limit = args.relevance_floor * min(known)
            kept = []
            for name in candidates:
                if name in pinned or name not in scores or scores[name] <= limit:
                    kept.append(name)
                else:
                    floor_dropped.append(name)
            return kept

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
            candidates = below_floor(names[:args.top_k])
            for name in candidates:
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
            if args.relevance_floor:
                packet['relevance_floor'] = {'ratio': args.relevance_floor,
                                             'below_floor': list(floor_dropped)}
            return packet

        def with_coverage(packet):
            if packet.get('status') == 'NOT_FOUND':
                packet['reason'] = (NO_TERMS if not query_terms
                                    else NOT_DELIVERED if ranked else NO_MATCH)
            packet['coverage'] = coverage(connection, digest, query_terms,
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
            options.focus = delivery == 'focus'
            reader = lambda name: source_path(vault, name, prefixes).read_bytes()  # noqa: E731
            if compact:
                return with_coverage(synapse.retrieve(vault, args.prompt, query_terms, rows,
                                                      set(allowed), indexed, reader, options))
            return with_coverage(with_withheld(synapse.extend(
                vault, args.prompt, query_terms, rows, lexical_evidence(ranked), set(allowed),
                indexed, reader, options)))
        if args.method == 'fts-canonical':
            parsed = context_router.parse_prompt(args.prompt, config)
            pinned_names = []
            for route in parsed['routes']:
                for entry in config['routes'][route].get('canonical_sources', []):
                    if isinstance(entry, dict) and entry.get('attachment') == 'opportunistic':
                        continue
                    name = entry['path'] if isinstance(entry, dict) else entry
                    source_path(vault, name, prefixes)
                    if name not in allowed:
                        raise ValueError(f'Canonical source not indexed: {name}')
                    pinned_names.append(name)
            ranked = list(dict.fromkeys(pinned_names + list(ranked)))
            pinned.update(pinned_names)
        evidence = lexical_evidence(ranked)
        return with_coverage(with_withheld({
            'schema': 'evidence-delivery-v1', 'operation_status': 'ok',
            'status': 'PARTIAL' if evidence else 'NOT_FOUND', 'evidence': evidence}))
    finally:
        connection.close()
        digest.wait()
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
    if args.relevance_floor and (args.method in ('grep', 'router')
                                 or (args.method == 'synaptic' and args.compact)):
        return ('--relevance-floor applies to --method fts, fts-canonical and the default '
                'synaptic packet (its fts part)')
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


class Parser(argparse.ArgumentParser):
    """argparse that keeps what it would print to stdout (--help, a usage line asked for)
    in `self.printed` instead of writing it, so an in-process caller gets it as text and
    sys.stdout is never swapped (other threads keep theirs). Errors still go to stderr."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.printed = []

    def _print_message(self, message, file=None):
        if message and (file is None or file is sys.stdout or file is sys.__stdout__):
            self.printed.append(message)
        else:
            super()._print_message(message, file)


class Exit(SystemExit):
    """SystemExit from argparse, carrying the text it would have printed to stdout."""

    def __init__(self, code, printed):
        super().__init__(code)
        self.printed = printed


def build_parser():
    parser = Parser(prog='retrieve.py', description=__doc__)
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
                             'behaviour); `focus` delivers only the blocks that hold query '
                             'terms and their same-section neighbours, even for a short note '
                             '(the prompt hook\'s default), and in synaptic reserves only '
                             'strongly activated linked notes.')
    parser.add_argument('--name-fields', action='store_true',
                        help='fts, fts-canonical and synaptic only: also search the file names, '
                             'frontmatter aliases and headings of an index built with '
                             '`index --name-fields`, and merge those hits into the ranking '
                             '(off by default; the default packet is unchanged).')
    parser.add_argument('--relevance-floor', type=float, default=0.0, metavar='R',
                        help='fts, fts-canonical and the fts part of the default synaptic '
                             'packet: drop a top-k note whose bm25 is weaker than R times the '
                             'strongest one (0 <= R < 1; default 0 = off). Fewer tokens; may '
                             'drop evidence. Dropped paths are listed under relevance_floor.')
    parser.add_argument('--prompt-file', metavar='PATH',
                        help='read the prompt from this UTF-8 file (- reads standard input) '
                             'instead of the last argument; avoids command-line length limits.')
    parser.add_argument('prompt', nargs='?')
    return parser


def execute(argv, prompt=None):
    """One retrieval, as `main` runs it: (exit code, the text main prints, packet or None,
    parsed arguments or None). Argparse problems raise SystemExit exactly as the command
    line does. With `prompt` given (an in-process caller), the prompt is that string and
    neither the positional prompt nor --prompt-file may be given."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        problem = inapplicable_flag(args)
        if problem:
            parser.exit(2, f'{parser.prog}: error: {problem}\n')
        for name, default in SYNAPTIC_DEFAULTS.items():
            if getattr(args, name) is None:
                setattr(args, name, default)
        if prompt is None:
            args.prompt = read_prompt(parser, args)
        elif args.prompt is not None or args.prompt_file is not None:
            parser.error('the prompt is passed by the caller; give no prompt argument')
        else:
            args.prompt = prompt
    except SystemExit as exc:
        raise Exit(exc.code, ''.join(parser.printed)) from None
    try:
        if min(args.top_k, args.budget, args.per_source, args.budget_tokens) <= 0 \
                or args.extra_tokens < 0:
            raise ValueError('Budgets and top-k must be positive')
        if not 0 <= args.relevance_floor < 1:
            raise ValueError('--relevance-floor must be at least 0 and below 1')
        packet = retrieve(args)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        return 1, json.dumps({'schema':'evidence-delivery-v1','operation_status':'error',
                              'status':'ERROR','evidence':[],'error':str(exc)}) + '\n', None, args
    return 0, json.dumps(packet, ensure_ascii=False, separators=(',', ':')) + '\n', packet, args


def run(argv, prompt=None):
    """In-process `retrieve.py ARGV`: (exit code, stdout text, packet or None).

    The text is what the command prints, byte for byte (help and usage output included);
    argparse errors give exit code 2 with the message on stderr, as on the command line.
    The opt-in usage ledger is written after the packet is built, as `main` does."""
    try:
        code, text, packet, args = execute(argv, prompt)
    except Exit as exc:
        code = exc.code
        if code is None:
            code = 0
        elif not isinstance(code, int):
            print(code, file=sys.stderr)
            code = 1
        return code, exc.printed, None
    if packet is not None:
        record_usage(args, packet)
    return code, text, packet


def serve(stdin=None, stdout=None):
    """`--serve`: a long-lived worker for the MCP server. One JSON request per line,
    {"argv": [...], "prompt": str or null}; one JSON response per line, {"code": int,
    "stdout": str, "stderr": str}. Each request is `run(argv, prompt)`: the same packet a
    fresh `retrieve.py` process gives (sources, config, index and graph are read per
    request; only the pure caches above persist). Ends at end of input."""
    reader = stdin if stdin is not None else sys.stdin.buffer
    writer = stdout if stdout is not None else sys.stdout.buffer
    sys.stdout = sys.stderr            # nothing but responses may reach the response pipe
    for raw in iter(reader.readline, b''):
        try:
            request = json.loads(raw.decode('utf-8'))
            argv = [str(part) for part in request['argv']]
            prompt = request.get('prompt')
            if prompt is not None and not isinstance(prompt, str):
                raise TypeError('prompt must be a string')
        except (ValueError, KeyError, TypeError) as exc:
            response = {'code': 1, 'stdout': '', 'stderr': f'bad worker request: {exc}'}
        else:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                try:
                    code, text, _ = run(argv, prompt)
                except Exception as exc:  # one bad request must not end the worker
                    code, text = 1, ''
                    print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
            response = {'code': code, 'stdout': text, 'stderr': err.getvalue()}
        writer.write(json.dumps(response, ensure_ascii=True).encode('ascii') + b'\n')
        writer.flush()
    return 0


def main(argv=None):
    configure_stdout()
    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments == ['--serve']:
        return serve()
    try:
        code, text, packet, args = execute(arguments)
    except Exit as exc:
        sys.stdout.write(exc.printed)
        raise SystemExit(exc.code) from None
    sys.stdout.write(text)
    if packet is not None:
        record_usage(args, packet)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
