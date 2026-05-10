"""Click CLI — entry point cho user.

Commands:
  - waytoagi setup            — wizard cài đặt lần đầu (đã có ở root setup_new_project.py)
  - waytoagi preflight        — kiểm tra sức khoẻ trước run
  - waytoagi schema-migrate   — tạo missing fields trong Lark Base
  - waytoagi crawl            — quét nguồn CN + tạo placeholder
  - waytoagi pipeline         — clone + translate cho records pending
  - waytoagi mirror           — fill VI content vào DST placeholder
  - waytoagi sync             — sync block-level diff cho records edited
  - waytoagi reorder          — fix tree order DST match source
  - waytoagi orchestrate      — chạy toàn bộ pipeline tự động
  - waytoagi status           — xem tiến độ realtime
  - waytoagi audit            — audit chất lượng UI/translation/tree
  - waytoagi resume           — resume từ crawl checkpoint cũ

Mọi command đều hỗ trợ:
  --dry-run    chạy thử không ghi xuống Lark
  --workers N  số records song song
  --verbose    log chi tiết
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import click
import structlog

from waytoagi.logging import configure_logging

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

logger = structlog.get_logger(__name__)


# ============================================================================
# Common options decorator
# ============================================================================
def common_options(fn: Callable[..., None]) -> Callable[..., None]:
    """Decorator gắn các options chung cho mọi command."""
    fn = click.option(
        "--dry-run", is_flag=True, default=False,
        help="Chạy thử không ghi xuống Lark",
    )(fn)
    fn = click.option(
        "--workers", type=int, default=4,
        help="Số records xử lý song song (default 4)",
    )(fn)
    fn = click.option(
        "--verbose", "-v", is_flag=True, default=False,
        help="Log chi tiết (DEBUG level)",
    )(fn)
    fn = click.option(
        "--config", type=click.Path(dir_okay=False),
        default=".env",
        help="Đường dẫn .env file (default .env). Không cần tồn tại lúc dry-run.",
    )(fn)
    return fn


# ============================================================================
# Async runner helper
# ============================================================================
def _run_async(coro: Coroutine[None, None, None]) -> None:
    """Run async coroutine với asyncio.run + handle KeyboardInterrupt."""
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        click.echo("\n[!] Đã nhận Ctrl+C, đang shutdown...", err=True)


def _setup_logging(verbose: bool) -> None:
    """Configure structlog level."""
    configure_logging(level="DEBUG" if verbose else "INFO")


# ============================================================================
# Root group
# ============================================================================


@click.group()
@click.version_option(version="2.0.0", prog_name="waytoagi")
def cli() -> None:
    """WaytoAGI — clone + dịch wiki CN sang Larksuite tiếng Việt.

    Mỗi command hỗ trợ --help để xem chi tiết options.
    """


# ============================================================================
# Commands
# ============================================================================


@cli.command()
@common_options
def preflight(
    config: str, workers: int, verbose: bool, dry_run: bool,
) -> None:
    """Kiểm tra sức khoẻ trước run (token, schema, LLM, quotas)."""
    _setup_logging(verbose)
    click.echo("[Preflight] Đang kiểm tra...")
    _run_async(_run_preflight(config))


async def _run_preflight(config: str) -> None:
    """Run PreflightCheck.run_all() và in báo cáo."""
    # Import lazy để startup CLI nhanh
    click.echo(f"  [.] Loading config từ {config}")
    click.echo("  [✓] Phần này cần wire-up với settings + Lark clients")
    click.echo("  [i] Xem `waytoagi.preflight.PreflightCheck` để chi tiết")


@cli.command(name="schema-migrate")
@common_options
def schema_migrate(
    config: str, workers: int, verbose: bool, dry_run: bool,
) -> None:
    """Tạo missing fields trong Lark Base table theo schema V2."""
    _setup_logging(verbose)
    mode = "[DRY RUN] " if dry_run else ""
    click.echo(f"{mode}[Migrate] Đang probe + diff schema...")
    _run_async(_run_schema_migrate(config, dry_run=dry_run))


async def _run_schema_migrate(config: str, *, dry_run: bool) -> None:
    """Run SchemaMigration."""
    click.echo(f"  [.] Loading config từ {config}")
    if dry_run:
        click.echo("  [i] Dry-run: sẽ không tạo field thực sự")
    click.echo("  [✓] Cần wire-up với settings → SchemaMigration.apply()")


@cli.command()
@common_options
def crawl(
    config: str, workers: int, verbose: bool, dry_run: bool,
) -> None:
    """Quét nguồn CN + tạo dst placeholder cho records mới."""
    _setup_logging(verbose)
    mode = "[DRY RUN] " if dry_run else ""
    click.echo(f"{mode}[Crawl] Bắt đầu...")
    _run_async(_run_crawl(config, workers=workers, dry_run=dry_run))


async def _run_crawl(
    config: str, *, workers: int, dry_run: bool,
) -> None:
    click.echo(f"  [.] Workers: {workers}")
    click.echo(f"  [.] Loading config từ {config}")
    click.echo("  [✓] Cần wire-up: CrawlStage(src_wiki, base, placeholder)")


@cli.command()
@common_options
@click.option(
    "--max-records", type=int, default=0,
    help="Limit số records xử lý (0 = unlimited)",
)
def pipeline(
    config: str, workers: int, verbose: bool, dry_run: bool,
    max_records: int,
) -> None:
    """Clone + translate cho records pending."""
    _setup_logging(verbose)
    mode = "[DRY RUN] " if dry_run else ""
    click.echo(f"{mode}[Pipeline] Đang xử lý records pending...")
    _run_async(_run_pipeline(
        config, workers=workers, dry_run=dry_run, max_records=max_records,
    ))


async def _run_pipeline(
    config: str, *, workers: int, dry_run: bool, max_records: int,
) -> None:
    click.echo(f"  [.] Workers: {workers}, max: {max_records or 'unlimited'}")
    click.echo("  [✓] Cần wire-up: PipelineOrchestrator.process_records()")


@cli.command()
@common_options
def mirror(
    config: str, workers: int, verbose: bool, dry_run: bool,
) -> None:
    """Fill VI content vào DST placeholder cho records mirror=Pending."""
    _setup_logging(verbose)
    mode = "[DRY RUN] " if dry_run else ""
    click.echo(f"{mode}[Mirror] Đang fill DST...")
    _run_async(_run_mirror(config, workers=workers, dry_run=dry_run))


async def _run_mirror(
    config: str, *, workers: int, dry_run: bool,
) -> None:
    click.echo(f"  [.] Workers: {workers}")
    click.echo("  [✓] Cần wire-up: MirrorStage cho mọi record needs_mirror")


@cli.command()
@common_options
def sync(
    config: str, workers: int, verbose: bool, dry_run: bool,
) -> None:
    """Sync block-level diff cho records Change Status='edited'."""
    _setup_logging(verbose)
    mode = "[DRY RUN] " if dry_run else ""
    click.echo(f"{mode}[Sync] Đang sync edited records...")
    _run_async(_run_sync(config, workers=workers, dry_run=dry_run))


async def _run_sync(
    config: str, *, workers: int, dry_run: bool,
) -> None:
    click.echo(f"  [.] Workers: {workers}")
    click.echo("  [✓] Cần wire-up: SmartSyncStage")


@cli.command()
@common_options
@click.option(
    "--max-children", type=int, default=50,
    help="Skip parents có > N children (tránh disrupt subtree lớn)",
)
def reorder(
    config: str, workers: int, verbose: bool, dry_run: bool,
    max_children: int,
) -> None:
    """Fix tree order DST match source CN."""
    _setup_logging(verbose)
    mode = "[DRY RUN] " if dry_run else ""
    click.echo(f"{mode}[Reorder] max_children={max_children}")
    _run_async(_run_reorder(
        config, dry_run=dry_run, max_children=max_children,
    ))


async def _run_reorder(
    config: str, *, dry_run: bool, max_children: int,
) -> None:
    click.echo(f"  [.] max_children: {max_children}, dry-run: {dry_run}")
    click.echo("  [✓] Cần wire-up: TreeOrderStage")


@cli.command()
@common_options
def orchestrate(
    config: str, workers: int, verbose: bool, dry_run: bool,
) -> None:
    """Chạy toàn bộ pipeline tự động: preflight → crawl → pipeline → mirror → sync → reorder."""
    _setup_logging(verbose)
    mode = "[DRY RUN] " if dry_run else ""
    click.echo(f"{mode}[Orchestrate] Bắt đầu full pipeline...")
    _run_async(_run_orchestrate(
        config, workers=workers, dry_run=dry_run,
    ))


async def _run_orchestrate(
    config: str, *, workers: int, dry_run: bool,
) -> None:
    click.echo("  [1/6] Preflight checks...")
    click.echo("  [2/6] Crawl + placeholder...")
    click.echo("  [3/6] Clone + translate (pipeline)...")
    click.echo("  [4/6] Mirror VI → DST...")
    click.echo("  [5/6] Sync edited records...")
    click.echo("  [6/6] Reorder tree...")
    click.echo("  [✓] Cần wire-up từng stage qua PipelineOrchestrator")


@cli.command()
@common_options
@click.option(
    "--watch", is_flag=True, default=False,
    help="Tự refresh mỗi 5s",
)
def status(
    config: str, workers: int, verbose: bool, dry_run: bool, watch: bool,
) -> None:
    """Xem tiến độ pipeline (records by status)."""
    _setup_logging(verbose)
    if watch:
        click.echo("[Status] --watch mode (Ctrl+C để thoát)")
    _run_async(_run_status(config, watch=watch))


async def _run_status(config: str, *, watch: bool) -> None:
    click.echo("  [✓] Cần wire-up: query Lark Base + group by Pipeline Stage")


@cli.command()
@common_options
@click.argument(
    "kind",
    type=click.Choice(["ui", "translation", "tree", "all"]),
    default="all",
)
def audit(
    config: str, workers: int, verbose: bool, dry_run: bool, kind: str,
) -> None:
    """Audit chất lượng (ui/translation/tree)."""
    _setup_logging(verbose)
    click.echo(f"[Audit] kind={kind}")
    _run_async(_run_audit(config, kind=kind))


async def _run_audit(config: str, *, kind: str) -> None:
    click.echo(f"  [.] Audit {kind} content...")
    click.echo("  [✓] Cần wire-up: per-kind audit logic")


@cli.command()
@common_options
def resume(
    config: str, workers: int, verbose: bool, dry_run: bool,
) -> None:
    """Resume crawl từ checkpoint cũ (sau crash/mất điện)."""
    _setup_logging(verbose)
    click.echo("[Resume] Đang tìm checkpoint cũ...")
    _run_async(_run_resume(config))


async def _run_resume(config: str) -> None:
    click.echo("  [✓] Cần wire-up: CrawlCheckpointStore.find_resumable()")


# ============================================================================
# Entry point
# ============================================================================


def main() -> None:
    """Entry point cho `python -m waytoagi.cli`."""
    cli()


if __name__ == "__main__":
    main()
