# Scope and beta limitations

AgentSeal is a local deterministic-core auditor for benchmark contamination-risk evidence.

It is designed to show:

- whether benchmark gold data overlaps with public/source artifacts;
- whether local corpus artifacts signal likely training-data availability;
- whether independent public-source evidence can be verified and linked.

Interpretation rules:

- CodeSeal overlap is a deterministic content-overlap signal against the packaged index.
- Stack v2 Bloom hits are probabilistic repository-membership signals.
- Independent public-source matches are public-availability evidence unless paired with corpus or temporal signals.
- Missing public links mean AgentSeal did not verify a link within the configured search path; they are not global absence evidence.
- Behavioral memorization claims require separate model-behavior testing.

Live GitHub/HuggingFace results can change. Reports are strongest when exact evidence URLs are present and verified.
