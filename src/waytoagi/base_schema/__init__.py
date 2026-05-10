"""Lark Base table schema definition + migration + audit trail.

3 nguyên tắc:
  1. **Đủ fields tracking**: 12 groups, mỗi stage có 5-7 fields (status,
     started_at, completed_at, duration, attempts, error, metrics).
  2. **Sắp xếp thông minh**: groups theo pipeline flow từ trái → phải:
     IDENTITY → STATUS → SOURCE → CRAWL → CLONE → TRANSLATE → MIRROR
     → SYNC → BACKLINKS → TREE_ORDER → QA → META.
  3. **Real-time updates + Audit trail**: `BaseFieldUpdater` service
     centralized + `AuditTrail` field append-only ghi mọi transition.

Public API:
    SCHEMA_FIELDS    — list FieldDef đầy đủ
    field_groups()   — group → list FieldDef
    BaseFieldUpdater — service real-time update
    AuditTrail       — append-only event log
    SchemaMigration  — auto-create missing fields
"""

from waytoagi.base_schema.audit import AuditEvent, AuditTrail
from waytoagi.base_schema.fields import (
    SCHEMA_FIELDS,
    FieldDef,
    FieldGroup,
    FieldType,
    field_groups,
    get_field,
)
from waytoagi.base_schema.migration import MigrationDiff, SchemaMigration
from waytoagi.base_schema.updater import BaseFieldUpdater, StageUpdate

__all__ = [
    "SCHEMA_FIELDS",
    "AuditEvent",
    "AuditTrail",
    "BaseFieldUpdater",
    "FieldDef",
    "FieldGroup",
    "FieldType",
    "MigrationDiff",
    "SchemaMigration",
    "StageUpdate",
    "field_groups",
    "get_field",
]
