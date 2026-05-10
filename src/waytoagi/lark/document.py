"""LarkDocument — async wrapper cho Docx blocks + spreadsheets."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

    from waytoagi.lark.auth import LarkAuth

logger = structlog.get_logger(__name__)


class LarkDocument:
    """Docx CRUD: documents, blocks, spreadsheets."""

    def __init__(self, auth: LarkAuth) -> None:
        self.auth = auth
        self._log = logger.bind(component="LarkDocument")

    # ============================================================
    # Documents
    # ============================================================

    async def create_document(
        self, *, title: str, folder_token: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"title": title}
        if folder_token:
            body["folder_token"] = folder_token
        return await self.auth.post("/docx/v1/documents", json_body=body)

    async def get_document(self, document_id: str) -> dict[str, Any]:
        return await self.auth.get(f"/docx/v1/documents/{document_id}")

    async def get_block(
        self, document_id: str, block_id: str,
    ) -> dict[str, Any]:
        return await self.auth.get(
            f"/docx/v1/documents/{document_id}/blocks/{block_id}",
        )

    # ============================================================
    # Blocks — list/iter
    # ============================================================

    async def list_blocks(
        self,
        document_id: str,
        *,
        page_size: int = 500,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        return await self.auth.get(
            f"/docx/v1/documents/{document_id}/blocks", params=params,
        )

    async def iter_blocks(self, document_id: str) -> AsyncIterator[dict[str, Any]]:
        """Async iterator over ALL blocks (auto-paginate)."""
        page_token: str | None = None
        while True:
            r = await self.list_blocks(document_id, page_token=page_token)
            data = r.get("data", {})
            for item in data.get("items", []):
                yield item
            if not data.get("has_more"):
                return
            page_token = data.get("page_token")

    async def collect_all_blocks(self, document_id: str) -> list[dict[str, Any]]:
        """Convenience: collect tất cả blocks vào list (memory-bound)."""
        return [b async for b in self.iter_blocks(document_id)]

    # ============================================================
    # Blocks — mutate
    # ============================================================

    async def create_children(
        self,
        document_id: str,
        parent_block_id: str,
        children: Sequence[Mapping[str, Any]],
        *,
        index: int = -1,
    ) -> dict[str, Any]:
        """Insert blocks dưới `parent_block_id`.

        - `index=-1` → append cuối
        - `index=k` → insert tại vị trí k của parent.children
        """
        body: dict[str, Any] = {"children": [dict(c) for c in children]}
        if index >= 0:
            body["index"] = index
        return await self.auth.post(
            f"/docx/v1/documents/{document_id}/blocks/{parent_block_id}/children",
            json_body=body,
        )

    async def patch_block(
        self,
        document_id: str,
        block_id: str,
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        """PATCH /blocks/{id} — update block (replace_image, update_text, etc.)."""
        return await self.auth.patch(
            f"/docx/v1/documents/{document_id}/blocks/{block_id}",
            json_body=dict(body),
        )

    async def delete_children(
        self,
        document_id: str,
        parent_block_id: str,
        *,
        start_index: int,
        end_index: int,
    ) -> dict[str, Any]:
        return await self.auth.request(
            "DELETE",
            f"/docx/v1/documents/{document_id}/blocks/{parent_block_id}/children/batch_delete",
            json_body={"start_index": start_index, "end_index": end_index},
        )
