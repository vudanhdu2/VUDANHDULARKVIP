"""Tests cho `BaseFieldUpdater` — centralized real-time stage updates."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from waytoagi.base_schema.audit import AuditOutcome
from waytoagi.base_schema.updater import BaseFieldUpdater


def _mk_base() -> AsyncMock:
    base = AsyncMock()
    base.update_record = AsyncMock(return_value={"code": 0})
    return base


def _mk_updater(base: AsyncMock, *, worker_id: str = "") -> BaseFieldUpdater:
    return BaseFieldUpdater(
        base=base, app_token="app", table_id="tbl", worker_id=worker_id,
    )


@pytest.mark.unit
class TestStageStart:
    @pytest.mark.asyncio
    async def test_clone_start_sets_status_running(self) -> None:
        base = _mk_base()
        updater = _mk_updater(base)
        result = await updater.stage_start("rec1", stage="clone")
        fields = result.fields
        assert fields["Clone Status"] == "Running"
        assert "Clone Started At" in fields
        assert isinstance(fields["Clone Started At"], int)
        assert fields["Pipeline Stage"] == "Cloning"

    @pytest.mark.asyncio
    async def test_translate_start_sets_pipeline_stage(self) -> None:
        base = _mk_base()
        updater = _mk_updater(base)
        result = await updater.stage_start("rec1", stage="translate")
        assert result.fields["Pipeline Stage"] == "Translating"
        assert result.fields["Translate Status"] == "Running"

    @pytest.mark.asyncio
    async def test_audit_trail_appended(self) -> None:
        base = _mk_base()
        updater = _mk_updater(base)
        result = await updater.stage_start("rec1", stage="clone")
        assert "Audit Trail" in result.fields
        trail_text = result.fields["Audit Trail"]
        assert "[CLONE]" in trail_text
        assert "INFO started" in trail_text

    @pytest.mark.asyncio
    async def test_existing_audit_preserved(self) -> None:
        base = _mk_base()
        updater = _mk_updater(base)
        existing = "2026-01-01T00:00:00Z [CRAWL] OK n=1"
        result = await updater.stage_start(
            "rec1", stage="clone", existing_audit_trail=existing,
        )
        # New trail bao gồm cả old + new event
        trail = result.fields["Audit Trail"]
        assert "[CRAWL] OK" in trail
        assert "[CLONE] INFO" in trail

    @pytest.mark.asyncio
    async def test_worker_id_included(self) -> None:
        base = _mk_base()
        updater = _mk_updater(base, worker_id="worker-42")
        result = await updater.stage_start("rec1", stage="clone")
        assert result.fields["Current Worker"] == "worker-42"

    @pytest.mark.asyncio
    async def test_calls_update_record(self) -> None:
        base = _mk_base()
        updater = _mk_updater(base)
        await updater.stage_start("rec1", stage="clone")
        base.update_record.assert_called_once()
        args = base.update_record.call_args.args
        assert args[0] == "app"
        assert args[1] == "tbl"
        assert args[2] == "rec1"


@pytest.mark.unit
class TestStageFinish:
    @pytest.mark.asyncio
    async def test_clone_finish_ok(self) -> None:
        base = _mk_base()
        updater = _mk_updater(base)
        result = await updater.stage_finish(
            "rec1",
            stage="clone",
            outcome=AuditOutcome.OK,
            metrics={"Clone Block Count": 247},
            duration_seconds=32.5,
        )
        fields = result.fields
        assert fields["Clone Status"] == "Done"
        assert fields["Clone Duration Seconds"] == 32.5
        assert fields["Clone Block Count"] == 247
        assert "Clone Completed At" in fields

    @pytest.mark.asyncio
    async def test_clone_finish_fail_sets_pipeline_failed(self) -> None:
        base = _mk_base()
        updater = _mk_updater(base)
        result = await updater.stage_finish(
            "rec1",
            stage="clone",
            outcome=AuditOutcome.FAIL,
            error="STAGE1-PERM-DENIED: 131006",
        )
        fields = result.fields
        assert fields["Clone Status"] == "Failed"
        assert fields["Pipeline Stage"] == "Failed"
        assert "131006" in fields["Clone Error"]

    @pytest.mark.asyncio
    async def test_placeholder_finish_ok_status_created(self) -> None:
        """Placeholder OK → status='Created' (special case)."""
        base = _mk_base()
        updater = _mk_updater(base)
        result = await updater.stage_finish(
            "rec1", stage="placeholder", outcome=AuditOutcome.OK,
        )
        assert result.fields["Placeholder Status"] == "Created"

    @pytest.mark.asyncio
    async def test_sync_finish_ok_status_synced(self) -> None:
        """Sync OK → status='Synced'."""
        base = _mk_base()
        updater = _mk_updater(base)
        result = await updater.stage_finish(
            "rec1", stage="sync", outcome=AuditOutcome.OK,
        )
        assert result.fields["Mirror Wiki Status"] == "Synced"

    @pytest.mark.asyncio
    async def test_tree_order_finish_ok_status_fixed(self) -> None:
        base = _mk_base()
        updater = _mk_updater(base)
        result = await updater.stage_finish(
            "rec1", stage="tree_order", outcome=AuditOutcome.OK,
        )
        assert result.fields["Tree Order Status"] == "Fixed"

    @pytest.mark.asyncio
    async def test_audit_trail_finish_includes_metrics(self) -> None:
        base = _mk_base()
        updater = _mk_updater(base)
        result = await updater.stage_finish(
            "rec1",
            stage="clone",
            outcome=AuditOutcome.OK,
            metrics={"Clone Block Count": 247},
            duration_seconds=32.5,
        )
        trail = result.fields["Audit Trail"]
        assert "[CLONE] OK" in trail
        assert "dt=32.5s" in trail
        assert "count=247" in trail  # short form

    @pytest.mark.asyncio
    async def test_skip_outcome_marks_skipped(self) -> None:
        base = _mk_base()
        updater = _mk_updater(base)
        result = await updater.stage_finish(
            "rec1", stage="clone", outcome=AuditOutcome.SKIP,
        )
        assert result.fields["Clone Status"] == "Skipped"


@pytest.mark.unit
class TestStageProgress:
    @pytest.mark.asyncio
    async def test_progress_updates_partial_fields(self) -> None:
        base = _mk_base()
        updater = _mk_updater(base)
        result = await updater.stage_progress(
            "rec1", stage="translate", fields={"% Dịch": 45},
        )
        assert result.fields["% Dịch"] == 45
        assert "Last Activity At" in result.fields

    @pytest.mark.asyncio
    async def test_progress_no_audit_event(self) -> None:
        base = _mk_base()
        updater = _mk_updater(base)
        result = await updater.stage_progress(
            "rec1", stage="translate", fields={"% Dịch": 45},
        )
        # Progress KHÔNG ghi audit (chỉ start/finish ghi)
        assert "Audit Trail" not in result.fields


@pytest.mark.unit
class TestUpdaterRobustness:
    @pytest.mark.asyncio
    async def test_base_update_failure_does_not_raise(self) -> None:
        from waytoagi.lark.auth import LarkAPIError

        base = AsyncMock()
        base.update_record = AsyncMock(
            side_effect=LarkAPIError(99991400, "rate limit", "/update"),
        )
        updater = _mk_updater(base)
        # Không raise — base update fail là supplemental
        result = await updater.stage_start("rec1", stage="clone")
        assert result.fields["Clone Status"] == "Running"
