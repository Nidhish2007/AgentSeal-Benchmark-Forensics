"""Central GitHub API token helper + rate-limit tracker.

files (github_fetch.py, pro_audit.py, independent_search.py, tui.py), each
with its own header-building logic. Worse, _get_default_branch in
pro_audit.py was NOT using the token at all — hitting the 60/hr
unauthenticated limit on the Pro path and silently defaulting to "main"
for repos that actually use "master"/"develop", causing file fetches to fail.

This module centralizes:
1. Token reading (always reflects current env, so /token paste takes effect)
2. Auth header building (consistent across all API calls)
3. Rate-limit tracking (reads X-RateLimit-Remaining from every response)
4. Pre-flight checks (warn before audits if budget is low)
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets as _secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests


# A per-installation random secret used to HMAC-sign cache integrity sidecars.
# (sha256("agentseal-pr-diff|repo|pr")[:16]) as its `.key` sidecar, and the
# search cache had NO sidecar at all — any local attacker who could write to
# the cache dirs could forge entries and poison the audit (false CRITICAL).
# The secret is generated once, stored at ~/.agentseal/secret with 0600 perms.

_SECRET_PATH = Path.home() / ".agentseal" / "secret"
_SECRET_LOCK = threading.Lock()
_SECRET_CACHE: Optional[bytes] = None


def get_cache_secret() -> bytes:
    """Return the per-installation cache-integrity secret (lazy, cached)."""
    global _SECRET_CACHE
    with _SECRET_LOCK:
        if _SECRET_CACHE is not None:
            return _SECRET_CACHE
        try:
            if _SECRET_PATH.exists() and not _SECRET_PATH.is_symlink():
                data = _SECRET_PATH.read_bytes()
                if len(data) >= 32:
                    _SECRET_CACHE = data
                    return data
        except Exception:
            pass
        # Generate a new secret.
        try:
            _SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
            # symlink at ~/.agentseal/secret — without this, an attacker who
            # plants secret → ~/.bashrc would cause us to TRUNCATE ~/.bashrc
            # with 32 random bytes. O_NOFOLLOW (Linux 2.1.126+) makes open()
            # fail with ELOOP if the path is a symlink. Fall back to O_EXCL
            # on platforms without O_NOFOLLOW.
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            else:
                # O_EXCL: fail if file exists. We then read+verify the
                # existing file (handled above) instead of truncating it.
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            fd = os.open(str(_SECRET_PATH), flags, 0o600)
            try:
                new_secret = _secrets.token_bytes(32)
                os.write(fd, new_secret)
            finally:
                os.close(fd)
            # If we used O_EXCL and the file already existed, the open above
            # failed — but the existing-file read path above should have
            # caught it. Belt-and-suspenders: re-read if cache still empty.
            if _SECRET_CACHE is None and _SECRET_PATH.exists() and not _SECRET_PATH.is_symlink():
                data = _SECRET_PATH.read_bytes()
                if len(data) >= 32:
                    _SECRET_CACHE = data
                    return data
            _SECRET_CACHE = new_secret
            return new_secret
        except OSError:
            # predictable username-derived secret — that would let any
            # attacker who knows the username forge all HMAC cache tags.
            # Instead, generate a per-process random secret (lost on restart,
            # so caches are invalidated — safe degradation, not a security hole).
            new_secret = _secrets.token_bytes(32)
            _SECRET_CACHE = new_secret
            return new_secret
        except Exception:
            new_secret = _secrets.token_bytes(32)
            _SECRET_CACHE = new_secret
            return new_secret


def hmac_integrity_tag(domain: str, payload: str, length: int = 16) -> str:
    """HMAC-SHA256 tag for a cache integrity sidecar.

    `domain` is a constant string naming the cache family (e.g.
    "agentseal-pr-diff"); `payload` is the cache-entry identifier. Returns a
    hex digest truncated to `length` chars. Unforgeable without the secret.
    """
    return hmac.new(
        get_cache_secret(),
        f"{domain}|{payload}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:length]




# Tokens pasted into a TUI often arrive as "Bearer <token>", quoted text,
# wrapped lines, or with invisible Unicode characters. Keep this logic
# centralized so direct /token, /token paste, persisted-file load, and env-var
# reads behave the same way. Never lower-case tokens: GitHub/HF tokens are
# case-sensitive.
import re as _re

_GITHUB_TOKEN_FULL_RE = _re.compile(
    r"^(?:github_pat_[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_-]{20,}|gh\.[A-Za-z0-9_-]{20,})$"
)
_GITHUB_TOKEN_FIND_RE = _re.compile(
    r"(?:github_pat_[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_-]{20,}|gh\.[A-Za-z0-9_-]{20,})"
)
_HF_TOKEN_FULL_RE = _re.compile(r"^hf_[A-Za-z0-9_-]{10,}$")
_HF_TOKEN_FIND_RE = _re.compile(r"hf_[A-Za-z0-9_-]{10,}")
_ZERO_WIDTH_RE = _re.compile(r"[\u200b-\u200f\ufeff]")


def normalize_secret_text(raw: object, *, kind: str = "github") -> str:
    """Extract and normalize a pasted token/secret without changing case.

    Handles direct tokens, `Bearer ...`, `Authorization: Bearer ...`, shell
    assignments like `GITHUB_TOKEN=...`, quoted/backticked strings, and line
    wrapping. Returns an empty string for empty input.
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    s = _ZERO_WIDTH_RE.sub("", s)
    s = s.strip().strip("` \t\r\n").strip('"').strip("'").strip()
    # Common paste shapes: Authorization: Bearer X, Bearer X, GITHUB_TOKEN=X
    s = _re.sub(r"(?is)^authorization\s*:\s*bearer\s+", "", s).strip()
    s = _re.sub(r"(?is)^bearer\s+", "", s).strip()
    s = _re.sub(r"(?is)^(?:export\s+)?(?:GITHUB_TOKEN|GH_TOKEN|HF_TOKEN|HUGGING_FACE_HUB_TOKEN)\s*=\s*", "", s).strip()
    finder = _HF_TOKEN_FIND_RE if kind == "hf" else _GITHUB_TOKEN_FIND_RE
    m = finder.search(s)
    if m:
        return m.group(0)
    # Some terminals wrap long tokens with newlines/spaces. Compact and retry.
    compact = _re.sub(r"\s+", "", s)
    m = finder.search(compact)
    if m:
        return m.group(0)
    return compact


def looks_like_github_token(token: object) -> bool:
    return bool(_GITHUB_TOKEN_FULL_RE.fullmatch(normalize_secret_text(token, kind="github")))


def looks_like_hf_token(token: object) -> bool:
    return bool(_HF_TOKEN_FULL_RE.fullmatch(normalize_secret_text(token, kind="hf")))


def mask_secret(token: object, *, prefix: int = 12, suffix: int = 4) -> str:
    tok = str(token or "")
    if not tok:
        return "<empty>"
    if len(tok) <= prefix + suffix + 3:
        return tok[:max(1, min(4, len(tok)))] + "…"
    return tok[:prefix] + "…" + tok[-suffix:]


# ── Token management ──────────────────────────────────────────────────────

def get_token() -> Optional[str]:
    """Read the GitHub token from the environment at CALL time.

    Always reflects the current environment — including tokens set via
    /token paste during the same TUI session. The returned token is sanitized
    but never lower-cased, because GitHub tokens are case-sensitive.
    """
    raw = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    tok = normalize_secret_text(raw, kind="github")
    return tok or None


def has_token() -> bool:
    """Quick check: is a token currently set?"""
    return bool(get_token())


# Centralized HF token reader, mirroring get_token(). Used by auto_discover
# to download gated/large datasets (e.g. Multi-SWE-bench). Set via /hf paste
# in the TUI, or HF_TOKEN in the environment.

_HF_TOKEN_FILE = Path.home() / ".agentseal_hf_token"


def get_hf_token() -> Optional[str]:
    """Read the HuggingFace token from the environment OR the persisted file.

    Priority: env var (HF_TOKEN / HUGGING_FACE_HUB_TOKEN) > persisted file
    (~/.agentseal_hf_token, written by /hf paste). Always reflects the
    current state — so /hf paste takes effect immediately.
    """
    env = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if env:
        tok = normalize_secret_text(env, kind="hf")
        return tok or None
    try:
        if _HF_TOKEN_FILE.exists() and not _HF_TOKEN_FILE.is_symlink():
            data = normalize_secret_text(_HF_TOKEN_FILE.read_text(encoding="utf-8"), kind="hf")
            if data:
                return data
    except Exception:
        pass
    return None


def has_hf_token() -> bool:
    """Quick check: is a HuggingFace token available?"""
    return bool(get_hf_token())


def persist_hf_token(token: str) -> bool:
    """Persist a HuggingFace token to ~/.agentseal_hf_token (mode 0600).

    Used by /hf paste. Returns True on success. Also sets it in the current
    process environment so subsequent calls work without a re-read.
    """
    token = normalize_secret_text(token, kind="hf")
    if not token:
        return False
    try:
        _HF_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        # get_cache_secret). Fall back to O_EXCL on platforms without O_NOFOLLOW
        # (e.g. older Windows) so we never truncate a planted symlink.
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        else:
            # O_EXCL: fail if the file already exists. If it exists, the caller
            # should clear it first (/hf clear). This prevents truncating a
            # symlink planted by an attacker.
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(str(_HF_TOKEN_FILE), flags, 0o600)
        try:
            os.write(fd, token.encode("utf-8"))
        finally:
            os.close(fd)
        os.environ["HF_TOKEN"] = token
        return True
    except OSError:
        # O_EXCL path: file exists. Try to read it; if it's a symlink or
        # wrong, refuse rather than truncate.
        try:
            if _HF_TOKEN_FILE.is_symlink():
                return False
            # File exists and is a real file — re-open with O_TRUNC (safe now).
            fd = os.open(str(_HF_TOKEN_FILE), os.O_WRONLY | os.O_TRUNC, 0o600)
            try:
                os.write(fd, token.encode("utf-8"))
            finally:
                os.close(fd)
            os.environ["HF_TOKEN"] = token
            return True
        except Exception:
            return False
    except Exception:
        return False


def clear_hf_token() -> bool:
    """Delete the persisted HuggingFace token + unset the env var."""
    try:
        if _HF_TOKEN_FILE.exists() and not _HF_TOKEN_FILE.is_symlink():
            _HF_TOKEN_FILE.unlink()
    except Exception:
        pass
    os.environ.pop("HF_TOKEN", None)
    os.environ.pop("HUGGING_FACE_HUB_TOKEN", None)
    return True


def get_hf_auth_headers() -> dict:
    """Build Authorization headers for a HuggingFace API request."""
    headers = {"User-Agent": "AgentSeal/5.0 (contamination audit)"}
    tok = get_hf_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return headers


def get_auth_headers(extra: dict | None = None) -> dict:
    """Build Authorization headers for a GitHub API request.

    Always includes User-Agent + Accept. Adds Authorization Bearer if a
    token is set. Merges any extra headers the caller provides.

    Every GitHub API call in the codebase should use this helper instead
    of building its own headers dict — that's how _get_default_branch
    ended up unauthenticated for 4 versions.
    """
    headers = {
        "User-Agent": "AgentSeal/5.0.0 (contamination audit)",
        "Accept": "application/vnd.github+json",
    }
    token = get_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra:
        headers.update(extra)
    return headers


# ── Rate-limit tracking ───────────────────────────────────────────────────

@dataclass
class RateLimitStatus:
    """Snapshot of GitHub rate-limit status for one endpoint category."""
    limit: int = 0
    remaining: int = 0
    reset_at: float = 0.0  # epoch seconds
    last_updated: float = 0.0

    @property
    def resets_in_seconds(self) -> int:
        if not self.reset_at:
            return 0
        return max(0, int(self.reset_at - time.time()))

    @property
    def is_exhausted(self) -> bool:
        return self.remaining == 0 and self.reset_at > time.time()

    @property
    def is_low(self) -> bool:
        """True if remaining budget is < 10% of limit."""
        if self.limit == 0:
            return False
        return self.remaining < (self.limit * 0.1)


class RateLimitTracker:
    """Thread-safe tracker for GitHub rate limits.

    Reads X-RateLimit-Remaining, X-RateLimit-Limit, and X-RateLimit-Reset
    headers from every API response and stores them per resource type
    ('core' for regular API, 'search' for code search).

    /status, and lets audits do pre-flight checks (warn before starting
    if budget is low).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._status: dict[str, RateLimitStatus] = {
            "core": RateLimitStatus(),
            "search": RateLimitStatus(),
        }

    def update_from_response(self, response: requests.Response, resource: str = "core") -> None:
        """Extract rate-limit info from a GitHub API response's headers.

        Call this after EVERY requests.get() to a GitHub API endpoint.
        GitHub includes X-RateLimit-* headers on all API responses.
        """
        try:
            remaining = response.headers.get("X-RateLimit-Remaining")
            limit = response.headers.get("X-RateLimit-Limit")
            reset = response.headers.get("X-RateLimit-Reset")
            if remaining is None and limit is None:
                return  # response didn't include rate-limit headers
            with self._lock:
                status = self._status.setdefault(resource, RateLimitStatus())
                if remaining is not None:
                    try:
                        status.remaining = int(remaining)
                    except ValueError:
                        pass
                if limit is not None:
                    try:
                        status.limit = int(limit)
                    except ValueError:
                        pass
                if reset is not None:
                    try:
                        status.reset_at = float(reset)
                    except ValueError:
                        pass
                status.last_updated = time.time()
        except Exception:
            pass  # never crash an audit over a rate-limit header parse error

    def get_status(self, resource: str = "core") -> RateLimitStatus:
        """Get the current rate-limit status for a resource type."""
        with self._lock:
            return self._status.get(resource, RateLimitStatus())

    def refresh_from_api(self) -> bool:
        """Fetch fresh rate-limit status from GitHub's /rate_limit endpoint.

        This endpoint is FREE (doesn't count against any limit) and returns
        the current status of ALL resource types. Used by /status command
        and pre-flight checks.

        Returns True if the refresh succeeded, False if it failed (no token,
        network error, etc.).
        """
        if not has_token():
            return False
        try:
            r = requests.get(
                "https://api.github.com/rate_limit",
                headers=get_auth_headers(),
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                resources = data.get("resources", {})
                with self._lock:
                    for name, info in resources.items():
                        status = self._status.setdefault(name, RateLimitStatus())
                        status.limit = info.get("limit", 0)
                        status.remaining = info.get("remaining", 0)
                        status.reset_at = float(info.get("reset", 0))
                        status.last_updated = time.time()
                return True
        except Exception:
            pass
        return False

    def format_status(self) -> str:
        """Format the current rate-limit status as a human-readable string."""
        self.refresh_from_api()
        core = self.get_status("core")
        search = self.get_status("search")
        lines = []
        lines.append(f"  Core API (repos, pulls, files): {core.remaining}/{core.limit} remaining")
        if core.is_exhausted:
            lines.append(f"    ⚠ EXHAUSTED — resets in {core.resets_in_seconds}s")
        elif core.is_low:
            lines.append(f"    ⚠ LOW — only {core.remaining} left, resets in {core.resets_in_seconds}s")
        lines.append(f"  Code search (/search/code):     {search.remaining}/{search.limit} remaining")
        if search.is_exhausted:
            lines.append(f"    ⚠ EXHAUSTED — resets in {search.resets_in_seconds}s")
        elif search.is_low:
            lines.append(f"    ⚠ LOW — only {search.remaining} left, resets in {search.resets_in_seconds}s")
        return "\n".join(lines)


# Singleton instance — imported by all modules that make GitHub API calls
rate_tracker = RateLimitTracker()


def check_preflight(sample_size: int = 0) -> list[str]:
    """Pre-flight check before an audit. Returns a list of warnings.

    Checks:
    1. Is a token set? (if not, 60/hr limit makes large audits impossible)
    2. Is the core API budget sufficient for the audit size?
    3. Is the code search budget available (if independent search enabled)?

    Returns a list of warning strings. Empty list = all good, proceed.
    """
    warnings = []

    if not has_token():
        if sample_size == 0 or sample_size > 60:
            warnings.append(
                "No GITHUB_TOKEN set. Unauthenticated limit is 60 req/hr — "
                f"a {sample_size or 'full'}-instance audit will hit rate limits. "
                "Run /token paste to set your token (unlocks 5000/hr)."
            )
        return warnings

    # Refresh from API to get accurate numbers
    rate_tracker.refresh_from_api()
    core = rate_tracker.get_status("core")
    search = rate_tracker.get_status("search")

    # Estimate API calls needed:
    # - Regular path: 1 per instance (PR diff) + 1 per instance (code search) + 1 per instance (merge date)
    # - Pro path: 1 per unique repo (default branch) + 2 per instance (head + base file) + 1 per instance (code search)
    estimated_api_calls = sample_size * 3 if sample_size > 0 else 1500  # assume 500 instances
    estimated_search_calls = sample_size if sample_size > 0 else 500

    if core.remaining < estimated_api_calls:
        warnings.append(
            f"Core API budget low: {core.remaining}/{core.limit} remaining, "
            f"audit needs ~{estimated_api_calls}. "
            f"Resets in {core.resets_in_seconds}s — consider waiting."
        )

    if search.remaining < estimated_search_calls:
        # incomplete" which alarmed users who had a token with 5000/hr. The
        # 30/min is GitHub's CODE SEARCH limit (separate from the 5000/hr
        # core API limit). The audit will PAUSE and wait for the rate limit
        # to reset — it won't be incomplete, just slower.
        warnings.append(
            f"Code search rate limit: {search.remaining}/{search.limit} per minute. "
            f"Audit needs ~{estimated_search_calls} searches — this will take "
            f"~{estimated_search_calls // 28 + 1} minutes (30/min is GitHub's "
            f"hardcoded ceiling for /search/code, separate from your 5000/hr "
            f"core API limit). The audit will PAUSE between batches, not skip any."
        )

    return warnings
