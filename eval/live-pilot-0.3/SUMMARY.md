# Live host run (unjudged)

Host `claude`, model `sonnet`, 24 cases, arms: baseline, hook-fts, hook-synaptic. Token and cost figures are the host's own counts.

| arm | calls | errors | mean input | mean cache create | mean cache read | mean output | mean total tokens | total cost USD | mean turns | mean host ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `baseline` | 24 | 0 | 7.1 | 4885.2 | 71733.1 | 535.1 | 77160.5 | 0.9421 | 4.4 | 7772.7 |
| `hook-fts` | 24 | 0 | 3.9 | 4543.2 | 38893.1 | 288.7 | 43728.9 | 0.6923 | 2.5 | 4264.9 |
| `hook-synaptic` | 24 | 0 | 3.6 | 5379.2 | 36003.6 | 298.0 | 41684.5 | 0.7609 | 2.2 | 4166.5 |

## Preflight

- hook-fts: hook ok (probe injected 1766 characters)
- hook-synaptic: hook ok (probe injected 2504 characters)

Answers are in `answers.jsonl`; fill `verdict` in `judge.jsonl` (correct / partial / wrong / abstained) and run `--summarize-judged`.
