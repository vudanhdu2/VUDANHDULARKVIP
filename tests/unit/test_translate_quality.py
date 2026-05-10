"""Unit tests cho `waytoagi.llm.quality` + `waytoagi.llm.glossary` +
`waytoagi.llm.prompts` — pure functions, không cần mock LLM.

Coverage:
  - clean_artifacts: strip wrapper, prefix, normalize NFC
  - detect_cjk_leak: count Hán
  - detect_unaccented_vietnamese: count from raw/telex
  - has_vi_diacritics: positive + negative
  - looks_untranslated: heuristic ratio CJK
  - assess_quality: composite pass/fail logic
  - glossary lookup + render
  - prompts: build_content_prompt + build_title_prompt
"""

from __future__ import annotations

import pytest

from waytoagi.llm.glossary import (
    AI_TOOLS,
    BRANDS,
    PEOPLE_NAMES,
    PHRASES,
    TECH_TERMS,
    lookup,
    render_glossary,
)
from waytoagi.llm.prompts import build_content_prompt, build_title_prompt
from waytoagi.llm.quality import (
    QualityIssue,
    assess_quality,
    clean_artifacts,
    detect_cjk_leak,
    detect_unaccented_vietnamese,
    has_cjk,
    has_vi_diacritics,
    looks_untranslated,
)


@pytest.mark.unit
class TestCleanArtifacts:
    def test_strip_code_fence_wrapper(self) -> None:
        text = "```vi\nXin chào bạn\n```"
        assert clean_artifacts(text) == "Xin chào bạn"

    def test_strip_markdown_wrapper(self) -> None:
        text = "```markdown\nNội dung dịch\n```"
        assert clean_artifacts(text) == "Nội dung dịch"

    def test_strip_quote_wrapper(self) -> None:
        text = '"Xin chào"'
        assert clean_artifacts(text) == "Xin chào"

    def test_strip_prefix_ban_dich(self) -> None:
        text = "Bản dịch: Xin chào bạn"
        assert clean_artifacts(text) == "Xin chào bạn"

    def test_strip_prefix_dich(self) -> None:
        text = "Dịch: Đây là nội dung"
        assert clean_artifacts(text) == "Đây là nội dung"

    def test_strip_prefix_translation(self) -> None:
        text = "Translation: Hello world"
        assert clean_artifacts(text) == "Hello world"

    def test_idempotent(self) -> None:
        text = "Xin chào bạn"
        assert clean_artifacts(clean_artifacts(text)) == text

    def test_normalize_unicode_nfc(self) -> None:
        # Decomposed form: o + combining circumflex
        decomposed = "ô"  # ô = o + ̂
        out = clean_artifacts(decomposed)
        assert out == "ô"

    def test_empty_input(self) -> None:
        assert clean_artifacts("") == ""

    def test_preserves_normal_text(self) -> None:
        text = "Đây là văn bản tiếng Việt bình thường."
        assert clean_artifacts(text) == text


@pytest.mark.unit
class TestDetectCJK:
    def test_no_cjk(self) -> None:
        assert detect_cjk_leak("Xin chào bạn") == 0
        assert has_cjk("Xin chào bạn") is False

    def test_single_cjk(self) -> None:
        assert detect_cjk_leak("飞书 là Feishu") == 2  # 飞 + 书
        assert has_cjk("飞书 là Feishu") is True

    def test_pure_cjk(self) -> None:
        assert detect_cjk_leak("通往AGI之路") == 4  # 通 往 之 路 (AGI Latin)

    def test_empty(self) -> None:
        assert detect_cjk_leak("") == 0
        assert has_cjk("") is False

    def test_extension_block(self) -> None:
        # CJK Extension A: 㐀 (U+3400)
        assert detect_cjk_leak("㐀") == 1


@pytest.mark.unit
class TestUnaccentedVietnamese:
    def test_pure_unaccented_words(self) -> None:
        # 4 từ raw (the không có trong regex) → trên ngưỡng 3 → flag
        text = "khong duoc viec nay nguoi"
        assert detect_unaccented_vietnamese(text, min_words=3) == 5

    def test_below_threshold(self) -> None:
        # 2 raw words, min 3 → 0
        text = "Xin chao ban — only 2 raw"
        assert detect_unaccented_vietnamese(text, min_words=3) == 0

    def test_proper_vietnamese(self) -> None:
        text = "Đây là bản dịch tiếng Việt có dấu đầy đủ."
        assert detect_unaccented_vietnamese(text) == 0

    def test_mixed_with_latin_names(self) -> None:
        # Tên Latin không bị flag
        text = "Tongyi Lingma là công cụ tốt"
        assert detect_unaccented_vietnamese(text) == 0


@pytest.mark.unit
class TestHasViDiacritics:
    def test_with_diacritics(self) -> None:
        assert has_vi_diacritics("Xin chào bạn") is True
        assert has_vi_diacritics("Hà Nội") is True

    def test_without_diacritics(self) -> None:
        assert has_vi_diacritics("Hello world") is False
        assert has_vi_diacritics("Tongyi Lingma") is False

    def test_empty(self) -> None:
        assert has_vi_diacritics("") is False


@pytest.mark.unit
class TestLooksUntranslated:
    def test_ouput_keeps_most_cjk(self) -> None:
        in_text = "通往AGI之路"  # 4 CJK
        out_text = "通往 AGI 之路"  # 4 CJK still
        assert looks_untranslated(in_text, out_text) is True

    def test_output_translated(self) -> None:
        in_text = "通往AGI之路"
        out_text = "Con đường tới AGI"
        assert looks_untranslated(in_text, out_text) is False

    def test_input_no_cjk(self) -> None:
        # Input không CJK → không thể "untranslated"
        assert looks_untranslated("hello", "hello") is False

    def test_empty_output_with_input(self) -> None:
        assert looks_untranslated("通往", "") is True

    def test_empty_both(self) -> None:
        assert looks_untranslated("", "") is False


@pytest.mark.unit
class TestAssessQuality:
    def test_pass_clean_translation(self) -> None:
        report = assess_quality(
            "通往AGI之路",
            "Con đường tới AGI",
        )
        assert report.passed is True
        assert report.issues == ()
        assert report.cjk_count == 0

    def test_fail_cjk_leak(self) -> None:
        report = assess_quality("通往AGI之路", "Con đường tới 通AGI")
        assert report.passed is False
        assert QualityIssue.CJK_LEAK in report.issues
        assert report.cjk_count == 1

    def test_fail_empty_output(self) -> None:
        report = assess_quality("通往AGI之路", "")
        assert report.passed is False
        assert QualityIssue.EMPTY_OUTPUT in report.issues

    def test_fail_looks_untranslated(self) -> None:
        report = assess_quality("通往AGI之路", "通往AGI之路")
        assert report.passed is False
        assert QualityIssue.LOOKS_UNTRANSLATED in report.issues

    def test_warning_unaccented_not_critical(self) -> None:
        # Unaccented detected nhưng không critical → vẫn pass
        report = assess_quality(
            "你好",
            "xin chao ban toi viec nay",  # 5 raw words, no CJK
        )
        assert QualityIssue.UNACCENTED_VIETNAMESE in report.issues
        # NOT critical → passed=True
        assert report.passed is True

    def test_suspicious_ratio_too_short(self) -> None:
        # Input dài, output quá ngắn → suspicious
        in_text = "通往AGI之路 — 这是一个很长的中文文本,需要翻译。" * 3
        report = assess_quality(in_text, "AGI", min_length_ratio=0.2)
        assert QualityIssue.SUSPICIOUS_RATIO in report.issues

    def test_short_input_no_ratio_check(self) -> None:
        # Input < 20 chars → skip ratio check
        report = assess_quality("你好", "Xin chào")
        assert QualityIssue.SUSPICIOUS_RATIO not in report.issues

    def test_detail_string(self) -> None:
        report = assess_quality(
            "通往AGI之路",
            "Con đường tới AGI",
        )
        assert "ratio=" in report.detail


@pytest.mark.unit
class TestGlossary:
    def test_lookup_brand(self) -> None:
        assert lookup("飞书") == "Feishu"
        assert lookup("微信") == "WeChat"

    def test_lookup_person(self) -> None:
        assert lookup("小互") == "Tiểu Hỗ"

    def test_lookup_ai_tool(self) -> None:
        assert lookup("通义灵码") == "Tongyi Lingma"
        assert lookup("剪映") == "CapCut"

    def test_lookup_phrase(self) -> None:
        assert lookup("通往AGI之路") == "Con đường tới AGI"

    def test_lookup_miss(self) -> None:
        assert lookup("不在表中") is None

    def test_render_glossary_contains_all_sections(self) -> None:
        rendered = render_glossary()
        assert "Hán-Việt" in rendered or "người TQ" in rendered
        assert "Latin" in rendered  # Brands section
        assert "AI" in rendered or "Tongyi" in rendered

    def test_render_glossary_max_per_section(self) -> None:
        rendered = render_glossary(max_per_section=2)
        # Mỗi section ≤ 2 entry
        # Sanity check: total length thấp
        assert len(rendered) < len(render_glossary(max_per_section=20))

    def test_brands_no_dict_overlap(self) -> None:
        """Tên brand không nên trùng nhân vật/tool."""
        all_keys = set(BRANDS) | set(PEOPLE_NAMES) | set(AI_TOOLS)
        # Đếm: nếu unique = sum, không có overlap
        assert len(all_keys) == len(BRANDS) + len(PEOPLE_NAMES) + len(AI_TOOLS)

    def test_phrases_and_tech_distinct(self) -> None:
        overlap = set(PHRASES) & set(TECH_TERMS)
        assert overlap == set(), f"Phrases vs tech terms overlap: {overlap}"


@pytest.mark.unit
class TestBuildPrompts:
    def test_content_prompt_contains_core_rules(self) -> None:
        p = build_content_prompt()
        assert "THUẦN VIỆT" in p
        assert "ZERO" in p  # zero CJK rule
        assert "CÓ DẤU" in p

    def test_content_prompt_contains_style_guide(self) -> None:
        p = build_content_prompt()
        assert "VĂN PHONG" in p or "văn phong" in p.lower()
        assert "câu" in p.lower()
        assert "thuần" in p.lower() or "thuần Việt" in p

    def test_content_prompt_contains_glossary(self) -> None:
        p = build_content_prompt()
        assert "飞书" in p  # Brand entry
        assert "Feishu" in p

    def test_content_prompt_contains_few_shot(self) -> None:
        p = build_content_prompt()
        assert "VÍ DỤ" in p
        # Must have both Input + Output examples
        assert "Input" in p
        assert "Output" in p

    def test_strict_mode_adds_warning(self) -> None:
        normal = build_content_prompt(strict=False)
        strict = build_content_prompt(strict=True)
        assert len(strict) > len(normal)
        assert "ZERO CJK" in strict or "Hán" in strict

    def test_title_prompt_shorter_than_content(self) -> None:
        title = build_title_prompt()
        content = build_content_prompt()
        # Title prompt should be shorter (less style guide)
        assert len(title) < len(content)

    def test_title_prompt_has_examples(self) -> None:
        p = build_title_prompt()
        assert "VÍ DỤ" in p
        assert "DeepSeek" in p  # Few-shot example

    def test_title_prompt_strict_adds_warning(self) -> None:
        normal = build_title_prompt(strict=False)
        strict = build_title_prompt(strict=True)
        assert len(strict) > len(normal)
