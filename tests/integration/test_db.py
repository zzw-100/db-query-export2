"""Mocked database-layer tests.

These cover pool lifecycle, schema introspection, and session setup without
a live PostgreSQL server or OpenAI account.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pg_mcp.config.settings import DatabaseConfig, SecurityConfig
from pg_mcp.db.introspection import SchemaIntrospector
from pg_mcp.db.pool import close_pools, create_pools
from pg_mcp.services.sql_executor import SQLExecutor


class FakeConnection:
    """Connection that answers introspection queries from in-memory rows."""

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "version()" in query:
            return "PostgreSQL 16.1, compiled by mock"
        if "SELECT EXISTS" in query:
            return args[-1] == "email"
        if "reltuples" in query:
            return 42
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "relkind = 'r'" in query:
            return [
                {"schema_name": "public", "table_name": "users", "comment": "accounts"},
            ]
        if "relkind = 'v'" in query:
            return []
        if "typtype = 'e'" in query:
            return [
                {
                    "schema_name": "public",
                    "type_name": "user_status",
                    "values": ["active", "disabled"],
                }
            ]
        if "format_type" in query:
            return [
                {
                    "column_name": "id",
                    "data_type": "integer",
                    "is_nullable": False,
                    "default_value": None,
                    "comment": None,
                },
                {
                    "column_name": "email",
                    "data_type": "text",
                    "is_nullable": False,
                    "default_value": None,
                    "comment": "login",
                },
            ]
        if "indisunique" in query:
            return [
                {
                    "index_name": "users_email_idx",
                    "is_unique": True,
                    "index_type": "btree",
                    "columns": ["email"],
                }
            ]
        if "indisprimary" in query:
            return [{"column_name": "id"}]
        if "contype = 'f'" in query:
            return [
                {
                    "constraint_name": "users_org_fk",
                    "column_name": "org_id",
                    "referenced_table": "orgs",
                    "referenced_column": "id",
                }
            ]
        return []


class FakePool:
    """Pool whose acquire() yields the fake connection."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False

    def acquire(self) -> "FakePool":
        return self

    async def __aenter__(self) -> FakeConnection:
        return FakeConnection()

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    def terminate(self) -> None:
        self.closed = True

    def get_size(self) -> int:
        return 2

    def get_idle_size(self) -> int:
        return 1


@pytest.mark.asyncio
async def test_create_pools_and_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_pools builds one pool per config and close_pools shuts them down."""
    created: list[str] = []

    async def fake_create_pool(config: DatabaseConfig) -> FakePool:
        created.append(config.name)
        return FakePool(config.name)

    monkeypatch.setattr("pg_mcp.db.pool.create_pool", fake_create_pool)
    configs = [
        DatabaseConfig(name="analytics", host="db-a"),
        DatabaseConfig(name="warehouse", host="db-b"),
    ]

    pools = await create_pools(configs)

    assert created == ["analytics", "warehouse"]
    assert set(pools) == {"analytics", "warehouse"}
    await close_pools(pools, timeout=1.0)
    assert all(pool.closed for pool in pools.values())


@pytest.mark.asyncio
async def test_schema_introspection_assembles_catalog() -> None:
    """Introspection maps catalog rows into a DatabaseSchema."""
    introspector = SchemaIntrospector(FakePool("analytics"), "analytics")  # type: ignore[arg-type]

    schema = await introspector.introspect()

    assert schema.database_name == "analytics"
    assert schema.version is not None
    assert schema.version.startswith("PostgreSQL 16.1")
    users = schema.get_table("users")
    assert users is not None
    assert users.comment == "accounts"
    assert [column.name for column in users.columns] == ["id", "email"]
    assert users.columns[0].is_primary_key is True
    assert users.columns[1].is_unique is True
    assert users.foreign_keys[0].referenced_table == "orgs"
    assert users.indexes[0].name == "users_email_idx"
    assert schema.enum_types[0].type_name == "user_status"
    assert users.row_count_estimate == 42


@pytest.mark.asyncio
async def test_executor_sets_session_parameters() -> None:
    """Execution sets statement_timeout, search_path, and the read-only role."""
    connection = MagicMock()
    connection.execute = AsyncMock()
    connection.fetch = AsyncMock(return_value=[])
    connection.transaction = MagicMock(return_value=connection)
    connection.__aenter__ = AsyncMock(return_value=connection)
    connection.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=connection)

    executor = SQLExecutor(
        pool=pool,
        security_config=SecurityConfig(
            max_execution_time=2.5,
            safe_search_path="public",
            readonly_role="pg_mcp_ro",
            max_rows=10,
        ),
        db_config=DatabaseConfig(name="analytics"),
    )

    results, total = await executor.execute("SELECT 1")

    assert results == []
    assert total == 0
    statements = [call.args[0] for call in connection.execute.await_args_list]
    assert "SET statement_timeout = 2500" in statements
    assert "SET search_path = 'public'" in statements
    assert "SET ROLE pg_mcp_ro" in statements
    connection.transaction.assert_called_once_with(readonly=True)
