"""Unit tests for exponential backoff retries."""

from typing import Any

import pytest

from pg_mcp.models.errors import DatabaseError, LLMTimeoutError
from pg_mcp.resilience.retry import is_transient_database_error, retry_async


@pytest.mark.asyncio
async def test_retry_uses_configured_backoff() -> None:
    """Transient failures wait retry_delay * backoff_factor ** attempt."""
    calls = 0
    delays: list[float] = []

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise LLMTimeoutError(message="timed out")
        return "ok"

    async def sleep(delay: float) -> None:
        delays.append(delay)

    result = await retry_async(
        operation,
        max_retries=3,
        retry_delay=0.5,
        backoff_factor=2.0,
        retry_on=(LLMTimeoutError,),
        sleep=sleep,
    )

    assert result == "ok"
    assert calls == 3
    assert delays == [0.5, 1.0]


@pytest.mark.asyncio
async def test_retry_stops_after_max_retries() -> None:
    """The last failure is raised once the retry budget is spent."""

    async def operation() -> str:
        raise LLMTimeoutError(message="timed out")

    async def sleep(_delay: float) -> None:
        return None

    with pytest.raises(LLMTimeoutError):
        await retry_async(
            operation,
            max_retries=1,
            retry_delay=1.0,
            backoff_factor=2.0,
            retry_on=(LLMTimeoutError,),
            sleep=sleep,
        )


def test_non_transient_database_error_is_not_retried() -> None:
    """Ordinary query failures are not treated as connection blips."""
    error = DatabaseError(message="Query execution failed")
    assert is_transient_database_error(error) is False


def test_connection_sqlstate_is_transient() -> None:
    """Known connection SQLSTATEs are retryable."""
    error = DatabaseError(
        message="could not connect",
        details={"error_code": "08006"},
    )
    assert is_transient_database_error(error) is True


@pytest.mark.asyncio
async def test_non_retryable_predicate_does_not_sleep() -> None:
    """A false retry predicate raises immediately."""
    slept = False

    async def operation() -> Any:
        raise DatabaseError(message="syntax error at or near SELECT")

    async def sleep(_delay: float) -> None:
        nonlocal slept
        slept = True

    with pytest.raises(DatabaseError):
        await retry_async(
            operation,
            max_retries=3,
            retry_delay=1.0,
            backoff_factor=2.0,
            retry_on=(DatabaseError,),
            is_retryable=is_transient_database_error,
            sleep=sleep,
        )

    assert slept is False
