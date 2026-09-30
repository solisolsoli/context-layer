#!/usr/bin/env python3
"""Validate and seal the benchmark cases and the fictional vault.

    python3 bench/seal.py validate   # every must_contain is verbatim in its note
    python3 bench/seal.py write      # validate, then write vault.sha256 and SEAL.md
    python3 bench/seal.py check      # recompute the hashes and compare with SEAL.md

The seal pins two things: the exact bytes of cases.jsonl and a manifest that
lists every vault file with its SHA-256. A stranger can rerun `check` on a
fresh clone and know they are scoring the same cases against the same notes.

Python 3.10+; standard library only.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys

BENCH = Path(__file__).resolve().parent
VAULT = BENCH / "vault"
CASES = BENCH / "cases.jsonl"
MANIFEST = BENCH / "vault.sha256"
SEAL = BENCH / "SEAL.md"

TYPES = ("single_hop", "bridge_2hop", "multi_note_aggregation",
         "supersession", "unanswerable", "distractor")
# Words too common to count as a note's "distinctive" vocabulary.
COMMON = {"what", "which", "when", "where", "does", "have", "with", "that", "this",
          "from", "they", "their", "will", "would", "should", "into", "about", "after",
          "been", "there", "whose", "whom", "much", "many", "long", "each", "other"}
# A term found in at most this many notes is treated as distinctive.
DISTINCTIVE_DF = 4


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def vault_files() -> list[Path]:
    return sorted((p for p in VAULT.rglob("*") if p.is_file()),
                  key=lambda p: p.relative_to(VAULT).as_posix())


def manifest_text() -> str:
    lines = [f"{sha256_bytes(p.read_bytes())}  {p.relative_to(VAULT).as_posix()}"
             for p in vault_files()]
    return "\n".join(lines) + "\n"


def load_cases(path: Path = CASES) -> list[dict]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"cases.jsonl line {number}: not an object")
            rows.append(row)
    return rows


def terms(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.casefold()) if len(t) >= 4} - COMMON


def validate(cases: list[dict]) -> list[str]:
    errors: list[str] = []
    notes = {p.relative_to(VAULT).as_posix(): p.read_text(encoding="utf-8")
             for p in vault_files() if p.suffix == ".md"}
    df: dict[str, int] = {}
    for text in notes.values():
        for term in terms(text):
            df[term] = df.get(term, 0) + 1
    seen = set()
    for case in cases:
        cid = case.get("id")
        if cid in seen:
            errors.append(f"{cid}: duplicate id")
        seen.add(cid)
        if case.get("type") not in TYPES:
            errors.append(f"{cid}: unknown type {case.get('type')!r}")
        if not isinstance(case.get("question"), str) or not case["question"].strip():
            errors.append(f"{cid}: empty question")
        gold = case.get("gold")
        if not isinstance(gold, list):
            errors.append(f"{cid}: gold must be a list")
            continue
        if (case.get("type") == "unanswerable") != (gold == []):
            errors.append(f"{cid}: gold must be empty exactly for unanswerable cases")
        for field in ("gold", "distractors"):
            for item in case.get(field) or []:
                path, needle = item.get("path"), item.get("must_contain")
                if path not in notes:
                    errors.append(f"{cid}: {field} path not in vault: {path}")
                elif not needle or needle not in notes[path]:
                    errors.append(f"{cid}: {field} must_contain not verbatim in {path}: {needle!r}")
        if case.get("type") == "bridge_2hop" and len(gold) >= 2:
            answer = gold[-1]["path"]
            if answer in notes:
                shared = sorted(t for t in terms(case["question"]) & terms(notes[answer])
                                if df.get(t, 0) <= DISTINCTIVE_DF)
                if shared:
                    errors.append(f"{cid}: bridge question shares distinctive terms "
                                  f"with its answer note {answer}: {shared}")
    return errors


def counts_by_type(cases: list[dict]) -> dict[str, int]:
    counts = {t: 0 for t in TYPES}
    for case in cases:
        counts[case["type"]] += 1
    return counts


def seal_text(cases_sha: str, manifest_sha: str, cases: list[dict], files: int) -> str:
    counts = counts_by_type(cases)
    rows = "\n".join(f"| `{t}` | {n} |" for t, n in counts.items())
    multi = counts["bridge_2hop"] + counts["multi_note_aggregation"]
    return f"""# Benchmark seal

The case file and the fictional vault were sealed before any retrieval method
was run against them. The scorer (`run_offline.py`) did not exist when this
file was written; it was added in a later commit.

| Artifact | SHA-256 |
| --- | --- |
| `bench/cases.jsonl` | `{cases_sha}` |
| `bench/vault.sha256` (manifest of {files} vault files, `sha256  path`) | `{manifest_sha}` |

Verify on a fresh clone:

```
python3 bench/seal.py check
```

## Cases by type

| Type | Cases |
| --- | --- |
{rows}
| **total** | **{len(cases)}** |

Bridge and aggregation cases: {multi} of {len(cases)} ({100 * multi / len(cases):.0f}%).

Validation at seal time (`python3 bench/seal.py validate`): every `must_contain`
string (gold and distractor) is present verbatim in the note it names, every
unanswerable case has empty gold, and no bridge question shares a distinctive
term (found in at most {DISTINCTIVE_DF} notes) with its answer note.
"""


def read_seal() -> tuple[str, str]:
    text = SEAL.read_text(encoding="utf-8")
    cases = re.search(r"`bench/cases\.jsonl` \| `([0-9a-f]{64})`", text)
    manifest = re.search(r"`bench/vault\.sha256`[^|]*\| `([0-9a-f]{64})`", text)
    if not cases or not manifest:
        raise ValueError("SEAL.md does not contain both hashes")
    return cases.group(1), manifest.group(1)


def check() -> list[str]:
    """Return a list of mismatches between the working tree and SEAL.md (empty = sealed)."""
    problems = []
    want_cases, want_manifest = read_seal()
    if sha256_bytes(CASES.read_bytes()) != want_cases:
        problems.append("cases.jsonl does not match the seal")
    if sha256_bytes(MANIFEST.read_bytes()) != want_manifest:
        problems.append("vault.sha256 does not match the seal")
    if MANIFEST.read_text(encoding="utf-8") != manifest_text():
        problems.append("vault files do not match vault.sha256")
    return problems


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "check"
    if command == "validate" or command == "write":
        cases = load_cases()
        errors = validate(cases)
        for error in errors:
            print("error:", error, file=sys.stderr)
        if errors:
            return 1
        print(f"ok: {len(cases)} cases valid; {counts_by_type(cases)}")
        if command == "write":
            manifest = manifest_text()
            MANIFEST.write_text(manifest, encoding="utf-8")
            text = seal_text(sha256_bytes(CASES.read_bytes()),
                             sha256_bytes(manifest.encode("utf-8")), cases,
                             manifest.count("\n"))
            SEAL.write_text(text, encoding="utf-8")
            print(f"wrote {MANIFEST.name} and {SEAL.name}")
        return 0
    if command == "check":
        problems = check()
        for problem in problems:
            print("seal mismatch:", problem, file=sys.stderr)
        if not problems:
            print("seal ok")
        return 1 if problems else 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
