# Legacy filename-scoring demonstration

This older demonstration uses seven fictional documents and 12 prompts. It
counts source-name mentions, not delivered passages or answer quality. The
adapters use different output formats and budgets. Use the newer
[common-budget comparison](comparison/README.md) for delivery checks.

From this directory, run `sh bench_fixture.sh`. The script writes `run-output/`.
A rerun of that script on 2026-09-29 (tree `bbe6d95`) produced the following
filename counts (equal to the counts this file printed before):

| Method | Filename hits | Complete cases |
| --- | ---: | ---: |
| grep, top-k 3 | 15/17 | 10/12 |
| grep, top-k 6 | 16/17 | 11/12 |
| FTS, top-k 3 | 14/17 | 9/12 |
| FTS, top-k 6 | 16/17 | 11/12 |
| demo router | 15/17 | 10/12 |
| demo router, full | 17/17 | 12/12 |

The heuristic rubric (the same run) gives both baselines 37/58 and the demo
router 42/56. On content axes both baselines score 28/32 and the router 24/30:
the router is behind on the decisive-fact axis (8/12 against 10/12), and one
timeliness item is scored not applicable to its packets, which is why its
denominators are smaller. The router's lead comes from provenance formatting
(sourcing 12/12 against 0/12), offset by waste (4/12 against 7/12); it is not
evidence of better semantic retrieval. The script reports actual character
costs each run, without converting them to tokens or money. These results are
fixture diagnostics only.
