# Beta limitations

AgentSeal is a local deterministic-core auditor, not a model-memorization oracle.

It can show:

- whether benchmark gold data overlaps with public/source artifacts;
- whether local corpus artifacts signal likely training-data availability;
- whether independent public-source evidence can be verified and linked.

It does not prove:

- that a particular model trained on a specific item;
- that a particular answer was memorized by a model;
- that a missing public link means no contamination exists.

Live GitHub/HuggingFace results can change. Reports are strongest when exact evidence URLs are present and verified.
