"""Integration tests cho `Translator` — mock LLMPool, real prompts/quality.

Test toàn bộ pipeline:
  1. Skip rỗng / không CJK
  2. Glossary fast-path
  3. Cache hit / miss
  4. Quality gate pass → cache PUT
  5. Quality gate fail → retry với strict
  6. Quality gate fail mọi attempt → fallback source
  7. LLM exception → fallback source
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from waytoagi.llm.translate import Translator


def _make_pool(*, response: str | list[str] | Exception) -> MagicMock:
    """Mock LLMPool.chat() → trả về response (hoặc raise Exception).

    Nếu response là list, mỗi call lấy phần tử kế tiếp.
    """
    pool = MagicMock()
    if isinstance(response, (list, Exception)):
        pool.chat = AsyncMock(side_effect=response)
    else:
        pool.chat = AsyncMock(return_value=response)
    return pool


def _make_cache() -> MagicMock:
    """Mock TranslationCache với in-memory dict."""
    store: dict[str, str] = {}

    async def get(key: str) -> str | None:
        return store.get(key)

    async def put(key: str, value: str) -> None:
        store[key] = value

    cache = MagicMock()
    cache.get = AsyncMock(side_effect=get)
    cache.put = AsyncMock(side_effect=put)
    cache._store = store  # type: ignore[attr-defined]  # for assertions
    return cache


@pytest.mark.unit
class TestTranslatorSkipPaths:
    @pytest.mark.asyncio
    async def test_empty_input_returns_empty(self) -> None:
        pool = _make_pool(response="never_called")
        translator = Translator(pool, cache=None)
        result = await translator.translate_one("")
        assert result == ""
        pool.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_whitespace_only_returns_unchanged(self) -> None:
        pool = _make_pool(response="never_called")
        translator = Translator(pool, cache=None)
        result = await translator.translate_one("   \n\t  ")
        assert result == "   \n\t  "
        pool.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_cjk_skips_llm(self) -> None:
        pool = _make_pool(response="never_called")
        translator = Translator(pool, cache=None)
        result = await translator.translate_one("Đây là text Việt rồi")
        assert result == "Đây là text Việt rồi"
        pool.chat.assert_not_called()


@pytest.mark.unit
class TestTranslatorGlossaryFastPath:
    @pytest.mark.asyncio
    async def test_brand_lookup_skips_llm(self) -> None:
        pool = _make_pool(response="never_called")
        translator = Translator(pool, cache=None)
        result = await translator.translate_one("飞书")
        assert result == "Feishu"
        pool.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_person_name_lookup(self) -> None:
        pool = _make_pool(response="never_called")
        translator = Translator(pool, cache=None)
        result = await translator.translate_one("小互")
        assert result == "Tiểu Hỗ"
        pool.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_long_text_skips_glossary_uses_llm(self) -> None:
        """Text > 20 chars even if starts with glossary entry → LLM."""
        pool = _make_pool(
            response="Feishu là công cụ cộng tác văn phòng.",
        )
        translator = Translator(pool, cache=None)
        long_text = "飞书是一个非常好用的协同办公工具。" * 2
        result = await translator.translate_one(long_text)
        # LLM was called
        pool.chat.assert_called()
        assert "Feishu" in result


@pytest.mark.unit
class TestTranslatorCache:
    @pytest.mark.asyncio
    async def test_cache_miss_calls_llm_then_stores(self) -> None:
        pool = _make_pool(response="Đây là một tài liệu thử nghiệm")
        cache = _make_cache()
        translator = Translator(pool, cache=cache)
        result = await translator.translate_one(
            "这是一个测试文档需要翻译成越南语",
            mode="content",
        )
        assert result == "Đây là một tài liệu thử nghiệm"
        pool.chat.assert_called_once()
        # Cache PUT đã gọi
        cache.put.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_hit_skips_llm(self) -> None:
        pool = _make_pool(response="never_called")
        cache = _make_cache()
        # Manually populate cache với key đúng format
        from waytoagi.llm.translate import _hash
        cache._store[_hash("这是一个测试文档需要翻译", "vi", "content")] = (
            "Cached translation"
        )
        translator = Translator(pool, cache=cache)
        result = await translator.translate_one("这是一个测试文档需要翻译")
        assert result == "Cached translation"
        pool.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_keyed_by_mode(self) -> None:
        """Title vs content cache key khác nhau → không đè."""
        pool = _make_pool(
            response=["Đây là content mode VI", "Đây là title mode VI"],
        )
        cache = _make_cache()
        translator = Translator(pool, cache=cache)

        c = await translator.translate_one(
            "这是一个测试文档", mode="content",
        )
        t = await translator.translate_one(
            "这是一个测试文档", mode="title",
        )
        # 2 calls, 2 cache entries
        assert c != t
        assert pool.chat.call_count == 2
        assert len(cache._store) == 2


@pytest.mark.unit
class TestTranslatorQualityGate:
    @pytest.mark.asyncio
    async def test_pass_first_attempt(self) -> None:
        pool = _make_pool(response="Đây là tài liệu thử nghiệm")
        translator = Translator(pool, cache=None)
        result = await translator.translate_one("这是测试文档需要翻译成越南语")
        assert result == "Đây là tài liệu thử nghiệm"
        pool.chat.assert_called_once()

    @pytest.mark.asyncio
    async def test_strip_artifacts_then_pass(self) -> None:
        """LLM trả wrapper → clean_artifacts strip → pass."""
        pool = _make_pool(response='```vi\nĐây là tài liệu thử nghiệm\n```')
        translator = Translator(pool, cache=None)
        result = await translator.translate_one("这是测试文档需要翻译成越南语")
        assert result == "Đây là tài liệu thử nghiệm"

    @pytest.mark.asyncio
    async def test_cjk_leak_triggers_retry_strict(self) -> None:
        """First attempt còn CJK → retry với strict prompt → 2nd pass."""
        pool = _make_pool(response=[
            "Đây 这 là tài liệu",  # 1 CJK leak
            "Đây là tài liệu thử nghiệm hoàn chỉnh",  # clean
        ])
        translator = Translator(pool, cache=None, max_retries=1)
        result = await translator.translate_one("这是测试文档需要翻译成越南语")
        assert result == "Đây là tài liệu thử nghiệm hoàn chỉnh"
        assert pool.chat.call_count == 2

    @pytest.mark.asyncio
    async def test_strict_prompt_used_on_retry(self) -> None:
        """Verify retry dùng strict prompt (chứa 'ZERO CJK' warning)."""
        pool = _make_pool(response=[
            "Đây 这 là tài liệu",  # fail gate
            "Đây là tài liệu thử nghiệm",  # pass
        ])
        translator = Translator(pool, cache=None, max_retries=1)
        await translator.translate_one("这是测试文档需要翻译")

        # First call: normal prompt (không strict warning)
        first_call_messages = pool.chat.call_args_list[0].args[0]
        first_system = first_call_messages[0]["content"]
        # Second call: strict prompt
        second_call_messages = pool.chat.call_args_list[1].args[0]
        second_system = second_call_messages[0]["content"]
        # Strict prompt dài hơn (extra warning)
        assert len(second_system) > len(first_system)

    @pytest.mark.asyncio
    async def test_all_retries_fail_returns_last_attempt(self) -> None:
        """Mọi attempt fail gate → return last attempt (best effort)."""
        pool = _make_pool(response=[
            "Đây 这 là tài liệu",  # fail
            "Đây 这 là tài liệu 2",  # also fail
        ])
        translator = Translator(pool, cache=None, max_retries=1)
        result = await translator.translate_one("这是测试文档需要翻译")
        # Last attempt returned (best effort)
        assert "tài liệu" in result

    @pytest.mark.asyncio
    async def test_failed_translation_not_cached(self) -> None:
        """Quality gate fail → KHÔNG cache (tránh poison)."""
        pool = _make_pool(response=[
            "这 still has CJK",
            "这 still has CJK 2",
        ])
        cache = _make_cache()
        translator = Translator(pool, cache=cache, max_retries=1)
        await translator.translate_one("这是测试文档需要翻译")
        # Cache PUT NOT called
        cache.put.assert_not_called()


@pytest.mark.unit
class TestTranslatorErrorHandling:
    @pytest.mark.asyncio
    async def test_llm_exception_returns_source(self) -> None:
        pool = _make_pool(response=RuntimeError("LLM down"))
        translator = Translator(pool, cache=None, max_retries=0)
        source = "这是测试文档需要翻译"
        result = await translator.translate_one(source)
        # Fallback to source (caller sees CN, knows nothing was translated)
        assert result == source

    @pytest.mark.asyncio
    async def test_partial_success_after_retry(self) -> None:
        """LLM fail attempt 1 (exception), success attempt 2."""

        async def chat_side_effect(
            *_args: Any, **_kwargs: Any,
        ) -> str:
            if pool.chat.call_count == 1:
                raise RuntimeError("LLM transient")
            return "Đây là tài liệu thử nghiệm"

        pool = MagicMock()
        pool.chat = AsyncMock(side_effect=chat_side_effect)
        translator = Translator(pool, cache=None, max_retries=1)
        result = await translator.translate_one("这是测试文档需要翻译")
        assert result == "Đây là tài liệu thử nghiệm"
        assert pool.chat.call_count == 2


@pytest.mark.unit
class TestTranslatorBatch:
    @pytest.mark.asyncio
    async def test_translate_many_parallel(self) -> None:
        pool = _make_pool(response=[
            "Bản dịch A đầy đủ dấu",
            "Bản dịch B đầy đủ dấu",
            "Bản dịch C đầy đủ dấu",
        ])
        translator = Translator(pool, cache=None, concurrency=3)
        results = await translator.translate_many([
            "这是文档A需要翻译",
            "这是文档B需要翻译",
            "这是文档C需要翻译",
        ])
        assert results == [
            "Bản dịch A đầy đủ dấu",
            "Bản dịch B đầy đủ dấu",
            "Bản dịch C đầy đủ dấu",
        ]
        assert pool.chat.call_count == 3

    @pytest.mark.asyncio
    async def test_translate_many_mixed_skip_and_call(self) -> None:
        """Mix: 1 empty, 1 no-CJK, 1 brand glossary, 1 needs LLM."""
        pool = _make_pool(response="Đây là tài liệu thử nghiệm dài")
        translator = Translator(pool, cache=None)
        results = await translator.translate_many([
            "",  # skip
            "Hello English",  # skip (no CJK)
            "飞书",  # glossary
            "这是测试文档需要翻译",  # LLM (not in glossary)
        ])
        assert results[0] == ""
        assert results[1] == "Hello English"
        assert results[2] == "Feishu"
        assert results[3] == "Đây là tài liệu thử nghiệm dài"
        # LLM called only once (cho item index 3)
        assert pool.chat.call_count == 1


@pytest.mark.unit
class TestTranslatorTitleMode:
    @pytest.mark.asyncio
    async def test_title_mode_uses_title_prompt(self) -> None:
        pool = _make_pool(
            response="Phân tích chi tiết: DeepSeek dẫn đầu",
        )
        translator = Translator(pool, cache=None)
        await translator.translate_one(
            "详解:DeepSeek 目前断档第一",
            mode="title",
        )
        call_args = pool.chat.call_args_list[0]
        system = call_args.args[0][0]["content"]
        assert "TIÊU ĐỀ" in system
