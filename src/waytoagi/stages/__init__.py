"""Pipeline stages — orchestration I/O cho mỗi giai đoạn.

Mỗi stage là 1 class với contract:
  - `__init__(...)`: nhận dependencies (clients, settings, logger).
  - `run(...)`: chạy stage, return summary frozen Pydantic model.
  - Stages tuyệt đối KHÔNG share state qua module globals; tất cả phải
    đi qua argument hoặc instance attr.

Stage 7 (TreeOrderStage) reorder dst wiki tree match source CN order.
"""

from waytoagi.stages.clone import CloneResult, CloneStage, CloneStats
from waytoagi.stages.crawl import CrawlStage
from waytoagi.stages.media_handler import MediaCloneResult, MediaHandler
from waytoagi.stages.mirror import MirrorResult, MirrorStage
from waytoagi.stages.placeholder import PlaceholderCreator
from waytoagi.stages.reorder import TreeOrderStage
from waytoagi.stages.sync import SmartSyncStage, SyncOutcome, SyncResult
from waytoagi.stages.translate import (
    TranslateResult,
    TranslateStage,
    TranslateStats,
)

__all__ = [
    "CloneResult",
    "CloneStage",
    "CloneStats",
    "CrawlStage",
    "MediaCloneResult",
    "MediaHandler",
    "MirrorResult",
    "MirrorStage",
    "PlaceholderCreator",
    "SmartSyncStage",
    "SyncOutcome",
    "SyncResult",
    "TranslateResult",
    "TranslateStage",
    "TranslateStats",
    "TreeOrderStage",
]
