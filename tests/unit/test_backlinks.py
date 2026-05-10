"""Unit tests cho waytoagi.backlinks module."""

from __future__ import annotations

import pytest

from waytoagi.backlinks import UrlMapper, fix_elements
from waytoagi.models.base import BaseRecord
from waytoagi.models.docs import Element, MentionDoc, TextElementStyle, TextRun


@pytest.fixture
def mapper() -> UrlMapper:
    """Sample mapper với 3 entries."""
    return UrlMapper(
        source_domain="waytoagi.feishu.cn",
        dst_domain="vudanhdu.sg.larksuite.com",
        mapping={
            "CNtokenA1234": "DSTtokenA1234",
            "CNtokenB5678": "DSTtokenB5678",
            "CNtokenC9012": "DSTtokenC9012",
        },
    )


class TestUrlMapper:
    def test_replace_basic(self, mapper: UrlMapper) -> None:
        url = "https://waytoagi.feishu.cn/wiki/CNtokenA1234"
        new, replaced = mapper.replace(url)
        assert replaced is True
        assert new == "https://vudanhdu.sg.larksuite.com/wiki/DSTtokenA1234"

    def test_preserve_anchor(self, mapper: UrlMapper) -> None:
        url = "https://waytoagi.feishu.cn/wiki/CNtokenA1234#section-2"
        new, replaced = mapper.replace(url)
        assert replaced is True
        assert new == "https://vudanhdu.sg.larksuite.com/wiki/DSTtokenA1234#section-2"

    def test_preserve_query(self, mapper: UrlMapper) -> None:
        url = "https://waytoagi.feishu.cn/wiki/CNtokenB5678?param=foo&bar=1"
        new, replaced = mapper.replace(url)
        assert replaced is True
        assert (
            new
            == "https://vudanhdu.sg.larksuite.com/wiki/DSTtokenB5678?param=foo&bar=1"
        )

    def test_unknown_token_no_replace(self, mapper: UrlMapper) -> None:
        url = "https://waytoagi.feishu.cn/wiki/UNKNOWN_TOKEN"
        new, replaced = mapper.replace(url)
        assert replaced is False
        assert new == url

    def test_different_domain_no_replace(self, mapper: UrlMapper) -> None:
        url = "https://example.com/wiki/CNtokenA1234"
        _new, replaced = mapper.replace(url)
        assert replaced is False

    def test_idempotent_already_dst(self, mapper: UrlMapper) -> None:
        """URL đã trỏ DST → KHÔNG touch."""
        url = "https://vudanhdu.sg.larksuite.com/wiki/DSTtokenA1234"
        new, replaced = mapper.replace(url)
        assert replaced is False
        assert new == url

    def test_url_encoded(self, mapper: UrlMapper) -> None:
        """URL bị URL-encode (thường khi qua Lark API)."""
        url = "https%3A%2F%2Fwaytoagi.feishu.cn%2Fwiki%2FCNtokenA1234"
        new, replaced = mapper.replace(url)
        assert replaced is True
        assert "DSTtokenA1234" in new

    def test_docx_path_also_replaced(self, mapper: UrlMapper) -> None:
        """/docx/, /base/, /sheets/ paths cũng replace."""
        url = "https://waytoagi.feishu.cn/docx/CNtokenC9012"
        new, replaced = mapper.replace(url)
        assert replaced is True
        # Always normalize sang /wiki/ path trên DST
        assert "/wiki/DSTtokenC9012" in new

    def test_empty_url(self, mapper: UrlMapper) -> None:
        new, replaced = mapper.replace("")
        assert replaced is False
        assert new == ""

    def test_from_records(self) -> None:
        recs = [
            BaseRecord.model_validate({
                "record_id": "r1",
                "Node Token": "CN_A",
                "Mirror Wiki Node Token": "DST_A",
            }),
            BaseRecord.model_validate({
                "record_id": "r2",
                "Node Token": "CN_B",
                "Mirror Wiki Node Token": "",  # no mirror — skip
            }),
            BaseRecord.model_validate({
                "record_id": "r3",
                "Node Token": "CN_C",
                "Mirror Wiki Node Token": "DST_C",
            }),
        ]
        mapper = UrlMapper.from_records(
            recs, source_domain="src.feishu.cn", dst_domain="dst.larksuite.com"
        )
        assert len(mapper) == 2  # r2 skipped
        assert mapper.mapping["CN_A"] == "DST_A"
        assert mapper.mapping["CN_C"] == "DST_C"
        assert "CN_B" not in mapper.mapping


class TestFixElements:
    def test_text_run_link_replaced(self, mapper: UrlMapper) -> None:
        elements = [
            Element(
                text_run=TextRun(
                    content="Click here",
                    text_element_style=TextElementStyle(
                        link={"url": "https://waytoagi.feishu.cn/wiki/CNtokenA1234"},
                    ),
                ),
            ),
        ]
        new, count = fix_elements(elements, mapper)
        assert count == 1
        assert new[0].text_run is not None
        assert new[0].text_run.text_element_style is not None
        assert new[0].text_run.text_element_style.link is not None
        assert (
            new[0].text_run.text_element_style.link["url"]
            == "https://vudanhdu.sg.larksuite.com/wiki/DSTtokenA1234"
        )

    def test_mention_doc_url_replaced(self, mapper: UrlMapper) -> None:
        elements = [
            Element(
                mention_doc=MentionDoc(
                    token="CNtokenB5678",
                    title="Sample doc",
                    url="https://waytoagi.feishu.cn/wiki/CNtokenB5678",
                ),
            ),
        ]
        new, count = fix_elements(elements, mapper)
        assert count == 1
        assert new[0].mention_doc is not None
        assert (
            new[0].mention_doc.url
            == "https://vudanhdu.sg.larksuite.com/wiki/DSTtokenB5678"
        )

    def test_mixed_elements(self, mapper: UrlMapper) -> None:
        elements = [
            Element(text_run=TextRun(content="Plain text, no link")),
            Element(
                text_run=TextRun(
                    content="Linked",
                    text_element_style=TextElementStyle(
                        link={"url": "https://waytoagi.feishu.cn/wiki/CNtokenA1234"},
                    ),
                ),
            ),
            Element(
                mention_doc=MentionDoc(
                    token="CNtokenB5678",
                    url="https://waytoagi.feishu.cn/wiki/CNtokenB5678",
                ),
            ),
            Element(
                text_run=TextRun(
                    content="External link",
                    text_element_style=TextElementStyle(
                        link={"url": "https://google.com"},
                    ),
                ),
            ),
        ]
        new, count = fix_elements(elements, mapper)
        assert count == 2  # 2 internal links replaced, external untouched
        # Verify external link preserved
        assert new[3].text_run is not None
        assert new[3].text_run.text_element_style is not None
        assert new[3].text_run.text_element_style.link is not None
        assert new[3].text_run.text_element_style.link["url"] == "https://google.com"

    def test_no_match_no_change(self, mapper: UrlMapper) -> None:
        elements = [
            Element(text_run=TextRun(content="Just plain text")),
        ]
        new, count = fix_elements(elements, mapper)
        assert count == 0
        assert new == elements
