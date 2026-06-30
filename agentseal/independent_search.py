"""Independent-source verification.

BREAKS THE CIRCULARITY: the regular audit path (engine.py) compares the gold
patch against the PR diff it was derived from — a 100% match is guaranteed by
construction. This module searches for the patch's distinctive lines across
ALL of GitHub (not just the source repo), which provides REAL evidence of
availability in independent sources.

How it works:
1. Extract the most distinctive 8-gram from the patch (the rarest token sequence)
2. Query the GitHub code search API for that 8-gram within GitHub's indexed public code, bounded by the configured result budget
3. If hits are found in repos OTHER than the source repo, that's independent
   evidence the patch text is replicated elsewhere on GitHub
4. Report independent_hits: count of verified processed repos (excluding source) containing the patch

The method is deterministic for a fixed API response/cache and fixed inputs. Live
GitHub search indexes and rate limits can change over time. No LLM or model
access is used.

Rate limits: GitHub code search via the REST API requires authentication for
meaningful usage (unauthenticated requests are heavily rate-limited). If
GITHUB_TOKEN is set in the environment, it's used; otherwise the module
degrades gracefully (returns 0 independent_hits with a note).
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import base64
import math
from urllib.parse import quote_plus
from dataclasses import dataclass

import requests

from .similarity import _tokenize_code, _ngrams, _is_boilerplate


# limit (30 req/min for authenticated users). Without this, a 500-instance
# audit would burn through the budget in ~17 seconds and get 403-stormed.
# ThreadPoolExecutor(max_workers=15) — without a lock, 15 threads can read
# _LAST_SEARCH_TIME simultaneously, all see it's old, all fire, and blow
# through the 30/min ceiling in 2 seconds.
# BURST mode. GitHub's code search allows 30 req/min with a rolling 60s
# window. Instead of dripping 1 req every 2.1s, we now:
#   1. Fire up to 28 requests as fast as the network allows (burst)
#   2. Track remaining budget via X-RateLimit-Remaining header
#   3. When budget hits 0, sleep until X-RateLimit-Reset
#   4. Then burst again
# This means the first 28 instances complete in ~5 seconds (network latency
# only), then we wait ~55s for the window to reset. For a 500-instance
# audit: 500/28 ≈ 18 windows × 1 min = ~18 min total (same as before, but
# the first results come back instantly instead of after 60s).
# We CANNOT exceed 30/min — that's GitHub's hardcoded ceiling for /search/code,
# separate from the 5000/hr core API limit. Setting the throttle higher would
# just produce 403 errors.
_SEARCH_LOCK = threading.Lock()
_SEARCH_BUDGET: int = 28  # requests remaining in current window (leave 2 as margin)
_SEARCH_RESET_AT: float = 0.0  # epoch time when budget resets
# Steady-state fallback: if we can't read X-RateLimit headers, drip at 2.1s
_MIN_SEARCH_INTERVAL: float = 2.1

# false positives in GitHub code search. If the 8-gram extraction produces
# a query containing ONLY these tokens, the search is skipped (returns 0
# hits with an error explaining why). This prevents false positives where
# common identifiers like "data", "value", "self" match thousands of repos.
_GENERIC_IDENTIFIER_BLOCKLIST = frozenset({
    "data", "self", "value", "none", "true", "false", "null", "void",
    "config", "model", "result", "default", "settings", "state", "status",
    "error", "message", "return", "import", "class", "def", "init",
    "test", "main", "type", "name", "path", "file", "code", "base",
    "list", "dict", "item", "key", "args", "kwargs", "obj", "var",
    "func", "call", "get", "set", "put", "run", "new", "old",
    "true", "false", "none", "null", "void", "empty", "blank",
    "index", "count", "size", "len", "max", "min", "sum", "avg",
    "start", "end", "begin", "stop", "next", "prev", "first", "last",
    "input", "output", "src", "dst", "tmp", "buf", "ptr", "ref",
    "response", "request", "header", "body", "url", "method",
    "assert", "raise", "except", "finally", "with", "async", "await",
    "print", "log", "warn", "info", "debug", "trace",
})


def _is_code_like_identifier(token: str) -> bool:
    """Return True for identifiers distinctive enough for code search."""
    if not token:
        return False
    tok = token.lower()
    if tok in _GENERIC_IDENTIFIER_BLOCKLIST:
        return False
    if "_" in tok or any(ch.isdigit() for ch in tok):
        return True
    return len(tok) >= 12



@dataclass(frozen=True)
class IndependentVerification:
    """Result of searching for the patch in independent GitHub sources.

    to its own PR diff. Searches ALL of GitHub for the patch's distinctive
    8-gram, then reports how many INDEPENDENT repos (not the source repo)
    contain that text.
    """
    query_8gram: str = ""              # the 8-gram searched
    total_hits: int = 0                # total repos found containing the 8-gram
    candidate_hits: int = 0            # search-hit repos EXCLUDING the source repo
    independent_hits: int = 0          # exact-verified repos EXCLUDING the source repo
    independent_repos: list = None     # exact-verified repos (first 5)
    verified_repos: list = None        # exact-verified repos (first 5)
    verified_sources: list = None      # exact verified file/issue hits with URLs and match counts
    verified_urls: list = None         # exact verified URLs (first 10)
    candidate_repos: list = None       # search-hit repos (first 5)
    vendor_like_hits: int = 0          # exact-verified repos that look like dependency copies
    non_vendor_hits: int = 0           # exact-verified repos not classified as vendored
    vendor_like_repos: list = None     # vendored/dependency-copy repos (first 5)
    verification_mode: str = ""        # exact_changed_lines, candidate_only, etc.
    source_repo: str = ""              # the source repo (excluded from independent_hits)
    searched: bool = False             # did the search actually run?
    error: str = ""                    # if searched=False, why (rate limit, no token, etc.)


def _normalize_patch_for_search(patch: str) -> str:
    """Normalize a patch before 8-gram extraction.

    was reformatted (whitespace, variable renaming) before being committed,
    the original patch text won't match. AgentSeal now NORMALIZES the patch
    before extracting identifiers:

    1. Remove comments (# ... and // ...)
    2. Lowercase all identifiers (so CamelCase → camelcase matches)
    3. Collapse ALL whitespace to single spaces (so indentation changes don't matter)
    4. Replace multiple underscores with single (so __private → _private)

    This catches the most common reformatting: whitespace + casing changes.
    """
    if not patch:
        return ""
    # Remove comments
    lines = []
    for line in patch.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        lines.append(stripped)  # strip leading/trailing whitespace per line
    text = " ".join(lines)
    # Lowercase
    text = text.lower()
    # Collapse all whitespace to single spaces
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'\s*([=+\-*/<>!&|^%,:;(){}[\]])\s*', r'\1', text)
    # Replace multiple underscores with single
    text = re.sub(r'_{2,}', '_', text)
    return text


# patch content, we only search GitHub once and share the result.
_PATCH_SEARCH_CACHE: dict[str, "IndependentVerification"] = {}
_SEARCH_CACHE_DIR = None
_SEARCH_CACHE_TTL = 24 * 3600
_SEARCH_CACHE_VERSION = 4


def _patch_content_hash(patch: str) -> str:
    """Compute a content hash for a patch (for deduplication).

    Two patches with identical normalized content get the same hash,
    so we only search GitHub once for them.
    """
    normalized = _normalize_patch_for_search(patch)
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _extract_most_distinctive_8gram(patch: str) -> str:
    """Extract the most distinctive 8-gram from the patch.

    "Distinctive" = the 8-gram least likely to appear in random code.
    We prefer 8-grams containing identifiers (longer alphanumeric tokens)
    over 8-grams of pure punctuation/keywords.

    reformatted patches (whitespace + casing changes) produce the same
    identifiers as the original, so the search catches them. This is the
    key rigor improvement over StarCoder2's raw-file approach.
    """
    if not patch:
        return ""
    normalized = _normalize_patch_for_search(patch)
    tokens = _tokenize_code(normalized)
    if len(tokens) < 8:
        return ""
    ng = _ngrams(tokens, 8)
    if not ng:
        return ""

    def _identifier_score(ngram_tuple):
        """Score an n-gram by how distinctive its identifiers are."""
        score = 0
        for tok in ngram_tuple:
            if re.match(r"^[a-zA-Z_]\w{4,}$", tok):
                score += len(tok)
            elif re.match(r"^[a-zA-Z_]\w+$", tok):
                score += 2
            # punctuation/keywords: score 0
        return score

    best = max(ng.keys(), key=_identifier_score)
    identifiers = [tok for tok in best if re.match(r"^[a-zA-Z_]\w{3,}$", tok)]
    if len(identifiers) < 2:
        # Fallback: if the best 8-gram has <2 distinctive identifiers, scan
        # ALL 8-grams and return the one with the most identifiers.
        best_count = 0
        best_ids = []
        for ngram_tuple in ng:
            ids = [tok for tok in ngram_tuple if re.match(r"^[a-zA-Z_]\w{3,}$", tok)]
            if len(ids) > best_count:
                best_count = len(ids)
                best_ids = ids
        if best_count < 2:
            return ""  # patch has no distinctive identifiers at all
        identifiers = best_ids

    # If so, the search would produce false positives — skip it.
    non_generic = [t for t in identifiers if _is_code_like_identifier(t)]
    if len(non_generic) < 2:
        return ""  # not enough distinctive identifiers — skip search
    return " ".join(non_generic[:5])  # max 5 non-generic identifiers


def _normalize_line_for_verification(line: str) -> str:
    return re.sub(r"\s+", " ", (line or "").strip())


def _extract_added_line_groups(patch: str) -> list[list[str]]:
    """Extract meaningful added lines grouped by changed file."""
    groups: list[list[str]] = []
    current: list[str] = []
    saw_diff_marker = False
    for line in (patch or "").splitlines():
        if line.startswith("diff --git "):
            saw_diff_marker = True
            if current:
                groups.append(current)
            current = []
            continue
        if line.startswith("@@ ") or line.startswith("---") or line.startswith("+++"):
            saw_diff_marker = True
        if line.startswith("+") and not line.startswith("+++"):
            content = line[1:].strip()
            if content and not _is_boilerplate(content):
                norm = _normalize_line_for_verification(content)
                if norm and norm not in current:
                    current.append(norm)
    if current:
        groups.append(current)
    if not groups and not saw_diff_marker:
        plain_group: list[str] = []
        for line in (patch or "").splitlines():
            content = line.strip()
            if not content or content.startswith(("#", "//")) or content in {"}", "{", ");"}:
                continue
            norm = _normalize_line_for_verification(content)
            if norm and norm not in plain_group:
                plain_group.append(norm)
        if plain_group:
            groups.append(plain_group)
    return groups


def _group_required_matches(group: list[str]) -> int:
    if not group:
        return 999999
    if len(group) == 1:
        return 1 if len(group[0]) >= 40 else 999999
    if len(group) <= 3:
        return 2
    return max(3, math.ceil(len(group) * 0.50))


def _verify_patch_lines_in_blob_with_locations(patch: str, blob: str) -> tuple[bool, int, int, list[int]]:
    """Return whether a changed-line group is present, plus matched line numbers.

    The previous implementation only returned counts. Reports then had to link
    to a replayed GitHub search, which can disappear or mismatch the API query.
    Keeping the matched line numbers lets the caller preserve exact provenance
    for the verified GitHub file that was actually fetched.
    """
    if not patch or not blob:
        return False, 0, 0, []

    line_locations: dict[str, list[int]] = {}
    for idx, line in enumerate(blob.splitlines(), start=1):
        norm = _normalize_line_for_verification(line)
        if norm:
            line_locations.setdefault(norm, []).append(idx)

    best_matched = 0
    best_total = 0
    best_lines: list[int] = []
    for group in _extract_added_line_groups(patch):
        total = len(group)
        matched_lines: list[int] = []
        for line in group:
            locs = line_locations.get(line) or []
            if locs:
                matched_lines.append(locs[0])
        matched = len(matched_lines)
        if matched > best_matched:
            best_matched = matched
            best_total = total
            best_lines = sorted(set(matched_lines))
        if matched >= _group_required_matches(group):
            return True, matched, total, sorted(set(matched_lines))
    return False, best_matched, best_total, best_lines


def _verify_patch_lines_in_blob(patch: str, blob: str) -> tuple[bool, int, int]:
    """Return whether a file-level changed-line group is present in a blob."""
    verified, matched, total, _lines = _verify_patch_lines_in_blob_with_locations(patch, blob)
    return verified, matched, total


def _line_anchor(line_numbers: list[int]) -> str:
    if not line_numbers:
        return ""
    first = min(line_numbers)
    last = max(line_numbers)
    if first == last:
        return f"#L{first}"
    return f"#L{first}-L{last}"


def _code_search_web_url(query: str) -> str:
    """GitHub web search URL matching the API's AND-token query semantics."""
    return f"https://github.com/search?q={quote_plus(query or '')}&type=code"


def _issue_search_web_url(query: str) -> str:
    return f"https://github.com/search?q={quote_plus(chr(34) + (query or '') + chr(34))}&type=issues"


def _build_verified_code_source(item: dict, matched: int, total: int, line_numbers: list[int], source_repo: str) -> dict:
    repo_full = item.get("repository", {}).get("full_name", "") or ""
    html_url = item.get("html_url") or ""
    if html_url and line_numbers and "#L" not in html_url:
        html_url = html_url + _line_anchor(line_numbers)
    return {
        "repo": repo_full,
        "path": item.get("path") or "",
        "html_url": html_url,
        "api_url": item.get("url") or "",
        "sha": item.get("sha") or "",
        "matched_lines": int(matched or 0),
        "total_lines": int(total or 0),
        "line_numbers": line_numbers[:20],
        "vendor_like": bool(_is_vendor_like_hit(item, source_repo)),
    }


def _decode_github_content_response(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    content = payload.get("content")
    if content and payload.get("encoding") == "base64":
        try:
            return base64.b64decode(str(content).encode()).decode("utf-8", errors="replace")
        except Exception:
            return ""
    if isinstance(content, str):
        return content
    return ""


def _fetch_search_item_text(item: dict, headers: dict, timeout: int) -> str:
    url = item.get("url") or ""
    if url:
        r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code == 200:
            try:
                text = _decode_github_content_response(r.json())
                if text:
                    return text
            except Exception:
                pass
    download_url = item.get("download_url") or ""
    if download_url:
        r = requests.get(download_url, headers=headers, timeout=timeout)
        if r.status_code == 200:
            return r.text or ""
    return ""




def _github_verify_workers(default: int = 6) -> int:
    """Return bounded workers for verifying returned GitHub code-search files."""
    raw = os.environ.get("AGENTSEAL_GITHUB_VERIFY_WORKERS", str(default)).strip()
    try:
        n = int(raw)
    except Exception:
        n = default
    return max(1, min(n, 12))


def _env_max_results(default: int) -> int:
    raw = os.environ.get("AGENTSEAL_INDEPENDENT_MAX_RESULTS", "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except Exception:
        return default
    return max(1, min(n, 100))

def _is_vendor_like_hit(item: dict, source_repo: str) -> bool:
    path = str(item.get("path") or "").replace("\\", "/").lower()
    parts = [p for p in path.split("/") if p]
    vendor_markers = {
        "vendor", "vendors", "third_party", "thirdparty", "3rdparty",
        "external", "externals", "deps", "dependencies", "submodules",
        "contrib", "vendored",
    }
    if any(part in vendor_markers for part in parts):
        return True
    source_name = (source_repo or "").split("/")[-1].lower()
    if source_name and source_name in parts:
        return True
    return False


def search_independent_sources(
    patch: str,
    source_repo: str,
    *,
    max_results: int = 30,
    timeout: int = 15,
) -> IndependentVerification:
    """Search GitHub for the patch's distinctive 8-gram in independent repos.

    Parameters
    ----------
    patch:
        The gold patch to search for.
    source_repo:
        The source repo (e.g. 'django/django') — excluded from independent_hits.
    max_results:
        Maximum number of search results to process.
    timeout:
        HTTP timeout in seconds.

    Returns
    -------
    IndependentVerification
        The search result, including how many INDEPENDENT repos contain the patch.

    the circularity of the regular audit path. Instead of comparing the patch
    to the PR it came from, we search ALL of GitHub for the patch's most
    distinctive 8-gram. Hits in other repos = real evidence of availability
    in independent sources.

    Deterministic for a fixed GitHub API response/cache; no LLM.
    """
    # before any use of the variable names in the function body).
    global _SEARCH_BUDGET, _SEARCH_RESET_AT

    max_results = _env_max_results(max_results)
    result = IndependentVerification(source_repo=source_repo)

    # Guard: None/empty patch
    if not patch or not isinstance(patch, str):
        object.__setattr__(result, "error", "patch is None or not a string")
        return result

    # for this exact patch content (normalized), reuse the result instead
    # of hitting the GitHub API again. This saves rate-limit budget for
    # benchmarks with duplicate or near-duplicate patches.
    patch_hash = _patch_content_hash(patch)
    if patch_hash in _PATCH_SEARCH_CACHE:
        cached = _PATCH_SEARCH_CACHE[patch_hash]
        # filtered against the ORIGINAL source_repo, not the current one. If
        # the current source_repo happened to be in that list, it would be
        # counted as "independent of itself" → false HIGH/CRITICAL. Re-filter
        # against the current source_repo (case-insensitive) before returning.
        cached_sources = list(getattr(cached, "verified_sources", None) or [])
        if cached_sources:
            filtered_sources = [
                h for h in cached_sources
                if str(h.get("repo", "")).lower() != source_repo.lower()
            ]
            filtered_repos_all = []
            for h in filtered_sources:
                repo_name = h.get("repo") or ""
                if repo_name and repo_name not in filtered_repos_all:
                    filtered_repos_all.append(repo_name)
            independent_hits = len(filtered_repos_all)
            vendor_like_repos_all = []
            for h in filtered_sources:
                repo_name = h.get("repo") or ""
                if h.get("vendor_like") and repo_name and repo_name not in vendor_like_repos_all:
                    vendor_like_repos_all.append(repo_name)
        else:
            filtered_repos_all = [r for r in (cached.independent_repos or [])
                                  if r.lower() != source_repo.lower()]
            filtered_sources = []
            vendor_like_repos_all = [r for r in (getattr(cached, "vendor_like_repos", None) or [])
                                     if r.lower() != source_repo.lower()]
            source_was_in_display_sample = len(filtered_repos_all) != len(cached.independent_repos or [])
            independent_hits = max(
                0,
                int(cached.independent_hits or 0) - (1 if source_was_in_display_sample else 0),
            )
        verified_urls = [h.get("html_url", "") for h in filtered_sources if h.get("html_url")]
        vendor_hits = len(vendor_like_repos_all)
        result = IndependentVerification(
            query_8gram=cached.query_8gram,
            total_hits=cached.total_hits,
            candidate_hits=getattr(cached, "candidate_hits", independent_hits),
            candidate_repos=getattr(cached, "candidate_repos", filtered_repos_all[:5]),
            source_repo=source_repo,
            searched=cached.searched,
            error=cached.error,
            verification_mode=getattr(cached, "verification_mode", ""),
            vendor_like_hits=vendor_hits,
            non_vendor_hits=max(0, independent_hits - vendor_hits),
            vendor_like_repos=vendor_like_repos_all[:5],
            verified_sources=filtered_sources,
            verified_urls=verified_urls[:10],
        )
        object.__setattr__(result, "independent_hits", independent_hits)
        object.__setattr__(result, "independent_repos", filtered_repos_all[:5])
        object.__setattr__(result, "verified_repos", filtered_repos_all[:5])
        return result

    # 1. Extract the most distinctive 8-gram (now with normalization)
    query = _extract_most_distinctive_8gram(patch)
    if not query:
        object.__setattr__(result, "error", "patch too short for 8-gram extraction")
        # Cache the negative result too
        _PATCH_SEARCH_CACHE[patch_hash] = result
        return result
    result = IndependentVerification(
        query_8gram=query,
        source_repo=source_repo,
    )

    # 2. Check for GitHub token (code search requires auth for meaningful usage)
    from .github_auth import get_token, get_auth_headers
    token = get_token()
    headers = get_auth_headers()
    if not token:
        # Without a token, code search is heavily rate-limited (10 req/min)
        # and may return 403. We try anyway but expect degradation.
        pass

    # remain deterministic regardless of whether GITHUB_TOKEN is set in the
    # shell environment.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return IndependentVerification(
            query_8gram=query,
            source_repo=source_repo,
            searched=False,
            error="skipped during pytest (PYTEST_CURRENT_TEST set)",
        )

    # pattern as github_fetch.py. Prevents re-searching the same 8-grams on
    # every audit run. Cache key is based on the query string.
    from pathlib import Path as _Path
    search_cache_dir = _SEARCH_CACHE_DIR or _Path(".agentseal_search_cache")

    def _search_cache_path(query: str) -> _Path:
        h = hashlib.sha256(f"{_SEARCH_CACHE_VERSION}|{query}".encode()).hexdigest()[:16]
        safe_query = re.sub(r'[^a-zA-Z0-9_]', '_', query)[:40]
        return search_cache_dir / f"{safe_query}_{h}.json"

    def _search_cache_key(query: str) -> str:
        # secret. Without this, any local attacker could drop a forged .json
        # with "independent_hits": 999 and force every audit to CRITICAL.
        from .github_auth import hmac_integrity_tag
        return hmac_integrity_tag("agentseal-search", f"{_SEARCH_CACHE_VERSION}|{query}")

    def _search_cache_valid(path: _Path, expected_key: str) -> bool:
        if not path.exists() or path.is_symlink():
            return False
        try:
            age = time.time() - path.stat().st_mtime
            if age > _SEARCH_CACHE_TTL:
                return False
            key_path = path.with_suffix(path.suffix + ".key")
            if key_path.is_symlink():
                return False
            try:
                stored = key_path.read_text(encoding="utf-8").strip()
                if stored != expected_key:
                    return False
            except Exception:
                return False
            return True
        except Exception:
            return False

    # Check cache first
    cache_path = _search_cache_path(query)
    _cache_integrity = _search_cache_key(query)
    if _search_cache_valid(cache_path, _cache_integrity):
        try:
            import json as _json
            cached = _json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("cache_version") != _SEARCH_CACHE_VERSION:
                raise ValueError("old search-cache version")
            # the current source_repo so a cached result from repo A is not
            # applied verbatim to repo B.
            cached_sources = list(cached.get("verified_sources") or [])
            if cached_sources:
                filtered_sources = [
                    h for h in cached_sources
                    if str(h.get("repo", "")).lower() != source_repo.lower()
                ]
                filtered = []
                for h in filtered_sources:
                    repo_name = h.get("repo") or ""
                    if repo_name and repo_name not in filtered:
                        filtered.append(repo_name)
                independent_hits = len(filtered)
                vendor_like_repos = []
                for h in filtered_sources:
                    repo_name = h.get("repo") or ""
                    if h.get("vendor_like") and repo_name and repo_name not in vendor_like_repos:
                        vendor_like_repos.append(repo_name)
            else:
                filtered_sources = []
                filtered = [r for r in (cached.get("independent_repos") or [])
                            if r.lower() != source_repo.lower()]
                source_was_in_display_sample = len(filtered) != len(cached.get("independent_repos") or [])
                independent_hits = max(
                    0,
                    int(cached.get("independent_hits", len(filtered)) or 0)
                    - (1 if source_was_in_display_sample else 0),
                )
                vendor_like_repos = [r for r in (cached.get("vendor_like_repos") or [])
                                     if r.lower() != source_repo.lower()]
            verified_urls = [h.get("html_url", "") for h in filtered_sources if h.get("html_url")]
            result = IndependentVerification(
                query_8gram=query,
                total_hits=cached.get("total_hits", 0),
                candidate_hits=cached.get("candidate_hits", cached.get("total_hits", 0)),
                candidate_repos=[
                    r for r in (cached.get("candidate_repos") or [])
                    if r.lower() != source_repo.lower()
                ][:5],
                source_repo=source_repo,
                searched=True,
                verification_mode=cached.get("verification_mode", ""),
                vendor_like_hits=len(vendor_like_repos),
                non_vendor_hits=max(0, independent_hits - len(vendor_like_repos)),
                vendor_like_repos=vendor_like_repos[:5],
                verified_sources=filtered_sources,
                verified_urls=verified_urls[:10],
            )
            object.__setattr__(result, "independent_hits", independent_hits)
            object.__setattr__(result, "independent_repos", filtered[:5])
            object.__setattr__(result, "verified_repos", filtered[:5])
            return result
        except Exception:
            pass  # corrupt cache, re-search

    # every 2.1s (28/min steady-state), we now burst up to 28 requests
    # instantly, then sleep until GitHub's rate-limit window resets.
    with _SEARCH_LOCK:
        now = time.time()
        # If the reset time has passed, refill the budget
        if _SEARCH_RESET_AT and now >= _SEARCH_RESET_AT:
            _SEARCH_BUDGET = 28
            _SEARCH_RESET_AT = 0.0
        # If we're out of budget, sleep until reset
        if _SEARCH_BUDGET <= 0:
            if _SEARCH_RESET_AT > now:
                wait = _SEARCH_RESET_AT - now
                # Don't sleep forever — cap at 65s (window is 60s + margin)
                wait = min(wait, 65.0)
                time.sleep(wait)
            _SEARCH_BUDGET = 28
            _SEARCH_RESET_AT = 0.0
        # Consume one unit of budget
        _SEARCH_BUDGET -= 1

    # 3. Query the GitHub code search API
    #    identifiers (no phrase quotes). GitHub code search treats space-
    #    separated terms as an AND query, which is what we want: find files
    #    containing ALL these distinctive identifiers together.
    url = "https://api.github.com/search/code"
    params = {
        "q": query,  # e.g. "uncertainty_dtype arithmetic_data" (no quotes)
        "per_page": min(max_results, 100),
    }

    try:
        r = requests.get(url, headers=headers, params=params, timeout=timeout)
        # X-RateLimit-Remaining tells us how many requests we have left in
        # the current window. X-RateLimit-Reset tells us when the window
        # resets (epoch seconds).
        with _SEARCH_LOCK:
            rem_hdr = r.headers.get("X-RateLimit-Remaining")
            reset_hdr = r.headers.get("X-RateLimit-Reset")
            if rem_hdr is not None:
                try:
                    _SEARCH_BUDGET = min(_SEARCH_BUDGET, int(rem_hdr))
                except ValueError:
                    pass
            if reset_hdr is not None:
                try:
                    _SEARCH_RESET_AT = float(reset_hdr)
                except ValueError:
                    pass

        if r.status_code == 200:
            data = r.json()
            total_count = data.get("total_count", 0)
            items = data.get("items", [])
            result = IndependentVerification(
                query_8gram=query,
                total_hits=total_count,
                source_repo=source_repo,
                searched=True,
                verification_mode="exact_changed_lines",
            )
            candidate_repos = []
            verified_repos = []
            verified_sources = []
            vendor_like_repos = []
            candidate_items = []
            seen_candidate_repos = set()
            for item in items:
                repo_full = item.get("repository", {}).get("full_name", "")
                # "Django/Django" is correctly excluded when source_repo is
                # "django/django" (GitHub repo names are case-insensitive).
                if repo_full and repo_full.lower() != source_repo.lower():
                    if repo_full not in seen_candidate_repos:
                        candidate_repos.append(repo_full)
                        seen_candidate_repos.add(repo_full)
                    # Keep every file candidate; the exact file may not be the
                    # first result returned for a repo.
                    candidate_items.append(item)

            # still rate-limited, but blob fetches use the core API budget and do
            # not need to be serialized behind max_results network round trips.
            def _verify_item(item: dict):
                repo_full = item.get("repository", {}).get("full_name", "")
                try:
                    blob = _fetch_search_item_text(item, headers, timeout)
                    verified, matched, total, line_numbers = _verify_patch_lines_in_blob_with_locations(patch, blob)
                    if verified:
                        return repo_full, _build_verified_code_source(item, matched, total, line_numbers, source_repo)
                except Exception:
                    return repo_full, None
                return repo_full, None

            if candidate_items:
                from concurrent.futures import ThreadPoolExecutor, as_completed
                verified_repo_set = set()
                with ThreadPoolExecutor(max_workers=min(_github_verify_workers(), len(candidate_items))) as ex:
                    futures = [ex.submit(_verify_item, item) for item in candidate_items]
                    for fut in as_completed(futures):
                        repo_full, hit = fut.result()
                        if not repo_full or repo_full in verified_repo_set or not hit:
                            continue
                        verified_repo_set.add(repo_full)
                        verified_sources.append(hit)
                        verified_repos.append(repo_full)
                        if hit.get("vendor_like") and repo_full not in vendor_like_repos:
                            vendor_like_repos.append(repo_full)
            non_vendor_hits = max(0, len(verified_repos) - len(vendor_like_repos))
            # Use object.__setattr__ because the dataclass is frozen
            object.__setattr__(result, "candidate_hits", len(candidate_repos))
            object.__setattr__(result, "candidate_repos", candidate_repos[:5])
            object.__setattr__(result, "independent_hits", len(verified_repos))
            object.__setattr__(result, "independent_repos", verified_repos[:5])
            object.__setattr__(result, "verified_repos", verified_repos[:5])
            object.__setattr__(result, "vendor_like_hits", len(vendor_like_repos))
            object.__setattr__(result, "non_vendor_hits", non_vendor_hits)
            object.__setattr__(result, "vendor_like_repos", vendor_like_repos[:5])
            object.__setattr__(result, "verified_sources", verified_sources)
            object.__setattr__(result, "verified_urls", [h.get("html_url", "") for h in verified_sources if h.get("html_url")][:10])
            try:
                import json as _json
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_payload = _json.dumps({
                    "cache_version": _SEARCH_CACHE_VERSION,
                    "query": query,
                    "total_hits": total_count,
                    "candidate_hits": len(candidate_repos),
                    "candidate_repos": candidate_repos[:5],
                    "independent_hits": len(verified_repos),
                    "independent_repos": verified_repos[:5],
                    "verified_repos": verified_repos[:5],
                    "vendor_like_hits": len(vendor_like_repos),
                    "non_vendor_hits": non_vendor_hits,
                    "vendor_like_repos": vendor_like_repos[:5],
                    "verified_sources": verified_sources,
                    "verified_urls": [h.get("html_url", "") for h in verified_sources if h.get("html_url")][:10],
                    "verification_mode": "exact_changed_lines",
                    "cached_at": time.time(),
                })
                # crash between the two leaves an invalid (keyless) entry
                # that _search_cache_valid will reject.
                key_path = cache_path.with_suffix(cache_path.suffix + ".key")
                key_path.write_text(_cache_integrity, encoding="utf-8")
                cache_path.write_text(cache_payload, encoding="utf-8")
            except Exception:
                pass  # never fail the search over a cache-write error
            _PATCH_SEARCH_CACHE[patch_hash] = result
            return result
        elif r.status_code == 403:
            # Check X-RateLimit-Remaining header.
            remaining = r.headers.get("X-RateLimit-Remaining", "?")
            if remaining == "0":
                reset = r.headers.get("X-RateLimit-Reset", "?")
                try:
                    reset_ts = int(reset)
                    wait = max(0, reset_ts - int(time.time()))
                    # call sleeps until reset instead of firing into a 403.
                    with _SEARCH_LOCK:
                        _SEARCH_BUDGET = 0
                        _SEARCH_RESET_AT = float(reset_ts)
                    error_msg = f"code search rate limited (resets in {wait}s — GitHub's 30/min hard ceiling)"
                except Exception:
                    error_msg = f"code search rate limited (reset={reset})"
            elif not token:
                error_msg = "code search requires GITHUB_TOKEN (run /token paste)"
            else:
                # 403 but not rate-limited and token IS set — permission issue
                error_msg = f"code search 403 forbidden (token may lack 'Code search' scope. remaining={remaining})"
            return IndependentVerification(
                query_8gram=query,
                source_repo=source_repo,
                searched=False,
                error=error_msg,
            )
        elif r.status_code == 422:
            # Query too complex or invalid
            # Try to extract the specific error from GitHub's response body
            try:
                err_data = r.json()
                err_msg = err_data.get("message", "query rejected by GitHub")
            except Exception:
                err_msg = "query rejected by GitHub (too complex or invalid)"
            return IndependentVerification(
                query_8gram=query,
                source_repo=source_repo,
                searched=False,
                error=f"code search 422: {err_msg} (query was: {query[:60]})",
            )
        elif r.status_code == 401:
            return IndependentVerification(
                query_8gram=query,
                source_repo=source_repo,
                searched=False,
                error="code search 401 — token is invalid or expired (run /token test to verify)",
            )
        else:
            return IndependentVerification(
                query_8gram=query,
                source_repo=source_repo,
                searched=False,
                error=f"code search HTTP {r.status_code}",
            )
    except requests.RequestException as e:
        return IndependentVerification(
            query_8gram=query,
            source_repo=source_repo,
            searched=False,
            error=f"network error: {type(e).__name__}: {e}",
        )


def is_circularly_matched(patch: str, source_diff: str) -> bool:
    """Check if a patch-to-source comparison is circular (tautological).

    The regular audit path compares the gold patch against the PR diff at
    github.com/{repo}/pull/{pr_number}.diff. If the instance_id encodes
    the PR number (SWE-bench Verified format), the patch was DERIVED from
    that exact PR — so a 100% match is guaranteed by construction.

    This function detects that situation: if the source_diff IS the PR diff
    (contains the same +/- lines as the patch in the same order), the
    comparison is circular.

    INSTEAD of (or in addition to) the circular PR-diff comparison. This
    helper lets them flag the circular case in the report.
    """
    if not patch or not source_diff:
        return False
    # If the patch is a verbatim substring of the source diff, and the
    # source diff looks like a PR diff (has diff headers), it's circular.
    if patch in source_diff:
        has_diff_headers = any(
            line.startswith("diff --git") or line.startswith("---") or line.startswith("+++")
            for line in source_diff.splitlines()[:20]
        )
        return has_diff_headers
    return False


__all__ = [
    "IndependentVerification",
    "search_independent_sources",
    "search_issues_for_problem_statement",
    "is_circularly_matched",
]


# Checks if the issue text (problem statement) appears in issues filed in
# OTHER repos — e.g., duplicate bug reports that quote the original issue.
# Uses the same /search/issues endpoint (30/min rate limit, shared with
# /search/code via the same throttle).

def search_issues_for_problem_statement(
    problem_statement: str,
    source_repo: str,
    *,
    max_results: int = 10,
    timeout: int = 15,
) -> IndependentVerification:
    """Search GitHub Issues for the problem statement text in other repos.

    M4 checks if the issue text appears in SOURCE CODE; this checks if it
    appears in ISSUES filed in other repos (duplicate bug reports, etc.).

    Uses /search/issues (same 30/min rate limit as /search/code, shared
    via the _SEARCH_LOCK throttle).
    """
    global _SEARCH_BUDGET, _SEARCH_RESET_AT
    max_results = _env_max_results(max_results)
    result = IndependentVerification(source_repo=source_repo)

    if not problem_statement or len(problem_statement) < 50:
        object.__setattr__(result, "error", "problem statement too short for search")
        return result

    # Extract a distinctive 40-60 char chunk from the problem statement
    # (first 200 chars, stripped of code blocks)
    chunk = problem_statement[:200].strip()
    # Remove code blocks and URLs
    chunk = re.sub(r'```.*?```', '', chunk, flags=re.DOTALL)
    chunk = re.sub(r'https?://\S+', '', chunk)
    chunk = re.sub(r'\s+', ' ', chunk).strip()
    if len(chunk) < 30:
        object.__setattr__(result, "error", "problem statement has no distinctive text")
        return result

    # Use first 60 chars as the search query (quoted for exact match)
    query = chunk[:60]
    result = IndependentVerification(query_8gram=query, source_repo=source_repo)

    # Skip during pytest
    if os.environ.get("PYTEST_CURRENT_TEST"):
        object.__setattr__(result, "error", "skipped during pytest")
        return result

    # Thread-safe throttle (shared with code search)
    with _SEARCH_LOCK:
        now = time.time()
        if _SEARCH_RESET_AT and now >= _SEARCH_RESET_AT:
            _SEARCH_BUDGET = 28
            _SEARCH_RESET_AT = 0.0
        if _SEARCH_BUDGET <= 0:
            if _SEARCH_RESET_AT > now:
                wait = min(_SEARCH_RESET_AT - now, 65.0)
                time.sleep(wait)
            _SEARCH_BUDGET = 28
            _SEARCH_RESET_AT = 0.0
        _SEARCH_BUDGET -= 1

    # Query /search/issues
    from .github_auth import get_auth_headers, rate_tracker
    url = "https://api.github.com/search/issues"
    params = {"q": f'"{query}"', "per_page": min(max_results, 30)}

    try:
        r = requests.get(url, headers=get_auth_headers(), params=params, timeout=timeout)
        rate_tracker.update_from_response(r, "search")

        with _SEARCH_LOCK:
            rem_hdr = r.headers.get("X-RateLimit-Remaining")
            reset_hdr = r.headers.get("X-RateLimit-Reset")
            if rem_hdr is not None:
                try:
                    _SEARCH_BUDGET = min(_SEARCH_BUDGET, int(rem_hdr))
                except ValueError:
                    pass
            if reset_hdr is not None:
                try:
                    _SEARCH_RESET_AT = float(reset_hdr)
                except ValueError:
                    pass

        if r.status_code == 200:
            data = r.json()
            total_count = data.get("total_count", 0)
            items = data.get("items", [])
            independent_repos = []
            issue_sources = []
            for item in items:
                repo_url = item.get("repository_url", "")
                # Extract repo full_name from URL: https://api.github.com/repos/{owner}/{repo}
                if "/repos/" in repo_url:
                    repo_full = repo_url.split("/repos/")[-1]
                    # missed search_issues_for_problem_statement. "Django/Django"
                    # should be excluded when source_repo is "django/django".
                    if repo_full and repo_full.lower() != source_repo.lower():
                        if repo_full not in independent_repos:
                            independent_repos.append(repo_full)
                        html_url = item.get("html_url") or ""
                        issue_sources.append({
                            "repo": repo_full,
                            "html_url": html_url,
                            "api_url": item.get("url") or "",
                            "title": item.get("title") or "",
                            "matched_lines": 1,
                            "total_lines": 1,
                            "vendor_like": False,
                        })
            object.__setattr__(result, "independent_hits", len(independent_repos))
            object.__setattr__(result, "independent_repos", independent_repos[:5])
            object.__setattr__(result, "verified_repos", independent_repos[:5])
            object.__setattr__(result, "verified_sources", issue_sources)
            object.__setattr__(result, "verified_urls", [h.get("html_url", "") for h in issue_sources if h.get("html_url")][:10])
            object.__setattr__(result, "total_hits", total_count)
            object.__setattr__(result, "searched", True)
            object.__setattr__(result, "verification_mode", "github_issue_search_result")
            object.__setattr__(result, "query_8gram", query)
            return result
        elif r.status_code == 403:
            remaining = r.headers.get("X-RateLimit-Remaining", "?")
            if remaining == "0":
                object.__setattr__(result, "error", "issues search rate limited")
            else:
                object.__setattr__(result, "error", f"issues search 403 (remaining={remaining})")
            return result
        elif r.status_code == 422:
            object.__setattr__(result, "error", "issues search query rejected (422)")
            return result
        else:
            object.__setattr__(result, "error", f"issues search HTTP {r.status_code}")
            return result
    except requests.RequestException as e:
        object.__setattr__(result, "error", f"network error: {type(e).__name__}")
        return result


# This lets callers access the verification URL without building it themselves.
@property
def _search_url(self):
    if self.verified_urls:
        return self.verified_urls[0]
    if self.query_8gram:
        return _code_search_web_url(self.query_8gram)
    return ""
IndependentVerification.search_url = _search_url
