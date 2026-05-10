"""MirrorStage — fill VI content vào DST placeholder.

Context: CrawlStage đã eager-create dst placeholder (empty doc + wiki node).
MirrorStage giờ chỉ cần copy content từ VI working doc → DST placeholder doc.

Không phải clone lần 2 (vì clone đã đảm bảo block-level fidelity).
Strategy: dùng `SmartSyncStage` đã có — diff + chỉ patch blocks khác.
DST placeholder ban đầu rỗng → toàn bộ blocks là APPEND.

Subsequent runs: diff sẽ no-op nếu VI không đổi.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from waytoagi.base_schema.audit import AuditOutcome
from waytoagi.stages.sync import SmartSyncStage, SyncOutcome

if TYPE_CHECKING:
    from waytoagi.base_schema.updater import BaseFieldUpdater
    from waytoagi.lark.base import LarkBase
    from waytoagi.lark.document import LarkDocument
    from waytoagi.lark.wiki import LarkWiki

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class MirrorResult:
    """Kết quả mirror 1 record."""

    record_id: str
    src_doc_id: str
    dst_doc_id: str
    success: bool
    blocks_filled: int = 0
    blocks_failed: int = 0
    error: str = ""
    duration_seconds: float = 0.0


class MirrorStage:
    """Fill VI content → DST placeholder. Wrapper trên SmartSyncStage.

    Args:
        vi_doc: LarkDocument bound vào tenant chứa VI working doc.
        dst_doc: LarkDocument bound vào DST tenant.
        dst_wiki: LarkWiki để update DST title sau khi mirror.
        base: LarkBase cho stage_finish updates.
        app_token: Bitable app_token.
        table_id: Bitable table_id.
        dst_space_id: DST wiki space_id.
        updater: BaseFieldUpdater (optional).
    """

    def __init__(
        self,
        *,
        vi_doc: LarkDocument,
        dst_doc: LarkDocument,
        dst_wiki: LarkWiki,
        base: LarkBase,
        app_token: str,
        table_id: str,
        dst_space_id: str,
        updater: BaseFieldUpdater | None = None,
    ) -> None:
        self._sync = SmartSyncStage(
            src_doc=vi_doc,
            dst_doc=dst_doc,
            base=base,
            app_token=app_token,
            table_id=table_id,
        )
        self._dst_wiki = dst_wiki
        self._dst_space = dst_space_id
        self._updater = updater
        self._log = logger.bind(component="MirrorStage")

    # ====================================================================
    # Public API
    # ====================================================================

    async def mirror_one(
        self,
        *,
        vi_doc_id: str,
        dst_doc_id: str,
        dst_node_token: str,
        record_id: str,
        vi_title: str = "",
        existing_audit_trail: str = "",
    ) -> MirrorResult:
        """Mirror 1 record.

        Args:
            vi_doc_id: VI working doc obj_token (đã translate xong).
            dst_doc_id: DST doc obj_token (placeholder hoặc đã có content).
            dst_node_token: DST wiki node token (cho update_title).
            record_id: Lark Base record_id.
            vi_title: VI title — sẽ update wiki node title nếu khác placeholder.

        Returns:
            MirrorResult — counters + status.
        """
        started = time.monotonic()
        log = self._log.bind(vi=vi_doc_id, dst=dst_doc_id)

        if self._updater and record_id:
            await self._updater.stage_start(
                record_id, stage="mirror",
                existing_audit_trail=existing_audit_trail,
            )

        # Phase 1: sync content (block-level diff + patch)
        sync_result = await self._sync.sync_one(
            src_doc_id=vi_doc_id,
            dst_doc_id=dst_doc_id,
            record_id=record_id,
        )

        # Map SyncOutcome → mirror status
        if sync_result.status in (SyncOutcome.DONE, SyncOutcome.NO_OP):
            mirror_outcome = AuditOutcome.OK
        elif sync_result.status == SyncOutcome.PARTIAL:
            mirror_outcome = AuditOutcome.OK  # partial vẫn coi là OK
        else:
            mirror_outcome = AuditOutcome.FAIL

        # Phase 2: update DST wiki node title nếu vi_title khác placeholder
        # (CrawlStage tạo placeholder với title CN gốc; sau translate ta
        # có VI title thật → update)
        if vi_title and dst_node_token:
            await self._update_dst_title(dst_node_token, vi_title, log)

        # Phase 3: finish
        duration = time.monotonic() - started
        if self._updater and record_id:
            await self._updater.stage_finish(
                record_id, stage="mirror",
                outcome=mirror_outcome,
                duration_seconds=duration,
                error=sync_result.error,
            )

        log.info(
            "mirror_done",
            sync_status=sync_result.status,
            patches_ok=sync_result.patches_succeeded,
            patches_fail=sync_result.patches_failed,
            dt_seconds=round(duration, 2),
        )
        return MirrorResult(
            record_id=record_id,
            src_doc_id=vi_doc_id,
            dst_doc_id=dst_doc_id,
            success=mirror_outcome == AuditOutcome.OK,
            blocks_filled=sync_result.patches_succeeded,
            blocks_failed=sync_result.patches_failed,
            error=sync_result.error,
            duration_seconds=round(duration, 2),
        )

    # ====================================================================
    # Internal
    # ====================================================================

    async def _update_dst_title(
        self,
        dst_node_token: str,
        vi_title: str,
        log: structlog.stdlib.BoundLogger,
    ) -> None:
        """Update wiki node title trên DST tenant.

        Lark API: POST /wiki/v2/spaces/{space_id}/nodes/{node_token}/update_title
        Body: {title: "..."}
        """
        try:
            await self._dst_wiki.auth.post(
                f"/wiki/v2/spaces/{self._dst_space}/nodes/"
                f"{dst_node_token}/update_title",
                json_body={"title": vi_title},
            )
        except Exception as e:
            log.warning(
                "dst_title_update_failed",
                err=str(e)[:120],
            )
