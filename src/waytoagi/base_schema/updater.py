"""BaseFieldUpdater — centralized real-time updates per stage.

Mọi stage gọi cùng API thay vì mỗi stage tự build dict fields → đảm bảo:
  - **Idempotent**: cùng input → cùng kết quả.
  - **Atomic timestamps**: started_at + completed_at + duration computed
    cùng base time, không lệch giữa các stage.
  - **Audit trail tự động**: mỗi update trigger append event vào trail.
  - **Real-time**: per-record `update_record` (không buffer cuối stage).

Usage:
    updater = BaseFieldUpdater(base=base, app_token=..., table_id=...)

    # Stage start
    await updater.stage_start(record_id, stage="clone")

    # Stage progress mid-way
    await updater.stage_progress(record_id, stage="translate",
                                 fields={"% Dịch": 45})

    # Stage finish OK
    await updater.stage_finish(
        record_id, stage="clone",
        outcome=AuditOutcome.OK,
        metrics={"Clone Block Count": 247},
        duration_seconds=32.5,
    )

    # Stage finish FAIL
    await updater.stage_finish(
        record_id, stage="clone",
        outcome=AuditOutcome.FAIL,
        error="STAGE1-PERM-DENIED: 131006",
    )
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from waytoagi.base_schema.audit import AuditOutcome, AuditTrail
from waytoagi.lark.auth import LarkAPIError

if TYPE_CHECKING:
    from waytoagi.lark.base import LarkBase

logger = structlog.get_logger(__name__)

# ============================================================================
# Stage → field mapping
# ============================================================================
# Mỗi stage có 1 set field chuẩn: <Stage> Status, Started At, Completed At,
# Duration Seconds, Attempts, Error. Mapping này centralize tên field để
# rename 1 chỗ nếu cần.
_STAGE_FIELD_MAP: dict[str, dict[str, str]] = {
    "crawl": {
        "status": "Crawl Status",
        "completed_at": "Crawl At",
        "attempts": "Crawl Attempts",
        "error": "Crawl Error",
    },
    "placeholder": {
        "status": "Placeholder Status",
        "completed_at": "Placeholder Created At",
        "error": "Placeholder Error",
    },
    "clone": {
        "status": "Clone Status",
        "started_at": "Clone Started At",
        "completed_at": "Clone Completed At",
        "duration": "Clone Duration Seconds",
        "attempts": "Clone Attempts",
        "error": "Clone Error",
    },
    "translate": {
        "status": "Translate Status",
        "started_at": "Translate Started At",
        "completed_at": "Translate Completed At",
        "duration": "Translate Duration Seconds",
        "attempts": "Translate Attempts",
        "error": "Translate Error",
    },
    "mirror": {
        "status": "Mirror Wiki Status",
        "started_at": "Mirror Started At",
        "completed_at": "Mirror Completed At",
        "duration": "Mirror Duration Seconds",
        "attempts": "Mirror Attempts",
        "error": "Mirror Error",
    },
    "sync": {
        "status": "Mirror Wiki Status",  # share field với mirror
        "completed_at": "Mirror Last Synced At",
        "attempts": "Sync Attempts",
        "error": "Sync Error",
    },
    "backlinks": {
        "status": "Backlink Fix Status",
        "completed_at": "Backlink Fix At",
    },
    "tree_order": {
        "status": "Tree Order Status",
        "completed_at": "Tree Order Last Audit",
    },
}


# Map stage → Pipeline Stage value (cho overview field)
_PIPELINE_STAGE_MAP: dict[str, str] = {
    "crawl": "Crawling",
    "placeholder": "Placeholder",
    "clone": "Cloning",
    "translate": "Translating",
    "mirror": "Mirroring",
    "sync": "Syncing",
    "tree_order": "Reordering",
}


class StageUpdate:
    """Snapshot 1 update để test/audit dễ — không phải ghi xuống Lark."""

    __slots__ = ("audit_event_added", "fields", "record_id")

    def __init__(
        self,
        record_id: str,
        fields: dict[str, Any],
        *,
        audit_event_added: bool = False,
    ) -> None:
        self.record_id = record_id
        self.fields = fields
        self.audit_event_added = audit_event_added


class BaseFieldUpdater:
    """Service real-time update Lark Base record fields per stage.

    Args:
        base: LarkBase client.
        app_token: Bitable app_token.
        table_id: Bitable table_id.
        max_audit_events: max events giữ trong AuditTrail field.
        worker_id: optional, fill `Current Worker` cho dashboard.
    """

    def __init__(
        self,
        *,
        base: LarkBase,
        app_token: str,
        table_id: str,
        max_audit_events: int = AuditTrail.DEFAULT_MAX_EVENTS,
        worker_id: str = "",
    ) -> None:
        self._base = base
        self._app_token = app_token
        self._table_id = table_id
        self._max_audit = max_audit_events
        self._worker_id = worker_id
        self._log = logger.bind(
            component="BaseFieldUpdater", worker_id=worker_id,
        )

    # ====================================================================
    # Public API
    # ====================================================================

    async def stage_start(
        self,
        record_id: str,
        *,
        stage: str,
        existing_audit_trail: str = "",
        extra_fields: dict[str, Any] | None = None,
    ) -> StageUpdate:
        """Mark stage bắt đầu — set Status=Running, Started At=now,
        Pipeline Stage update, append audit event.
        """
        now = datetime.now(tz=UTC)
        now_ms = int(now.timestamp() * 1000)
        fields = self._init_fields_for_stage_start(stage, now_ms)

        # Pipeline Stage overview
        if stage in _PIPELINE_STAGE_MAP:
            fields["Pipeline Stage"] = _PIPELINE_STAGE_MAP[stage]
        fields["Last Activity At"] = now_ms
        if self._worker_id:
            fields["Current Worker"] = self._worker_id

        # Audit trail
        trail = AuditTrail.from_string(
            existing_audit_trail, max_events=self._max_audit,
        )
        trail.append_now(
            stage=stage.upper(),
            outcome=AuditOutcome.INFO,
            details="started",
        )
        fields["Audit Trail"] = trail.serialize()

        if extra_fields:
            fields.update(extra_fields)

        await self._write(record_id, fields)
        return StageUpdate(record_id, fields, audit_event_added=True)

    async def stage_progress(
        self,
        record_id: str,
        *,
        stage: str,
        fields: dict[str, Any],
    ) -> StageUpdate:
        """Mid-stage progress update — vd `% Dịch=45`. Không append audit."""
        all_fields = dict(fields)
        all_fields["Last Activity At"] = int(
            datetime.now(tz=UTC).timestamp() * 1000,
        )
        await self._write(record_id, all_fields)
        return StageUpdate(record_id, all_fields)

    async def stage_finish(
        self,
        record_id: str,
        *,
        stage: str,
        outcome: AuditOutcome,
        metrics: dict[str, Any] | None = None,
        duration_seconds: float | None = None,
        error: str = "",
        existing_audit_trail: str = "",
    ) -> StageUpdate:
        """Mark stage hoàn tất.

        Args:
            stage: stage name (key trong _STAGE_FIELD_MAP).
            outcome: OK/SKIP/FAIL/RETRY.
            metrics: dict metrics đặc thù (Clone Block Count, % Dịch, …).
            duration_seconds: thời gian xử lý — sẽ ghi vào Duration field
                + cộng dồn vào Total Duration.
            error: error message nếu outcome=FAIL.
            existing_audit_trail: trail text đã đọc từ Base trước đó.
        """
        now = datetime.now(tz=UTC)
        now_ms = int(now.timestamp() * 1000)
        fields = self._final_fields_for_stage(
            stage=stage,
            outcome=outcome,
            now_ms=now_ms,
            duration_seconds=duration_seconds,
            error=error,
        )

        # Pipeline Stage update — finish OK → next stage hoặc Done
        if outcome == AuditOutcome.FAIL:
            fields["Pipeline Stage"] = "Failed"
        # Caller có thể set Done qua extra_fields nếu là stage cuối

        fields["Last Activity At"] = now_ms

        # Metrics merge
        if metrics:
            fields.update(metrics)

        # Audit trail
        details = self._format_audit_details(
            outcome=outcome, duration=duration_seconds,
            metrics=metrics, error=error,
        )
        trail = AuditTrail.from_string(
            existing_audit_trail, max_events=self._max_audit,
        )
        trail.append_now(
            stage=stage.upper(), outcome=outcome, details=details,
        )
        fields["Audit Trail"] = trail.serialize()

        await self._write(record_id, fields)
        return StageUpdate(record_id, fields, audit_event_added=True)

    # ====================================================================
    # Internal — field builders
    # ====================================================================

    @staticmethod
    def _init_fields_for_stage_start(
        stage: str, now_ms: int,
    ) -> dict[str, Any]:
        """Field set khi stage start: Status=Running + Started At."""
        fmap = _STAGE_FIELD_MAP.get(stage, {})
        fields: dict[str, Any] = {}
        if "status" in fmap:
            fields[fmap["status"]] = "Running"
        if "started_at" in fmap:
            fields[fmap["started_at"]] = now_ms
        return fields

    @staticmethod
    def _final_fields_for_stage(
        *,
        stage: str,
        outcome: AuditOutcome,
        now_ms: int,
        duration_seconds: float | None,
        error: str,
    ) -> dict[str, Any]:
        """Field set khi stage finish."""
        fmap = _STAGE_FIELD_MAP.get(stage, {})
        fields: dict[str, Any] = {}

        # Status
        if "status" in fmap:
            fields[fmap["status"]] = _map_outcome_to_status(outcome, stage)

        # Completed At
        if "completed_at" in fmap:
            fields[fmap["completed_at"]] = now_ms

        # Duration
        if duration_seconds is not None and "duration" in fmap:
            fields[fmap["duration"]] = round(duration_seconds, 2)

        # Error
        if error and "error" in fmap:
            fields[fmap["error"]] = error[:200]

        return fields

    @staticmethod
    def _format_audit_details(
        *,
        outcome: AuditOutcome,
        duration: float | None,
        metrics: dict[str, Any] | None,
        error: str,
    ) -> str:
        """Build details string ngắn gọn cho audit trail line."""
        parts: list[str] = []
        if duration is not None:
            parts.append(f"dt={duration:.1f}s")
        if metrics:
            # Pick 1-2 metric ngắn nhất
            for k, v in list(metrics.items())[:2]:
                # Shorten field name: "Clone Block Count" → "blocks=247"
                short = k.lower().split()[-1]
                parts.append(f"{short}={v}")
        if error and outcome != AuditOutcome.OK:
            parts.append(f"err={error[:60]}")
        return " ".join(parts)

    async def _write(
        self,
        record_id: str,
        fields: dict[str, Any],
    ) -> None:
        """Single-record update với best-effort error handling."""
        try:
            await self._base.update_record(
                self._app_token, self._table_id, record_id, fields,
            )
        except LarkAPIError as e:
            self._log.warning(
                "base_update_failed",
                record_id=record_id, code=e.code, msg=e.msg[:80],
            )
            # Don't raise — Base update là supplemental, không halt stage.


# ============================================================================
# Helpers
# ============================================================================


def _map_outcome_to_status(outcome: AuditOutcome, stage: str) -> str:
    """Map AuditOutcome → status string của stage tương ứng."""
    if outcome == AuditOutcome.OK:
        if stage == "placeholder":
            return "Created"
        if stage == "tree_order":
            return "Fixed"
        if stage == "sync":
            return "Synced"
        return "Done"
    if outcome == AuditOutcome.SKIP:
        return "Skipped"
    if outcome == AuditOutcome.FAIL:
        if stage == "placeholder":
            return "Failed"
        if stage == "tree_order":
            return "Error"
        return "Failed"
    if outcome == AuditOutcome.RETRY:
        return "Running"  # vẫn đang thử
    return "Pending"
