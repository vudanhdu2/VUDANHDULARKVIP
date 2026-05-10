"""LarkWiki — async wrapper cho Wiki space + nodes + tree walk."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from waytoagi.lark.auth import LarkAuth

logger = structlog.get_logger(__name__)


class LarkWiki:
    """Wiki space, nodes, recursive walk."""

    def __init__(self, auth: LarkAuth) -> None:
        self.auth = auth
        self._log = logger.bind(component="LarkWiki")

    # ============================================================
    # Spaces
    # ============================================================

    async def list_spaces(
        self, *, page_size: int = 50, page_token: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        return await self.auth.get("/wiki/v2/spaces", params=params)

    async def get_space(self, space_id: str, *, lang: str = "en") -> dict[str, Any]:
        return await self.auth.get(
            f"/wiki/v2/spaces/{space_id}", params={"lang": lang},
        )

    # ============================================================
    # Nodes
    # ============================================================

    async def list_nodes(
        self,
        space_id: str,
        *,
        parent_node_token: str | None = None,
        page_size: int = 50,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page_size": page_size}
        if parent_node_token:
            params["parent_node_token"] = parent_node_token
        if page_token:
            params["page_token"] = page_token
        return await self.auth.get(
            f"/wiki/v2/spaces/{space_id}/nodes", params=params,
        )

    async def get_node(self, token: str, *, obj_type: str = "wiki") -> dict[str, Any]:
        """Resolve wiki node hoặc doc token → metadata + obj_token."""
        return await self.auth.get(
            "/wiki/v2/spaces/get_node",
            params={"token": token, "obj_type": obj_type},
        )

    async def create_node(
        self,
        space_id: str,
        *,
        obj_type: str = "docx",
        title: str = "",
        parent_node_token: str | None = None,
        node_type: str = "origin",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"obj_type": obj_type, "node_type": node_type}
        if title:
            body["title"] = title
        if parent_node_token:
            body["parent_node_token"] = parent_node_token
        return await self.auth.post(
            f"/wiki/v2/spaces/{space_id}/nodes", json_body=body,
        )

    async def move_doc_to_wiki(
        self,
        space_id: str,
        *,
        obj_token: str,
        obj_type: str,
        parent_wiki_token: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"obj_type": obj_type, "obj_token": obj_token}
        if parent_wiki_token:
            body["parent_wiki_token"] = parent_wiki_token
        return await self.auth.post(
            f"/wiki/v2/spaces/{space_id}/nodes/move_docs_to_wiki",
            json_body=body,
        )

    async def move_node(
        self,
        space_id: str,
        *,
        node_token: str,
        target_parent_token: str,
        target_space_id: str | None = None,
    ) -> dict[str, Any]:
        """Move 1 wiki node sang parent khác (cùng space hoặc khác space).

        Lark behaviour: node được đặt vào **CUỐI** danh sách children của
        target_parent. Để xếp lại order trong cùng parent → gọi move với
        cùng parent token theo desired order, từng node một.

        Idempotent: gọi move khi node đã ở đúng parent vẫn return code=0,
        nhưng node sẽ bị đẩy xuống cuối → caller cần kiểm tra trước.
        """
        body: dict[str, Any] = {
            "target_parent_token": target_parent_token,
            "target_space_id": target_space_id or space_id,
        }
        return await self.auth.post(
            f"/wiki/v2/spaces/{space_id}/nodes/{node_token}/move",
            json_body=body,
        )

    async def iter_children(
        self,
        space_id: str,
        *,
        parent_node_token: str | None = None,
        page_size: int = 50,
    ) -> AsyncIterator[dict[str, Any]]:
        """Async iterator yield từng child theo display order, paginate hết.

        KHÔNG đệ quy — chỉ direct children của parent. Khác với `walk_tree`.
        Dùng cho tree-order audit: chỉ cần biết thứ tự children trực tiếp.
        """
        page_token: str | None = None
        while True:
            r = await self.list_nodes(
                space_id,
                parent_node_token=parent_node_token,
                page_size=page_size,
                page_token=page_token,
            )
            data = r.get("data", {})
            for item in data.get("items", []):
                yield item
            if not data.get("has_more"):
                return
            page_token = data.get("page_token")

    async def list_children_tokens(
        self,
        space_id: str,
        parent_node_token: str | None = None,
    ) -> list[str]:
        """Trả về list[node_token] của children theo display order.

        Convenience wrapper trên `iter_children` — dùng cho tree-order
        audit (chỉ cần token sequence).
        """
        tokens: list[str] = []
        async for item in self.iter_children(
            space_id, parent_node_token=parent_node_token,
        ):
            tok = item.get("node_token")
            if isinstance(tok, str):
                tokens.append(tok)
        return tokens

    # ============================================================
    # Tree walk
    # ============================================================

    async def walk_tree(
        self,
        space_id: str,
        *,
        parent_node_token: str | None = None,
        max_depth: int = 10,
    ) -> AsyncIterator[dict[str, Any]]:
        """Async generator yield từng node trong cây (DFS, depth-first).

        Mỗi node được yield kèm `_depth` (int) để biết level trong cây.
        """
        async for n in self._walk(space_id, parent_node_token, depth=0, max_depth=max_depth):
            yield n

    async def _walk(
        self,
        space_id: str,
        parent: str | None,
        *,
        depth: int,
        max_depth: int,
    ) -> AsyncIterator[dict[str, Any]]:
        if depth > max_depth:
            return
        page_token: str | None = None
        while True:
            r = await self.list_nodes(
                space_id, parent_node_token=parent, page_token=page_token,
            )
            data = r.get("data", {})
            for item in data.get("items", []):
                item["_depth"] = depth
                yield item
                if item.get("has_child"):
                    async for child in self._walk(
                        space_id, item["node_token"],
                        depth=depth + 1, max_depth=max_depth,
                    ):
                        yield child
            if not data.get("has_more"):
                return
            page_token = data.get("page_token")
