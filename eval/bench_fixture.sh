#!/usr/bin/env sh
# Reproduce the fixture comparison in BENCHMARK_FIXTURE.md with one command:
#
#     sh bench_fixture.sh
#
# It runs every system in the table against the SAME stimulus set with the SAME
# metrics, writes the raw JSON into run-output/ (git-ignored), and prints the
# table. It measures a seven-file toy corpus. It is a self-test of this harness,
# not a benchmark of retrieval methods in general -- see BENCHMARK_FIXTURE.md.
set -eu

cd "$(dirname "$0")"
OUT="${OUT:-run-output}"
STIM="${STIM:-stimulus-set.example.jsonl}"
mkdir -p "$OUT"
rm -f "$OUT"/.eval-fts-index.sqlite3

run() {  # run <label> <command...>
  label="$1"; shift
  command=$(python3 -c 'import shlex, sys; print(shlex.join(sys.argv[1:]))' "$@")
  python3 evaluate.py --command "$command" --stimuli "$STIM" \
    --out "$OUT/$label.json" --quiet > "$OUT/$label.txt" 2>&1
  printf '  measured %s\n' "$label" >&2
}

echo "Measuring on $STIM ..." >&2
run grep-k3      python3 adapters/grep_baseline.py --vault fixtures/docs --top-k 3
run grep-k6      python3 adapters/grep_baseline.py --vault fixtures/docs --top-k 6
run fts-k3       python3 adapters/fts_sqlite.py --vault fixtures/docs --top-k 3 --index "$OUT/.eval-fts-index.sqlite3"
run fts-k6       python3 adapters/fts_sqlite.py --vault fixtures/docs --top-k 6 --index "$OUT/.eval-fts-index.sqlite3"
run router      python3 fixtures/demo_router.py
run router-full python3 fixtures/demo_router.py --full

echo "Scoring the 7-axis rubric on the 6 example cases ..." >&2
rubric() {  # rubric <label> <command...>
  label="$1"; shift
  command=$(python3 -c 'import shlex, sys; print(shlex.join(sys.argv[1:]))' "$@")
  python3 fixtures/gen_packets.py --cases cases.example.json --command "$command" \
    --out-dir "$OUT/packets-$label" > /dev/null
  python3 score_packet.py --cases cases.example.json \
    --batch-dir "$OUT/packets-$label" --out "$OUT/rubric-$label.json" > /dev/null
  printf '  scored %s\n' "$label" >&2
}
rubric grep   python3 adapters/grep_baseline.py --vault fixtures/docs --top-k 3
rubric fts    python3 adapters/fts_sqlite.py --vault fixtures/docs --top-k 3 --index "$OUT/.eval-fts-index.sqlite3"
rubric router python3 fixtures/demo_router.py

python3 - "$OUT" <<'PY'
import json, sys, pathlib
out = pathlib.Path(sys.argv[1])
rows = [
    ("grep_baseline (top-k 3)", "grep-k3"),
    ("grep_baseline (top-k 6)", "grep-k6"),
    ("fts_sqlite BM25 (top-k 3)", "fts-k3"),
    ("fts_sqlite BM25 (top-k 6)", "fts-k6"),
    ("demo_router (default)", "router"),
    ("demo_router --full", "router-full"),
]
print()
print("| System | hit rate | prompts fully hit | zero-hit prompts | mean cost (chars) | cost per hit |")
print("|---|---:|---:|---:|---:|---:|")
for label, key in rows:
    s = json.loads((out / f"{key}.json").read_text())["summary"]
    cph = f"{s['cost_per_hit_chars']:.0f}" if s["cost_per_hit_chars"] else "n/a"
    print(f"| `{label}` | {s['total_hit']}/{s['total_expected']} ({s['hit_rate']*100:.1f}%) "
          f"| {s['fully_hit_prompts']}/{s['n']} | {s['zero_hit_prompts']}/{s['n']} "
          f"| {s['mean_cost_chars']:.0f} | {cph} |")
print()
print("| System | rubric total | content axes 1-4 | sourcing (axis 5) | waste (axis 7) |")
print("|---|---:|---:|---:|---:|")
for label, key in [("grep_baseline (top-k 3)", "grep"),
                   ("fts_sqlite BM25 (top-k 3)", "fts"),
                   ("demo_router (default)", "router")]:
    a = json.loads((out / f"rubric-{key}.json").read_text())["aggregate"]
    ax = a["per_axis"]
    names = ["decisive_fact", "timeliness", "unstated_constraint", "pitfall"]
    content = sum(ax[n]["earned"] for n in names)
    content_max = sum(ax[n]["applicable_n"] * 2 for n in names)
    def cell(axis):
        return f"{ax[axis]['earned']}/{ax[axis]['applicable_n'] * 2}"
    print(f"| `{label}` | {a['points_earned']}/{a['points_possible']} "
          f"({a['score_ratio']*100:.1f}%) | {content}/{content_max} | "
          f"{cell('sourcing_grade')} | {cell('waste')} |")
print()
PY
