# AgentSeal: A Benchmark-Forensics Audit of SWE-bench Pro Public

I built **AgentSeal**, an open-source benchmark-forensics auditor for AI-agent evaluations.

It does **not** test whether a model memorized an answer.

It audits the evidence surfaces around a benchmark: public answer exposure, corpus-overlap signals, probable corpus membership, public replication, and test-signal exposure.

I ran it on all **731 public SWE-bench Pro instances**.

The result is not a simple "this benchmark is contaminated" claim. The result is more specific, and more useful:

> SWE-bench Pro Public contains multiple measurable public-availability and corpus-overlap signals that should be audited before using the benchmark as a clean frontier evaluation.

Full report:

[Read the SWE-bench Pro audit report](reports/swe-bench-pro-audit.md)

Project:

[AgentSeal on GitHub](https://github.com/Nidhish2007/AgentSeal-Benchmark-Forensics)

---

## The Short Version

AgentSeal found:

| Signal | Count | What it means |
|---|---:|---|
| CodeSeal deterministic content-overlap | 12 instances | Strong deterministic overlap against an indexed corpus |
| Stack v2 Bloom probable corpus membership | 76 source repos | Probable membership in a Stack v2-style code corpus |
| Public test-signal exposure | 148 instances | Public artifacts expose test or benchmark-relevant signals |
| Date-unknown public replication | 234 instances | Gold-patch-like material appears publicly somewhere, but timing and origin are not proven |
| Default-branch gold-patch exposure path | 75.4% | A large fraction have a public exposure path through merged/default-branch state |

The most important correction:

**The 234 number is not "32% proven contamination."**

That would be overclaiming.

It means **234 date-unknown public replication signals**. That bucket can include forks, mirrors, benchmark/eval repositories, teaching material, vendored copies, and organic downstream copies.

It is a triage signal, not proof by itself.

The stronger evidence buckets are the deterministic CodeSeal overlap and the Bloom corpus-membership signals.

---

## Why I Built This

Modern AI-agent benchmarks are becoming extremely important.

They decide which systems look strong, which methods get attention, and which models appear to generalize.

But there is a hidden problem:

A benchmark can look private or difficult at the task level while parts of its answer surface, patch surface, tests, issue context, or derived artifacts are already public somewhere else.

That does not automatically mean a model cheated.

It does mean the benchmark deserves a forensic audit.

AgentSeal is my attempt to build that missing layer.

Not a model evaluator.

Not a leaderboard.

A benchmark-forensics tool.

---

## What AgentSeal Audits

AgentSeal looks for several different evidence types instead of collapsing everything into one vague contamination score.

### 1. Deterministic content overlap

This is the cleanest category.

AgentSeal checks whether benchmark-relevant content overlaps with an indexed corpus source.

In the SWE-bench Pro Public run, this produced **12 deterministic CodeSeal overlap signals**.

This is not the biggest number in the report, but it is one of the strongest kinds of evidence.

### 2. Bloom-filter corpus-membership signals

AgentSeal also checks probable membership against a Stack v2-style Bloom filter.

This produced **76 source-repo membership signals**.

A Bloom filter is probabilistic, so this should not be treated as a perfect exact match. But it is still much stronger than a random GitHub search result, because it is testing likely corpus membership rather than just public presence.

### 3. Public test-signal exposure

AgentSeal checks whether public artifacts expose signals that may help solve or identify benchmark instances.

In this run, AgentSeal found **148 public test-signal exposure instances**.

This matters because benchmark leakage is not only about the final answer.

Sometimes the dangerous signal is:

- the failing test
- the expected behavior
- the patch context
- the issue-to-fix mapping
- an eval harness artifact
- a dataset cache
- a reproduced oracle file

That can still weaken the benchmark as an unseen evaluation.

### 4. Date-unknown public replication

AgentSeal found **234 date-unknown public replication signals**.

This is the number that needs the most careful framing.

It means patch-like or answer-like material appears publicly somewhere.

But it does **not** prove:

- the public copy existed before SWE-bench Pro
- the public copy was in a model training corpus
- the public copy is organic
- the model saw it
- the benchmark is invalid

Some of these results may be forks, mirrors, eval repos, teaching material, vendored code, or downstream benchmark artifacts.

That is exactly why AgentSeal reports this bucket as **date-unknown public replication**, not as proven contamination.

The value of this bucket is triage.

It tells researchers:

> "These instances deserve inspection first."

---

## What This Report Does Not Claim

This report does **not** claim that SWE-bench Pro is fake.

It does **not** claim that every flagged instance is contaminated.

It does **not** claim that a model memorized the benchmark.

It does **not** claim that public replication equals training-data proof.

It does **not** claim that all 234 replication signals are organic independent sources.

The correct claim is:

> AgentSeal found measurable public-availability, corpus-overlap, and benchmark-exposure signals across SWE-bench Pro Public, including stronger deterministic and probabilistic corpus signals, plus a larger date-unknown public replication bucket that should be treated as triage evidence.

---

## Why This Still Matters

Even with the caveats, this matters.

Because frontier AI evaluation is becoming too important to rely on vibes.

If a benchmark is used to compare advanced coding agents, then we need to know:

- whether answer artifacts are public
- whether patches exist in likely training corpora
- whether tests or oracle files are exposed
- whether benchmark instances appear in eval harness repos
- whether forks and mirrors inflate apparent replication
- whether the benchmark has public exposure paths

A benchmark can still be useful after this audit.

But it should be used with clearer evidence.

AgentSeal does not destroy benchmarks.

It helps calibrate them.

---

## Why I Am Publishing the Full Report

I am publishing the full report because the details matter.

A single headline number would be misleading.

The report includes caveats such as:

- temporal alignment was unavailable for this run
- merge dates were missing for the audited public instances
- date-unknown replication should not be treated as pre-cutoff corpus proof
- benchmark/eval repositories should be separated from organic public copies
- forks and mirrors can create expected replication signals

That honesty is important.

If a tool finds something uncomfortable but hides its uncertainty, it becomes hype.

If it exposes both the signal and the uncertainty, it becomes useful.

That is what I want AgentSeal to be.

---

## The Main Lesson

The main lesson is not:

"32% of SWE-bench Pro is contaminated."

That is not the right conclusion.

The main lesson is:

> AI benchmarks need their own audit layer.

Before we treat a benchmark as clean, private, unseen, or frontier-grade, we should be able to inspect its exposure surface.

AgentSeal is a first open-source step toward that.

---

## Links

GitHub repo:

[https://github.com/Nidhish2007/AgentSeal-Benchmark-Forensics](https://github.com/Nidhish2007/AgentSeal-Benchmark-Forensics)

Full SWE-bench Pro audit report:

[https://github.com/Nidhish2007/AgentSeal-Benchmark-Forensics/blob/main/docs/reports/swe-bench-pro-audit.md](https://github.com/Nidhish2007/AgentSeal-Benchmark-Forensics/blob/main/docs/reports/swe-bench-pro-audit.md)

---

## Final Thought

I do not want people to blindly trust AgentSeal.

I want people to run it, inspect the reports, challenge the methodology, and improve the audit layer around AI benchmarks.

If we are going to build stronger AI agents, we also need stronger ways to verify what our evaluations are actually measuring.

That is the category AgentSeal is trying to open.
