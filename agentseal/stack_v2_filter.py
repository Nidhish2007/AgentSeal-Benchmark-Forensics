"""Stack v2 Bloom filter support.

The release wheel may include a packaged Stack v2 repository-membership Bloom
filter. The filter is probabilistic: a positive result is a corpus-membership
signal, not proof; a negative result means the active filter did not match.

If no packaged/user filter is present, corpus membership checks stay disabled
rather than falling back to an unverified curated list. Users can provide a
replacement filter at ``~/.agentseal/stack_v2.bloom``.
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import Optional


# ═══════════════════════════════════════════════════════════════════════════
# Bloom filter implementation — pure Python, no external deps
# ═══════════════════════════════════════════════════════════════════════════

class BloomFilter:
    """A space-efficient probabilistic membership test.

    Uses k hash functions (derived from SHA-256) over a bit array of size m.
    For n elements at false positive rate p:
      m = -n * ln(p) / (ln(2)^2)  bits
      k = m/n * ln(2)             hash functions

    The packaged full filter exposes its actual parameters via get_filter_stats().
    For a curated fallback of ~500 repos at 0.1% FP rate: m ≈ 5.8K bits (725 bytes), k = 8.
    """

    def __init__(self, num_bits: int, num_hashes: int):
        self.num_bits = num_bits
        self.num_hashes = num_hashes
        self.num_elements = 0
        # Bit array — stored as bytearray (8 bits per byte)
        self._byte_array = bytearray((num_bits + 7) // 8)

    def _get_hash_positions(self, item: str) -> list[int]:
        """Get k bit positions for an item using double hashing (Kirsch-Mitzenmacher)."""
        h = hashlib.sha256(item.encode('utf-8')).digest()
        h1 = struct.unpack('<Q', h[:8])[0]  # first 64 bits
        h2 = struct.unpack('<Q', h[8:16])[0]  # next 64 bits
        return [(h1 + i * h2) % self.num_bits for i in range(self.num_hashes)]

    def add(self, item: str) -> None:
        """Add an item to the filter."""
        for pos in self._get_hash_positions(item):
            byte_idx = pos >> 3
            bit_idx = pos & 7
            self._byte_array[byte_idx] |= (1 << bit_idx)
        self.num_elements += 1

    def contains(self, item: str) -> bool:
        """Test if an item is possibly in the set.

        Returns True if the item MIGHT be in the set (false positive possible).
        Returns False if the item is DEFINITELY not in the set (no false negatives).
        """
        for pos in self._get_hash_positions(item):
            byte_idx = pos >> 3
            bit_idx = pos & 7
            if not (self._byte_array[byte_idx] & (1 << bit_idx)):
                return False
        return True

    def save(self, path: Path) -> None:
        """Save the filter to a binary file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'wb') as f:
            # Header: magic (4B) + version (1B) + num_bits (8B) + num_hashes (1B) + num_elements (8B)
            f.write(b'STV2')  # magic
            f.write(struct.pack('<B', 1))  # version
            f.write(struct.pack('<Q', self.num_bits))
            f.write(struct.pack('<B', self.num_hashes))
            f.write(struct.pack('<Q', self.num_elements))
            f.write(bytes(self._byte_array))

    @classmethod
    def load(cls, path: Path) -> Optional['BloomFilter']:
        """Load a filter from a binary file. Returns None if file is invalid.

        a crafted `.bloom` file cannot request gigabytes of memory (DoS) or
        tens of thousands of hash iterations. num_bits is capped at 1e9
        (~125MB), num_hashes at 20, and num_elements must be <= num_bits.
        """
        try:
            with open(path, 'rb') as f:
                magic = f.read(4)
                if magic != b'STV2':
                    return None
                version = struct.unpack('<B', f.read(1))[0]
                if version != 1:
                    return None
                num_bits = struct.unpack('<Q', f.read(8))[0]
                num_hashes = struct.unpack('<B', f.read(1))[0]
                num_elements = struct.unpack('<Q', f.read(8))[0]
                if num_bits == 0 or num_bits > 10**9:
                    return None
                if num_hashes == 0 or num_hashes > 20:
                    return None
                if num_elements > num_bits:
                    return None
                byte_len = (num_bits + 7) // 8
                byte_array = bytearray(f.read(byte_len))
                if len(byte_array) != byte_len:
                    return None
            bf = cls(num_bits, num_hashes)
            bf._byte_array = byte_array
            bf.num_elements = num_elements
            return bf
        except Exception:
            return None

    @classmethod
    def create_optimal(cls, expected_items: int, false_positive_rate: float = 0.001) -> 'BloomFilter':
        """Create a Bloom filter optimized for a given number of items and FP rate."""
        import math
        expected_items = max(1, int(expected_items or 1))
        false_positive_rate = min(0.5, max(1e-9, float(false_positive_rate or 0.001)))
        m = max(64, int(-expected_items * math.log(false_positive_rate) / (math.log(2) ** 2)))
        k = max(1, int(m / expected_items * math.log(2)))
        return cls(m, k)


# ═══════════════════════════════════════════════════════════════════════════
# Optional fallback
# ═══════════════════════════════════════════════════════════════════════════

_CURATED_REPOS: list[str] = []


# ═══════════════════════════════════════════════════════════════════════════
# Stack v2 filter — singleton with lazy loading
# ═══════════════════════════════════════════════════════════════════════════

_filter: Optional[BloomFilter] = None
_filter_loaded = False
_USES_FULL_FILTER = False

# Paths to check for a downloaded full filter
_FILTER_PATHS = [
    Path.home() / ".agentseal" / "stack_v2.bloom",
    Path(".agentseal") / "stack_v2.bloom",
    Path(__file__).with_name("data") / "stack_v2.bloom",
]


def _load_filter() -> BloomFilter:
    """Load the Stack v2 Bloom filter (lazy, cached).

    Priority:
    1. Full packaged/user filter from ~/.agentseal/stack_v2.bloom, ./.agentseal/stack_v2.bloom,
       or the wheel's bundled data/stack_v2.bloom
    2. Empty disabled filter when no full filter is available.
    """
    global _filter, _filter_loaded, _USES_FULL_FILTER

    if _filter_loaded:
        return _filter or _load_curated()

    _filter_loaded = True

    # Try to load the full filter
    for path in _FILTER_PATHS:
        if path.exists():
            bf = BloomFilter.load(path)
            if bf and bf.num_elements > 1000:
                _filter = bf
                _USES_FULL_FILTER = True
                return bf

    # No full filter found: disable corpus-membership positives.
    _filter = _load_curated()
    return _filter


def _load_curated() -> BloomFilter:
    """Build the disabled fallback filter used when no artifact is available."""
    bf = BloomFilter.create_optimal(len(_CURATED_REPOS) + 100, 0.001)
    for repo in _CURATED_REPOS:
        bf.add(repo.strip().lower())
    return bf


def repo_in_stack_v2(repo: str) -> bool:
    """Check if a repo is in The Stack v2 training corpus.

    Returns True if the repo is POSSIBLY in The Stack v2 (false positive possible).
    Returns False if the repo is DEFINITELY not in The Stack v2.

    Uses the full packaged/user filter when available. If no filter artifact is
    available, the disabled fallback returns False for all repositories.
    """
    if not repo or not isinstance(repo, str):
        return False
    bf = _load_filter()
    return bf.contains(repo.strip().lower())


def get_filter_stats() -> dict:
    """Get statistics about the loaded filter, including estimated FP rate."""
    bf = _load_filter()
    try:
        import math
        estimated_fp_rate = (1 - math.exp(-bf.num_hashes * bf.num_elements / bf.num_bits)) ** bf.num_hashes
    except Exception:
        estimated_fp_rate = None
    return {
        "num_elements": bf.num_elements,
        "num_bits": bf.num_bits,
        "num_hashes": bf.num_hashes,
        "size_bytes": len(bf._byte_array),
        "estimated_false_positive_rate": estimated_fp_rate,
        "uses_full_filter": _USES_FULL_FILTER,
        "filter_type": (
            f"full/packaged Stack v2 Bloom ({bf.num_elements:,} repos)"
            if _USES_FULL_FILTER else
            "disabled (no Stack v2 Bloom artifact loaded)"
        ),
    }


def build_full_filter(repo_list_path: str, output_path: str) -> int:
    """Build a full Stack v2 filter from a repo list file.

    The repo list file should contain one repo per line (owner/repo format).
    This is used to generate the downloadable filter from HuggingFace data.

    Returns the number of repos added.
    """
    # Builder default; packaged filters may contain more entries.
    bf = BloomFilter.create_optimal(7_000_000, 0.001)
    count = 0
    with open(repo_list_path) as f:
        for line in f:
            repo = line.strip().lower()
            if repo and "/" in repo:
                bf.add(repo)
                count += 1
    bf.save(Path(output_path))
    return count


__all__ = [
    "BloomFilter",
    "repo_in_stack_v2",
    "get_filter_stats",
    "build_full_filter",
]
