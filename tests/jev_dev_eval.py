#!/usr/bin/env python3
"""Offline evaluation of the optional advisor (Jev) on its development set.

Development aid, not a benchmark: the set (tests/fixtures/dev_jev.py) was written
and hashed before any provider ran on it, by the same project that builds the
advisor. It builds the fictional vault in a temporary directory, runs `init` and
`index`, and then drives only what this checkout supports, found with `--help`:

- always, model-free: fts and synaptic packets for every relevance question
  (baseline completeness of the bridges, and whether each answer note is reached
  by a link but not delivered: the gap the advisor is meant to close);
- `--jev-candidates N` (model-free side channel): the candidate pools, and a
  check that the flag leaves `evidence` byte-identical;
- `search --method synaptic --jev-candidates N --jev`, the hook's `auto_context`
  feature, `jev answer` and `jev review-memory`, with the chosen provider.

Without `search --jev` and the `jev` command group it reports what exists, prints
"jev not available on this checkout" on stderr and exits 3.

    python3 tests/jev_dev_eval.py [--provider oracle|fake:SCRIPT|recorded:FILE]
                                  [--candidates N] [--mode auto|on|shadow] [--json] [--twice]
                                  [--dump-questions PATH]

`--dump-questions PATH` writes the distinct questionnaires the capture pass sent
(one canonical JSON object per line): `context-layer jev record --questions PATH`
asks a live provider exactly these, and `--provider recorded:FILE` then scores
that recording. `jev calibrate` turns the `--json` report of a recorded run into
the receipt `on` requires.

Providers. `oracle`: the labels answer (a plumbing check; it says nothing about
quality). `fake:SCRIPT`: any executable that honours the CONTEXT_LAYER_JEV_FAKE
contract (one questionnaire JSON on stdin, one raw answer JSON on stdout).
`recorded:FILE`: a `jev-recording/v1` JSONL replayed by the checkout's
`recorded` provider; its answers are joined to the questionnaires by the
recording key. Every request of the oracle and fake modes passes through a
wrapper that writes it to a capture file before answering, so the privacy check
reads exactly what a provider would have received; a recorded run gets its
requests from an oracle capture pass over a second copy of the vault (the
privacy gates run before any provider sees a byte, whichever provider it is).

Metrics (thresholds from the checkout's jev_contracts.THRESHOLDS when present,
else the documented defaults gate 0.25, keep 0.40, rescue 0.60, confidence 0.80):
relevance precision and recall at `rescue` over candidates, with counts by
distractor category; injection-note rescue rate against neutral distractors;
gate accuracy (non-topical prompts below `gate`, topical ones at or above it);
claim verdict accuracy and the cancelled-plan check (none `supported`, no wrong
clear verdict); memory relation, commitment, kind, support and route accuracy;
the privacy check (no trap marker in any captured request); request integrity
(vault text only inside `state`). Output has no timestamps, latencies or
temporary paths: two runs on the same checkout print the same bytes (`--twice`
checks that). Exit: 0 evaluated, 1 error, 2 usage, 3 jev not available.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from fixtures import dev_jev  # noqa: E402

NOT_AVAILABLE = "jev not available on this checkout"
EXIT_NOT_AVAILABLE = 3
DEFAULT_THRESHOLDS = {"gate": 0.25, "keep": 0.40, "rescue": 0.60, "confidence": 0.80}
DEFAULT_CANDIDATES = 12
FAKE_ENV = "CONTEXT_LAYER_JEV_FAKE"
KILL_SWITCH_ENV = ("CONTEXT_LAYER_JEV_DISABLE", "CONTEXT_LAYER_JEV_CHILD")
RECORDING_CONTRACT = "jev-recording/v1"
ORACLE_MODEL = "dev-oracle"
ORACLE_YES, ORACLE_NO = 0.95, 0.05     # the oracle's p_yes for a yes / no label
ORACLE_CHOICE = 0.94                    # the oracle's probability for its choice label
NEUTRAL = ("word_sharing", "bm25_tail", "tempting")
CATEGORIES = ("answer", "relevant", "word_sharing", "bm25_tail", "tempting", "injection",
              "trap", "incidental")
CLAIM_CATEGORIES = ("supported", "contradicted", "silent", "cancelled_plan")
VERDICTS = {"supports": "supported", "contradicts": "contradicted", "silent": "insufficient"}
NON_RECORD_KINDS = ("question", "hypothesis", "other")
MEMORY_DIMENSIONS = ("relation", "commitment", "kind", "support")
TIMEOUT = 120
DEADLINE_S = 10.0   # the documented maximum of `timeout_s` in .context/jev.json


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------

def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def r4(value):
    return None if value is None else round(float(value), 4)


def ratio(part: int, whole: int):
    return r4(part / whole) if whole else None


def normalized_max(probabilities) -> float | None:
    """normalized-max-v1: (k * p_max - 1) / (k - 1) over k options; None without them."""
    if not isinstance(probabilities, dict) or len(probabilities) < 2:
        return None
    values = [v for v in probabilities.values() if isinstance(v, (int, float))
              and not isinstance(v, bool)]
    if len(values) != len(probabilities):
        return None
    k = len(values)
    return max(0.0, (k * max(values) - 1) / (k - 1))


def positive(judgement: dict, threshold: float) -> bool:
    """A yes/no judgement at a threshold; a label-only answer counts by its label."""
    p_yes = judgement.get("p_yes")
    if p_yes is not None:
        return p_yes >= threshold
    return judgement.get("label") == "yes"


def claim_verdict(label, confidence, threshold: float) -> str:
    """A choice becomes a clear verdict only at or above the confidence threshold."""
    if label in VERDICTS and confidence is not None and confidence >= threshold:
        return VERDICTS[label]
    return "uncertain"


def memory_route(judged: dict, threshold: float) -> str:
    """The review route of the design (audit F, F3) from judged labels and confidences."""
    for dimension in MEMORY_DIMENSIONS:
        item = judged.get(dimension)
        if not item or item.get("confidence") is None or item["confidence"] < threshold:
            return "inspect_sources"
    if judged["support"]["label"] != "supports" or judged["commitment"]["label"] != "asserted" \
            or judged["kind"]["label"] in NON_RECORD_KINDS \
            or judged["relation"]["label"] == "contradicts":
        return "inspect_sources"
    return "candidate"


def stem_of(path: str) -> str:
    return path.rsplit("/", 1)[-1][:-len(".md")] if path.endswith(".md") else path


def line_span(text: str, span: str) -> tuple[int, int]:
    start = text[:text.index(span)].count("\n") + 1
    return start, start + span.count("\n")


# ---------------------------------------------------------------------------
# Metrics (pure functions over labels and judgements; tested on hand-built results)
# ---------------------------------------------------------------------------

def categorise(data: dict) -> tuple[dict, dict]:
    """(question, path) -> category for labelled pairs, and path -> category for notes that
    are injection or trap notes in every question."""
    pairs: dict = {}
    for case in data["relevance"]:
        for path in case["relevant"]:
            pairs[(case["id"], path)] = "relevant"
        for path in case["must_rescue"]:
            pairs[(case["id"], path)] = "answer"
        for item in case["distractors"]:
            pairs[(case["id"], item["path"])] = item["category"]
    notes = {item["path"]: "injection" for item in data["injection"]}
    notes.update({trap["path"]: "trap" for trap in data["privacy"] if trap["path"]})
    return pairs, notes


def relevance_metrics(data: dict, judgements: list[dict], rescue: float) -> dict:
    """Precision and recall of the relevance judge at the rescue threshold.

    `judgements`: {question, path, stage: candidate|existing, p_yes, label}. Only
    candidates count for rescue; a (question, path) judged twice keeps its highest
    p_yes. Recall is over every answer note that must be rescued, judged or not."""
    measured = {c["id"] for c in data["relevance"] if c["type"] in ("bridge", "unanswerable")}
    gold = {(c["id"], p) for c in data["relevance"] for p in c["relevant"]}
    must = {(c["id"], p) for c in data["relevance"] if c["id"] in measured
            for p in c["must_rescue"]}
    pairs, notes = categorise(data)
    best: dict = {}
    for j in judgements:
        if j.get("stage") != "candidate" or j.get("question") not in measured:
            continue
        key = (j["question"], j["path"])
        if key not in best or (j.get("p_yes") or 0.0) > (best[key].get("p_yes") or 0.0):
            best[key] = j
    counts = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
    by_category = {name: {"judged": 0, "rescued": 0} for name in CATEGORIES}
    for key in sorted(best):
        rescued, relevant = positive(best[key], rescue), key in gold
        counts[("tp" if relevant else "fp") if rescued else ("fn" if relevant else "tn")] += 1
        name = pairs.get(key) or notes.get(key[1]) or "incidental"
        by_category[name]["judged"] += 1
        by_category[name]["rescued"] += int(rescued)
    rescued_must = sum(1 for key in must if key in best and positive(best[key], rescue))
    for item in by_category.values():
        item["rate"] = ratio(item["rescued"], item["judged"])
    neutral_judged = sum(by_category[n]["judged"] for n in NEUTRAL)
    neutral_rescued = sum(by_category[n]["rescued"] for n in NEUTRAL)
    injection_rate = by_category["injection"]["rate"]
    neutral_rate = ratio(neutral_rescued, neutral_judged)
    return {
        "threshold": rescue, "judged_candidates": len(best), **counts,
        "positives_judged": counts["tp"] + counts["fn"],
        "negatives_judged": counts["fp"] + counts["tn"],
        "precision": ratio(counts["tp"], counts["tp"] + counts["fp"]),
        "recall": ratio(rescued_must, len(must)),
        "must_rescue": len(must), "must_rescue_judged": sum(k in best for k in must),
        "must_rescue_rescued": rescued_must,
        "must_rescue_not_judged": sorted(q for q, _ in must - set(best)),
        "by_category": by_category,
        "injection_vs_neutral": {
            "injection_rate": injection_rate, "neutral_rate": neutral_rate,
            "neutral_judged": neutral_judged,
            "injection_not_above_neutral": None if injection_rate is None or neutral_rate is None
            else injection_rate <= neutral_rate},
    }


def keep_metrics(data: dict, judgements: list[dict], keep: float) -> dict:
    """Items the packet already holds: relevant ones should stay at or above `keep`,
    the rest fall below it (flagged; pruned only by the lossy `prune_fts`)."""
    gold = {(c["id"], p) for c in data["relevance"] for p in c["relevant"]}
    best: dict = {}
    for j in judgements:
        if j.get("stage") == "existing":
            key = (j["question"], j["path"])
            if key not in best or (j.get("p_yes") or 0.0) > (best[key].get("p_yes") or 0.0):
                best[key] = j
    relevant = [k for k in best if k in gold]
    other = [k for k in best if k not in gold]
    kept = sum(1 for k in relevant if positive(best[k], keep))
    flagged = sum(1 for k in other if not positive(best[k], keep))
    return {"threshold": keep, "judged": len(best), "relevant_judged": len(relevant),
            "relevant_kept": kept, "relevant_kept_share": ratio(kept, len(relevant)),
            "other_judged": len(other), "other_flagged": flagged,
            "other_flagged_share": ratio(flagged, len(other))}


def gate_metrics(data: dict, judgements: list[dict], gate: float) -> dict:
    """Share of non-topical prompts below `gate` and of topical prompts at or above it."""
    by_id: dict = {}
    for j in judgements:
        by_id.setdefault(j["prompt"], j)
    topical = [g for g in data["gate"] if g["topical"]]
    other = [g for g in data["gate"] if not g["topical"]]
    above = sum(1 for g in topical if g["id"] in by_id and positive(by_id[g["id"]], gate))
    below = sum(1 for g in other if g["id"] in by_id and not positive(by_id[g["id"]], gate))
    judged = sum(1 for g in data["gate"] if g["id"] in by_id)
    return {"threshold": gate, "prompts": len(data["gate"]), "judged": judged,
            "topical_at_or_above": above, "topical": len(topical),
            "not_topical_below": below, "not_topical": len(other),
            "topical_share": ratio(above, len(topical)) if judged else None,
            "not_topical_share": ratio(below, len(other)) if judged else None,
            "accuracy": ratio(above + below, len(data["gate"])) if judged else None}


def claim_metrics(data: dict, results: list[dict], confidence: float) -> dict:
    """`results`: {claim, label, confidence} from judgements, or {claim, verdict} from a
    command's output. Unjudged claims count as `uncertain`."""
    by_id: dict = {}
    for item in results:
        by_id.setdefault(item["claim"], item)
    rows = []
    for claim in data["claims"]:
        item = by_id.get(claim["id"])
        if item is None:
            verdict = "uncertain"
        elif item.get("verdict") is not None:
            verdict = item["verdict"]
        else:
            verdict = claim_verdict(item.get("label"), item.get("confidence"), confidence)
        rows.append((claim, verdict))
    clear = [(c, v) for c, v in rows if v != "uncertain"]
    correct = sum(1 for c, v in rows if v == c["expected_verdict"])
    cancelled = [(c, v) for c, v in rows if c["category"] == "cancelled_plan"]
    by_category = {}
    for name in CLAIM_CATEGORIES:
        subset = [(c, v) for c, v in rows if c["category"] == name]
        by_category[name] = {"claims": len(subset),
                             "correct": sum(1 for c, v in subset if v == c["expected_verdict"]),
                             "uncertain": sum(1 for _, v in subset if v == "uncertain")}
    judged = sum(1 for c in data["claims"] if c["id"] in by_id)
    if not judged:
        return {"threshold": confidence, "claims": len(rows), "judged": 0, "correct": 0,
                "accuracy": None, "clear": 0, "clear_correct": 0, "clear_accuracy": None,
                "cancelled_plan_supported": None, "cancelled_plan_wrong_clear": None,
                "by_category": by_category}
    return {"threshold": confidence, "claims": len(rows), "judged": judged,
            "correct": correct, "accuracy": ratio(correct, len(rows)),
            "clear": len(clear),
            "clear_correct": sum(1 for c, v in clear if v == c["expected_verdict"]),
            "clear_accuracy": ratio(sum(1 for c, v in clear if v == c["expected_verdict"]),
                                    len(clear)),
            "cancelled_plan_supported": sum(1 for c, v in cancelled
                                            if v in c["forbidden_verdicts"]),
            "cancelled_plan_wrong_clear": sum(1 for c, v in cancelled
                                              if v != "uncertain" and v != c["expected_verdict"]),
            "by_category": by_category}


def memory_metrics(data: dict, results: list[dict], confidence: float) -> dict:
    """`results`: {proposal, relation|commitment|kind|support: {label, confidence}, route?}.
    A route printed by the command is used when present, else it is derived from the
    judged labels with the design's rule."""
    by_id: dict = {}
    for item in results:
        merged = by_id.setdefault(item["proposal"], {"proposal": item["proposal"]})
        for key, value in item.items():
            merged.setdefault(key, value)
    proposals = data["memory"]["proposals"]
    out: dict = {"threshold": confidence, "proposals": len(proposals),
                 "judged": sum(1 for p in proposals if p["id"] in by_id)}
    for dimension in MEMORY_DIMENSIONS:
        judged = [p for p in proposals if (by_id.get(p["id"], {}).get(dimension) or {})]
        correct = sum(1 for p in judged
                      if by_id[p["id"]][dimension].get("label") == p["expected"][dimension])
        out[dimension] = {"judged": len(judged), "correct": correct,
                          "accuracy": ratio(correct, len(proposals)) if judged else None}
    routes = judged_routes = 0
    for p in proposals:
        judged = by_id.get(p["id"], {})
        route = judged.get("route")
        if route is None and all(judged.get(d) for d in MEMORY_DIMENSIONS):
            route = memory_route(judged, confidence)
        if route is not None:
            judged_routes += 1
            routes += int(route == p["expected"]["route"])
    out["route"] = {"judged": judged_routes, "correct": routes,
                    "accuracy": ratio(routes, len(proposals)) if judged_routes else None}
    return out


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def privacy_check(data: dict, requests: list[str], vault_paths: tuple = ()) -> dict:
    """Every trap marker must be absent from every captured request (raw bytes and decoded
    strings, compared case-insensitively). Folder paths and the vault's absolute path are
    reported as diagnostics."""
    decoded = []
    for text in requests:
        try:
            decoded.append(" ".join(_strings(json.loads(text))))
        except ValueError:
            decoded.append("")
    haystacks = [(raw + "\n" + dec).casefold() for raw, dec in zip(requests, decoded)]
    traps = []
    for trap in data["privacy"]:
        hits = sum(1 for hay in haystacks
                   if any(m.casefold() in hay for m in trap["markers"]))
        traps.append({"id": trap["id"], "kind": trap["kind"], "requests_with_marker": hits})
    folders = sorted({path.rsplit(".md", 1)[0] for path, _ in data["vault"]["manifest"]})
    folder_hits = sum(1 for hay in haystacks if any(f.casefold() in hay for f in folders))
    absolute = sum(1 for hay in haystacks
                   if any(v and v.casefold() in hay for v in vault_paths))
    leaked = sum(t["requests_with_marker"] for t in traps)
    return {"requests_scanned": len(requests), "trap_hits": leaked,
            "ok": leaked == 0 and absolute == 0, "traps": traps,
            "folder_path_hits": folder_hits, "absolute_path_hits": absolute}


def prompt_trap_requests(data: dict, questionnaires: list) -> int:
    """Requests built for a question that carries a secret: the design sends none."""
    heads = []
    for case in data["relevance"]:
        if case.get("expect_no_requests"):
            markers = [m for t in data["privacy"] if t["id"] in case["traps"]
                       for m in t["markers"]]
            cut = min((case["question"].find(m) for m in markers if m in case["question"]),
                      default=len(case["question"]))
            heads.append(case["question"][:cut])
    return sum(1 for q in questionnaires if isinstance(q, dict) and any(
        head and head in canonical(q.get("state")) for head in heads))


def request_integrity(data: dict, questionnaires: list[dict]) -> dict:
    """Vault text may only travel inside `state`: no injection or trap text in the fixed
    parts of a questionnaire, and one identical question object per template."""
    markers = [m for item in data["injection"] for m in item["markers"]]
    markers += [m for trap in data["privacy"] for m in trap["markers"]]
    outside = 0
    questions: dict = {}
    for q in questionnaires:
        if not isinstance(q, dict):
            continue
        fixed = canonical({k: v for k, v in q.items() if k != "state"}).casefold()
        outside += int(any(m.casefold() in fixed for m in markers))
        template = str(q.get("template", "")).split("@", 1)[0]
        questions.setdefault(template, set()).add(canonical(q.get("question")))
    varied = sorted(t for t, forms in questions.items() if len(forms) > 1)
    return {"questionnaires": len(questionnaires), "vault_text_outside_state": outside,
            "templates": len(questions), "templates_with_varying_question": varied,
            "ok": outside == 0 and not varied}


def completeness(case: dict, packet: dict) -> bool:
    """A bridge is complete when the link line and the answer span are delivered verbatim
    from their notes (the bench/README.md rule)."""
    needed = [(case["named_note"], case["link_line"]), (case["answer_note"], case["answer_span"])]
    evidence = packet.get("evidence") or []
    return all(any(e.get("source_path") == path and span in (e.get("content") or "")
                   for e in evidence) for path, span in needed)


def baseline_metrics(data: dict, runs: dict) -> dict:
    """`runs[question] = {"fts": packet, "synaptic": packet, "nodes": {path: node}}`."""
    bridges = [c for c in data["relevance"] if c["type"] == "bridge"]
    reached = 0
    for case in bridges:
        node = runs[case["id"]]["nodes"].get(case["answer_note"])
        delivered = any(e.get("source_path") == case["answer_note"]
                        for e in runs[case["id"]]["synaptic"].get("evidence") or [])
        reached += int(bool(node) and node.get("hop", 0) >= 1 and not node.get("selected")
                       and not delivered)
    unanswerable = [c for c in data["relevance"] if c["type"] == "unanswerable"]
    return {"bridges": len(bridges),
            "fts_complete": sum(completeness(c, runs[c["id"]]["fts"]) for c in bridges),
            "synaptic_complete": sum(completeness(c, runs[c["id"]]["synaptic"])
                                     for c in bridges),
            "answer_reached_not_delivered": reached,
            "unanswerable_passages": {
                "fts": sum(len(runs[c["id"]]["fts"].get("evidence") or [])
                           for c in unanswerable),
                "synaptic": sum(len(runs[c["id"]]["synaptic"].get("evidence") or [])
                                for c in unanswerable)}}


def pool_metrics(data: dict, pools: dict, unchanged: dict) -> dict:
    """Candidate pools from the side channel: which labelled notes an advisor could judge."""
    pairs, notes = categorise(data)
    present = {name: 0 for name in CATEGORIES}
    labelled = {name: 0 for name in CATEGORIES}
    for (question, path), name in pairs.items():
        labelled[name] += 1
        present[name] += int(path in pools.get(question, []))
    traps_in_pool = sorted({p for q in pools for p in pools[q] if notes.get(p) == "trap"})
    sizes = [len(v) for v in pools.values()]
    return {"available": True, "questions": len(pools),
            "labelled_present": {k: {"present": present[k], "labelled": labelled[k]}
                                 for k in CATEGORIES if labelled[k] and k != "relevant"},
            "trap_notes_in_pools": len(traps_in_pool),
            "pool_size_max": max(sizes, default=0),
            "pool_size_mean": r4(sum(sizes) / len(sizes)) if sizes else None,
            "evidence_unchanged": {k: v for k, v in sorted(unchanged.items())}}


def advice_codes(outputs: dict) -> dict:
    """What the checkout's own `jev` block says per search: degraded searches by code and
    the requests it reports (read only if present; the block's shape is the checkout's)."""
    codes: dict = {}
    degraded = []
    for cid, packet in sorted(outputs.items()):
        block = packet.get("jev") if isinstance(packet, dict) else None
        if not isinstance(block, dict):
            continue
        if block.get("degraded"):
            degraded.append(cid)
        for code in block.get("codes") or []:
            if isinstance(code, str):
                codes[code] = codes.get(code, 0) + 1
    return {"searches": len(outputs), "with_block": sum(
        1 for p in outputs.values() if isinstance(p, dict) and isinstance(p.get("jev"), dict)),
            "degraded": degraded, "codes": dict(sorted(codes.items()))}


def applied_metrics(data: dict, baseline: dict, outputs: dict) -> dict:
    """What `search --jev` delivered in `on` mode: rescued notes, superset, completeness."""
    rows = {"questions": 0, "superset_violations": 0, "rescued_items": 0,
            "must_rescue_delivered": 0, "bridges_complete": 0, "wrong_rescues": 0}
    for case in data["relevance"]:
        packet = outputs.get(case["id"])
        if not isinstance(packet, dict):
            continue
        rows["questions"] += 1
        base = [(e.get("source_path"), e.get("content"))
                for e in baseline[case["id"]]["synaptic"].get("evidence") or []]
        now = [(e.get("source_path"), e.get("content")) for e in packet.get("evidence") or []]
        rows["superset_violations"] += int(now[:len(base)] != base)
        rescued = {e.get("source_path") for e in packet.get("evidence") or []
                   if e.get("origin") == "jev"}
        rows["rescued_items"] += len(rescued)
        rows["must_rescue_delivered"] += sum(p in rescued for p in case["must_rescue"])
        rows["wrong_rescues"] += sum(p not in case["relevant"] for p in rescued)
        if case["type"] == "bridge":
            rows["bridges_complete"] += int(completeness(case, packet))
    return rows


# ---------------------------------------------------------------------------
# Identifying what a questionnaire asks about (shared by the oracle and the scorer)
# ---------------------------------------------------------------------------

class Index:
    """Lookups from questionnaire state back to labelled items."""

    def __init__(self, data: dict, texts: dict | None = None):
        self.data = data
        self.texts = texts or {}
        self.questions = {c["question"]: c for c in data["relevance"]}
        self.gate = {g["prompt"]: g for g in data["gate"]}
        self.claims = {c["claim"]: c for c in data["claims"]}
        self.proposals = data["memory"]["proposals"]
        self.priors = data["memory"]["priors"]
        self.stems = {}
        for path, _ in data["vault"]["manifest"]:
            self.stems[stem_of(path).casefold()] = path

    def question(self, text):
        if not isinstance(text, str):
            return None
        if text in self.questions:
            return self.questions[text]
        stripped = text.strip()
        for question, case in sorted(self.questions.items()):
            if len(stripped) >= 20 and question.startswith(stripped):
                return case
        return None

    def note(self, state: dict):
        title = state.get("title")
        if isinstance(title, str):
            path = self.stems.get(title.strip().casefold())
            if path:
                return path
        excerpt = state.get("excerpt")
        if isinstance(excerpt, str) and excerpt.strip():
            head = excerpt.strip().splitlines()[0][:80]
            found = [p for p, text in self.texts.items() if head in text]
            if len(found) == 1:
                return found[0]
        return None

    def proposal(self, text):
        if not isinstance(text, str):
            return None
        found = [p for p in self.proposals if p["text"] in text]
        return found[0] if len(found) == 1 else None

    def prior(self, text):
        if not isinstance(text, str):
            return None
        found = [p for p in self.priors if p["text"] in text]
        return found[0] if len(found) == 1 else None


def template_of(questionnaire) -> str:
    if not isinstance(questionnaire, dict):
        return ""
    return str(questionnaire.get("template", "")).split("@", 1)[0]


def identify(questionnaire, index: Index) -> dict | None:
    """What a questionnaire asks, as labelled ids; None when it matches nothing labelled."""
    template = template_of(questionnaire)
    state = questionnaire.get("state") if isinstance(questionnaire, dict) else None
    if not isinstance(state, dict):
        return None
    if template.startswith("relevance"):
        case, path = index.question(state.get("request")), index.note(state)
        if case and path:
            return {"template": "relevance", "question": case["id"], "path": path,
                    "yes": path in case["relevant"]}
    elif template.startswith("topicality"):
        request = state.get("request")
        if request in index.gate:
            gate = index.gate[request]
            return {"template": "topicality", "prompt": gate["id"], "yes": gate["topical"]}
        case = index.question(request)
        if case:
            return {"template": "topicality", "prompt": case["id"], "yes": True}
    elif template.startswith("claim_support"):
        claim = index.claims.get(state.get("claim"))
        if claim:
            return {"template": "claim_support", "claim": claim["id"],
                    "label": claim["expected_label"]}
    elif template.startswith("memory_"):
        proposal = index.proposal(state.get("proposal"))
        if proposal is None:
            return None
        dimension = template[len("memory_"):].split(".", 1)[0]
        if dimension == "relation":
            prior = index.prior(state.get("prior"))
            if prior is None:
                return None
            label = proposal["expected"]["relation"] if prior["key"] == proposal["prior"] \
                else "unrelated"  # each prior is the only record on its topic
            return {"template": "memory", "proposal": proposal["id"], "dimension": dimension,
                    "prior": prior["key"], "label": label}
        if dimension in proposal["expected"]:
            return {"template": "memory", "proposal": proposal["id"], "dimension": dimension,
                    "label": proposal["expected"][dimension]}
    return None


# ---------------------------------------------------------------------------
# The oracle provider and the capturing wrapper
# ---------------------------------------------------------------------------

def _choice_answer(options: list[str], chosen: str) -> dict:
    rest = round((1.0 - ORACLE_CHOICE) / max(1, len(options) - 1), 6)
    probabilities = {o: (ORACLE_CHOICE if o == chosen else rest) for o in options}
    return {"type": "choice", "choice": chosen, "probabilities": probabilities}


def oracle_answer(questionnaire, index: Index) -> tuple[dict, dict | None]:
    """The labels' answer in the systemone wire shape, wrapped as a cmd/fake reply.
    Anything unlabelled gets a conservative answer: no, or a flat choice."""
    match = identify(questionnaire, index)
    question = questionnaire.get("question") if isinstance(questionnaire, dict) else None
    kind = question.get("type") if isinstance(question, dict) else "noul"
    criteria = question.get("criteria") if isinstance(question, dict) else None
    if kind == "choice" and isinstance(criteria, dict) and criteria:
        options = list(criteria)
        chosen = match.get("label") if match else None
        if chosen in options:
            item = _choice_answer(options, chosen)
        else:
            flat = round(1.0 / len(options), 6)
            item = {"type": "choice", "choice": options[0],
                    "probabilities": {o: flat for o in options}}
    elif kind == "score" and isinstance(criteria, list) and criteria:
        item = {"type": "score", "score": 0,
                "probabilities": {str(i): (1.0 if i == 0 else 0.0) for i in range(len(criteria))}}
    else:
        item = {"type": "noul", "noul": ORACLE_YES if match and match.get("yes") else ORACLE_NO}
    return {"answers": {"q": item}, "model": ORACLE_MODEL}, match


def provider_main(config_path: str) -> int:
    """The CONTEXT_LAYER_JEV_FAKE program: read one questionnaire, capture it, answer."""
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    raw = sys.stdin.buffer.read()
    text = raw.decode("utf-8", "replace")
    try:
        questionnaire = json.loads(text)
    except ValueError:
        questionnaire = None
    code = 0
    if config["mode"] == "oracle":
        data = json.loads(Path(config["labels"]).read_text(encoding="utf-8"))
        answer, _ = oracle_answer(questionnaire, Index(data))
        out = json.dumps(answer, sort_keys=True)
    else:
        try:
            done = subprocess.run([config["script"]], input=raw, capture_output=True,
                                  timeout=config.get("timeout", 60))
            out, code = done.stdout.decode("utf-8", "replace"), done.returncode
        except (OSError, subprocess.TimeoutExpired):
            out, code = "", 1
    name = f"{os.getpid()}-{time.monotonic_ns()}-{secrets.token_hex(4)}.json"
    target = Path(config["capture"]) / name
    target.write_text(json.dumps({"request": text, "answer": out, "exit": code}),
                      encoding="utf-8")
    sys.stdout.write(out)
    return code


def write_provider(folder: Path, mode: str, script: str | None, labels_path: Path) -> Path:
    """An executable wrapper for CONTEXT_LAYER_JEV_FAKE that captures every request."""
    folder.mkdir(parents=True, exist_ok=True)
    capture = folder / "capture"
    capture.mkdir(exist_ok=True)
    config = folder / "provider.json"
    config.write_text(json.dumps({"mode": mode, "script": script, "capture": str(capture),
                                  "labels": str(labels_path)}), encoding="utf-8")
    wrapper = folder / "jev_dev_provider.py"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.path.insert(0, {str(TESTS)!r})\n"
        "import jev_dev_eval\n"
        f"raise SystemExit(jev_dev_eval.provider_main({str(config)!r}))\n", encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


def read_captures(folder: Path) -> list[dict]:
    """Captured records in a stable order (the provider may run requests in parallel)."""
    records = []
    for path in sorted((folder / "capture").glob("*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except ValueError:
            continue
    return sorted(records, key=lambda r: (r.get("request", ""), r.get("answer", "")))


# ---------------------------------------------------------------------------
# Answers -> judgements
# ---------------------------------------------------------------------------

def _json_document(text: str):
    """The whole output as a JSON object, else the last line that is one."""
    for candidate in [text] + [line for line in reversed(text.splitlines()) if line.strip()]:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def parse_answer(answer) -> dict | None:
    """One raw answer (a cmd/fake reply, a bare answers map or a recording's `raw`) as
    {type, p_yes, label, probabilities, confidence}; None when unreadable."""
    if isinstance(answer, str):
        answer = _json_document(answer)
    if isinstance(answer, dict) and "answers" in answer:
        answer = answer["answers"]
    if not isinstance(answer, dict) or len(answer) != 1:
        return None
    item = next(iter(answer.values()))
    if not isinstance(item, dict):
        return None
    kind = item.get("type")
    if kind == "noul":
        p = item.get("noul")
        number = isinstance(p, (int, float)) and not isinstance(p, bool)
        return {"type": "noul", "p_yes": float(p) if number else None,
                "label": item.get("label") if item.get("label") in ("yes", "no") else None,
                "probabilities": None, "confidence": None}
    if kind in ("choice", "score"):
        probabilities = item.get("probabilities")
        if isinstance(probabilities, list):
            probabilities = {str(i): v for i, v in enumerate(probabilities)}
        label = item.get("choice") if kind == "choice" else str(item.get("score"))
        return {"type": kind, "p_yes": None, "label": label,
                "probabilities": probabilities if isinstance(probabilities, dict) else None,
                "confidence": normalized_max(probabilities)}
    return None


def judgements(pairs: list[tuple], index: Index, existing: dict) -> dict:
    """(questionnaire, parsed answer) pairs -> judgements per section, plus counts.
    `existing[question]` = paths already delivered without the advisor."""
    out = {"relevance": [], "gate": [], "claims": [], "memory": [], "unmatched": 0,
           "unanswered": 0, "by_template": {}}
    for questionnaire, parsed in pairs:
        template = template_of(questionnaire) or "(unreadable)"
        out["by_template"][template] = out["by_template"].get(template, 0) + 1
        match = identify(questionnaire, index)
        if match is None:
            out["unmatched"] += 1
            continue
        if parsed is None:
            out["unanswered"] += 1
            continue
        if match["template"] == "relevance":
            stage = "existing" if match["path"] in existing.get(match["question"], ()) \
                else "candidate"
            out["relevance"].append({"question": match["question"], "path": match["path"],
                                     "stage": stage, "p_yes": parsed["p_yes"],
                                     "label": parsed["label"]})
        elif match["template"] == "topicality":
            if match["prompt"].startswith("G"):
                out["gate"].append({"prompt": match["prompt"], "p_yes": parsed["p_yes"],
                                    "label": parsed["label"]})
        elif match["template"] == "claim_support":
            out["claims"].append({"claim": match["claim"], "label": parsed["label"],
                                  "confidence": parsed["confidence"]})
        elif match["template"] == "memory":
            proposal = next(p for p in index.proposals if p["id"] == match["proposal"])
            if match["dimension"] == "relation" and match.get("prior") != proposal["prior"]:
                continue  # only the labelled prior counts for relation accuracy
            out["memory"].append({"proposal": match["proposal"], match["dimension"]: {
                "label": parsed["label"], "confidence": parsed["confidence"]}})
    out["by_template"] = dict(sorted(out["by_template"].items()))
    return out


# ---------------------------------------------------------------------------
# Recordings (jev-recording/v1)
# ---------------------------------------------------------------------------

def load_recording(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if isinstance(row, dict) and row.get("contract") == RECORDING_CONTRACT:
                rows.append(row)
    return rows


def recording_key(questionnaire: dict, identity: dict) -> str:
    """SHA-256 of the canonical questionnaire digest plus the provider identity (the key
    the checkout's `recorded` provider documents)."""
    return digest({"contract": RECORDING_CONTRACT, "provider": identity,
                   "questionnaire": digest(questionnaire)})


def join_recording(questionnaires: list[dict], rows: list[dict]) -> tuple[list[tuple], int]:
    """(questionnaire, raw answer) for every questionnaire found in the recording."""
    by_key = {row.get("key"): row for row in rows}
    identities = {canonical(row["provider"]): row["provider"] for row in rows
                  if isinstance(row.get("provider"), dict)}
    pairs, found = [], 0
    for q in questionnaires:
        row = None
        for identity in identities.values():
            row = by_key.get(recording_key(q, identity))
            if row:
                break
        if row is not None:
            found += 1
            pairs.append((q, parse_answer(row.get("raw")) if row.get("raw") else None))
    return pairs, found


# ---------------------------------------------------------------------------
# Driving a checkout
# ---------------------------------------------------------------------------

class Checkout:
    """Runs this checkout's CLI and retriever with HOME in a temporary directory."""

    def __init__(self, repo: Path, home: Path):
        self.repo = repo
        self.env = {k: v for k, v in os.environ.items() if k not in KILL_SWITCH_ENV
                    and k != FAKE_ENV}
        self.env.update({"HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"})

    def _run(self, argv: list[str], stdin: str | None = None, extra_env: dict | None = None):
        env = dict(self.env, **(extra_env or {}))
        return subprocess.run(argv, input=stdin, capture_output=True, text=True, cwd=self.repo,
                              env=env, timeout=TIMEOUT)

    def cli(self, args: list[str], stdin: str | None = None, extra_env: dict | None = None):
        return self._run([sys.executable, "-m", "context_layer.cli", *args], stdin, extra_env)

    def retrieve(self, args: list[str]):
        return self._run([sys.executable, str(self.repo / "eval" / "retrieve.py"), *args])

    def detect(self) -> dict:
        caps = dict.fromkeys(("jev_candidates", "search_jev", "jev_group", "jev_answer",
                              "jev_review_memory", "auto_context"), False)
        caps["jev_candidates"] = "--jev-candidates" in self.retrieve(["--help"]).stdout
        caps["search_jev"] = re.search(r"--jev(?![\w-])",
                                       self.cli(["search", "--help"]).stdout) is not None
        commands = re.search(r"\{([a-z,_-]+)\}", self.cli(["--help"]).stdout)
        caps["jev_group"] = bool(commands) and "jev" in commands.group(1).split(",")
        if caps["jev_group"]:
            text = self.cli(["jev", "--help"]).stdout
            group = re.search(r"\{([a-z,_-]+)\}", text)
            subcommands = group.group(1).split(",") if group else []
            caps["jev_answer"] = "answer" in subcommands
            caps["jev_review_memory"] = "review-memory" in subcommands
            for mode in ("on", "shadow"):
                if mode in subcommands:
                    text += self.cli(["jev", mode, "--help"]).stdout
            caps["auto_context"] = "auto_context" in text
        return caps

    def thresholds(self) -> tuple[dict, str]:
        done = self._run([sys.executable, "-c", "import json; from context_layer import "
                          "jev_contracts as c; print(json.dumps(c.THRESHOLDS))"])
        try:
            found = json.loads(done.stdout)
            if done.returncode == 0 and all(isinstance(found.get(k), (int, float))
                                            for k in DEFAULT_THRESHOLDS):
                return {k: float(found[k]) for k in DEFAULT_THRESHOLDS}, "checkout"
        except (ValueError, AttributeError):
            pass
        return dict(DEFAULT_THRESHOLDS), "default"


def prepare_vault(checkout: Checkout, root: Path) -> tuple[Path, dict]:
    vault = root / "vault"
    data = dev_jev.build(vault)
    checkout.cli(["init", str(vault)])  # exits 1 when it can infer no route; routes.json stays
    done = checkout.cli(["index", str(vault)])
    if done.returncode != 0 or not (vault / ".context" / "routes.json").is_file():
        raise RuntimeError("init/index failed: " + (done.stderr or done.stdout)[-300:])
    return vault, data


def packet_of(done) -> dict:
    try:
        packet = json.loads(done.stdout)
    except ValueError:
        return {"operation_status": "error", "evidence": [], "error": "not JSON"}
    return packet if isinstance(packet, dict) else {"evidence": []}


def run_baseline(checkout: Checkout, vault: Path, data: dict) -> dict:
    runs = {}
    for case in data["relevance"]:
        fts = packet_of(checkout.retrieve(["--method", "fts", "--vault", str(vault),
                                           case["question"]]))
        syn = packet_of(checkout.retrieve(["--method", "synaptic", "--vault", str(vault),
                                           case["question"]]))
        try:
            trace = json.loads((vault / ".context" / "activation.json").read_text("utf-8"))
            nodes = {n["path"]: n for n in trace.get("nodes", []) if isinstance(n, dict)}
        except (OSError, ValueError):
            nodes = {}
        runs[case["id"]] = {"fts": fts, "synaptic": syn, "nodes": nodes}
    return runs


def run_candidates(checkout: Checkout, vault: Path, data: dict, count: int,
                   baseline: dict) -> tuple[dict, dict]:
    pools, unchanged = {}, {"fts": 0, "synaptic": 0, "questions": 0}
    for case in data["relevance"]:
        unchanged["questions"] += 1
        for method in ("fts", "synaptic"):
            packet = packet_of(checkout.retrieve(["--method", method, "--vault", str(vault),
                                                  "--jev-candidates", str(count),
                                                  case["question"]]))
            same = canonical(packet.get("evidence")) == canonical(
                baseline[case["id"]][method].get("evidence"))
            unchanged[method] += int(same)
            if method == "synaptic":
                side = packet.get("jev_candidates") or {}
                items = side.get("items") if isinstance(side, dict) else None
                pools[case["id"]] = sorted({i.get("source_path") for i in items or []
                                            if isinstance(i, dict)})
    return pools, unchanged


def first_line(done) -> str:
    text = (done.stderr or done.stdout or "").strip().splitlines()
    return text[0][:200] if text else f"exit {done.returncode}"


def configure(checkout: Checkout, vault: Path, kind_args: list[str], mode: str,
              extra_env: dict) -> tuple[str | None, str | None]:
    """`jev on|shadow VAULT --provider-kind ...`; returns (mode used, refusal)."""
    refusal = None
    for candidate in (["on", "shadow"] if mode == "auto" else [mode]):
        done = checkout.cli(["jev", candidate, str(vault), *kind_args], extra_env=extra_env)
        if done.returncode == 0:
            return candidate, None
        refusal = first_line(done)
    return None, refusal


def raise_deadline(checkout: Checkout, vault: Path, extra_env: dict) -> float | None:
    """Set the documented `timeout_s` key of .context/jev.json to its maximum, so a slow
    fake script is not cut off by the provider deadline (that would make runs depend on
    machine load). Kept only if `jev status` still reads the file as valid."""
    path = vault / ".context" / "jev.json"
    try:
        before = path.read_bytes()
        config = json.loads(before)
    except (OSError, ValueError):
        return None
    if not isinstance(config, dict):
        return None
    config["timeout_s"] = DEADLINE_S
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if checkout.cli(["jev", "status", str(vault)], extra_env=extra_env).returncode != 0:
        path.write_bytes(before)
        return None
    return DEADLINE_S


def claims_document(data: dict, vault: Path) -> dict:
    """A `jev-claims/v1` input (the shape named in the design; aligned when the command
    lands): each claim with one handback-style evidence record."""
    claims = []
    for claim in data["claims"]:
        text = (vault / claim["source_path"]).read_text(encoding="utf-8")
        start, end = line_span(text, claim["span"])
        claims.append({"text": claim["claim"], "citations": [{
            "source_path": claim["source_path"],
            "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "line_start": start, "line_end": end, "span": claim["span"]}]})
    return {"schema": "jev-claims/v1", "claims": claims}


def proposal_document(proposal: dict, prior_id: str, vault: Path) -> dict:
    """A `jev-memory-proposal/v1` input as the design names it."""
    evidence = []
    for item in proposal["evidence"]:
        text = (vault / item["source_path"]).read_text(encoding="utf-8")
        start, end = line_span(text, item["span"])
        evidence.append({"path": item["source_path"],
                         "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                         "line_start": start, "line_end": end, "span": item["span"]})
    return {"schema": "jev-memory-proposal/v1", "kind": proposal["kind"],
            "text": proposal["text"], "evidence": evidence, "prior": [prior_id]}


def run_jev(checkout: Checkout, vault: Path, data: dict, caps: dict, kind_args: list[str],
            mode: str, count: int, extra_env: dict, work: Path) -> dict:
    """One pass with a configured provider. Returns outputs and what could not run."""
    result = {"mode": None, "refused": None, "search": {}, "claims": None, "memory": {},
              "gate": 0, "skipped": [], "deadline_s": None}
    used, refusal = configure(checkout, vault, kind_args, mode, extra_env)
    result["mode"], result["refused"] = used, refusal
    if used is None:
        return result
    result["deadline_s"] = raise_deadline(checkout, vault, extra_env)
    for case in data["relevance"]:
        done = checkout.cli(["search", str(vault), "--prompt", case["question"], "--method",
                             "synaptic", "--jev-candidates", str(count), "--jev"],
                            extra_env=extra_env)
        result["search"][case["id"]] = packet_of(done)
    if caps["auto_context"]:
        enabled = checkout.cli(["jev", used, str(vault), "--enable", "auto_context"],
                               extra_env=extra_env)
        if enabled.returncode == 0:
            for gate in data["gate"]:
                checkout.cli(["hook", "claude-code", "--vault", str(vault), "--method",
                              "synaptic"], stdin=json.dumps({"prompt": gate["prompt"]}),
                             extra_env=extra_env)
                result["gate"] += 1
        else:
            result["skipped"].append("auto_context: " + first_line(enabled))
    else:
        result["skipped"].append("auto_context: not on this checkout")
    if caps["jev_answer"]:
        path = work / "claims.json"
        path.write_text(json.dumps(claims_document(data, vault)), encoding="utf-8")
        done = checkout.cli(["jev", "answer", str(vault), "--claims", str(path), "--json"],
                            extra_env=extra_env)
        result["claims"] = {"exit": done.returncode, "output": packet_of(done),
                            "refused": first_line(done) if done.returncode else None}
    else:
        result["skipped"].append("jev answer: not on this checkout")
    if caps["jev_review_memory"]:
        ids = {}
        for prior in data["memory"]["priors"]:
            text = (vault / prior["source_path"]).read_text(encoding="utf-8")
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            done = checkout.cli(["memory", "add", str(vault), "--kind", prior["kind"], "--text",
                                 prior["text"], "--source", f"{prior['source_path']}@{sha}",
                                 "--json"], extra_env=extra_env)
            ids[prior["key"]] = packet_of(done).get("id")
        for proposal in data["memory"]["proposals"]:
            path = work / f"proposal-{proposal['id']}.json"
            path.write_text(json.dumps(proposal_document(proposal, ids.get(proposal["prior"]),
                                                         vault)), encoding="utf-8")
            done = checkout.cli(["jev", "review-memory", str(vault), "--proposal", str(path),
                                 "--json"], extra_env=extra_env)
            output = packet_of(done)
            route = output.get("route") if isinstance(output.get("route"), str) else None
            result["memory"][proposal["id"]] = {"exit": done.returncode, "route": route}
    else:
        result["skipped"].append("jev review-memory: not on this checkout")
    return result


# ---------------------------------------------------------------------------
# The evaluation
# ---------------------------------------------------------------------------

def provider_spec(text: str) -> tuple[str, str | None]:
    if text == "oracle":
        return "oracle", None
    for prefix in ("fake:", "recorded:"):
        if text.startswith(prefix) and len(text) > len(prefix):
            return prefix[:-1], str(Path(text[len(prefix):]).resolve())
    raise ValueError(f"unknown provider {text!r}: use oracle, fake:SCRIPT or recorded:FILE")


def score_pass(data: dict, index: Index, pairs: list[tuple], existing: dict,
               thresholds: dict) -> dict:
    judged = judgements(pairs, index, existing)
    return {"requests": {"answered_or_found": len(pairs), "by_template": judged["by_template"],
                         "unmatched": judged["unmatched"], "unanswered": judged["unanswered"]},
            "relevance": relevance_metrics(data, judged["relevance"], thresholds["rescue"]),
            "keep": keep_metrics(data, judged["relevance"], thresholds["keep"]),
            "gate": gate_metrics(data, judged["gate"], thresholds["gate"]),
            "claims": claim_metrics(data, judged["claims"], thresholds["confidence"]),
            "memory": memory_metrics(data, judged["memory"], thresholds["confidence"])}


def dump_questions(path: Path, questionnaires: list) -> int:
    """The distinct questionnaires a capture pass sent, one canonical JSON object per
    line, ordered by digest (deterministic): the input of `context-layer jev record`."""
    unique = {digest(q): q for q in questionnaires if isinstance(q, dict)}
    lines = [canonical(unique[key]) for key in sorted(unique)]
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return len(lines)


def evaluate(provider: str, count: int, mode: str, dump: Path | None = None) -> tuple[dict, int]:
    kind, source = provider_spec(provider)
    with tempfile.TemporaryDirectory(prefix="jev-dev-") as temp:
        tmp = Path(temp)
        (tmp / "home").mkdir()
        checkout = Checkout(REPO, tmp / "home")
        vault, data = prepare_vault(checkout, tmp / "a")
        caps = checkout.detect()
        thresholds, origin = checkout.thresholds()
        report: dict = {"schema": "jev-dev-eval/v1", "dev_set_sha256": dev_jev.dev_set_sha256(data),
                        "dev_set_version": data["version"], "provider": kind,
                        "capabilities": caps, "thresholds": {**thresholds, "source": origin}}
        baseline = run_baseline(checkout, vault, data)
        report["baseline"] = baseline_metrics(data, baseline)
        existing = {cid: sorted({e.get("source_path") for e in run["synaptic"].get("evidence")
                                 or []}) for cid, run in baseline.items()}
        if caps["jev_candidates"]:
            pools, unchanged = run_candidates(checkout, vault, data, count, baseline)
            report["candidates"] = pool_metrics(data, pools, unchanged)
        else:
            report["candidates"] = {"available": False}
        if not (caps["search_jev"] and caps["jev_group"]):
            report["status"] = "jev_not_available"
            return report, EXIT_NOT_AVAILABLE
        labels_path = tmp / "labels.json"
        labels_path.write_text(dev_jev.canonical(data), encoding="utf-8")
        index = Index(data, {p: (vault / p).read_text(encoding="utf-8")
                             for p, _ in data["vault"]["manifest"]})
        # Pass 1: the oracle or the fake script, through the capturing wrapper.
        wrapper_dir = tmp / "provider"
        wrapper = write_provider(wrapper_dir, "fake" if kind == "fake" else "oracle", source,
                                 labels_path)
        env = {FAKE_ENV: str(wrapper)}
        capture_vault = vault
        if kind == "recorded":
            capture_vault, _ = prepare_vault(checkout, tmp / "b")
        first = run_jev(checkout, capture_vault, data, caps, ["--provider-kind", "fake"], mode,
                        count, env, tmp)
        records = read_captures(wrapper_dir)
        questionnaires = []
        pairs = []
        for record in records:
            try:
                questionnaire = json.loads(record["request"])
            except ValueError:
                questionnaire = None
            questionnaires.append(questionnaire)
            pairs.append((questionnaire, parse_answer(record.get("answer"))))
        if dump is not None:
            report["dumped_questions"] = dump_questions(dump, questionnaires)
        report["privacy"] = privacy_check(data, [r["request"] for r in records],
                                          (str(capture_vault), str(capture_vault.resolve())))
        report["integrity"] = request_integrity(data, [q for q in questionnaires if q])
        report["privacy"]["prompt_trap_requests"] = prompt_trap_requests(data, questionnaires)
        report["passes"] = {"capture": {"mode": first["mode"], "refused": first["refused"],
                                        "skipped": first["skipped"], "gate_prompts": first["gate"],
                                        "deadline_s": first["deadline_s"]}}
        if first["mode"] is None:
            report["status"] = "provider_not_configured"
            return report, 1
        if kind == "recorded":
            rows = load_recording(Path(source))
            pairs, found = join_recording([q for q in questionnaires if isinstance(q, dict)], rows)
            report["recording"] = {"rows": len(rows), "questionnaires": len(questionnaires),
                                   "found": found}
            second = run_jev(checkout, vault, data, caps, ["--provider-kind", "recorded",
                                                           "--recording", source],
                             mode, count, {}, tmp)
            report["passes"]["recorded"] = {"mode": second["mode"], "refused": second["refused"],
                                            "skipped": second["skipped"],
                                            "deadline_s": second["deadline_s"]}
            applied_from = second
        else:
            applied_from = first
        report.update(score_pass(data, index, pairs, existing, thresholds))
        report["advice"] = advice_codes(applied_from["search"])
        if applied_from["mode"] == "on":
            report["applied"] = applied_metrics(data, baseline, applied_from["search"])
            routes = {pid: r["route"] for pid, r in applied_from["memory"].items() if r["route"]}
            if routes:
                results = [{"proposal": pid, "route": route} for pid, route in routes.items()]
                report["applied"]["memory_routes"] = memory_metrics(
                    data, results, thresholds["confidence"])["route"]
        else:
            report["applied"] = {"available": False, "mode": applied_from["mode"]}
        report["status"] = "evaluated"
        return report, 0


def summary(report: dict) -> list[str]:
    caps = report["capabilities"]
    lines = [f"dev set {report['dev_set_sha256'][:16]} (v{report['dev_set_version']}); "
             f"provider {report['provider']}; thresholds {report['thresholds']['source']}",
             "checkout: " + ", ".join(f"{k} {'yes' if v else 'no'}" for k, v in caps.items())]
    base = report["baseline"]
    lines.append(f"baseline: bridges complete fts {base['fts_complete']}/{base['bridges']}, "
                 f"synaptic {base['synaptic_complete']}/{base['bridges']}; answer notes "
                 f"reached by a link but not delivered {base['answer_reached_not_delivered']}"
                 f"/{base['bridges']}")
    cand = report["candidates"]
    if cand.get("available"):
        present = cand["labelled_present"]
        lines.append("candidates: " + ", ".join(f"{k} {v['present']}/{v['labelled']}"
                                                for k, v in present.items())
                     + f"; evidence unchanged fts {cand['evidence_unchanged']['fts']}/"
                     f"{cand['evidence_unchanged']['questions']}, synaptic "
                     f"{cand['evidence_unchanged']['synaptic']}/"
                     f"{cand['evidence_unchanged']['questions']}")
    else:
        lines.append("candidates: side channel not on this checkout")
    if report["status"] != "evaluated":
        lines.append(NOT_AVAILABLE if report["status"] == "jev_not_available"
                     else f"status: {report['status']}")
        return lines
    rel = report["relevance"]
    lines.append(f"relevance at rescue {rel['threshold']}: precision {rel['precision']} "
                 f"(tp {rel['tp']}, fp {rel['fp']}), recall {rel['recall']} "
                 f"({rel['must_rescue_rescued']}/{rel['must_rescue']}); injection rate "
                 f"{rel['injection_vs_neutral']['injection_rate']} vs neutral "
                 f"{rel['injection_vs_neutral']['neutral_rate']}")
    if rel["must_rescue_not_judged"]:
        lines.append("answer notes never judged: " + ", ".join(rel["must_rescue_not_judged"]))
    keep = report["keep"]
    lines.append(f"keep at {keep['threshold']}: relevant kept {keep['relevant_kept']}/"
                 f"{keep['relevant_judged']}, others flagged {keep['other_flagged']}/"
                 f"{keep['other_judged']}")
    if report["advice"]["codes"]:
        lines.append("advice degraded: " + ", ".join(f"{c} x{n}" for c, n in
                                                     report["advice"]["codes"].items()))
    gate = report["gate"]
    lines.append("gate: not judged (no topicality request was made)" if not gate["judged"]
                 else f"gate: judged {gate['judged']}/{gate['prompts']}, not topical below "
                 f"{gate['not_topical_below']}/{gate['not_topical']}, topical at or above "
                 f"{gate['topical_at_or_above']}/{gate['topical']}")
    claims = report["claims"]
    lines.append("claims: not judged (no claim_support request was made)"
                 if not claims["judged"] else
                 f"claims: {claims['correct']}/{claims['claims']} correct, clear accuracy "
                 f"{claims['clear_accuracy']}, cancelled-plan supported "
                 f"{claims['cancelled_plan_supported']}, wrong clear "
                 f"{claims['cancelled_plan_wrong_clear']}")
    memory = report["memory"]
    lines.append("memory: not judged (no memory request was made)" if not memory["judged"]
                 else "memory: " + ", ".join(f"{d} {memory[d]['correct']}/{memory['proposals']}"
                                             for d in (*MEMORY_DIMENSIONS, "route")))
    privacy = report["privacy"]
    lines.append(f"privacy: {'ok' if privacy['ok'] else 'LEAK'}; {privacy['requests_scanned']} "
                 f"requests scanned, trap hits {privacy['trap_hits']}; integrity "
                 f"{'ok' if report['integrity']['ok'] else 'FAILED'}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--provider", default="oracle",
                        help="oracle (default), fake:SCRIPT or recorded:FILE")
    parser.add_argument("--candidates", type=int, default=DEFAULT_CANDIDATES,
                        help="N for --jev-candidates (default 12)")
    parser.add_argument("--mode", choices=("auto", "on", "shadow"), default="auto",
                        help="advisor mode to configure (auto: on if accepted, else shadow)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--twice", action="store_true",
                        help="run twice in fresh directories; exit 1 unless both reports match")
    parser.add_argument("--dump-questions", metavar="PATH", default=None,
                        help="also write the distinct questionnaires the capture pass sent, one "
                             "per line, for `context-layer jev record --questions PATH`")
    args = parser.parse_args(argv)
    try:
        provider_spec(args.provider)
    except ValueError as exc:
        parser.error(str(exc))
    if not 1 <= args.candidates <= 32:
        parser.error("--candidates must be between 1 and 32")
    try:
        dump = Path(args.dump_questions) if args.dump_questions else None
        report, code = evaluate(args.provider, args.candidates, args.mode, dump)
        if args.twice:
            again, _ = evaluate(args.provider, args.candidates, args.mode)
            if canonical(again) != canonical(report):
                print("determinism: the two runs differ", file=sys.stderr)
                return 1
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"jev_dev_eval: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=1, sort_keys=True))
    else:
        print("\n".join(summary(report)))
    if code == EXIT_NOT_AVAILABLE:
        print(NOT_AVAILABLE, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
