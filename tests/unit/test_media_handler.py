"""Tests cho `MediaHandler` — orchestrate download/upload/PATCH."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from waytoagi.lark.auth import LarkAPIError
from waytoagi.stages.media_handler import MediaHandler


def _mk_handler(
    *,
    src_data: bytes = b"image-data-fake",
    src_error: Exception | None = None,
    upload_token: str = "DST_TOKEN",
    upload_error: Exception | None = None,
    patch_error: Exception | None = None,
    cache: AsyncMock | None = None,
) -> tuple[MediaHandler, AsyncMock, AsyncMock, AsyncMock]:
    src_media = AsyncMock()
    if src_error:
        src_media.download = AsyncMock(side_effect=src_error)
    else:
        src_media.download = AsyncMock(return_value=src_data)

    dst_media = AsyncMock()
    if upload_error:
        dst_media.upload = AsyncMock(side_effect=upload_error)
    else:
        dst_media.upload = AsyncMock(return_value=upload_token)

    dst_doc = AsyncMock()
    if patch_error:
        dst_doc.patch_block = AsyncMock(side_effect=patch_error)
    else:
        dst_doc.patch_block = AsyncMock(return_value={"code": 0})

    handler = MediaHandler(
        src_media=src_media,
        dst_media=dst_media,
        dst_doc=dst_doc,
        cache=cache,
    )
    return handler, src_media, dst_media, dst_doc


def _mk_cache() -> AsyncMock:
    store: dict[str, str] = {}

    async def get(token: str) -> str | None:
        return store.get(token)

    async def put(src: str, dst: str, *, size: int | None = None) -> None:
        store[src] = dst

    cache = AsyncMock()
    cache.get = AsyncMock(side_effect=get)
    cache.put = AsyncMock(side_effect=put)
    cache._store = store  # type: ignore[attr-defined]
    return cache


@pytest.mark.unit
class TestMediaHandlerHappyPath:
    @pytest.mark.asyncio
    async def test_clone_image_basic(self) -> None:
        handler, src, dst, doc = _mk_handler()
        result = await handler.clone_media_to_block(
            src_file_token="SRC1",
            dst_doc_id="doc-id",
            dst_block_id="block-1",
            kind="image",
        )
        assert result.success is True
        assert result.dst_file_token == "DST_TOKEN"
        assert result.cached is False
        src.download.assert_called_once_with("SRC1")
        dst.upload.assert_called_once()
        doc.patch_block.assert_called_once()
        # Verify PATCH body
        body = doc.patch_block.call_args.args[2]
        assert "replace_image" in body
        assert body["replace_image"]["token"] == "DST_TOKEN"

    @pytest.mark.asyncio
    async def test_clone_file_uses_replace_file(self) -> None:
        handler, _src, dst, doc = _mk_handler()
        await handler.clone_media_to_block(
            src_file_token="SRC2",
            dst_doc_id="doc",
            dst_block_id="b1",
            kind="file",
            file_name="report.pdf",
        )
        body = doc.patch_block.call_args.args[2]
        assert "replace_file" in body
        # Upload kwargs contain parent_type=docx_file
        upload_kwargs = dst.upload.call_args.kwargs
        assert upload_kwargs["parent_type"] == "docx_file"
        assert upload_kwargs["file_name"] == "report.pdf"


@pytest.mark.unit
class TestMediaHandlerCache:
    @pytest.mark.asyncio
    async def test_cache_hit_skips_download_upload(self) -> None:
        cache = _mk_cache()
        cache._store["SRC1"] = "EXISTING_DST"
        handler, src, dst, doc = _mk_handler(cache=cache)
        result = await handler.clone_media_to_block(
            src_file_token="SRC1",
            dst_doc_id="doc",
            dst_block_id="b1",
            kind="image",
        )
        assert result.success is True
        assert result.cached is True
        assert result.dst_file_token == "EXISTING_DST"
        # KHÔNG download/upload
        src.download.assert_not_called()
        dst.upload.assert_not_called()
        # Vẫn PATCH để gắn vào block mới
        doc.patch_block.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_miss_then_put(self) -> None:
        cache = _mk_cache()
        handler, _, _, _ = _mk_handler(cache=cache)
        await handler.clone_media_to_block(
            src_file_token="NEW_SRC",
            dst_doc_id="doc",
            dst_block_id="b1",
            kind="image",
        )
        # Cache PUT đã được gọi
        assert "NEW_SRC" in cache._store


@pytest.mark.unit
class TestMediaHandlerFailures:
    @pytest.mark.asyncio
    async def test_download_failure(self) -> None:
        handler, _, dst, _doc = _mk_handler(
            src_error=LarkAPIError(131005, "not found", "/download"),
        )
        result = await handler.clone_media_to_block(
            src_file_token="MISSING",
            dst_doc_id="doc",
            dst_block_id="b1",
            kind="image",
        )
        assert result.success is False
        assert "131005" in result.error
        # Không upload vì download fail
        dst.upload.assert_not_called()

    @pytest.mark.asyncio
    async def test_upload_failure(self) -> None:
        handler, _, _, doc = _mk_handler(
            upload_error=LarkAPIError(99991400, "rate limit", "/upload"),
        )
        result = await handler.clone_media_to_block(
            src_file_token="SRC",
            dst_doc_id="doc",
            dst_block_id="b1",
            kind="image",
        )
        assert result.success is False
        assert "99991400" in result.error
        doc.patch_block.assert_not_called()

    @pytest.mark.asyncio
    async def test_patch_failure_marks_failed_no_cache_put(self) -> None:
        cache = _mk_cache()
        handler, _, _, _ = _mk_handler(
            cache=cache,
            patch_error=LarkAPIError(1770013, "relation mismatch", "/patch"),
        )
        result = await handler.clone_media_to_block(
            src_file_token="SRC",
            dst_doc_id="doc",
            dst_block_id="b1",
            kind="image",
        )
        # Upload OK nhưng PATCH fail → success=False, không cache
        assert result.success is False
        assert "SRC" not in cache._store

    @pytest.mark.asyncio
    async def test_empty_download_treated_as_failure(self) -> None:
        handler, _, dst, _doc = _mk_handler(src_data=b"")
        result = await handler.clone_media_to_block(
            src_file_token="EMPTY",
            dst_doc_id="doc",
            dst_block_id="b1",
            kind="image",
        )
        assert result.success is False
        assert "empty" in result.error.lower()
        dst.upload.assert_not_called()
