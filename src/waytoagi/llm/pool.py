"""LLM POOL — async round-robin client cho nhiều OpenAI-compatible endpoints.

Mỗi endpoint: {endpoint, api_key, model, name}. Pool chia rate limit GLOBAL bằng
aiolimiter + per-endpoint counter để tránh hammer 1 endpoint khi N >> rate.
"""

from __future__ import annotations

import asyncio
from itertools import cycle
from typing import TYPE_CHECKING, Any

import httpx
import structlog
from aiolimiter import AsyncLimiter
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from waytoagi.config.settings import LLMEndpoint

logger = structlog.get_logger(__name__)


class LLMPoolError(Exception):
    """Raised khi tất cả endpoint đều fail."""


class LLMPool:
    """Round-robin pool cho OpenAI-compatible /chat/completions endpoints.

    Usage:
        pool = LLMPool(settings.llm_endpoints, rate_limit_rps=10)
        try:
            text = await pool.chat([{"role": "user", "content": "Hi"}])
        finally:
            await pool.aclose()
    """

    def __init__(
        self,
        endpoints: Sequence[LLMEndpoint],
        *,
        rate_limit_rps: int = 10,
        timeout: float = 60.0,
    ) -> None:
        if not endpoints:
            raise LLMPoolError("LLM pool is empty — provide at least 1 endpoint")
        self._endpoints: list[LLMEndpoint] = list(endpoints)
        self._cycle: Iterator[LLMEndpoint] = cycle(self._endpoints)
        self._cycle_lock = asyncio.Lock()
        self._limiter = AsyncLimiter(rate_limit_rps, 1.0)
        self._client = httpx.AsyncClient(timeout=timeout)
        self._log = logger.bind(component="LLMPool", endpoints=len(self._endpoints))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> LLMPool:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def _next_endpoint(self) -> LLMEndpoint:
        async with self._cycle_lock:
            return next(self._cycle)

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """Gửi 1 request /chat/completions, return assistant content.

        Retry trong cùng endpoint 3 lần (exponential), sau đó rotate endpoint.
        Thử tối đa N_endpoints * 3 attempts trước khi raise LLMPoolError.
        """
        last_err: Exception | None = None
        for _ in range(len(self._endpoints)):
            ep = await self._next_endpoint()
            try:
                async for attempt in AsyncRetrying(
                    stop=stop_after_attempt(3),
                    wait=wait_exponential(multiplier=1, min=1, max=8),
                    retry=retry_if_exception_type(httpx.HTTPError),
                    reraise=True,
                ):
                    with attempt:
                        return await self._call_one(
                            ep, messages,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            response_format=response_format,
                        )
            except Exception as e:
                last_err = e
                self._log.warning("llm_endpoint_failed", name=ep.name, err=str(e))
                continue
        raise LLMPoolError(f"all endpoints failed; last={last_err}")

    async def _call_one(
        self,
        ep: LLMEndpoint,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int | None,
        response_format: dict[str, Any] | None,
    ) -> str:
        body: dict[str, Any] = {
            "model": ep.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if response_format is not None:
            body["response_format"] = response_format

        async with self._limiter:
            r = await self._client.post(
                f"{ep.endpoint.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {ep.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        r.raise_for_status()
        data = r.json()
        return str(data["choices"][0]["message"]["content"])
