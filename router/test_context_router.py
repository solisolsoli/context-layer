#!/usr/bin/env python3
"""Regression tests for prompt routing and evidence retrieval.

Runs against the example vault in `example-vault/`. Build the index first:

    python3 build_index.py --vault example-vault
    python3 test_context_router.py
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile


HERE = Path(__file__).resolve().parent
VAULT = HERE / "example-vault"
INDEX = VAULT / ".context" / "index.sqlite"
CONFIG_PATH = VAULT / ".context" / "routes.json"
FACTS_PATH = VAULT / ".context" / "facts.json"

SPEC = importlib.util.spec_from_file_location("context_router", HERE / "context_router.py")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


FIXTURES = [
    {
        "name": "writing_standards_summary_line",
        "prompt": "What is the summary line character limit in the house style?",
        "routes": {"writing-standards"},
        "canonical": {"writing-standards.md"},
    },
    {
        "name": "project_history_decision",
        "prompt": "When did we decide the decision log rule about publishing?",
        "routes": {"project-history"},
        "canonical": {"project-history.md"},
    },
    {
        "name": "reference_glossary",
        "prompt": "What is the glossary definition of a canonical source?",
        "routes": {"reference-docs"},
        "canonical": {"reference-docs/glossary.md"},
    },
]


def check(condition: bool, message: str, failures: "list[str]") -> None:
    if not condition:
        failures.append(message)


def main() -> int:
    failures: "list[str]" = []
    results = []

    if not INDEX.is_file():
        print(json.dumps({
            "passed": False,
            "failures": [f"index missing: {INDEX}; run build_index.py --vault example-vault"],
        }, indent=2))
        return 1

    config = json.loads(CONFIG_PATH.read_text())
    connection = sqlite3.connect(f"file:{INDEX}?mode=ro", uri=True)

    for fixture in FIXTURES:
        parsed = MODULE.parse_prompt(fixture["prompt"], config)
        ranked, _, errors, canonical_issues = MODULE.retrieve(VAULT, connection, config, parsed)
        selected = MODULE.choose_records(ranked, parsed, 14, config)
        top10 = selected[:10]
        paths = {c.source_path for c in top10}
        check(fixture["routes"].issubset(set(parsed["routes"])),
              f"{fixture['name']}: route mismatch {parsed['routes']}", failures)
        check(fixture["canonical"].issubset(paths),
              f"{fixture['name']}: canonical recall@10 failed ({sorted(paths)})", failures)
        check(not errors, f"{fixture['name']}: FTS errors {errors}", failures)
        check(not canonical_issues, f"{fixture['name']}: canonical issues {canonical_issues}", failures)
        check(all(c.record_type in set(config["record_type_allowlist"]) for c in top10),
              f"{fixture['name']}: record outside the allowlist entered top10", failures)
        check(not any(MODULE.is_derived_text_mirror(config, c.source_path) for c in top10),
              f"{fixture['name']}: derived text mirror entered top10", failures)
        check(not any(MODULE.is_excluded_prefix(config, c.source_path) for c in top10),
              f"{fixture['name']}: excluded lab artifact entered top10", failures)
        unique_bodies = {MODULE.semantic_hash(c.content) for c in top10}
        check(len(unique_bodies) == len(top10),
              f"{fixture['name']}: duplicate bodies survived semantic_hash dedup", failures)
        for candidate in top10:
            if candidate.row_id < 0:
                continue  # synthetic whole-file canonical read, not an index row
            check(MODULE.sha256_bytes(candidate.content.encode("utf-8")) == candidate.content_sha256,
                  f"{fixture['name']}: indexed content hash mismatch at {candidate.locator}", failures)
        results.append({
            "name": fixture["name"],
            "routes": parsed["routes"],
            "selected": len(selected),
            "canonical_recall": len(fixture["canonical"] & paths),
            "canonical_required": len(fixture["canonical"]),
        })

    # --- Evidence gate categories -------------------------------------------
    current = MODULE.parse_prompt(
        "Is the glossary definition of canonical source still current policy today?", config)
    check(current["web_revalidation_required"],
          "external fact: revalidation flag missing", failures)
    check(MODULE.determine_status([], [], current, [])[0] == "EXTERNAL_RECHECK",
          "external fact: wrong status", failures)

    vague = MODULE.parse_prompt("remember what we discussed earlier", config)
    check(vague["needs_clarification"], "vague request: abstention flag missing", failures)
    vague_ranked, _, _, _ = MODULE.retrieve(VAULT, connection, config, vague)
    vague_selected = MODULE.choose_records(vague_ranked, vague, 14, config)
    check(all(c.mandatory or c.historical for c in vague_selected),
          "vague request: lexical noise entered packet", failures)
    check(MODULE.determine_status([], [], vague, [])[0] == "NOT_FOUND",
          "vague request: wrong status", failures)

    unknown = MODULE.parse_prompt("Zzzquux Frobulator 99173, what is it?", config)
    unknown_ranked, _, _, _ = MODULE.retrieve(VAULT, connection, config, unknown)
    unknown_selected = MODULE.choose_records(unknown_ranked, unknown, 14, config)
    check(not unknown_selected, "unknown entity: unrelated records retrieved", failures)
    check(MODULE.determine_status(unknown_selected, [], unknown, [])[0] == "NOT_FOUND",
          "unknown entity: wrong status", failures)

    # A recorded continuation carries no anchor; it must abstain, not sweep.
    for text in ("continue", "keep going", "shorter", "approved"):
        continuation = MODULE.parse_prompt(text, config)
        check(continuation["contextless_continuation"],
              f"continuation {text!r}: not detected as contextless", failures)
        ranked, _, _, _ = MODULE.retrieve(VAULT, connection, config, continuation)
        selected = MODULE.choose_records(ranked, continuation, 14, config)
        check(not selected, f"continuation {text!r}: lexical sweep entered the packet", failures)
        check(MODULE.determine_status(selected, [], continuation, [])[0] == "NOT_FOUND",
              f"continuation {text!r}: wrong status", failures)

    # --- Suffix tolerance and identity suppression ---------------------------
    stem = config.get("stem_suffix_tolerance", {})
    check(MODULE.contains_phrase(MODULE.normalize("apply the standardised rules"), "standard", stem),
          "suffix tolerance failed on an inflected stem", failures)
    check(not MODULE.contains_phrase(MODULE.normalize("the cats are fine"), "cat", stem),
          "short stem suffix tolerance leaked a route match", failures)
    check(not MODULE.contains_phrase(MODULE.normalize("styles and fonts"), "style", stem),
          "blocklisted stem matched through suffix tolerance", failures)

    adverb = MODULE.parse_prompt("Totally understood?", config)
    check(not adverb["exact_phrases"],
          f"sentence-opening adverb treated as identity: {adverb['exact_phrases']}", failures)
    check(adverb["needs_clarification"],
          "sentence-opening adverb: abstention flag missing", failures)
    check(MODULE.parse_prompt("Zzzquux Frobulator, what is it?", config)["exact_phrases"],
          "identity suppression went too far and dropped a real name", failures)

    # --- Configurable stopwords ----------------------------------------------
    check("frobulator" in MODULE.tokens("frobulator settings"),
          "a word that is not a default stopword was dropped", failures)
    extra = dict(config, stopwords=["Frobulator"])
    check("frobulator" not in MODULE.parse_prompt("frobulator settings", extra)["tokens"],
          "a configured stopword still reached the prompt tokens", failures)
    check("frobulator" in MODULE.parse_prompt("frobulator settings", config)["tokens"],
          "configured stopwords leaked into the next config", failures)
    try:
        MODULE.parse_prompt("anything", dict(config, stopwords="frobulator"))
        check(False, "a non-list stopwords value was accepted", failures)
    except ValueError:
        pass

    # --- Activation budget ---------------------------------------------------
    abstain_budget = MODULE.activation_budget(MODULE.parse_prompt("continue", config), "continue")
    short_prompt = "summary line length limit"
    brief_budget = MODULE.activation_budget(MODULE.parse_prompt(short_prompt, config), short_prompt)
    long_prompt = "What is the summary line character limit in the house style? " * 12
    full_budget = MODULE.activation_budget(MODULE.parse_prompt(long_prompt, config), long_prompt)
    check(abstain_budget["tier"] == "abstain" and abstain_budget["max_context_chars"] == 0,
          "abstain budget is not zero", failures)
    check(brief_budget["max_context_chars"] < full_budget["max_context_chars"],
          "brief budget is not smaller than the full budget", failures)
    check(brief_budget["canonical_sections"] and not full_budget["canonical_sections"],
          "canonical section narrowing is not tied to the tier", failures)

    # --- Answer cards --------------------------------------------------------
    cards = MODULE.load_fact_cards(VAULT, FACTS_PATH)
    declared = json.loads(FACTS_PATH.read_text())["cards"]
    check(bool(cards), "no verified answer card loaded", failures)
    check(len(cards) == len(declared),
          f"answer card verification dropped {len(declared) - len(cards)} card(s); check the quotes",
          failures)
    with tempfile.TemporaryDirectory() as temporary:
        fake = Path(temporary)
        (fake / "facts.json").write_text(json.dumps({"cards": [{
            "id": "bogus", "answer": "x", "terms": ["x"], "evidence_grade": "SUPPORTED",
            "path": "writing-standards.md",
            "quote": "this sentence is not in any source",
        }]}))
        check(not MODULE.load_fact_cards(VAULT, fake / "facts.json"),
              "unverifiable answer card was not dropped", failures)

    fast_rule = MODULE.fast_activation(
        short_prompt, MODULE.parse_prompt(short_prompt, config), config, VAULT, FACTS_PATH)
    check(fast_rule is not None
          and any(card["id"] == "summary-line-length" for card in fast_rule["cards"]),
          "fast path missed the summary-line rule card", failures)
    if fast_rule:
        check(all("40 to 80" in card["quote"] or card["id"] != "summary-line-length"
                  for card in fast_rule["cards"]),
              "summary-line card quote no longer carries the current limit", failures)
    check(MODULE.fast_activation("continue", MODULE.parse_prompt("continue", config),
                                 config, VAULT, FACTS_PATH) is None,
          "fast path fired on a contextless prompt", failures)

    # --- Canonical narrowing -------------------------------------------------
    canonical_path = "writing-standards.md"
    row = connection.execute(
        "SELECT id,source_path,source_sha256,source_format,record_type,locator,timestamp,"
        "role,content_sha256,content FROM records WHERE source_path=? LIMIT 1",
        (canonical_path,)).fetchone()
    check(row is not None, "canonical narrowing fixture missing from the index", failures)
    if row:
        candidate = MODULE.load_candidate(row)
        candidate.mandatory = True
        anchors = MODULE.parse_prompt(short_prompt, config)["tokens"]
        with tempfile.TemporaryDirectory() as temporary:
            narrow, narrow_scope, _, _, _ = MODULE.materialize_candidate(
                VAULT, candidate, anchors, max_per_source=300,
                overflow_dir=Path(temporary), canonical_sections=True)
            wide, _, _, _, _ = MODULE.materialize_candidate(
                VAULT, candidate, anchors, max_per_source=24000,
                overflow_dir=Path(temporary), canonical_sections=False)
        check(len(narrow) < len(wide),
              f"canonical narrowing did not reduce inlined size ({len(narrow)} vs {len(wide)})",
              failures)
        check(narrow_scope.startswith("canonical_"),
              f"canonical narrowing reported an unexpected scope {narrow_scope}", failures)
        check(narrow in wide, "canonical narrowing emitted text that is not verbatim in the source",
              failures)

    # --- Labelled historical access -----------------------------------------
    def fake_candidate(path, rule_state="", superseded_by=""):
        candidate = MODULE.Candidate(
            row_id=0, source_path=path, source_sha256="", source_format=".md",
            record_type="verbatim_text_file", locator="complete file", timestamp="",
            role="", content_sha256="", content=path)
        candidate.rule_state = rule_state
        candidate.superseded_by = superseded_by
        candidate.mandatory = True
        return candidate

    kept, dropped = MODULE.superseded_guard([
        fake_candidate("reference-docs/legacy-style-guide.md", "superseded", "writing-standards.md"),
    ])
    check(not kept and len(dropped) == 1,
          "superseded source survived without the rule that replaced it", failures)

    kept, dropped = MODULE.superseded_guard([
        fake_candidate("writing-standards.md", "current"),
        fake_candidate("reference-docs/legacy-style-guide.md", "superseded", "writing-standards.md"),
    ])
    check(len(kept) == 2 and not dropped,
          "superseded source was dropped although its replacement was present", failures)

    over_cap = [fake_candidate("writing-standards.md", "current")]
    over_cap += [fake_candidate(f"archive/OLD_{i}.md", "superseded", "writing-standards.md")
                 for i in range(MODULE.MAX_SUPERSEDED_SOURCES + 2)]
    kept, dropped = MODULE.superseded_guard(over_cap)
    check(sum(1 for c in kept if c.rule_state == "superseded") == MODULE.MAX_SUPERSEDED_SOURCES,
          "superseded source cap was not enforced", failures)
    check(len(dropped) == 2, "over-cap superseded sources were not reported as dropped", failures)

    # Configuration integrity: a superseded entry must name a replacement that the
    # same route also carries as a current source, so the guard can never be the
    # only thing standing between the packet and a bare historical rule.
    for route_name, route_cfg in config["routes"].items():
        entries = [e for e in route_cfg.get("canonical_sources", []) if isinstance(e, dict)]
        current_names = {Path(e["path"]).name for e in entries if e.get("rule_state") != "superseded"}
        for entry in entries:
            if entry.get("rule_state") != "superseded":
                continue
            check(bool(entry.get("superseded_by")),
                  f"{route_name}: {entry['path']} is superseded but names no replacement", failures)
            check(Path(entry.get("superseded_by", "")).name in current_names,
                  f"{route_name}: replacement for {entry['path']} is not a current source "
                  "on the same route", failures)

    # End to end: a real prompt that reaches a historical document must also carry
    # the rule in force, and must carry it labelled.
    probe = "What is the summary line character limit in the house style?"
    parsed_probe = MODULE.parse_prompt(probe, config)
    ranked_probe, _, _, _ = MODULE.retrieve(VAULT, connection, config, parsed_probe)
    chosen = MODULE.choose_records(ranked_probe, parsed_probe, 22, config)
    chosen, _ = MODULE.superseded_guard(chosen)
    names = {Path(c.source_path).name for c in chosen}
    check("legacy-style-guide.md" in names,
          "opportunistic historical attachment never reached the packet", failures)
    for candidate in chosen:
        if candidate.rule_state != "superseded":
            continue
        check(candidate.superseded_by != "",
              f"{candidate.source_path} returned unlabelled", failures)
        check(Path(candidate.superseded_by).name in names,
              f"{candidate.source_path} returned without its replacement", failures)

    # Mirror suppression: the snapshot copy must not take a second source slot.
    selected_paths = [c.source_path for c in chosen]
    check("writing-standards.md" in selected_paths, "live revision missing from packet", failures)
    check("snapshots/2026-01/writing-standards.md" not in selected_paths,
          "mirror copy took a second source slot", failures)
    check("snapshots/2026-01/writing-standards.md"
          in getattr(MODULE.choose_records, "last_mirror_superseded", {}),
          "mirror suppression was not reported", failures)

    # Lab-artifact exclusion: an excluded prefix must never reach a packet, even
    # though its text matches the prompt's terms.
    artifact_probe = MODULE.parse_prompt("packet route glossary summary line", config)
    artifact_ranked, _, _, _ = MODULE.retrieve(VAULT, connection, config, artifact_probe)
    check(not any(c.source_path.startswith("generated/") for c in artifact_ranked),
          "excluded lab artifact was retrieved", failures)

    connection.close()

    # --- CLI smoke -----------------------------------------------------------
    cli = subprocess.run(
        [sys.executable, str(HERE / "context_router.py"), "--vault", str(VAULT),
         "--prompt", probe, "--no-save", "--json"],
        capture_output=True, text=True)
    check(cli.returncode == 0, f"CLI run failed: {cli.stderr.strip()}", failures)
    if cli.returncode == 0:
        summary = json.loads(cli.stdout)
        check(summary["status"] in {"SUPPORTED", "USER_STATED", "PARTIAL"},
              f"CLI smoke returned unexpected status {summary['status']}", failures)
        check(summary["activation_tier"] == "brief",
              f"CLI smoke used tier {summary['activation_tier']}", failures)
    help_run = subprocess.run(
        [sys.executable, str(HERE / "context_router.py"), "--help"],
        capture_output=True, text=True)
    check(help_run.returncode == 0 and "--vault" in help_run.stdout,
          "--help did not render", failures)

    output = {
        "passed": not failures,
        "checks_run": CHECKS_RUN,
        "fixtures": len(FIXTURES),
        "results": results,
        "failures": failures,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


# Counted by instrumenting check(); see below.
CHECKS_RUN = 0
_original_check = check


def check(condition: bool, message: str, failures: "list[str]") -> None:  # noqa: F811
    global CHECKS_RUN
    CHECKS_RUN += 1
    _original_check(condition, message, failures)


if __name__ == "__main__":
    raise SystemExit(main())
