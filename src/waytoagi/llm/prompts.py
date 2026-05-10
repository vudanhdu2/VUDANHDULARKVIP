# ruff: noqa: E501
"""Prompt engineering cho dịch CN → VI — layered, strict, có few-shot.

Triết lý:
  - **Thuần Việt, văn phong tự nhiên**: ưu tiên cách diễn đạt người Việt
    đọc thấy mượt, không bị "dịch máy cứng" word-by-word.
  - **Layered**: tách system rules + style guide + glossary + few-shot.
    Mỗi layer test riêng được.
  - **Strict output**: LLM chỉ trả về phần dịch, KHÔNG meta, KHÔNG markdown
    wrapper, KHÔNG giải thích.
  - **Quality gate**: prompt yêu cầu zero CJK, đủ dấu, dùng Unicode chuẩn.

2 prompts public:
  - `build_content_prompt()` — dịch nội dung dài (block, paragraph)
  - `build_title_prompt()` — dịch tiêu đề ngắn (≤200 chars)

Có biến thể `strict=True` cho retry sau quality-gate fail (siết chặt hơn).
"""

from __future__ import annotations

from waytoagi.llm.glossary import render_glossary

# ============================================================================
# CORE RULES — luôn áp dụng, viết ngắn gọn để LLM follow
# ============================================================================
_CORE_RULES = """\
NHIỆM VỤ: Dịch văn bản Trung Quốc sang tiếng Việt cho người Việt đọc.

YÊU CẦU CỨNG:
1. THUẦN VIỆT, văn phong TỰ NHIÊN — viết như người Việt nói chuyện, KHÔNG dịch máy cứng.
2. CÓ DẤU đầy đủ (Unicode chuẩn): á à ả ã ạ â ầ ấ ẩ ẫ ậ ă ằ ắ ẳ ẵ ặ đ é è ẻ ẽ ẹ ê ề ế ể ễ ệ í ì ỉ ĩ ị ó ò ỏ õ ọ ô ồ ố ổ ỗ ộ ơ ờ ớ ở ỡ ợ ú ù ủ ũ ụ ư ừ ứ ử ữ ự ý ỳ ỷ ỹ ỵ.
3. ZERO ký tự Hán/CJK trong output. Tên người TQ → Hán-Việt CÓ DẤU. Brand TQ → Latin.
4. GIỮ NGUYÊN: URL, email, code (\\`...\\`), tên file, biến số, emoji, số liệu, markdown syntax.
5. TRẢ VỀ CHỈ phần dịch — không thêm "Dịch:", "Bản dịch:", không quote, không wrapper.
"""

# ============================================================================
# STYLE GUIDE — định hướng văn phong tự nhiên
# ============================================================================
_STYLE_GUIDE = """\
VĂN PHONG TỰ NHIÊN (rất quan trọng):
- Câu NGẮN, GỌN. Câu CN dài → tách thành 2-3 câu Việt ngắn.
- Ưu tiên CÂU CHỦ ĐỘNG ("AI tạo ảnh") thay vì bị động ("Ảnh được tạo bởi AI").
- Dùng từ THUẦN VIỆT khi có sẵn:
    "sử dụng" → "dùng";  "tiến hành" → "làm";  "thực hiện" → "làm";
    "đối với" → "với";   "thông qua" → "qua";  "nhằm" → "để";
    "bởi vì" → "vì";     "có thể" → "có thể" (giữ);
    "việc..." → bỏ "việc" nếu thừa.
- Xưng hô: gọi "bạn" (KHÔNG "ngài", "quý vị", "các bạn" lặp).
- Tránh đệm Hán-Việt nặng nếu có từ Việt thay được:
    "tối ưu hóa" → "tối ưu";  "khả năng" → "có thể";
    "phương thức" → "cách";   "hiện tượng" → "việc";
    "tình huống" → "trường hợp";  "thực hiện việc" → "làm".
- Câu hỏi rhetorical CN ("是不是…呢?") → câu khẳng định Việt rõ ý.
- Liên từ rườm: "trong khi đó", "trên thực tế", "nói chung là" → cắt nếu không cần.
- Dùng số: "3 cách" thay "ba cách" trong văn bản kỹ thuật.
"""

# ============================================================================
# FEW-SHOT EXAMPLES — dạy LLM style qua ví dụ cụ thể
# ============================================================================
_FEW_SHOT_CONTENT = """\
VÍ DỤ ĐÚNG:

Input CN:
通过提示词工程,我们可以让大模型生成更准确的内容。例如,如果你想让 AI 写一篇关于飞书的文章,
可以先给它一个清晰的指令,然后再补充上下文信息。

Output VI (TỐT — câu ngắn, chủ động, thuần Việt):
Bằng kỹ thuật prompt, ta có thể giúp mô hình lớn tạo nội dung chính xác hơn. \
Ví dụ, nếu bạn muốn AI viết bài về Feishu, hãy cho nó một chỉ dẫn rõ ràng trước, rồi bổ sung ngữ cảnh sau.

---

Input CN:
小互老师在课程中分享了如何使用通义灵码进行代码生成,这是一个非常实用的 AI 编程工具。

Output VI (TỐT — tên Hán-Việt, brand Latin, câu mượt):
Thầy Tiểu Hỗ chia sẻ trong khóa học cách dùng Tongyi Lingma để sinh code. Đây là công cụ lập trình AI rất hữu ích.

---

Input CN:
注意:本文档仅供学习参考,请勿用于商业用途。

Output VI (TỐT — gọn, đúng tone cảnh báo):
Lưu ý: Tài liệu này chỉ dùng để học, không dùng cho mục đích thương mại.
"""

_FEW_SHOT_TITLE = """\
VÍ DỤ DỊCH TIÊU ĐỀ:

CN: 详解:DeepSeek深度推理+联网搜索 目前断档第一
VI: Phân tích chi tiết: DeepSeek suy luận sâu + tìm kiếm online — hiện đang dẫn đầu

CN: 【PROMPT共学快闪】文理兼修话 PROMPT
VI: [Học prompt chớp nhoáng] Bàn về prompt — kết hợp văn và lý

CN: 飞书多维表格自动化教程
VI: Hướng dẫn tự động hoá Feishu Bitable

CN: AI工具导航(2024更新版)
VI: Danh bạ công cụ AI (bản cập nhật 2024)
"""


def build_content_prompt(*, strict: bool = False) -> str:
    """System prompt cho dịch nội dung dài.

    Args:
        strict: True khi retry sau quality-gate fail — siết chặt thêm
            warning về CJK leak.
    """
    blocks = [
        _CORE_RULES,
        "",
        _STYLE_GUIDE,
        "",
        "BẢNG THUẬT NGỮ:",
        render_glossary(max_per_section=8),
        "",
        _FEW_SHOT_CONTENT,
    ]
    if strict:
        blocks.append(
            "\n⚠️ LẦN TRƯỚC OUTPUT CÒN KÝ TỰ HÁN. LẦN NÀY TUYỆT ĐỐI ZERO CJK. "
            "Mọi ký tự 一-鿿 PHẢI dịch hoặc bỏ. Mọi tên người TQ PHẢI Hán-Việt có dấu.",
        )
    return "\n".join(blocks)


def build_title_prompt(*, strict: bool = False) -> str:
    """System prompt cho dịch tiêu đề (ngắn, súc tích, ZERO CJK)."""
    blocks = [
        "Dịch TIÊU ĐỀ Trung→Việt cho tài liệu AI/Feishu.",
        "",
        "YÊU CẦU CỨNG:",
        "1. ZERO ký tự Hán/CJK. Tên TQ → Hán-Việt CÓ DẤU. Brand TQ → Latin.",
        "2. CÓ DẤU đầy đủ (Unicode chuẩn).",
        "3. NGẮN, RÕ Ý, văn phong người Việt đọc tự nhiên.",
        "4. Giữ emoji, số, ký hiệu (【】, :, |) nếu có.",
        "5. CHỈ trả tiêu đề — không quote, không thêm prefix/suffix.",
        "",
        "BẢNG THUẬT NGỮ (entries quan trọng):",
        render_glossary(max_per_section=5),
        "",
        _FEW_SHOT_TITLE,
    ]
    if strict:
        blocks.append(
            "\n⚠️ LẦN TRƯỚC OUTPUT CÒN HÁN. LẦN NÀY ZERO CJK TUYỆT ĐỐI.",
        )
    return "\n".join(blocks)
