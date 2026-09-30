# Frozen synthetic comparison

24 questions over two fictional domains: Orchard Docs and a note-writing project.
12 development and 12 acceptance cases, 4 unanswerable and 4 multi-source cases.
Files are snapshots of bundled examples, remapped to explicit domain folders.
Labels live outside the searchable vault. The supplied route vocabulary is from
the pre-existing example configuration; it was not trained on these questions.

Labels were drafted blind to the ranking code and its results, then corrected
by a second reviewer: three false unanswerable labels, ambiguous domains,
unnecessary full-file spans and multi-source requirements. Retrieval code was
frozen before that correction.
No result-driven tuning was performed. The splits share a small synthetic source
corpus, so they are not an independent real-world semantic acceptance set.
They demonstrate delivery mechanics and method differences only. In particular,
keyword retrieval cannot establish unanswerability; a nonempty packet on an
unanswerable case is a failed abstention, not proof that an answer exists.

Run `python3 eval/compare.py --out /path/to/results` from the checkout.
The command creates only disposable synthetic vault/index copies and the named
result directory. Defaults: top-k 3, total evidence content 6000 characters,
per source 2000; actual JSON stdout cost includes framing. All methods use the
same source versions, index, policy, output format and final content bounds.

`results.json` is the `summary.json` that command writes, committed as produced on
the release tree (Python and code hashes recorded in it; the per-case rows keep the
same fields for every method). Its completeness counts are the delivery mechanics of
the frozen questions; its cost fields are the characters of each method's whole JSON
packet, so they move whenever the packet framing changes. `tests/test_comparison.py`
(`make test`) and the `bench-reproduce` CI job rerun the comparison and fail when
`results`, `bounds` or `contract_sha256` differ from the committed file. After a change
to `eval/retrieve.py` or the router that moves them, regenerate and commit it:

    python3 eval/compare.py --out /tmp/comparison && cp /tmp/comparison/summary.json eval/comparison/results.json

Last regenerated on the tree of the 0.4.0 release candidate (window delivery,
`coverage` block and per-item line anchors included): completeness unchanged
(grep 24/25 groups, fts and fts-canonical 25/25, router 7/25); mean packet cost
per question grep 3,003, fts 2,814, fts-canonical 2,837, router 1,112 (the router delivers less because it answers fewer
questions).
