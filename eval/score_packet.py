#!/usr/bin/env python3
"""Scores a context packet's INTELLIGENCE against a 7-axis rubric.

Stdlib only. No network calls, no LLM calls -- this is a pattern and structure
checker, not a semantic judge. Anything it cannot honestly verify is reported
as "MANUAL" or "N/A" and is never folded into the numeric score. The rule the
whole file obeys: do not manufacture false precision. An axis that cannot be
automated is handed to a human, not rounded to a plausible-looking 1.

Axes (0/1/2 each)
-----------------
  1. decisive_fact       AUTO  -- are the facts that actually change the answer
                                  present? (regex list from the case)
  2. timeliness          AUTO  -- when the topic has a stale/current pair, does
                                  the packet resolve which one is in force?
  3. unstated_constraint AUTO  -- does the packet carry the rule the prompt did
                                  NOT name but that binds the answer?
  4. pitfall             AUTO  -- does it warn about the approach already tried
                                  and rejected?
  5. sourcing_grade      AUTO  -- structural: does every evidence block carry
                                  its full provenance fields?
  6. honest_abstention   AUTO only where the case defines a fabrication trap;
                                  otherwise MANUAL. A sprung trap zeroes the
                                  WHOLE case (all axes) -- fabrication is not a
                                  one-axis deduction.
  7. waste               AUTO  -- explicitly a proxy: the share of evidence-block
                                  text matching at least one required pattern.
                                  Not a semantic relevance judgement. It is
                                  scored LAST and on purpose: "less" only counts
                                  after capability has been counted.

Pattern axes (1-4 and 6) read the bodies of the packet's evidence and card blocks
only: never the echoed prompt, the routing metadata (which repeats the prompt's
words) or the block headings (paths, card answers). A case whose packet file is
missing scores 0 on every axis its definition makes applicable, and the headline
says how many such cases there were.

Usage
-----
  python3 score_packet.py --cases cases.example.json \
      --packet packets/packet-C01.md --case-id C01
  python3 score_packet.py --cases cases.example.json \
      --batch-dir packets/ --out rubric-results.json

Packet structure is read from --packet-format (default
packet-format.example.json), so this works against any router whose output has
per-source blocks with provenance fields. If yours does not, axis 5 will score
0 and it will be telling you something true.
"""
import argparse
import difflib
import json
import os
import re
import sys

AXES = ["decisive_fact", "timeliness", "unstated_constraint", "pitfall",
        "sourcing_grade", "honest_abstention", "waste"]

DEFAULT_FORMAT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "packet-format.example.json")


def rx(pattern):
    return re.compile(pattern, re.IGNORECASE | re.UNICODE)


class PacketFormat:
    def __init__(self, cfg):
        self.echoed_prompt = re.compile(cfg["echoed_prompt_pattern"], re.DOTALL)
        # Optional: a router block that repeats the prompt's words as metadata.
        routing = cfg.get("routing_metadata_pattern")
        self.routing_metadata = re.compile(routing, re.DOTALL) if routing else None
        self.block = re.compile(cfg["block_pattern"], re.MULTILINE | re.DOTALL)
        self.evidence_id = re.compile(cfg["evidence_block_id_pattern"])
        self.evidence_fields = {k: re.compile(v, re.MULTILINE)
                                for k, v in cfg["evidence_fields"].items()}
        self.card_fields = {k: re.compile(v, re.MULTILINE)
                            for k, v in cfg["card_fields"].items()}
        self.card_marker = re.compile(cfg["card_block_marker_pattern"])
        self.claims_support = re.compile(cfg["claims_support_pattern"])
        self.structural_field_line = re.compile(cfg["structural_field_line_pattern"], re.MULTILINE)
        self.hedge_word = rx(cfg["hedge_word_pattern"])
        self.dup_threshold = cfg["duplicate_similarity_threshold"]
        self.waste_bands = cfg["waste_bands"]
        self.waste_v2_bands = cfg["waste_v2_bands"]


def load_cases(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {c["id"]: c for c in data["cases"]}


def strip_echoed_prompt(packet_text, fmt):
    """Routers commonly echo the user's exact prompt back inside the packet. If
    a case's own prompt contains a word this rubric treats as a fabrication trap
    or as a decisive fact, matching inside that echo would credit or blame the
    ROUTER for the USER's words. Every pattern check runs against the stripped
    text, so a hit can only come from something the packet itself added.

    This single correction changed a real score in a maintainer baseline: a
    case was being zeroed for "fabricating" a phrase the user had typed.

    The routing metadata goes too: it lists the prompt's own tokens, so a
    decisive-fact pattern could otherwise be credited for the user's words."""
    text = fmt.echoed_prompt.sub("", packet_text, count=1)
    if fmt.routing_metadata is not None:
        text = fmt.routing_metadata.sub("", text, count=1)
    return text


def evidence_text(packet_text, fmt):
    """The bodies of the evidence and card blocks, joined: the only text the pattern
    axes read. Block headings are left out (a path or a card's answer is not
    evidence)."""
    return "\n".join(body for _name, body in fmt.block.findall(packet_text))


def score_list_axis(items, packet_text):
    """Generic AUTO scorer for decisive_fact / unstated_constraint / pitfall."""
    if not items:
        return None, 0, 0, []
    detail, matched = [], 0
    for it in items:
        hit = bool(rx(it["pattern"]).search(packet_text))
        matched += 1 if hit else 0
        detail.append({"pattern": it["pattern"], "hit": hit,
                       "source_path": it.get("source_path"),
                       "confidence": it.get("confidence", "medium")})
    total = len(items)
    score = 2 if matched == total else (0 if matched == 0 else 1)
    return score, matched, total, detail


def score_timeliness(t, packet_text):
    if not t:
        return "N/A", {}
    stale = bool(rx(t["stale_pattern"]).search(packet_text))
    current = bool(rx(t["current_pattern"]).search(packet_text))
    resolved = bool(rx(t["resolution_pattern"]).search(packet_text))
    detail = {"stale_present": stale, "current_present": current, "resolution_present": resolved}
    if current and resolved:
        return 2, detail
    if current and not stale:
        return 2, detail          # no confusion risk: the old value is simply absent
    if current and stale and not resolved:
        return 0, detail          # both values side by side, unresolved
    if stale and not current:
        return 0, detail
    return "N/A", detail          # topic never surfaced: that is axis 1's failure, not this one


def score_sourcing_grade(packet_text, fmt):
    blocks = fmt.block.findall(packet_text)
    full_blocks = [(n, b) for n, b in blocks if fmt.evidence_id.match(n.strip())]
    card_blocks = [(n, b) for n, b in blocks
                   if not fmt.evidence_id.match(n.strip()) and fmt.card_marker.search(b)]

    for shape, group, fields in (("evidence", full_blocks, fmt.evidence_fields),
                                 ("card", card_blocks, fmt.card_fields)):
        if not group:
            continue
        per_block, complete = [], 0
        for name, body in group:
            ok = all(f.search(body) for f in fields.values())
            per_block.append({"block": name, "complete": ok, "shape": shape})
            complete += 1 if ok else 0
        ratio = complete / len(group)
        score = 2 if ratio == 1.0 else (1 if ratio > 0 else 0)
        return score, {"shape": shape, "blocks": len(group),
                       "complete_blocks": complete, "per_block": per_block}

    # No blocks at all, yet the packet still asserts supported evidence: that is
    # structural overconfidence and scores 0, not N/A.
    return 0, {"shape": "none", "evidence_blocks": 0,
               "claims_support_without_evidence": bool(fmt.claims_support.search(packet_text))}


def score_abstention(trap, packet_text):
    if not trap:
        return "MANUAL", {"reason": "no trap defined for this case; fabrication-risk judgement "
                                    "is left to a human reviewer"}
    forbidden = bool(rx(trap["forbidden_pattern"]).search(packet_text))
    hedge = bool(rx(trap["hedge_pattern"]).search(packet_text))
    detail = {"forbidden_hit": forbidden, "hedge_hit": hedge}
    if forbidden and not hedge:
        return 0, detail      # trap sprung -> whole-case override in score_case()
    if hedge:
        return 2, detail
    return "MANUAL", detail   # topic silent: neither fabricated nor explicitly hedged


def score_abstention_heuristic_signal(packet_text, fmt):
    """Descriptive only. Counts hedge-flavoured phrases anywhere in the packet so
    a human triaging the MANUAL cases has a cheap hint of where to look. It never
    changes the honest_abstention score and is never folded into the total: a word
    count cannot tell whether a hedge was warranted or correctly placed."""
    return {"hedge_phrases_found": len(fmt.hedge_word.findall(packet_text)),
            "note": "heuristic signal only; does not affect any score"}


def _required_patterns(case):
    pats = []
    for group in ("decisive_facts", "unstated_constraints", "pitfalls"):
        pats += [it["pattern"] for it in (case.get(group) or [])]
    if case.get("timeliness"):
        t = case["timeliness"]
        pats += [t["stale_pattern"], t["current_pattern"], t["resolution_pattern"]]
    if case.get("abstention_trap"):
        a = case["abstention_trap"]
        pats += [a["forbidden_pattern"], a["hedge_pattern"]]
    return pats


def score_waste(case, packet_text, fmt):
    """v1: block-level credit. A block counts as fully relevant if ANY line in it
    matches a required pattern. Coarse, and labelled coarse."""
    pats = _required_patterns(case)
    if not packet_text:
        return "N/A", {}
    blocks = fmt.block.findall(packet_text)
    if not blocks:
        return 0, {"reason": "no evidence blocks to be relevant", "ratio": 0.0}
    relevant = block_chars = 0
    for _name, body in blocks:
        block_chars += len(body)
        if any(rx(p).search(body) for p in pats):
            relevant += len(body)
    ratio = (relevant / block_chars) if block_chars else 0.0
    b = fmt.waste_bands
    score = 2 if ratio >= b["two"] else (1 if ratio >= b["one"] else 0)
    return score, {"ratio": round(ratio, 3), "relevant_chars": relevant,
                   "block_chars": block_chars,
                   "note": "heuristic proxy: share of evidence-block text matching >=1 required "
                           "pattern; not a semantic relevance judgement"}


def _normalize_block(body):
    return re.sub(r"\s+", " ", body).strip().lower()


def score_waste_v2(case, packet_text, fmt):
    """v2: two refinements over v1, reported alongside it rather than replacing it.

      (a) line-level credit -- only the lines that actually match count, so a long
          block with one on-topic line no longer scores like a short on-topic one.
      (b) duplicate penalty -- a block that is a near-duplicate of an earlier block
          in the same packet earns zero credit while still costing its characters.

    Mandatory provenance field lines are excluded from BOTH numerator and
    denominator: they are required structure already graded by axis 5, and
    penalising them here would double-count the same text under two axes.

    The bands are re-fitted for this finer granularity. Do not compare a v2 score
    to a v1 score as if they were the same measurement."""
    pats = _required_patterns(case)
    if not packet_text:
        return "N/A", {}
    blocks = fmt.block.findall(packet_text)
    if not blocks:
        return 0, {"reason": "no evidence blocks to be relevant", "ratio": 0.0}

    seen, dup_flags = [], []
    for _name, body in blocks:
        norm = _normalize_block(body)
        dup_flags.append(any(difflib.SequenceMatcher(None, norm, prior).ratio() >= fmt.dup_threshold
                             for prior in seen))
        seen.append(norm)

    relevant = block_chars = duplicates = 0
    per_block = []
    for (name, body), is_dup in zip(blocks, dup_flags):
        if is_dup:
            duplicates += 1
            block_chars += len(body)
            per_block.append({"block": name, "duplicate_of_earlier_block": True,
                              "relevant_chars": 0, "chars": len(body)})
            continue
        lines = [ln for ln in body.splitlines()
                 if ln.strip() and not fmt.structural_field_line.match(ln)]
        content_chars = sum(len(ln) for ln in lines)
        block_relevant = sum(len(ln) for ln in lines if any(rx(p).search(ln) for p in pats))
        block_chars += content_chars
        relevant += block_relevant
        per_block.append({"block": name, "duplicate_of_earlier_block": False,
                          "relevant_chars": block_relevant, "content_chars": content_chars})

    ratio = (relevant / block_chars) if block_chars else 0.0
    b = fmt.waste_v2_bands
    score = 2 if ratio >= b["two"] else (1 if ratio >= b["one"] else 0)
    return score, {"ratio": round(ratio, 3), "relevant_chars": relevant, "block_chars": block_chars,
                   "duplicate_blocks": duplicates, "total_blocks": len(blocks),
                   "note": "heuristic proxy v2: line-level credit plus zero credit for near-duplicate "
                           "blocks; bands re-fitted, not comparable to v1"}


def score_case(case, raw_packet_text, fmt):
    packet_text = strip_echoed_prompt(raw_packet_text, fmt)
    evidence = evidence_text(packet_text, fmt)
    result = {"case_id": case["id"], "category": case.get("category")}

    df, dm, dt, dd = score_list_axis(case.get("decisive_facts"), evidence)
    result["decisive_fact"] = {"score": df if df is not None else "N/A",
                               "matched": dm, "total": dt, "detail": dd}
    tl, tld = score_timeliness(case.get("timeliness"), evidence)
    result["timeliness"] = {"score": tl, "detail": tld}
    uc, um, ut, ud = score_list_axis(case.get("unstated_constraints"), evidence)
    result["unstated_constraint"] = {"score": uc if uc is not None else "N/A",
                                     "matched": um, "total": ut, "detail": ud}
    pf, pm, pt, pd = score_list_axis(case.get("pitfalls"), evidence)
    result["pitfall"] = {"score": pf if pf is not None else "N/A",
                         "matched": pm, "total": pt, "detail": pd}
    sg, sgd = score_sourcing_grade(packet_text, fmt)
    result["sourcing_grade"] = {"score": sg, "detail": sgd}
    ab, abd = score_abstention(case.get("abstention_trap"), evidence)
    result["honest_abstention"] = {"score": ab, "detail": abd}
    ws, wsd = score_waste(case, packet_text, fmt)
    result["waste"] = {"score": ws, "detail": wsd}

    # Additive diagnostics; neither changes any scored axis.
    w2, w2d = score_waste_v2(case, packet_text, fmt)
    result["waste_v2"] = {"score": w2, "detail": w2d}
    result["abstention_heuristic_signal"] = score_abstention_heuristic_signal(packet_text, fmt)

    fabrication = (result["honest_abstention"]["score"] == 0)
    result["fabrication_override_applied"] = fabrication
    if fabrication:
        for ax in ["decisive_fact", "timeliness", "unstated_constraint", "pitfall",
                   "sourcing_grade", "waste", "waste_v2"]:
            if isinstance(result[ax]["score"], int):
                result[ax]["score_before_override"] = result[ax]["score"]
                result[ax]["score"] = 0
    return result


def score_missing(case, reason):
    """A case with no packet: 0 on every axis its definition makes applicable.

    Leaving it out would score a router only on the cases it answered. Axes the
    case does not define stay N/A; honest_abstention counts only where a trap is
    defined, because only then is it scored automatically."""
    result = {"case_id": case["id"], "category": case.get("category"),
              "missing_packet": True, "error": reason}
    defined = {"decisive_fact": bool(case.get("decisive_facts")),
               "timeliness": bool(case.get("timeliness")),
               "unstated_constraint": bool(case.get("unstated_constraints")),
               "pitfall": bool(case.get("pitfalls")),
               "sourcing_grade": True,
               "honest_abstention": bool(case.get("abstention_trap")),
               "waste": True}
    for ax in AXES:
        result[ax] = {"score": 0 if defined[ax] else "N/A", "detail": {"reason": reason}}
    return result


def aggregate(results):
    """Total over APPLICABLE axes only. N/A and MANUAL are excluded from both the
    numerator and the denominator -- they are not zeros, and pretending they are
    would let an unmeasurable axis quietly drag a score down (or, worse, let a
    router look good by making axes inapplicable). A missing packet is different:
    its applicable axes count as 0 (see score_missing), and they are counted."""
    earned = possible = 0
    per_axis = {ax: {"0": 0, "1": 0, "2": 0, "N/A": 0, "MANUAL": 0, "applicable_n": 0,
                     "earned": 0} for ax in AXES}
    for r in results:
        if "error" in r and not r.get("missing_packet"):
            continue
        for ax in AXES:
            s = r[ax]["score"]
            if isinstance(s, int):
                per_axis[ax][str(s)] += 1
                per_axis[ax]["applicable_n"] += 1
                per_axis[ax]["earned"] += s
                earned += s
                possible += 2
            else:
                per_axis[ax][s] += 1
    for ax in AXES:
        n = per_axis[ax]["applicable_n"]
        per_axis[ax]["mean"] = (per_axis[ax]["earned"] / n) if n else None
    return {
        "cases_scored": sum(1 for r in results if "error" not in r),
        "missing_packets": sum(1 for r in results if r.get("missing_packet")),
        "points_earned": earned,
        "points_possible": possible,
        "score_ratio": (earned / possible) if possible else None,
        "fabrication_overrides": sum(1 for r in results if r.get("fabrication_override_applied")),
        "manual_axis_verdicts": sum(1 for r in results for ax in AXES
                                    if ax in r and r[ax]["score"] == "MANUAL"),
        "per_axis": per_axis,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", required=True)
    ap.add_argument("--packet", help="path to a single packet file")
    ap.add_argument("--case-id", help="case id matching --packet")
    ap.add_argument("--batch-dir", help="directory containing packet-<CASEID>.md files")
    ap.add_argument("--packet-format", default=DEFAULT_FORMAT)
    ap.add_argument("--out", help="write JSON results here instead of stdout")
    args = ap.parse_args()

    with open(args.packet_format, "r", encoding="utf-8") as f:
        fmt = PacketFormat(json.load(f))
    cases = load_cases(args.cases)
    results = []

    if args.batch_dir:
        for case_id, case in cases.items():
            fn = os.path.join(args.batch_dir, f"packet-{case_id}.md")
            if not os.path.exists(fn):
                results.append(score_missing(case, f"missing packet file packet-{case_id}.md"))
                continue
            with open(fn, "r", encoding="utf-8") as f:
                results.append(score_case(case, f.read(), fmt))
    elif args.packet and args.case_id:
        case = cases.get(args.case_id)
        if not case:
            print(f"Unknown case id {args.case_id}", file=sys.stderr)
            sys.exit(1)
        with open(args.packet, "r", encoding="utf-8") as f:
            results.append(score_case(case, f.read(), fmt))
    else:
        print("Provide either --packet + --case-id or --batch-dir", file=sys.stderr)
        sys.exit(1)

    agg = aggregate(results)
    out = {"schema": "packet-intelligence-score-v1", "aggregate": agg, "results": results}
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Wrote {agg['cases_scored']} scored cases to {args.out}")
    else:
        print(text)
    print(f"rubric score: {agg['points_earned']}/{agg['points_possible']} "
          f"({(agg['score_ratio'] or 0)*100:.1f}%) over applicable axes; "
          f"{agg['missing_packets']} missing packet(s) counted as 0; "
          f"{agg['manual_axis_verdicts']} MANUAL verdicts excluded; "
          f"{agg['fabrication_overrides']} fabrication override(s)")


if __name__ == "__main__":
    main()
