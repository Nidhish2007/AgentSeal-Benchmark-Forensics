"""Risk scoring — assign per-instance risk based on exposure evidence.

Bloom filter that checks membership against the packaged/user Stack v2 Bloom artifact.
The legacy 13-repo set is kept as a fallback if the Bloom filter module
is unavailable (shouldn't happen, but defense-in-depth).
"""

from __future__ import annotations

from .schemas import (
    AuditSummary,
    InstanceRisk,
    MatchType,
    RiskLevel,
)


def _date_gte(a: str, b: str) -> bool:
    """Return True if ISO date `a` >= `b` using parsed datetimes."""
    from datetime import datetime, timezone

    def _parse(s):
        if not s:
            return None
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            try:
                return datetime.fromisoformat(str(s)[:10])
            except (ValueError, TypeError):
                return None

    da, db = _parse(a), _parse(b)
    if da is None or db is None:
        return False
    # Normalize to naive UTC so offset-aware and offset-naive dates compare.
    def _naive_utc(d):
        if d.tzinfo is not None:
            d = d.astimezone(timezone.utc).replace(tzinfo=None)
        return d
    return _naive_utc(da) >= _naive_utc(db)

# (shouldn't happen), fall back to the legacy 13-repo allowlist.
try:
    from .stack_v2_filter import repo_in_stack_v2 as _repo_in_stack_v2
    _USE_BLOOM_FILTER = True
except ImportError:
    _USE_BLOOM_FILTER = False


# Legacy 13-repo allowlist — used as fallback if Bloom filter is unavailable.
# Kept for backwards compatibility and as a safety net.
KNOWN_TRAINING_REPOS = {
    "django/django", "sympy/sympy", "sphinx-doc/sphinx",
    "matplotlib/matplotlib", "scikit-learn/scikit-learn",
    "astropy/astropy", "pydata/xarray", "pytest-dev/pytest",
    "pylint-dev/pylint", "psf/requests", "mwaskom/seaborn",
    "pallets/flask",
}


def repo_in_training_corpus(repo: str) -> bool:
    """Check if a repo is in the LLM training corpus.

    available, falling back to the legacy 13-repo allowlist.

    audit with 10 unique repos does 10 Bloom checks instead of 500.
    """
    if not repo:
        return False
    # EFFICIENCY 4: cache per-repo (same repo = same result)
    repo_lower = repo.strip().lower()
    if repo_lower in _REPO_CACHE:
        return _REPO_CACHE[repo_lower]
    if _USE_BLOOM_FILTER:
        result = _repo_in_stack_v2(repo)
    else:
        # original-case `repo`, but the cache key and the Bloom path use
        # lower-case. "DJANGO/DJANGO" would miss the allowlist (case-sensitive
        # set lookup) and cache False under "django/django", poisoning later
        # case-correct lookups. Use the lower-case form consistently.
        result = repo_lower in KNOWN_TRAINING_REPOS
    _REPO_CACHE[repo_lower] = result
    return result

_REPO_CACHE: dict[str, bool] = {}


def score_instance(
    instance_id: str,
    repo: str,
    patch_exposure_rate: float,
    problem_statement_exposure: float,
    test_patch_exposure: float,
    repo_in_corpus: bool,
    pr_url: str | None,
    evidence_count: int,
    top_match_type: MatchType | None,
    snippet: str,
    not_evaluated: bool = False,
    merge_date: str | None = None,
    model_cutoff: str | None = None,
) -> InstanceRisk:
    """Compute the risk verdict for one benchmark instance.

    Risk rules (highest priority wins):
    - CRITICAL: patch verbatim or near-verbatim in source AND repo in training corpus
    - HIGH:     patch >= 80% line overlap AND repo in training corpus
      (tiny, 13-entry) training-corpus allowlist is no longer silently downgraded
      to MEDIUM. The patch IS public on GitHub; corpus membership is a confidence
      booster, not a gate. Such instances now score HIGH ("fully public, corpus
      unconfirmed") instead of MEDIUM.
    - MEDIUM: patch >= 50% line overlap, OR problem_statement/test_patch verbatim
    - LOW: some exposure but below 50%
    - CLEAN: no exposure detected (or not_evaluated with no other evidence)

    (default: Stack v2 cutoff 2024-03-15), the patch CANNOT be in that training
    corpus. Risk is DOWNGRADED (never upgraded) by one level:
    - CRITICAL → HIGH (was in corpus by allowlist, but post-cutoff means it can't be)
    - HIGH → MEDIUM
    - MEDIUM → LOW
    Only applies when merge_date is confirmed (not None) and post-cutoff.
    """
    # Prevents crashes when malformed datasets pass inf/negative/out-of-range values
    try:
        patch_exposure_rate = max(0.0, min(1.0, float(patch_exposure_rate)))
        problem_statement_exposure = max(0.0, min(1.0, float(problem_statement_exposure)))
        test_patch_exposure = max(0.0, min(1.0, float(test_patch_exposure)))
    except (TypeError, ValueError):
        patch_exposure_rate = 0.0
        problem_statement_exposure = 0.0
        test_patch_exposure = 0.0

    max_exposure = max(patch_exposure_rate, problem_statement_exposure, test_patch_exposure)

    if patch_exposure_rate >= 0.95 and repo_in_corpus:
        risk = RiskLevel.CRITICAL
    elif patch_exposure_rate >= 0.95:
        risk = RiskLevel.HIGH          # fully public, corpus membership unconfirmed
    elif patch_exposure_rate >= 0.80 and repo_in_corpus:
        risk = RiskLevel.HIGH
    elif patch_exposure_rate >= 0.80:
        risk = RiskLevel.MEDIUM        # mostly public, corpus unconfirmed
    elif max_exposure >= 0.50:
        risk = RiskLevel.MEDIUM
    elif max_exposure > 0.0:
        risk = RiskLevel.LOW
    else:
        risk = RiskLevel.CLEAN

    # If merge_date is after the model cutoff, the patch cannot be in that
    # specific training corpus. Cap at HIGH (not CRITICAL).
    # comparing raw strings. String comparison breaks on malformed dates
    # (e.g. "unknown" >= "2024-03-15" silently does the wrong thing) and on
    # non-zero-padded months. On any parse failure, do NOT downgrade (we
    # can't prove the temporal premise).
    if merge_date and model_cutoff and _date_gte(merge_date, model_cutoff):
        if risk == RiskLevel.CRITICAL:
            risk = RiskLevel.HIGH
        elif risk == RiskLevel.HIGH:
            risk = RiskLevel.MEDIUM
        elif risk == RiskLevel.MEDIUM:
            risk = RiskLevel.LOW
        # LOW and CLEAN stay as-is

    return InstanceRisk(
        instance_id=instance_id,
        repo=repo,
        risk=risk,
        patch_exposure=patch_exposure_rate,
        problem_statement_exposure=problem_statement_exposure,
        test_patch_exposure=test_patch_exposure,
        repo_in_training_corpus=repo_in_corpus,
        pr_url=pr_url,
        evidence_count=evidence_count,
        top_match_type=top_match_type,
        snippet=snippet,
        not_evaluated=bool(not_evaluated),
    )


def build_summary(instance_risks: list[InstanceRisk]) -> AuditSummary:
    """Aggregate per-instance risks into a summary."""
    total = len(instance_risks)
    if total == 0:
        return AuditSummary()

    patch_exposed = sum(1 for r in instance_risks if r.patch_exposure > 0)
    ps_exposed = sum(1 for r in instance_risks if r.problem_statement_exposure > 0)
    tp_exposed = sum(1 for r in instance_risks if r.test_patch_exposure > 0)
    repo_in_corpus = sum(1 for r in instance_risks if r.repo_in_training_corpus)
    codeseal_content = sum(1 for r in instance_risks if r.codeseal_matches > 0)
    # unavailable). Detected via the canonical "(PR diff not available)" snippet
    # the engine emits when the fetcher returns None. Surfacing this separately
    # prevents the headline contamination_rate from being silently deflated by
    # fetch failures (404s, rate limits, deleted PRs).
    not_evaluated = sum(
        1 for r in instance_risks
        if getattr(r, "not_evaluated", False)
        or (r.snippet == "(PR diff not available)" and r.patch_exposure == 0.0)
    )

    counts = {level: 0 for level in RiskLevel}
    for r in instance_risks:
        counts[r.risk] += 1

    flagged = total - counts[RiskLevel.CLEAN]
    contamination_rate = flagged / total if total > 0 else 0.0

    return AuditSummary(
        total_instances=total,
        instances_with_patch_exposure=patch_exposed,
        instances_with_problem_statement_exposure=ps_exposed,
        instances_with_test_patch_exposure=tp_exposed,
        instances_with_repo_in_corpus=repo_in_corpus,
        patch_exposure_rate=patch_exposed / total,
        problem_statement_exposure_rate=ps_exposed / total,
        test_patch_exposure_rate=tp_exposed / total,
        repo_in_corpus_rate=repo_in_corpus / total,
        instances_with_codeseal_content=codeseal_content,
        codeseal_content_rate=codeseal_content / total,
        instances_not_evaluated=not_evaluated,
        critical_count=counts[RiskLevel.CRITICAL],
        high_count=counts[RiskLevel.HIGH],
        medium_count=counts[RiskLevel.MEDIUM],
        low_count=counts[RiskLevel.LOW],
        clean_count=counts[RiskLevel.CLEAN],
        contamination_rate=contamination_rate,
    )


def top_contaminated(instance_risks: list[InstanceRisk], k: int = 20) -> list[InstanceRisk]:
    """Return the k most-contaminated instances, sorted by severity then exposure."""
    return sorted(
        instance_risks,
        key=lambda r: (-RiskLevel.severity(r.risk), -r.patch_exposure, r.instance_id),
    )[:k]


def standard_limitations(benchmark_name: str = "", audit_type: str = "pr_diff") -> list[str]:
    """Limitations, adaptive to the benchmark being audited."""
    benchmark_lower = benchmark_name.lower()
    is_swebench = "swe" in benchmark_lower and "bench" in benchmark_lower
    is_solution = str(audit_type or "").lower() == "solution" or any(
        name in benchmark_lower for name in ("humaneval", "mbpp", "bigcodebench")
    )
    lims = [
        "AgentSeal uses GitHub PR diffs as a proxy for training data. The actual training data of closed models is unknown.",
        "AgentSeal does NOT prove that any specific model memorized any specific item. It shows that the gold answers are publicly available in sources that are standard LLM training data.",
        "The line-overlap metric is permissive — it counts any patch line that appears in the source diff. Verbatim substring match is stricter but may miss patches with minor formatting differences.",
        "Stack v2 Bloom membership is probabilistic. Treat Bloom-positive repos as corpus-membership signals, not proof; inspect get_filter_stats() for the active filter size and estimated false-positive rate.",
    ]
    if is_swebench:
        lims.append("Some SWE-bench instances may correspond to commits rather than PRs, in which case the PR diff fetch will return None and the instance will be marked as not-evaluated.")
    elif is_solution:
        lims.append("Solution-mode benchmarks do not have source PR diffs by design. AgentSeal reports source-PR exposure as 0 for that baseline and relies on CodeSeal, corpus-membership, and independent-source search signals.")
    else:
        lims.append("If the repos in this benchmark don't exist on GitHub (or are private), the PR diff fetch will return None and instances will be marked as not-evaluated. Ensure repo names are correct (e.g. 'django/django', not 'test-org/test-repo').")
    lims.append("Problem-statement near-duplicate detection uses word-shingle Jaccard between the issue prose and the PR code diff. Because prose and code share little vocabulary, this near-duplicate path rarely triggers; verbatim substring match is the primary reliable signal for problem-statement exposure.")
    lims.append("AgentSeal operates entirely on public data and makes only public-data claims.")
    return lims


def standard_remediation(benchmark_name: str = "") -> list[str]:
    """Remediation, adaptive to the benchmark being audited."""
    is_swebench = "swe" in benchmark_name.lower() and "bench" in benchmark_name.lower()
    recs = [
        f"For benchmark maintainers: regenerate {benchmark_name or 'the benchmark'} from private/future commits that cannot have been in any training data.",
    ]
    if is_swebench:
        recs.append("For model developers: stop reporting headline scores on benchmarks with unresolved high-risk contamination until the benchmark is regenerated or the contaminated split is excluded.")
        recs.append("For evaluators: do NOT assume that benchmarks marketed as 'contamination-resistant' are clean — audit them with AgentSeal first and publish the exact report artifact alongside any score.")
    else:
        recs.append("For model developers: if this benchmark is contaminated, stop reporting scores on it until the data is regenerated from non-public sources.")
        recs.append("For evaluators: audit any benchmark with AgentSeal before trusting its scores. Contamination is common in benchmarks sourced from public GitHub PRs.")
    recs.append("For the community: build and maintain a public contamination registry so model cards can cite a verifiable audit.")
    recs.append("Re-run AgentSeal periodically as new benchmarks are released and as training corpora evolve.")
    return recs
