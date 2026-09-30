#!/usr/bin/env python3
"""Offline benchmark: score each retrieval method's evidence against the sealed cases.

    python3 bench/run_offline.py                       # grep, fts, synaptic
    python3 bench/run_offline.py --methods grep,fts
    python3 bench/run_offline.py --budget-tokens 400   # one packet budget, mapped per method

What it does, in order:

1. checks the seal (`bench/SEAL.md`) so the cases and the vault are the ones
   that were committed before any method was scored;
2. copies `bench/vault/` to a temporary directory and runs
   `python3 -m context_layer.cli init` and `index` on the copy (the checkout
   is put on PYTHONPATH, so no install is needed);
3. for every case and method runs
   `python3 -m context_layer.cli search <copy> --prompt <question> --method <m>`
   and reads the evidence JSON it prints;
4. scores only what can be checked without trusting the method: a gold item
   counts as found when its `must_contain` string occurs verbatim (after
   whitespace is collapsed) in a returned passage whose path is the gold path,
   and that passage is itself a verbatim span of the file it names. No field a
   method adds about itself (scores, roles, confidence) is read;
5. writes `bench/results/<method>.json`, `bench/results/SUMMARY.md`, and the
   same run in TREC form: `qrels.txt` and `<method>.run`.

`--budget-tokens N` maps one budget of N estimated tokens onto each method's
own flags (grep and fts: `--budget 4N` characters; synaptic: `--compact
--budget-tokens N`; plus a labelled `synaptic-extra` arm that runs the superset
packer inside the same total: `--budget 4(N-E) --extra-tokens E`). After the
run it checks that no packet of any arm exceeds N and exits 3 if one does. The
SUMMARY records the mapping and the check.

No network, no model calls, no clock in the outputs: the same checkout gives
the same files. `est_tokens` is ceil(characters / 4), an estimate, not a
tokenizer count.

Python 3.10+; standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
from urllib.parse import quote

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parent
VAULT = BENCH / "vault"
CASES = BENCH / "cases.jsonl"
RESULTS = BENCH / "results"
SCHEMA = "context-layer-bench-offline-v1"
TYPES = ("single_hop", "bridge_2hop", "multi_note_aggregation",
         "supersession", "distractor", "unanswerable")
CHARS_PER_TOKEN = 4
Z95 = 1.959963984540054
# Flags `search` applies only to the synaptic method (see eval/retrieve.py).
SYNAPTIC_ONLY = ("--extra-tokens", "--compact", "--budget-tokens", "--max-hops", "--record-query")
# Flags the equal-budget mapping sets itself; passing them through --search-arg as well
# would make the recorded mapping false.
BUDGET_FLAGS = ("--budget", "--budget-tokens", "--extra-tokens", "--compact")
# The CLI's own default split for the synaptic superset packer: an fts packet of 6,000
# characters (1,500 est. tokens) plus 600 extra tokens. The labelled `synaptic-extra` arm
# keeps that share of the equal budget for its extras unless --extra-tokens says otherwise.
DEFAULT_FTS_TOKENS = 6000 // CHARS_PER_TOKEN
DEFAULT_EXTRA_TOKENS = 600
SCORER_FILES = ("bench/run_offline.py", "bench/seal.py")

sys.path.insert(0, str(BENCH))
import seal  # noqa: E402  (bench/seal.py, same directory)


# ---------------------------------------------------------------------------
# Packet parsing and scoring (pure functions; exercised by test_scorer.py)
# ---------------------------------------------------------------------------

def normalize_text(text: str) -> str:
    """NFC and collapsed whitespace, so a passage re-wrapped by a method still matches."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def normalize_path(path: str, vault: Path | None = None) -> str:
    """Vault-relative POSIX path. Absolute paths under the vault are relativised."""
    text = str(path).replace("\\", "/")
    if vault is not None and os.path.isabs(text):
        try:
            text = Path(text).resolve().relative_to(vault.resolve()).as_posix()
        except ValueError:
            return text
    while text.startswith("./"):
        text = text[2:]
    return PurePosixPath(text).as_posix() if text else text


def extract_passages(packet, vault: Path | None = None) -> list[dict]:
    """Return [{path, text}] from an evidence packet.

    Accepts the evidence-delivery-v1 shape (`evidence: [{source_path, content}]`)
    and a `passages: [{path, text}]` shape. Only the path and the verbatim text
    are kept; every other field is ignored on purpose, so no method is rewarded
    for what it says about its own output.
    """
    if not isinstance(packet, dict):
        return []
    items = []
    for key in ("evidence", "passages"):
        value = packet.get(key)
        if isinstance(value, list):
            items.extend(v for v in value if isinstance(v, dict))
    passages = []
    for item in items:
        path = item.get("source_path", item.get("path"))
        text = item.get("content", item.get("text", item.get("excerpt")))
        if isinstance(path, str) and isinstance(text, str):
            passages.append({"path": normalize_path(path, vault), "text": text})
    return passages


def parse_stdout(stdout: str):
    """The CLI prints one JSON object; tolerate log lines before it."""
    text = stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    start = text.find("{")
    if start >= 0:
        try:
            return json.loads(text[start:])
        except ValueError:
            return None
    return None


def item_found(item: dict, passages: list[dict]) -> bool:
    needle = normalize_text(item["must_contain"])
    return any(p["path"] == item["path"] and needle in normalize_text(p["text"])
               for p in passages)


def is_verbatim(passage: dict, file_texts: dict[str, str]) -> bool:
    """True when the passage text is a span of the file it names: exactly, or after
    the same NFC/whitespace normalisation the matcher uses. A passage naming a file
    that is not in the vault copy cannot be verified and is not verbatim."""
    text = file_texts.get(passage["path"])
    if text is None:
        return False
    return passage["text"] in text or normalize_text(passage["text"]) in normalize_text(text)


def est_tokens(chars: int) -> int:
    return math.ceil(chars / CHARS_PER_TOKEN)


def score_case(case: dict, passages: list[dict], file_chars: dict[str, int],
               file_texts: dict[str, str] | None = None) -> dict:
    """Score one case. `file_chars` maps vault-relative path -> full file length.

    With `file_texts` (vault-relative path -> file text), a passage that is not a
    verbatim span of the file it names earns nothing: it is left out of the gold,
    distractor and path checks and counted in `non_verbatim`. It still counts
    toward the packet size, because a host would still read it.
    """
    if file_texts is None:
        credited, non_verbatim = passages, None
    else:
        credited = [p for p in passages if is_verbatim(p, file_texts)]
        non_verbatim = len(passages) - len(credited)
    gold = case.get("gold") or []
    distractors = case.get("distractors") or []
    found = [item_found(item, credited) for item in gold]
    gold_paths = sorted({item["path"] for item in gold})
    surfaced = sorted({p["path"] for p in passages})
    credited_paths = {p["path"] for p in credited}
    packet_chars = sum(len(p["text"]) for p in passages)
    answerable = bool(gold)
    distractor_hits = sum(item_found(item, credited) for item in distractors)
    complete = all(found) if answerable else None
    return {
        "id": case["id"],
        "type": case["type"],
        "answerable": answerable,
        "gold_items": len(gold),
        "gold_found": sum(found),
        "gold_recall": (sum(found) / len(gold)) if answerable else None,
        "complete": complete,
        # Looser, path-level view: were all required notes in the packet at all?
        "required_paths_present": (all(p in credited_paths for p in gold_paths)
                                   if answerable else None),
        "distractor_items": len(distractors),
        "distractor_hits": distractor_hits,
        # Supersession/distractor: wrong evidence present while the right one is not.
        "misled": (distractor_hits > 0 and not complete) if (answerable and distractors) else None,
        "passages": len(passages),
        "non_verbatim": non_verbatim,
        "distinct_files": len(surfaced),
        "surfaced_paths": surfaced,
        # Delivery order of the distinct files, for the TREC run file.
        "ranked_paths": list(dict.fromkeys(p["path"] for p in passages)),
        "packet_chars": packet_chars,
        "est_tokens": est_tokens(packet_chars),
        "whole_file_tokens": sum(est_tokens(file_chars[p]) for p in surfaced if p in file_chars),
        "unknown_paths": [p for p in surfaced if p not in file_chars],
        "false_evidence_on_unanswerable": None if answerable else len(passages),
    }


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def wilson(successes: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for a proportion; (0, 0) when n is 0."""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def sign_test(wins: int, losses: int) -> float:
    """Exact two-sided sign test (binomial, p=0.5) on discordant pairs."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def mean(values: list) -> float | None:
    values = [v for v in values if v is not None]
    return (sum(values) / len(values)) if values else None


def median(values: list) -> float | None:
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    return float(values[mid]) if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def completed(row: dict) -> bool:
    """A case counts as complete only when the method ran and delivered every gold item."""
    return bool(row["complete"]) and not row.get("error")


def aggregate(rows: list[dict]) -> dict:
    """Rates over every answerable case: a case whose search errored stays in the
    denominator and counts as incomplete (conservative). The rate over error-free
    cases is reported beside it. Packet sizes are over error-free rows only, so an
    error cannot make a method look cheaper."""
    answerable = [r for r in rows if r["answerable"]]
    answerable_ok = [r for r in answerable if not r.get("error")]
    unanswerable = [r for r in rows if not r["answerable"] and not r.get("error")]
    ok = [r for r in rows if not r.get("error")]
    complete = sum(1 for r in answerable if completed(r))
    low, high = wilson(complete, len(answerable))
    with_distractors = [r for r in answerable if r["misled"] is not None]
    return {
        "cases": len(rows),
        "errors": sum(1 for r in rows if r.get("error")),
        "error_ids": [r["id"] for r in rows if r.get("error")],
        "answerable": len(answerable),
        "complete": complete,
        "complete_rate": (complete / len(answerable)) if answerable else None,
        "complete_ci95": [round(low, 4), round(high, 4)],
        "answerable_without_errors": len(answerable_ok),
        "complete_rate_without_errors": (complete / len(answerable_ok)) if answerable_ok else None,
        "mean_gold_recall": mean([0.0 if r.get("error") else r["gold_recall"] for r in answerable]),
        "required_paths_present": sum(1 for r in answerable
                                      if r["required_paths_present"] and not r.get("error")),
        "misled": sum(1 for r in with_distractors if r["misled"] and not r.get("error")),
        "with_distractors": len(with_distractors),
        "non_verbatim_passages": sum(r.get("non_verbatim") or 0 for r in ok),
        "unanswerable": len(unanswerable),
        "unanswerable_with_passages": sum(1 for r in unanswerable if r["passages"]),
        "mean_passages_on_unanswerable": mean([r["passages"] for r in unanswerable]),
        "mean_passages": mean([r["passages"] for r in ok]),
        "mean_packet_chars": mean([r["packet_chars"] for r in ok]),
        "mean_est_tokens": mean([r["est_tokens"] for r in ok]),
        "median_est_tokens": median([r["est_tokens"] for r in ok]),
        "max_est_tokens": max((r["est_tokens"] for r in ok), default=None),
        "mean_whole_file_tokens": mean([r["whole_file_tokens"] for r in ok]),
        "est_tokens_per_complete": (sum(r["est_tokens"] for r in answerable_ok) / complete)
                                   if complete else None,
    }


def paired(rows_a: list[dict], rows_b: list[dict]) -> dict:
    """Completeness on the same answerable cases: A-only wins, B-only wins, sign test.

    An errored case is an incomplete case, so "A errored, B complete" is a B-only win."""
    by_id = {r["id"]: r for r in rows_b}
    wins = losses = both = neither = 0
    for a in rows_a:
        b = by_id.get(a["id"])
        if not b or not a["answerable"]:
            continue
        a_ok, b_ok = completed(a), completed(b)
        if a_ok and b_ok:
            both += 1
        elif a_ok:
            wins += 1
        elif b_ok:
            losses += 1
        else:
            neither += 1
    return {"a_only": wins, "b_only": losses, "both": both, "neither": neither,
            "sign_test_p": round(sign_test(wins, losses), 4)}


# ---------------------------------------------------------------------------
# Equal-budget mapping (E-01): one budget, each method's own flags
# ---------------------------------------------------------------------------

def default_extra_tokens(budget_tokens: int) -> int:
    """The CLI's default share of a synaptic packet that goes to link extras."""
    share = round(budget_tokens * DEFAULT_EXTRA_TOKENS / (DEFAULT_FTS_TOKENS + DEFAULT_EXTRA_TOKENS))
    return max(1, min(budget_tokens - 1, share))


def flag_names(tokens: list[str]) -> list[str]:
    return [t.split("=", 1)[0] for t in tokens if t.startswith("--")]


def check_search_args(methods: list[str], search_args: list[str], budget_tokens: int | None) -> list[str]:
    """Refuse forwarded flags `search` would not apply, so the SUMMARY never lists one.

    Mirrors the CLI contract (synaptic-only flags need --method synaptic; --budget-tokens
    needs --compact) and works whether or not the installed CLI enforces it itself."""
    problems = []
    names = flag_names(search_args)
    for flag in names:
        if flag in SYNAPTIC_ONLY:
            others = [m for m in methods if m != "synaptic"]
            if others:
                problems.append(f"{flag} applies only to --method synaptic, but it would be "
                                f"passed to {', '.join(others)}; run it with --methods synaptic")
    if "--budget-tokens" in names and "--compact" not in names:
        problems.append("--budget-tokens sizes only the --compact synaptic packet; add "
                        "--search-arg=--compact, or use the runner's own --budget-tokens")
    if budget_tokens is not None:
        clash = sorted(set(names) & set(BUDGET_FLAGS))
        if clash:
            problems.append(f"--budget-tokens sets {', '.join(BUDGET_FLAGS)} per method itself; "
                            f"do not also forward {', '.join(clash)}")
    return problems


def plan_arms(methods: list[str], args) -> list[dict]:
    """One arm per method; with --budget-tokens, the per-method budget flags and a bound."""
    common: list[str] = []
    if args.top_k is not None:
        common += ["--top-k", str(args.top_k)]
    common += list(args.search_arg or [])
    arms = []
    n = args.budget_tokens
    for method in methods:
        if n is None:
            arms.append({"label": method, "method": method, "flags": list(common),
                         "budget_flags": [], "bound": None, "mapping": None})
            continue
        if method == "synaptic":
            flags = ["--compact", "--budget-tokens", str(n)]
            arms.append({"label": "synaptic", "method": method, "flags": common + flags,
                         "budget_flags": flags, "bound": n,
                         "mapping": "compact packer sized by --budget-tokens N"})
            extra = args.extra_tokens if args.extra_tokens is not None else default_extra_tokens(n)
            flags = ["--budget", str((n - extra) * CHARS_PER_TOKEN), "--extra-tokens", str(extra)]
            arms.append({"label": "synaptic-extra", "method": method, "flags": common + flags,
                         "budget_flags": flags, "bound": n,
                         "mapping": (f"superset packer inside the same total: an fts packet of "
                                     f"4 x (N - E) characters plus E = {extra} extra tokens"
                                     + ("" if args.extra_tokens is not None else
                                        f" (the CLI default share {DEFAULT_EXTRA_TOKENS}/"
                                        f"{DEFAULT_FTS_TOKENS + DEFAULT_EXTRA_TOKENS} of N)"))})
        else:
            flags = ["--budget", str(n * CHARS_PER_TOKEN)]
            arms.append({"label": method, "method": method, "flags": common + flags,
                         "budget_flags": flags, "bound": n,
                         "mapping": f"characters = N x {CHARS_PER_TOKEN}"})
    return arms


def budget_check(arms: list[dict], results: dict) -> list[str]:
    """Every error-free packet of every bounded arm must fit its bound."""
    failures = []
    for arm in arms:
        if arm["bound"] is None or arm["label"] not in results:
            continue
        worst = results[arm["label"]]["aggregate"]["max_est_tokens"]
        arm["observed_max"] = worst
        if worst is not None and worst > arm["bound"]:
            failures.append(f"`{arm['label']}`: largest packet {worst} est. tokens > bound "
                            f"{arm['bound']} with {' '.join(arm['budget_flags'])}")
    return failures


# ---------------------------------------------------------------------------
# Running the CLI
# ---------------------------------------------------------------------------

def cli_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONHASHSEED"] = "0"
    # The CLI's output is read as UTF-8 here, so the CLI writes UTF-8 whatever the locale.
    env["PYTHONUTF8"] = "1"
    env.pop("CONTEXT_LAYER_HOME", None)
    return env


def cli(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "context_layer.cli", *args],
                          capture_output=True, text=True, encoding="utf-8", env=cli_env(),
                          cwd=str(REPO), timeout=timeout, stdin=subprocess.DEVNULL)


def prepare_vault(workdir: Path) -> Path:
    vault = workdir / "vault"
    shutil.copytree(VAULT, vault)
    for step in (["init", str(vault)], ["index", str(vault)]):
        done = cli(step)
        if done.returncode != 0:
            raise SystemExit(f"run_offline: `context-layer {step[0]}` failed "
                             f"(exit {done.returncode}):\n{done.stderr[-2000:]}")
    return vault


def rejected_method(done: subprocess.CompletedProcess) -> bool:
    return done.returncode == 2 and "invalid choice" in done.stderr


def rejected_flags(done: subprocess.CompletedProcess) -> bool:
    """A usage error (exit 2, no packet): an older CLI that does not know a flag, or a
    newer one that refuses a flag the method would not apply."""
    return done.returncode == 2 and parse_stdout(done.stdout) is None


def search(vault: Path, question: str, method: str, extra: list[str]) -> subprocess.CompletedProcess:
    return cli(["search", str(vault), "--prompt", question, "--method", method, *extra])


def probe_arm(vault: Path, arm: dict, probe: str) -> str | None:
    """Run one search with the arm's flags; return a skip note, or None when it runs.

    A method the CLI does not know is skipped with a note. Flags the CLI refuses stop
    the run: the runner only forwards flags the method applies, so a refusal means the
    CLI and this runner disagree, and a quiet skip would hide it."""
    done = search(vault, probe, arm["method"], arm["flags"])
    if rejected_method(done):
        return f"method `{arm['method']}` rejected by the CLI (invalid choice); skipped"
    if rejected_flags(done):
        last = (done.stderr.strip().splitlines() or ["(no message)"])[-1]
        raise SystemExit(f"run_offline: `search --method {arm['method']}` refused "
                         f"{' '.join(arm['flags']) or 'its flags'}: {last}")
    return None


def run_method(vault: Path, method: str, cases: list[dict], extra: list[str],
               file_chars: dict[str, int], file_texts: dict[str, str] | None = None,
               label: str | None = None) -> list[dict]:
    rows = []
    for case in cases:
        done = search(vault, case["question"], method, extra)
        packet = parse_stdout(done.stdout)
        error = None
        if packet is None:
            error = f"exit {done.returncode}; no JSON on stdout: {done.stderr.strip()[-300:]}"
        elif isinstance(packet, dict) and packet.get("operation_status") == "error":
            error = f"operation_status=error: {packet.get('error')}"
        passages = extract_passages(packet, vault) if packet is not None else []
        row = score_case(case, passages, file_chars, file_texts)
        row["method"] = method
        if label and label != method:
            row["arm"] = label
        row["exit_code"] = done.returncode
        row["status"] = packet.get("status") if isinstance(packet, dict) else None
        row["json_chars"] = len(done.stdout.strip())
        row["error"] = error
        rows.append(row)
        mark = "E" if error else ("." if row["complete"] in (True, None) else "x")
        print(mark, end="", flush=True, file=sys.stderr)
    print(f"  {label or method}", file=sys.stderr)
    return rows


# ---------------------------------------------------------------------------
# TREC export (E-03): qrels and one run file per arm
# ---------------------------------------------------------------------------

def docno(path: str) -> str:
    """A vault path as one whitespace-free TREC document id (percent-encoded)."""
    return quote(path, safe="/")


def run_tag(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.+-]", "_", label) or "run"


def qrels_text(cases: list[dict]) -> str:
    """`qid 0 docno rel`: gold notes rel 1, listed distractor notes rel 0."""
    lines = []
    for case in cases:
        rel: dict[str, int] = {}
        for item in case.get("distractors") or []:
            rel.setdefault(item["path"], 0)
        for item in case.get("gold") or []:
            rel[item["path"]] = 1
        lines += [f"{case['id']} 0 {docno(path)} {grade}" for path, grade in sorted(rel.items())]
    return "".join(line + "\n" for line in lines)


def run_text(rows: list[dict], label: str) -> str:
    """`qid Q0 docno rank score tag`: files in delivery order, score = files - rank + 1."""
    lines = []
    for row in rows:
        ranked = row.get("ranked_paths") or []
        for rank, path in enumerate(ranked, 1):
            lines.append(f"{row['id']} Q0 {docno(path)} {rank} {len(ranked) - rank + 1} {run_tag(label)}")
    return "".join(line + "\n" for line in lines)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def fmt(value, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def pct(k: int, n: int) -> str:
    return f"{k}/{n} ({100 * k / n:.0f}%)" if n else "n/a"


def provenance_line(meta: dict) -> str:
    """The one line that differs between checkouts and machines; CI ignores it."""
    detail = ""
    if meta.get("scorer_sha256"):
        detail = (f" (scorer `{meta['scorer_sha256'][:12]}`; Python {meta['python']}, "
                  f"SQLite {meta['sqlite']}, Unicode {meta['unicode']}; {meta['platform']})")
    return (f"Checkout: `{meta['git_head']}`{detail}. {meta['cases']} cases, "
            f"{meta['answerable']} answerable, {meta['unanswerable']} unanswerable.")


def summary_markdown(results: dict, skipped: dict, notes: list[str], meta: dict,
                     arms: list[dict] | None = None) -> str:
    methods = list(results)
    out = ["# Offline benchmark results", "",
           f"Generated by `{meta['command']}` from the sealed cases "
           f"(cases `{meta['cases_sha256'][:12]}`, vault manifest `{meta['manifest_sha256'][:12]}`).",
           provenance_line(meta), "",
           "`complete` = every gold `must_contain` string was found verbatim in a passage from "
           "its gold path. `est_tokens` = ceil(passage characters / 4), an **estimate**. "
           "`whole-file tokens` = the same estimate for reading every surfaced file in full. "
           "CI is a 95% Wilson interval.", ""]
    notes = list(notes)
    if methods:
        out += ["## Overall", "",
                "| method | complete (answerable) | 95% CI | mean gold recall | all gold notes surfaced "
                "| misled by distractor | mean est_tokens (all cases) | median | max | mean whole-file tokens "
                "| est_tokens per complete case | errors |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for m in methods:
            a = results[m]["aggregate"]
            lo, hi = a["complete_ci95"]
            out.append(
                f"| `{m}` | {pct(a['complete'], a['answerable'])} | {lo:.2f}-{hi:.2f} "
                f"| {fmt(a['mean_gold_recall'], 3)} | {pct(a['required_paths_present'], a['answerable'])} "
                f"| {pct(a['misled'], a['with_distractors'])} | {fmt(a['mean_est_tokens'])} "
                f"| {fmt(a['median_est_tokens'])} | {fmt(a['max_est_tokens'])} "
                f"| {fmt(a['mean_whole_file_tokens'])} | {fmt(a['est_tokens_per_complete'])} "
                f"| {a['errors']} |")
            if a["errors"]:
                notes.append(f"`{m}`: {a['errors']} case(s) ended in an error "
                             f"({', '.join(a['error_ids'])}); they count as incomplete above. "
                             f"Complete among the {a['answerable_without_errors']} error-free "
                             f"answerable cases: {pct(a['complete'], a['answerable_without_errors'])}.")
            if a.get("non_verbatim_passages"):
                notes.append(f"`{m}`: {a['non_verbatim_passages']} passage(s) were not verbatim "
                             f"spans of the file they name and earned nothing.")
        out += ["", "## Complete by case type", "",
                "| type | n | " + " | ".join(f"`{m}`" for m in methods) + " |",
                "| --- | --- | " + " | ".join("---" for _ in methods) + " |"]
        for t in TYPES:
            if t == "unanswerable":
                continue
            n = results[methods[0]]["by_type"].get(t, {}).get("answerable", 0)
            if not n:
                continue
            cells = []
            for m in methods:
                b = results[m]["by_type"].get(t)
                cells.append(f"{b['complete']} ({fmt(b['mean_gold_recall'], 2)} recall, "
                             f"{fmt(b['mean_est_tokens'], 0)} tok)" if b else "n/a")
            out.append(f"| `{t}` | {n} | " + " | ".join(cells) + " |")
        out += ["", "## Unanswerable cases", "",
                "Any passage is allowed; the count only shows how much text a method hands the host "
                "when nothing answers the question.", "",
                "| method | cases with passages | mean passages | mean est_tokens |",
                "| --- | --- | --- | --- |"]
        for m in methods:
            a = results[m]["aggregate"]
            u = results[m]["by_type"].get("unanswerable", {})
            out.append(f"| `{m}` | {pct(a['unanswerable_with_passages'], a['unanswerable'])} "
                       f"| {fmt(a['mean_passages_on_unanswerable'], 2)} "
                       f"| {fmt(u.get('mean_est_tokens'))} |")
        if len(methods) > 1:
            out += ["", "## Paired completeness", "",
                    "Same answerable cases; exact two-sided sign test on the discordant pairs.", "",
                    "| A vs B | A only | B only | both | neither | p |",
                    "| --- | --- | --- | --- | --- | --- |"]
            for i, a in enumerate(methods):
                for b in methods[i + 1:]:
                    p = paired(results[a]["rows"], results[b]["rows"])
                    out.append(f"| `{a}` vs `{b}` | {p['a_only']} | {p['b_only']} | {p['both']} "
                               f"| {p['neither']} | {p['sign_test_p']} |")
        out += ["", "## Flags passed to `search`", ""]
        for m in methods:
            flags = " ".join(results[m]["search_flags"]) or "(CLI defaults)"
            out.append(f"- `{m}`: `{flags}`")
        bounded = [arm for arm in arms or [] if arm["bound"] is not None and arm["label"] in results]
        if bounded:
            out += ["", "## Equal packet budget", "",
                    f"One budget of N = {meta['budget']['tokens']} est. tokens, mapped onto each "
                    "method's own flags. Every error-free packet of every arm is checked against "
                    "its bound; the run exits 3 if one is larger.", "",
                    "| arm | method | budget flags | how N maps | bound | largest packet | check |",
                    "| --- | --- | --- | --- | --- | --- | --- |"]
            for arm in bounded:
                worst = arm.get("observed_max")
                ok = worst is None or worst <= arm["bound"]
                out.append(f"| `{arm['label']}` | {arm['method']} | `{' '.join(arm['budget_flags'])}` "
                           f"| {arm['mapping']} | {arm['bound']} | {fmt(worst)} "
                           f"| {'ok' if ok else 'FAILED'} |")
    if skipped or notes:
        out += ["", "## Notes", ""]
        out += [f"- {n}" for n in notes]
    out += ["", "Limits: one fictional vault, author-written questions, lexical `must_contain` "
            "matching (a paraphrased passage earns nothing), and token counts are estimates. "
            "See `bench/README.md`.", ""]
    return "\n".join(out)


def git_head() -> str:
    try:
        done = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(REPO),
                              capture_output=True, text=True, timeout=10)
        # The scorer lives in bench/, so a change there makes the run "dirty" too; the
        # result files the run itself writes (bench/results/) do not.
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "context_layer", "router",
                                "eval", "bench", ":(exclude)bench/results"],
                               cwd=str(REPO), capture_output=True, text=True, timeout=10)
        head = done.stdout.strip() or "unknown"
        return head + ("+dirty" if dirty.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def scorer_sha256() -> str:
    """One digest over the scorer's own files (path, NUL, bytes, NUL for each)."""
    digest = hashlib.sha256()
    for name in SCORER_FILES:
        digest.update(name.encode("utf-8") + b"\0" + (REPO / name).read_bytes() + b"\0")
    return digest.hexdigest()


def environment() -> dict:
    """Where the numbers were produced: no host name, user or path."""
    return {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
            "unicode": unicodedata.unidata_version, "platform": platform.platform(),
            "scorer_sha256": scorer_sha256()}


def vault_texts(vault: Path) -> tuple[dict[str, int], dict[str, str]]:
    file_chars = {p.relative_to(vault).as_posix(): len(p.read_text(encoding="utf-8"))
                  for p in vault.rglob("*.md")}
    file_texts = {}
    for p in vault.rglob("*.md"):
        try:
            file_texts[p.relative_to(vault).as_posix()] = p.read_bytes().decode("utf-8")
        except UnicodeError:
            continue
    return file_chars, file_texts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--methods", default="grep,fts,synaptic",
                        help="Comma-separated search methods (default: grep,fts,synaptic).")
    parser.add_argument("--budget-tokens", type=int, default=None,
                        help="One packet budget N in est. tokens, mapped per method: grep/fts "
                             "--budget 4N; synaptic --compact --budget-tokens N, plus a labelled "
                             "synaptic-extra arm (--budget 4(N-E) --extra-tokens E). Every packet "
                             "is checked against N; exit 3 if one is larger.")
    parser.add_argument("--extra-tokens", type=int, default=None,
                        help="With --budget-tokens: E for the synaptic-extra arm (0 < E < N; "
                             "default: the CLI's default share 600/2100 of N).")
    parser.add_argument("--top-k", type=int, default=None, help="Passed through as --top-k.")
    parser.add_argument("--search-arg", action="append", default=None,
                        help="Extra argument forwarded to every search call (repeatable). "
                             "Flags a method would not apply are refused.")
    parser.add_argument("--only", default=None, help="Comma-separated case ids to run.")
    parser.add_argument("--out", type=Path, default=RESULTS, help="Output directory.")
    parser.add_argument("--allow-unsealed", action="store_true",
                        help="Run even if cases or vault differ from SEAL.md (results are marked).")
    args = parser.parse_args(argv)

    methods = [m.strip() for m in args.methods.split(",") if m.strip()]
    if args.budget_tokens is not None and args.budget_tokens < 2:
        parser.error("--budget-tokens must be at least 2")
    if args.extra_tokens is not None:
        if args.budget_tokens is None:
            parser.error("--extra-tokens is used only with --budget-tokens")
        if not 0 < args.extra_tokens < args.budget_tokens:
            parser.error("--extra-tokens E needs 0 < E < --budget-tokens N")
    problems_args = check_search_args(methods, list(args.search_arg or []), args.budget_tokens)
    if problems_args:
        for problem in problems_args:
            print(f"run_offline: {problem}", file=sys.stderr)
        return 2

    problems = seal.check()
    if problems and not args.allow_unsealed:
        for problem in problems:
            print("run_offline: seal mismatch:", problem, file=sys.stderr)
        print("run_offline: refusing to score unsealed cases (use --allow-unsealed to override)",
              file=sys.stderr)
        return 2
    cases = seal.load_cases(CASES)
    if args.only:
        wanted = [c for c in args.only.split(",") if c]
        unknown = sorted(set(wanted) - {c["id"] for c in cases})
        if unknown:
            print(f"run_offline: unknown case id(s): {', '.join(unknown)}", file=sys.stderr)
            return 2
        cases = [c for c in cases if c["id"] in set(wanted)]
    cases_sha = seal.sha256_bytes(CASES.read_bytes())
    manifest_sha = seal.sha256_bytes((BENCH / "vault.sha256").read_bytes())
    arms = plan_arms(methods, args)

    results: dict[str, dict] = {}
    skipped: dict[str, str] = {}
    notes: list[str] = [f"seal mismatch: {p}" for p in problems]
    with tempfile.TemporaryDirectory(prefix="cl-bench-") as tmp:
        vault = prepare_vault(Path(tmp))
        file_chars, file_texts = vault_texts(vault)
        for arm in arms:
            skip = probe_arm(vault, arm, cases[0]["question"])
            if skip:
                notes.append(skip)
                skipped[arm["label"]] = skip
                print(f"run_offline: {skip}", file=sys.stderr)
                continue
            rows = run_method(vault, arm["method"], cases, arm["flags"], file_chars, file_texts,
                              label=arm["label"])
            by_type = {t: aggregate([r for r in rows if r["type"] == t])
                       for t in TYPES if any(r["type"] == t for r in rows)}
            results[arm["label"]] = {"search_flags": arm["flags"], "aggregate": aggregate(rows),
                                     "by_type": by_type, "rows": rows}

    failures = budget_check(arms, results)
    command = "python3 bench/run_offline.py" + (f" --methods {args.methods}"
                                                if args.methods != "grep,fts,synaptic" else "")
    if args.budget_tokens is not None:
        command += f" --budget-tokens {args.budget_tokens}"
    if args.extra_tokens is not None:
        command += f" --extra-tokens {args.extra_tokens}"
    if args.top_k is not None:
        command += f" --top-k {args.top_k}"
    for extra_arg in args.search_arg or []:
        command += f" --search-arg={extra_arg}"
    if args.only:
        command += f" --only {args.only}"
    meta = {"schema": SCHEMA, "command": command, "git_head": git_head(),
            "cases_sha256": cases_sha, "manifest_sha256": manifest_sha, "sealed": not problems,
            "cases": len(cases), "answerable": sum(1 for c in cases if c["gold"]),
            "unanswerable": sum(1 for c in cases if not c["gold"]),
            "est_tokens": f"ceil(chars / {CHARS_PER_TOKEN}); an estimate, not a tokenizer count",
            **environment()}
    if args.budget_tokens is not None:
        meta["budget"] = {"tokens": args.budget_tokens, "chars_per_token": CHARS_PER_TOKEN,
                          "arms": {arm["label"]: {"method": arm["method"],
                                                  "budget_flags": arm["budget_flags"],
                                                  "bound_est_tokens": arm["bound"],
                                                  "largest_est_tokens": arm.get("observed_max")}
                                   for arm in arms if arm["label"] in results},
                          "check": "failed" if failures else "ok", "failures": failures}
    args.out.mkdir(parents=True, exist_ok=True)
    for label, data in results.items():
        payload = {**meta, "method": label, **data}
        (args.out / f"{label}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        (args.out / f"{label}.run").write_text(run_text(data["rows"], label), encoding="utf-8")
    for label, reason in skipped.items():
        (args.out / f"{label}.json").write_text(
            json.dumps({**meta, "method": label, "skipped": reason}, indent=2,
                       ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    (args.out / "qrels.txt").write_text(qrels_text(cases), encoding="utf-8")
    summary = summary_markdown(results, skipped, notes, meta, arms)
    (args.out / "SUMMARY.md").write_text(summary, encoding="utf-8")
    print(summary)
    if failures:
        for failure in failures:
            print(f"run_offline: equal-budget check FAILED: {failure}", file=sys.stderr)
        return 3
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
