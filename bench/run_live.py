#!/usr/bin/env python3
"""Live host arm: ask each sealed question to a headless host in three arms.

Dry run is the default: without --run nothing calls the host; the script
prints the plan, preflights each hook command against a vault copy and exits.

    python3 bench/run_live.py --host-cmd claude                 # plan only
    python3 bench/run_live.py --host-cmd claude --run           # spends money
    python3 bench/run_live.py --host-cmd claude --run --arms baseline,hook-fts --only B01,B02
    python3 bench/run_live.py --write-judge-template            # regenerate judge_template.jsonl
    python3 bench/run_live.py --summarize-judged path/to/judge.jsonl

Arms (all use the host's own file tools, Read/Grep/Glob, on a copy of the vault):

- `baseline`       no hook; the host searches the copy itself.
- `hook-fts`       a UserPromptSubmit hook in the copy's `.claude/settings.json`
                   runs `context_layer.cli hook claude-code --vault <copy>` and
                   injects its evidence before the model sees the question.
- `hook-synaptic`  the same hook with `--method synaptic`. If the checkout's
                   `hook` subcommand does not accept `--method`, the preflight
                   records that and the arm is skipped (see --force-arms).

Recorded per call, from the host's own `--output-format json` payload: the
answer text, input / cache-creation / cache-read / output tokens, cost, turns
and duration. The script never grades an answer. It writes `judge.jsonl`
(one row per case and arm, verdict empty) for a human or a separate judge;
`--summarize-judged` only counts verdicts somebody else wrote.

Python 3.10+; standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parent
VAULT = BENCH / "vault"
CASES = BENCH / "cases.jsonl"
TEMPLATE = BENCH / "judge_template.jsonl"
SCHEMA = "context-layer-bench-live-v1"
ARMS = ("baseline", "hook-fts", "hook-synaptic")
VERDICTS = ("correct", "partial", "wrong", "abstained")
ABSTAIN = "NOT_FOUND"
ALLOW = "Read,Grep,Glob"
DENY = "Bash,WebSearch,WebFetch,Edit,Write,Task,NotebookEdit"
USAGE_FIELDS = {"input": "input_tokens", "cache_creation": "cache_creation_input_tokens",
                "cache_read": "cache_read_input_tokens", "output": "output_tokens"}
SYSTEM = ("Answer the user's question from the Markdown notes in the current directory only. "
          "Name the note path for every fact you use. If the notes do not answer the question, "
          f"reply with exactly {ABSTAIN} and nothing else.")

sys.path.insert(0, str(BENCH))
import seal  # noqa: E402


# ---------------------------------------------------------------------------
# Vault copy and hook
# ---------------------------------------------------------------------------

def checkout_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONUTF8"] = "1"      # the hook's output is read as UTF-8, whatever the locale
    return env


def cli(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "context_layer.cli", *args], input=stdin,
                          capture_output=True, text=True, encoding="utf-8", env=checkout_env(),
                          cwd=str(REPO),
                          timeout=180)


def prepare_vault(workdir: Path) -> Path:
    vault = workdir / "vault"
    shutil.copytree(VAULT, vault)
    for step in (["init", str(vault)], ["index", str(vault)]):
        done = cli(step)
        if done.returncode != 0:
            raise SystemExit(f"run_live: `context-layer {step[0]}` failed:\n{done.stderr[-2000:]}")
    return vault


def hook_argv(vault: Path, arm: str, extra: list[str]) -> list[str]:
    """Mirror of context_layer/install.py hook_entry for a checkout (no installed script).

    Always the checkout's interpreter and module, so the arm measures this
    checkout and never a different installed version.
    """
    argv = [sys.executable, "-m", "context_layer.cli", "hook", "claude-code", "--vault", str(vault)]
    if arm == "hook-synaptic":
        argv += ["--method", "synaptic"]
    return argv + extra


def hook_command(vault: Path, arm: str, extra: list[str]) -> str:
    return f"PYTHONPATH={shlex.quote(str(REPO))} " + shlex.join(hook_argv(vault, arm, extra))


def preflight_hook(vault: Path, arm: str, extra: list[str], probe: str) -> tuple[bool, str]:
    """Run the hook once locally (no host involved) and report whether the arm can work."""
    done = subprocess.run(hook_argv(vault, arm, extra), input=json.dumps(
        {"hook_event_name": "UserPromptSubmit", "prompt": probe}), capture_output=True,
        text=True, encoding="utf-8", env=checkout_env(), cwd=str(vault), timeout=180)
    if done.returncode != 0 and ("unrecognized arguments" in done.stderr
                                 or "unrecognised arguments" in done.stderr):
        return False, (f"`hook` rejected {' '.join(extra) or 'its flags'}"
                       + (" / --method synaptic" if arm == "hook-synaptic" else "")
                       + f": {done.stderr.strip().splitlines()[-1]}")
    if done.returncode != 0:
        return False, f"hook exited {done.returncode}: {done.stderr.strip()[-300:]}"
    injected = 0
    if done.stdout.strip():
        try:
            injected = len(json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"])
        except (ValueError, KeyError, TypeError):
            return False, "hook printed something that is not hookSpecificOutput JSON"
    return True, f"hook ok (probe injected {injected} characters)"


def write_settings(vault: Path, arm: str, extra: list[str]) -> None:
    path = vault / ".claude" / "settings.json"
    if arm == "baseline":
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    settings = {"hooks": {"UserPromptSubmit": [
        {"hooks": [{"type": "command", "command": hook_command(vault, arm, extra)}]}]}}
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Host calls
# ---------------------------------------------------------------------------

def host_argv(args, question: str) -> list[str]:
    argv = [*shlex.split(args.host_cmd), "-p", question, "--output-format", "json",
            "--model", args.model, "--max-turns", str(args.max_turns),
            "--no-session-persistence", "--strict-mcp-config",
            "--allowedTools", ALLOW, "--disallowedTools", DENY,
            "--append-system-prompt", SYSTEM]
    if args.max_budget_usd is not None:
        argv += ["--max-budget-usd", str(args.max_budget_usd)]
    if args.setting_sources:
        argv += ["--setting-sources", args.setting_sources]
    return argv


def usage_tokens(usage) -> dict:
    counts = {name: 0 for name in USAGE_FIELDS}
    if isinstance(usage, dict):
        for name, field in USAGE_FIELDS.items():
            value = usage.get(field)
            counts[name] = int(value) if isinstance(value, (int, float)) else 0
    counts["total"] = sum(counts.values())
    return counts


def run_one(args, vault: Path, case: dict, arm: str, raw_dir: Path) -> dict:
    argv = host_argv(args, case["question"])
    clock = time.monotonic()
    try:
        done = subprocess.run(argv, cwd=str(vault), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=args.timeout)
        stdout, stderr, code = done.stdout, done.stderr, done.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        stdout, stderr, code = "", f"{type(exc).__name__}: {exc}", -1
    wall_ms = int((time.monotonic() - clock) * 1000)
    (raw_dir / f"{case['id']}-{arm}.json").write_text(stdout, encoding="utf-8")
    (raw_dir / f"{case['id']}-{arm}.err").write_text(stderr, encoding="utf-8")
    payload, error = None, None
    try:
        payload = json.loads(stdout)
        if not isinstance(payload, dict):
            payload, error = None, "host JSON is not an object"
    except ValueError as exc:
        error = f"unparsable host JSON: {exc}"
    if code != 0:
        error = f"host exit {code}: {(stderr.strip() or stdout.strip())[:300]}"
    if payload and payload.get("is_error"):
        error = error or f"host is_error (subtype {payload.get('subtype')})"
    answer = payload.get("result") if payload else ""
    return {
        "schema": SCHEMA, "id": case["id"], "type": case["type"], "arm": arm,
        "model": args.model, "question": case["question"],
        "answer": answer if isinstance(answer, str) else "",
        "error": error,
        "tokens": usage_tokens(payload.get("usage") if payload else None),
        "cost_usd": payload.get("total_cost_usd") if payload else None,
        "num_turns": payload.get("num_turns") if payload else None,
        "host_duration_ms": payload.get("duration_ms") if payload else None,
        "wall_ms": wall_ms,
    }


# ---------------------------------------------------------------------------
# Judging support (the script never fills a verdict)
# ---------------------------------------------------------------------------

def judge_row(case: dict, arm: str | None = None, answer: str | None = None) -> dict:
    return {"id": case["id"], "type": case["type"], "question": case["question"],
            "gold": [g["must_contain"] for g in case["gold"]],
            "gold_paths": [g["path"] for g in case["gold"]],
            "arm": arm, "answer": answer, "verdict": None, "judge": None, "notes": ""}


def write_judge_template() -> None:
    rows = [judge_row(case) for case in seal.load_cases()]
    TEMPLATE.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                        encoding="utf-8")
    print(f"wrote {TEMPLATE.relative_to(REPO)} ({len(rows)} rows)")


def summarize_judged(path: Path) -> str:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    arms = sorted({r["arm"] for r in rows if r.get("arm")})
    out = ["| arm | judged | " + " | ".join(VERDICTS) + " | unjudged |",
           "| --- | --- | " + " | ".join("---" for _ in VERDICTS) + " | --- |"]
    for arm in arms:
        mine = [r for r in rows if r.get("arm") == arm]
        judged = [r for r in mine if r.get("verdict") in VERDICTS]
        bad = [r for r in mine if r.get("verdict") not in (None, *VERDICTS)]
        if bad:
            raise SystemExit(f"run_live: unknown verdict {bad[0]['verdict']!r} in {bad[0]['id']}")
        counts = [sum(1 for r in judged if r["verdict"] == v) for v in VERDICTS]
        out.append(f"| `{arm}` | {len(judged)} | " + " | ".join(map(str, counts))
                   + f" | {len(mine) - len(judged)} |")
    return "\n".join(out)


def cost_summary(rows: list[dict]) -> str:
    out = ["| arm | calls | errors | mean input | mean cache create | mean cache read | mean output "
           "| mean total tokens | total cost USD | mean turns | mean host ms |",
           "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for arm in ARMS:
        mine = [r for r in rows if r["arm"] == arm]
        if not mine:
            continue
        ok = [r for r in mine if not r["error"]]

        def avg(get):
            values = [v for v in (get(r) for r in ok) if isinstance(v, (int, float))]
            return f"{sum(values) / len(values):.1f}" if values else "n/a"
        cost = sum(r["cost_usd"] for r in ok if isinstance(r["cost_usd"], (int, float)))
        out.append(f"| `{arm}` | {len(mine)} | {len(mine) - len(ok)} "
                   f"| {avg(lambda r: r['tokens']['input'])} "
                   f"| {avg(lambda r: r['tokens']['cache_creation'])} "
                   f"| {avg(lambda r: r['tokens']['cache_read'])} "
                   f"| {avg(lambda r: r['tokens']['output'])} "
                   f"| {avg(lambda r: r['tokens']['total'])} | {cost:.4f} "
                   f"| {avg(lambda r: r['num_turns'])} | {avg(lambda r: r['host_duration_ms'])} |")
    return "\n".join(out)


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host-cmd", default="claude", help="Host executable (default: claude).")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--arms", default=",".join(ARMS))
    parser.add_argument("--only", default=None, help="Comma-separated case ids.")
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--max-budget-usd", type=float, default=None,
                        help="Per-call spending cap passed to the host.")
    parser.add_argument("--setting-sources", default="project",
                        help="Host setting sources (default: project, so the person's own user-level "
                             "hooks do not fire inside the benchmark). Pass '' to omit the flag.")
    parser.add_argument("--hook-arg", action="append", default=None,
                        help="Extra argument for the hook command, e.g. --hook-arg=--budget=3200.")
    parser.add_argument("--timeout", type=int, default=600, help="Seconds per host call.")
    parser.add_argument("--out", type=Path, default=BENCH / "results" / "live")
    parser.add_argument("--run", action="store_true", help="Actually call the host (costs money).")
    parser.add_argument("--force-arms", action="store_true",
                        help="Run an arm even if its hook preflight failed.")
    parser.add_argument("--write-judge-template", action="store_true")
    parser.add_argument("--summarize-judged", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.write_judge_template:
        write_judge_template()
        return 0
    if args.summarize_judged:
        print(summarize_judged(args.summarize_judged))
        return 0

    problems = seal.check()
    if problems:
        for problem in problems:
            print("run_live: seal mismatch:", problem, file=sys.stderr)
        return 2
    cases = seal.load_cases()
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    arms = [a for a in args.arms.split(",") if a]
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        parser.error(f"unknown arm(s): {', '.join(unknown)}")
    extra = list(args.hook_arg or [])

    with tempfile.TemporaryDirectory(prefix="cl-bench-live-") as tmp:
        vault = prepare_vault(Path(tmp))
        notes, runnable = [], []
        for arm in arms:
            if arm == "baseline":
                runnable.append(arm)
                continue
            ok, note = preflight_hook(vault, arm, extra, cases[0]["question"])
            notes.append(f"{arm}: {note}")
            if ok or args.force_arms:
                runnable.append(arm)
        for note in notes:
            print(f"run_live: {note}", file=sys.stderr)
        print(f"run_live: arms to run: {', '.join(runnable) or 'none'}; {len(cases)} cases", file=sys.stderr)
        print("run_live: host argv: " + shlex.join(host_argv(args, "<question>")), file=sys.stderr)
        for arm in runnable:
            if arm != "baseline":
                print(f"run_live: {arm} hook: {hook_command(vault, arm, extra)}", file=sys.stderr)
        if not args.run:
            print("run_live: dry run; pass --run to call the host.", file=sys.stderr)
            return 0

        out = args.out
        raw = out / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        rows = []
        for arm in runnable:
            write_settings(vault, arm, extra)
            for case in cases:
                row = run_one(args, vault, case, arm, raw)
                rows.append(row)
                print(f"{case['id']} {arm}: {'ERROR ' + row['error'] if row['error'] else 'ok'} "
                      f"{row['tokens']['total']} tokens", file=sys.stderr)
            write_settings(vault, "baseline", extra)

    (out / "answers.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    by_id = {c["id"]: c for c in cases}
    (out / "judge.jsonl").write_text("".join(
        json.dumps(judge_row(by_id[r["id"]], r["arm"], r["answer"]), ensure_ascii=False) + "\n"
        for r in rows), encoding="utf-8")
    summary = ["# Live host run (unjudged)", "",
               f"Host `{args.host_cmd}`, model `{args.model}`, {len(cases)} cases, arms: "
               f"{', '.join(runnable)}. Token and cost figures are the host's own counts.", "",
               cost_summary(rows), "", "## Preflight", ""] + [f"- {n}" for n in notes] + [
               "", "Answers are in `answers.jsonl`; fill `verdict` in `judge.jsonl` "
               f"({' / '.join(VERDICTS)}) and run `--summarize-judged`.", ""]
    (out / "SUMMARY.md").write_text("\n".join(summary), encoding="utf-8")
    print("\n".join(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
