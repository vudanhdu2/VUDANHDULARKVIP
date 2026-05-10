"""Pure-function tests cho `waytoagi.sync.diff`."""

from __future__ import annotations

from typing import Any

import pytest

from waytoagi.models.docs import Block
from waytoagi.sync.diff import (
    BlockDiffOp,
    compute_block_hashes,
    compute_diff,
)


def _mk_text_block(
    block_id: str,
    content: str,
    *,
    block_type: int = 2,
    link_url: str = "",
) -> Block:
    """Build text-block dict đủ data cho hash."""
    text_run: dict[str, Any] = {"content": content}
    if link_url:
        text_run["text_element_style"] = {"link": {"url": link_url}}
    return Block.model_validate({
        "block_id": block_id,
        "block_type": block_type,
        "text": {
            "elements": [{"text_run": text_run}],
        },
    })


@pytest.mark.unit
class TestComputeBlockHashes:
    def test_same_content_same_hash(self) -> None:
        b1 = _mk_text_block("id1", "Hello")
        b2 = _mk_text_block("id2", "Hello")
        h = compute_block_hashes([b1, b2])
        assert h["id1"] == h["id2"]

    def test_different_content_different_hash(self) -> None:
        b1 = _mk_text_block("id1", "Hello")
        b2 = _mk_text_block("id2", "World")
        h = compute_block_hashes([b1, b2])
        assert h["id1"] != h["id2"]

    def test_link_url_affects_hash(self) -> None:
        b1 = _mk_text_block("id1", "click", link_url="https://a.com")
        b2 = _mk_text_block("id2", "click", link_url="https://b.com")
        h = compute_block_hashes([b1, b2])
        assert h["id1"] != h["id2"]

    def test_block_type_affects_hash(self) -> None:
        b1 = _mk_text_block("id1", "Hi", block_type=2)  # text
        b2 = _mk_text_block("id2", "Hi", block_type=3)  # heading1
        h = compute_block_hashes([b1, b2])
        assert h["id1"] != h["id2"]

    def test_skip_block_with_empty_id(self) -> None:
        """Block với block_id="" được skip."""
        b = Block.model_validate({
            "block_id": "",
            "block_type": 2,
            "text": {"elements": []},
        })
        h = compute_block_hashes([b])
        assert h == {}


@pytest.mark.unit
class TestComputeDiff:
    def test_empty_vs_empty_no_op(self) -> None:
        plan = compute_diff([], [])
        assert plan.is_no_op
        assert plan.total_changes == 0

    def test_identical_all_keep(self) -> None:
        a = _mk_text_block("a1", "A")
        b = _mk_text_block("b1", "B")
        # Use different block_ids to simulate src vs dst
        src = [a, b]
        dst = [_mk_text_block("dst1", "A"), _mk_text_block("dst2", "B")]
        plan = compute_diff(src, dst)
        assert plan.n_keep == 2
        assert plan.total_changes == 0
        assert plan.is_no_op

    def test_one_block_changed_one_replace(self) -> None:
        src = [
            _mk_text_block("s1", "A"),
            _mk_text_block("s2", "B-CHANGED"),
            _mk_text_block("s3", "C"),
        ]
        dst = [
            _mk_text_block("d1", "A"),
            _mk_text_block("d2", "B"),
            _mk_text_block("d3", "C"),
        ]
        plan = compute_diff(src, dst)
        assert plan.n_keep == 2
        assert plan.n_replace == 1
        assert plan.diffs[1].op == BlockDiffOp.REPLACE
        assert plan.diffs[1].dst is not None
        assert plan.diffs[1].dst.block_id == "d2"

    def test_src_longer_appends(self) -> None:
        src = [
            _mk_text_block("s1", "A"),
            _mk_text_block("s2", "B"),
            _mk_text_block("s3", "C"),
        ]
        dst = [_mk_text_block("d1", "A")]
        plan = compute_diff(src, dst)
        assert plan.n_keep == 1
        assert plan.n_append == 2

    def test_src_shorter_deletes(self) -> None:
        src = [_mk_text_block("s1", "A")]
        dst = [
            _mk_text_block("d1", "A"),
            _mk_text_block("d2", "extra"),
            _mk_text_block("d3", "extra2"),
        ]
        plan = compute_diff(src, dst)
        assert plan.n_keep == 1
        assert plan.n_delete == 2
        assert plan.diffs[1].op == BlockDiffOp.DELETE
        assert plan.diffs[2].op == BlockDiffOp.DELETE

    def test_total_changes_excludes_keep(self) -> None:
        src = [
            _mk_text_block("s1", "A"),
            _mk_text_block("s2", "B"),
            _mk_text_block("s3", "C-CHANGED"),
        ]
        dst = [
            _mk_text_block("d1", "A"),
            _mk_text_block("d2", "B"),
            _mk_text_block("d3", "C"),
        ]
        plan = compute_diff(src, dst)
        assert plan.total_changes == 1  # only 1 REPLACE

    def test_idempotent_after_apply(self) -> None:
        """Sau apply, re-diff trả no_op."""
        src = [_mk_text_block("s1", "A"), _mk_text_block("s2", "B")]
        dst_after_apply = [
            _mk_text_block("d1", "A"),
            _mk_text_block("d2", "B"),
        ]
        plan = compute_diff(src, dst_after_apply)
        assert plan.is_no_op

    def test_big_doc_save_calls(self) -> None:
        """Big doc 100 blocks edit chỉ 5 → save 95 calls."""
        src = [
            _mk_text_block(f"s{i}", f"content-{i}" + ("-EDITED" if i % 20 == 0 else ""))
            for i in range(100)
        ]
        dst = [_mk_text_block(f"d{i}", f"content-{i}") for i in range(100)]
        plan = compute_diff(src, dst)
        # 5 blocks changed (i=0,20,40,60,80) → 5 REPLACE, 95 KEEP
        assert plan.n_replace == 5
        assert plan.n_keep == 95
        assert plan.total_changes == 5
