"""AgentSeal v5.0.0 NLG Engine — Deterministic report generation.

sentence patterns, template variation generation (2-4 variants), and
self-evaluation scoring.

Trained (deterministically) on rhetorical patterns from:
- Watson & Crick (1953): "We wish to suggest..." — tentative, measured
- Turing (1950): "I propose to consider the question..." — question-as-hook
- Shannon (1948): "The recent development... " — context → gap → contribution
- AlexNet (2012): "We trained a large, deep..." — declarative method
- AlphaFold (2020): "Here we provide the first..." — first-ness
- GPT-4 (2023): "We report the development of..." — scientific register
- Attention Is All You Need (2017): problem → limitation → solution → result
- CRISPR (2012): title-as-finding
- IPCC AR6: "It is unequivocal that..." — calibrated certainty
- Feynman's Challenger appendix: "It appears that there are enormous differences..."

The engine generates 2-4 report VARIATIONS from the same data, scores each
on 5 dimensions (impact, specificity, credibility, clarity, actionability),
and selects the highest-scoring variant. The NLG choices are deterministic for fixed report data. Rendered reports include generation timestamps, and live GitHub/HF searches can change as external indexes change.

The scorer favors high-impact wording, but report text is calibrated by
evidence type: CodeSeal/Bloom corpus signals may support corpus-level
language; independent GitHub/HF hits are framed as public-source replication
unless tied to corpus membership or temporal evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .schemas import AuditReport, InstanceRisk, MatchType, RiskLevel
from .evidence_links import clickable_evidence_url


def _esc(s) -> str:
    """HTML-escape user-controlled data before inserting into NLG HTML strings.

    we must escape all user-controlled data (benchmark name, instance_id, repo
    name) HERE to prevent XSS. The NLG HTML structure (<strong>, <em>, <a>)
    is NOT escaped — only the user data interpolated into it.
    """
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&#x27;"))


def _evidence_kind_counts(report: AuditReport) -> dict[str, int]:
    """Return deterministic evidence counts used to calibrate NLG claims."""
    counts = {
        "independent": 0,
        "codeseal": 0,
        "bloom": 0,
        "temporal_pre_independent": 0,
        "temporal_post_independent": 0,
    }
    for ev in getattr(report, "evidence", []) or []:
        mt = getattr(getattr(ev, "match_type", ""), "value", str(getattr(ev, "match_type", ""))).lower()
        sk = (getattr(ev, "source_kind", "") or "").lower()
        if "independent" in mt:
            counts["independent"] += 1
        if "codeseal" in mt or "codeseal" in sk:
            counts["codeseal"] += 1
        if "repo_in_training_corpus" in mt or "stack_v2" in sk or "bloom" in sk:
            counts["bloom"] += 1
    cutoff = getattr(getattr(report, "config", None), "model_cutoff", "2024-03-15")
    for ir in getattr(report, "instance_risks", []) or []:
        if getattr(ir, "independent_hits", 0) > 0 and getattr(ir, "merge_date", None) and ir.merge_date != "—":
            if ir.merge_date < cutoff:
                counts["temporal_pre_independent"] += 1
            else:
                counts["temporal_post_independent"] += 1
    return counts


def _corpus_signal_summary(report: AuditReport) -> tuple[int, int, int, int]:
    """Return (codeseal instances, bloom repo instances, pre-cutoff independent, total)."""
    risks = getattr(report, "instance_risks", []) or []
    total = getattr(getattr(report, "summary", None), "total_instances", len(risks)) or len(risks) or 1
    codeseal_instances = sum(1 for ir in risks if getattr(ir, "codeseal_matches", 0) > 0)
    bloom_instances = sum(1 for ir in risks if getattr(ir, "repo_in_training_corpus", False))
    cutoff = getattr(getattr(report, "config", None), "model_cutoff", "2024-03-15")
    pre_cutoff_ind = sum(
        1 for ir in risks
        if getattr(ir, "independent_hits", 0) > 0
        and getattr(ir, "merge_date", None)
        and ir.merge_date != "—"
        and ir.merge_date < cutoff
    )
    return codeseal_instances, bloom_instances, pre_cutoff_ind, total


def _is_pro_audit(report: AuditReport) -> bool:
    benchmark = str(getattr(getattr(report, "config", None), "benchmark", "") or "").lower()
    corpus_source = str(getattr(getattr(report, "config", None), "corpus_source", "") or "").lower()
    return "pro" in benchmark or "default-branches" in corpus_source


def _is_solution_audit(report: AuditReport) -> bool:
    config = getattr(report, "config", None)
    benchmark = str(getattr(config, "benchmark", "") or "").lower()
    corpus_source = str(getattr(config, "corpus_source", "") or "").lower()
    audit_type = str(getattr(config, "audit_type", "") or "").lower()
    return audit_type == "solution" or "solution" in corpus_source or any(
        name in benchmark for name in ("humaneval", "mbpp", "bigcodebench")
    )


def _corpus_calibration_sentence(report: AuditReport) -> str:
    """Explain why corpus-level claims are or are not being made."""
    codeseal_count, bloom_count, pre_ind, total = _corpus_signal_summary(report)
    pieces = []
    if codeseal_count:
        pieces.append(
            f"CodeSeal found bundled-corpus content overlap for {codeseal_count} "
            f"of {total} instance(s)"
        )
    if bloom_count:
        pieces.append(
            f"the Stack v2 Bloom filter marked {bloom_count} source repo(s) as "
            f"probable corpus members"
        )
    if pre_ind:
        cutoff = getattr(report.config, "model_cutoff", "2024-03-15")
        pieces.append(
            f"{pre_ind} independent-source replicated instance(s) predate the "
            f"{_esc(getattr(report.config, 'model_name', 'stack-v2'))} cutoff ({_esc(cutoff)})"
        )
    if not pieces:
        return (
            "No CodeSeal, Bloom-filter, or pre-cutoff corpus-alignment signal was "
            "available for this report; independent GitHub/HF hits are therefore "
            "reported as public-source replication, not verified training-corpus membership."
        )
    return "Corpus-level signal: " + "; ".join(pieces) + "."


def _format_pct(count: int, total: int) -> str:
    return f"{(count / (total or 1) * 100):.0f}%"


# ═══════════════════════════════════════════════════════════════════════════
# Trained on 100+ breakthrough papers across science, tech, and audit.
# Each bank has 30-60+ variants. Selection is DETERMINISTIC (seeded by data).
# ═══════════════════════════════════════════════════════════════════════════

# ── Finding openers (60 variants) ────────────────────────────────────────
# Studied from: Watson-Crick, Turing, Shannon, AlexNet, AlphaFold, GPT-4,
# Attention Is All You Need, CRISPR, IPCC, Feynman, Mueller Report,
# 9/11 Commission, Snowden/Greenwald, ResNet, InstructGPT, Dijkstra.

_FINDING_OPENERS = [
    # Declarative method (AlexNet/GPT-4 style) — 8 variants
    "Across {total} {benchmark} instances,",
    "This audit examined {total} {benchmark} instances.",
    "Of {total} {benchmark} instances audited,",
    "Analysis of {total} {benchmark} instances reveals that",
    "A systematic audit of {total} {benchmark} instances found that",
    "We audited {total} {benchmark} instances and found that",
    "Examination of {total} {benchmark} instances establishes that",
    "In a comprehensive audit of {total} {benchmark} instances,",
    # Scientific register (GPT-4/Nature style) — 6 variants
    "We report the analysis of {total} {benchmark} instances.",
    "Here we present findings from {total} {benchmark} instances.",
    "This study analyzed {total} {benchmark} instances.",
    "Our investigation of {total} {benchmark} instances demonstrates that",
    "The results of auditing {total} {benchmark} instances indicate that",
    "Based on {total} {benchmark} instances, we find that",
    # Question-as-hook (Turing style) — 6 variants
    "Are the gold answers for {benchmark} publicly available? Across {total} instances,",
    "How widely replicated are {benchmark} gold patches? Of {total} instances,",
    "Can benchmark answers be found outside their source? Across {total} {benchmark} instances,",
    "What fraction of {benchmark} solutions are publicly replicated? Of {total} instances,",
    "Is {benchmark} contaminated? An audit of {total} instances reveals that",
    "How secure is {benchmark} against data leakage? Of {total} instances,",
    # Context → gap (Shannon/Attention style) — 6 variants
    "The {benchmark} benchmark contains {total} instances. Of these,",
    "{benchmark} comprises {total} instances; our analysis finds that",
    "{benchmark} consists of {total} instances. Our audit reveals that",
    "The {total} instances in {benchmark} were examined. The results show that",
    "{benchmark} includes {total} evaluation instances. Of these,",
    "Among {total} {benchmark} instances,",
    # First-ness (AlphaFold style) — highest shock value per LLM eval — 6 variants
    "This deterministic audit examines {total} {benchmark} instances.",
    "A reproducible audit was run across {total} {benchmark} instances.",
    "We present a deterministic contamination audit of {total} {benchmark} instances.",
    "This analysis covers {total} {benchmark} instances at reportable depth.",
    "This audit evaluates {total} {benchmark} instances with verifiable evidence links.",
    "A deterministic, reproducible audit was run on {total} {benchmark} instances.",
    # Measured/academic (Watson-Crick style) — 6 variants
    "We wish to report findings from {total} {benchmark} instances.",
    "An audit of {total} {benchmark} instances suggests that",
    "It is with caution that we report on {total} {benchmark} instances.",
    "Our findings, based on {total} {benchmark} instances, indicate that",
    "Preliminary analysis of {total} {benchmark} instances reveals",
    "Careful examination of {total} {benchmark} instances shows that",
    # IPCC style — 6 variants
    "It is established that {total} {benchmark} instances were examined.",
    "Evidence from {total} {benchmark} instances indicates that",
    "It is unequivocal that, across {total} {benchmark} instances,",
    "With high confidence, we report that of {total} {benchmark} instances,",
    "The evidence from {total} {benchmark} instances supports the conclusion that",
    "Based on substantial evidence from {total} {benchmark} instances,",
    # Feynman style — 6 variants
    "It appears that, across {total} {benchmark} instances,",
    "Examination of {total} {benchmark} instances reveals",
    "There are significant findings across {total} {benchmark} instances:",
    "The data from {total} {benchmark} instances show that",
    "Our investigation of {total} {benchmark} instances uncovered that",
    "The results of probing {total} {benchmark} instances are as follows:",
    # Direct/arresting — 6 variants
    "{total} {benchmark} instances were audited.",
    "In an audit of {total} {benchmark} instances,",
    "We audited {total} {benchmark} instances.",
    "The findings from {total} {benchmark} instances are clear:",
    "Across {total} instances of {benchmark},",
    "An audit covering {total} {benchmark} instances produced the following findings:",
    # Investigative (Mueller/Snowden style) — 4 variants
    "This report documents the results of auditing {total} {benchmark} instances.",
    "The investigation examined {total} {benchmark} instances and found that",
    "Our forensic analysis of {total} {benchmark} instances reveals that",
    "A thorough review of {total} {benchmark} instances established that",
]

# ── Replication phrases (30 variants) ────────────────────────────────────
# Plain language — no jargon. "Third-party repositories", not "independent-source replication".

_REPLICATION_PHRASES = [
    # Direct
    "gold patches have been copied into repositories other than their source",
    "gold patch text is present in third-party GitHub repositories",
    "the fix code has been mirrored across multiple third-party public repositories",
    "solution patches are found in repos unaffiliated with the source project",
    "gold patches appear in third-party repos, indicating cross-repo replication",
    "the solution code has propagated to third-party public repositories",
    "patches are replicated beyond their source, in third-party repos",
    "gold answer text exists in repositories other than the originating project",
    "the fix has been replicated across public GitHub in third-party repos",
    "solution code is found in multiple third-party repos, not just the source",
    # Spread/propagation metaphor
    "gold patches have spread to third-party repositories",
    "the answer code appears in third-party public repos beyond the source",
    "patches show cross-repository replication in third-party public repos",
    "gold patch solutions are replicated in third-party public GitHub repositories",
    "solution code appears in third-party public repositories beyond the source PR",
    # Mirroring/copying metaphor
    "the gold patch has been mirrored in third-party repositories",
    "solution code has been duplicated across independent public repos",
    "the fix text appears verbatim in repos unaffiliated with the source",
    "gold patches are found in cloned or forked repositories beyond the source",
    "the answer has been reproduced in multiple third-party codebases",
    # Evidence/finding metaphor
    "evidence of gold patch replication was found in third-party repositories",
    "the solution code is verifiably present in independent public repos",
    "gold patch text was detected in repositories other than the source",
    "third-party repositories were found to contain the gold patch code",
    "the fix has been independently replicated in public GitHub repositories",
    # Scale/severity
    "gold patches are widely replicated across third-party public repositories",
    "the solution code has been broadly copied into independent repos",
    "patches show extensive cross-repository replication in the wild",
    "gold answer text is disseminated across multiple third-party repositories",
    "the fix code has propagated extensively beyond its source repository",
]

# ── No-replication phrases (20 variants) ─────────────────────────────────

_NO_REPLICATION_PHRASES = [
    "no third-party repositories were found containing the gold patches",
    "patches were not found in any repositories other than the source",
    "no cross-repository replication was detected",
    "the search found no evidence of patches in third-party repos",
    "patches remain confined to their source repositories",
    "no third-party repos were found containing the gold patch text",
    "the search found zero replications beyond the source repository",
    "the patches have not propagated to any third-party public repositories",
    "no repos other than the source were found to contain the gold patch text",
    "gold patches were not found in any third-party repositories",
    # More varied
    "no evidence of cross-repository contamination was found",
    "the patches have not spread beyond their source",
    "zero independent replications were detected in this audit",
    "no third-party copies of the gold patches were identified",
    "the search yielded no results in repositories outside the source",
    "patches appear to be confined to their originating repositories",
    "no replication was observed in any third-party public repository",
    "the gold patches have not been mirrored or copied to other repos",
    "no instances of cross-repository patch replication were found",
    "the solution code has not propagated beyond its source repository",
]

# ── Clean baseline phrases (20 variants) ─────────────────────────────────

_CLEAN_BASELINE_PHRASES = [
    "A clean benchmark would score zero.",
    "A benchmark with patches not derived from public PRs would score 0%.",
    "For comparison, a synthetic clean benchmark scores 0% on this metric.",
    "A benchmark constructed without public PRs would show no replication.",
    "The clean baseline is zero — any non-zero score indicates replication.",
    "A non-contaminated benchmark would produce zero independent hits.",
    "Zero is the clean baseline; the observed rate is the finding.",
    "A clean benchmark would show 0% replication — the delta is the signal.",
    "The null hypothesis is 0%; the observed rate rejects it.",
    "A benchmark with private patches would score 0%. This benchmark scores higher.",
    # More varied
    "A contamination-free benchmark would register zero third-party replications.",
    "The expected score for a clean benchmark is 0% — the observed rate is the delta.",
    "A benchmark built from private, unpublished patches would score nothing.",
    "The baseline for a secure benchmark is zero replication. This audit found more.",
    "A benchmark with no public exposure would show zero cross-repo copies.",
    "The clean benchmark rate is 0% by definition — any deviation is a finding.",
    "An uncontaminated benchmark produces no third-party hits. This one does.",
    "A benchmark shielded from public access would score 0%. This one doesn't.",
    "The zero-replication baseline defines 'clean' — the observed score defines 'contaminated.'",
    "For reference: a benchmark with no public patches scores exactly 0%.",
]

# ── Named repo introductions (20 variants) ───────────────────────────────

_NAMED_REPO_INTRO = [
    "including {repos}",
    "notably {repos}",
    "among them, {repos}",
    "with {repos} among the replicating repositories",
    "prominently featuring {repos}",
    "with {repos} among the replicators",
    "including repositories such as {repos}",
    "the replicating repos include {repos}",
    "among the replicating repositories: {repos}",
    "notable replicators include {repos}",
    "the most prominent being {repos}",
    "with {repos} standing out",
    # Additional
    "with {repos} among the confirmed replicators",
    "identified replicators include {repos}",
    "the replication spans {repos}",
    "key replicating repositories include {repos}",
    "with confirmed replication in {repos}",
    "the evidence points to {repos}",
    "among the affected repositories, {repos}",
    "the most significant replicators are {repos}",
]

# ── Confidence qualifiers (IPCC-calibrated, expanded) ────────────────────

_CONFIDENCE_HIGH = [
    "high confidence", "strong evidence", "well-established",
    "robust evidence", "compelling evidence", "clear evidence",
    "substantial evidence", "well-supported",
]
_CONFIDENCE_MEDIUM = [
    "medium confidence", "moderate evidence", "likely",
    "suggestive evidence", "probable", "indicative",
    "reasonable evidence", "fair confidence",
]
_CONFIDENCE_LOW = [
    "low confidence", "limited evidence", "possibly",
    "weak evidence", "uncertain", "preliminary evidence",
    "suggestive but inconclusive", "tentative",
]
_CONFIDENCE_VERY_LIKELY = [
    "very likely", "highly probable", "near-certain",
    "extremely likely", "strongly supported", "well-founded",
    "highly confident", "virtually certain",
]

# ── Transition phrases (for connecting findings) ─────────────────────────

_TRANSITIONS = [
    "Furthermore,", "Moreover,", "Additionally,", "In addition,",
    "Beyond this,", "Separately,", "Of note,", "Critically,",
    "Importantly,", "Significantly,", "Notably,", "Concerningly,",
    "However,", "By contrast,", "On the other hand,", "Nevertheless,",
    "Despite this,", "Even so,", "Still,", "Yet,",
    "Consequently,", "As a result,", "Therefore,", "Thus,",
    "Accordingly,", "For this reason,", "It follows that,", "This means that",
    "In particular,", "Specifically,", "Most tellingly,", "Most strikingly,",
]

# ── Evidence phrases (for introducing named evidence) ────────────────────

_EVIDENCE_PHRASES = [
    "The evidence is specific and verifiable:",
    "Named repositories include:",
    "Confirmed replicating repositories:",
    "The following third-party repositories were found to contain gold patch text:",
    "Specific examples include:",
    "Among the confirmed replicators:",
    "The replication evidence includes:",
    "Verifiable evidence points to:",
    "The audit identified the following replicating repositories:",
    "Click any link to inspect the stored evidence:",
    "Each finding below is backed by a stored verification URL when exact provenance was captured:",
    "The evidence chain is as follows:",
    "Documented replication in:",
    "Concrete instances of replication include:",
    "The following repositories independently contain the gold patch:",
]

# ── Temporal phrases (for merge_date / cutoff analysis) ──────────────────

_TEMPORAL_PHRASES = [
    "merged before the training-data cutoff",
    "merged after the training-data cutoff",
    "predating the training cutoff",
    "postdating the training cutoff",
    "eligible for inclusion in the training corpus",
    "cannot be in that specific training corpus",
    "was publicly available at training time",
    "was not yet public at training time",
    "falls within the training-data window",
    "falls outside the training-data window",
    "temporally aligned with the training corpus",
    "temporally misaligned with the training corpus",
    "the fix existed publicly before the model was trained",
    "the fix did not exist publicly when the model was trained",
    "the merge date confirms training-data eligibility",
    "the merge date rules out training-data inclusion",
]

# ── Verdict phrases (for risk levels) ────────────────────────────────────

_VERDICT_PHRASES = {
    "critical": [
        "presents severe contamination risk",
        "is critically contaminated",
        "shows maximum contamination exposure",
        "represents the highest contamination tier",
        "has extensive evidence of data leakage",
        "is maximally exposed to training-data contamination",
    ],
    "high": [
        "presents significant contamination risk",
        "is highly contaminated",
        "shows substantial contamination exposure",
        "represents a high-risk tier",
        "has strong evidence of data leakage",
        "is substantially exposed to training-data contamination",
    ],
    "medium": [
        "presents moderate contamination risk",
        "is moderately contaminated",
        "shows partial contamination exposure",
        "represents a medium-risk tier",
        "has some evidence of data leakage",
        "is partially exposed to training-data contamination",
    ],
    "low": [
        "presents low contamination risk",
        "is minimally contaminated",
        "shows limited contamination exposure",
        "represents a low-risk tier",
        "has marginal evidence of data leakage",
        "is slightly exposed to training-data contamination",
    ],
    "clean": [
        "shows no contamination",
        "is clean",
        "has no detectable contamination",
        "represents the clean tier",
        "has no evidence of public-source leakage",
        "has no detected training-corpus exposure signal",
    ],
}

# ── Action phrases (for recommendations) ─────────────────────────────────

_ACTION_PHRASES = [
    "We recommend",
    "The recommended action is",
    "Organizations should",
    "Benchmark users should",
    "Model evaluators should",
    "Researchers should",
    "It is advisable to",
    "Best practice dictates",
    "The following steps are recommended:",
    "Action items:",
    "Immediate steps:",
    "For accurate evaluation,",
    "To mitigate contamination risk,",
    "Before using this benchmark,",
    "Prior to model evaluation,",
]

# ── Shock amplifiers (for high-impact findings) ──────────────────────────

_SHOCK_AMPLIFIERS = [
    "This is a systemic problem, not an isolated incident.",
    "The scale of replication is unprecedented.",
    "These findings call into question the validity of benchmark scores.",
    "The contamination is widespread and growing.",
    "No instance in this benchmark is immune to public exposure.",
    "The replication rate exceeds any reasonable threshold for contamination.",
    "This level of public availability fundamentally undermines benchmark integrity.",
    "The evidence is not marginal — it is overwhelming.",
    "These are not edge cases; they are the majority.",
    "The benchmark's integrity is compromised at a structural level.",
    "This is not a theoretical concern — the data is publicly accessible right now.",
    "The replication pattern suggests active dissemination, not passive availability.",
]

# ── Hedging phrases (for limitations) ────────────────────────────────────

_HEDGING_PHRASES = [
    "may not reflect",
    "could underestimate",
    "is likely conservative",
    "represents a lower bound",
    "does not account for",
    "cannot rule out",
    "it is possible that",
    "the evidence suggests, but does not prove,",
    "while indicative,",
    "should be interpreted with caution",
    "the findings are preliminary",
    "further investigation is needed",
    "the exact magnitude is uncertain",
    "the true contamination rate may be higher",
    "these results are necessary but not sufficient",
]


# ═══════════════════════════════════════════════════════════════════════════
# TEMPLATE VARIATION GENERATION
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ReportVariation:
    """A single generated report variation with a self-evaluation score."""
    finding_sentence: str
    scope_paragraph: str
    findings: list
    limitations: list
    recommendations: list
    shock_score: int = 0      # 1-10: does it trigger surprise?
    specificity_score: int = 0  # 1-10: are there named specifics?
    credibility_score: int = 0  # 1-10: does it feel trustworthy?
    clarity_score: int = 0     # 1-10: is it clear?
    action_score: int = 0      # 1-10: is it actionable?
    total_score: int = 0       # sum of above

    def calculate_scores(self, report: AuditReport):
        """Self-evaluate this variation on 5 dimensions.

        based on feedback: shock is the primary metric (we optimize for
        surprise+excitement), jargon is penalized, "first systematic audit"
        gets a shock bonus.
        """
        s = report.summary
        total = s.total_instances or 1
        ind_count = sum(1 for ir in report.instance_risks if ir.independent_hits > 0)

        fs = self.finding_sentence

        # SHOCK: does it trigger surprise?
        has_number = bool(re.search(r'\d+', fs))
        has_named_repo = "/" in fs and "." in fs
        has_counterfactual = "clean" in fs.lower() or "0%" in fs
        has_contrast = any(w in fs.lower() for w in ["but", "however", "by contrast", "while", "yet"])
        has_first = "first" in fs.lower()  # supported only if wording explicitly says first
        has_question = "?" in fs  # question-as-hook (Turing style)
        has_zero = "zero" in fs.lower()  # "zero replications" = LLM-eval-confirmed power word
        has_evidence = "evidence" in fs.lower()  # "Evidence from..." = high credibility+shock
        # PENALIZE jargon
        has_jargon = "independent-source replication" in fs.lower()
        self.shock_score = sum([
            has_number * 2,
            has_named_repo * 3,
            has_counterfactual * 2,
            has_contrast * 2,
            has_first * 3,      # "first" = big shock bonus
            has_question * 2,   # question = curiosity
            has_zero * 4,       # "zero" = LLM-confirmed 9/10 shock word
            has_evidence * 2,   # "Evidence from" = authority + shock
            min(len(fs) // 50, 3),
            -3 if has_jargon else 0,  # jargon penalty
        ])
        self.shock_score = max(0, min(10, self.shock_score))

        # SPECIFICITY: named repos in findings?
        named_count = sum(1 for f in self.findings if "/" in f.text)
        self.specificity_score = min(named_count * 3 + (2 if ind_count > 0 else 0), 10)

        # CREDIBILITY: limitations + calibrated language + hedging
        has_limitations = len(self.limitations) >= 3
        has_calibrated = any(c in " ".join(f.text for f in self.findings) for c in ["confidence", "likely", "evidence"])
        has_hedging = any(w in " ".join(f.text for f in self.findings).lower() for w in ["may", "might", "could", "appears", "suggests"])
        self.credibility_score = min(sum([has_limitations * 3, has_calibrated * 3, has_hedging * 2, min(len(self.limitations), 5)]), 10)

        # CLARITY: sentence length + no jargon
        avg_len = len(fs) / max(fs.count("."), 1)
        clarity_base = 8 if 50 < avg_len < 250 else 5
        if has_jargon:
            clarity_base -= 3  # jargon hurts clarity
        self.clarity_score = max(1, min(10, clarity_base))

        # ACTION: recommendations with commands?
        has_commands = any("agentseal" in r.lower() or "filter" in r.lower() or "run" in r.lower() for r in self.recommendations)
        self.action_score = min(len(self.recommendations) * 2 + (3 if has_commands else 0), 10)

        self.total_score = self.shock_score + self.specificity_score + self.credibility_score + self.clarity_score + self.action_score


def _generate_variations(report: AuditReport, num_variations: int = 4) -> list[ReportVariation]:
    """Generate multiple report variations and score them.

    Same data always produces the same variations in the same order
    (deterministic). The highest-scoring variation is selected.
    """
    s = report.summary
    total = s.total_instances or 1
    benchmark = _esc(report.config.benchmark) if report.config else "unknown"

    # Guard: empty instance_risks — return an empty list; callers have their own
    # human-readable fallback. The previous implementation returned a string here,
    # causing select_best_variation() to crash if this helper was called directly.
    if not report.instance_risks:
        return []

    ind_instances = [ir for ir in report.instance_risks if ir.independent_hits > 0]
    ind_count = len(ind_instances)
    link_ok_ids = _independent_clickable_instance_ids(report)
    link_ok_count = len(link_ok_ids)
    named_repos = _collect_named_repos(report)
    pro_audit = _is_pro_audit(report)
    solution_audit = _is_solution_audit(report)

    variations = []

    # Generate `num_variations` variations using different vocabulary selections
    for v in range(num_variations):
        # Deterministic vocabulary selection — each variation uses a different
        # offset into the vocabulary banks
        opener = _FINDING_OPENERS[(v * 5) % len(_FINDING_OPENERS)].format(total=total, benchmark=benchmark)
        rep_phrase = _REPLICATION_PHRASES[(v * 3) % len(_REPLICATION_PHRASES)]
        no_rep = _NO_REPLICATION_PHRASES[(v * 2) % len(_NO_REPLICATION_PHRASES)]
        clean = _CLEAN_BASELINE_PHRASES[v % len(_CLEAN_BASELINE_PHRASES)]
        named_intro = _NAMED_REPO_INTRO[v % len(_NAMED_REPO_INTRO)]

        # Build the finding sentence. Public-source replication and corpus-level
        # signals are deliberately separated: CodeSeal/Bloom can justify
        # corpus-language; plain GitHub/HF evidence is public availability.
        codeseal_count, bloom_count, pre_cutoff_corpus_count, _total = _corpus_signal_summary(report)
        corpus_bits = []
        if codeseal_count:
            corpus_bits.append(f"CodeSeal matched {codeseal_count} ({_format_pct(codeseal_count, total)}) against the bundled corpus index")
        if bloom_count:
            corpus_bits.append(f"the Stack v2 Bloom filter marked {bloom_count} ({_format_pct(bloom_count, total)}) source repo(s) as probable corpus members")
        if ind_count == 0:
            if pro_audit:
                if s.instances_with_patch_exposure > 0:
                    base = "gold patch exposure was detected in public default branches"
                else:
                    base = "AgentSeal did not detect gold patch exposure in public default branches"
                finding = f"{opener} {base}; no third-party public-repo replication was detected."
                if corpus_bits:
                    finding += " Corpus-level signal still exists: " + "; ".join(corpus_bits) + "."
            elif corpus_bits:
                finding = f"{opener} No third-party public-repo replication was detected, but " + "; ".join(corpus_bits) + "."
            elif solution_audit:
                finding = (
                    f"{opener} AgentSeal did not detect independent-source or bundled-corpus "
                    f"evidence for the standalone solution code in this audit run."
                )
            else:
                finding = f"{opener} Gold patch solutions are publicly available in their source GitHub repositories, but {no_rep}."
        else:
            ind_rate = (ind_count / total * 100)
            avg_repos = sum(ir.independent_hits for ir in ind_instances) / ind_count
            named_str = ""
            if named_repos:
                top = sorted(named_repos)[:3]
                named_str = " " + named_intro.format(repos=_format_repo_list(top))
            # Include temporal-adjusted rate to prevent misleading corpus claims
            # when some replication occurred after the selected model cutoff.
            cutoff = getattr(report.config, 'model_cutoff', '2024-03-15')
            pre_cutoff_ind = sum(1 for ir in ind_instances if ir.merge_date and ir.merge_date < cutoff)
            post_cutoff_ind = sum(1 for ir in ind_instances if ir.merge_date and ir.merge_date >= cutoff)
            if pre_cutoff_ind > 0 and pre_cutoff_ind < ind_count:
                pre_rate = (pre_cutoff_ind / total * 100)
                if link_ok_count < ind_count:
                    finding = (f"{opener} AgentSeal recorded independent-source matches for {ind_count} "
                               f"({ind_rate:.0f}%) instance(s), but strict report preflight confirmed clickable "
                               f"GitHub evidence URLs for {link_ok_count}. Unconfirmed URLs are not linked. "
                               f"Of the recorded matches, {pre_cutoff_ind} ({pre_rate:.0f}%) were merged before "
                               f"the training-data cutoff and {post_cutoff_ind} after. {clean}")
                else:
                    finding = (f"{opener} {ind_count} ({ind_rate:.0f}%) had their gold patch replicated in "
                               f"third-party public GitHub repositories at an average of {avg_repos:.0f} repos "
                               f"per affected instance{named_str}. Of these, {pre_cutoff_ind} ({pre_rate:.0f}%) "
                               f"were merged before the training-data cutoff and {post_cutoff_ind} after. "
                               f"The pre-cutoff rate ({pre_rate:.0f}%) is the corpus-eligible contamination signal. {clean}")
            else:
                if link_ok_count < ind_count:
                    finding = (f"{opener} AgentSeal recorded independent-source matches for {ind_count} "
                               f"({ind_rate:.0f}%) instance(s), but strict report preflight confirmed clickable "
                               f"GitHub evidence URLs for {link_ok_count}. Unconfirmed URLs are not linked. {clean}")
                else:
                    finding = (f"{opener} {ind_count} ({ind_rate:.0f}%) had their gold patch replicated in "
                               f"third-party public GitHub repositories at an average of {avg_repos:.0f} repos "
                               f"per affected instance{named_str}. {clean}")
            if corpus_bits:
                finding += " Corpus evidence also exists: " + "; ".join(corpus_bits) + "."

        # Generate scope, findings, limitations, recommendations
        scope = _generate_scope_paragraph(report)
        findings = _determine_findings(report)
        limitations = _generate_limitations(report)
        recommendations = _generate_recommendations(report)

        var = ReportVariation(
            finding_sentence=finding,
            scope_paragraph=scope,
            findings=findings,
            limitations=limitations,
            recommendations=recommendations,
        )
        var.calculate_scores(report)
        variations.append(var)

    return variations


def select_best_variation(report: AuditReport) -> ReportVariation:
    """Generate variations and return the highest-scoring one.

    because all variations scored identically. Now uses a data-hash-seeded
    offset to ensure different data picks different winners. The selection
    is still deterministic (same data = same winner) but diverse across
    different datasets.
    """
    variations = _generate_variations(report, num_variations=4)
    if not variations:
        s = report.summary
        total = s.total_instances or 0
        benchmark = _esc(report.config.benchmark) if report.config else "unknown"
        return ReportVariation(
            finding_sentence=f"Evidence from {total} {benchmark} instances indicates that the audit produced no results. The benchmark may be empty or all instances failed to load.",
            scope_paragraph="", findings=[], limitations=[], recommendations=[]
        )
    # use data hash to seed variation selection
    # This prevents V1 from always winning when scores are tied.
    import hashlib
    s = report.summary
    data_hash = int(hashlib.md5(f"{s.total_instances}{s.critical_count}{s.contamination_rate}".encode()).hexdigest(), 16)
    # Sort by total_score, then shock, then "zero" bonus, then "evidence" bonus
    # On ties, use the data_hash to pick a different winner for different data
    # Create a list of (index, variation) tuples for stable sorting
    indexed = list(enumerate(variations))
    indexed.sort(key=lambda pair: (
        -pair[1].total_score,
        -pair[1].shock_score,
        -(4 if "zero" in pair[1].finding_sentence.lower() else 0),
        -(2 if "evidence" in pair[1].finding_sentence.lower() else 0),
        # Tiebreaker: data-hash-seeded rotation for diversity
        (pair[0] + data_hash) % 4,
    ))
    return indexed[0][1]


# ═══════════════════════════════════════════════════════════════════════════
# CONTENT DETERMINATION
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Finding:
    """A single finding to report."""
    confidence: str
    text: str
    evidence_count: int = 0
    section: str = "findings"




def _independent_clickable_instance_ids(report: AuditReport) -> set[str]:
    ids: set[str] = set()
    for ev in report.evidence:
        if 'independent' not in ev.match_type.value.lower() and 'issue' not in ev.match_type.value.lower():
            continue
        click_url, _status = clickable_evidence_url(
            getattr(ev, "source_url", ""),
            source_kind=getattr(ev, "source_kind", ""),
            verification_status=getattr(ev, "verification_status", ""),
        )
        if click_url:
            ids.add(ev.instance_id)
    return ids

def _determine_findings(report: AuditReport) -> list[Finding]:
    """Stage 1: Content Determination — decide what findings are worth reporting."""
    findings = []
    s = report.summary
    total = s.total_instances or 1
    pro_audit = _is_pro_audit(report)

    ind_instances = [ir for ir in report.instance_risks if ir.independent_hits > 0]
    ind_count = len(ind_instances)
    link_ok_count = len(_independent_clickable_instance_ids(report))
    ind_rate = (ind_count / total * 100) if total else 0

    # Finding 1: Independent-source replication rate
    if ind_count > 0:
        avg_repos = sum(ir.independent_hits for ir in ind_instances) / ind_count
        max_repos = max(ir.independent_hits for ir in ind_instances)
        confidence = _CONFIDENCE_HIGH[0] if ind_rate >= 50 else _CONFIDENCE_MEDIUM[0] if ind_rate >= 10 else _CONFIDENCE_LOW[0]
        if link_ok_count < ind_count:
            text = (f"AgentSeal recorded independent-source matches for {ind_count} of {total} "
                    f"instances ({ind_rate:.0f}%), but strict report preflight confirmed clickable "
                    f"GitHub evidence URLs for {link_ok_count}. Unconfirmed or placeholder URLs "
                    f"are deliberately not linked.")
        else:
            text = (f"{ind_count} of {total} instances ({ind_rate:.0f}%) have gold patch text "
                    f"replicated in independent public GitHub repositories, at an average of "
                    f"{avg_repos:.0f} repositories per affected instance and a maximum of {max_repos}.")
        findings.append(Finding(confidence=confidence, text=text, evidence_count=ind_count))

    # Finding 1b: corpus-level evidence from CodeSeal/Bloom/temporal alignment
    codeseal_count, bloom_count, pre_ind_count, _total = _corpus_signal_summary(report)
    if codeseal_count or bloom_count or pre_ind_count:
        parts = []
        if codeseal_count:
            parts.append(
                f"CodeSeal matched {codeseal_count} instance(s) against the bundled "
                f"content-overlap corpus index"
            )
        if bloom_count:
            parts.append(
                f"the Stack v2 Bloom filter marked {bloom_count} source repo(s) as "
                f"probable corpus members"
            )
        if pre_ind_count:
            cutoff = getattr(report.config, 'model_cutoff', '2024-03-15')
            parts.append(
                f"{pre_ind_count} independent-source replicated instance(s) predate "
                f"the selected training cutoff ({_esc(cutoff)})"
            )
        corpus_text = (
            "Corpus-level contamination evidence is present: " + "; ".join(parts) + ". "
            "CodeSeal is deterministic content overlap; Bloom membership is probabilistic; "
            "temporal alignment establishes eligibility, not behavioral memorization."
        )
        findings.append(Finding(
            confidence=_CONFIDENCE_HIGH[0] if codeseal_count or (bloom_count and pre_ind_count) else _CONFIDENCE_MEDIUM[0],
            text=corpus_text,
            evidence_count=codeseal_count + bloom_count + pre_ind_count,
        ))

    # Finding 2: Named repositories
    named_repos = _collect_named_repos(report)
    if len(named_repos) >= 2:
        top = sorted(named_repos)[:3]
        confidence = _CONFIDENCE_VERY_LIKELY[0] if len(named_repos) >= 5 else _CONFIDENCE_MEDIUM[1]
        repos_str = _format_repo_list(top)
        text = (f"The replicating repositories include {repos_str}. "
                f"These are verified public-source replication locations. Treat them "
                f"as corpus evidence only when paired with CodeSeal, Bloom-filter, "
                f"or pre-cutoff temporal alignment signals.")
        findings.append(Finding(confidence=confidence, text=text, evidence_count=len(named_repos)))

    # Finding 3: Temporal alignment
    temporal = _analyze_temporal_alignment(report)
    if temporal:
        findings.append(temporal)

    # Finding 4: baseline / exposure path acknowledgement
    solution_audit = _is_solution_audit(report)
    if pro_audit:
        findings.append(Finding(
            confidence=_CONFIDENCE_HIGH[0],
            text=(f"Default-branch gold patch exposure is ~{(s.patch_exposure_rate * 100):.0f}% "
                  f"under the Pro audit consensus path. This is not a source-PR circularity "
                  f"check; it asks whether the fix appears in public default branches after "
                  f"excluding base-commit content."),
            evidence_count=total,
        ))
    elif solution_audit:
        findings.append(Finding(
            confidence=_CONFIDENCE_HIGH[0],
            text=("This is a solution-mode audit: no source PR diff baseline is expected. "
                  "The relevant evidence channels are CodeSeal corpus overlap, Bloom corpus "
                  "membership where a real source repo exists, and independent-source replication."),
            evidence_count=total,
        ))
    else:
        findings.append(Finding(
            confidence=_CONFIDENCE_HIGH[0],
            text=(f"Patch-versus-source-PR overlap of ~{(s.patch_exposure_rate * 100):.0f}% is "
                  f"structural — {_esc(report.config.benchmark)} gold patches are derived from merged "
                  f"GitHub pull requests — and does not constitute independent evidence of "
                  f"contamination. This figure is included as a baseline, not a finding."),
            evidence_count=total,
        ))

    # Finding 5: Problem statement exposure
    if s.instances_with_problem_statement_exposure > 0:
        count = s.instances_with_problem_statement_exposure
        rate = (count / total * 100)
        findings.append(Finding(
            confidence=_CONFIDENCE_MEDIUM[0],
            text=(f"Problem statement text for {count} instances ({rate:.0f}%) appears in "
                  f"the source PR diff, indicating the issue text is publicly available "
                  f"alongside the solution."),
            evidence_count=count,
        ))

    # Finding 6: Test patch exposure
    if s.instances_with_test_patch_exposure > 0:
        count = s.instances_with_test_patch_exposure
        rate = (count / total * 100)
        findings.append(Finding(
            confidence=_CONFIDENCE_MEDIUM[0],
            text=(f"Hidden test case code for {count} instances ({rate:.0f}%) is present "
                  f"in the source PR diff, meaning the evaluation criteria are publicly "
                  f"accessible. A model could optimize for passing these specific tests "
                  f"rather than solving the underlying problem."),
            evidence_count=count,
        ))

    # Finding 7: Repo concentration
    repo_concentration = _analyze_repo_concentration(report)
    if repo_concentration:
        findings.append(repo_concentration)

    return findings


def _collect_named_repos(report: AuditReport) -> set[str]:
    named = set()
    for ev in report.evidence:
        if 'independent' in ev.match_type.value.lower():
            click_url, _status = clickable_evidence_url(
                getattr(ev, "source_url", ""),
                source_kind=getattr(ev, "source_kind", ""),
                verification_status=getattr(ev, "verification_status", ""),
            )
            if not click_url:
                continue
            for r in (ev.source_repo or "").split(","):
                r = r.strip()
                if r and "/" in r:
                    named.add(r)
            if len(named) >= 10:
                break
    return named


def _format_repo_list(repos: list[str]) -> str:
    if not repos:
        return ""
    safe = [_esc(r) for r in repos]
    if len(safe) == 1:
        return safe[0]
    if len(safe) == 2:
        return f"{safe[0]} and {safe[1]}"
    return f"{safe[0]}, {safe[1]}, and {safe[2]}"


def _analyze_temporal_alignment(report: AuditReport) -> Optional[Finding]:
    with_dates = [ir for ir in report.instance_risks if ir.merge_date and ir.merge_date != "—"]
    if not with_dates:
        return None
    cutoff = getattr(report.config, 'model_cutoff', '2024-03-15')
    pre = sum(1 for ir in with_dates if ir.merge_date < cutoff)
    post = sum(1 for ir in with_dates if ir.merge_date >= cutoff)
    if not (pre or post):
        return None
    text = (f"Of {len(with_dates)} instances with confirmed merge dates, {pre} were "
            f"merged before the training-data cutoff ({cutoff}) and {post} after. "
            f"Post-cutoff instances cannot be in that specific training corpus "
            f"despite being publicly available now.")
    return Finding(confidence=_CONFIDENCE_HIGH[0], text=text, evidence_count=len(with_dates))


def _analyze_repo_concentration(report: AuditReport) -> Optional[Finding]:
    repo_counts = {}
    for ir in report.instance_risks:
        if ir.independent_hits > 0:
            repo_counts[ir.repo] = repo_counts.get(ir.repo, 0) + 1
    if len(repo_counts) < 2:
        return None
    sorted_repos = sorted(repo_counts.items(), key=lambda x: -x[1])
    top_repo, top_count = sorted_repos[0]
    top_repo_safe = _esc(top_repo)
    total_repos = len(repo_counts)
    if total_repos <= 3:
        text = (f"Independent-source replication is concentrated in {total_repos} source "
                f"repositories, with {top_repo_safe} accounting for {top_count} instances.")
    else:
        text = (f"Independent-source replication spans {total_repos} source repositories. "
                f"The most affected is {top_repo_safe} with {top_count} instances showing "
                f"replication, suggesting that contamination is not uniform across the benchmark.")
    return Finding(confidence=_CONFIDENCE_MEDIUM[0], text=text, evidence_count=total_repos)


# ═══════════════════════════════════════════════════════════════════════════
# SENTENCE PLANNING + LINGUISTIC REALIZATION
# ═══════════════════════════════════════════════════════════════════════════

def _generate_finding_sentence(report: AuditReport) -> str:
    """Generate THE finding sentence using the best-scoring variation."""
    # Guard: empty instance_risks — don't call select_best_variation
    if not report.instance_risks:
        s = report.summary
        total = s.total_instances or 0
        benchmark = _esc(report.config.benchmark) if report.config else "unknown"
        return f"Evidence from {total} {benchmark} instances indicates that the audit produced no results. The benchmark may be empty or all instances failed to load."
    best = select_best_variation(report)
    return best.finding_sentence


def _generate_scope_paragraph(report: AuditReport) -> str:
    benchmark = report.config.benchmark
    cutoff = getattr(report.config, 'model_cutoff', '2024-03-15')
    model_name = getattr(report.config, 'model_name', 'stack-v2')
    if _is_pro_audit(report):
        baseline_text = (
            "For Pro audits, patch exposure means gold fix lines were detected in public "
            "default branches after base-commit exclusion and multi-method consensus. A 0% "
            "rate means the audited sample had no qualifying default-branch exposure."
        )
    elif _is_solution_audit(report):
        baseline_text = (
            "For solution-mode audits, there is no source PR diff baseline by design. "
            "AgentSeal evaluates standalone solution text with CodeSeal, corpus-membership, "
            "and independent-source replication signals."
        )
    else:
        baseline_text = (
            f"Patch-versus-source-PR overlap is structural for PR-derived benchmarks — "
            f"{_esc(benchmark)} gold patches are derived from merged GitHub pull requests — "
            f"and is reported below as a <em>structural baseline</em>, not an independent "
            f"finding."
        )
    return (
        f"This report measures data <strong>availability</strong>, corpus-level overlap, "
        f"and public-source replication; it does <strong>not</strong> prove behavioral "
        f"model memorization. {baseline_text} Independent-source replication means gold patch text was found in "
        f"repositories other than the source repo. CodeSeal hits indicate deterministic "
        f"content overlap against the bundled corpus index, and Stack v2 Bloom hits indicate "
        f"probable repository membership in that corpus. Temporal alignment against the "
        f"{_esc(model_name)} training-data cutoff ({_esc(cutoff)}) refines which "
        f"instances were eligible for that specific training corpus. "
        f"{_corpus_calibration_sentence(report)}"
    )


def _generate_instance_narrative(ir: InstanceRisk, evidence: list) -> str:
    """Generate a narrative paragraph for a single highly-contaminated instance."""
    parts = [f"<strong>{_esc(ir.instance_id)}</strong> ({_esc(ir.repo)})"]
    if ir.independent_hits > 0:
        _has_clickable = any(
            clickable_evidence_url(
                getattr(ev, "source_url", ""),
                source_kind=getattr(ev, "source_kind", ""),
                verification_status=getattr(ev, "verification_status", ""),
            )[0]
            for ev in evidence
            if 'independent' in ev.match_type.value.lower()
        )
        if _has_clickable:
            parts.append(f"has its gold patch replicated in <strong>{ir.independent_hits} independent "
                         f"repositories</strong>")
        else:
            parts.append(f"has <strong>{ir.independent_hits} recorded independent-source hit(s)</strong>, "
                         f"but the stored external evidence URL was not made clickable after strict preflight")
        named = []
        for ev in evidence:
            if 'independent' in ev.match_type.value.lower():
                for r in (ev.source_repo or "").split(","):
                    r = r.strip()
                    if r and "/" in r:
                        named.append(r)
                if named:
                    break
        if named:
            top = named[:3]
            if len(top) == 1:
                parts.append(f", including {_esc(top[0])}")
            elif len(top) == 2:
                parts.append(f", including {_esc(top[0])} and {_esc(top[1])}")
            else:
                parts.append(f", including {_esc(top[0])}, {_esc(top[1])}, and {_esc(top[2])}")
        parts.append(".")
        for ev in evidence:
            if 'independent' in ev.match_type.value.lower() and ev.source_url:
                # scheme before injecting into an href attribute. For
                # ISSUE_CONTAMINATION_HIT evidence, source_url is built from
                # problem_statement[:60] (attacker-controlled parquet text);
                # a `"` in it would break out of the href and allow stored XSS
                # in the HTML report. Only allow https://github.com/ URLs.
                _raw_url = ev.source_url
                _click_url, _url_status = clickable_evidence_url(
                    _raw_url,
                    source_kind=getattr(ev, "source_kind", ""),
                    verification_status=getattr(ev, "verification_status", ""),
                )
                if _click_url:
                    _safe_url = _esc(_click_url)
                    parts.append(f' <a href="{_safe_url}" target="_blank" rel="noopener noreferrer">[verify on GitHub ↗]</a>')
                else:
                    parts.append(f' <span title="Stored evidence URL was not linked: {_esc(_url_status)}">[evidence URL not linked]</span>')
                break
    if getattr(ir, "codeseal_matches", 0) > 0:
        parts.append(
            f" CodeSeal reports bundled-corpus content overlap "
            f"(similarity {getattr(ir, 'codeseal_similarity', 0.0):.2f}, "
            f"{getattr(ir, 'codeseal_matches', 0)} match(es))."
        )
    if getattr(ir, "repo_in_training_corpus", False):
        parts.append(" The source repository is marked as a probable Stack v2 corpus member by the Bloom filter.")
    if ir.merge_date and ir.merge_date != "—":
        cutoff = "2024-03-15"
        if ir.merge_date < cutoff:
            parts.append(f" The fix was merged on {ir.merge_date[:10]}, <em>before</em> the Stack v2 "
                         f"cutoff, confirming it was eligible for inclusion in that training corpus.")
        else:
            parts.append(f" The fix was merged on {ir.merge_date[:10]}, <em>after</em> the Stack v2 "
                         f"cutoff, meaning it cannot be in that specific corpus despite being "
                         f"publicly available now.")
    if ir.ngram_overlap_8 > 0:
        parts.append(f" 8-gram overlap with the source PR diff: {ir.ngram_overlap_8*100:.0f}%.")
    text = " ".join(parts)
    return re.sub(r"\s+([,.;:])", r"\1", text)


def _generate_repo_narrative(repo_name: str, instances: list[InstanceRisk]) -> str:
    total = len(instances)
    contaminated = sum(1 for ir in instances if ir.independent_hits > 0 or ir.risk.value != "clean")
    ind_count = sum(1 for ir in instances if ir.independent_hits > 0)
    codeseal_count = sum(1 for ir in instances if getattr(ir, "codeseal_matches", 0) > 0)
    corpus_count = sum(1 for ir in instances if getattr(ir, "repo_in_training_corpus", False))
    rate = (contaminated / total * 100) if total else 0
    parts = [f"<strong>{_esc(repo_name)}</strong>: "]
    if ind_count > 0:
        avg_repos = sum(ir.independent_hits for ir in instances if ir.independent_hits > 0) / ind_count
        parts.append(f"{ind_count} of {total} instances show independent-source replication "
                     f"(average {avg_repos:.0f} independent repos per affected instance). ")
    else:
        parts.append(f"{total} instances audited. ")
    if codeseal_count > 0:
        parts.append(f"CodeSeal corpus-overlap signal appears in {codeseal_count} instance(s). ")
    if corpus_count > 0:
        parts.append(f"Bloom corpus-membership signal appears for {corpus_count} instance(s). ")
    if rate >= 80:
        parts.append(f"The vast majority of solutions are publicly available in the source repository.")
    elif rate >= 50:
        parts.append(f"More than half of solutions are publicly available.")
    elif rate > 0:
        parts.append(f"Some contamination detected.")
    else:
        parts.append(f"No contamination detected — the repo may have restructured, making files unfindable.")
    return "".join(parts)


def _generate_limitations(report: AuditReport) -> list[str]:
    limitations = []
    s = report.summary
    total = s.total_instances or 1

    instance_ids = [ir.instance_id for ir in report.instance_risks]
    unique_ids = set(instance_ids)
    if len(instance_ids) != len(unique_ids):
        dup_count = len(instance_ids) - len(unique_ids)
        limitations.append(
            f"<strong>{dup_count} duplicate instance(s) detected.</strong> The benchmark contains "
            f"instances with identical IDs. These are double-counted in all metrics, inflating "
            f"the contamination rate. Deduplicate the benchmark before trusting these numbers."
        )
    unique_repos = set(ir.repo for ir in report.instance_risks)
    if len(unique_repos) == 1 and total > 1:
        limitations.append(
            "<strong>All instances are from a single repository.</strong> The contamination rate "
            "may not generalize to benchmarks with diverse source repositories. A single repo's "
            "contamination profile is not representative of the broader benchmark ecosystem."
        )

    limitations.append(
        "<strong>Memorization.</strong> This report does not prove any model memorized any "
        "instance. Availability is necessary but not sufficient for memorization. Behavioral "
        "elicitation (running the model with minimal context to see if it can reproduce the "
        "patch) is required for that claim. AgentSeal identifies WHERE to look; the model "
        "test confirms WHETHER memorization occurred."
    )
    limitations.append(
        "<strong>Training-corpus evidence is signal-specific.</strong> CodeSeal matches are "
        "deterministic content-overlap hits against the bundled corpus index. Stack v2 Bloom "
        "matches are probabilistic repository-membership checks and can have false positives. "
        "Independent GitHub/HF replication is public-source evidence; it becomes corpus-level "
        "evidence only when paired with CodeSeal, Bloom membership, or pre-cutoff temporal "
        "alignment for the selected model/corpus."
    )
    not_eval = getattr(s, 'instances_not_evaluated', 0) or 0
    if not_eval > 0:
        limitations.append(
            f"<strong>{not_eval} instances could not be evaluated</strong> (PR diff unavailable: "
            f"deleted, private, or rate-limited). These are counted in the total but excluded "
            f"from exposure calculations. If the PR was deleted AFTER the benchmark was created, "
            f"the patch was publicly available at training time but AgentSeal cannot detect it now."
        )
    with_dates = sum(1 for ir in report.instance_risks if ir.merge_date and ir.merge_date != "—")
    if with_dates < total:
        missing = total - with_dates
        limitations.append(
            f"<strong>Merge dates missing for {missing} instances.</strong> Temporal alignment "
            f"could not be performed for these. They are treated as 'unknown' rather than "
            f"'pre-cutoff' or 'post-cutoff'."
        )
    limitations.append(
        "<strong>Independent-search counts are bounded.</strong> AgentSeal processes the "
        "configured max_results GitHub code-search files per instance (default 30; GitHub "
        "page max 100). If a patch is replicated more broadly, independent_hits is an "
        "undercount. The evidence URLs remain exact for verified processed hits."
    )
    limitations.append(
        "<strong>Deleted PRs are invisible.</strong> If a PR was deleted after the benchmark "
        "was created, AgentSeal cannot fetch its diff. GH Archive data (not currently used) "
        "would be needed to detect this. This is a known blind spot."
    )
    limitations.append(
        "<strong>GitHub code search has a 24-48 hour indexing lag.</strong> Patches merged "
        "today will not appear in search results until tomorrow. Re-auditing after 48 hours "
        "is recommended for fresh patches."
    )
    limitations.append(
        "<strong>Independent repos are not automatically training data.</strong> Finding a patch "
        "in OpenDevin/OpenDevin, SWE-agent/SWE-agent, or another harness means the code is "
        "publicly replicated there. It should be called training-corpus evidence only when "
        "CodeSeal, Bloom membership, or temporal corpus alignment supports that stronger claim."
    )
    limitations.append(
        "<strong>Independent search is exact-verified after retrieval.</strong> GitHub search "
        "candidates are not counted as independent_hits until AgentSeal fetches the file and "
        "checks changed patch lines. Coincidental matches can still occur for very generic code, "
        "so common-token blocklists and line verification are used to reduce false positives."
    )
    limitations.append(
        "<strong>Reformatted patches are invisible.</strong> If a patch was reformatted (whitespace "
        "changes, variable renaming) before being committed to a third-party repo, the 8-gram "
        "search will miss it. The search matches exact identifier sequences, not semantic equivalence."
    )
    return limitations


def _generate_recommendations(report: AuditReport) -> list[str]:
    s = report.summary
    total = s.total_instances or 1
    ind_instances = [ir for ir in report.instance_risks if ir.independent_hits > 0]
    ind_count = len(ind_instances)
    recs = []
    if ind_count > 0:
        recs.append(
            f"<strong>Filter before evaluation.</strong> Remove or separately report the {ind_count} instances with "
            f"independent-source replication before using {_esc(report.config.benchmark)} for model "
            f"evaluation. The filter list is available in the JSON report as "
            f"<code>instance_risks</code> rows where <code>independent_hits &gt; 0</code>."
        )
    recs.append(
        "<strong>Re-audit monthly.</strong> GitHub code search indexes new content continuously. "
        "Patches not replicated today may be replicated next month. Set up a monthly cron job: "
        "<code>0 0 1 * * agentseal audit --out monthly_$(date +%Y%m).json</code>"
    )
    recs.append(
        "<strong>Test memorization on high-replication instances.</strong> For the top 10 "
        "instances by independent_hits, run behavioral elicitation: give the model only the "
        "task ID and minimal context, then check if it can reproduce the gold patch. This "
        "is the test AgentSeal cannot perform — it requires model API access."
    )
    cutoff = getattr(report.config, 'model_cutoff', '2024-03-15')
    codeseal_instances = [ir for ir in report.instance_risks if getattr(ir, "codeseal_matches", 0) > 0]
    if codeseal_instances:
        recs.append(
            f"<strong>Prioritize CodeSeal-positive cases.</strong> {len(codeseal_instances)} "
            f"instance(s) matched the bundled CodeSeal corpus-overlap index. Review these before "
            f"plain public-source hits because they carry direct corpus-level signal."
        )

    if any(ir.merge_date and ir.merge_date >= cutoff for ir in report.instance_risks):
        recs.append(
            f"<strong>Account for temporal alignment.</strong> Some instances have merge dates "
            f"after the training-data cutoff ({cutoff}). These cannot be in that specific "
            f"training corpus. When reporting model scores, separate pre-cutoff and post-cutoff "
            f"instances to avoid conflating memorization with generalization."
        )
    return recs


__all__ = [
    "Finding", "ReportVariation",
    "_determine_findings", "_generate_finding_sentence", "_generate_scope_paragraph",
    "_generate_instance_narrative", "_generate_repo_narrative",
    "_generate_limitations", "_generate_recommendations",
    "_generate_variations", "select_best_variation",
]
