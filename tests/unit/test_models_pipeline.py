"""Unit tests cho waytoagi.models.pipeline."""

from __future__ import annotations

from datetime import datetime

import pytest

from waytoagi.models.pipeline import (
    PipelineCounters,
    PipelineStage,
    StageOutcome,
    StageResult,
)


class TestPipelineCounters:
    def test_empty_counters(self) -> None:
        c = PipelineCounters()
        assert c.total == 0
        assert c.success_rate == 0.0

    def test_update_outcome(self) -> None:
        c = PipelineCounters()
        c.update(StageOutcome.OK)
        c.update(StageOutcome.OK)
        c.update(StageOutcome.SKIP)
        c.update(StageOutcome.FAIL_PERMANENT)
        assert c.ok == 2
        assert c.skip == 1
        assert c.fail_permanent == 1
        assert c.total == 4
        assert c.success_rate == 0.5

    @pytest.mark.parametrize(
        ("ok", "fail", "expected_rate"),
        [
            (10, 0, 1.0),
            (5, 5, 0.5),
            (0, 10, 0.0),
            (0, 0, 0.0),
        ],
    )
    def test_success_rate(self, ok: int, fail: int, *, expected_rate: float) -> None:
        c = PipelineCounters(ok=ok, fail_permanent=fail)
        assert c.success_rate == pytest.approx(expected_rate)


class TestStageResult:
    def test_construct(self) -> None:
        now = datetime.now()
        r = StageResult(
            stage=PipelineStage.CLONE,
            record_id="rec123",
            stt=100,
            outcome=StageOutcome.OK,
            started_at=now,
            completed_at=now,
            duration_seconds=1.5,
        )
        assert r.stage == PipelineStage.CLONE
        assert r.outcome == StageOutcome.OK
        assert r.duration_seconds == 1.5
        assert r.error_message == ""

    def test_negative_duration_rejected(self) -> None:
        now = datetime.now()
        with pytest.raises(ValueError, match="greater than or equal"):
            StageResult(
                stage=PipelineStage.CLONE,
                record_id="rec123",
                outcome=StageOutcome.OK,
                started_at=now,
                completed_at=now,
                duration_seconds=-1.0,
            )
