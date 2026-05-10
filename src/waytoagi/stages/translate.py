"""TranslateStage — block-level in-place translate cho VI doc.

V1 problem: mỗi block 1 LLM call → doc 5000 blocks = 30-90 phút + fail
rate cao do rate-limit.

V2 solution: pipeline multi-layer:
  1. **Read VI blocks** từ working doc.
  2. **Filter text-bearing blocks có CJK** — skip blocks đã dịch hoặc
     không cần dịch (image/file/board).
  3. **Build BatchItem** cho mỗi block: id=block_id, text=concatenated
     text_run contents.
  4. **BatchTranslator.translate_batch()** — gom 30 blocks/call:
     - Glossary fast-path (Feishu, 小互, 通往AGI之路, ...)
     - TranslationCache hit (SHA-256)
     - LLM call với layered prompts (style guide + few-shot)
     - Quality gate retry (CJK leak detection)
  5. **PATCH update_text_elements** mỗi block bằng VI translation.
  6. **Real-time Base updates** — % Dịch tăng dần, Translate Cache Hit Pct,
     Translate LLM Calls.
  7. **Final**: stage_finish với metrics đầy đủ.

Idempotent: re-run sau commit → blocks không CJK → skip; cache hit → không
gọi LLM.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from waytoagi.base_schema.audit import AuditOutcome
from waytoagi.lark.auth import LarkAPIError
from waytoagi.llm.quality import has_cjk
from waytoagi.optimize.batch_translate import BatchItem

if TYPE_CHECKING:
    from collections.abc import Mapping

    from waytoagi.base_schema.updater import BaseFieldUpdater
    from waytoagi.lark.document import LarkDocument
    from waytoagi.optimize.batch_translate import BatchTranslator
    from waytoagi.stages.clone import (  # cycle-safe via TYPE_CHECKING
        TEXT_FIELD_BY_TYPE,
    )

logger = structlog.get_logger(__name__)


# Re-import constants để runtime usage (TYPE_CHECKING bỏ qua)
from waytoagi.stages.clone import (  # noqa: E402
    TEXT_BLOCK_TYPES,
    TEXT_FIELD_BY_TYPE,
)


@dataclass(slots=True)
class TranslateStats:
    """Counters cho 1 lần translate."""

    blocks_total: int = 0
    blocks_with_cjk: int = 0
    blocks_translated_ok: int = 0
    blocks_translated_fail: int = 0
    blocks_no_cjk_skipped: int = 0
    cache_hits: int = 0
    glossary_hits: int = 0
    llm_calls: int = 0
    llm_avg_batch_size: float = 0.0


@dataclass(slots=True)
class TranslateResult:
    """Kết quả translate 1 doc."""

    doc_id: str
    success: bool
    stats: TranslateStats = field(default_factory=TranslateStats)
    error: str = ""
    duration_seconds: float = 0.0

    @property
    def cache_hit_pct(self) -> float:
        if self.stats.blocks_with_cjk == 0:
            return 0.0
        return 100.0 * self.stats.cache_hits / self.stats.blocks_with_cjk


class TranslateStage:
    """In-place block-level translate cho VI working doc.

    Args:
        doc: LarkDocument bound vào tenant chứa VI doc (working space).
        translator: BatchTranslator — tự gom batch + cache + glossary.
        updater: BaseFieldUpdater (optional) — real-time Base updates.
    """

    def __init__(
        self,
        *,
        doc: LarkDocument,
        translator: BatchTranslator,
        updater: BaseFieldUpdater | None = None,
    ) -> None:
        self._doc = doc
        self._translator = translator
        self._updater = updater
        self._log = logger.bind(component="TranslateStage")

    # ====================================================================
    # Public API
    # ====================================================================

    async def translate_one(
        self,
        *,
        doc_id: str,
        record_id: str = "",
        existing_audit_trail: str = "",
    ) -> TranslateResult:
        """Translate in-place 1 doc."""
        started = time.monotonic()
        log = self._log.bind(doc=doc_id)
        stats = TranslateStats()

        if self._updater and record_id:
            await self._updater.stage_start(
                record_id, stage="translate",
                existing_audit_trail=existing_audit_trail,
            )

        # Phase 1: read all blocks
        try:
            blocks = await self._doc.collect_all_blocks(doc_id)
        except LarkAPIError as e:
            error = f"read:[{e.code}]{e.msg[:80]}"
            log.warning("translate_read_failed", code=e.code, msg=e.msg)
            await self._mark_finish(
                record_id, AuditOutcome.FAIL,
                stats=stats, error=error,
                duration=time.monotonic() - started,
            )
            return TranslateResult(
                doc_id=doc_id, success=False, error=error, stats=stats,
                duration_seconds=round(time.monotonic() - started, 2),
            )

        stats.blocks_total = len(blocks)

        # Phase 2: build batch items cho blocks có CJK
        items, block_meta = self._build_batch_items(blocks, stats)

        if not items:
            # Không có gì cần dịch → mark OK no-op
            log.info("translate_no_op", blocks_total=stats.blocks_total)
            await self._mark_finish(
                record_id, AuditOutcome.OK, stats=stats,
                duration=time.monotonic() - started,
            )
            return TranslateResult(
                doc_id=doc_id, success=True, stats=stats,
                duration_seconds=round(time.monotonic() - started, 2),
            )

        # Phase 3: batch translate
        result = await self._translator.translate_batch(items)
        stats.cache_hits = result.cache_hits
        stats.glossary_hits = result.glossary_hits
        stats.llm_calls = result.llm_calls
        stats.llm_avg_batch_size = result.avg_items_per_call

        # Phase 4: PATCH translated blocks
        for item_id, vi_text in result.translated.items():
            meta = block_meta.get(item_id)
            if meta is None:
                continue
            ok = await self._patch_block_text(
                doc_id, item_id, meta=meta, new_text=vi_text,
            )
            if ok:
                stats.blocks_translated_ok += 1
            else:
                stats.blocks_translated_fail += 1

            # Real-time progress update mỗi 10 blocks
            if (
                self._updater and record_id
                and stats.blocks_translated_ok % 10 == 0
                and stats.blocks_translated_ok > 0
            ):
                pct = (
                    100.0 * stats.blocks_translated_ok
                    / max(stats.blocks_with_cjk, 1)
                )
                await self._updater.stage_progress(
                    record_id, stage="translate",
                    fields={"% Dịch": round(pct, 1)},
                )

        for item_id in result.failed:
            stats.blocks_translated_fail += 1
            log.warning("translate_block_fail", block_id=item_id)

        # Phase 5: finish
        outcome = (
            AuditOutcome.OK
            if stats.blocks_translated_fail < stats.blocks_with_cjk * 0.2
            else AuditOutcome.FAIL
        )
        duration = time.monotonic() - started
        await self._mark_finish(
            record_id, outcome, stats=stats, duration=duration,
        )

        log.info(
            "translate_done",
            **{
                "blocks_total": stats.blocks_total,
                "blocks_with_cjk": stats.blocks_with_cjk,
                "translated_ok": stats.blocks_translated_ok,
                "translated_fail": stats.blocks_translated_fail,
                "cache_hits": stats.cache_hits,
                "llm_calls": stats.llm_calls,
                "dt_seconds": round(duration, 2),
            },
        )
        return TranslateResult(
            doc_id=doc_id,
            success=outcome == AuditOutcome.OK,
            stats=stats,
            duration_seconds=round(duration, 2),
        )

    # ====================================================================
    # Internal
    # ====================================================================

    def _build_batch_items(
        self,
        blocks: list[dict[str, Any]],
        stats: TranslateStats,
    ) -> tuple[list[BatchItem], dict[str, dict[str, Any]]]:
        """Filter blocks có CJK + build BatchItem.

        Returns:
            (items, block_meta) — block_meta map block_id → original block
            data để build PATCH body sau.
        """
        items: list[BatchItem] = []
        block_meta: dict[str, dict[str, Any]] = {}

        for block in blocks:
            block_type = block.get("block_type")
            if not isinstance(block_type, int):
                continue
            if block_type not in TEXT_BLOCK_TYPES:
                continue

            block_id = str(block.get("block_id", ""))
            if not block_id:
                continue

            field_name = TEXT_FIELD_BY_TYPE[block_type]
            content = block.get(field_name, {})
            if not isinstance(content, dict):
                continue

            elements = content.get("elements", [])
            if not isinstance(elements, list):
                continue

            text = _concat_text(elements)
            if not text:
                continue
            if not has_cjk(text):
                stats.blocks_no_cjk_skipped += 1
                continue

            stats.blocks_with_cjk += 1
            items.append(BatchItem(item_id=block_id, text=text))
            block_meta[block_id] = {
                "block_type": block_type,
                "field_name": field_name,
                "elements": elements,
            }

        return items, block_meta

    async def _patch_block_text(
        self,
        doc_id: str,
        block_id: str,
        *,
        meta: Mapping[str, Any],
        new_text: str,
    ) -> bool:
        """PATCH update_text_elements block với new_text.

        Strategy minimal: replace toàn bộ elements bằng 1 text_run với
        new_text + giữ style của text_run đầu tiên (best-effort).
        """
        # Build new elements: 1 text_run với new_text, copy style từ src first
        elements = meta.get("elements", [])
        first_style: dict[str, Any] = {}
        if isinstance(elements, list):
            for el in elements:
                if isinstance(el, dict) and "text_run" in el:
                    tr = el["text_run"]
                    if isinstance(tr, dict):
                        style = tr.get("text_element_style")
                        if isinstance(style, dict):
                            first_style = dict(style)
                            break

        new_text_run: dict[str, Any] = {"content": new_text}
        if first_style:
            new_text_run["text_element_style"] = first_style

        body = {
            "update_text_elements": {
                "elements": [{"text_run": new_text_run}],
            },
        }
        try:
            await self._doc.patch_block(doc_id, block_id, body)
        except LarkAPIError as e:
            self._log.warning(
                "patch_block_text_fail",
                block_id=block_id, code=e.code, msg=e.msg[:80],
            )
            return False
        return True

    async def _mark_finish(
        self,
        record_id: str,
        outcome: AuditOutcome,
        *,
        stats: TranslateStats,
        duration: float,
        error: str = "",
    ) -> None:
        if not self._updater or not record_id:
            return

        cache_hit_pct = 0.0
        if stats.blocks_with_cjk > 0:
            cache_hit_pct = 100.0 * stats.cache_hits / stats.blocks_with_cjk
        translated_pct = 100.0
        if stats.blocks_with_cjk > 0:
            translated_pct = (
                100.0 * stats.blocks_translated_ok / stats.blocks_with_cjk
            )

        await self._updater.stage_finish(
            record_id,
            stage="translate",
            outcome=outcome,
            metrics={
                "Translate Block Count": stats.blocks_translated_ok,
                "% Dịch": round(translated_pct, 1),
                "Translate Cache Hit Pct": round(cache_hit_pct, 1),
                "Translate LLM Calls": stats.llm_calls,
            },
            duration_seconds=duration,
            error=error,
        )


# ============================================================================
# Helpers
# ============================================================================


def _concat_text(elements: list[Any]) -> str:
    """Concat text_run.content từ list elements thành 1 string."""
    parts: list[str] = []
    for el in elements:
        if not isinstance(el, dict):
            continue
        tr = el.get("text_run")
        if isinstance(tr, dict):
            content = tr.get("content", "")
            if isinstance(content, str):
                parts.append(content)
        # mention_doc.title — cũng dịch
        md = el.get("mention_doc")
        if isinstance(md, dict):
            title = md.get("title", "")
            if isinstance(title, str):
                parts.append(title)
    return "".join(parts)
