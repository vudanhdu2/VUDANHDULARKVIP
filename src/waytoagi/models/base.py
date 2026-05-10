"""Lark Base record schemas — strict typing cho 73 fields trong table."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RecordStatus(StrEnum):
    """Trạng thái clone của record."""

    PENDING = "Pending"
    DONE = "Done"
    FAILED = "Failed"
    SKIPPED = "Skipped"


class TranslateStatus(StrEnum):
    """Trạng thái dịch của record."""

    PENDING = "Pending"
    DONE = "Done"
    FAILED = "Failed"
    SKIPPED = "Skipped"
    TRANSLATING = "Translating"


class ChangeStatus(StrEnum):
    """Detect-change marker do crawl đặt."""

    EMPTY = ""
    EDITED = "edited"
    RENAMED = "renamed"
    DELETED = "deleted"


class SourceStatus(StrEnum):
    """Source CN node có còn tồn tại không."""

    PRESENT = "Present"
    DELETED = "Deleted"


class LinkField(BaseModel):
    """Lark Base hyperlink field — {link, text}."""

    link: str = ""
    text: str = ""

    @classmethod
    def from_lark(cls, raw: Any) -> LinkField:
        """Parse từ Lark API response (có thể là str, dict, list[dict])."""
        if not raw:
            return cls()
        if isinstance(raw, list) and raw:
            raw = raw[0]
        if isinstance(raw, dict):
            return cls(link=raw.get("link", "") or "", text=raw.get("text", "") or "")
        if isinstance(raw, str):
            return cls(link=raw, text=raw)
        return cls()


class BaseRecord(BaseModel):
    """Lark Base record — pipeline-relevant subset of 73 fields.

    Chỉ field cần cho pipeline. Các field còn lại (audit timestamps, dashboards)
    pass-through qua extra dict.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    record_id: str = Field(..., description="Lark Base record_id")

    # Identity
    stt: int | None = Field(default=None, alias="STT")
    title: str = Field(default="", alias="Title", description="CN title (original)")
    tieude: str = Field(default="", alias="Tiêu đề", description="VI title (translated)")
    node_token: str = Field(default="", alias="Node Token")
    parent_node_token: str = Field(default="", alias="Parent Node Token")
    obj_token: str = Field(default="", alias="Obj Token")
    obj_type: str = Field(default="", alias="Obj Type")
    node_type: str = Field(default="", alias="Node Type")

    # Status
    trang_thai: RecordStatus = Field(default=RecordStatus.PENDING, alias="Trạng thái")
    trang_thai_dich: TranslateStatus = Field(
        default=TranslateStatus.PENDING, alias="Trạng thái dịch"
    )
    source_status: SourceStatus = Field(default=SourceStatus.PRESENT, alias="Source Status")
    change_status: ChangeStatus = Field(default=ChangeStatus.EMPTY, alias="Change Status")

    # Links
    lien_ket_goc: LinkField = Field(default_factory=LinkField, alias="Liên kết gốc")
    lien_ket_clone: LinkField = Field(default_factory=LinkField, alias="Liên kết clone")
    lien_ket_dich: LinkField = Field(default_factory=LinkField, alias="Liên kết dịch")
    lien_ket_wiki_dich_moi: LinkField = Field(
        default_factory=LinkField, alias="Liên kết wiki dịch mới"
    )

    # Mirror
    mirror_wiki_node_token: str = Field(default="", alias="Mirror Wiki Node Token")
    mirror_wiki_status: str = Field(default="", alias="Mirror Wiki Status")
    mirror_last_synced_at: int | None = Field(default=None, alias="Mirror Last Synced At")

    # Translate quality
    pct_dich: float | None = Field(default=None, alias="% Dịch", ge=0, le=100)
    so_segment_dich: int | None = Field(default=None, alias="Số segment dịch", ge=0)

    # Errors
    loi: str = Field(default="", alias="Lỗi")
    loi_dich: str = Field(default="", alias="Lỗi dịch")
    so_lan_thu: int = Field(default=0, alias="Số lần thử", ge=0)

    # Backlink tracking
    backlink_fix_status: str = Field(default="", alias="Backlink Fix Status")
    backlink_fix_at: int | None = Field(default=None, alias="Backlink Fix At")
    backlink_links_total: int = Field(default=0, alias="Backlink Links Total", ge=0)
    backlink_links_replaced: int = Field(default=0, alias="Backlink Links Replaced", ge=0)

    # Tree-order tracking (Stage 7 — TreeOrderStage)
    tree_order_status: str = Field(
        default="", alias="Tree Order Status",
        description="OK / Mismatch / Fixed / Skipped / Error",
    )
    tree_order_last_audit: int | None = Field(
        default=None, alias="Tree Order Last Audit",
    )
    tree_order_mismatches: int = Field(
        default=0, alias="Tree Order Mismatches", ge=0,
    )

    # Timestamps
    crawled_at: int | None = Field(default=None, alias="Crawled At")
    last_seen_at: int | None = Field(default=None, alias="Last Seen At")
    last_edit_time: int | None = Field(default=None, alias="Last Edit Time")
    thoi_gian: int | None = Field(default=None, alias="Thời gian")
    thoi_gian_dich: int | None = Field(default=None, alias="Thời gian dịch")

    # ============================================================
    # Validators — handle Lark API quirks (text/dict/list polymorphism)
    # ============================================================

    @field_validator(
        "lien_ket_goc",
        "lien_ket_clone",
        "lien_ket_dich",
        "lien_ket_wiki_dich_moi",
        mode="before",
    )
    @classmethod
    def _parse_link(cls, v: Any) -> LinkField:
        if isinstance(v, LinkField):
            return v
        return LinkField.from_lark(v)

    @field_validator("title", "tieude", "loi", "loi_dich", mode="before")
    @classmethod
    def _parse_str(cls, v: Any) -> str:
        """Lark trả text fields là list[{text:...}] hoặc str."""
        if v is None or v == "":
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, list) and v:
            first = v[0]
            if isinstance(first, dict):
                return str(first.get("text", "") or "")
            return str(first)
        if isinstance(v, dict):
            return str(v.get("text", "") or "")
        return str(v)

    @field_validator("stt", "so_segment_dich", "so_lan_thu", "backlink_links_total",
                     "backlink_links_replaced", mode="before")
    @classmethod
    def _parse_int(cls, v: Any) -> int | None:
        if v is None or v == "":
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    @field_validator("pct_dich", mode="before")
    @classmethod
    def _parse_float(cls, v: Any) -> float | None:
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # ============================================================
    # Computed properties
    # ============================================================

    @property
    def is_done(self) -> bool:
        return (
            self.trang_thai == RecordStatus.DONE
            and self.trang_thai_dich == TranslateStatus.DONE
        )

    @property
    def is_pending(self) -> bool:
        return (
            self.trang_thai == RecordStatus.PENDING
            or self.trang_thai_dich == TranslateStatus.PENDING
        )

    @property
    def needs_clone(self) -> bool:
        """Cần Stage 1 (clone) — chưa có Liên kết clone."""
        return not self.lien_ket_clone.link

    @property
    def needs_translate(self) -> bool:
        """Cần Stage 2 (translate) — đã clone nhưng chưa dịch xong."""
        return (
            bool(self.lien_ket_clone.link)
            and self.trang_thai_dich != TranslateStatus.DONE
            and self.trang_thai_dich != TranslateStatus.SKIPPED
        )

    @property
    def needs_mirror(self) -> bool:
        """Cần Stage 3 (mirror) — đã dịch xong nhưng chưa mirror."""
        return (
            self.trang_thai_dich == TranslateStatus.DONE
            and bool(self.lien_ket_dich.link)
            and not self.mirror_wiki_node_token
        )

    @classmethod
    def from_lark_response(cls, item: dict[str, Any]) -> BaseRecord:
        """Parse 1 record từ Lark API response.

        Args:
            item: dict {record_id, fields: {...}}

        Returns:
            Validated BaseRecord
        """
        fields = item.get("fields", {}) or {}
        # Tạo dict với record_id ở top level + fields ở same level
        return cls.model_validate({"record_id": item.get("record_id", ""), **fields})
