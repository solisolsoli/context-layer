#!/usr/bin/env python3
"""Every number the docs quote from a result file or a measuring command, recomputed.

Sources and what is checked against them:

- bench/results/SUMMARY.md (committed) and the sealed files: the README's
  benchmark table and sentences. With DOC_CLAIMS_BENCH_DIR pointing at fresh
  runs (CI's bench-reproduce job writes <dir>/default and <dir>/compact), the
  same rows are checked against those runs, including the `--compact` row,
  which no committed file holds.
- eval/live-pilot-0.3/ (answers and blind-judging files): the README's pilot
  sentence and every cell of eval/LIVE_PILOT_0.3.md's two tables.
- eval/LIVE_COMPARE.md (a private run; the raw data is not in the
  repository): the README's 0.2 ratios are recomputed from the table there,
  and the README must say where they come from.
- eval/orchestration_cost.py --json: the README's sub-agent sentence and the
  numbers of docs/subagents.md section 5.
- bench/INSPECTIONS.md: the README's count of design-time looks.
- eval/evaluate.py (README quickstart step 1): eval/README.md's output tail.
- eval/comparison/: the counts in eval/comparison/README.md.
- Also: the Makefile's demo runs and delivers verbatim evidence; the
  repository's AGENTS.md and CLAUDE.md are byte-identical; every flag of
  `search` has a row in eval/README.md.

A wrong number fails; nothing here is skipped because the doc is known to be stale.

Every command runs on committed fixtures with HOME in a temporary directory.
No sealed benchmark run happens here unless DOC_CLAIMS_BENCH_DIR is given.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import unittest

from _portable_helpers import isolated_home_env

REPO = Path(os.environ.get("TEST_REPO_HOME", Path(__file__).resolve().parents[1]))
BENCH_DIR = os.environ.get("DOC_CLAIMS_BENCH_DIR")
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
         "eight": 8, "nine": 9, "ten": 10}
NUMBER = re.compile(r"(?<![\w.,])(\d{1,3}(?:,\d{3})+|\d{4,})(?![\w,]|\.\d)")


def read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


def run(*argv, cwd=REPO):
    home = tempfile.mkdtemp(prefix="cl-doc-claims-")
    try:
        env = dict(isolated_home_env(os.environ, home), PYTHONHASHSEED="0")
        env.pop("CONTEXT_LAYER_HOME", None)
        done = subprocess.run([sys.executable, *argv], cwd=cwd, env=env, capture_output=True,
                              text=True, encoding="utf-8", timeout=600)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if done.returncode != 0:
        raise AssertionError(f"{' '.join(argv)} exited {done.returncode}: {done.stderr[-1500:]}")
    return done.stdout


def run_sh(script, cwd=REPO, **env_extra):
    home = tempfile.mkdtemp(prefix="cl-doc-claims-")
    try:
        env = dict(isolated_home_env(os.environ, home), PYTHONHASHSEED="0", **env_extra)
        done = subprocess.run(["sh", script], cwd=cwd, env=env, capture_output=True, text=True,
                              encoding="utf-8", timeout=600)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if done.returncode != 0:
        raise AssertionError(f"sh {script} exited {done.returncode}: {done.stderr[-1500:]}")
    return done.stdout


def section(text, start, end=None):
    """The text from the first line containing `start` up to `end` (exclusive)."""
    at = text.index(start)
    stop = text.index(end, at + len(start)) if end else len(text)
    return text[at:stop]


def table_rows(text):
    """Markdown table rows as lists of stripped cells, bold markers removed."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("|") and not re.match(r"^\|\s*-", line):
            rows.append([c.strip().replace("**", "") for c in line.strip("|").split("|")])
    return rows


def flat(text):
    """One line, single spaces, no bold markers: for sentences that wrap in the Markdown."""
    return re.sub(r"\s+", " ", text.replace("**", ""))


def as_int(cell):
    return int(cell.replace(",", "").replace("$", "").strip())


def rounded(value):
    """Integers a careful author may print for `value`: half to even or half up."""
    return {round(value), math.floor(value + 0.5)}


def numbers(text):
    """Comma-grouped or four-digit numbers, skipping ones the text calls approximate."""
    found = []
    for match in NUMBER.finditer(text):
        before = text[max(0, match.start() - 8):match.start()].lower()
        if "about" in before or "~" in before:
            continue
        found.append(int(match.group(1).replace(",", "")))
    return found


# ---------------------------------------------------------------------------
# The sealed offline benchmark
# ---------------------------------------------------------------------------

def summary_tables(text):
    """Parsed bench SUMMARY: overall rows, bridge row, paired rows."""
    overall, bridge, paired = {}, {}, {}
    methods = []
    for cells in table_rows(section(text, "## Overall", "## Complete by case type")):
        if cells[0].startswith("`"):
            method = cells[0].strip("`")
            k, n, pct = re.match(r"(\d+)/(\d+) \((\d+)%\)", cells[1]).groups()
            overall[method] = {"complete": int(k), "answerable": int(n), "pct": int(pct),
                               "mean_est_tokens": float(cells[6])}
    by_type = table_rows(section(text, "## Complete by case type", "## Unanswerable cases"))
    header = by_type[0]
    methods = [h.strip("`") for h in header[2:]]
    for cells in by_type[1:]:
        if cells[0] == "`bridge_2hop`":
            bridge = {"n": int(cells[1])}
            for method, cell in zip(methods, cells[2:]):
                bridge[method] = int(cell.split()[0])
    if "## Paired completeness" in text:
        for cells in table_rows(section(text, "## Paired completeness", "## Flags passed")):
            m = re.match(r"`([^`]+)` vs `([^`]+)`", cells[0])
            if m:
                paired[m.groups()] = {"a_only": int(cells[1]), "b_only": int(cells[2]),
                                      "p": float(cells[5])}
    return overall, bridge, paired


def summary_from_json(directory, method):
    data = json.loads((Path(directory) / f"{method}.json").read_text(encoding="utf-8"))
    a = data["aggregate"]
    bridge = data["by_type"]["bridge_2hop"]
    return {"complete": a["complete"], "answerable": a["answerable"],
            "pct": round(100 * a["complete"] / a["answerable"]),
            "mean_est_tokens": a["mean_est_tokens"], "bridge": bridge["complete"],
            "bridge_n": bridge["answerable"]}


class SealedBenchmark(unittest.TestCase):
    ROW_LABELS = {"grep": "grep", "FTS (default)": "fts", "synaptic (FTS + link extras)": "synaptic",
                  "synaptic `--compact`": "compact"}

    @classmethod
    def setUpClass(cls):
        cls.readme = read("README.md")
        cls.block = section(cls.readme, "**Sealed offline benchmark**", "**Live host runs**")
        cls.summary = read("bench/results/SUMMARY.md")
        cls.overall, cls.bridge, cls.paired = summary_tables(cls.summary)
        cls.rows = {}
        for cells in table_rows(cls.block):
            if cells[0] in cls.ROW_LABELS:
                cls.rows[cls.ROW_LABELS[cells[0]]] = cells

    def check_row(self, name, expected):
        cells = self.rows[name]
        k, pct = re.match(r"(\d+) \((\d+)%\)", cells[1]).groups()
        self.assertEqual((int(k), int(pct)), (expected["complete"], expected["pct"]), name)
        self.assertEqual(int(cells[2]), expected["bridge"], name)
        self.assertIn(int(cells[3]), rounded(expected["mean_est_tokens"]), name)

    def test_table_rows_match_the_committed_summary(self):
        self.assertEqual(set(self.rows) - {"compact"}, {"grep", "fts", "synaptic"})
        for name in ("grep", "fts", "synaptic"):
            self.check_row(name, {**self.overall[name], "bridge": self.bridge[name]})

    def test_table_header_counts(self):
        header = next(cells for cells in table_rows(self.block) if cells[0] == "Method")
        self.assertIn(f"({self.overall['fts']['answerable']} answerable)", header[1])
        self.assertIn(f"({self.bridge['n']})", header[2])

    def test_paired_sentence(self):
        pair = self.paired[("fts", "synaptic")]
        m = re.search(r"(\d+) cases only synaptic completes, (\d+) only FTS\s+completes "
                      r"\(exact sign test p = ([0-9.]+)\)", self.block)
        self.assertIsNotNone(m, "the paired sentence moved; update this test")
        self.assertEqual((int(m.group(1)), int(m.group(2)), float(m.group(3))),
                         (pair["b_only"], pair["a_only"], pair["p"]))

    def test_cost_sentence(self):
        m = re.search(r"about (\d+)% more packet\s+tokens", self.block)
        extra = 100 * (self.overall["synaptic"]["mean_est_tokens"] / self.overall["fts"]["mean_est_tokens"] - 1)
        self.assertIn(int(m.group(1)), rounded(extra))

    def test_vault_and_case_counts(self):
        m = re.search(r"fictional\s+(\d+)-note vault and (\d+) questions", self.block)
        notes = [line for line in read("bench/vault.sha256").splitlines() if line.endswith(".md")]
        cases = [line for line in read("bench/cases.jsonl").splitlines() if line.strip()]
        self.assertEqual((int(m.group(1)), int(m.group(2))), (len(notes), len(cases)))

    def test_default_bounds(self):
        m = re.search(r"Default bounds: top-k (\d+), ([\d,]+) characters", self.block)
        source = read("eval/retrieve.py")
        top_k = re.search(r"'--top-k', type=int, default=(\d+)", source).group(1)
        budget = re.search(r"'--budget', type=int, default=(\d+)", source).group(1)
        self.assertEqual((int(m.group(1)), as_int(m.group(2))), (int(top_k), int(budget)))

    def test_inspection_count_matches_the_ledger(self):
        ledger = read("bench/INSPECTIONS.md")
        looks = sum(1 for cells in table_rows(ledger) if len(cells) > 2 and cells[2] == "design look")
        stated = re.search(r"\*\*Design-time looks at a candidate so far: (\d+)\*\*", ledger)
        self.assertEqual(int(stated.group(1)), looks)
        m = re.search(r"\b(" + "|".join(WORDS) + r")\s+(?:times|design-time\s+looks?)", self.block)
        self.assertIsNotNone(m, "the README no longer states how often the sealed set was looked at")
        self.assertEqual(WORDS[m.group(1)], looks)
        bench = flat(read("bench/README.md"))
        m = re.search(r"\b(" + "|".join(WORDS) + r") design-time looks", bench)
        self.assertIsNotNone(m, "bench/README.md no longer states the number of design-time looks")
        self.assertEqual(WORDS[m.group(1)], looks)

    @unittest.skipUnless(BENCH_DIR, "needs DOC_CLAIMS_BENCH_DIR with fresh runs (CI bench-reproduce)")
    def test_rows_match_fresh_runs(self):
        for name in ("grep", "fts", "synaptic"):
            fresh = summary_from_json(Path(BENCH_DIR) / "default", name)
            self.check_row(name, fresh)
        fresh = summary_from_json(Path(BENCH_DIR) / "compact", "synaptic")
        self.check_row("compact", fresh)

    def test_compact_row_provenance(self):
        # No committed file holds the --compact run; CI regenerates it (test_rows_match_fresh_runs).
        self.assertIn("compact", self.rows)
        if not (REPO / ".github" / "workflows" / "tests.yml").is_file():
            self.skipTest("a source archive carries no .github/ (the checkout does)")
        ci = read(".github/workflows/tests.yml")
        self.assertIn("--methods synaptic --search-arg=--compact", ci)


# ---------------------------------------------------------------------------
# Live host runs
# ---------------------------------------------------------------------------

ARMS = ("baseline", "hook-fts", "hook-synaptic")
TYPES = {"single-hop": "single_hop", "two-hop bridge": "bridge_2hop",
         "aggregation": "multi_note_aggregation", "supersession": "supersession",
         "distractor": "distractor", "unanswerable": "unanswerable"}


def pilot():
    base = REPO / "eval" / "live-pilot-0.3"
    rows = [json.loads(line) for line in (base / "answers.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    key = json.loads((base / "blind-key.json").read_text(encoding="utf-8"))
    judged = {r["blind_id"]: r["verdict"] for r in
              map(json.loads, (base / "blind-judged.jsonl").read_text(encoding="utf-8").splitlines())}
    verdict = {(v["id"], v["arm"]): judged[blind] for blind, v in key.items()}
    stats = {}
    for arm in ARMS:
        mine = [r for r in rows if r["arm"] == arm]
        totals = [r["tokens"]["total"] for r in mine]
        stats[arm] = {"n": len(mine), "correct": sum(verdict[(r["id"], arm)] == "correct" for r in mine),
                      "mean": statistics.mean(totals), "median": statistics.median(totals),
                      "turns": statistics.mean(r["num_turns"] for r in mine),
                      "cost": sum(r["cost_usd"] for r in mine),
                      "types": {t: [r["tokens"]["total"] for r in mine if r["type"] == t]
                                for t in TYPES.values()}}
    return stats


@unittest.skipUnless((REPO / "eval" / "live-pilot-0.3").is_dir(), "the pilot files are repository-only")
class LivePilot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stats = pilot()
        cls.doc = read("eval/LIVE_PILOT_0.3.md")

    def test_result_table(self):
        base = self.stats["baseline"]["mean"]
        rows = {cells[0]: cells for cells in table_rows(section(self.doc, "| Arm |", "Means and medians"))}
        for arm in ARMS:
            s, cells = self.stats[arm], rows[arm]
            self.assertEqual(cells[1], f"{s['correct']}/{s['n']}", arm)
            self.assertIn(as_int(cells[2]), rounded(s["mean"]), arm)
            self.assertIn(int(cells[3].rstrip("%")), rounded(100 * s["mean"] / base), arm)
            self.assertIn(as_int(cells[4]), rounded(s["median"]), arm)
            self.assertEqual(cells[5], f"{s['turns']:.1f}", arm)
            self.assertEqual(cells[6], f"${s['cost']:.2f}", arm)

    def test_by_type_table(self):
        rows = table_rows(section(self.doc, "| Type | n |", "## What this shows"))
        seen = set()
        for cells in rows[1:]:
            kind = TYPES[cells[0]]
            seen.add(kind)
            self.assertEqual(int(cells[1]), len(self.stats["baseline"]["types"][kind]))
            for arm, cell in zip(ARMS, cells[2:]):
                self.assertIn(as_int(cell), rounded(statistics.mean(self.stats[arm]["types"][kind])),
                              (kind, arm))
        self.assertEqual(seen, set(TYPES.values()))

    def fewer(self, arm, kind=None, than="baseline"):
        mean = (lambda a: statistics.mean(self.stats[a]["types"][kind])) if kind else \
               (lambda a: self.stats[a]["mean"])
        return 100 * (1 - mean(arm) / mean(than))

    def test_text_claims(self):
        m = re.search(r"with (\d+)[–-](\d+)% fewer total tokens", self.doc)
        self.assertIn(int(m.group(1)), rounded(self.fewer("hook-fts")))
        self.assertIn(int(m.group(2)), rounded(self.fewer("hook-synaptic")))
        m = re.search(r"synaptic hook used\s+(\d+)% fewer tokens than the FTS hook \((\d+)% fewer than "
                      r"baseline\)", self.doc)
        self.assertIn(int(m.group(1)), rounded(self.fewer("hook-synaptic", "bridge_2hop", "hook-fts")))
        self.assertIn(int(m.group(2)), rounded(self.fewer("hook-synaptic", "bridge_2hop")))
        m = re.search(r"\(\$([\d.]+) vs \$([\d.]+)\)", self.doc)
        self.assertEqual((m.group(1), m.group(2)), (f"{self.stats['hook-synaptic']['cost']:.2f}",
                                                     f"{self.stats['hook-fts']['cost']:.2f}"))

    def test_readme_pilot_sentence(self):
        block = flat(section(read("README.md"), "*0.3 pilot", "**Sub-agent payloads**"))
        problems = []
        if f"answered {self.stats['baseline']['correct']}/24" not in block:
            problems.append("the 24/24 correctness")
        m = re.search(r"were ([\d,]+) \(host alone\), ([\d,]+) with the FTS hook \((\d+)%\) and ([\d,]+) "
                      r"with the synaptic hook \((\d+)%\)", block)
        base = self.stats["baseline"]["mean"]
        for value, arm in ((m.group(1), "baseline"), (m.group(2), "hook-fts"), (m.group(4), "hook-synaptic")):
            if as_int(value) not in rounded(self.stats[arm]["mean"]):
                problems.append(f"{arm} mean {value} (recorded {self.stats[arm]['mean']:.2f})")
        for value, arm in ((m.group(3), "hook-fts"), (m.group(5), "hook-synaptic")):
            if int(value) not in rounded(100 * self.stats[arm]["mean"] / base):
                problems.append(f"{arm} share {value}%")
        m = re.search(r"synaptic hook used (\d+)% fewer tokens than the FTS hook", block)
        if int(m.group(1)) not in rounded(self.fewer("hook-synaptic", "bridge_2hop", "hook-fts")):
            problems.append("the bridge saving")
        if not all(self.stats[a]["turns"] <= 0.6 * self.stats["baseline"]["turns"] for a in ARMS[1:]):
            problems.append("'about half the turns'")
        self.assertEqual(problems, [])


class LiveCompare02(unittest.TestCase):
    """The 0.2 run used a private vault: ratios are recomputed, provenance is required."""

    def test_ratios_and_provenance(self):
        table = {cells[0]: cells for cells in table_rows(section(read("eval/LIVE_COMPARE.md"),
                                                                "## Recorded run", "Read with care"))}
        base, hook = as_int(table["mean total tokens"][1]), as_int(table["mean total tokens"][3])
        turns_base, turns_hook = float(table["mean turns"][1]), float(table["mean turns"][3])
        block = flat(section(read("README.md"), "*0.2, one private vault", "*0.3 pilot"))
        m = re.search(r"about (\d+)% of the baseline's mean total tokens \(about (\d+)% fewer\)", block)
        self.assertIn(int(m.group(1)), rounded(100 * hook / base))
        self.assertIn(int(m.group(2)), rounded(100 * (1 - hook / base)))
        self.assertIn("half the turns", block)
        self.assertLessEqual(turns_hook / turns_base, 0.55)
        for provenance in ("one private vault", "N = 12", "eval/LIVE_COMPARE.md"):
            self.assertIn(provenance, block)


# ---------------------------------------------------------------------------
# Sub-agent payload estimates
# ---------------------------------------------------------------------------

def int_values(value, out):
    if isinstance(value, bool):
        return out
    if isinstance(value, int):
        out.add(value)
    elif isinstance(value, dict):
        for item in value.values():
            int_values(item, out)
    elif isinstance(value, list):
        for item in value:
            int_values(item, out)
    return out


class SubagentPayloads(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.standin = json.loads(run("eval/orchestration_cost.py", "--standin", "--json"))
        cls.rules = json.loads(run("eval/orchestration_cost.py", "--rules", "templates/vault/CLAUDE.md",
                                   "--json"))
        cls.default = json.loads(run("eval/orchestration_cost.py", "--standin", "--packet", "default",
                                     "--json"))
        cls.values = (int_values(cls.standin, set()) | int_values(cls.rules, set())
                      | int_values(cls.default, set()))
        # Budgets the docs name are settings in the script itself, not outputs.
        cls.values |= {int(v) for v in re.findall(r"budget_tokens=(\d+)", read("eval/orchestration_cost.py"))}

    def test_readme_sentence(self):
        block = section(read("README.md"), "**Sub-agent payloads**", "## What it does not claim")
        problems = [f"{n:,} is not in orchestration_cost.py --standin --json" for n in numbers(block)
                    if n not in int_values(self.standin, set())]
        self.assertEqual(problems, [])

    def test_subagents_section_5(self):
        doc = read("docs/subagents.md")
        block = section(doc, "## 5. Measured", "How to read these tables")
        problems = [f"{n:,} is not in orchestration_cost.py --json output" for n in numbers(block)
                    if n not in self.values]
        gold = self.standin["gold_fact_in_payload"]
        first = table_rows(section(block, "| Arm (4 workers)", "| Root verification"))
        for cells in first[1:]:
            arm = cells[0].split(":")[0].strip()
            m = re.match(r"(\d+)/4", cells[-1])
            if arm in gold and m and int(m.group(1)) != gold[arm]:
                problems.append(f"{arm}: gold fact {m.group(0)}, the script says {gold[arm]}/4")
        self.assertEqual(problems, [])


# ---------------------------------------------------------------------------
# Harness examples, demo, repository rules, flag docs
# ---------------------------------------------------------------------------

class HarnessExamples(unittest.TestCase):
    def test_eval_readme_output_tail(self):
        out = run("evaluate.py", "--command", "python3 fixtures/demo_router.py", "--stimuli",
                  "stimulus-set.example.jsonl", cwd=REPO / "eval")
        printed = section(out, "=== SUMMARY ===", "\n\n").strip().splitlines()
        documented = section(read("eval/README.md"), "=== SUMMARY ===", "```").strip().splitlines()
        self.assertEqual(documented, printed)

    def test_benchmark_fixture_numbers(self):
        with tempfile.TemporaryDirectory(prefix="cl-fixture-out-") as parent:
            out = str(Path(parent) / "output with spaces")
            printed = run_sh("bench_fixture.sh", cwd=REPO / "eval", OUT=out)
        hits_table = section(printed, "| System | hit rate", "| System | rubric total")
        rubric_table = printed[printed.index("| System | rubric total"):]
        hits = {c[0].strip("`"): (re.match(r"\d+/\d+", c[1]).group(0), c[2])
                for c in table_rows(hits_table)[1:]}
        rubric = {c[0].strip("`"): c[1:] for c in table_rows(rubric_table)[1:]}
        names = {"grep, top-k 3": "grep_baseline (top-k 3)", "grep, top-k 6": "grep_baseline (top-k 6)",
                 "FTS, top-k 3": "fts_sqlite BM25 (top-k 3)", "FTS, top-k 6": "fts_sqlite BM25 (top-k 6)",
                 "demo router": "demo_router (default)", "demo router, full": "demo_router --full"}
        doc = read("eval/BENCHMARK_FIXTURE.md")
        for cells in table_rows(section(doc, "| Method", "\n\n"))[1:]:
            self.assertEqual((cells[1], cells[2]), hits[names[cells[0]]], cells[0])
        text = flat(doc)
        base, fts = rubric["grep_baseline (top-k 3)"], rubric["fts_sqlite BM25 (top-k 3)"]
        router = rubric["demo_router (default)"]
        self.assertEqual(base[0].split(" ")[0], fts[0].split(" ")[0])
        self.assertIn(f"both baselines {base[0].split(' ')[0]} and the demo router "
                      f"{router[0].split(' ')[0]}", text)
        self.assertIn(f"both baselines score {base[1]} and the router {router[1]}", text)
        self.assertIn(f"(sourcing {router[2]} against {base[2]})", text)
        self.assertIn(f"waste ({router[3]} against {base[3]})", text.replace("offset by waste (", "waste ("))

    def test_comparison_readme_counts(self):
        doc = read("eval/comparison/README.md").replace("\n", " ")
        stimuli = [json.loads(line) for line in read("eval/comparison/stimuli.jsonl").splitlines() if line.strip()]
        contract = json.loads(read("eval/comparison/contract.json"))
        dev = sum(r["split"] == "dev" for r in stimuli)
        unanswerable = sum(not r["answerable"] for r in stimuli)
        multi = sum(len(q["required_groups"]) > 1 for q in contract["queries"].values())
        m = re.search(r"(\d+) questions .*? (\d+) development and (\d+) acceptance cases, (\d+) unanswerable "
                      r"and (\d+) multi-source cases", doc)
        self.assertEqual(tuple(int(g) for g in m.groups()),
                         (len(stimuli), dev, len(stimuli) - dev, unanswerable, multi))


class RepositoryRules(unittest.TestCase):
    def test_agents_and_claude_are_byte_identical(self):
        self.assertEqual((REPO / "AGENTS.md").read_bytes(), (REPO / "CLAUDE.md").read_bytes())

    def test_make_demo_delivers_verbatim_evidence(self):
        recipe = section(read("Makefile"), "\ndemo:", "\nlint")
        source = re.search(r"cp -R (\S+) \.demo-vault", recipe).group(1)
        prompt = re.search(r'--prompt "([^"]+)"', recipe).group(1)
        for step in ("context_layer.cli init .demo-vault", "context_layer.cli index .demo-vault",
                     "context_layer.cli search .demo-vault"):
            self.assertIn(step, recipe)
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "demo-vault"
            shutil.copytree(REPO / source, vault)
            run("-m", "context_layer.cli", "init", str(vault))
            run("-m", "context_layer.cli", "index", str(vault))
            packet = json.loads(run("-m", "context_layer.cli", "search", str(vault), "--prompt", prompt))
            self.assertEqual((packet["operation_status"], packet["status"]), ("ok", "PARTIAL"))
            self.assertTrue(packet["evidence"])
            for item in packet["evidence"]:
                raw = (vault / item["source_path"]).read_bytes()
                self.assertIn(item["content"], raw.decode("utf-8"))
                self.assertEqual(item["source_sha256"], hashlib.sha256(raw).hexdigest())

    def test_every_search_flag_has_a_row_in_eval_readme(self):
        help_text = run("eval/retrieve.py", "--help")
        flags = set(re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", help_text)) - {"--help", "--vault"}
        flags |= {"--prompt", "--method"}
        table = section(read("eval/README.md"), "## Flags of `context-layer search`",
                        "`context-layer eval` defines")
        documented = set(re.findall(r"^\| `(--[a-z0-9-]+)", table, re.M))
        missing = sorted(flags - documented)
        self.assertEqual(missing, [], "add a row for each to the search flag table in eval/README.md")


if __name__ == "__main__":
    unittest.main()
