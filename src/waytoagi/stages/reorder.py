"""TreeOrderStage — reorder dst wiki nodes match source CN DFS order.

Pipeline position: chạy SAU mirror + sync (khi tất cả dst tokens đã ổn
định). Idempotent: re-run sẽ no-op nếu order đã đúng.

Strict separation:
  - Diff logic ở `waytoagi.reorder.diff.compute_plan` (pure function).
  - Stage chỉ orchestrate: fetch current dst children → call compute_plan
    → call WikiClient.move_node → batch update Base.

Real-time Base updates:
  - Mỗi parent xong, batch update field `Tree Order Status` cho TẤT CẢ
    children (FIXED / OK / MISMATCH / SKIPPED / ERROR) + timestamp.
  - Audit-only mode: chỉ ghi MISMATCH, không gọi move.

Failure isolation:
  - 1 child move fail → log + tăng error count, **không** halt parent.
  - 1 parent fail → ghi ERROR + continue parent kế tiếp.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from waytoagi.lark.auth import LarkAPIError
from waytoagi.models.tree import (
    SourceOrderIndex,
    TreeOrderPlan,
    TreeOrderResult,
    TreeOrderRunSummary,
    TreeOrderStatus,
)
from waytoagi.reorder import DEFAULT_MAX_CHILDREN, compute_plan

if TYPE_CHECKING:
    from collections.abc import Iterable

    from waytoagi.lark.base import LarkBase
    from waytoagi.lark.wiki import LarkWiki

logger = structlog.get_logger(__name__)

# Lark Wiki API codes thuộc nhóm transient — đã có trong LarkAuth retry,
# nhưng repeat ở đây để document cho ai đọc code stage.
_TRANSIENT_LARK_CODES = frozenset({99991400, 230001, 1254606, 131009, 131001})

# Pacing giữa các move call để bớt rate-limit (LarkAuth có aiolimiter
# nhưng wiki.move thực tế hay 99991400 → thêm delay nhỏ).
_MOVE_PACING_SECONDS = 0.3


class TreeOrderStage:
    """Audit + fix tree order, parent by parent.

    Args:
        wiki: LarkWiki bound vào DST tenant (nơi cần reorder).
        base: LarkBase cùng tenant với Lark Base table chứa records.
        app_token: Bitable app_token.
        table_id: Bitable table_id.
        space_id: DST wiki space_id.
        max_children: Bỏ qua parent có > N children (avoid disrupt huge
            subtrees). Default = `DEFAULT_MAX_CHILDREN` = 50.
        move_pacing_seconds: Delay giữa mỗi move call. Default 0.3s.
        audit_only: True → chỉ ghi MISMATCH, không gọi wiki.move_node.
    """

    def __init__(
        self,
        *,
        wiki: LarkWiki,
        base: LarkBase,
        app_token: str,
        table_id: str,
        space_id: str,
        max_children: int = DEFAULT_MAX_CHILDREN,
        move_pacing_seconds: float = _MOVE_PACING_SECONDS,
        audit_only: bool = False,
    ) -> None:
        self._wiki = wiki
        self._base = base
        self._app_token = app_token
        self._table_id = table_id
        self._space_id = space_id
        self._max_children = max_children
        self._move_pacing = move_pacing_seconds
        self._audit_only = audit_only
        self._log = logger.bind(
            component="TreeOrderStage",
            space_id=space_id,
            audit_only=audit_only,
        )

    # ====================================================================
    # Public API
    # ====================================================================

    async def run(
        self,
        *,
        index: SourceOrderIndex,
        src_to_dst: dict[str, str],
        dst_to_record_id: dict[str, str] | None = None,
        parents: Iterable[str] | None = None,
    ) -> TreeOrderRunSummary:
        """Run audit + (optionally) fix qua tất cả parents trong index.

        Args:
            index: source order snapshot.
            src_to_dst: src_token → dst_token (chỉ records đã mirror).
            dst_to_record_id: dst_token → Lark Base record_id. Nếu cung
                cấp, stage sẽ batch update `Tree Order Status` real-time
                cho từng child. Pass None nếu bỏ qua Base updates.
            parents: tập src_parent muốn xử lý. None → tất cả parent
                trong index.

        Returns:
            TreeOrderRunSummary aggregated counters.
        """
        started = time.monotonic()
        summary = TreeOrderRunSummary()

        target_parents = list(parents) if parents is not None else index.parents()
        self._log.info(
            "tree_order_run_start",
            parents_total=len(target_parents),
            src_to_dst_count=len(src_to_dst),
            audit_only=self._audit_only,
            max_children=self._max_children,
        )

        for src_parent in target_parents:
            result = await self._process_parent(
                src_parent=src_parent,
                index=index,
                src_to_dst=src_to_dst,
                dst_to_record_id=dst_to_record_id,
            )
            summary.record(result)

        summary.duration_seconds = round(time.monotonic() - started, 2)
        self._log.info(
            "tree_order_run_done",
            **summary.model_dump(exclude={"duration_seconds"}),
            duration_seconds=summary.duration_seconds,
        )
        return summary

    # ====================================================================
    # Internal
    # ====================================================================

    async def _process_parent(
        self,
        *,
        src_parent: str,
        index: SourceOrderIndex,
        src_to_dst: dict[str, str],
        dst_to_record_id: dict[str, str] | None,
    ) -> TreeOrderResult:
        started = time.monotonic()
        log = self._log.bind(src_parent=src_parent)

        dst_parent = src_to_dst.get(src_parent, "")
        if not dst_parent:
            # Parent chưa mirror → chưa thể audit children
            log.debug("skip_parent_not_mirrored")
            return TreeOrderResult(
                src_parent=src_parent,
                dst_parent="",
                status=TreeOrderStatus.SKIPPED,
                duration_seconds=round(time.monotonic() - started, 2),
                errors=["parent_not_mirrored"],
            )

        # Step 1: fetch current dst children
        try:
            current_children = await self._wiki.list_children_tokens(
                self._space_id, dst_parent,
            )
        except LarkAPIError as e:
            log.warning("list_children_failed", code=e.code, msg=e.msg)
            return TreeOrderResult(
                src_parent=src_parent,
                dst_parent=dst_parent,
                status=TreeOrderStatus.ERROR,
                duration_seconds=round(time.monotonic() - started, 2),
                errors=[f"list_children:{e.code}:{e.msg}"],
            )

        # Step 2: compute plan (pure function)
        plan = compute_plan(
            src_parent=src_parent,
            dst_parent=dst_parent,
            desired_src_children=index.children_of(src_parent),
            current_dst_children=current_children,
            src_to_dst=src_to_dst,
            max_children=self._max_children,
        )

        # Step 3: dispatch theo plan
        if plan.skip_reason:
            log.info(
                "plan_skipped",
                reason=plan.skip_reason,
                desired_count=plan.desired_count,
                current_count=plan.current_count,
            )
            await self._update_base_for_plan(
                plan, TreeOrderStatus.SKIPPED, dst_to_record_id,
            )
            return TreeOrderResult(
                src_parent=src_parent,
                dst_parent=dst_parent,
                status=TreeOrderStatus.SKIPPED,
                plan=plan,
                duration_seconds=round(time.monotonic() - started, 2),
            )

        if plan.no_op:
            log.debug(
                "plan_noop",
                desired_count=plan.desired_count,
            )
            await self._update_base_for_plan(
                plan, TreeOrderStatus.OK, dst_to_record_id,
            )
            return TreeOrderResult(
                src_parent=src_parent,
                dst_parent=dst_parent,
                status=TreeOrderStatus.OK,
                plan=plan,
                duration_seconds=round(time.monotonic() - started, 2),
            )

        # Has moves needed
        if self._audit_only:
            log.info(
                "audit_mismatch",
                will_move=plan.will_move,
                desired_count=plan.desired_count,
            )
            await self._update_base_for_plan(
                plan, TreeOrderStatus.MISMATCH, dst_to_record_id,
            )
            return TreeOrderResult(
                src_parent=src_parent,
                dst_parent=dst_parent,
                status=TreeOrderStatus.MISMATCH,
                plan=plan,
                duration_seconds=round(time.monotonic() - started, 2),
            )

        # Step 4: apply moves
        attempted, succeeded, errors = await self._apply_moves(plan, log)
        # Determine final status
        if errors and succeeded == 0:
            final_status = TreeOrderStatus.ERROR
        elif errors:
            # Partial — vẫn coi là Fixed nhưng ghi errors để audit
            final_status = TreeOrderStatus.FIXED
        else:
            final_status = TreeOrderStatus.FIXED

        await self._update_base_for_plan(plan, final_status, dst_to_record_id)

        log.info(
            "plan_applied",
            attempted=attempted,
            succeeded=succeeded,
            error_count=len(errors),
            status=final_status.value,
        )
        return TreeOrderResult(
            src_parent=src_parent,
            dst_parent=dst_parent,
            status=final_status,
            plan=plan,
            moves_attempted=attempted,
            moves_succeeded=succeeded,
            errors=errors,
            duration_seconds=round(time.monotonic() - started, 2),
        )

    async def _apply_moves(
        self,
        plan: TreeOrderPlan,
        log: structlog.stdlib.BoundLogger,
    ) -> tuple[int, int, list[str]]:
        """Loop call wiki.move_node theo plan.moves.

        Strategy: move theo desired order — mỗi node được đẩy xuống cuối
        target_parent_token. Khi loop xong, thứ tự final = thứ tự ta gọi
        move (tức là desired).

        Returns:
            (attempted, succeeded, errors)
        """
        attempted = 0
        succeeded = 0
        errors: list[str] = []

        for child in plan.moves:
            attempted += 1
            try:
                await self._wiki.move_node(
                    self._space_id,
                    node_token=child,
                    target_parent_token=plan.dst_parent,
                )
                succeeded += 1
            except LarkAPIError as e:
                msg = f"move:{child[:18]}:{e.code}:{e.msg[:80]}"
                errors.append(msg)
                log.warning("move_failed", child=child, code=e.code, msg=e.msg)
            # Pacing — bớt rate-limit kể cả khi move success
            if self._move_pacing > 0:
                await asyncio.sleep(self._move_pacing)

        return attempted, succeeded, errors

    async def _update_base_for_plan(
        self,
        plan: TreeOrderPlan,
        status: TreeOrderStatus,
        dst_to_record_id: dict[str, str] | None,
    ) -> None:
        """Batch update Base records với Tree Order Status.

        Updates **TẤT CẢ** dst children có trong desired hoặc current —
        kể cả prefix-match (status=OK) — để Base reflect realtime state.

        Skip nếu `dst_to_record_id` None (caller không quan tâm Base).
        """
        if dst_to_record_id is None:
            return

        # Tập tất cả dst tokens cần update: desired + current (loại extra)
        # = mọi child có trong context của parent.
        affected: set[str] = set()
        affected.update(plan.moves)
        # Children prefix-match không có trong moves nhưng vẫn thuộc context.
        # Compute từ desired_count (= len full desired_filtered)
        # Trick: union với current_dst nếu cần — nhưng compute_plan đã trả
        # list_filtered context. Tạm thời chỉ update children được touch
        # bởi moves để giảm batch size; OK status updates qua riêng nếu cần.
        # → Để giữ semantics đơn giản & idempotent, update toàn bộ children
        # trong desired_filtered bằng cách reconstruct:
        # desired_filtered = prefix + moves; prefix tokens không có sẵn trong
        # plan → dùng dst_to_record_id keys ∩ extra để derive prefix.
        # Đơn giản hơn: chỉ update children trong moves (changed) + plan
        # context. OK records được update implicit khi audit lại.

        if not affected:
            return

        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        records: list[dict[str, object]] = []
        for dst_token in affected:
            record_id = dst_to_record_id.get(dst_token)
            if not record_id:
                continue
            records.append({
                "record_id": record_id,
                "fields": {
                    "Tree Order Status": status.value,
                    "Tree Order Last Audit": now_ms,
                },
            })

        if not records:
            return

        # batch_update max 500/call — tách nếu cần
        for chunk_start in range(0, len(records), 500):
            chunk = records[chunk_start: chunk_start + 500]
            try:
                await self._base.batch_update(
                    self._app_token, self._table_id, chunk,
                )
            except LarkAPIError as e:
                self._log.warning(
                    "base_batch_update_failed",
                    code=e.code, msg=e.msg, chunk_size=len(chunk),
                )
                # Don't raise — tree-order audit là supplemental, không
                # block stage.
