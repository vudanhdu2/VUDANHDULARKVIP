"""Pipeline state schemas — counters, stage results, audit entries."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # Pydantic needs runtime type
from enum import StrEnum

from pydantic import BaseModel, Field


class PipelineStage(StrEnum):
    """Pipeline stages."""

    CRAWL = "crawl"
    CLONE = "clone"
    TRANSLATE = "translate"
    MIRROR = "mirror"
    SYNC = "sync"
    AUDIT = "audit"


class StageOutcome(StrEnum):
    """Outcome của 1 stage cho 1 record."""

    OK = "ok"
    SKIP = "skip"
    FAIL_TRANSIENT = "fail_transient"
    FAIL_PERMANENT = "fail_permanent"
    SOURCE_DELETED = "source_deleted"
    UNSUPPORTED_TYPE = "unsupported_type"
    PERM_DENIED = "perm_denied"


class StageResult(BaseModel):
    """Kết quả của 1 stage cho 1 record."""

    stage: PipelineStage
    record_id: str
    stt: int | None = None
    outcome: StageOutcome
    started_at: datetime
    completed_at: datetime
    duration_seconds: float = Field(ge=0)
    error_message: str = ""
    error_code: int | None = None
    detail: dict[str, str | int | float | bool] = Field(default_factory=dict)


class PipelineCounters(BaseModel):
    """Counters tổng hợp cho 1 pipeline run."""

    ok: int = 0
    skip: int = 0
    fail_transient: int = 0
    fail_permanent: int = 0
    source_deleted: int = 0
    unsupported_type: int = 0
    perm_denied: int = 0
    uncaught: int = 0

    @property
    def total(self) -> int:
        return (
            self.ok + self.skip + self.fail_transient + self.fail_permanent
            + self.source_deleted + self.unsupported_type + self.perm_denied
            + self.uncaught
        )

    @property
    def success_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.ok / self.total

    def update(self, outcome: StageOutcome) -> None:
        """Increment counter cho outcome."""
        attr = outcome.value
        current = getattr(self, attr, 0)
        setattr(self, attr, current + 1)


class AuditEntry(BaseModel):
    """1 entry trong audit trail — log mỗi state transition."""

    timestamp: datetime
    correlation_id: str
    stage: PipelineStage
    record_id: str
    stt: int | None = None
    action: str = Field(..., description="vd 'set_status_done', 'update_field'")
    before: dict[str, str | int | float | bool] = Field(default_factory=dict)
    after: dict[str, str | int | float | bool] = Field(default_factory=dict)
    actor: str = "pipeline"
