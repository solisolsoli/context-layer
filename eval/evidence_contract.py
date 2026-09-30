"""Exact evidence-delivery checks against frozen UTF-8 sources and span labels.

This measures delivery, not truth, relevance, answer quality or release approval.
The labels must be frozen independently of the system being compared.
"""
from __future__ import annotations

import hashlib
import json


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_contract(contract):
    """Reject incomplete labels instead of silently treating them as misses."""
    if contract.get("schema") != "evidence-contract-v1":
        raise ValueError("Expected evidence-contract-v1")
    sources = contract["sources"]
    if not isinstance(sources, dict) or not isinstance(contract["queries"], dict):
        raise ValueError("sources and queries must be objects")
    for path, source in sources.items():
        if not path or not isinstance(source["text"], str) or source["sha256"] != digest(source["text"]):
            raise ValueError(f"Frozen source digest mismatch: {path}")
    for query_id, query in contract["queries"].items():
        groups = query["required_groups"]
        if not isinstance(groups, list) or type(query["answerable"]) is not bool:
            raise ValueError(f"Invalid query label: {query_id}")
        if query["answerable"] != bool(groups):
            raise ValueError(f"Answerability and requirements disagree: {query_id}")
        for alternatives in groups:
            if not isinstance(alternatives, list) or not alternatives:
                raise ValueError(f"Empty required group: {query_id}")
            for witness in alternatives:
                source = sources[witness["source_path"]]
                span = witness["required_text"]
                if (not isinstance(span, str) or not span or span not in source["text"]
                        or witness["source_sha256"] != source["sha256"]):
                    raise ValueError(f"Invalid required span: {query_id}")
    return contract


def validate_contract(contract):
    """Expose malformed labels as one consistent input error."""
    try:
        return _validate_contract(contract)
    except (AttributeError, KeyError, TypeError) as exc:
        raise ValueError(f"Malformed evidence contract: {exc}") from exc


def score_delivery(contract, query_id, stdout, *, command_ok=True):
    """Score actual consumer JSON; metadata, omitted paths and claims get no credit.

    Each required group is AND; its explicitly labelled alternatives are OR.
    A delivered excerpt must be a contiguous, unmodified span of its exact
    frozen source version. All supplied evidence is checked, including extras.
    An answerable abstention is a miss. A labelled unanswerable abstention is
    reported separately and does not inflate evidence recall.
    """
    query = contract["queries"][query_id]
    groups = query["required_groups"]
    errors = []
    valid = []
    abstained = False
    if not command_ok:
        errors.append("command_failed")
    try:
        packet = json.loads(stdout)
        if not isinstance(packet, dict) or packet.get("schema") != "evidence-delivery-v1":
            raise ValueError("invalid delivery schema")
        if packet.get("operation_status") != "ok":
            raise ValueError("operational failure")
        status = packet["status"]
        if status not in {"SUPPORTED", "USER_STATED", "PARTIAL", "EXTERNAL_RECHECK", "NOT_FOUND", "ABSTAINED"}:
            raise ValueError("invalid evidence status")
        evidence = packet["evidence"]
        if not isinstance(evidence, list):
            raise ValueError("evidence must be an array")
        abstained = status in {"NOT_FOUND", "ABSTAINED"}
        if abstained and evidence:
            raise ValueError("abstention includes evidence")
        for item in evidence:
            source = contract["sources"].get(item["source_path"])
            content = item["content"]
            if (source is None or not isinstance(content, str) or not content
                    or item["source_sha256"] != source["sha256"]
                    or content.encode("utf-8") not in source["text"].encode("utf-8")):
                raise ValueError("unknown, altered or wrong-version evidence")
            valid.append(item)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        errors.append(str(exc))
    hits = []
    for alternatives in groups:
        hit = not errors and not abstained and any(
            item["source_path"] == witness["source_path"]
            and item["source_sha256"] == witness["source_sha256"]
            and witness["required_text"].encode("utf-8") in item["content"].encode("utf-8")
            for witness in alternatives for item in valid
        )
        hits.append(bool(hit))
    safe_abstention = abstained and not errors
    correct_abstention = safe_abstention and not query["answerable"]
    delivery_pass = bool(groups) and all(hits) and not errors
    return {"hit_count": sum(hits), "expected_count": len(groups), "group_hits": hits,
            "delivery_pass": delivery_pass, "safe_abstention": safe_abstention,
            "correct_abstention": correct_abstention,
            "case_pass": delivery_pass or correct_abstention, "errors": errors}
