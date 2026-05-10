"""Pure-function block-level diff cho SmartSyncStage.

Algorithm:
  1. Hash mỗi block: MD5(block_type + content_string + style_marker)
  2. So src vs dst hash arrays:
     - Cùng len + hash[i] khác → REPLACE block i
     - src dài hơn → APPEND blocks dst[len:]
     - src ngắn hơn → DELETE blocks dst[len(src):]
     - Khác cấu trúc lớn → fallback LCS để tìm minimal edit
  3. Trả về `SyncPlan` với list `BlockDiffOp` — caller execute.

Pure: không I/O, deterministic, dễ test.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from waytoagi.models.docs import Block


# ============================================================================
# Hash function
# ============================================================================


def _hash_text_runs(elements: list[dict[str, object]]) -> str:
    """Hash content + link.url của mọi text_run (mention_doc cũng dùng url)."""
    parts: list[str] = []
    for el in elements:
        if not isinstance(el, dict):
            continue
        tr = el.get("text_run")
        if isinstance(tr, dict):
            parts.append(str(tr.get("content", "")))
            style = tr.get("text_element_style", {})
            if isinstance(style, dict):
                link = style.get("link", {})
                if isinstance(link, dict):
                    parts.append(str(link.get("url", "")))
        md = el.get("mention_doc")
        if isinstance(md, dict):
            parts.append(str(md.get("token", "")))
            parts.append(str(md.get("url", "")))
            parts.append(str(md.get("title", "")))
    return "\x00".join(parts)


def _hash_block(block: Block) -> str:
    """Hash 1 block. Block hash hợp lại từ:
      - block_type
      - elements text content (text_run + mention_doc.url)
      - heading/list/code marker
    """
    h = hashlib.md5(usedforsecurity=False)
    h.update(str(block.block_type).encode("utf-8"))
    h.update(b"\x01")

    # Each block_type có 1 trường text_block (text/heading1/.../code/quote)
    # với .elements list. Hash content luôn.
    bd = block.model_dump(mode="python", exclude_none=True)
    # Loại metadata không quan trọng cho diff
    bd.pop("block_id", None)
    bd.pop("parent_id", None)
    bd.pop("children", None)

    # Tìm trường có .elements và hash content
    for key, value in bd.items():
        if isinstance(value, dict) and "elements" in value:
            elems = value.get("elements")
            if isinstance(elems, list):
                h.update(key.encode("utf-8"))
                h.update(b"\x02")
                h.update(_hash_text_runs(elems).encode("utf-8"))
                h.update(b"\x03")

    return h.hexdigest()


def compute_block_hashes(blocks: Sequence[Block]) -> dict[str, str]:
    """Map block_id → hash. Skip block không có block_id."""
    out: dict[str, str] = {}
    for b in blocks:
        if b.block_id:
            out[b.block_id] = _hash_block(b)
    return out


# ============================================================================
# Models
# ============================================================================


class BlockDiffOp(StrEnum):
    """Loại operation cho 1 block trong sync plan."""

    REPLACE = "replace"
    """Block đã có ở dst, content khác → patch."""

    APPEND = "append"
    """Block mới ở src, dst chưa có → tạo + insert cuối parent."""

    DELETE = "delete"
    """Block còn ở dst nhưng src đã xoá → remove dst block."""

    KEEP = "keep"
    """Hash trùng → không touch (giảm chi phí)."""


@dataclass(frozen=True, slots=True)
class BlockSnapshot:
    """Snapshot 1 block để tham chiếu trong plan."""

    block_id: str
    """ID nguyên bản (src.block_id hoặc dst.block_id)."""

    hash: str
    """Hash content."""

    index: int
    """Vị trí trong sequence (0-based)."""


@dataclass(frozen=True, slots=True)
class BlockDiff:
    """1 operation trong sync plan."""

    op: BlockDiffOp
    src: BlockSnapshot | None = None
    """Source block (nếu op cần content): REPLACE, APPEND."""
    dst: BlockSnapshot | None = None
    """DST block (nếu op tham chiếu existing): REPLACE, DELETE, KEEP."""


@dataclass(slots=True)
class SyncPlan:
    """Plan tổng hợp diff giữa src vs dst block sequences."""

    diffs: list[BlockDiff] = field(default_factory=list)

    @property
    def n_replace(self) -> int:
        return sum(1 for d in self.diffs if d.op == BlockDiffOp.REPLACE)

    @property
    def n_append(self) -> int:
        return sum(1 for d in self.diffs if d.op == BlockDiffOp.APPEND)

    @property
    def n_delete(self) -> int:
        return sum(1 for d in self.diffs if d.op == BlockDiffOp.DELETE)

    @property
    def n_keep(self) -> int:
        return sum(1 for d in self.diffs if d.op == BlockDiffOp.KEEP)

    @property
    def total_changes(self) -> int:
        return self.n_replace + self.n_append + self.n_delete

    @property
    def is_no_op(self) -> bool:
        return self.total_changes == 0


# ============================================================================
# Diff algorithm
# ============================================================================


def compute_diff(
    src_blocks: Sequence[Block],
    dst_blocks: Sequence[Block],
) -> SyncPlan:
    """Tính SyncPlan từ src vs dst blocks.

    Algorithm: aligned by index. Đơn giản nhưng đủ cho 95% case (clone
    1-1 chỉ thay đổi content, không reorder). Reorder thuộc TreeOrder
    (trong wiki tree), không phải block content.

    Pseudocode:
        for i in 0..max(len_src, len_dst):
            if i < len_src and i < len_dst:
                if hash(src[i]) == hash(dst[i]):  →  KEEP
                else:                              →  REPLACE
            elif i < len_src:
                APPEND src[i]                      (src dài hơn)
            else:
                DELETE dst[i]                      (dst dài hơn)

    Trade-off: KHÔNG dùng LCS — đơn giản, deterministic, đủ cho use case
    "edit content vài block trong doc đã clone xong". Nếu user reorder
    blocks thực sự, fallback path = REPLACE toàn bộ → vẫn đúng nhưng
    chậm hơn.
    """
    src_hashes = [_hash_block(b) for b in src_blocks]
    dst_hashes = [_hash_block(b) for b in dst_blocks]

    plan = SyncPlan()
    n_src = len(src_blocks)
    n_dst = len(dst_blocks)
    n_max = max(n_src, n_dst)

    for i in range(n_max):
        if i < n_src and i < n_dst:
            src_snap = BlockSnapshot(
                block_id=src_blocks[i].block_id,
                hash=src_hashes[i], index=i,
            )
            dst_snap = BlockSnapshot(
                block_id=dst_blocks[i].block_id,
                hash=dst_hashes[i], index=i,
            )
            if src_hashes[i] == dst_hashes[i]:
                plan.diffs.append(BlockDiff(
                    op=BlockDiffOp.KEEP, src=src_snap, dst=dst_snap,
                ))
            else:
                plan.diffs.append(BlockDiff(
                    op=BlockDiffOp.REPLACE, src=src_snap, dst=dst_snap,
                ))
        elif i < n_src:
            plan.diffs.append(BlockDiff(
                op=BlockDiffOp.APPEND,
                src=BlockSnapshot(
                    block_id=src_blocks[i].block_id,
                    hash=src_hashes[i], index=i,
                ),
            ))
        else:
            plan.diffs.append(BlockDiff(
                op=BlockDiffOp.DELETE,
                dst=BlockSnapshot(
                    block_id=dst_blocks[i].block_id,
                    hash=dst_hashes[i], index=i,
                ),
            ))

    return plan
