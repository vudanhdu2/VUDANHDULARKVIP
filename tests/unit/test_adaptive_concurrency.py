"""Unit tests cho `AdaptiveConcurrency` — workers tự scale theo rate-limit."""

from __future__ import annotations

import asyncio

import pytest

from waytoagi.optimize.adaptive import AdaptiveConcurrency, ConcurrencySignal


@pytest.mark.unit
class TestAdaptiveConcurrencyInit:
    def test_initial_in_bounds(self) -> None:
        ac = AdaptiveConcurrency(initial=3, min_workers=1, max_workers=5)
        assert ac.current_workers == 3

    def test_initial_below_min_raises(self) -> None:
        with pytest.raises(ValueError, match="ngoài"):
            AdaptiveConcurrency(initial=0, min_workers=1, max_workers=5)

    def test_initial_above_max_raises(self) -> None:
        with pytest.raises(ValueError, match="ngoài"):
            AdaptiveConcurrency(initial=10, min_workers=1, max_workers=5)

    def test_threshold_invariant(self) -> None:
        with pytest.raises(ValueError, match="threshold"):
            AdaptiveConcurrency(
                initial=2, min_workers=1, max_workers=5,
                scale_up_threshold=0.5, scale_down_threshold=0.4,
            )


@pytest.mark.unit
class TestAdaptiveSlot:
    @pytest.mark.asyncio
    async def test_slot_acquire_release(self) -> None:
        ac = AdaptiveConcurrency(initial=2, min_workers=1, max_workers=4)
        # 2 concurrent slots OK
        async with ac.slot(), ac.slot():
            pass

    @pytest.mark.asyncio
    async def test_slot_blocks_at_capacity(self) -> None:
        ac = AdaptiveConcurrency(initial=1, min_workers=1, max_workers=4)
        async with ac.slot():
            # 2nd acquire phải timeout (not enough slot)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(ac.slot().__aenter__(), timeout=0.05)


@pytest.mark.unit
class TestAdaptiveScale:
    @pytest.mark.asyncio
    async def test_scale_up_after_clean_window(self) -> None:
        """Window đầy với toàn OK → scale up."""
        ac = AdaptiveConcurrency(
            initial=2, min_workers=1, max_workers=4,
            window_size=10, cooldown_signals=10,
        )
        # 10 OK signals → fill window + cooldown ready → scale up
        for _ in range(10):
            await ac.signal_ok()
        assert ac.current_workers == 3

    @pytest.mark.asyncio
    async def test_scale_down_after_rate_limit_burst(self) -> None:
        """Window có >= scale_down_threshold rate-limit → scale down."""
        ac = AdaptiveConcurrency(
            initial=4, min_workers=1, max_workers=8,
            window_size=10, cooldown_signals=10,
            scale_down_threshold=0.2,
        )
        # 8 OK + 2 RL = 20% rate-limit → scale down
        for _ in range(8):
            await ac.signal_ok()
        for _ in range(2):
            await ac.signal_rate_limited()
        assert ac.current_workers == 3

    @pytest.mark.asyncio
    async def test_does_not_scale_above_max(self) -> None:
        ac = AdaptiveConcurrency(
            initial=4, min_workers=1, max_workers=4,
            window_size=5, cooldown_signals=5,
        )
        for _ in range(5):
            await ac.signal_ok()
        assert ac.current_workers == 4  # capped at max

    @pytest.mark.asyncio
    async def test_does_not_scale_below_min(self) -> None:
        ac = AdaptiveConcurrency(
            initial=1, min_workers=1, max_workers=4,
            window_size=5, cooldown_signals=5,
            scale_down_threshold=0.1,
        )
        for _ in range(5):
            await ac.signal_rate_limited()
        assert ac.current_workers == 1  # floor at min

    @pytest.mark.asyncio
    async def test_cooldown_blocks_rapid_oscillation(self) -> None:
        """Sau scale up, cooldown N signals trước khi scale lại."""
        ac = AdaptiveConcurrency(
            initial=2, min_workers=1, max_workers=8,
            window_size=10, cooldown_signals=20,
        )
        for _ in range(10):
            await ac.signal_ok()
        first_count = ac.current_workers
        # Tiếp tục push 5 OK signals — chưa đủ cooldown 20 → KHÔNG scale up
        for _ in range(5):
            await ac.signal_ok()
        assert ac.current_workers == first_count

    @pytest.mark.asyncio
    async def test_window_must_be_full_before_scaling(self) -> None:
        ac = AdaptiveConcurrency(
            initial=2, min_workers=1, max_workers=4,
            window_size=10, cooldown_signals=5,
        )
        # 5 OK signals → cooldown OK nhưng window chưa đầy
        for _ in range(5):
            await ac.signal_ok()
        assert ac.current_workers == 2  # chưa scale


@pytest.mark.unit
class TestAdaptiveStats:
    @pytest.mark.asyncio
    async def test_stats_snapshot(self) -> None:
        ac = AdaptiveConcurrency(initial=2, min_workers=1, max_workers=4)
        await ac.signal_ok()
        await ac.signal_rate_limited()
        await ac.signal_error()
        stats = ac.stats()
        assert stats.window_size == 3
        assert stats.ok_count == 1
        assert stats.rate_limited_count == 1
        assert stats.error_count == 1
        assert stats.current_workers == 2

    @pytest.mark.asyncio
    async def test_rate_limited_pct(self) -> None:
        ac = AdaptiveConcurrency(initial=2, min_workers=1, max_workers=4)
        await ac.signal_ok()
        await ac.signal_rate_limited()
        stats = ac.stats()
        assert stats.rate_limited_pct == 0.5

    @pytest.mark.asyncio
    async def test_scale_counters(self) -> None:
        ac = AdaptiveConcurrency(
            initial=2, min_workers=1, max_workers=4,
            window_size=5, cooldown_signals=5,
            scale_down_threshold=0.5,
        )
        # Trigger 1 scale up
        for _ in range(5):
            await ac.signal_ok()
        # Trigger 1 scale down
        for _ in range(5):
            await ac.signal_rate_limited()
        stats = ac.stats()
        assert stats.scale_up_count == 1
        assert stats.scale_down_count == 1


@pytest.mark.unit
class TestSignalConvenience:
    @pytest.mark.asyncio
    async def test_signal_methods_dispatch(self) -> None:
        ac = AdaptiveConcurrency(initial=2, min_workers=1, max_workers=4)
        await ac.signal_ok()
        await ac.signal_rate_limited()
        await ac.signal_error()
        stats = ac.stats()
        assert stats.window_size == 3

    @pytest.mark.asyncio
    async def test_signal_enum_dispatch(self) -> None:
        ac = AdaptiveConcurrency(initial=2, min_workers=1, max_workers=4)
        await ac.signal(ConcurrencySignal.OK)
        await ac.signal(ConcurrencySignal.RATE_LIMITED)
        stats = ac.stats()
        assert stats.window_size == 2
