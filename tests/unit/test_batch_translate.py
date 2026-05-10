"""Unit tests cho `BatchTranslator` — gom N blocks vào 1 LLM call.

Coverage:
  - parse_batch_response: success / count mismatch / missing index
  - _build_user_message: format với delimiter
  - skip rỗng / không CJK
  - glossary fast-path
  - cache hit / miss
  - LLM call grouping theo char + item budget
  - Quality gate verify per item, kept best-effort
  - Format mismatch → split fallback (binary recurse)
  - Single item still fail → ghi failed
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from waytoagi.optimize.batch_translate import (
    BatchItem,
    BatchTranslator,
    _build_user_message,
    _delim,
    parse_batch_response,
)


def _make_pool(*, response: str | list[str] | Exception) -> MagicMock:
    pool = MagicMock()
    if isinstance(response, (list, Exception)):
        pool.chat = AsyncMock(side_effect=response)
    else:
        pool.chat = AsyncMock(return_value=response)
    return pool


def _make_cache() -> MagicMock:
    store: dict[str, str] = {}

    async def get(key: str) -> str | None:
        return store.get(key)

    async def put(key: str, value: str) -> None:
        store[key] = value

    cache = MagicMock()
    cache.get = AsyncMock(side_effect=get)
    cache.put = AsyncMock(side_effect=put)
    cache._store = store  # type: ignore[attr-defined]
    return cache


def _build_response(translations: list[str]) -> str:
    """Helper build response giả lập format LLM trả về."""
    parts: list[str] = []
    for i, t in enumerate(translations):
        parts.append(_delim(i))
        parts.append(t)
    return "\n".join(parts)


@pytest.mark.unit
class TestParseBatchResponse:
    def test_parse_success(self) -> None:
        raw = _build_response(["Bản dịch A", "Bản dịch B", "Bản dịch C"])
        parsed = parse_batch_response(raw, expected_count=3)
        assert parsed is not None
        assert parsed[0] == "Bản dịch A"
        assert parsed[1] == "Bản dịch B"
        assert parsed[2] == "Bản dịch C"

    def test_parse_count_mismatch(self) -> None:
        raw = _build_response(["A", "B"])  # only 2
        parsed = parse_batch_response(raw, expected_count=3)
        assert parsed is None

    def test_parse_extra_delimiters(self) -> None:
        raw = _build_response(["A", "B", "C", "D"])  # 4 instead of 3
        parsed = parse_batch_response(raw, expected_count=3)
        assert parsed is None

    def test_parse_skipped_index(self) -> None:
        # delimiter 0, 2 — missing 1
        raw = f"{_delim(0)}\nA\n{_delim(2)}\nC"
        parsed = parse_batch_response(raw, expected_count=2)
        assert parsed is None  # indices không phải [0, 1]

    def test_parse_with_artifacts(self) -> None:
        """LLM thêm wrapper code-fence → clean_artifacts strip → vẫn parse được."""
        raw = "```vi\n" + _build_response(["A", "B"]) + "\n```"
        parsed = parse_batch_response(raw, expected_count=2)
        assert parsed is not None
        assert parsed[0] == "A"
        assert parsed[1] == "B"

    def test_parse_multiline_content(self) -> None:
        raw = (
            f"{_delim(0)}\n"
            "Đoạn dịch A\nDòng 2 của A\n"
            f"{_delim(1)}\n"
            "Đoạn dịch B"
        )
        parsed = parse_batch_response(raw, expected_count=2)
        assert parsed is not None
        assert "Dòng 2 của A" in parsed[0]


@pytest.mark.unit
class TestBuildUserMessage:
    def test_packs_with_delimiters(self) -> None:
        items = [
            BatchItem("a", "中文1"),
            BatchItem("b", "中文2"),
        ]
        msg = _build_user_message(items)
        assert _delim(0) in msg
        assert _delim(1) in msg
        assert "中文1" in msg
        assert "中文2" in msg

    def test_index_starts_at_zero(self) -> None:
        items = [BatchItem("only", "中文")]
        msg = _build_user_message(items)
        assert _delim(0) in msg
        assert _delim(1) not in msg


@pytest.mark.unit
class TestBatchTranslatorSkipPaths:
    @pytest.mark.asyncio
    async def test_skip_empty_input(self) -> None:
        pool = _make_pool(response="never_called")
        bt = BatchTranslator(pool, cache=None)
        result = await bt.translate_batch([
            BatchItem("a", ""),
            BatchItem("b", "   "),
        ])
        assert result.translated == {"a": "", "b": "   "}
        assert result.skip_no_cjk == 2
        pool.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_no_cjk(self) -> None:
        pool = _make_pool(response="never_called")
        bt = BatchTranslator(pool, cache=None)
        result = await bt.translate_batch([
            BatchItem("a", "Already Vietnamese"),
            BatchItem("b", "Hello world"),
        ])
        assert result.skip_no_cjk == 2
        assert result.translated["a"] == "Already Vietnamese"
        pool.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_glossary_fast_path(self) -> None:
        pool = _make_pool(response="never_called")
        bt = BatchTranslator(pool, cache=None)
        result = await bt.translate_batch([
            BatchItem("a", "飞书"),
            BatchItem("b", "小互"),
        ])
        assert result.translated["a"] == "Feishu"
        assert result.translated["b"] == "Tiểu Hỗ"
        assert result.glossary_hits == 2
        pool.chat.assert_not_called()


@pytest.mark.unit
class TestBatchTranslatorCache:
    @pytest.mark.asyncio
    async def test_cache_hit_skips_llm(self) -> None:
        pool = _make_pool(response="never_called")
        cache = _make_cache()
        from waytoagi.optimize.batch_translate import _cache_key
        cache._store[_cache_key("这是测试文档")] = "Đây là tài liệu cũ"
        bt = BatchTranslator(pool, cache=cache)
        result = await bt.translate_batch([
            BatchItem("a", "这是测试文档"),
        ])
        assert result.translated["a"] == "Đây là tài liệu cũ"
        assert result.cache_hits == 1
        pool.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_put_after_success(self) -> None:
        pool = _make_pool(response=_build_response([
            "Đây là tài liệu mới dịch xong",
        ]))
        cache = _make_cache()
        bt = BatchTranslator(pool, cache=cache)
        await bt.translate_batch([
            BatchItem("a", "这是测试文档需要翻译"),
        ])
        # Cache đã được PUT
        assert len(cache._store) == 1


@pytest.mark.unit
class TestBatchTranslatorBatching:
    @pytest.mark.asyncio
    async def test_single_batch_when_under_budget(self) -> None:
        pool = _make_pool(response=_build_response([
            "Dịch A đầy đủ dấu",
            "Dịch B đầy đủ dấu",
            "Dịch C đầy đủ dấu",
        ]))
        bt = BatchTranslator(pool, cache=None)
        result = await bt.translate_batch([
            BatchItem("a", "中文文档第一"),
            BatchItem("b", "中文文档第二"),
            BatchItem("c", "中文文档第三"),
        ])
        assert pool.chat.call_count == 1
        assert result.llm_calls == 1
        assert result.items_per_call == [3]

    @pytest.mark.asyncio
    async def test_split_when_over_item_budget(self) -> None:
        # max_items=2 → 5 items split thành 3 batches
        pool = _make_pool(response=[
            _build_response(["A1", "A2"]),
            _build_response(["B1", "B2"]),
            _build_response(["C1"]),
        ])
        bt = BatchTranslator(
            pool, cache=None, max_items_per_batch=2,
        )
        items = [BatchItem(f"i{i}", f"中文{i}") for i in range(5)]
        result = await bt.translate_batch(items)
        assert pool.chat.call_count == 3
        assert result.items_per_call == [2, 2, 1]

    @pytest.mark.asyncio
    async def test_split_when_over_char_budget(self) -> None:
        pool = _make_pool(response=[
            _build_response(["dịch nhỏ A"]),
            _build_response(["dịch nhỏ B"]),
        ])
        # max_chars=20 — mỗi item 25 chars sẽ ở batch riêng
        big_text = "中文" * 13  # 26 chars
        bt = BatchTranslator(
            pool, cache=None,
            max_chars_per_batch=20, max_items_per_batch=10,
        )
        items = [BatchItem("a", big_text), BatchItem("b", big_text)]
        await bt.translate_batch(items)
        assert pool.chat.call_count == 2


@pytest.mark.unit
class TestBatchTranslatorFallback:
    @pytest.mark.asyncio
    async def test_format_mismatch_triggers_split_then_pass(self) -> None:
        """Batch 4 fail format → split 2+2 → mỗi batch 2 succeed."""
        pool = _make_pool(response=[
            "rubbish output without delimiters",  # batch of 4 fails
            # retry attempt with strict prompt also fails:
            "still rubbish",
            # split [0:2]:
            _build_response(["A", "B"]),
            # split [2:4]:
            _build_response(["C", "D"]),
        ])
        bt = BatchTranslator(
            pool, cache=None, max_items_per_batch=10, max_retries=1,
        )
        items = [BatchItem(f"i{i}", f"中文{i}") for i in range(4)]
        result = await bt.translate_batch(items)
        assert len(result.translated) == 4
        # 2 fail attempts (original + retry) + 2 successful split batches = 4
        assert pool.chat.call_count == 4

    @pytest.mark.asyncio
    async def test_single_item_fails_recorded(self) -> None:
        """Batch size=1 vẫn fail format → ghi vào failed, không recurse vô hạn."""
        pool = _make_pool(response="rubbish")  # always rubbish
        bt = BatchTranslator(
            pool, cache=None, max_items_per_batch=2, max_retries=0,
        )
        items = [BatchItem("only", "中文测试")]
        result = await bt.translate_batch(items)
        assert "only" in result.failed
        assert result.failed["only"] == "format_fail_at_size_1"


@pytest.mark.unit
class TestBatchTranslatorQualityGate:
    @pytest.mark.asyncio
    async def test_pass_quality_gate(self) -> None:
        pool = _make_pool(response=_build_response([
            "Đây là bản dịch tốt",
        ]))
        bt = BatchTranslator(pool, cache=None)
        result = await bt.translate_batch([
            BatchItem("a", "这是一个测试文档"),
        ])
        assert result.translated["a"] == "Đây là bản dịch tốt"

    @pytest.mark.asyncio
    async def test_quality_fail_kept_best_effort(self) -> None:
        """Output có CJK leak → kept best-effort không cache."""
        pool = _make_pool(response=_build_response([
            "Đây 这 còn CJK",  # fail gate
        ]))
        cache = _make_cache()
        bt = BatchTranslator(pool, cache=cache)
        result = await bt.translate_batch([
            BatchItem("a", "这是一个测试文档"),
        ])
        # Best-effort: kept output anyway
        assert "a" in result.translated
        # But NOT cached
        assert len(cache._store) == 0


@pytest.mark.unit
class TestBatchTranslatorPerformance:
    @pytest.mark.asyncio
    async def test_30_blocks_in_one_call(self) -> None:
        """Verify giảm 30x round-trip cho doc trung bình."""
        n = 30
        translations = [f"Dịch {i} đầy đủ dấu" for i in range(n)]
        pool = _make_pool(response=_build_response(translations))
        bt = BatchTranslator(pool, cache=None, max_items_per_batch=50)
        items = [BatchItem(f"b{i}", f"中文段{i}aaa") for i in range(n)]
        result = await bt.translate_batch(items)
        # 1 LLM call thay vì 30
        assert pool.chat.call_count == 1
        assert result.total_via_llm == 30
        assert result.avg_items_per_call == 30.0
