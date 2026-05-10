"""Tests cho `SmartSyncStage` — orchestration với mock LarkDocument + LarkBase."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from waytoagi.lark.auth import LarkAPIError
from waytoagi.stages.sync import SmartSyncStage, SyncOutcome


def _block_dict(block_id: str, content: str) -> dict[str, Any]:
    """Build dict raw block (như Lark API trả)."""
    return {
        "block_id": block_id,
        "block_type": 2,  # text
        "text": {
            "elements": [{"text_run": {"content": content}}],
        },
    }


def _mk_doc_with(blocks: list[dict[str, Any]]) -> AsyncMock:
    """Mock LarkDocument với collect_all_blocks return list."""
    doc = AsyncMock()
    doc.collect_all_blocks = AsyncMock(return_value=blocks)
    doc.patch_block = AsyncMock(return_value={"code": 0})
    doc.create_children = AsyncMock(return_value={"code": 0})
    doc.delete_children = AsyncMock(return_value={"code": 0})
    return doc


def _mk_base() -> AsyncMock:
    base = AsyncMock()
    base.update_record = AsyncMock(return_value={"code": 0})
    return base


def _mk_stage(src_doc: AsyncMock, dst_doc: AsyncMock, base: AsyncMock) -> SmartSyncStage:
    return SmartSyncStage(
        src_doc=src_doc,
        dst_doc=dst_doc,
        base=base,
        app_token="app",
        table_id="tbl",
    )


@pytest.mark.unit
class TestSmartSyncStageNoOp:
    @pytest.mark.asyncio
    async def test_identical_docs_no_op(self) -> None:
        """Src + dst giống hệt → 0 patches, status NoChange."""
        blocks = [
            _block_dict("b1", "A"),
            _block_dict("b2", "B"),
        ]
        src_doc = _mk_doc_with(blocks)
        dst_doc = _mk_doc_with([
            _block_dict("d1", "A"),  # same content
            _block_dict("d2", "B"),
        ])
        base = _mk_base()
        stage = _mk_stage(src_doc, dst_doc, base)
        result = await stage.sync_one(
            src_doc_id="src", dst_doc_id="dst", record_id="rec1",
        )
        assert result.status == SyncOutcome.NO_OP
        assert result.patches_succeeded == 0
        # No patch_block calls
        dst_doc.patch_block.assert_not_called()
        # Base updated với NoChange
        base.update_record.assert_called_once()
        update_fields = base.update_record.call_args.args[3]
        assert update_fields["Mirror Wiki Status"] == SyncOutcome.NO_OP


@pytest.mark.unit
class TestSmartSyncStageReplace:
    @pytest.mark.asyncio
    async def test_one_block_changed_one_patch(self) -> None:
        src = [
            _block_dict("s1", "A"),
            _block_dict("s2", "B-CHANGED"),
            _block_dict("s3", "C"),
        ]
        dst = [
            _block_dict("d1", "A"),
            _block_dict("d2", "B"),
            _block_dict("d3", "C"),
        ]
        src_doc = _mk_doc_with(src)
        dst_doc = _mk_doc_with(dst)
        base = _mk_base()
        stage = _mk_stage(src_doc, dst_doc, base)
        result = await stage.sync_one(
            src_doc_id="src", dst_doc_id="dst", record_id="rec1",
        )
        assert result.status == SyncOutcome.DONE
        assert result.patches_succeeded == 1
        # Patched đúng block d2
        dst_doc.patch_block.assert_called_once()
        call = dst_doc.patch_block.call_args
        assert call.args[1] == "d2"  # block_id

    @pytest.mark.asyncio
    async def test_saved_calls_for_big_doc(self) -> None:
        """Big doc edit ít → saved_calls cao."""
        src = [
            _block_dict(f"s{i}", f"content-{i}" + ("-EDITED" if i == 50 else ""))
            for i in range(100)
        ]
        dst = [_block_dict(f"d{i}", f"content-{i}") for i in range(100)]
        src_doc = _mk_doc_with(src)
        dst_doc = _mk_doc_with(dst)
        base = _mk_base()
        stage = _mk_stage(src_doc, dst_doc, base)
        result = await stage.sync_one(
            src_doc_id="src", dst_doc_id="dst", record_id="rec1",
        )
        # 1 patch, 99 keep saved
        assert result.patches_succeeded == 1
        assert result.saved_calls == 99
        assert dst_doc.patch_block.call_count == 1


@pytest.mark.unit
class TestSmartSyncStageAppend:
    @pytest.mark.asyncio
    async def test_src_longer_appends(self) -> None:
        src = [
            _block_dict("s1", "A"),
            _block_dict("s2", "B"),  # new block
        ]
        dst = [_block_dict("d1", "A")]
        src_doc = _mk_doc_with(src)
        dst_doc = _mk_doc_with(dst)
        base = _mk_base()
        stage = _mk_stage(src_doc, dst_doc, base)
        result = await stage.sync_one(
            src_doc_id="src", dst_doc_id="dst", record_id="rec1",
        )
        assert result.status == SyncOutcome.DONE
        # 1 create_children call cho block mới
        dst_doc.create_children.assert_called_once()


@pytest.mark.unit
class TestSmartSyncStageFailure:
    @pytest.mark.asyncio
    async def test_read_failure_marks_failed(self) -> None:
        src_doc = AsyncMock()
        src_doc.collect_all_blocks = AsyncMock(
            side_effect=LarkAPIError(131005, "doc not found", "/get"),
        )
        dst_doc = _mk_doc_with([])
        base = _mk_base()
        stage = _mk_stage(src_doc, dst_doc, base)
        result = await stage.sync_one(
            src_doc_id="src", dst_doc_id="dst", record_id="rec1",
        )
        assert result.status == SyncOutcome.FAILED
        assert "131005" in result.error
        # Base status updated với Failed
        base.update_record.assert_called_once()

    @pytest.mark.asyncio
    async def test_partial_patch_fail_marks_partial(self) -> None:
        """1 patch fail giữa 2 → status PartialFail."""
        src = [
            _block_dict("s1", "A-CHANGED"),
            _block_dict("s2", "B-CHANGED"),
        ]
        dst = [
            _block_dict("d1", "A"),
            _block_dict("d2", "B"),
        ]
        src_doc = _mk_doc_with(src)
        dst_doc = _mk_doc_with(dst)
        # 2nd patch fails
        call_count = {"i": 0}

        async def patch_side_effect(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            call_count["i"] += 1
            if call_count["i"] == 2:
                raise LarkAPIError(99991400, "rate limit", "/patch")
            return {"code": 0}

        dst_doc.patch_block = AsyncMock(side_effect=patch_side_effect)
        base = _mk_base()
        stage = _mk_stage(src_doc, dst_doc, base)
        result = await stage.sync_one(
            src_doc_id="src", dst_doc_id="dst", record_id="rec1",
        )
        assert result.status == SyncOutcome.PARTIAL
        assert result.patches_succeeded == 1
        assert result.patches_failed == 1


@pytest.mark.unit
class TestSmartSyncStageBaseUpdate:
    @pytest.mark.asyncio
    async def test_base_status_done_after_success(self) -> None:
        src = [_block_dict("s1", "A-CHANGED")]
        dst = [_block_dict("d1", "A")]
        src_doc = _mk_doc_with(src)
        dst_doc = _mk_doc_with(dst)
        base = _mk_base()
        stage = _mk_stage(src_doc, dst_doc, base)
        await stage.sync_one(
            src_doc_id="src", dst_doc_id="dst", record_id="rec1",
        )
        update_fields = base.update_record.call_args.args[3]
        assert update_fields["Mirror Wiki Status"] == SyncOutcome.DONE
        assert "Mirror Last Synced At" in update_fields
        assert isinstance(update_fields["Mirror Last Synced At"], int)

    @pytest.mark.asyncio
    async def test_base_update_failure_does_not_raise(self) -> None:
        """Base update fail → log warning, không halt sync."""
        src = [_block_dict("s1", "A")]
        dst = [_block_dict("d1", "A")]
        src_doc = _mk_doc_with(src)
        dst_doc = _mk_doc_with(dst)
        base = AsyncMock()
        base.update_record = AsyncMock(
            side_effect=LarkAPIError(1254000, "table down", "/update"),
        )
        stage = _mk_stage(src_doc, dst_doc, base)
        # Không raise
        result = await stage.sync_one(
            src_doc_id="src", dst_doc_id="dst", record_id="rec1",
        )
        assert result.status == SyncOutcome.NO_OP
