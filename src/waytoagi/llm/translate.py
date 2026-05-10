"""Translator — batch CN→VI qua LLMPool, với quality gate + retry.

Pipeline cho mỗi request:
  1. Skip nếu input rỗng hoặc không chứa CJK (đã VI rồi).
  2. Cache lookup (SHA-256 key) — hit → return.
  3. Glossary fast-path: nếu input match exact 1 entry → trả VI ngay
     không gọi LLM (tiết kiệm token + tránh fail trên text quá ngắn).
  4. LLM call với system prompt layered (rules + style + glossary +
     few-shot) — `mode='content'` hoặc `mode='title'`.
  5. Post-process: clean_artifacts + assess_quality.
  6. Nếu fail quality gate → retry 1 lần với `strict=True` (siết thêm).
  7. Nếu vẫn fail → return text gốc (fail-safe), log warning.
  8. Cache PUT chỉ khi pass gate — tránh poison cache.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING, Literal

import structlog

from waytoagi.llm.glossary import lookup as glossary_lookup
from waytoagi.llm.prompts import build_content_prompt, build_title_prompt
from waytoagi.llm.quality import (
    QualityReport,
    assess_quality,
    clean_artifacts,
    has_cjk,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from waytoagi.cache.sqlite import TranslationCache
    from waytoagi.llm.pool import LLMPool

logger = structlog.get_logger(__name__)

TranslateMode = Literal["content", "title"]


def _hash(text: str, target_lang: str, mode: TranslateMode) -> str:
    """Cache key — phụ thuộc cả mode để tránh content/title cache đè."""
    h = hashlib.sha256()
    h.update(target_lang.encode("utf-8"))
    h.update(b"\x00")
    h.update(mode.encode("utf-8"))
    h.update(b"\x00")
    h.update(text.encode("utf-8"))
    return h.hexdigest()


class Translator:
    """Batch translator với glossary + quality gate + retry.

    Args:
        pool: LLMPool round-robin.
        cache: TranslationCache SQLite (optional).
        concurrency: số request LLM song song tối đa.
        max_retries: số retry sau khi fail quality gate. 0 → no retry.
            Mặc định 1 = thử thêm 1 lần với prompt strict.

    Usage:
        translator = Translator(pool=pool, cache=cache)
        vi = await translator.translate_one(zh_text, mode="content")
        vi_titles = await translator.translate_many(zh_titles, mode="title")
    """

    def __init__(
        self,
        pool: LLMPool,
        *,
        cache: TranslationCache | None = None,
        concurrency: int = 4,
        max_retries: int = 1,
    ) -> None:
        self._pool = pool
        self._cache = cache
        self._semaphore = asyncio.Semaphore(concurrency)
        self._max_retries = max_retries
        self._log = logger.bind(component="Translator")

    # ====================================================================
    # Public API
    # ====================================================================

    async def translate_one(
        self,
        text: str,
        *,
        mode: TranslateMode = "content",
        target: str = "vi",
    ) -> str:
        """Dịch 1 đoạn text. Fail-safe: trả text gốc nếu LLM lỗi/fail gate."""
        if not text or not text.strip():
            return text

        # Skip nếu không có CJK — đã VI hoặc text Latin thuần
        if not has_cjk(text):
            return text

        # Glossary fast-path: input == entry trong bảng → trả ngay
        gloss_hit = glossary_lookup(text.strip())
        if gloss_hit and len(text.strip()) <= 20:
            return gloss_hit

        # Cache lookup
        key = _hash(text, target, mode)
        if self._cache:
            cached = await self._cache.get(key)
            if cached is not None:
                return cached

        # LLM call với retry quality-gated
        result, report = await self._translate_with_quality_gate(
            text, mode=mode,
        )

        # Cache PUT chỉ khi pass — tránh poison
        if self._cache and report.passed:
            await self._cache.put(key, result)

        return result

    async def translate_many(
        self,
        texts: Sequence[str],
        *,
        mode: TranslateMode = "content",
        target: str = "vi",
    ) -> list[str]:
        """Dịch list[str] async parallel (bounded by concurrency)."""
        return await asyncio.gather(
            *(self.translate_one(t, mode=mode, target=target) for t in texts),
        )

    # ====================================================================
    # Internal
    # ====================================================================

    async def _translate_with_quality_gate(
        self,
        text: str,
        *,
        mode: TranslateMode,
    ) -> tuple[str, QualityReport]:
        """LLM call + quality gate + retry với strict prompt.

        Returns:
            (result_text, quality_report) — luôn return text non-empty
            (fallback to source nếu mọi attempt fail).
        """
        last_result = text  # fallback
        last_report: QualityReport | None = None

        for attempt in range(self._max_retries + 1):
            strict = attempt > 0  # retry → strict prompt
            try:
                raw = await self._call_llm(text, mode=mode, strict=strict)
            except Exception as e:  # any LLM error → fallback
                self._log.warning(
                    "llm_call_failed",
                    err=str(e)[:120],
                    attempt=attempt,
                    mode=mode,
                    text_len=len(text),
                )
                continue

            cleaned = clean_artifacts(raw)
            report = assess_quality(text, cleaned)
            last_result = cleaned
            last_report = report

            if report.passed:
                if attempt > 0:
                    self._log.info(
                        "quality_gate_pass_after_retry",
                        attempt=attempt,
                        mode=mode,
                    )
                return cleaned, report

            self._log.warning(
                "quality_gate_fail",
                attempt=attempt,
                mode=mode,
                issues=[i.value for i in report.issues],
                detail=report.detail,
            )

        # All attempts fail — return last result (or source) + final report
        if last_report is None:
            last_report = assess_quality(text, text)
        return last_result, last_report

    async def _call_llm(
        self,
        text: str,
        *,
        mode: TranslateMode,
        strict: bool,
    ) -> str:
        """Build prompt + call LLM với semaphore concurrency limit."""
        if mode == "title":
            system = build_title_prompt(strict=strict)
        else:
            system = build_content_prompt(strict=strict)

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ]
        async with self._semaphore:
            return await self._pool.chat(messages)
