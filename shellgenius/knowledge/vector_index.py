"""
Vector index wrapper — ScaNN-backed approximate nearest neighbor search.

Hides ScaNN's builder behind a small surface that mirrors the FAISS call
shape used elsewhere in the codebase:

    vi = build(embeddings)              # build (adaptive strategy)
    vi.save(dir)                        # serialize atomically
    vi = VectorIndex.load(dir)          # deserialize
    distances, indices = vi.search(q, k)  # (1, k) arrays, larger=better

Adaptive strategy:
    - n < BRUTE_FORCE_THRESHOLD   → exact score_brute_force (no training)
    - n >= BRUTE_FORCE_THRESHOLD  → tree + asymmetric hashing + reorder

The threshold is env-overridable via SHELLGENIUS_SCANN_BRUTE_THRESHOLD.

Score semantics: distance_measure="dot_product" on L2-normalized vectors
equals cosine similarity. Under AH quantization scores can drift outside
[-1, 1] by ~1-2% — callers that treat the score as a strict cosine bound
should clamp.
"""

from __future__ import annotations

import math
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    import numpy as np

_scann = None


def _get_scann():
    global _scann
    if _scann is None:
        import scann
        _scann = scann
    return _scann


BRUTE_FORCE_THRESHOLD = int(os.environ.get("SHELLGENIUS_SCANN_BRUTE_THRESHOLD", 50_000))
DEFAULT_REORDER_K = 200
_CONFIG_SENTINEL = "scann_config.pb"


@dataclass
class VectorIndex:
    searcher: object
    strategy: str
    _ntotal: int

    @property
    def ntotal(self) -> int:
        return self._ntotal

    def search(
        self,
        query,
        k: int,
        *,
        leaves_to_search: Optional[int] = None,
    ):
        """Return (distances, indices), each shaped (1, k), matching FAISS."""
        import numpy as np

        if self.strategy == "tree_ah" and k > DEFAULT_REORDER_K:
            raise ValueError(
                f"k={k} exceeds reorder depth {DEFAULT_REORDER_K}; "
                f"raise DEFAULT_REORDER_K or reduce k"
            )

        q = np.ascontiguousarray(query, dtype=np.float32)
        if q.ndim == 1:
            q = q.reshape(1, -1)

        kwargs = {"final_num_neighbors": k}
        if leaves_to_search is not None and self.strategy == "tree_ah":
            kwargs["leaves_to_search"] = leaves_to_search

        neighbors, distances = self.searcher.search_batched(q, **kwargs)
        distances = np.asarray(distances).reshape(q.shape[0], -1)
        neighbors = np.asarray(neighbors).reshape(q.shape[0], -1)
        return distances, neighbors

    def save(self, directory: str | Path) -> None:
        """Atomic serialize: write to {dir}.tmp, swap onto {dir}, then
        rewrite scann_assets.pbtxt so the absolute asset paths — which
        ScaNN bakes in at serialize() time — point at the final location
        instead of the tmp directory."""
        directory = Path(directory)
        tmp = directory.with_name(directory.name + ".tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True, exist_ok=True)
        self.searcher.serialize(str(tmp))
        if directory.exists():
            shutil.rmtree(directory)
        os.replace(tmp, directory)

        assets = directory / "scann_assets.pbtxt"
        if assets.exists():
            text = assets.read_text()
            text = text.replace(str(tmp.resolve()), str(directory.resolve()))
            assets.write_text(text)

    @classmethod
    def load(cls, directory: str | Path) -> "VectorIndex":
        scann = _get_scann()
        directory = Path(directory)
        if not (directory / _CONFIG_SENTINEL).exists():
            raise FileNotFoundError(
                f"No ScaNN index at {directory} (missing {_CONFIG_SENTINEL})"
            )
        searcher = scann.scann_ops_pybind.load_searcher(str(directory))
        strategy = "tree_ah" if (directory / "serialized_partitioner.pb").exists() else "brute"
        return cls(searcher=searcher, strategy=strategy, _ntotal=int(searcher.size()))


def build(embeddings) -> VectorIndex:
    """Adaptive build: brute-force for small corpora, tree+AH for large."""
    import numpy as np

    scann = _get_scann()
    emb = np.ascontiguousarray(embeddings, dtype=np.float32)
    n = emb.shape[0]

    builder = scann.scann_ops_pybind.builder(emb, 10, "dot_product")

    if n < BRUTE_FORCE_THRESHOLD:
        strategy = "brute"
        searcher = builder.score_brute_force().build()
    else:
        strategy = "tree_ah"
        num_leaves = max(1, int(math.sqrt(n)))
        leaves_to_search = max(1, num_leaves // 20)
        searcher = (
            builder
            .tree(
                num_leaves=num_leaves,
                num_leaves_to_search=leaves_to_search,
                training_sample_size=min(n, 250_000),
            )
            .score_ah(2, anisotropic_quantization_threshold=0.2)
            .reorder(DEFAULT_REORDER_K)
            .build()
        )

    return VectorIndex(searcher=searcher, strategy=strategy, _ntotal=n)


def is_scann_index(directory: str | Path) -> bool:
    """True if `directory` contains a complete ScaNN index."""
    return (Path(directory) / _CONFIG_SENTINEL).exists()
