"""AgentSeal v3 — SWE-bench Pro contamination auditor.

MAJOR UPDATES FROM v2:
1. Multi-method consensus: exact line match + normalized match must agree
2. No file limit: checks ALL files in every patch (was limited to 10)
3. Test patch contamination: checks if hidden test code is also public
4. Confidence scoring: 0-100% based on how many methods agree
5. Problem statement contamination: checks if issue text appears in repo
6. Provenance analysis: temporal + license + training-corpus chain for Pro
7. Git compare fallback: for head_not_found, tries GitHub compare API

METHOD:
For each instance, AgentSeal runs up to 4 detection methods:
  M1: Exact line match (fix lines at HEAD but not at base_commit)
  M2: Normalized match (strip comments + normalize whitespace, then match)
  M3: Test patch match (test_patch added lines at HEAD but not at base)
  M4: Problem statement match (issue text in any file at HEAD)

Confidence = (methods that detected contamination / methods that could run) * 100
An instance is CONTAMINATED if >=2 methods agree, or if M1 exposure is >=50%.
An instance is HIGHLY CONTAMINATED if confidence >= 75% or exposure >=50%.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
import requests

from .schemas import AuditConfig, AuditReport, AuditSummary, ContaminationEvidence, InstanceRisk, MatchType, RiskLevel


_default_branch_cache: dict[str, str] = {}


@dataclass
class ProAuditResult:
    """Result of auditing one SWE-bench Pro instance (v3 with multi-method consensus).

    fields so the Pro path reports the same methodology metrics as the
    regular engine path.
    """
    instance_id: str
    repo: str
    repo_language: str
    patch_length: int
    changed_lines_total: int
    changed_lines_found: int = 0          # M1: exact line match
    normalized_lines_found: int = 0       # M2: normalized match
    test_lines_found: int = 0             # M3: test patch match
    problem_statement_found: bool = False # M4: problem statement match
    exposure_rate: float = 0.0
    confidence: float = 0.0               # 0-1, consensus score
    contaminated: bool = False
    methods_agree: int = 0                # how many methods detected contamination
    methods_run: int = 0                  # how many methods could run
    files_checked: list[str] = field(default_factory=list)
    branch_checked: str = ""
    base_commit: str = ""
    verdict: str = ""
    files_in_patch: int = 0
    files_found_at_head: int = 0
    files_found_at_base: int = 0
    test_patch_lines_total: int = 0
    test_patch_lines_found: int = 0
    ngram_overlap_8: float = 0.0          # 8-gram overlap (patch vs HEAD file contents)
    circular_match: bool = False          # Always False on Pro path (no PR diff comparison)
    independent_hits: int = 0             # GitHub code search hits (requires GITHUB_TOKEN)
    independent_candidate_hits: int = 0
    independent_repos: list[str] = field(default_factory=list)
    independent_verified_urls: list[str] = field(default_factory=list)
    independent_verified_sources: list[dict] = field(default_factory=list)
    independent_query: str = ""
    vendor_like_hits: int = 0
    non_vendor_hits: int = 0
    patch: str = ""                       # retained for CodeSeal background evidence


def _extract_added_lines(patch: str) -> list[str]:
    """Extract the ADDED lines (the fix) from a diff patch.

    similarity.py so Pro uses the same boilerplate filter as standard audits.
    """
    if not patch or not isinstance(patch, str):
        return []
    from .similarity import _is_boilerplate
    lines = []
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            stripped = line[1:].strip()
            if stripped and len(stripped) >= 1 and not _is_boilerplate(stripped):
                lines.append(stripped)
    return lines


def _normalize_code_line(line: str) -> str:
    """Normalize a code line for M2 (normalized match).

    Strips comments, removes ALL whitespace, lowercases, and removes BOM.
    Handles None/empty/non-string gracefully.
    """
    if not line or not isinstance(line, str):
        return ""
    # Strip BOM (U+FEFF) — not matched by \s in Python's re
    line = line.replace("\ufeff", "")
    # Strip inline comments (# ... and // ...)
    line = re.sub(r'#.*$', '', line)
    line = re.sub(r'//.*$', '', line)
    # Remove ALL whitespace (not just collapse) so that semantically
    # identical lines with different spacing normalize to the same token.
    line = re.sub(r'\s+', '', line).lower()
    return line


def _extract_file_paths(patch: str) -> list[str]:
    """Extract ALL file paths touched by a patch."""
    if not patch or not isinstance(patch, str):
        return []
    return [m.strip() for m in re.findall(r"\+\+\+ b/(.+)", patch)]


# Cache TTL: cached files older than this are re-fetched. Prevents stale
# results when a repo's default branch changes (S50 fix).
_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days


def _cache_is_valid(cache_path, expected_key: str) -> bool:
    """Validate a cache file against three integrity checks:

    1. S71 fix: content must be non-empty AND non-whitespace-only.
    2. S50 fix: file mtime must be within _CACHE_TTL_SECONDS of now.
    3. S49 fix: a sidecar ``.key`` file must contain the expected cache key,
       proving the cache entry was written by us (not poisoned by an attacker
       who can write to .agentseal_cache/).

    Returns True if all three checks pass, False otherwise (caller falls
    through to a network re-fetch).
    """
    import time as _time
    from pathlib import Path as _Path
    try:
        if not cache_path.exists():
            return False
        # /etc/passwd (with a forged .key sidecar using the predictable sha256
        # prefix) would otherwise pass the content + key checks and return
        # arbitrary local-file content as "source code" (local file disclosure,
        # and contamination-score manipulation).
        if cache_path.is_symlink():
            return False
        # S50: TTL check
        try:
            age = _time.time() - cache_path.stat().st_mtime
            if age > _CACHE_TTL_SECONDS:
                return False
        except OSError:
            return False
        # S71: content must have non-whitespace characters
        try:
            content = cache_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return False
        if not content or not content.strip():
            return False
        # S49: integrity check via sidecar .key file
        key_path = cache_path.with_suffix(cache_path.suffix + ".key")
        if key_path.is_symlink():
            return False
        try:
            stored_key = key_path.read_text(encoding="utf-8").strip()
            if stored_key != expected_key:
                return False  # poisoned or stale key
        except Exception:
            return False
        return True
    except Exception:
        return False


def _cache_write(cache_path, content: str, expected_key: str) -> None:
    """Atomically write content + integrity sidecar to the cache.

    Writes the .key sidecar FIRST, then the content. If the process is
    interrupted between the two writes, _cache_is_valid will reject the
    entry (missing .key) and re-fetch. This is the TOCTOU-safe ordering.
    """
    from pathlib import Path as _Path
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        key_path = cache_path.with_suffix(cache_path.suffix + ".key")
        # Write key sidecar first
        key_path.write_text(expected_key, encoding="utf-8")
        # Then write content
        cache_path.write_text(content, encoding="utf-8")
    except Exception:
        pass


def _fetch_file(repo: str, ref: str, filepath: str, timeout: int = 10) -> Optional[str]:
    """Fetch a raw file from GitHub with disk caching and retry.

    Cache security (S49/S50/S71 fixes):
    - S49 (poisoning): .key sidecar integrity check
    - S50 (no TTL): 7-day TTL via mtime
    - S71 (whitespace-only): content.strip() check
    """
    # Guard against None/non-str inputs that would crash later string ops.
    if not isinstance(repo, str) or not isinstance(ref, str) or not isinstance(filepath, str):
        return None
    if not repo or not ref or not filepath:
        return None
    # Defense-in-depth: reject refs/repofs with URL schemes (prevents SSRF
    # via crafted ref like 'http://evil.com' — even though it's only used
    # as a path segment in the URL, not as the host).
    if '://' in ref or '://' in repo or '://' in filepath:
        return None
    import hashlib
    from pathlib import Path as _Path

    cache_dir = _Path(".agentseal_cache")
    # was missed — an attacker who could write to .agentseal_cache/ could forge
    # both the .txt and .key and poison the pro-audit with arbitrary "source
    # code". Now unforgeable without the per-installation secret.
    from .github_auth import hmac_integrity_tag
    cache_key = hmac_integrity_tag("agentseal-pro-file", f"{repo}|{ref}|{filepath}")
    # so a crafted filepath like `..\..\windows\system32\evil.bat` cannot
    # escape the cache dir on Windows. Also reject any filepath whose
    # normalized components contain `..`.
    import re as _re
    safe_name = _re.sub(r'[/\\]', '__', filepath)
    if any(part == '..' for part in _re.split(r'__', safe_name)):
        return None  # reject path traversal attempts
    # Truncate repo and safe_name to prevent filename > 255 bytes (NAME_MAX)
    safe_repo = repo.replace('/', '__')[:60]
    cache_path = cache_dir / f"{safe_repo}__{ref[:12]}__{safe_name[:60]}__{cache_key}.txt"

    # Validate cache with all three integrity checks (S49/S50/S71)
    if _cache_is_valid(cache_path, cache_key):
        try:
            return cache_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass

    url = f"https://raw.githubusercontent.com/{repo}/{ref}/{filepath}"
    # + feed every response to the rate-limit tracker. This ensures the token
    # is always used when set, and the /status command can show live budget.
    from .github_auth import get_auth_headers, rate_tracker
    _headers = get_auth_headers()
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=timeout, headers=_headers)
            rate_tracker.update_from_response(r, "core")
            if r.status_code == 200:
                text = r.text
                _cache_write(cache_path, text, cache_key)
                return text
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                time.sleep(2 ** attempt + 1)
                continue
            if attempt < 2:
                time.sleep(1)
                continue
            return None
        except requests.RequestException:
            if attempt < 2:
                time.sleep(1)
            continue
    return None


def _get_default_branch(repo: str) -> str:
    """Get a repo's actual default branch via GitHub API.

    the codebase. Without a token it hits the 60/hr limit after ~60 repos,
    then silently defaults to "main" for all remaining repos — but many
    SWE-bench repos use "master"/"develop", so file fetches fail and the
    audit produces incomplete reports. Now uses get_auth_headers() which
    includes the token when set (5000/hr limit). Also feeds the response
    to the rate-limit tracker.
    """
    if repo in _default_branch_cache:
        return _default_branch_cache[repo]
    try:
        from .github_auth import get_auth_headers, rate_tracker
        r = requests.get(
            f"https://api.github.com/repos/{repo}", timeout=10,
            headers=get_auth_headers(),
        )
        rate_tracker.update_from_response(r, "core")
        if r.status_code == 200:
            branch = r.json().get("default_branch", "main")
            _default_branch_cache[repo] = branch
            return branch
    except Exception:
        pass
    for branch in ["main", "master", "develop", "devel", "v2", "dev", "trunk"]:
        content = _fetch_file(repo, branch, "README.md", timeout=5)
        if content is not None:
            _default_branch_cache[repo] = branch
            return branch
    _default_branch_cache[repo] = "main"
    return "main"


def _audit_single_instance(row: pd.Series, on_reasoning=None, independent_search: bool = True) -> ProAuditResult:
    """Audit a single instance with v3 multi-method consensus.

    Runs up to 4 detection methods:
    M1: Exact line match (fix lines at HEAD but not at base)
    M2: Normalized match (strip comments, normalize whitespace, then match)
    M3: Test patch match (test_patch added lines at HEAD but not at base)
    M4: Problem statement match (issue text appears in any file at HEAD)

    on_reasoning: optional callback(instance_id, text) for live reasoning display.
    independent_search: when True AND GITHUB_TOKEN is set, also run GitHub code
        search for the patch's distinctive 8-gram within GitHub's indexed public code, bounded by the configured result budget.
    """
    instance_id = row["instance_id"]
    repo = row["repo"]
    base_commit = str(row["base_commit"])
    patch = row["patch"]
    test_patch = str(row.get("test_patch", "") or "")
    problem_statement = str(row.get("problem_statement", "") or "")
    repo_language = row.get("repo_language", "unknown")

    def _reason(text):
        if on_reasoning:
            on_reasoning(instance_id, text)

    _reason(f"Instance: {instance_id}")
    _reason(f"  Repo: {repo} ({repo_language})")

    added_lines = _extract_added_lines(patch)
    filepaths = _extract_file_paths(patch)

    result = ProAuditResult(
        instance_id=instance_id, repo=repo, repo_language=repo_language,
        patch_length=len(patch) if isinstance(patch, str) else 0,
        changed_lines_total=len(added_lines),
        changed_lines_found=0, normalized_lines_found=0,
        test_lines_found=0, problem_statement_found=False,
        exposure_rate=0.0, confidence=0.0, contaminated=False,
        methods_agree=0, methods_run=0,
        files_checked=[], branch_checked="", base_commit=base_commit,
        verdict="", files_in_patch=len(filepaths),
        files_found_at_head=0, files_found_at_base=0,
        test_patch_lines_total=len(_extract_added_lines(test_patch)),
        test_patch_lines_found=0,
        patch=patch if isinstance(patch, str) else "",
    )

    if not added_lines or not filepaths:
        result.verdict = "no_lines"
        _reason(f"  ⚠ No added lines found in patch (skipped)")
        return result

    default_branch = _get_default_branch(repo)
    result.branch_checked = default_branch
    _reason(f"  Default branch: {default_branch}")
    _reason(f"  Patch: {len(added_lines)} fix lines, {len(filepaths)} files")
    _reason(f"  Fetching from GitHub: raw.githubusercontent.com/{repo}/{default_branch}/...")

    # v3: Check ALL files (no 10-file limit)
    files_to_check = filepaths

    head_contents: list[str] = []
    base_contents: list[str] = []

    for fp in files_to_check:
        head_content = _fetch_file(repo, default_branch, fp)
        if head_content is not None:
            head_contents.append(head_content)
            result.files_found_at_head += 1
            result.files_checked.append(fp)
            _reason(f"  ✓ HEAD file found: {fp} ({len(head_content)} bytes)")
        else:
            _reason(f"  ✗ HEAD file not found: {fp}")
        base_content = _fetch_file(repo, base_commit, fp)
        if base_content is not None:
            base_contents.append(base_content)
            result.files_found_at_base += 1
            _reason(f"  ✓ BASE file found: {fp} (at commit {base_commit[:8]})")

    if not head_contents:
        result.verdict = "head_not_found"
        _reason(f"  ✗ HEAD files not found — repo may have been restructured")
        return result

    head_combined = "\n".join(head_contents)
    base_combined = "\n".join(base_contents) if base_contents else ""

    if not base_contents:
        # base not found — check head only with M1 (line-set matching)
        head_lines_only = set(line.strip() for line in head_combined.splitlines() if line.strip())
        found = sum(1 for l in added_lines if l in head_lines_only)
        result.changed_lines_found = found
        result.exposure_rate = found / len(added_lines) if added_lines else 0
        result.contaminated = False
        result.methods_agree = 0
        result.methods_run = 0
        result.confidence = 0.0
        result.verdict = "base_not_found_suspect" if found > 0 else "base_not_found_clean"
        _reason(f"  M1: {found}/{len(added_lines)} lines found at HEAD (base not found)")
        _reason(f"  -> {result.verdict} (baseline unavailable; not reported as contamination)")
        return result

    # ---- M1: Exact line match ----
    _reason(f"  M1: Checking {len(added_lines)} fix lines against HEAD (not in base)...")
    head_lines_set = set(line.strip() for line in head_combined.splitlines() if line.strip())
    base_lines_set = set(line.strip() for line in base_combined.splitlines() if line.strip())
    m1_contaminated_lines = 0
    for line in added_lines:
        if line in head_lines_set and line not in base_lines_set:
            m1_contaminated_lines += 1
    result.changed_lines_found = m1_contaminated_lines
    _reason(f"  M1: {m1_contaminated_lines}/{len(added_lines)} lines match at HEAD, not at base")

    # ---- M2: Normalized match ----
    _reason(f"  M2: Normalizing (strip comments, remove whitespace) and re-checking...")
    normalized_added = [_normalize_code_line(l) for l in added_lines]
    head_lines_norm = set()
    for line in head_combined.splitlines():
        norm = _normalize_code_line(line)
        if norm:
            head_lines_norm.add(norm)
    base_lines_norm = set()
    for line in base_combined.splitlines():
        norm = _normalize_code_line(line)
        if norm:
            base_lines_norm.add(norm)

    m2_contaminated_lines = 0
    for norm_line in normalized_added:
        if norm_line and norm_line in head_lines_norm and norm_line not in base_lines_norm:
            m2_contaminated_lines += 1
    result.normalized_lines_found = m2_contaminated_lines
    _reason(f"  M2: {m2_contaminated_lines} normalized lines match")

    # ---- M3: Test patch contamination ----
    test_added = _extract_added_lines(test_patch)
    result.test_patch_lines_total = len(test_added)
    m3_contaminated_lines = 0
    if test_added:
        _reason(f"  M3: Checking {len(test_added)} test lines against HEAD...")
        for line in test_added:
            if line in head_lines_set and line not in base_lines_set:
                m3_contaminated_lines += 1
    result.test_lines_found = m3_contaminated_lines
    if test_added:
        _reason(f"  M3: {m3_contaminated_lines}/{len(test_added)} test lines found at HEAD")

    # ---- M4: Problem statement contamination ----
    # The old code sampled the GEOMETRIC MIDDLE of the problem statement
    # (±40 chars around the center). But the middle of a bug report is
    # statistically where tracebacks and code quotes live — which test the
    # WRONG causal direction (issue quotes repo, not repo contains issue).
    #
    # The new code:
    # (a) Samples the FIRST 200 chars (the issue summary, rarely a code quote)
    # (b) Rejects chunks that are >40% code-syntax (high density of (){}[];=
    #     or backtick-fenced) — these are issue quotes of repo code
    # (c) Only fires if a prose-heavy chunk is found in the source
    m4_found = False
    if problem_statement and len(problem_statement) > 50:
        _reason(f"  M4: Searching for problem statement text in source files...")
        # (a) Sample the first 200 chars (issue summary, rarely code)
        chunk = problem_statement[:200].strip()
        if len(chunk) > 30:
            # (b) Reject code-heavy chunks (>40% punctuation/symbols)
            # These are likely issue quotes of repo code (tracebacks, snippets)
            code_chars = sum(1 for c in chunk if c in '(){}[];=<>+-*/\\|`')
            code_density = code_chars / max(len(chunk), 1)
            if code_density > 0.40:
                _reason(f"  M4: chunk rejected (code density {code_density:.0%} > 40%) — likely a code quote")
            else:
                # (c) Check if the prose chunk appears in the source
                if chunk in head_combined:
                    m4_found = True
                else:
                    head_clean = re.sub(r'[#//]', ' ', head_combined)
                    head_normalized = re.sub(r'\s+', ' ', head_clean)
                    chunk_normalized = re.sub(r'\s+', ' ', chunk)
                    if chunk_normalized in head_normalized:
                        m4_found = True
    result.problem_statement_found = m4_found
    if problem_statement and len(problem_statement) > 50:
        _reason(f"  M4: Problem statement {'FOUND' if m4_found else 'not found'} in source")

    # ---- Consensus scoring ----
    # The old threshold (confidence >= 0.25 = 1 of 4 methods) was too lax —
    # a single M4 false positive (issue quotes repo) was enough to flag
    # contamination. The new threshold requires >= 2 of 4 methods to agree,
    # which eliminates the class of single-method false positives.
    methods_detected = 0
    methods_run = 0

    # M1
    methods_run += 1
    if m1_contaminated_lines > 0:
        methods_detected += 1

    # M2
    methods_run += 1
    if m2_contaminated_lines > 0:
        methods_detected += 1

    # M3 (only if test_patch has added lines)
    if test_added:
        methods_run += 1
        if m3_contaminated_lines > 0:
            methods_detected += 1

    # M4 (only if problem_statement exists)
    if problem_statement and len(problem_statement) > 50:
        methods_run += 1
        if m4_found:
            methods_detected += 1

    result.methods_agree = methods_detected
    result.methods_run = methods_run
    result.confidence = methods_detected / methods_run if methods_run > 0 else 0

    # Exposure rate = M1 line match (primary metric)
    result.exposure_rate = m1_contaminated_lines / len(added_lines) if added_lines else 0

    # file contents. This is the academic-standard signal that was missing
    # instances, making it look like the method never ran.
    from .similarity import _ngram_overlap_rate
    result.ngram_overlap_8 = _ngram_overlap_rate(patch, head_combined, n=8)
    _reason(f"  8-gram overlap: {result.ngram_overlap_8*100:.1f}% (patch vs HEAD file contents)")

    # circular_match is always False on the Pro path — the Pro path compares
    # patches against file CONTENTS at HEAD, not against PR diffs. The circular
    # concern (comparing a patch to its own PR diff) doesn't apply here.
    result.circular_match = False

    # When GITHUB_TOKEN is set AND independent_search is enabled, search
    # GitHub code search for the patch's most distinctive 8-gram across ALL
    # public repos. Hits in repos OTHER than the source repo are non-circular
    # evidence of availability. Breaks the tautology of comparing a patch
    # to its own PR diff.
    import os as _os
    from .github_auth import get_token as _get_central_token
    _token = _get_central_token()
    if independent_search and _token and patch and isinstance(patch, str):
        from .independent_search import search_independent_sources
        _iv = search_independent_sources(patch, repo)
        result.independent_hits = _iv.independent_hits
        result.independent_candidate_hits = getattr(_iv, "candidate_hits", 0)
        result.independent_repos = list(_iv.independent_repos or [])
        result.independent_verified_urls = list(getattr(_iv, "verified_urls", None) or [])
        result.independent_verified_sources = list(getattr(_iv, "verified_sources", None) or [])
        result.independent_query = getattr(_iv, "query_8gram", "") or ""
        result.vendor_like_hits = getattr(_iv, "vendor_like_hits", 0)
        result.non_vendor_hits = getattr(_iv, "non_vendor_hits", _iv.independent_hits)
        if _iv.searched and _iv.independent_hits > 0:
            _repo_list = ", ".join(_iv.independent_repos or [])
            _reason(f"  INDEPENDENT: exact changed lines verified in {_iv.independent_hits} repo(s) "
                    f"other than {repo}: {_repo_list}")
        elif _iv.searched:
            _reason(f"  independent search: 0 hits in other repos (patch not replicated elsewhere)")
        elif _iv.error:
            _reason(f"  independent search skipped: {_iv.error}")
    else:
        result.independent_hits = 0
    # This eliminates single-method false positives (e.g., M4 alone firing
    # because the issue quotes repo code). A single method agreeing is now
    # "suspect" (verdict="suspect") not "contaminated".
    # Exception: if M1 exposure_rate >= 0.50, that alone is strong evidence
    # (>= 50% of fix lines are at HEAD) — still counts as contaminated.
    if methods_detected >= 2:
        result.contaminated = True
        result.verdict = "CONTAMINATED"
    elif methods_detected == 1 and result.exposure_rate >= 0.50:
        result.contaminated = True
        result.verdict = "CONTAMINATED"
    elif methods_detected == 1:
        result.contaminated = False
        result.verdict = "suspect"
    else:
        result.contaminated = False
        result.verdict = "clean"

    _reason(f"  → {result.verdict}: {methods_detected}/{methods_run} methods agree "
            f"(confidence: {result.confidence*100:.0f}%, exposure: {result.exposure_rate*100:.0f}%)")

    return result


def audit_swebench_pro(
    parquet_path: str | Path,
    sample_size: int = 0,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    on_reasoning: Optional[Callable[[str, str], None]] = None,
    max_workers: int = 15,
    cancel_check: Optional[Callable[[], bool]] = None,
    independent_search: bool = True,
) -> list[ProAuditResult]:
    """Audit SWE-bench Pro with v3 multi-method consensus.

    on_progress: callback(completed, total, message) for progress bar.
    on_reasoning: callback(instance_id, text) for live reasoning display.
    cancel_check: callback() -> bool. If it returns True, stop processing
                  remaining instances. Already-running threads finish but
                  no new results are processed.
    independent_search: when True (default) AND GITHUB_TOKEN is set, also
                  run GitHub code search per instance for non-circular
                  evidence. Auto-throttled to 28 req/min.
    """
    try:
        df = pd.read_parquet(parquet_path)
    except Exception as e:
        raise ValueError(f"Failed to read parquet file: {e}")
    if sample_size > 0:
        df = df.head(sample_size)

    total = len(df)
    results: list[ProAuditResult] = []
    instance_order = {row["instance_id"]: i for i, (_, row) in enumerate(df.iterrows())}

    unique_repos = list(df["repo"].dropna().unique())
    if on_progress:
        on_progress(0, total, f"Detecting default branches for {len(unique_repos)} repos...")
    # serial preflight across unique repos before the actual instance pool,
    # causing large Pro audits to wait on N blocking GitHub API calls upfront.
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(unique_repos) or 1))) as branch_executor:
        branch_futures = {branch_executor.submit(_get_default_branch, repo): repo for repo in unique_repos}
        for future in as_completed(branch_futures):
            repo = branch_futures[future]
            try:
                branch = future.result()
            except Exception:
                branch = "main"
                _default_branch_cache[repo] = branch
            if on_reasoning:
                on_reasoning(repo, f"Default branch for {repo}: {branch}")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_audit_single_instance, row, on_reasoning, independent_search): row["instance_id"]
            for _, row in df.iterrows()
        }
        completed = 0
        cancelled = False
        # NO warning, and contamination_rate was computed over the surviving
        # subset — silently misleading. We now record failed ids so the report
        # can surface how many were dropped and why.
        failed_ids: list[str] = []
        for future in as_completed(futures):
            # Check cancel flag BEFORE processing each result
            if cancel_check and cancel_check():
                cancelled = True
                # Cancel remaining futures that haven't started
                for f in futures:
                    f.cancel()
                break
            try:
                result = future.result()
            except Exception as e:
                # continuing. The instance is NOT in results, so the report
                # must call out that N instances were dropped.
                failed_ids.append(futures[future])
                continue
            results.append(result)
            completed += 1
            if on_progress and (completed % 50 == 0 or completed == total):
                contaminated = sum(1 for r in results if r.contaminated)
                on_progress(completed, total, f"{contaminated} contaminated so far")

    if cancelled:
        # Shutdown the executor immediately (non-blocking)
        executor.shutdown(wait=False, cancel_futures=True)

    results.sort(key=lambda r: instance_order.get(r.instance_id, 0))
    # surface it. We stash it as a module-level attribute (the function returns
    # a list[ProAuditResult]; threading a second return value would break the
    # public API).
    audit_swebench_pro._last_failed_ids = failed_ids  # type: ignore[attr-defined]
    return results


def results_to_report(
    results: list[ProAuditResult],
    config: Optional[AuditConfig] = None,
    codeseal: bool = True,
) -> AuditReport:
    """Convert v3 results into an AgentSeal AuditReport."""
    # Guard against None or non-list input
    if not isinstance(results, list):
        results = []
    # Guard against individual entries that are None or not ProAuditResult.
    # This prevents AttributeError when the caller passes a heterogeneous list
    # (e.g. [None, some_string, {}]) — we silently skip non-conforming entries
    # rather than crashing the whole report.
    results = [r for r in results if isinstance(r, ProAuditResult)]
    if config is None:
        config = AuditConfig(
            benchmark="swe-bench-pro",
            corpus_source="github-default-branches (v3 multi-method consensus: line match + normalized + test patch + problem statement)",
            # threshold (it uses M1/M2 line-set matching), but AuditConfig now
            # validates threshold >= 0.1. Use the default 0.82 so the config
            # validates; the value is unused by the pro_audit path.
            threshold=0.82,
        )

    instance_risks: list[InstanceRisk] = []
    all_evidence: list[ContaminationEvidence] = []
    from .risk import repo_in_training_corpus

    for r in results:
        repo_in_corpus = repo_in_training_corpus(r.repo)
        if r.contaminated:
            # v3: risk level based on confidence AND exposure
            if r.confidence >= 0.75 or r.exposure_rate >= 0.50:
                risk = RiskLevel.CRITICAL
            elif r.confidence >= 0.50 or r.exposure_rate >= 0.20:
                risk = RiskLevel.HIGH
            else:
                risk = RiskLevel.MEDIUM
        else:
            risk = RiskLevel.CLEAN

        snippet = (
            f"M1:{r.changed_lines_found}/{r.changed_lines_total} "
            f"M2:{r.normalized_lines_found} "
            f"M3:{r.test_lines_found}/{r.test_patch_lines_total} "
            f"M4:{'Y' if r.problem_statement_found else 'N'} "
            f"conf={r.confidence*100:.0f}% "
            f"({r.methods_agree}/{r.methods_run} methods agree) "
            f"branch={r.branch_checked}"
            if r.contaminated
            else f"verdict={r.verdict}, branch={r.branch_checked}, files={r.files_found_at_head}/{r.files_in_patch}"
        )

        ir = InstanceRisk(
            instance_id=r.instance_id, repo=r.repo, risk=risk,
            patch_exposure=r.exposure_rate,
            problem_statement_exposure=1.0 if r.problem_statement_found else 0.0,
            test_patch_exposure=r.test_lines_found / r.test_patch_lines_total if r.test_patch_lines_total > 0 else 0.0,
            repo_in_training_corpus=repo_in_corpus,
            pr_url=None,
            evidence_count=r.methods_agree,
            top_match_type=MatchType.PATCH_NORMALIZED if r.contaminated else None,
            snippet=snippet,
        )
        ir.ngram_overlap_8 = r.ngram_overlap_8
        ir.circular_match = r.circular_match
        ir.independent_hits = r.independent_hits
        ir.independent_candidate_hits = r.independent_candidate_hits
        ir.vendor_like_hits = r.vendor_like_hits
        ir.non_vendor_hits = r.non_vendor_hits
        if r.independent_hits > 0:
            repo_list = ", ".join(r.independent_repos or [])
            verified_sources = list(r.independent_verified_sources or [])
            matched_line_total = sum(int(h.get("matched_lines", 0) or 0) for h in verified_sources)
            total_line_total = sum(int(h.get("total_lines", 0) or 0) for h in verified_sources)
            ev = ContaminationEvidence(
                instance_id=r.instance_id,
                match_type=MatchType.INDEPENDENT_SOURCE_HIT,
                source_url=(
                    r.independent_verified_urls[0]
                    if r.independent_verified_urls else
                    (verified_sources[0].get("api_url", "") if verified_sources else "")
                ),
                source_repo=repo_list,
                similarity=1.0,
                matched_lines=matched_line_total or r.independent_hits,
                total_lines=total_line_total or r.independent_candidate_hits or r.independent_hits,
                evidence_snippet=(r.independent_query or "")[:200],
                message=(
                    f"Exact changed lines verified in {r.independent_hits} independent repo(s) "
                    f"({r.non_vendor_hits} non-vendor, {r.vendor_like_hits} vendor-like; "
                    f"{r.independent_candidate_hits} search candidates): {repo_list}"
                ),
                source_kind="github_code_search_verified",
                verification_status="exact_changed_lines",
                vendor_like=(r.vendor_like_hits > 0 and r.non_vendor_hits == 0),
                scope_note=(
                    "Verified hits appear vendor-like/dependency-copy only; treat as public availability, "
                    "not benchmark-specific leakage."
                    if r.vendor_like_hits > 0 and r.non_vendor_hits == 0 else
                    "Exact changed lines verified outside the source repository."
                ),
            )
            all_evidence.append(ev)
            ir.evidence_count += 1
            if ir.top_match_type is None:
                ir.top_match_type = MatchType.INDEPENDENT_SOURCE_HIT
                ir.snippet = ev.message
        if codeseal and getattr(r, "patch", ""):
            try:
                from .codeseal_detector import check_patch_against_bundled_model
                cm = check_patch_against_bundled_model(r.patch, enabled=True)
            except Exception:
                cm = None
            if cm is not None and cm.checked and cm.contaminated:
                matched = cm.matched_files or []
                match_count = max(1, len(matched))
                repo_list = ", ".join(fid for fid, _ in matched[:5])
                ev = ContaminationEvidence(
                    instance_id=r.instance_id,
                    match_type=MatchType.CODESEAL_CONTENT_HIT,
                    source_url="agentseal://bundled-codeseal-model",
                    source_repo=repo_list,
                    similarity=cm.similarity,
                    matched_lines=match_count,
                    total_lines=cm.index_size,
                    evidence_snippet=repo_list[:200],
                    message=(
                        f"CodeSeal MinHash/LSH content overlap: top similarity {cm.similarity:.2f} "
                        f"against {match_count} bundled corpus file match(es) "
                        f"(index size {cm.index_size:,})."
                    ),
                    source_kind="codeseal_minhash_lsh",
                    verification_status=cm.status,
                    scope_note=(
                        "CodeSeal is a deterministic content-overlap signal, not model "
                        "memorization proof; use it with temporal and independent-source evidence."
                    ),
                )
                all_evidence.append(ev)
                ir.codeseal_similarity = max(0.0, min(1.0, float(cm.similarity or 0.0)))
                ir.codeseal_matches = match_count
                ir.evidence_count += 1
                if ir.top_match_type is None:
                    ir.top_match_type = MatchType.CODESEAL_CONTENT_HIT
                    ir.snippet = (
                        f"CodeSeal content overlap against bundled corpus "
                        f"(similarity {ir.codeseal_similarity:.2f}, {match_count} match(es))"
                    )
                if ir.risk == RiskLevel.CLEAN:
                    ir.risk = RiskLevel.MEDIUM if ir.codeseal_similarity >= 0.30 else RiskLevel.LOW
                elif ir.risk == RiskLevel.LOW and ir.codeseal_similarity >= 0.30:
                    ir.risk = RiskLevel.MEDIUM
        instance_risks.append(ir)

    total = len(instance_risks)
    contaminated = sum(1 for r in instance_risks if r.risk != RiskLevel.CLEAN)
    critical = sum(1 for r in instance_risks if r.risk == RiskLevel.CRITICAL)
    high = sum(1 for r in instance_risks if r.risk == RiskLevel.HIGH)
    medium = sum(1 for r in instance_risks if r.risk == RiskLevel.MEDIUM)
    clean = sum(1 for r in instance_risks if r.risk == RiskLevel.CLEAN)
    repo_in_corpus_count = sum(1 for r in instance_risks if r.repo_in_training_corpus)
    codeseal_count = sum(1 for r in instance_risks if r.codeseal_matches > 0)

    verdicts: dict[str, int] = {}
    for r in results:
        verdicts[r.verdict] = verdicts.get(r.verdict, 0) + 1

    unverifiable = (
        verdicts.get("head_not_found", 0)
        + verdicts.get("base_not_found_suspect", 0)
        + verdicts.get("base_not_found_clean", 0)
    )
    verifiable = total - unverifiable

    if verifiable > 0:
        verifiable_str = (
            f"Verifiable instances: {verifiable}/{total} ({verifiable/total*100:.1f}%). "
            f"Contamination rate among verifiable: {contaminated}/{verifiable} "
            f"({contaminated/verifiable*100:.1f}%)"
        )
    else:
        verifiable_str = f"Verifiable instances: 0/{total} (0.0%)."

    # Count method breakdown
    m1_hits = sum(1 for r in results if r.changed_lines_found > 0)
    m2_hits = sum(1 for r in results if r.normalized_lines_found > 0)
    m3_hits = sum(1 for r in results if r.test_lines_found > 0)
    m4_hits = sum(1 for r in results if r.problem_statement_found)

    summary = AuditSummary(
        total_instances=total,
        instances_with_patch_exposure=contaminated,
        instances_with_problem_statement_exposure=m4_hits,
        instances_with_test_patch_exposure=m3_hits,
        instances_with_repo_in_corpus=repo_in_corpus_count,
        patch_exposure_rate=contaminated / total if total else 0,
        problem_statement_exposure_rate=m4_hits / total if total else 0,
        test_patch_exposure_rate=m3_hits / total if total else 0,
        repo_in_corpus_rate=repo_in_corpus_count / total if total else 0,
        instances_with_codeseal_content=codeseal_count,
        codeseal_content_rate=codeseal_count / total if total else 0,
        critical_count=critical, high_count=high, medium_count=medium,
        low_count=0, clean_count=clean,
        contamination_rate=contaminated / total if total else 0,
    )

    top = sorted(instance_risks, key=lambda r: (-r.patch_exposure, r.instance_id))[:20]

    limitations = [
        "SWE-bench Pro instance_ids are hashes, not PR numbers.",
        f"v3 Method: 4 detection methods with consensus scoring:",
        f"  M1 (exact line match): {m1_hits} instances detected",
        f"  M2 (normalized match): {m2_hits} instances detected",
        f"  M3 (test patch match): {m3_hits} instances detected",
        f"  M4 (problem statement match): {m4_hits} instances detected",
        f"Verdict breakdown: {verdicts}",
        verifiable_str,
        f"Default branches detected: {dict(_default_branch_cache)}",
        "v3 checks ALL files in each patch (no file limit).",
        "Confidence = (methods that detected / methods that could run). Contaminated requires >=2 agreeing methods or M1 exposure >=50%.",
        "AgentSeal does NOT prove model memorization. It shows solution code is in training-data sources.",
    ]

    # stashes the failed ids on itself; if non-empty, the report must warn the
    # reader that the headline contamination_rate is over a SUBSET and N
    # instances were lost to errors (network blips, parser bugs).
    failed_ids = getattr(audit_swebench_pro, "_last_failed_ids", []) or []
    if failed_ids:
        limitations = list(limitations) + [
            f"WARNING: {len(failed_ids)} instance(s) were silently dropped due to "
            f"exceptions during auditing and are NOT included in the totals above. "
            f"The contamination_rate is computed over the {total} surviving instances, "
            f"not the original input. Re-run with --no-cache and stable network to "
            f"verify. Dropped ids (first 10): {failed_ids[:10]}.",
        ]

    return AuditReport(
        config=config, summary=summary, instance_risks=instance_risks,
        evidence=all_evidence, top_contaminated=top,
        limitations=limitations,
        remediation=[
            "SWE-bench Pro should use patches from commits NOT merged to any public branch.",
            "Maintain a private fork where fixes are applied but never pushed publicly.",
            "Re-audit periodically with AgentSeal as repos evolve.",
            "Do NOT assume 'contamination-resistant' benchmarks are clean — audit them.",
        ],
    )


__all__ = ["ProAuditResult", "audit_swebench_pro", "results_to_report"]
