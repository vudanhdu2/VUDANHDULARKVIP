"""Unit tests cho waytoagi.models.base."""

from __future__ import annotations

import pytest

from waytoagi.models.base import (
    BaseRecord,
    LinkField,
    RecordStatus,
    TranslateStatus,
)


class TestLinkField:
    """LinkField parsing từ Lark API responses."""

    def test_empty(self) -> None:
        link = LinkField.from_lark(None)
        assert link.link == ""
        assert link.text == ""

    def test_dict_form(self) -> None:
        link = LinkField.from_lark({"link": "https://example.com", "text": "Example"})
        assert link.link == "https://example.com"
        assert link.text == "Example"

    def test_list_dict_form(self) -> None:
        """Lark trả list[{link,text}] cho hyperlink fields."""
        link = LinkField.from_lark([{"link": "https://example.com", "text": "Example"}])
        assert link.link == "https://example.com"
        assert link.text == "Example"

    def test_string_form(self) -> None:
        link = LinkField.from_lark("https://example.com")
        assert link.link == "https://example.com"
        assert link.text == "https://example.com"


class TestBaseRecord:
    """BaseRecord parsing từ Lark API."""

    def test_minimal_record(self) -> None:
        item = {
            "record_id": "rec123",
            "fields": {
                "STT": 100,
                "Tiêu đề": "Test record",
            },
        }
        rec = BaseRecord.from_lark_response(item)
        assert rec.record_id == "rec123"
        assert rec.stt == 100
        assert rec.tieude == "Test record"
        assert rec.trang_thai == RecordStatus.PENDING

    def test_link_fields(self) -> None:
        item = {
            "record_id": "rec123",
            "fields": {
                "Liên kết clone": [{"link": "https://abc.com", "text": "Clone"}],
                "Liên kết dịch": [{"link": "https://xyz.com", "text": "Dịch"}],
            },
        }
        rec = BaseRecord.from_lark_response(item)
        assert rec.lien_ket_clone.link == "https://abc.com"
        assert rec.lien_ket_clone.text == "Clone"
        assert rec.lien_ket_dich.link == "https://xyz.com"

    def test_status_enum(self) -> None:
        item = {
            "record_id": "rec123",
            "fields": {"Trạng thái": "Done", "Trạng thái dịch": "Failed"},
        }
        rec = BaseRecord.from_lark_response(item)
        assert rec.trang_thai == RecordStatus.DONE
        assert rec.trang_thai_dich == TranslateStatus.FAILED

    def test_text_field_polymorphic(self) -> None:
        """Lark trả text fields là list[{text:...}] hoặc str."""
        # List form
        item1 = {
            "record_id": "r1",
            "fields": {"Tiêu đề": [{"text": "Tiêu đề list"}]},
        }
        assert BaseRecord.from_lark_response(item1).tieude == "Tiêu đề list"

        # String form
        item2 = {"record_id": "r2", "fields": {"Tiêu đề": "Tiêu đề str"}}
        assert BaseRecord.from_lark_response(item2).tieude == "Tiêu đề str"

        # None form
        item3 = {"record_id": "r3", "fields": {"Tiêu đề": None}}
        assert BaseRecord.from_lark_response(item3).tieude == ""

    def test_invalid_int_handled(self) -> None:
        """Bad STT value should not raise."""
        item = {"record_id": "r1", "fields": {"STT": "not-a-number"}}
        rec = BaseRecord.from_lark_response(item)
        assert rec.stt is None

    @pytest.mark.parametrize(
        ("trang_thai", "trang_thai_dich", "expected"),
        [
            ("Done", "Done", True),
            ("Done", "Pending", False),
            ("Pending", "Done", False),
            ("Skipped", "Skipped", False),
        ],
    )
    def test_is_done_property(
        self, trang_thai: str, trang_thai_dich: str, *, expected: bool
    ) -> None:
        item = {
            "record_id": "r1",
            "fields": {
                "Trạng thái": trang_thai,
                "Trạng thái dịch": trang_thai_dich,
            },
        }
        rec = BaseRecord.from_lark_response(item)
        assert rec.is_done is expected

    def test_needs_clone_when_link_empty(self) -> None:
        fields: dict[str, object] = {}
        item = {"record_id": "r1", "fields": fields}
        rec = BaseRecord.from_lark_response(item)
        assert rec.needs_clone is True

        fields["Liên kết clone"] = [{"link": "https://a.com"}]
        rec2 = BaseRecord.from_lark_response(item)
        assert rec2.needs_clone is False

    def test_needs_translate(self) -> None:
        fields: dict[str, object] = {
            "Liên kết clone": [{"link": "https://a.com"}],
            "Trạng thái dịch": "Pending",
        }
        item = {"record_id": "r1", "fields": fields}
        rec = BaseRecord.from_lark_response(item)
        assert rec.needs_translate is True

        fields["Trạng thái dịch"] = "Done"
        rec2 = BaseRecord.from_lark_response(item)
        assert rec2.needs_translate is False
