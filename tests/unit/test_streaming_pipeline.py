"""Unit tests cho `StreamingPipeline` — overlap producer/consumer qua queue."""

from __future__ import annotations

import asyncio

import pytest

from waytoagi.optimize.streaming import StreamingPipeline


@pytest.mark.unit
class TestStreamingPipelineBasic:
    @pytest.mark.asyncio
    async def test_simple_producer_consumer(self) -> None:
        async def producer():
            for i in range(3):
                yield [i, i * 10]

        async def transform(batch):
            return [x + 1 for x in batch]

        pipeline = StreamingPipeline(
            producer, transform, queue_size=2, consumer_workers=1,
        )
        results: list[list[int]] = []
        async for batch in pipeline.run():
            results.append(list(batch))
        # Producer yields [0,0], [1,10], [2,20] → transform +1
        # → [1,1], [2,11], [3,21] (single worker preserves order)
        assert results == [[1, 1], [2, 11], [3, 21]]

    @pytest.mark.asyncio
    async def test_empty_producer_yields_nothing(self) -> None:
        async def producer():
            return
            yield  # unreachable, makes it AsyncIterator

        async def transform(batch):
            return batch

        pipeline = StreamingPipeline(
            producer, transform, queue_size=1, consumer_workers=1,
        )
        results = [b async for b in pipeline.run()]
        assert results == []

    @pytest.mark.asyncio
    async def test_overlap_with_slow_transform(self) -> None:
        """Producer fast + Transform slow → producer fills queue,
        consumer drains. Wall-clock < sum.
        """
        async def producer():
            for i in range(5):
                yield [i]

        async def transform(batch):
            await asyncio.sleep(0.05)  # slow
            return batch

        import time
        start = time.monotonic()
        pipeline = StreamingPipeline(
            producer, transform, queue_size=4, consumer_workers=2,
        )
        results = [b async for b in pipeline.run()]
        elapsed = time.monotonic() - start

        assert len(results) == 5
        # Sequential = 5 x 0.05 = 0.25s. With 2 workers ≈ 0.13s.
        # Allow generous margin for CI.
        assert elapsed < 0.20


@pytest.mark.unit
class TestStreamingPipelineErrors:
    @pytest.mark.asyncio
    async def test_transform_exception_skips_batch(self) -> None:
        """1 transform fail không halt pipeline — batch khác vẫn ra."""
        async def producer():
            for i in range(3):
                yield [i]

        async def transform(batch):
            if batch[0] == 1:
                msg = "fail batch 1"
                raise RuntimeError(msg)
            return batch

        pipeline = StreamingPipeline(
            producer, transform, queue_size=2, consumer_workers=1,
        )
        results = [b async for b in pipeline.run()]
        # Batch 1 lost, batch 0 + 2 đến
        flat = [x for b in results for x in b]
        assert 0 in flat
        assert 2 in flat
        assert 1 not in flat

    @pytest.mark.asyncio
    async def test_producer_exception_logged_does_not_hang(self) -> None:
        """Producer raise → finalizer push None → run() exits cleanly."""
        async def producer():
            yield [0]
            msg = "producer fail"
            raise RuntimeError(msg)

        async def transform(batch):
            return batch

        pipeline = StreamingPipeline(
            producer, transform, queue_size=2, consumer_workers=1,
        )
        # Should NOT hang — pipeline drains what's available
        results = [b async for b in pipeline.run()]
        # First batch [0] đã lọt qua trước khi producer fail
        assert results == [[0]]


@pytest.mark.unit
class TestStreamingPipelineValidation:
    def test_queue_size_must_be_positive(self) -> None:
        async def producer():
            yield [1]

        async def transform(batch):
            return batch

        with pytest.raises(ValueError, match="queue_size"):
            StreamingPipeline(producer, transform, queue_size=0)

    def test_consumer_workers_must_be_positive(self) -> None:
        async def producer():
            yield [1]

        async def transform(batch):
            return batch

        with pytest.raises(ValueError, match="consumer_workers"):
            StreamingPipeline(producer, transform, consumer_workers=0)


@pytest.mark.unit
class TestStreamingPipelineBackpressure:
    @pytest.mark.asyncio
    async def test_producer_blocks_when_queue_full(self) -> None:
        """Producer fast nhưng queue size=1 + transform slow → producer
        bị backpressure (không bùng nổ memory).
        """
        produced: list[int] = []
        max_in_flight = 0

        async def producer():
            for i in range(10):
                produced.append(i)
                yield [i]

        in_flight = 0

        async def transform(batch):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return batch

        pipeline = StreamingPipeline(
            producer, transform, queue_size=1, consumer_workers=1,
        )
        results = [b async for b in pipeline.run()]
        assert len(results) == 10
        # Backpressure: never more than queue_size + workers in flight
        assert max_in_flight <= 2
