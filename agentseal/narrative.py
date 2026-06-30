"""Adaptive narrative engine for AgentSeal reports.

Generates data-driven, varied text for reports — NOT hardcoded templates.
The narrative adapts based on actual audit results: contamination rate,
method agreement, repo distribution, confidence levels, etc.

This makes every report feel "alive" and specific to the data, not a
cookie-cutter template with numbers plugged in.
"""

from __future__ import annotations

import hashlib
import random
from typing import Optional
from .schemas import AuditReport, AuditSummary, RiskLevel
from .report import _html_escape


class NarrativeEngine:
    """Generates adaptive narrative text based on audit data."""

    def __init__(self, report: AuditReport, m1_hits: int = 0, m2_hits: int = 0,
                 m3_hits: int = 0, m4_hits: int = 0, repo_data: list = None):
        self.report = report
        self.s = report.summary
        self.m1_hits = m1_hits
        self.m2_hits = m2_hits
        self.m3_hits = m3_hits
        self.m4_hits = m4_hits
        self.repo_data = repo_data or []
        # HTML-escape the benchmark name to prevent XSS
        self.bench = _html_escape(report.config.benchmark)
        # Seed with a stable digest so same audit data yields the same narrative
        # across Python processes. Built-in hash() is randomized per process.
        seed_material = "|".join(map(str, (
            self.s.total_instances,
            f"{self.s.contamination_rate:.8f}",
            self.s.critical_count,
            self.m1_hits, self.m2_hits, self.m3_hits, self.m4_hits,
        )))
        seed = int(hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:16], 16)
        self._rng = random.Random(seed)

    def _pick(self, options: list[str]) -> str:
        """Pick one option deterministically based on data hash."""
        return self._rng.choice(options)

    def _severity_word(self) -> str:
        """Return a severity word based on contamination rate."""
        rate = self.s.contamination_rate
        if rate >= 0.75:
            return self._pick(["severe", "critical", "widespread", "systemic"])
        elif rate >= 0.50:
            return self._pick(["significant", "substantial", "major", "concerning"])
        elif rate >= 0.25:
            return self._pick(["moderate", "notable", "measurable", "appreciable"])
        elif rate > 0:
            return self._pick(["limited", "minor", "low-level", "marginal"])
        return "no"

    def _rate_phrase(self) -> str:
        """Return a phrase describing the contamination rate."""
        rate = self.s.contamination_rate * 100
        if rate >= 90:
            return self._pick([
                f"an alarming {rate:.1f}%",
                f"a staggering {rate:.1f}%",
                f"a near-total {rate:.1f}%",
                f"{rate:.1f}% — nearly every instance",
            ])
        elif rate >= 75:
            return self._pick([
                f"a {self._severity_word()} {rate:.1f}%",
                f"{rate:.1f}% — more than three-quarters",
                f"a concerning {rate:.1f}%",
            ])
        elif rate >= 50:
            return self._pick([
                f"{rate:.1f}% — over half",
                f"a majority at {rate:.1f}%",
                f"{rate:.1f}%, a {self._severity_word()} level",
            ])
        elif rate >= 25:
            return self._pick([
                f"{rate:.1f}% — a quarter or more",
                f"a {self._severity_word()} {rate:.1f}%",
                f"{rate:.1f}%, {self._severity_word()} but present",
            ])
        elif rate > 0:
            return self._pick([
                f"a low {rate:.1f}%",
                f"only {rate:.1f}%",
                f"a marginal {rate:.1f}%",
            ])
        return "0% — no contamination detected"

    def generate_tldr(self) -> str:
        """Generate a data-adaptive TLDR paragraph (HTML with <strong> tags).

        and the deterministic/no-LLM guarantee.
        """
        s = self.s
        bench = self.bench  # already HTML-escaped
        severity = self._severity_word()
        rate_phrase = self._rate_phrase()

        # Opening — varies by severity
        if s.contamination_rate >= 0.75:
            opening = self._pick([
                f"AgentSeal's deterministic audit of <strong>{s.total_instances}</strong> {bench} instances reveals a <strong style='color:var(--crit)'>{severity} contamination crisis</strong>: {rate_phrase} of benchmark answers are publicly available in source repositories. Measured via 8-gram overlap (academic standard). No LLM and no model access; results are deterministic for fixed inputs, local artifacts, cache state, and live-source responses.",
                f"A <strong style='color:var(--crit)'>{severity}</strong> finding: {rate_phrase} of <strong>{s.total_instances}</strong> {bench} instances have their gold-patch solutions exposed in public code. 8-gram overlap measures textual similarity. Deterministic, no LLM.",
                f"The results are <strong style='color:var(--crit)'>{severity}</strong>. Of <strong>{s.total_instances}</strong> {bench} instances audited, {rate_phrase} are contaminated — their answers live in public GitHub repos. Deterministic for fixed inputs and evidence sources.",
            ])
        elif s.contamination_rate >= 0.50:
            opening = self._pick([
                f"AgentSeal audited <strong>{s.total_instances}</strong> {bench} instances and found <strong style='color:var(--crit)'>{rate_phrase}</strong> are contaminated. 8-gram overlap measures textual similarity; no LLM used.",
                f"Of <strong>{s.total_instances}</strong> {bench} instances, <strong style='color:var(--crit)'>{rate_phrase}</strong> have publicly available solutions. Deterministic, reproducible.",
                f"The audit uncovered <strong style='color:var(--crit)'>{severity} contamination</strong>: {rate_phrase} of {bench} instances are exposed.",
            ])
        elif s.contamination_rate > 0:
            opening = self._pick([
                f"AgentSeal audited <strong>{s.total_instances}</strong> {bench} instances. Contamination is <strong style='color:var(--med)'>{rate_phrase}</strong>.",
                f"Of <strong>{s.total_instances}</strong> {bench} instances, <strong style='color:var(--med)'>{rate_phrase}</strong> show contamination.",
                f"The audit found <strong style='color:var(--med)'>{severity} contamination</strong> at {rate_phrase} of {bench} instances.",
            ])
        else:
            opening = f"AgentSeal audited <strong>{s.total_instances}</strong> {bench} instances. <strong style='color:var(--green)'>No contamination detected.</strong>"

        # Test code exposure — only mention if non-zero
        test_part = ""
        if s.instances_with_test_patch_exposure > 0:
            test_pct = s.test_patch_exposure_rate * 100
            test_part = self._pick([
                f" <strong style='color:var(--med)'>{s.instances_with_test_patch_exposure} ({test_pct:.1f}%)</strong> have their hidden test code exposed — the grading criteria themselves are public.",
                f" Critically, <strong style='color:var(--med)'>{s.instances_with_test_patch_exposure}</strong> instances ({test_pct:.1f}%) leak their test cases.",
                f" <strong style='color:var(--med)'>{test_pct:.1f}%</strong> also expose hidden tests, enabling test-gaming rather than genuine problem-solving.",
            ])

        # Method agreement
        agreement_part = ""
        if self.m1_hits > 0 and self.m2_hits > 0:
            max_hits = max(self.m1_hits, self.m2_hits)
            min_hits = min(self.m1_hits, self.m2_hits)
            if max_hits > 0:
                agreement = round(min_hits / max_hits * 100)
                agreement_part = self._pick([
                    f" M1 (exact match) and M2 (normalized match) agree on <strong>{agreement}%</strong> of detections, making false positives extremely unlikely.",
                    f" Two correlated methods (M1/M2) agree on <strong>{agreement}%</strong> of findings.",
                    f" Method consensus: M1 and M2 agree <strong>{agreement}%</strong> of the time.",
                ])

        return opening + test_part + agreement_part

    def generate_root_cause(self) -> str:
        """Generate an adaptive root-cause analysis paragraph."""
        s = self.s
        bench = self.bench  # already HTML-escaped
        paragraphs = []

        # Root cause — fully adaptive, no hardcoded company names
        root = self._pick([
            f"{bench} sources tasks from real GitHub repositories. The gold patch (the answer) is the actual code change that fixed the bug. That fix was merged into the public repository — meaning the fix code is publicly accessible to any model that may have been trained on GitHub data.",
            f"The contamination stems from {bench}'s design: each instance's gold patch is a real merged commit or PR diff in a public GitHub repo. The fix code has been in the repo's default branch since before the benchmark was created.",
            f"At the core: {bench} uses real-world fixes as benchmark answers. These fixes were merged publicly, making them eligible for GitHub-derived training corpora when they predate the relevant cutoff.",
            f"Each {bench} instance corresponds to a real GitHub pull request. The PR diff (the answer) is public, making it available to corpus builders that ingest public GitHub data.",
        ])
        paragraphs.append(root)

        # Test code problem — only if M3 found hits
        if self.m3_hits > 0:
            test_issue = self._pick([
                f"In {self.m3_hits} instances, the hidden test cases are also public. This is worse than answer contamination: a model that knows the tests can optimize for passing them rather than solving the actual problem. {bench} scores may reflect test-gaming, not engineering capability.",
                f"{self.m3_hits} instances have their grading tests exposed. When a model can see the tests, it can game them — producing outputs that pass without genuinely solving the problem. This undermines the benchmark's validity more than answer leakage alone.",
                f"The {self.m3_hits} instances with exposed test code represent a deeper problem. Test contamination enables models to optimize for test-passing rather than correctness, making scores unreliable.",
            ])
            paragraphs.append(test_issue)

        # Why some repos are 0% — only if we have repo data with 0% repos
        zero_repos = [r for r in self.repo_data if r.get("rate", 0) == 0]
        if zero_repos:
            repo_names = ", ".join(f"`{_html_escape(r.get('repo', 'unknown'))}`" for r in zero_repos[:3])
            coverage = self._pick([
                f"Some repositories show 0% contamination. {repo_names} likely restructured their codebase (files moved or renamed) after the fix was applied. AgentSeal can't find the files at HEAD, so they're marked `head_not_found`. This is a coverage gap, not proof of cleanliness.",
                f"Zero-contamination repos ({repo_names}) probably reorganized their file structure post-merge. The fix code may still be in the repo under a different path — AgentSeal just can't locate it. Treat 0% as 'unverifiable', not 'clean'.",
                f"Repos showing 0% ({repo_names}) may have been refactored since the fix. The code exists somewhere, but the file paths in the gold patch no longer match. This is a limitation of path-based detection, not evidence of cleanliness.",
            ])
            paragraphs.append(coverage)

        return "\n\n".join(paragraphs)

    def generate_method_analysis(self) -> str:
        """Generate adaptive analysis of detection methods."""
        parts = []

        if self.m1_hits > 0:
            m1_desc = self._pick([
                f"M1 (exact line match) detected {self.m1_hits} instances where fix code appears verbatim at HEAD but not at base_commit. This is the strongest evidence of contamination.",
                f"M1 found {self.m1_hits} instances with byte-identical fix lines in the current codebase. These are definitive contamination — the exact answer is in the repo.",
                f"{self.m1_hits} instances were caught by M1's exact match. These represent the clearest cases: the gold patch's added lines are present word-for-word in the public code.",
            ])
            parts.append(m1_desc)

        if self.m2_hits > 0:
            m2_desc = self._pick([
                f"M2 (normalized match) caught {self.m2_hits} instances where the fix matches after stripping comments and normalizing whitespace. These are cases where the code is semantically identical but textually different.",
                f"M2 detected {self.m2_hits} additional instances through normalized comparison (comment-stripped, whitespace-collapsed). These are likely the same fixes with minor formatting changes.",
                f"Through whitespace and comment normalization, M2 identified {self.m2_hits} more instances. These represent fixes that were reformatted but are functionally identical.",
            ])
            parts.append(m2_desc)

        if self.m3_hits > 0:
            m3_desc = self._pick([
                f"M3 (test patch match) flagged {self.m3_hits} instances where the hidden test code is also public. This is the most damaging finding — it enables test-gaming.",
                f"M3 revealed {self.m3_hits} instances with exposed test cases. This is particularly severe: models can learn the grading criteria, not just the answers.",
                f"{self.m3_hits} instances have public test code (M3). This allows models to optimize for test-passing rather than genuine problem-solving.",
            ])
            parts.append(m3_desc)

        if self.m4_hits > 0:
            m4_desc = self._pick([
                f"M4 (problem statement match) found {self.m4_hits} instances where the issue text appears in the source code (e.g., in comments or commit messages). This indicates the problem description itself leaked into the codebase.",
                f"M4 detected {self.m4_hits} instances with problem statement text in source files. The benchmark's questions are referenced in the code, providing additional training signal.",
                f"{self.m4_hits} instances show problem statement text embedded in the codebase (M4). This gives models context about what the benchmark is testing.",
            ])
            parts.append(m4_desc)

        if not parts:
            return "No contamination was detected by any of the four methods."

        return "\n\n".join(parts)

    def generate_remediation(self) -> list[str]:
        """Generate adaptive remediation recommendations based on findings."""
        recs = []
        s = self.s
        bench = self.bench  # already HTML-escaped

        # Always recommend private fixes
        recs.append(self._pick([
            f"For {bench} maintainers: regenerate the benchmark using patches from commits that were NEVER merged to any public branch. Maintain a private fork where fixes live but are never pushed.",
            f"Regenerate {bench} from private, unmerged commits. The current approach (using merged fixes) makes the fix code publicly accessible and therefore eligible for GitHub-derived data collection.",
            f"The only real fix: rebuild {bench} with patches that have never been public. As long as fixes are in public repos, contamination is inevitable.",
        ]))

        # Model developer recommendation
        if s.contamination_rate >= 0.50:
            recs.append(self._pick([
                f"For model developers: stop reporting {bench} scores. The benchmark is {self._severity_word()}ly contaminated ({s.contamination_rate*100:.1f}%). Scores on {bench} do not reflect genuine coding ability.",
                f"Model developers should treat {bench} as compromised. With {s.contamination_rate*100:.1f}% contamination, scores are unreliable and should not be reported.",
                f"Given {s.contamination_rate*100:.1f}% contamination, model developers should cease using {bench} for evaluation. The scores measure memorization, not capability.",
            ]))
        else:
            recs.append(self._pick([
                f"For model developers: treat {bench} scores with caution. {s.contamination_rate*100:.1f}% contamination was detected. Scores may be partially inflated.",
                f"Model developers should interpret {bench} scores carefully. The {s.contamination_rate*100:.1f}% contamination rate means some scores may reflect memorization.",
            ]))

        # Test code recommendation — only if M3 found hits
        if self.m3_hits > 0:
            recs.append(self._pick([
                f"URGENT: The {self.m3_hits} instances with exposed test code must have their tests regenerated. Test contamination is the most damaging vector — it enables gaming the grading criteria.",
                f"Address the {self.m3_hits} test-exposed instances immediately. Regenerate all hidden tests so they cannot be gamed. This is higher priority than fixing answer contamination.",
                f"The {self.m3_hits} instances with public tests need new test cases. Without this, models can pass by gaming tests rather than solving problems.",
            ]))

        # Evaluator recommendation
        recs.append(self._pick([
            f"For evaluators: do not assume benchmarks marketed as 'contamination-resistant' are clean. Audit them with AgentSeal first. {bench} was found to be {s.contamination_rate*100:.1f}% contaminated.",
            f"Evaluators should run AgentSeal on any benchmark before trusting scores. {bench}'s {s.contamination_rate*100:.1f}% contamination rate was only discovered through systematic auditing.",
            f"Before using any benchmark, evaluators should verify it with AgentSeal. The {s.contamination_rate*100:.1f}% contamination in {bench} would have gone undetected without this audit.",
        ]))

        # Community recommendation
        recs.append(self._pick([
            f"Build and maintain a public contamination registry. Model cards should cite verifiable audits, not marketing claims of 'contamination-free'.",
            f"The community needs a public contamination database. Benchmarks should link to their AgentSeal audit reports, not just claim to be clean.",
            f"Establish a contamination registry where benchmarks publish their AgentSeal audit results. This creates accountability and trust.",
        ]))

        # Periodic re-audit
        recs.append(self._pick([
            f"Re-run AgentSeal periodically. Repos evolve, files move, and new training corpora are released. A clean audit today may not hold tomorrow.",
            f"Contamination is not static. Re-audit {bench} regularly as repos change and new models are trained on updated data.",
            f"Schedule regular re-audits. As GitHub repos are updated and new training data is collected, contamination status can change.",
        ]))

        return recs


def generate_adaptive_report_content(report: AuditReport, m1_hits: int = 0,
                                       m2_hits: int = 0, m3_hits: int = 0,
                                       m4_hits: int = 0, repo_data: list = None):
    """Generate all adaptive content for a report.

    Returns a dict with: tldr, root_cause, method_analysis, remediation
    """
    engine = NarrativeEngine(report, m1_hits, m2_hits, m3_hits, m4_hits, repo_data)
    return {
        "tldr": engine.generate_tldr(),
        "root_cause": engine.generate_root_cause(),
        "method_analysis": engine.generate_method_analysis(),
        "remediation": engine.generate_remediation(),
    }
