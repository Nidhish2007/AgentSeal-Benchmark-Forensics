"""AgentSeal /auto command — autonomous benchmark discovery, download, and audit.

/auto <name> and AgentSeal:
1. Checks if the benchmark is in the known registry
2. If not, auto-discovers via HuggingFace → GitHub → PyPI
3. Downloads to ~/.agentseal/downloads/ (persistent cache, 7-day TTL)
4. Auto-detects the schema (column names vary across benchmarks)
5. Selects audit mode: PR-diff (SWE-bench) or solution (HumanEval/MBPP)
6. Runs the full audit
7. Generates report to ~/.agentseal/reports/

No manual downloads. No format guessing. No schema mapping. One command.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests


# ═══════════════════════════════════════════════════════════════════════════
# BENCHMARK REGISTRY — known benchmarks AgentSeal can audit
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class BenchmarkInfo:
    """Metadata about a known benchmark."""
    name: str                    # short name for /auto
    hf_id: str                   # HuggingFace dataset ID
    instances: int               # expected instance count
    audit_type: str              # "pr_diff" or "solution"
    languages: list              # programming languages
    description: str = ""
    # Column mapping (benchmark-specific column names → AgentSeal's standard names)
    # If empty, auto-detection is used
    column_map: dict = field(default_factory=dict)


REGISTRY: dict[str, BenchmarkInfo] = {
    # ── PR-diff benchmarks (SWE-bench family) ──
    "swe-bench-verified": BenchmarkInfo(
        name="swe-bench-verified",
        hf_id="princeton-nlp/SWE-bench_Verified",
        instances=500,
        audit_type="pr_diff",
        languages=["python"],
        description="SWE-bench Verified — 500 human-validated instances from 12 Python repos",
    ),
    "swe-bench": BenchmarkInfo(
        name="swe-bench",
        hf_id="SWE-bench/SWE-bench",
        instances=2294,
        audit_type="pr_diff",
        languages=["python"],
        description="SWE-bench Full — 2,294 instances from 12 Python repos",
    ),
    "swe-bench-lite": BenchmarkInfo(
        name="swe-bench-lite",
        hf_id="princeton-nlp/SWE-bench_Lite",
        instances=300,
        audit_type="pr_diff",
        languages=["python"],
        description="SWE-bench Lite — 300 simpler instances",
    ),
    "multi-swe-bench": BenchmarkInfo(
        name="multi-swe-bench",
        hf_id="ByteDance-Seed/Multi-SWE-bench",
        instances=1632,
        audit_type="pr_diff",
        languages=["java", "cpp", "javascript", "go", "rust", "c", "php", "ruby", "python"],
        description="Multi-SWE-bench — 1,632 instances across 9 programming languages",
    ),
    # ── Solution benchmarks (standalone code) ──
    "humaneval": BenchmarkInfo(
        name="humaneval",
        hf_id="openai/openai_humaneval",
        instances=164,
        audit_type="solution",
        languages=["python"],
        description="HumanEval — 164 Python function generation problems",
        column_map={
            "instance_id": "task_id",
            "patch": "canonical_solution",
            "problem_statement": "prompt",
        },
    ),
    "mbpp": BenchmarkInfo(
        name="mbpp",
        hf_id="google-research-datasets/mbpp",
        instances=974,
        audit_type="solution",
        languages=["python"],
        description="MBPP — 974 basic Python programming problems",
        column_map={
            "instance_id": "task_id",
            "patch": "code",
            "problem_statement": "text",
        },
    ),
    "bigcodebench": BenchmarkInfo(
        name="bigcodebench",
        hf_id="bigcode/bigcodebench",
        instances=1140,
        audit_type="solution",
        languages=["python"],
        description="BigCodeBench — 1,140 practical coding tasks",
    ),
}


# ═══════════════════════════════════════════════════════════════════════════
# DOWNLOAD CACHE — persistent storage at ~/.agentseal/downloads/
# ═══════════════════════════════════════════════════════════════════════════

CACHE_DIR = Path.home() / ".agentseal" / "downloads"
CACHE_TTL = 7 * 24 * 3600  # 7 days


def _cache_path(benchmark_name: str) -> Path:
    """Get the cache directory for a benchmark, with path traversal blocked.

    The benchmark name can be user-provided (e.g. `/auto owner/dataset`). On
    Windows, both forward-slash and backslash are path separators, and names like `..` or
    `C:/tmp` must never influence the cache location. Keep only a compact,
    filesystem-safe slug under ~/.agentseal/downloads.
    """
    raw = str(benchmark_name or "benchmark")
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw.replace("/", "_").replace("\\", "_"))
    safe_name = safe_name.strip("._ ") or "benchmark"
    return CACHE_DIR / safe_name[:160]


def _safe_download_name(name: str, default: str = "data") -> str:
    """Return a single safe filename for remote dataset artifacts."""
    raw = Path(str(name or default)).name
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw.replace("/", "_").replace("\\", "_"))
    safe = safe.strip("._ ") or default
    return safe[:180]


def _is_cache_valid(path: Path) -> bool:
    """Check if cached data is still valid (exists + not expired)."""
    data_file = _find_data_file(path)
    if data_file is None:
        return False
    try:
        return (time.time() - data_file.stat().st_mtime) < CACHE_TTL
    except OSError:
        return False


def _find_data_file(path: Path) -> Optional[Path]:
    """Find the main data file in a cache directory, recursively and safely."""
    if not path.exists():
        return None
    base = path.resolve()
    candidates = []
    for ext in [".parquet", ".jsonl", ".json"]:
        for f in path.rglob(f"*{ext}"):
            try:
                if f.is_symlink() or not f.is_file():
                    continue
                f.resolve().relative_to(base)
                candidates.append(f)
            except Exception:
                continue
    if candidates:
        return max(candidates, key=lambda f: f.stat().st_size)
    return None



def _format_bytes(n: int | float | None) -> str:
    """Human-readable byte counter for download progress."""
    try:
        n = float(n or 0)
    except Exception:
        n = 0.0
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    if i == 0:
        return f"{int(n)} {units[i]}"
    return f"{n:.1f} {units[i]}"


def _emit(progress_callback, stage: str, message: str) -> None:
    """Best-effort progress callback; never let UI logging crash work."""
    if not progress_callback:
        return
    try:
        progress_callback(stage, message)
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════════════════════
# AUTO-DISCOVERY — HuggingFace → GitHub → PyPI
# ═══════════════════════════════════════════════════════════════════════════

def discover_and_download(
    name: str,
    progress_callback=None,
) -> tuple[Path, BenchmarkInfo]:
    """Discover and download a benchmark by name.

    Returns (data_file_path, BenchmarkInfo).
    Raises ValueError if the benchmark cannot be found.
    """
    if not name or not name.strip():
        raise ValueError("Benchmark name is empty. Use /auto to list known benchmarks.")
    # 1. Check registry first
    if name in REGISTRY:
        info = REGISTRY[name]
    else:
        # Try to find in registry by partial match
        for key, val in REGISTRY.items():
            if name.lower() in key.lower() or key.lower() in name.lower():
                info = val
                break
        else:
            # Not in registry — create a generic entry and try to discover
            info = BenchmarkInfo(
                name=name,
                hf_id=name,
                instances=0,
                audit_type="pr_diff",  # default, will be auto-detected
                languages=[],
                description=f"Auto-discovered benchmark: {name}",
            )

    # 2. Check cache
    cache_dir = _cache_path(info.hf_id)
    if _is_cache_valid(cache_dir):
        data_file = _find_data_file(cache_dir)
        if data_file:
            if progress_callback:
                progress_callback("cache", f"Loaded from cache: {data_file.name}")
            return data_file, info

    # 3. Download from HuggingFace
    cache_dir.mkdir(parents=True, exist_ok=True)
    data_file = _download_from_huggingface(info.hf_id, cache_dir, progress_callback)
    if data_file:
        return data_file, info

    # 4. Try GitHub
    data_file = _download_from_github(name, cache_dir, progress_callback)
    if data_file:
        return data_file, info

    # 5. Try PyPI
    data_file = _download_from_pypi(name, cache_dir, progress_callback)
    if data_file:
        return data_file, info

    raise ValueError(
        f"Could not find '{name}' on HuggingFace, GitHub, or PyPI. "
        f"Try /auto (no arguments) to see known benchmarks, "
        f"or use the full HuggingFace dataset ID (e.g., /auto princeton-nlp/SWE-bench_Verified)."
    )


def _download_from_huggingface(
    hf_id: str,
    cache_dir: Path,
    progress_callback=None,
) -> Optional[Path]:
    """Download a dataset from HuggingFace with visible stage progress.

    Public datasets download without a token. Gated datasets need `/hf paste` or
    HF_TOKEN/HUGGINGFACE_TOKEN in the environment; when no token is present the
    UI says that up front instead of silently looking frozen.
    """
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        _emit(progress_callback, "discover", f"Checking HuggingFace: {hf_id}")

        from .github_auth import get_hf_auth_headers, has_hf_token
        hf_headers = get_hf_auth_headers()
        if has_hf_token():
            _emit(progress_callback, "auth", "HuggingFace token detected; gated datasets can download if access was accepted.")
        else:
            _emit(progress_callback, "auth", "No HuggingFace token set; public datasets can download, gated datasets need /hf paste.")

        api_url = f"https://huggingface.co/api/datasets/{hf_id}"
        r = requests.get(api_url, timeout=15, headers=hf_headers)

        if r.status_code in (401, 403) and not has_hf_token():
            _emit(
                progress_callback,
                "auth",
                f"HuggingFace returned HTTP {r.status_code} for {hf_id}. Run /hf paste, then /hf test, then retry /auto.",
            )
            return None

        if r.status_code != 200:
            _emit(progress_callback, "discover", f"HuggingFace metadata unavailable for {hf_id}: HTTP {r.status_code}")
            return None

        data = r.json()
        siblings = data.get("siblings", [])
        parquet_files = [s.get("rfilename", "") for s in siblings if str(s.get("rfilename", "")).endswith(".parquet")]
        jsonl_files = [s.get("rfilename", "") for s in siblings if str(s.get("rfilename", "")).endswith(".jsonl")]
        parquet_files = [f for f in parquet_files if f]
        jsonl_files = [f for f in jsonl_files if f]
        files_to_try = parquet_files + jsonl_files
        _emit(progress_callback, "discover", f"HuggingFace files: {len(parquet_files)} parquet, {len(jsonl_files)} JSONL")

        # Multi-SWE-bench-style repositories contain many JSONL shards. Download
        # and combine with visible per-shard progress so the TUI does not look
        # dead for minutes.
        split_names = ("train", "test", "validation", "dev")
        if jsonl_files and not parquet_files and len(jsonl_files) > 1 and not any(
            Path(name).stem.lower() in split_names for name in jsonl_files
        ):
            combined = cache_dir / f"{hf_id.replace('/', '__')}__combined.jsonl"
            total = len(jsonl_files)
            _emit(progress_callback, "download", f"Downloading + combining {total} JSONL shards. CodeSeal/Bloom activate after dataset load.")
            written = 0
            approx_rows = 0
            with open(combined, "wb") as out:
                for i, filename in enumerate(jsonl_files, start=1):
                    safe_filename = _safe_download_name(filename, f"shard_{i}.jsonl")
                    url = f"https://huggingface.co/datasets/{hf_id}/resolve/main/{filename}"
                    tmp = cache_dir / safe_filename
                    _emit(progress_callback, "download", f"Shard {i}/{total}: {Path(filename).name}")
                    ok = _download_file(
                        url,
                        tmp,
                        headers=hf_headers,
                        progress_callback=progress_callback,
                        label=f"shard {i}/{total} {Path(filename).name}",
                    )
                    if not ok:
                        _emit(progress_callback, "download", f"Shard {i}/{total} skipped: download failed")
                        continue
                    # Stream shards line-by-line so large JSONL files do not
                    # block the terminal UI or spike memory.
                    shard_rows = 0
                    last_combine_emit = time.monotonic()
                    try:
                        with open(tmp, "rb") as src:
                            for line in src:
                                if not line.strip():
                                    continue
                                out.write(line.rstrip(b"\n"))
                                out.write(b"\n")
                                shard_rows += 1
                                approx_rows += 1
                                now = time.monotonic()
                                if shard_rows == 1 or shard_rows % 50000 == 0 or now - last_combine_emit >= 2.0:
                                    last_combine_emit = now
                                    _emit(
                                        progress_callback,
                                        "combine",
                                        f"Combining shard {i}/{total}: {shard_rows:,} rows; total approx {approx_rows:,}",
                                    )
                    except OSError as exc:
                        _emit(progress_callback, "combine", f"Shard {i}/{total} read failed: {exc}")
                        continue
                    if shard_rows <= 0:
                        _emit(progress_callback, "combine", f"Shard {i}/{total} skipped: empty")
                        continue
                    written += 1
                    _emit(progress_callback, "combine", f"Combined shard {i}/{total}; written {written}/{total}; approx rows {approx_rows:,}")
            if written:
                _emit(progress_callback, "cache", f"Combined dataset cached: {combined.name} ({written}/{total} shards, approx {approx_rows:,} rows)")
                return combined

        if not files_to_try:
            # HuggingFace auto-converts datasets to parquet.
            try:
                convert_url = f"https://huggingface.co/api/datasets/{hf_id}/parquet"
                _emit(progress_callback, "discover", "Trying HuggingFace auto-converted parquet endpoint...")
                r2 = requests.get(convert_url, timeout=15, headers=hf_headers)
                if r2.status_code in (401, 403) and not has_hf_token():
                    _emit(progress_callback, "auth", f"Parquet endpoint returned HTTP {r2.status_code}. Run /hf paste and retry.")
                    return None
                if r2.status_code == 200:
                    convert_data = r2.json()
                    parquet_urls = convert_data.get("parquet_files", [])
                    _emit(progress_callback, "discover", f"Auto-converted parquet files: {len(parquet_urls)}")
                    for pq in parquet_urls:
                        if pq.get("split") in ["test", "train", "validation"]:
                            url = pq.get("url")
                            if url:
                                filename = _safe_download_name(pq.get("filename", "data.parquet"), "data.parquet")
                                dest = cache_dir / filename
                                _emit(progress_callback, "download", f"Downloading {filename}...")
                                if _download_file(url, dest, headers=hf_headers, progress_callback=progress_callback, label=filename):
                                    _emit(progress_callback, "cache", f"Downloaded dataset cached: {dest.name}")
                                    return dest
                else:
                    _emit(progress_callback, "discover", f"Parquet endpoint unavailable: HTTP {r2.status_code}")
            except Exception as exc:
                _emit(progress_callback, "discover", f"Parquet endpoint failed: {exc}")
            return None

        # Download the first suitable file, preferring train/test/validation.
        for filename in files_to_try:
            if "test" in filename or "train" in filename or "validation" in filename or len(files_to_try) == 1:
                safe_filename = _safe_download_name(filename, "data")
                url = f"https://huggingface.co/datasets/{hf_id}/resolve/main/{filename}"
                dest = cache_dir / safe_filename
                _emit(progress_callback, "download", f"Downloading {safe_filename}...")
                if _download_file(url, dest, headers=hf_headers, progress_callback=progress_callback, label=safe_filename):
                    _emit(progress_callback, "cache", f"Downloaded dataset cached: {dest.name}")
                    return dest

        if files_to_try:
            filename = files_to_try[0]
            safe_filename = _safe_download_name(filename, "data")
            url = f"https://huggingface.co/datasets/{hf_id}/resolve/main/{filename}"
            dest = cache_dir / safe_filename
            _emit(progress_callback, "download", f"Downloading {safe_filename}...")
            if _download_file(url, dest, headers=hf_headers, progress_callback=progress_callback, label=safe_filename):
                _emit(progress_callback, "cache", f"Downloaded dataset cached: {dest.name}")
                return dest

        return None
    except Exception as exc:
        _emit(progress_callback, "discover", f"HuggingFace discovery failed: {exc}")
        return None

def _download_from_github(
    name: str,
    cache_dir: Path,
    progress_callback=None,
) -> Optional[Path]:
    """Download a benchmark from a GitHub repo."""
    try:
        if progress_callback:
            progress_callback("discover", f"Checking GitHub: {name}")

        from .github_auth import get_auth_headers, rate_tracker
        headers = get_auth_headers()

        # List repo contents
        url = f"https://api.github.com/repos/{name}/contents/"
        r = requests.get(url, headers=headers, timeout=15)
        rate_tracker.update_from_response(r, "core")

        if r.status_code != 200:
            return None

        items = r.json()
        # Look for data files
        for item in items:
            if item.get("type") == "file":
                fname = item.get("name", "")
                if fname.endswith((".parquet", ".jsonl", ".json")):
                    download_url = item.get("download_url")
                    if download_url:
                        safe_fname = _safe_download_name(fname, "data")
                        dest = cache_dir / safe_fname
                        if progress_callback:
                            progress_callback("download", f"Downloading {safe_fname}...")
                        if _download_file(download_url, dest, progress_callback=progress_callback, label=dest.name):
                            return dest

        return None
    except Exception:
        return None


def _download_from_pypi(
    name: str,
    cache_dir: Path,
    progress_callback=None,
) -> Optional[Path]:
    """Download a benchmark from a PyPI package."""
    try:
        if progress_callback:
            progress_callback("discover", f"Checking PyPI: {name}")

        r = requests.get(f"https://pypi.org/pypi/{name}/json", timeout=15)
        if r.status_code != 200:
            return None

        data = r.json()
        urls = data.get("urls", [])
        # Look for source distribution
        for url_info in urls:
            if url_info.get("packagetype") == "sdist":
                download_url = url_info.get("url")
                if download_url:
                    dest = cache_dir / f"{name}.tar.gz"
                    if progress_callback:
                        progress_callback("download", f"Downloading {name}.tar.gz...")
                    if _download_file(download_url, dest, progress_callback=progress_callback, label=dest.name):
                        # Extract and look for data files
                        import tarfile
                        # SECURITY: never call extractall() on downloaded sdists.
                        # Manually copy only regular files/directories that stay
                        # inside cache_dir; reject symlinks, hardlinks, devices,
                        # absolute paths, and traversal members.
                        _base = Path(cache_dir).resolve()
                        with tarfile.open(dest) as tf:
                            for member in tf.getmembers():
                                target = (_base / member.name).resolve()
                                try:
                                    target.relative_to(_base)
                                except ValueError:
                                    raise ValueError(
                                        f"Unsafe tarball member escapes cache dir: {member.name}")
                                if member.isdir():
                                    target.mkdir(parents=True, exist_ok=True)
                                    continue
                                if not member.isfile():
                                    continue
                                target.parent.mkdir(parents=True, exist_ok=True)
                                src = tf.extractfile(member)
                                if src is None:
                                    continue
                                with src, target.open("wb") as out_f:
                                    for chunk in iter(lambda: src.read(1024 * 1024), b""):
                                        out_f.write(chunk)
                        # Find data files
                        data_file = _find_data_file(cache_dir)
                        if data_file:
                            return data_file
        return None
    except Exception:
        return None


def _download_file(
    url: str,
    dest: Path,
    timeout: int = 120,
    headers: dict | None = None,
    progress_callback=None,
    label: str | None = None,
) -> bool:
    """Download a file with visible, bounded progress.

    `progress_callback` receives human-readable stage messages so the TUI never
    appears frozen during large HuggingFace JSONL/parquet downloads.
    """
    label = label or dest.name
    try:
        hdrs = {"User-Agent": "AgentSeal/5.0"}
        if headers:
            hdrs.update(headers)
        _emit(progress_callback, "download", f"Starting {label}...")
        r = requests.get(url, stream=True, timeout=timeout, headers=hdrs)
        if r.status_code != 200:
            _emit(progress_callback, "download", f"Skipped {label}: HTTP {r.status_code}")
            return False
        total = 0
        try:
            total = int(r.headers.get("content-length") or 0)
        except Exception:
            total = 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        done = 0
        next_emit = 0
        # Emit at most about 20 progress messages for known-size files, and
        # every 5 MB for unknown-size streams.
        step = max(total // 20, 1024 * 1024) if total else 5 * 1024 * 1024
        if total:
            _emit(progress_callback, "download", f"{label}: 0% ({_format_bytes(0)} / {_format_bytes(total)})")
        last_emit_time = time.monotonic()
        last_yield_time = last_emit_time
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 64):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                # Emit on byte threshold OR time threshold. The time threshold
                # matters for slow public HF downloads where content-length may
                # be missing and chunks arrive slowly; the TUI then still shows
                # life instead of appearing frozen.
                if done >= next_emit or now - last_emit_time >= 1.5:
                    if total:
                        pct = min(100.0, done * 100.0 / max(1, total))
                        _emit(progress_callback, "download", f"{label}: {pct:5.1f}% ({_format_bytes(done)} / {_format_bytes(total)})")
                    else:
                        _emit(progress_callback, "download", f"{label}: {_format_bytes(done)} downloaded")
                    next_emit = done + step
                    last_emit_time = now
                # Give the scheduler/UI room during long local writes.
                if now - last_yield_time >= 0.25:
                    time.sleep(0)
                    last_yield_time = now
        if total:
            _emit(progress_callback, "download", f"Finished {label}: {_format_bytes(done)}")
        else:
            _emit(progress_callback, "download", f"Finished {label}: {_format_bytes(done)} downloaded")
        return True
    except Exception as exc:
        _emit(progress_callback, "download", f"Failed {label}: {exc}")
        return False


# ═══════════════════════════════════════════════════════════════════════════
# AUTO-SCHEMA DETECTION
# ═══════════════════════════════════════════════════════════════════════════

# Column name variations across benchmarks
_COLUMN_ALIASES = {
    "instance_id": ["instance_id", "id", "task_id", "problem_id", "name"],
    "repo": ["repo", "repository", "source_repo", "project"],
    "patch": ["patch", "fix_patch", "solution", "canonical_solution", "gold_patch", "answer", "code", "reference_solution"],
    "problem_statement": ["problem_statement", "body", "description", "prompt", "text", "question", "instruct_prompt"],
    "test_patch": ["test_patch", "tests", "test", "test_list", "test_code"],
    "base_commit": ["base_commit", "commit_id", "base_sha"],
}


def detect_schema(df) -> dict:
    """Auto-detect the column mapping for a benchmark DataFrame.

    Returns a dict mapping AgentSeal's standard names to the DataFrame's column names.
    """
    columns = set(df.columns)
    mapping = {}

    for standard_name, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in columns:
                mapping[standard_name] = alias
                break
        # Also try case-insensitive
        if standard_name not in mapping:
            for alias in aliases:
                for col in columns:
                    if col.lower() == alias.lower():
                        mapping[standard_name] = col
                        break
                if standard_name in mapping:
                    break

    return mapping


def detect_audit_type(df, schema: dict) -> str:
    """Detect whether to use PR-diff mode or solution mode.

    PR-diff mode: has 'repo' and 'base_commit' columns → SWE-bench family
    Solution mode: has 'patch'/'solution' but no 'repo' → HumanEval/MBPP
    """
    if "repo" in schema and "base_commit" in schema:
        return "pr_diff"
    if "repo" in schema:
        return "pr_diff"  # has repo info, try PR-diff
    return "solution"  # no repo → standalone solution


def instances_from_dataframe(df, schema: dict):
    """Normalize a benchmark DataFrame into AgentSeal BenchmarkInstance rows.

    Shared by /auto and the TUI wizard so custom files get the same schema
    mapping, org/repo handling, and safe PR derivation.
    """
    from .loaders import BenchmarkInstance

    def cell_text(value, default: str = "") -> str:
        if value is None:
            return default
        if hasattr(value, "tolist"):
            value = value.tolist()
        if isinstance(value, float):
            try:
                if value != value:
                    return default
            except Exception:
                pass
        if isinstance(value, (list, tuple, set)):
            return "\n".join(str(v) for v in value)
        if isinstance(value, dict):
            return json.dumps(value, sort_keys=True)
        text = str(value)
        if text.lower() in {"nan", "none", "null"}:
            return default
        return text

    instances = []
    for _, row in df.iterrows():
        instance_id = cell_text(row.get(schema.get("instance_id", "instance_id"), ""), f"instance-{len(instances)}")
        repo = cell_text(row.get(schema.get("repo", "repo"), "unknown/repo"), "unknown/repo")
        patch = cell_text(row.get(schema.get("patch", "patch"), ""))
        test_patch = cell_text(row.get(schema.get("test_patch", "test_patch"), ""))
        problem_statement = cell_text(row.get(schema.get("problem_statement", "problem_statement"), ""))
        base_commit = cell_text(row.get(schema.get("base_commit", "base_commit"), ""))

        org = cell_text(row.get("org", ""), "")
        if org and repo and "/" not in repo and repo != "unknown/repo":
            repo = f"{org}/{repo}"
        if not base_commit:
            base = row.get("base", {})
            if isinstance(base, dict):
                base_commit = str(base.get("sha", "") or "")

        pr_number, pr_url = _safe_pr_from_row(row, instance_id, repo)

        instances.append(BenchmarkInstance(
            instance_id=instance_id,
            repo=repo,
            base_commit=base_commit,
            patch=patch,
            test_patch=test_patch,
            problem_statement=problem_statement,
            pr_number=pr_number,
            pr_url=pr_url,
        ))
    return instances


def _stratified_sample_df(df, sample_size: int, schema: dict):
    """Return a deterministic round-robin sample across repo/language groups."""
    if sample_size <= 0 or sample_size >= len(df):
        return df

    repo_col = schema.get("repo")
    language_cols = [c for c in ("language", "lang", "_agentseal_language") if c in df.columns]
    has_org_repo = "org" in df.columns and repo_col in df.columns
    if not language_cols and repo_col not in df.columns and not has_org_repo:
        return df.head(sample_size)

    groups: dict[tuple, list] = {}
    order: list[tuple] = []
    for idx, row in df.iterrows():
        parts = []
        if language_cols:
            parts.append(str(row.get(language_cols[0], "") or ""))
        if has_org_repo:
            parts.append(f"{row.get('org', '')}/{row.get(repo_col, '')}")
        elif repo_col in df.columns:
            parts.append(str(row.get(repo_col, "") or ""))
        key = tuple(parts) if parts else ("__all__",)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(idx)

    selected = []
    round_index = 0
    while len(selected) < sample_size:
        added = False
        for key in order:
            items = groups[key]
            if round_index < len(items):
                selected.append(items[round_index])
                added = True
                if len(selected) >= sample_size:
                    break
        if not added:
            break
        round_index += 1

    return df.loc[selected]


def _row_group_key(row: dict) -> tuple:
    lang = row.get("language") or row.get("lang") or row.get("_agentseal_language") or ""
    org = row.get("org") or ""
    repo = row.get("repo") or row.get("repository") or row.get("source_repo") or ""
    if org and repo and "/" not in str(repo):
        repo = f"{org}/{repo}"
    parts = []
    if lang:
        parts.append(str(lang))
    if repo:
        parts.append(str(repo))
    return tuple(parts) if parts else ("__all__",)



def _read_jsonl_dataframe_streaming(data_file: Path, progress_callback=None, chunksize: int = 5000):
    """Read a JSONL file with progress instead of a silent full pandas load.

    This is primarily a UX/freeze hardening path: pandas.read_json(lines=True)
    can be CPU-heavy and silent, which makes the TUI look stuck even when the
    worker thread is alive. Reading in chunks emits progress and yields between
    chunks.
    """
    import pandas as pd

    frames = []
    rows = 0
    last_emit = time.monotonic()
    _emit(progress_callback, "load", f"Reading JSONL in chunks from {data_file.name}...")
    try:
        reader = pd.read_json(data_file, lines=True, chunksize=max(1, int(chunksize)))
        for chunk in reader:
            frames.append(chunk)
            rows += len(chunk)
            now = time.monotonic()
            if rows <= len(chunk) or rows % (chunksize * 2) == 0 or now - last_emit >= 1.5:
                last_emit = now
                _emit(progress_callback, "load", f"Loaded {rows:,} JSONL rows so far...")
            time.sleep(0)
        if not frames:
            return pd.DataFrame()
        _emit(progress_callback, "load", f"Concatenating {len(frames):,} JSONL chunks ({rows:,} rows)...")
        return pd.concat(frames, ignore_index=True)
    except ValueError:
        return pd.DataFrame()

def _stream_jsonl_stratified_sample(data_file: Path, sample_size: int, progress_callback=None):
    """Load a small stratified JSONL sample without materializing huge files."""
    import pandas as pd

    groups: dict[tuple, list] = {}
    order: list[tuple] = []
    lines_seen = 0
    # Stop once enough valid candidate rows are loaded; grouping is used only to
    # diversify the selected sample.
    min_scan = max(200, min(5000, sample_size * 200))
    max_scan = max(min_scan, sample_size * 1000)
    candidates_seen = 0
    with open(data_file, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            lines_seen += 1
            if lines_seen == 1 or lines_seen % 5000 == 0:
                _emit(progress_callback, "load", f"Streaming JSONL sample: scanned {lines_seen:,} lines; groups {len(order)}; candidates {candidates_seen:,}")
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = _row_group_key(row)
            if key not in groups:
                groups[key] = []
                order.append(key)
            if len(groups[key]) < sample_size:
                groups[key].append(row)
                candidates_seen += 1
            if candidates_seen >= sample_size and (lines_seen >= min_scan or len(order) >= sample_size):
                break
            if lines_seen >= max_scan and candidates_seen >= sample_size:
                break

    _emit(progress_callback, "load", f"Streaming JSONL sample selected candidates from {lines_seen:,} scanned lines")

    selected = []
    round_index = 0
    while len(selected) < sample_size:
        added = False
        for key in order:
            rows = groups[key]
            if round_index < len(rows):
                selected.append(rows[round_index])
                added = True
                if len(selected) >= sample_size:
                    break
        if not added:
            break
        round_index += 1

    return pd.DataFrame(selected)


def _coerce_optional_int(value):
    """Return int(value) for real PR-number columns; otherwise None."""
    try:
        if value is None:
            return None
        text = str(value).strip()
        if not text or text.lower() in {"nan", "none", "null"}:
            return None
        if text.endswith(".0"):
            text = text[:-2]
        return int(text) if text.isdigit() else None
    except Exception:
        return None


def _explicit_pr_url_from_row(row) -> str | None:
    """Use an explicit GitHub pull-request URL if the dataset provides one."""
    for col in ("pr_url", "pull_request_url", "pull_url", "github_pr_url", "html_url", "url"):
        try:
            val = row.get(col, "")
        except Exception:
            val = ""
        text = str(val or "").strip()
        if "github.com/" in text and "/pull/" in text:
            return text
    return None


def _safe_pr_from_instance_id(instance_id: str, repo: str) -> tuple[int | None, str | None]:
    """Derive PR only from SWE-bench-style ids matching the repo.

    Prevents broken evidence/report links for custom HF datasets where a final
    numeric suffix is often an issue id, row id, or task id rather than a PR.
    """
    m = re.match(r"^([^_]+)__([^/]+)-(\d+)$", str(instance_id or ""))
    if not m:
        return None, None
    owner, repo_name, num = m.groups()
    expected = f"{owner}/{repo_name}".lower()
    if str(repo or "").lower() != expected:
        return None, None
    pr_number = int(num)
    return pr_number, f"https://github.com/{repo}/pull/{pr_number}"


def _safe_pr_from_row(row, instance_id: str, repo: str) -> tuple[int | None, str | None]:
    explicit_url = _explicit_pr_url_from_row(row)
    if explicit_url:
        m = re.search(r"/pull/(\d+)", explicit_url)
        return (int(m.group(1)) if m else None), explicit_url
    for col in ("pr_number", "pull_number", "pull_request_number"):
        try:
            pr_number = _coerce_optional_int(row.get(col, None))
        except Exception:
            pr_number = None
        if pr_number and repo and "/" in repo:
            return pr_number, f"https://github.com/{repo}/pull/{pr_number}"
    return _safe_pr_from_instance_id(instance_id, repo)


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════

def list_known_benchmarks() -> list[BenchmarkInfo]:
    """List all benchmarks in the registry."""
    return list(REGISTRY.values())


def run_auto(
    name: str,
    sample_size: int = 0,
    model: str = "stack-v2",
    progress_callback=None,
) -> dict:
    """Run the full /auto pipeline.

    1. Discover + download
    2. Load data
    3. Detect schema + audit type
    4. Run audit
    5. Generate report

    Returns a dict with: benchmark_info, schema, audit_type, instance_count, report_paths.
    """
    import pandas as pd

    # 1. Discover + download
    if progress_callback:
        progress_callback("discover", f"Discovering benchmark: {name}")

    data_file, info = discover_and_download(name, progress_callback)

    # 2. Load data
    if progress_callback:
        progress_callback("load", f"Loading {data_file.name}...")

    pre_sampled = False
    if data_file.suffix == ".parquet":
        _emit(progress_callback, "load", "Reading parquet into memory...")
        df = pd.read_parquet(data_file)
    elif data_file.suffix == ".jsonl":
        if sample_size > 0:
            df = _stream_jsonl_stratified_sample(data_file, sample_size, progress_callback=progress_callback)
            pre_sampled = True
        else:
            df = _read_jsonl_dataframe_streaming(data_file, progress_callback=progress_callback)
    elif data_file.suffix == ".json":
        df = pd.read_json(data_file)
    else:
        raise ValueError(f"Unsupported file format: {data_file.suffix}")

    _emit(progress_callback, "load", f"Loaded dataframe: {len(df):,} rows x {len(df.columns)} columns")

    # 3. Detect schema
    if info.column_map:
        schema = info.column_map
        # Verify the mapped columns exist
        for standard, actual in list(schema.items()):
            if actual not in df.columns:
                schema.pop(standard)
        # Fill in any missing mappings with auto-detection
        auto = detect_schema(df)
        for standard, actual in auto.items():
            if standard not in schema:
                schema[standard] = actual
    else:
        schema = detect_schema(df)

    # 4. Detect audit type
    if info.audit_type:
        audit_type = info.audit_type
    else:
        audit_type = detect_audit_type(df, schema)

    if progress_callback:
        progress_callback("schema", f"Schema: {schema}")
        progress_callback("mode", f"Audit mode: {audit_type}")

    if sample_size > 0 and not pre_sampled:
        df = _stratified_sample_df(df, sample_size, schema)
        if progress_callback:
            progress_callback("sample", f"Stratified sample: {len(df)} rows")
    elif sample_size > 0 and pre_sampled and progress_callback:
        progress_callback("sample", f"Streaming stratified sample: {len(df)} rows")

    # 5. Convert to BenchmarkInstances
    instances = instances_from_dataframe(df, schema)

    if progress_callback:
        progress_callback("loaded", f"Loaded {len(instances)} instances")

    return {
        "benchmark_info": info,
        "schema": schema,
        "audit_type": audit_type,
        "instances": instances,
        "instance_count": len(instances),
        "data_file": str(data_file),
    }


__all__ = [
    "BenchmarkInfo",
    "REGISTRY",
    "list_known_benchmarks",
    "discover_and_download",
    "detect_schema",
    "detect_audit_type",
    "instances_from_dataframe",
    "run_auto",
]
