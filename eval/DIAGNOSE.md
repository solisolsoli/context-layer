# Diagnose retrieval

Use a frozen, independently labelled question set. For each answerable question, record exact required source passages; for an unanswerable question, record an empty requirement. A source filename is not evidence delivery. The default filename mode is a diagnostic baseline and can award a hit to output containing no body text. Use `--evidence-contract` to check source path, frozen hash, and required contiguous passage.

```sh
python3 eval/compare.py --out /tmp/context-comparison
python3 eval/evaluate.py --help
```

The synthetic comparison is bounded to its frozen 24-question corpus and does not measure semantic answer quality, independent human acceptance, or a real personal vault. Report delivery, abstention, operational errors, and character cost separately. Do not convert these results into timing, token, or universal superiority claims.
