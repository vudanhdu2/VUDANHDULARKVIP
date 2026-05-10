"""Replace URLs trong list of Element (text_run + mention_doc).

Áp dụng cho 2 path:
  1. Inline khi clone (CN → DST block recreate) — fix elements TRƯỚC khi PATCH
  2. Inline khi sync (sau khi update_docx_in_place) — scan dst doc full + fix
"""

from __future__ import annotations

from waytoagi.backlinks.mapper import UrlMapper
from waytoagi.models.docs import Element


def fix_elements(
    elements: list[Element], mapper: UrlMapper
) -> tuple[list[Element], int]:
    """Replace URLs trong list of Element.

    Iterate qua mỗi element:
      - text_run.text_element_style.link.url → replace nếu match
      - mention_doc.url → replace nếu match

    Args:
        elements: list of Element (parsed Lark Docx blocks)
        mapper: UrlMapper instance

    Returns:
        (new_elements, count_replaced):
          - new_elements: list mới với URLs đã replace
          - count_replaced: số URLs replaced
    """
    new_elements: list[Element] = []
    count_replaced = 0

    for el in elements:
        new_el = el.model_copy(deep=True)

        # text_run.style.link.url
        if new_el.text_run and new_el.text_run.text_element_style:
            link = new_el.text_run.text_element_style.link
            if link and link.get("url"):
                new_url, replaced = mapper.replace(link["url"])
                if replaced:
                    new_el.text_run.text_element_style.link = {"url": new_url}
                    count_replaced += 1

        # mention_doc.url
        if new_el.mention_doc and new_el.mention_doc.url:
            new_url, replaced = mapper.replace(new_el.mention_doc.url)
            if replaced:
                new_el.mention_doc.url = new_url
                count_replaced += 1

        new_elements.append(new_el)

    return new_elements, count_replaced
