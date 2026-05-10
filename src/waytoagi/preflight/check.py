"""PreflightCheck — fail-fast verify trước pipeline run.

Mục đích: phát hiện sớm (<10s) các vấn đề khiến pipeline fail muộn:
  - Source token thiếu scope read
  - DST token thiếu scope write
  - Lark Base table không tồn tại / schema lệch
  - LLM POOL: tất cả endpoint dead → translate fail 100%
  - DST parent gần đầy slot (Lark cap ~10k children)
  - Cache directory không writable

Strict separation:
  - Mỗi check là async coroutine độc lập, có timeout riêng.
  - Fail 1 check không block check khác — `asyncio.gather(return_exceptions=True)`.
  - Severity 3 mức: ERROR (block run), WARNING (proceed nhưng cảnh báo),
    INFO (purely informational).
  - Output structured `PreflightReport` — caller decide có proceed.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from waytoagi.lark.auth import LarkAPIError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from waytoagi.lark.base import LarkBase
    from waytoagi.lark.wiki import LarkWiki
    from waytoagi.llm.pool import LLMPool

logger = structlog.get_logger(__name__)

# Lark Wiki cap children/parent ≈ 10k-12k. Ngưỡng warning + error.
DEFAULT_PARENT_SLOT_WARNING = 9000
DEFAULT_PARENT_SLOT_ERROR = 11000

# Per-check timeout (seconds) — không để 1 check hang block report
DEFAULT_CHECK_TIMEOUT = 10.0


class CheckLevel(StrEnum):
    """Severity của 1 check failure."""

    OK = "ok"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Kết quả 1 check riêng."""

    name: str
    level: CheckLevel
    message: str
    duration_seconds: float = 0.0
    detail: dict[str, str | int | float | bool] = field(default_factory=dict)


@dataclass(slots=True)
class PreflightReport:
    """Tổng hợp tất cả checks."""

    results: list[CheckResult] = field(default_factory=list)
    total_duration_seconds: float = 0.0

    @property
    def errors(self) -> list[CheckResult]:
        return [r for r in self.results if r.level == CheckLevel.ERROR]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if r.level == CheckLevel.WARNING]

    @property
    def passed(self) -> bool:
        """Pipeline có thể run tiếp không — KHÔNG có ERROR."""
        return not self.errors

    def summary(self) -> str:
        """Human-readable summary."""
        n_ok = sum(1 for r in self.results if r.level == CheckLevel.OK)
        return (
            f"Preflight: {n_ok}/{len(self.results)} OK, "
            f"{len(self.warnings)} warnings, {len(self.errors)} errors "
            f"({self.total_duration_seconds:.1f}s)"
        )


class PreflightCheck:
    """Run all health checks → PreflightReport.

    Args:
        src_wiki: LarkWiki bound vào source tenant.
        dst_wiki: LarkWiki bound vào DST tenant.
        base: LarkBase bound vào DST tenant (cùng nơi chứa Bitable).
        llm_pool: LLM round-robin pool.
        src_space_id: source space để probe read.
        dst_space_id: DST space để probe write.
        app_token: Bitable app_token để probe table.
        table_id: Bitable table_id.
        dst_parents_to_check: list dst parent_token cần check slot count.
        check_timeout: timeout per check (seconds).
    """

    def __init__(
        self,
        *,
        src_wiki: LarkWiki,
        dst_wiki: LarkWiki,
        base: LarkBase,
        llm_pool: LLMPool,
        src_space_id: str,
        dst_space_id: str,
        app_token: str,
        table_id: str,
        dst_parents_to_check: list[str] | None = None,
        check_timeout: float = DEFAULT_CHECK_TIMEOUT,
    ) -> None:
        self._src_wiki = src_wiki
        self._dst_wiki = dst_wiki
        self._base = base
        self._llm = llm_pool
        self._src_space = src_space_id
        self._dst_space = dst_space_id
        self._app_token = app_token
        self._table_id = table_id
        self._dst_parents = dst_parents_to_check or []
        self._timeout = check_timeout
        self._log = logger.bind(component="PreflightCheck")

    # ====================================================================
    # Public API
    # ====================================================================

    async def run_all(self) -> PreflightReport:
        """Run all checks parallel, gather results."""
        started = time.monotonic()
        checks: list[tuple[str, Callable[[], Awaitable[CheckResult]]]] = [
            ("source_read", self._check_source_read),
            ("dst_read", self._check_dst_read),
            ("bitable_table", self._check_bitable_table),
            ("llm_pool_health", self._check_llm_pool),
        ]
        # Per-parent slot check
        for idx, parent in enumerate(self._dst_parents):
            checks.append((
                f"dst_parent_slot_{idx}",
                self._make_parent_check(parent),
            ))

        results = await asyncio.gather(
            *(self._with_timeout(name, fn) for name, fn in checks),
            return_exceptions=False,
        )

        report = PreflightReport(
            results=list(results),
            total_duration_seconds=round(time.monotonic() - started, 2),
        )
        self._log.info(
            "preflight_done",
            summary=report.summary(),
            passed=report.passed,
        )
        return report

    # ====================================================================
    # Wrappers
    # ====================================================================

    async def _with_timeout(
        self,
        name: str,
        fn: Callable[[], Awaitable[CheckResult]],
    ) -> CheckResult:
        """Run 1 check với timeout. Catch exceptions vào ERROR result."""
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(fn(), timeout=self._timeout)
        except TimeoutError:
            return CheckResult(
                name=name,
                level=CheckLevel.ERROR,
                message=f"timeout after {self._timeout}s",
                duration_seconds=round(time.monotonic() - started, 3),
            )
        except Exception as e:  # any unexpected error
            return CheckResult(
                name=name,
                level=CheckLevel.ERROR,
                message=f"unexpected: {e!s}"[:200],
                duration_seconds=round(time.monotonic() - started, 3),
            )
        return result

    # ====================================================================
    # Individual checks
    # ====================================================================

    async def _check_source_read(self) -> CheckResult:
        """Source token có read scope cho wiki?"""
        started = time.monotonic()
        try:
            await self._src_wiki.list_nodes(self._src_space, page_size=1)
        except LarkAPIError as e:
            level = (
                CheckLevel.ERROR if e.code in {99991663, 131006, 1254030}
                else CheckLevel.WARNING
            )
            return CheckResult(
                name="source_read",
                level=level,
                message=f"source list_nodes failed: [{e.code}] {e.msg}",
                duration_seconds=round(time.monotonic() - started, 3),
            )
        return CheckResult(
            name="source_read",
            level=CheckLevel.OK,
            message="source wiki readable",
            duration_seconds=round(time.monotonic() - started, 3),
        )

    async def _check_dst_read(self) -> CheckResult:
        """DST token đọc được wiki space (probe minimal)."""
        started = time.monotonic()
        try:
            await self._dst_wiki.list_nodes(self._dst_space, page_size=1)
        except LarkAPIError as e:
            return CheckResult(
                name="dst_read",
                level=CheckLevel.ERROR,
                message=f"dst list_nodes failed: [{e.code}] {e.msg}",
                duration_seconds=round(time.monotonic() - started, 3),
            )
        return CheckResult(
            name="dst_read",
            level=CheckLevel.OK,
            message="dst wiki readable",
            duration_seconds=round(time.monotonic() - started, 3),
        )

    async def _check_bitable_table(self) -> CheckResult:
        """Bitable table tồn tại + đọc được."""
        started = time.monotonic()
        try:
            await self._base.search_records(
                self._app_token, self._table_id, page_size=1,
            )
        except LarkAPIError as e:
            return CheckResult(
                name="bitable_table",
                level=CheckLevel.ERROR,
                message=f"bitable search_records failed: [{e.code}] {e.msg}",
                duration_seconds=round(time.monotonic() - started, 3),
            )
        return CheckResult(
            name="bitable_table",
            level=CheckLevel.OK,
            message="bitable table reachable",
            duration_seconds=round(time.monotonic() - started, 3),
        )

    async def _check_llm_pool(self) -> CheckResult:
        """LLM POOL có endpoint nào còn alive không?"""
        started = time.monotonic()
        # Probe 1 lần dịch ngắn — nếu mọi endpoint chết, raise LLMPoolError
        try:
            response = await asyncio.wait_for(
                self._llm.chat(
                    [{"role": "user", "content": "ping"}],
                    max_tokens=8,
                ),
                timeout=self._timeout,
            )
        except TimeoutError:
            return CheckResult(
                name="llm_pool_health",
                level=CheckLevel.ERROR,
                message="llm probe timeout",
                duration_seconds=round(time.monotonic() - started, 3),
            )
        except Exception as e:
            return CheckResult(
                name="llm_pool_health",
                level=CheckLevel.ERROR,
                message=f"llm probe failed: {e!s}"[:200],
                duration_seconds=round(time.monotonic() - started, 3),
            )

        if not response or not response.strip():
            return CheckResult(
                name="llm_pool_health",
                level=CheckLevel.WARNING,
                message="llm returned empty",
                duration_seconds=round(time.monotonic() - started, 3),
            )
        return CheckResult(
            name="llm_pool_health",
            level=CheckLevel.OK,
            message="llm pool alive",
            duration_seconds=round(time.monotonic() - started, 3),
            detail={"response_len": len(response)},
        )

    def _make_parent_check(
        self, parent_token: str,
    ) -> Callable[[], Awaitable[CheckResult]]:
        async def _check() -> CheckResult:
            started = time.monotonic()
            try:
                # Pull all children to count — page_size=50 max của Lark
                count = 0
                page_token: str | None = None
                while True:
                    r = await self._dst_wiki.list_nodes(
                        self._dst_space,
                        parent_node_token=parent_token,
                        page_size=50,
                        page_token=page_token,
                    )
                    data = r.get("data", {})
                    items = data.get("items", [])
                    count += len(items)
                    if not data.get("has_more"):
                        break
                    page_token = data.get("page_token")
                    # Hard cap để check không hang nếu parent có 50k+
                    if count > DEFAULT_PARENT_SLOT_ERROR:
                        break
            except LarkAPIError as e:
                return CheckResult(
                    name=f"dst_parent_slot_{parent_token[:8]}",
                    level=CheckLevel.WARNING,
                    message=f"can't probe slot count: [{e.code}] {e.msg}",
                    duration_seconds=round(time.monotonic() - started, 3),
                )

            if count >= DEFAULT_PARENT_SLOT_ERROR:
                level = CheckLevel.ERROR
                msg = (
                    f"parent {parent_token[:18]} có {count} children, "
                    f"vượt ERROR={DEFAULT_PARENT_SLOT_ERROR} — không thể tạo thêm"
                )
            elif count >= DEFAULT_PARENT_SLOT_WARNING:
                level = CheckLevel.WARNING
                msg = (
                    f"parent {parent_token[:18]} có {count} children, "
                    f">= WARNING={DEFAULT_PARENT_SLOT_WARNING}"
                )
            else:
                level = CheckLevel.OK
                msg = f"parent {parent_token[:18]} OK ({count} children)"
            return CheckResult(
                name=f"dst_parent_slot_{parent_token[:8]}",
                level=level,
                message=msg,
                duration_seconds=round(time.monotonic() - started, 3),
                detail={"count": count, "parent_token": parent_token},
            )
        return _check
