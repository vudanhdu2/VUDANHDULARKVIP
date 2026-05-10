"""Lark Docx block schemas — block types + element types."""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BlockType(IntEnum):
    """Lark Docx block types — subset most common."""

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
    UNSUPPORTED = 18
    CALLOUT = 19
    DIAGRAM = 22
    FILE = 23
    GRID = 24
    GRID_COLUMN = 25
    IFRAME = 26
    IMAGE = 27
    ISV = 30
    TABLE = 31
    TABLE_CELL = 32
    VIEW = 33
    QUOTE_CONTAINER = 34
    ADD_ONS = 40
    BOARD = 43
    SYNCED_BLOCK = 49
    REFERENCE_SYNCED = 50
    REFERENCE_BASE = 53
    MEETING_QA = 55


# Block type → field name mapping (text-bearing blocks)
TEXT_BLOCK_FIELDS: dict[int, str] = {
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
    BlockType.CALLOUT: "callout",
    BlockType.QUOTE_CONTAINER: "quote_container",
}


class TextElementStyle(BaseModel):
    """Style cho text_run element."""

    model_config = ConfigDict(extra="allow")

    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    strikethrough: bool | None = None
    inline_code: bool | None = None
    background_color: int | None = None
    text_color: int | None = None
    link: dict[str, str] | None = None  # {url: ...}


class TextRun(BaseModel):
    """text_run element — basic text với optional style."""

    content: str = ""
    text_element_style: TextElementStyle | None = None


class MentionDoc(BaseModel):
    """mention_doc element — link to another wiki/docx."""

    token: str = ""
    obj_type: int = 0
    title: str = ""
    url: str = ""


class MentionUser(BaseModel):
    """mention_user element — @user reference."""

    user_id: str = ""
    notify: bool = False


class Equation(BaseModel):
    """equation element — LaTeX inline."""

    content: str = ""


class Element(BaseModel):
    """Polymorphic element — exactly 1 field set."""

    model_config = ConfigDict(extra="allow")

    text_run: TextRun | None = None
    mention_doc: MentionDoc | None = None
    mention_user: MentionUser | None = None
    equation: Equation | None = None

    @property
    def is_text(self) -> bool:
        return self.text_run is not None

    @property
    def text_content(self) -> str:
        """Extract plain text content."""
        if self.text_run:
            return self.text_run.content
        if self.mention_doc:
            return self.mention_doc.title
        if self.mention_user:
            return ""
        if self.equation:
            return self.equation.content
        return ""


class TextBlockField(BaseModel):
    """Common shape của text-bearing block field (text, heading*, bullet, ...)."""

    elements: list[Element] = Field(default_factory=list)
    style: dict[str, Any] = Field(default_factory=dict)


class Image(BaseModel):
    """Image block content (block_type=27)."""

    token: str = ""
    width: int = 0
    height: int = 0
    align: int = 0
    caption: TextBlockField | None = None


class FileBlock(BaseModel):
    """File block content (block_type=23)."""

    token: str = ""
    name: str = ""
    view_type: int = 2  # 2=card


class TableProperty(BaseModel):
    """Table property — row_size, column_size."""

    row_size: int = 1
    column_size: int = 1
    column_width: list[int] | None = None


class Table(BaseModel):
    """Table block (block_type=31)."""

    property: TableProperty = Field(default_factory=TableProperty)
    cells: list[str] = Field(default_factory=list, description="Cell block IDs (row-major)")


class GridColumn(BaseModel):
    """Grid column property."""

    width_ratio: int = 50


class Grid(BaseModel):
    """Grid block (block_type=24)."""

    column_size: int = 1
    column_size_ratio: list[int] = Field(default_factory=list)


class Block(BaseModel):
    """Lark Docx block — superset polymorphic structure.

    Một block có CHỈ MỘT field content set (theo block_type). Pydantic không
    enforce mạnh điều này (cho phép linh hoạt parse Lark response).
    """

    model_config = ConfigDict(extra="allow")

    block_id: str
    parent_id: str = ""
    block_type: int
    children: list[str] = Field(default_factory=list)

    # Polymorphic content — chỉ 1 field set theo block_type
    text: TextBlockField | None = None
    heading1: TextBlockField | None = None
    heading2: TextBlockField | None = None
    heading3: TextBlockField | None = None
    heading4: TextBlockField | None = None
    heading5: TextBlockField | None = None
    heading6: TextBlockField | None = None
    heading7: TextBlockField | None = None
    heading8: TextBlockField | None = None
    heading9: TextBlockField | None = None
    bullet: TextBlockField | None = None
    ordered: TextBlockField | None = None
    code: TextBlockField | None = None
    quote: TextBlockField | None = None
    todo: TextBlockField | None = None
    callout: TextBlockField | None = None
    quote_container: TextBlockField | None = None
    image: Image | None = None
    file: FileBlock | None = None
    table: Table | None = None
    grid: Grid | None = None
    table_cell: dict[str, Any] | None = None
    view: dict[str, Any] | None = None
    iframe: dict[str, Any] | None = None

    def get_text_field(self) -> TextBlockField | None:
        """Return text-bearing field theo block_type."""
        field_name = TEXT_BLOCK_FIELDS.get(self.block_type)
        if not field_name:
            return None
        return getattr(self, field_name, None)
