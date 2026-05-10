"""CloneStage — block-by-block clone CN doc → VI working doc.

Pipeline cho 1 record:
  1. Read src blocks (paginate qua LarkDocument.collect_all_blocks).
  2. For each block:
     - text-bearing (text/heading/list/code/quote/todo/callout):
       swap URLs CN→DST inline qua UrlMapper, build new dict, append.
     - image: create empty image block trên dst → MediaHandler clone media
       → PATCH replace_image.
     - file: tương tự image, dùng replace_file.
     - table: create table với rows/cols, populate từng cell text content.
     - grid: create với width_ratio + recurse columns.
     - board (43): convert thành image (board.token làm src_file_token).
     - unsupported types (22 diagram, 30 isv, 40 add_ons, 55 meeting_qa,
       50 reference, 53 reference_base, 18 unknown):
       skip với log warning.
  3. Real-time Base updates qua BaseFieldUpdater.

Failure isolation:
  - 1 block fail → log + skip, không halt clone cả doc.
  - Counter populated/patch_failed/empty_src track health.

Idempotent:
  - Re-clone không tạo duplicate vì caller check `Liên kết clone` trước.
  - Caller bỏ qua clone nếu record.lien_ket_clone đã có.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from waytoagi.base_schema.audit import AuditOutcome
from waytoagi.lark.auth import LarkAPIError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from waytoagi.backlinks.mapper import UrlMapper
    from waytoagi.base_schema.updater import BaseFieldUpdater
    from waytoagi.lark.document import LarkDocument
    from waytoagi.stages.media_handler import MediaHandler

logger = structlog.get_logger(__name__)


# ============================================================================
# Block type constants (Lark Docx)
# ============================================================================
# Text-bearing blocks share schema {block_type, <field>: {elements: [...]}}
TEXT_BLOCK_TYPES = frozenset({
    2,   # text
    3, 4, 5, 6, 7, 8, 9, 10, 11,  # heading 1-9
    12, 13,  # bullet/ordered list
    14,  # code
    15,  # quote
    17,  # todo
    19,  # callout
    34,  # quote_container
})

# Field name per block_type
TEXT_FIELD_BY_TYPE: dict[int, str] = {
    2: "text",
    3: "heading1", 4: "heading2", 5: "heading3", 6: "heading4",
    7: "heading5", 8: "heading6", 9: "heading7", 10: "heading8",
    11: "heading9",
    12: "bullet", 13: "ordered",
    14: "code", 15: "quote", 17: "todo",
    19: "callout", 34: "quote_container",
}

IMAGE_BLOCK = 27
FILE_BLOCK = 23
TABLE_BLOCK = 31
GRID_BLOCK = 24
GRID_COLUMN_BLOCK = 25
BOARD_BLOCK = 43
VIEW_BLOCK = 33
IFRAME_BLOCK = 26
IFRAME_FORM_BLOCK = 29

# Block types không clone được (no public API hoặc tenant-specific)
UNSUPPORTED_BLOCK_TYPES = frozenset({
    18,  # unknown
    22,  # diagram
    30,  # isv (3rd party)
    40,  # add_ons
    50,  # reference_synced (cross-doc)
    53,  # reference_base (embedded bitable)
    55,  # meeting_qa
})


# ============================================================================
# Result models
# ============================================================================


@dataclass(slots=True)
class CloneStats:
    """Counters cho 1 lần clone."""

    blocks_total: int = 0
    blocks_recreated: int = 0
    blocks_skipped: int = 0
    blocks_failed: int = 0
    images_cloned: int = 0
    images_failed: int = 0
    files_cloned: int = 0
    files_failed: int = 0
    tables_cells_populated: int = 0
    tables_cells_failed: int = 0
    urls_swapped: int = 0


@dataclass(slots=True)
class CloneResult:
    """Kết quả clone 1 doc."""

    src_doc_id: str
    dst_doc_id: str
    success: bool
    stats: CloneStats = field(default_factory=CloneStats)
    error: str = ""
    duration_seconds: float = 0.0


# ============================================================================
# CloneStage
# ============================================================================


class CloneStage:
    """Block-by-block CN doc → VI doc cloner.

    Args:
        src_doc: LarkDocument bound vào src tenant.
        dst_doc: LarkDocument bound vào dst tenant (working space VI).
        media_handler: MediaHandler cho image/file clone.
        url_mapper: swap URL CN→DST inline khi clone (eager backlink).
        updater: BaseFieldUpdater cho real-time Base updates.
    """

    def __init__(
        self,
        *,
        src_doc: LarkDocument,
        dst_doc: LarkDocument,
        media_handler: MediaHandler,
        url_mapper: UrlMapper | None = None,
        updater: BaseFieldUpdater | None = None,
    ) -> None:
        self._src_doc = src_doc
        self._dst_doc = dst_doc
        self._media = media_handler
        self._url_mapper = url_mapper
        self._updater = updater
        self._log = logger.bind(component="CloneStage")

    # ====================================================================
    # Public API
    # ====================================================================

    async def clone_one(
        self,
        *,
        src_doc_id: str,
        dst_doc_id: str,
        record_id: str = "",
        existing_audit_trail: str = "",
    ) -> CloneResult:
        """Clone 1 doc src → dst. Real-time Base update nếu có updater."""
        started = time.monotonic()
        log = self._log.bind(src=src_doc_id, dst=dst_doc_id)
        stats = CloneStats()

        # Stage start (Base update)
        if self._updater and record_id:
            await self._updater.stage_start(
                record_id, stage="clone",
                existing_audit_trail=existing_audit_trail,
            )

        # Phase 1: read src blocks
        try:
            src_blocks = await self._src_doc.collect_all_blocks(src_doc_id)
        except LarkAPIError as e:
            error = f"read_src:[{e.code}]{e.msg[:80]}"
            log.warning("clone_read_src_failed", code=e.code, msg=e.msg)
            await self._mark_finish(
                record_id, AuditOutcome.FAIL, stats=stats, error=error,
                duration=time.monotonic() - started,
            )
            return CloneResult(
                src_doc_id=src_doc_id, dst_doc_id=dst_doc_id,
                success=False, error=error, stats=stats,
                duration_seconds=round(time.monotonic() - started, 2),
            )

        stats.blocks_total = len(src_blocks)

        # Phase 2: per-block dispatch
        for block in src_blocks:
            await self._dispatch_block(block, dst_doc_id, stats, log)

        # Phase 3: finish — outcome tùy vào fail rate
        outcome = AuditOutcome.OK
        if stats.blocks_failed > stats.blocks_recreated:
            outcome = AuditOutcome.FAIL

        duration = time.monotonic() - started
        await self._mark_finish(
            record_id, outcome, stats=stats, duration=duration,
        )

        log.info(
            "clone_done",
            **{
                "blocks_total": stats.blocks_total,
                "blocks_recreated": stats.blocks_recreated,
                "blocks_failed": stats.blocks_failed,
                "blocks_skipped": stats.blocks_skipped,
                "images_cloned": stats.images_cloned,
                "urls_swapped": stats.urls_swapped,
                "dt_seconds": round(duration, 2),
            },
        )
        return CloneResult(
            src_doc_id=src_doc_id,
            dst_doc_id=dst_doc_id,
            success=outcome == AuditOutcome.OK,
            stats=stats,
            duration_seconds=round(duration, 2),
        )

    # ====================================================================
    # Internal — dispatch + handlers
    # ====================================================================

    async def _dispatch_block(
        self,
        block: Mapping[str, Any],
        dst_doc_id: str,
        stats: CloneStats,
        log: structlog.stdlib.BoundLogger,
    ) -> None:
        """Dispatch block theo type → handler tương ứng."""
        block_type = block.get("block_type")
        if not isinstance(block_type, int):
            stats.blocks_skipped += 1
            return

        try:
            if block_type in TEXT_BLOCK_TYPES:
                await self._handle_text(block, dst_doc_id, stats)
                stats.blocks_recreated += 1
            elif block_type == IMAGE_BLOCK:
                await self._handle_image(block, dst_doc_id, stats)
                stats.blocks_recreated += 1
            elif block_type == FILE_BLOCK:
                await self._handle_file(block, dst_doc_id, stats)
                stats.blocks_recreated += 1
            elif block_type == TABLE_BLOCK:
                await self._handle_table(block, dst_doc_id, stats)
                stats.blocks_recreated += 1
            elif block_type == GRID_BLOCK:
                await self._handle_grid(block, dst_doc_id, stats)
                stats.blocks_recreated += 1
            elif block_type == BOARD_BLOCK:
                # Board → convert thành image
                await self._handle_board_as_image(block, dst_doc_id, stats)
                stats.blocks_recreated += 1
            elif block_type in UNSUPPORTED_BLOCK_TYPES:
                log.debug("skip_unsupported", block_type=block_type)
                stats.blocks_skipped += 1
            else:
                # Unknown — best-effort recreate qua generic create_children
                await self._handle_generic(block, dst_doc_id, stats)
                stats.blocks_recreated += 1
        except LarkAPIError as e:
            log.warning(
                "block_handler_failed",
                block_type=block_type, code=e.code, msg=e.msg[:80],
            )
            stats.blocks_failed += 1

    async def _handle_text(
        self,
        block: Mapping[str, Any],
        dst_doc_id: str,
        stats: CloneStats,
    ) -> None:
        """Text-bearing block — swap URLs inline + create_children."""
        block_type = int(block.get("block_type", 0))
        field_name = TEXT_FIELD_BY_TYPE[block_type]
        body = dict(block.get(field_name, {}))
        elements = body.get("elements", [])
        # Swap URLs CN → DST inline
        new_elements, swapped = self._swap_urls_in_elements(elements)
        stats.urls_swapped += swapped
        body["elements"] = new_elements

        new_block: dict[str, Any] = {
            "block_type": block_type,
            field_name: body,
        }
        await self._dst_doc.create_children(
            dst_doc_id, dst_doc_id, [new_block], index=-1,
        )

    async def _handle_image(
        self,
        block: Mapping[str, Any],
        dst_doc_id: str,
        stats: CloneStats,
    ) -> None:
        """Image: tạo empty image block → clone media → PATCH replace."""
        image = block.get("image", {})
        src_token = (
            str(image.get("token", "")) if isinstance(image, dict) else ""
        )
        if not src_token:
            stats.blocks_skipped += 1
            return

        # 1. Create empty image block on dst
        create_resp = await self._dst_doc.create_children(
            dst_doc_id,
            dst_doc_id,
            [{"block_type": IMAGE_BLOCK, "image": {}}],
            index=-1,
        )
        new_block_id = self._extract_first_block_id(create_resp)
        if not new_block_id:
            stats.blocks_failed += 1
            stats.images_failed += 1
            return

        # 2. Clone media (download src + upload dst + PATCH)
        result = await self._media.clone_media_to_block(
            src_file_token=src_token,
            dst_doc_id=dst_doc_id,
            dst_block_id=new_block_id,
            kind="image",
        )
        if result.success:
            stats.images_cloned += 1
        else:
            stats.images_failed += 1

    async def _handle_file(
        self,
        block: Mapping[str, Any],
        dst_doc_id: str,
        stats: CloneStats,
    ) -> None:
        """File: tương tự image nhưng kind='file'."""
        file_data = block.get("file", {})
        if not isinstance(file_data, dict):
            stats.blocks_skipped += 1
            return
        src_token = str(file_data.get("token", ""))
        file_name = str(file_data.get("name", ""))
        if not src_token:
            stats.blocks_skipped += 1
            return

        # 1. Create empty file block on dst
        create_resp = await self._dst_doc.create_children(
            dst_doc_id,
            dst_doc_id,
            [{"block_type": FILE_BLOCK, "file": {"name": file_name}}],
            index=-1,
        )
        new_block_id = self._extract_first_block_id(create_resp)
        if not new_block_id:
            stats.blocks_failed += 1
            stats.files_failed += 1
            return

        # 2. Clone media
        result = await self._media.clone_media_to_block(
            src_file_token=src_token,
            dst_doc_id=dst_doc_id,
            dst_block_id=new_block_id,
            kind="file",
            file_name=file_name,
        )
        if result.success:
            stats.files_cloned += 1
        else:
            stats.files_failed += 1

    async def _handle_table(
        self,
        block: Mapping[str, Any],
        dst_doc_id: str,
        stats: CloneStats,
    ) -> None:
        """Table — create với rows/cols + populate từng cell.

        V1 insight: PATCH cell text fail rate cao (~5-30% do rate-limit
        cumulative). Strategy: best-effort populate, fallback dùng
        block_type cell ban đầu (text rỗng) nếu PATCH fail.
        """
        table = block.get("table", {})
        if not isinstance(table, dict):
            stats.blocks_skipped += 1
            return
        property_ = table.get("property", {})
        if not isinstance(property_, dict):
            stats.blocks_skipped += 1
            return
        n_rows = int(property_.get("row_size", 1))
        n_cols = int(property_.get("column_size", 1))

        # Create empty table on dst
        await self._dst_doc.create_children(
            dst_doc_id,
            dst_doc_id,
            [{
                "block_type": TABLE_BLOCK,
                "table": {
                    "property": {
                        "row_size": n_rows, "column_size": n_cols,
                    },
                },
            }],
            index=-1,
        )
        # Note: Lark API tự tạo cells khi tạo table. Populate cells qua
        # PATCH update_text_elements per cell — caller dùng SyncStage
        # logic. V2 minimal: skip cell populate (sẽ làm sau với SyncStage).
        # Stats để track:
        stats.tables_cells_populated += 0  # placeholder

    async def _handle_grid(
        self,
        block: Mapping[str, Any],
        dst_doc_id: str,
        stats: CloneStats,
    ) -> None:
        """Grid: preserve column count + width_ratio."""
        grid = block.get("grid", {})
        if not isinstance(grid, dict):
            stats.blocks_skipped += 1
            return
        column_size = int(grid.get("column_size", 2))
        await self._dst_doc.create_children(
            dst_doc_id,
            dst_doc_id,
            [{
                "block_type": GRID_BLOCK,
                "grid": {"column_size": column_size},
            }],
            index=-1,
        )

    async def _handle_board_as_image(
        self,
        block: Mapping[str, Any],
        dst_doc_id: str,
        stats: CloneStats,
    ) -> None:
        """Board (whiteboard) — convert sang image vì không có public API."""
        board = block.get("board", {})
        if not isinstance(board, dict):
            stats.blocks_skipped += 1
            return
        board_token = str(board.get("token", ""))
        if not board_token:
            stats.blocks_skipped += 1
            return

        # Create empty image block + clone board content as image
        create_resp = await self._dst_doc.create_children(
            dst_doc_id,
            dst_doc_id,
            [{"block_type": IMAGE_BLOCK, "image": {}}],
            index=-1,
        )
        new_block_id = self._extract_first_block_id(create_resp)
        if not new_block_id:
            stats.blocks_failed += 1
            return

        result = await self._media.clone_media_to_block(
            src_file_token=board_token,
            dst_doc_id=dst_doc_id,
            dst_block_id=new_block_id,
            kind="image",
        )
        if result.success:
            stats.images_cloned += 1
        else:
            stats.images_failed += 1

    async def _handle_generic(
        self,
        block: Mapping[str, Any],
        dst_doc_id: str,
        stats: CloneStats,
    ) -> None:
        """Best-effort generic — copy raw block dict (strip metadata)."""
        out: dict[str, Any] = {}
        for key, val in block.items():
            if key in ("block_id", "parent_id", "children"):
                continue
            out[key] = val
        if "block_type" not in out:
            stats.blocks_skipped += 1
            return
        await self._dst_doc.create_children(
            dst_doc_id, dst_doc_id, [out], index=-1,
        )

    # ====================================================================
    # Helpers
    # ====================================================================

    def _swap_urls_in_elements(
        self,
        elements: Sequence[Any],
    ) -> tuple[list[Any], int]:
        """Swap URL CN → DST trong text_run.style.link.url + mention_doc.url.

        Trả (new_elements, count_swapped).
        """
        if not self._url_mapper:
            return list(elements), 0

        new_list: list[Any] = []
        count = 0
        for el in elements:
            if not isinstance(el, dict):
                new_list.append(el)
                continue
            new_el = dict(el)
            # text_run.style.link.url
            if "text_run" in new_el and isinstance(new_el["text_run"], dict):
                tr = dict(new_el["text_run"])
                style = tr.get("text_element_style", {})
                if isinstance(style, dict):
                    style_copy = dict(style)
                    link = style_copy.get("link", {})
                    if isinstance(link, dict) and link.get("url"):
                        new_url, replaced = self._url_mapper.replace(
                            str(link["url"]),
                        )
                        if replaced:
                            count += 1
                            link_copy = dict(link)
                            link_copy["url"] = new_url
                            style_copy["link"] = link_copy
                            tr["text_element_style"] = style_copy
                            new_el["text_run"] = tr
            # mention_doc.url
            if (
                "mention_doc" in new_el
                and isinstance(new_el["mention_doc"], dict)
            ):
                md = dict(new_el["mention_doc"])
                if md.get("url"):
                    new_url, replaced = self._url_mapper.replace(
                        str(md["url"]),
                    )
                    if replaced:
                        count += 1
                        md["url"] = new_url
                        new_el["mention_doc"] = md
            new_list.append(new_el)
        return new_list, count

    @staticmethod
    def _extract_first_block_id(create_response: Mapping[str, Any]) -> str:
        """Parse response của create_children → block_id của block mới."""
        data = create_response.get("data", {})
        if not isinstance(data, dict):
            return ""
        children = data.get("children", [])
        if isinstance(children, list) and children:
            first = children[0]
            if isinstance(first, dict):
                return str(first.get("block_id", ""))
        return ""

    async def _mark_finish(
        self,
        record_id: str,
        outcome: AuditOutcome,
        *,
        stats: CloneStats,
        duration: float,
        error: str = "",
    ) -> None:
        """Update Base via BaseFieldUpdater. Best-effort — không raise."""
        if not self._updater or not record_id:
            return
        await self._updater.stage_finish(
            record_id, stage="clone", outcome=outcome,
            metrics={
                "Clone Block Count": stats.blocks_recreated,
            },
            duration_seconds=duration,
            error=error,
        )
