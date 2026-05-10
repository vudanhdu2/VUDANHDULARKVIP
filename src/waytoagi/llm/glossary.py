"""Glossary CN→VI chuẩn cho dịch tài liệu WaytoAGI.

Tách riêng module để dễ maintain + test + extend. Glossary được inject
vào system prompt dưới dạng bảng, giúp LLM nhất quán giữa các record.

Chia 4 nhóm:
  - PEOPLE_NAMES: nhân vật, tác giả TQ → Hán-Việt CÓ DẤU
  - BRANDS: thương hiệu → Latin (KHÔNG dịch)
  - AI_TOOLS: công cụ AI TQ → Latin name
  - PHRASES: cụm thông dụng → Việt tự nhiên
  - TECH_TERMS: thuật ngữ kỹ thuật → Việt rõ nghĩa

Ưu tiên:
  1. Tên người TQ → Hán-Việt có dấu (KHÔNG để raw 小互, 雪梅)
  2. Brand TQ → Latin (KHÔNG dịch nghĩa: 飞书→Feishu, KHÔNG → "Thư bay")
  3. Thuật ngữ AI giữ Latin nếu phổ biến (prompt, embedding, agent…)
  4. Câu/cụm thuần Việt → văn phong tự nhiên người Việt đọc
"""

from __future__ import annotations

# ============================================================================
# 1. NHÂN VẬT, TÁC GIẢ — Hán-Việt có dấu
# ============================================================================
PEOPLE_NAMES: dict[str, str] = {
    "小互": "Tiểu Hỗ",
    "黄叔": "Hoàng Thúc",
    "雪梅": "Tuyết Mai",
    "陈财猫": "Trần Tài Mèo",
    "南瓜博士": "Tiến sĩ Bí Ngô",
    "李继刚": "Lý Kế Cương",
    "王建硕": "Vương Kiến Thạc",
    "宝玉": "Bảo Ngọc",
    "歸藏": "Quy Tàng",
    "向阳乔木": "Hướng Dương Kiều Mộc",
    "卡兹克": "Kazik",
    "宝碎念": "Bảo Toái Niệm",
}

# ============================================================================
# 2. THƯƠNG HIỆU — KHÔNG dịch, dùng Latin
# ============================================================================
BRANDS: dict[str, str] = {
    "飞书": "Feishu",
    "钉钉": "DingTalk",
    "微信": "WeChat",
    "抖音": "Douyin",
    "快手": "Kuaishou",
    "小红书": "Xiaohongshu",
    "知乎": "Zhihu",
    "百度": "Baidu",
    "阿里": "Alibaba",
    "腾讯": "Tencent",
    "字节跳动": "ByteDance",
    "美团": "Meituan",
    "京东": "JD",
    "淘宝": "Taobao",
    "天猫": "Tmall",
    "B站": "Bilibili",
    "哔哩哔哩": "Bilibili",
    "公众号": "Official Account",
    "视频号": "Video Channel",
}

# ============================================================================
# 3. CÔNG CỤ AI / SẢN PHẨM — Latin name
# ============================================================================
AI_TOOLS: dict[str, str] = {
    "通义灵码": "Tongyi Lingma",
    "通义千问": "Tongyi Qianwen",
    "扣子": "Coze",
    "影刀": "Yingdao",
    "豆包": "Doubao",
    "智谱": "Zhipu",
    "文心一言": "Wenxin Yiyan",
    "讯飞": "iFlytek",
    "可灵": "Kling",
    "即梦": "Jimeng",
    "海螺": "Hailuo",
    "万兴": "Wondershare",
    "剪映": "CapCut",
    "稿定": "Gaoding",
    "墨刀": "MockingBot",
    "石墨": "Shimo",
    "腾讯文档": "Tencent Docs",
    "金山办公": "Kingsoft Office",
    "WPS": "WPS",
}

# ============================================================================
# 4. CỤM THÔNG DỤNG — văn phong tự nhiên
# Ưu tiên cấu trúc gọn, dễ đọc cho người Việt
# ============================================================================
PHRASES: dict[str, str] = {
    "通往AGI之路": "Con đường tới AGI",
    "训练营": "Trại huấn luyện",
    "教程": "Hướng dẫn",
    "入门": "Nhập môn",
    "进阶": "Nâng cao",
    "实战": "Thực chiến",
    "案例": "Ví dụ thực tế",
    "经验": "Kinh nghiệm",
    "技巧": "Mẹo",
    "注意": "Lưu ý",
    "总结": "Tóm lại",
    "例如": "Ví dụ",
    "比如": "Chẳng hạn",
    "也就是说": "Nói cách khác",
    "因此": "Do đó",
    "所以": "Nên",
    "但是": "Nhưng",
    "不过": "Tuy nhiên",
    "当然": "Tất nhiên",
    "如果": "Nếu",
    "请": "Hãy",
    "建议": "Khuyên",
    "推荐": "Gợi ý",
    "参考": "Tham khảo",
    "操作": "Thao tác",
    "步骤": "Bước",
    "方法": "Cách",
    "工具": "Công cụ",
    "效果": "Kết quả",
    "应用": "Ứng dụng",
    "场景": "Tình huống",
}

# ============================================================================
# 5. THUẬT NGỮ KỸ THUẬT — Việt rõ nghĩa hoặc giữ Latin nếu phổ biến
# ============================================================================
TECH_TERMS: dict[str, str] = {
    # Giữ Latin (đã quá phổ biến trong cộng đồng Việt)
    "提示词": "prompt",
    "智能体": "agent",
    "大模型": "mô hình lớn",
    "大语言模型": "LLM",
    "多模态": "đa phương thức",
    "微调": "fine-tune",
    "训练": "huấn luyện",
    "推理": "suy luận",
    "向量": "vector",
    "嵌入": "embedding",
    "知识库": "kho tri thức",
    "知识图谱": "knowledge graph",
    "工作流": "workflow",
    "插件": "plugin",
    "接口": "API",
    "数据集": "tập dữ liệu",
    "标注": "gán nhãn",
    "推荐系统": "hệ thống gợi ý",
    "搜索引擎": "công cụ tìm kiếm",
    "图像生成": "tạo ảnh",
    "视频生成": "tạo video",
    "语音识别": "nhận dạng giọng nói",
    "语音合成": "tổng hợp giọng nói",
    "自然语言": "ngôn ngữ tự nhiên",
    "深度学习": "học sâu",
    "机器学习": "học máy",
    "强化学习": "học tăng cường",
    "上下文": "ngữ cảnh",
    "幻觉": "ảo giác (hallucination)",
}


def render_glossary(*, max_per_section: int = 8) -> str:
    """Render glossary thành text block để inject vào system prompt.

    `max_per_section` giới hạn số entry mỗi nhóm để tiết kiệm token —
    LLM thường chỉ cần biết những entry phổ biến nhất, các entry hiếm
    có thể nhầm lẫn vẫn fallback sang luật chung (Hán-Việt cho người,
    Latin cho brand).
    """
    sections = [
        ("Tên người TQ → Hán-Việt CÓ DẤU", PEOPLE_NAMES),
        ("Thương hiệu → Latin (KHÔNG dịch nghĩa)", BRANDS),
        ("Công cụ AI → Latin name", AI_TOOLS),
        ("Cụm thông dụng → văn phong Việt", PHRASES),
        ("Thuật ngữ kỹ thuật", TECH_TERMS),
    ]
    blocks: list[str] = []
    for label, table in sections:
        items = list(table.items())[:max_per_section]
        pairs = " | ".join(f"{cn}→{vi}" for cn, vi in items)
        blocks.append(f"- {label}: {pairs}")
    return "\n".join(blocks)


def lookup(text: str) -> str | None:
    """Tra cứu chính xác 1 token. Trả None nếu không có.

    Dùng cho path dịch tiêu đề ngắn — nếu match exact 1 entry, skip LLM.
    """
    for table in (PEOPLE_NAMES, BRANDS, AI_TOOLS, PHRASES, TECH_TERMS):
        if text in table:
            return table[text]
    return None
