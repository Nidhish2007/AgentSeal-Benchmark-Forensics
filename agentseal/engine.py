"""The AgentSeal audit engine.

For each benchmark instance, the engine:
1. Fetches the corresponding GitHub PR diff (the source the patch came from)
2. Computes patch exposure (line overlap, verbatim match, Jaccard)
3. Computes problem_statement exposure
4. Computes test_patch exposure
5. Checks if the source repo is in known training corpora
6. Assigns a risk level
7. Emits progress + evidence events for the UI
"""

from __future__ import annotations

import os
from typing import Callable, Optional

from .github_fetch import fetch_pr_diff
from .loaders import BenchmarkInstance
from .risk import (
    build_summary,
    score_instance,
    standard_limitations,
    standard_remediation,
    top_contaminated,
    repo_in_training_corpus,
)
from .schemas import (
    AuditConfig,
    AuditReport,
    ContaminationEvidence,
    InstanceRisk,
    MatchType,
    RiskLevel,
)
from .similarity import compute_patch_exposure, compute_text_exposure


ProgressCallback = Callable[[str, int, int, str], None]
EvidenceCallback = Callable[[str, RiskLevel, MatchType, float, str], None]
ReasoningCallback = Callable[[str, str], None]


def _date_gte(a: str, b: str) -> bool:
    """Return True if date `a` >= date `b`, parsing as ISO 8601.

    (`merge_date >= model_cutoff`), which is fragile — `"2024-4-15" > "2024-03-15"`
    is True by string compare but so is `"unknown" >= "2024-03-15"` (False
    actually, 'u' > '2'), and any non-ISO value gives a silently-wrong answer.
    Parse to datetime objects; on any parse failure return False (do NOT
    downgrade, since we can't prove the temporal premise).
    """
    from datetime import datetime, timezone
    def _parse(s):
        if not s:
            return None
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            # Fall back to the leading YYYY-MM-DD if present.
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




def _issue_search_enabled() -> bool:
    """Return whether optional GitHub issue-search is enabled.

    Issue search uses the same search API budget as code search. It is
    supplementary evidence, so it stays opt-in via AGENTSEAL_ISSUE_SEARCH=1.
    """
    return os.environ.get("AGENTSEAL_ISSUE_SEARCH", "0").strip().lower() in {"1", "true", "yes", "on"}


class AgentSealEngine:
    """Run a contamination audit on an agent benchmark.

    The engine is stateless between runs: configure it, call :meth:`run`,
    collect the report.
    """

    def __init__(
        self,
        instances: list[BenchmarkInstance],
        config: Optional[AuditConfig] = None,
        on_progress: Optional[ProgressCallback] = None,
        on_evidence: Optional[EvidenceCallback] = None,
        on_reasoning: Optional[ReasoningCallback] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        fetcher: Optional[Callable] = None,
        independent_search: bool = True,
        max_workers: int = 10,
        codeseal: bool = True,
    ) -> None:
        self.instances = list(instances or [])
        self.config = config or AuditConfig()
        self._on_progress = on_progress
        self._on_evidence = on_evidence
        self._on_reasoning = on_reasoning
        self._cancel_check = cancel_check
        # Allow injecting a custom fetcher for testing
        self._fetcher = fetcher or fetch_pr_diff
        # code search. Auto-enabled when GITHUB_TOKEN is set; the search
        # itself degrades gracefully (returns 0 hits) if the token is
        # absent. Users can force-disable via --no-independent-search.
        self._independent_search = independent_search
        self._codeseal = codeseal
        # bottleneck (network I/O), not the similarity computation. We
        # fetch all PR diffs in parallel, then process sequentially.
        self._max_workers = max_workers

    def run(self) -> AuditReport:
        """Execute the full audit and return a report.

        bottleneck (each PR diff is a separate HTTP request). We fetch all
        PR diffs in parallel using ThreadPoolExecutor, then process the
        results sequentially (similarity computation + independent search +
        scoring are fast or rate-limited by code-search, not by our CPU).
        """
        # Filter valid instances first
        valid_instances = [inst for inst in self.instances if inst and inst.instance_id]
        total = len(valid_instances)
        solution_mode = str(getattr(self.config, "audit_type", "pr_diff") or "pr_diff").lower() == "solution"
        self._emit_progress("loading benchmark", 0, total, f"{total} instances to audit")

        from concurrent.futures import ThreadPoolExecutor, as_completed
        pr_diffs: dict[str, Optional[str]] = {}  # instance_id → pr_diff (or None)

        def _fetch_one(inst):
            if not inst.pr_number:
                return (inst.instance_id, None)
            try:
                result = self._fetcher(inst.repo, inst.pr_number)
                if result is not None and not isinstance(result, str):
                    result = None
                return (inst.instance_id, result)
            except Exception:
                return (inst.instance_id, None)

        source_phase = "source baseline" if solution_mode else "fetching PR diffs"
        source_message = (
            "No source PR baseline expected for solution-mode audit"
            if solution_mode else
            f"Parallel fetch ({self._max_workers} workers)..."
        )
        self._emit_progress(source_phase, 0, total, source_message)
        # Guard: no valid instances — return empty report
        if total == 0:
            summary = build_summary([])
            report = AuditReport(
                config=self.config, summary=summary,
                instance_risks=[], evidence=[],
                limitations=standard_limitations(self.config.benchmark, getattr(self.config, "audit_type", "pr_diff")),
                remediation=standard_remediation(self.config.benchmark),
            )
            self._emit_progress("complete", 0, 0, "No instances to audit")
            return report
        def _prewarm_local_artifacts():
            # network PR fetching so the first audited instance does not pay
            # the full local-artifact startup cost on the foreground path.
            try:
                from .stack_v2_filter import get_filter_stats
                get_filter_stats()
            except Exception:
                pass
            if self._codeseal:
                try:
                    from .codeseal_detector import _load_bundled_model
                    _load_bundled_model()
                except Exception:
                    pass

        with ThreadPoolExecutor(max_workers=min(self._max_workers, max(total, 1)) + 1) as executor:
            executor.submit(_prewarm_local_artifacts)
            # None/empty instance_id), not self.instances — invalid instances
            # crash _fetch_one at inst.pr_number with AttributeError.
            futures = {executor.submit(_fetch_one, inst): inst for inst in valid_instances}
            fetched = 0
            for future in as_completed(futures):
                if self._cancel_check and self._cancel_check():
                    for f in futures:
                        f.cancel()
                    break
                inst_id, pr_diff = future.result()
                pr_diffs[inst_id] = pr_diff
                fetched += 1
                if fetched % 10 == 0 or fetched == total:
                    fetch_label = "checked" if solution_mode else "fetched"
                    self._emit_progress(source_phase, fetched, total, f"{fetched}/{total} {fetch_label}")

        # instance fetched PR metadata sequentially during scoring, creating one
        # extra blocking GitHub API round trip per instance.
        merge_dates: dict[str, Optional[str]] = {}
        if total > 0 and not solution_mode and os.environ.get("AGENTSEAL_FETCH_MERGE_DATES", "1").strip().lower() not in {"0", "false", "no", "off"}:
            def _fetch_merge_one(inst):
                if not inst.pr_number:
                    return inst.instance_id, None
                try:
                    from .github_fetch import fetch_pr_merge_date
                    return inst.instance_id, fetch_pr_merge_date(inst.repo, inst.pr_number)
                except Exception:
                    return inst.instance_id, None
            self._emit_progress("fetching PR metadata", 0, total, "Parallel merge-date fetch...")
            with ThreadPoolExecutor(max_workers=min(self._max_workers, max(total, 1))) as meta_executor:
                meta_futures = {meta_executor.submit(_fetch_merge_one, inst): inst for inst in valid_instances if inst.pr_number}
                meta_done = 0
                for future in as_completed(meta_futures):
                    if self._cancel_check and self._cancel_check():
                        for f in meta_futures:
                            f.cancel()
                        break
                    inst_id, merge_date = future.result()
                    merge_dates[inst_id] = merge_date
                    meta_done += 1
                    if meta_done % 10 == 0 or meta_done == len(meta_futures):
                        self._emit_progress("fetching PR metadata", meta_done, len(meta_futures), f"{meta_done}/{len(meta_futures)} merge dates")

        instance_risks: list[InstanceRisk] = []
        all_evidence: list[ContaminationEvidence] = []

        for i, inst in enumerate(valid_instances):
            # Check cancel flag
            if self._cancel_check and self._cancel_check():
                break

            self._emit_progress(
                "auditing",
                i,
                total,
                f"{inst.instance_id} ({inst.repo})",
            )

            # Reasoning: show what we're doing
            self._emit_reasoning(inst.instance_id, f"Instance: {inst.instance_id}")
            self._emit_reasoning(inst.instance_id, f"  Repo: {inst.repo}")
            self._emit_reasoning(inst.instance_id, f"  PR: #{inst.pr_number}" if inst.pr_number else "  PR: (no PR number)")

            evidence_for_inst: list[ContaminationEvidence] = []
            patch_exp_rate = 0.0
            ps_exp = 0.0
            tp_exp = 0.0
            top_match: Optional[MatchType] = None
            snippet = ""

            pr_diff = pr_diffs.get(inst.instance_id)
            if inst.pr_number and pr_diff is not None:
                self._emit_reasoning(inst.instance_id, f"  ✓ PR diff ready ({len(pr_diff)} bytes, pre-fetched)")
            elif inst.pr_number and pr_diff is None:
                self._emit_reasoning(inst.instance_id, f"  ⚠ PR diff fetch failed (pre-fetched phase)")

            if pr_diff is None:
                from .github_fetch import get_last_fetch_error
                fetch_err = get_last_fetch_error(inst.repo, inst.pr_number) if inst.pr_number else None
                no_pr_expected = solution_mode and not inst.pr_number
                if no_pr_expected:
                    self._emit_reasoning(inst.instance_id, "  ℹ source PR diff not expected for solution-mode audit")
                elif fetch_err:
                    self._emit_reasoning(inst.instance_id, f"  ⚠ PR diff fetch failed: {fetch_err}")
                else:
                    self._emit_reasoning(inst.instance_id, f"  ⚠ PR diff not available (rate limited or deleted)")
                # Missing PR diffs are only "not evaluated" for PR-diff audits.
                # Solution-mode benchmarks have no source PR baseline by design,
                # but can still be evaluated via CodeSeal/corpus/independent-source signals.
                repo_in_corpus = repo_in_training_corpus(inst.repo)
                if repo_in_corpus:
                    evidence_for_inst.append(ContaminationEvidence(
                        instance_id=inst.instance_id,
                        match_type=MatchType.REPO_IN_TRAINING_CORPUS,
                        source_url=f"https://github.com/{inst.repo}",
                        source_repo=inst.repo,
                        similarity=1.0,
                        message=f"Source repo {inst.repo} is in known LLM training corpora (The Stack v2 / GH Archive)",
                        source_kind="stack_v2_bloom",
                        verification_status="probabilistic_membership",
                        scope_note="Bloom filter membership is probabilistic; false positives are possible.",
                    ))
                codeseal_similarity, codeseal_matches = self._collect_codeseal_evidence(inst, evidence_for_inst)
                ir = score_instance(
                    instance_id=inst.instance_id,
                    repo=inst.repo,
                    patch_exposure_rate=0.0,
                    problem_statement_exposure=0.0,
                    test_patch_exposure=0.0,
                    repo_in_corpus=repo_in_corpus,
                    pr_url=inst.pr_url,
                    evidence_count=len(evidence_for_inst),
                    top_match_type=None,
                    snippet="(solution-mode audit: no source PR baseline)" if no_pr_expected else "(PR diff not available)",
                    not_evaluated=not no_pr_expected,
                )
                self._apply_codeseal_risk(ir, codeseal_similarity, codeseal_matches, len(evidence_for_inst))

                # the source PR diff could not be fetched. The user may have a
                # valid GitHub token and the patch text itself is enough to run
                # GitHub code search. The old `continue` path marked these
                # instances as merely not-evaluated, losing exact external
                # evidence URLs and making token-based verification look broken.
                if self._independent_search and inst.patch:
                    try:
                        from .independent_search import search_independent_sources
                        iv = search_independent_sources(inst.patch, inst.repo)
                        ir.independent_hits = iv.independent_hits
                        ir.independent_candidate_hits = getattr(iv, "candidate_hits", 0)
                        ir.vendor_like_hits = getattr(iv, "vendor_like_hits", 0)
                        ir.non_vendor_hits = getattr(iv, "non_vendor_hits", iv.independent_hits)
                        if iv.searched and iv.independent_hits > 0:
                            repo_list = ", ".join(iv.independent_repos or [])
                            candidate_hits = getattr(iv, "candidate_hits", iv.independent_hits)
                            vendor_hits = getattr(iv, "vendor_like_hits", 0)
                            non_vendor_hits = getattr(iv, "non_vendor_hits", iv.independent_hits)
                            verified_urls = list(getattr(iv, "verified_urls", None) or [])
                            verified_sources = list(getattr(iv, "verified_sources", None) or [])
                            exact_url = (
                                verified_urls[0]
                                if verified_urls else
                                (verified_sources[0].get("html_url", "") if verified_sources else "")
                            )
                            matched_line_total = sum(int(h.get("matched_lines", 0) or 0) for h in verified_sources)
                            total_line_total = sum(int(h.get("total_lines", 0) or 0) for h in verified_sources)
                            ev = ContaminationEvidence(
                                instance_id=inst.instance_id,
                                match_type=MatchType.INDEPENDENT_SOURCE_HIT,
                                source_url=exact_url,
                                source_repo=repo_list,
                                similarity=1.0,
                                matched_lines=matched_line_total or iv.independent_hits,
                                total_lines=total_line_total or candidate_hits,
                                evidence_snippet=(iv.query_8gram or "")[:200],
                                message=(
                                    f"Exact changed lines verified in {iv.independent_hits} independent repo(s) "
                                    f"({non_vendor_hits} non-vendor, {vendor_hits} vendor-like; "
                                    f"{candidate_hits} search candidates): {repo_list}"
                                ),
                                source_kind="github_code_search_verified",
                                verification_status=getattr(iv, "verification_mode", "exact_changed_lines") or "exact_changed_lines",
                                vendor_like=vendor_hits > 0 and non_vendor_hits == 0,
                                scope_note=(
                                    "Verified hits appear vendor-like/dependency-copy only; treat as public availability, not benchmark-specific leakage."
                                    if vendor_hits > 0 and non_vendor_hits == 0 else
                                    "Exact changed lines verified outside the source repository even though the source PR diff was unavailable."
                                ),
                            )
                            evidence_for_inst.append(ev)
                            if ir.top_match_type is None:
                                ir.top_match_type = MatchType.INDEPENDENT_SOURCE_HIT
                                ir.snippet = ev.message
                            if non_vendor_hits >= 3 and ir.risk != RiskLevel.CRITICAL:
                                ir.risk = RiskLevel.CRITICAL
                            elif ir.risk in (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.CLEAN):
                                ir.risk = RiskLevel.HIGH
                            ir.evidence_count = len(evidence_for_inst)
                            self._emit_reasoning(inst.instance_id,
                                f"  ✓ INDEPENDENT: exact changed lines verified despite missing source PR diff: {repo_list}")
                        elif iv.searched:
                            self._emit_reasoning(inst.instance_id,
                                "  ✓ independent search ran: 0 hits in other repos (source PR diff unavailable)")
                        elif iv.error:
                            self._emit_reasoning(inst.instance_id,
                                f"  ⚠ independent search skipped: {iv.error}")
                    except Exception as exc:
                        self._emit_reasoning(inst.instance_id, f"  ⚠ independent search failed: {exc}")

                instance_risks.append(ir)
                all_evidence.extend(evidence_for_inst)
                for ev in evidence_for_inst:
                    self._emit_evidence(
                        inst.instance_id,
                        ir.risk,
                        ev.match_type,
                        ev.similarity,
                        ev.message,
                    )
                continue

            # 2. Compute patch exposure
            self._emit_reasoning(inst.instance_id, f"  ✓ PR diff fetched ({len(pr_diff)} bytes)")
            self._emit_reasoning(inst.instance_id, f"  Comparing patch against PR diff...")
            pe = compute_patch_exposure(inst.patch, pr_diff)
            # The "exposure rate" is the fraction of CHANGED lines (the actual fix)
            # that appear in the source diff. This is stricter than line_overlap
            # (which counts context lines too).
            if pe.total_changed_lines > 0:
                patch_exp_rate = pe.exposed_changed_lines / pe.total_changed_lines
            else:
                # fix code to be exposed. The previous fallback to pe.line_overlap
                # counted CONTEXT lines (e.g. `def foo():`, `pass`) that happen
                # to appear in the PR diff, producing a non-CLEAN risk (false
                # positive) despite there being no actual fix leakage. There is
                # nothing to expose -> exposure is 0.0.
                patch_exp_rate = 0.0
                self._emit_reasoning(inst.instance_id,
                    f"  ⚠ patch has 0 changed +/- lines — no fix code to expose; exposure=0.0")

            self._emit_reasoning(inst.instance_id,
                f"  Patch exposure: {pe.exposed_changed_lines}/{pe.total_changed_lines} lines ({patch_exp_rate*100:.0f}%)")

            if pe.verbatim_match:
                ev = ContaminationEvidence(
                    instance_id=inst.instance_id,
                    match_type=MatchType.PATCH_VERBATIM,
                    source_url=inst.pr_url or "",
                    source_repo=inst.repo,
                    similarity=1.0,
                    matched_lines=pe.matched_lines,
                    total_lines=pe.total_lines,
                    evidence_snippet=inst.patch[:200],
                    message=f"Gold patch is a VERBATIM substring of the GitHub PR diff ({pe.matched_lines}/{pe.total_lines} lines)",
                    source_kind="source_pr_diff",
                    verification_status="verbatim_substring",
                    scope_note="Source PR overlap is public answer availability; for GitHub-derived benchmarks it is a structural baseline.",
                )
                evidence_for_inst.append(ev)
                top_match = MatchType.PATCH_VERBATIM
                snippet = ev.message
            elif pe.normalized_match or patch_exp_rate >= 0.95:
                ev = ContaminationEvidence(
                    instance_id=inst.instance_id,
                    match_type=MatchType.PATCH_NORMALIZED,
                    source_url=inst.pr_url or "",
                    source_repo=inst.repo,
                    similarity=patch_exp_rate,
                    matched_lines=pe.exposed_changed_lines,
                    total_lines=pe.total_changed_lines,
                    evidence_snippet=inst.patch[:200],
                    message=f"Gold patch matches after normalization ({pe.exposed_changed_lines}/{pe.total_changed_lines} changed lines, {patch_exp_rate*100:.0f}%)",
                    source_kind="source_pr_diff",
                    verification_status="normalized_changed_lines",
                    scope_note="Source PR overlap is public answer availability; for GitHub-derived benchmarks it is a structural baseline.",
                )
                evidence_for_inst.append(ev)
                top_match = MatchType.PATCH_NORMALIZED
                snippet = ev.message
            elif patch_exp_rate >= self.config.threshold:
                ev = ContaminationEvidence(
                    instance_id=inst.instance_id,
                    match_type=MatchType.PATCH_NEAR_DUPLICATE,
                    source_url=inst.pr_url or "",
                    source_repo=inst.repo,
                    similarity=patch_exp_rate,
                    matched_lines=pe.exposed_changed_lines,
                    total_lines=pe.total_changed_lines,
                    evidence_snippet=inst.patch[:200],
                    message=f"Gold patch near-duplicate in PR diff ({pe.exposed_changed_lines}/{pe.total_changed_lines} changed lines, {patch_exp_rate*100:.0f}%)",
                    source_kind="source_pr_diff",
                    verification_status="changed_line_overlap",
                    scope_note="Source PR overlap is public answer availability; for GitHub-derived benchmarks it is a structural baseline.",
                )
                evidence_for_inst.append(ev)
                if top_match is None:
                    top_match = MatchType.PATCH_NEAR_DUPLICATE
                    snippet = ev.message

            # 3. Compute problem_statement exposure
            ps_verbatim, ps_jaccard = compute_text_exposure(inst.problem_statement, pr_diff)
            if ps_verbatim:
                ev = ContaminationEvidence(
                    instance_id=inst.instance_id,
                    match_type=MatchType.PROBLEM_STATEMENT_VERBATIM,
                    source_url=inst.pr_url or "",
                    source_repo=inst.repo,
                    similarity=1.0,
                    matched_lines=0,
                    total_lines=0,
                    evidence_snippet=inst.problem_statement[:200],
                    message="Problem statement is a verbatim substring of the PR body/diff",
                    source_kind="source_pr_diff",
                    verification_status="verbatim_substring",
                )
                evidence_for_inst.append(ev)
                ps_exp = 1.0
                if top_match is None:
                    top_match = MatchType.PROBLEM_STATEMENT_VERBATIM
                    snippet = ev.message
            elif ps_jaccard >= self.config.threshold:
                ev = ContaminationEvidence(
                    instance_id=inst.instance_id,
                    match_type=MatchType.PROBLEM_STATEMENT_NEAR_DUPLICATE,
                    source_url=inst.pr_url or "",
                    source_repo=inst.repo,
                    similarity=ps_jaccard,
                    evidence_snippet=inst.problem_statement[:200],
                    message=f"Problem statement near-duplicate (Jaccard {ps_jaccard:.2f})",
                    source_kind="source_pr_diff",
                    verification_status="jaccard_near_duplicate",
                )
                evidence_for_inst.append(ev)
                ps_exp = ps_jaccard
                if top_match is None:
                    top_match = MatchType.PROBLEM_STATEMENT_NEAR_DUPLICATE
                    snippet = ev.message

            # 4. Compute test_patch exposure
            tp_verbatim, tp_jaccard = compute_text_exposure(inst.test_patch, pr_diff)
            if tp_verbatim:
                ev = ContaminationEvidence(
                    instance_id=inst.instance_id,
                    match_type=MatchType.TEST_PATCH_VERBATIM,
                    source_url=inst.pr_url or "",
                    source_repo=inst.repo,
                    similarity=1.0,
                    evidence_snippet=inst.test_patch[:200],
                    message="Test patch (hidden tests) is a verbatim substring of the PR diff",
                    source_kind="source_pr_diff",
                    verification_status="verbatim_substring",
                )
                evidence_for_inst.append(ev)
                tp_exp = 1.0
                if top_match is None:
                    top_match = MatchType.TEST_PATCH_VERBATIM
                    snippet = ev.message
            elif tp_jaccard >= self.config.threshold:
                ev = ContaminationEvidence(
                    instance_id=inst.instance_id,
                    match_type=MatchType.TEST_PATCH_NEAR_DUPLICATE,
                    source_url=inst.pr_url or "",
                    source_repo=inst.repo,
                    similarity=tp_jaccard,
                    evidence_snippet=inst.test_patch[:200],
                    message=f"Test patch near-duplicate (Jaccard {tp_jaccard:.2f})",
                    source_kind="source_pr_diff",
                    verification_status="jaccard_near_duplicate",
                )
                evidence_for_inst.append(ev)
                tp_exp = tp_jaccard
                if top_match is None:
                    top_match = MatchType.TEST_PATCH_NEAR_DUPLICATE
                    snippet = ev.message

            # 5. Check repo membership in training corpus
            repo_in_corpus = repo_in_training_corpus(inst.repo)
            if repo_in_corpus:
                ev = ContaminationEvidence(
                    instance_id=inst.instance_id,
                    match_type=MatchType.REPO_IN_TRAINING_CORPUS,
                    source_url=f"https://github.com/{inst.repo}",
                    source_repo=inst.repo,
                    similarity=1.0,
                    message=f"Source repo {inst.repo} is in known LLM training corpora (The Stack v2 / GH Archive)",
                    source_kind="stack_v2_bloom",
                    verification_status="probabilistic_membership",
                    scope_note="Bloom filter membership is probabilistic; false positives are possible.",
                )
                evidence_for_inst.append(ev)

            codeseal_similarity, codeseal_matches = self._collect_codeseal_evidence(inst, evidence_for_inst)

            # 6. Score the instance
            # - ngram_overlap_8: the academic-standard 8-gram overlap rate
            # - circular_match: True if the patch was compared to its own PR diff
            #   (the regular audit path is circular by construction for SWE-bench
            #   Verified — instance_id encodes the PR number, so the patch WAS
            #   derived from that exact PR. A 100% match is guaranteed.)
            from .independent_search import is_circularly_matched
            circular = is_circularly_matched(inst.patch, pr_diff)

            # scoring. Fall back to None rather than blocking this instance.
            merge_date = merge_dates.get(inst.instance_id)
            if merge_date:
                self._emit_reasoning(inst.instance_id, f"  PR merged at: {merge_date}")

            ir = score_instance(
                instance_id=inst.instance_id,
                repo=inst.repo,
                patch_exposure_rate=patch_exp_rate,
                problem_statement_exposure=ps_exp,
                test_patch_exposure=tp_exp,
                repo_in_corpus=repo_in_corpus,
                pr_url=inst.pr_url,
                evidence_count=len(evidence_for_inst),
                top_match_type=top_match,
                snippet=snippet,
                merge_date=merge_date,
                model_cutoff=getattr(self.config, 'model_cutoff', '2024-03-15'),
            )
            ir.ngram_overlap_8 = pe.ngram_overlap_8
            ir.embedding_similarity = pe.embedding_similarity  # 7th signal
            ir.circular_match = circular
            if merge_date:
                ir.merge_date = merge_date
                ir.temporal_aligned = bool(merge_date and not _date_gte(merge_date, getattr(self.config, 'model_cutoff', '2024-03-15')))
            self._apply_codeseal_risk(ir, codeseal_similarity, codeseal_matches, len(evidence_for_inst))
            if circular:
                self._emit_reasoning(inst.instance_id,
                    f"  ⚠ CIRCULAR: patch compared to its own PR diff (instance_id encodes PR #{inst.pr_number}). "
                    f"A 100% match is guaranteed by construction, not discovered. "
                    f"Independent-source verification (below) provides non-circular evidence.")
            self._emit_reasoning(inst.instance_id,
                f"  8-gram overlap: {pe.ngram_overlap_8*100:.1f}% (academic standard signal)")
            self._emit_reasoning(inst.instance_id,
                f"  embedding similarity: {pe.embedding_similarity*100:.1f}% (7th signal — paraphrase-resistant)")

            # but n-gram overlap is LOW (<0.50), this is a PARAPHRASED
            # contamination match — the patch was reformatted (whitespace,
            # casing, identifier renames) to defeat n-gram detection, but the
            # char-3-gram cosine still catches it. Add evidence + bump risk.
            # This is exactly the gap "Rethinking Benchmark and Contamination"
            # (2024) identified: n-gram is blind to rephrased samples.
            if pe.embedding_similarity >= 0.85 and pe.ngram_overlap_8 < 0.50:
                ev = ContaminationEvidence(
                    instance_id=inst.instance_id,
                    match_type=MatchType.EMBEDDING_SIMILARITY_HIT,
                    source_url=inst.pr_url or "",
                    source_repo=inst.repo,
                    similarity=pe.embedding_similarity,
                    evidence_snippet=inst.patch[:200],
                    message=(
                        f"Paraphrased contamination: embedding similarity {pe.embedding_similarity*100:.0f}% "
                        f"but 8-gram only {pe.ngram_overlap_8*100:.0f}% — patch was reformatted "
                        f"(whitespace/casing/renames) to defeat n-gram detection."
                    ),
                )
                evidence_for_inst.append(ev)
                if top_match is None:
                    top_match = MatchType.EMBEDDING_SIMILARITY_HIT
                    snippet = ev.message
                    # above were dead code — score_instance already ran and
                    # ir.top_match_type/ir.snippet were set to None/"" (since
                    # the 7th signal fires AFTER scoring). Sync them to ir so
                    # the JSON report shows the paraphrase evidence correctly.
                    ir.top_match_type = top_match
                    ir.snippet = snippet
                # Bump risk: a paraphrased match the n-gram missed is strong
                # evidence of deliberate evasion. Upgrade to at least HIGH.
                if ir.risk in (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.CLEAN):
                    ir.risk = RiskLevel.HIGH
                ir.evidence_count = len(evidence_for_inst)
                self._emit_reasoning(inst.instance_id,
                    f"  ⚠ PARAPHRASE MATCH: embedding {pe.embedding_similarity*100:.0f}% vs ngram "
                    f"{pe.ngram_overlap_8*100:.0f}% — contamination detected despite reformatting. "
                    f"Risk bumped to {ir.risk.value}.")

            # verification. When GITHUB_TOKEN is set, search GitHub code
            # search for the patch's most distinctive 8-gram across the
            # GitHub code-search index (bounded by max_results/rate limits). Hits in repos OTHER than the source repo break
            # the circularity of comparing a patch to its own PR diff.
            if self._independent_search and inst.patch:
                from .independent_search import search_independent_sources
                iv = search_independent_sources(inst.patch, inst.repo)
                ir.independent_hits = iv.independent_hits
                ir.independent_candidate_hits = getattr(iv, "candidate_hits", 0)
                ir.vendor_like_hits = getattr(iv, "vendor_like_hits", 0)
                ir.non_vendor_hits = getattr(iv, "non_vendor_hits", iv.independent_hits)
                if iv.searched:
                    if iv.independent_hits > 0:
                        repo_list = ", ".join(iv.independent_repos or [])
                        candidate_hits = getattr(iv, "candidate_hits", iv.independent_hits)
                        vendor_hits = getattr(iv, "vendor_like_hits", 0)
                        non_vendor_hits = getattr(iv, "non_vendor_hits", iv.independent_hits)
                        self._emit_reasoning(inst.instance_id,
                            f"  ✓ INDEPENDENT: exact changed lines verified in {iv.independent_hits} repo(s) "
                            f"OTHER than {inst.repo} ({non_vendor_hits} non-vendor, {vendor_hits} vendor-like; "
                            f"{candidate_hits} search candidates): {repo_list}")
                        # Add non-circular evidence. Prefer the exact verified
                        # GitHub file URL that was fetched and line-checked; only
                        # fall back to a replay search when no exact URL is present.
                        verified_urls = list(getattr(iv, "verified_urls", None) or [])
                        verified_sources = list(getattr(iv, "verified_sources", None) or [])
                        exact_url = (
                            verified_urls[0]
                            if verified_urls else
                            (verified_sources[0].get("html_url", "") if verified_sources else "")
                        )
                        matched_line_total = sum(int(h.get("matched_lines", 0) or 0) for h in verified_sources)
                        total_line_total = sum(int(h.get("total_lines", 0) or 0) for h in verified_sources)
                        ev = ContaminationEvidence(
                            instance_id=inst.instance_id,
                            match_type=MatchType.INDEPENDENT_SOURCE_HIT,
                            source_url=exact_url,
                            source_repo=repo_list,
                            similarity=1.0,
                            matched_lines=matched_line_total or iv.independent_hits,
                            total_lines=total_line_total or candidate_hits,
                            evidence_snippet=(iv.query_8gram or "")[:200],
                            message=(
                                f"Exact changed lines verified in {iv.independent_hits} independent repo(s) "
                                f"({non_vendor_hits} non-vendor, {vendor_hits} vendor-like; "
                                f"{candidate_hits} search candidates): {repo_list}"
                            ),
                            source_kind="github_code_search_verified",
                            verification_status=getattr(iv, "verification_mode", "exact_changed_lines") or "exact_changed_lines",
                            vendor_like=vendor_hits > 0 and non_vendor_hits == 0,
                            scope_note=(
                                "Verified hits appear vendor-like/dependency-copy only; treat as public availability, "
                                "not benchmark-specific leakage."
                                if vendor_hits > 0 and non_vendor_hits == 0 else
                                "Exact changed lines verified outside the source repository."
                            ),
                        )
                        evidence_for_inst.append(ev)
                        # Bump risk: independent evidence is strong.
                        # >=3 non-vendor exact repos = CRITICAL; >=1 exact repo = at least HIGH.
                        # Vendor-like dependency copies are public availability, but not
                        # benchmark-specific leakage by themselves.
                        if non_vendor_hits >= 3 and ir.risk != RiskLevel.CRITICAL:
                            ir.risk = RiskLevel.CRITICAL
                        elif ir.risk in (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.CLEAN):
                            ir.risk = RiskLevel.HIGH
                        ir.evidence_count = len(evidence_for_inst)
                        # above can OVERWRITE the temporal downgrade applied by
                        # score_instance (which caps post-cutoff patches at
                        # HIGH, since they cannot be in the training corpus).
                        # Re-apply the temporal cap here so a post-cutoff patch
                        # is never reported CRITICAL on independent evidence
                        # alone (it's strong, but it still can't be in that
                        # specific corpus).
                        _cutoff = getattr(self.config, 'model_cutoff', '2024-03-15')
                        if merge_date and _cutoff and _date_gte(merge_date, _cutoff):
                            if ir.risk == RiskLevel.CRITICAL:
                                ir.risk = RiskLevel.HIGH
                    else:
                        self._emit_reasoning(inst.instance_id,
                            f"  ✓ independent search ran: 0 hits in other repos "
                            f"(patch not replicated elsewhere on GitHub)")
                elif iv.error:
                    self._emit_reasoning(inst.instance_id,
                        f"  ⚠ independent search skipped: {iv.error}")

            if self._independent_search and _issue_search_enabled() and inst.problem_statement and len(inst.problem_statement) > 50:
                try:
                    from .independent_search import search_issues_for_problem_statement
                    iv2 = search_issues_for_problem_statement(inst.problem_statement, inst.repo)
                    if iv2.searched and iv2.independent_hits > 0:
                        repo_list2 = ", ".join(iv2.independent_repos or [])
                        self._emit_reasoning(inst.instance_id,
                            f"  ✓ ISSUES: problem statement found in {iv2.independent_hits} issue(s) "
                            f"in repos other than {inst.repo}: {repo_list2}")
                        issue_urls = list(getattr(iv2, "verified_urls", None) or [])
                        ev2 = ContaminationEvidence(
                            instance_id=inst.instance_id,
                            match_type=MatchType.ISSUE_CONTAMINATION_HIT,
                            source_url=issue_urls[0] if issue_urls else "",
                            source_repo=repo_list2,
                            similarity=1.0,
                            matched_lines=iv2.independent_hits,
                            total_lines=iv2.independent_hits,
                            evidence_snippet=inst.problem_statement[:200],
                            message=f"Problem statement found in {iv2.independent_hits} independent issue(s): {repo_list2}",
                            source_kind="github_issue_search_result",
                            verification_status=getattr(iv2, "verification_mode", "github_issue_search_result") or "github_issue_search_result",
                            scope_note="Problem statement text found in an independent GitHub issue result.",
                        )
                        evidence_for_inst.append(ev2)
                        ir.evidence_count = len(evidence_for_inst)
                except Exception:
                    pass  # issues search is best-effort

            instance_risks.append(ir)
            all_evidence.extend(evidence_for_inst)

            # Reasoning: show the verdict
            self._emit_reasoning(inst.instance_id,
                f"  → {ir.risk.value.upper()}: patch_exp={patch_exp_rate*100:.0f}% "
                f"ngram8={pe.ngram_overlap_8*100:.0f}% "
                f"circular={circular} "
                f"ind_hits={ir.independent_hits} "
                f"repo_in_corpus={repo_in_corpus}")

            # 7. Emit evidence events for the UI
            for ev in evidence_for_inst:
                self._emit_evidence(
                    inst.instance_id,
                    ir.risk,
                    ev.match_type,
                    ev.similarity,
                    ev.message,
                )

            self._emit_progress(
                "auditing",
                i + 1,
                total,
                f"{inst.instance_id}: risk={ir.risk.value} patch_exp={patch_exp_rate*100:.0f}%",
            )

        # Build the report
        self._emit_progress("building report", total, total, "Assembling report")
        summary = build_summary(instance_risks)
        top = top_contaminated(instance_risks, k=20)
        report = AuditReport(
            config=self.config,
            summary=summary,
            instance_risks=instance_risks,
            evidence=all_evidence,
            top_contaminated=top,
                limitations=standard_limitations(self.config.benchmark, getattr(self.config, "audit_type", "pr_diff")),
            remediation=standard_remediation(self.config.benchmark),
        )
        self._emit_progress("complete", total, total, "Audit complete")
        return report

    def _collect_codeseal_evidence(
        self,
        inst: BenchmarkInstance,
        evidence_for_inst: list[ContaminationEvidence],
    ) -> tuple[float, int]:
        if not self._codeseal or not getattr(inst, "patch", ""):
            return 0.0, 0
        try:
            from .codeseal_detector import check_patch_against_bundled_model
            match = check_patch_against_bundled_model(inst.patch, enabled=True)
        except Exception as exc:
            self._emit_reasoning(inst.instance_id, f"  CodeSeal skipped: {exc}")
            return 0.0, 0

        if match.error and match.status != "hash_verified_bundled_model":
            self._emit_reasoning(inst.instance_id, f"  CodeSeal skipped: {match.error}")
            return 0.0, 0
        if not match.checked:
            self._emit_reasoning(inst.instance_id, "  CodeSeal skipped: insufficient distinctive patch tokens")
            return match.similarity, 0
        if not match.contaminated:
            self._emit_reasoning(
                inst.instance_id,
                f"  CodeSeal: no bundled-corpus content match ({match.patch_tokens} distinctive tokens)",
            )
            return match.similarity, 0

        matched = match.matched_files or []
        repo_list = ", ".join(fid for fid, _ in matched[:5])
        message = (
            f"CodeSeal MinHash/LSH content overlap: top similarity {match.similarity:.2f} "
            f"against {len(matched)} bundled corpus file match(es) "
            f"(index size {match.index_size:,})."
        )
        evidence_for_inst.append(ContaminationEvidence(
            instance_id=inst.instance_id,
            match_type=MatchType.CODESEAL_CONTENT_HIT,
            source_url="agentseal://bundled-codeseal-model",
            source_repo=repo_list,
            similarity=match.similarity,
            matched_lines=len(matched),
            total_lines=match.index_size,
            evidence_snippet=repo_list[:200],
            message=message,
            source_kind="codeseal_minhash_lsh",
            verification_status=match.status,
            scope_note=(
                "CodeSeal is a deterministic content-overlap signal, not model "
                "memorization proof; use it with temporal and independent-source evidence."
            ),
        ))
        self._emit_reasoning(inst.instance_id, f"  {message}")
        return match.similarity, len(matched)

    def _apply_codeseal_risk(
        self,
        ir: InstanceRisk,
        similarity: float,
        matches: int,
        evidence_count: int,
    ) -> None:
        if matches <= 0:
            return
        ir.codeseal_similarity = max(0.0, min(1.0, float(similarity or 0.0)))
        ir.codeseal_matches = matches
        ir.evidence_count = evidence_count
        if ir.top_match_type is None:
            ir.top_match_type = MatchType.CODESEAL_CONTENT_HIT
            ir.snippet = (
                f"CodeSeal content overlap against bundled corpus "
                f"(similarity {ir.codeseal_similarity:.2f}, {matches} match(es))"
            )
        # CodeSeal is strong triage evidence, but it is not independent
        # source replication and never upgrades to CRITICAL on its own.
        if ir.risk == RiskLevel.CLEAN:
            ir.risk = RiskLevel.MEDIUM if ir.codeseal_similarity >= 0.30 else RiskLevel.LOW
        elif ir.risk == RiskLevel.LOW and ir.codeseal_similarity >= 0.30:
            ir.risk = RiskLevel.MEDIUM

    def _emit_progress(self, phase: str, completed: int, total: int, message: str) -> None:
        if self._on_progress is None:
            return
        try:
            self._on_progress(phase, completed, total, message)
        except Exception:
            pass

    def _emit_reasoning(self, instance_id: str, text: str) -> None:
        if self._on_reasoning is None:
            return
        try:
            self._on_reasoning(instance_id, text)
        except Exception:
            pass

    def _emit_evidence(
        self,
        instance_id: str,
        risk: RiskLevel,
        match_type: MatchType,
        similarity: float,
        message: str,
    ) -> None:
        if self._on_evidence is None:
            return
        try:
            self._on_evidence(instance_id, risk, match_type, similarity, message)
        except Exception:
            pass


__all__ = ["AgentSealEngine", "ProgressCallback", "EvidenceCallback"]
