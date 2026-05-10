"""Resilience layer — chống các tình huống production.

6 module:
  1. `ErrorClassifier`: phân loại errors (rate-limit, network, auth, quota,
     lock) → routing decision.
  2. `CircuitBreaker`: 3-state (CLOSED/OPEN/HALF_OPEN) per-resource.
  3. `QuotaTracker`: sliding window per resource, predict + throttle.
  4. `PersistentQueue`: SQLite-backed durable queue, recover sau crash.
  5. `GracefulShutdown`: SIGTERM/SIGINT handler, flush cleanup.
  6. `RetryPolicy`: centralized retry strategy theo error category.

Cover:
  - Lark API rate-limit (99991400, 230001, 1254606)
  - Network failures (DNS, timeout, connection reset)
  - Process crash mid-write (mất điện, OOM)
  - LLM API rate-limit + quota exhaustion
  - Token expire mid-request
  - Disk full, cache eviction
  - Idempotent retry với operation keys
"""

from waytoagi.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
)
from waytoagi.resilience.error_classifier import (
    ErrorCategory,
    classify_error,
    classify_lark_code,
    is_retryable,
    recommended_backoff_seconds,
)
from waytoagi.resilience.persistent_queue import (
    OperationStatus,
    PendingOperation,
    PersistentQueue,
)
from waytoagi.resilience.quota_tracker import (
    QuotaResource,
    QuotaTracker,
    QuotaUsage,
)
from waytoagi.resilience.retry_policy import RetryPolicy, retry_with_policy
from waytoagi.resilience.shutdown import GracefulShutdown, ShutdownPhase

__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
    "ErrorCategory",
    "GracefulShutdown",
    "OperationStatus",
    "PendingOperation",
    "PersistentQueue",
    "QuotaResource",
    "QuotaTracker",
    "QuotaUsage",
    "RetryPolicy",
    "ShutdownPhase",
    "classify_error",
    "classify_lark_code",
    "is_retryable",
    "recommended_backoff_seconds",
    "retry_with_policy",
]
