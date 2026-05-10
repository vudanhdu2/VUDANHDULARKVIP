"""Tests cho `RetryPolicy` — tích hợp ErrorClassifier + CircuitBreaker."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from waytoagi.lark.auth import LarkAPIError
from waytoagi.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
)
from waytoagi.resilience.quota_tracker import QuotaTracker
from waytoagi.resilience.retry_policy import RetryPolicy


@pytest.mark.unit
class TestRetryPolicySuccess:
    @pytest.mark.asyncio
    async def test_first_call_success(self) -> None:
        fn = AsyncMock(return_value="ok")
        policy = RetryPolicy(max_attempts=3)
        result = await policy.execute(fn)
        assert result == "ok"
        assert fn.call_count == 1


@pytest.mark.unit
class TestRetryPolicyTransient:
    @pytest.mark.asyncio
    async def test_retry_then_success(self) -> None:
        # Fail rate-limit lần 1 + 2, success lần 3
        rate_err = LarkAPIError(99991400, "rate limit", "/x")
        fn = AsyncMock(side_effect=[rate_err, rate_err, "ok"])
        # Backoff cố tình thấp để test nhanh
        policy = RetryPolicy(max_attempts=5)
        # Override backoff via mock recommended_backoff_seconds — không
        # cần, vì rate_limit base=2, cap=60 → 2,4,8,16... Test chấp nhận
        # < 1s tổng cho 3 calls với backoff đầu tiên = 2s.
        # Solve: dùng network err có backoff 1,2,4,8

        net_err = TimeoutError("read timed out")
        fn = AsyncMock(side_effect=[net_err, net_err, "ok"])
        result = await policy.execute(fn)
        assert result == "ok"
        assert fn.call_count == 3


@pytest.mark.unit
class TestRetryPolicyPermanent:
    @pytest.mark.asyncio
    async def test_permanent_error_no_retry(self) -> None:
        perm_err = LarkAPIError(131006, "perm denied", "/x")
        fn = AsyncMock(side_effect=perm_err)
        policy = RetryPolicy(max_attempts=5)
        with pytest.raises(LarkAPIError):
            await policy.execute(fn)
        # Chỉ gọi 1 lần
        assert fn.call_count == 1

    @pytest.mark.asyncio
    async def test_not_found_no_retry(self) -> None:
        err = LarkAPIError(131005, "not found", "/x")
        fn = AsyncMock(side_effect=err)
        policy = RetryPolicy(max_attempts=5)
        with pytest.raises(LarkAPIError):
            await policy.execute(fn)
        assert fn.call_count == 1


@pytest.mark.unit
class TestRetryPolicyExhausted:
    @pytest.mark.asyncio
    async def test_giveup_raise(self) -> None:
        net_err = TimeoutError("read timed out")
        fn = AsyncMock(side_effect=net_err)
        policy = RetryPolicy(max_attempts=2)
        with pytest.raises(TimeoutError):
            await policy.execute(fn)
        assert fn.call_count == 2

    @pytest.mark.asyncio
    async def test_giveup_return_none(self) -> None:
        net_err = TimeoutError("timeout")
        fn = AsyncMock(side_effect=net_err)
        policy = RetryPolicy(max_attempts=2, on_giveup="return_none")
        result = await policy.execute(fn)
        assert result is None


@pytest.mark.unit
class TestRetryPolicyCircuitBreaker:
    @pytest.mark.asyncio
    async def test_circuit_open_raises_no_retry(self) -> None:
        cb = CircuitBreaker(
            "test", failure_threshold=1, recovery_timeout=10.0,
        )
        # Mở circuit
        async def _f() -> str:
            msg = "boom"
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError):
            await cb.call(_f)
        # Circuit OPEN

        fn = AsyncMock(return_value="never")
        policy = RetryPolicy(max_attempts=5, circuit_breaker=cb)
        with pytest.raises(CircuitOpenError):
            await policy.execute(fn)
        # Không gọi fn (circuit open ngay)
        fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_circuit_open_with_giveup_returns_none(self) -> None:
        cb = CircuitBreaker(
            "test", failure_threshold=1, recovery_timeout=10.0,
        )

        async def _f() -> str:
            msg = "boom"
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError):
            await cb.call(_f)

        fn = AsyncMock(return_value="never")
        policy = RetryPolicy(
            max_attempts=5, circuit_breaker=cb, on_giveup="return_none",
        )
        result = await policy.execute(fn)
        assert result is None


@pytest.mark.unit
class TestRetryPolicyQuotaIntegration:
    @pytest.mark.asyncio
    async def test_records_call_to_quota(self) -> None:
        tracker = QuotaTracker({"test": (10.0, None)})
        fn = AsyncMock(return_value="ok")
        policy = RetryPolicy(
            max_attempts=3,
            quota_tracker=tracker,
            quota_resource="test",
        )
        await policy.execute(fn)
        usage = await tracker.usage("test")
        assert usage is not None
        assert usage.rps_used == 1


@pytest.mark.unit
class TestRetryPolicyValidation:
    def test_invalid_max_attempts(self) -> None:
        with pytest.raises(ValueError, match="max_attempts"):
            RetryPolicy(max_attempts=0)
