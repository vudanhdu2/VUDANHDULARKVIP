"""PersistentQueue — SQLite-backed durable queue cho recover sau crash.

V1 problem: pipeline crash giữa chừng (mất điện, OOM, kill -9) → các
in-flight Lark Base updates bị mất → records ở state inconsistent.

V2 solution:
  - Mọi operation pending (update_record, batch_update, …) lưu vào SQLite
    queue TRƯỚC khi gọi API.
  - Worker process pull từ queue → execute → mark done.
  - Crash giữa chừng → restart pull lại pending operations.
  - Idempotency key đảm bảo không double-execute.

Schema:
  pending_operations(
      operation_id PRIMARY KEY,  -- caller-provided UUID, idempotency key
      operation_type TEXT,        -- vd "lark_base_update", "audit_event"
      payload_json TEXT,
      status TEXT,                -- pending/processing/done/failed
      attempts INTEGER,
      max_attempts INTEGER,
      last_error TEXT,
      created_at REAL,
      scheduled_at REAL,
      processed_at REAL
  )

Caller pattern:
  1. Build operation → enqueue (atomic)
  2. Worker dequeue + mark processing
  3. Execute API call
  4. mark_done OR mark_failed (with retry scheduled_at)
  5. Crash → next process startup → dequeue lại pending/processing
     stale (> stale_threshold) → re-execute
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, TypeVar

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

T = TypeVar("T")

logger = structlog.get_logger(__name__)

# Operation đang processing > ngưỡng này → coi là stale, re-dequeue
DEFAULT_STALE_PROCESSING_SECONDS = 300.0  # 5 phút


class OperationStatus(StrEnum):
    """Trạng thái 1 operation trong queue."""

    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    """Vĩnh viễn fail — vượt max_attempts hoặc permanent error."""


@dataclass(frozen=True, slots=True)
class PendingOperation:
    """1 operation trong queue."""

    operation_id: str
    operation_type: str
    payload: dict[str, object]
    status: OperationStatus
    attempts: int
    max_attempts: int
    last_error: str
    created_at: float
    scheduled_at: float
    """Earliest time có thể dequeue (epoch). Dùng cho backoff."""

    processed_at: float | None


class PersistentQueue:
    """SQLite-backed durable queue.

    Args:
        db_path: file path SQLite. Tự tạo nếu chưa có.
        stale_seconds: operations PROCESSING quá ngưỡng này coi là stale
            (worker chết) → re-eligible dequeue.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS pending_operations (
        operation_id TEXT PRIMARY KEY,
        operation_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 5,
        last_error TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        scheduled_at REAL NOT NULL,
        processed_at REAL
    );
    CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_operations(status);
    CREATE INDEX IF NOT EXISTS idx_pending_sched
        ON pending_operations(scheduled_at);
    CREATE INDEX IF NOT EXISTS idx_pending_type
        ON pending_operations(operation_type);
    """

    def __init__(
        self,
        db_path: Path,
        *,
        stale_seconds: float = DEFAULT_STALE_PROCESSING_SECONDS,
    ) -> None:
        self.db_path = db_path
        self._stale = stale_seconds
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._log = logger.bind(component="PersistentQueue", db=str(db_path))

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self.db_path),
                isolation_level=None,
                check_same_thread=False,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(self.SCHEMA)
        return self._conn

    async def _run(self, fn: Callable[[], T]) -> T:
        return await asyncio.get_running_loop().run_in_executor(None, fn)

    # ====================================================================
    # Public API
    # ====================================================================

    async def enqueue(
        self,
        *,
        operation_type: str,
        payload: dict[str, object],
        operation_id: str | None = None,
        max_attempts: int = 5,
        delay_seconds: float = 0.0,
    ) -> str:
        """Enqueue 1 operation. Idempotent: cùng operation_id → INSERT OR IGNORE.

        Args:
            operation_type: tên operation (logging/filter).
            payload: dict serializable JSON.
            operation_id: UUID — nếu cùng id đã có, KHÔNG enqueue lại.
                Pass None → tự generate.
            max_attempts: max retry trước khi mark FAILED.
            delay_seconds: defer dequeue thêm N giây.

        Returns:
            operation_id (caller có thể track).
        """
        op_id = operation_id or str(uuid.uuid4())
        now = time.time()
        scheduled = now + delay_seconds
        payload_json = json.dumps(payload, ensure_ascii=False)

        async with self._lock:
            def _ins() -> None:
                self._connect().execute(
                    "INSERT OR IGNORE INTO pending_operations"
                    "(operation_id, operation_type, payload_json, status, "
                    "max_attempts, created_at, scheduled_at) "
                    "VALUES(?, ?, ?, 'pending', ?, ?, ?)",
                    (op_id, operation_type, payload_json,
                     max_attempts, now, scheduled),
                )
            await self._run(_ins)
        return op_id

    async def dequeue(
        self,
        *,
        operation_type: str | None = None,
        limit: int = 10,
    ) -> list[PendingOperation]:
        """Pull tối đa `limit` operations eligible.

        Eligible:
          - status = pending AND scheduled_at <= now, OR
          - status = processing AND age > stale_seconds (recover crash).

        Atomic mark processing để worker khác không pickup cùng op.
        """
        now = time.time()
        stale_cutoff = now - self._stale

        async with self._lock:
            def _q() -> list[PendingOperation]:
                conn = self._connect()
                conn.execute("BEGIN IMMEDIATE")
                try:
                    type_filter = ""
                    params: list[object] = [now, stale_cutoff]
                    if operation_type:
                        type_filter = " AND operation_type = ?"
                        params.append(operation_type)
                    params.append(limit)

                    cur = conn.execute(
                        "SELECT operation_id, operation_type, payload_json, "
                        "status, attempts, max_attempts, last_error, "
                        "created_at, scheduled_at, processed_at "
                        "FROM pending_operations WHERE "
                        "((status = 'pending' AND scheduled_at <= ?) "
                        " OR (status = 'processing' AND created_at < ?))"
                        + type_filter
                        + " ORDER BY scheduled_at LIMIT ?",
                        params,
                    )
                    rows = cur.fetchall()

                    # Mark processing atomic + bump attempts
                    if rows:
                        ids = [r[0] for r in rows]
                        placeholders = ",".join("?" * len(ids))
                        conn.execute(
                            f"UPDATE pending_operations SET status='processing', "
                            f"attempts = attempts + 1 "
                            f"WHERE operation_id IN ({placeholders})",
                            ids,
                        )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

                # Returned ops phải reflect bumped attempts + status='processing'
                # (vì SELECT trước UPDATE → row tuple có giá trị cũ)
                return [
                    _row_to_op(
                        r,
                        override_attempts=_to_int(r[4]) + 1,
                        override_status="processing",
                    )
                    for r in rows
                ]
            return await self._run(_q)

    async def mark_done(self, operation_id: str) -> None:
        """Mark op DONE — không retry nữa."""
        now = time.time()
        async with self._lock:
            def _upd() -> None:
                self._connect().execute(
                    "UPDATE pending_operations SET status='done', "
                    "processed_at=? WHERE operation_id=?",
                    (now, operation_id),
                )
            await self._run(_upd)

    async def mark_failed(
        self,
        operation_id: str,
        *,
        error: str,
        retry_after_seconds: float = 0.0,
        permanent: bool = False,
    ) -> None:
        """Mark op fail. Nếu `permanent` hoặc đạt max_attempts → FAILED.
        Else → re-schedule sang pending sau `retry_after_seconds`.
        """
        now = time.time()
        scheduled = now + retry_after_seconds
        truncated_err = error[:500]

        async with self._lock:
            def _upd() -> None:
                conn = self._connect()
                if permanent:
                    conn.execute(
                        "UPDATE pending_operations SET status='failed', "
                        "last_error=?, processed_at=? WHERE operation_id=?",
                        (truncated_err, now, operation_id),
                    )
                    return

                # Check max_attempts
                cur = conn.execute(
                    "SELECT attempts, max_attempts FROM pending_operations "
                    "WHERE operation_id=?",
                    (operation_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return
                attempts, max_attempts = int(row[0]), int(row[1])
                if attempts >= max_attempts:
                    conn.execute(
                        "UPDATE pending_operations SET status='failed', "
                        "last_error=?, processed_at=? WHERE operation_id=?",
                        (truncated_err, now, operation_id),
                    )
                else:
                    conn.execute(
                        "UPDATE pending_operations SET status='pending', "
                        "last_error=?, scheduled_at=? WHERE operation_id=?",
                        (truncated_err, scheduled, operation_id),
                    )
            await self._run(_upd)

    async def stats(self) -> dict[str, int]:
        """Counter per status — cho monitoring."""
        async with self._lock:
            def _q() -> dict[str, int]:
                cur = self._connect().execute(
                    "SELECT status, COUNT(*) FROM pending_operations "
                    "GROUP BY status",
                )
                return {str(r[0]): int(r[1]) for r in cur.fetchall()}
            return await self._run(_q)

    async def get(self, operation_id: str) -> PendingOperation | None:
        """Lookup 1 op by id (debug/audit)."""
        async with self._lock:
            def _q() -> PendingOperation | None:
                cur = self._connect().execute(
                    "SELECT operation_id, operation_type, payload_json, "
                    "status, attempts, max_attempts, last_error, "
                    "created_at, scheduled_at, processed_at "
                    "FROM pending_operations WHERE operation_id=?",
                    (operation_id,),
                )
                row = cur.fetchone()
                return _row_to_op(row) if row else None
            return await self._run(_q)

    async def cleanup_done(self, *, older_than_seconds: float = 7 * 86400) -> int:
        """Xoá operations DONE cũ — return số rows deleted."""
        cutoff = time.time() - older_than_seconds
        async with self._lock:
            def _del() -> int:
                cur = self._connect().execute(
                    "DELETE FROM pending_operations WHERE status='done' "
                    "AND processed_at < ?",
                    (cutoff,),
                )
                return int(cur.rowcount)
            return await self._run(_del)

    async def aclose(self) -> None:
        async with self._lock:
            if self._conn is not None:
                with suppress(sqlite3.Error):
                    self._conn.close()
                self._conn = None


def _to_int(v: object) -> int:
    """Helper safe int conversion từ SQLite row value."""
    if isinstance(v, (int, float, str)):
        return int(v)
    msg = f"Cannot convert {type(v).__name__} to int"
    raise TypeError(msg)


def _to_float(v: object) -> float:
    """Helper safe float conversion từ SQLite row value."""
    if isinstance(v, (int, float, str)):
        return float(v)
    msg = f"Cannot convert {type(v).__name__} to float"
    raise TypeError(msg)


def _row_to_op(
    row: tuple[object, ...],
    *,
    override_attempts: int | None = None,
    override_status: str | None = None,
) -> PendingOperation:
    """Convert SQLite row → PendingOperation.

    Optional override để reflect post-UPDATE state khi caller đã bump
    attempts/status trong cùng transaction.
    """
    status_str = override_status if override_status is not None else str(row[3])
    attempts = (
        override_attempts if override_attempts is not None
        else _to_int(row[4])
    )
    return PendingOperation(
        operation_id=str(row[0]),
        operation_type=str(row[1]),
        payload=json.loads(str(row[2])),
        status=OperationStatus(status_str),
        attempts=attempts,
        max_attempts=_to_int(row[5]),
        last_error=str(row[6]),
        created_at=_to_float(row[7]),
        scheduled_at=_to_float(row[8]),
        processed_at=_to_float(row[9]) if row[9] is not None else None,
    )
