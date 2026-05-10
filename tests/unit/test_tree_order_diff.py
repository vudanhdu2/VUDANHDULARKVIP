"""Unit tests cho `waytoagi.reorder.diff.compute_plan` — pure function.

Coverage:
  - E1: empty desired → no_op (no_overlap)
  - E2: all desired tokens missing in current → no_op
  - E3: already correct order → no_op (0 moves)
  - E4: completely reverse order → N moves
  - E5: prefix match → only suffix moves (optimization)
  - E6: extra dst children → preserved as extra_dst, not in moves
  - E7: above max_children → skipped
  - E8: missing in dst (chưa mirror) → captured in missing_in_dst, drop
  - E9: idempotent — apply plan twice → 2nd is no_op
"""

from __future__ import annotations

import pytest

from waytoagi.models.tree import SourceOrderIndex
from waytoagi.reorder.diff import DEFAULT_MAX_CHILDREN, compute_plan


@pytest.mark.unit
class TestComputePlan:
    """compute_plan — diff algorithm tests."""

    def test_empty_desired_no_op(self) -> None:
        plan = compute_plan(
            src_parent="P_SRC",
            dst_parent="P_DST",
            desired_src_children=[],
            current_dst_children=["dst1", "dst2"],
            src_to_dst={},
        )
        assert plan.no_op is True
        assert plan.skip_reason == "no_overlap"
        assert plan.moves == []

    def test_no_overlap_skip(self) -> None:
        """Desired tokens không có cái nào trong current → no_op."""
        plan = compute_plan(
            src_parent="P_SRC",
            dst_parent="P_DST",
            desired_src_children=["src1", "src2"],
            current_dst_children=["dst99"],
            src_to_dst={"src1": "dst1", "src2": "dst2"},
        )
        assert plan.no_op is True
        assert plan.skip_reason == "no_overlap"
        assert plan.missing_in_dst == []  # both have dst tokens
        assert plan.extra_dst == ["dst99"]

    def test_already_correct_order(self) -> None:
        """Current đã match desired → no_op, 0 moves."""
        plan = compute_plan(
            src_parent="P_SRC",
            dst_parent="P_DST",
            desired_src_children=["src1", "src2", "src3"],
            current_dst_children=["dst1", "dst2", "dst3"],
            src_to_dst={"src1": "dst1", "src2": "dst2", "src3": "dst3"},
        )
        assert plan.no_op is True
        assert plan.moves == []
        assert plan.desired_count == 3
        assert plan.current_count == 3

    def test_complete_reverse_order(self) -> None:
        """Reverse order → ALL N moves needed (worst case)."""
        plan = compute_plan(
            src_parent="P_SRC",
            dst_parent="P_DST",
            desired_src_children=["src1", "src2", "src3"],
            current_dst_children=["dst3", "dst2", "dst1"],
            src_to_dst={"src1": "dst1", "src2": "dst2", "src3": "dst3"},
        )
        assert plan.no_op is False
        assert plan.moves == ["dst1", "dst2", "dst3"]
        assert plan.will_move == 3

    def test_prefix_match_only_suffix_moves(self) -> None:
        """First 2 đúng vị trí → chỉ move 2 cuối."""
        plan = compute_plan(
            src_parent="P_SRC",
            dst_parent="P_DST",
            desired_src_children=["src1", "src2", "src3", "src4"],
            current_dst_children=["dst1", "dst2", "dst4", "dst3"],
            src_to_dst={
                "src1": "dst1", "src2": "dst2",
                "src3": "dst3", "src4": "dst4",
            },
        )
        assert plan.no_op is False
        # prefix [dst1, dst2] match → moves = [dst3, dst4]
        assert plan.moves == ["dst3", "dst4"]

    def test_extra_dst_children_preserved(self) -> None:
        """Dst có node KHÔNG có trong desired → để extra_dst, không touch."""
        plan = compute_plan(
            src_parent="P_SRC",
            dst_parent="P_DST",
            desired_src_children=["src1", "src2"],
            current_dst_children=["dst_extra", "dst1", "dst2"],
            src_to_dst={"src1": "dst1", "src2": "dst2"},
        )
        # dst_extra không thuộc desired → loại khỏi moves
        # current_filtered = [dst1, dst2] (loại extra)
        # desired_filtered = [dst1, dst2]
        # → match → no_op
        assert plan.no_op is True
        assert plan.extra_dst == ["dst_extra"]
        assert plan.moves == []

    def test_extra_dst_with_misorder(self) -> None:
        """Có extra + misorder → chỉ move desired."""
        plan = compute_plan(
            src_parent="P_SRC",
            dst_parent="P_DST",
            desired_src_children=["src1", "src2"],
            current_dst_children=["dst2", "dst_extra", "dst1"],
            src_to_dst={"src1": "dst1", "src2": "dst2"},
        )
        # current_filtered (after removing extra) = [dst2, dst1]
        # desired_filtered = [dst1, dst2]
        # prefix length 0 → moves = [dst1, dst2]
        assert plan.moves == ["dst1", "dst2"]
        assert plan.extra_dst == ["dst_extra"]

    def test_above_threshold_skipped(self) -> None:
        """Parent với nhiều children hơn threshold → skip."""
        desired = [f"src{i}" for i in range(60)]
        current = [f"dst{i}" for i in range(60)]
        mapping = {f"src{i}": f"dst{i}" for i in range(60)}
        # Make order wrong to ensure normally would move
        current_wrong = list(reversed(current))
        plan = compute_plan(
            src_parent="P_SRC",
            dst_parent="P_DST",
            desired_src_children=desired,
            current_dst_children=current_wrong,
            src_to_dst=mapping,
            max_children=50,
        )
        assert plan.skip_reason.startswith("above_threshold")
        assert plan.moves == []

    def test_missing_in_dst_captured(self) -> None:
        """Src chưa mirror → trong missing_in_dst, drop khỏi moves."""
        plan = compute_plan(
            src_parent="P_SRC",
            dst_parent="P_DST",
            desired_src_children=["src1", "src2_unmirrored", "src3"],
            current_dst_children=["dst3", "dst1"],
            src_to_dst={"src1": "dst1", "src3": "dst3"},
        )
        assert "src2_unmirrored" in plan.missing_in_dst
        # desired_filtered = [dst1, dst3] (drop src2_unmirrored)
        # current_filtered = [dst3, dst1]
        # No prefix match → moves = [dst1, dst3]
        assert plan.moves == ["dst1", "dst3"]

    def test_idempotent_after_apply(self) -> None:
        """Sau khi 'apply' (simulate), re-compute → no_op."""
        # Lần 1
        plan1 = compute_plan(
            src_parent="P",
            dst_parent="P_DST",
            desired_src_children=["s1", "s2", "s3"],
            current_dst_children=["d3", "d1", "d2"],
            src_to_dst={"s1": "d1", "s2": "d2", "s3": "d3"},
        )
        assert plan1.moves == ["d1", "d2", "d3"]
        # Sau khi apply: current → desired
        new_current = ["d1", "d2", "d3"]
        plan2 = compute_plan(
            src_parent="P",
            dst_parent="P_DST",
            desired_src_children=["s1", "s2", "s3"],
            current_dst_children=new_current,
            src_to_dst={"s1": "d1", "s2": "d2", "s3": "d3"},
        )
        assert plan2.no_op is True
        assert plan2.moves == []

    def test_max_children_zero_disables_threshold(self) -> None:
        """max_children=0 → không apply threshold guard."""
        desired = [f"src{i}" for i in range(100)]
        current = list(reversed([f"dst{i}" for i in range(100)]))
        mapping = {f"src{i}": f"dst{i}" for i in range(100)}
        plan = compute_plan(
            src_parent="P",
            dst_parent="P_DST",
            desired_src_children=desired,
            current_dst_children=current,
            src_to_dst=mapping,
            max_children=0,
        )
        assert plan.skip_reason == ""
        assert plan.will_move == 100

    def test_default_max_children_is_50(self) -> None:
        assert DEFAULT_MAX_CHILDREN == 50


@pytest.mark.unit
class TestSourceOrderIndex:
    """SourceOrderIndex — model + legacy V1 format coercion."""

    def test_empty_index(self) -> None:
        idx = SourceOrderIndex()
        assert len(idx) == 0
        assert idx.parents() == []
        assert idx.children_of("anything") == []

    def test_parse_list_format(self) -> None:
        idx = SourceOrderIndex(order={
            "P1": ["c1", "c2", "c3"],
            "P2": ["c4"],
        })
        assert len(idx) == 2
        assert idx.children_of("P1") == ["c1", "c2", "c3"]
        assert idx.children_of("P2") == ["c4"]
        assert idx.children_of("P_unknown") == []

    def test_parse_legacy_dict_format(self) -> None:
        """V1 mirror state file: {parent: {child: index}} → coerce → list."""
        idx = SourceOrderIndex(order={  # type: ignore[arg-type]
            "P1": {"c2": 1, "c1": 0, "c3": 2},
        })
        # Sorted by index → c1, c2, c3
        assert idx.children_of("P1") == ["c1", "c2", "c3"]

    def test_frozen_immutability(self) -> None:
        """SourceOrderIndex là frozen — không cho mutate."""
        from pydantic import ValidationError

        idx = SourceOrderIndex(order={"P1": ["c1"]})
        with pytest.raises(ValidationError, match="frozen"):
            idx.order = {"P2": ["c2"]}  # type: ignore[misc]

    def test_filters_invalid_entries(self) -> None:
        """Non-string parents/children được drop."""
        idx = SourceOrderIndex(order={  # type: ignore[arg-type]
            "P1": ["c1", 42, "c2"],  # 42 không phải string → drop
        })
        assert idx.children_of("P1") == ["c1", "c2"]
