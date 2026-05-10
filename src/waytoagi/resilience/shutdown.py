"""GracefulShutdown — SIGTERM/SIGINT handler + cleanup callbacks.

Khi receive signal:
  1. Set flag `is_shutting_down=True` để tasks check và exit smoothly.
  2. Run cleanup callbacks theo thứ tự đăng ký (FIFO).
  3. Mỗi callback có timeout riêng — không hang shutdown.
  4. Sau timeout cứng (`hard_timeout`) thì exit dù callback chưa xong.

Phases:
  - **TRIGGERED**: signal received, đã start cleanup.
  - **DRAINING**: callbacks đang chạy.
  - **DONE**: tất cả callbacks finished hoặc timeout.

Usage:
    shutdown = GracefulShutdown()
    shutdown.install_signal_handlers()

    shutdown.register("flush_queue", queue.aclose, timeout=5.0)
    shutdown.register("save_checkpoint", ckpt.mark_complete, timeout=2.0)

    while not shutdown.is_shutting_down:
        await process_one_batch()

    await shutdown.run_cleanup()
"""

from __future__ import annotations

import asyncio
import signal
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = structlog.get_logger(__name__)


class ShutdownPhase(StrEnum):
    """Phase trong shutdown lifecycle."""

    NORMAL = "normal"
    TRIGGERED = "triggered"
    DRAINING = "draining"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class CleanupTask:
    """1 callback đăng ký để chạy lúc shutdown."""

    name: str
    callback: Callable[[], Awaitable[None]]
    timeout: float = 10.0
    """Max seconds chờ callback. Hết giờ → cancel + log."""


@dataclass(slots=True)
class _ShutdownState:
    """Mutable state — không expose direct."""

    phase: ShutdownPhase = ShutdownPhase.NORMAL
    triggered_at: float | None = None
    triggered_by: str = ""
    cleanups_run: list[str] = field(default_factory=list)
    cleanups_failed: list[tuple[str, str]] = field(default_factory=list)


class GracefulShutdown:
    """Coordinator cho graceful shutdown.

    Args:
        hard_timeout: max total seconds cho phase DRAINING. Sau đó force
            exit dù còn callback chưa xong.
        signals: list signal numbers để catch. Default SIGINT + SIGTERM.

    Thread-safety: callbacks chạy trong async loop, không cần lock.
    Signal handler chỉ set flag — không gọi async trực tiếp.
    """

    def __init__(
        self,
        *,
        hard_timeout: float = 30.0,
        signals: tuple[int, ...] | None = None,
    ) -> None:
        self._hard_timeout = hard_timeout
        self._signals = signals or (signal.SIGINT, signal.SIGTERM)
        self._tasks: list[CleanupTask] = []
        self._state = _ShutdownState()
        self._shutdown_event = asyncio.Event()
        self._installed = False
        self._log = logger.bind(component="GracefulShutdown")

    # ====================================================================
    # Public API
    # ====================================================================

    @property
    def is_shutting_down(self) -> bool:
        return self._state.phase != ShutdownPhase.NORMAL

    @property
    def phase(self) -> ShutdownPhase:
        return self._state.phase

    def register(
        self,
        name: str,
        callback: Callable[[], Awaitable[None]],
        *,
        timeout: float = 10.0,
    ) -> None:
        """Đăng ký 1 cleanup callback. FIFO order khi run_cleanup."""
        self._tasks.append(CleanupTask(
            name=name, callback=callback, timeout=timeout,
        ))

    def install_signal_handlers(self) -> None:
        """Hook SIGINT/SIGTERM → trigger shutdown.

        Idempotent: gọi nhiều lần OK. Gọi 1 lần đủ cho process lifecycle.
        """
        if self._installed:
            return
        loop = asyncio.get_event_loop()
        for sig in self._signals:
            try:
                loop.add_signal_handler(
                    sig, self._make_signal_handler(sig),
                )
            except NotImplementedError:
                # Windows không support add_signal_handler cho async loop
                # — fallback dùng signal.signal trực tiếp
                signal.signal(sig, self._make_sync_signal_handler(sig))
        self._installed = True
        self._log.info("shutdown_handlers_installed", signals=[
            signal.Signals(s).name for s in self._signals
        ])

    def _make_signal_handler(
        self, sig: int,
    ) -> Callable[[], None]:
        """Closure factory — đóng gói sig number để tránh B023."""
        sig_name = signal.Signals(sig).name

        def _handler() -> None:
            self.trigger(f"signal_{sig_name}")

        return _handler

    def _make_sync_signal_handler(
        self, sig: int,
    ) -> Callable[[int, object | None], None]:
        """Sync variant cho signal.signal() (Windows fallback)."""
        sig_name = signal.Signals(sig).name

        def _handler(_signum: int, _frame: object | None) -> None:
            self.trigger(f"signal_{sig_name}")

        return _handler

    def trigger(self, reason: str) -> None:
        """Manually trigger shutdown. Idempotent (chỉ trigger 1 lần)."""
        if self._state.phase != ShutdownPhase.NORMAL:
            return
        self._state.phase = ShutdownPhase.TRIGGERED
        self._state.triggered_at = time.monotonic()
        self._state.triggered_by = reason
        self._shutdown_event.set()
        self._log.warning("shutdown_triggered", reason=reason)

    async def wait_for_shutdown(self) -> None:
        """Block đến khi shutdown trigger."""
        await self._shutdown_event.wait()

    async def run_cleanup(self) -> None:
        """Run tất cả cleanup callbacks theo thứ tự đăng ký.

        Mỗi callback có timeout riêng. Tổng thời gian không vượt
        `hard_timeout`.
        """
        if self._state.phase == ShutdownPhase.NORMAL:
            self.trigger("manual_run_cleanup")

        self._state.phase = ShutdownPhase.DRAINING
        started = time.monotonic()
        deadline = started + self._hard_timeout

        for task in self._tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._log.warning(
                    "shutdown_hard_timeout",
                    skipped_remaining=len(self._tasks)
                    - len(self._state.cleanups_run)
                    - len(self._state.cleanups_failed),
                )
                break

            timeout = min(task.timeout, remaining)
            try:
                await asyncio.wait_for(task.callback(), timeout=timeout)
                self._state.cleanups_run.append(task.name)
                self._log.info("cleanup_done", name=task.name)
            except TimeoutError:
                self._state.cleanups_failed.append(
                    (task.name, f"timeout after {timeout:.1f}s"),
                )
                self._log.warning(
                    "cleanup_timeout", name=task.name, timeout=timeout,
                )
            except Exception as e:
                self._state.cleanups_failed.append((task.name, str(e)[:200]))
                self._log.warning(
                    "cleanup_error", name=task.name, err=str(e)[:120],
                )

        self._state.phase = ShutdownPhase.DONE
        self._log.info(
            "shutdown_complete",
            cleanups_run=len(self._state.cleanups_run),
            cleanups_failed=len(self._state.cleanups_failed),
            duration=round(time.monotonic() - started, 2),
        )

    def stats(self) -> dict[str, object]:
        """Snapshot state for testing / dashboard."""
        return {
            "phase": self._state.phase.value,
            "triggered_by": self._state.triggered_by,
            "cleanups_run": list(self._state.cleanups_run),
            "cleanups_failed": list(self._state.cleanups_failed),
        }
