"""Loaders for SWE-bench and other agent benchmark datasets."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

import pandas as pd


def find_data_file(filename: str) -> Optional[Path]:
    """Find a data file by searching common locations.

    Lookup order (most robust first):
    1. importlib.resources — the canonical way to locate data bundled inside
       an INSTALLED wheel. Works regardless of CWD, venv, or install path.
       This is what makes `pip install agentseal` "just work" with no manual
       download — the parquet files ship inside the wheel and are located
       via the package's own resource path.
    2. PyInstaller frozen mode (sys._MEIPASS).
    3. Source/dev mode (CWD-relative, package-relative, source-root-relative).

    Used by the CLI, TUI, and interactive mode. Single source of truth.
    """
    # 1. importlib.resources — works for installed wheels AND editable installs
    try:
        from importlib.resources import files as _ir_files
        # The data/ folder lives inside the agentseal package (see pyproject.toml
        # [tool.setuptools.package-data] -> agentseal = ["data/*.parquet", ...])
        candidate = _ir_files("agentseal").joinpath("data", filename)
        if candidate.is_file():
            return Path(str(candidate))
    except Exception:
        pass

    # 2. PyInstaller frozen mode
    if getattr(sys, 'frozen', False):
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            p = Path(meipass) / "data" / filename
            if p.exists():
                return p

    # 3. Source / dev mode — search the package dir, source root, and CWD
    for d in _get_search_dirs_fallback():
        try:
            p = Path(d) / filename
            if p.exists():
                return p
        except Exception:
            continue
    return None


def _get_search_dirs_fallback() -> list[str]:
    """Fallback search dirs for source/dev mode (used only if importlib.resources fails)."""
    dirs: list[str] = []
    # PyInstaller frozen mode
    if getattr(sys, 'frozen', False):
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            dirs.extend([
                str(Path(meipass) / "data"),
                str(Path(meipass) / "examples" / "data"),
                str(meipass),
            ])
        exe_dir = Path(sys.executable).parent
        dirs.extend([
            str(exe_dir / "data"),
            str(exe_dir / "examples" / "data"),
            str(exe_dir),
        ])
    # Source/pip mode — data is INSIDE the agentseal package
    try:
        pkg_dir = Path(__file__).resolve().parent  # agentseal/ package dir
        dirs.extend([
            str(pkg_dir / "data"),
            str(pkg_dir / "examples" / "data"),
            str(pkg_dir),
        ])
    except Exception:
        pass
    # Also check CWD-relative paths (for dev mode)
    dirs.extend(["data", "../data", "examples/data", "."])
    # And the source root (parent of package, for editable installs)
    try:
        pkg_root = Path(__file__).resolve().parent.parent
        dirs.extend([
            str(pkg_root / "data"),
            str(pkg_root / "examples" / "data"),
            str(pkg_root),
        ])
    except Exception:
        pass
    return dirs


# Keep the old name for backward compatibility with any external callers
_get_search_dirs = _get_search_dirs_fallback


from .schemas import BenchmarkInstance  # noqa: E402


def extract_pr_number(instance_id: str) -> Optional[int]:
    """Extract a PR number only from a SWE-bench-style instance_id.

    Earlier builds accepted any trailing ``-NNNN`` suffix. That produced broken
    GitHub ``/pull/NNNN`` links for custom/HF datasets where the suffix is an
    issue id, row id, or task id. SWE-bench ids look like
    ``owner__repo-NNNNN``; for anything else we return None.
    """
    m = re.match(r"^[^_]+__[^/]+-(\d+)$", str(instance_id or ""))
    return int(m.group(1)) if m else None


def github_pr_url_from_instance(instance_id: str, repo: str) -> tuple[Optional[int], Optional[str]]:
    """Return a GitHub PR URL only when the SWE-bench id matches the repo.

    A malicious or custom dataset can contain an id like ``django__django-1``
    while the repo column says ``other/project``. Older builds would still emit
    ``https://github.com/other/project/pull/1``, creating broken evidence links.
    Only derive PR URLs when the encoded owner/repo equals the repo column.
    """
    text = str(instance_id or "")
    m = re.match(r"^([^_]+)__([^/]+)-(\d+)$", text)
    if not m or not repo or "/" not in str(repo):
        return None, None
    owner, repo_name, num = m.groups()
    expected = f"{owner}/{repo_name}".lower()
    if str(repo).strip().lower() != expected:
        return None, None
    pr_num = int(num)
    return pr_num, f"https://github.com/{repo}/pull/{pr_num}"


def load_swebench_verified(parquet_path: str | Path) -> list[BenchmarkInstance]:
    """Load the SWE-bench Verified dataset from a local parquet file.

    Download from:
    https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified

    The file is ~2 MB and contains 500 instances.
    """
    df = pd.read_parquet(parquet_path)
    instances: list[BenchmarkInstance] = []
    def _cell(value, default="") -> str:
        try:
            if value is None or pd.isna(value):
                return default
        except Exception:
            pass
        text = str(value)
        return text if text and text.lower() != "nan" else default

    for _, row in df.iterrows():
        instance_id = _cell(row.get("instance_id", ""), f"instance-{len(instances)}")
        repo = _cell(row.get("repo", ""), "unknown/repo")
        pr_num, pr_url = github_pr_url_from_instance(instance_id, repo)
        created_at = _cell(row.get("created_at", ""), "")
        instances.append(
            BenchmarkInstance(
                instance_id=instance_id,
                repo=repo,
                base_commit=_cell(row.get("base_commit", ""), ""),
                patch=_cell(row.get("patch", ""), ""),
                test_patch=_cell(row.get("test_patch", ""), ""),
                problem_statement=_cell(row.get("problem_statement", ""), ""),
                created_at=created_at or None,
                pr_number=pr_num,
                pr_url=pr_url,
            )
        )
    return instances


def load_swebench_sample(parquet_path: str | Path, n: int = 10) -> list[BenchmarkInstance]:
    """Load only the first ``n`` instances — useful for quick tests."""
    all_instances = load_swebench_verified(parquet_path)
    return all_instances[:n]
