"""Tests cho `MirrorStage`."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from waytoagi.stages.mirror import MirrorStage


def _mk_doc(blocks: list[dict[str, Any]]) -> AsyncMock:
    doc = AsyncMock()
    doc.collect_all_blocks = AsyncMock(return_value=blocks)
    doc.patch_block = AsyncMock(return_value={"code": 0})
    doc.create_children = AsyncMock(return_value={"code": 0})
    return doc


def _text_block(block_id: str, content: str) -> dict[str, Any]:
    return {
        "block_id": block_id,
        "block_type": 2,
        "text": {"elements": [{"text_run": {"content": content}}]},
    }


def _mk_wiki() -> AsyncMock:
    wiki = AsyncMock()
    wiki.auth = AsyncMock()
    wiki.auth.post = AsyncMock(return_value={"code": 0})
    return wiki


def _mk_base() -> AsyncMock:
    base = AsyncMock()
    base.update_record = AsyncMock(return_value={"code": 0})
    return base


@pytest.mark.unit
class TestMirrorStageBasic:
    @pytest.mark.asyncio
    async def test_mirror_fills_placeholder(self) -> None:
        # VI doc có 2 blocks, DST placeholder rỗng
        vi_doc = _mk_doc([
            _text_block("v1", "Đoạn 1"),
            _text_block("v2", "Đoạn 2"),
        ])
        dst_doc = _mk_doc([])  # placeholder rỗng
        wiki = _mk_wiki()
        base = _mk_base()

        stage = MirrorStage(
            vi_doc=vi_doc, dst_doc=dst_doc, dst_wiki=wiki,
            base=base, app_token="app", table_id="tbl",
            dst_space_id="space-1",
        )
        result = await stage.mirror_one(
            vi_doc_id="vi-1",
            dst_doc_id="dst-1",
            dst_node_token="dst-node",
            record_id="rec-1",
        )
        assert result.success is True
        # Empty dst → diff append cả 2 → có patch hoặc create
        # (SmartSyncStage sẽ append vào dst)

    @pytest.mark.asyncio
    async def test_mirror_no_op_when_identical(self) -> None:
        # VI và DST giống hệt → no-op
        same = [
            _text_block("v1", "A"),
            _text_block("v2", "B"),
        ]
        vi_doc = _mk_doc(same)
        # Cùng content nhưng block_ids khác (như Lark sau real clone)
        dst_doc = _mk_doc([
            _text_block("d1", "A"),
            _text_block("d2", "B"),
        ])
        stage = MirrorStage(
            vi_doc=vi_doc, dst_doc=dst_doc, dst_wiki=_mk_wiki(),
            base=_mk_base(), app_token="app", table_id="tbl",
            dst_space_id="space-1",
        )
        result = await stage.mirror_one(
            vi_doc_id="vi", dst_doc_id="dst",
            dst_node_token="t", record_id="r1",
        )
        assert result.success is True
        # 0 PATCH calls vì content identical
        assert dst_doc.patch_block.call_count == 0


@pytest.mark.unit
class TestMirrorStageTitleUpdate:
    @pytest.mark.asyncio
    async def test_vi_title_updates_dst_node(self) -> None:
        vi_doc = _mk_doc([_text_block("v1", "Hello")])
        dst_doc = _mk_doc([])
        wiki = _mk_wiki()
        stage = MirrorStage(
            vi_doc=vi_doc, dst_doc=dst_doc, dst_wiki=wiki,
            base=_mk_base(), app_token="app", table_id="tbl",
            dst_space_id="space-x",
        )
        await stage.mirror_one(
            vi_doc_id="vi", dst_doc_id="dst",
            dst_node_token="dst-node",
            record_id="r1",
            vi_title="Tiêu đề tiếng Việt",
        )
        # Verify update_title API được gọi
        wiki.auth.post.assert_called_once()
        call = wiki.auth.post.call_args
        path = call.args[0]
        body = call.kwargs.get("json_body") or call.args[1]
        assert "update_title" in path
        assert body == {"title": "Tiêu đề tiếng Việt"}

    @pytest.mark.asyncio
    async def test_no_title_no_update(self) -> None:
        vi_doc = _mk_doc([_text_block("v1", "Hello")])
        dst_doc = _mk_doc([])
        wiki = _mk_wiki()
        stage = MirrorStage(
            vi_doc=vi_doc, dst_doc=dst_doc, dst_wiki=wiki,
            base=_mk_base(), app_token="app", table_id="tbl",
            dst_space_id="space-x",
        )
        await stage.mirror_one(
            vi_doc_id="vi", dst_doc_id="dst",
            dst_node_token="dst-node",
            record_id="r1",
            vi_title="",  # empty → skip
        )
        wiki.auth.post.assert_not_called()
