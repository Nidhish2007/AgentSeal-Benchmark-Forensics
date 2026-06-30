"""Pydantic schemas for AgentSeal audit data."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class MatchType(str, Enum):
    """How a benchmark item was found in a training-data source."""

    PATCH_VERBATIM = "patch_verbatim"          # gold patch lines appear verbatim in source
    PATCH_NORMALIZED = "patch_normalized"      # patch matches after whitespace normalization
    PATCH_NEAR_DUPLICATE = "patch_near_duplicate"  # patch near-duplicate (Jaccard >= threshold)
    PROBLEM_STATEMENT_VERBATIM = "problem_statement_verbatim"
    PROBLEM_STATEMENT_NEAR_DUPLICATE = "problem_statement_near_duplicate"
    TEST_PATCH_VERBATIM = "test_patch_verbatim"
    TEST_PATCH_NEAR_DUPLICATE = "test_patch_near_duplicate"
    REPO_IN_TRAINING_CORPUS = "repo_in_training_corpus"  # source repo is in known training data
    # repo OTHER than the source repo via GitHub code search. This is the
    INDEPENDENT_SOURCE_HIT = "independent_source_hit"
    # issues filed in OTHER repos (duplicate bug reports, etc.)
    ISSUE_CONTAMINATION_HIT = "issue_contamination_hit"
    # match — patch is highly similar to the source after normalization that
    # defeats n-gram (whitespace/casing/identifier renames). Catches the
    # contamination pattern the "Rethinking Benchmark and Contamination"
    # (2024) study showed n-gram cannot detect.
    EMBEDDING_SIMILARITY_HIT = "embedding_similarity_hit"
    CODESEAL_CONTENT_HIT = "codeseal_content_hit"


class RiskLevel(str, Enum):
    """Risk that a benchmark instance is contaminated."""

    CLEAN = "clean"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @classmethod
    def severity(cls, level: "RiskLevel") -> int:
        order = {cls.CLEAN: 0, cls.LOW: 1, cls.MEDIUM: 2, cls.HIGH: 3, cls.CRITICAL: 4}
        return order[level]


class BenchmarkInstance(BaseModel):
    """A single benchmark instance (e.g. one SWE-bench task)."""

    instance_id: str = Field(min_length=1)  # reject empty instance_id
    repo: str
    base_commit: str = ""
    patch: str = ""                      # the gold patch (the answer)
    test_patch: str = ""                 # the test cases
    problem_statement: str = ""          # the issue text
    created_at: Optional[str] = None
    pr_number: Optional[int] = None      # GitHub PR number, if derivable from instance_id
    pr_url: Optional[str] = None         # full GitHub PR URL


class ContaminationEvidence(BaseModel):
    """Evidence that one benchmark instance is contaminated."""

    instance_id: str
    match_type: MatchType
    source_url: str = ""                 # where the match was found (e.g. GitHub PR URL)
    source_repo: str = ""
    similarity: float = Field(ge=0.0, le=1.0)
    matched_lines: int = 0               # how many lines matched
    total_lines: int = 0                 # total lines in the benchmark item
    evidence_snippet: str = ""
    message: str = ""
    source_kind: str = "unknown"         # e.g. source_pr_diff, package_artifact, github_code_search_verified
    verification_status: str = "unknown" # e.g. exact_changed_lines, normalized, candidate_only
    vendor_like: bool = False            # true when evidence appears to be a dependency/source copy
    scope_note: str = ""                 # calibrated caveat for reports


class InstanceRisk(BaseModel):
    """Aggregated risk verdict for one benchmark instance.

    independent_hits, merge_date, temporal_aligned, and circular_match
    fields to support the 6 deterministic improvements.
    """

    instance_id: str
    repo: str
    risk: RiskLevel
    patch_exposure: float = Field(default=0.0, ge=0.0, le=1.0)
    problem_statement_exposure: float = Field(default=0.0, ge=0.0, le=1.0)
    test_patch_exposure: float = Field(default=0.0, ge=0.0, le=1.0)
    repo_in_training_corpus: bool = False
    pr_url: Optional[str] = None
    evidence_count: int = 0
    top_match_type: Optional[MatchType] = None
    snippet: str = ""
    # True when the source PR diff/file could not be fetched/evaluated.
    # This remains true even if independent GitHub/CodeSeal/Bloom evidence is
    # found later, so summary.not_evaluated is not erased by other evidence.
    not_evaluated: bool = False
    ngram_overlap_8: float = Field(default=0.0, ge=0.0, le=1.0)
    # (char-3-gram cosine). Catches reformatted contamination that n-gram
    # misses. See compute_embedding_similarity in similarity.py.
    embedding_similarity: float = Field(default=0.0, ge=0.0, le=1.0)
    independent_hits: int = 0          # repos OTHER than source containing the patch
    independent_candidate_hits: int = 0
    vendor_like_hits: int = 0
    non_vendor_hits: int = 0
    circular_match: bool = False       # True if patch was compared to its own PR diff
    codeseal_similarity: float = Field(default=0.0, ge=0.0, le=1.0)
    codeseal_matches: int = 0
    merge_date: Optional[str] = None   # PR merged_at timestamp
    temporal_aligned: bool = False     # True if merge_date predates training cutoff


class AuditSummary(BaseModel):
    """Aggregate counts for the whole audit."""

    total_instances: int = 0
    instances_with_patch_exposure: int = 0
    instances_with_problem_statement_exposure: int = 0
    instances_with_test_patch_exposure: int = 0
    instances_with_repo_in_corpus: int = 0
    patch_exposure_rate: float = 0.0
    problem_statement_exposure_rate: float = 0.0
    test_patch_exposure_rate: float = 0.0
    repo_in_corpus_rate: float = 0.0
    instances_with_codeseal_content: int = 0
    codeseal_content_rate: float = 0.0
    # These count in total_instances but should be surfaced separately so the
    # headline contamination_rate is not silently deflated by fetch failures.
    instances_not_evaluated: int = 0
    critical_count: int = 0
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0
    clean_count: int = 0
    contamination_rate: float = 0.0


class AuditConfig(BaseModel):
    """Configuration captured in the audit report."""

    benchmark: str = "swe-bench-verified"
    corpus_source: str = "github-pr-diffs"
    # accepted and caused spurious near-duplicate evidence for trivial word
    # overlap (Jaccard ~0 between prose and code). Jaccard values below 0.1
    # are statistically meaningless for contamination detection.
    threshold: float = Field(default=0.82, ge=0.1, le=1.0)
    sample_size: int = 0                  # 0 = all instances
    created_at: datetime = Field(default_factory=datetime.utcnow)
    # __init__.py 0.3.0, pyproject 0.4.0, VERSION_MARKER 0.5.0, CLI banner 0.1.0).
    agentseal_version: str = "5.0.0"
    # model_cutoff, the patch cannot be in that model's training corpus.
    # Defaults to Stack v2 cutoff (2024-03-15). Override with --model or
    # --model-cutoff CLI flags.
    model_cutoff: str = "2024-03-15"
    model_name: str = "stack-v2"
    audit_type: str = "pr_diff"  # "pr_diff" for SWE-style PR audits, "solution" for standalone code benchmarks

    @field_validator("threshold")
    @classmethod
    def _threshold_sanity(cls, v: float) -> float:
        if v < 0.1:
            raise ValueError(
                f"threshold={v} is too low (must be >= 0.1). Near-duplicate "
                f"Jaccard detection below 0.1 produces spurious false positives "
                f"because prose-vs-code word overlap is structurally near zero."
            )
        return v


class AuditReport(BaseModel):
    """The complete output of an AgentSeal audit."""

    config: AuditConfig
    summary: AuditSummary
    instance_risks: list[InstanceRisk] = Field(default_factory=list)
    evidence: list[ContaminationEvidence] = Field(default_factory=list)
    top_contaminated: list[InstanceRisk] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    remediation: list[str] = Field(default_factory=list)
