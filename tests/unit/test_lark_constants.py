"""Unit tests cho lark/constants.py — sanity checks on enums + maps."""

from __future__ import annotations

import pytest

from waytoagi.lark.constants import (
    BLOCK_TYPE_FIELD,
    BLOCK_TYPE_NAME,
    TEXT_BLOCK_TYPES,
    UNSUPPORTED_BLOCK_TYPES,
    VALID_TEXT_ELEMENT_STYLE_KEYS,
    BlockType,
    CloneStatus,
    ErrorCategory,
    MirrorStatus,
    TranslateStatus,
)


@pytest.mark.unit
class TestBlockTypes:
    def test_field_map_keys_are_text_blocks(self) -> None:
        assert BlockType.TEXT in BLOCK_TYPE_FIELD
        assert BlockType.HEADING1 in BLOCK_TYPE_FIELD
        assert BLOCK_TYPE_FIELD[BlockType.HEADING1] == "heading1"
        assert BlockType.IMAGE not in BLOCK_TYPE_FIELD

    def test_text_block_types_consistency(self) -> None:
        assert set(TEXT_BLOCK_TYPES) == set(BLOCK_TYPE_FIELD.keys())

    def test_block_type_name_complete(self) -> None:
        assert BLOCK_TYPE_NAME[1] == "page"
        assert BLOCK_TYPE_NAME[27] == "image"
        assert BLOCK_TYPE_NAME[49] == "synced_block"

    def test_unsupported_block_types(self) -> None:
        assert BlockType.DIAGRAM in UNSUPPORTED_BLOCK_TYPES
        assert BlockType.SHEET in UNSUPPORTED_BLOCK_TYPES
        assert BlockType.SYNCED_BLOCK in UNSUPPORTED_BLOCK_TYPES
        assert BlockType.IMAGE not in UNSUPPORTED_BLOCK_TYPES


@pytest.mark.unit
class TestStyle:
    def test_valid_style_keys(self) -> None:
        assert "bold" in VALID_TEXT_ELEMENT_STYLE_KEYS
        assert "link" in VALID_TEXT_ELEMENT_STYLE_KEYS
        assert "mention_user" not in VALID_TEXT_ELEMENT_STYLE_KEYS


@pytest.mark.unit
class TestEnums:
    def test_clone_status_values(self) -> None:
        assert str(CloneStatus.DONE) == "Done"
        assert str(CloneStatus.FAILED) == "Failed"

    def test_translate_mirror_status(self) -> None:
        assert str(TranslateStatus.DONE) == "Done"
        assert str(MirrorStatus.PENDING) == "Pending"

    def test_error_category(self) -> None:
        assert str(ErrorCategory.STAGE1_SOURCE_DELETED) == "STAGE1-SOURCE-DELETED"
        assert str(ErrorCategory.STAGE1_PERM_DENIED) == "STAGE1-PERM-DENIED"
