"""Tests cho `PersistentQueue` — durable resume sau crash."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from waytoagi.resilience.persistent_queue import (
    OperationStatus,
    PersistentQueue,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "queue.db"


@pytest.mark.unit
class TestEnqueue:
    @pytest.mark.asyncio
    async def test_enqueue_basic(self, db_path: Path) -> None:
        q = PersistentQueue(db_path)
        op_id = await q.enqueue(
            operation_type="lark_update",
            payload={"record_id": "rec1", "fields": {"foo": "bar"}},
        )
        assert op_id  # UUID returned
        op = await q.get(op_id)
        assert op is not None
        assert op.status == OperationStatus.PENDING
        await q.aclose()

    @pytest.mark.asyncio
    async def test_enqueue_with_explicit_id_idempotent(
        self, db_path: Path,
    ) -> None:
        q = PersistentQueue(db_path)
        op_id = "my-fixed-id"
        await q.enqueue(
            operation_type="x", payload={"a": 1}, operation_id=op_id,
        )
        # Re-enqueue cùng id → no-op
        await q.enqueue(
            operation_type="x", payload={"b": 2}, operation_id=op_id,
        )
        # Stats only 1 pending
        stats = await q.stats()
        assert stats.get("pending", 0) == 1
        await q.aclose()


@pytest.mark.unit
class TestDequeue:
    @pytest.mark.asyncio
    async def test_dequeue_pending_marks_processing(
        self, db_path: Path,
    ) -> None:
        q = PersistentQueue(db_path)
        await q.enqueue(operation_type="x", payload={"a": 1})
        ops = await q.dequeue(limit=10)
        assert len(ops) == 1
        # Status updated to processing
        op = await q.get(ops[0].operation_id)
        assert op is not None
        assert op.status == OperationStatus.PROCESSING
        assert op.attempts == 1
        await q.aclose()

    @pytest.mark.asyncio
    async def test_dequeue_filters_by_type(self, db_path: Path) -> None:
        q = PersistentQueue(db_path)
        await q.enqueue(operation_type="alpha", payload={})
        await q.enqueue(operation_type="beta", payload={})
        ops_alpha = await q.dequeue(operation_type="alpha", limit=10)
        assert len(ops_alpha) == 1
        assert ops_alpha[0].operation_type == "alpha"
        await q.aclose()

    @pytest.mark.asyncio
    async def test_dequeue_respects_scheduled_at(self, db_path: Path) -> None:
        q = PersistentQueue(db_path)
        # Defer 10s
        await q.enqueue(
            operation_type="x", payload={}, delay_seconds=10.0,
        )
        ops = await q.dequeue(limit=10)
        # Chưa đến giờ dequeue
        assert len(ops) == 0
        await q.aclose()

    @pytest.mark.asyncio
    async def test_dequeue_recovers_stale_processing(
        self, db_path: Path,
    ) -> None:
        """Op processing > stale_seconds → re-eligible (mô phỏng crash)."""
        q = PersistentQueue(db_path, stale_seconds=0.01)
        op_id = await q.enqueue(operation_type="x", payload={})
        # Dequeue lần 1 → mark processing
        ops = await q.dequeue(limit=10)
        assert ops[0].operation_id == op_id

        # Wait stale window
        await asyncio.sleep(0.02)

        # Dequeue lần 2 → recover stale
        ops_again = await q.dequeue(limit=10)
        assert len(ops_again) == 1
        assert ops_again[0].operation_id == op_id
        # Attempt count tăng
        assert ops_again[0].attempts == 2
        await q.aclose()


@pytest.mark.unit
class TestMarkDone:
    @pytest.mark.asyncio
    async def test_mark_done_updates_status(self, db_path: Path) -> None:
        q = PersistentQueue(db_path)
        op_id = await q.enqueue(operation_type="x", payload={})
        await q.dequeue(limit=10)
        await q.mark_done(op_id)
        op = await q.get(op_id)
        assert op is not None
        assert op.status == OperationStatus.DONE
        assert op.processed_at is not None
        await q.aclose()


@pytest.mark.unit
class TestMarkFailed:
    @pytest.mark.asyncio
    async def test_failed_reschedules_for_retry(self, db_path: Path) -> None:
        q = PersistentQueue(db_path)
        op_id = await q.enqueue(
            operation_type="x", payload={}, max_attempts=3,
        )
        await q.dequeue(limit=10)
        # Mark fail (transient) — reschedule
        await q.mark_failed(
            op_id, error="rate limit", retry_after_seconds=0,
        )
        op = await q.get(op_id)
        assert op is not None
        assert op.status == OperationStatus.PENDING
        assert "rate limit" in op.last_error

    @pytest.mark.asyncio
    async def test_max_attempts_exceeded_marks_failed(
        self, db_path: Path,
    ) -> None:
        q = PersistentQueue(db_path)
        op_id = await q.enqueue(
            operation_type="x", payload={}, max_attempts=2,
        )
        await q.dequeue(limit=10)
        await q.mark_failed(op_id, error="err1")
        await q.dequeue(limit=10)
        await q.mark_failed(op_id, error="err2")
        op = await q.get(op_id)
        # Đạt max_attempts=2 → mark FAILED
        assert op is not None
        assert op.status == OperationStatus.FAILED
        await q.aclose()

    @pytest.mark.asyncio
    async def test_permanent_marks_failed_immediately(
        self, db_path: Path,
    ) -> None:
        q = PersistentQueue(db_path)
        op_id = await q.enqueue(
            operation_type="x", payload={}, max_attempts=10,
        )
        await q.dequeue(limit=10)
        await q.mark_failed(op_id, error="perm denied", permanent=True)
        op = await q.get(op_id)
        assert op is not None
        assert op.status == OperationStatus.FAILED
        await q.aclose()


@pytest.mark.unit
class TestStats:
    @pytest.mark.asyncio
    async def test_stats_aggregates(self, db_path: Path) -> None:
        q = PersistentQueue(db_path)
        await q.enqueue(operation_type="x", payload={})
        await q.enqueue(operation_type="y", payload={})
        stats = await q.stats()
        assert stats.get("pending", 0) == 2
        await q.aclose()


@pytest.mark.unit
class TestCleanupDone:
    @pytest.mark.asyncio
    async def test_cleanup_removes_old_done(self, db_path: Path) -> None:
        q = PersistentQueue(db_path)
        op_id = await q.enqueue(operation_type="x", payload={})
        await q.dequeue(limit=10)
        await q.mark_done(op_id)
        # Force old timestamp
        conn = q._connect()  # type: ignore[attr-defined]
        conn.execute(
            "UPDATE pending_operations SET processed_at = 0 WHERE operation_id=?",
            (op_id,),
        )
        deleted = await q.cleanup_done(older_than_seconds=86400)
        assert deleted == 1
        op = await q.get(op_id)
        assert op is None
        await q.aclose()


@pytest.mark.unit
class TestRoundTripPersistence:
    @pytest.mark.asyncio
    async def test_data_survives_aclose_reopen(self, db_path: Path) -> None:
        """Data persist sau aclose + reopen — simulate restart."""
        q1 = PersistentQueue(db_path)
        op_id = await q1.enqueue(
            operation_type="x", payload={"k": "v"},
        )
        await q1.aclose()

        # New instance same DB → vẫn thấy operation
        q2 = PersistentQueue(db_path)
        op = await q2.get(op_id)
        assert op is not None
        assert op.payload == {"k": "v"}
        await q2.aclose()
