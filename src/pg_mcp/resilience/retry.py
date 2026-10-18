"""Retry with exponential backoff for transient failures.

Validation feedback retries stay immediate: they correct a bad SQL draft and
are not infrastructure failures. This helper is for operations that fail
because a dependency is briefly unavailable (LLM timeout, dropped DB connection).
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from pg_mcp.models.errors import DatabaseError

T = TypeVar("T")

_TRANSIENT_SQLSTATES = frozenset(
    {
        "40001",  # serialization_failure
        "40P01",  # deadlock_detected
        "08000",  # connection_exception
        "08001",  # sqlclient_unable_to_establish_sqlconnection
        "08003",  # connection_does_not_exist
        "08004",  # sqlserver_rejected_establishment_of_sqlconnection
        "08006",  # connection_failure
        "57P01",  # admin_shutdown
        "57P02",  # crash_shutdown
        "57P03",  # cannot_connect_now
        "53300",  # too_many_connections
    }
)

_TRANSIENT_MESSAGE_TOKENS = (
    "connection refused",
    "connection reset",
    "connection closed",
    "server closed the connection",
    "could not connect",
    "temporarily unavailable",
    "too many connections",
    "deadlock",
    "serialization failure",
    "broken pipe",
)


def is_transient_database_error(exc: BaseException) -> bool:
    """Return True when a database error is safe to retry.

    Args:
        exc: Exception raised by the database layer.

    Returns:
        bool: True for connection, deadlock, and serialization failures.
    """
    if not isinstance(exc, DatabaseError):
        return False
    code = str((exc.details or {}).get("error_code") or "")
    if code in _TRANSIENT_SQLSTATES:
        return True
    message = exc.message.lower()
    return any(token in message for token in _TRANSIENT_MESSAGE_TOKENS)


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    max_retries: int,
    retry_delay: float,
    backoff_factor: float,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    is_retryable: Callable[[BaseException], bool] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> T:
    """Run an async operation, retrying transient failures with backoff.

    The first attempt is immediate. After each retryable failure the helper
    waits ``retry_delay * (backoff_factor ** attempt)`` seconds. ``max_retries``
    is the number of retries after the initial attempt.

    Args:
        operation: Zero-argument coroutine factory.
        max_retries: Extra attempts after the first failure. Zero disables retry.
        retry_delay: Delay in seconds before the first retry.
        backoff_factor: Multiplier applied to the delay after each retry.
        retry_on: Exception types that may be retried.
        is_retryable: Optional predicate. When it returns False the error is raised.
        sleep: Awaitable used to wait. Defaults to asyncio.sleep. Tests inject a fake.

    Returns:
        The operation result.

    Raises:
        BaseException: The last failure, or the first non-retryable failure.
    """
    sleeper = sleep or asyncio.sleep
    attempt = 0
    while True:
        try:
            return await operation()
        except retry_on as exc:
            if is_retryable is not None and not is_retryable(exc):
                raise
            if attempt >= max_retries:
                raise
            delay = retry_delay * (backoff_factor**attempt)
            attempt += 1
            await sleeper(delay)
