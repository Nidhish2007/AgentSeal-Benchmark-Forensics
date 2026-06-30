"""CodeSeal MinHash/LSH background evidence.

This module integrates the bundled CodeSeal artifact as a deterministic
content-overlap signal. The bundled model is a pickle, so AgentSeal verifies
its SHA-256 before unpickling and uses a restricted unpickler. External models
must be explicitly hash-pinned by callers.
"""

from __future__ import annotations

import hashlib
import io
import pickle
import re
import sqlite3
import struct
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


BUNDLED_MODEL_SHA256 = "2A6B61B4ABC089EBE56D0BEBDAB68A487B4333C42ECA2C7526A77F8976397334"
BUNDLED_MODEL_PATH = Path(__file__).with_name("data") / "codeseal_model.pkl"
BUNDLED_SQLITE_SHA256 = "310EB88247B86BA33C97D80914D58BE784828A781D78C1EB02897553199076FC"
BUNDLED_SQLITE_PATH = Path(__file__).with_name("data") / "codeseal_model.sqlite"


class CodeSealModelTrustError(RuntimeError):
    """Raised when a pickle model fails the explicit trust check."""


class _RestrictedUnpickler(pickle.Unpickler):
    """Unpickler that only permits inert builtins if a GLOBAL is encountered."""

    _ALLOWED = {
        ("builtins", "dict"),
        ("builtins", "list"),
        ("builtins", "set"),
        ("builtins", "frozenset"),
        ("builtins", "tuple"),
        ("builtins", "str"),
        ("builtins", "int"),
        ("builtins", "float"),
    }

    def find_class(self, module: str, name: str):
        if (module, name) in self._ALLOWED:
            return super().find_class(module, name)
        raise CodeSealModelTrustError(f"disallowed pickle global: {module}.{name}")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def sha256_file(path: str | Path) -> str:
    """Public helper used by tests/build tooling for hash-pinned artifacts."""

    return _sha256_file(Path(path))


class MinHash:
    """Deterministic MinHash signature using SHA-256."""

    def __init__(self, num_hashes: int = 128, seed: int = 42):
        self.num_hashes = num_hashes
        self.seed = seed
        self._salts = [
            hashlib.sha256(f"codeseal-{seed}-{i}".encode()).digest()
            for i in range(num_hashes)
        ]

    def signature(self, tokens: set[str]) -> list[int]:
        if not tokens:
            return [0] * self.num_hashes
        sig = [float("inf")] * self.num_hashes
        for token in tokens:
            tb = token.encode("utf-8")
            for i, salt in enumerate(self._salts):
                h = hashlib.sha256(salt + tb).digest()
                x = struct.unpack("<Q", h[:8])[0]
                if x < sig[i]:
                    sig[i] = x
        return [int(s) if s != float("inf") else 0 for s in sig]

    @staticmethod
    def jaccard(sig1: list[int], sig2: list[int]) -> float:
        if len(sig1) != len(sig2) or not sig1:
            return 0.0
        return sum(1 for a, b in zip(sig1, sig2) if a == b) / len(sig1)


@dataclass
class LSHIndex:
    num_hashes: int = 128
    num_bands: int = 32
    rows_per_band: int = 4
    threshold: float = 0.3
    _index: dict = field(default_factory=lambda: defaultdict(list))
    _files: dict = field(default_factory=dict)
    _minhash: Optional[MinHash] = None

    def __post_init__(self) -> None:
        if self.num_bands * self.rows_per_band != self.num_hashes:
            raise ValueError("num_bands * rows_per_band must equal num_hashes")
        self._minhash = MinHash(self.num_hashes)

    def _band_hashes(self, sig: list[int]) -> list[str]:
        bands = []
        for i in range(self.num_bands):
            start = i * self.rows_per_band
            band = tuple(sig[start:start + self.rows_per_band])
            bands.append(hashlib.sha256(str(band).encode()).hexdigest()[:16])
        return bands

    def add(self, file_id: str, tokens: set[str], filename: str = "") -> None:
        sig = self._minhash.signature(tokens)
        self._files[file_id] = (filename, tokens, sig)
        for band_hash in self._band_hashes(sig):
            self._index[band_hash].append(file_id)

    def query(self, tokens: set[str]) -> list[tuple[str, float]]:
        sig = self._minhash.signature(tokens)
        candidates = set()
        for band_hash in self._band_hashes(sig):
            candidates.update(self._index.get(band_hash, []))
        results = []
        for fid in candidates:
            _, file_tokens, file_sig = self._files[fid]
            est_jaccard = MinHash.jaccard(sig, file_sig)
            if est_jaccard >= self.threshold * 0.5:
                exact = len(tokens & file_tokens) / max(len(tokens | file_tokens), 1)
                if exact >= 0.1:
                    results.append((fid, exact))
        results.sort(key=lambda x: -x[1])
        return results

    @property
    def size(self) -> int:
        return len(self._files)


_GENERIC_TOKENS = frozenset({
    "def", "class", "return", "import", "from", "if", "else", "elif",
    "for", "while", "try", "except", "finally", "with", "as", "in",
    "not", "and", "or", "is", "none", "true", "false", "pass", "break",
    "continue", "lambda", "yield", "global", "nonlocal", "raise", "assert",
    "self", "cls", "init", "str", "int", "float", "bool", "list", "dict",
    "set", "tuple", "len", "range", "print", "open", "type", "isinstance",
    "super", "property", "staticmethod", "classmethod", "name", "value",
    "data", "result", "args", "kwargs", "key", "item", "obj", "config",
    "get", "set", "add", "remove", "update", "append", "extend", "items",
    "keys", "values", "main", "test", "base", "new", "old", "null",
    "void", "empty", "start", "end", "begin", "stop",
})


def tokenize_code(code: str) -> set[str]:
    if not code or not isinstance(code, str):
        return set()
    lines = []
    for line in code.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        lines.append(stripped)
    text = " ".join(lines).lower()
    tokens = set(re.findall(r"[a-z_]\w{3,}", text))
    return tokens - _GENERIC_TOKENS


def extract_patch_tokens(patch: str) -> set[str]:
    if not patch or not isinstance(patch, str):
        return set()
    added_lines = []
    saw_diff_marker = False
    for line in patch.splitlines():
        if line.startswith(("diff --git ", "+++", "---", "@@")):
            saw_diff_marker = True
        if line.startswith("+") and not line.startswith("+++"):
            added_lines.append(line[1:])
    if added_lines:
        return tokenize_code("\n".join(added_lines))
    if not saw_diff_marker:
        return tokenize_code(patch)
    return set()


@dataclass
class CodeSealMatch:
    checked: bool = False
    contaminated: bool = False
    similarity: float = 0.0
    matched_files: list[tuple[str, float]] = field(default_factory=list)
    patch_tokens: int = 0
    index_size: int = 0
    status: str = "not_checked"
    error: str = ""


class CodeSeal:
    """Deterministic content-overlap checker backed by MinHash LSH."""

    def __init__(self, index: Optional[LSHIndex] = None):
        self.index = index or LSHIndex()

    @property
    def size(self) -> int:
        return self.index.size

    def check_patch(self, patch: str) -> CodeSealMatch:
        result = CodeSealMatch(index_size=self.index.size)
        if self.index.size <= 0:
            result.error = "model not trained"
            return result
        tokens = extract_patch_tokens(patch)
        result.patch_tokens = len(tokens)
        if len(tokens) < 3:
            result.error = "too few distinctive tokens"
            return result
        matches = self.index.query(tokens)
        result.checked = True
        if matches:
            result.contaminated = True
            result.similarity = matches[0][1]
            result.matched_files = matches[:10]
        return result


class SQLiteCodeSeal:
    """SQLite-backed CodeSeal index.

    This avoids runtime pickle loading. Only candidate rows selected by LSH band
    collisions are read from SQLite, so startup stays lightweight.
    """

    def __init__(
        self,
        path: Path,
        *,
        num_hashes: int,
        num_bands: int,
        rows_per_band: int,
        threshold: float,
        size: int,
    ):
        self.path = Path(path)
        self.num_hashes = num_hashes
        self.num_bands = num_bands
        self.rows_per_band = rows_per_band
        self.threshold = threshold
        self._size = size
        self._minhash = MinHash(num_hashes)
        # A sqlite3 connection created in one thread cannot safely be used from
        # another, which could crash or serialize the whole run. Keep one read-only
        # connection per worker thread instead.
        self._local = threading.local()

    def _connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                f"file:{self.path.as_posix()}?mode=ro",
                uri=True,
                check_same_thread=False,
            )
            try:
                conn.execute("PRAGMA query_only=ON")
                conn.execute("PRAGMA temp_store=MEMORY")
            except Exception:
                pass
            self._local.conn = conn
        return conn

    @property
    def size(self) -> int:
        return self._size

    def _band_hashes(self, sig: list[int]) -> list[str]:
        bands = []
        for i in range(self.num_bands):
            start = i * self.rows_per_band
            band = tuple(sig[start:start + self.rows_per_band])
            bands.append(hashlib.sha256(str(band).encode()).hexdigest()[:16])
        return bands

    def _load_candidates(self, band_hashes: list[str]) -> list[tuple[str, str, set[str], list[int]]]:
        placeholders = ",".join("?" for _ in band_hashes)
        conn = self._connection()
        rows = conn.execute(
            f"SELECT DISTINCT file_id FROM bands WHERE band_hash IN ({placeholders})",
            band_hashes,
        ).fetchall()
        file_ids = [row[0] for row in rows]
        if not file_ids:
            return []
        placeholders = ",".join("?" for _ in file_ids)
        file_rows = conn.execute(
            f"SELECT file_id, filename, tokens, signature FROM files WHERE file_id IN ({placeholders})",
            file_ids,
        ).fetchall()
        candidates = []
        for file_id, filename, tokens_text, sig_blob in file_rows:
            tokens = set(tokens_text.split("\n")) if tokens_text else set()
            sig = list(struct.unpack("<" + "Q" * (len(sig_blob) // 8), sig_blob))
            candidates.append((file_id, filename, tokens, sig))
        return candidates

    def check_patch(self, patch: str) -> CodeSealMatch:
        result = CodeSealMatch(index_size=self.size)
        tokens = extract_patch_tokens(patch)
        result.patch_tokens = len(tokens)
        if len(tokens) < 3:
            result.error = "too few distinctive tokens"
            return result
        sig = self._minhash.signature(tokens)
        results = []
        for file_id, _filename, file_tokens, file_sig in self._load_candidates(self._band_hashes(sig)):
            est_jaccard = MinHash.jaccard(sig, file_sig)
            if est_jaccard >= self.threshold * 0.5:
                exact = len(tokens & file_tokens) / max(len(tokens | file_tokens), 1)
                if exact >= 0.1:
                    results.append((file_id, exact))
        results.sort(key=lambda x: -x[1])
        result.checked = True
        if results:
            result.contaminated = True
            result.similarity = results[0][1]
            result.matched_files = results[:10]
        return result


def _validate_model_payload(data: object) -> dict:
    if not isinstance(data, dict):
        raise CodeSealModelTrustError("model payload is not a dictionary")
    required = {"num_hashes", "num_bands", "rows_per_band", "threshold", "_index", "_files"}
    missing = sorted(required - set(data))
    if missing:
        raise CodeSealModelTrustError(f"model payload missing keys: {missing}")
    if not isinstance(data["_index"], dict) or not isinstance(data["_files"], dict):
        raise CodeSealModelTrustError("model index/files have invalid types")
    return data


def load_trusted_model(path: str | Path, expected_sha256: str) -> CodeSeal:
    """Load a CodeSeal pickle only after matching an expected SHA-256."""

    model_path = Path(path)
    if not model_path.exists():
        raise FileNotFoundError(str(model_path))
    actual = _sha256_file(model_path)
    if actual != expected_sha256.upper():
        raise CodeSealModelTrustError(
            f"CodeSeal model hash mismatch: expected {expected_sha256.upper()}, got {actual}"
        )
    raw = model_path.read_bytes()
    data = _validate_model_payload(_RestrictedUnpickler(io.BytesIO(raw)).load())
    idx = LSHIndex(
        num_hashes=int(data["num_hashes"]),
        num_bands=int(data["num_bands"]),
        rows_per_band=int(data["rows_per_band"]),
        threshold=float(data["threshold"]),
    )
    idx._index = defaultdict(list, data["_index"])
    idx._files = data["_files"]
    return CodeSeal(idx)


def save_sqlite_model(model: CodeSeal, path: str | Path) -> Path:
    """Persist a CodeSeal model to an inert SQLite index."""

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    conn = sqlite3.connect(out)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("CREATE TABLE bands (band_hash TEXT NOT NULL, file_id TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE files ("
            "file_id TEXT PRIMARY KEY, "
            "filename TEXT NOT NULL, "
            "tokens TEXT NOT NULL, "
            "signature BLOB NOT NULL)"
        )
        meta = {
            "num_hashes": str(model.index.num_hashes),
            "num_bands": str(model.index.num_bands),
            "rows_per_band": str(model.index.rows_per_band),
            "threshold": str(model.index.threshold),
            "size": str(model.index.size),
        }
        conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", meta.items())
        band_rows = []
        for band_hash, file_ids in model.index._index.items():
            for file_id in file_ids:
                band_rows.append((str(band_hash), str(file_id)))
        conn.executemany("INSERT INTO bands (band_hash, file_id) VALUES (?, ?)", band_rows)
        file_rows = []
        for file_id, (filename, tokens, sig) in model.index._files.items():
            sig_blob = struct.pack("<" + "Q" * len(sig), *[int(x) for x in sig])
            file_rows.append((str(file_id), str(filename or file_id), "\n".join(sorted(tokens)), sig_blob))
        conn.executemany(
            "INSERT INTO files (file_id, filename, tokens, signature) VALUES (?, ?, ?, ?)",
            file_rows,
        )
        conn.execute("CREATE INDEX idx_bands_hash ON bands (band_hash)")
        conn.commit()
    finally:
        conn.close()
    return out


def load_sqlite_model(path: str | Path, expected_sha256: str | None = None) -> SQLiteCodeSeal:
    """Load a SQLite CodeSeal model, optionally hash-pinned."""

    model_path = Path(path)
    if expected_sha256:
        actual = _sha256_file(model_path)
        if actual != expected_sha256.upper():
            raise CodeSealModelTrustError(
                f"CodeSeal SQLite model hash mismatch: expected {expected_sha256.upper()}, got {actual}"
            )
    conn = sqlite3.connect(f"file:{model_path.as_posix()}?mode=ro", uri=True)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    finally:
        conn.close()
    return SQLiteCodeSeal(
        model_path,
        num_hashes=int(meta["num_hashes"]),
        num_bands=int(meta["num_bands"]),
        rows_per_band=int(meta["rows_per_band"]),
        threshold=float(meta["threshold"]),
        size=int(meta["size"]),
    )


_MODEL_CACHE: Optional[CodeSeal] = None
_MODEL_ERROR: str = ""
_CHECK_CACHE: dict[str, CodeSealMatch] = {}
_CHECK_CACHE_LOCK = threading.Lock()
_CHECK_CACHE_MAX = 4096


def get_bundled_model_status() -> dict:
    """Return lightweight status for report/CLI diagnostics."""

    exists = BUNDLED_MODEL_PATH.exists()
    sqlite_exists = BUNDLED_SQLITE_PATH.exists()
    return {
        "path": str(BUNDLED_MODEL_PATH),
        "sqlite_path": str(BUNDLED_SQLITE_PATH),
        "exists": exists,
        "sqlite_exists": sqlite_exists,
        "sha256": BUNDLED_MODEL_SHA256 if exists else "",
        "sqlite_sha256": BUNDLED_SQLITE_SHA256 if sqlite_exists else "",
        "loaded": _MODEL_CACHE is not None,
        "error": _MODEL_ERROR,
    }


def _load_bundled_model() -> CodeSeal:
    global _MODEL_CACHE, _MODEL_ERROR
    if _MODEL_CACHE is not None:
        return _MODEL_CACHE
    try:
        if BUNDLED_SQLITE_PATH.exists():
            _MODEL_CACHE = load_sqlite_model(
                BUNDLED_SQLITE_PATH,
                BUNDLED_SQLITE_SHA256 or None,
            )
        else:
            _MODEL_CACHE = load_trusted_model(BUNDLED_MODEL_PATH, BUNDLED_MODEL_SHA256)
        _MODEL_ERROR = ""
        return _MODEL_CACHE
    except Exception as exc:
        _MODEL_ERROR = str(exc)
        raise


def check_patch_against_bundled_model(patch: str, *, enabled: bool = True) -> CodeSealMatch:
    """Check a patch against the bundled CodeSeal model if enabled.

    Results are cached by patch content hash. Large audits often retry the same
    patch through multiple paths, and the SQLite LSH query is deterministic.
    """

    if not enabled:
        return CodeSealMatch(status="disabled")
    if not patch:
        return CodeSealMatch(status="not_checked", error="patch is empty")
    try:
        cache_key = hashlib.sha256(str(patch).encode("utf-8", errors="replace")).hexdigest()[:24]
    except Exception:
        cache_key = ""
    if cache_key:
        with _CHECK_CACHE_LOCK:
            cached = _CHECK_CACHE.get(cache_key)
        if cached is not None:
            return cached
    tokens = extract_patch_tokens(patch)
    if len(tokens) < 3:
        result = CodeSealMatch(status="not_checked", error="too few distinctive tokens")
        result.patch_tokens = len(tokens)
        if cache_key:
            with _CHECK_CACHE_LOCK:
                if len(_CHECK_CACHE) < _CHECK_CACHE_MAX:
                    _CHECK_CACHE[cache_key] = result
        return result
    try:
        model = _load_bundled_model()
    except Exception as exc:
        return CodeSealMatch(status="unavailable", error=str(exc))
    result = model.check_patch(patch)
    result.status = "hash_verified_bundled_model"
    if cache_key:
        with _CHECK_CACHE_LOCK:
            if len(_CHECK_CACHE) >= _CHECK_CACHE_MAX:
                try:
                    _CHECK_CACHE.pop(next(iter(_CHECK_CACHE)))
                except Exception:
                    _CHECK_CACHE.clear()
            _CHECK_CACHE[cache_key] = result
    return result


__all__ = [
    "BUNDLED_MODEL_PATH",
    "BUNDLED_MODEL_SHA256",
    "BUNDLED_SQLITE_PATH",
    "BUNDLED_SQLITE_SHA256",
    "CodeSeal",
    "CodeSealMatch",
    "CodeSealModelTrustError",
    "extract_patch_tokens",
    "get_bundled_model_status",
    "load_sqlite_model",
    "load_trusted_model",
    "save_sqlite_model",
    "check_patch_against_bundled_model",
    "sha256_file",
    "tokenize_code",
]
