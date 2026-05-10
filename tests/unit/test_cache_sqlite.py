"""Unit tests cho TranslationCache + MediaTokenCache."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from waytoagi.cache.sqlite import MediaTokenCache, TranslationCache

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.unit
@pytest.mark.asyncio
class TestTranslationCache:
    async def test_get_miss_returns_none(self, tmp_path: Path) -> None:
        c = TranslationCache(tmp_path / "t.sqlite")
        try:
            assert await c.get("nope") is None
        finally:
            await c.aclose()

    async def test_put_and_get(self, tmp_path: Path) -> None:
        c = TranslationCache(tmp_path / "t.sqlite")
        try:
            await c.put("k1", "Xin chào")
            assert await c.get("k1") == "Xin chào"
        finally:
            await c.aclose()

    async def test_put_overrides(self, tmp_path: Path) -> None:
        c = TranslationCache(tmp_path / "t.sqlite")
        try:
            await c.put("k1", "v1")
            await c.put("k1", "v2")
            assert await c.get("k1") == "v2"
        finally:
            await c.aclose()

    async def test_put_many(self, tmp_path: Path) -> None:
        c = TranslationCache(tmp_path / "t.sqlite")
        try:
            await c.put_many([("a", "1"), ("b", "2"), ("c", "3")])
            assert await c.get("b") == "2"
        finally:
            await c.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
class TestMediaTokenCache:
    async def test_round_trip(self, tmp_path: Path) -> None:
        c = MediaTokenCache(tmp_path / "m.sqlite")
        try:
            await c.put("src-1", "dst-1", size=12345)
            assert await c.get("src-1") == "dst-1"
            assert await c.get("missing") is None
        finally:
            await c.aclose()
