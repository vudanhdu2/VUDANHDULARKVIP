"""StreamingPipeline — overlap clone + translate qua asyncio.Queue.

V1 problem: clone xong toàn bộ doc rồi mới translate → wall-clock =
clone_time + translate_time. Doc 5000 blocks: ~10 phút clone + ~30 phút
translate = ~40 phút tổng.

V2 solution: producer-consumer overlap:
  - Producer: clone từng batch blocks → push vào queue.
  - Consumer: pop batch → translate → push xuống stage tiếp theo.
  - Wall-clock ≈ max(clone_time, translate_time) thay vì tổng.

Use case chính: trong StageClone + StageTranslate orchestrator chạy
song song qua queue chung. StreamingPipeline đóng gói pattern này
để tránh viết lại boilerplate trong mỗi stage.

Generic over input/output type — không lock vào Block schema cụ thể.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Generic, TypeVar

import structlog

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

logger = structlog.get_logger(__name__)

TIn = TypeVar("TIn")
TOut = TypeVar("TOut")


class StreamingPipeline(Generic[TIn, TOut]):
    """2-stage overlap pipeline: produce → transform.

    Args:
        producer: async callable trả AsyncIterator[batch[TIn]]. Mỗi
            batch là 1 list items.
        transform: async callable nhận batch[TIn] → batch[TOut].
        queue_size: max batches buffered giữa producer và consumer.
            Backpressure: producer block khi queue đầy.
        consumer_workers: số worker song song xử lý transform.

    Usage:
        async def producer():
            async for batch in clone_stage.stream_batches():
                yield batch

        async def transform(batch):
            return await translate_stage.translate_batch(batch)

        pipeline = StreamingPipeline(producer, transform, queue_size=4)
        async for translated_batch in pipeline.run():
            await base_writer.write(translated_batch)
    """

    def __init__(
        self,
        producer: Callable[[], AsyncIterator[Sequence[TIn]]],
        transform: Callable[[Sequence[TIn]], Awaitable[Sequence[TOut]]],
        *,
        queue_size: int = 4,
        consumer_workers: int = 2,
    ) -> None:
        if queue_size < 1:
            msg = "queue_size phải >= 1"
            raise ValueError(msg)
        if consumer_workers < 1:
            msg = "consumer_workers phải >= 1"
            raise ValueError(msg)
        self._producer = producer
        self._transform = transform
        self._queue_size = queue_size
        self._workers = consumer_workers
        self._log = logger.bind(component="StreamingPipeline")

    async def run(self) -> AsyncIterator[Sequence[TOut]]:
        """Run pipeline, yield kết quả transform theo thứ tự FIFO.

        Producer chạy trong 1 task, N consumer chạy parallel. Output
        queue đảm bảo ordering = thứ tự consumer hoàn thành (KHÔNG
        đảm bảo cùng thứ tự producer push — nếu cần ordering chặt thì
        dùng `consumer_workers=1`).
        """
        in_queue: asyncio.Queue[Sequence[TIn] | None] = asyncio.Queue(
            maxsize=self._queue_size,
        )
        out_queue: asyncio.Queue[Sequence[TOut] | None] = asyncio.Queue(
            maxsize=self._queue_size,
        )

        # 1 sentinel (None) per consumer để báo done
        sentinel_count = self._workers

        producer_task = asyncio.create_task(
            self._producer_loop(in_queue, sentinel_count),
        )
        consumer_tasks = [
            asyncio.create_task(self._consumer_loop(in_queue, out_queue, i))
            for i in range(self._workers)
        ]
        finalizer_task = asyncio.create_task(
            self._finalizer(consumer_tasks, out_queue),
        )

        try:
            while True:
                item = await out_queue.get()
                if item is None:
                    break
                yield item
        finally:
            # Đảm bảo cleanup
            producer_task.cancel()
            for c in consumer_tasks:
                c.cancel()
            finalizer_task.cancel()
            await asyncio.gather(
                producer_task, *consumer_tasks, finalizer_task,
                return_exceptions=True,
            )

    async def _producer_loop(
        self,
        in_queue: asyncio.Queue[Sequence[TIn] | None],
        sentinel_count: int,
    ) -> None:
        """Pull từ producer, push vào in_queue, kết thúc bằng sentinels."""
        try:
            async for batch in self._producer():
                await in_queue.put(batch)
        except Exception as e:
            self._log.exception("producer_failed", err=str(e))
        finally:
            for _ in range(sentinel_count):
                await in_queue.put(None)

    async def _consumer_loop(
        self,
        in_queue: asyncio.Queue[Sequence[TIn] | None],
        out_queue: asyncio.Queue[Sequence[TOut] | None],
        worker_id: int,
    ) -> None:
        """Pull từ in_queue, transform, push vào out_queue."""
        log = self._log.bind(worker=worker_id)
        while True:
            batch = await in_queue.get()
            if batch is None:  # sentinel
                return
            try:
                result = await self._transform(batch)
                await out_queue.put(result)
            except Exception as e:
                log.exception("consumer_transform_failed", err=str(e))
                # Không halt pipeline — caller sẽ thấy thiếu batch và
                # quyết định có retry hay không.

    async def _finalizer(
        self,
        consumer_tasks: list[asyncio.Task[None]],
        out_queue: asyncio.Queue[Sequence[TOut] | None],
    ) -> None:
        """Đợi tất cả consumers xong → push None vào out_queue."""
        await asyncio.gather(*consumer_tasks, return_exceptions=True)
        await out_queue.put(None)
