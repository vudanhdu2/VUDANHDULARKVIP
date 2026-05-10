"""SQLite-backed cache cho translations + media token mapping.

- TranslationCache: SHA-256(text|target) → translated text
- MediaTokenCache: src_file_token → dst_file_token (tránh re-download/re-upload)

Async-friendly via run_in_executor wrapping (sqlite3 không native async).
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import suppress
from typing import TYPE_CHECKING, TypeVar

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

T = TypeVar("T")

logger = structlog.get_logger(__name__)


def _ensure_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


class TranslationCache:
    """SQLite cache cho translation segments."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS translations (
        key TEXT PRIMARY KEY,
        translated TEXT NOT NULL,
        created_at REAL DEFAULT (julianday('now'))
    );
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        _ensure_dir(db_path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._log = logger.bind(component="TranslationCache", db=str(db_path))

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self.db_path), isolation_level=None, check_same_thread=False,
            )
            self._conn.executescript(self.SCHEMA)
        return self._conn

    async def _run(self, fn: Callable[[], T]) -> T:
        return await asyncio.get_running_loop().run_in_executor(None, fn)

    async def get(self, key: str) -> str | None:
        async with self._lock:
            def _q() -> str | None:
                cur = self._connect().execute(
                    "SELECT translated FROM translations WHERE key=?", (key,),
                )
                row = cur.fetchone()
                return None if row is None else str(row[0])
            return await self._run(_q)

    async def put(self, key: str, translated: str) -> None:
        async with self._lock:
            def _ins() -> None:
                self._connect().execute(
                    "INSERT OR REPLACE INTO translations(key, translated) VALUES(?, ?)",
                    (key, translated),
                )
            await self._run(_ins)

    async def put_many(self, items: Iterable[tuple[str, str]]) -> None:
        async with self._lock:
            def _ins_many() -> None:
                self._connect().executemany(
                    "INSERT OR REPLACE INTO translations(key, translated) VALUES(?, ?)",
                    list(items),
                )
            await self._run(_ins_many)

    async def aclose(self) -> None:
        async with self._lock:
            if self._conn is not None:
                with suppress(sqlite3.Error):
                    self._conn.close()
                self._conn = None


class MediaTokenCache:
    """src_file_token (CN tenant) → dst_file_token (Larksuite tenant)."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS media_tokens (
        src_token TEXT PRIMARY KEY,
        dst_token TEXT NOT NULL,
        size INTEGER,
        created_at REAL DEFAULT (julianday('now'))
    );
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        _ensure_dir(db_path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self.db_path), isolation_level=None, check_same_thread=False,
            )
            self._conn.executescript(self.SCHEMA)
        return self._conn

    async def _run(self, fn: Callable[[], T]) -> T:
        return await asyncio.get_running_loop().run_in_executor(None, fn)

    async def get(self, src_token: str) -> str | None:
        async with self._lock:
            def _q() -> str | None:
                cur = self._connect().execute(
                    "SELECT dst_token FROM media_tokens WHERE src_token=?",
                    (src_token,),
                )
                row = cur.fetchone()
                return None if row is None else str(row[0])
            return await self._run(_q)

    async def put(
        self, src_token: str, dst_token: str, *, size: int | None = None,
    ) -> None:
        async with self._lock:
            def _ins() -> None:
                self._connect().execute(
                    "INSERT OR REPLACE INTO media_tokens(src_token, dst_token, size) "
                    "VALUES(?, ?, ?)",
                    (src_token, dst_token, size),
                )
            await self._run(_ins)

    async def aclose(self) -> None:
        async with self._lock:
            if self._conn is not None:
                with suppress(sqlite3.Error):
                    self._conn.close()
                self._conn = None
