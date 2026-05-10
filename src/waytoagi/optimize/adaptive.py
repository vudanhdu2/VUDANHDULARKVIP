"""AdaptiveConcurrency — tự scale workers theo rate-limit feedback.

V1 problem: workers fixed = 4 → khi gặp 99991400 (Lark rate-limit) thì
fail liên tục, retry vô ích vì vẫn pump cùng tốc độ.

V2 solution:
  - Track rolling window N feedback signals (success/rate-limited/error).
  - Tăng workers khi window không có rate-limit.
  - Giảm workers ngay khi có rate-limit > threshold.
  - Bound: [min_workers, max_workers].

Thread/async-safe qua asyncio.Lock. Sử dụng như semaphore dynamic:
    adapt = AdaptiveConcurrency(initial=2, min_workers=1, max_workers=8)
    async with adapt.slot():
        try:
            await call_lark_api()
            adapt.signal_ok()
        except RateLimitError:
            adapt.signal_rate_limited()
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = structlog.get_logger(__name__)


class ConcurrencySignal(StrEnum):
    """Phản hồi từ caller về kết quả 1 request."""

    OK = "ok"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"  # non-rate-limit failure (network, 5xx, etc.)


@dataclass(frozen=True, slots=True)
class AdaptiveStats:
    """Snapshot statistics — read-only view."""

    current_workers: int
    window_size: int
    ok_count: int
    rate_limited_count: int
    error_count: int
    scale_up_count: int = 0
    scale_down_count: int = 0

    @property
    def rate_limited_pct(self) -> float:
        if self.window_size == 0:
            return 0.0
        return self.rate_limited_count / self.window_size


class AdaptiveConcurrency:
    """Dynamic semaphore scaling theo rate-limit feedback.

    Args:
        initial: số workers ban đầu.
        min_workers: floor (luôn giữ tối thiểu).
        max_workers: ceiling.
        window_size: rolling window N signals gần nhất để tính %.
        scale_up_threshold: rate-limited% < ngưỡng này trong window đầy
            → tăng workers (default 0.0 = không có rate-limit).
        scale_down_threshold: rate-limited% >= ngưỡng → giảm workers
            (default 0.15 = 15% calls bị rate-limit).
        cooldown_signals: sau scale, đợi N signal mới scale lại để tránh
            oscillation.
    """

    def __init__(
        self,
        *,
        initial: int = 2,
        min_workers: int = 1,
        max_workers: int = 8,
        window_size: int = 20,
        scale_up_threshold: float = 0.0,
        scale_down_threshold: float = 0.15,
        cooldown_signals: int = 10,
    ) -> None:
        if initial < min_workers or initial > max_workers:
            msg = f"initial={initial} ngoài [{min_workers}, {max_workers}]"
            raise ValueError(msg)
        if scale_down_threshold <= scale_up_threshold:
            msg = "scale_down_threshold phải > scale_up_threshold"
            raise ValueError(msg)

        self._min = min_workers
        self._max = max_workers
        self._window: deque[ConcurrencySignal] = deque(maxlen=window_size)
        self._window_size = window_size
        self._scale_up_th = scale_up_threshold
        self._scale_down_th = scale_down_threshold
        self._cooldown = cooldown_signals
        self._signals_since_scale = 0
        self._scale_up_count = 0
        self._scale_down_count = 0

        self._current = initial
        self._sem = asyncio.Semaphore(initial)
        self._lock = asyncio.Lock()
        self._log = logger.bind(component="AdaptiveConcurrency")
        # Background tasks giữ reference để GC không thu hồi sớm
        self._bg_tasks: set[asyncio.Task[None]] = set()

    # ====================================================================
    # Public API
    # ====================================================================

    @property
    def current_workers(self) -> int:
        return self._current

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Acquire 1 slot từ semaphore. Caller PHẢI gọi signal_* sau đó."""
        await self._sem.acquire()
        try:
            yield
        finally:
            self._sem.release()

    async def signal(self, sig: ConcurrencySignal) -> None:
        """Push 1 signal vào window + maybe scale."""
        async with self._lock:
            self._window.append(sig)
            self._signals_since_scale += 1
            await self._maybe_scale()

    async def signal_ok(self) -> None:
        await self.signal(ConcurrencySignal.OK)

    async def signal_rate_limited(self) -> None:
        await self.signal(ConcurrencySignal.RATE_LIMITED)

    async def signal_error(self) -> None:
        await self.signal(ConcurrencySignal.ERROR)

    def stats(self) -> AdaptiveStats:
        """Snapshot stats — không lock (best-effort read)."""
        ok = sum(1 for s in self._window if s == ConcurrencySignal.OK)
        rl = sum(1 for s in self._window if s == ConcurrencySignal.RATE_LIMITED)
        err = sum(1 for s in self._window if s == ConcurrencySignal.ERROR)
        return AdaptiveStats(
            current_workers=self._current,
            window_size=len(self._window),
            ok_count=ok,
            rate_limited_count=rl,
            error_count=err,
            scale_up_count=self._scale_up_count,
            scale_down_count=self._scale_down_count,
        )

    # ====================================================================
    # Internal
    # ====================================================================

    async def _maybe_scale(self) -> None:
        """Decide scale up/down dựa trên window."""
        # Cooldown — chỉ scale sau N signals
        if self._signals_since_scale < self._cooldown:
            return
        # Window phải đầy mới có ý nghĩa thống kê
        if len(self._window) < self._window_size:
            return

        rl_count = sum(
            1 for s in self._window if s == ConcurrencySignal.RATE_LIMITED
        )
        rl_pct = rl_count / len(self._window)

        if rl_pct >= self._scale_down_th and self._current > self._min:
            await self._scale_down(rl_pct)
        elif rl_pct <= self._scale_up_th and self._current < self._max:
            await self._scale_up(rl_pct)

    async def _scale_up(self, rl_pct: float) -> None:
        """Tăng 1 worker → release thêm 1 permit semaphore."""
        old = self._current
        self._current += 1
        self._sem.release()  # add 1 permit
        self._signals_since_scale = 0
        self._scale_up_count += 1
        self._log.info(
            "scale_up", from_workers=old, to_workers=self._current,
            rate_limited_pct=round(rl_pct, 3),
        )

    async def _scale_down(self, rl_pct: float) -> None:
        """Giảm 1 worker → acquire 1 permit (giữ luôn → giảm capacity)."""
        old = self._current
        # Try non-blocking acquire để giảm capacity. Nếu sem đang full
        # waiting, sẽ tốn 1 slot → workers thực giảm 1.
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=0.001)
        except TimeoutError:
            # Tất cả slot đang dùng → đặt cờ "phantom" giảm bằng cách
            # giữ permit khi worker tới release. Ở đây, đơn giản thôi:
            # giảm counter, lần release tiếp theo sẽ giữ lại bằng cách
            # acquire lại trong background.
            task = asyncio.create_task(self._delayed_acquire())
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        self._current -= 1
        self._signals_since_scale = 0
        self._scale_down_count += 1
        self._log.warning(
            "scale_down", from_workers=old, to_workers=self._current,
            rate_limited_pct=round(rl_pct, 3),
        )

    async def _delayed_acquire(self) -> None:
        """Khi sem đang full, await acquire để giảm capacity dần."""
        await self._sem.acquire()
        # Permit này KHÔNG release — đó là cách "giảm" capacity.
