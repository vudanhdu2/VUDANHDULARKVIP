"""Tests cho `error_classifier`."""

from __future__ import annotations

import pytest

from waytoagi.lark.auth import LarkAPIError
from waytoagi.resilience.error_classifier import (
    ErrorCategory,
    classify_error,
    classify_lark_code,
    is_retryable,
    recommended_backoff_seconds,
)


@pytest.mark.unit
class TestClassifyLarkCode:
    @pytest.mark.parametrize("code", [99991400, 230001])
    def test_rate_limit_codes(self, code: int) -> None:
        assert classify_lark_code(code) == ErrorCategory.TRANSIENT_RATE_LIMIT

    @pytest.mark.parametrize("code", [131009, 1254606, 1770001, 4000080])
    def test_lock_codes(self, code: int) -> None:
        assert classify_lark_code(code) == ErrorCategory.TRANSIENT_LOCK

    @pytest.mark.parametrize("code", [99991663, 99991664, 1061045])
    def test_auth_codes(self, code: int) -> None:
        assert classify_lark_code(code) == ErrorCategory.PERMANENT_AUTH

    @pytest.mark.parametrize("code", [131005, 131008, 1770003])
    def test_not_found_codes(self, code: int) -> None:
        assert classify_lark_code(code) == ErrorCategory.PERMANENT_NOT_FOUND

    @pytest.mark.parametrize("code", [131006, 1770032, 1254030])
    def test_perm_denied_codes(self, code: int) -> None:
        assert classify_lark_code(code) == ErrorCategory.PERMANENT_PERM_DENIED

    @pytest.mark.parametrize("code", [1254045, 1254046])
    def test_schema_codes(self, code: int) -> None:
        assert classify_lark_code(code) == ErrorCategory.PERMANENT_SCHEMA

    @pytest.mark.parametrize("code", [131001, 230002, 800004135])
    def test_server_codes(self, code: int) -> None:
        assert classify_lark_code(code) == ErrorCategory.TRANSIENT_SERVER

    def test_http_429(self) -> None:
        assert classify_lark_code(429) == ErrorCategory.TRANSIENT_RATE_LIMIT

    @pytest.mark.parametrize("code", [500, 502, 503, 504])
    def test_http_5xx(self, code: int) -> None:
        assert classify_lark_code(code) == ErrorCategory.TRANSIENT_SERVER

    @pytest.mark.parametrize("code", [401, 403])
    def test_http_4xx_perm(self, code: int) -> None:
        assert classify_lark_code(code) == ErrorCategory.PERMANENT_PERM_DENIED

    def test_http_404(self) -> None:
        assert classify_lark_code(404) == ErrorCategory.PERMANENT_NOT_FOUND

    def test_unknown_code(self) -> None:
        assert classify_lark_code(999999) == ErrorCategory.UNKNOWN


@pytest.mark.unit
class TestClassifyError:
    def test_lark_api_error(self) -> None:
        err = LarkAPIError(99991400, "rate limit", "/test")
        assert classify_error(err) == ErrorCategory.TRANSIENT_RATE_LIMIT

    def test_network_timeout(self) -> None:
        err = TimeoutError("read timed out")
        assert classify_error(err) == ErrorCategory.TRANSIENT_NETWORK

    def test_connection_refused(self) -> None:
        err = ConnectionError("Connection refused")
        assert classify_error(err) == ErrorCategory.TRANSIENT_NETWORK

    def test_dns_failure(self) -> None:
        err = OSError("Name resolution failed")
        assert classify_error(err) == ErrorCategory.TRANSIENT_NETWORK

    def test_llm_quota_exhausted(self) -> None:
        err = RuntimeError("insufficient_quota: out of credit")
        assert classify_error(err) == ErrorCategory.TRANSIENT_QUOTA

    def test_llm_rate_limit_msg(self) -> None:
        err = RuntimeError("rate_limit reached")
        assert classify_error(err) == ErrorCategory.TRANSIENT_RATE_LIMIT

    def test_unknown_exception(self) -> None:
        err = ValueError("random unexpected error")
        assert classify_error(err) == ErrorCategory.UNKNOWN

    def test_status_code_attribute(self) -> None:
        class FakeError(Exception):
            status_code = 503

        assert classify_error(FakeError("server bom")) == (
            ErrorCategory.TRANSIENT_SERVER
        )


@pytest.mark.unit
class TestIsRetryable:
    @pytest.mark.parametrize("cat", [
        ErrorCategory.TRANSIENT_RATE_LIMIT,
        ErrorCategory.TRANSIENT_NETWORK,
        ErrorCategory.TRANSIENT_LOCK,
        ErrorCategory.TRANSIENT_QUOTA,
        ErrorCategory.TRANSIENT_SERVER,
        ErrorCategory.PERMANENT_AUTH,  # retry sau refresh
        ErrorCategory.UNKNOWN,
    ])
    def test_retryable(self, cat: ErrorCategory) -> None:
        assert is_retryable(cat) is True

    @pytest.mark.parametrize("cat", [
        ErrorCategory.PERMANENT_NOT_FOUND,
        ErrorCategory.PERMANENT_PERM_DENIED,
        ErrorCategory.PERMANENT_SCHEMA,
    ])
    def test_not_retryable(self, cat: ErrorCategory) -> None:
        assert is_retryable(cat) is False


@pytest.mark.unit
class TestRecommendedBackoff:
    def test_rate_limit_exp_growth(self) -> None:
        b0 = recommended_backoff_seconds(
            ErrorCategory.TRANSIENT_RATE_LIMIT, 0,
        )
        b3 = recommended_backoff_seconds(
            ErrorCategory.TRANSIENT_RATE_LIMIT, 3,
        )
        assert b3 > b0

    def test_quota_longer_than_rate_limit(self) -> None:
        rl = recommended_backoff_seconds(
            ErrorCategory.TRANSIENT_RATE_LIMIT, 0,
        )
        quota = recommended_backoff_seconds(ErrorCategory.TRANSIENT_QUOTA, 0)
        assert quota > rl

    def test_lock_longer_than_network(self) -> None:
        net = recommended_backoff_seconds(
            ErrorCategory.TRANSIENT_NETWORK, 0,
        )
        lock = recommended_backoff_seconds(ErrorCategory.TRANSIENT_LOCK, 0)
        assert lock > net

    def test_auth_zero_no_sleep(self) -> None:
        """Auth fail không cần sleep — refresh ngay."""
        assert recommended_backoff_seconds(
            ErrorCategory.PERMANENT_AUTH, 0,
        ) == 0.0

    def test_caps_at_max(self) -> None:
        """High attempt → cap by max_delay."""
        b = recommended_backoff_seconds(
            ErrorCategory.TRANSIENT_RATE_LIMIT, 100,
        )
        assert b == 60.0  # cap
