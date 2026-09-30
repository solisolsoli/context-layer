#!/usr/bin/env python3
"""Run a retrieval command against frozen stimulus labels.

Default mode measures source-name mentions only (a diagnostic). With
--evidence-contract, validate the actual evidence-delivery-v1 JSON on stdout
against frozen source versions and required verbatim spans. Both modes measure
output characters. Neither judges semantic quality or authorises promotion;
--gate always records independent semantic acceptance as unmeasured and fails.

The command is split using shlex (no shell) and the prompt is appended as the
last argument. Use --evidence-json --no-save --prompt with the bundled router
for the delivery mode. Full schema and examples: EVIDENCE_CONTRACT.md.
Stdlib only. No network calls by the harness itself. Writes --out and optional
--packets-dir artifacts; the invoked command controls its own side effects.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

from evidence_contract import validate_contract, score_delivery

DEFAULT_FACETS = ["intent_type", "confidence", "length_bucket"]


def load_stimuli(path, filters=None, limit=None):
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    for field, allowed in (filters or {}).items():
        rows = [r for r in rows if r.get(field) in allowed]
    if limit:
        rows = rows[:limit]
    return rows


def run_router(command_parts, prompt_text, timeout):
    argv = command_parts + [prompt_text]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout)
        decode_error = ""
        try:
            stdout = proc.stdout.decode("utf-8")
        except UnicodeError:
            stdout = proc.stdout.decode("utf-8", errors="replace")
            decode_error = "stdout is not valid UTF-8; no evidence credit. "
        return {
            "ok": proc.returncode == 0 and not decode_error,
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": decode_error + proc.stderr.decode("utf-8", errors="replace"),
            "elapsed_s": round(time.monotonic() - t0, 2),
            "timed_out": False,
            "launch_error": None,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": (exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout) or "",
            "stderr": (exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr) or "",
            "elapsed_s": round(time.monotonic() - t0, 2),
            "timed_out": True,
            "launch_error": None,
        }
    except OSError as exc:
        # The command could not be started at all (missing interpreter, wrong
        # path, not executable). This is a setup problem, not a retrieval
        # result, and it must not be reported as a miss.
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "elapsed_s": round(time.monotonic() - t0, 2),
            "timed_out": False,
            "launch_error": f"{type(exc).__name__}: {exc}",
        }


def score_row(row, router_stdout, match_mode="basename", facets=DEFAULT_FACETS):
    """Score one stimulus row against one router output.

    Kept import-safe on purpose: a separate harness can `from evaluate import
    score_row` and reuse the exact same hit definition rather than
    reimplementing (and quietly loosening) it.
    """
    expected = row.get("expected_sources", [])
    hits = []
    for full in expected:
        needle = os.path.basename(full) if match_mode == "basename" else full
        hits.append({"path": full, "needle": needle, "hit": needle in router_stdout})
    n_hit = sum(1 for h in hits if h["hit"])
    n_expected = len(expected)
    cost_chars = len(router_stdout)
    scored = {
        "id": row["id"],
        "expected_count": n_expected,
        "hit_count": n_hit,
        "hit_rate": (n_hit / n_expected) if n_expected else None,
        "cost_chars": cost_chars,
        "cost_per_hit": (cost_chars / n_hit) if n_hit else None,
        "hits": hits,
    }
    for facet in facets:
        scored[facet] = row.get(facet)
    return scored


def breakdown(results, facet):
    groups = {}
    for r in results:
        groups.setdefault(r.get(facet), []).append(r)
    out = {}
    for key, rs in groups.items():
        e = sum(x["expected_count"] for x in rs)
        h = sum(x["hit_count"] for x in rs)
        c = sum(x["cost_chars"] for x in rs)
        out[str(key)] = {
            "n": len(rs), "expected": e, "hit": h,
            "hit_rate": (h / e) if e else None,
            "mean_cost_chars": c / len(rs),
        }
    return out


def check_gate(summary, facet_tables, gate, rubric_score=None):
    """Evaluate a measured run against a promotion gate.

    Every condition must hold at once. See PROMOTION_GATE.md for how to set the
    numbers; this function invents none of them.
    """
    checks = []

    def add(name, threshold, actual, ok):
        checks.append({"condition": name, "threshold": threshold, "actual": actual, "pass": ok})

    # Neither path mentions nor delivered bytes establish semantic quality.
    # This diagnostic runner cannot authorise promotion with self-supplied labels.
    add("independent semantic acceptance", "external review required", "not measured", False)
    add("operational failures", 0, summary.get("router_failures", 0),
        summary.get("router_failures", 0) == 0)
    if "min_hit_rate" in gate:
        actual = summary["hit_rate"]
        add("aggregate hit rate", gate["min_hit_rate"], actual, actual >= gate["min_hit_rate"])
    for facet, wanted in (gate.get("min_facet_hit_rate") or {}).items():
        for key, threshold in wanted.items():
            actual = (facet_tables.get(facet, {}).get(key) or {}).get("hit_rate")
            add(f"{facet}={key} hit rate", threshold, actual,
                actual is not None and actual >= threshold)
    if "max_mean_cost_chars" in gate:
        actual = summary["mean_cost_chars"]
        add("mean packet cost (chars)", gate["max_mean_cost_chars"], actual,
            actual <= gate["max_mean_cost_chars"])
    if "min_rubric_score" in gate:
        add("rubric score", gate["min_rubric_score"], rubric_score,
            rubric_score is not None and rubric_score >= gate["min_rubric_score"])

    return {"pass": all(c["pass"] for c in checks) and bool(checks), "checks": checks}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--command", required=True,
                    help="Router command; the prompt text is appended as the final argv token.")
    ap.add_argument("--stimuli", default="stimulus-set.example.jsonl")
    ap.add_argument("--packets-dir", type=Path, help="Optional directory to retain actual stdout/stderr per case.")
    ap.add_argument("--out", default=None, help="Optional path for full JSON results.")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--timeout", type=float, default=90.0, help="Per-prompt subprocess timeout in seconds (default: %(default)s). A timed-out run is scored as a miss and reported separately.")
    ap.add_argument("--evidence-contract", help="Frozen evidence-contract-v1 JSON; evaluates actual evidence-delivery-v1 stdout.")
    ap.add_argument("--match-mode", choices=["basename", "path"], default="basename")
    ap.add_argument("--facets", default=",".join(DEFAULT_FACETS),
                    help="Comma-separated stimulus fields to break results down by.")
    ap.add_argument("--filter", action="append", default=[], metavar="FIELD=V1,V2",
                    help="Keep only rows whose FIELD is one of the listed values. Repeatable.")
    ap.add_argument("--gate", default=None, help="Promotion-gate JSON to check the run against.")
    ap.add_argument("--rubric-results", default=None,
                    help="score_packet.py --out JSON, for the gate's rubric condition.")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    facets = [f for f in args.facets.split(",") if f]
    filters = {}
    for spec in args.filter:
        if "=" not in spec:
            print(f"--filter needs FIELD=VALUES, got {spec!r}", file=sys.stderr)
            sys.exit(2)
        field, values = spec.split("=", 1)
        filters[field] = set(values.split(","))

    stimuli = load_stimuli(args.stimuli, filters, args.limit)
    if not stimuli:
        print("No stimuli matched the given filters.", file=sys.stderr)
        sys.exit(1)

    contract = None
    if args.evidence_contract:
        try:
            with open(args.evidence_contract, encoding="utf-8") as handle:
                contract = validate_contract(json.load(handle))
            for row in stimuli:
                if row["id"] not in contract["queries"]:
                    raise ValueError(f"No frozen label for {row['id']}")
        except (ValueError, KeyError, TypeError, OSError) as exc:
            print(f"Invalid evidence contract: {exc}", file=sys.stderr)
            return 2

    command_parts = shlex.split(args.command)
    if not command_parts:
        print("--command is empty after shell-splitting.", file=sys.stderr)
        sys.exit(2)
    results = []
    empty_outputs = 0
    for i, row in enumerate(stimuli, 1):
        run = run_router(command_parts, row["prompt"], args.timeout)
        if run.get("launch_error"):
            print(f"\nCannot run --command {args.command!r}: {run['launch_error']}\n"
                  f"Nothing was measured. The prompt is appended as the final argv\n"
                  f"token, so the command must be the program and its flags only,\n"
                  f'e.g. --command "python3 adapters/grep_baseline.py --vault ~/notes".',
                  file=sys.stderr)
            sys.exit(2)
        if args.packets_dir:
            args.packets_dir.mkdir(parents=True, exist_ok=True)
            key = hashlib.sha256(str(row["id"]).encode("utf-8")).hexdigest()[:20]
            (args.packets_dir / (key + ".stdout")).write_text(run["stdout"], encoding="utf-8")
            (args.packets_dir / (key + ".stderr")).write_text(run["stderr"], encoding="utf-8")
        # Failed processes never earn hits, even if they printed expected names.
        scored = score_row(row, run["stdout"] if run["ok"] else "", args.match_mode, facets)
        scored["cost_chars"] = len(run["stdout"])
        if contract is not None:
            # NOT_FOUND's established CLI exit 2 is a normal abstention only
            # with the explicit, successful abstention envelope.
            abstention_exit = False
            if run.get("returncode") == 2 and not run["timed_out"]:
                try:
                    envelope = json.loads(run["stdout"])
                    abstention_exit = (envelope.get("operation_status") == "ok"
                                       and envelope.get("status") in {"NOT_FOUND", "ABSTAINED"}
                                       and envelope.get("evidence") == [])
                except (ValueError, AttributeError):
                    pass
            if abstention_exit:
                run["ok"] = True
            scored.update(score_delivery(contract, row["id"], run["stdout"], command_ok=run["ok"]))
            # Legacy filename rows are not the span metric and must not survive.
            scored.pop("hits", None)
            scored["hit_rate"] = (scored["hit_count"] / scored["expected_count"]
                                  if scored["expected_count"] else None)
            scored["cost_per_hit"] = (scored["cost_chars"] / scored["hit_count"]
                                      if scored["hit_count"] else None)
        if args.packets_dir:
            scored["stdout_artifact"] = str(args.packets_dir / (key + ".stdout"))
            scored["stderr_artifact"] = str(args.packets_dir / (key + ".stderr"))
        scored["router_ok"] = run["ok"]
        scored["router_timed_out"] = run["timed_out"]
        scored["router_elapsed_s"] = run["elapsed_s"]
        scored["router_stderr_tail"] = run["stderr"][-500:] if run["stderr"] else ""
        results.append(scored)
        if not run["stdout"].strip():
            empty_outputs += 1
        if not run["ok"] and not args.quiet:
            why = (f"timed out after {args.timeout}s" if run["timed_out"]
                   else f"exited {run['returncode']}")
            print(f"  !! {row['id']}: command {why}. Its sources are being scored as\n"
                  f"     misses, which is a fact about the run, not about your vault."
                  + (f"\n     stderr: {run['stderr'].strip()[-300:]}" if run["stderr"].strip() else ""),
                  file=sys.stderr)
        if not args.quiet:
            print(f"[{i}/{len(stimuli)}] {row['id']:>6} "
                  f"hit={scored['hit_count']}/{scored['expected_count']} "
                  f"cost={scored['cost_chars']:>7d}ch "
                  f"t={scored['router_elapsed_s']:>5.1f}s ok={scored['router_ok']}",
                  file=sys.stderr)

    n = len(results)
    total_expected = sum(r["expected_count"] for r in results)
    total_hit = sum(r["hit_count"] for r in results)
    total_cost = sum(r["cost_chars"] for r in results)
    summary = {
        "contract_content_sha256": hashlib.sha256(json.dumps(contract, sort_keys=True,
            ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest() if contract else None,
        "measurement_kind": "evidence_delivery_v1" if contract else "source_name_diagnostic",
        "semantic_quality_measured": False,
        "promotion_eligible": False,
        "correct_abstentions": sum(r.get("correct_abstention", False) for r in results),
        "delivery_passes": sum(r.get("delivery_pass", False) for r in results),
        "n": n,
        "total_expected": total_expected,
        "total_hit": total_hit,
        "hit_rate": (total_hit / total_expected) if total_expected else 0.0,
        "fully_hit_prompts": sum(1 for r in results
                                 if r["expected_count"] and r["hit_count"] == r["expected_count"]),
        "zero_hit_prompts": sum(1 for r in results if r["hit_count"] == 0),
        "router_failures": sum(1 for r in results if not r["router_ok"]),
        "total_cost_chars": total_cost,
        "mean_cost_chars": total_cost / n,
        "cost_per_hit_chars": (total_cost / total_hit) if total_hit else None,
    }

    print("\n=== SUMMARY ===")
    print(f"measurement: {summary['measurement_kind']} (semantic quality not measured)")
    print(f"prompts evaluated:            {summary['n']}")
    print(f"router non-zero-exit/timeout: {summary['router_failures']}")
    print(f"aggregate requirement hit-rate:    {total_hit}/{total_expected} ({summary['hit_rate']*100:.1f}%)")
    print(f"prompts with ALL sources hit: {summary['fully_hit_prompts']}/{n}")
    print(f"prompts with ZERO sources hit:{summary['zero_hit_prompts']}/{n}")
    print(f"total packet cost (chars):    {total_cost}")
    print(f"mean packet cost (chars):     {summary['mean_cost_chars']:.0f}")
    print("cost per hit (chars):         "
          + (f"{summary['cost_per_hit_chars']:.0f}" if total_hit else "n/a (zero hits)"))

    if summary["router_failures"] or summary["zero_hit_prompts"] == n or empty_outputs:
        print("\n=== DIAGNOSTICS ===")
    if summary["router_failures"]:
        print(f"  {summary['router_failures']}/{n} runs failed (non-zero exit or timeout). "
              f"Their expected sources were counted as misses.")
        print("  Re-run one prompt by hand to see the error; the hit rate above is a "
              "floor, not a measurement, until those runs succeed.")
    if empty_outputs:
        print(f"  {empty_outputs}/{n} runs printed nothing on stdout. A retrieval command "
              "must print the context it retrieved TO STDOUT; output on stderr is not read.")
    if total_hit == 0 and contract is None:
        print("  ZERO sources hit on EVERY prompt. This is almost always a wiring "
              "problem, not a retrieval failure. In order of likelihood:")
        print("   1. Wrong vault: the command is searching a directory that does not "
              "contain the files named in expected_sources.")
        print(f"   2. Match mode: --match-mode is {args.match_mode!r}. 'basename' needs the "
              "file's name to appear in the output; 'path' needs the whole relative path, "
              "spelled exactly as in expected_sources.")
        print("   3. Labels point at files that do not exist (a typo in expected_sources "
              "is a silent permanent miss). Verify each path resolves before believing "
              "any number here.")
        print("   4. The command prints a summary or an answer instead of the retrieved "
              "text, so no filename ever appears. Print the source paths.")
        print("  Check with one prompt by hand:")
        print(f"    {args.command} {stimuli[0]['prompt']!r} | head -40")

    facet_tables = {}
    for facet in facets:
        table = breakdown(results, facet)
        facet_tables[facet] = table
        print(f"\n=== BY {facet.upper()} ===")
        for key in sorted(table, key=str):
            row = table[key]
            rate = f"{row['hit_rate']*100:5.1f}%" if row["hit_rate"] is not None else "   n/a"
            print(f"  {key:22s} n={row['n']:3d}  hit={row['hit']}/{row['expected']} ({rate})"
                  f"  mean_cost={row['mean_cost_chars']:8.0f}ch")

    rubric_score = None
    if args.rubric_results:
        with open(args.rubric_results, "r", encoding="utf-8") as f:
            rubric_score = json.load(f).get("aggregate", {}).get("score_ratio")

    gate_report = None
    if args.gate:
        with open(args.gate, "r", encoding="utf-8") as f:
            gate = json.load(f)
        gate_report = check_gate(summary, facet_tables, gate, rubric_score)
        print("\n=== PROMOTION GATE ===")
        for c in gate_report["checks"]:
            actual = c["actual"]
            shown = f"{actual:.4f}" if isinstance(actual, float) else str(actual)
            print(f"  [{'PASS' if c['pass'] else 'FAIL'}] {c['condition']:34s} "
                  f"threshold={c['threshold']}  actual={shown}")
        print(f"  => {'GATE PASSED' if gate_report['pass'] else 'GATE FAILED - candidate does not go live'}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"command": args.command, "match_mode": None if contract else args.match_mode,
                       "evidence_contract": args.evidence_contract,
                       "summary": summary, "by_facet": facet_tables,
                       "gate": gate_report, "results": results},
                      f, ensure_ascii=False, indent=2)
        print(f"\nFull results written to {args.out}")

    if gate_report is not None and not gate_report["pass"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
