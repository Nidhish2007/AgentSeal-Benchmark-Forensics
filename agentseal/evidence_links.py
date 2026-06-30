"""Strict evidence-link rendering helpers for AgentSeal reports.

Reports must not present brittle GitHub search replays or synthetic/broken URLs
as clickable proof. The audit JSON may still store recorded provenance, but the
HTML/Markdown renderer only turns a URL into a clickable link after basic safety
checks and, by default, a lightweight live preflight.
"""

from __future__ import annotations

import os
import re
import threading
from functools import lru_cache
from urllib.parse import urlparse, urlunparse


_PLACEHOLDER_OWNERS = {
    "example", "example-org", "example-user", "external-org", "mirror-labs",
    "test-org", "test-user", "dummy", "placeholder", "localhost",
}
_PLACEHOLDER_RE = re.compile(r"/(?:example|dummy|placeholder|test|fixture|corpus-copy|duplicate-bug)(?:[-_/]|$)", re.I)
_LOCK = threading.Lock()
_PREFLIGHT_COUNT = 0


def _strip_fragment(url: str) -> str:
    p = urlparse(url or "")
    return urlunparse((p.scheme, p.netloc, p.path, p.params, p.query, ""))


def is_github_url(url: str) -> bool:
    try:
        p = urlparse(url or "")
        return p.scheme == "https" and p.netloc.lower() == "github.com" and bool(p.path.strip("/"))
    except Exception:
        return False


def github_owner_repo(url: str) -> tuple[str, str]:
    if not is_github_url(url):
        return "", ""
    parts = [x for x in urlparse(url).path.split("/") if x]
    if len(parts) < 2:
        return "", ""
    return parts[0], parts[1]


def looks_like_placeholder_url(url: str) -> bool:
    owner, repo = github_owner_repo(url)
    if owner.lower() in _PLACEHOLDER_OWNERS:
        return True
    joined = f"/{owner}/{repo}/"
    if _PLACEHOLDER_RE.search(joined):
        return True
    return False


def _preflight_limit_reached() -> bool:
    raw = os.environ.get("AGENTSEAL_EVIDENCE_LINK_PREFLIGHT_MAX", "250").strip()
    try:
        max_checks = int(raw)
    except Exception:
        max_checks = 25
    if max_checks <= 0:
        return True
    global _PREFLIGHT_COUNT
    with _LOCK:
        if _PREFLIGHT_COUNT >= max_checks:
            return True
        _PREFLIGHT_COUNT += 1
        return False


@lru_cache(maxsize=512)
def preflight_github_url(url: str) -> tuple[bool, str]:
    """Return (is_clickable, status) for a public GitHub evidence URL.

    The fragment (#Lx-Ly) is ignored for the HTTP check. A 2xx/3xx page is
    considered clickable. 404/401/403/network failures are deliberately not
    clickable because the report should not invite users to click dead proof.
    """
    if not is_github_url(url):
        return False, "not_github_url"
    if looks_like_placeholder_url(url):
        return False, "placeholder_or_synthetic_url"
    if os.environ.get("AGENTSEAL_EVIDENCE_LINK_PREFLIGHT", "1").strip().lower() in {"0", "false", "no", "off"}:
        return True, "preflight_disabled"
    if _preflight_limit_reached():
        return False, "preflight_limit_reached"
    try:
        import requests
        base = _strip_fragment(url)
        timeout = float(os.environ.get("AGENTSEAL_EVIDENCE_LINK_TIMEOUT", "1.5"))
        headers = {"User-Agent": "AgentSeal evidence-link preflight"}
        # pages during report rendering. Some routes may reject HEAD, so fall
        # back to GET only on method-related/ambiguous statuses.
        r = requests.head(base, timeout=timeout, allow_redirects=True, headers=headers)
        if int(r.status_code) in {405, 501} or int(r.status_code) >= 500:
            r = requests.get(base, timeout=timeout, allow_redirects=True, headers=headers)
        if 200 <= int(r.status_code) < 400:
            return True, "http_ok"
        return False, f"http_{int(r.status_code)}"
    except Exception as exc:
        return False, f"network_unavailable:{type(exc).__name__}"


def clickable_evidence_url(url: str, *, source_kind: str = "", verification_status: str = "") -> tuple[str, str]:
    """Return (clickable_url_or_empty, status_label).

    Only GitHub URLs from evidence-like sources are eligible. The renderer may
    still display a status note when the URL is rejected, but should not render
    it as a clickable proof link.
    """
    url = (url or "").strip()
    if not url:
        return "", "missing_url"
    if not is_github_url(url):
        return "", "non_github_url"
    if looks_like_placeholder_url(url):
        return "", "placeholder_or_synthetic_url"
    kind = (source_kind or "").lower()
    status = (verification_status or "").lower()
    evidence_like = (
        "github_code_search" in kind
        or "github_issue_search" in kind
        or "source_pr" in kind
        or "exact" in status
        or "github_issue" in status
    )
    if not evidence_like:
        return "", "not_verified_evidence_kind"
    ok, preflight_status = preflight_github_url(url)
    if ok:
        return url, preflight_status
    return "", preflight_status
