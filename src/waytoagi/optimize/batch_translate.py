"""Batch-translate: gom N blocks vào 1 LLM call (giảm 20-50x round-trip).

V1 problem: doc có 5000 blocks x 2-5s/LLM call = 30-90 phút translate.

V2 solution:
  - Pack N blocks vào 1 prompt với delimiter ổn định.
  - LLM dịch tất cả 1 lần → trả về cùng format.
  - Parser tách lại theo delimiter, verify count match.
  - Fallback an toàn: nếu parse fail → split lẻ ra dịch riêng từng item
    (đảm bảo correctness không bao giờ fail vì batch).

Chìa khóa thiết kế:
  1. **Delimiter sentinel** vừa unique vừa LLM khó nhầm:
     `<<<wta-block-{N}>>>` — viết thường, có dấu `<<<` `>>>`, có index.
  2. **Token budget**: mỗi batch tối đa M ký tự input + N items.
     Mặc định 4000 chars + 30 items.
  3. **Verify protocol**:
     - Count delimiter trong output == count input.
     - Mỗi delimiter có content non-empty.
     - Quality gate per-item (CJK leak detection).
  4. **Idempotent fallback**: nếu batch lệch → split nhỏ hơn, không
     fall back về translate_one trực tiếp (vẫn vào BatchTranslator
     nhưng size=1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from waytoagi.llm.glossary import lookup as glossary_lookup
from waytoagi.llm.prompts import build_content_prompt
from waytoagi.llm.quality import (
    assess_quality,
    clean_artifacts,
    has_cjk,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from waytoagi.cache.sqlite import TranslationCache
    from waytoagi.llm.pool import LLMPool

logger = structlog.get_logger(__name__)

# ----------------------------------------------------------------------------
# Delimiter format
# ----------------------------------------------------------------------------
# Pattern phải:
#   - Unique đủ để không xuất hiện trong content thật
#   - Có index để verify thứ tự + detect missing
#   - LLM dễ replicate (alphanumeric, không emoji lẩn dấu)
_DELIM_OPEN = "<<<wta-block-"
_DELIM_CLOSE = ">>>"

_DELIM_RE = re.compile(
    re.escape(_DELIM_OPEN) + r"(\d+)" + re.escape(_DELIM_CLOSE),
)


def _delim(idx: int) -> str:
    return f"{_DELIM_OPEN}{idx}{_DELIM_CLOSE}"


# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
DEFAULT_MAX_CHARS_PER_BATCH = 4000
"""Tổng ký tự input mỗi batch — vừa đủ để output cũng nằm trong context window."""

DEFAULT_MAX_ITEMS_PER_BATCH = 30
"""Số block tối đa 1 batch — cao hơn dễ làm LLM bỏ sót delimiter."""

MIN_BATCH_SIZE = 1
"""Khi fallback: split tới batch_size=1 thì gọi translate_one."""


# ----------------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BatchItem:
    """1 đơn vị dịch trong batch — id để caller correlate kết quả."""

    item_id: str
    text: str


@dataclass(slots=True)
class BatchTranslateResult:
    """Kết quả batch translate."""

    translated: dict[str, str] = field(default_factory=dict)
    """item_id → translated text."""

    failed: dict[str, str] = field(default_factory=dict)
    """item_id → error reason (nếu mọi attempt fail)."""

    cache_hits: int = 0
    glossary_hits: int = 0
    skip_no_cjk: int = 0
    llm_calls: int = 0
    items_per_call: list[int] = field(default_factory=list)
    """Track size mỗi batch thực gọi LLM — cho thấy adaptive split."""

    @property
    def total_processed(self) -> int:
        return len(self.translated) + len(self.failed)

    @property
    def total_via_llm(self) -> int:
        return self.total_processed - self.cache_hits - self.glossary_hits - self.skip_no_cjk

    @property
    def avg_items_per_call(self) -> float:
        if not self.items_per_call:
            return 0.0
        return sum(self.items_per_call) / len(self.items_per_call)


# ----------------------------------------------------------------------------
# Build / parse prompt
# ----------------------------------------------------------------------------


def _build_user_message(items: Sequence[BatchItem]) -> str:
    """Pack items vào 1 user message với delimiter.

    Format:
        <<<wta-block-0>>>
        nội dung block 0
        <<<wta-block-1>>>
        nội dung block 1
        ...
    """
    lines: list[str] = []
    for idx, it in enumerate(items):
        lines.append(_delim(idx))
        lines.append(it.text)
    return "\n".join(lines)


def _build_batch_system_prompt(*, strict: bool = False) -> str:
    """System prompt với hướng dẫn đặc biệt về batch format."""
    base = build_content_prompt(strict=strict)
    batch_addon = (
        "\n\nĐỊNH DẠNG BATCH (RẤT QUAN TRỌNG):\n"
        f"- User message gồm nhiều block, mỗi block bắt đầu bằng marker "
        f"'{_delim(0)}', '{_delim(1)}'…\n"
        "- Bạn PHẢI giữ NGUYÊN các marker, KHÔNG thay đổi index.\n"
        "- Sau mỗi marker là phần dịch (1 hoặc nhiều dòng). "
        "KHÔNG xen kẽ marker với phần dịch.\n"
        "- Số marker trong output PHẢI BẰNG số marker trong input.\n"
        "- KHÔNG thêm marker mới, KHÔNG bỏ marker nào.\n"
        "- KHÔNG thêm chú thích, prefix, suffix nào ngoài các block dịch."
    )
    return base + batch_addon


def parse_batch_response(raw: str, expected_count: int) -> dict[int, str] | None:
    """Parse response của LLM về dict[idx → translated_text].

    Returns None nếu detect lỗi format (missing delimiter, count mismatch).
    Caller sẽ fallback split khi nhận None.
    """
    cleaned = clean_artifacts(raw)
    matches = list(_DELIM_RE.finditer(cleaned))
    if len(matches) != expected_count:
        return None

    result: dict[int, str] = {}
    for i, m in enumerate(matches):
        idx = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(cleaned)
        text = cleaned[start:end].strip()
        result[idx] = text

    # Verify all indices 0..N-1 present (no duplicate, no skip)
    if set(result.keys()) != set(range(expected_count)):
        return None

    return result


# ----------------------------------------------------------------------------
# BatchTranslator
# ----------------------------------------------------------------------------


class BatchTranslator:
    """Batch-aware CN→VI translator.

    Args:
        pool: LLMPool round-robin.
        cache: TranslationCache SQLite (optional).
        max_chars_per_batch: ngưỡng tổng ký tự input để tách batch.
        max_items_per_batch: ngưỡng số item/batch.
        max_retries: số lần retry strict prompt nếu LLM trả format lệch.
            0 → fallback split ngay khi format sai.

    Usage:
        bt = BatchTranslator(pool=pool, cache=cache)
        items = [BatchItem("b1", "中文1"), BatchItem("b2", "中文2"), ...]
        result = await bt.translate_batch(items)
        for item_id, vi in result.translated.items():
            ...
    """

    def __init__(
        self,
        pool: LLMPool,
        *,
        cache: TranslationCache | None = None,
        max_chars_per_batch: int = DEFAULT_MAX_CHARS_PER_BATCH,
        max_items_per_batch: int = DEFAULT_MAX_ITEMS_PER_BATCH,
        max_retries: int = 1,
    ) -> None:
        self._pool = pool
        self._cache = cache
        self._max_chars = max_chars_per_batch
        self._max_items = max_items_per_batch
        self._max_retries = max_retries
        self._log = logger.bind(component="BatchTranslator")

    # ====================================================================
    # Public API
    # ====================================================================

    async def translate_batch(
        self,
        items: Sequence[BatchItem],
    ) -> BatchTranslateResult:
        """Dịch list[BatchItem] với batching + cache + glossary fast-path.

        Pipeline cho mỗi item:
          1. Skip nếu rỗng / không CJK → đưa vào result.translated (text gốc)
          2. Glossary fast-path → result.translated[id] = VI
          3. Cache lookup → hit → result.translated[id] = cached
          4. Group items còn lại thành batches theo size + char budget
          5. Mỗi batch → 1 LLM call → parse → verify → cache PUT
          6. Batch fail format → split đôi và recurse (binary fallback)
          7. Batch size = 1 vẫn fail → ghi vào result.failed
        """
        result = BatchTranslateResult()

        # Phase 1-3: skip + glossary + cache filter
        pending: list[BatchItem] = []
        for it in items:
            if not it.text or not it.text.strip():
                result.translated[it.item_id] = it.text
                result.skip_no_cjk += 1
                continue
            if not has_cjk(it.text):
                result.translated[it.item_id] = it.text
                result.skip_no_cjk += 1
                continue
            gloss_hit = glossary_lookup(it.text.strip())
            if gloss_hit and len(it.text.strip()) <= 20:
                result.translated[it.item_id] = gloss_hit
                result.glossary_hits += 1
                continue
            if self._cache:
                cached = await self._cache.get(_cache_key(it.text))
                if cached is not None:
                    result.translated[it.item_id] = cached
                    result.cache_hits += 1
                    continue
            pending.append(it)

        # Phase 4-7: batch + LLM
        await self._process_pending(pending, result)
        return result

    # ====================================================================
    # Internal
    # ====================================================================

    async def _process_pending(
        self,
        pending: Sequence[BatchItem],
        result: BatchTranslateResult,
    ) -> None:
        """Group thành batches + xử lý từng batch."""
        for batch in self._group_batches(pending):
            await self._translate_one_batch(batch, result)

    def _group_batches(
        self,
        items: Sequence[BatchItem],
    ) -> list[list[BatchItem]]:
        """Greedy pack items vào batches theo char budget + item count."""
        batches: list[list[BatchItem]] = []
        current: list[BatchItem] = []
        current_chars = 0
        for it in items:
            it_chars = len(it.text)
            # If single item exceeds char budget, give it its own batch
            if it_chars >= self._max_chars and not current:
                batches.append([it])
                continue
            # Would exceeding either budget? Flush.
            if current and (
                current_chars + it_chars > self._max_chars
                or len(current) >= self._max_items
            ):
                batches.append(current)
                current = []
                current_chars = 0
            current.append(it)
            current_chars += it_chars
        if current:
            batches.append(current)
        return batches

    async def _translate_one_batch(
        self,
        batch: list[BatchItem],
        result: BatchTranslateResult,
    ) -> None:
        """Translate 1 batch. Tự fallback split nếu format lệch."""
        if not batch:
            return

        # Try LLM call với retry quality-gated
        parsed = await self._call_llm_with_parse(batch)

        if parsed is not None:
            # Parse OK — verify quality từng item
            await self._absorb_parsed(batch, parsed, result)
            return

        # Format lệch — split đôi recurse
        if len(batch) == MIN_BATCH_SIZE:
            # Bất khả: 1 item vẫn fail format → ghi failed
            it = batch[0]
            result.failed[it.item_id] = "format_fail_at_size_1"
            self._log.warning(
                "batch_translate_fail_size_1",
                item_id=it.item_id,
                text_len=len(it.text),
            )
            return

        mid = len(batch) // 2
        self._log.info("batch_split_fallback", from_size=len(batch), mid=mid)
        await self._translate_one_batch(batch[:mid], result)
        await self._translate_one_batch(batch[mid:], result)

    async def _call_llm_with_parse(
        self,
        batch: list[BatchItem],
    ) -> dict[int, str] | None:
        """Gọi LLM, retry với strict prompt nếu fail format."""
        user_msg = _build_user_message(batch)
        for attempt in range(self._max_retries + 1):
            strict = attempt > 0
            system = _build_batch_system_prompt(strict=strict)
            try:
                raw = await self._pool.chat([
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ])
            except Exception as e:  # any LLM error
                self._log.warning(
                    "llm_call_failed",
                    err=str(e)[:120],
                    attempt=attempt,
                    batch_size=len(batch),
                )
                continue
            parsed = parse_batch_response(raw, expected_count=len(batch))
            if parsed is not None:
                return parsed
            self._log.warning(
                "batch_parse_fail",
                attempt=attempt,
                batch_size=len(batch),
            )
        return None

    async def _absorb_parsed(
        self,
        batch: list[BatchItem],
        parsed: dict[int, str],
        result: BatchTranslateResult,
    ) -> None:
        """Verify quality + absorb vào result + cache PUT cho item pass."""
        result.llm_calls += 1
        result.items_per_call.append(len(batch))

        for idx, item in enumerate(batch):
            translated = parsed.get(idx, "")
            report = assess_quality(item.text, translated)
            if report.passed:
                result.translated[item.item_id] = translated
                if self._cache:
                    await self._cache.put(_cache_key(item.text), translated)
            elif translated:
                # Best-effort: vẫn dùng output dù fail gate (caller có thể
                # post-validate). Không cache để tránh poison.
                result.translated[item.item_id] = translated
                self._log.warning(
                    "quality_gate_fail_but_kept",
                    item_id=item.item_id,
                    issues=[i.value for i in report.issues],
                )
            else:
                result.failed[item.item_id] = "empty_translation"


def _cache_key(text: str) -> str:
    """Cache key cho batch — share với Translator content-mode key."""
    import hashlib
    h = hashlib.sha256()
    h.update(b"vi\x00content\x00")
    h.update(text.encode("utf-8"))
    return h.hexdigest()
