# Adapters — how to make your own setup measurable

An **adapter** is a program that takes a prompt and prints the context your
system would have retrieved for it. That is the entire interface. Once your
setup has one, `evaluate.py` can measure it exactly the way it measures the
router in this repository — same stimulus set, same hit definition, same cost
count — and you can find out whether your second brain retrieves the right
things.

> The question this unlocks: **is my setup better than grep?** Most people
> cannot answer that about their own notes. Two of the adapters here exist so
> that you can.

## The contract

1. The prompt arrives as the **final argv token**. Everything before it is
   whatever flags you need (`--vault`, `--top-k`, …).
2. The retrieved context goes to **stdout**. Nothing else does. Diagnostics go
   to stderr; `evaluate.py` scores stdout only.
3. **Print the source path of every document you return**, on its own line.
   A hit is scored when the expected source's name appears in stdout.
4. **Print the text too.** A packet that lists filenames without their content
   scores full marks and helps nobody; see the warning at the top of
   [../README.md](../README.md).
5. Exit 0 on success. On a configuration problem, exit non-zero **with a message
   on stderr** — never print an empty packet. An empty packet is scored as a
   total retrieval failure, and blaming your vault for your wiring is the one
   thing a measurement instrument must not do.
6. Be read-only. None of these adapters writes to the vault.

## What is here

| File | What it is |
|---|---|
| `grep_baseline.py` | Naive term search over the vault. **The control.** No index, no ranking model. If your setup does not beat this, it is not delivering synonym matching, paraphrase matching, or the ability to tell a passing mention from the document that owns the answer. |
| `fts_sqlite.py` | SQLite FTS5 + BM25. The "good enough" baseline, with no dependency beyond the standard library. This is the bar a vector setup actually has to clear. The index is one file per vault by default; it records the vault and a digest of every file's path, size and mtime, and is rebuilt when either differs, so a shared `--index` path never answers from another vault. |
| `embedding_stub.py` | A **template**, not a working retriever. Chunking, cosine ranking, caching and packet rendering are written; you implement one function, `embed_texts`, and name your model in `MODEL_ID`. It refuses to run until you do. Cached vectors are keyed by `MODEL_ID`, the input kind and the text, so switching models never mixes old and new vectors. |
| `_common.py` | Tokenising, file walking, packet rendering, shared by the three above. |

Run any of them directly to see what they return:

```sh
python3 adapters/grep_baseline.py --vault fixtures/docs "release steps?"
python3 adapters/fts_sqlite.py    --vault fixtures/docs "release steps?"
```

Then measure one:

```sh
python3 evaluate.py \
  --command "python3 adapters/fts_sqlite.py --vault fixtures/docs --top-k 3" \
  --stimuli stimulus-set.example.jsonl
```

Measured results for all three on the fixture vault, with the misses explained:
[../BENCHMARK_FIXTURE.md](../BENCHMARK_FIXTURE.md).

## Writing your own, in about twenty lines

If your setup already has a CLI that prints search results with their paths, you
do not need an adapter at all — point `--command` straight at it. If it has a
Python API, or a JSON endpoint, or a database, this is the whole job:

```python
#!/usr/bin/env python3
"""Adapter for <my setup>. Prompt in on argv, retrieved context out on stdout."""
import sys

import my_setup          # your search library, client, or database wrapper

def main():
    prompt = sys.argv[-1]            # evaluate.py appends the prompt last
    try:
        results = my_setup.search(prompt, limit=5)
    except Exception as exc:         # a broken connection is not a miss
        print(f"adapter error: {exc}", file=sys.stderr)
        return 2
    if not results:
        print(f"# Retrieved context\n\nNo source matched: {prompt}")
        return 0                     # a real empty result, honestly reported
    print(f"# Retrieved context\n\n## Query\n{prompt}\n")
    for i, r in enumerate(results, 1):
        print(f"### R{i:03d} — `{r.path}`\n")   # the path: this is what is scored
        print(r.text.strip(), "\n")             # the body: this is what is used
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
```

Four mistakes that will cost you a day, all of them seen in practice:

- **Printing an answer instead of the context.** If your system summarises
  before it returns, the filenames never appear and everything scores zero. Put
  the retrieval step behind its own flag.
- **Printing paths in a different spelling from your labels.** `notes/x.md` in
  the label and `/home/me/vault/notes/x.md` in the output do not match under
  `--match-mode path`. Use `--match-mode basename`, or make both sides agree.
- **Swallowing errors into an empty result.** Then a timeout looks like a
  retrieval failure and you will "fix" the wrong thing.
- **Embedding the query with a different model from the documents.** Silent, and
  it produces a system that scores below grep for reasons no log will show.

When something scores zero everywhere, `evaluate.py` now prints a diagnostics
block naming the four usual causes. Read it before you believe the number.
