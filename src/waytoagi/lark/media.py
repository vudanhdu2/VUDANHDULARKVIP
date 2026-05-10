"""LarkMedia — async download/upload với 4-method fallback + multipart cho file >20MB.

Port từ legacy clone_v2.py + agents/utils.py. Giữ nguyên 4 phương thức download:
1. /drive/v1/medias/{token}/download (official)
2. CDN với session cookies (cho public wiki)
3. File preview download (cho video thumbnails)
4. CDN với bearer token

Upload:
- File ≤ 20MB → /drive/v1/medias/upload_all (single shot)
- File > 20MB → upload_prepare → upload_part (4MB chunks) → upload_finish
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import structlog

if TYPE_CHECKING:
    from waytoagi.lark.auth import LarkAuth

logger = structlog.get_logger(__name__)

CHUNK_SIZE: int = 4 * 1024 * 1024  # 4 MB
SINGLE_UPLOAD_THRESHOLD: int = 20 * 1024 * 1024  # 20 MB


def _detect_ext(head: bytes) -> str:
    """Detect file extension by magic bytes (8-16 bytes)."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"GIF8"):
        return "gif"
    if len(head) >= 12 and head[8:12] == b"WEBP":
        return "webp"
    if head[:4] == b"%PDF":
        return "pdf"
    if head[4:8] == b"ftyp":
        return "mp4"
    return "bin"


class MediaDownloadError(Exception):
    """All 4 download methods failed."""


class LarkMedia:
    """Media download (4-method fallback) + upload (single + multipart)."""

    def __init__(self, auth: LarkAuth) -> None:
        self.auth = auth
        self._log = logger.bind(component="LarkMedia")

    # ============================================================
    # Download — 4-method fallback
    # ============================================================

    async def download(self, file_token: str) -> bytes:
        """Download media bytes. Try 4 methods in order, return first success."""
        # Method 1: official drive media download
        try:
            r = await self.auth.get_raw(
                f"/drive/v1/medias/{file_token}/download",
            )
            if r.status_code == 200 and len(r.content) > 0:
                self._log.info("media_download_ok", file_token=file_token,
                               method="drive_medias", size=len(r.content))
                return r.content
        except httpx.HTTPError as e:
            self._log.warning("media_download_method1_fail", file_token=file_token, err=str(e))

        # Method 2: im/v1/images (alt endpoint cho image)
        try:
            r = await self.auth.get_raw(f"/im/v1/images/{file_token}")
            if r.status_code == 200 and len(r.content) > 0:
                self._log.info("media_download_ok", file_token=file_token,
                               method="im_images", size=len(r.content))
                return r.content
        except httpx.HTTPError as e:
            self._log.warning("media_download_method2_fail", file_token=file_token, err=str(e))

        # Method 3: drive file download (cho file/video block, không phải media)
        try:
            r = await self.auth.get_raw(
                f"/drive/v1/files/{file_token}/download",
            )
            if r.status_code == 200 and len(r.content) > 0:
                self._log.info("media_download_ok", file_token=file_token,
                               method="drive_files", size=len(r.content))
                return r.content
        except httpx.HTTPError as e:
            self._log.warning("media_download_method3_fail", file_token=file_token, err=str(e))

        # Method 4: drive media batch_get_tmp_download_url (signed URL)
        try:
            sign_resp = await self.auth.get(
                "/drive/v1/medias/batch_get_tmp_download_url",
                params={"file_tokens": file_token},
            )
            urls = sign_resp.get("data", {}).get("tmp_download_urls", [])
            if urls:
                signed = urls[0].get("tmp_download_url")
                if signed:
                    async with httpx.AsyncClient(timeout=60.0) as anon:
                        rr = await anon.get(signed)
                        if rr.status_code == 200 and len(rr.content) > 0:
                            self._log.info("media_download_ok", file_token=file_token,
                                           method="tmp_url", size=len(rr.content))
                            return rr.content
        except (httpx.HTTPError, KeyError, IndexError) as e:
            self._log.warning("media_download_method4_fail", file_token=file_token, err=str(e))

        raise MediaDownloadError(f"all 4 methods failed for file_token={file_token}")

    # ============================================================
    # Upload
    # ============================================================

    async def upload(
        self,
        *,
        data: bytes,
        parent_node: str,
        parent_type: str = "docx_image",
        file_name: str | None = None,
    ) -> str:
        """Upload bytes lên Lark drive, return file_token mới.

        - parent_type: "docx_image" cho image block, "docx_file" cho file block
        - parent_node: ID của block placeholder (image/file block đã tạo)
        """
        size = len(data)
        if file_name is None:
            ext = _detect_ext(data[:16])
            file_name = f"upload_{parent_node}.{ext}"

        if size <= SINGLE_UPLOAD_THRESHOLD:
            return await self._upload_single(
                data=data, parent_node=parent_node,
                parent_type=parent_type, file_name=file_name,
            )
        return await self._upload_multipart(
            data=data, parent_node=parent_node,
            parent_type=parent_type, file_name=file_name,
        )

    async def _upload_single(
        self,
        *,
        data: bytes,
        parent_node: str,
        parent_type: str,
        file_name: str,
    ) -> str:
        size = len(data)
        result = await self.auth.post_form(
            "/drive/v1/medias/upload_all",
            data={
                "file_name": file_name,
                "parent_type": parent_type,
                "parent_node": parent_node,
                "size": str(size),
            },
            files={"file": (file_name, data, "application/octet-stream")},
        )
        if result.get("code") != 0:
            from waytoagi.lark.auth import LarkAPIError  # local to avoid cycle
            raise LarkAPIError(
                result.get("code", -1), result.get("msg", ""), "upload_all",
            )
        token: str = result["data"]["file_token"]
        self._log.info("media_upload_ok", file_token=token, method="single", size=size)
        return token

    async def _upload_multipart(
        self,
        *,
        data: bytes,
        parent_node: str,
        parent_type: str,
        file_name: str,
    ) -> str:
        from waytoagi.lark.auth import LarkAPIError

        size = len(data)
        # 1. prepare
        prep = await self.auth.post(
            "/drive/v1/medias/upload_prepare",
            json_body={
                "file_name": file_name,
                "parent_type": parent_type,
                "parent_node": parent_node,
                "size": size,
            },
        )
        if prep.get("code") != 0:
            raise LarkAPIError(prep.get("code", -1), prep.get("msg", ""), "upload_prepare")
        upload_id: str = prep["data"]["upload_id"]
        block_size: int = prep["data"].get("block_size", CHUNK_SIZE)
        block_num: int = prep["data"].get("block_num", -(-size // block_size))

        # 2. parts
        for seq in range(block_num):
            start = seq * block_size
            chunk = data[start:start + block_size]
            r = await self.auth.post_form(
                "/drive/v1/medias/upload_part",
                data={
                    "upload_id": upload_id,
                    "seq": str(seq),
                    "size": str(len(chunk)),
                },
                files={"file": (file_name, chunk, "application/octet-stream")},
            )
            if r.get("code") != 0:
                raise LarkAPIError(r.get("code", -1), r.get("msg", ""), "upload_part")
            self._log.debug("media_upload_part", seq=seq, total=block_num, size=len(chunk))

        # 3. finish
        fin = await self.auth.post(
            "/drive/v1/medias/upload_finish",
            json_body={"upload_id": upload_id, "block_num": block_num},
        )
        if fin.get("code") != 0:
            raise LarkAPIError(fin.get("code", -1), fin.get("msg", ""), "upload_finish")
        token: str = fin["data"]["file_token"]
        self._log.info("media_upload_ok", file_token=token, method="multipart", size=size)
        return token
