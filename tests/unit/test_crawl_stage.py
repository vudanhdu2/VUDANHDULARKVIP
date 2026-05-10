"""Tests cho `CrawlStage` — eager-placeholder crawl.

Coverage:
  - Detect: NEW / EDITED / RENAMED / UNCHANGED / DELETED
  - Phase 2: parallel placeholder creation, skip existing
  - Phase 3: NEW → batch_create với placeholder bound; EDITED → reset
  - SourceOrderIndex captured đúng DFS order
  - Idempotent re-run: 0 placeholders created lần 2
  - Failure isolation: 1 placeholder fail không halt run
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from waytoagi.lark.auth import LarkAPIError
from waytoagi.models.base import BaseRecord
from waytoagi.stages.crawl import CrawlStage
from waytoagi.stages.placeholder import PlaceholderCreator


def _make_record(
    *,
    record_id: str,
    node_token: str,
    title: str = "",
    last_edit_ms: int = 0,
    mirror_token: str = "",
) -> BaseRecord:
    return BaseRecord.model_validate({
        "record_id": record_id,
        "Title": title,
        "Node Token": node_token,
        "Last Edit Time": last_edit_ms,
        "Mirror Wiki Node Token": mirror_token,
    })


async def _async_iter(items: list[dict[str, Any]]):
    for item in items:
        yield item


def _make_src_wiki(nodes: list[dict[str, Any]]) -> AsyncMock:
    """Mock LarkWiki.walk_tree async generator."""
    wiki = AsyncMock()
    wiki.walk_tree = lambda *_args, **_kwargs: _async_iter(nodes)
    return wiki


def _make_dst_wiki_creating(
    *,
    fail_for_tokens: set[str] | None = None,
) -> AsyncMock:
    """Mock LarkWiki cho DST tenant — create_node trả token unique per call."""
    wiki = AsyncMock()
    counter = {"i": 0}

    async def create_node(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        title = kwargs.get("title", "")
        if fail_for_tokens and title in fail_for_tokens:
            raise LarkAPIError(99991400, "rate limit", "/create")
        counter["i"] += 1
        return {
            "code": 0,
            "data": {"node": {"node_token": f"DST_{counter['i']}"}},
        }

    wiki.create_node = AsyncMock(side_effect=create_node)
    return wiki


def _make_base() -> AsyncMock:
    base = AsyncMock()
    base.batch_create = AsyncMock(return_value={"code": 0})
    base.batch_update = AsyncMock(return_value={"code": 0})
    return base


def _make_stage(
    src_wiki: AsyncMock,
    dst_wiki: AsyncMock,
    base: AsyncMock,
) -> CrawlStage:
    placeholder = PlaceholderCreator(
        wiki=dst_wiki,
        space_id="dst-space",
        default_parent_token="dst-root",
        dst_domain="dst.larksuite.com",
    )
    return CrawlStage(
        src_wiki=src_wiki,
        src_space_id="src-space",
        base=base,
        app_token="app-tk",
        table_id="tbl-id",
        placeholder=placeholder,
    )


@pytest.mark.unit
class TestCrawlStageDetect:
    @pytest.mark.asyncio
    async def test_all_new_when_existing_empty(self) -> None:
        src_nodes = [
            {"node_token": "src1", "title": "A", "obj_token": "o1",
             "obj_type": "docx", "node_type": "origin",
             "parent_node_token": "", "obj_edit_time": 1000000},
            {"node_token": "src2", "title": "B", "obj_token": "o2",
             "obj_type": "docx", "node_type": "origin",
             "parent_node_token": "src1", "obj_edit_time": 1000000},
        ]
        stage = _make_stage(
            _make_src_wiki(src_nodes),
            _make_dst_wiki_creating(),
            _make_base(),
        )
        result, _ = await stage.run(existing_records=[])
        assert result.nodes_walked == 2
        assert result.new_count == 2
        assert result.placeholders_created == 2

    @pytest.mark.asyncio
    async def test_unchanged_record_only_touches(self) -> None:
        src_nodes = [
            {"node_token": "src1", "title": "A", "obj_token": "o1",
             "obj_type": "docx", "parent_node_token": "",
             "obj_edit_time": 1000},
        ]
        existing = [_make_record(
            record_id="r1", node_token="src1", title="A",
            last_edit_ms=1_000_000,  # already in ms
        )]
        stage = _make_stage(
            _make_src_wiki(src_nodes),
            _make_dst_wiki_creating(),
            _make_base(),
        )
        result, _ = await stage.run(existing_records=existing)
        assert result.unchanged_count == 1
        assert result.new_count == 0
        assert result.placeholders_created == 0

    @pytest.mark.asyncio
    async def test_renamed_detected(self) -> None:
        src_nodes = [
            {"node_token": "src1", "title": "B (renamed)", "obj_token": "o1",
             "obj_type": "docx", "parent_node_token": "",
             "obj_edit_time": 1000},
        ]
        existing = [_make_record(
            record_id="r1", node_token="src1", title="A (old)",
            last_edit_ms=1_000_000,
        )]
        stage = _make_stage(
            _make_src_wiki(src_nodes),
            _make_dst_wiki_creating(),
            _make_base(),
        )
        result, _ = await stage.run(existing_records=existing)
        assert result.renamed_count == 1
        assert result.new_count == 0

    @pytest.mark.asyncio
    async def test_edited_detected_when_edit_time_advances(self) -> None:
        # New edit time 2000s = 2_000_000 ms; old 1_000_000 ms
        # Diff > 60s tolerance → EDITED
        src_nodes = [
            {"node_token": "src1", "title": "A", "obj_token": "o1",
             "obj_type": "docx", "parent_node_token": "",
             "obj_edit_time": 2000},
        ]
        existing = [_make_record(
            record_id="r1", node_token="src1", title="A",
            last_edit_ms=1_000_000,
        )]
        stage = _make_stage(
            _make_src_wiki(src_nodes),
            _make_dst_wiki_creating(),
            _make_base(),
        )
        result, _ = await stage.run(existing_records=existing)
        assert result.edited_count == 1

    @pytest.mark.asyncio
    async def test_deleted_detected(self) -> None:
        # Source walk returns nothing, but Base có 1 record
        src_nodes: list[dict[str, Any]] = []
        existing = [_make_record(
            record_id="r1", node_token="vanished_src", title="X",
        )]
        stage = _make_stage(
            _make_src_wiki(src_nodes),
            _make_dst_wiki_creating(),
            _make_base(),
        )
        result, _ = await stage.run(existing_records=existing)
        assert result.deleted_count == 1
        assert result.nodes_walked == 0


@pytest.mark.unit
class TestCrawlStagePlaceholder:
    @pytest.mark.asyncio
    async def test_placeholder_created_for_each_new(self) -> None:
        src_nodes = [
            {"node_token": f"src{i}", "title": f"Doc {i}",
             "obj_token": f"o{i}", "obj_type": "docx",
             "parent_node_token": "", "obj_edit_time": 1000}
            for i in range(3)
        ]
        dst_wiki = _make_dst_wiki_creating()
        stage = _make_stage(
            _make_src_wiki(src_nodes),
            dst_wiki,
            _make_base(),
        )
        result, _ = await stage.run(existing_records=[])
        assert result.placeholders_created == 3
        assert dst_wiki.create_node.call_count == 3

    @pytest.mark.asyncio
    async def test_idempotent_re_run_skips_existing_placeholders(self) -> None:
        """Re-run sau khi đã có placeholder → 0 create_node calls."""
        src_nodes = [
            {"node_token": "src1", "title": "A", "obj_token": "o1",
             "obj_type": "docx", "parent_node_token": "",
             "obj_edit_time": 1000},
        ]
        existing = [_make_record(
            record_id="r1", node_token="src1", title="A",
            last_edit_ms=1_000_000,
            mirror_token="DST_ALREADY",
        )]
        dst_wiki = _make_dst_wiki_creating()
        stage = _make_stage(
            _make_src_wiki(src_nodes),
            dst_wiki,
            _make_base(),
        )
        result, _ = await stage.run(existing_records=existing)
        assert result.unchanged_count == 1
        # No new placeholders since record had no NEW event
        assert result.placeholders_created == 0
        dst_wiki.create_node.assert_not_called()

    @pytest.mark.asyncio
    async def test_placeholder_failure_does_not_halt_others(self) -> None:
        """1 placeholder fail → vẫn xử lý các record kế tiếp."""
        src_nodes = [
            {"node_token": f"src{i}", "title": f"Doc {i}",
             "obj_token": f"o{i}", "obj_type": "docx",
             "parent_node_token": "", "obj_edit_time": 1000}
            for i in range(3)
        ]
        # Doc 1 fails
        dst_wiki = _make_dst_wiki_creating(fail_for_tokens={"Doc 1"})
        stage = _make_stage(
            _make_src_wiki(src_nodes),
            dst_wiki,
            _make_base(),
        )
        result, _ = await stage.run(existing_records=[])
        assert result.placeholders_created == 2
        assert result.placeholders_failed == 1
        assert any("placeholder:src1" in e for e in result.errors)


@pytest.mark.unit
class TestCrawlStageBaseWrites:
    @pytest.mark.asyncio
    async def test_new_records_batch_created(self) -> None:
        src_nodes = [
            {"node_token": "src1", "title": "A", "obj_token": "o1",
             "obj_type": "docx", "parent_node_token": "",
             "obj_edit_time": 1000},
        ]
        base = _make_base()
        stage = _make_stage(
            _make_src_wiki(src_nodes),
            _make_dst_wiki_creating(),
            base,
        )
        await stage.run(existing_records=[])
        base.batch_create.assert_called_once()
        call_records = base.batch_create.call_args.args[2]
        assert len(call_records) == 1
        fields = call_records[0]["fields"]
        assert fields["Node Token"] == "src1"
        assert fields["Title"] == "A"
        # Mirror Wiki Node Token + Liên kết wiki dịch mới ĐÃ ĐƯỢC bind
        assert fields["Mirror Wiki Node Token"].startswith("DST_")
        assert fields["Mirror Wiki Status"] == "Placeholder"
        assert fields["Liên kết wiki dịch mới"]["link"].startswith("https://")
        # Trạng thái Pending để pipeline pick up
        assert fields["Trạng thái"] == "Pending"
        assert fields["Trạng thái dịch"] == "Pending"

    @pytest.mark.asyncio
    async def test_edited_record_reset_to_pending(self) -> None:
        src_nodes = [
            {"node_token": "src1", "title": "A", "obj_token": "o1",
             "obj_type": "docx", "parent_node_token": "",
             "obj_edit_time": 9999},  # ms = 9_999_000
        ]
        existing = [_make_record(
            record_id="r1", node_token="src1", title="A",
            last_edit_ms=1_000_000,
            mirror_token="DST_OLD",
        )]
        base = _make_base()
        stage = _make_stage(
            _make_src_wiki(src_nodes),
            _make_dst_wiki_creating(),
            base,
        )
        await stage.run(existing_records=existing)
        base.batch_update.assert_called_once()
        call_records = base.batch_update.call_args.args[2]
        update = call_records[0]["fields"]
        assert update["Change Status"] == "edited"
        assert update["Trạng thái"] == "Pending"
        assert update["Trạng thái dịch"] == "Pending"
        # Mirror Wiki Node Token NOT cleared — placeholder still valid
        assert "Mirror Wiki Node Token" not in update

    @pytest.mark.asyncio
    async def test_deleted_record_marked_source_status(self) -> None:
        src_nodes: list[dict[str, Any]] = []
        existing = [_make_record(
            record_id="r1", node_token="vanished", title="X",
            mirror_token="DST_X",
        )]
        base = _make_base()
        stage = _make_stage(
            _make_src_wiki(src_nodes),
            _make_dst_wiki_creating(),
            base,
        )
        await stage.run(existing_records=existing)
        # batch_update called for deleted records
        base.batch_update.assert_called()
        update_records = base.batch_update.call_args.args[2]
        deleted_update = next(
            r for r in update_records if r["record_id"] == "r1"
        )
        assert deleted_update["fields"]["Source Status"] == "Deleted"
        assert deleted_update["fields"]["Change Status"] == "deleted"


@pytest.mark.unit
class TestCrawlStageSourceOrder:
    @pytest.mark.asyncio
    async def test_source_order_captured_dfs(self) -> None:
        """SourceOrderIndex chứa DFS order từ walk."""
        src_nodes = [
            {"node_token": "root1", "title": "R1", "obj_token": "o",
             "obj_type": "docx", "parent_node_token": "",
             "obj_edit_time": 1000},
            {"node_token": "child1a", "title": "C1A", "obj_token": "o",
             "obj_type": "docx", "parent_node_token": "root1",
             "obj_edit_time": 1000},
            {"node_token": "child1b", "title": "C1B", "obj_token": "o",
             "obj_type": "docx", "parent_node_token": "root1",
             "obj_edit_time": 1000},
            {"node_token": "root2", "title": "R2", "obj_token": "o",
             "obj_type": "docx", "parent_node_token": "",
             "obj_edit_time": 1000},
        ]
        stage = _make_stage(
            _make_src_wiki(src_nodes),
            _make_dst_wiki_creating(),
            _make_base(),
        )
        _, src_order = await stage.run(existing_records=[])
        # Top-level (parent="") có 2 roots theo thứ tự walk
        assert src_order.children_of("") == ["root1", "root2"]
        # root1 có 2 children
        assert src_order.children_of("root1") == ["child1a", "child1b"]
