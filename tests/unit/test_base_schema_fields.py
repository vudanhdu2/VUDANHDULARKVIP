"""Tests cho schema definition + invariants."""

from __future__ import annotations

import pytest

from waytoagi.base_schema.fields import (
    SCHEMA_FIELDS,
    FieldGroup,
    FieldType,
    field_groups,
    get_field,
)


@pytest.mark.unit
class TestSchemaInvariants:
    def test_unique_field_names(self) -> None:
        names = [f.name for f in SCHEMA_FIELDS]
        assert len(names) == len(set(names)), "Field names phải unique"

    def test_exactly_one_primary_field(self) -> None:
        primary = [f for f in SCHEMA_FIELDS if f.is_primary]
        assert len(primary) == 1
        assert primary[0].name == "Tiêu đề"

    def test_required_fields_have_node_token(self) -> None:
        required = {f.name for f in SCHEMA_FIELDS if f.is_required}
        assert "Node Token" in required

    def test_audit_trail_field_exists(self) -> None:
        f = get_field("Audit Trail")
        assert f is not None
        assert f.field_type == FieldType.TEXT
        assert f.group == FieldGroup.QA

    def test_pipeline_stage_has_select_options(self) -> None:
        f = get_field("Pipeline Stage")
        assert f is not None
        assert f.field_type == FieldType.SINGLE_SELECT
        assert "Pending" in f.select_options
        assert "Done" in f.select_options
        assert "Failed" in f.select_options


@pytest.mark.unit
class TestFieldGroups:
    def test_all_groups_populated(self) -> None:
        groups = field_groups()
        for g in FieldGroup:
            assert len(groups[g]) >= 1, f"Group {g.value} phải có ít nhất 1 field"

    def test_identity_first_in_order(self) -> None:
        # SCHEMA_FIELDS phải bắt đầu với IDENTITY group
        assert SCHEMA_FIELDS[0].group == FieldGroup.IDENTITY
        assert SCHEMA_FIELDS[0].name == "STT"

    def test_meta_last_in_order(self) -> None:
        assert SCHEMA_FIELDS[-1].group == FieldGroup.META

    def test_pipeline_flow_order(self) -> None:
        """Groups xếp theo flow: Identity → Status → Source → stages → QA → Meta."""
        seen_groups: list[FieldGroup] = []
        for f in SCHEMA_FIELDS:
            if not seen_groups or seen_groups[-1] != f.group:
                seen_groups.append(f.group)

        expected_order = [
            FieldGroup.IDENTITY,
            FieldGroup.PIPELINE_STATUS,
            FieldGroup.SOURCE,
            FieldGroup.CRAWL,
            FieldGroup.CLONE,
            FieldGroup.TRANSLATE,
            FieldGroup.MIRROR,
            FieldGroup.SYNC,
            FieldGroup.BACKLINKS,
            FieldGroup.TREE_ORDER,
            FieldGroup.QA,
            FieldGroup.META,
        ]
        assert seen_groups == expected_order


@pytest.mark.unit
class TestStageFieldsCompleteness:
    """Mỗi stage chính phải có 5+ fields tracking."""

    def test_clone_has_full_tracking(self) -> None:
        groups = field_groups()
        clone_names = {f.name for f in groups[FieldGroup.CLONE]}
        # Status, Started At, Completed At, Duration, Block Count, Attempts, Error
        for required in (
            "Clone Status", "Clone Started At", "Clone Completed At",
            "Clone Duration Seconds", "Clone Block Count",
            "Clone Attempts", "Clone Error",
        ):
            assert required in clone_names, f"Missing {required} in CLONE group"

    def test_translate_has_full_tracking(self) -> None:
        groups = field_groups()
        names = {f.name for f in groups[FieldGroup.TRANSLATE]}
        for req in (
            "Translate Status", "Translate Started At",
            "Translate Completed At", "Translate Duration Seconds",
            "Translate Block Count", "% Dịch",
            "Translate LLM Calls", "Translate Cache Hit Pct",
            "Translate Attempts", "Translate Error",
        ):
            assert req in names, f"Missing {req} in TRANSLATE group"

    def test_mirror_has_full_tracking(self) -> None:
        groups = field_groups()
        names = {f.name for f in groups[FieldGroup.MIRROR]}
        for req in (
            "Mirror Wiki Node Token", "Mirror Wiki Status",
            "Mirror Started At", "Mirror Completed At",
            "Mirror Duration Seconds", "Mirror Attempts", "Mirror Error",
        ):
            assert req in names

    def test_sync_has_block_diff_metrics(self) -> None:
        groups = field_groups()
        names = {f.name for f in groups[FieldGroup.SYNC]}
        for req in (
            "Sync Block Replaced", "Sync Block Appended",
            "Sync Block Kept", "Sync Saved Calls",
        ):
            assert req in names

    def test_tree_order_has_status_and_audit(self) -> None:
        groups = field_groups()
        names = {f.name for f in groups[FieldGroup.TREE_ORDER]}
        for req in (
            "Tree Order Status", "Tree Order Last Audit",
            "Tree Order Mismatches",
        ):
            assert req in names


@pytest.mark.unit
class TestGetField:
    def test_lookup_existing(self) -> None:
        f = get_field("Liên kết clone")
        assert f is not None
        assert f.field_type == FieldType.URL

    def test_lookup_missing(self) -> None:
        assert get_field("Không tồn tại") is None
