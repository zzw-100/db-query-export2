"""Unit tests for tracing context and the metrics collector."""

import pytest

from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import get_request_id, request_context
from pg_mcp.server import build_health_status


@pytest.mark.asyncio
async def test_request_context_sets_and_clears_request_id() -> None:
    """The request id is visible inside the context and gone after it."""
    assert get_request_id() is None
    async with request_context("req-fixed") as request_id:
        assert request_id == "req-fixed"
        assert get_request_id() == "req-fixed"
    assert get_request_id() is None


def test_metrics_collector_records_request_path_events() -> None:
    """The collector accepts the events the orchestrator emits."""
    collector = MetricsCollector()
    collector.increment_query_request("success", "analytics")
    collector.increment_llm_call("generate_sql")
    collector.observe_llm_latency("generate_sql", 0.01)
    collector.increment_llm_tokens("generate_sql", 4)
    collector.increment_sql_rejected("blocked_table")
    collector.observe_db_query_duration(0.02)
    collector.set_db_connections_active("analytics", 1)
    collector.set_schema_cache_age("analytics", 3.0)


def test_health_status_before_startup() -> None:
    """Before the lifespan runs, health reports that the server is starting."""
    report = build_health_status()
    assert report["status"] == "starting"
    assert report["databases"] == []
    assert "circuit_breaker" in report
    assert "rate_limiter" in report
