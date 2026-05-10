"""Shared pytest fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def fixtures_dir() -> Path:
    """Path to tests/fixtures/ directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def lark_responses_dir(fixtures_dir: Path) -> Path:
    """Path to tests/fixtures/lark_responses/."""
    return fixtures_dir / "lark_responses"


@pytest.fixture
def load_fixture(fixtures_dir: Path):  # type: ignore[no-untyped-def]
    """Helper to load JSON fixture by relative path."""

    def _load(rel_path: str) -> Any:
        full = fixtures_dir / rel_path
        return json.loads(full.read_text(encoding="utf-8"))

    return _load
