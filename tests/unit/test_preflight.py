"""Tests cho `PreflightCheck` — fail-fast pre-pipeline verify."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from waytoagi.lark.auth import LarkAPIError
from waytoagi.preflight.check import (
    CheckLevel,
    PreflightCheck,
    PreflightReport,
)


def _mk_wiki(*, list_ok: bool = True, list_error: LarkAPIError | None = None) -> AsyncMock:
    wiki = AsyncMock()
    if list_error:
        wiki.list_nodes = AsyncMock(side_effect=list_error)
    else:
        wiki.list_nodes = AsyncMock(return_value={
            "code": 0,
            "data": {"items": [], "has_more": False},
        })
    return wiki


def _mk_base(*, search_ok: bool = True) -> AsyncMock:
    base = AsyncMock()
    if search_ok:
        base.search_records = AsyncMock(return_value={
            "code": 0, "data": {"items": [], "has_more": False},
        })
    else:
        base.search_records = AsyncMock(
            side_effect=LarkAPIError(1254000, "table not found", "/search"),
        )
    return base


def _mk_llm(*, response: str = "pong") -> AsyncMock:
    llm = AsyncMock()
    llm.chat = AsyncMock(return_value=response)
    return llm


@pytest.mark.unit
class TestPreflightAllOk:
    @pytest.mark.asyncio
    async def test_all_pass(self) -> None:
        check = PreflightCheck(
            src_wiki=_mk_wiki(),
            dst_wiki=_mk_wiki(),
            base=_mk_base(),
            llm_pool=_mk_llm(),
            src_space_id="src",
            dst_space_id="dst",
            app_token="app",
            table_id="tbl",
            check_timeout=2.0,
        )
        report = await check.run_all()
        assert report.passed is True
        assert len(report.errors) == 0
        # 4 base checks (no parent slot checks)
        assert len(report.results) == 4


@pytest.mark.unit
class TestPreflightSourceFailure:
    @pytest.mark.asyncio
    async def test_source_perm_denied_is_error(self) -> None:
        check = PreflightCheck(
            src_wiki=_mk_wiki(
                list_error=LarkAPIError(131006, "perm denied", "/list"),
            ),
            dst_wiki=_mk_wiki(),
            base=_mk_base(),
            llm_pool=_mk_llm(),
            src_space_id="src",
            dst_space_id="dst",
            app_token="app",
            table_id="tbl",
        )
        report = await check.run_all()
        assert report.passed is False
        assert any(
            r.name == "source_read" and r.level == CheckLevel.ERROR
            for r in report.results
        )

    @pytest.mark.asyncio
    async def test_source_other_error_is_warning(self) -> None:
        """Non-perm errors → WARNING (proceed but flag)."""
        check = PreflightCheck(
            src_wiki=_mk_wiki(
                list_error=LarkAPIError(99991400, "rate limit", "/list"),
            ),
            dst_wiki=_mk_wiki(),
            base=_mk_base(),
            llm_pool=_mk_llm(),
            src_space_id="src",
            dst_space_id="dst",
            app_token="app",
            table_id="tbl",
        )
        report = await check.run_all()
        # source_read là WARNING, các check khác OK → vẫn passed=True
        assert report.passed is True
        assert any(
            r.name == "source_read" and r.level == CheckLevel.WARNING
            for r in report.results
        )


@pytest.mark.unit
class TestPreflightDstFailure:
    @pytest.mark.asyncio
    async def test_dst_failure_is_error(self) -> None:
        check = PreflightCheck(
            src_wiki=_mk_wiki(),
            dst_wiki=_mk_wiki(
                list_error=LarkAPIError(131006, "perm", "/list"),
            ),
            base=_mk_base(),
            llm_pool=_mk_llm(),
            src_space_id="src",
            dst_space_id="dst",
            app_token="app",
            table_id="tbl",
        )
        report = await check.run_all()
        assert report.passed is False


@pytest.mark.unit
class TestPreflightBitable:
    @pytest.mark.asyncio
    async def test_bitable_table_missing_is_error(self) -> None:
        check = PreflightCheck(
            src_wiki=_mk_wiki(),
            dst_wiki=_mk_wiki(),
            base=_mk_base(search_ok=False),
            llm_pool=_mk_llm(),
            src_space_id="src",
            dst_space_id="dst",
            app_token="app",
            table_id="bad-tbl",
        )
        report = await check.run_all()
        assert report.passed is False


@pytest.mark.unit
class TestPreflightLLM:
    @pytest.mark.asyncio
    async def test_llm_dead_is_error(self) -> None:
        llm = AsyncMock()
        llm.chat = AsyncMock(side_effect=RuntimeError("connection refused"))
        check = PreflightCheck(
            src_wiki=_mk_wiki(),
            dst_wiki=_mk_wiki(),
            base=_mk_base(),
            llm_pool=llm,
            src_space_id="src",
            dst_space_id="dst",
            app_token="app",
            table_id="tbl",
        )
        report = await check.run_all()
        assert report.passed is False
        assert any(
            r.name == "llm_pool_health" and r.level == CheckLevel.ERROR
            for r in report.results
        )

    @pytest.mark.asyncio
    async def test_llm_empty_response_is_warning(self) -> None:
        check = PreflightCheck(
            src_wiki=_mk_wiki(),
            dst_wiki=_mk_wiki(),
            base=_mk_base(),
            llm_pool=_mk_llm(response=""),
            src_space_id="src",
            dst_space_id="dst",
            app_token="app",
            table_id="tbl",
        )
        report = await check.run_all()
        # Empty là WARNING — không block run
        assert any(
            r.name == "llm_pool_health" and r.level == CheckLevel.WARNING
            for r in report.results
        )


@pytest.mark.unit
class TestPreflightParentSlot:
    @pytest.mark.asyncio
    async def test_parent_with_few_children_ok(self) -> None:
        wiki = AsyncMock()
        wiki.list_nodes = AsyncMock(return_value={
            "code": 0, "data": {
                "items": [{"node_token": f"c{i}"} for i in range(5)],
                "has_more": False,
            },
        })
        check = PreflightCheck(
            src_wiki=_mk_wiki(),
            dst_wiki=wiki,
            base=_mk_base(),
            llm_pool=_mk_llm(),
            src_space_id="src",
            dst_space_id="dst",
            app_token="app",
            table_id="tbl",
            dst_parents_to_check=["parent-x"],
        )
        report = await check.run_all()
        slot_check = next(
            r for r in report.results if r.name.startswith("dst_parent_slot")
        )
        assert slot_check.level == CheckLevel.OK


@pytest.mark.unit
class TestPreflightTimeout:
    @pytest.mark.asyncio
    async def test_check_timeout_returns_error(self) -> None:
        """Check hang quá timeout → ERROR + name preserved."""
        import asyncio

        slow_wiki = AsyncMock()

        async def slow_list(*_args: object, **_kwargs: object) -> dict[str, object]:
            await asyncio.sleep(5)
            return {"code": 0, "data": {"items": []}}

        slow_wiki.list_nodes = AsyncMock(side_effect=slow_list)
        check = PreflightCheck(
            src_wiki=slow_wiki,
            dst_wiki=_mk_wiki(),
            base=_mk_base(),
            llm_pool=_mk_llm(),
            src_space_id="src",
            dst_space_id="dst",
            app_token="app",
            table_id="tbl",
            check_timeout=0.1,
        )
        report = await check.run_all()
        source_check = next(
            r for r in report.results if r.name == "source_read"
        )
        assert source_check.level == CheckLevel.ERROR
        assert "timeout" in source_check.message


@pytest.mark.unit
class TestPreflightReport:
    def test_summary_format(self) -> None:
        from waytoagi.preflight.check import CheckResult
        report = PreflightReport(
            results=[
                CheckResult(name="a", level=CheckLevel.OK, message="ok"),
                CheckResult(name="b", level=CheckLevel.WARNING, message="warn"),
                CheckResult(name="c", level=CheckLevel.ERROR, message="err"),
            ],
            total_duration_seconds=1.23,
        )
        s = report.summary()
        assert "1/3 OK" in s
        assert "1 warnings" in s
        assert "1 errors" in s
        assert "1.2s" in s
        assert report.passed is False

    def test_passed_when_no_errors(self) -> None:
        from waytoagi.preflight.check import CheckResult
        report = PreflightReport(results=[
            CheckResult(name="a", level=CheckLevel.OK, message="ok"),
            CheckResult(name="b", level=CheckLevel.WARNING, message="warn"),
        ])
        assert report.passed is True
