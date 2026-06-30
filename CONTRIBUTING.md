# Contributing

AgentSeal is in beta. Contributions should preserve the evidence standard: do not add report language that overclaims model memorization, and do not render unverified URLs as proof.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install -e .[dev]
```

## Checks

```bash
python -m compileall agentseal
pytest -q
```

## Pull request checklist

- Add or update a test for the changed behavior.
- Do not commit tokens, reports with private data, or cache files.
- Do not add large artifacts to normal Git history.
- Keep CodeSeal/Bloom/independent-public evidence wording separate.

## AI-assisted contributions

AI-assisted contributions are welcome, but contributors are responsible for the submitted code. Please review, test, and understand changes before opening a pull request. Avoid submitting unreviewed generated code, especially in token handling, dataset downloading, evidence verification, report rendering, and security-sensitive paths.

