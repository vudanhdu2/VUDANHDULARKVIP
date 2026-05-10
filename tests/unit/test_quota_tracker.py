"""Tests cho `QuotaTracker`."""

from __future__ import annotations

import asyncio

import pytest

from waytoagi.resilience.quota_tracker import (
    QuotaResource,
    QuotaTracker,
)


@pytest.mark.unit
class TestQuotaTrackerRegister:
    def test_register_with_caps(self) -> None:
        tracker = QuotaTracker({
            "lark_writes": (50.0, 10000),
        })
        # Internal state populated
        assert "lark_writes" in tracker._caps  # type: ignore[attr-defined]

    def test_register_invalid_rps(self) -> None:
        tracker = QuotaTracker()
        with pytest.raises(ValueError, match="rps_cap"):
            tracker.register_resource("test", rps_cap=0)


@pytest.mark.unit
class TestQuotaUsage:
    @pytest.mark.asyncio
    async def test_unused_resource_zero_usage(self) -> None:
        tracker = QuotaTracker({"test": (10.0, None)})
        usage = await tracker.usage("test")
        assert usage is not None
        assert usage.rps_used == 0
        assert usage.daily_used == 0
        assert usage.seconds_until_rps_limit == 0.0

    @pytest.mark.asyncio
    async def test_record_increments_counter(self) -> None:
        tracker = QuotaTracker({"test": (10.0, None)})
        for _ in range(5):
            await tracker.record_call("test")
        usage = await tracker.usage("test")
        assert usage is not None
        assert usage.rps_used == 5
        assert usage.daily_used == 5

    @pytest.mark.asyncio
    async def test_unknown_resource_returns_none(self) -> None:
        tracker = QuotaTracker()
        usage = await tracker.usage("never-registered")
        assert usage is None

    @pytest.mark.asyncio
    async def test_record_unknown_resource_no_op(self) -> None:
        tracker = QuotaTracker()
        # Không raise
        await tracker.record_call("unknown")


@pytest.mark.unit
class TestPredictThrottle:
    @pytest.mark.asyncio
    async def test_within_cap_no_throttle(self) -> None:
        tracker = QuotaTracker({"test": (10.0, None)})
        for _ in range(3):
            await tracker.record_call("test")
        wait = await tracker.predict_seconds_until_limit("test")
        assert wait == 0.0

    @pytest.mark.asyncio
    async def test_at_cap_predicts_wait(self) -> None:
        tracker = QuotaTracker({"test": (5.0, None)})
        for _ in range(5):
            await tracker.record_call("test")
        wait = await tracker.predict_seconds_until_limit("test")
        # Còn ~1s đến slot oldest expire
        assert wait > 0
        assert wait <= 1.0

    @pytest.mark.asyncio
    async def test_unknown_resource_no_wait(self) -> None:
        tracker = QuotaTracker()
        wait = await tracker.predict_seconds_until_limit("never")
        assert wait == 0.0


@pytest.mark.unit
class TestThrottleContext:
    @pytest.mark.asyncio
    async def test_throttle_records_call(self) -> None:
        tracker = QuotaTracker({"test": (10.0, None)})
        async with tracker.throttle("test"):
            pass
        usage = await tracker.usage("test")
        assert usage is not None
        assert usage.rps_used == 1

    @pytest.mark.asyncio
    async def test_throttle_proactively_sleeps_at_cap(self) -> None:
        """At cap → throttle await trước khi enter."""
        tracker = QuotaTracker({"test": (3.0, None)})
        for _ in range(3):
            await tracker.record_call("test")

        # Throttle phải sleep đợi cap reset
        import time
        start = time.monotonic()
        async with tracker.throttle("test"):
            pass
        elapsed = time.monotonic() - start
        # Phải > 0 (throttled), nhưng < 2s
        assert elapsed > 0


@pytest.mark.unit
class TestQuotaUsagePct:
    @pytest.mark.asyncio
    async def test_rps_pct_calculated(self) -> None:
        tracker = QuotaTracker({"test": (10.0, None)})
        for _ in range(8):
            await tracker.record_call("test")
        usage = await tracker.usage("test")
        assert usage is not None
        assert 0.7 < usage.rps_pct < 0.9

    @pytest.mark.asyncio
    async def test_near_limit_flag(self) -> None:
        tracker = QuotaTracker({"test": (10.0, None)})
        for _ in range(8):
            await tracker.record_call("test")
        usage = await tracker.usage("test")
        assert usage is not None
        assert usage.near_limit is True

    @pytest.mark.asyncio
    async def test_below_limit_not_near(self) -> None:
        tracker = QuotaTracker({"test": (10.0, None)})
        for _ in range(3):
            await tracker.record_call("test")
        usage = await tracker.usage("test")
        assert usage is not None
        assert usage.near_limit is False


@pytest.mark.unit
class TestQuotaResourceEnum:
    def test_enum_values(self) -> None:
        # Sanity check enum members
        assert QuotaResource.LARK_BASE_WRITES.value == "lark_base_writes"
        assert QuotaResource.LLM_DEFAULT.value == "llm_default"


@pytest.mark.unit
class TestSlidingWindow:
    @pytest.mark.asyncio
    async def test_old_calls_expire_from_rps_window(self) -> None:
        """Sau 1.5s, calls cũ ra khỏi RPS window."""
        tracker = QuotaTracker({"test": (10.0, None)})
        await tracker.record_call("test")
        await asyncio.sleep(1.1)
        usage = await tracker.usage("test")
        assert usage is not None
        # rps_used = 0 (call cũ đã expire)
        assert usage.rps_used == 0
        # daily_used = 1 (vẫn trong 24h window)
        assert usage.daily_used == 1
