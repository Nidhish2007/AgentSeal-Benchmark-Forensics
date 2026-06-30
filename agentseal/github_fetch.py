"""GitHub PR diff fetcher with rate-limit handling.

Fetches the raw diff for a GitHub PR. Unauthenticated requests are
limited to 60/hour by GitHub, so this module includes:
- Exponential backoff on 429 (rate limit) and 5xx responses
- Caching to disk so re-runs don't re-fetch
- Graceful degradation on failure (returns None, doesn't crash the audit)

(api.github.com/repos/{repo}/pulls/{n}) with Accept: application/vnd.github.v3.diff
when a token is available. This uses the 5000/hr authenticated rate limit
instead of the web endpoint's separate, stricter limit that was causing
"rate limited" errors even when the token was set via /token paste.

The old web endpoint (github.com/{repo}/pull/{n}.diff) is kept as a fallback
for when no token is set.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Optional

import requests

from .github_auth import get_auth_headers, rate_tracker


CACHE_DIR = Path(".agentseal_cache")

# PR history (deleted PRs, force-pushes) doesn't serve stale diffs forever.
_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days


def _get_token() -> Optional[str]:
    """Read the GitHub token from the centralized sanitizer at CALL time."""
    from .github_auth import get_token
    return get_token()


def _cache_key(repo: str, pr_number: int) -> Path:
    """Return the cache file path for a repo+PR."""
    h = hashlib.sha256(f"{repo}#{pr_number}".encode()).hexdigest()[:16]
    return CACHE_DIR / f"{repo.replace('/', '__')}_{pr_number}_{h}.diff"


def _cache_integrity_key(repo: str, pr_number: int) -> str:
    """Return the expected .key sidecar content for a repo+PR.

    check on its cache (only a whitespace check, S232). An attacker who can
    write to .agentseal_cache/ could poison the PR-diff cache with arbitrary
    content, causing the engine to score the gold patch against attacker-
    controlled text.

    `sha256("agentseal-pr-diff|{repo}|{pr_number}")[:16]` with NO secret —
    anyone with the source could forge it. Now it is an HMAC-SHA256 tag keyed
    by a per-installation random secret (github_auth.get_cache_secret), so a
    local attacker who cannot read ~/.agentseal/secret cannot forge a valid
    .key sidecar.
    """
    from .github_auth import hmac_integrity_tag
    return hmac_integrity_tag("agentseal-pr-diff", f"{repo}|{pr_number}")


def _cache_is_valid(cache_path: Path, expected_key: str) -> bool:
    """Validate a cache file with three integrity checks:

    1. Content non-empty and non-whitespace-only.
    2. mtime within _CACHE_TTL_SECONDS.
    3. A sidecar .key file whose content equals expected_key, preventing stale
       or poisoned cache entries from being trusted as real PR diffs.
    """
    import time as _time
    try:
        if not cache_path.exists():
            return False
        # /etc/passwd (with a forged .key sidecar) would otherwise pass the
        # content check and return arbitrary local-file content as a "PR diff".
        if cache_path.is_symlink():
            return False
        # TTL check
        try:
            age = _time.time() - cache_path.stat().st_mtime
            if age > _CACHE_TTL_SECONDS:
                return False
        except OSError:
            return False
        # content non-empty
        try:
            content = cache_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return False
        if not content or not content.strip():
            return False
        # .key sidecar integrity check
        key_path = cache_path.with_suffix(cache_path.suffix + ".key")
        # symlink pointing at a file containing the predictable old key (or a
        # forged HMAC) would otherwise pass the content check. The cache_path
        # itself is already symlink-rejected above; extend the same defense to
        # the sidecar.
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


def _cache_write(cache_path: Path, content: str, expected_key: str) -> None:
    """Atomically write content + integrity .key sidecar.

    Writes the .key sidecar FIRST, then the content. If interrupted between
    the two, _cache_is_valid rejects the entry (missing .key) and re-fetches.
    """
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        key_path = cache_path.with_suffix(cache_path.suffix + ".key")
        key_path.write_text(expected_key, encoding="utf-8")
        cache_path.write_text(content, encoding="utf-8")
    except Exception:
        pass


# repo+pr, so the engine can emit a detailed error message instead of the
# generic "rate limited or deleted".
_last_fetch_errors: dict[str, str] = {}


def get_last_fetch_error(repo: str, pr_number: int) -> Optional[str]:
    """Return the last error message for a repo+pr, or None if the last fetch succeeded."""
    return _last_fetch_errors.get(f"{repo}#{pr_number}")


def fetch_pr_diff(
    repo: str,
    pr_number: int,
    *,
    timeout: int = 20,
    max_retries: int = 3,
    use_cache: bool = True,
) -> Optional[str]:
    """Fetch the raw diff text for a GitHub PR.

    Returns the diff as a string, or None if the fetch failed.
    Caches results to ``.agentseal_cache/`` so re-runs are fast.

    (api.github.com/repos/{repo}/pulls/{pr_number}) with
    Accept: application/vnd.github.v3.diff. This uses the 5000/hr
    authenticated rate limit, NOT the web endpoint's separate limit
    that was causing "rate limited" errors even with the token set.

    Falls back to the web endpoint (github.com/{repo}/pull/{pr_number}.diff)
    when no token is set (60/hr unauthenticated limit).
    """
    if not repo or not pr_number:
        return None

    cache_key_str = f"{repo}#{pr_number}"
    cache_path = _cache_key(repo, pr_number)
    expected_key = _cache_integrity_key(repo, pr_number)
    if use_cache and _cache_is_valid(cache_path, expected_key):
        try:
            _last_fetch_errors.pop(cache_key_str, None)
            return cache_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass  # corrupt cache, re-fetch

    token = _get_token()

    # ── Strategy: use the API endpoint when we have a token (5000/hr limit) ──
    # endpoint). Even with a token, the web endpoint has separate, stricter
    # rate limiting. The API endpoint (api.github.com/repos/{repo}/pulls/{n})
    # with Accept: application/vnd.github.v3.diff returns the same diff text
    # but uses the 5000/hr authenticated API rate limit.
    # response to the central rate-limit tracker.
    if token:
        url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
        headers = get_auth_headers({"Accept": "application/vnd.github.v3.diff"})
    else:
        # No token — fall back to the web endpoint (60/hr unauthenticated)
        url = f"https://github.com/{repo}/pull/{pr_number}.diff"
        headers = {"User-Agent": "AgentSeal/4.9.8 (contamination audit)"}

    for attempt in range(max_retries):
        try:
            r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            try:
                from .github_auth import rate_tracker
                rate_tracker.update_from_response(r, "core")
            except Exception:
                pass
            if r.status_code == 200:
                text = r.text
                if text and text.strip():
                    if use_cache:
                        _cache_write(cache_path, text, expected_key)
                    _last_fetch_errors.pop(cache_key_str, None)
                    return text
                else:
                    _last_fetch_errors[cache_key_str] = "200 OK but empty response body"
                    return None
            elif r.status_code in (429, 503):
                # Rate limited — check the X-RateLimit-Remaining header for diagnostics
                remaining = r.headers.get("X-RateLimit-Remaining", "?")
                reset = r.headers.get("X-RateLimit-Reset", "?")
                if remaining == "0" and reset != "?":
                    try:
                        reset_ts = int(reset)
                        wait_msg = f"resets in {max(0, reset_ts - int(time.time()))}s"
                    except Exception:
                        wait_msg = f"reset epoch={reset}"
                else:
                    wait_msg = f"remaining={remaining}"
                _last_fetch_errors[cache_key_str] = f"HTTP {r.status_code} rate limited ({wait_msg})"
                # Back off
                wait = 2 ** attempt + 1
                time.sleep(wait)
                continue
            elif r.status_code == 404:
                _last_fetch_errors[cache_key_str] = f"HTTP 404 — PR #{pr_number} not found in {repo}"
                return None
            elif r.status_code == 401:
                if token:
                    fallback_url = f"https://github.com/{repo}/pull/{pr_number}.diff"
                    fallback_headers = {"User-Agent": "AgentSeal/5.0 (contamination audit)"}
                    try:
                        fallback = requests.get(
                            fallback_url,
                            headers=fallback_headers,
                            timeout=timeout,
                            allow_redirects=True,
                        )
                        if fallback.status_code == 200 and fallback.text and fallback.text.strip():
                            text = fallback.text
                            if use_cache:
                                _cache_write(cache_path, text, expected_key)
                            _last_fetch_errors.pop(cache_key_str, None)
                            return text
                        _last_fetch_errors[cache_key_str] = (
                            f"HTTP 401 from GitHub API token and fallback HTTP {fallback.status_code}"
                        )
                    except requests.RequestException as e:
                        _last_fetch_errors[cache_key_str] = (
                            f"HTTP 401 from GitHub API token; fallback failed: {type(e).__name__}: {e}"
                        )
                    return None
                _last_fetch_errors[cache_key_str] = "HTTP 401 — token is invalid or expired"
                return None
            elif r.status_code == 403:
                # Could be rate limit OR insufficient permissions
                remaining = r.headers.get("X-RateLimit-Remaining", "?")
                _last_fetch_errors[cache_key_str] = f"HTTP 403 forbidden (rate_limit_remaining={remaining}). Token may lack 'Contents: Read' permission."
                time.sleep(1)
                continue
            else:
                _last_fetch_errors[cache_key_str] = f"HTTP {r.status_code} — unexpected response from GitHub"
                time.sleep(1)
                continue
        except requests.RequestException as e:
            _last_fetch_errors[cache_key_str] = f"network error: {type(e).__name__}: {e}"
            time.sleep(1)
            continue

    # instead of hanging. The engine marks the instance as "not evaluated"
    # and the audit continues.
    return None


# =============================================================================
# =============================================================================

# API. This is the operative date for contamination analysis: if the fix
# was merged AFTER the training-data cutoff (e.g. The Stack v2 = 2024-03-15),
# the patch CANNOT be in that training corpus regardless of whether it's
# "on GitHub" now. The old provenance.py checked instance.created_at (issue
# date) which is the WRONG date — an issue from 2022 whose fix landed in
# 2024-04 would pass the temporal premise despite the fix post-dating the
# Stack v2 cutoff.

_pr_meta_cache: dict[str, dict] = {}


def fetch_pr_merge_date(repo: str, pr_number: int, *, timeout: int = 10) -> Optional[str]:
    """Fetch the merged_at timestamp for a GitHub PR via the REST API.

    Returns an ISO 8601 string like '2024-04-15T10:30:00Z', or None if:
    - the PR doesn't exist
    - the PR is not merged (open or closed-without-merge)
    - the API call fails (rate limit, network error)

    Cached in-memory per repo+pr to avoid redundant API calls.

    unauthenticated before, causing 60/hr rate limit exhaustion on
    large audits).
    """
    if not repo or not pr_number:
        return None
    cache_key = f"{repo}#{pr_number}"
    if cache_key in _pr_meta_cache:
        return _pr_meta_cache[cache_key].get("merged_at")

    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    headers = get_auth_headers()

    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        rate_tracker.update_from_response(r, "core")
        if r.status_code == 200:
            data = r.json()
            merged_at = data.get("merged_at")  # None if not merged
            _pr_meta_cache[cache_key] = {"merged_at": merged_at}
            return merged_at
        elif r.status_code == 404:
            _pr_meta_cache[cache_key] = {"merged_at": None}
            return None
        elif r.status_code == 429:
            # Rate limited — don't cache, will retry next call
            return None
    except Exception:
        return None
    return None


def clear_cache() -> None:
    """Clear the on-disk PR diff cache + in-memory caches.

    don't persist after a cache clear (e.g. when /token paste is used and
    the user wants to re-fetch everything with the new token).
    """
    _pr_meta_cache.clear()
    _last_fetch_errors.clear()
    if CACHE_DIR.exists():
        for f in CACHE_DIR.iterdir():
            try:
                f.unlink()
            except Exception:
                pass


def verify_token() -> tuple[bool, str]:
    """Verify the GitHub token works by making a test API call.

    BEFORE running an audit. Returns (success, message).

    Uses the /rate_limit endpoint which is free (doesn't count against
    the 5000/hr limit) and returns the current rate limit status.
    """
    token = _get_token()
    if not token:
        return (False, "No GITHUB_TOKEN set in environment. Run /token paste first.")

    try:
        r = requests.get(
            "https://api.github.com/rate_limit",
            headers=get_auth_headers(),
            timeout=10,
        )
        rate_tracker.update_from_response(r, "core")
        if r.status_code == 200:
            data = r.json()
            core = data.get("resources", {}).get("core", {})
            remaining = core.get("remaining", "?")
            limit = core.get("limit", "?")
            search = data.get("resources", {}).get("search", {})
            search_remaining = search.get("remaining", "?")
            search_limit = search.get("limit", "?")
            masked = token[:12] + "…" + token[-4:] if len(token) > 20 else token[:4] + "…"
            return (True, f"Token valid (masked: {masked}). API rate limit: {remaining}/{limit} remaining. Code search: {search_remaining}/{search_limit} remaining.")
        elif r.status_code == 401:
            return (False, "HTTP 401 — token is invalid, expired, or revoked. Generate a new one at https://github.com/settings/tokens")
        elif r.status_code == 403:
            remaining = r.headers.get("X-RateLimit-Remaining", "?")
            return (False, f"HTTP 403 — forbidden (rate_limit_remaining={remaining}). Token may lack permissions or rate limit is exhausted.")
        else:
            return (False, f"HTTP {r.status_code} — unexpected response from GitHub API")
    except requests.RequestException as e:
        return (False, f"Network error: {type(e).__name__}: {e}")
    except Exception as e:
        return (False, f"Error: {type(e).__name__}: {e}")
