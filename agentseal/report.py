"""AgentSeal v5.0.0 Report Engine — Scientific-report renderer.


The previous report engine (v0.5–v4.9.8) used a dashboard information
architecture: KPI cards → pie chart → bar chart → table → methodology →
limitations. This architecture led with the WEAKEST measurement (circular
patch-vs-PR overlap, structural for PR-derived benchmarks) and buried the STRONGEST
measurement (independent-source replication). Sophisticated readers (AI labs,
researchers) dismissed the report because the headline claim didn't match
the headline evidence.

This rebuild uses a NARRATIVE information architecture, modeled on canonical
scientific papers (Watson & Crick 1953, Attention Is All You Need 2017,
IPCC AR6) and investigative reports (Mueller Report, 9/11 Commission,
Feynman's Challenger appendix):

  1. THE FINDING — one declarative sentence, measured tone, named specifics
  2. THE FRAME — what this measures, what it doesn't, what's baseline
  3. FINDINGS — numbered, IPCC-calibrated confidence qualifiers
  4. THE EVIDENCE — named, specific, clickable verification URLs
  5. THE COUNTERFACTUAL — what a clean benchmark would score
  6. WHAT THIS REPORT DOES NOT ESTABLISH — honest limitations (prominent)
  7. TRIAGE AND ACTION — specific instance lists, exportable filters
  8. METHODOLOGY — reference material (at the end, not the beginning)
  9. APPENDICES — raw data, per-repo breakdowns, structural baselines

Typography: system serif/body fonts for offline-friendly reports
authority, monospace (Consolas) for data/code, generous whitespace.
Dark cockpit theme retained but refined.

The data layer (schemas, engine, audit results) is UNCHANGED — only the
presentation layer is rebuilt.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, select_autoescape
from pydantic import BaseModel

from .schemas import AuditReport, ContaminationEvidence, InstanceRisk, MatchType, RiskLevel
from .evidence_links import clickable_evidence_url


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

def write_json(report: AuditReport, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return out


def read_json(path: str | Path) -> AuditReport:
    return AuditReport.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def _truncate(s, n=80):
    if not s:
        return ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _html_escape(s) -> str:
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&#x27;")
    )


def _safe_pct(numerator, denominator):
    if not denominator or denominator == 0:
        return 0.0
    return round(numerator / denominator * 100, 1)


def _corpus_signal_counts(report: AuditReport) -> tuple[int, int, int, int]:
    """Return CodeSeal/Bloom/pre-cutoff-independent counts for legacy MD NLG."""
    risks = getattr(report, "instance_risks", []) or []
    total = getattr(getattr(report, "summary", None), "total_instances", len(risks)) or len(risks) or 1
    codeseal = sum(1 for ir in risks if getattr(ir, "codeseal_matches", 0) > 0)
    bloom = sum(1 for ir in risks if getattr(ir, "repo_in_training_corpus", False))
    cutoff = getattr(getattr(report, "config", None), "model_cutoff", "2024-03-15")
    pre_ind = sum(
        1 for ir in risks
        if getattr(ir, "independent_hits", 0) > 0
        and getattr(ir, "merge_date", None)
        and ir.merge_date != "—"
        and ir.merge_date < cutoff
    )
    return codeseal, bloom, pre_ind, total


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


def _risk_value(ir: InstanceRisk) -> str:
    risk = getattr(ir, "risk", RiskLevel.CLEAN)
    return str(getattr(risk, "value", risk)).lower()


def _has_source_pr_exposure(ir: InstanceRisk) -> bool:
    return (
        float(getattr(ir, "patch_exposure", 0.0) or 0.0) > 0.0
        or float(getattr(ir, "problem_statement_exposure", 0.0) or 0.0) > 0.0
        or float(getattr(ir, "test_patch_exposure", 0.0) or 0.0) > 0.0
    )


def _has_independent_or_corpus_signal(ir: InstanceRisk) -> bool:
    return (
        int(getattr(ir, "independent_hits", 0) or 0) > 0
        or bool(getattr(ir, "repo_in_training_corpus", False))
        or int(getattr(ir, "codeseal_matches", 0) or 0) > 0
    )


def _per_repo_signal_data(report: AuditReport) -> list[dict[str, int | str]]:
    """Aggregate repository rows without relabeling source-PR exposure as contamination."""
    repos: dict[str, dict[str, int]] = {}
    for ir in report.instance_risks:
        repo = ir.repo
        if repo not in repos:
            repos[repo] = {
                "total": 0,
                "source": 0,
                "signal": 0,
                "independent": 0,
                "corpus": 0,
                "risk_flagged": 0,
            }
        row = repos[repo]
        row["total"] += 1
        if _has_source_pr_exposure(ir):
            row["source"] += 1
        if int(getattr(ir, "independent_hits", 0) or 0) > 0:
            row["independent"] += 1
        if bool(getattr(ir, "repo_in_training_corpus", False)) or int(getattr(ir, "codeseal_matches", 0) or 0) > 0:
            row["corpus"] += 1
        if _has_independent_or_corpus_signal(ir):
            row["signal"] += 1
        if _risk_value(ir) != "clean":
            row["risk_flagged"] += 1

    repo_data = []
    for repo in sorted(
        repos.keys(),
        key=lambda name: (
            -repos[name]["signal"] / max(repos[name]["total"], 1),
            -repos[name]["source"] / max(repos[name]["total"], 1),
            name,
        ),
    ):
        row = repos[repo]
        rate = round(100 * row["signal"] / row["total"]) if row["total"] > 0 else 0
        repo_data.append({"repo": repo, **row, "rate": rate})
    return repo_data


# ---------------------------------------------------------------------------
# Narrative engine — generates the finding sentence + frame from real data
# ---------------------------------------------------------------------------

def _generate_finding_sentence(report: AuditReport) -> str:
    """Generate THE finding sentence for legacy Markdown reports.

    Calibrated by evidence type: CodeSeal/Bloom are corpus-level signals;
    independent GitHub/HF hits are public-source replication unless paired
    with corpus membership or temporal alignment.
    """
    s = report.summary
    total = s.total_instances or 1
    benchmark = _html_escape(report.config.benchmark)
    scope_prefix = (
        f"Across the audited sample of {total} {benchmark} instances"
        if getattr(report.config, "sample_size", 0) else
        f"Across {total} {benchmark} instances"
    )

    ind_instances = [ir for ir in report.instance_risks if ir.independent_hits > 0]
    ind_count = len(ind_instances)
    link_ok_count = len(_independent_clickable_instance_ids(report))
    ind_rate = (ind_count / total * 100) if total else 0
    codeseal_count, bloom_count, pre_ind_count, _ = _corpus_signal_counts(report)
    pro_audit = _is_pro_audit(report)
    solution_audit = _is_solution_audit(report)
    corpus_bits = []
    if codeseal_count:
        corpus_bits.append(f"CodeSeal matched {codeseal_count} instance(s) against the bundled corpus index")
    if bloom_count:
        corpus_bits.append(f"the Stack v2 Bloom filter marked {bloom_count} source repo(s) as probable corpus members")
    if pre_ind_count:
        corpus_bits.append(f"{pre_ind_count} independent replicated instance(s) predate the selected cutoff")

    if ind_count == 0:
        if s.instances_with_patch_exposure > 0:
            location = "public default branches" if pro_audit else "their source GitHub repositories"
            base = (f"{scope_prefix}, gold patch solutions are publicly available in {location}. "
                    f"No independent-source replication was detected in this audit run.")
        elif solution_audit:
            base = (f"{scope_prefix}, AgentSeal did not detect independent-source or bundled-corpus "
                    f"evidence for the standalone solution code in this audit run.")
        else:
            exposure_target = "public default branches" if pro_audit else "source GitHub repositories"
            base = (f"{scope_prefix}, AgentSeal did not detect gold patch exposure in {exposure_target}. "
                    f"No independent-source replication was detected in this audit run.")
        if corpus_bits:
            base += " Corpus-level signal still exists: " + "; ".join(corpus_bits) + "."
        return base

    avg_repos = sum(ir.independent_hits for ir in ind_instances) / ind_count if ind_count else 0
    named_repos = set()
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
                    named_repos.add(_html_escape(r))
            if len(named_repos) >= 5:
                break

    named_list = sorted(named_repos)[:3]
    named_str = ""
    if named_list:
        if len(named_list) == 1:
            named_str = f" including {named_list[0]}"
        elif len(named_list) == 2:
            named_str = f" including {named_list[0]} and {named_list[1]}"
        else:
            named_str = f" including {named_list[0]}, {named_list[1]}, and {named_list[2]}"

    if link_ok_count < ind_count:
        out = (f"{scope_prefix}, AgentSeal recorded independent-source matches for {ind_count} "
               f"({ind_rate:.0f}%) instance(s), but strict report preflight confirmed clickable "
               f"GitHub evidence URLs for {link_ok_count}. Unconfirmed URLs are not linked. "
               f"A clean benchmark would score zero recorded independent matches.")
    else:
        out = (f"{scope_prefix}, {ind_count} ({ind_rate:.0f}%) had their gold patch "
               f"replicated in independent public GitHub repositories — repositories other "
               f"than the source — at an average of {avg_repos:.0f} repos per affected "
               f"instance{named_str}. A clean benchmark would score zero.")
    if corpus_bits:
        out += " Corpus evidence also exists: " + "; ".join(corpus_bits) + "."
    return out


def _generate_sample_scope_note(report: AuditReport) -> str:
    if not getattr(report.config, "sample_size", 0):
        return ""
    repos = sorted({ir.repo for ir in report.instance_risks if ir.repo})
    repo_phrase = "" if _is_solution_audit(report) else (f" across {len(repos)} source repo(s)" if repos else "")
    return (
        f"This is an audited sample of {report.summary.total_instances} instance(s){repo_phrase}; "
        f"it is not a benchmark-wide estimate unless the sample covers the full benchmark. "
        f"Use stratified or full-run results before making benchmark-level claims."
    )


def _generate_frame_paragraph(report: AuditReport) -> str:
    """Generate the frame paragraph — what this measures, what it doesn't."""
    benchmark = _html_escape(report.config.benchmark)
    model_name = _html_escape(getattr(report.config, "model_name", "stack-v2"))
    cutoff = _html_escape(getattr(report.config, "model_cutoff", "2024-03-15"))
    codeseal_count, bloom_count, pre_ind_count, total = _corpus_signal_counts(report)
    pro_audit = _is_pro_audit(report)
    solution_audit = _is_solution_audit(report)
    corpus_note = []
    if codeseal_count:
        corpus_note.append(f"CodeSeal found bundled-corpus content overlap for {codeseal_count} of {total} instance(s)")
    if bloom_count:
        corpus_note.append(f"Bloom membership marked {bloom_count} source repo(s) as probable corpus members")
    if pre_ind_count:
        corpus_note.append(f"{pre_ind_count} independent replicated instance(s) predate {model_name}'s cutoff")
    corpus_text = (" Corpus-level signal: " + "; ".join(corpus_note) + ".") if corpus_note else ""
    if pro_audit:
        baseline_text = (
            f"For Pro audits, patch exposure means gold fix lines were detected in public "
            f"default branches after base-commit exclusion and multi-method consensus. A 0% "
            f"rate means the audited sample had no qualifying default-branch exposure."
        )
    elif solution_audit:
        baseline_text = (
            f"For solution-mode audits, there is no source PR diff baseline by design. "
            f"AgentSeal evaluates standalone solution text with CodeSeal, corpus-membership, "
            f"and independent-source replication signals."
        )
    else:
        baseline_text = (
            f"Patch-versus-source-PR overlap is structural for PR-derived benchmarks — {benchmark} gold "
            f"patches are derived from merged GitHub pull requests — and is reported below "
            f"as a <em>structural baseline</em>, not an independent finding."
        )
    return (
        f"This report measures data <strong>availability</strong>, corpus-level overlap, "
        f"and public-source replication; it does <strong>not</strong> prove behavioral model "
        f"memorization. {baseline_text} Independent-source replication means gold patch "
        f"text was found in repositories other than the source repo. CodeSeal hits indicate "
        f"deterministic content overlap against the bundled corpus index, and Stack v2 Bloom "
        f"hits indicate probable repository membership in that corpus. Temporal alignment "
        f"against {model_name}'s training-data cutoff ({cutoff}) refines which instances were "
        f"eligible for that specific corpus.{corpus_text}"
    )


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


def _generate_findings_list(report: AuditReport) -> list[dict]:
    """Generate numbered findings with IPCC-style confidence qualifiers.

    Returns a list of {number, confidence, text} dicts.
    """
    s = report.summary
    total = s.total_instances or 1
    findings = []
    pro_audit = _is_pro_audit(report)

    ind_instances = [ir for ir in report.instance_risks if ir.independent_hits > 0]
    ind_count = len(ind_instances)
    link_ok_count = len(_independent_clickable_instance_ids(report))
    ind_rate = (ind_count / total * 100) if total else 0

    # Finding 1: Independent-source replication rate
    if ind_count > 0:
        avg_repos = sum(ir.independent_hits for ir in ind_instances) / ind_count
        confidence = "high confidence" if ind_rate >= 50 else "medium confidence"
        if link_ok_count < ind_count:
            text = (f"AgentSeal recorded independent-source matches for {ind_count} of {total} "
                    f"instances ({ind_rate:.0f}%), but strict report preflight confirmed clickable "
                    f"GitHub evidence URLs for {link_ok_count}. Unconfirmed or placeholder URLs "
                    f"are deliberately not linked.")
        else:
            text = (f"Gold patches for {ind_count} of {total} instances ({ind_rate:.0f}%) "
                    f"are replicated in independent public GitHub repositories, at an average "
                    f"of {avg_repos:.0f} repositories per affected instance.")
        findings.append({
            "confidence": confidence,
            "text": text
        })

    # Finding 1b: Corpus-level signals from CodeSeal/Bloom/temporal alignment
    codeseal_count, bloom_count, pre_ind_count, _total = _corpus_signal_counts(report)
    if codeseal_count or bloom_count or pre_ind_count:
        parts = []
        if codeseal_count:
            parts.append(f"CodeSeal matched {codeseal_count} instance(s) against the bundled content-overlap corpus index")
        if bloom_count:
            parts.append(f"the Stack v2 Bloom filter marked {bloom_count} source repo(s) as probable corpus members")
        if pre_ind_count:
            parts.append(f"{pre_ind_count} independent-source replicated instance(s) predate the selected training cutoff")
        findings.append({
            "confidence": "high confidence" if codeseal_count or (bloom_count and pre_ind_count) else "medium confidence",
            "text": "Corpus-level contamination evidence is present: " + "; ".join(parts) + ". "
                    "CodeSeal is deterministic content overlap; Bloom membership is probabilistic; "
                    "temporal alignment establishes eligibility, not behavioral memorization."
        })

    # Finding 2: Named repos (if available)
    named_repos = set()
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
                    named_repos.add(r)
            if len(named_repos) >= 3:
                break
    if named_repos:
        top = sorted(named_repos)[:5]
        confidence = "very likely" if len(top) >= 3 else "likely"
        findings.append({
            "confidence": confidence,
            "text": f"The replicating repositories include {', '.join(_html_escape(x) for x in top[:3])}"
                    f"{', among others' if len(top) > 3 else ''}. These are verified public-source "
                    f"replication locations. Treat them as corpus evidence only when paired with "
                    f"CodeSeal, Bloom-filter, or pre-cutoff temporal alignment signals."
        })

    # Finding 3: Temporal alignment (if merge_date data exists)
    with_dates = [ir for ir in report.instance_risks if ir.merge_date]
    if with_dates:
        # Stack v2 cutoff: 2024-03-15
        pre_cutoff = sum(1 for ir in with_dates if ir.merge_date and ir.merge_date < "2024-03-15")
        post_cutoff = sum(1 for ir in with_dates if ir.merge_date and ir.merge_date >= "2024-03-15")
        if pre_cutoff or post_cutoff:
            findings.append({
                "confidence": "high confidence",
                "text": f"Of {len(with_dates)} instances with confirmed merge dates, "
                        f"{pre_cutoff} were merged before the Stack v2 cutoff (2024-03-15) "
                        f"and {post_cutoff} after. Post-cutoff instances cannot be in that "
                        f"specific training corpus despite being publicly available now."
            })

    # Finding 4: Baseline/exposure-method acknowledgment
    if pro_audit:
        findings.append({
            "confidence": "high confidence",
            "text": f"Default-branch gold patch exposure is ~{(s.patch_exposure_rate * 100):.0f}% "
                    f"under the Pro audit consensus path. This is not a source-PR circularity check; "
                    f"it asks whether the fix appears in public default branches after excluding "
                    f"base-commit content."
        })
    elif _is_solution_audit(report):
        findings.append({
            "confidence": "high confidence",
            "text": "This is a solution-mode audit: no source PR diff baseline is expected. "
                    "The relevant evidence channels are CodeSeal corpus overlap, Bloom corpus "
                    "membership where a real source repo exists, and independent-source replication."
        })
    else:
        findings.append({
            "confidence": "high confidence",
            "text": f"Patch-versus-source-PR overlap of ~{(s.patch_exposure_rate * 100):.0f}% is "
                    f"structural — {report.config.benchmark} gold patches are derived from merged GitHub pull requests — "
                    f"and does not constitute independent evidence of contamination. This figure "
                    f"is included as a baseline, not a finding."
        })

    # Finding 5: Problem statement / test patch exposure (if any)
    if s.instances_with_problem_statement_exposure > 0:
        findings.append({
            "confidence": "medium confidence",
            "text": f"Problem statement text for {s.instances_with_problem_statement_exposure} "
                    f"instances appears in the source PR diff, indicating the issue text is "
                    f"publicly available alongside the solution."
        })

    if s.instances_with_test_patch_exposure > 0:
        findings.append({
            "confidence": "medium confidence",
            "text": f"Hidden test case code for {s.instances_with_test_patch_exposure} instances "
                    f"is present in the source PR diff, meaning the evaluation criteria are "
                    f"publicly accessible."
        })

    return findings


def _get_independent_evidence_rows(report: AuditReport) -> list[dict]:
    """Build the evidence table rows for instances with independent-source hits.

    Sorted by independent_hits descending (most replicated first).
    Each row includes a clickable exact evidence URL when the audit stored one.

    evidence.source_url being populated. If the search found hits but
    didn't populate independent_repos (None), these fields were empty —
    leaving the Named Repositories / Merged / Verify columns blank.
    Now avoids fabricating a verification URL. If exact provenance was not
    stored, the Verify column is blank instead of linking to a misleading
    GitHub search replay.
    """
    rows = []
    # Build a lookup from instance_id to ALL evidence (not just independent)
    ev_by_instance = {}
    for ev in report.evidence:
        ev_by_instance.setdefault(ev.instance_id, []).append(ev)

    for ir in report.instance_risks:
        if ir.independent_hits > 0:
            evs = ev_by_instance.get(ir.instance_id, [])
            named_repos = ""
            search_url = ""
            query_text = ""

            # Try to find independent-source evidence
            url_status = "missing_url"
            raw_url = ""
            for ev in evs:
                if 'independent' in ev.match_type.value.lower():
                    named_repos = ev.source_repo or ""
                    raw_url = ev.source_url or ""
                    search_url, url_status = clickable_evidence_url(
                        raw_url,
                        source_kind=getattr(ev, "source_kind", ""),
                        verification_status=getattr(ev, "verification_status", ""),
                    )
                    query_text = ev.evidence_snippet or ""
                    break

            if not named_repos:
                # Try issue contamination evidence too
                for ev in evs:
                    if 'issue' in ev.match_type.value.lower():
                        named_repos = ev.source_repo or "(search ran — see evidence)"
                        raw_url = ev.source_url or raw_url
                        search_url, url_status = clickable_evidence_url(
                            raw_url,
                            source_kind=getattr(ev, "source_kind", ""),
                            verification_status=getattr(ev, "verification_status", ""),
                        )
                        break
                if not named_repos:
                    named_repos = f"{ir.independent_hits} repo(s) — see JSON report for details"
            # Do not fabricate a GitHub search URL here. The audit must store
            # exact evidence provenance when it verifies a hit; otherwise the
            # report leaves Verify blank instead of presenting a brittle replay
            # search as proof.

            # merge_date fallback: check evidence too, not just InstanceRisk
            merge_date = ir.merge_date or "—"
            if merge_date == "—":
                for ev in evs:
                    if ev.source_url and "pull" in ev.source_url:
                        merge_date = "—"
                        break

            rows.append({
                "instance_id": ir.instance_id,
                "repo": ir.repo,
                "independent_hits": ir.independent_hits,
                "named_repos": named_repos,
                "search_url": search_url,
                "raw_url": raw_url,
                "url_status": url_status,
                "query_text": query_text[:80],
                "merge_date": merge_date,
                "ngram_overlap": ir.ngram_overlap_8,
                "risk": ir.risk.value,
            })

    rows.sort(key=lambda r: -r["independent_hits"])
    return rows


# ---------------------------------------------------------------------------
# Markdown Report
# ---------------------------------------------------------------------------

_MD_TEMPLATE = """# AgentSeal Contamination Audit

> {{ tagline }}

**Benchmark:** {{ report.config.benchmark }}
**Generated:** {{ generated_at }}
**AgentSeal version:** {{ report.config.agentseal_version }}
**Instances audited:** {{ s.total_instances }}
**CodeSeal content matches:** {{ s.instances_with_codeseal_content }} ({{ (s.codeseal_content_rate * 100)|round(1) }}%)
{% if sample_scope_note %}
**Scope note:** {{ sample_scope_note }}
{% endif %}

---

## The Finding

{{ finding_sentence }}

## Scope and Limitations of This Report

{{ frame_paragraph_html_free }}

This report does **not** establish that any specific model memorized any specific instance. Memorization requires behavioral elicitation against the model itself. This report identifies where benchmark answers are publicly available, which is a necessary but not sufficient condition for memorization.

---

## Findings

{% for f in findings %}
**{{ loop.index }}. ({{ f.confidence }})** {{ f.text }}
{% endfor %}

---

## The Evidence

Instances with independent-source replication, sorted by replication breadth.

| Instance | Source Repo | Independent Repos | Named Repositories | Merge Date | 8-gram Overlap | Evidence |
|---|---|---|---|---|---|---|
{% for row in evidence_rows -%}
| `{{ row.instance_id[:45] }}` | {{ row.repo }} | {{ row.independent_hits }} | {{ row.named_repos[:80] }} | {{ row.merge_date }} | {{ (row.ngram_overlap * 100)|round(0) }}% | {% if row.search_url %}[evidence]({{ row.search_url }}){% else %}not linked ({{ row.url_status }}){% endif %} |
{% endfor %}

{% if not evidence_rows %}
{% if token_used %}
*No independent-source replication was detected in this audit run. GitHub token authentication was available, so this usually means the processed patch fingerprints were not found beyond their source repositories within the configured search budget.*
{% else %}
*No independent-source replication was detected in this audit run. This may indicate the independent-source search was not enabled, the GITHUB_TOKEN was not set, or the patches have not yet been replicated beyond their source repositories.*
{% endif %}
{% endif %}

---

## The Counterfactual

{% if is_pro_audit %}
A benchmark with no public default-branch exposure would score **0%** on the Pro exposure metric. {{ report.config.benchmark }} scores **{{ (s.patch_exposure_rate * 100)|round(1) }}%** on this metric for this run.
{% elif is_solution_audit %}
A standalone-solution benchmark with no independent-source or bundled-corpus evidence would score **0%** on the evidence metric. {{ report.config.benchmark }} scores **{{ ind_rate_md }}%** on independent-source replication in this run.
{% else %}
A benchmark constructed with patches **not** derived from public GitHub pull requests would score **0%** on independent-source replication. {{ report.config.benchmark }} scores **{{ ind_rate_md }}%** on this metric. The delta between 0% and {{ ind_rate_md }}% is the finding.
{% endif %}

---

## What This Report Does Not Establish

- **Memorization.** This report does not prove any model memorized any instance. Availability is necessary but not sufficient for memorization. Behavioral elicitation (running the model) is required for that claim.

- **Training-corpus evidence is signal-specific.** CodeSeal matches are deterministic content-overlap hits against the bundled corpus index. Stack v2 Bloom matches are probabilistic repository-membership checks and can have false positives. Independent GitHub/HF replication is public-source evidence; it becomes corpus-level evidence only when paired with CodeSeal, Bloom membership, or pre-cutoff temporal alignment.

- **Common Crawl, Wayback Machine, HuggingFace datasets.** These are not checked directly. GitHub coverage is used as a proxy.

- **CodeSeal content overlap.** The bundled CodeSeal MinHash/LSH model is a deterministic content-overlap signal over its indexed corpus. It does not prove behavioral model memorization, but it is stronger corpus-level evidence than a plain public GitHub/HF replication hit. Read it alongside Bloom corpus-membership and temporal evidence.

- **Independent-search counts are bounded.** AgentSeal processes the configured `max_results` GitHub code-search files per instance (default 30; GitHub page max 100). Counts can understate broad replication; stored evidence URLs remain exact for verified processed hits.

- **Deleted PRs.** If a PR was deleted after the benchmark was created, AgentSeal cannot detect it. GH Archive data (not currently used) would be needed.

---

## Triage and Action

{% if ind_count_md > 0 %}
Of {{ s.total_instances }} instances, **{{ ind_count_md }}** have independent-source replication in ≥1 repository.

**Recommended action:** Filter these {{ ind_count_md }} instances before using {{ report.config.benchmark }} for model evaluation. The filter list is available in the JSON report (`instance_risks` where `independent_hits > 0`).

{% else %}
No instances require filtering based on independent-source replication in this run.

{% if token_used %}
**Recommended action:** Keep the JSON report as the current no-replication baseline and re-audit periodically; independent-source search was already token-enabled for this report.
{% else %}
**Recommended action:** Re-audit with `GITHUB_TOKEN` set to enable independent-source search. Run `/token paste` in the TUI, then `/audit` again.
{% endif %}
{% endif %}

**Re-audit cadence:** Run monthly. GitHub code search indexes new content continuously; patches not replicated today may be replicated next month.

---

## Methodology

### Audit Path

{% if has_consensus %}
This audit used the **Pro audit path** (4-method consensus): M1 exact line match, M2 normalized match, M3 test-patch match, M4 problem-statement match. Consensus requires ≥2/4 methods to agree.
{% elif is_solution_audit %}
This audit used the **solution-mode audit path** for standalone code-generation benchmarks. For each instance:
1. The canonical solution text was normalized as the gold answer artifact.
2. No source PR diff baseline was assumed or required.
3. The solution's distinctive identifiers were searched within GitHub's indexed public code via code search, bounded by the configured result budget.
4. A hash-pinned bundled CodeSeal MinHash/LSH model ran as a background content-overlap signal.
{% else %}
This audit used the **regular audit path** (GitHub PR-diff exposure + independent-source search). For each instance:
1. The gold patch's changed lines were compared against the public PR diff (structural baseline).
2. The patch's distinctive identifiers were searched within GitHub's indexed public code via code search, bounded by the configured result budget.
3. Hits in repos other than the source repo were recorded as independent-source evidence.
4. PR merge dates were fetched for temporal alignment against training-data cutoffs.
5. A hash-pinned bundled CodeSeal MinHash/LSH model ran as a background content-overlap signal.
{% endif %}

### Reproducibility

The detection and NLG logic are deterministic for fixed inputs, fixed local artifacts, and fixed cached network results. Rendered files include a generation timestamp, and live GitHub/HF search results may change as external indexes change. No LLM or model access is used.

**AgentSeal version:** {{ report.config.agentseal_version }}
**Audit date:** {{ generated_at }}
**GitHub token used:** {{ "yes" if token_used else "no" }}

---

## Appendices

### A. {% if is_pro_audit %}Public Default-Branch Exposure{% elif is_solution_audit %}Standalone Solution Signals{% else %}Structural Baseline (Patch-vs-Source-PR Overlap){% endif %}

| Metric | Value | Interpretation |
|---|---|---|
| Patch exposure rate | {{ (s.patch_exposure_rate * 100)|round(1) }}% | {% if is_pro_audit %}Default-branch exposure after Pro consensus checks{% elif is_solution_audit %}No source PR baseline in solution mode{% else %}Structural baseline for PR-derived benchmarks{% endif %} |
| Problem statement exposure | {{ (s.problem_statement_exposure_rate * 100)|round(1) }}% | {% if is_pro_audit %}Problem text detected in public target sources{% elif is_solution_audit %}No source PR baseline in solution mode{% else %}Issue text in PR diff{% endif %} |
| Test patch exposure | {{ (s.test_patch_exposure_rate * 100)|round(1) }}% | {% if is_pro_audit %}Test-patch text detected in public target sources{% elif is_solution_audit %}No source PR baseline in solution mode{% else %}Hidden test code in PR diff{% endif %} |
| Source repos marked by Stack v2 Bloom | {{ (s.repo_in_corpus_rate * 100)|round(1) }}% | Probabilistic corpus-membership signal |

### B. Risk Distribution

| Risk Level | Count | Percentage |
|---|---|---|
| CRITICAL | {{ s.critical_count }} | {{ (s.critical_count / total * 100)|round(1) }}% |
| HIGH | {{ s.high_count }} | {{ (s.high_count / total * 100)|round(1) }}% |
| MEDIUM | {{ s.medium_count }} | {{ (s.medium_count / total * 100)|round(1) }}% |
| LOW | {{ s.low_count }} | {{ (s.low_count / total * 100)|round(1) }}% |
| CLEAN | {{ s.clean_count }} | {{ (s.clean_count / total * 100)|round(1) }}% |

### C. Per-Repository Evidence Breakdown

| Repository | Total | Source-PR exposure | Independent/corpus signal | Rate |
|---|---|---|---|---|
{% for repo in repo_data -%}
| {{ repo.repo }} | {{ repo.total }} | {{ repo.source }} | {{ repo.signal }} | {{ repo.rate }}% |
{% endfor %}

### D. Not Evaluated

{% if is_solution_audit %}
{{ s.instances_not_evaluated if s.instances_not_evaluated else 0 }} instance(s) could not be evaluated. In solution mode, absence of a source PR diff is expected and is not counted as unevaluated.
{% else %}
{{ s.instances_not_evaluated if s.instances_not_evaluated else 0 }} instance(s) could not be evaluated (PR diff unavailable: deleted, private, or rate-limited). These are counted in the total but excluded from exposure calculations.
{% endif %}

---

_Generated by AgentSeal v{{ report.config.agentseal_version }} · {{ generated_at }} · Detection/NLG are deterministic for fixed inputs, local artifacts, cache state, and live-source responses._
"""


# ---------------------------------------------------------------------------
# HTML Report — scientific paper typography
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgentSeal — {{ report.config.benchmark }} Contamination Audit</title>
<style>
  :root {
    --bg: #0d1117;
    --bg-elevated: #161b22;
    --fg: #e6edf3;
    --fg-dim: #8b949e;
    --muted: #6e7681;
    --border: #30363d;
    --border-dim: #21262d;
    --accent: #ff8c42;
    --accent-dim: #ff8c4233;
    --green: #3fb950;
    --red: #ff4d4d;
    --amber: #d29922;
    --blue: #58a6ff;
    --serif: "Georgia", "Source Serif Pro", Georgia, "Times New Roman", serif;
    --mono: "Consolas", "Fira Code", ui-monospace, Menlo, monospace;
    --sans: "system-ui", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--fg);
    font-family: var(--serif);
    font-size: 17px;
    line-height: 1.75;
    -webkit-font-smoothing: antialiased;
    text-rendering: optimizeLegibility;
  }
  .container { max-width: 780px; margin: 0 auto; padding: 48px 24px 96px; }

  /* Header — paper-style */
  .paper-header {
    border-bottom: 1px solid var(--border);
    padding-bottom: 32px;
    margin-bottom: 40px;
  }
  .paper-meta {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 8px;
  }
  h1.paper-title {
    font-family: var(--sans);
    font-size: 42px;
    font-weight: 800;
    line-height: 1.15;
    margin-bottom: 16px;
    letter-spacing: -0.03em;
    color: var(--fg);
  }
  .paper-authors {
    font-family: var(--sans);
    font-size: 13px;
    color: var(--fg-dim);
    font-style: normal;
  }
  .paper-abstract-label {
    font-family: var(--mono);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--accent);
    margin-top: 24px;
    margin-bottom: 8px;
  }

  /* Section headings — sans-serif for contrast (Nature/arXiv pattern) */
  h2.section {
    font-family: var(--sans);
    font-size: 13px;
    font-weight: 700;
    margin: 56px 0 16px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border-dim);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--fg-dim);
  }
  h3.subsection {
    font-family: var(--sans);
    font-size: 14px;
    font-weight: 600;
    margin: 32px 0 12px;
    color: var(--fg);
    letter-spacing: -0.005em;
  }

  /* Prose — serif body text */
  p { margin-bottom: 16px; }
  p.lead {
    font-size: 17px;
    line-height: 1.75;
    color: var(--fg);
  }
  strong { font-weight: 600; }
  em { font-style: italic; }

  /* Code — monospace */
  code {
    font-family: var(--mono);
    font-size: 0.88em;
    background: var(--bg-elevated);
    padding: 2px 6px;
    border-radius: 4px;
    border: 1px solid var(--border-dim);
  }
  pre {
    font-family: var(--mono);
    font-size: 13px;
    background: var(--bg-elevated);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px 20px;
    overflow-x: auto;
    line-height: 1.6;
    margin: 16px 0;
  }
  pre code { background: none; border: none; padding: 0; font-size: inherit; }

  /* Findings — numbered list, IPCC-style */
  .findings { list-style: none; counter-reset: finding; }
  .findings li {
    counter-increment: finding;
    margin-bottom: 20px;
    padding-left: 40px;
    position: relative;
  }
  .findings li::before {
    content: counter(finding) ".";
    position: absolute;
    left: 0;
    top: 0;
    font-family: var(--mono);
    font-size: 13px;
    font-weight: 600;
    color: var(--accent);
  }
  .confidence {
    font-family: var(--mono);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--fg-dim);
    display: block;
    margin-top: 4px;
  }
  .confidence.high { color: var(--green); }
  .confidence.medium { color: var(--amber); }
  .confidence.very { color: var(--blue); }

  /* Evidence table */
  .evidence-table {
    width: 100%;
    border-collapse: collapse;
    font-family: var(--mono);
    font-size: 12px;
    margin: 16px 0;
  }
  .evidence-table th {
    text-align: left;
    padding: 10px 12px;
    border-bottom: 2px solid var(--border);
    font-weight: 600;
    color: var(--fg-dim);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-size: 10px;
  }
  .evidence-table td {
    padding: 10px 12px;
    border-bottom: 1px solid var(--border-dim);
    vertical-align: top;
  }
  .evidence-table tr:hover { background: var(--bg-elevated); }
  .evidence-table .instance { color: var(--accent); font-weight: 500; }
  .evidence-table .hits { color: var(--red); font-weight: 600; text-align: center; }
  .evidence-table .repos { color: var(--fg-dim); max-width: 250px; word-break: break-word; }
  .evidence-table a { color: var(--blue); text-decoration: none; }
  .evidence-table a:hover { text-decoration: underline; }

  /* Callout — frame paragraph */
  .callout {
    background: var(--bg-elevated);
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent);
    border-radius: 4px;
    padding: 20px 24px;
    margin: 24px 0;
  }
  .callout-frame {
    border-left-color: var(--blue);
  }

  /* Limitations — prominent, not buried */
  .limitations {
    background: rgba(210, 153, 34, 0.08);
    border: 1px solid var(--amber);
    border-radius: 8px;
    padding: 20px 24px;
    margin: 24px 0;
  }
  .limitations h3 {
    color: var(--amber);
    margin-top: 0;
    margin-bottom: 12px;
    font-family: var(--serif);
    font-size: 15px;
  }
  .limitations ul { padding-left: 20px; }
  .limitations li { margin-bottom: 8px; }

  /* Action triage */
  .action {
    background: rgba(255, 140, 66, 0.08);
    border: 1px solid var(--accent);
    border-radius: 8px;
    padding: 20px 24px;
    margin: 24px 0;
  }
  .action h3 {
    color: var(--accent);
    margin-top: 0;
    margin-bottom: 12px;
    font-family: var(--serif);
    font-size: 15px;
  }

  /* Stats inline */
  .stat-inline {
    font-family: var(--mono);
    font-variant-numeric: tabular-nums;
    font-weight: 600;
  }

  /* Appendix tables */
  .appendix-table {
    width: 100%;
    border-collapse: collapse;
    font-family: var(--mono);
    font-size: 12px;
    margin: 12px 0;
  }
  .appendix-table th {
    text-align: left;
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    color: var(--fg-dim);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .appendix-table td {
    padding: 8px 12px;
    border-bottom: 1px solid var(--border-dim);
  }

  /* Footer */
  .paper-footer {
    margin-top: 64px;
    padding-top: 24px;
    border-top: 1px solid var(--border);
    font-family: var(--mono);
    font-size: 11px;
    color: var(--muted);
    line-height: 1.6;
  }

  /* Counterfactual box */
  .counterfactual {
    text-align: center;
    padding: 32px 24px;
    margin: 24px 0;
    background: var(--bg-elevated);
    border: 1px solid var(--border);
    border-radius: 8px;
  }
  .counterfactual .clean-score {
    font-family: var(--mono);
    font-size: 48px;
    font-weight: 300;
    color: var(--green);
  }
  .counterfactual .actual-score {
    font-family: var(--mono);
    font-size: 48px;
    font-weight: 600;
    color: var(--red);
  }
  .counterfactual .arrow {
    font-family: var(--serif);
    font-size: 36px;
    color: var(--muted);
    margin: 0 24px;
  }
  .counterfactual .label {
    font-family: var(--mono);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--muted);
    margin-top: 8px;
  }

  /* Charts — CSS-only, no JavaScript */
  .chart-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 24px;
    margin: 24px 0;
  }
  @media (max-width: 640px) {
    .chart-grid { grid-template-columns: 1fr; }
  }
  .chart-card {
    background: var(--bg-elevated);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 20px;
  }
  .chart-card h4 {
    font-family: var(--sans);
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--fg-dim);
    margin-bottom: 16px;
  }

  /* Donut chart (risk distribution) */
  .donut-container {
    display: flex;
    align-items: center;
    gap: 20px;
  }
  .donut {
    width: 120px;
    height: 120px;
    border-radius: 50%;
    flex-shrink: 0;
    position: relative;
  }
  .donut::after {
    content: '';
    position: absolute;
    top: 20px; left: 20px; right: 20px; bottom: 20px;
    background: var(--bg-elevated);
    border-radius: 50%;
  }
  .donut-center {
    position: absolute;
    top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    z-index: 1;
    text-align: center;
  }
  .donut-center .pct {
    font-family: var(--mono);
    font-size: 22px;
    font-weight: 700;
    line-height: 1;
  }
  .donut-center .lbl {
    font-family: var(--mono);
    font-size: 9px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-top: 2px;
  }
  .donut-legend {
    flex: 1;
    font-family: var(--mono);
    font-size: 11px;
    line-height: 1.8;
  }
  .donut-legend-item {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .donut-legend-dot {
    width: 10px;
    height: 10px;
    border-radius: 2px;
    flex-shrink: 0;
  }

  /* Bar chart (per-repo independent/corpus evidence) */
  .bar-chart {
    margin-top: 8px;
  }
  .bar-row {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 6px;
  }
  .bar-label {
    width: 140px;
    font-family: var(--mono);
    font-size: 10px;
    color: var(--fg-dim);
    text-align: right;
    flex-shrink: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .bar-track {
    flex: 1;
    height: 20px;
    background: var(--bg);
    border-radius: 3px;
    overflow: hidden;
    position: relative;
  }
  .bar-fill {
    height: 100%;
    border-radius: 3px;
    display: flex;
    align-items: center;
    padding-left: 8px;
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 600;
    color: #fff;
    min-width: 30px;
    transition: width 0.3s;
  }
  .bar-fill.crit { background: var(--red); }
  .bar-fill.high { background: var(--accent); }
  .bar-fill.med { background: var(--amber); color: #000; }
  .bar-fill.low { background: var(--blue); }
  .bar-fill.clean { background: var(--green); }
  .bar-meta {
    width: 180px;
    font-family: var(--mono);
    font-size: 9px;
    color: var(--muted);
    flex-shrink: 0;
  }
  .chart-note {
    color: var(--muted);
    font-size: 12px;
    margin: 6px 0 10px;
  }

  /* Histogram (independent hits distribution) */
  .histogram {
    display: flex;
    align-items: flex-end;
    gap: 4px;
    height: 120px;
    margin-top: 12px;
    padding-bottom: 24px;
    border-bottom: 1px solid var(--border);
    position: relative;
  }
  .hist-bar {
    flex: 1;
    background: var(--accent);
    border-radius: 3px 3px 0 0;
    min-height: 2px;
    position: relative;
    transition: opacity 0.2s;
  }
  .hist-bar:hover { opacity: 0.8; }
  .hist-bar .hist-count {
    position: absolute;
    top: -18px;
    left: 50%;
    transform: translateX(-50%);
    font-family: var(--mono);
    font-size: 9px;
    color: var(--fg-dim);
  }
  .hist-label {
    position: absolute;
    bottom: -16px;
    left: 50%;
    transform: translateX(-50%);
    font-family: var(--mono);
    font-size: 8px;
    color: var(--muted);
    white-space: nowrap;
  }
  .hist-x-axis {
    display: flex;
    gap: 4px;
    margin-top: 20px;
  }
  .hist-x-label {
    flex: 1;
    text-align: center;
    font-family: var(--mono);
    font-size: 9px;
    color: var(--muted);
  }

  /* Temporal alignment bar */
  .temporal-bar {
    display: flex;
    height: 32px;
    border-radius: 6px;
    overflow: hidden;
    margin: 12px 0;
  }
  .temporal-pre {
    background: var(--red);
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 600;
    color: #fff;
  }
  .temporal-post {
    background: var(--green);
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 600;
    color: #fff;
  }
  .temporal-legend {
    display: flex;
    gap: 20px;
    margin-top: 8px;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--fg-dim);
  }
  .temporal-legend-dot {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 2px;
    margin-right: 6px;
    vertical-align: middle;
  }

  /* Responsive */
  @media (max-width: 640px) {
    .container { padding: 24px 16px 48px; }
    h1.paper-title { font-size: 22px; }
    .evidence-table { font-size: 11px; }
    .evidence-table th, .evidence-table td { padding: 6px 8px; }
  }

  a { color: var(--blue); }
  ::selection { background: var(--accent-dim); }
</style>
</head>
<body>
<div class="container">

<!-- ═══ Paper Header ═══ -->
<header class="paper-header">
  <div class="paper-meta">AgentSeal Contamination Audit · v{{ report.config.agentseal_version }}</div>
  <h1 class="paper-title">{{ report.config.benchmark }}: Independent-Source Replication Analysis</h1>
  <div class="paper-authors">AgentSeal · {{ generated_at }}</div>
  <div class="paper-abstract-label">Finding</div>
  <p class="lead">{{ finding_sentence }}</p>
  {% if sample_scope_note %}
  <div class="callout callout-frame">{{ sample_scope_note }}</div>
  {% endif %}
</header>

<!-- ═══ Frame ═══ -->
<section>
  <h2 class="section">Scope of This Report</h2>
  <div class="callout callout-frame">
    {{ frame_paragraph | safe }}
  </div>
  <p>This report does <strong>not</strong> establish that any specific model memorized any specific instance. Memorization requires behavioral elicitation against the model itself. This report identifies where benchmark answers are publicly available — a necessary but not sufficient condition for memorization.</p>
</section>

<!-- ═══ Findings ═══ -->
<section>
  <h2 class="section">Findings</h2>
  <ol class="findings">
    {% for f in findings %}
    <li>
      {{ f.text | safe }}
      <span class="confidence {{ f.confidence.split(' ')[0] }}">{{ f.confidence }}</span>
    </li>
    {% endfor %}
  </ol>
</section>

<!-- ═══ Evidence ═══ -->
<section>
  <h2 class="section">The Evidence</h2>
  <p>Instances with independent-source replication, sorted by replication breadth. Each row includes a stored verification link when exact provenance was captured.</p>
  {% if evidence_rows %}
  <table class="evidence-table">
    <thead>
      <tr>
        <th>Instance</th>
        <th>Source Repo</th>
        <th>Ind. Repos</th>
        <th>Named Repositories</th>
        <th>Merged</th>
        <th>Evidence</th>
      </tr>
    </thead>
    <tbody>
      {% for row in evidence_rows %}
      <tr>
        <td class="instance">{{ row.instance_id[:40] }}</td>
        <td>{{ row.repo }}</td>
        <td class="hits">{{ row.independent_hits }}</td>
        <td class="repos">{{ row.named_repos[:100] }}</td>
        <td>{{ row.merge_date[:10] if row.merge_date != '—' else '—' }}</td>
        <td>{% if row.search_url %}<a href="{{ row.search_url }}" target="_blank" rel="noopener noreferrer">evidence ↗</a>{% else %}<span title="Stored URL was missing or failed strict preflight: {{ row.url_status }}">not linked</span>{% endif %}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="callout">
    {% if token_used %}
    <p><em>No independent-source replication was detected in this audit run.</em> GitHub token authentication was available, so this usually means the processed patch fingerprints were not found beyond their source repositories within the configured search budget.</p>
    {% else %}
    <p><em>No independent-source replication was detected in this audit run.</em> This may indicate the independent-source search was not enabled, the <code>GITHUB_TOKEN</code> was not set, or the patches have not yet been replicated beyond their source repositories.</p>
    <p style="margin-top:12px;">To enable: run <code>/token paste</code> in the TUI to set your GitHub token, then re-run <code>/audit</code>. The independent-source search uses GitHub's code search API to look for patch text within GitHub's indexed public code, bounded by the configured result budget.</p>
    {% endif %}
  </div>
  {% endif %}
</section>

<!-- ═══ Counterfactual ═══ -->
<section>
  <h2 class="section">The Counterfactual</h2>
  <div class="counterfactual">
    {% if is_pro_audit %}
    <p style="margin-bottom:16px;">A benchmark with no public default-branch exposure would score <strong>0%</strong> on the Pro exposure metric. This run scores <strong>{{ (s.patch_exposure_rate * 100)|round(1) }}%</strong>.</p>
    {% else %}
    <div style="display:flex; justify-content:center; align-items:center; flex-wrap:wrap; gap:8px;">
      <div>
        <div class="clean-score">0%</div>
        <div class="label">Clean Benchmark</div>
      </div>
      <div class="arrow">→</div>
      <div>
        <div class="actual-score">{{ ind_rate_html }}%</div>
        <div class="label">{{ report.config.benchmark }}</div>
      </div>
    </div>
    <p style="margin-top:24px; color:var(--fg-dim); font-size:14px;">
      A benchmark constructed with patches <em>not</em> derived from public GitHub pull requests would score <strong>0%</strong> on independent-source replication. The delta between 0% and {{ ind_rate_html }}% is the finding.
    </p>
    {% endif %}
    {% if temporal_pre_ind is defined and temporal_pre_ind >= 0 %}
    <p style="margin-top:8px; color:var(--amber); font-size:13px;">
      <strong>Temporal-adjusted rate:</strong> Of the {{ ind_count_html }} replicated instances, {{ temporal_pre_ind }} were merged before the training-data cutoff ({{ model_cutoff }}). The <strong>pre-cutoff contamination rate is {{ temporal_pre_ind_pct }}%</strong> — this is the meaningful signal. The remaining {{ temporal_post_ind }} are publicly available now but were not yet public at training time.
    </p>
    {% endif %}
  </div>
</section>

<!-- ═══ Visual Analysis (charts) ═══ -->
<section>
  <h2 class="section">Visual Analysis</h2>
  <div class="chart-grid">

    <!-- Risk Distribution Donut -->
    <div class="chart-card">
      <h4>Risk Distribution</h4>
      {% if is_solution_audit %}
      <p class="chart-note">Risk verdicts include independent/corpus signals. Source-PR exposure is not applicable in solution mode.</p>
      {% else %}
      <p class="chart-note">Risk verdicts include structural source-PR exposure. Use the evidence chart for independent/corpus signals.</p>
      {% endif %}
      <div class="donut-container">
        <div class="donut" style="background: conic-gradient(
          var(--red) 0deg {{ risk_crit_deg }}deg,
          var(--accent) {{ risk_crit_deg }}deg {{ risk_high_deg }}deg,
          var(--amber) {{ risk_high_deg }}deg {{ risk_med_deg }}deg,
          var(--blue) {{ risk_med_deg }}deg {{ risk_low_deg }}deg,
          var(--green) {{ risk_low_deg }}deg 360deg
        );">
          <div class="donut-center">
            <div class="pct" style="color:var(--red)">{{ ((s.critical_count + s.high_count) / total * 100)|round(0) }}%</div>
            <div class="lbl">flagged</div>
          </div>
        </div>
        <div class="donut-legend">
          <div class="donut-legend-item"><div class="donut-legend-dot" style="background:var(--red)"></div> CRITICAL: {{ s.critical_count }}</div>
          <div class="donut-legend-item"><div class="donut-legend-dot" style="background:var(--accent)"></div> HIGH: {{ s.high_count }}</div>
          <div class="donut-legend-item"><div class="donut-legend-dot" style="background:var(--amber)"></div> MEDIUM: {{ s.medium_count }}</div>
          <div class="donut-legend-item"><div class="donut-legend-dot" style="background:var(--blue)"></div> LOW: {{ s.low_count }}</div>
          <div class="donut-legend-item"><div class="donut-legend-dot" style="background:var(--green)"></div> CLEAN: {{ s.clean_count }}</div>
        </div>
      </div>
    </div>

    <!-- Temporal Alignment -->
    <div class="chart-card">
      <h4>Temporal Alignment (vs {{ model_cutoff }})</h4>
      {% if temporal_pre > 0 or temporal_post > 0 %}
      <div class="temporal-bar">
        <div class="temporal-pre" style="width: {{ temporal_pre_pct }}%;">{{ temporal_pre }} pre-cutoff</div>
        <div class="temporal-post" style="width: {{ temporal_post_pct }}%;">{{ temporal_post }} post-cutoff</div>
      </div>
      <div class="temporal-legend">
        <span><span class="temporal-legend-dot" style="background:var(--red)"></span>Pre-cutoff (eligible for training corpus)</span>
        <span><span class="temporal-legend-dot" style="background:var(--green)"></span>Post-cutoff (NOT in that corpus)</span>
      </div>
      {% else %}
      <p style="color:var(--muted); font-size:13px;">No merge date data available.</p>
      {% endif %}
    </div>

    <!-- Per-Repository Independent/Corpus Evidence Bars -->
    <div class="chart-card">
      <h4>Per-Repository Independent/Corpus Evidence</h4>
      {% if is_solution_audit %}
      <p class="chart-note">Bars show independent/corpus signals; source-PR structural exposure is not applicable.</p>
      {% else %}
      <p class="chart-note">Bars exclude source-PR structural exposure.</p>
      {% endif %}
      <div class="bar-chart">
        {% for repo in repo_data[:8] %}
        <div class="bar-row">
          <div class="bar-label">{{ repo.repo[:25] }}</div>
          <div class="bar-track">
            <div class="bar-fill {{ 'crit' if repo.rate >= 75 else 'high' if repo.rate >= 50 else 'med' if repo.rate >= 25 else 'clean' }}" style="width: {{ repo.rate }}%;">
              {{ repo.signal }}/{{ repo.total }}
            </div>
          </div>
          <div class="bar-meta">source {{ repo.source }}/{{ repo.total }} · independent {{ repo.independent }} · corpus {{ repo.corpus }}</div>
        </div>
        {% endfor %}
      </div>
    </div>

    <!-- Independent Hits Histogram -->
    <div class="chart-card">
      <h4>Independent-Source Replication Distribution</h4>
      {% if hist_data %}
      <div class="histogram">
        {% for h in hist_data %}
        <div class="hist-bar" style="height: {{ h.pct }}%; background: {{ h.color }};" title="{{ h.label }}: {{ h.count }} instances">
          {% if h.count > 0 %}<span class="hist-count">{{ h.count }}</span>{% endif %}
        </div>
        {% endfor %}
      </div>
      <div class="hist-x-axis">
        {% for h in hist_data %}
        <div class="hist-x-label">{{ h.label }}</div>
        {% endfor %}
      </div>
      {% else %}
      <p style="color:var(--muted); font-size:13px;">No independent-source data available.</p>
      {% endif %}
    </div>

  </div>
</section>

<!-- ═══ Limitations (prominent) ═══ -->
<section>
  <h2 class="section">What This Report Does Not Establish</h2>
  <div class="limitations">
    <ul>
      {% for lim in limitations %}
      <li>{{ lim | safe }}</li>
      {% endfor %}
    </ul>
  </div>
</section>

<!-- ═══ Per-Instance Deep Dives ═══ -->
{% if instance_narratives %}
<section>
  <h2 class="section">Instance Deep Dives</h2>
  <p>The following instances show the highest independent-source replication. Each entry is generated from the actual audit data — named repos, merge dates, and clickable verification links.</p>
  {% for narrative in instance_narratives %}
  <p style="margin-bottom:20px;">{{ loop.index }}. {{ narrative | safe }}</p>
  {% endfor %}
</section>
{% endif %}

<!-- ═══ Per-Repository Analysis ═══ -->
{% if repo_narratives %}
<section>
  <h2 class="section">Repository Analysis</h2>
  {% if is_solution_audit %}
  <p>Repository/group labels ranked by independent/corpus evidence signal. Source-PR structural exposure is not applicable in solution mode.</p>
  {% else %}
  <p>Source repositories ranked by independent/corpus evidence signal, with source-PR structural exposure reported separately.</p>
  {% endif %}
  {% for narrative in repo_narratives %}
  <p style="margin-bottom:12px;">{{ narrative | safe }}</p>
  {% endfor %}
</section>
{% endif %}

<!-- ═══ Triage ═══ -->
<section>
  <h2 class="section">Triage and Action</h2>
  <div class="action">
    <h3>Recommendations</h3>
    <ul style="padding-left:20px; margin-bottom:16px;">
      {% for rec in recommendations %}
      <li style="margin-bottom:12px;">{{ rec | safe }}</li>
      {% endfor %}
    </ul>
  </div>
</section>

<!-- ═══ Methodology ═══ -->
<section>
  <h2 class="section">Methodology</h2>

  <h3 class="subsection">Audit Path</h3>
  {% if has_consensus %}
  <p>This audit used the <strong>Pro audit path</strong> (4-method consensus): M1 exact line match, M2 normalized match, M3 test-patch match, M4 problem-statement match. Consensus requires ≥2/4 methods to agree.</p>
  {% elif is_solution_audit %}
  <p>This audit used the <strong>solution-mode audit path</strong> for standalone code-generation benchmarks. For each instance:</p>
  <ol style="margin-left:20px; margin-bottom:16px;">
    <li>The canonical solution text was normalized as the gold answer artifact.</li>
    <li>No source PR diff baseline was assumed or required.</li>
    <li>The solution's distinctive identifiers were searched within GitHub's indexed public code via code search, bounded by the configured result budget.</li>
    <li>The hash-pinned bundled CodeSeal MinHash/LSH artifact ran as a background content-overlap signal when enabled.</li>
  </ol>
  {% else %}
  <p>This audit used the <strong>regular audit path</strong> (GitHub PR-diff exposure + independent-source search). For each instance:</p>
  <ol style="margin-left:20px; margin-bottom:16px;">
    <li>The gold patch's changed lines were compared against the public PR diff (structural baseline).</li>
    <li>The patch's distinctive identifiers were searched within GitHub's indexed public code via code search, bounded by the configured result budget.</li>
    <li>Hits in repos other than the source repo were recorded as independent-source evidence.</li>
    <li>PR merge dates were fetched for temporal alignment against training-data cutoffs.</li>
    <li>The hash-pinned bundled CodeSeal MinHash/LSH artifact ran as a background content-overlap signal when enabled.</li>
  </ol>
  {% endif %}

  <h3 class="subsection">Reproducibility</h3>
  <p>The detection and NLG logic are <strong>deterministic for fixed inputs, fixed local artifacts, and fixed cached network results</strong>. Rendered files include a generation timestamp, and live GitHub/HF search results may change as external indexes change. No LLM or model access is used.</p>
  <p style="font-family:var(--mono); font-size:12px; color:var(--fg-dim);">
    AgentSeal version: {{ report.config.agentseal_version }} · Audit date: {{ generated_at }} · GitHub token used: {{ "yes" if token_used else "no" }} · Temporal alignment: {{ model_name }} (cutoff: {{ model_cutoff }})
  </p>
</section>

<!-- ═══ Appendices ═══ -->
<section>
  <h2 class="section">Appendices</h2>

  <h3 class="subsection">A. {% if is_pro_audit %}Public Default-Branch Exposure{% elif is_solution_audit %}Standalone Solution Signals{% else %}Structural Baseline{% endif %}</h3>
  {% if is_pro_audit %}
  <p>Pro exposure means gold fix lines were detected in public default branches after base-commit exclusion and multi-method consensus.</p>
  {% elif is_solution_audit %}
  <p>Solution-mode benchmarks do not have source PR diffs by design. These rows show that the source-PR baseline is not applicable; independent/corpus evidence is reported separately.</p>
  {% else %}
  <p>Patch-versus-source-PR overlap is structural for PR-derived benchmarks. These metrics are included for completeness, not as findings.</p>
  {% endif %}
  <table class="appendix-table">
    <thead><tr><th>Metric</th><th>Value</th><th>Interpretation</th></tr></thead>
    <tbody>
      <tr><td>Patch exposure rate</td><td>{{ (s.patch_exposure_rate * 100)|round(1) }}%</td><td>{% if is_pro_audit %}Default-branch exposure after Pro consensus checks{% elif is_solution_audit %}No source PR baseline in solution mode{% else %}Structural baseline{% endif %}</td></tr>
      <tr><td>Problem statement exposure</td><td>{{ (s.problem_statement_exposure_rate * 100)|round(1) }}%</td><td>{% if is_pro_audit %}Problem text detected in public target sources{% elif is_solution_audit %}No source PR baseline in solution mode{% else %}Issue text in PR diff{% endif %}</td></tr>
      <tr><td>Test patch exposure</td><td>{{ (s.test_patch_exposure_rate * 100)|round(1) }}%</td><td>{% if is_pro_audit %}Test-patch text detected in public target sources{% elif is_solution_audit %}No source PR baseline in solution mode{% else %}Hidden test code in PR diff{% endif %}</td></tr>
      <tr><td>Source repos marked by Stack v2 Bloom</td><td>{{ (s.repo_in_corpus_rate * 100)|round(1) }}%</td><td>Probabilistic corpus-membership signal</td></tr>
    </tbody>
  </table>

  <h3 class="subsection">B. Risk Distribution</h3>
  <table class="appendix-table">
    <thead><tr><th>Risk Level</th><th>Count</th><th>Percentage</th></tr></thead>
    <tbody>
      <tr><td>Critical</td><td>{{ s.critical_count }}</td><td>{{ (s.critical_count / total * 100)|round(1) }}%</td></tr>
      <tr><td>High</td><td>{{ s.high_count }}</td><td>{{ (s.high_count / total * 100)|round(1) }}%</td></tr>
      <tr><td>Medium</td><td>{{ s.medium_count }}</td><td>{{ (s.medium_count / total * 100)|round(1) }}%</td></tr>
      <tr><td>Low</td><td>{{ s.low_count }}</td><td>{{ (s.low_count / total * 100)|round(1) }}%</td></tr>
      <tr><td>Clean</td><td>{{ s.clean_count }}</td><td>{{ (s.clean_count / total * 100)|round(1) }}%</td></tr>
    </tbody>
  </table>

  <h3 class="subsection">C. Per-Repository Evidence Breakdown</h3>
  <table class="appendix-table">
    <thead><tr><th>Repository</th><th>Total</th><th>Source-PR exposure</th><th>Independent/corpus signal</th><th>Rate</th></tr></thead>
    <tbody>
      {% for repo in repo_data %}
      <tr><td>{{ repo.repo }}</td><td>{{ repo.total }}</td><td>{{ repo.source }}</td><td>{{ repo.signal }}</td><td>{{ repo.rate }}%</td></tr>
      {% endfor %}
    </tbody>
  </table>

  <h3 class="subsection">D. Not Evaluated</h3>
  {% if is_solution_audit %}
  <p>{{ s.instances_not_evaluated if s.instances_not_evaluated else 0 }} instance(s) could not be evaluated. In solution mode, absence of a source PR diff is expected and is not counted as unevaluated.</p>
  {% else %}
  <p>{{ s.instances_not_evaluated if s.instances_not_evaluated else 0 }} instance(s) could not be evaluated (PR diff unavailable: deleted, private, or rate-limited). Counted in the total, excluded from exposure calculations.</p>
  {% endif %}
</section>

<!-- ═══ Footer ═══ -->
<footer class="paper-footer">
  Generated by AgentSeal v{{ report.config.agentseal_version }} · {{ generated_at }}<br>
  Detection/NLG are deterministic for fixed inputs, local artifacts, cache state, and live-source responses.<br>
  AgentSeal measures data availability, not model memorization.
</footer>

</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def write_markdown(report: AuditReport, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Guard: empty report
    if not report.instance_risks:
        out.write_text("# Empty Report\n\nNo instances were audited. The benchmark may be empty or all instances failed to load.\n", encoding="utf-8")
        return out

    env = Environment(autoescape=True, extensions=[])
    env.filters["pct"] = lambda n, total: _safe_pct(n, total)
    template = env.from_string(_MD_TEMPLATE)

    s = report.summary
    total = s.total_instances or 1

    # Compute aggregates
    ind_instances = [ir for ir in report.instance_risks if ir.independent_hits > 0]
    ind_count = len(ind_instances)
    link_ok_count = len(_independent_clickable_instance_ids(report))
    ind_rate = (ind_count / total * 100) if total else 0

    m1_hits = sum(1 for ir in report.instance_risks if "M1:" in ir.snippet and "M1:0/" not in ir.snippet)
    m2_hits = sum(1 for ir in report.instance_risks if "M2:" in ir.snippet and "M2:0" not in ir.snippet)
    has_consensus = bool(m1_hits or m2_hits)

    repo_data = _per_repo_signal_data(report)

    # Generate narrative
    finding_sentence = _generate_finding_sentence(report)
    frame_paragraph = _generate_frame_paragraph(report)
    sample_scope_note = _generate_sample_scope_note(report)
    frame_paragraph_html_free = re.sub(r'<[^>]+>', '', frame_paragraph)
    findings = _generate_findings_list(report)
    evidence_rows = _get_independent_evidence_rows(report)

    # Token check
    from .github_auth import get_token as _get_central_token
    token_used = bool(_get_central_token())

    md = template.render(
        report=report, s=s, total=total,
        tagline="AgentSeal — deterministic contamination auditor for AI agent benchmarks",
        generated_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
        finding_sentence=finding_sentence,
        sample_scope_note=sample_scope_note,
        frame_paragraph_html_free=frame_paragraph_html_free,
        findings=findings,
        evidence_rows=evidence_rows,
        ind_rate_md=f"{ind_rate:.1f}",
        ind_count_md=ind_count,
        is_pro_audit=_is_pro_audit(report),
        is_solution_audit=_is_solution_audit(report),
        has_consensus=has_consensus,
        repo_data=repo_data,
        token_used=token_used,
    )
    md = _sanitize_md_uris(md)
    out.write_text(md, encoding="utf-8")
    return out


def write_html(report: AuditReport, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Guard: empty report
    if not report.instance_risks:
        out.write_text("<html><body><h1>Empty Report</h1><p>No instances were audited. The benchmark may be empty or all instances failed to load.</p></body></html>", encoding="utf-8")
        return out

    env = Environment(autoescape=True, extensions=[])
    env.filters["pct"] = lambda n, total: _safe_pct(n, total)
    template = env.from_string(_HTML_TEMPLATE)

    s = report.summary
    total = s.total_instances or 1

    # Compute aggregates
    ind_instances = [ir for ir in report.instance_risks if ir.independent_hits > 0]
    ind_count = len(ind_instances)
    link_ok_count = len(_independent_clickable_instance_ids(report))
    ind_rate = (ind_count / total * 100) if total else 0

    m1_hits = sum(1 for ir in report.instance_risks if "M1:" in ir.snippet and "M1:0/" not in ir.snippet)
    m2_hits = sum(1 for ir in report.instance_risks if "M2:" in ir.snippet and "M2:0" not in ir.snippet)
    has_consensus = bool(m1_hits or m2_hits)

    repo_data = _per_repo_signal_data(report)

    from .narrative_gen import (
        _generate_finding_sentence as nlg_finding,
        _generate_scope_paragraph as nlg_scope,
        _determine_findings as nlg_findings,
        _generate_limitations as nlg_limitations,
        _generate_recommendations as nlg_recs,
        _generate_instance_narrative as nlg_instance,
        _generate_repo_narrative as nlg_repo,
    )

    finding_sentence = nlg_finding(report)
    frame_paragraph = nlg_scope(report)
    sample_scope_note = _generate_sample_scope_note(report)
    findings = nlg_findings(report)
    evidence_rows = _get_independent_evidence_rows(report)
    limitations = nlg_limitations(report)
    recommendations = nlg_recs(report)

    # Generate per-instance deep dives for top 10 most contaminated
    ev_by_instance = {}
    for ev in report.evidence:
        ev_by_instance.setdefault(ev.instance_id, []).append(ev)
    _risk_weight = {"critical": 4, "high": 3, "medium": 2, "low": 1, "clean": 0}
    top_instances = sorted(
        report.instance_risks,
        key=lambda ir: (
            -getattr(ir, "independent_hits", 0),
            -getattr(ir, "codeseal_matches", 0),
            -int(getattr(ir, "repo_in_training_corpus", False)),
            -_risk_weight.get(getattr(getattr(ir, "risk", "clean"), "value", str(getattr(ir, "risk", "clean"))), 0),
        ),
    )[:10]
    instance_narratives = [nlg_instance(ir, ev_by_instance.get(ir.instance_id, [])) for ir in top_instances]

    # Generate per-repo narratives for top 10 repos
    repo_narratives = []
    for repo_row in repo_data[:10]:
        repo_name = str(repo_row["repo"])
        repo_instances = [ir for ir in report.instance_risks if ir.repo == repo_name]
        repo_narratives.append(nlg_repo(repo_name, repo_instances))

    # Token check
    from .github_auth import get_token as _get_central_token
    token_used = bool(_get_central_token())

    # Model info
    model_name = getattr(report.config, 'model_name', 'stack-v2')
    model_cutoff = getattr(report.config, 'model_cutoff', '2024-03-15')

    ind_instances_list = [ir for ir in report.instance_risks if ir.independent_hits > 0]
    temporal_pre_ind = sum(1 for ir in ind_instances_list if ir.merge_date and ir.merge_date != "—" and ir.merge_date < model_cutoff)
    temporal_post_ind = sum(1 for ir in ind_instances_list if ir.merge_date and ir.merge_date != "—" and ir.merge_date >= model_cutoff)
    temporal_pre_ind_pct = round(temporal_pre_ind / total * 100) if total else 0

    # Risk distribution donut (conic-gradient degrees)
    risk_crit_deg = round(s.critical_count / total * 360) if total else 0
    risk_high_deg = risk_crit_deg + round(s.high_count / total * 360) if total else 0
    risk_med_deg = risk_high_deg + round(s.medium_count / total * 360) if total else 0
    risk_low_deg = risk_med_deg + round(s.low_count / total * 360) if total else 0

    # Temporal alignment
    cutoff = model_cutoff
    temporal_pre = sum(1 for ir in report.instance_risks if ir.merge_date and ir.merge_date != "—" and ir.merge_date < cutoff)
    temporal_post = sum(1 for ir in report.instance_risks if ir.merge_date and ir.merge_date != "—" and ir.merge_date >= cutoff)
    temporal_total = temporal_pre + temporal_post
    temporal_pre_pct = round(temporal_pre / temporal_total * 100) if temporal_total else 0
    temporal_post_pct = round(temporal_post / temporal_total * 100) if temporal_total else 0

    # Independent hits histogram
    ind_hits_list = [ir.independent_hits for ir in report.instance_risks if ir.independent_hits > 0]
    hist_bins = [
        ("1-5", 0, "#ff8c42"),
        ("6-10", 0, "#ff7a45"),
        ("11-15", 0, "#ff6b3d"),
        ("16-20", 0, "#ff5c35"),
        ("21-25", 0, "#ff4d2d"),
        ("26-30", 0, "#ff3e25"),
        ("31+", 0, "#ff2f1d"),
    ]
    for h in ind_hits_list:
        if h <= 5: hist_bins[0] = (hist_bins[0][0], hist_bins[0][1] + 1, hist_bins[0][2])
        elif h <= 10: hist_bins[1] = (hist_bins[1][0], hist_bins[1][1] + 1, hist_bins[1][2])
        elif h <= 15: hist_bins[2] = (hist_bins[2][0], hist_bins[2][1] + 1, hist_bins[2][2])
        elif h <= 20: hist_bins[3] = (hist_bins[3][0], hist_bins[3][1] + 1, hist_bins[3][2])
        elif h <= 25: hist_bins[4] = (hist_bins[4][0], hist_bins[4][1] + 1, hist_bins[4][2])
        elif h <= 30: hist_bins[5] = (hist_bins[5][0], hist_bins[5][1] + 1, hist_bins[5][2])
        else: hist_bins[6] = (hist_bins[6][0], hist_bins[6][1] + 1, hist_bins[6][2])
    max_count = max((b[1] for b in hist_bins), default=1)
    hist_data = [{"label": b[0], "count": b[1], "pct": round(b[1] / max_count * 100) if max_count else 0, "color": b[2]} for b in hist_bins]

    html = template.render(
        report=report, s=s, total=total,
        tagline="AgentSeal — deterministic contamination auditor for AI agent benchmarks",
        generated_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
        finding_sentence=finding_sentence,
        sample_scope_note=sample_scope_note,
        frame_paragraph=frame_paragraph,
        findings=findings,
        evidence_rows=evidence_rows,
        limitations=limitations,
        recommendations=recommendations,
        instance_narratives=instance_narratives,
        repo_narratives=repo_narratives,
        ind_rate_html=f"{ind_rate:.0f}",
        ind_count_html=ind_count,
        is_pro_audit=_is_pro_audit(report),
        is_solution_audit=_is_solution_audit(report),
        has_consensus=has_consensus,
        repo_data=repo_data,
        token_used=token_used,
        model_name=model_name,
        model_cutoff=model_cutoff,
        temporal_pre_ind=temporal_pre_ind,
        temporal_post_ind=temporal_post_ind,
        temporal_pre_ind_pct=temporal_pre_ind_pct,
        risk_crit_deg=risk_crit_deg,
        risk_high_deg=risk_high_deg,
        risk_med_deg=risk_med_deg,
        risk_low_deg=risk_low_deg,
        temporal_pre=temporal_pre,
        temporal_post=temporal_post,
        temporal_pre_pct=temporal_pre_pct,
        temporal_post_pct=temporal_post_pct,
        hist_data=hist_data,
    )
    out.write_text(html, encoding="utf-8")
    return out


def _sanitize_md_uris(md: str) -> str:
    """Neutralize dangerous URI schemes in Markdown link/image syntax."""
    md = re.sub(r'\]\((javascript|data|vbscript):', '](blocked:', md, flags=re.IGNORECASE)
    return md


def open_in_browser(path) -> bool:
    """Open a file in the system default browser."""
    import webbrowser
    try:
        return webbrowser.open(f"file://{Path(path).resolve()}")
    except Exception:
        return False


__all__ = ["read_json", "write_html", "write_json", "write_markdown", "open_in_browser"]
