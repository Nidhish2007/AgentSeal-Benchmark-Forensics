# AgentSeal

**Local-first contamination auditing for AI-agent benchmarks.**

AgentSeal audits benchmark instances for evidence that gold patches, problem statements, or test signals are already present in public or corpus-like sources. It is built for benchmark maintainers, model evaluators, and researchers who need evidence-first contamination reports instead of vague leakage claims.

> **Status:** public beta. AgentSeal produces audit evidence, not a claim that any specific model memorized a specific item.

## Highlights

- Terminal UI plus CLI entry points
- Local CodeSeal content-overlap signal
- Stack v2 repository-membership Bloom filter support
- Bundled SWE-bench Verified / Pro beta data in the release wheel
- Optional GitHub evidence verification with exact public URLs
- Optional HuggingFace dataset loading for `/auto` and `/wizard` workflows
- JSON, Markdown, and HTML reports
- Strict evidence-link rendering: broken, placeholder, or unverified URLs are not shown as clickable proof


## Capabilities (Beta)

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

## Install from the beta wheel

Download the wheel from the latest GitHub Release.

Windows / PowerShell:

```powershell
cd "$env:USERPROFILE\Downloads"
python -m pip install --upgrade pip
python -m pip install --force-reinstall ".\agentseal-5.0.0-1beta2fix1-py3-none-any.whl"
agentseal
```

macOS / Linux:

```bash
python3 -m pip install --upgrade pip
python3 -m pip install --force-reinstall ./agentseal-5.0.0-1beta2fix1-py3-none-any.whl
agentseal
```

Fallback launcher:

```bash
python -m agentseal
```

## Quick start

Inside the AgentSeal terminal UI:

```text
/help                      Show commands
/token paste               Paste a GitHub token for stronger public evidence search
/hf paste                  Paste a HuggingFace token for gated datasets
/audit 10                  Audit 10 bundled SWE-bench Verified instances
/pro 10                    Audit 10 bundled SWE-bench Pro instances
/auto multi-swe-bench 10   Load Multi-SWE-bench and audit 10 instances
/wizard                    Browse/select a local dataset
/reports                   List recent reports
/open                      Open the latest report
/quit                      Exit
```



## AI-native development disclosure

AgentSeal was created as an AI-native engineering project using human-directed, multi-model AI orchestration. Multiple AI systems were used during design exploration, implementation iteration, debugging, testing, documentation drafting, and release-hardening.

The maintainer remains responsible for the project direction, review, release decisions, security posture, and evidence standards. See [AI-native development disclosure](docs/AI_NATIVE_DEVELOPMENT.md).

## Full user guide

For public beta users, the complete docs are:

- [Command guide](docs/COMMANDS.md) — every `/` command, examples, token requirements, and outputs.
- [Workflows](docs/WORKFLOWS.md) — recommended beta workflows for SWE-bench Verified, SWE-bench Pro, Multi-SWE-bench, and custom datasets.
- [Methodology](docs/METHODOLOGY.md) — how AgentSeal normalizes rows, verifies evidence, scores risk, and separates evidence classes.
- [Token setup](docs/TOKENS.md) — how to create, paste, test, and revoke GitHub/HuggingFace tokens.
- [Report review checklist](docs/REPORT_REVIEW_CHECKLIST.md) — what to check before publishing a report.
- [Capabilities](docs/CAPABILITIES.md) — supported beta capabilities beyond SWE-bench.
- [AI-native development disclosure](docs/AI_NATIVE_DEVELOPMENT.md) — how AI-assisted engineering was used responsibly in the project.

## Tokens

Tokens are optional.

- GitHub token: improves rate limits and enables stronger independent evidence checks.
- HuggingFace token: needed only for gated/private HuggingFace datasets.

Never commit tokens. AgentSeal redacts token-like input in the TUI, but users should still treat pasted secrets carefully.

## Evidence standard

AgentSeal separates evidence classes:

- **CodeSeal:** local content-overlap signal from the packaged index.
- **Stack v2 Bloom:** probabilistic repository-membership signal.
- **Independent public-source evidence:** exact URLs verified through GitHub/HuggingFace search paths when available.

A report should not show a broken or synthetic evidence URL as clickable proof. If a URL is missing, placeholder-like, or fails strict checks, AgentSeal renders it as `not linked`.

## Development from source

The source repository does not commit the large beta artifacts. For full offline audits, install the release wheel or copy release artifacts into `agentseal/data/`.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install -e .[dev]
python -m compileall agentseal
pytest -q
```

Run the smoke test:

```bash
python scripts/smoke_test.py
```

## Repository contents

```text
agentseal/                 Python package
docs/                      Capabilities, install, token, beta, and report-review docs
tests/                     Basic import/smoke tests
scripts/                   Maintainer smoke-test helpers
.github/workflows/         CI workflow
THIRD_PARTY_NOTICES.md     Artifact/data notices
SECURITY.md                Security reporting
CONTRIBUTING.md            Contributor guide
```

## License

AgentSeal source code is released under the MIT License. See `LICENSE`.

Review `THIRD_PARTY_NOTICES.md` for beta wheel artifact notes.
