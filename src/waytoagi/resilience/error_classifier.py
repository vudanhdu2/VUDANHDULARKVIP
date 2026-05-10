"""ErrorClassifier — phân loại errors thành category để routing decision.

Mỗi exception/error_code → 1 ErrorCategory. Caller dựa vào category để
quyết định: retry, route khác, fail-fast, hay alert.

Nguyên tắc:
  - **Pure function** không I/O, deterministic.
  - **Lark API codes** mapping cứng theo Lark docs.
  - **HTTP errors** mapping qua status code + exception type.
  - **Network errors** detect qua substring matching trong message
    (workable cross-libs httpx/aiohttp).

Categories:
  - TRANSIENT_RATE_LIMIT: 99991400, 230001 — backoff exponential
  - TRANSIENT_NETWORK: timeout, DNS, connection reset — retry quick
  - TRANSIENT_LOCK: 131009, 1254606 — backoff longer
  - TRANSIENT_QUOTA: HTTP 429, LLM quota exhausted — backoff dài + route
  - PERMANENT_AUTH: 99991663, token expired — refresh hoặc fail
  - PERMANENT_NOT_FOUND: 131005, 404 — không retry, mark deleted
  - PERMANENT_PERM_DENIED: 131006, 403 — không retry
  - PERMANENT_SCHEMA: 1254045, schema mismatch — fix code, không retry
  - UNKNOWN: catch-all, default treat as transient
"""

from __future__ import annotations

from enum import StrEnum

from waytoagi.lark.auth import LarkAPIError


class ErrorCategory(StrEnum):
    """Category cho routing decision."""

    TRANSIENT_RATE_LIMIT = "transient_rate_limit"
    """API trả rate-limit (Lark 99991400/230001, HTTP 429)."""

    TRANSIENT_NETWORK = "transient_network"
    """Connection/DNS/timeout errors."""

    TRANSIENT_LOCK = "transient_lock"
    """Resource lock (Lark 131009/1254606 — frequency control)."""

    TRANSIENT_QUOTA = "transient_quota"
    """Quota gần hết — retry sau dài, route endpoint khác nếu có."""

    TRANSIENT_SERVER = "transient_server"
    """HTTP 5xx — server transient issue."""

    PERMANENT_AUTH = "permanent_auth"
    """Token expired/invalid — refresh hoặc fail-fast."""

    PERMANENT_NOT_FOUND = "permanent_not_found"
    """Resource đã xoá hoặc không tồn tại."""

    PERMANENT_PERM_DENIED = "permanent_perm_denied"
    """Permission denied — không retry."""

    PERMANENT_SCHEMA = "permanent_schema"
    """Schema validation fail — fix code."""

    UNKNOWN = "unknown"
    """Chưa phân loại được — default transient."""


# ============================================================================
# Lark API code mappings (theo Lark docs + V1 observed)
# ============================================================================
_RATE_LIMIT_CODES = frozenset({99991400, 230001})
_LOCK_CODES = frozenset({131009, 1254606, 1770001, 4000080})
_AUTH_CODES = frozenset({99991663, 99991664, 99991665, 1061045})
_NOT_FOUND_CODES = frozenset({131005, 131008, 1770003, 1770006})
_PERM_DENIED_CODES = frozenset({131006, 1770032, 1254030, 1254040})
_SCHEMA_CODES = frozenset({1254045, 1254046, 1254050})
_SERVER_TRANSIENT_CODES = frozenset({131001, 230002, 800004135, 800014300})


# ============================================================================
# Network error patterns (substring match trong str(exception))
# ============================================================================
_NETWORK_PATTERNS = (
    "timeout",
    "connection refused",
    "connection reset",
    "connection aborted",
    "broken pipe",
    "eof",
    "no route to host",
    "network is unreachable",
    "name resolution failed",
    "temporary failure in name resolution",
    "dns",
    "tls handshake",
    "remote disconnected",
    "read timed out",
)


def classify_lark_code(code: int) -> ErrorCategory:
    """Pure: map Lark API code → category."""
    if code in _RATE_LIMIT_CODES:
        return ErrorCategory.TRANSIENT_RATE_LIMIT
    if code in _LOCK_CODES:
        return ErrorCategory.TRANSIENT_LOCK
    if code in _AUTH_CODES:
        return ErrorCategory.PERMANENT_AUTH
    if code in _NOT_FOUND_CODES:
        return ErrorCategory.PERMANENT_NOT_FOUND
    if code in _PERM_DENIED_CODES:
        return ErrorCategory.PERMANENT_PERM_DENIED
    if code in _SCHEMA_CODES:
        return ErrorCategory.PERMANENT_SCHEMA
    if code in _SERVER_TRANSIENT_CODES:
        return ErrorCategory.TRANSIENT_SERVER
    # HTTP-like codes mapped vào category
    if code == 429:
        return ErrorCategory.TRANSIENT_RATE_LIMIT
    if 500 <= code < 600:
        return ErrorCategory.TRANSIENT_SERVER
    if code in {401, 403}:
        return ErrorCategory.PERMANENT_PERM_DENIED
    if code == 404:
        return ErrorCategory.PERMANENT_NOT_FOUND
    return ErrorCategory.UNKNOWN


def classify_error(error: BaseException) -> ErrorCategory:
    """Phân loại 1 exception → category.

    Order:
      1. LarkAPIError → check code
      2. Match keyword "quota" trong msg → TRANSIENT_QUOTA
      3. Match network patterns → TRANSIENT_NETWORK
      4. Generic Exception → UNKNOWN
    """
    if isinstance(error, LarkAPIError):
        return classify_lark_code(error.code)

    msg = str(error).lower()

    # LLM quota exhausted patterns
    if any(p in msg for p in (
        "quota", "out of credit", "insufficient_quota",
        "rate limit", "rate_limit",
    )):
        # quota là dạng đặc biệt của rate-limit nhưng cần backoff dài hơn
        if "quota" in msg or "credit" in msg:
            return ErrorCategory.TRANSIENT_QUOTA
        return ErrorCategory.TRANSIENT_RATE_LIMIT

    if any(p in msg for p in _NETWORK_PATTERNS):
        return ErrorCategory.TRANSIENT_NETWORK

    # HTTP exceptions với status code
    status_code = getattr(error, "status_code", None) or getattr(
        error, "status", None,
    )
    if isinstance(status_code, int):
        return classify_lark_code(status_code)

    return ErrorCategory.UNKNOWN


def is_retryable(category: ErrorCategory) -> bool:
    """True nếu category nên retry, False nếu permanent fail."""
    return category in {
        ErrorCategory.TRANSIENT_RATE_LIMIT,
        ErrorCategory.TRANSIENT_NETWORK,
        ErrorCategory.TRANSIENT_LOCK,
        ErrorCategory.TRANSIENT_QUOTA,
        ErrorCategory.TRANSIENT_SERVER,
        ErrorCategory.PERMANENT_AUTH,  # retry sau khi refresh token
        ErrorCategory.UNKNOWN,
    }


def recommended_backoff_seconds(
    category: ErrorCategory,
    attempt: int,
    *,
    base: float = 1.0,
    max_delay: float = 60.0,
) -> float:
    """Recommended sleep seconds cho retry attempt N (0-indexed).

    Strategy:
      - RATE_LIMIT: exponential 2,4,8,16,32,60 — Lark hay cấp lại nhanh
      - NETWORK: linear ngắn 1,2,3 — connection blip
      - LOCK: longer exp 5,10,20,40,60 — Lark lock thường lâu
      - QUOTA: very long exp 30,60,120,240 — quota daily/hourly
      - SERVER: exp 2,4,8,16
      - AUTH: 0 (refresh ngay không sleep)
      - default: exp 1,2,4
    """
    if category == ErrorCategory.PERMANENT_AUTH:
        return 0.0

    multiplier_map: dict[ErrorCategory, tuple[float, float]] = {
        # (base, max)
        ErrorCategory.TRANSIENT_RATE_LIMIT: (2.0, 60.0),
        ErrorCategory.TRANSIENT_NETWORK: (1.0, 8.0),
        ErrorCategory.TRANSIENT_LOCK: (5.0, 60.0),
        ErrorCategory.TRANSIENT_QUOTA: (30.0, 600.0),
        ErrorCategory.TRANSIENT_SERVER: (2.0, 30.0),
        ErrorCategory.UNKNOWN: (base, max_delay),
    }
    cat_base, cat_max = multiplier_map.get(category, (base, max_delay))
    delay = cat_base * (2 ** max(0, attempt))
    return float(min(delay, cat_max))
