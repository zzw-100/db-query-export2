"""FastMCP server for PostgreSQL natural language query interface.

This module implements the MCP server using FastMCP, exposing the query
functionality as an MCP tool. It includes complete lifespan management for
initializing and cleaning up all components.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from asyncpg import Pool
from mcp.server.fastmcp import FastMCP

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import Settings
from pg_mcp.db.pool import close_pools, create_pools
from pg_mcp.models.query import QueryRequest, QueryResponse, ReturnType
from pg_mcp.observability.logging import configure_logging, get_logger
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import request_context
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import SQLGenerator
from pg_mcp.services.sql_validator import SQLValidator

logger = get_logger(__name__)

# Global state for lifespan management
_settings: Settings | None = None
_pools: dict[str, Pool] | None = None
_schema_cache: SchemaCache | None = None
_orchestrator: QueryOrchestrator | None = None
_metrics: MetricsCollector | None = None
_circuit_breaker: CircuitBreaker | None = None
_rate_limiter: MultiRateLimiter | None = None


@asynccontextmanager
async def lifespan(_app: FastMCP) -> AsyncIterator[None]:  # type: ignore[type-arg]
    """Lifespan context manager for server initialization and cleanup.

    This function manages the complete lifecycle of the MCP server:

    Startup:
        1. Load configuration from Settings
        2. Configure logging
        3. Create database connection pools
        4. Load schema cache for all databases
        5. Initialize metrics collector
        6. Create service components (generators, validators, executors)
        7. Initialize resilience components (circuit breaker, rate limiter)
        8. Create query orchestrator
        9. Start metrics HTTP server (optional)

    Shutdown:
        1. Stop schema auto-refresh (if enabled)
        2. Close all database connection pools
        3. Stop metrics HTTP server (if running)

    Yields:
        None

    Example:
        >>> async with lifespan():
        ...     # Server is running with all components initialized
        ...     pass
    """
    global _settings, _pools, _schema_cache, _orchestrator, _metrics
    global _circuit_breaker, _rate_limiter

    logger.info("Starting PostgreSQL MCP Server initialization...")

    try:
        # 1. Load Settings
        logger.info("Loading configuration...")
        _settings = Settings()

        # 2. Configure logging
        logger.info("Configuring logging...")
        configure_logging(
            level=_settings.observability.log_level,
            log_format=_settings.observability.log_format,
            enable_sensitive_filter=True,
        )

        logger.info(
            "Configuration loaded",
            extra={
                "environment": _settings.environment,
                "log_level": _settings.observability.log_level,
            },
        )

        # 3. Create one connection pool per configured database
        logger.info("Creating database connection pools...")
        db_by_name = {cfg.name: cfg for cfg in _settings.databases}
        _pools = await create_pools(_settings.databases)
        for db_name, db_config in db_by_name.items():
            logger.info(
                f"Created connection pool for database '{db_name}'",
                extra={
                    "min_size": db_config.min_pool_size,
                    "max_size": db_config.max_pool_size,
                },
            )

        # 4. Load Schema cache
        logger.info("Initializing schema cache...")
        _schema_cache = SchemaCache(_settings.cache)

        for db_name, pool in _pools.items():
            logger.info(f"Loading schema for database '{db_name}'...")
            schema = await _schema_cache.load(db_name, pool)
            logger.info(
                f"Schema loaded for '{db_name}'",
                extra={
                    "tables": len(schema.tables),
                },
            )

        # Optional: Start schema auto-refresh
        # Disabled by default to avoid unnecessary background tasks
        # Uncomment to enable:
        # if _settings.cache.enabled:
        #     logger.info("Starting schema auto-refresh...")
        #     await _schema_cache.start_auto_refresh(
        #         interval_minutes=60,  # Refresh every hour
        #         pools=_pools,
        #     )

        # 5. Initialize metrics collector
        logger.info("Initializing metrics collector...")
        _metrics = MetricsCollector()

        # Start metrics HTTP server if enabled
        if _settings.observability.metrics_enabled:
            from prometheus_client import start_http_server

            start_http_server(_settings.observability.metrics_port)
            logger.info(f"Metrics server started on port {_settings.observability.metrics_port}")

        # 6. Create service components
        logger.info("Initializing service components...")

        # SQL Generator
        sql_generator = SQLGenerator(_settings.openai)

        # SQL Validator uses blocked tables/columns and EXPLAIN policy from config.
        # Passing None (or omitting the args) reads those fields instead of disabling them.
        sql_validator = SQLValidator(config=_settings.security)

        # SQL Executor (one per database, bound to that database's pool and config)
        sql_executors: dict[str, SQLExecutor] = {}
        for db_name, pool in _pools.items():
            executor = SQLExecutor(
                pool=pool,
                security_config=_settings.security,
                db_config=db_by_name[db_name],
            )
            sql_executors[db_name] = executor
            logger.info(f"Created SQL executor for database '{db_name}'")

        # Result Validator
        result_validator = ResultValidator(
            openai_config=_settings.openai,
            validation_config=_settings.validation,
        )

        # 7. Initialize resilience components
        logger.info("Initializing resilience components...")

        # Circuit Breaker for LLM calls
        _circuit_breaker = CircuitBreaker(
            failure_threshold=_settings.resilience.circuit_breaker_threshold,
            recovery_timeout=_settings.resilience.circuit_breaker_timeout,
        )

        # Rate Limiter honors the configured concurrency caps
        _rate_limiter = MultiRateLimiter(
            query_limit=_settings.resilience.max_concurrent_queries,
            llm_limit=_settings.resilience.max_concurrent_llm_calls,
        )

        # 8. Create QueryOrchestrator with the shared breaker, limiter, and metrics.
        # The per-database executor map is what actually runs the query.
        logger.info("Creating query orchestrator...")
        _orchestrator = QueryOrchestrator(
            sql_generator=sql_generator,
            sql_validator=sql_validator,
            sql_executor=None,
            result_validator=result_validator,
            schema_cache=_schema_cache,
            pools=_pools,
            resilience_config=_settings.resilience,
            validation_config=_settings.validation,
            sql_executors=sql_executors,
            rate_limiter=_rate_limiter,
            metrics=_metrics,
            circuit_breaker=_circuit_breaker,
        )

        logger.info("PostgreSQL MCP Server initialization complete!")
        logger.info(
            "Server ready to accept requests",
            extra={
                "databases": list(_pools.keys()),
                "cache_enabled": _settings.cache.enabled,
                "metrics_enabled": _settings.observability.metrics_enabled,
            },
        )

        # Yield to run the server
        yield

    finally:
        # Shutdown sequence
        logger.info("Starting PostgreSQL MCP Server shutdown...")

        # Stop schema auto-refresh with timeout
        if _schema_cache is not None:
            try:
                import asyncio
                await asyncio.wait_for(
                    _schema_cache.stop_auto_refresh(),
                    timeout=3.0
                )
                logger.info("Schema auto-refresh stopped")
            except asyncio.TimeoutError:
                logger.warning("Schema auto-refresh stop timed out")
            except Exception as e:
                logger.warning(f"Error stopping schema auto-refresh: {e!s}")

        # Close database connection pools with timeout
        if _pools is not None:
            try:
                # Use 5 second timeout for graceful shutdown
                await close_pools(_pools, timeout=5.0)
                logger.info("Database connection pools closed")
            except Exception as e:
                logger.error(f"Error closing connection pools: {e!s}")

        logger.info("PostgreSQL MCP Server shutdown complete")


def build_health_status() -> dict[str, Any]:
    """Collect pool, circuit-breaker, and rate-limiter state.

    Returns:
        dict: Health payload. ``status`` is ``starting`` until the lifespan
        finishes, ``degraded`` when the circuit is open, otherwise ``ok``.
    """
    databases: list[dict[str, Any]] = []
    if _pools is not None:
        for name, pool in _pools.items():
            entry: dict[str, Any] = {"name": name}
            get_size = getattr(pool, "get_size", None)
            get_idle = getattr(pool, "get_idle_size", None)
            if callable(get_size):
                try:
                    entry["size"] = get_size()
                except Exception:
                    entry["size"] = None
            if callable(get_idle):
                try:
                    entry["idle"] = get_idle()
                except Exception:
                    entry["idle"] = None
            databases.append(entry)

    breaker: dict[str, Any] | None = None
    circuit_open = False
    if _circuit_breaker is not None:
        stats = dict(_circuit_breaker.get_stats())
        state = stats.get("state")
        state_text = getattr(state, "value", str(state) if state is not None else None)
        stats["state"] = state_text
        if stats.get("last_failure_time") is not None:
            stats["last_failure_time"] = float(stats["last_failure_time"])
        breaker = stats
        circuit_open = state_text == "open"

    limiter = _rate_limiter.get_all_stats() if _rate_limiter is not None else None
    if _orchestrator is None:
        status = "starting"
    elif circuit_open:
        status = "degraded"
    else:
        status = "ok"

    return {
        "status": status,
        "databases": databases,
        "circuit_breaker": breaker,
        "rate_limiter": limiter,
    }


# Create FastMCP server instance with lifespan
mcp = FastMCP("pg-mcp", lifespan=lifespan)


def _mount_health_route() -> None:
    """Expose GET /health when the server transport supports custom routes."""
    if not hasattr(mcp, "custom_route"):
        return
    try:
        from starlette.responses import JSONResponse
    except ImportError:
        return

    @mcp.custom_route("/health", methods=["GET"])
    async def health_endpoint(_request: Any) -> JSONResponse:
        return JSONResponse(build_health_status())


_mount_health_route()


@mcp.tool()
async def health() -> dict[str, Any]:
    """Report process health, configured databases, and resilience state.

    Returns:
        dict: Health payload. status is "starting", "ok", or "degraded".
    """
    return build_health_status()


@mcp.tool()
async def query(
    question: str,
    database: str | None = None,
    return_type: str = "result",
) -> dict[str, Any]:
    """Execute a natural language query against PostgreSQL database.

    This tool converts natural language questions into SQL queries and executes
    them against the specified PostgreSQL database. It includes comprehensive
    security validation, result verification, and error handling.

    Args:
        question: Natural language description of the query.
            Examples:
                - "How many users registered in the last 30 days?"
                - "Show me the top 10 products by revenue"
                - "What is the average order value by country?"

        database: Target database name (optional if only one database is configured).
            If not specified and only one database is available, it will be
            automatically selected.

        return_type: Type of result to return.
            Options:
                - "sql": Return only the generated SQL query without executing it
                - "result": Execute the query and return results (default)

    Returns:
        dict: Query response containing:
            - success (bool): Whether the query succeeded
            - generated_sql (str): The generated SQL query
            - data (dict): Query results if executed (columns, rows, row_count, etc.)
            - error (dict): Error information if query failed
            - confidence (int): Confidence score (0-100) for result quality
            - tokens_used (int): Number of LLM tokens consumed

    Examples:
        >>> # Get query results
        >>> result = await query(
        ...     question="How many active users are there?",
        ...     return_type="result"
        ... )
        >>> print(result["data"]["rows"])

        >>> # Get SQL only
        >>> result = await query(
        ...     question="Count all products",
        ...     return_type="sql"
        ... )
        >>> print(result["generated_sql"])

    Raises:
        This function does not raise exceptions. All errors are captured and
        returned in the response with success=False and error details.

    Security:
        - Only SELECT queries are allowed (no INSERT, UPDATE, DELETE, DROP, etc.)
        - Dangerous PostgreSQL functions are blocked (pg_sleep, file operations, etc.)
        - Query execution timeout is enforced
        - Row count limits prevent memory exhaustion
        - All queries run in read-only transactions
    """
    global _orchestrator

    if _orchestrator is None:
        return {
            "success": False,
            "error": {
                "code": "SERVER_NOT_INITIALIZED",
                "message": "Server not initialized properly",
                "details": None,
            },
        }

    # Validate return_type
    if return_type not in ("sql", "result"):
        return {
            "success": False,
            "error": {
                "code": "INVALID_PARAMETER",
                "message": f"Invalid return_type: '{return_type}'. Must be 'sql' or 'result'.",
                "details": {"return_type": return_type},
            },
        }

    # Build request
    try:
        request = QueryRequest(
            question=question,
            database=database,
            return_type=ReturnType(return_type),
        )
    except Exception as e:
        return {
            "success": False,
            "error": {
                "code": "INVALID_REQUEST",
                "message": f"Invalid request parameters: {e!s}",
                "details": {"error": str(e)},
            },
        }

    # Execute query through orchestrator. The request id is shared with the pipeline.
    try:
        async with request_context() as request_id:
            logger.info(
                "Query tool invoked",
                extra={
                    "request_id": request_id,
                    "database": database,
                    "return_type": return_type,
                },
            )
            response: QueryResponse = await _orchestrator.execute_query(request)
            result = response.to_dict()
            if "tokens_used" not in result:
                result["tokens_used"] = 0
            return result
    except Exception as e:
        logger.exception("Unexpected error in query tool")
        return {
            "success": False,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": f"Internal server error: {e!s}",
                "details": {"error_type": type(e).__name__},
            },
            "tokens_used": 0,
        }


if __name__ == "__main__":
    """Run the server when executed directly."""
    import anyio

    anyio.run(mcp.run_stdio_async)
