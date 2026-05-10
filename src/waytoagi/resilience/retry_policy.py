"""RetryPolicy — centralized retry strategy dựa trên ErrorCategory.

Một class wrap async fn với:
  - Phân loại error qua `classify_error()`
  - Quyết định retry hay không qua `is_retryable()`
  - Sleep theo `recommended_backoff_seconds()` (cap by max_attempts)
  - Optional: integrate `CircuitBreaker` (raise CircuitOpenError → permanent fail)
  - Optional: integrate `QuotaTracker` (proactive throttle trước call)

Pattern: tiêu chuẩn cho mọi async I/O của V2.

Usage:
    policy = RetryPolicy(max_attempts=5, on_giveup="raise")

    async def my_call():
        return await base.update_record(...)

    result = await policy.execute(my_call, op_name="update_record")
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, TypeVar

import structlog

from waytoagi.resilience.circuit_breaker import CircuitOpenError
from waytoagi.resilience.error_classifier import (
    classify_error,
    is_retryable,
    recommended_backoff_seconds,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from waytoagi.resilience.circuit_breaker import CircuitBreaker
    from waytoagi.resilience.quota_tracker import QuotaTracker

logger = structlog.get_logger(__name__)

T = TypeVar("T")

GiveupBehavior = Literal["raise", "return_none"]


@dataclass(slots=True)
class RetryAttempt:
    """1 attempt trong execution log — cho audit/debug."""

    attempt_number: int
    error: str = ""
    category: str = ""
    backoff_seconds: float = 0.0
    duration_seconds: float = 0.0


@dataclass(slots=True)
class RetryResult:
    """Kết quả 1 lần execute — caller có thể inspect attempts log."""

    succeeded: bool
    attempts: list[RetryAttempt] = field(default_factory=list)
    final_error: str = ""
    total_duration_seconds: float = 0.0


class RetryPolicy:
    """Async retry với error category-aware backoff.

    Args:
        max_attempts: max tổng attempts (bao gồm cả lần đầu).
        on_giveup: hành vi khi vượt max_attempts:
          - "raise" (default): re-raise exception cuối
          - "return_none": return None
        circuit_breaker: optional, fail-fast khi circuit OPEN.
        quota_tracker: optional, proactive throttle trước call.
        quota_resource: tên resource trong quota_tracker.
    """

    def __init__(
        self,
        *,
        max_attempts: int = 5,
        on_giveup: GiveupBehavior = "raise",
        circuit_breaker: CircuitBreaker | None = None,
        quota_tracker: QuotaTracker | None = None,
        quota_resource: str | None = None,
    ) -> None:
        if max_attempts < 1:
            msg = "max_attempts phải >= 1"
            raise ValueError(msg)
        self._max_attempts = max_attempts
        self._on_giveup = on_giveup
        self._circuit = circuit_breaker
        self._quota = quota_tracker
        self._quota_resource = quota_resource
        self._log = logger.bind(component="RetryPolicy")

    async def execute(
        self,
        fn: Callable[[], Awaitable[T]],
        *,
        op_name: str = "",
    ) -> T | None:
        """Execute fn với retry. Trả T nếu OK, None hoặc raise nếu fail.

        Loop:
          1. Quota throttle (nếu có quota_tracker)
          2. Circuit breaker check (nếu có) → raise CircuitOpenError
             coi như permanent fail (không retry)
          3. Call fn
          4. On exception: classify → backoff → retry
        """
        log = self._log.bind(op=op_name) if op_name else self._log
        last_error: BaseException | None = None
        attempts_log: list[RetryAttempt] = []

        for attempt in range(self._max_attempts):
            # Quota throttle
            if self._quota and self._quota_resource:
                wait = await self._quota.predict_seconds_until_limit(
                    self._quota_resource,
                )
                if wait > 0:
                    log.debug("quota_throttle", wait=round(wait, 2))
                    await asyncio.sleep(wait)

            # Try call
            attempt_started = asyncio.get_running_loop().time()
            try:
                if self._circuit:
                    result = await self._circuit.call(fn)
                else:
                    result = await fn()
            except CircuitOpenError as e:
                # Circuit fail-fast — không count vào retry, raise ngay
                log.warning("circuit_open", err=str(e))
                if self._on_giveup == "return_none":
                    return None
                raise
            except BaseException as e:
                last_error = e
                category = classify_error(e)
                duration = (
                    asyncio.get_running_loop().time() - attempt_started
                )

                # Record call vào quota (vẫn count fail call)
                if self._quota and self._quota_resource:
                    await self._quota.record_call(self._quota_resource)

                if not is_retryable(category):
                    # Permanent error — không retry
                    log.warning(
                        "retry_giveup_permanent",
                        attempt=attempt + 1,
                        category=category.value,
                        err=str(e)[:120],
                    )
                    attempts_log.append(RetryAttempt(
                        attempt_number=attempt + 1,
                        error=str(e)[:120],
                        category=category.value,
                        duration_seconds=round(duration, 3),
                    ))
                    if self._on_giveup == "return_none":
                        return None
                    raise

                # Backoff trước attempt tiếp theo
                if attempt + 1 >= self._max_attempts:
                    log.warning(
                        "retry_exhausted",
                        attempts=attempt + 1,
                        category=category.value,
                        err=str(e)[:120],
                    )
                    attempts_log.append(RetryAttempt(
                        attempt_number=attempt + 1,
                        error=str(e)[:120],
                        category=category.value,
                        duration_seconds=round(duration, 3),
                    ))
                    break

                backoff = recommended_backoff_seconds(category, attempt)
                log.info(
                    "retry_scheduled",
                    attempt=attempt + 1,
                    category=category.value,
                    backoff=round(backoff, 2),
                    err=str(e)[:120],
                )
                attempts_log.append(RetryAttempt(
                    attempt_number=attempt + 1,
                    error=str(e)[:120],
                    category=category.value,
                    backoff_seconds=round(backoff, 2),
                    duration_seconds=round(duration, 3),
                ))
                if backoff > 0:
                    await asyncio.sleep(backoff)
                continue
            else:
                # Success
                if self._quota and self._quota_resource:
                    await self._quota.record_call(self._quota_resource)
                if attempt > 0:
                    log.info("retry_success", final_attempt=attempt + 1)
                return result

        # Exhausted retries
        if self._on_giveup == "return_none":
            return None
        if last_error is not None:
            raise last_error
        return None


async def retry_with_policy(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 5,
    op_name: str = "",
) -> T | None:
    """Convenience helper — 1-shot retry với default policy."""
    policy = RetryPolicy(max_attempts=max_attempts)
    return await policy.execute(fn, op_name=op_name)
