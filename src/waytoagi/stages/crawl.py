"""CrawlStage — eager-placeholder crawl: walk source DFS + tạo dst
placeholder cho mọi NEW record.

V1 problem: backlink chỉ resolve được sau MIRROR stage → khi clone
doc A có link tới doc B chưa mirror, URL vẫn trỏ source CN. Phải
chạy `fix_backlinks.py` riêng để catch-up.

V2 solution: ngay tại CRAWL stage, mỗi NEW record được tạo placeholder
dst (empty doc + wiki node) → Mirror Wiki Node Token CÓ SẴN cho stages
sau. CLONE stage swap URL CN→DST INLINE → MIRROR stage chỉ fill content.

3 phase:
  1. **Detect**: walk source DFS + load existing Base records → build
     `CrawlPlan` (NEW/EDITED/RENAMED/UNCHANGED/DELETED). Phase pure
     theo nghĩa không tạo placeholder, chỉ đọc + diff.
  2. **Apply placeholders**: parallel call `PlaceholderCreator` cho
     items NEW + chưa có dst_token. Bounded bằng `AdaptiveConcurrency`
     để tự scale theo rate-limit signal.
  3. **Apply Base writes**: real-time per-record update Base với:
       - NEW: batch_create với fields đầy đủ (Mirror Wiki Node Token,
         Liên kết wiki dịch mới, Mirror Wiki Status=Placeholder)
       - EDITED: reset Trạng thái=Pending, Trạng thái dịch=Pending
       - RENAMED: update Title only
       - DELETED: set Source Status=Deleted
       - UNCHANGED: chỉ touch Last Seen At

Real-time updates đảm bảo crash giữa chừng vẫn resume được — record
đã có dst_token vẫn hợp lệ cho stage sau.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from waytoagi.lark.auth import LarkAPIError
from waytoagi.models.crawl import (
    CrawlEvent,
    CrawlPlan,
    CrawlPlanItem,
    CrawlResult,
    PlaceholderStatus,
)
from waytoagi.models.tree import SourceOrderIndex
from waytoagi.optimize.adaptive import AdaptiveConcurrency, ConcurrencySignal

if TYPE_CHECKING:
    from collections.abc import Iterable

    from waytoagi.lark.base import LarkBase
    from waytoagi.lark.wiki import LarkWiki
    from waytoagi.models.base import BaseRecord
    from waytoagi.stages.placeholder import PlaceholderCreator

logger = structlog.get_logger(__name__)

# Edit time threshold (ms) — chênh lệch <60s coi như không đổi để
# tránh false-positive khi Lark API trả timestamp jitter
_EDIT_TIME_TOLERANCE_MS = 60_000


class CrawlStage:
    """Eager-placeholder crawl stage.

    Args:
        src_wiki: LarkWiki bound vào source CN tenant.
        src_space_id: source CN wiki space_id.
        base: LarkBase bound vào DST tenant (nơi chứa Lark Base table).
        app_token: Bitable app_token.
        table_id: Bitable table_id.
        placeholder: PlaceholderCreator (đã bind DST wiki + space).
        max_depth: tree walk depth limit.
        concurrency: AdaptiveConcurrency cho phase 2 (placeholder).
            None → tạo default `initial=2, max=8`.
    """

    def __init__(
        self,
        *,
        src_wiki: LarkWiki,
        src_space_id: str,
        base: LarkBase,
        app_token: str,
        table_id: str,
        placeholder: PlaceholderCreator,
        max_depth: int = 12,
        concurrency: AdaptiveConcurrency | None = None,
    ) -> None:
        self._src_wiki = src_wiki
        self._src_space = src_space_id
        self._base = base
        self._app_token = app_token
        self._table_id = table_id
        self._placeholder = placeholder
        self._max_depth = max_depth
        self._concurrency = concurrency or AdaptiveConcurrency(
            initial=2, min_workers=1, max_workers=8,
        )
        self._log = logger.bind(
            component="CrawlStage", src_space=src_space_id,
        )

    # ====================================================================
    # Public API
    # ====================================================================

    async def run(
        self,
        *,
        existing_records: Iterable[BaseRecord],
    ) -> tuple[CrawlResult, SourceOrderIndex]:
        """Run 3 phase. Trả về (result, source_order_index).

        `source_order_index` capture luôn DFS order để TreeOrderStage
        dùng sau (tận dụng walk single-pass).
        """
        result = CrawlResult()
        started = time.monotonic()

        # Phase 1: detect
        existing_map = {r.node_token: r for r in existing_records if r.node_token}
        plan, src_order = await self._detect_phase(
            existing_map=existing_map,
            result=result,
        )

        # Phase 2: apply placeholders
        await self._apply_placeholders_phase(plan=plan, result=result)

        # Phase 3: apply Base writes
        await self._apply_base_writes_phase(
            plan=plan, existing_map=existing_map, result=result,
        )

        result.duration_seconds = round(time.monotonic() - started, 2)
        self._log.info(
            "crawl_run_done",
            **result.model_dump(exclude={"errors", "duration_seconds"}),
            duration_seconds=result.duration_seconds,
            error_count=len(result.errors),
        )
        return result, src_order

    # ====================================================================
    # Phase 1 — Detect
    # ====================================================================

    async def _detect_phase(
        self,
        *,
        existing_map: dict[str, BaseRecord],
        result: CrawlResult,
    ) -> tuple[CrawlPlan, SourceOrderIndex]:
        """Walk source DFS + diff vs existing records → CrawlPlan."""
        plan = CrawlPlan()
        order_dict: dict[str, list[str]] = {}
        seen_tokens: set[str] = set()

        async for node in self._src_wiki.walk_tree(
            self._src_space, max_depth=self._max_depth,
        ):
            result.nodes_walked += 1
            tok = str(node.get("node_token", ""))
            if not tok:
                continue
            seen_tokens.add(tok)

            parent = str(node.get("parent_node_token", ""))
            order_dict.setdefault(parent, []).append(tok)

            # Build plan item
            existing = existing_map.get(tok)
            event = self._classify(node, existing)
            result.record_event(event)

            obj_edit_ms = _epoch_ms(node.get("obj_edit_time"))

            item = CrawlPlanItem(
                src_node_token=tok,
                src_parent_token=parent,
                src_obj_token=str(node.get("obj_token", "")),
                src_obj_type=str(node.get("obj_type", "")),
                src_node_type=str(node.get("node_type", "")),
                title=str(node.get("title", "") or ""),
                obj_edit_time_ms=obj_edit_ms,
                event=event,
                record_id=existing.record_id if existing else "",
                existing_dst_token=(
                    existing.mirror_wiki_node_token if existing else ""
                ),
            )
            plan.items.append(item)

        # Detect deleted: records trong existing_map nhưng không seen
        for tok, rec in existing_map.items():
            if tok not in seen_tokens:
                plan.deleted_record_ids.append(rec.record_id)
                result.record_event(CrawlEvent.DELETED)

        src_order = SourceOrderIndex(
            order=order_dict,
            captured_at=datetime.now(tz=UTC),
        )
        return plan, src_order

    @staticmethod
    def _classify(
        node: dict[str, object],
        existing: BaseRecord | None,
    ) -> CrawlEvent:
        """Classify event cho 1 source node."""
        if existing is None:
            return CrawlEvent.NEW

        # Title diff → RENAMED (priority cao hơn EDITED vì rename không
        # nhất thiết edit content)
        new_title = str(node.get("title", "") or "")
        if new_title and existing.title and new_title != existing.title:
            return CrawlEvent.RENAMED

        # Edit time diff (với tolerance)
        new_edit_ms = _epoch_ms(node.get("obj_edit_time"))
        old_edit_ms = existing.last_edit_time or 0
        if new_edit_ms and new_edit_ms - old_edit_ms > _EDIT_TIME_TOLERANCE_MS:
            return CrawlEvent.EDITED

        return CrawlEvent.UNCHANGED

    # ====================================================================
    # Phase 2 — Apply placeholders (parallel, adaptive)
    # ====================================================================

    async def _apply_placeholders_phase(
        self,
        *,
        plan: CrawlPlan,
        result: CrawlResult,
    ) -> None:
        """Tạo placeholder cho NEW items song song với AdaptiveConcurrency."""
        targets = plan.needs_placeholder
        if not targets:
            return

        self._log.info(
            "placeholder_phase_start",
            target_count=len(targets),
            initial_workers=self._concurrency.current_workers,
        )

        async def _worker(item: CrawlPlanItem) -> None:
            async with self._concurrency.slot():
                pres = await self._placeholder.create_for_item(item)
            # Feed signal vào concurrency
            if pres.success:
                await self._concurrency.signal(ConcurrencySignal.OK)
                if pres.skipped_existing:
                    result.placeholders_skipped_existing += 1
                else:
                    result.placeholders_created += 1
            else:
                # Heuristic: 99991400 / 230001 → rate-limit signal
                err = pres.error.lower()
                if "99991400" in err or "230001" in err or "131009" in err:
                    await self._concurrency.signal(ConcurrencySignal.RATE_LIMITED)
                else:
                    await self._concurrency.signal(ConcurrencySignal.ERROR)
                result.placeholders_failed += 1
                result.errors.append(
                    f"placeholder:{item.src_node_token[:18]}:{pres.error[:80]}",
                )
            # Mutate item in-place (frozen dataclass → workaround: dùng
            # dict mapping ngoài). Vì frozen, ta lưu kết quả ở dict
            # bên ngoài qua attribute setattr trên CrawlPlan instead.
            # Để giữ frozen, ta route qua plan-level dict:
            self._placeholder_results[item.src_node_token] = pres

        self._placeholder_results: dict[str, object] = {}
        await asyncio.gather(*(_worker(it) for it in targets))

    # ====================================================================
    # Phase 3 — Apply Base writes
    # ====================================================================

    async def _apply_base_writes_phase(
        self,
        *,
        plan: CrawlPlan,
        existing_map: dict[str, BaseRecord],
        result: CrawlResult,
    ) -> None:
        """Real-time write Base records — NEW batch_create, others update."""
        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)

        # NEW records → batch_create
        new_records: list[dict[str, object]] = []
        for item in plan.items:
            if item.event != CrawlEvent.NEW:
                continue
            pres = getattr(self, "_placeholder_results", {}).get(
                item.src_node_token,
            )
            fields = self._build_new_fields(item, pres, now_ms)
            new_records.append({"fields": fields})

        if new_records:
            try:
                await self._batch_create(new_records)
                result.base_creates += len(new_records)
            except LarkAPIError as e:
                result.errors.append(f"batch_create:{e.code}:{e.msg[:80]}")
                result.base_failures += len(new_records)

        # Updates: EDITED, RENAMED, UNCHANGED — update_record per record
        # (batch_update cũng OK nhưng dễ partial fail; per-record an toàn hơn)
        update_payload: list[dict[str, object]] = []
        for item in plan.items:
            existing = existing_map.get(item.src_node_token)
            if not existing:
                continue
            update = self._build_update_fields(item, existing, now_ms)
            if update:
                update_payload.append({
                    "record_id": existing.record_id,
                    "fields": update,
                })

        # DELETED: set Source Status
        for record_id in plan.deleted_record_ids:
            update_payload.append({
                "record_id": record_id,
                "fields": {
                    "Source Status": "Deleted",
                    "Change Status": "deleted",
                    "Last Seen At": now_ms,
                },
            })

        # Batch update — chunk theo Lark cap 500
        await self._batch_update_chunked(update_payload, result)

    def _build_new_fields(
        self,
        item: CrawlPlanItem,
        pres: object | None,
        now_ms: int,
    ) -> dict[str, object]:
        """Fields cho NEW record — bao gồm placeholder dst nếu tạo OK."""
        fields: dict[str, object] = {
            "Title": item.title,
            "Node Token": item.src_node_token,
            "Parent Node Token": item.src_parent_token,
            "Obj Token": item.src_obj_token,
            "Obj Type": item.src_obj_type,
            "Node Type": item.src_node_type,
            "Trạng thái": "Pending",
            "Trạng thái dịch": "Pending",
            "Source Status": "Present",
            "Change Status": "",
            "Crawled At": now_ms,
            "Last Seen At": now_ms,
            "Last Edit Time": item.obj_edit_time_ms,
        }
        # Bind placeholder nếu phase 2 đã tạo
        if pres and getattr(pres, "success", False):
            dst_token = getattr(pres, "dst_node_token", "")
            dst_url = getattr(pres, "dst_url", "")
            if dst_token:
                fields["Mirror Wiki Node Token"] = dst_token
                fields["Mirror Wiki Status"] = PlaceholderStatus.CREATED.value
                if dst_url:
                    fields["Liên kết wiki dịch mới"] = {
                        "link": dst_url, "text": dst_url,
                    }
        elif pres and not getattr(pres, "success", False):
            fields["Mirror Wiki Status"] = PlaceholderStatus.FAILED.value
        return fields

    def _build_update_fields(
        self,
        item: CrawlPlanItem,
        existing: BaseRecord,
        now_ms: int,
    ) -> dict[str, object] | None:
        """Fields cho UPDATE — return None nếu không có gì đổi (tiết kiệm API)."""
        update: dict[str, object] = {"Last Seen At": now_ms}

        if item.event == CrawlEvent.UNCHANGED:
            return update  # chỉ touch

        if item.event == CrawlEvent.RENAMED:
            update["Title"] = item.title
            update["Change Status"] = "renamed"
            return update

        if item.event == CrawlEvent.EDITED:
            update["Last Edit Time"] = item.obj_edit_time_ms
            update["Change Status"] = "edited"
            update["Trạng thái"] = "Pending"
            update["Trạng thái dịch"] = "Pending"
            # Clear stale links — pipeline sẽ re-clone
            update["Liên kết clone"] = ""
            update["Liên kết dịch"] = ""
            update["Lỗi"] = ""

            # NẾU record CHƯA có placeholder (legacy data), tạo nhân
            # cơ hội này — nhưng để tránh phình logic, leave tới crawl
            # sau khi user mark Mirror Wiki Node Token=empty manually.
            return update

        return None  # NEW + DELETED đã handle ở chỗ khác

    async def _batch_create(
        self,
        records: list[dict[str, object]],
    ) -> None:
        """Wrap batch_create với chunk 500."""
        for i in range(0, len(records), 500):
            chunk = records[i: i + 500]
            await self._base.batch_create(self._app_token, self._table_id, chunk)

    async def _batch_update_chunked(
        self,
        records: list[dict[str, object]],
        result: CrawlResult,
    ) -> None:
        """Wrap batch_update với chunk 500 + best-effort error handling."""
        if not records:
            return
        for i in range(0, len(records), 500):
            chunk = records[i: i + 500]
            try:
                await self._base.batch_update(
                    self._app_token, self._table_id, chunk,
                )
                result.base_updates += len(chunk)
            except LarkAPIError as e:
                result.errors.append(
                    f"batch_update:{e.code}:{e.msg[:80]}:n={len(chunk)}",
                )
                result.base_failures += len(chunk)


def _epoch_ms(value: object) -> int:
    """Convert Lark API timestamp (sec or ms) sang ms."""
    if value is None:
        return 0
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    if v <= 0:
        return 0
    # Heuristic: nếu < 10^11 thì là seconds
    if v < 1e11:
        v *= 1000
    return int(v)
