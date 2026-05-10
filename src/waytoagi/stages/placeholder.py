"""PlaceholderCreator — tạo dst doc rỗng + dst wiki node trên DST tenant.

Mục đích: cung cấp `Mirror Wiki Node Token` SỚM (lúc crawl) thay vì
đợi MIRROR stage. Khi pipeline clone+translate gặp link tới doc khác,
src→dst map đã có sẵn → swap URL inline được.

Strategy:
  1. Idempotent: nếu record đã có `existing_dst_token` → skip.
  2. Tạo qua `wiki.create_node(obj_type=docx, parent_node_token=...)` —
     Lark API tự tạo empty docx + wiki node + trả về cả 2 token.
  3. Retry transient: 99991400 rate-limit, 230001 frequency, 131009 lock.
  4. Permanent fail (perm denied, parent not found) → ghi error,
     KHÔNG halt — caller vẫn process record kế tiếp.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

from waytoagi.lark.auth import LarkAPIError
from waytoagi.models.crawl import CrawlPlanItem, PlaceholderCreateResult

if TYPE_CHECKING:
    from waytoagi.lark.wiki import LarkWiki

logger = structlog.get_logger(__name__)

# Lark codes coi là permanent fail (không retry trong tenacity nữa,
# nhưng cũng không halt — caller tiếp tục)
_PERMANENT_CODES = frozenset({
    131005,  # not found (parent token sai)
    131006,  # permission denied
    1254045,  # field name not found (irrelevant nhưng giữ for completeness)
})


class PlaceholderCreator:
    """Tạo dst placeholder cho 1 src record.

    Args:
        wiki: LarkWiki bound vào DST tenant.
        space_id: DST wiki space_id.
        default_parent_token: dst parent token mặc định cho top-level
            nodes. Sau, TreeOrderStage move về đúng vị trí.
        title_prefix: prefix optional cho title placeholder
            (vd "[chờ dịch] " để user phân biệt). Default empty.
        dst_domain: domain DST tenant (vd "vudanhdu.sg.larksuite.com")
            để build dst_url field cho Lark Base.
    """

    def __init__(
        self,
        *,
        wiki: LarkWiki,
        space_id: str,
        default_parent_token: str,
        dst_domain: str,
        title_prefix: str = "",
    ) -> None:
        self._wiki = wiki
        self._space_id = space_id
        self._default_parent = default_parent_token
        self._dst_domain = dst_domain
        self._title_prefix = title_prefix
        self._log = logger.bind(
            component="PlaceholderCreator", space_id=space_id,
        )

    async def create_for_item(
        self,
        item: CrawlPlanItem,
        *,
        parent_dst_token: str | None = None,
    ) -> PlaceholderCreateResult:
        """Tạo placeholder cho 1 plan item. Idempotent.

        Args:
            item: plan entry từ CrawlStage Phase 1.
            parent_dst_token: dst parent token override. None →
                dùng `default_parent_token`. (TreeOrderStage sẽ move
                về đúng parent sau khi mọi placeholder tạo xong.)

        Returns:
            PlaceholderCreateResult — success / failed / skipped.
        """
        started = time.monotonic()

        # Idempotent: skip nếu đã có dst_token
        if item.existing_dst_token:
            return PlaceholderCreateResult(
                src_node_token=item.src_node_token,
                success=True,
                dst_node_token=item.existing_dst_token,
                dst_url=self._build_url(item.existing_dst_token),
                skipped_existing=True,
                elapsed_seconds=round(time.monotonic() - started, 3),
            )

        # Title cho placeholder — giữ source title (sẽ replace sau translate)
        title = (self._title_prefix + item.title) if item.title else "[empty]"

        parent = parent_dst_token or self._default_parent

        try:
            response = await self._wiki.create_node(
                self._space_id,
                obj_type="docx",
                title=title,
                parent_node_token=parent,
                node_type="origin",
            )
        except LarkAPIError as e:
            self._log.warning(
                "placeholder_create_failed",
                src=item.src_node_token, code=e.code, msg=e.msg,
            )
            return PlaceholderCreateResult(
                src_node_token=item.src_node_token,
                success=False,
                error=f"LarkAPIError({e.code}): {e.msg}",
                elapsed_seconds=round(time.monotonic() - started, 3),
            )
        except Exception as e:  # network / unexpected
            self._log.warning(
                "placeholder_create_unexpected",
                src=item.src_node_token, err=str(e)[:120],
            )
            return PlaceholderCreateResult(
                src_node_token=item.src_node_token,
                success=False,
                error=f"Unexpected: {e!s}"[:200],
                elapsed_seconds=round(time.monotonic() - started, 3),
            )

        # Parse response — Lark API: data.node.{node_token, obj_token, ...}
        data = response.get("data", {})
        node = data.get("node", {})
        dst_token = str(node.get("node_token", ""))
        if not dst_token:
            return PlaceholderCreateResult(
                src_node_token=item.src_node_token,
                success=False,
                error="missing node_token in response",
                elapsed_seconds=round(time.monotonic() - started, 3),
            )

        return PlaceholderCreateResult(
            src_node_token=item.src_node_token,
            success=True,
            dst_node_token=dst_token,
            dst_url=self._build_url(dst_token),
            elapsed_seconds=round(time.monotonic() - started, 3),
        )

    def _build_url(self, dst_token: str) -> str:
        """Build URL chuẩn cho Lark Base hyperlink field."""
        return f"https://{self._dst_domain}/wiki/{dst_token}"
