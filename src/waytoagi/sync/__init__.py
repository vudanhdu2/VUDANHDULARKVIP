"""Sync module — block-level diff + smart patch.

Khi VI doc edit → DST doc cần update. Naive approach: replace toàn bộ
dst blocks. V2: chỉ patch blocks thực sự đổi (hash compare) → giảm
99% PATCH calls cho doc edit nhỏ.
"""

from waytoagi.sync.diff import (
    BlockDiff,
    BlockDiffOp,
    BlockSnapshot,
    SyncPlan,
    compute_block_hashes,
    compute_diff,
)

__all__ = [
    "BlockDiff",
    "BlockDiffOp",
    "BlockSnapshot",
    "SyncPlan",
    "compute_block_hashes",
    "compute_diff",
]
