"""Schema definition cho Lark Base table — 12 nhóm fields theo pipeline flow.

Nguyên tắc thiết kế:
  - **Field name VI** (theo CLAUDE.md): "Tiêu đề", "Liên kết clone"…
  - **Group theo stage**: IDENTITY → STATUS → SOURCE → CRAWL → CLONE
    → TRANSLATE → MIRROR → SYNC → BACKLINKS → TREE_ORDER → QA → META.
  - **5-7 fields per stage**:
      Status, Started At, Completed At, Duration Seconds, Attempts,
      Error, + 1-2 metrics đặc thù.
  - **Sticky left**: STT + Tiêu đề (Lark Base support sticky).
  - **Audit trail field cuối** — text dài, append-only.

Lark Field Type codes (theo Lark API docs):
  1=Text, 2=Number, 3=SingleSelect, 4=MultiSelect, 5=DateTime,
  7=Checkbox, 11=User, 13=Phone, 15=URL, 17=Attachment, 18=OneWayLink,
  19=Lookup, 20=Formula, 21=DuplexLink, 22=Location, 23=Group,
  1001=Created, 1002=Modified, 1003=CreatedBy, 1004=ModifiedBy,
  1005=AutoSerial.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum


class FieldType(IntEnum):
    """Lark Base field type code."""

    TEXT = 1
    NUMBER = 2
    SINGLE_SELECT = 3
    MULTI_SELECT = 4
    DATETIME = 5
    CHECKBOX = 7
    URL = 15
    FORMULA = 20
    AUTO_SERIAL = 1005
    CREATED_TIME = 1001
    MODIFIED_TIME = 1002


class FieldGroup(StrEnum):
    """Nhóm fields theo pipeline stage — sắp xếp left → right."""

    IDENTITY = "📌 Identity"
    PIPELINE_STATUS = "🔄 Pipeline Status"
    SOURCE = "🌐 Source Info"
    CRAWL = "📥 Stage 1: Crawl + Placeholder"
    CLONE = "📋 Stage 2: Clone"
    TRANSLATE = "🌐 Stage 3: Translate"
    MIRROR = "🪞 Stage 4: Mirror"
    SYNC = "🔄 Stage 5: Sync"
    BACKLINKS = "🔗 Stage 6: Backlinks"
    TREE_ORDER = "🌳 Stage 7: Tree Order"
    QA = "✅ QA + Audit"
    META = "🚨 Meta + Legacy"


# Common select options reused across stages
_STAGE_STATUS_OPTIONS = [
    "Pending", "Running", "Done", "Failed", "Skipped",
]
_PIPELINE_STAGE_OPTIONS = [
    "Pending", "Crawling", "Placeholder", "Cloning",
    "Translating", "Mirroring", "Syncing", "Reordering",
    "Done", "Failed",
]


@dataclass(frozen=True, slots=True)
class FieldDef:
    """1 field trong Lark Base — name + type + optional select options."""

    name: str
    """Field name VI/EN — public, dùng cho Pydantic alias."""

    field_type: FieldType
    """Lark API field type code."""

    group: FieldGroup
    """Nhóm UI — Lark Base GUI dùng để tạo View partition."""

    description: str = ""
    """Mô tả field — sẽ display trong Lark Base hover."""

    select_options: tuple[str, ...] = ()
    """Cho SINGLE_SELECT/MULTI_SELECT."""

    formula: str = ""
    """Cho FORMULA — Lark formula expression."""

    is_primary: bool = False
    """True nếu là primary field (chỉ 1 field/table)."""

    is_required: bool = False
    """True → caller phải fill khi create record."""

    sticky_left: bool = False
    """Đề xuất sticky-left trong default View."""


# ============================================================================
# 1. IDENTITY — sticky left, key fields
# ============================================================================
_IDENTITY: list[FieldDef] = [
    FieldDef("STT", FieldType.NUMBER, FieldGroup.IDENTITY,
             description="Số thứ tự", sticky_left=True),
    FieldDef("Tiêu đề", FieldType.TEXT, FieldGroup.IDENTITY,
             description="Tiêu đề tiếng Việt (sau translate)",
             is_primary=True, sticky_left=True),
    FieldDef("Title", FieldType.TEXT, FieldGroup.IDENTITY,
             description="Tiêu đề gốc CN từ source"),
    FieldDef("Node Token", FieldType.TEXT, FieldGroup.IDENTITY,
             description="Wiki node token CN — primary key của record",
             is_required=True),
    FieldDef("Parent Node Token", FieldType.TEXT, FieldGroup.IDENTITY,
             description="Parent wiki node token CN"),
    FieldDef("Obj Token", FieldType.TEXT, FieldGroup.IDENTITY,
             description="Obj token (docx/sheet/bitable...)"),
    FieldDef("Obj Type", FieldType.TEXT, FieldGroup.IDENTITY,
             description="docx/sheet/bitable/file/mindnote..."),
    FieldDef("Node Type", FieldType.TEXT, FieldGroup.IDENTITY,
             description="origin/shortcut"),
]


# ============================================================================
# 2. PIPELINE STATUS — overview cho dashboard
# ============================================================================
_PIPELINE_STATUS: list[FieldDef] = [
    FieldDef("Pipeline Stage", FieldType.SINGLE_SELECT,
             FieldGroup.PIPELINE_STATUS,
             description="Stage hiện tại trong pipeline",
             select_options=tuple(_PIPELINE_STAGE_OPTIONS)),
    FieldDef("Last Activity At", FieldType.DATETIME,
             FieldGroup.PIPELINE_STATUS,
             description="Lần cuối có activity (any stage)"),
    FieldDef("Total Duration Seconds", FieldType.NUMBER,
             FieldGroup.PIPELINE_STATUS,
             description="Tổng thời gian xử lý (sum tất cả stages)"),
    FieldDef("Current Worker", FieldType.TEXT,
             FieldGroup.PIPELINE_STATUS,
             description="Process/correlation_id worker đang xử lý"),
]


# ============================================================================
# 3. SOURCE INFO — CN tenant
# ============================================================================
_SOURCE: list[FieldDef] = [
    FieldDef("Liên kết gốc", FieldType.URL, FieldGroup.SOURCE,
             description="URL doc gốc CN (waytoagi.feishu.cn)"),
    FieldDef("Source Status", FieldType.SINGLE_SELECT, FieldGroup.SOURCE,
             description="Source CN còn tồn tại không",
             select_options=("Present", "Deleted")),
    FieldDef("Crawled At", FieldType.DATETIME, FieldGroup.SOURCE,
             description="Lần đầu crawl detect record"),
    FieldDef("Last Seen At", FieldType.DATETIME, FieldGroup.SOURCE,
             description="Crawl gần nhất thấy record này"),
    FieldDef("Last Edit Time", FieldType.DATETIME, FieldGroup.SOURCE,
             description="obj_edit_time của source CN"),
    FieldDef("Source Block Count", FieldType.NUMBER, FieldGroup.SOURCE,
             description="Số block trong source doc"),
    FieldDef("Change Status", FieldType.SINGLE_SELECT, FieldGroup.SOURCE,
             description="Đổi gì so lần crawl trước",
             select_options=("", "edited", "renamed", "deleted")),
]


# ============================================================================
# 4. STAGE 1 — CRAWL + PLACEHOLDER
# ============================================================================
_CRAWL: list[FieldDef] = [
    FieldDef("Crawl Status", FieldType.SINGLE_SELECT, FieldGroup.CRAWL,
             description="Trạng thái phase crawl",
             select_options=tuple(_STAGE_STATUS_OPTIONS)),
    FieldDef("Crawl At", FieldType.DATETIME, FieldGroup.CRAWL,
             description="Hoàn tất crawl record"),
    FieldDef("Crawl Attempts", FieldType.NUMBER, FieldGroup.CRAWL,
             description="Số lần thử crawl record này"),
    FieldDef("Crawl Error", FieldType.TEXT, FieldGroup.CRAWL,
             description="Error message nếu fail"),
    FieldDef("Placeholder Status", FieldType.SINGLE_SELECT, FieldGroup.CRAWL,
             description="Trạng thái tạo dst placeholder",
             select_options=("", "Created", "Failed")),
    FieldDef("Placeholder Created At", FieldType.DATETIME, FieldGroup.CRAWL,
             description="Khi nào tạo dst placeholder thành công"),
    FieldDef("Placeholder Error", FieldType.TEXT, FieldGroup.CRAWL,
             description="Error nếu placeholder fail"),
]


# ============================================================================
# 5. STAGE 2 — CLONE (CN → VI working copy)
# ============================================================================
_CLONE: list[FieldDef] = [
    FieldDef("Liên kết clone", FieldType.URL, FieldGroup.CLONE,
             description="URL VI working doc đã clone"),
    FieldDef("Clone Status", FieldType.SINGLE_SELECT, FieldGroup.CLONE,
             description="Trạng thái clone",
             select_options=tuple(_STAGE_STATUS_OPTIONS)),
    FieldDef("Clone Started At", FieldType.DATETIME, FieldGroup.CLONE,
             description="Bắt đầu clone"),
    FieldDef("Clone Completed At", FieldType.DATETIME, FieldGroup.CLONE,
             description="Hoàn tất clone"),
    FieldDef("Clone Duration Seconds", FieldType.NUMBER, FieldGroup.CLONE,
             description="Thời gian clone (giây)"),
    FieldDef("Clone Block Count", FieldType.NUMBER, FieldGroup.CLONE,
             description="Số block đã recreate"),
    FieldDef("Clone Attempts", FieldType.NUMBER, FieldGroup.CLONE,
             description="Số lần thử clone"),
    FieldDef("Clone Error", FieldType.TEXT, FieldGroup.CLONE,
             description="Error message nếu clone fail"),
]


# ============================================================================
# 6. STAGE 3 — TRANSLATE (CN → VI in-place)
# ============================================================================
_TRANSLATE: list[FieldDef] = [
    FieldDef("Liên kết dịch", FieldType.URL, FieldGroup.TRANSLATE,
             description="URL VI doc đã translate"),
    FieldDef("Translate Status", FieldType.SINGLE_SELECT, FieldGroup.TRANSLATE,
             description="Trạng thái dịch",
             select_options=tuple(_STAGE_STATUS_OPTIONS)),
    FieldDef("Translate Started At", FieldType.DATETIME, FieldGroup.TRANSLATE),
    FieldDef("Translate Completed At", FieldType.DATETIME,
             FieldGroup.TRANSLATE),
    FieldDef("Translate Duration Seconds", FieldType.NUMBER,
             FieldGroup.TRANSLATE),
    FieldDef("Translate Block Count", FieldType.NUMBER, FieldGroup.TRANSLATE,
             description="Số block đã translate"),
    FieldDef("% Dịch", FieldType.NUMBER, FieldGroup.TRANSLATE,
             description="Tỷ lệ block dịch xong (0-100)"),
    FieldDef("Số segment dịch", FieldType.NUMBER, FieldGroup.TRANSLATE,
             description="Số text segment đã dịch (sub-block)"),
    FieldDef("Translate Cache Hit Pct", FieldType.NUMBER,
             FieldGroup.TRANSLATE,
             description="% block hit translation cache (0-100)"),
    FieldDef("Translate LLM Calls", FieldType.NUMBER, FieldGroup.TRANSLATE,
             description="Số LLM call thực sự gọi (sau cache + glossary)"),
    FieldDef("Translate Attempts", FieldType.NUMBER, FieldGroup.TRANSLATE),
    FieldDef("Translate Error", FieldType.TEXT, FieldGroup.TRANSLATE),
]


# ============================================================================
# 7. STAGE 4 — MIRROR (VI → DST tenant)
# ============================================================================
_MIRROR: list[FieldDef] = [
    FieldDef("Liên kết wiki dịch mới", FieldType.URL, FieldGroup.MIRROR,
             description="URL DST doc cuối — public người Việt đọc"),
    FieldDef("Mirror Wiki Node Token", FieldType.TEXT, FieldGroup.MIRROR,
             description="DST wiki node token (placeholder hoặc filled)"),
    FieldDef("Mirror Wiki Status", FieldType.SINGLE_SELECT, FieldGroup.MIRROR,
             description="Trạng thái mirror",
             select_options=("", "Placeholder", "PlaceholderFailed",
                             "Filling", "Done", "Synced", "PartialFail",
                             "NoChange", "Failed")),
    FieldDef("Mirror Started At", FieldType.DATETIME, FieldGroup.MIRROR),
    FieldDef("Mirror Completed At", FieldType.DATETIME, FieldGroup.MIRROR),
    FieldDef("Mirror Duration Seconds", FieldType.NUMBER, FieldGroup.MIRROR),
    FieldDef("Mirror Attempts", FieldType.NUMBER, FieldGroup.MIRROR),
    FieldDef("Mirror Error", FieldType.TEXT, FieldGroup.MIRROR),
]


# ============================================================================
# 8. STAGE 5 — SYNC (block-level diff)
# ============================================================================
_SYNC: list[FieldDef] = [
    FieldDef("Mirror Last Synced At", FieldType.DATETIME, FieldGroup.SYNC,
             description="Lần sync gần nhất"),
    FieldDef("Sync Block Replaced", FieldType.NUMBER, FieldGroup.SYNC,
             description="Số block đã PATCH (REPLACE op)"),
    FieldDef("Sync Block Appended", FieldType.NUMBER, FieldGroup.SYNC,
             description="Số block append (src dài hơn)"),
    FieldDef("Sync Block Kept", FieldType.NUMBER, FieldGroup.SYNC,
             description="Số block KEEP (hash trùng, không touch)"),
    FieldDef("Sync Saved Calls", FieldType.NUMBER, FieldGroup.SYNC,
             description="Số API call tiết kiệm nhờ block-diff"),
    FieldDef("Sync Attempts", FieldType.NUMBER, FieldGroup.SYNC),
    FieldDef("Sync Error", FieldType.TEXT, FieldGroup.SYNC),
]


# ============================================================================
# 9. STAGE 6 — BACKLINKS
# ============================================================================
_BACKLINKS: list[FieldDef] = [
    FieldDef("Backlink Fix Status", FieldType.SINGLE_SELECT,
             FieldGroup.BACKLINKS,
             description="Trạng thái backlink fix",
             select_options=tuple(_STAGE_STATUS_OPTIONS)),
    FieldDef("Backlink Fix At", FieldType.DATETIME, FieldGroup.BACKLINKS),
    FieldDef("Backlink Links Total", FieldType.NUMBER, FieldGroup.BACKLINKS,
             description="Tổng số link CN trong doc"),
    FieldDef("Backlink Links Replaced", FieldType.NUMBER,
             FieldGroup.BACKLINKS,
             description="Số link đã swap CN → DST"),
]


# ============================================================================
# 10. STAGE 7 — TREE ORDER
# ============================================================================
_TREE_ORDER: list[FieldDef] = [
    FieldDef("Tree Order Status", FieldType.SINGLE_SELECT,
             FieldGroup.TREE_ORDER,
             description="Trạng thái audit/fix tree order",
             select_options=("", "OK", "Mismatch", "Fixed", "Skipped",
                             "Error")),
    FieldDef("Tree Order Last Audit", FieldType.DATETIME,
             FieldGroup.TREE_ORDER),
    FieldDef("Tree Order Mismatches", FieldType.NUMBER,
             FieldGroup.TREE_ORDER,
             description="Số lần audit detect mismatch tích lũy"),
]


# ============================================================================
# 11. QA + AUDIT TRAIL
# ============================================================================
_QA: list[FieldDef] = [
    FieldDef("QA bản clone", FieldType.SINGLE_SELECT, FieldGroup.QA,
             description="QA review bản clone",
             select_options=("Pending", "Pass", "Fail")),
    FieldDef("QA bản dịch", FieldType.SINGLE_SELECT, FieldGroup.QA,
             description="QA review bản dịch",
             select_options=("Pending", "Pass", "Fail")),
    FieldDef("Audit Trail", FieldType.TEXT, FieldGroup.QA,
             description="Append-only event log — last N events"),
    FieldDef("Audit Run ID", FieldType.TEXT, FieldGroup.QA,
             description="Crawl checkpoint run_id (UUID) — link tracing"),
]


# ============================================================================
# 12. META — legacy + system fields
# ============================================================================
_META: list[FieldDef] = [
    # Legacy fields giữ back-compat với V1 base records
    FieldDef("Trạng thái", FieldType.SINGLE_SELECT, FieldGroup.META,
             description="(legacy) — V2 dùng Pipeline Stage thay thế",
             select_options=tuple(_STAGE_STATUS_OPTIONS)),
    FieldDef("Trạng thái dịch", FieldType.SINGLE_SELECT, FieldGroup.META,
             description="(legacy) — V2 dùng Translate Status",
             select_options=(*_STAGE_STATUS_OPTIONS, "Translating")),
    FieldDef("Lỗi", FieldType.TEXT, FieldGroup.META,
             description="(legacy) — V2 dùng <Stage> Error"),
    FieldDef("Số lần thử", FieldType.NUMBER, FieldGroup.META,
             description="(legacy) — V2 dùng <Stage> Attempts"),
    FieldDef("Thời gian", FieldType.NUMBER, FieldGroup.META,
             description="(legacy) — V2 dùng Clone Duration"),
    FieldDef("Thời gian dịch", FieldType.NUMBER, FieldGroup.META,
             description="(legacy) — V2 dùng Translate Duration"),
    FieldDef("Created Time", FieldType.CREATED_TIME, FieldGroup.META),
    FieldDef("Modified Time", FieldType.MODIFIED_TIME, FieldGroup.META),
]


# ============================================================================
# Aggregate
# ============================================================================
SCHEMA_FIELDS: list[FieldDef] = [
    *_IDENTITY,
    *_PIPELINE_STATUS,
    *_SOURCE,
    *_CRAWL,
    *_CLONE,
    *_TRANSLATE,
    *_MIRROR,
    *_SYNC,
    *_BACKLINKS,
    *_TREE_ORDER,
    *_QA,
    *_META,
]
"""All fields theo thứ tự pipeline flow — dùng làm column order
mặc định cho default View."""


# ============================================================================
# Lookup helpers
# ============================================================================
_BY_NAME: dict[str, FieldDef] = {f.name: f for f in SCHEMA_FIELDS}


def get_field(name: str) -> FieldDef | None:
    """Lookup field by name. Trả None nếu không có."""
    return _BY_NAME.get(name)


def field_groups() -> dict[FieldGroup, list[FieldDef]]:
    """Trả về dict group → list FieldDef preserving order."""
    out: dict[FieldGroup, list[FieldDef]] = {g: [] for g in FieldGroup}
    for f in SCHEMA_FIELDS:
        out[f.group].append(f)
    return out


# Verify schema invariants tại import time (fail fast)
def _verify_schema() -> None:
    names = [f.name for f in SCHEMA_FIELDS]
    if len(names) != len(set(names)):
        dups = {n for n in names if names.count(n) > 1}
        msg = f"Duplicate field names: {dups}"
        raise RuntimeError(msg)
    primary = [f for f in SCHEMA_FIELDS if f.is_primary]
    if len(primary) != 1:
        msg = f"Schema phải có đúng 1 primary field, got {len(primary)}"
        raise RuntimeError(msg)


_verify_schema()
