"""Unit tests cho LLMPool — round-robin + retry + fallback."""

from __future__ import annotations

import httpx
import pytest
import respx

from waytoagi.config.settings import LLMEndpoint
from waytoagi.llm.pool import LLMPool, LLMPoolError


def _ep(name: str, url: str) -> LLMEndpoint:
    return LLMEndpoint(name=name, endpoint=url, api_key="k", model="gpt-test")


@pytest.mark.unit
class TestPool:
    @pytest.mark.asyncio
    async def test_empty_pool_raises(self) -> None:
        with pytest.raises(LLMPoolError):
            LLMPool([], rate_limit_rps=10)

    @pytest.mark.asyncio
    @respx.mock
    async def test_chat_success(self) -> None:
        respx.post("https://llm-a.example.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={
                "choices": [{"message": {"content": "Xin chào"}}],
            }),
        )
        pool = LLMPool([_ep("a", "https://llm-a.example.com/v1")], rate_limit_rps=50)
        try:
            out = await pool.chat([{"role": "user", "content": "Hi"}])
            assert out == "Xin chào"
        finally:
            await pool.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_failover_to_second_endpoint(self) -> None:
        respx.post("https://llm-a.example.com/v1/chat/completions").mock(
            return_value=httpx.Response(500),
        )
        respx.post("https://llm-b.example.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={
                "choices": [{"message": {"content": "OK"}}],
            }),
        )
        pool = LLMPool(
            [_ep("a", "https://llm-a.example.com/v1"),
             _ep("b", "https://llm-b.example.com/v1")],
            rate_limit_rps=50,
        )
        try:
            out = await pool.chat([{"role": "user", "content": "Hi"}])
            assert out == "OK"
        finally:
            await pool.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_all_endpoints_fail(self) -> None:
        respx.post("https://llm-a.example.com/v1/chat/completions").mock(
            return_value=httpx.Response(500),
        )
        respx.post("https://llm-b.example.com/v1/chat/completions").mock(
            return_value=httpx.Response(500),
        )
        pool = LLMPool(
            [_ep("a", "https://llm-a.example.com/v1"),
             _ep("b", "https://llm-b.example.com/v1")],
            rate_limit_rps=50,
        )
        try:
            with pytest.raises(LLMPoolError):
                await pool.chat([{"role": "user", "content": "Hi"}])
        finally:
            await pool.aclose()
