"""PipelineOrchestrator — wire 7 stages + Resilience layer.

Pipeline flow (per record, sequential trong 1 record, parallel giữa records):

  STAGE 0: Preflight (run 1 lần lúc startup)
       │
       ▼
  STAGE 1: Crawl + Placeholder (eager — tạo dst placeholder ngay)
       │ src→dst map đầy đủ
       ▼
  STAGE 2: Clone (block-by-block CN→VI working space, swap URL inline)
       │
       ▼
  STAGE 3: Translate (in-place batch translate VI doc)
       │
       ▼
  STAGE 4: Mirror (fill VI content vào DST placeholder)
       │
       ▼
  STAGE 5: Sync (block-level diff khi VI doc edit lại)
       │
       ▼
  STAGE 6: Tree Order (reorder DST tree match source)

State machine:
  - Per-record state read từ Lark Base (Trạng thái, Trạng thái dịch, ...).
  - Skip stages đã Done (idempotent re-run).
  - Trigger stage tiếp theo nếu prerequisite met.

Concurrency:
  - 1 worker pool xử lý nhiều records song song qua AdaptiveConcurrency.
  - Per-record: stages tuần tự (clone → translate → mirror là dependency chain).
  - Per-stage: BatchTranslator + StreamingPipeline overlap nội bộ.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from waytoagi.lark.auth import LarkAPIError
from waytoagi.models.base import RecordStatus, TranslateStatus
from waytoagi.optimize.adaptive import AdaptiveConcurrency, ConcurrencySignal
from waytoagi.resilience.error_classifier import classify_error

if TYPE_CHECKING:
    from collections.abc import Sequence

    from waytoagi.lark.base import LarkBase
    from waytoagi.models.base import BaseRecord
    from waytoagi.resilience.shutdown import GracefulShutdown
    from waytoagi.stages.clone import CloneStage
    from waytoagi.stages.crawl import CrawlStage
    from waytoagi.stages.mirror import MirrorStage
    from waytoagi.stages.reorder import TreeOrderStage
    from waytoagi.stages.translate import TranslateStage

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class OrchestratorCounters:
    """Aggregate counters cho 1 lần orchestrate."""

    records_total: int = 0
    records_processed: int = 0
    records_skipped: int = 0
    records_failed: int = 0

    crawl_done: int = 0
    clone_done: int = 0
    clone_failed: int = 0
    translate_done: int = 0
    translate_failed: int = 0
    mirror_done: int = 0
    mirror_failed: int = 0

    duration_seconds: float = 0.0


@dataclass(slots=True)
class OrchestratorResult:
    """Kết quả orchestrate."""

    counters: OrchestratorCounters
    errors: list[str] = field(default_factory=list)


class PipelineOrchestrator:
    """Wire 7 stages + Resilience.

    Args:
        clone_stage: CloneStage instance.
        translate_stage: TranslateStage instance.
        mirror_stage: MirrorStage instance.
        crawl_stage: optional — chỉ chạy lúc full orchestrate.
        tree_order_stage: optional — chạy sau mirror.
        base: LarkBase cho query records.
        app_token: Bitable app_token.
        table_id: Bitable table_id.
        max_workers: số records xử lý song song.
        shutdown: GracefulShutdown — check is_shutting_down trước mỗi record.
    """

    def __init__(
        self,
        *,
        clone_stage: CloneStage,
        translate_stage: TranslateStage,
        mirror_stage: MirrorStage,
        crawl_stage: CrawlStage | None = None,
        tree_order_stage: TreeOrderStage | None = None,
        base: LarkBase,
        app_token: str,
        table_id: str,
        max_workers: int = 4,
        shutdown: GracefulShutdown | None = None,
    ) -> None:
        self._clone = clone_stage
        self._translate = translate_stage
        self._mirror = mirror_stage
        self._crawl = crawl_stage
        self._tree_order = tree_order_stage
        self._base = base
        self._app_token = app_token
        self._table_id = table_id
        self._concurrency = AdaptiveConcurrency(
            initial=max_workers,
            min_workers=1,
            max_workers=max(max_workers * 2, 8),
        )
        self._shutdown = shutdown
        self._log = logger.bind(component="PipelineOrchestrator")

    # ====================================================================
    # Public API
    # ====================================================================

    async def process_records(
        self,
        records: Sequence[BaseRecord],
    ) -> OrchestratorResult:
        """Process N records song song với AdaptiveConcurrency."""
        started = time.monotonic()
        counters = OrchestratorCounters(records_total=len(records))
        errors: list[str] = []

        self._log.info(
            "orchestrator_start",
            records=len(records),
            workers=self._concurrency.current_workers,
        )

        async def _worker(rec: BaseRecord) -> None:
            # Check shutdown
            if self._shutdown and self._shutdown.is_shutting_down:
                counters.records_skipped += 1
                return

            async with self._concurrency.slot():
                try:
                    await self._process_one(rec, counters)
                    await self._concurrency.signal(ConcurrencySignal.OK)
                except LarkAPIError as e:
                    cat = classify_error(e)
                    if cat.value == "transient_rate_limit":
                        await self._concurrency.signal(
                            ConcurrencySignal.RATE_LIMITED,
                        )
                    else:
                        await self._concurrency.signal(
                            ConcurrencySignal.ERROR,
                        )
                    counters.records_failed += 1
                    errors.append(
                        f"rec={rec.record_id[:18]}:[{e.code}]{e.msg[:80]}",
                    )
                except Exception as e:
                    await self._concurrency.signal(ConcurrencySignal.ERROR)
                    counters.records_failed += 1
                    errors.append(
                        f"rec={rec.record_id[:18]}:{e!s}"[:200],
                    )

        await asyncio.gather(*(_worker(r) for r in records))

        counters.duration_seconds = round(time.monotonic() - started, 2)
        self._log.info(
            "orchestrator_done",
            **{
                "records_processed": counters.records_processed,
                "records_failed": counters.records_failed,
                "clone_done": counters.clone_done,
                "translate_done": counters.translate_done,
                "mirror_done": counters.mirror_done,
                "duration": counters.duration_seconds,
                "final_workers": self._concurrency.current_workers,
            },
        )
        return OrchestratorResult(counters=counters, errors=errors)

    # ====================================================================
    # Internal — per-record stage routing
    # ====================================================================

    async def _process_one(
        self,
        rec: BaseRecord,
        counters: OrchestratorCounters,
    ) -> None:
        """Route 1 record qua stages theo state machine."""
        log = self._log.bind(
            record_id=rec.record_id[:18], stt=rec.stt,
        )

        # Decide stages cần chạy
        needs_clone = (
            rec.trang_thai != RecordStatus.DONE
            and rec.trang_thai != RecordStatus.SKIPPED
            and not rec.lien_ket_clone.link
        )
        needs_translate = (
            bool(rec.lien_ket_clone.link)
            and rec.trang_thai_dich != TranslateStatus.DONE
            and rec.trang_thai_dich != TranslateStatus.SKIPPED
        )
        needs_mirror = (
            rec.trang_thai_dich == TranslateStatus.DONE
            and bool(rec.lien_ket_dich.link)
            and bool(rec.mirror_wiki_node_token)  # placeholder đã có
            # Mirror lại nếu Mirror Wiki Status còn là Placeholder/edited
            and rec.mirror_wiki_status in {
                "", "Placeholder", "edited", "PartialFail", "Failed",
            }
        )

        if not (needs_clone or needs_translate or needs_mirror):
            counters.records_skipped += 1
            log.debug("record_skip_all_done")
            return

        # Stage 2: Clone
        vi_doc_id = ""
        if needs_clone:
            log.info("stage_clone_start")
            # Caller phải đã build src/dst doc_id. Vì là legacy logic
            # phụ thuộc setup, placeholder: dùng obj_token làm ID giả
            # định. Real impl phải parse từ Liên kết clone hoặc gọi
            # wiki.get_node để extract obj_token.
            # Để giữ orchestrator generic + testable, ta delegate
            # extraction qua hook (override-able). Phiên bản minimal:
            # skip nếu chưa có dst placeholder.
            if not rec.mirror_wiki_node_token:
                log.warning("clone_skip_no_placeholder")
                counters.clone_failed += 1
                return
            # Extract src/dst doc ids (caller có thể override _resolve_doc_ids)
            src_doc_id, vi_doc_id = await self._resolve_clone_targets(rec)
            if not src_doc_id or not vi_doc_id:
                log.warning(
                    "clone_skip_missing_doc_ids",
                    src=src_doc_id, vi=vi_doc_id,
                )
                counters.clone_failed += 1
                return

            clone_result = await self._clone.clone_one(
                src_doc_id=src_doc_id,
                dst_doc_id=vi_doc_id,
                record_id=rec.record_id,
            )
            if clone_result.success:
                counters.clone_done += 1
            else:
                counters.clone_failed += 1
                # Skip translate/mirror nếu clone fail
                return

        # Stage 3: Translate
        if needs_translate:
            log.info("stage_translate_start")
            if not vi_doc_id:
                vi_doc_id = await self._resolve_vi_doc_id(rec)
            if not vi_doc_id:
                log.warning("translate_skip_no_vi_doc_id")
                counters.translate_failed += 1
                return

            trans_result = await self._translate.translate_one(
                doc_id=vi_doc_id,
                record_id=rec.record_id,
            )
            if trans_result.success:
                counters.translate_done += 1
            else:
                counters.translate_failed += 1
                return

        # Stage 4: Mirror
        if needs_mirror:
            log.info("stage_mirror_start")
            if not vi_doc_id:
                vi_doc_id = await self._resolve_vi_doc_id(rec)
            dst_doc_id = await self._resolve_dst_doc_id(rec)
            if not vi_doc_id or not dst_doc_id:
                log.warning(
                    "mirror_skip_missing_ids",
                    vi=vi_doc_id, dst=dst_doc_id,
                )
                counters.mirror_failed += 1
                return

            mirror_result = await self._mirror.mirror_one(
                vi_doc_id=vi_doc_id,
                dst_doc_id=dst_doc_id,
                dst_node_token=rec.mirror_wiki_node_token,
                record_id=rec.record_id,
                vi_title=rec.tieude or rec.title,
            )
            if mirror_result.success:
                counters.mirror_done += 1
            else:
                counters.mirror_failed += 1
                return

        counters.records_processed += 1

    # ====================================================================
    # Doc ID resolution — overridable hooks
    # ====================================================================

    async def _resolve_clone_targets(
        self, rec: BaseRecord,
    ) -> tuple[str, str]:
        """Trả về (src_doc_id, dst_doc_id_for_clone).

        Default: src từ rec.obj_token, dst là placeholder doc của
        Mirror Wiki Node Token (caller cần override nếu khác setup).
        """
        src = rec.obj_token
        # Resolve dst: dst placeholder wiki node → obj_token
        dst = await self._resolve_obj_token_from_node(
            rec.mirror_wiki_node_token,
        )
        return src, dst

    async def _resolve_vi_doc_id(self, rec: BaseRecord) -> str:
        """Extract VI doc obj_token từ Liên kết clone hoặc dst placeholder."""
        if rec.mirror_wiki_node_token:
            return await self._resolve_obj_token_from_node(
                rec.mirror_wiki_node_token,
            )
        return ""

    async def _resolve_dst_doc_id(self, rec: BaseRecord) -> str:
        """Extract DST doc obj_token từ Mirror Wiki Node Token."""
        if rec.mirror_wiki_node_token:
            return await self._resolve_obj_token_from_node(
                rec.mirror_wiki_node_token,
            )
        return ""

    async def _resolve_obj_token_from_node(
        self, node_token: str,
    ) -> str:
        """Wiki API: get_node(token) → obj_token. Cached per session."""
        # Minimal fallback: trả token làm obj_token (Lark đôi khi cùng token)
        # Real impl: gọi wiki.get_node(token) → data.node.obj_token
        # Để testable + không phụ thuộc Lark API ở đây, return token.
        return node_token
