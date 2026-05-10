"""Tests cho `AuditTrail` — append-only event log."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from waytoagi.base_schema.audit import (
    AuditEvent,
    AuditOutcome,
    AuditTrail,
)


@pytest.mark.unit
class TestAuditEventSerialize:
    def test_format_basic(self) -> None:
        ev = AuditEvent(
            stage="CLONE",
            outcome=AuditOutcome.OK,
            timestamp=datetime(2026, 5, 10, 7, 30, 12, tzinfo=UTC),
            details="dt=32s blocks=247",
        )
        line = ev.serialize()
        assert line == "2026-05-10T07:30:12Z [CLONE] OK dt=32s blocks=247"

    def test_format_no_details(self) -> None:
        ev = AuditEvent(
            stage="CRAWL",
            outcome=AuditOutcome.SKIP,
            timestamp=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        )
        line = ev.serialize()
        assert line == "2026-01-01T00:00:00Z [CRAWL] SKIP"

    def test_now_uses_current_time(self) -> None:
        ev = AuditEvent.now("CLONE", AuditOutcome.OK, "test")
        # Timestamp recent (< 5s)
        diff = (datetime.now(tz=UTC) - ev.timestamp).total_seconds()
        assert 0 <= diff < 5


@pytest.mark.unit
class TestAuditTrailFromString:
    def test_parse_empty(self) -> None:
        trail = AuditTrail.from_string("")
        assert len(trail) == 0
        assert trail.events == []

    def test_parse_single_line(self) -> None:
        text = "2026-05-10T07:30:12Z [CRAWL] OK n=12876"
        trail = AuditTrail.from_string(text)
        assert len(trail) == 1
        assert trail.events[0].stage == "CRAWL"
        assert trail.events[0].outcome == AuditOutcome.OK
        assert trail.events[0].details == "n=12876"

    def test_parse_multiple_lines(self) -> None:
        text = "\n".join([
            "2026-05-10T07:30:12Z [CRAWL] OK n=12876",
            "2026-05-10T07:35:42Z [CLONE] OK 247 blocks dt=32s",
            "2026-05-10T07:36:34Z [TRANSLATE] OK cache=45% dt=52s",
        ])
        trail = AuditTrail.from_string(text)
        assert len(trail) == 3
        stages = [e.stage for e in trail.events]
        assert stages == ["CRAWL", "CLONE", "TRANSLATE"]

    def test_parse_skips_malformed_lines(self) -> None:
        text = "\n".join([
            "2026-05-10T07:30:12Z [CRAWL] OK ok",
            "this is not valid",
            "2026-05-10T07:35:42Z [CLONE] OK done",
            "",  # blank line
            "garbage",
        ])
        trail = AuditTrail.from_string(text)
        # Chỉ 2 dòng valid
        assert len(trail) == 2

    def test_parse_unknown_outcome_skipped(self) -> None:
        text = "2026-05-10T07:30:12Z [CRAWL] WAT details"
        trail = AuditTrail.from_string(text)
        assert len(trail) == 0


@pytest.mark.unit
class TestAuditTrailAppend:
    def test_append_grows_list(self) -> None:
        trail = AuditTrail()
        trail.append_now("CRAWL", AuditOutcome.OK, "n=1")
        trail.append_now("CLONE", AuditOutcome.OK, "n=2")
        assert len(trail) == 2

    def test_append_truncates_to_max(self) -> None:
        trail = AuditTrail(max_events=3)
        for i in range(5):
            trail.append_now("STAGE", AuditOutcome.OK, f"n={i}")
        assert len(trail) == 3
        # Last 3 events giữ lại
        details = [e.details for e in trail.events]
        assert details == ["n=2", "n=3", "n=4"]


@pytest.mark.unit
class TestAuditTrailSerialize:
    def test_serialize_sorted_by_timestamp(self) -> None:
        trail = AuditTrail()
        # Append theo thứ tự lộn xộn
        ts2 = datetime(2026, 5, 10, 8, 0, 0, tzinfo=UTC)
        ts1 = datetime(2026, 5, 10, 7, 0, 0, tzinfo=UTC)
        trail.append(AuditEvent("LATER", AuditOutcome.OK, ts2, ""))
        trail.append(AuditEvent("EARLIER", AuditOutcome.OK, ts1, ""))

        text = trail.serialize()
        lines = text.split("\n")
        assert lines[0].startswith("2026-05-10T07:00")
        assert lines[1].startswith("2026-05-10T08:00")

    def test_round_trip(self) -> None:
        """Serialize → from_string → giống nhau."""
        trail = AuditTrail()
        trail.append_now("CRAWL", AuditOutcome.OK, "n=12876")
        trail.append_now("CLONE", AuditOutcome.FAIL, "err=131006")

        text = trail.serialize()
        restored = AuditTrail.from_string(text)
        assert len(restored) == 2
        assert restored.events[0].stage in {"CRAWL", "CLONE"}
        assert restored.events[1].stage in {"CRAWL", "CLONE"}


@pytest.mark.unit
class TestAuditTrailLastEvent:
    def test_last_event_overall(self) -> None:
        trail = AuditTrail()
        trail.append_now("CRAWL", AuditOutcome.OK)
        trail.append_now("CLONE", AuditOutcome.OK)
        trail.append_now("TRANSLATE", AuditOutcome.OK)
        last = trail.last_event()
        assert last is not None
        assert last.stage == "TRANSLATE"

    def test_last_event_by_stage(self) -> None:
        trail = AuditTrail()
        trail.append_now("CLONE", AuditOutcome.OK, "first")
        trail.append_now("TRANSLATE", AuditOutcome.OK)
        trail.append_now("CLONE", AuditOutcome.FAIL, "retry")
        last_clone = trail.last_event(stage="CLONE")
        assert last_clone is not None
        assert last_clone.details == "retry"

    def test_last_event_no_match(self) -> None:
        trail = AuditTrail()
        trail.append_now("CLONE", AuditOutcome.OK)
        assert trail.last_event(stage="NONEXISTENT") is None

    def test_last_event_empty_trail(self) -> None:
        trail = AuditTrail()
        assert trail.last_event() is None
