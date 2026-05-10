"""Crawl-stage models — events, plan items, results.

Triết lý:
  - CrawlStage là **eager**: ngay khi phát hiện source node mới, tạo
    luôn placeholder (empty doc + wiki node) trên DST space để có
    `Mirror Wiki Node Token` SẴN SÀNG cho stages sau.
  - Lý do: khi clone+translate doc A có link tới doc B, src→dst map
    đã có B → có thể swap URL CN→DST INLINE ngay khi clone, không
    cần MIRROR stage fix lại.

Models tách 3 lớp:
  1. `CrawlEvent`: event detected per source node.
  2. `CrawlPlanItem`: 1 entry trong plan, chứa event + metadata.
  3. `CrawlResult`: counters tổng hợp 1 lần CrawlStage.run().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class CrawlEvent(StrEnum):
    """Loại event detected khi crawl 1 source node."""

    NEW = "new"
    """Lần đầu thấy node này — chưa có trong Base."""

    EDITED = "edited"
    """Source `obj_edit_time` đổi vs Base.Last Edit Time."""

    RENAMED = "renamed"
    """Title source đổi, content có thể chưa đổi."""

    UNCHANGED = "unchanged"
    """Không có gì đổi — chỉ touch `Last Seen At`."""

    DELETED = "deleted"
    """Có trong Base nhưng không thấy trong source DFS lần này."""


class PlaceholderStatus(StrEnum):
    """Trạng thái placeholder trên DST tenant."""

    NONE = ""
    """Chưa attempt tạo."""

    CREATED = "Placeholder"
    """Tạo thành công, chờ content fill bởi pipeline."""

    FAILED = "PlaceholderFailed"
    """Tạo fail (rate-limit, perm) — sẽ retry lần crawl sau."""

    FILLED = "Done"
    """Đã được CLONE/TRANSLATE/SYNC fill content (set bởi stage sau)."""


@dataclass(frozen=True, slots=True)
class CrawlPlanItem:
    """1 entry trong plan crawl — không có I/O state.

    Dùng cho:
      - Phase 1 (detect): build plan từ source walk + Base records.
      - Phase 2 (apply): execute placeholder creation theo plan.
      - Test: trả về deterministic, dễ assert.
    """

    src_node_token: str
    src_parent_token: str
    src_obj_token: str
    src_obj_type: str
    src_node_type: str
    title: str
    obj_edit_time_ms: int
    event: CrawlEvent
    record_id: str = ""
    """Lark Base record_id nếu đã tồn tại — empty cho NEW."""
    existing_dst_token: str = ""
    """Mirror Wiki Node Token đã có (nếu có) → skip placeholder."""


class CrawlResult(BaseModel):
    """Counters tổng hợp 1 lần CrawlStage.run().

    Field chính cho real-time monitoring:
      - `new_count`: bao nhiêu node mới detect → cần tạo placeholder.
      - `placeholders_created`: thực tạo thành công bao nhiêu.
      - `placeholders_failed`: tạo fail (sẽ retry lần sau).
      - `edited_count`: source edit detected → reset clone/translate state.
      - `deleted_count`: source biến mất → mark Source Status=Deleted.
      - `unchanged_count`: chỉ touch Last Seen At.
    """

    model_config = ConfigDict(frozen=False)

    nodes_walked: int = Field(default=0, ge=0)
    new_count: int = Field(default=0, ge=0)
    edited_count: int = Field(default=0, ge=0)
    renamed_count: int = Field(default=0, ge=0)
    unchanged_count: int = Field(default=0, ge=0)
    deleted_count: int = Field(default=0, ge=0)

    # Placeholder execution
    placeholders_created: int = Field(default=0, ge=0)
    placeholders_skipped_existing: int = Field(default=0, ge=0)
    placeholders_failed: int = Field(default=0, ge=0)

    # Base writes
    base_creates: int = Field(default=0, ge=0)
    base_updates: int = Field(default=0, ge=0)
    base_failures: int = Field(default=0, ge=0)

    # Errors
    errors: list[str] = Field(default_factory=list)

    duration_seconds: float = Field(default=0.0, ge=0)

    def record_event(self, event: CrawlEvent) -> None:
        """Increment counter cho event."""
        if event == CrawlEvent.NEW:
            self.new_count += 1
        elif event == CrawlEvent.EDITED:
            self.edited_count += 1
        elif event == CrawlEvent.RENAMED:
            self.renamed_count += 1
        elif event == CrawlEvent.UNCHANGED:
            self.unchanged_count += 1
        elif event == CrawlEvent.DELETED:
            self.deleted_count += 1


@dataclass(slots=True)
class PlaceholderCreateResult:
    """Kết quả tạo 1 placeholder trên DST."""

    src_node_token: str
    success: bool
    dst_node_token: str = ""
    dst_url: str = ""
    error: str = ""
    skipped_existing: bool = False
    """True nếu record đã có dst_token, không attempt create lại."""
    elapsed_seconds: float = 0.0


@dataclass(slots=True)
class CrawlPlan:
    """Plan tổng hợp sau Phase 1 (detect).

    Dùng để Phase 2 (apply) có deterministic input. Tách phase giúp
    testing dễ hơn (mock detect, test apply riêng).
    """

    items: list[CrawlPlanItem] = field(default_factory=list)
    deleted_record_ids: list[str] = field(default_factory=list)
    """Records trong Base nhưng không thấy trong walk → cần mark Deleted."""

    @property
    def new_items(self) -> list[CrawlPlanItem]:
        return [i for i in self.items if i.event == CrawlEvent.NEW]

    @property
    def needs_placeholder(self) -> list[CrawlPlanItem]:
        """Items cần tạo placeholder (NEW + chưa có dst_token)."""
        return [
            i for i in self.items
            if i.event == CrawlEvent.NEW and not i.existing_dst_token
        ]

    @property
    def edited_items(self) -> list[CrawlPlanItem]:
        return [i for i in self.items if i.event == CrawlEvent.EDITED]

    @property
    def renamed_items(self) -> list[CrawlPlanItem]:
        return [i for i in self.items if i.event == CrawlEvent.RENAMED]
