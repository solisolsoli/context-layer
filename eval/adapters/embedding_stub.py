#!/usr/bin/env python3
"""Adapter TEMPLATE: wire your own embedding / vector retrieval in one function.

Everything a vector setup needs except the vectors is already written below:
chunking, similarity, ranking, caching, packet rendering, and honest failure.
You fill in exactly one function -- `embed_texts` -- and then your setup is
measurable by `evaluate.py` alongside grep and BM25.

This file deliberately:

  * has NO dependencies (no numpy, no SDK, no vector database),
  * makes NO network calls of its own,
  * and REFUSES TO RUN until you configure it, with a message that says so.

That last point is the whole design. An unconfigured retrieval adapter that
printed an empty packet would score zero hits, and zero hits looks identical to
"my second brain is broken". A measurement instrument must never manufacture the
failure it is measuring, so this one exits non-zero and explains itself instead.

HOW TO USE IT
-------------
1. Copy this file:            cp embedding_stub.py my_vectors.py
2. Implement `embed_texts`    (the single block marked FILL THIS IN).
3. Check it by hand:          python3 my_vectors.py --vault ~/notes "a real question"
4. Measure it:
       python3 evaluate.py \
         --command "python3 adapters/my_vectors.py --vault ~/notes" \
         --stimuli my-stimuli.jsonl

`--smoke-test` runs the plumbing with a deterministic hashing pseudo-embedding.
It is NOT semantic and its retrieval quality is meaningless -- it exists only so
you can confirm the chunking and ranking work before you spend money or tokens.
Never report a number produced under --smoke-test as a result.

Stdlib only. The vault is only ever read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (  # noqa: E402
    add_common_args, clip, read_text, rel, render_packet, resolve_vault, walk_vault,
)

# ---------------------------------------------------------------------------
# FILL THIS IN -- the one thing this template cannot do for you.
# ---------------------------------------------------------------------------
# Return one embedding vector per input string, in the same order, all the same
# length. Whatever produces them -- a local model, a hosted API, an existing
# vector store's embed endpoint -- is your choice and stays your dependency, not
# this repository's.
#
# Two rules that decide whether your measurement means anything:
#
#   1. Embed the QUERY and the DOCUMENTS with the SAME model. Mixing models
#      produces vectors that are numerically comparable and semantically
#      meaningless, which is worse than no retrieval because it looks like it
#      worked.
#   2. If your provider distinguishes a "query" input type from a "document"
#      one, honour it (see the `kind` argument). Getting this backwards is the
#      single most common cause of a vector setup quietly underperforming grep.
#
# Example shape (pseudo-code, intentionally not runnable here):
#
#     def embed_texts(texts, kind="document"):
#         import my_provider                      # your dependency, not ours
#         resp = my_provider.embed(model="...", input=texts, input_type=kind)
#         return [item.vector for item in resp.data]
#
CONFIGURED = False  # <- flip to True once embed_texts below actually embeds.
# The model (and version) embed_texts uses, e.g. "provider/model-name@2026-01". The
# vector cache is keyed by it, so switching models can never mix old document
# vectors with new query vectors. Required once CONFIGURED is True.
MODEL_ID = ""


def embed_texts(texts: list[str], kind: str = "document") -> list[list[float]]:
    """texts -> one vector per text. Replace the body of this function."""
    raise NotImplementedError(
        "embed_texts() is not implemented.\n"
        "\n"
        "This is the adapter TEMPLATE (adapters/embedding_stub.py). It cannot\n"
        "retrieve anything until you implement embed_texts() and set\n"
        "CONFIGURED = True at the top of the file.\n"
        "\n"
        "  - Copy it first:  cp adapters/embedding_stub.py adapters/my_vectors.py\n"
        "  - Implement embed_texts() with YOUR embedding model.\n"
        "  - Use the same model for the query and the documents.\n"
        "  - To test the plumbing only:  --smoke-test  (not a real result)\n"
        "\n"
        "Exiting non-zero on purpose: an empty packet would have been scored as\n"
        "a retrieval failure by evaluate.py, and that would have been a lie\n"
        "about your vault rather than the truth about this file."
    )
# ---------------------------------------------------------------------------
# Nothing below needs to change for a normal setup.
# ---------------------------------------------------------------------------


def smoke_embed(texts: list[str], kind: str = "document") -> list[list[float]]:
    """Deterministic hashed bag-of-words vectors. NOT SEMANTIC. Plumbing test only."""
    dim = 256
    out = []
    for text in texts:
        vec = [0.0] * dim
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            h = int(hashlib.sha1(word.encode()).hexdigest(), 16)
            vec[h % dim] += 1.0
        out.append(vec)
    return out


def chunk_document(text: str, max_chars: int, overlap: int) -> list[str]:
    """Split on blank lines, then pack paragraphs up to max_chars.

    Chunking is a retrieval decision, not a formatting one: chunks larger than
    the idea they contain dilute the vector, and chunks smaller than it strand
    the answer across two neighbours. If your measured hit rate is bad, change
    this before you change models.
    """
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paras:
        if buf and len(buf) + len(para) + 2 > max_chars:
            chunks.append(buf)
            buf = (buf[-overlap:] + "\n\n" + para) if overlap else para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    return chunks or [text]


def cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return num / (na * nb) if na and nb else 0.0


def cache_key(text: str, kind: str, model_id: str) -> str:
    """Cache key for one vector: the model id, the input kind and the text's SHA-256.
    Vectors cached by an older version of this file (keyed by the text alone) are
    simply not found, and are recomputed."""
    text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return hashlib.sha256(f"{model_id}\0{kind}\0{text_sha}".encode("utf-8")).hexdigest()


def load_cache(path: str | None) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cache(path: str | None, cache: dict) -> None:
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(ap)
    ap.add_argument("--chunk-chars", type=int, default=900, help="Target chunk size.")
    ap.add_argument("--chunk-overlap", type=int, default=100, help="Chars carried between chunks.")
    ap.add_argument("--cache", default=".eval-embed-cache.json",
                    help="Embedding cache file; '' disables caching.")
    ap.add_argument("--smoke-test", action="store_true",
                    help="Run with a NON-SEMANTIC hashed embedder to test the plumbing only.")
    args = ap.parse_args()

    embedder = embed_texts
    if args.smoke_test:
        embedder = smoke_embed
        print("WARNING: --smoke-test uses a non-semantic hashed embedder. "
              "Any number produced from this run is meaningless as a retrieval "
              "result and must not be reported as one.", file=sys.stderr)
    elif CONFIGURED and not MODEL_ID.strip():
        print("adapter error: set MODEL_ID next to CONFIGURED to the model you embed with; "
              "the vector cache is keyed by it.", file=sys.stderr)
        return 2
    elif not CONFIGURED:
        # Fail loudly and early, before reading the vault, so the message is the
        # first thing the user sees rather than a zero-hit table an hour later.
        try:
            embed_texts(["configuration check"], kind="query")
        except NotImplementedError as exc:
            print(f"adapter error: {exc}", file=sys.stderr)
            return 2
        print("adapter error: CONFIGURED is False but embed_texts() did not raise. "
              "Set CONFIGURED = True once your embedder is real.", file=sys.stderr)
        return 2

    vault = resolve_vault(args.vault)
    prompt = " ".join(args.prompt)
    exts = tuple(e.strip().lower() for e in args.ext.split(",") if e.strip())
    files = walk_vault(vault, exts)
    if not files:
        print(f"adapter error: no files with extensions {args.ext} under {args.vault!r}.",
              file=sys.stderr)
        return 2

    records = []
    for path in files:
        for i, chunk in enumerate(chunk_document(read_text(path), args.chunk_chars,
                                                 args.chunk_overlap)):
            records.append({"path": rel(path, vault), "chunk": i, "text": chunk})

    cache_path = args.cache or None
    if args.smoke_test:
        cache_path = None  # never let fake vectors contaminate a real cache
    cache = load_cache(cache_path)
    model_id = "smoke-test" if args.smoke_test else MODEL_ID
    todo = [r for r in records if cache_key(r["text"], "document", model_id) not in cache]
    if todo:
        vectors = embedder([r["text"] for r in todo], kind="document")
        if len(vectors) != len(todo):
            print(f"adapter error: embed_texts returned {len(vectors)} vectors for "
                  f"{len(todo)} inputs. It must return one per input, in order.",
                  file=sys.stderr)
            return 2
        for r, v in zip(todo, vectors):
            cache[cache_key(r["text"], "document", model_id)] = v
        save_cache(cache_path, cache)
    for r in records:
        r["vec"] = cache[cache_key(r["text"], "document", model_id)]

    qvec = embedder([prompt], kind="query")[0]
    for r in records:
        r["score"] = cosine(qvec, r["vec"])
    records.sort(key=lambda r: (-r["score"], r["path"], r["chunk"]))

    chosen, seen = [], set()
    for r in records:
        if r["path"] in seen:
            continue  # one entry per document, so hits are comparable with the
            # other adapters rather than the same file counted three times
        seen.add(r["path"])
        chosen.append((r["path"],
                       f"cosine {r['score']:.4f} (best chunk #{r['chunk']})",
                       clip(r["text"], args.max_chars)))
        if len(chosen) >= args.top_k:
            break

    note = (f"{len(records)} chunks over {len(files)} documents."
            + (" SMOKE TEST: non-semantic hashed vectors, not a real result."
               if args.smoke_test else ""))
    print(render_packet("embedding_stub", prompt, chosen, note))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
