"""AuditTrail — append-only event log per record, trong field text Lark Base.

Format mỗi event 1 dòng:
    YYYY-MM-DDTHH:MM:SSZ [STAGE] OUTCOME details

Ví dụ:
    2026-05-10T07:30:12Z [CRAWL] OK detected new
    2026-05-10T07:30:18Z [PLACEHOLDER] OK dst=Lm7hwND...
    2026-05-10T07:35:42Z [CLONE] OK 247 blocks dt=32s
    2026-05-10T07:36:34Z [TRANSLATE] OK cache_hit=45% dt=52s
    2026-05-10T07:37:12Z [SYNC] OK replaced=3 kept=244 saved=244

Constraints:
  - **Append-only**: caller chỉ append, KHÔNG sửa event cũ.
  - **Truncate cuối**: giữ last N events (default 30) để tránh phình
    field text quá kích thước Lark (giới hạn ~64KB).
  - **Idempotent serialize**: cùng input → cùng output (sort, format ổn).

Pure module — không I/O. Caller (BaseFieldUpdater) merge với existing
trail field rồi gọi update_record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar


class AuditOutcome(StrEnum):
    """Outcome của 1 event."""

    OK = "OK"
    SKIP = "SKIP"
    FAIL = "FAIL"
    RETRY = "RETRY"
    INFO = "INFO"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """1 event trong audit trail."""

    stage: str
    """Tên stage (CRAWL, PLACEHOLDER, CLONE, TRANSLATE, MIRROR, SYNC, …)"""

    outcome: AuditOutcome
    timestamp: datetime
    details: str = ""
    """Free-form short details — duration, count, error msg…"""

    def serialize(self) -> str:
        """Format thành 1 dòng plaintext."""
        ts = self.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"{ts} [{self.stage}] {self.outcome.value} {self.details}".rstrip()

    @classmethod
    def now(
        cls,
        stage: str,
        outcome: AuditOutcome = AuditOutcome.OK,
        details: str = "",
    ) -> AuditEvent:
        """Convenience — create event với timestamp = now (UTC)."""
        return cls(
            stage=stage,
            outcome=outcome,
            timestamp=datetime.now(tz=UTC),
            details=details,
        )


@dataclass(slots=True)
class AuditTrail:
    """Manage list events cho 1 record.

    Args:
        existing: trail string đã đọc từ Lark Base (multi-line).
        max_events: giữ tối đa N events gần nhất.

    Usage:
        trail = AuditTrail.from_string(record.audit_trail, max_events=30)
        trail.append(AuditEvent.now("CRAWL", AuditOutcome.OK, "n=12876"))
        new_text = trail.serialize()
        await base.update_record(..., {"Audit Trail": new_text})
    """

    DEFAULT_MAX_EVENTS: ClassVar[int] = 30

    events: list[AuditEvent] = field(default_factory=list)
    max_events: int = DEFAULT_MAX_EVENTS

    @classmethod
    def from_string(
        cls,
        text: str,
        *,
        max_events: int = DEFAULT_MAX_EVENTS,
    ) -> AuditTrail:
        """Parse trail text → AuditTrail. Best-effort: skip dòng lỗi format.

        Format mỗi dòng:
            YYYY-MM-DDTHH:MM:SSZ [STAGE] OUTCOME details
        """
        events: list[AuditEvent] = []
        if not text:
            return cls(events=events, max_events=max_events)

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            ev = _parse_line(line)
            if ev is not None:
                events.append(ev)
        return cls(events=events, max_events=max_events)

    def append(self, event: AuditEvent) -> None:
        """Append event. Tự truncate giữ last `max_events`."""
        self.events.append(event)
        if len(self.events) > self.max_events:
            self.events = self.events[-self.max_events:]

    def append_now(
        self,
        stage: str,
        outcome: AuditOutcome = AuditOutcome.OK,
        details: str = "",
    ) -> None:
        """Convenience — append event với timestamp = now."""
        self.append(AuditEvent.now(stage, outcome, details))

    def serialize(self) -> str:
        """Multi-line text — 1 event/line, sorted theo timestamp ASC."""
        sorted_events = sorted(self.events, key=lambda e: e.timestamp)
        return "\n".join(e.serialize() for e in sorted_events)

    def last_event(self, stage: str | None = None) -> AuditEvent | None:
        """Return event mới nhất (optional filter theo stage)."""
        if not self.events:
            return None
        if stage is None:
            return max(self.events, key=lambda e: e.timestamp)
        candidates = [e for e in self.events if e.stage == stage]
        return max(candidates, key=lambda e: e.timestamp) if candidates else None

    def __len__(self) -> int:
        return len(self.events)


# ============================================================================
# Parser
# ============================================================================


def _parse_line(line: str) -> AuditEvent | None:
    """Parse 1 dòng audit trail. Trả None nếu format lệch."""
    # Expected: "YYYY-MM-DDTHH:MM:SSZ [STAGE] OUTCOME details..."
    # Tách: timestamp_str, "[STAGE]", outcome, rest
    try:
        # Split first 2 spaces → ts, "[STAGE]", rest
        parts = line.split(" ", 2)
        if len(parts) < 3:
            return None
        ts_str = parts[0]
        stage_token = parts[1]
        rest = parts[2]

        # Parse timestamp
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC,
            )
        except ValueError:
            return None

        # Parse [STAGE]
        if not (stage_token.startswith("[") and stage_token.endswith("]")):
            return None
        stage = stage_token[1:-1]
        if not stage:
            return None

        # Parse OUTCOME details — split first space
        outcome_parts = rest.split(" ", 1)
        outcome_str = outcome_parts[0]
        details = outcome_parts[1] if len(outcome_parts) > 1 else ""

        try:
            outcome = AuditOutcome(outcome_str)
        except ValueError:
            return None

        return AuditEvent(
            stage=stage,
            outcome=outcome,
            timestamp=ts,
            details=details,
        )
    except Exception:
        return None
