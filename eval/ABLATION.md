# Ablation

Change one retrieval layer at a time and rerun the same frozen corpus. Keep source bytes, labels, command and budget fixed; use a fresh output directory for each run. Compare delivery groups and correct abstentions, then inspect the mechanism. A changed hit count identifies a changed outcome; it does not by itself prove why.

The current synthetic comparison uses 3 documents, a 6,000-character total budget, and a 2,000-character per-source budget. Root-reviewed fixture results are diagnostic only and are not semantic acceptance or a real-vault benchmark.

Do not relabel after seeing output. Do not claim a speed or token saving without measurement. Keep operational failures separate from retrieval misses. The router remains experimental and optional.
