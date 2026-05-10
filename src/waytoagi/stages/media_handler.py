"""MediaHandler — orchestrate download src → cache check → upload dst → PATCH.

Tầng cao hơn `LarkMedia` client. Lý do tách:
  - LarkMedia là raw API wrapper, không biết về `MediaTokenCache`.
  - MediaHandler thêm logic:
      1. Cache check: src_file_token đã upload chưa → reuse dst_file_token
      2. Download từ tenant CN
      3. Upload lên tenant DST
      4. Cache PUT (src → dst)
      5. PATCH replace_image / replace_file

Idempotent qua cache: clone lại cùng src → reuse dst_file_token, không
re-download/re-upload.

Failure isolation: 1 media fail → log + skip block đó, không halt clone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import structlog

from waytoagi.lark.auth import LarkAPIError

if TYPE_CHECKING:
    from waytoagi.cache.sqlite import MediaTokenCache
    from waytoagi.lark.document import LarkDocument
    from waytoagi.lark.media import LarkMedia

logger = structlog.get_logger(__name__)

MediaKind = Literal["image", "file"]


@dataclass(slots=True)
class MediaCloneResult:
    """Kết quả 1 lần clone media."""

    src_file_token: str
    dst_file_token: str = ""
    cached: bool = False
    success: bool = False
    error: str = ""
    bytes_transferred: int = 0


class MediaHandler:
    """High-level orchestrator cho media clone CN → DST.

    Args:
        src_media: LarkMedia bound vào tenant CN (download).
        dst_media: LarkMedia bound vào tenant DST (upload).
        dst_doc: LarkDocument bound vào tenant DST (PATCH replace).
        cache: MediaTokenCache SQLite — skip nếu đã có dst_token.
    """

    def __init__(
        self,
        *,
        src_media: LarkMedia,
        dst_media: LarkMedia,
        dst_doc: LarkDocument,
        cache: MediaTokenCache | None = None,
    ) -> None:
        self._src_media = src_media
        self._dst_media = dst_media
        self._dst_doc = dst_doc
        self._cache = cache
        self._log = logger.bind(component="MediaHandler")

    # ====================================================================
    # Public API
    # ====================================================================

    async def clone_media_to_block(
        self,
        *,
        src_file_token: str,
        dst_doc_id: str,
        dst_block_id: str,
        kind: MediaKind,
        file_name: str = "",
    ) -> MediaCloneResult:
        """Clone 1 media (CN → DST) + bind vào dst block.

        Pipeline:
          1. Cache lookup → nếu hit, dùng dst_token cũ
          2. Download từ src tenant
          3. Upload lên dst tenant với parent_node = dst_block_id
          4. PATCH replace_image / replace_file vào block
          5. Cache PUT
        """
        log = self._log.bind(
            src=src_file_token, dst_block=dst_block_id, kind=kind,
        )

        # 1. Cache lookup
        if self._cache:
            cached_dst = await self._cache.get(src_file_token)
            if cached_dst:
                # Cache hit — vẫn cần PATCH để gắn vào block mới
                ok = await self._patch_replace(
                    dst_doc_id, dst_block_id, cached_dst, kind, log,
                )
                return MediaCloneResult(
                    src_file_token=src_file_token,
                    dst_file_token=cached_dst,
                    cached=True,
                    success=ok,
                )

        # 2. Download
        try:
            data = await self._src_media.download(src_file_token)
        except LarkAPIError as e:
            return MediaCloneResult(
                src_file_token=src_file_token,
                success=False,
                error=f"download:[{e.code}]{e.msg[:80]}",
            )
        except Exception as e:  # network / unexpected
            return MediaCloneResult(
                src_file_token=src_file_token,
                success=False,
                error=f"download:{e!s}"[:200],
            )

        if not data:
            return MediaCloneResult(
                src_file_token=src_file_token,
                success=False,
                error="download_empty",
            )

        # 3. Upload
        parent_type = "docx_image" if kind == "image" else "docx_file"
        try:
            dst_token = await self._dst_media.upload(
                data=data,
                parent_node=dst_block_id,
                parent_type=parent_type,
                file_name=file_name or None,
            )
        except LarkAPIError as e:
            return MediaCloneResult(
                src_file_token=src_file_token,
                success=False,
                error=f"upload:[{e.code}]{e.msg[:80]}",
                bytes_transferred=len(data),
            )
        except Exception as e:
            return MediaCloneResult(
                src_file_token=src_file_token,
                success=False,
                error=f"upload:{e!s}"[:200],
                bytes_transferred=len(data),
            )

        # 4. PATCH replace_image / replace_file
        ok = await self._patch_replace(
            dst_doc_id, dst_block_id, dst_token, kind, log,
        )

        # 5. Cache PUT — chỉ khi success toàn bộ flow
        if ok and self._cache:
            await self._cache.put(
                src_file_token, dst_token, size=len(data),
            )

        log.info(
            "media_clone_done",
            success=ok, bytes=len(data), dst_token=dst_token[:18],
        )
        return MediaCloneResult(
            src_file_token=src_file_token,
            dst_file_token=dst_token,
            success=ok,
            bytes_transferred=len(data),
        )

    # ====================================================================
    # Internal
    # ====================================================================

    async def _patch_replace(
        self,
        dst_doc_id: str,
        dst_block_id: str,
        dst_file_token: str,
        kind: MediaKind,
        log: structlog.stdlib.BoundLogger,
    ) -> bool:
        """PATCH block để gắn file_token (replace_image / replace_file)."""
        op_name = "replace_image" if kind == "image" else "replace_file"
        body = {op_name: {"token": dst_file_token}}
        try:
            await self._dst_doc.patch_block(dst_doc_id, dst_block_id, body)
        except LarkAPIError as e:
            log.warning(
                "media_patch_failed",
                code=e.code, msg=e.msg[:80], op=op_name,
            )
            return False
        return True
