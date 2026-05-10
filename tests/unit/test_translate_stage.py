"""Tests cho `TranslateStage`."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from waytoagi.lark.auth import LarkAPIError
from waytoagi.optimize.batch_translate import BatchTranslateResult
from waytoagi.stages.translate import TranslateStage


def _text_block_cn(block_id: str, content: str) -> dict[str, Any]:
    return {
        "block_id": block_id,
        "block_type": 2,
        "text": {"elements": [{"text_run": {"content": content}}]},
    }


def _mk_doc_with(blocks: list[dict[str, Any]]) -> AsyncMock:
    doc = AsyncMock()
    doc.collect_all_blocks = AsyncMock(return_value=blocks)
    doc.patch_block = AsyncMock(return_value={"code": 0})
    return doc


def _mk_translator(*, translated: dict[str, str]) -> AsyncMock:
    """Mock BatchTranslator.translate_batch."""
    tr = AsyncMock()
    tr.translate_batch = AsyncMock(return_value=BatchTranslateResult(
        translated=translated,
        cache_hits=0,
        glossary_hits=0,
        skip_no_cjk=0,
        llm_calls=1,
        items_per_call=[len(translated)],
    ))
    return tr


@pytest.mark.unit
class TestTranslateStageBasic:
    @pytest.mark.asyncio
    async def test_translate_cjk_blocks(self) -> None:
        doc = _mk_doc_with([
            _text_block_cn("b1", "你好世界"),
            _text_block_cn("b2", "通往AGI之路是什么"),
        ])
        translator = _mk_translator(translated={
            "b1": "Xin chào thế giới",
            "b2": "Con đường tới AGI là gì",
        })
        stage = TranslateStage(doc=doc, translator=translator)
        result = await stage.translate_one(doc_id="d1")
        assert result.success is True
        assert result.stats.blocks_with_cjk == 2
        assert result.stats.blocks_translated_ok == 2
        # 2 PATCH calls (per block)
        assert doc.patch_block.call_count == 2

    @pytest.mark.asyncio
    async def test_skip_blocks_without_cjk(self) -> None:
        doc = _mk_doc_with([
            _text_block_cn("b1", "Already Vietnamese"),
            _text_block_cn("b2", "你好"),
        ])
        translator = _mk_translator(translated={"b2": "Xin chào"})
        stage = TranslateStage(doc=doc, translator=translator)
        result = await stage.translate_one(doc_id="d1")
        assert result.stats.blocks_with_cjk == 1
        assert result.stats.blocks_no_cjk_skipped == 1

    @pytest.mark.asyncio
    async def test_no_cjk_no_op(self) -> None:
        doc = _mk_doc_with([
            _text_block_cn("b1", "Hello"),
            _text_block_cn("b2", "World"),
        ])
        translator = _mk_translator(translated={})
        stage = TranslateStage(doc=doc, translator=translator)
        result = await stage.translate_one(doc_id="d1")
        assert result.success is True
        translator.translate_batch.assert_not_called()
        doc.patch_block.assert_not_called()


@pytest.mark.unit
class TestTranslateStageFailures:
    @pytest.mark.asyncio
    async def test_read_failure(self) -> None:
        doc = AsyncMock()
        doc.collect_all_blocks = AsyncMock(
            side_effect=LarkAPIError(131005, "not found", "/get"),
        )
        translator = _mk_translator(translated={})
        stage = TranslateStage(doc=doc, translator=translator)
        result = await stage.translate_one(doc_id="d1")
        assert result.success is False
        assert "131005" in result.error

    @pytest.mark.asyncio
    async def test_partial_patch_failures_acceptable(self) -> None:
        """20% block fail → still considered OK."""
        # 10 blocks, 2 patch fails (20%)
        blocks = [_text_block_cn(f"b{i}", f"中文{i}") for i in range(10)]
        translated = {f"b{i}": f"VI{i}" for i in range(10)}
        doc = _mk_doc_with(blocks)

        call_count = {"i": 0}

        async def patch_side(*_a: Any, **_k: Any) -> dict[str, Any]:
            call_count["i"] += 1
            if call_count["i"] in (3, 7):
                raise LarkAPIError(99991400, "rate", "/patch")
            return {"code": 0}

        doc.patch_block = AsyncMock(side_effect=patch_side)
        translator = _mk_translator(translated=translated)
        stage = TranslateStage(doc=doc, translator=translator)
        result = await stage.translate_one(doc_id="d1")
        assert result.stats.blocks_translated_ok == 8
        assert result.stats.blocks_translated_fail == 2

    @pytest.mark.asyncio
    async def test_high_fail_rate_marks_fail(self) -> None:
        """>20% fail → mark FAIL."""
        blocks = [_text_block_cn(f"b{i}", f"中文{i}") for i in range(5)]
        translated = {f"b{i}": f"VI{i}" for i in range(5)}
        doc = _mk_doc_with(blocks)
        doc.patch_block = AsyncMock(
            side_effect=LarkAPIError(99991400, "rate", "/patch"),
        )
        translator = _mk_translator(translated=translated)
        stage = TranslateStage(doc=doc, translator=translator)
        result = await stage.translate_one(doc_id="d1")
        # All 5 fails → > 20% → FAIL
        assert result.success is False


@pytest.mark.unit
class TestTranslateStageStats:
    @pytest.mark.asyncio
    async def test_cache_hit_pct_computed(self) -> None:
        doc = _mk_doc_with([
            _text_block_cn(f"b{i}", f"中文{i}") for i in range(10)
        ])
        # Simulate 4 cache hits
        translator = AsyncMock()
        translator.translate_batch = AsyncMock(
            return_value=BatchTranslateResult(
                translated={f"b{i}": f"VI{i}" for i in range(10)},
                cache_hits=4,
                glossary_hits=0,
                llm_calls=1,
                items_per_call=[6],
            ),
        )
        stage = TranslateStage(doc=doc, translator=translator)
        result = await stage.translate_one(doc_id="d1")
        assert result.cache_hit_pct == 40.0
