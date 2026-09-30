# Design rationale

This document explains why context-layer is built the way it is. Each section says what we decided, why we decided it, and which public sources back the reasoning. The decisions describe the intended design; this text was written for 0.3.0 and revised for 0.4.0. What a given release actually does is recorded in [CHANGELOG.md](../CHANGELOG.md). What the tests actually cover is recorded in the test suite. Where this document and the code disagree, the code is the fact and this document is the bug.

## 1. Deliver verbatim, hash-pinned evidence, not generated citations

**Decision.** Retrieval returns unmodified source text. Each passage carries the file's vault-relative path, the SHA-256 of the exact bytes it came from, and its position in that file. A packet is withheld when a source changed after indexing. The tool never paraphrases, summarises or rewrites what it delivers.

**Why.**
- Citing is not the same as being supported. Human evaluation of generative search engines found that only about half of generated sentences were fully supported by their citations [Liu, Zhang & Liang 2023]. Even strong models leave citation support incomplete [Gao et al. 2023].
- A correct-looking citation also does not show that the model used that source [Wallat et al. 2024].
- The tool cannot make a model faithful. What it can do is make the evidence itself checkable. Anyone can re-hash the file and compare the delivered text byte for byte.

**Consequence.** A hash proves identity and freshness of the source, not relevance and not truth. The packet status says whether evidence was delivered (`PARTIAL`), whether none was found (`NOT_FOUND`), or whether retrieval failed (`ERROR`). It never says whether the question was answered. That judgement stays with the caller. An abstention is a normal result, not an error [Joren et al. 2025].

## 2. Lexical full-text search is the default and the reference

**Decision.** The default method is SQLite FTS5 with BM25 ranking over the whole allowed vault. Plain `grep` is kept as a comparison arm. Any other method must beat both at the same output budget before the documentation makes a quality claim for it.

**Why.**
- BM25 is a strong zero-shot baseline across heterogeneous retrieval tasks [Thakur et al. 2021].
- Fusing rankers is not an automatic gain over the best single ranker [Bruch et al. 2022].
- Routing between retrieval strategies trades accuracy against cost rather than improving both [Jeong et al. 2024].
- FTS5 ships with Python's standard library SQLite in common builds. It needs no model, no network and no extra dependency [SQLite FTS5].

**Limits stated plainly.**
- Matching is on exact tokens with diacritic folding.
- There is no stemming beyond the optional English Porter tokenizer. Agglutinative languages lose recall [SQLite FTS5; Can et al. 2008].
- Keyword search cannot prove that an answer is absent from the vault.

## 3. Deliver the passage that matched, within a declared budget

**Decision.** Evidence should be a verbatim window around each match, not the beginning of each file: overlapping windows from one file merge, and a cut passage is marked visibly. What this release does, by method:

- **Synaptic** (`--method synaptic`) delivers match-anchored windows. Each item carries `source_chars` and `truncated`, and so do the fts passages inside a synaptic packet; the synaptic prompt hook shows `[truncated]` for a cut item. Passages left out for lack of budget are counted in `synapse.stops.budget_limited`.
- **Default fts** (`--method fts`) delivers a note whole when it fits `--per-source` (2,000 characters by default; 6,000 in total), and otherwise the match-anchored verbatim windows in it: the paragraph-sized blocks in which FTS5 finds query terms, at most 3 per note, blank-line neighbours merged. Every item carries `start`/`end` (bytes), `line_start`/`line_end`, `source_chars`, `truncated` and `match_in_content`. `--delivery prefix` restores the 0.3 behaviour (the beginning of the file, whatever the match position) for comparison. When no block holds a term, the beginning is delivered and `match_in_content` says so.
- Budgets count the passage text, not the JSON framing around it (`est_tokens` scope: evidence content only).
- A source that changed after indexing is withheld and listed under `withheld` with its reason, instead of being dropped silently.

**Why.**
- Models use long inputs unevenly, and the position of relevant text matters [Liu et al. 2024].
- More retrieved context can plateau or reverse, depending on the model [Long Context RAG Performance 2024].
- Relevance-anchored windows let a small budget carry the required text instead of unrelated preamble.
- Model-free structural chunking is a reasonable default: semantic chunking has not shown consistent gains for its cost [Qu et al. 2025].
- FTS5's `snippet()` is not used: its output adds markup and is capped, so it is not verbatim evidence. Windows are cut from the source text itself [SQLite FTS5].

**Budget units.** Budgets are declared in characters, or as estimated tokens with the estimator named. Token counts differ by tokenizer and by language, so no fixed conversion is assumed [Ahia et al. 2023].

## 4. An opt-in link-graph method: bounded spreading activation over explicit links

**Decision.** An experimental method (`--method synaptic`) works in three steps:

1. It starts from FTS hits and the notes the prompt names (the seeds).
2. It spreads a decaying activation score along the vault's own explicit links: wikilinks, embeds, Markdown links and declared frontmatter relations.
3. In its default mode it keeps the plain-search packet unchanged and adds the most activated passages after it, in a separate declared budget (`--extra-tokens`). The opt-in compact mode packs passages into its own token budget (`--budget-tokens`) instead, and can drop evidence plain search would have delivered.

The method follows these rules:
- It is off by default.
- Every edge is anchored to the file and line where the link is written.
- A mention of a note's name without a link never becomes an edge.
- Ambiguous or unresolved link targets make no edge, and the reason is recorded.
- Each hop is subject to the same exclusion rules and hash check as a search hit.
- The default depth is one hop. A second hop needs a flag.
- A node's outgoing activation is divided by its number of links, so hub notes cannot flood the packet.
- Fan-out is capped.
- Ties are broken deterministically.
- Edges are described literally ("A links to B at line N"). A link is not evidence that A supports, agrees with, or supersedes B.

**Why.**
- Spreading activation comes from cognitive psychology [Collins & Loftus 1975]. It has a long history as an information-retrieval technique [Crestani 1997].
- Unconstrained spreading converges to results that no longer depend on the query [Berthold et al. 2009]. Seeds from the query, decay, degree normalisation and hop caps are therefore essential, not tuning details.
- Some questions need two documents that only a link connects. Multi-hop benchmarks score whether *all* required evidence arrived [Yang et al. 2018; Thorne et al. 2018].
- Many questions labelled multi-hop can be answered from one document [Min et al. 2019]. A graph method should be judged per question type, not on average.
- Graph-based retrieval has improved some multi-hop benchmarks and regressed on others relative to a strong single-step retriever [Gutiérrez et al. 2024].
- LLM-extracted entity graphs with generated community summaries [Edge et al. 2024] are a different design. They put model-written text in the evidence path, which this project does not do.
- Iterative retrieval interleaved with model reasoning [Trivedi et al. 2023] needs a model in the loop and is out of scope for a standard-library tool.

**What is not claimed.**
- No general quality, token or latency gain is claimed for this method.
- The bundled benchmark (§7) shows how the mechanism behaves on designed cases in a fictional vault. It cannot show that the method helps on a particular real vault.
- Linked copies of one note are not independent corroboration. Byte-identical passages are merged, and every path is listed.
- Activation ignores whether a note is current or superseded. A passage reached through a frontmatter relation is labelled with that key.

**Naming.** "Synaptic" and "Brain View" are product names, not descriptions of how the code works. The mechanism is a bounded spreading-activation ranking over an explicit link graph. It does not learn, it does not change its weights with use, and it does not model how the AI host reasons. Neuroscience framing can make explanations seem better than they are [Weisberg et al. 2008]; a large replication found the effect smaller and dependent on expertise [Väth et al. 2024]. Documentation therefore describes operations (seed, hop, activation score, explicit link) rather than biological analogies.

## 5. The activation view shows what the last retrieval selected

**Decision.**
- After a link-graph retrieval, the tool atomically writes one small file, `.context/activation.json`. It lists the activated notes, the edges traversed with their source anchors, and the packet counts.
- The Obsidian view renders the vault's link graph and highlights that record. It shows the record's timestamp and treats a missing, stale or malformed file as "nothing to show".
- The file is overwritten on each run. It is never read back into ranking. It never lists excluded notes.
- By default it stores neither the query text nor a hash of it.

**Why.**
- Obsidian already provides a graph view and a local graph with a depth limit [Obsidian Help: Graph view]. What is new here is only the overlay of what a retrieval actually used.
- A lit-up graph is a record of selection, not a judgement that the retrieval was good. Activation scores are ranking scores, not probabilities or confidence. A percentage or a colour scale implying certainty invites misreading [Akhawe & Felt 2013].
- A deterministic hash of a short prompt can be confirmed by guessing, and it links repeated queries. Stable query identifiers leak information even when the text is hidden [Cash et al. 2015]. So the default stores a random run identifier instead of a hash.
- Anything retrieval learns from its own past output can reinforce its own mistakes. Keeping the file write-only makes every packet a pure function of the files, the configuration and the query.

## 6. Fail loudly, and keep outcome and exit status unambiguous

**Decision.** Every operation separates three outcomes:
1. The operation failed: `ERROR`, a non-zero exit, and no evidence.
2. The operation succeeded and found evidence: `PARTIAL`.
3. The operation succeeded and found nothing: `NOT_FOUND`.

`search` and the prompt hook exit 0 when retrieval completed, whatever it found, 1 on an operational error and 2 on a usage error. This follows the 0/1/2 convention of `grep` [GNU grep manual]. `status` has its own documented table.

**Checks.**
- The index build runs FTS5's `integrity-check` before it is published.
- Readers verify that the full-text index is consistent with its content table. An emptied or desynchronised index is an error, not an empty result. With external-content FTS5 tables, the application is responsible for keeping the two in sync [SQLite FTS5].
- Index and configuration files carry a format version. Readers refuse unknown versions and ask for a rebuild, following Git's repository-format precedent [Git: repository format versions].

**Host integration.** The prompt hook exits non-zero on failure. Claude Code then shows a hook-error notice with the first line of the error and continues without evidence [Claude Code hooks]. The hook's own timeout is set below the host's default for prompt hooks, so a slow retrieval fails with this tool's message. The MCP server answers `initialize` with a protocol version it actually supports [MCP specification: lifecycle].

## 7. Measure with a sealed, fictional benchmark that includes the baselines

**Decision.**
- The benchmark (`bench/`) is a fictional 130-note vault with 72 cases whose hashes were committed (`bench/SEAL.md`) before any method ran on the vault. Development of the link-graph method used a separate generated dev set (`tests/fixtures/dev_bridge.py`), not the sealed cases; every run of the sealed set, and whether it could influence design, is listed in `bench/INSPECTIONS.md`.
- The scorer does not import the code under test. It runs the CLI in a subprocess and reads only each passage's path and text. A gold item counts only when its required text occurs in a passage from the right file, and that passage is itself a verbatim span of the file it names.
- The arms are grep, FTS and synaptic (FTS plus link extras), in one evidence format. The default run uses each method's default budget, so synaptic may add up to 600 estimated tokens on top of the FTS packet, and packet size is reported beside completeness. `--budget-tokens N` runs every arm inside one budget of N estimated tokens, mapped onto each method's own flags, and fails if any packet is larger. A grep-plus-links arm, which would separate the effect of following links from the search engine, does not exist yet; it needs retrieval code and is deferred.
- Results are reported per case and per case type (single-hop, two-hop bridge, multi-note aggregation, supersession, near-duplicate distractor, unanswerable). Wins and losses are paired and tested with an exact sign test. Packet size is always reported beside completeness, never alone. A case whose search failed counts as incomplete.
- Each result file records the command, the code revision (marked `+dirty` for uncommitted changes), the hashes of the cases and of the vault manifest, the Python, SQLite and Unicode versions, the platform and a hash of the scorer. Each run also writes TREC-format `qrels.txt` and `<method>.run` files, so others can rescore it with their own tools [ir_measures]. The CI workflow reruns the benchmark and fails unless the committed summary and the README's benchmark numbers are reproduced.

**Why.**
- Requiring the correct evidence, not just a plausible label, is the stricter and more honest measure [Thorne et al. 2018].
- A retrieval metric is a delivery measure, not answer quality. Retrieved context can be related but insufficient [Joren et al. 2025].
- Python randomises hashing per process [Python docs: PYTHONHASHSEED], and SQLite does not guarantee row order without `ORDER BY` [SQLite PRAGMA reverse_unordered_selects]. So every ordering in the pipeline ends in an explicit tie-break, and determinism is tested under several hash seeds.

## 8. Notes are data, and prompt injection is disclosed, not solved

**Decision.**
- Delivered notes are framed as data rather than instructions, and each evidence item is delimited with a per-packet random marker so that a note cannot forge another item.
- The documentation says plainly that this reduces the risk of a model following instructions planted in a note, but does not prevent it.
- The link-graph method increases exposure, because it adds linked notes that did not match the query.
- Sub-agent tasks run a host with its own tool permissions. The real boundary is the host's permission model plus narrow source scopes.

**Why.**
- Retrieved content can carry instructions that a model follows [Greshake et al. 2023].
- Prompt injection has no complete mitigation. The practical defence is to limit what a compromised model can do [UK NCSC 2025; Claude Code security documentation].
- The strongest measured reductions come from transforming the input [Hines et al. 2024] or from model training [Wallace et al. 2024]. Neither is available to a tool whose promise is verbatim delivery.

## 9. Local and plain, with every write disclosed

**Decision.**
- State lives in the vault, under `.context/`, as SQLite, JSON, JSONL and Markdown that a person can open and delete.
- The package's own code makes no network request unless the optional advisor is enabled ([docs/jev.md](jev.md)); a clean install never loads its client.
- The optional sub-agent runner starts a host command-line tool, which talks to its model provider.
- Retrieval exclusions apply before any source read. Paths are compared case-insensitively and against the exact on-disk name, and one strict loader serves every entry point.
- The documentation lists every file the tool writes, what it contains and how to remove it. That includes the previous index generation, which keeps text from notes that were later excluded or deleted until it is removed.

**Why.**
- Obsidian Sync does not sync dot folders other than its own settings folder [Obsidian Help: Sync settings]. Other sync tools and version control do sync them. Copying an SQLite database mid-transaction, or separating it from its journal, can corrupt it [SQLite: How to corrupt]. The documentation recommends excluding `.context/` from third-party sync, or rebuilding after a sync.
- Exclusions are a retrieval filter with stated limits, not an operating-system sandbox.

## 10. Help the corpus, not only the retriever

Some retrieval failures are properties of the notes themselves: one subject written under several words, or titles that do not say what a note is about. People choose the same term for the same thing surprisingly rarely [Furnas et al. 1987]. Explicit aliases help both search and link resolution [Obsidian Help: Aliases].

The documentation therefore offers optional authoring guidance: descriptive titles, aliases added when a search fails, and explicit links where one note depends on another. The tool does not require any of these and does not rewrite notes.

## References

- Ahia, O. et al. (2023). Do All Languages Cost the Same? Tokenization in the Era of Commercial Language Models. EMNLP. arXiv:2305.13707.
- Akhawe, D., Felt, A. P. (2013). Alice in Warningland: A Large-Scale Field Study of Browser Security Warning Effectiveness. USENIX Security.
- Berthold, M. R., Brandes, U., Kötter, T., Mader, M., Nagel, U., Thiel, K. (2009). Pure spreading activation is pointless. CIKM, 1915–1918. doi:10.1145/1645953.1646264.
- Bruch, S., Gai, S., Ingber, A. (2022). An Analysis of Fusion Functions for Hybrid Retrieval. arXiv:2210.11934; ACM TOIS 2023.
- Can, F. et al. (2008). Information retrieval on Turkish texts. JASIST 59(3). doi:10.1002/asi.20750.
- Cash, D., Grubbs, P., Perry, J., Ristenpart, T. (2015). Leakage-Abuse Attacks Against Searchable Encryption. ACM CCS. https://eprint.iacr.org/2016/718
- Claude Code documentation: Hooks reference, https://code.claude.com/docs/en/hooks ; Security, https://code.claude.com/docs/en/security
- Collins, A. M., Loftus, E. F. (1975). A spreading-activation theory of semantic processing. Psychological Review 82(6), 407–428.
- Crestani, F. (1997). Application of Spreading Activation Techniques in Information Retrieval. Artificial Intelligence Review 11, 453–482. doi:10.1023/A:1006569829653.
- Edge, D. et al. (2024). From Local to Global: A Graph RAG Approach to Query-Focused Summarization. arXiv:2404.16130.
- Furnas, G. W., Landauer, T. K., Gomez, L. M., Dumais, S. T. (1987). The Vocabulary Problem in Human-System Communication. CACM 30(11). doi:10.1145/32206.32212.
- Gao, T., Yen, H., Yu, J., Chen, D. (2023). Enabling Large Language Models to Generate Text with Citations. arXiv:2305.14627.
- Git documentation: Repository format versions. https://git-scm.com/docs/repository-version
- GNU grep manual: Exit Status. https://www.gnu.org/software/grep/manual/html_node/Exit-Status.html
- Greshake, K., Abdelnabi, S., Mishra, S., Endres, C., Holz, T., Fritz, M. (2023). Not what you've signed up for: Compromising Real-World LLM-Integrated Applications with Indirect Prompt Injection. arXiv:2302.12173.
- Gutiérrez, B. J. et al. (2024). HippoRAG: Neurobiologically Inspired Long-Term Memory for Large Language Models. NeurIPS. arXiv:2405.14831.
- Hines, K. et al. (2024). Defending Against Indirect Prompt Injection Attacks With Spotlighting. arXiv:2403.14720.
- ir_measures documentation. https://ir-measur.es/
- Jeong, S. et al. (2024). Adaptive-RAG. NAACL. arXiv:2403.14403.
- Joren, H. et al. (2025). Sufficient Context: A New Lens on Retrieval Augmented Generation Systems. arXiv:2411.06037.
- Long Context RAG Performance of Large Language Models (2024). arXiv:2411.03538.
- Liu, N. F. et al. (2024). Lost in the Middle: How Language Models Use Long Contexts. TACL. arXiv:2307.03172.
- Liu, N. F., Zhang, T., Liang, P. (2023). Evaluating Verifiability in Generative Search Engines. arXiv:2304.09848.
- Model Context Protocol specification (2025-06-18): Lifecycle. https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle
- Min, S. et al. (2019). Compositional Questions Do Not Necessitate Multi-hop Reasoning. ACL. https://aclanthology.org/P19-1416/
- Obsidian Help: Internal links https://obsidian.md/help/links ; Aliases https://obsidian.md/help/aliases ; Backlinks https://obsidian.md/help/plugins/backlinks ; Graph view https://obsidian.md/help/plugins/graph ; Sync settings https://obsidian.md/help/sync/settings
- Python documentation: PYTHONHASHSEED. https://docs.python.org/3/using/cmdline.html#envvar-PYTHONHASHSEED
- Qu, R., Tu, R., Bao, F. S. (2025). Is Semantic Chunking Worth the Computational Cost? Findings of NAACL. https://aclanthology.org/2025.findings-naacl.114/
- SQLite: FTS5 Extension https://www.sqlite.org/fts5.html ; How To Corrupt An SQLite Database File https://www.sqlite.org/howtocorrupt.html ; PRAGMA reverse_unordered_selects https://www.sqlite.org/pragma.html#pragma_reverse_unordered_selects
- Thakur, N. et al. (2021). BEIR: A Heterogeneous Benchmark for Zero-shot Evaluation of Information Retrieval Models. NeurIPS Datasets and Benchmarks. arXiv:2104.08663.
- Thorne, J., Vlachos, A., Christodoulopoulos, C., Mittal, A. (2018). FEVER: a Large-scale Dataset for Fact Extraction and VERification. NAACL. https://aclanthology.org/N18-1074/
- Trivedi, H. et al. (2023). Interleaving Retrieval with Chain-of-Thought Reasoning for Knowledge-Intensive Multi-Step Questions. ACL. https://aclanthology.org/2023.acl-long.557/
- UK National Cyber Security Centre (2025). Prompt injection is not SQL injection (it may be worse). https://www.ncsc.gov.uk/blog-post/prompt-injection-is-not-sql-injection
- Väth et al. (2024). Replicating the "seductive allure of neuroscience explanations" effect. Royal Society Open Science. doi:10.1098/rsos.241120.
- Wallace, E. et al. (2024). The Instruction Hierarchy: Training LLMs to Prioritize Privileged Instructions. arXiv:2404.13208.
- Wallat, J. et al. (2024). Correctness is not Faithfulness in RAG Attributions. arXiv:2412.18004.
- Weisberg, D. S., Keil, F. C., Goodstein, J., Rawson, E., Gray, J. R. (2008). The Seductive Allure of Neuroscience Explanations. Journal of Cognitive Neuroscience 20(3), 470–477. doi:10.1162/jocn.2008.20040.
- Yang, Z. et al. (2018). HotpotQA: A Dataset for Diverse, Explainable Multi-hop Question Answering. EMNLP. https://aclanthology.org/D18-1259/
