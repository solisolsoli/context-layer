# Benchmark seal

The case file and the fictional vault were sealed before any retrieval method
was run against them. The scorer (`run_offline.py`) did not exist when this
file was written; it was added in a later commit.

| Artifact | SHA-256 |
| --- | --- |
| `bench/cases.jsonl` | `03a4f732129a3491038e29f6112070389cfcf979e67db59e729a4344ce5d6dd3` |
| `bench/vault.sha256` (manifest of 130 vault files, `sha256  path`) | `f5685eb1faccc930bc12881f1ef5ee8b29e17460c3ce2514782abdaec060ad66` |

Verify on a fresh clone:

```
python3 bench/seal.py check
```

## Cases by type

| Type | Cases |
| --- | --- |
| `single_hop` | 14 |
| `bridge_2hop` | 20 |
| `multi_note_aggregation` | 12 |
| `supersession` | 8 |
| `unanswerable` | 8 |
| `distractor` | 10 |
| **total** | **72** |

Bridge and aggregation cases: 32 of 72 (44%).

Validation at seal time (`python3 bench/seal.py validate`): every `must_contain`
string (gold and distractor) is present verbatim in the note it names, every
unanswerable case has empty gold, and no bridge question shares a distinctive
term (found in at most 4 notes) with its answer note.
