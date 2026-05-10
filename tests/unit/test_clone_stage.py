"""Tests cho `CloneStage` — block-by-block clone."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from waytoagi.backlinks.mapper import UrlMapper
from waytoagi.lark.auth import LarkAPIError
from waytoagi.stages.clone import CloneStage


def _text_block(block_id: str, content: str, *, link_url: str = "") -> dict[str, Any]:
    """Build text block với 1 text_run."""
    text_run: dict[str, Any] = {"content": content}
    if link_url:
        text_run["text_element_style"] = {"link": {"url": link_url}}
    return {
        "block_id": block_id,
        "block_type": 2,
        "text": {"elements": [{"text_run": text_run}]},
    }


def _image_block(block_id: str, file_token: str) -> dict[str, Any]:
    return {
        "block_id": block_id,
        "block_type": 27,
        "image": {"token": file_token},
    }


def _mk_doc_with_blocks(blocks: list[dict[str, Any]]) -> AsyncMock:
    doc = AsyncMock()
    doc.collect_all_blocks = AsyncMock(return_value=blocks)
    doc.create_children = AsyncMock(return_value={
        "code": 0,
        "data": {"children": [{"block_id": "new-block-id"}]},
    })
    doc.patch_block = AsyncMock(return_value={"code": 0})
    return doc


def _mk_media_handler(*, success: bool = True) -> AsyncMock:
    from waytoagi.stages.media_handler import MediaCloneResult
    handler = AsyncMock()
    handler.clone_media_to_block = AsyncMock(return_value=MediaCloneResult(
        src_file_token="SRC",
        dst_file_token="DST" if success else "",
        success=success,
    ))
    return handler


@pytest.mark.unit
class TestCloneStageBasic:
    @pytest.mark.asyncio
    async def test_clone_text_blocks(self) -> None:
        src_doc = _mk_doc_with_blocks([
            _text_block("s1", "Hello"),
            _text_block("s2", "World"),
        ])
        dst_doc = _mk_doc_with_blocks([])
        media = _mk_media_handler()
        stage = CloneStage(
            src_doc=src_doc, dst_doc=dst_doc, media_handler=media,
        )
        result = await stage.clone_one(src_doc_id="src", dst_doc_id="dst")
        assert result.success is True
        assert result.stats.blocks_total == 2
        assert result.stats.blocks_recreated == 2
        # 2 create_children calls (1 per text block)
        assert dst_doc.create_children.call_count == 2

    @pytest.mark.asyncio
    async def test_clone_image_block(self) -> None:
        src_doc = _mk_doc_with_blocks([
            _image_block("s1", "SRC_FILE_TOKEN"),
        ])
        dst_doc = _mk_doc_with_blocks([])
        media = _mk_media_handler(success=True)
        stage = CloneStage(
            src_doc=src_doc, dst_doc=dst_doc, media_handler=media,
        )
        result = await stage.clone_one(src_doc_id="src", dst_doc_id="dst")
        assert result.success is True
        assert result.stats.images_cloned == 1
        media.clone_media_to_block.assert_called_once()


@pytest.mark.unit
class TestCloneStageUrlSwap:
    @pytest.mark.asyncio
    async def test_url_swapped_inline(self) -> None:
        # Mapper: src_token "ABC123" → dst_token "DST456"
        mapper = UrlMapper(
            source_domain="waytoagi.feishu.cn",
            dst_domain="vudanhdu.sg.larksuite.com",
            mapping={"ABC123": "DST456"},
        )
        src_doc = _mk_doc_with_blocks([
            _text_block(
                "s1", "Click here",
                link_url="https://waytoagi.feishu.cn/wiki/ABC123",
            ),
        ])
        dst_doc = _mk_doc_with_blocks([])
        media = _mk_media_handler()
        stage = CloneStage(
            src_doc=src_doc, dst_doc=dst_doc,
            media_handler=media, url_mapper=mapper,
        )
        result = await stage.clone_one(src_doc_id="src", dst_doc_id="dst")
        assert result.stats.urls_swapped == 1
        # Verify body của create_children có URL swapped
        call_body = dst_doc.create_children.call_args.args[2]
        block = call_body[0]
        elements = block["text"]["elements"]
        url = elements[0]["text_run"]["text_element_style"]["link"]["url"]
        assert "vudanhdu.sg.larksuite.com" in url
        assert "DST456" in url


@pytest.mark.unit
class TestCloneStageFailures:
    @pytest.mark.asyncio
    async def test_read_failure_marks_fail(self) -> None:
        src_doc = AsyncMock()
        src_doc.collect_all_blocks = AsyncMock(
            side_effect=LarkAPIError(131005, "not found", "/x"),
        )
        dst_doc = _mk_doc_with_blocks([])
        media = _mk_media_handler()
        stage = CloneStage(
            src_doc=src_doc, dst_doc=dst_doc, media_handler=media,
        )
        result = await stage.clone_one(src_doc_id="src", dst_doc_id="dst")
        assert result.success is False
        assert "131005" in result.error

    @pytest.mark.asyncio
    async def test_block_handler_fail_isolated(self) -> None:
        """1 block fail → các blocks khác vẫn process."""
        src_doc = _mk_doc_with_blocks([
            _text_block("s1", "OK"),
            _text_block("s2", "FAIL"),
            _text_block("s3", "OK"),
        ])
        dst_doc = AsyncMock()
        dst_doc.collect_all_blocks = AsyncMock(return_value=[])
        # 2nd create fails
        call_count = {"i": 0}

        async def create_side(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            call_count["i"] += 1
            if call_count["i"] == 2:
                raise LarkAPIError(99991400, "rate", "/create")
            return {"code": 0, "data": {"children": [{"block_id": "x"}]}}

        dst_doc.create_children = AsyncMock(side_effect=create_side)
        dst_doc.patch_block = AsyncMock(return_value={"code": 0})
        media = _mk_media_handler()
        stage = CloneStage(
            src_doc=src_doc, dst_doc=dst_doc, media_handler=media,
        )
        result = await stage.clone_one(src_doc_id="src", dst_doc_id="dst")
        # 2 OK + 1 fail
        assert result.stats.blocks_recreated == 2
        assert result.stats.blocks_failed == 1


@pytest.mark.unit
class TestCloneStageUnsupported:
    @pytest.mark.asyncio
    async def test_skip_unsupported_block_type(self) -> None:
        src_doc = _mk_doc_with_blocks([
            {"block_id": "s1", "block_type": 22},  # diagram
            _text_block("s2", "OK"),
        ])
        dst_doc = _mk_doc_with_blocks([])
        media = _mk_media_handler()
        stage = CloneStage(
            src_doc=src_doc, dst_doc=dst_doc, media_handler=media,
        )
        result = await stage.clone_one(src_doc_id="src", dst_doc_id="dst")
        assert result.stats.blocks_skipped == 1
        assert result.stats.blocks_recreated == 1
