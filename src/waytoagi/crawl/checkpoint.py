"""SQLite-backed checkpoint cho CrawlStage resume.

V1 problem: crawl 12k+ records, crash giữa chừng → restart từ đầu.

V2 solution:
  - Save checkpoint sau mỗi N nodes (default 200) — atomic transaction.
  - Mỗi run có `run_id` (UUID); checkpoint sống cho đến khi complete
    hoặc invalidate (TTL 24h hoặc full re-crawl manual).
  - Resume: load latest non-complete checkpoint → skip nodes đã walked
    → tiếp từ chỗ dừng.
  - Idempotent: write same node 2 lần không hỏng (INSERT OR IGNORE).

Schema:
  runs(run_id, started_at, completed_at, status, src_space, last_walked)
  walked_tokens(run_id, src_token, walked_at)

Concurrency: 1 process crawl tại 1 thời điểm. SQLite WAL mode đủ.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, TypeVar

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

T = TypeVar("T")

logger = structlog.get_logger(__name__)

# TTL cho checkpoint chưa complete — sau ngưỡng này coi là dead, full
# re-crawl an toàn hơn resume từ stale state.
DEFAULT_TTL_SECONDS = 24 * 3600  # 24h


class CheckpointStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INVALIDATED = "invalidated"


@dataclass(frozen=True, slots=True)
class CrawlCheckpoint:
    """Snapshot trạng thái 1 lần crawl để resume."""

    run_id: str
    src_space_id: str
    started_at: float  # epoch seconds
    completed_at: float | None
    status: CheckpointStatus
    walked_count: int
    last_walked_token: str

    @property
    def is_resumable(self) -> bool:
        return self.status == CheckpointStatus.RUNNING

    @property
    def age_seconds(self) -> float:
        return time.time() - self.started_at


class CrawlCheckpointStore:
    """SQLite-backed checkpoint store cho CrawlStage.

    Args:
        db_path: file path SQLite. Tự tạo nếu chưa có.
        ttl_seconds: checkpoint cũ hơn ngưỡng → invalidate khi load.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        src_space_id TEXT NOT NULL,
        started_at REAL NOT NULL,
        completed_at REAL,
        status TEXT NOT NULL DEFAULT 'running',
        walked_count INTEGER NOT NULL DEFAULT 0,
        last_walked_token TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
    CREATE INDEX IF NOT EXISTS idx_runs_space ON runs(src_space_id);

    CREATE TABLE IF NOT EXISTS walked_tokens (
        run_id TEXT NOT NULL,
        src_token TEXT NOT NULL,
        walked_at REAL NOT NULL,
        PRIMARY KEY (run_id, src_token),
        FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_walked_run ON walked_tokens(run_id);
    """

    def __init__(
        self,
        db_path: Path,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.db_path = db_path
        self._ttl = ttl_seconds
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._log = logger.bind(component="CrawlCheckpointStore", db=str(db_path))

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self.db_path),
                isolation_level=None,
                check_same_thread=False,
            )
            # WAL mode → reader không block writer
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(self.SCHEMA)
        return self._conn

    async def _run(self, fn: Callable[[], T]) -> T:
        return await asyncio.get_running_loop().run_in_executor(None, fn)

    # ====================================================================
    # Public API
    # ====================================================================

    async def begin_run(self, src_space_id: str) -> CrawlCheckpoint:
        """Tạo run mới — caller giữ run_id để pass vào save_progress."""
        run_id = str(uuid.uuid4())
        started_at = time.time()

        async with self._lock:
            def _ins() -> None:
                self._connect().execute(
                    "INSERT INTO runs(run_id, src_space_id, started_at, status) "
                    "VALUES(?, ?, ?, ?)",
                    (run_id, src_space_id, started_at, CheckpointStatus.RUNNING.value),
                )
            await self._run(_ins)

        return CrawlCheckpoint(
            run_id=run_id,
            src_space_id=src_space_id,
            started_at=started_at,
            completed_at=None,
            status=CheckpointStatus.RUNNING,
            walked_count=0,
            last_walked_token="",
        )

    async def find_resumable(
        self, src_space_id: str,
    ) -> CrawlCheckpoint | None:
        """Tìm run đang RUNNING gần nhất cho space.

        Logic:
          1. Filter status=RUNNING + cùng space
          2. Sort theo started_at DESC, take first
          3. Nếu age > ttl → invalidate + return None
          4. Nếu age <= ttl → return resumable checkpoint
        """
        async with self._lock:
            def _q() -> CrawlCheckpoint | None:
                cur = self._connect().execute(
                    "SELECT run_id, src_space_id, started_at, completed_at, "
                    "status, walked_count, last_walked_token "
                    "FROM runs "
                    "WHERE src_space_id=? AND status=? "
                    "ORDER BY started_at DESC LIMIT 1",
                    (src_space_id, CheckpointStatus.RUNNING.value),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                ckpt = CrawlCheckpoint(
                    run_id=str(row[0]),
                    src_space_id=str(row[1]),
                    started_at=float(row[2]),
                    completed_at=float(row[3]) if row[3] is not None else None,
                    status=CheckpointStatus(row[4]),
                    walked_count=int(row[5]),
                    last_walked_token=str(row[6]),
                )
                if ckpt.age_seconds > self._ttl:
                    # Stale → invalidate inline
                    self._connect().execute(
                        "UPDATE runs SET status=? WHERE run_id=?",
                        (CheckpointStatus.INVALIDATED.value, ckpt.run_id),
                    )
                    return None
                return ckpt
            return await self._run(_q)

    async def get_walked_tokens(self, run_id: str) -> set[str]:
        """Load set token đã walked cho 1 run — dùng để skip khi resume."""
        async with self._lock:
            def _q() -> set[str]:
                cur = self._connect().execute(
                    "SELECT src_token FROM walked_tokens WHERE run_id=?",
                    (run_id,),
                )
                return {str(row[0]) for row in cur.fetchall()}
            return await self._run(_q)

    async def save_progress(
        self,
        run_id: str,
        *,
        new_walked_tokens: Iterable[str],
        last_walked_token: str,
        walked_count: int,
    ) -> None:
        """Atomic save: insert walked tokens + update last_walked + count.

        `new_walked_tokens` là delta từ lần save_progress trước.
        """
        async with self._lock:
            def _save() -> None:
                conn = self._connect()
                conn.execute("BEGIN")
                try:
                    # INSERT OR IGNORE để idempotent (re-save same token OK)
                    conn.executemany(
                        "INSERT OR IGNORE INTO walked_tokens"
                        "(run_id, src_token, walked_at) VALUES(?, ?, ?)",
                        [(run_id, t, time.time()) for t in new_walked_tokens],
                    )
                    conn.execute(
                        "UPDATE runs SET walked_count=?, last_walked_token=? "
                        "WHERE run_id=?",
                        (walked_count, last_walked_token, run_id),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            await self._run(_save)

    async def mark_complete(self, run_id: str) -> None:
        """Mark run là COMPLETED — không resume nữa."""
        async with self._lock:
            def _upd() -> None:
                self._connect().execute(
                    "UPDATE runs SET status=?, completed_at=? WHERE run_id=?",
                    (CheckpointStatus.COMPLETED.value, time.time(), run_id),
                )
            await self._run(_upd)

    async def mark_failed(self, run_id: str, *, reason: str = "") -> None:
        """Mark run FAILED — sẽ vẫn được resume nếu < TTL."""
        async with self._lock:
            def _upd() -> None:
                self._connect().execute(
                    "UPDATE runs SET status=? WHERE run_id=?",
                    (CheckpointStatus.FAILED.value, run_id),
                )
            await self._run(_upd)
        if reason:
            self._log.warning("checkpoint_marked_failed", run_id=run_id, reason=reason)

    async def invalidate(self, run_id: str) -> None:
        """Force invalidate — caller muốn full re-crawl."""
        async with self._lock:
            def _upd() -> None:
                self._connect().execute(
                    "UPDATE runs SET status=? WHERE run_id=?",
                    (CheckpointStatus.INVALIDATED.value, run_id),
                )
            await self._run(_upd)

    async def cleanup_old(self, *, max_age_seconds: int = 7 * 24 * 3600) -> int:
        """Xoá runs + walked_tokens cũ hơn ngưỡng. Trả về số runs deleted."""
        cutoff = time.time() - max_age_seconds
        async with self._lock:
            def _cleanup() -> int:
                conn = self._connect()
                cur = conn.execute(
                    "DELETE FROM runs WHERE started_at < ? AND status != ?",
                    (cutoff, CheckpointStatus.RUNNING.value),
                )
                # walked_tokens auto-deleted via CASCADE (nếu enabled)
                conn.execute(
                    "DELETE FROM walked_tokens WHERE run_id NOT IN "
                    "(SELECT run_id FROM runs)",
                )
                return int(cur.rowcount)
            return await self._run(_cleanup)

    async def aclose(self) -> None:
        async with self._lock:
            if self._conn is not None:
                with suppress(sqlite3.Error):
                    self._conn.close()
                self._conn = None


def now_iso() -> str:
    """Helper trả về ISO timestamp dùng cho audit log."""
    return datetime.now(tz=UTC).isoformat()
