# Capabilities (Beta)

AgentSeal is not limited to one SWE-bench command. In this beta, it can be used as a broader benchmark and dataset contamination audit framework.

### Built-in benchmark audits (Beta)

- Audit bundled SWE-bench Verified data with `/audit`.
- Audit bundled SWE-bench Pro data with `/pro`.
- Load and audit known remote benchmark families with `/auto`, including SWE-bench, SWE-bench Lite, SWE-bench Verified, Multi-SWE-bench, HumanEval, MBPP, and BigCodeBench when the source data is accessible.
- Run small sampled beta checks such as `/audit 10`, `/pro 10`, and `/auto multi-swe-bench 10` before running larger audits.

### Custom dataset auditing (Beta)

- Use `/wizard` to inspect local `.parquet`, `.jsonl`, or `.json` benchmark files.
- Auto-detect common schema columns such as `instance_id`, `repo`, `patch`, `test_patch`, `problem_statement`, and `base_commit`.
- Normalize custom rows into AgentSeal audit instances.
- Audit PR-diff style datasets and standalone solution-style datasets.
- Generate reports for custom benchmark files without manually rewriting them into SWE-bench format.

### Evidence engines (Beta)

- **CodeSeal local index:** checks content-overlap signals from the packaged local index.
- **Stack v2 Bloom filter:** checks probable repository membership against a 16M+ repository Bloom filter.
- **Patch overlap analysis:** detects verbatim, normalized, and near-duplicate patch overlap.
- **Test-signal analysis:** checks whether benchmark test patches or test logic are exposed.
- **Problem-statement signal analysis:** checks problem text exposure where supported.
- **Independent public evidence search:** optionally verifies exact public evidence URLs through GitHub/HuggingFace-backed paths when tokens and network access are available.

### Report generation (Beta)

- Produces JSON, Markdown, and HTML reports.
- Separates corpus signals from independently verified public-source evidence.
- Suppresses broken, placeholder-like, or unverified evidence URLs instead of presenting them as clickable proof.
- Includes methodology, limitations, evidence classes, and item-level findings.
- Opens or lists recent reports directly from the terminal UI.

### Automation and discovery (Beta)

- `/auto` can discover known benchmark sources and cache downloaded datasets locally.
- Supports HuggingFace dataset discovery for public and gated datasets when the user provides an HF token.
- Includes remote download progress, shard-combine progress, and cache reuse messages.
- Keeps CodeSeal/Bloom local checks separate from remote dataset download steps.

### Token-assisted verification (Beta)

- GitHub tokens improve rate limits and evidence search reliability.
- HuggingFace tokens enable gated/private dataset download where the user has access.
- Token-like input is redacted in the terminal UI.
- AgentSeal can still run local CodeSeal/Bloom checks without tokens when the benchmark rows are already local.

### What AgentSeal does not claim

- It does not prove that a specific model memorized a specific benchmark item.
- It does not claim every public URL is training data.
- It does not treat Bloom hits as exact proof; Bloom matches are probabilistic corpus-membership signals.
- It does not render unverifiable or broken evidence links as clickable proof.
