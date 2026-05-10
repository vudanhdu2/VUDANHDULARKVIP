"""QuotaTracker — sliding window per resource, proactive throttle.

Track API call timestamps per resource. Trước khi gọi API, hỏi tracker:
"Còn bao lâu nữa thì hit cap?". Nếu < ngưỡng → tự sleep proactive thay
vì để API trả 99991400.

Resources tracked:
  - lark_base_writes (50 req/s cap default)
  - lark_wiki_reads (100 req/s cap default)
  - lark_docx_reads (100 req/s cap default)
  - llm_endpoint_<name> (per endpoint quota)

Async-safe via asyncio.Lock per resource.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from enum import StrEnum

import structlog

logger = structlog.get_logger(__name__)


class QuotaResource(StrEnum):
    """Standard resource names — caller có thể tự định nghĩa thêm."""

    LARK_BASE_WRITES = "lark_base_writes"
    LARK_BASE_READS = "lark_base_reads"
    LARK_WIKI_READS = "lark_wiki_reads"
    LARK_WIKI_WRITES = "lark_wiki_writes"
    LARK_DOCX_READS = "lark_docx_reads"
    LARK_DOCX_WRITES = "lark_docx_writes"
    LARK_DRIVE_UPLOADS = "lark_drive_uploads"
    LLM_DEFAULT = "llm_default"


@dataclass(frozen=True, slots=True)
class QuotaUsage:
    """Snapshot usage cho monitoring/alerting."""

    resource: str
    rps_cap: float
    """Request/second cap."""

    daily_cap: int | None
    """Optional daily cap."""

    rps_used: float
    """Hiện tại x req/sec (sliding window)."""

    daily_used: int
    """Calls trong 24h gần nhất."""

    seconds_until_rps_limit: float
    """0.0 = đã hit. Float lớn = còn nhiều slot."""

    @property
    def rps_pct(self) -> float:
        if self.rps_cap == 0:
            return 0.0
        return self.rps_used / self.rps_cap

    @property
    def daily_pct(self) -> float:
        if not self.daily_cap or self.daily_cap == 0:
            return 0.0
        return self.daily_used / self.daily_cap

    @property
    def near_limit(self) -> bool:
        """True khi >= 80% — alert/throttle threshold."""
        return self.rps_pct >= 0.8 or self.daily_pct >= 0.8


class QuotaTracker:
    """Multi-resource quota tracker với sliding window.

    Args:
        resources: dict resource_name → (rps_cap, daily_cap_or_None).
            Caller có thể add resource bất kỳ qua register_resource().

    Usage:
        tracker = QuotaTracker({
            QuotaResource.LARK_BASE_WRITES: (50, None),
            QuotaResource.LARK_WIKI_READS: (100, None),
            "llm_local": (10, 10000),
        })

        # Trước mỗi API call
        wait = await tracker.predict_seconds_until_limit(
            QuotaResource.LARK_BASE_WRITES,
        )
        if wait > 0:
            await asyncio.sleep(wait)

        # Hoặc dùng auto-throttle
        async with tracker.throttle(QuotaResource.LARK_BASE_WRITES):
            await base.update_record(...)
    """

    # Window size cho RPS calculation — last 1 second
    _RPS_WINDOW_SECONDS = 1.0
    # Window size cho daily — last 24 hours
    _DAILY_WINDOW_SECONDS = 24 * 3600.0

    def __init__(
        self,
        resources: dict[str, tuple[float, int | None]] | None = None,
    ) -> None:
        self._caps: dict[str, tuple[float, int | None]] = {}
        self._timestamps: dict[str, deque[float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._log = logger.bind(component="QuotaTracker")

        if resources:
            for name, (rps, daily) in resources.items():
                self.register_resource(name, rps_cap=rps, daily_cap=daily)

    def register_resource(
        self,
        name: str,
        *,
        rps_cap: float,
        daily_cap: int | None = None,
    ) -> None:
        """Add 1 resource cho tracking."""
        if rps_cap <= 0:
            msg = f"rps_cap phải > 0 (got {rps_cap})"
            raise ValueError(msg)
        self._caps[name] = (rps_cap, daily_cap)
        # Pre-allocate deque size = ~daily_cap để không grow vô tội vạ
        max_size = daily_cap if daily_cap and daily_cap > 0 else 100_000
        self._timestamps[name] = deque(maxlen=max_size)
        self._locks[name] = asyncio.Lock()

    async def record_call(self, resource: str) -> None:
        """Caller gọi sau khi 1 API call hoàn tất (success or fail)."""
        if resource not in self._caps:
            return  # untracked
        async with self._locks[resource]:
            self._timestamps[resource].append(time.monotonic())

    async def predict_seconds_until_limit(
        self,
        resource: str,
    ) -> float:
        """Estimate seconds đến lúc hit cap nếu pump tiếp ở rate hiện tại.

        Returns:
          0.0 nếu chưa near limit (rps_pct < 0.8).
          > 0 nếu cần throttle: seconds đợi đến slot oldest expire.
        """
        if resource not in self._caps:
            return 0.0
        async with self._locks[resource]:
            usage = self._compute_usage_unlocked(resource)
            if usage.rps_used < usage.rps_cap:
                # Còn slot
                return 0.0
            # Đã đầy slot — tính khi nào slot oldest expire
            ts_list = self._timestamps[resource]
            if not ts_list:
                return 0.0
            oldest_in_window = min(ts_list)
            now = time.monotonic()
            wait = oldest_in_window + self._RPS_WINDOW_SECONDS - now
            return max(0.0, wait)

    async def usage(self, resource: str) -> QuotaUsage | None:
        """Snapshot usage. None nếu resource không tracked."""
        if resource not in self._caps:
            return None
        async with self._locks[resource]:
            return self._compute_usage_unlocked(resource)

    def throttle(self, resource: str) -> _ThrottleContext:
        """Async context manager auto-sleep nếu near limit + record_call.

        Usage:
            async with tracker.throttle("lark_base_writes"):
                await base.update_record(...)
        """
        return _ThrottleContext(self, resource)

    # ====================================================================
    # Internal
    # ====================================================================

    def _compute_usage_unlocked(self, resource: str) -> QuotaUsage:
        """Compute usage. Caller must hold lock."""
        rps_cap, daily_cap = self._caps[resource]
        ts_list = self._timestamps[resource]
        now = time.monotonic()

        # Prune timestamps cũ hơn daily window
        cutoff_daily = now - self._DAILY_WINDOW_SECONDS
        while ts_list and ts_list[0] < cutoff_daily:
            ts_list.popleft()

        # Count rps window
        cutoff_rps = now - self._RPS_WINDOW_SECONDS
        rps_count = sum(1 for t in ts_list if t >= cutoff_rps)
        daily_count = len(ts_list)

        # Compute predict
        if rps_count >= rps_cap:
            in_window = [t for t in ts_list if t >= cutoff_rps]
            oldest = min(in_window) if in_window else now
            wait = oldest + self._RPS_WINDOW_SECONDS - now
            seconds_until = max(0.0, wait)
        else:
            seconds_until = 0.0

        return QuotaUsage(
            resource=resource,
            rps_cap=rps_cap,
            daily_cap=daily_cap,
            rps_used=float(rps_count),
            daily_used=daily_count,
            seconds_until_rps_limit=seconds_until,
        )


class _ThrottleContext:
    """Async context manager dùng cho `tracker.throttle(...)`."""

    __slots__ = ("_resource", "_tracker")

    def __init__(self, tracker: QuotaTracker, resource: str) -> None:
        self._tracker = tracker
        self._resource = resource

    async def __aenter__(self) -> None:
        wait = await self._tracker.predict_seconds_until_limit(self._resource)
        if wait > 0:
            await asyncio.sleep(wait)

    async def __aexit__(self, *_exc: object) -> None:
        await self._tracker.record_call(self._resource)
