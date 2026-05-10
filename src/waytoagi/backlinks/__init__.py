"""Backlink fix module — replace source URLs với DST URLs.

Khi clone CN → DST, URLs trong content vẫn trỏ source domain (vd waytoagi.feishu.cn).
Cần replace sang DST domain (vd vudanhdu.sg.larksuite.com) để link nội bộ
hoạt động đúng.

Public API:
    UrlMapper       — build CN→DST mapping từ Base records
    replace_url     — replace 1 URL nếu có trong mapping
    fix_elements    — replace URLs trong list[Element] (text_run + mention_doc)
"""

from waytoagi.backlinks.fixer import fix_elements
from waytoagi.backlinks.mapper import UrlMapper, replace_url

__all__ = ["UrlMapper", "fix_elements", "replace_url"]
