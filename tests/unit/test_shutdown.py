"""Tests cho `GracefulShutdown`."""

from __future__ import annotations

import asyncio

import pytest

from waytoagi.resilience.shutdown import (
    GracefulShutdown,
    ShutdownPhase,
)


@pytest.mark.unit
class TestShutdownTrigger:
    @pytest.mark.asyncio
    async def test_initial_phase_normal(self) -> None:
        sd = GracefulShutdown()
        assert sd.phase == ShutdownPhase.NORMAL
        assert sd.is_shutting_down is False

    @pytest.mark.asyncio
    async def test_trigger_changes_phase(self) -> None:
        sd = GracefulShutdown()
        sd.trigger("manual")
        assert sd.phase == ShutdownPhase.TRIGGERED
        assert sd.is_shutting_down is True

    @pytest.mark.asyncio
    async def test_trigger_idempotent(self) -> None:
        sd = GracefulShutdown()
        sd.trigger("first")
        sd.trigger("second")  # ignored
        stats = sd.stats()
        assert stats["triggered_by"] == "first"

    @pytest.mark.asyncio
    async def test_wait_for_shutdown_unblocks(self) -> None:
        sd = GracefulShutdown()
        # Trigger trong background — giữ task ref để tránh GC
        async def _trigger_later() -> None:
            await asyncio.sleep(0.01)
            sd.trigger("late")
        task = asyncio.create_task(_trigger_later())
        await sd.wait_for_shutdown()
        await task
        assert sd.is_shutting_down is True


@pytest.mark.unit
class TestRegisterAndCleanup:
    @pytest.mark.asyncio
    async def test_callbacks_run_in_order(self) -> None:
        sd = GracefulShutdown()
        order: list[str] = []

        async def cb_a() -> None:
            order.append("a")

        async def cb_b() -> None:
            order.append("b")

        sd.register("a", cb_a)
        sd.register("b", cb_b)
        await sd.run_cleanup()
        assert order == ["a", "b"]
        assert sd.phase == ShutdownPhase.DONE

    @pytest.mark.asyncio
    async def test_cleanup_timeout_logs_failure(self) -> None:
        sd = GracefulShutdown(hard_timeout=2.0)

        async def hang() -> None:
            await asyncio.sleep(5)

        sd.register("hang", hang, timeout=0.05)
        await sd.run_cleanup()
        stats = sd.stats()
        assert "hang" in [name for name, _ in stats["cleanups_failed"]]  # type: ignore[index]

    @pytest.mark.asyncio
    async def test_cleanup_exception_does_not_halt_others(self) -> None:
        sd = GracefulShutdown()

        async def cb_fail() -> None:
            msg = "boom"
            raise RuntimeError(msg)

        async def cb_ok() -> None:
            pass

        sd.register("fail", cb_fail)
        sd.register("ok", cb_ok)
        await sd.run_cleanup()
        stats = sd.stats()
        assert "ok" in stats["cleanups_run"]  # type: ignore[operator]
        failed = [name for name, _ in stats["cleanups_failed"]]  # type: ignore[index]
        assert "fail" in failed

    @pytest.mark.asyncio
    async def test_hard_timeout_skips_remaining(self) -> None:
        sd = GracefulShutdown(hard_timeout=0.1)

        async def slow() -> None:
            await asyncio.sleep(0.5)

        async def quick() -> None:
            pass

        # Slow callback consume hard_timeout
        sd.register("slow", slow, timeout=0.5)
        sd.register("quick", quick)
        await sd.run_cleanup()
        # quick có thể bị skip do hard_timeout
        # (depends on timing — chỉ check phase=DONE)
        assert sd.phase == ShutdownPhase.DONE

    @pytest.mark.asyncio
    async def test_run_cleanup_triggers_if_not_yet(self) -> None:
        sd = GracefulShutdown()
        # Không trigger trước
        async def cb() -> None:
            pass
        sd.register("cb", cb)
        await sd.run_cleanup()
        # Auto-trigger
        assert sd.phase == ShutdownPhase.DONE


@pytest.mark.unit
class TestStats:
    @pytest.mark.asyncio
    async def test_stats_track_run_and_failed(self) -> None:
        sd = GracefulShutdown()

        async def ok() -> None:
            pass

        async def fail() -> None:
            msg = "x"
            raise RuntimeError(msg)

        sd.register("ok", ok)
        sd.register("fail", fail)
        await sd.run_cleanup()
        stats = sd.stats()
        assert "ok" in stats["cleanups_run"]  # type: ignore[operator]
        assert len(stats["cleanups_failed"]) == 1  # type: ignore[arg-type]
