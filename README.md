# AgentSeal Benchmark Forensics

**AgentSeal is an open-source benchmark-forensics auditor for AI-agent evaluations.**

AgentSeal audits AI coding benchmarks for contamination-risk, corpus exposure, public replication, and dataset leakage signals. It asks a simple question before you trust a leaderboard:

> Are the answers, tests, or benchmark artifacts already visible in public code or corpus-like indexes?

AgentSeal does **not** prove model memorization. It measures the exposure surfaces that make contamination possible and reports each evidence class separately.

This project is the **benchmark-forensics AgentSeal**. It is not affiliated with unrelated AgentSeal/agent-security projects.

## SWE-bench Pro Public Audit

AgentSeal audited **731 SWE-bench Pro public instances**.

| Signal | Result | Interpretation |
| --- | ---: | --- |
| CodeSeal deterministic content-overlap | 12 instances | Direct bundled-index content-overlap signal |
| Stack v2 Bloom probable corpus membership | 76 source repos | Probabilistic corpus-membership signal |
| Public test-signal exposure | 148 instances | Hidden/evaluation test code visible in source PR diffs |
| Date-unknown public replication | 234 instances | Mixed public-replication triage bucket |
| Default-branch gold-patch exposure path | 75.4% | Pro consensus/default-branch exposure signal |

The 234 public-replication signal is **not** a proven contamination rate. It can include forks, mirrors, benchmark/eval repos, teaching repos, vendored copies, and organic downstream copies. Treat it as a triage list unless backed by CodeSeal, Bloom, or date-known pre-cutoff temporal evidence.

**Read the public report without installing anything:**

- [SWE-bench Pro corpus-availability audit](docs/reports/swe-bench-pro-audit.md)
- [Rich HTML report](docs/reports/swe-bench-pro-audit.html)

## Install in One Command

```powershell
python -m pip install --force-reinstall "https://github.com/Nidhish2007/AgentSeal-Benchmark-Forensics/releases/download/v5.0.0-beta.2/agentseal-5.0.0-1beta2fix2-py3-none-any.whl"
```

Then run:

```powershell
agentseal
```

If `agentseal` is not found:

```powershell
python -m agentseal
```

Wheel SHA256:

```text
e0d9cd2cc7e2c1463ff91b96513e3df851f73c00375c8faed9c7630b430f8c46
```

## Reproduce the First Audit

Inside the AgentSeal terminal UI:

```text
/pro 10     Run a quick SWE-bench Pro sample
/pro        Run the bundled SWE-bench Pro public audit
/open       Open the latest report
```

GitHub/HuggingFace tokens are optional. They improve public evidence search and gated dataset loading, but local CodeSeal/Bloom checks can run without pasted tokens when benchmark rows are already local.

Do not trust the claim. Run the audit, inspect the report, and challenge the methodology.

> **Status:** public beta / reproducibility release. AgentSeal detects contamination-risk evidence layers; behavioral model memorization testing is a separate downstream step.

## Highlights

- Terminal UI plus CLI entry points
- Local CodeSeal content-overlap signal
- Stack v2 repository-membership Bloom filter support
- Bundled SWE-bench Verified / Pro beta data in the release wheel
- Optional GitHub evidence verification with exact public URLs
- Optional HuggingFace dataset loading for `/auto` and `/wizard` workflows
- JSON, Markdown, and HTML reports
- Strict evidence-link rendering: broken, placeholder, or unverified URLs are not shown as evidence


## Capabilities (Beta)

AgentSeal is not limited to one SWE-bench command. In this beta, it is a broader benchmark and dataset contamination-risk audit framework.

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
- Suppresses broken, placeholder-like, or unverified evidence URLs instead of presenting them as evidence.
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

### Evidence standard

AgentSeal reports evidence classes separately instead of collapsing every signal into one vague "contaminated" label:

- CodeSeal matches are deterministic content-overlap signals against the packaged local index.
- Stack v2 Bloom hits are probabilistic repository-membership signals.
- Mixed public-source matches are public-availability evidence unless paired with corpus or temporal signals.
- Test-signal exposure is reported separately from solution exposure.

This makes reports useful for filtering, bucketing, and deeper review without overstating what any single signal proves.

## Local Wheel File

```powershell
cd "$env:USERPROFILE\Downloads"
python -m pip install --upgrade pip
python -m pip install --force-reinstall ".\agentseal-5.0.0-1beta2fix2-py3-none-any.whl"
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

- [Command guide](docs/COMMANDS.md) - every `/` command, examples, token requirements, and outputs.
- [Workflows](docs/WORKFLOWS.md) - recommended beta workflows for SWE-bench Verified, SWE-bench Pro, Multi-SWE-bench, and custom datasets.
- [Methodology](docs/METHODOLOGY.md) - how AgentSeal normalizes rows, verifies evidence, scores risk, and separates evidence classes.
- [Token setup](docs/TOKENS.md) - how to create, paste, test, and revoke GitHub/HuggingFace tokens.
- [Report review checklist](docs/REPORT_REVIEW_CHECKLIST.md) - what to check before publishing a report.
- [Capabilities](docs/CAPABILITIES.md) - supported beta capabilities beyond SWE-bench.
- [AI-native development disclosure](docs/AI_NATIVE_DEVELOPMENT.md) - how AI-assisted engineering was used responsibly in the project.

## Tokens

Tokens are optional.

- GitHub token: improves rate limits and enables stronger independent evidence checks.
- HuggingFace token: needed only for gated/private HuggingFace datasets.

Never commit tokens. AgentSeal redacts token-like input in the TUI, but users should still treat pasted secrets carefully.

## Evidence standard

AgentSeal separates evidence classes:

- **CodeSeal:** local content-overlap signal from the packaged index.
- **Stack v2 Bloom:** probabilistic repository-membership signal.
- **Mixed public-source evidence:** exact URLs verified through GitHub/HuggingFace search paths when available.

A report should not show a broken or synthetic evidence URL as evidence. If a URL is missing, placeholder-like, or fails strict checks, AgentSeal renders it as `not linked`.

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
