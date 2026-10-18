"""Query orchestrator for coordinating the complete query flow.

This module provides the QueryOrchestrator class that coordinates all components
of the query processing pipeline: SQL generation, validation, execution, and result
validation. It implements retry logic, error handling, and request tracking.
"""

import contextvars
import time
from typing import Any

from asyncpg import Pool

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
from pg_mcp.models.errors import (
    DatabaseError,
    ErrorCode,
    LLMError,
    LLMTimeoutError,
    LLMUnavailableError,
    PgMcpError,
    QuestionTooLongError,
    RateLimitExceededError,
    SchemaLoadError,
    SecurityViolationError,
    SQLParseError,
)
from pg_mcp.models.query import (
    ErrorDetail,
    QueryRequest,
    QueryResponse,
    QueryResult,
    ReturnType,
    ValidationResult,
)
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import get_request_id, get_tracing_logger, request_context
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.resilience.retry import is_transient_database_error, retry_async
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import SQLGenerator
from pg_mcp.services.sql_validator import SQLValidator

logger = get_tracing_logger(__name__)

_metric_database: contextvars.ContextVar[str] = contextvars.ContextVar(
    "pg_mcp_metric_database",
    default="unknown",
)
_metric_status: contextvars.ContextVar[str] = contextvars.ContextVar(
    "pg_mcp_metric_status",
    default="error",
)


class QueryOrchestrator:
    """Orchestrates the complete query processing pipeline.

    This class coordinates SQL generation, validation, execution, and result
    validation. It implements retry logic with error feedback, circuit breaker
    pattern for fault tolerance, and comprehensive error handling.

    Example:
        >>> orchestrator = QueryOrchestrator(
        ...     sql_generator=generator,
        ...     sql_validator=validator,
        ...     sql_executor=executor,
        ...     result_validator=result_validator,
        ...     schema_cache=cache,
        ...     pools={"mydb": pool},
        ...     resilience_config=resilience_config,
        ...     validation_config=validation_config,
        ... )
        >>> response = await orchestrator.execute_query(QueryRequest(
        ...     question="How many users?",
        ...     database="mydb"
        ... ))
    """

    def __init__(
        self,
        sql_generator: SQLGenerator,
        sql_validator: SQLValidator,
        sql_executor: SQLExecutor | None,
        result_validator: ResultValidator,
        schema_cache: SchemaCache,
        pools: dict[str, Pool],
        resilience_config: ResilienceConfig,
        validation_config: ValidationConfig,
        sql_executors: dict[str, SQLExecutor] | None = None,
        rate_limiter: MultiRateLimiter | None = None,
        metrics: MetricsCollector | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        """Initialize query orchestrator.

        Args:
            sql_generator: SQL generation service.
            sql_validator: SQL validation service.
            sql_executor: Legacy single executor. Used only when sql_executors
                does not contain the resolved database.
            result_validator: Result validation service.
            schema_cache: Schema cache instance.
            pools: Dictionary mapping database names to connection pools.
            resilience_config: Resilience configuration for retries and circuit breaker.
            validation_config: Validation configuration including thresholds.
            sql_executors: Executors keyed by database name. This is the path
                that keeps a request on the database it asked for.
            rate_limiter: Shared limiter for LLM calls and query execution.
            metrics: Metrics collector. Omitted in unit tests.
            circuit_breaker: Shared breaker. A private one is created when omitted.
        """
        self.sql_generator = sql_generator
        self.sql_validator = sql_validator
        self.sql_executor = sql_executor
        self.sql_executors = sql_executors or {}
        self.result_validator = result_validator
        self.schema_cache = schema_cache
        self.pools = pools
        self.resilience_config = resilience_config
        self.validation_config = validation_config
        self.rate_limiter = rate_limiter
        self.metrics = metrics

        self.circuit_breaker = circuit_breaker or CircuitBreaker(
            failure_threshold=resilience_config.circuit_breaker_threshold,
            recovery_timeout=resilience_config.circuit_breaker_timeout,
        )

    async def execute_query(self, request: QueryRequest) -> QueryResponse:
        """Execute complete query flow from question to results.

        This method orchestrates the entire pipeline:
        1. Generate request_id for tracking
        2. Resolve and validate database name
        3. Load schema from cache
        4. Generate and validate SQL with retry logic
        5. Execute SQL (if return_type == RESULT)
        6. Validate results (optional)
        7. Return structured response

        Args:
            request: Query request containing question and parameters.

        Returns:
            QueryResponse: Complete response with SQL, results, or error information.

        Example:
            >>> response = await orchestrator.execute_query(
            ...     QueryRequest(question="Count all users", return_type="result")
            ... )
            >>> if response.success:
            ...     print(f"Found {response.data.row_count} rows")
        """
        existing_request_id = get_request_id()
        async with request_context(existing_request_id) as request_id:
            started = time.perf_counter()
            db_token = _metric_database.set("unknown")
            status_token = _metric_status.set("error")
            try:
                return await self._execute_query_impl(request, request_id)
            finally:
                if self.metrics is not None:
                    self.metrics.query_duration.observe(time.perf_counter() - started)
                    self.metrics.increment_query_request(
                        _metric_status.get(),
                        _metric_database.get(),
                    )
                _metric_database.reset(db_token)
                _metric_status.reset(status_token)

    async def _execute_query_impl(self, request: QueryRequest, request_id: str) -> QueryResponse:
        """Run the query pipeline inside an active trace context."""
        logger.info(
            "Starting query execution",
            extra={"request_id": request_id, "question": request.question[:100]},
        )

        try:
            self._enforce_question_length(request.question)

            # Step 1: Resolve database name
            database_name = self._resolve_database(request.database)
            _metric_database.set(database_name)
            logger.debug(
                "Resolved database",
                extra={"request_id": request_id, "database": database_name},
            )

            # Step 2: Get schema from cache
            schema = self.schema_cache.get(database_name)
            if schema is None:
                # Schema not in cache, load it
                pool = self.pools.get(database_name)
                if pool is None:
                    raise DatabaseError(
                        message=f"No connection pool available for database '{database_name}'",
                        details={"database": database_name},
                    )
                try:
                    schema = await self.schema_cache.load(database_name, pool)
                except Exception as e:
                    raise SchemaLoadError(
                        message=f"Failed to load schema for database '{database_name}': {e!s}",
                        details={"database": database_name, "error": str(e)},
                    ) from e

            logger.debug(
                "Schema loaded",
                extra={
                    "request_id": request_id,
                    "database": database_name,
                    "tables": len(schema.tables),
                },
            )

            # Step 3: Generate and validate SQL with retry logic
            generated_sql, validation_result, tokens_used = await self._generate_sql_with_retry(
                question=request.question,
                schema=schema,
                request_id=request_id,
            )

            # Step 4: If return_type is SQL, return early
            if request.return_type == ReturnType.SQL:
                logger.info(
                    "Returning SQL only",
                    extra={"request_id": request_id, "sql_length": len(generated_sql)},
                )
                _metric_status.set("success")
                return QueryResponse(
                    success=True,
                    generated_sql=generated_sql,
                    validation=validation_result,
                    data=None,
                    error=None,
                    confidence=100,
                    tokens_used=tokens_used,
                )

            # Step 5: Execute SQL on the executor bound to the resolved database
            logger.debug("Executing SQL", extra={"request_id": request_id, "database": database_name})
            start_time = self._get_current_time_ms()
            executor = self._executor_for(database_name)

            results, total_count = await self._execute_with_resilience(
                executor,
                generated_sql,
                database_name,
            )

            execution_time_ms = self._get_current_time_ms() - start_time
            logger.info(
                "SQL executed successfully",
                extra={
                    "request_id": request_id,
                    "row_count": total_count,
                    "execution_time_ms": execution_time_ms,
                },
            )

            # Step 6: Validate results (non-blocking, failures don't fail the request)
            result_confidence = await self._validate_results_safely(
                question=request.question,
                sql=generated_sql,
                results=results,
                row_count=total_count,
                request_id=request_id,
            )

            # Step 7: Build successful response
            query_result = QueryResult(
                columns=list(results[0].keys()) if results else [],
                rows=results,
                row_count=len(results),  # Limited row count (after max_rows applied)
                execution_time_ms=execution_time_ms,
            )

            _metric_status.set("success")
            return QueryResponse(
                success=True,
                generated_sql=generated_sql,
                validation=validation_result,
                data=query_result,
                error=None,
                confidence=result_confidence,
                tokens_used=tokens_used,
            )

        except PgMcpError as e:
            # Handle known application errors
            logger.warning(
                "Query execution failed with known error",
                extra={
                    "request_id": request_id,
                    "error_code": e.code,
                    "error_message": str(e),
                },
            )
            _metric_status.set(self._status_for_error(e.code))
            return QueryResponse(
                success=False,
                generated_sql=None,
                validation=None,
                data=None,
                error=ErrorDetail(
                    code=e.code,
                    message=e.message,
                    details=e.details,
                ),
                confidence=0,
                tokens_used=None,
            )
        except Exception as e:
            # Handle unexpected errors
            logger.exception(
                "Query execution failed with unexpected error",
                extra={"request_id": request_id},
            )
            _metric_status.set("error")
            return QueryResponse(
                success=False,
                generated_sql=None,
                validation=None,
                data=None,
                error=ErrorDetail(
                    code=ErrorCode.INTERNAL_ERROR,
                    message=f"Internal server error: {e!s}",
                    details={"error_type": type(e).__name__},
                ),
                confidence=0,
                tokens_used=None,
            )

    def _resolve_database(self, database: str | None) -> str:
        """Resolve database name from request or auto-select.

        If database is specified, validate it exists.
        If not specified and only one database available, auto-select it.

        Args:
            database: Database name from request (optional).

        Returns:
            str: Resolved database name.

        Raises:
            DatabaseError: If database is invalid or cannot be auto-selected.

        Example:
            >>> name = orchestrator._resolve_database("mydb")  # Validates "mydb" exists
            >>> name = orchestrator._resolve_database(None)  # Auto-selects if only one DB
        """
        if database is not None:
            # Validate specified database exists
            if database not in self.pools:
                raise DatabaseError(
                    message=f"Database '{database}' not found",
                    details={
                        "requested_database": database,
                        "available_databases": list(self.pools.keys()),
                    },
                )
            return database

        # Auto-select if only one database available
        available_dbs = list(self.pools.keys())
        if len(available_dbs) == 0:
            raise DatabaseError(
                message="No databases configured",
                details={},
            )
        if len(available_dbs) == 1:
            return available_dbs[0]

        # Multiple databases, must specify
        raise DatabaseError(
            message="Multiple databases available, please specify which to query",
            details={"available_databases": available_dbs},
        )

    async def _generate_sql_with_retry(
        self,
        question: str,
        schema: Any,
        request_id: str,
    ) -> tuple[str, ValidationResult, int | None]:
        """Generate and validate SQL with retry logic on validation failures.

        This method implements a retry loop that:
        1. Checks circuit breaker state
        2. Generates SQL using LLM
        3. Validates the generated SQL
        4. On validation failure, retries with error feedback
        5. Records success/failure to circuit breaker

        Args:
            question: User's natural language question.
            schema: Database schema for context.
            request_id: Request ID for tracking.

        Returns:
            tuple: (generated_sql, validation_result, tokens_used)

        Raises:
            LLMError: If circuit breaker is open or generation fails.
            SecurityViolationError: If SQL fails validation after all retries.
            SQLParseError: If SQL cannot be parsed.

        Example:
            >>> sql, validation, tokens = await orchestrator._generate_sql_with_retry(
            ...     question="Count users",
            ...     schema=db_schema,
            ...     request_id="123",
            ... )
        """
        # Check circuit breaker
        if not self.circuit_breaker.allow_request():
            raise LLMError(
                message="SQL generation service is temporarily unavailable (circuit breaker open)",
                details={
                    "circuit_state": self.circuit_breaker.state,
                    "failure_count": self.circuit_breaker.failure_count,
                },
            )

        previous_sql: str | None = None
        error_feedback: str | None = None
        max_retries = self.resilience_config.max_retries
        tokens_used: int | None = None

        for attempt in range(max_retries + 1):
            try:
                logger.debug(
                    "Generating SQL",
                    extra={
                        "request_id": request_id,
                        "attempt": attempt + 1,
                        "max_retries": max_retries + 1,
                    },
                )

                # Generate SQL. Transient LLM failures use configured backoff.
                generated_sql = await self._generate_with_resilience(
                    question=question,
                    schema=schema,
                    previous_attempt=previous_sql,
                    error_feedback=error_feedback,
                )
                call_tokens = getattr(self.sql_generator, "last_tokens_used", None)
                if isinstance(call_tokens, int):
                    tokens_used = (tokens_used or 0) + call_tokens

                logger.debug(
                    "SQL generated",
                    extra={
                        "request_id": request_id,
                        "sql_length": len(generated_sql),
                    },
                )

                # Validate SQL
                try:
                    self.sql_validator.validate_or_raise(generated_sql)
                except (SecurityViolationError, SQLParseError) as validation_error:
                    feedback = self._validation_feedback(generated_sql, validation_error)
                    self._record_rejection(validation_error)
                    if attempt < max_retries:
                        # Record as failure and retry with the validator's actual result
                        logger.warning(
                            "SQL validation failed, retrying with feedback",
                            extra={
                                "request_id": request_id,
                                "attempt": attempt + 1,
                                "error": str(validation_error),
                                "is_valid": feedback.is_valid,
                                "blocked_functions": feedback.uses_blocked_functions,
                                "allows_data_modification": feedback.allows_data_modification,
                            },
                        )
                        previous_sql = generated_sql
                        error_feedback = feedback.error_message or str(validation_error)
                        continue
                    else:
                        # Out of retries, record failure and raise
                        self.circuit_breaker.record_failure()
                        logger.error(
                            "SQL validation failed after all retries",
                            extra={
                                "request_id": request_id,
                                "attempts": attempt + 1,
                                "error": str(validation_error),
                                "is_valid": feedback.is_valid,
                                "blocked_functions": feedback.uses_blocked_functions,
                            },
                        )
                        raise

                # Validation successful
                self.circuit_breaker.record_success()
                logger.info(
                    "SQL generated and validated successfully",
                    extra={
                        "request_id": request_id,
                        "attempts": attempt + 1,
                    },
                )

                validation_result = self._validation_feedback(generated_sql, None)

                return generated_sql, validation_result, tokens_used

            except PgMcpError:
                # Keep the original error code (validation, rate limit, LLM, parse).
                raise
            except Exception as e:
                # Unexpected error during generation
                self.circuit_breaker.record_failure()
                logger.exception(
                    "Unexpected error during SQL generation",
                    extra={"request_id": request_id},
                )
                raise LLMError(
                    message=f"SQL generation failed unexpectedly: {e!s}",
                    details={"error_type": type(e).__name__},
                ) from e

        # Should not reach here, but just in case
        self.circuit_breaker.record_failure()
        raise LLMError(
            message="SQL generation failed after all retry attempts",
            details={"max_retries": max_retries},
        )

    async def _validate_results_safely(
        self,
        question: str,
        sql: str,
        results: list[dict[str, Any]],
        row_count: int,
        request_id: str,
    ) -> int:
        """Validate query results with error handling (non-blocking).

        This method attempts to validate results using LLM, but failures
        don't cause the overall query to fail. Returns a confidence score.

        Args:
            question: User's original question.
            sql: Generated SQL query.
            results: Query results.
            row_count: Total row count.
            request_id: Request ID for tracking.

        Returns:
            int: Confidence score (0-100). Returns 100 if validation is disabled.
            Returns 0 when validation itself fails, so a broken check is not
            reported as a perfect result.

        Example:
            >>> confidence = await orchestrator._validate_results_safely(
            ...     question="Count users",
            ...     sql="SELECT COUNT(*) FROM users",
            ...     results=[{"count": 42}],
            ...     row_count=1,
            ...     request_id="123",
            ... )
        """
        if not self.validation_config.enabled:
            return 100

        try:
            logger.debug(
                "Validating results",
                extra={"request_id": request_id},
            )

            validation_result = await self.result_validator.validate(
                question=question,
                sql=sql,
                results=results,
                row_count=row_count,
            )

            required = max(
                self.validation_config.confidence_threshold,
                self.validation_config.min_confidence_score,
            )
            logger.info(
                "Result validation completed",
                extra={
                    "request_id": request_id,
                    "confidence": validation_result.confidence,
                    "is_acceptable": validation_result.is_acceptable,
                    "required_confidence": required,
                },
            )
            if validation_result.confidence < required:
                logger.warning(
                    "Result confidence is below the configured threshold",
                    extra={
                        "request_id": request_id,
                        "confidence": validation_result.confidence,
                        "required_confidence": required,
                    },
                )

            return validation_result.confidence

        except Exception as e:
            # Log but don't fail the query. A failed check is not high confidence.
            logger.warning(
                "Result validation failed, continuing with low confidence",
                extra={
                    "request_id": request_id,
                    "error": str(e),
                    "min_confidence_score": self.validation_config.min_confidence_score,
                },
            )
            return 0

    def _enforce_question_length(self, question: str) -> None:
        """Reject questions longer than ValidationConfig.max_question_length."""
        limit = self.validation_config.max_question_length
        if len(question) > limit:
            raise QuestionTooLongError(
                message=f"Question length {len(question)} exceeds the limit of {limit} characters",
                details={"length": len(question), "max_question_length": limit},
            )

    def _executor_for(self, database_name: str) -> SQLExecutor:
        """Select the executor bound to a database.

        When a per-database map is configured, a request never falls through
        to a different database's executor.

        Args:
            database_name: Resolved database name.

        Returns:
            SQLExecutor: Executor for that database.

        Raises:
            DatabaseError: If no executor is available for the database.
        """
        if database_name in self.sql_executors:
            return self.sql_executors[database_name]
        if self.sql_executors:
            raise DatabaseError(
                message=f"No executor configured for database '{database_name}'",
                details={
                    "database": database_name,
                    "available_databases": list(self.sql_executors.keys()),
                },
            )
        if self.sql_executor is not None:
            return self.sql_executor
        raise DatabaseError(
            message="No SQL executor configured",
            details={"database": database_name},
        )

    async def _generate_with_resilience(
        self,
        *,
        question: str,
        schema: Any,
        previous_attempt: str | None,
        error_feedback: str | None,
    ) -> str:
        """Call the SQL generator under the LLM rate limit and retry policy."""

        async def _once() -> str:
            if self.rate_limiter is None:
                return await self.sql_generator.generate(
                    question=question,
                    schema=schema,
                    previous_attempt=previous_attempt,
                    error_feedback=error_feedback,
                )
            try:
                async with self.rate_limiter.for_llm():
                    return await self.sql_generator.generate(
                        question=question,
                        schema=schema,
                        previous_attempt=previous_attempt,
                        error_feedback=error_feedback,
                    )
            except TimeoutError as exc:
                raise RateLimitExceededError(
                    message="LLM rate limit exceeded",
                    details={"limiter": "llm"},
                ) from exc

        started = time.perf_counter()
        try:
            sql = await retry_async(
                _once,
                max_retries=self.resilience_config.max_retries,
                retry_delay=self.resilience_config.retry_delay,
                backoff_factor=self.resilience_config.backoff_factor,
                retry_on=(LLMTimeoutError, LLMUnavailableError),
            )
        except Exception:
            self._record_llm_metrics(started, include_tokens=False)
            raise
        self._record_llm_metrics(started, include_tokens=True)
        return sql

    def _record_llm_metrics(self, started: float, *, include_tokens: bool) -> None:
        """Record one SQL-generation attempt."""
        if self.metrics is None:
            return
        self.metrics.increment_llm_call("generate_sql")
        self.metrics.observe_llm_latency("generate_sql", time.perf_counter() - started)
        if not include_tokens:
            return
        tokens = getattr(self.sql_generator, "last_tokens_used", None)
        if isinstance(tokens, int):
            self.metrics.increment_llm_tokens("generate_sql", tokens)

    async def _execute_with_resilience(
        self,
        executor: SQLExecutor,
        sql: str,
        database_name: str,
    ) -> tuple[list[dict[str, Any]], int]:
        """Execute SQL under the query rate limit and transient-error retry policy."""

        async def _once() -> tuple[list[dict[str, Any]], int]:
            if self.rate_limiter is None:
                return await executor.execute(sql)
            try:
                async with self.rate_limiter.for_queries():
                    return await executor.execute(sql)
            except TimeoutError as exc:
                raise RateLimitExceededError(
                    message="Query rate limit exceeded",
                    details={"limiter": "query", "database": database_name},
                ) from exc

        started = time.perf_counter()
        try:
            return await retry_async(
                _once,
                max_retries=self.resilience_config.max_retries,
                retry_delay=self.resilience_config.retry_delay,
                backoff_factor=self.resilience_config.backoff_factor,
                retry_on=(DatabaseError,),
                is_retryable=is_transient_database_error,
            )
        finally:
            if self.metrics is not None:
                self.metrics.observe_db_query_duration(time.perf_counter() - started)
                pool = getattr(executor, "pool", None)
                get_size = getattr(pool, "get_size", None)
                get_idle = getattr(pool, "get_idle_size", None)
                if callable(get_size) and callable(get_idle):
                    try:
                        active = int(get_size()) - int(get_idle())
                    except (TypeError, ValueError):
                        active = None
                    if isinstance(active, int):
                        self.metrics.set_db_connections_active(database_name, active)

    def _validation_feedback(self, sql: str, error: Exception | None) -> ValidationResult:
        """Use the validator's own summary when it provides one.

        Test doubles that do not return ValidationResult keep the previous
        success/failure shape so existing unit tests stay stable.
        """
        summarize = getattr(self.sql_validator, "summarize", None)
        if callable(summarize):
            try:
                summary = summarize(sql)
            except Exception:
                summary = None
            if isinstance(summary, ValidationResult):
                if error is not None and summary.is_valid:
                    return summary.model_copy(
                        update={"is_valid": False, "error_message": str(error)}
                    )
                return summary
        if error is not None:
            return ValidationResult(
                is_valid=False,
                is_select=False,
                allows_data_modification=False,
                uses_blocked_functions=[],
                error_message=str(error),
            )
        return ValidationResult(
            is_valid=True,
            is_select=True,
            allows_data_modification=False,
            uses_blocked_functions=[],
            error_message=None,
        )

    def _record_rejection(self, error: Exception) -> None:
        """Count a rejected SQL statement by the reason the validator reported."""
        if self.metrics is None:
            return
        message = str(error).lower()
        if "function" in message:
            reason = "blocked_function"
        elif "column" in message:
            reason = "blocked_column"
        elif "table" in message:
            reason = "blocked_table"
        elif "explain" in message:
            reason = "explain_not_allowed"
        elif isinstance(error, SQLParseError):
            reason = "parse_error"
        else:
            reason = "security_violation"
        self.metrics.increment_sql_rejected(reason)

    @staticmethod
    def _status_for_error(code: ErrorCode) -> str:
        """Map an application error onto a query-request metric status."""
        if code in {
            ErrorCode.SECURITY_VIOLATION,
            ErrorCode.SQL_PARSE_ERROR,
            ErrorCode.VALIDATION_FAILED,
            ErrorCode.QUESTION_TOO_LONG,
        }:
            return "validation_failed"
        if code == ErrorCode.RATE_LIMIT_EXCEEDED:
            return "rate_limited"
        return "error"

    @staticmethod
    def _get_current_time_ms() -> float:
        """Get current time in milliseconds.

        Returns:
            float: Current time in milliseconds since epoch.
        """
        import time

        return time.time() * 1000
