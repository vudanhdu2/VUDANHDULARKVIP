"""Tree order models — capture DFS order tại CRAWL, plan reorder tại STAGE.

Strict separation of concerns:
  - `SourceOrderIndex`: snapshot DFS-order của source CN wiki (1 dict
    parent_token → list[child_token]). Dữ liệu thuần — sinh khi crawl,
    persist ra JSON.
  - `TreeOrderPlan`: kết quả pure-function diff giữa current dst order và
    desired dst order. Không I/O.
  - `TreeOrderResult`: tổng hợp kết quả 1 lần audit/fix cho 1 parent.

Idempotent: re-run 0 moves nếu đã match.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TreeOrderStatus(StrEnum):
    """Trạng thái tree-order audit/fix per parent."""

    OK = "OK"               # current == desired, không cần move
    MISMATCH = "Mismatch"   # phát hiện sai, chưa fix (audit-only mode)
    FIXED = "Fixed"         # đã apply move thành công
    SKIPPED = "Skipped"     # bỏ qua (above threshold, no dst children, etc.)
    ERROR = "Error"         # lỗi giữa chừng (rate-limit cạn retry, perm denied)


class SourceOrderIndex(BaseModel):
    """DFS-order snapshot của source CN wiki tree.

    Format:
        order[parent_token] = [child_token_0, child_token_1, ...]

    `parent_token == ""` đại diện cho top-level (root của space).

    Built bởi crawl stage: mỗi lần `list_children(parent)` trả ra items
    theo display order của Lark → đó là source-of-truth ordering.
    """

    model_config = ConfigDict(frozen=True)

    order: dict[str, list[str]] = Field(
        default_factory=dict,
        description="parent_token → ordered list of child tokens",
    )
    captured_at: datetime | None = Field(
        default=None,
        description="Khi nào snapshot này được build",
    )

    @field_validator("order", mode="before")
    @classmethod
    def _normalize_order(cls, v: Any) -> dict[str, list[str]]:
        """Accept legacy format `{parent: {child: idx}}` from V1 state file.

        V1 mirror script stored `source_order` as nested dict — we coerce to
        ordered list so V2 algorithms can rely on a single shape.
        """
        if not isinstance(v, dict):
            return {}
        out: dict[str, list[str]] = {}
        for parent, children in v.items():
            if not isinstance(parent, str):
                continue
            if isinstance(children, list):
                out[parent] = [c for c in children if isinstance(c, str)]
            elif isinstance(children, dict):
                # legacy: {child_token: index}
                pairs = [(c, i) for c, i in children.items()
                         if isinstance(c, str) and isinstance(i, (int, float))]
                pairs.sort(key=lambda p: p[1])
                out[parent] = [p[0] for p in pairs]
        return out

    def parents(self) -> list[str]:
        """Trả về tất cả parent_token trong index (không sort)."""
        return list(self.order.keys())

    def children_of(self, parent_token: str) -> list[str]:
        """Children theo desired order. Empty list nếu parent không có index."""
        return list(self.order.get(parent_token, []))

    def __len__(self) -> int:
        """Số parent."""
        return len(self.order)


class TreeOrderPlan(BaseModel):
    """Kế hoạch fix order cho 1 parent — pure function output.

    Built bởi `compute_plan(desired, current)`. Không I/O.
    """

    model_config = ConfigDict(frozen=True)

    src_parent: str
    dst_parent: str
    desired_count: int = Field(ge=0)
    current_count: int = Field(ge=0)
    no_op: bool = Field(
        description="True nếu current đã match desired prefix → 0 moves",
    )
    skip_reason: str = Field(
        default="",
        description="Non-empty → skip (above_threshold, no_overlap, ...)",
    )
    moves: list[str] = Field(
        default_factory=list,
        description="Dst child tokens cần gọi move(child, dst_parent) "
                    "theo thứ tự — mỗi call put vào END → cuối loop thứ tự đúng",
    )
    extra_dst: list[str] = Field(
        default_factory=list,
        description="Dst children có trong current nhưng không có trong "
                    "desired — sẽ trôi xuống cuối sau reorder, log để audit",
    )
    missing_in_dst: list[str] = Field(
        default_factory=list,
        description="Desired children chưa có dst_token — chưa mirror, "
                    "skip lần này, lần sau retry",
    )

    @property
    def will_move(self) -> int:
        """Số move sẽ gọi (= len(moves) sau optimize prefix)."""
        return len(self.moves)


class TreeOrderResult(BaseModel):
    """Kết quả của 1 audit-fix pass cho 1 parent."""

    src_parent: str
    dst_parent: str
    status: TreeOrderStatus
    plan: TreeOrderPlan | None = None
    moves_attempted: int = Field(default=0, ge=0)
    moves_succeeded: int = Field(default=0, ge=0)
    errors: list[str] = Field(default_factory=list)
    duration_seconds: float = Field(default=0.0, ge=0)


class TreeOrderRunSummary(BaseModel):
    """Tổng hợp 1 lần TreeOrderStage.run()."""

    parents_total: int = 0
    parents_ok: int = 0
    parents_mismatch: int = 0
    parents_fixed: int = 0
    parents_skipped: int = 0
    parents_error: int = 0
    total_moves: int = 0
    duration_seconds: float = Field(default=0.0, ge=0)

    def record(self, result: TreeOrderResult) -> None:
        """Update counters từ 1 result."""
        self.parents_total += 1
        if result.status == TreeOrderStatus.OK:
            self.parents_ok += 1
        elif result.status == TreeOrderStatus.MISMATCH:
            self.parents_mismatch += 1
        elif result.status == TreeOrderStatus.FIXED:
            self.parents_fixed += 1
        elif result.status == TreeOrderStatus.SKIPPED:
            self.parents_skipped += 1
        elif result.status == TreeOrderStatus.ERROR:
            self.parents_error += 1
        self.total_moves += result.moves_succeeded
