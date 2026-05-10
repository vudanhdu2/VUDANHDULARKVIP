"""Tests cho `TreeOrderStage` — orchestration với mock WikiClient + LarkBase.

Coverage:
  - run_with_no_op: index empty / parent đã đúng → 0 moves
  - run_audit_only: phát hiện mismatch → ghi MISMATCH, không gọi move
  - run_full: phát hiện mismatch → gọi move + ghi FIXED
  - per_child_failure_isolation: 1 move fail không halt parent
  - parent_list_children_failure: ghi ERROR, continue parent kế tiếp
  - skip_parent_not_mirrored: src_parent chưa có dst_token → SKIPPED
  - threshold_skip: > max_children → SKIPPED
  - base_real_time_updates: batch_update gọi đúng record_id + status
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from waytoagi.lark.auth import LarkAPIError
from waytoagi.models.tree import SourceOrderIndex, TreeOrderStatus
from waytoagi.stages.reorder import TreeOrderStage


@pytest.fixture
def mock_wiki() -> AsyncMock:
    """Mock LarkWiki — list_children_tokens + move_node."""
    wiki = AsyncMock()
    wiki.list_children_tokens = AsyncMock()
    wiki.move_node = AsyncMock(return_value={"code": 0})
    return wiki


@pytest.fixture
def mock_base() -> AsyncMock:
    """Mock LarkBase — batch_update."""
    base = AsyncMock()
    base.batch_update = AsyncMock(return_value={"code": 0})
    return base


def _make_stage(
    wiki: AsyncMock,
    base: AsyncMock,
    *,
    audit_only: bool = False,
    max_children: int = 50,
) -> TreeOrderStage:
    return TreeOrderStage(
        wiki=wiki,
        base=base,
        app_token="app-tk",
        table_id="tbl-id",
        space_id="space-1",
        max_children=max_children,
        move_pacing_seconds=0.0,  # tests: no sleep
        audit_only=audit_only,
    )


@pytest.mark.unit
class TestTreeOrderStageRun:
    @pytest.mark.asyncio
    async def test_empty_index_no_op(
        self, mock_wiki: AsyncMock, mock_base: AsyncMock,
    ) -> None:
        stage = _make_stage(mock_wiki, mock_base)
        idx = SourceOrderIndex()
        summary = await stage.run(index=idx, src_to_dst={})
        assert summary.parents_total == 0
        assert summary.total_moves == 0
        mock_wiki.move_node.assert_not_called()

    @pytest.mark.asyncio
    async def test_already_correct_marks_ok(
        self, mock_wiki: AsyncMock, mock_base: AsyncMock,
    ) -> None:
        idx = SourceOrderIndex(order={"P_SRC": ["s1", "s2"]})
        src_to_dst = {"P_SRC": "P_DST", "s1": "d1", "s2": "d2"}
        mock_wiki.list_children_tokens.return_value = ["d1", "d2"]

        stage = _make_stage(mock_wiki, mock_base)
        summary = await stage.run(index=idx, src_to_dst=src_to_dst)
        assert summary.parents_total == 1
        assert summary.parents_ok == 1
        assert summary.parents_fixed == 0
        mock_wiki.move_node.assert_not_called()

    @pytest.mark.asyncio
    async def test_audit_only_marks_mismatch(
        self, mock_wiki: AsyncMock, mock_base: AsyncMock,
    ) -> None:
        idx = SourceOrderIndex(order={"P_SRC": ["s1", "s2"]})
        src_to_dst = {"P_SRC": "P_DST", "s1": "d1", "s2": "d2"}
        mock_wiki.list_children_tokens.return_value = ["d2", "d1"]  # reversed

        stage = _make_stage(mock_wiki, mock_base, audit_only=True)
        summary = await stage.run(index=idx, src_to_dst=src_to_dst)
        assert summary.parents_mismatch == 1
        assert summary.parents_fixed == 0
        mock_wiki.move_node.assert_not_called()

    @pytest.mark.asyncio
    async def test_apply_moves_marks_fixed(
        self, mock_wiki: AsyncMock, mock_base: AsyncMock,
    ) -> None:
        idx = SourceOrderIndex(order={"P_SRC": ["s1", "s2", "s3"]})
        src_to_dst = {
            "P_SRC": "P_DST", "s1": "d1", "s2": "d2", "s3": "d3",
        }
        mock_wiki.list_children_tokens.return_value = ["d3", "d2", "d1"]

        stage = _make_stage(mock_wiki, mock_base, audit_only=False)
        summary = await stage.run(index=idx, src_to_dst=src_to_dst)
        assert summary.parents_fixed == 1
        assert summary.total_moves == 3
        # Move calls: theo desired order [d1, d2, d3]
        calls = mock_wiki.move_node.call_args_list
        assert len(calls) == 3
        called_tokens = [c.kwargs["node_token"] for c in calls]
        assert called_tokens == ["d1", "d2", "d3"]

    @pytest.mark.asyncio
    async def test_per_child_failure_isolation(
        self, mock_wiki: AsyncMock, mock_base: AsyncMock,
    ) -> None:
        """1 move fail không halt parent — vẫn process các child khác."""
        idx = SourceOrderIndex(order={"P_SRC": ["s1", "s2", "s3"]})
        src_to_dst = {
            "P_SRC": "P_DST", "s1": "d1", "s2": "d2", "s3": "d3",
        }
        mock_wiki.list_children_tokens.return_value = ["d3", "d2", "d1"]

        # 2nd call (d2) fails permanently
        async def move_side_effect(*_args: Any, **kwargs: Any) -> dict[str, Any]:
            if kwargs.get("node_token") == "d2":
                raise LarkAPIError(131005, "not found", "/wiki/.../move")
            return {"code": 0}

        mock_wiki.move_node.side_effect = move_side_effect

        stage = _make_stage(mock_wiki, mock_base)
        summary = await stage.run(index=idx, src_to_dst=src_to_dst)
        # All 3 attempts made, 2 succeeded, 1 error
        assert mock_wiki.move_node.call_count == 3
        assert summary.parents_fixed == 1  # partial fix vẫn coi là Fixed
        assert summary.total_moves == 2  # only succeeded

    @pytest.mark.asyncio
    async def test_skip_parent_not_mirrored(
        self, mock_wiki: AsyncMock, mock_base: AsyncMock,
    ) -> None:
        idx = SourceOrderIndex(order={"P_NOT_MIRRORED": ["s1"]})
        src_to_dst: dict[str, str] = {"s1": "d1"}  # parent missing
        stage = _make_stage(mock_wiki, mock_base)
        summary = await stage.run(index=idx, src_to_dst=src_to_dst)
        assert summary.parents_skipped == 1
        mock_wiki.list_children_tokens.assert_not_called()
        mock_wiki.move_node.assert_not_called()

    @pytest.mark.asyncio
    async def test_list_children_failure_marks_error(
        self, mock_wiki: AsyncMock, mock_base: AsyncMock,
    ) -> None:
        idx = SourceOrderIndex(order={
            "P_OK_SRC": ["s1"],
            "P_FAIL_SRC": ["s2"],
        })
        src_to_dst = {
            "P_OK_SRC": "P_OK_DST", "s1": "d1",
            "P_FAIL_SRC": "P_FAIL_DST", "s2": "d2",
        }

        # P_FAIL_DST → list raises
        async def list_side_effect(_space: str, parent: str) -> list[str]:
            if parent == "P_FAIL_DST":
                raise LarkAPIError(99991400, "rate limit", "/list")
            return ["d1"]

        mock_wiki.list_children_tokens.side_effect = list_side_effect

        stage = _make_stage(mock_wiki, mock_base)
        summary = await stage.run(index=idx, src_to_dst=src_to_dst)
        # 2 parents processed: 1 OK, 1 ERROR
        assert summary.parents_total == 2
        assert summary.parents_error == 1
        assert summary.parents_ok == 1

    @pytest.mark.asyncio
    async def test_above_threshold_skipped(
        self, mock_wiki: AsyncMock, mock_base: AsyncMock,
    ) -> None:
        """Parent với 100 children, threshold=10 → skip không gọi move."""
        srcs = [f"s{i}" for i in range(20)]
        dsts = [f"d{i}" for i in range(20)]
        idx = SourceOrderIndex(order={"P_SRC": srcs})
        src_to_dst = {"P_SRC": "P_DST", **dict(zip(srcs, dsts, strict=True))}
        mock_wiki.list_children_tokens.return_value = list(reversed(dsts))

        stage = _make_stage(mock_wiki, mock_base, max_children=10)
        summary = await stage.run(index=idx, src_to_dst=src_to_dst)
        assert summary.parents_skipped == 1
        mock_wiki.move_node.assert_not_called()

    @pytest.mark.asyncio
    async def test_base_batch_update_called_with_record_ids(
        self, mock_wiki: AsyncMock, mock_base: AsyncMock,
    ) -> None:
        """dst_to_record_id provided → stage gọi batch_update."""
        idx = SourceOrderIndex(order={"P_SRC": ["s1", "s2"]})
        src_to_dst = {"P_SRC": "P_DST", "s1": "d1", "s2": "d2"}
        dst_to_record_id = {"d1": "rec1", "d2": "rec2"}
        mock_wiki.list_children_tokens.return_value = ["d2", "d1"]

        stage = _make_stage(mock_wiki, mock_base, audit_only=False)
        await stage.run(
            index=idx,
            src_to_dst=src_to_dst,
            dst_to_record_id=dst_to_record_id,
        )
        # batch_update should have been called at least once
        assert mock_base.batch_update.called
        call = mock_base.batch_update.call_args_list[0]
        records = call.args[2]
        # Records are dicts {record_id, fields}
        rec_ids = {r["record_id"] for r in records}
        assert rec_ids.issubset({"rec1", "rec2"})
        # Status should be "Fixed"
        statuses = {r["fields"]["Tree Order Status"] for r in records}
        assert statuses == {"Fixed"}

    @pytest.mark.asyncio
    async def test_no_base_updates_when_dst_to_record_id_none(
        self, mock_wiki: AsyncMock, mock_base: AsyncMock,
    ) -> None:
        idx = SourceOrderIndex(order={"P_SRC": ["s1", "s2"]})
        src_to_dst = {"P_SRC": "P_DST", "s1": "d1", "s2": "d2"}
        mock_wiki.list_children_tokens.return_value = ["d2", "d1"]

        stage = _make_stage(mock_wiki, mock_base)
        await stage.run(
            index=idx, src_to_dst=src_to_dst, dst_to_record_id=None,
        )
        mock_base.batch_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_idempotent_second_run_no_moves(
        self, mock_wiki: AsyncMock, mock_base: AsyncMock,
    ) -> None:
        """Run lần 2 sau khi Lark đã commit order → 0 moves."""
        idx = SourceOrderIndex(order={"P_SRC": ["s1", "s2", "s3"]})
        src_to_dst = {
            "P_SRC": "P_DST", "s1": "d1", "s2": "d2", "s3": "d3",
        }
        # Lần 1: reversed
        mock_wiki.list_children_tokens.return_value = ["d3", "d2", "d1"]
        stage = _make_stage(mock_wiki, mock_base)
        summary1 = await stage.run(index=idx, src_to_dst=src_to_dst)
        assert summary1.total_moves == 3

        # Lần 2: order đã đúng (simulate Lark đã commit)
        mock_wiki.move_node.reset_mock()
        mock_wiki.list_children_tokens.return_value = ["d1", "d2", "d3"]
        summary2 = await stage.run(index=idx, src_to_dst=src_to_dst)
        assert summary2.parents_ok == 1
        assert summary2.total_moves == 0
        mock_wiki.move_node.assert_not_called()

    @pytest.mark.asyncio
    async def test_specific_parents_filter(
        self, mock_wiki: AsyncMock, mock_base: AsyncMock,
    ) -> None:
        idx = SourceOrderIndex(order={
            "P1": ["s1"], "P2": ["s2"], "P3": ["s3"],
        })
        src_to_dst = {
            "P1": "DP1", "P2": "DP2", "P3": "DP3",
            "s1": "d1", "s2": "d2", "s3": "d3",
        }
        mock_wiki.list_children_tokens.return_value = ["d1"]

        stage = _make_stage(mock_wiki, mock_base)
        summary = await stage.run(
            index=idx, src_to_dst=src_to_dst, parents=["P1", "P3"],
        )
        # Chỉ 2 parents trong filter được process
        assert summary.parents_total == 2


@pytest.mark.unit
class TestTreeOrderStatus:
    def test_all_status_values(self) -> None:
        """Đảm bảo enum exhaustive — match strings dùng trong stage."""
        values = {s.value for s in TreeOrderStatus}
        assert values == {"OK", "Mismatch", "Fixed", "Skipped", "Error"}
