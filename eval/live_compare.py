#!/usr/bin/env python3
"""Phase A6 live runner: one host, one model, three arms, held-out cases.

`baseline` is the host alone on a copy of the vault with its own file tools.
`candidate` is the same host with no file tools and only the context-layer MCP
tools. `hook` is baseline's tool set plus the context-layer UserPromptSubmit
hook, so evidence arrives with the prompt instead of being searched for. Every
arm gets the same prompt, model, turn cap and budget, and every arm is launched
headless from the vault directory.

Every number here comes from the host's own `--output-format json` payload or
from this runner's wall clock; the runner only sums and averages them. The cost
is the host's own client-side estimate (`total_cost_usd`), so every report labels
it host-estimated: it is not billing data. `delivered` means every expected
source was named in the answer as a whole path token, which is a delivery
check, not a correctness check -- correctness is the `judge` column a
coordinator fills in later with `--judgements`.

Results append to `<out>/results.jsonl`, so an interrupted run resumes; a host
failure or a call that outlives `--timeout-s` is recorded as an error row
rather than dropped. The case file and the log are JSON Lines read with the
package's line model (a line ends at a line feed, nothing else), and rows are
written as ASCII JSON.

Python 3.10+; standard library only. No model API call happens in this module;
the host binary is the only thing that talks to a model.
"""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import statistics
import subprocess
import sys
import time
import unicodedata

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from context_layer import install  # noqa: E402
from context_layer.platform_support import managed_process_tree  # noqa: E402

SCHEMA = "context-layer-live-compare-v1"
ARMS = ("baseline", "candidate", "hook")
VERDICTS = ("correct", "partial", "wrong", "abstained")
ABSTAIN = "NOT_FOUND"
CASE_KEYS = ("id", "prompt", "answerable")

BASELINE_ALLOW = "Read,Grep,Glob"
BASELINE_DENY = "Bash,WebSearch,WebFetch,Edit,Write,Task"
CANDIDATE_ALLOW = (f"mcp__{install.SERVER_NAME}__search_vault,"
                   f"mcp__{install.SERVER_NAME}__read_source")
CANDIDATE_DENY = "Read,Grep,Glob,Bash,WebSearch,WebFetch,Edit,Write,Task"
# The hook arm's only edit to the vault copy, put back when the run ends.
HOOK_SETTINGS = PurePosixPath(".claude/settings.json")

# Host usage field -> the short name this runner records.
USAGE_FIELDS = {"input": "input_tokens", "cache_creation": "cache_creation_input_tokens",
                "cache_read": "cache_read_input_tokens", "output": "output_tokens"}

SIGNAL = ("A small N is a signal, not proof: these counts describe this case set "
          "on one host, one model and one vault, and nothing beyond it.")
COST_BASIS = ("host-estimated: the host's total_cost_usd, a client-side estimate, "
              "not billing data")
TIMEOUT_DEFAULT = 600.0     # seconds per host call; a call that outlives it is an error row

# A path token is bounded by characters that cannot continue a path.
_BEFORE = r"(?<![\w./\\-])"
_AFTER = r"(?![\w/\\-]|\.\w)"


# ---------------------------------------------------------------------------
# Cases and results
# ---------------------------------------------------------------------------

def jsonl_lines(text: str):
    """(line number, line) with the package's line model: a line ends at "\\n" only,
    one trailing "\\r" is dropped, and U+2028/U+2029/U+0085 stay inside a line."""
    for number, line in enumerate(text.split("\n"), 1):
        yield number, line[:-1] if line.endswith("\r") else line


def load_cases(path: Path, only: set | None) -> list:
    """One JSON object per line. A malformed case file is an error, never a silent skip."""
    cases, seen = [], set()
    for number, line in jsonl_lines(path.read_text(encoding="utf-8")):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"{path.name} line {number}: expected a JSON object")
        missing = [key for key in CASE_KEYS if key not in row]
        if missing:
            raise ValueError(f"{path.name} line {number}: missing {', '.join(missing)}")
        if row["id"] in seen:
            raise ValueError(f"{path.name} line {number}: duplicate id {row['id']}")
        seen.add(row["id"])
        if only is None or row["id"] in only:
            cases.append(row)
    if only:
        unknown = sorted(only - seen)
        if unknown:
            raise ValueError("--only names ids the case file does not have: " + ", ".join(unknown))
    return cases


def read_results(path: Path) -> list:
    rows = []
    if path.is_file():
        for _, line in jsonl_lines(path.read_text(encoding="utf-8")):
            if line.strip():
                rows.append(json.loads(line))
    return rows


def latest(rows: list) -> list:
    """Last row wins per (case, arm); an append-only log may hold a repaired retry."""
    keep = {}
    for row in rows:
        keep[(row["id"], row["arm"])] = row
    return [keep[key] for key in sorted(keep)]


def merge_judgements(rows: list, mapping: dict, path: Path) -> list:
    """Fold a coordinator's verdicts into the log without re-running anything."""
    bad = {key: value for key, value in mapping.items() if value not in VERDICTS}
    if bad:
        raise ValueError("judgements must be one of " + "/".join(VERDICTS)
                         + "; got " + ", ".join(f"{k}={v}" for k, v in sorted(bad.items())))
    matched = set()
    for row in rows:
        key = f"{row['id']}|{row['arm']}"
        if key in mapping:
            row["judge"] = mapping[key]
            matched.add(key)
    for key in sorted(set(mapping) - matched):
        print(f"judgement for {key} matches no result row", file=sys.stderr)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("".join(json.dumps(row, ensure_ascii=True) + "\n" for row in rows),
                         encoding="utf-8")
    temporary.replace(path)
    return rows


# ---------------------------------------------------------------------------
# One host call
# ---------------------------------------------------------------------------

def system_prompt(arm: str, vault: Path) -> str:
    if arm == "baseline":
        return ("Answer only from the Markdown vault at " + str(vault) + ", using your own "
                "file tools (Read, Grep, Glob) to find the answer there. Cite the "
                "vault-relative path of every source you used. If the vault does not "
                f"contain the answer, reply with exactly {ABSTAIN} and nothing else.")
    if arm == "hook":
        return ("Answer only from the Markdown vault at " + str(vault) + ". Evidence lines "
                "from the vault are injected with the prompt: prefer them, and use your "
                "file tools (Read, Grep, Glob) only for what they do not cover. Cite the "
                "vault-relative path of every source you used. If the vault does not "
                f"contain the answer, reply with exactly {ABSTAIN} and nothing else.")
    return ("Answer only from the context-layer tools "
            f"(mcp__{install.SERVER_NAME}__search_vault and "
            f"mcp__{install.SERVER_NAME}__read_source). "
            "You have no file tools and must not guess from memory. Cite the source_path "
            "and sha256 of every source you used. If the tools return no evidence for the "
            f"question, reply with exactly {ABSTAIN} and nothing else.")


@contextlib.contextmanager
def project_hook(vault: Path):
    """Install the UserPromptSubmit hook in the vault copy for one run, then put it back.

    The host reads project settings from its cwd, so this is the only way to run
    the hook arm without touching the person's own host config. Whatever was
    there before is restored byte for byte, and the note says what happened.
    """
    path = vault / HOOK_SETTINGS
    had_directory = path.parent.is_dir()
    before = path.read_bytes() if path.is_file() else None
    data = json.loads(before.decode("utf-8")) if before else {}
    if not isinstance(data, dict):
        raise ValueError(f"{HOOK_SETTINGS}: expected a JSON object")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{HOOK_SETTINGS}: 'hooks' is not an object")
    hooks.setdefault(install.HOOK_EVENT, []).append({"hooks": [install.hook_entry(vault)]})
    note = {"settings": str(HOOK_SETTINGS), "action": "merged" if before else "created"}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        yield note
    finally:
        if before is None:
            path.unlink(missing_ok=True)
            if not had_directory:
                with contextlib.suppress(OSError):        # only if we left it empty
                    path.parent.rmdir()
            note["restored"] = "removed"
        else:
            path.write_bytes(before)
            note["restored"] = "previous bytes"


def argv_for(arm: str, case: dict, args, vault: Path, mcp_path: Path) -> list:
    argv = [args.claude, "-p", case["prompt"],
            "--output-format", "json",
            "--model", args.model,
            "--max-turns", str(args.max_turns),
            "--max-budget-usd", str(args.budget_usd),
            "--no-session-persistence",
            "--strict-mcp-config"]
    if arm == "candidate":
        argv += ["--mcp-config", str(mcp_path),
                 "--allowedTools", CANDIDATE_ALLOW,
                 "--disallowedTools", CANDIDATE_DENY]
    else:                                   # baseline and hook share the file-tool set
        argv += ["--add-dir", str(vault),
                 "--allowedTools", BASELINE_ALLOW,
                 "--disallowedTools", BASELINE_DENY]
    return argv + ["--append-system-prompt", system_prompt(arm, vault)]


def usage_tokens(usage) -> dict:
    counts = {name: 0 for name in USAGE_FIELDS}
    if isinstance(usage, dict):
        for name, field in USAGE_FIELDS.items():
            value = usage.get(field)
            counts[name] = int(value) if isinstance(value, (int, float)) else 0
    counts["total"] = sum(counts.values())
    return counts


def mentions(answer: str, name: str) -> bool:
    """`name` occurs in `answer` as a whole path token, not inside a longer path or name.

    `notes/a.md` is not in `notes/delta.md`, `plan.md` is not in `archive/old-plan.md`
    or `archive/plan.md`, and `a.md` is not in `a.md.bak`; a trailing sentence period,
    a leading `./`, brackets and backticks around the path are fine.
    """
    text = unicodedata.normalize("NFC", answer or "")
    text = re.sub(_BEFORE + r"\./", "", text)
    target = unicodedata.normalize("NFC", name)
    return re.search(_BEFORE + re.escape(target) + _AFTER, text) is not None


def is_delivered(answer: str, expected) -> bool:
    """Every expected source is named in the answer. Naming a path is not an answer.

    A source counts when its full vault-relative path, or its file name on its
    own, appears as a whole path token. Nothing to deliver is not a delivery, so
    a case with no expected source is False.
    """
    paths = [p for p in (expected or []) if isinstance(p, str) and p]
    if not paths:
        return False
    return all(mentions(answer, path) or mentions(answer, PurePosixPath(path).name)
               for path in paths)


def is_abstention(answer: str) -> bool:
    """The answer's first word (letters, digits and underscores) is exactly NOT_FOUND."""
    first = re.search(r"\w+", answer or "")
    return first is not None and first.group(0) == ABSTAIN


def safe_name(text: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in text)


def launch(argv: list, vault: Path, timeout_s: float | None = None):
    """One host call from the vault directory.

    Returns start, wall clock (ms), stdout, stderr, exit code and whether the
    call was stopped at `timeout_s`.
    """
    started = datetime.now(timezone.utc)
    clock = time.monotonic()
    timed_out = False
    # Explicit Python host scripts are useful for offline evaluation fixtures
    # and do not depend on a POSIX shebang or executable permission bit.
    command = [sys.executable, *argv] if Path(argv[0]).suffix.lower() == ".py" else argv
    try:
        with managed_process_tree(command, cwd=str(vault), stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  encoding="utf-8") as host:
            try:
                stdout, stderr = host.communicate(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                host.kill()
                try:
                    stdout, stderr = host.communicate(timeout=5)
                except subprocess.TimeoutExpired:  # a child that escaped the group holds the pipes
                    stdout, stderr = "", ""
                    for stream in (host.stdout, host.stderr):
                        with contextlib.suppress(OSError):
                            stream.close()
                    host.wait()
            return (started, int((time.monotonic() - clock) * 1000), stdout or "", stderr or "",
                    host.returncode, timed_out)
    except OSError as exc:                       # host missing or not executable
        return started, int((time.monotonic() - clock) * 1000), "", \
            f"{type(exc).__name__}: {exc}", -1, False


def run_case(case: dict, arm: str, args, vault: Path, out: Path, mcp_path: Path) -> dict:
    argv = argv_for(arm, case, args, vault, mcp_path)
    hook_note = None
    if arm == "hook":
        with project_hook(vault) as hook_note:
            started, duration_ms, stdout, stderr, code, timed_out = launch(
                argv, vault, args.timeout_s)
    else:
        started, duration_ms, stdout, stderr, code, timed_out = launch(
            argv, vault, args.timeout_s)

    stem = safe_name(f"{case['id']}-{arm}")
    (out / "raw" / f"{stem}.json").write_text(stdout, encoding="utf-8")
    (out / "raw" / f"{stem}.err").write_text(stderr, encoding="utf-8")

    error = None
    if timed_out:
        error = f"host timed out after {args.timeout_s:g} s and was stopped"
    elif code != 0:
        error = f"host exit {code}: " + ((stderr.strip() or stdout.strip())[:500] or "no output")
    payload = None
    try:
        parsed = json.loads(stdout) if not timed_out else None
        if not isinstance(parsed, dict):
            raise ValueError("host JSON is not an object")
        payload = parsed
    except ValueError as exc:
        error = error or f"unparsable host JSON: {exc}"
    if payload is not None and payload.get("is_error"):
        error = error or f"host reported is_error (subtype {payload.get('subtype')})"

    answer = payload.get("result") if payload else ""
    answer = answer if isinstance(answer, str) else ""
    abstained = is_abstention(answer)
    answerable = bool(case.get("answerable"))
    return {
        "schema": SCHEMA,
        "id": case["id"],
        "arm": arm,
        "model": args.model,
        "category": case.get("category"),
        "split": case.get("split"),
        "answerable": answerable,
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "duration_ms": duration_ms,                                  # this runner's wall clock
        "host_duration_ms": payload.get("duration_ms") if payload else None,
        "num_turns": payload.get("num_turns") if payload else None,
        "is_error": error is not None,
        "error": error,
        "timed_out": timed_out,
        "cost_usd": payload.get("total_cost_usd") if payload else None,   # host-estimated
        "tokens": usage_tokens(payload.get("usage") if payload else None),
        "answer": answer,
        "delivered": is_delivered(answer, case.get("expected_sources")),
        "abstained": abstained,
        "correct_abstention": abstained and not answerable,
        "false_abstention": abstained and answerable,
        "judge": None,                        # a coordinator fills this in with --judgements
        "hook_settings": hook_note,           # what the hook arm wrote and put back
        "raw": f"raw/{stem}.json",
        "stderr": f"raw/{stem}.err",
    }


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def mean(values, digits=3):
    return round(statistics.fmean(values), digits) if values else None


def median(values, digits=3):
    return round(statistics.median(values), digits) if values else None


def rate(hits, total, digits=3):
    return round(hits / total, digits) if total else None


def numbers(rows, key):
    return [row[key] for row in rows if isinstance(row.get(key), (int, float))]


def summarize(rows, arms, args, vault: Path, case_sha: str, case_count: int) -> dict:
    # An arm already in the log is summarised too, so a one-arm rerun still
    # reports every row the output directory holds.
    arms = [arm for arm in ARMS if arm in set(arms) | {row["arm"] for row in rows}]
    per_arm = {}
    for arm in arms:
        arm_rows = [row for row in rows if row["arm"] == arm]
        ok = [row for row in arm_rows if not row["is_error"]]
        answerable = [row for row in arm_rows if row["answerable"]]
        unanswerable = [row for row in arm_rows if not row["answerable"]]
        delivered = sum(1 for row in answerable if row["delivered"])
        correct = sum(1 for row in unanswerable if row["correct_abstention"])
        judged = {verdict: sum(1 for row in arm_rows if row.get("judge") == verdict)
                  for verdict in VERDICTS}
        totals = [row["tokens"]["total"] for row in ok if isinstance(row.get("tokens"), dict)]
        per_arm[arm] = {
            "cases_run": len(arm_rows),
            "errors": sum(1 for row in arm_rows if row["is_error"]),
            "timeouts": sum(1 for row in arm_rows if row.get("timed_out")),
            "answerable_cases": len(answerable),
            "delivered": delivered,
            "delivered_rate": rate(delivered, len(answerable)),
            "unanswerable_cases": len(unanswerable),
            "correct_abstentions": correct,
            "correct_abstention_rate": rate(correct, len(unanswerable)),
            "false_abstentions": sum(1 for row in arm_rows if row["false_abstention"]),
            "mean_total_tokens": mean(totals, 1),
            "median_total_tokens": median(totals, 1),
            "mean_cost_usd": mean(numbers(ok, "cost_usd"), 4),
            "mean_duration_ms": mean(numbers(ok, "duration_ms"), 1),
            "mean_turns": mean(numbers(ok, "num_turns"), 2),
            "judged": judged if any(judged.values()) else None,
        }
    table = [{key: row.get(key) for key in
              ("id", "category", "arm", "delivered", "abstained", "is_error",
               "num_turns", "cost_usd", "duration_ms", "judge")}
             | {"total_tokens": row.get("tokens", {}).get("total")}
             for row in rows]
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "model": args.model,
        "vault": vault.name,                       # basename only: never a personal path
        "cases_file": args.cases.name,
        "cases_sha256": case_sha,
        "cases_selected": case_count,
        "arms": list(arms),
        "bounds": {"max_turns": args.max_turns, "budget_usd": args.budget_usd,
                   "timeout_s": args.timeout_s},
        "cost_basis": COST_BASIS,
        "allowed_tools": {"baseline": BASELINE_ALLOW, "candidate": CANDIDATE_ALLOW,
                          "hook": BASELINE_ALLOW + f" + {install.HOOK_EVENT} hook"},
        "note": SIGNAL,
        "per_arm": per_arm,
        "cases": table,
    }


def cell(value):
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def render_report(summary: dict) -> str:
    arms = summary["arms"]
    lines = ["# Live comparison report", "",
             f"- Model: `{summary['model']}`",
             f"- Date (UTC): {summary['generated_at']}",
             f"- Vault: `{summary['vault']}` (name only; the vault itself is not distributed)",
             f"- Cases: `{summary['cases_file']}`, sha256 `{summary['cases_sha256']}`, "
             f"{summary['cases_selected']} selected",
             f"- Bounds: max {summary['bounds']['max_turns']} turns, "
             f"${summary['bounds']['budget_usd']} budget and "
             f"{summary['bounds'].get('timeout_s') or '-'} s per call",
             f"- Cost: {summary.get('cost_basis', COST_BASIS)}",
             "", SIGNAL, "",
             "`delivered` means every expected source was named in the answer as a whole",
             "path token. It does not mean the answer was right; `judge` is the only",
             "correctness column and it is filled in by a coordinator, not by this runner.", "",
             "## Per arm", "",
             "| Measure | " + " | ".join(arms) + " |",
             "| --- | " + " | ".join("---:" for _ in arms) + " |"]
    measures = [("cases run", "cases_run"), ("errors", "errors"), ("timeouts", "timeouts"),
                ("answerable cases", "answerable_cases"), ("delivered", "delivered"),
                ("delivered rate", "delivered_rate"),
                ("unanswerable cases", "unanswerable_cases"),
                ("correct abstentions", "correct_abstentions"),
                ("correct abstention rate", "correct_abstention_rate"),
                ("false abstentions", "false_abstentions"),
                ("mean total tokens", "mean_total_tokens"),
                ("median total tokens", "median_total_tokens"),
                ("mean host-estimated cost (USD)", "mean_cost_usd"),
                ("mean duration (ms, runner clock)", "mean_duration_ms"),
                ("mean turns", "mean_turns")]
    for label, key in measures:
        lines.append(f"| {label} | " + " | ".join(cell(summary["per_arm"][a].get(key))
                                                  for a in arms) + " |")
    for verdict in VERDICTS:
        if any(summary["per_arm"][a]["judged"] for a in arms):
            lines.append(f"| judged {verdict} | "
                         + " | ".join(cell((summary["per_arm"][a]["judged"] or {}).get(verdict))
                                      for a in arms) + " |")
    lines += ["", "## Per case", "",
              "| id | category | arm | delivered | abstained | error | tokens | cost (est.) | ms | turns | judge |",
              "| --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- |"]
    for row in summary["cases"]:
        lines.append("| " + " | ".join(cell(row[key]) for key in
                     ("id", "category", "arm", "delivered", "abstained", "is_error",
                      "total_tokens", "cost_usd", "duration_ms", "num_turns", "judge")) + " |")
    lines += ["", "The hook arm installs the context-layer UserPromptSubmit hook in the vault",
              "copy's project settings for its own runs and restores them afterwards; each row",
              "records what it wrote and put back.",
              "", "Token, cost, turn and duration means exclude rows recorded with an error.",
              "Raw host JSON and stderr for every run are in `raw/`; the append-only log is",
              "`results.jsonl`. See `eval/LIVE_COMPARE.md` for what each number is and is not.", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase A6 baseline/candidate live comparison.")
    parser.add_argument("--vault", required=True, type=Path, help="vault the host runs against")
    parser.add_argument("--cases", required=True, type=Path, help="held-out cases, JSONL")
    parser.add_argument("--out", required=True, type=Path, help="output directory (resumable)")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--arms", default=",".join(ARMS),
                        help="comma-separated: " + ",".join(ARMS))
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--budget-usd", type=float, default=0.60)
    parser.add_argument("--timeout-s", type=float, default=TIMEOUT_DEFAULT,
                        help=f"seconds a host call may take before it is stopped and recorded "
                             f"as an error row (default {TIMEOUT_DEFAULT:g})")
    parser.add_argument("--claude", default="claude", help="host executable")
    parser.add_argument("--only", default="", help="comma-separated case ids")
    parser.add_argument("--judgements", type=Path,
                        help="JSON {\"<id>|<arm>\": verdict}; merges and re-summarises only")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    arms = [arm.strip() for arm in args.arms.split(",") if arm.strip()]
    unknown = [arm for arm in arms if arm not in ARMS]
    if not arms or unknown:
        parser.error("--arms accepts " + ", ".join(ARMS))
    vault = args.vault.resolve()
    if not vault.is_dir():
        parser.error("--vault is not a directory")
    if not args.cases.is_file():
        parser.error("--cases is not a file")
    if not args.timeout_s > 0:
        parser.error("--timeout-s must be a positive number of seconds")
    only = {name.strip() for name in args.only.split(",") if name.strip()} or None

    case_sha = hashlib.sha256(args.cases.read_bytes()).hexdigest()
    try:
        cases = load_cases(args.cases, only)
    except ValueError as exc:
        parser.error(str(exc))
    out = args.out.resolve()
    (out / "raw").mkdir(parents=True, exist_ok=True)
    results_path = out / "results.jsonl"
    rows = read_results(results_path)

    if args.judgements is not None:
        try:
            rows = merge_judgements(rows, json.loads(args.judgements.read_text(encoding="utf-8")),
                                    results_path)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        mcp_path = out / "mcp.json"
        if "candidate" in arms:
            mcp_path.write_text(json.dumps(install.mcp_snippet(vault), indent=2) + "\n",
                                encoding="utf-8")
        done = {(row["id"], row["arm"]) for row in rows}
        with open(results_path, "a", encoding="utf-8") as log:
            for case in cases:
                for arm in arms:
                    if (case["id"], arm) in done:
                        print(f"skip {case['id']} {arm}: already in results.jsonl", file=sys.stderr)
                        continue
                    row = run_case(case, arm, args, vault, out, mcp_path)
                    log.write(json.dumps(row, ensure_ascii=True) + "\n")
                    log.flush()
                    rows.append(row)
                    state = row["error"] if row["is_error"] else "ok"
                    print(f"{case['id']} {arm}: {state}, {row['tokens']['total']} tokens, "
                          f"{row['duration_ms']} ms", file=sys.stderr)

    rows = latest(rows)
    summary = summarize(rows, arms, args, vault, case_sha, len(cases))
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8")
    (out / "REPORT.md").write_text(render_report(summary), encoding="utf-8")
    for arm in summary["arms"]:
        stats = summary["per_arm"][arm]
        print(f"{arm}: {stats['cases_run']} run, {stats['delivered']}/{stats['answerable_cases']} "
              f"delivered, {stats['correct_abstentions']}/{stats['unanswerable_cases']} correct "
              f"abstentions, {stats['errors']} errors, mean {stats['mean_total_tokens']} tokens")
    print(SIGNAL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
