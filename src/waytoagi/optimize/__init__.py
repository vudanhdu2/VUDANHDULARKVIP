"""Optimization layer cho big-doc clone + translate.

Đối phó với case bài có 5000+ blocks → V1 mất 30+ phút và fail-rate 80%.

Chiến lược:
  1. `BatchTranslator`: gom N blocks vào 1 LLM call → giảm 20-50x round-trip,
     mỗi block chỉ tốn ~50-200ms thay vì 2-5s.
  2. `AdaptiveConcurrency`: workers tự scale theo rate-limit signal —
     bắt đầu thấp, tăng dần khi không bị 99991400, giảm ngay khi gặp.
  3. `ContentHashCache` (sống trong waytoagi.cache): skip dịch block
     không đổi giữa các lần re-run.
  4. `StreamingPipeline`: clone batch N+1 song song với translate batch N
     qua asyncio.Queue → overlap I/O, throughput ~2x.
"""

from waytoagi.optimize.adaptive import AdaptiveConcurrency, ConcurrencySignal
from waytoagi.optimize.batch_translate import (
    BatchItem,
    BatchTranslateResult,
    BatchTranslator,
)
from waytoagi.optimize.streaming import StreamingPipeline

__all__ = [
    "AdaptiveConcurrency",
    "BatchItem",
    "BatchTranslateResult",
    "BatchTranslator",
    "ConcurrencySignal",
    "StreamingPipeline",
]
