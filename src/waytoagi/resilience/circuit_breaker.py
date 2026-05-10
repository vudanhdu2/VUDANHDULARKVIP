"""CircuitBreaker — 3-state machine chặn cascade failure.

Pattern Hystrix-style cho async ops. Khi 1 resource (LLM endpoint X,
Lark Base API, …) fail liên tục → mở circuit, fail-fast cho mọi call
tiếp theo trong khoảng `recovery_timeout`. Sau timeout → HALF_OPEN, thử
N call test, nếu OK → CLOSED, ngược lại → OPEN lại.

States:
  - **CLOSED**: bình thường, mọi call qua được. Track consecutive
    failures + total failures trong rolling window.
  - **OPEN**: fail-fast, mọi call raise `CircuitOpenError` ngay. Sau
    `recovery_timeout` chuyển sang HALF_OPEN.
  - **HALF_OPEN**: cho phép `success_threshold` calls đi qua. Nếu hết
    fail → OPEN lại + reset timer. Nếu hết success → CLOSED.

Per-resource: 1 instance per (resource_name) — vd "lark_base_writes",
"llm_endpoint_local", "llm_endpoint_llmgate".

Async-safe via asyncio.Lock.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, TypeVar

import structlog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = structlog.get_logger(__name__)

T = TypeVar("T")


class CircuitState(StrEnum):
    """Circuit breaker state."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised khi caller cố gọi qua circuit đang OPEN."""

    def __init__(self, resource: str, opened_for: float) -> None:
        super().__init__(
            f"Circuit '{resource}' is OPEN (opened {opened_for:.1f}s ago)",
        )
        self.resource = resource
        self.opened_for = opened_for


@dataclass(frozen=True, slots=True)
class CircuitStats:
    """Snapshot stats cho monitoring."""

    resource: str
    state: CircuitState
    consecutive_failures: int
    total_calls: int
    total_failures: int
    opened_at: float | None
    half_open_successes: int
    half_open_failures: int

    @property
    def failure_rate(self) -> float:
        if self.total_calls == 0:
            return 0.0
        return self.total_failures / self.total_calls


class CircuitBreaker:
    """Async circuit breaker per resource.

    Args:
        resource: tên resource (cho logging/stats).
        failure_threshold: số consecutive failures để mở circuit.
        recovery_timeout: seconds OPEN trước khi thử HALF_OPEN.
        success_threshold: số successes trong HALF_OPEN để CLOSE lại.
        half_open_max_calls: số calls tối đa cho phép trong HALF_OPEN
            (sau đó chỉ chờ kết quả).

    Usage:
        breaker = CircuitBreaker("llm_local", failure_threshold=5,
                                 recovery_timeout=30)
        try:
            result = await breaker.call(lambda: pool.chat([...]))
        except CircuitOpenError:
            # Route sang endpoint khác
            result = await fallback_pool.chat([...])
    """

    def __init__(
        self,
        resource: str,
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        success_threshold: int = 2,
        half_open_max_calls: int = 3,
    ) -> None:
        if failure_threshold < 1:
            msg = "failure_threshold phải >= 1"
            raise ValueError(msg)
        if recovery_timeout <= 0:
            msg = "recovery_timeout phải > 0"
            raise ValueError(msg)
        if success_threshold < 1:
            msg = "success_threshold phải >= 1"
            raise ValueError(msg)

        self._resource = resource
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._success_threshold = success_threshold
        self._half_open_max = half_open_max_calls

        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._total_calls = 0
        self._total_failures = 0
        self._opened_at: float | None = None
        self._half_open_successes = 0
        self._half_open_failures = 0
        self._half_open_in_flight = 0
        self._lock = asyncio.Lock()
        self._log = logger.bind(component="CircuitBreaker", resource=resource)

    # ====================================================================
    # Public API
    # ====================================================================

    @property
    def state(self) -> CircuitState:
        """Snapshot state — không lock (best-effort read).

        Re-evaluate timer (CLOSED→HALF_OPEN tự động sau recovery_timeout):
        nếu timer expired, caller cần await call() để actually transit.
        Property này không mutate state, chỉ dùng cho monitoring.
        """
        return self._state

    @property
    def resource(self) -> str:
        return self._resource

    def stats(self) -> CircuitStats:
        return CircuitStats(
            resource=self._resource,
            state=self._state,
            consecutive_failures=self._consecutive_failures,
            total_calls=self._total_calls,
            total_failures=self._total_failures,
            opened_at=self._opened_at,
            half_open_successes=self._half_open_successes,
            half_open_failures=self._half_open_failures,
        )

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Execute coroutine qua circuit. Raise CircuitOpenError nếu OPEN.

        Trên success: signal_success.
        Trên exception: signal_failure rồi re-raise.
        """
        await self._maybe_transit_to_half_open()

        async with self._lock:
            if self._state == CircuitState.OPEN:
                opened_for = time.monotonic() - (self._opened_at or 0)
                raise CircuitOpenError(self._resource, opened_for)

            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_in_flight >= self._half_open_max:
                    # Đầy slot test — fail-fast như OPEN
                    opened_for = time.monotonic() - (self._opened_at or 0)
                    raise CircuitOpenError(self._resource, opened_for)
                self._half_open_in_flight += 1

            self._total_calls += 1

        try:
            result = await fn()
        except BaseException:
            await self._signal_failure()
            raise
        else:
            await self._signal_success()
            return result

    async def signal_success(self) -> None:
        """External signal — manual call cho ai không dùng `call()`."""
        await self._signal_success()

    async def signal_failure(self) -> None:
        """External signal failure."""
        await self._signal_failure()

    async def reset(self) -> None:
        """Force reset về CLOSED + zero counters."""
        async with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._opened_at = None
            self._half_open_successes = 0
            self._half_open_failures = 0
            self._half_open_in_flight = 0
            self._log.info("circuit_reset")

    # ====================================================================
    # Internal — state transitions
    # ====================================================================

    async def _signal_success(self) -> None:
        async with self._lock:
            self._consecutive_failures = 0
            if self._state == CircuitState.HALF_OPEN:
                self._half_open_successes += 1
                self._half_open_in_flight = max(
                    0, self._half_open_in_flight - 1,
                )
                if self._half_open_successes >= self._success_threshold:
                    self._transit_to_closed()

    async def _signal_failure(self) -> None:
        async with self._lock:
            self._total_failures += 1
            self._consecutive_failures += 1
            if self._state == CircuitState.HALF_OPEN:
                self._half_open_failures += 1
                self._half_open_in_flight = max(
                    0, self._half_open_in_flight - 1,
                )
                # Bất kỳ fail nào trong HALF_OPEN → OPEN ngay
                self._transit_to_open()
            elif self._state == CircuitState.CLOSED:
                if self._consecutive_failures >= self._failure_threshold:
                    self._transit_to_open()

    async def _maybe_transit_to_half_open(self) -> None:
        """Check timer + transit OPEN → HALF_OPEN nếu đủ recovery_timeout."""
        async with self._lock:
            if self._state != CircuitState.OPEN:
                return
            if self._opened_at is None:
                return
            if time.monotonic() - self._opened_at >= self._recovery_timeout:
                self._transit_to_half_open()

    def _transit_to_open(self) -> None:
        """Lock đã được giữ bởi caller."""
        previous = self._state
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
        self._half_open_successes = 0
        self._half_open_failures = 0
        self._half_open_in_flight = 0
        self._log.warning(
            "circuit_opened",
            from_state=previous.value,
            consecutive_failures=self._consecutive_failures,
        )

    def _transit_to_half_open(self) -> None:
        previous = self._state
        self._state = CircuitState.HALF_OPEN
        self._half_open_successes = 0
        self._half_open_failures = 0
        self._half_open_in_flight = 0
        self._log.info(
            "circuit_half_open",
            from_state=previous.value,
        )

    def _transit_to_closed(self) -> None:
        previous = self._state
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = None
        self._half_open_successes = 0
        self._half_open_failures = 0
        self._half_open_in_flight = 0
        self._log.info(
            "circuit_closed",
            from_state=previous.value,
        )
