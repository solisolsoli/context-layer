"""Deterministic checks for claims with cited vault passages.

This module checks whether each citation points to an unchanged, verbatim source
span. It does not decide whether that passage supports the claim's meaning.
"""

from __future__ import annotations

from pathlib import Path
import re

from . import orchestrate


CLAIMS_SCHEMA = "context-layer-claims/v1"
REPORT_SCHEMA = "context-layer-claim-check/v1"
CLAIMS_MAX = 20
CITATIONS_MAX = 8
CLAIM_CHARS = 2000
CLAIM_SPAN_INPUT_CHARS = 20000
CLAIM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SHA = re.compile(r"[0-9a-f]{64}")
CITATION_KEYS = ("source_path", "source_sha256", "line_start", "line_end", "span")


class Refused(ValueError):
    """An invalid claim document or an unusable vault boundary."""


def parse_claims(data) -> list[dict]:
    """Validate a bounded claims document without reading any source files."""
    if not isinstance(data, dict) or data.get("schema", CLAIMS_SCHEMA) != CLAIMS_SCHEMA \
            or set(data) - {"schema", "claims"} or not isinstance(data.get("claims"), list):
        raise Refused(f"expected a {CLAIMS_SCHEMA} object with claims")
    claims = data["claims"]
    if not 1 <= len(claims) <= CLAIMS_MAX:
        raise Refused(f"claims must hold 1 to {CLAIMS_MAX} items, not {len(claims)}")
    out = []
    for number, claim in enumerate(claims, 1):
        where = f"claim {number}"
        if not isinstance(claim, dict) or not {"text", "citations"} <= set(claim) \
                or set(claim) - {"text", "citations", "id"}:
            raise Refused(f"{where} must have text and citations, and optionally id")
        value = claim["text"]
        if not isinstance(value, str) or not value.strip() or len(value) > CLAIM_CHARS:
            raise Refused(f"{where}: text must be 1 to {CLAIM_CHARS} characters")
        ident = claim.get("id")
        if ident is not None and (not isinstance(ident, str) or not CLAIM_ID.fullmatch(ident)):
            raise Refused(f"{where}: id must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}")
        citations = claim["citations"]
        if not isinstance(citations, list) or not 1 <= len(citations) <= CITATIONS_MAX:
            raise Refused(f"{where}: citations must hold 1 to {CITATIONS_MAX} items")
        clean = []
        for position, cite in enumerate(citations, 1):
            spot = f"{where}, citation {position}"
            if not isinstance(cite, dict) or set(cite) != set(CITATION_KEYS):
                raise Refused(f"{spot} must have exactly {', '.join(CITATION_KEYS)}")
            name = cite["source_path"]
            if not isinstance(name, str) or not name or len(name) > 1024:
                raise Refused(f"{spot}: source_path must be a vault-relative path")
            digest = cite["source_sha256"]
            if not isinstance(digest, str) or not SHA.fullmatch(digest):
                raise Refused(f"{spot}: source_sha256 must be 64 lowercase hex characters")
            for key in ("line_start", "line_end"):
                item = cite[key]
                if isinstance(item, bool) or not isinstance(item, int) or item < 1:
                    raise Refused(f"{spot}: {key} must be a positive integer")
            if cite["line_start"] > cite["line_end"]:
                raise Refused(f"{spot}: line_start is after line_end")
            span = cite["span"]
            if not isinstance(span, str) or not span.strip() \
                    or len(span) > CLAIM_SPAN_INPUT_CHARS:
                raise Refused(f"{spot}: span must be 1 to {CLAIM_SPAN_INPUT_CHARS} characters")
            clean.append(dict(cite))
        out.append({"text": value, "id": ident, "citations": clean})
    return out


def claims_report(vault, claims: list[dict]) -> dict:
    """Report mechanical citation checks for claims returned by parse_claims.

    A checked citation proves only source identity and exact span placement. The
    separate anchor notes flag hard tokens in the claim absent from the quote;
    these notes do not change the mechanical result.
    """
    vault = Path(vault).resolve()
    try:
        prefixes = orchestrate._prefixes(vault)
    except orchestrate.OrchestrateError as exc:
        raise Refused(str(exc)) from None
    cache: dict = {}
    entries = []
    checked = 0
    for number, claim in enumerate(claims, 1):
        cites = []
        for position, cite in enumerate(claim["citations"], 1):
            record = {"id": f"c{number}.{position}", "observation": cite["span"],
                      "source_path": cite["source_path"],
                      "source_sha256": cite["source_sha256"],
                      "line_start": cite["line_start"], "line_end": cite["line_end"],
                      "span": cite["span"], "method": "", "uncertainty": ""}
            detail = orchestrate.check_record_detail(vault, record, ["."], prefixes,
                                                     None, cache)
            valid = detail["mechanically_checked"]
            checked += int(valid)
            cites.append({"source_path": cite["source_path"],
                          "line_start": cite["line_start"],
                          "line_end": cite["line_end"],
                          "mechanically_checked": valid,
                          "reasons": detail["reasons"],
                          "anchor_notes": orchestrate.claim_problems(claim["text"], cite["span"])
                          if valid else []})
        entry = {"claim": number, "citations": cites}
        if claim.get("id"):
            entry["id"] = claim["id"]
        entries.append(entry)
    return {"schema": REPORT_SCHEMA, "claims": entries,
            "citations": sum(len(entry["citations"]) for entry in entries),
            "mechanically_checked": checked,
            "approved": False, "memory_written": False, "rewrites": False,
            "meaning": "mechanically_checked = the citation is a verbatim span at the "
                       "cited lines of the current file inside the vault's boundaries; "
                       "it does not make the claim true. anchor_notes lists numbers, "
                       "dates and names the claim asserts that the quote does not carry."}


def check_claims(vault, data) -> dict:
    """Validate a claims document and check all its citations."""
    return claims_report(vault, parse_claims(data))
