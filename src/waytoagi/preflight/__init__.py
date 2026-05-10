"""Preflight health checks — verify môi trường trước pipeline run.

Stage 0: chạy < 10s, fail-fast nếu thiếu permission/quota/cache. Tránh
case pipeline chạy 30 phút mới fail vì token expire.
"""

from waytoagi.preflight.check import (
    CheckLevel,
    CheckResult,
    PreflightCheck,
    PreflightReport,
)

__all__ = [
    "CheckLevel",
    "CheckResult",
    "PreflightCheck",
    "PreflightReport",
]
