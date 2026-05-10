"""Pure-function diff: desired vs current → TreeOrderPlan.

Lark Wiki API quirk: `wiki.nodes.move(child, dst_parent)` đặt child vào
**cuối** danh sách children của dst_parent. Strategy:
  1. Tính `desired_dst_seq` = desired src order, dịch qua `src_to_dst` map
     (drop src chưa có dst — tức là chưa mirror).
  2. Filter `desired_dst_seq` xuống chỉ những token thực sự có trong
     `current_dst_seq` (tránh "move" 1 node không tồn tại).
  3. Tìm **longest common prefix** của `desired_filtered` so với
     `current_filtered` (chỉ xét các token có cả 2 bên) → prefix đã
     đúng vị trí, không cần đụng.
  4. Suffix sau prefix → cần move theo thứ tự để xếp vào cuối.
  5. Nếu suffix rỗng → no_op.

Idempotent: re-run lần 2 sẽ thấy current đã match → no_op.
"""

from __future__ import annotations

from waytoagi.models.tree import TreeOrderPlan

DEFAULT_MAX_CHILDREN = 50
"""Threshold mặc định: parent có > N children → skip để tránh chạm subtree
quá lớn (1 lần chạy sai có thể disrupt hàng trăm wiki nodes)."""


def _longest_common_prefix(a: list[str], b: list[str]) -> int:
    """Trả về độ dài longest common prefix của 2 list."""
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


def compute_plan(
    *,
    src_parent: str,
    dst_parent: str,
    desired_src_children: list[str],
    current_dst_children: list[str],
    src_to_dst: dict[str, str],
    max_children: int = DEFAULT_MAX_CHILDREN,
) -> TreeOrderPlan:
    """Tính plan reorder cho 1 parent.

    Args:
        src_parent: source CN parent token (cho audit log).
        dst_parent: dst parent token (cần có để gọi move).
        desired_src_children: ordered list source children (DFS order).
        current_dst_children: ordered list dst children theo display
            hiện tại của Lark (lấy qua list_nodes API).
        src_to_dst: src_token → dst_token mapping (chỉ records đã mirror).
        max_children: parent với > N children sẽ skip.

    Returns:
        TreeOrderPlan — frozen, audit-friendly.
    """
    # Step 1: dịch desired src → desired dst (drop src chưa có dst).
    desired_dst_full: list[str] = []
    missing: list[str] = []
    for src_child in desired_src_children:
        dst_child = src_to_dst.get(src_child)
        if dst_child:
            desired_dst_full.append(dst_child)
        else:
            missing.append(src_child)

    # Step 2: filter desired xuống chỉ những token có trong current
    # (tránh move node không tồn tại — sẽ 131005 not found).
    current_set = set(current_dst_children)
    desired_filtered = [d for d in desired_dst_full if d in current_set]

    # Extra dst children = có trong current nhưng không có trong desired.
    # (vd dst tạo manually, hoặc record có dst_token nhưng src đã deleted.)
    desired_set = set(desired_filtered)
    extra_dst = [d for d in current_dst_children if d not in desired_set]

    # Step 3: threshold guard — skip nếu quá lớn
    if max_children > 0 and len(desired_filtered) > max_children:
        return TreeOrderPlan(
            src_parent=src_parent,
            dst_parent=dst_parent,
            desired_count=len(desired_filtered),
            current_count=len(current_dst_children),
            no_op=False,
            skip_reason=f"above_threshold (>{max_children})",
            moves=[],
            extra_dst=extra_dst,
            missing_in_dst=missing,
        )

    # Step 4: nếu không có overlap thực sự, no-op (tránh corrupt extras)
    if not desired_filtered:
        return TreeOrderPlan(
            src_parent=src_parent,
            dst_parent=dst_parent,
            desired_count=0,
            current_count=len(current_dst_children),
            no_op=True,
            skip_reason="no_overlap",
            moves=[],
            extra_dst=extra_dst,
            missing_in_dst=missing,
        )

    # Step 5: filter current cũng xuống chỉ token có trong desired (loại extra
    # ra để so prefix). Extra trôi xuống cuối sau khi reorder — chấp nhận.
    current_filtered = [c for c in current_dst_children if c in desired_set]

    # Step 6: longest common prefix → suffix cần move
    prefix_len = _longest_common_prefix(desired_filtered, current_filtered)
    moves = desired_filtered[prefix_len:]

    no_op = len(moves) == 0

    return TreeOrderPlan(
        src_parent=src_parent,
        dst_parent=dst_parent,
        desired_count=len(desired_filtered),
        current_count=len(current_dst_children),
        no_op=no_op,
        skip_reason="",
        moves=moves,
        extra_dst=extra_dst,
        missing_in_dst=missing,
    )
