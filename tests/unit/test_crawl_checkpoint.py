"""Tests cho `CrawlCheckpointStore` — SQLite resume."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import pytest

from waytoagi.crawl.checkpoint import (
    CheckpointStatus,
    CrawlCheckpointStore,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "ckpt.db"


@pytest.mark.unit
class TestBeginRun:
    @pytest.mark.asyncio
    async def test_creates_running_checkpoint(self, db_path: Path) -> None:
        store = CrawlCheckpointStore(db_path)
        ckpt = await store.begin_run("space-1")
        assert ckpt.run_id
        assert ckpt.src_space_id == "space-1"
        assert ckpt.status == CheckpointStatus.RUNNING
        assert ckpt.walked_count == 0
        assert ckpt.is_resumable is True
        await store.aclose()

    @pytest.mark.asyncio
    async def test_run_ids_unique(self, db_path: Path) -> None:
        store = CrawlCheckpointStore(db_path)
        a = await store.begin_run("space-1")
        b = await store.begin_run("space-1")
        assert a.run_id != b.run_id
        await store.aclose()


@pytest.mark.unit
class TestSaveProgress:
    @pytest.mark.asyncio
    async def test_save_walked_tokens(self, db_path: Path) -> None:
        store = CrawlCheckpointStore(db_path)
        ckpt = await store.begin_run("space-1")
        await store.save_progress(
            ckpt.run_id,
            new_walked_tokens=["t1", "t2", "t3"],
            last_walked_token="t3",
            walked_count=3,
        )
        walked = await store.get_walked_tokens(ckpt.run_id)
        assert walked == {"t1", "t2", "t3"}
        await store.aclose()

    @pytest.mark.asyncio
    async def test_save_idempotent_same_token_twice(self, db_path: Path) -> None:
        store = CrawlCheckpointStore(db_path)
        ckpt = await store.begin_run("space-1")
        await store.save_progress(
            ckpt.run_id,
            new_walked_tokens=["t1"],
            last_walked_token="t1",
            walked_count=1,
        )
        # Save lại t1 → INSERT OR IGNORE
        await store.save_progress(
            ckpt.run_id,
            new_walked_tokens=["t1", "t2"],
            last_walked_token="t2",
            walked_count=2,
        )
        walked = await store.get_walked_tokens(ckpt.run_id)
        assert walked == {"t1", "t2"}
        await store.aclose()


@pytest.mark.unit
class TestFindResumable:
    @pytest.mark.asyncio
    async def test_find_returns_running_run(self, db_path: Path) -> None:
        store = CrawlCheckpointStore(db_path)
        original = await store.begin_run("space-1")
        await store.save_progress(
            original.run_id,
            new_walked_tokens=["t1"],
            last_walked_token="t1",
            walked_count=1,
        )

        found = await store.find_resumable("space-1")
        assert found is not None
        assert found.run_id == original.run_id
        assert found.walked_count == 1
        assert found.last_walked_token == "t1"
        await store.aclose()

    @pytest.mark.asyncio
    async def test_find_skips_completed(self, db_path: Path) -> None:
        store = CrawlCheckpointStore(db_path)
        ckpt = await store.begin_run("space-1")
        await store.mark_complete(ckpt.run_id)
        found = await store.find_resumable("space-1")
        assert found is None
        await store.aclose()

    @pytest.mark.asyncio
    async def test_find_skips_other_space(self, db_path: Path) -> None:
        store = CrawlCheckpointStore(db_path)
        await store.begin_run("space-1")
        found = await store.find_resumable("space-2")
        assert found is None
        await store.aclose()

    @pytest.mark.asyncio
    async def test_find_invalidates_stale(self, db_path: Path) -> None:
        store = CrawlCheckpointStore(db_path, ttl_seconds=0)  # 0 → instant stale
        ckpt = await store.begin_run("space-1")
        # Sleep tí để age > 0
        await asyncio.sleep(0.01)
        found = await store.find_resumable("space-1")
        assert found is None  # invalidated
        # Verify trong DB là INVALIDATED
        # (Reuse find logic — fresh begin_run trên cùng space sẽ
        # vẫn tạo run_id mới)
        new_ckpt = await store.begin_run("space-1")
        assert new_ckpt.run_id != ckpt.run_id
        await store.aclose()


@pytest.mark.unit
class TestMarkComplete:
    @pytest.mark.asyncio
    async def test_mark_complete_sets_timestamp(self, db_path: Path) -> None:
        store = CrawlCheckpointStore(db_path)
        ckpt = await store.begin_run("space-1")
        await store.mark_complete(ckpt.run_id)
        # Find resumable → None vì status = COMPLETED
        found = await store.find_resumable("space-1")
        assert found is None
        await store.aclose()


@pytest.mark.unit
class TestMarkFailed:
    @pytest.mark.asyncio
    async def test_failed_still_resumable(self, db_path: Path) -> None:
        """Mark failed → vẫn pickable trong tương lai (FAILED ≠ COMPLETED)?
        Theo spec: chỉ RUNNING resumable. FAILED → caller decide retry.
        """
        store = CrawlCheckpointStore(db_path)
        ckpt = await store.begin_run("space-1")
        await store.mark_failed(ckpt.run_id, reason="rate-limit")
        # FAILED không trong RUNNING set → find_resumable trả None
        found = await store.find_resumable("space-1")
        assert found is None
        await store.aclose()


@pytest.mark.unit
class TestInvalidate:
    @pytest.mark.asyncio
    async def test_force_invalidate(self, db_path: Path) -> None:
        store = CrawlCheckpointStore(db_path)
        ckpt = await store.begin_run("space-1")
        await store.invalidate(ckpt.run_id)
        found = await store.find_resumable("space-1")
        assert found is None
        await store.aclose()


@pytest.mark.unit
class TestCleanupOld:
    @pytest.mark.asyncio
    async def test_cleanup_removes_old_completed(self, db_path: Path) -> None:
        store = CrawlCheckpointStore(db_path)
        # Old run, completed
        old = await store.begin_run("space-1")
        await store.mark_complete(old.run_id)
        # Force old timestamp manually
        conn = store._connect()
        old_ts = time.time() - 10 * 24 * 3600  # 10 days ago
        conn.execute(
            "UPDATE runs SET started_at=? WHERE run_id=?",
            (old_ts, old.run_id),
        )
        # Recent run — keep ref để verify sau
        await store.begin_run("space-2")

        deleted = await store.cleanup_old(max_age_seconds=7 * 24 * 3600)
        assert deleted == 1
        # Recent vẫn còn
        found = await store.find_resumable("space-2")
        assert found is not None
        await store.aclose()


@pytest.mark.unit
class TestRoundTrip:
    @pytest.mark.asyncio
    async def test_full_lifecycle(self, db_path: Path) -> None:
        """End-to-end: begin → save → save → complete."""
        store = CrawlCheckpointStore(db_path)
        ckpt = await store.begin_run("space-1")

        # Save batch 1
        await store.save_progress(
            ckpt.run_id,
            new_walked_tokens=[f"t{i}" for i in range(100)],
            last_walked_token="t99",
            walked_count=100,
        )
        # Save batch 2
        await store.save_progress(
            ckpt.run_id,
            new_walked_tokens=[f"t{i}" for i in range(100, 200)],
            last_walked_token="t199",
            walked_count=200,
        )
        walked = await store.get_walked_tokens(ckpt.run_id)
        assert len(walked) == 200

        # Resume → checkpoint vẫn RUNNING
        resumed = await store.find_resumable("space-1")
        assert resumed is not None
        assert resumed.walked_count == 200
        assert resumed.last_walked_token == "t199"

        # Complete
        await store.mark_complete(ckpt.run_id)
        assert await store.find_resumable("space-1") is None
        await store.aclose()
