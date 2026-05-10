"""Tests cho `SchemaMigration` — diff + apply schema."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from waytoagi.base_schema.fields import (
    SCHEMA_FIELDS,
    FieldDef,
    FieldGroup,
    FieldType,
)
from waytoagi.base_schema.migration import (
    SchemaMigration,
    diff_against_schema,
)
from waytoagi.lark.auth import LarkAPIError


@pytest.mark.unit
class TestDiffAgainstSchema:
    def test_empty_actual_all_missing(self) -> None:
        diff = diff_against_schema({}, SCHEMA_FIELDS)
        assert len(diff.missing) == len(SCHEMA_FIELDS)
        assert diff.extra == []
        assert diff.type_mismatch == []
        assert diff.has_changes is True

    def test_full_match_no_changes(self) -> None:
        # Build actual = mọi field từ schema với type đúng
        actual = {
            f.name: {"type": f.field_type.value, "field_id": f"fld-{i}"}
            for i, f in enumerate(SCHEMA_FIELDS)
        }
        diff = diff_against_schema(actual, SCHEMA_FIELDS)
        assert diff.missing == []
        assert diff.has_changes is False

    def test_extra_fields_listed(self) -> None:
        actual: dict[str, dict[str, Any]] = {
            f.name: {"type": f.field_type.value, "field_id": "x"}
            for f in SCHEMA_FIELDS
        }
        actual["Custom User Field"] = {"type": 1, "field_id": "x"}
        diff = diff_against_schema(actual, SCHEMA_FIELDS)
        assert "Custom User Field" in diff.extra
        assert diff.has_changes is False  # extra không phải change

    def test_type_mismatch_detected(self) -> None:
        # Tất cả fields trong schema, nhưng "STT" type sai (Text thay Number)
        actual: dict[str, dict[str, Any]] = {}
        for f in SCHEMA_FIELDS:
            actual[f.name] = {
                "type": f.field_type.value, "field_id": "x",
            }
        # Force STT type sai
        actual["STT"] = {"type": 1, "field_id": "x"}  # Text instead of Number
        diff = diff_against_schema(actual, SCHEMA_FIELDS)
        assert any(
            name == "STT" for name, _, _ in diff.type_mismatch
        )

    def test_missing_subset(self) -> None:
        # Chỉ có half schema → other half = missing
        half = SCHEMA_FIELDS[: len(SCHEMA_FIELDS) // 2]
        actual = {
            f.name: {"type": f.field_type.value, "field_id": "x"}
            for f in half
        }
        diff = diff_against_schema(actual, SCHEMA_FIELDS)
        assert len(diff.missing) == len(SCHEMA_FIELDS) - len(half)


@pytest.mark.unit
class TestSchemaMigrationProbe:
    @pytest.mark.asyncio
    async def test_probe_parses_response(self) -> None:
        base = AsyncMock()
        base.list_fields = AsyncMock(return_value={
            "code": 0,
            "data": {
                "items": [
                    {"field_name": "STT", "type": 2, "field_id": "fld-1"},
                    {"field_name": "Tiêu đề", "type": 1, "field_id": "fld-2"},
                ],
            },
        })
        mig = SchemaMigration(base=base, app_token="app", table_id="tbl")
        actual = await mig.probe()
        assert "STT" in actual
        assert actual["STT"]["type"] == 2
        assert actual["STT"]["field_id"] == "fld-1"


@pytest.mark.unit
class TestSchemaMigrationApply:
    @pytest.mark.asyncio
    async def test_apply_creates_missing_fields(self) -> None:
        base = AsyncMock()
        # Empty table — every field missing
        base.list_fields = AsyncMock(return_value={
            "code": 0, "data": {"items": []},
        })
        base.create_field = AsyncMock(return_value={"code": 0})
        mig = SchemaMigration(
            base=base, app_token="app", table_id="tbl", per_field_sleep=0,
        )
        result = await mig.apply()
        # Tất cả fields từ schema được create
        assert result.n_created == len(SCHEMA_FIELDS)
        assert result.n_failed == 0

    @pytest.mark.asyncio
    async def test_apply_idempotent_full_match(self) -> None:
        """Schema khớp 100% với actual → 0 created."""
        base = AsyncMock()
        base.list_fields = AsyncMock(return_value={
            "code": 0,
            "data": {
                "items": [
                    {"field_name": f.name, "type": f.field_type.value,
                     "field_id": "x"}
                    for f in SCHEMA_FIELDS
                ],
            },
        })
        base.create_field = AsyncMock()
        mig = SchemaMigration(
            base=base, app_token="app", table_id="tbl", per_field_sleep=0,
        )
        result = await mig.apply()
        assert result.n_created == 0
        base.create_field.assert_not_called()

    @pytest.mark.asyncio
    async def test_apply_dry_run_no_api_calls(self) -> None:
        base = AsyncMock()
        base.list_fields = AsyncMock(return_value={
            "code": 0, "data": {"items": []},
        })
        base.create_field = AsyncMock()
        mig = SchemaMigration(
            base=base, app_token="app", table_id="tbl", per_field_sleep=0,
        )
        result = await mig.apply(dry_run=True)
        assert result.n_created == len(SCHEMA_FIELDS)
        # KHÔNG gọi create_field thực
        base.create_field.assert_not_called()

    @pytest.mark.asyncio
    async def test_apply_partial_failure(self) -> None:
        """1 field create fail → record vào failed, các field khác vẫn process."""
        base = AsyncMock()
        base.list_fields = AsyncMock(return_value={
            "code": 0, "data": {"items": []},
        })
        # 3rd call fails
        call_count = {"i": 0}

        async def create_side(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            call_count["i"] += 1
            if call_count["i"] == 3:
                raise LarkAPIError(99991400, "rate limit", "/create")
            return {"code": 0}

        base.create_field = AsyncMock(side_effect=create_side)
        mig = SchemaMigration(
            base=base, app_token="app", table_id="tbl", per_field_sleep=0,
        )
        result = await mig.apply()
        assert result.n_failed == 1
        # 1 fail, các fields khác vẫn process
        assert result.n_created == len(SCHEMA_FIELDS) - 1


@pytest.mark.unit
class TestSchemaMigrationDiffSummary:
    def test_summary_format(self) -> None:
        from waytoagi.base_schema.migration import MigrationDiff

        diff = MigrationDiff(
            missing=[FieldDef(
                "x", FieldType.TEXT, FieldGroup.IDENTITY,
            )],
            extra=["legacy"],
            type_mismatch=[("y", 1, 2)],
        )
        s = diff.summary()
        assert "missing=1" in s
        assert "extra=1" in s
        assert "type_mismatch=1" in s

    def test_has_changes_when_missing(self) -> None:
        from waytoagi.base_schema.migration import MigrationDiff

        diff = MigrationDiff(missing=[
            FieldDef("x", FieldType.TEXT, FieldGroup.IDENTITY),
        ])
        assert diff.has_changes is True

    def test_has_changes_when_type_mismatch(self) -> None:
        from waytoagi.base_schema.migration import MigrationDiff

        diff = MigrationDiff(type_mismatch=[("x", 1, 2)])
        assert diff.has_changes is True

    def test_no_changes_when_only_extra(self) -> None:
        from waytoagi.base_schema.migration import MigrationDiff

        diff = MigrationDiff(extra=["legacy"])
        assert diff.has_changes is False
