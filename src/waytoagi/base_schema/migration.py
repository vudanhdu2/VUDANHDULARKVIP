"""Schema migration — probe Lark Base table + tạo missing fields.

Workflow:
  1. `probe()`: list_fields qua Lark API → get tên + type của fields hiện có.
  2. `diff()`: so với SCHEMA_FIELDS → ra `MigrationDiff` (missing, extra, mismatched_type).
  3. `apply()`: tạo missing fields qua `create_field`.

Idempotent: re-run sau khi đã tạo → diff trả 0 missing.

Lark API rate-limit-aware: sleep nhẹ giữa calls (`per_field_sleep`).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from waytoagi.base_schema.fields import SCHEMA_FIELDS, FieldDef, FieldType
from waytoagi.lark.auth import LarkAPIError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from waytoagi.lark.base import LarkBase

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class MigrationDiff:
    """Kết quả diff schema chuẩn vs Lark Base table thực tế."""

    missing: list[FieldDef] = field(default_factory=list)
    """Fields có trong schema nhưng chưa có trong Lark Base."""

    extra: list[str] = field(default_factory=list)
    """Field name có trong Lark Base nhưng không có trong schema (legacy/custom)."""

    type_mismatch: list[tuple[str, int, int]] = field(default_factory=list)
    """(field_name, expected_type, actual_type) — type lệch."""

    @property
    def has_changes(self) -> bool:
        return bool(self.missing) or bool(self.type_mismatch)

    def summary(self) -> str:
        return (
            f"missing={len(self.missing)}, "
            f"extra={len(self.extra)}, "
            f"type_mismatch={len(self.type_mismatch)}"
        )


@dataclass(slots=True)
class MigrationResult:
    """Kết quả apply migration."""

    fields_created: list[str] = field(default_factory=list)
    fields_failed: list[tuple[str, str]] = field(default_factory=list)
    """(field_name, error)."""

    @property
    def n_created(self) -> int:
        return len(self.fields_created)

    @property
    def n_failed(self) -> int:
        return len(self.fields_failed)


class SchemaMigration:
    """Service: probe + diff + apply schema migration trên Lark Base.

    Args:
        base: LarkBase client.
        app_token: Bitable app_token.
        table_id: Bitable table_id.
        per_field_sleep: delay giữa create_field calls (rate-limit aware).
    """

    def __init__(
        self,
        *,
        base: LarkBase,
        app_token: str,
        table_id: str,
        per_field_sleep: float = 0.3,
    ) -> None:
        self._base = base
        self._app_token = app_token
        self._table_id = table_id
        self._sleep = per_field_sleep
        self._log = logger.bind(component="SchemaMigration")

    # ====================================================================
    # Public API
    # ====================================================================

    async def probe(self) -> dict[str, dict[str, Any]]:
        """Trả về dict field_name → {type, property, field_id}."""
        response = await self._base.list_fields(
            self._app_token, self._table_id,
        )
        items = response.get("data", {}).get("items", [])
        out: dict[str, dict[str, Any]] = {}
        for item in items:
            name = item.get("field_name")
            if not isinstance(name, str):
                continue
            out[name] = {
                "type": int(item.get("type", 0)),
                "property": item.get("property") or {},
                "field_id": str(item.get("field_id", "")),
            }
        return out

    async def diff(self) -> MigrationDiff:
        """Compute diff schema vs actual."""
        actual = await self.probe()
        return diff_against_schema(actual, SCHEMA_FIELDS)

    async def apply(
        self,
        *,
        dry_run: bool = False,
    ) -> MigrationResult:
        """Apply diff: tạo missing fields. Skip extra/mismatched."""
        result = MigrationResult()
        d = await self.diff()
        self._log.info(
            "migration_diff", summary=d.summary(), dry_run=dry_run,
        )

        if not d.missing:
            return result

        for fdef in d.missing:
            if dry_run:
                self._log.info(
                    "would_create_field", name=fdef.name,
                    type=fdef.field_type.value,
                )
                result.fields_created.append(fdef.name)
                continue
            try:
                body = _build_create_body(fdef)
                await self._base.create_field(
                    self._app_token, self._table_id,
                    field_name=fdef.name,
                    field_type=fdef.field_type.value,
                    property_=body.get("property"),
                    description=fdef.description,
                )
                result.fields_created.append(fdef.name)
                self._log.info("field_created", name=fdef.name)
            except LarkAPIError as e:
                msg = f"[{e.code}] {e.msg[:80]}"
                result.fields_failed.append((fdef.name, msg))
                self._log.warning("field_create_failed", name=fdef.name, msg=msg)
            if self._sleep > 0:
                await asyncio.sleep(self._sleep)
        return result


# ============================================================================
# Pure helpers (test-friendly)
# ============================================================================


def diff_against_schema(
    actual: Mapping[str, Mapping[str, Any]],
    schema: list[FieldDef],
) -> MigrationDiff:
    """Pure: so actual fields vs schema. Không I/O."""
    schema_by_name = {f.name: f for f in schema}
    diff = MigrationDiff()

    # Missing = schema có, actual không
    for fdef in schema:
        if fdef.name not in actual:
            diff.missing.append(fdef)
            continue
        # Check type mismatch (skip CREATED_TIME/MODIFIED_TIME — Lark
        # luôn auto-create với type khác)
        if fdef.field_type in (
            FieldType.CREATED_TIME, FieldType.MODIFIED_TIME,
        ):
            continue
        actual_type = int(actual[fdef.name].get("type", 0))
        if actual_type and actual_type != fdef.field_type.value:
            diff.type_mismatch.append((
                fdef.name, fdef.field_type.value, actual_type,
            ))

    # Extra = actual có, schema không
    for name in actual:
        if name not in schema_by_name:
            diff.extra.append(name)

    return diff


def _build_create_body(fdef: FieldDef) -> dict[str, Any]:
    """Build property object cho create_field từ FieldDef."""
    body: dict[str, Any] = {}

    # Single/Multi select cần property.options
    if fdef.field_type in (FieldType.SINGLE_SELECT, FieldType.MULTI_SELECT):
        if fdef.select_options:
            body["property"] = {
                "options": [
                    {"name": opt} for opt in fdef.select_options if opt
                ],
            }
    # DateTime cần property.date_formatter (Lark default OK)
    elif fdef.field_type == FieldType.DATETIME:
        body["property"] = {
            "date_formatter": "yyyy/MM/dd HH:mm",
            "auto_fill": False,
        }
    # Number — default decimal places
    elif fdef.field_type == FieldType.NUMBER:
        body["property"] = {"formatter": "0"}

    return body
