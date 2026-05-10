"""Quality gates cho output dịch — pure functions, không I/O.

Layer post-process sau khi LLM trả về:
  1. `clean_artifacts()`: cắt prefix/suffix LLM hay thêm sai
     ("Bản dịch:", "Dịch:", quote bao quanh, markdown wrapper).
  2. `detect_cjk_leak()`: tìm ký tự Hán còn sót trong output.
  3. `detect_unaccented_vietnamese()`: tìm chuỗi Việt không dấu (telex/raw).
  4. `assess_quality()`: tổng hợp pass/fail + reason.

Mọi gate là pure function → unit test không cần mock LLM.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

# ============================================================================
# Regex patterns
# ============================================================================

# Khối CJK chính: U+4E00-U+9FFF (Hán cơ bản). Đủ cho 99% nội dung CN.
# Mở rộng thêm: U+3400-U+4DBF (Extension A), U+F900-U+FAFF (Compat).
CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")

# Markdown/quote wrapper LLM thường thêm sai
_WRAPPER_PATTERNS = [
    re.compile(r"^```(?:vietnamese|vi|markdown|text)?\s*\n", re.IGNORECASE),
    re.compile(r"\n```\s*$"),
    re.compile(r'^["""\'\']'),
    re.compile(r'["""\'\']$'),
]

# Prefix LLM hay thêm — fullwidth colon là CN punctuation cần match
_PREFIX_PATTERNS = [
    re.compile(
        r"^(?:Bản\s*dịch|Dịch|Translation|Đây là bản dịch|Sau đây là)\s*[:：]\s*",  # noqa: RUF001
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:Output|Kết quả)\s*[:：]\s*", re.IGNORECASE,  # noqa: RUF001
    ),
]

# Vietnamese characters (có dấu) — dùng để check "đã có dấu chưa"
_VI_DIACRITIC_RE = re.compile(
    r"[àáảãạâầấẩẫậăằắẳẵặđèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵ"
    r"ÀÁẢÃẠÂẦẤẨẪẬĂẰẮẲẴẶĐÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢÙÚỦŨỤƯỪỨỬỮỰỲÝỶỸỴ]",
)

# Words tiếng Việt không dấu phổ biến (raw/telex) → flag
_RAW_TELEX_WORDS = re.compile(
    r"\b(?:khong|duoc|nguoi|viec|thoi|gian|cong|hoi|nghi|huong|dan|tai|"
    r"khoa|hoc|thanh|cong|truong|thong|tin|trong|ngoai|chao|ban|toi|bay|"
    r"gio|nay|kia|do|day|kia|ay|nha|cua|tren|duoi|dau|tien|cuoi|gan|xa)\b",
    re.IGNORECASE,
)

# ============================================================================
# Models
# ============================================================================


class QualityIssue(StrEnum):
    """Lý do fail gate — dùng cho retry decision + audit."""

    CJK_LEAK = "cjk_leak"
    UNACCENTED_VIETNAMESE = "unaccented_vietnamese"
    EMPTY_OUTPUT = "empty_output"
    SUSPICIOUS_RATIO = "suspicious_ratio"
    LOOKS_UNTRANSLATED = "looks_untranslated"


@dataclass(frozen=True, slots=True)
class QualityReport:
    """Kết quả assess 1 output dịch."""

    passed: bool
    issues: tuple[QualityIssue, ...]
    cjk_count: int
    unaccented_word_count: int
    output_length: int
    input_length: int
    detail: str = ""

    @property
    def length_ratio(self) -> float:
        if self.input_length == 0:
            return 0.0
        return self.output_length / self.input_length


# ============================================================================
# Cleaners (pure functions)
# ============================================================================


def clean_artifacts(text: str) -> str:
    """Strip prefix/suffix/wrapper LLM thường thêm sai.

    Idempotent: chạy nhiều lần kết quả như nhau.
    """
    if not text:
        return text
    out = text.strip()

    # Strip wrapper code-fence + quote bao quanh, lặp đến hết
    changed = True
    while changed:
        changed = False
        for pat in _WRAPPER_PATTERNS:
            new = pat.sub("", out)
            if new != out:
                out = new.strip()
                changed = True

    # Strip prefix dạng "Bản dịch:", "Dịch:"
    for pat in _PREFIX_PATTERNS:
        new = pat.sub("", out, count=1)
        if new != out:
            out = new.lstrip()

    # Normalize Unicode (NFC) — gộp dấu rời thành ký tự kết hợp chuẩn
    out = unicodedata.normalize("NFC", out)
    return out


# ============================================================================
# Detectors (pure functions)
# ============================================================================


def detect_cjk_leak(text: str) -> int:
    """Đếm ký tự CJK còn trong output. > 0 → fail gate."""
    return len(CJK_RE.findall(text))


def has_cjk(text: str) -> bool:
    """Convenience — text có chứa CJK không (dùng để skip translate)."""
    return bool(CJK_RE.search(text))


def detect_unaccented_vietnamese(text: str, *, min_words: int = 3) -> int:
    """Đếm số từ Việt-không-dấu (raw/telex) → flag.

    Args:
        min_words: tối thiểu N từ raw mới count làm issue. Tránh false
            positive cho text chứa lác đác tên Latin.

    Returns:
        Số từ raw nếu >= min_words, else 0.
    """
    matches = _RAW_TELEX_WORDS.findall(text)
    count = len(matches)
    return count if count >= min_words else 0


def has_vi_diacritics(text: str) -> bool:
    """Text có chứa ký tự Việt có dấu không. Dùng để verify đã dịch."""
    return bool(_VI_DIACRITIC_RE.search(text))


def looks_untranslated(input_text: str, output_text: str) -> bool:
    """Heuristic: output gần giống input → khả năng cao chưa dịch.

    True khi output có > 30% ký tự CJK gốc còn sót, hoặc output rỗng
    nhưng input không rỗng.
    """
    if not output_text.strip():
        return bool(input_text.strip())
    in_cjk = detect_cjk_leak(input_text)
    out_cjk = detect_cjk_leak(output_text)
    if in_cjk == 0:
        return False
    return out_cjk / in_cjk > 0.3


# ============================================================================
# Aggregate
# ============================================================================


def assess_quality(
    input_text: str,
    output_text: str,
    *,
    min_unaccented_words: int = 3,
    min_length_ratio: float = 0.2,
    max_length_ratio: float = 5.0,
) -> QualityReport:
    """Tổng hợp đánh giá 1 output dịch.

    Args:
        input_text: nguyên bản CN.
        output_text: bản dịch VI (đã clean_artifacts).
        min_unaccented_words: ngưỡng từ raw để flag.
        min_length_ratio: output/input < ratio → suspicious (under-translation).
        max_length_ratio: output/input > ratio → suspicious (over-translation).

    Returns:
        QualityReport — passed=True nếu KHÔNG có issue critical.
    """
    issues: list[QualityIssue] = []
    cjk_count = detect_cjk_leak(output_text)
    unacc_count = detect_unaccented_vietnamese(
        output_text, min_words=min_unaccented_words,
    )
    in_len = len(input_text)
    out_len = len(output_text)

    # Empty output
    if input_text.strip() and not output_text.strip():
        issues.append(QualityIssue.EMPTY_OUTPUT)

    # CJK leak — critical
    if cjk_count > 0:
        issues.append(QualityIssue.CJK_LEAK)

    # Unaccented Vietnamese — warning level
    if unacc_count > 0:
        issues.append(QualityIssue.UNACCENTED_VIETNAMESE)

    # Looks untranslated
    if looks_untranslated(input_text, output_text):
        issues.append(QualityIssue.LOOKS_UNTRANSLATED)

    # Suspicious ratio (chỉ check khi input đủ dài để ratio có ý nghĩa)
    if in_len >= 20 and out_len > 0:
        ratio = out_len / in_len
        if ratio < min_length_ratio or ratio > max_length_ratio:
            issues.append(QualityIssue.SUSPICIOUS_RATIO)

    # passed = không có CJK_LEAK / EMPTY / LOOKS_UNTRANSLATED (critical issues)
    critical = {
        QualityIssue.CJK_LEAK,
        QualityIssue.EMPTY_OUTPUT,
        QualityIssue.LOOKS_UNTRANSLATED,
    }
    has_critical = any(i in critical for i in issues)

    detail_parts: list[str] = []
    if cjk_count:
        detail_parts.append(f"cjk={cjk_count}")
    if unacc_count:
        detail_parts.append(f"unacc={unacc_count}")
    if in_len > 0:
        detail_parts.append(f"ratio={out_len / in_len:.2f}")

    return QualityReport(
        passed=not has_critical,
        issues=tuple(issues),
        cjk_count=cjk_count,
        unaccented_word_count=unacc_count,
        output_length=out_len,
        input_length=in_len,
        detail=" ".join(detail_parts),
    )
