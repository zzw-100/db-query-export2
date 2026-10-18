"""Tests for the Codex review gaps: multi-db routing, security, and resilience."""

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pg_mcp.config.settings import (
    DatabaseConfig,
    OpenAIConfig,
    ResilienceConfig,
    SecurityConfig,
    Settings,
    ValidationConfig,
)
from pg_mcp.models.query import QueryRequest, ReturnType
from pg_mcp.models.schema import DatabaseSchema
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.sql_validator import SQLValidator


class RecordingLimiter:
    """Rate limiter double that records which gates the orchestrator enters."""

    def __init__(self) -> None:
        self.llm_calls = 0
        self.query_calls = 0

    @asynccontextmanager
    async def for_llm(self, *, timeout: float | None = None) -> Any:
        self.llm_calls += 1
        yield

    @asynccontextmanager
    async def for_queries(self, *, timeout: float | None = None) -> Any:
        self.query_calls += 1
        yield


def _orchestrator(**overrides: Any) -> QueryOrchestrator:
    schema = DatabaseSchema(database_name="test_db")
    cache = MagicMock()
    cache.get.return_value = schema
    generator = AsyncMock()
    generator.generate.return_value = "SELECT 1"
    generator.last_tokens_used = 12
    validator = MagicMock()
    validator.validate_or_raise.return_value = None
    result_validator = AsyncMock()
    defaults: dict[str, Any] = {
        "sql_generator": generator,
        "sql_validator": validator,
        "sql_executor": AsyncMock(),
        "result_validator": result_validator,
        "schema_cache": cache,
        "pools": {"analytics": MagicMock(), "warehouse": MagicMock()},
        "resilience_config": ResilienceConfig(max_retries=0, retry_delay=0.1, backoff_factor=2.0),
        "validation_config": ValidationConfig(),
    }
    defaults.update(overrides)
    return QueryOrchestrator(**defaults)


@pytest.mark.asyncio
async def test_request_uses_executor_for_named_database() -> None:
    """A request for warehouse must not run on the analytics executor."""
    analytics = AsyncMock()
    analytics.execute.return_value = ([{"id": 1}], 1)
    warehouse = AsyncMock()
    warehouse.execute.return_value = ([{"id": 2}], 1)
    limiter = RecordingLimiter()
    orchestrator = _orchestrator(
        sql_executor=None,
        sql_executors={"analytics": analytics, "warehouse": warehouse},
        rate_limiter=limiter,
    )

    response = await orchestrator.execute_query(
        QueryRequest(question="count rows", database="warehouse", return_type=ReturnType.RESULT)
    )

    assert response.success is True
    warehouse.execute.assert_awaited()
    analytics.execute.assert_not_awaited()
    assert limiter.llm_calls == 1
    assert limiter.query_calls == 1
    assert response.tokens_used == 12


@pytest.mark.asyncio
async def test_missing_executor_does_not_fall_through() -> None:
    """When the executor map is set, an unknown database is an error."""
    analytics = AsyncMock()
    orchestrator = _orchestrator(
        sql_executor=analytics,
        sql_executors={"analytics": analytics},
        pools={"analytics": MagicMock(), "warehouse": MagicMock()},
    )

    response = await orchestrator.execute_query(
        QueryRequest(question="count rows", database="warehouse", return_type=ReturnType.RESULT)
    )

    assert response.success is False
    assert response.error is not None
    assert response.error.code == "database_error"
    analytics.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_question_length_is_enforced_before_llm() -> None:
    """Questions over max_question_length never reach the generator."""
    generator = AsyncMock()
    orchestrator = _orchestrator(
        sql_generator=generator,
        pools={"analytics": MagicMock()},
        validation_config=ValidationConfig(max_question_length=5),
    )

    response = await orchestrator.execute_query(
        QueryRequest(question="too long", database="analytics", return_type=ReturnType.SQL)
    )

    assert response.success is False
    assert response.error is not None
    assert response.error.code == "question_too_long"
    generator.generate.assert_not_awaited()


def test_validator_uses_security_config_when_overrides_omitted() -> None:
    """None overrides mean the SecurityConfig lists and EXPLAIN flag apply."""
    config = SecurityConfig(
        blocked_tables=["salaries"],
        blocked_columns=["ssn"],
        allow_explain=False,
    )
    validator = SQLValidator(config=config)

    valid, error = validator.validate("SELECT ssn FROM salaries")
    assert valid is False
    assert error is not None
    assert "salaries" in error or "ssn" in error

    explain_ok, explain_error = validator.validate("EXPLAIN SELECT 1")
    assert explain_ok is False
    assert explain_error is not None
    assert "EXPLAIN" in explain_error


def test_settings_databases_fall_back_to_primary() -> None:
    """An empty databases list is filled from the primary DATABASE_* config."""
    primary = DatabaseConfig(name="blog_small", host="db.internal")
    settings = Settings(
        openai=OpenAIConfig(api_key="sk-test"),
        database=primary,
    )
    assert [db.name for db in settings.databases] == ["blog_small"]
    assert settings.databases[0].host == "db.internal"


def test_settings_reject_duplicate_database_names() -> None:
    """Two configured databases cannot share a name."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="唯一"):
        Settings(
            openai=OpenAIConfig(api_key="sk-test"),
            databases=[
                DatabaseConfig(name="analytics", host="a"),
                DatabaseConfig(name="analytics", host="b"),
            ],
        )


def test_security_lists_parse_from_csv() -> None:
    """Blocked tables and columns accept the same comma-separated env form."""
    config = SecurityConfig(
        blocked_tables="salaries, public.audit_log",  # type: ignore[arg-type]
        blocked_columns="ssn, users.password",  # type: ignore[arg-type]
        allow_explain=True,
    )
    assert config.blocked_tables == ["salaries", "public.audit_log"]
    assert config.blocked_columns == ["ssn", "users.password"]
    assert config.allow_explain is True
