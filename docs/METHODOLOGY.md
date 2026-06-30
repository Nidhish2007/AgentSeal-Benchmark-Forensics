# Methodology (Beta)

AgentSeal is an evidence-first contamination-risk auditor. It is designed to answer questions like:

- Is this benchmark instance already visible in public or corpus-like sources?
- Does the gold patch overlap with code that appears elsewhere?
- Is the source repository likely present in a large code corpus?
- Are exact public evidence URLs available and clickable?
- Which findings are corpus signals, public-source evidence, temporal evidence, or weaker heuristics?

AgentSeal's job is the audit layer before model evaluation: it separates contamination-risk evidence classes so benchmark maintainers can filter, bucket, or investigate affected instances with clear provenance.

## High-level pipeline

```text
input dataset
-> row/schema normalization
-> source metadata extraction
-> source PR or patch retrieval where possible
-> local text/patch/test checks
-> CodeSeal local content-overlap lookup
-> Stack v2 Bloom repository-membership check
-> optional GitHub/HF public evidence verification
-> risk scoring
-> JSON/Markdown/HTML report generation
```

## Input normalization

AgentSeal accepts bundled datasets and custom local datasets. It tries to normalize common benchmark fields:

- repository name;
- instance identifier;
- patch or solution text;
- test patch or test signal;
- problem statement;
- base commit/source metadata;
- PR/issue/source URLs when provided.

When metadata is missing, AgentSeal should degrade gracefully instead of fabricating links.

## Evidence classes

AgentSeal separates evidence into different classes.

### 1. Source/benchmark construction evidence

This is evidence from the benchmark row itself, such as:

- repository name;
- PR number;
- pull request URL;
- base commit;
- patch text;
- problem statement;
- tests.

This evidence explains where the benchmark item came from. It is not automatically a training-data claim.

### 2. CodeSeal local content-overlap signal

CodeSeal is a local packaged index used to check content-overlap style signals. It supports stronger corpus/content-overlap language than simple public search because it is tied to a defined local index.

### 3. Stack v2 Bloom repository-membership signal

The packaged Stack v2 Bloom filter checks whether a repository is probably present in the corpus represented by the filter.

Important limitations:

- Bloom filters are probabilistic.
- A positive Bloom result is a corpus-membership signal, not exact code-line proof.
- A negative Bloom result reduces confidence for the indexed corpus; it is not global absence evidence.

### 4. Patch/problem/test overlap checks

AgentSeal compares benchmark-visible fields against available source/public/corpus signals. It looks for:

- exact patch overlap;
- normalized patch overlap;
- problem-statement exposure;
- test-patch or test-logic exposure;
- high-risk combinations such as patch plus test exposure.

### 5. Independent public evidence verification

When GitHub/HuggingFace tokens and network access are available, AgentSeal can search public sources and preserve exact evidence URLs.

A public evidence URL should only be clickable in the report if it is exact and passes strict rendering/preflight checks. Broken, placeholder-like, synthetic, or unverified URLs should be rendered as `not linked`.

## Risk scoring

Risk is based on the combination of signals, not a single magic number. Stronger risk usually means multiple independent indicators, such as:

- source repository likely in corpus;
- patch or solution overlap;
- test signal exposure;
- exact public evidence URL;
- source PR older than the model cutoff being evaluated.

## Report outputs

AgentSeal generates:

- JSON for machine processing;
- Markdown for GitHub-readable review;
- HTML for visual inspection.

Reports should include:

- summary metrics;
- methodology;
- limitations;
- evidence classes;
- item-level findings;
- evidence URLs only when valid enough to show.

## How to review a report

Before publishing a report, check:

- No token or local secret is visible.
- No local path such as `C:\Users\...` is visible unless intentionally included.
- No clickable evidence URL returns 404.
- GitHub search pages are not presented as exact evidence.
- Bloom findings are described as probabilistic.
- Behavioral memorization claims are backed by separate model-behavior testing.
- The report clearly distinguishes corpus signals from public-source verification.

See [`REPORT_REVIEW_CHECKLIST.md`](REPORT_REVIEW_CHECKLIST.md).
