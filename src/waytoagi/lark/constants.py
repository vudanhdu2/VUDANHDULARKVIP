"""Lark Docx block type constants — port từ legacy agents/utils.py."""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Final


class BlockType(IntEnum):
    """Lark Docx block_type enum (đầy đủ 35+ types)."""

    PAGE = 1
    TEXT = 2
    HEADING1 = 3
    HEADING2 = 4
    HEADING3 = 5
    HEADING4 = 6
    HEADING5 = 7
    HEADING6 = 8
    HEADING7 = 9
    HEADING8 = 10
    HEADING9 = 11
    BULLET = 12
    ORDERED = 13
    CODE = 14
    QUOTE = 15
    EQUATION = 16
    TODO = 17
    BITABLE = 18
    CALLOUT = 19
    CHAT_CARD = 20
    DIAGRAM = 21
    DIVIDER = 22
    FILE = 23
    GRID = 24
    GRID_COLUMN = 25
    IFRAME = 26
    IMAGE = 27
    ISV = 28
    MINDNOTE = 29
    SHEET = 30
    TABLE = 31
    TABLE_CELL = 32
    VIEW = 33
    QUOTE_CONTAINER = 34
    SYNCED_BLOCK = 49


# Map block_type int → field name in block dict
BLOCK_TYPE_FIELD: Final[dict[int, str]] = {
    BlockType.TEXT: "text",
    BlockType.HEADING1: "heading1",
    BlockType.HEADING2: "heading2",
    BlockType.HEADING3: "heading3",
    BlockType.HEADING4: "heading4",
    BlockType.HEADING5: "heading5",
    BlockType.HEADING6: "heading6",
    BlockType.HEADING7: "heading7",
    BlockType.HEADING8: "heading8",
    BlockType.HEADING9: "heading9",
    BlockType.BULLET: "bullet",
    BlockType.ORDERED: "ordered",
    BlockType.CODE: "code",
    BlockType.QUOTE: "quote",
    BlockType.TODO: "todo",
}

BLOCK_TYPE_NAME: Final[dict[int, str]] = {
    bt.value: bt.name.lower() for bt in BlockType
}

# Block types có rich-text "elements" array
TEXT_BLOCK_TYPES: Final[frozenset[int]] = frozenset(BLOCK_TYPE_FIELD.keys())

# Style keys mà Larksuite chấp nhận (filter để tránh cross-tenant rejection)
VALID_TEXT_ELEMENT_STYLE_KEYS: Final[frozenset[str]] = frozenset({
    "bold", "italic", "underline", "strikethrough", "inline_code",
    "background_color", "text_color", "link",
})

# Block types KHÔNG hỗ trợ clone (Lark API limit)
UNSUPPORTED_BLOCK_TYPES: Final[frozenset[int]] = frozenset({
    BlockType.DIAGRAM,    # type 21
    BlockType.SHEET,      # type 30
    BlockType.SYNCED_BLOCK,  # type 49 — flatten thay vì clone
})


class CloneStatus(StrEnum):
    """Trạng thái clone trong Bitable state table."""

    PENDING = "Pending"
    RUNNING = "Running"
    DONE = "Done"
    FAILED = "Failed"
    SKIPPED = "Skipped"


class TranslateStatus(StrEnum):
    """Trạng thái translate."""

    PENDING = "Pending"
    RUNNING = "Running"
    DONE = "Done"
    FAILED = "Failed"


class MirrorStatus(StrEnum):
    """Trạng thái mirror sang DST space."""

    PENDING = "Pending"
    RUNNING = "Running"
    DONE = "Done"
    FAILED = "Failed"


class ErrorCategory(StrEnum):
    """Phân loại lỗi (theo CLAUDE.md global rule — vddclonelark)."""

    STAGE1_SOURCE_DELETED = "STAGE1-SOURCE-DELETED"   # 131005
    STAGE1_PERM_DENIED = "STAGE1-PERM-DENIED"          # 131006
    STAGE1_TYPE_UNSUPPORTED = "STAGE1-TYPE-UNSUPPORTED"  # sheet/slides/mindnote
    STAGE1_FAIL = "STAGE1-FAIL"                          # retryable
    STAGE23_TRANSLATE_RATE = "STAGE23-FAIL-TRANSLATE-RATE"
    STAGE3_MIRROR_FAIL = "STAGE3-MIRROR-FAIL"
