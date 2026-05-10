"""Tests cho `CircuitBreaker`."""

from __future__ import annotations

import asyncio

import pytest

from waytoagi.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
)


async def _ok() -> str:
    return "ok"


async def _fail() -> str:
    msg = "boom"
    raise RuntimeError(msg)


@pytest.mark.unit
class TestCircuitBreakerInit:
    def test_initial_closed(self) -> None:
        cb = CircuitBreaker("test")
        assert cb.state == CircuitState.CLOSED

    def test_invalid_failure_threshold(self) -> None:
        with pytest.raises(ValueError, match="failure_threshold"):
            CircuitBreaker("test", failure_threshold=0)

    def test_invalid_recovery_timeout(self) -> None:
        with pytest.raises(ValueError, match="recovery_timeout"):
            CircuitBreaker("test", recovery_timeout=0)


@pytest.mark.unit
class TestCircuitClosedToOpen:
    @pytest.mark.asyncio
    async def test_consecutive_failures_open_circuit(self) -> None:
        cb = CircuitBreaker(
            "test", failure_threshold=3, recovery_timeout=10.0,
        )
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await cb.call(_fail)
        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_success_resets_consecutive_count(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=3)
        with pytest.raises(RuntimeError):
            await cb.call(_fail)
        with pytest.raises(RuntimeError):
            await cb.call(_fail)
        # Success → reset counter
        await cb.call(_ok)
        # 2 fails next → vẫn không OPEN (counter reset)
        with pytest.raises(RuntimeError):
            await cb.call(_fail)
        with pytest.raises(RuntimeError):
            await cb.call(_fail)
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_open_raises_circuit_open_error(self) -> None:
        cb = CircuitBreaker(
            "test", failure_threshold=2, recovery_timeout=10.0,
        )
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(_fail)
        # Circuit OPEN → fail-fast
        with pytest.raises(CircuitOpenError) as exc:
            await cb.call(_ok)
        assert exc.value.resource == "test"


@pytest.mark.unit
class TestCircuitOpenToHalfOpen:
    @pytest.mark.asyncio
    async def test_recovery_timeout_transitions_half_open(self) -> None:
        cb = CircuitBreaker(
            "test", failure_threshold=2, recovery_timeout=0.05,
            success_threshold=2,
        )
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(_fail)
        assert cb.state == CircuitState.OPEN

        # Sleep để timer expire
        await asyncio.sleep(0.06)

        # Next call → transit HALF_OPEN
        await cb.call(_ok)
        # Sau 1 success vẫn HALF_OPEN (chưa đủ success_threshold=2)
        assert cb.state == CircuitState.HALF_OPEN


@pytest.mark.unit
class TestCircuitHalfOpenToClosed:
    @pytest.mark.asyncio
    async def test_success_threshold_closes_circuit(self) -> None:
        cb = CircuitBreaker(
            "test", failure_threshold=2, recovery_timeout=0.05,
            success_threshold=2,
        )
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(_fail)
        await asyncio.sleep(0.06)

        # 2 successes → close
        await cb.call(_ok)
        await cb.call(_ok)
        assert cb.state == CircuitState.CLOSED


@pytest.mark.unit
class TestCircuitHalfOpenToOpen:
    @pytest.mark.asyncio
    async def test_any_failure_in_half_open_reopens(self) -> None:
        cb = CircuitBreaker(
            "test", failure_threshold=2, recovery_timeout=0.05,
            success_threshold=2,
        )
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(_fail)
        await asyncio.sleep(0.06)

        # 1 success then fail → reopen
        await cb.call(_ok)
        with pytest.raises(RuntimeError):
            await cb.call(_fail)
        assert cb.state == CircuitState.OPEN


@pytest.mark.unit
class TestCircuitReset:
    @pytest.mark.asyncio
    async def test_reset_returns_to_closed(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=2)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(_fail)
        assert cb.state == CircuitState.OPEN

        await cb.reset()
        assert cb.state == CircuitState.CLOSED


@pytest.mark.unit
class TestCircuitStats:
    @pytest.mark.asyncio
    async def test_stats_track_calls(self) -> None:
        cb = CircuitBreaker("test")
        await cb.call(_ok)
        with pytest.raises(RuntimeError):
            await cb.call(_fail)
        stats = cb.stats()
        assert stats.total_calls == 2
        assert stats.total_failures == 1
        assert stats.consecutive_failures == 1
        assert stats.failure_rate == 0.5

    @pytest.mark.asyncio
    async def test_stats_reset_consecutive_on_success(self) -> None:
        cb = CircuitBreaker("test")
        with pytest.raises(RuntimeError):
            await cb.call(_fail)
        await cb.call(_ok)
        stats = cb.stats()
        assert stats.consecutive_failures == 0
        assert stats.total_failures == 1


@pytest.mark.unit
class TestCircuitHalfOpenMaxCalls:
    @pytest.mark.asyncio
    async def test_too_many_in_flight_rejects(self) -> None:
        """HALF_OPEN với half_open_max=1 → only 1 call allowed in-flight."""
        cb = CircuitBreaker(
            "test", failure_threshold=1, recovery_timeout=0.05,
            success_threshold=2, half_open_max_calls=1,
        )
        with pytest.raises(RuntimeError):
            await cb.call(_fail)
        await asyncio.sleep(0.06)

        # Slow ok task
        async def _slow_ok() -> str:
            await asyncio.sleep(0.1)
            return "ok"

        # Start 1 in-flight
        task = asyncio.create_task(cb.call(_slow_ok))
        await asyncio.sleep(0.01)
        # 2nd call rejected (slot full)
        with pytest.raises(CircuitOpenError):
            await cb.call(_ok)
        await task
