"""Tests cho `PlaceholderCreator` — tạo dst doc rỗng trên DST tenant."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from waytoagi.lark.auth import LarkAPIError
from waytoagi.models.crawl import CrawlEvent, CrawlPlanItem
from waytoagi.stages.placeholder import PlaceholderCreator


def _make_item(
    *,
    src_token: str = "src1",
    title: str = "Test Doc",
    existing_dst: str = "",
) -> CrawlPlanItem:
    return CrawlPlanItem(
        src_node_token=src_token,
        src_parent_token="",
        src_obj_token="obj1",
        src_obj_type="docx",
        src_node_type="origin",
        title=title,
        obj_edit_time_ms=0,
        event=CrawlEvent.NEW,
        existing_dst_token=existing_dst,
    )


def _make_creator(wiki: AsyncMock) -> PlaceholderCreator:
    return PlaceholderCreator(
        wiki=wiki,
        space_id="dst-space",
        default_parent_token="dst-root",
        dst_domain="vudanhdu.sg.larksuite.com",
    )


@pytest.mark.unit
class TestPlaceholderCreatorIdempotent:
    @pytest.mark.asyncio
    async def test_skip_if_existing_dst_token(self) -> None:
        wiki = AsyncMock()
        wiki.create_node = AsyncMock()
        creator = _make_creator(wiki)
        result = await creator.create_for_item(
            _make_item(existing_dst="DST_EXISTING"),
        )
        assert result.success is True
        assert result.skipped_existing is True
        assert result.dst_node_token == "DST_EXISTING"
        wiki.create_node.assert_not_called()

    @pytest.mark.asyncio
    async def test_url_built_from_existing_token(self) -> None:
        wiki = AsyncMock()
        creator = _make_creator(wiki)
        result = await creator.create_for_item(
            _make_item(existing_dst="abc123"),
        )
        assert result.dst_url == (
            "https://vudanhdu.sg.larksuite.com/wiki/abc123"
        )


@pytest.mark.unit
class TestPlaceholderCreatorSuccess:
    @pytest.mark.asyncio
    async def test_create_basic_returns_dst_token(self) -> None:
        wiki = AsyncMock()
        wiki.create_node = AsyncMock(return_value={
            "code": 0,
            "data": {"node": {"node_token": "DST_NEW", "obj_token": "OBJ_NEW"}},
        })
        creator = _make_creator(wiki)
        result = await creator.create_for_item(_make_item(title="Hello"))
        assert result.success is True
        assert result.dst_node_token == "DST_NEW"
        assert result.dst_url.endswith("/wiki/DST_NEW")
        assert result.skipped_existing is False
        wiki.create_node.assert_called_once()

    @pytest.mark.asyncio
    async def test_passes_title_and_parent(self) -> None:
        wiki = AsyncMock()
        wiki.create_node = AsyncMock(return_value={
            "code": 0,
            "data": {"node": {"node_token": "DST_X"}},
        })
        creator = _make_creator(wiki)
        await creator.create_for_item(_make_item(title="Hello"))
        call = wiki.create_node.call_args
        assert call.kwargs["title"] == "Hello"
        assert call.kwargs["parent_node_token"] == "dst-root"
        assert call.kwargs["obj_type"] == "docx"

    @pytest.mark.asyncio
    async def test_parent_override(self) -> None:
        wiki = AsyncMock()
        wiki.create_node = AsyncMock(return_value={
            "code": 0,
            "data": {"node": {"node_token": "X"}},
        })
        creator = _make_creator(wiki)
        await creator.create_for_item(
            _make_item(),
            parent_dst_token="custom-parent",
        )
        call = wiki.create_node.call_args
        assert call.kwargs["parent_node_token"] == "custom-parent"

    @pytest.mark.asyncio
    async def test_title_prefix_applied(self) -> None:
        wiki = AsyncMock()
        wiki.create_node = AsyncMock(return_value={
            "code": 0,
            "data": {"node": {"node_token": "X"}},
        })
        creator = PlaceholderCreator(
            wiki=wiki,
            space_id="dst-space",
            default_parent_token="dst-root",
            dst_domain="dst.larksuite.com",
            title_prefix="[chờ dịch] ",
        )
        await creator.create_for_item(_make_item(title="Hello"))
        call = wiki.create_node.call_args
        assert call.kwargs["title"] == "[chờ dịch] Hello"

    @pytest.mark.asyncio
    async def test_empty_title_uses_fallback(self) -> None:
        wiki = AsyncMock()
        wiki.create_node = AsyncMock(return_value={
            "code": 0,
            "data": {"node": {"node_token": "X"}},
        })
        creator = _make_creator(wiki)
        await creator.create_for_item(_make_item(title=""))
        call = wiki.create_node.call_args
        assert call.kwargs["title"] == "[empty]"


@pytest.mark.unit
class TestPlaceholderCreatorFailure:
    @pytest.mark.asyncio
    async def test_lark_api_error_returns_failed(self) -> None:
        wiki = AsyncMock()
        wiki.create_node = AsyncMock(
            side_effect=LarkAPIError(99991400, "rate limit", "/create"),
        )
        creator = _make_creator(wiki)
        result = await creator.create_for_item(_make_item())
        assert result.success is False
        assert "99991400" in result.error
        assert "rate limit" in result.error
        assert result.dst_node_token == ""

    @pytest.mark.asyncio
    async def test_unexpected_exception_caught(self) -> None:
        wiki = AsyncMock()
        wiki.create_node = AsyncMock(
            side_effect=RuntimeError("network down"),
        )
        creator = _make_creator(wiki)
        result = await creator.create_for_item(_make_item())
        assert result.success is False
        assert "network down" in result.error

    @pytest.mark.asyncio
    async def test_missing_node_token_in_response(self) -> None:
        """Lark trả response không có node_token → fail."""
        wiki = AsyncMock()
        wiki.create_node = AsyncMock(return_value={
            "code": 0,
            "data": {},  # missing node
        })
        creator = _make_creator(wiki)
        result = await creator.create_for_item(_make_item())
        assert result.success is False
        assert "missing node_token" in result.error


@pytest.mark.unit
class TestPlaceholderCreatorTiming:
    @pytest.mark.asyncio
    async def test_elapsed_seconds_recorded(self) -> None:
        wiki = AsyncMock()
        wiki.create_node = AsyncMock(return_value={
            "code": 0,
            "data": {"node": {"node_token": "X"}},
        })
        creator = _make_creator(wiki)
        result = await creator.create_for_item(_make_item())
        assert result.elapsed_seconds >= 0
