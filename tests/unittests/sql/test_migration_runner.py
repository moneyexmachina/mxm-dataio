"""Unit tests for PostgreSQL schema migration discovery and execution.

TODO(architecture):
    This test module intentionally duplicates the generic PostgreSQL migration
    runner tests from mxm-refdata for ED005. Once MigrationRunner is extracted
    into shared infrastructure (likely mxm-infra), move these generic tests
    with it. Keep DataIO-specific migration/schema integration tests local to
    mxm-dataio.
"""

from __future__ import annotations

import hashlib
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import cast

import pytest
from psycopg import sql

from mxm.dataio.sql.migration_runner import (
    Migration,
    MigrationChecksumMismatchError,
    MigrationDiscoveryError,
    MigrationRunner,
    MigrationStateError,
    _render_migration_sql,
)
from mxm.dataio.sql.postgres import PostgresDatabase

_BOOTSTRAP_SQL = """
CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.schema_migrations (
    version text PRIMARY KEY,
    checksum text NOT NULL
);
""".strip()

_INITIAL_MIGRATION_SQL = """
CREATE TABLE {schema}.example (
    example_id text PRIMARY KEY
);
""".strip()

_SECOND_MIGRATION_SQL = """
ALTER TABLE {schema}.example
    ADD COLUMN description text;
""".strip()

_THIRD_MIGRATION_SQL = """
CREATE INDEX example_description_index
    ON {schema}.example (description);
""".strip()


@dataclass(frozen=True, slots=True)
class Execution:
    """One SQL execution recorded by a fake cursor."""

    query: object
    parameters: object | None


class FakeCursor:
    """Minimal cursor implementing the migration runner's required surface."""

    def __init__(
        self,
        *,
        rows: list[tuple[object, ...]] | None = None,
        fail_on_execution: int | None = None,
        execution_error: BaseException | None = None,
    ) -> None:
        """Configure query rows and an optional execution failure."""

        self.rows = rows if rows is not None else []
        self.fail_on_execution = fail_on_execution
        self.execution_error = execution_error

        self.executions: list[Execution] = []
        self.entered = False
        self.exited = False

    def __enter__(self) -> FakeCursor:
        """Enter the cursor context."""

        self.entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Exit the cursor context."""

        del exc_type, exc_value, traceback
        self.exited = True

    def execute(
        self,
        query: object,
        parameters: object | None = None,
    ) -> None:
        """Record one SQL execution and optionally raise an error."""

        self.executions.append(
            Execution(
                query=query,
                parameters=parameters,
            )
        )

        execution_number = len(self.executions)

        if (
            self.fail_on_execution == execution_number
            and self.execution_error is not None
        ):
            raise self.execution_error

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return the configured query result rows."""

        return list(self.rows)


class FakeConnection:
    """Minimal connection exposing one fake cursor."""

    def __init__(
        self,
        *,
        rows: list[tuple[object, ...]] | None = None,
        fail_on_execution: int | None = None,
        execution_error: BaseException | None = None,
    ) -> None:
        """Configure the connection's cursor behavior."""

        self.fake_cursor = FakeCursor(
            rows=rows,
            fail_on_execution=fail_on_execution,
            execution_error=execution_error,
        )

    def cursor(self) -> FakeCursor:
        """Return the connection's configured cursor."""

        return self.fake_cursor


class FakePostgresDatabase:
    """Fake transaction provider used by migration-runner unit tests."""

    def __init__(
        self,
        connections: list[FakeConnection],
        *,
        schema: str = "dataio",
    ) -> None:
        """Configure ordered connections and the owned schema name."""

        self.schema = schema
        self.connections = connections
        self.transaction_calls = 0
        self.used_connections: list[FakeConnection] = []

    @contextmanager
    def transaction(self) -> Generator[FakeConnection]:
        """Yield the next configured connection."""

        connection_index = self.transaction_calls
        self.transaction_calls += 1

        if connection_index >= len(self.connections):
            raise AssertionError(
                "Migration runner requested more transactions than expected"
            )

        connection = self.connections[connection_index]
        self.used_connections.append(connection)

        yield connection


def _migration_resources(
    *resources: tuple[str, str],
) -> list[tuple[str, str]]:
    """Return migration resources in the supplied order."""

    return list(resources)


def _make_runner(
    resources: list[tuple[str, str]],
    connections: list[FakeConnection],
    *,
    schema: str = "dataio",
) -> tuple[MigrationRunner, FakePostgresDatabase]:
    """Construct a migration runner with in-memory resources and database."""

    database = FakePostgresDatabase(
        connections,
        schema=schema,
    )

    runner = MigrationRunner(
        cast(PostgresDatabase, database),
        migration_loader=lambda: resources,
    )

    return runner, database


def _make_migration(
    filename: str,
    sql_text: str,
) -> Migration:
    """Construct a migration using production validation."""

    return Migration.from_resource(
        filename,
        sql_text,
    )


def _query_text(query: object) -> str:
    """Render a recorded SQL query as text for focused assertions."""

    if isinstance(query, sql.Composable):
        return query.as_string()

    if isinstance(query, str):
        return query

    raise AssertionError(f"Unexpected recorded query type: {type(query)!r}")


def _cursor_for(
    database: FakePostgresDatabase,
    transaction_index: int,
) -> FakeCursor:
    """Return the cursor used by a given transaction."""

    return database.used_connections[transaction_index].fake_cursor


# ---------------------------------------------------------------------
# MIGRATION CONSTRUCTION
# ---------------------------------------------------------------------


def test_migration_from_resource_extracts_identity() -> None:
    migration = _make_migration(
        "001_initial_dataio.sql",
        _INITIAL_MIGRATION_SQL,
    )

    assert migration.version == "001"
    assert migration.name == "initial_dataio"
    assert migration.filename == "001_initial_dataio.sql"
    assert migration.sql_text == _INITIAL_MIGRATION_SQL
    assert migration.is_bootstrap is False


def test_bootstrap_migration_is_identified() -> None:
    migration = _make_migration(
        "000_bootstrap.sql",
        _BOOTSTRAP_SQL,
    )

    assert migration.version == "000"
    assert migration.name == "bootstrap"
    assert migration.is_bootstrap is True


def test_migration_checksum_uses_exact_unrendered_source() -> None:
    expected_checksum = hashlib.sha256(
        _INITIAL_MIGRATION_SQL.encode("utf-8")
    ).hexdigest()

    migration = _make_migration(
        "001_initial_dataio.sql",
        _INITIAL_MIGRATION_SQL,
    )

    assert migration.checksum == expected_checksum


def test_migration_checksum_changes_when_source_changes() -> None:
    original = _make_migration(
        "001_initial_dataio.sql",
        _INITIAL_MIGRATION_SQL,
    )
    changed = _make_migration(
        "001_initial_dataio.sql",
        f"{_INITIAL_MIGRATION_SQL}\n-- changed",
    )

    assert changed.checksum != original.checksum


@pytest.mark.parametrize(
    "filename",
    [
        "initial_dataio.sql",
        "01_initial_dataio.sql",
        "0001_initial_dataio.sql",
        "001-incorrect.sql",
        "001_InitialDataio.sql",
        "001_.sql",
        "001_initial-dataio.sql",
        "001_initial_dataio.txt",
    ],
)
def test_migration_rejects_invalid_filename(
    filename: str,
) -> None:
    with pytest.raises(
        MigrationDiscoveryError,
        match=r"Invalid migration filename",
    ):
        _make_migration(
            filename,
            _INITIAL_MIGRATION_SQL,
        )


@pytest.mark.parametrize(
    "sql_text",
    [
        "",
        "   ",
        "\n\t",
    ],
)
def test_migration_rejects_empty_source(
    sql_text: str,
) -> None:
    with pytest.raises(
        MigrationDiscoveryError,
        match=r"contains no SQL",
    ):
        _make_migration(
            "001_initial_dataio.sql",
            sql_text,
        )


def test_migration_requires_schema_placeholder() -> None:
    with pytest.raises(
        MigrationDiscoveryError,
        match=r"required.*schema",
    ):
        _make_migration(
            "001_initial_dataio.sql",
            "CREATE TABLE example (id integer);",
        )


# ---------------------------------------------------------------------
# DISCOVERY
# ---------------------------------------------------------------------


def test_discover_returns_migrations_in_version_order() -> None:
    resources = _migration_resources(
        ("002_second.sql", _SECOND_MIGRATION_SQL),
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        ("001_initial.sql", _INITIAL_MIGRATION_SQL),
    )
    runner, _ = _make_runner(
        resources,
        connections=[],
    )

    migrations = runner.discover()

    assert [migration.filename for migration in migrations] == [
        "000_bootstrap.sql",
        "001_initial.sql",
        "002_second.sql",
    ]


def test_discover_rejects_empty_resource_set() -> None:
    runner, _ = _make_runner(
        [],
        connections=[],
    )

    with pytest.raises(
        MigrationDiscoveryError,
        match=r"No packaged PostgreSQL migrations",
    ):
        runner.discover()


def test_discover_requires_bootstrap() -> None:
    resources = _migration_resources(
        ("001_initial.sql", _INITIAL_MIGRATION_SQL),
    )
    runner, _ = _make_runner(
        resources,
        connections=[],
    )

    with pytest.raises(
        MigrationDiscoveryError,
        match=r"000_bootstrap\.sql",
    ):
        runner.discover()


def test_discover_rejects_duplicate_bootstrap() -> None:
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
    )
    runner, _ = _make_runner(
        resources,
        connections=[],
    )

    with pytest.raises(
        MigrationDiscoveryError,
        match=r"Exactly one.*bootstrap",
    ):
        runner.discover()


def test_discover_rejects_duplicate_versions() -> None:
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        ("001_first.sql", _INITIAL_MIGRATION_SQL),
        ("001_second.sql", _SECOND_MIGRATION_SQL),
    )
    runner, _ = _make_runner(
        resources,
        connections=[],
    )

    with pytest.raises(
        MigrationDiscoveryError,
        match=r"Duplicate migration version.*001",
    ):
        runner.discover()


def test_discover_reserves_version_zero_for_bootstrap() -> None:
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        ("000_other.sql", _INITIAL_MIGRATION_SQL),
    )
    runner, _ = _make_runner(
        resources,
        connections=[],
    )

    with pytest.raises(
        MigrationDiscoveryError,
        match=r"Duplicate migration version|reserved for bootstrap",
    ):
        runner.discover()


# ---------------------------------------------------------------------
# SCHEMA RENDERING
# ---------------------------------------------------------------------


def test_render_migration_sql_quotes_configured_schema() -> None:
    migration = _make_migration(
        "001_initial.sql",
        ("CREATE TABLE {schema}.example (id integer); SELECT * FROM {schema}.example;"),
    )

    rendered = _render_migration_sql(
        migration,
        schema="dataio_test_abc123",
    )
    rendered_text = rendered.as_string()

    assert '"dataio_test_abc123".example' in rendered_text
    assert rendered_text.count('"dataio_test_abc123"') == 2
    assert "{schema}" not in rendered_text


def test_render_migration_sql_preserves_unrelated_braces() -> None:
    source = """
CREATE TABLE {schema}.example (
    payload jsonb DEFAULT '{"key": "value"}'::jsonb
);
-- unrelated marker: {other}
""".strip()
    migration = _make_migration(
        "001_initial.sql",
        source,
    )

    rendered = _render_migration_sql(
        migration,
        schema="dataio",
    )
    rendered_text = rendered.as_string()

    assert '{"key": "value"}' in rendered_text
    assert "{other}" in rendered_text
    assert "{schema}" not in rendered_text


def test_rendering_does_not_change_migration_identity() -> None:
    migration = _make_migration(
        "001_initial.sql",
        _INITIAL_MIGRATION_SQL,
    )
    original_checksum = migration.checksum
    original_source = migration.sql_text

    _render_migration_sql(
        migration,
        schema="dataio_test_one",
    )
    _render_migration_sql(
        migration,
        schema="dataio_test_two",
    )

    assert migration.checksum == original_checksum
    assert migration.sql_text == original_source


# ---------------------------------------------------------------------
# BOOTSTRAP
# ---------------------------------------------------------------------


def test_migrate_runs_bootstrap_before_ledger_read() -> None:
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        ("001_initial.sql", _INITIAL_MIGRATION_SQL),
    )
    connections = [
        FakeConnection(),
        FakeConnection(rows=[]),
        FakeConnection(),
    ]
    runner, database = _make_runner(
        resources,
        connections,
    )

    runner.migrate()

    bootstrap_cursor = _cursor_for(database, 0)
    ledger_cursor = _cursor_for(database, 1)

    assert len(bootstrap_cursor.executions) == 1
    assert "CREATE SCHEMA" in _query_text(bootstrap_cursor.executions[0].query)

    assert len(ledger_cursor.executions) == 1
    assert "SELECT version, checksum" in _query_text(ledger_cursor.executions[0].query)


def test_bootstrap_runs_in_its_own_transaction() -> None:
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        ("001_initial.sql", _INITIAL_MIGRATION_SQL),
    )
    connections = [
        FakeConnection(),
        FakeConnection(rows=[]),
        FakeConnection(),
    ]
    runner, database = _make_runner(
        resources,
        connections,
    )

    runner.migrate()

    assert database.transaction_calls == 3
    assert len(database.used_connections) == 3

    bootstrap_executions = _cursor_for(
        database,
        0,
    ).executions
    ledger_executions = _cursor_for(
        database,
        1,
    ).executions

    assert len(bootstrap_executions) == 1
    assert len(ledger_executions) == 1


def test_bootstrap_is_not_recorded_in_migration_ledger() -> None:
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        ("001_initial.sql", _INITIAL_MIGRATION_SQL),
    )
    connections = [
        FakeConnection(),
        FakeConnection(rows=[]),
        FakeConnection(),
    ]
    runner, database = _make_runner(
        resources,
        connections,
    )

    runner.migrate()

    recorded_parameters = [
        execution.parameters
        for connection in database.used_connections
        for execution in connection.fake_cursor.executions
        if execution.parameters is not None
    ]

    assert ("000",) not in recorded_parameters
    assert all(
        not (isinstance(parameters, tuple) and parameters and parameters[0] == "000")
        for parameters in recorded_parameters
    )


def test_bootstrap_failure_stops_migration_processing() -> None:
    expected_error = RuntimeError("bootstrap failed")
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        ("001_initial.sql", _INITIAL_MIGRATION_SQL),
    )
    connections = [
        FakeConnection(
            fail_on_execution=1,
            execution_error=expected_error,
        ),
        FakeConnection(rows=[]),
        FakeConnection(),
    ]
    runner, database = _make_runner(
        resources,
        connections,
    )

    with pytest.raises(RuntimeError) as exc_info:
        runner.migrate()

    assert exc_info.value is expected_error
    assert database.transaction_calls == 1
    assert len(database.used_connections) == 1


# ---------------------------------------------------------------------
# APPLIED MIGRATION STATE
# ---------------------------------------------------------------------


def test_matching_applied_migration_is_skipped() -> None:
    migration = _make_migration(
        "001_initial.sql",
        _INITIAL_MIGRATION_SQL,
    )
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        (migration.filename, migration.sql_text),
    )
    connections = [
        FakeConnection(),
        FakeConnection(
            rows=[
                (
                    migration.version,
                    migration.checksum,
                )
            ]
        ),
    ]
    runner, database = _make_runner(
        resources,
        connections,
    )

    applied_versions = runner.migrate()

    assert applied_versions == []
    assert database.transaction_calls == 2
    assert len(database.used_connections) == 2


def test_checksum_mismatch_is_rejected() -> None:
    migration = _make_migration(
        "001_initial.sql",
        _INITIAL_MIGRATION_SQL,
    )
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        (migration.filename, migration.sql_text),
    )
    connections = [
        FakeConnection(),
        FakeConnection(
            rows=[
                (
                    migration.version,
                    "f" * 64,
                )
            ]
        ),
    ]
    runner, database = _make_runner(
        resources,
        connections,
    )

    with pytest.raises(
        MigrationChecksumMismatchError,
        match=r"Checksum mismatch.*001_initial\.sql",
    ):
        runner.migrate()

    assert database.transaction_calls == 2


def test_checksum_mismatch_stops_pending_migrations() -> None:
    migration_001 = _make_migration(
        "001_initial.sql",
        _INITIAL_MIGRATION_SQL,
    )
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        (migration_001.filename, migration_001.sql_text),
        ("002_second.sql", _SECOND_MIGRATION_SQL),
    )
    connections = [
        FakeConnection(),
        FakeConnection(
            rows=[
                (
                    migration_001.version,
                    "f" * 64,
                )
            ]
        ),
        FakeConnection(),
    ]
    runner, database = _make_runner(
        resources,
        connections,
    )

    with pytest.raises(MigrationChecksumMismatchError):
        runner.migrate()

    assert database.transaction_calls == 2
    assert len(database.used_connections) == 2


def test_unknown_applied_migration_is_rejected() -> None:
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        ("001_initial.sql", _INITIAL_MIGRATION_SQL),
    )
    connections = [
        FakeConnection(),
        FakeConnection(
            rows=[
                (
                    "009",
                    "a" * 64,
                )
            ]
        ),
    ]
    runner, _ = _make_runner(
        resources,
        connections,
    )

    with pytest.raises(
        MigrationStateError,
        match=r"absent from the package.*009",
    ):
        runner.migrate()


@pytest.mark.parametrize(
    ("row", "error_match"),
    [
        (("001",), r"unexpected row shape"),
        (
            ("001", "a" * 64, "extra"),
            r"unexpected row shape",
        ),
        ((1, "a" * 64), r"version must be text"),
        (("001", 123), r"checksum must be text"),
        (("1", "a" * 64), r"invalid version"),
        (("001", "not-a-checksum"), r"invalid checksum"),
    ],
)
def test_invalid_migration_ledger_row_is_rejected(
    row: tuple[object, ...],
    error_match: str,
) -> None:
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        ("001_initial.sql", _INITIAL_MIGRATION_SQL),
    )
    connections = [
        FakeConnection(),
        FakeConnection(rows=[row]),
    ]
    runner, _ = _make_runner(
        resources,
        connections,
    )

    with pytest.raises(
        MigrationStateError,
        match=error_match,
    ):
        runner.migrate()


def test_duplicate_ledger_version_is_rejected() -> None:
    migration = _make_migration(
        "001_initial.sql",
        _INITIAL_MIGRATION_SQL,
    )
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        (migration.filename, migration.sql_text),
    )
    connections = [
        FakeConnection(),
        FakeConnection(
            rows=[
                (
                    migration.version,
                    migration.checksum,
                ),
                (
                    migration.version,
                    migration.checksum,
                ),
            ]
        ),
    ]
    runner, _ = _make_runner(
        resources,
        connections,
    )

    with pytest.raises(
        MigrationStateError,
        match=r"duplicate version.*001",
    ):
        runner.migrate()


# ---------------------------------------------------------------------
# PENDING MIGRATION APPLICATION
# ---------------------------------------------------------------------


def test_pending_migration_is_executed_and_recorded_atomically() -> None:
    migration = _make_migration(
        "001_initial.sql",
        _INITIAL_MIGRATION_SQL,
    )
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        (migration.filename, migration.sql_text),
    )
    connections = [
        FakeConnection(),
        FakeConnection(rows=[]),
        FakeConnection(),
    ]
    runner, database = _make_runner(
        resources,
        connections,
    )

    applied_versions = runner.migrate()

    assert applied_versions == ["001"]
    assert database.transaction_calls == 3

    migration_cursor = _cursor_for(
        database,
        2,
    )

    assert len(migration_cursor.executions) == 2

    migration_execution = migration_cursor.executions[0]
    ledger_execution = migration_cursor.executions[1]

    assert "CREATE TABLE" in _query_text(migration_execution.query)
    assert migration_execution.parameters is None

    assert "INSERT INTO" in _query_text(ledger_execution.query)
    assert "schema_migrations" in _query_text(ledger_execution.query)
    assert ledger_execution.parameters == (
        migration.version,
        migration.checksum,
    )


def test_pending_migration_uses_configured_schema() -> None:
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        ("001_initial.sql", _INITIAL_MIGRATION_SQL),
    )
    connections = [
        FakeConnection(),
        FakeConnection(rows=[]),
        FakeConnection(),
    ]
    runner, database = _make_runner(
        resources,
        connections,
        schema="dataio_test_abc123",
    )

    runner.migrate()

    bootstrap_query = _query_text(
        _cursor_for(
            database,
            0,
        )
        .executions[0]
        .query
    )
    migration_query = _query_text(
        _cursor_for(
            database,
            2,
        )
        .executions[0]
        .query
    )
    ledger_insert_query = _query_text(
        _cursor_for(
            database,
            2,
        )
        .executions[1]
        .query
    )

    assert '"dataio_test_abc123"' in bootstrap_query
    assert '"dataio_test_abc123"' in migration_query
    assert '"dataio_test_abc123"' in ledger_insert_query
    assert "{schema}" not in migration_query


def test_multiple_pending_migrations_apply_in_version_order() -> None:
    migration_001 = _make_migration(
        "001_initial.sql",
        _INITIAL_MIGRATION_SQL,
    )
    migration_002 = _make_migration(
        "002_second.sql",
        _SECOND_MIGRATION_SQL,
    )
    resources = _migration_resources(
        (migration_002.filename, migration_002.sql_text),
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        (migration_001.filename, migration_001.sql_text),
    )
    connections = [
        FakeConnection(),
        FakeConnection(rows=[]),
        FakeConnection(),
        FakeConnection(),
    ]
    runner, database = _make_runner(
        resources,
        connections,
    )

    applied_versions = runner.migrate()

    assert applied_versions == ["001", "002"]
    assert database.transaction_calls == 4

    first_migration_cursor = _cursor_for(
        database,
        2,
    )
    second_migration_cursor = _cursor_for(
        database,
        3,
    )

    assert first_migration_cursor.executions[1].parameters == (
        migration_001.version,
        migration_001.checksum,
    )
    assert second_migration_cursor.executions[1].parameters == (
        migration_002.version,
        migration_002.checksum,
    )


def test_applied_migration_is_skipped_before_pending_migration() -> None:
    migration_001 = _make_migration(
        "001_initial.sql",
        _INITIAL_MIGRATION_SQL,
    )
    migration_002 = _make_migration(
        "002_second.sql",
        _SECOND_MIGRATION_SQL,
    )
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        (migration_001.filename, migration_001.sql_text),
        (migration_002.filename, migration_002.sql_text),
    )
    connections = [
        FakeConnection(),
        FakeConnection(
            rows=[
                (
                    migration_001.version,
                    migration_001.checksum,
                )
            ]
        ),
        FakeConnection(),
    ]
    runner, database = _make_runner(
        resources,
        connections,
    )

    applied_versions = runner.migrate()

    assert applied_versions == ["002"]
    assert database.transaction_calls == 3

    pending_cursor = _cursor_for(
        database,
        2,
    )
    assert pending_cursor.executions[1].parameters == (
        migration_002.version,
        migration_002.checksum,
    )


# ---------------------------------------------------------------------
# FAILURE BEHAVIOUR
# ---------------------------------------------------------------------


def test_failed_migration_sql_is_not_recorded() -> None:
    expected_error = RuntimeError("migration failed")
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        ("001_initial.sql", _INITIAL_MIGRATION_SQL),
    )
    connections = [
        FakeConnection(),
        FakeConnection(rows=[]),
        FakeConnection(
            fail_on_execution=1,
            execution_error=expected_error,
        ),
    ]
    runner, database = _make_runner(
        resources,
        connections,
    )

    with pytest.raises(RuntimeError) as exc_info:
        runner.migrate()

    assert exc_info.value is expected_error
    assert database.transaction_calls == 3

    migration_cursor = _cursor_for(
        database,
        2,
    )

    assert len(migration_cursor.executions) == 1
    assert migration_cursor.executions[0].parameters is None


def test_failed_migration_stops_later_migrations() -> None:
    expected_error = RuntimeError("second migration failed")
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        ("001_initial.sql", _INITIAL_MIGRATION_SQL),
        ("002_second.sql", _SECOND_MIGRATION_SQL),
        ("003_third.sql", _THIRD_MIGRATION_SQL),
    )
    connections = [
        FakeConnection(),
        FakeConnection(rows=[]),
        FakeConnection(),
        FakeConnection(
            fail_on_execution=1,
            execution_error=expected_error,
        ),
        FakeConnection(),
    ]
    runner, database = _make_runner(
        resources,
        connections,
    )

    with pytest.raises(RuntimeError) as exc_info:
        runner.migrate()

    assert exc_info.value is expected_error
    assert database.transaction_calls == 4
    assert len(database.used_connections) == 4

    first_migration_cursor = _cursor_for(
        database,
        2,
    )
    failed_migration_cursor = _cursor_for(
        database,
        3,
    )

    assert len(first_migration_cursor.executions) == 2
    assert len(failed_migration_cursor.executions) == 1


def test_failed_ledger_insert_stops_migration_processing() -> None:
    expected_error = RuntimeError("ledger insert failed")
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        ("001_initial.sql", _INITIAL_MIGRATION_SQL),
        ("002_second.sql", _SECOND_MIGRATION_SQL),
    )
    connections = [
        FakeConnection(),
        FakeConnection(rows=[]),
        FakeConnection(
            fail_on_execution=2,
            execution_error=expected_error,
        ),
        FakeConnection(),
    ]
    runner, database = _make_runner(
        resources,
        connections,
    )

    with pytest.raises(RuntimeError) as exc_info:
        runner.migrate()

    assert exc_info.value is expected_error
    assert database.transaction_calls == 3
    assert len(database.used_connections) == 3

    migration_cursor = _cursor_for(
        database,
        2,
    )

    assert len(migration_cursor.executions) == 2
    assert migration_cursor.executions[1].parameters is not None


# ---------------------------------------------------------------------
# MIGRATION INSPECTION
# ---------------------------------------------------------------------


def test_inspect_reports_uninitialised_schema() -> None:
    """An absent schema is reported as normal uninitialised state."""

    migration_001 = _make_migration(
        "001_initial.sql",
        _INITIAL_MIGRATION_SQL,
    )
    migration_002 = _make_migration(
        "002_second.sql",
        _SECOND_MIGRATION_SQL,
    )
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        (migration_001.filename, migration_001.sql_text),
        (migration_002.filename, migration_002.sql_text),
    )
    connections = [
        FakeConnection(
            rows=[
                (
                    False,
                    False,
                )
            ]
        ),
    ]
    runner, database = _make_runner(
        resources,
        connections,
    )

    inspection = runner.inspect()

    assert inspection.initialised is False
    assert inspection.current is False
    assert inspection.packaged_versions == (
        "001",
        "002",
    )
    assert inspection.applied_versions == ()
    assert inspection.pending_versions == (
        "001",
        "002",
    )

    assert database.transaction_calls == 1
    assert len(database.used_connections) == 1


def test_inspect_reports_current_schema() -> None:
    """A fully migrated schema is reported as current."""

    migration_001 = _make_migration(
        "001_initial.sql",
        _INITIAL_MIGRATION_SQL,
    )
    migration_002 = _make_migration(
        "002_second.sql",
        _SECOND_MIGRATION_SQL,
    )
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        (migration_001.filename, migration_001.sql_text),
        (migration_002.filename, migration_002.sql_text),
    )
    connections = [
        FakeConnection(
            rows=[
                (
                    True,
                    True,
                )
            ]
        ),
        FakeConnection(
            rows=[
                (
                    migration_001.version,
                    migration_001.checksum,
                ),
                (
                    migration_002.version,
                    migration_002.checksum,
                ),
            ]
        ),
    ]
    runner, database = _make_runner(
        resources,
        connections,
    )

    inspection = runner.inspect()

    assert inspection.initialised is True
    assert inspection.current is True
    assert inspection.packaged_versions == (
        "001",
        "002",
    )
    assert inspection.applied_versions == (
        "001",
        "002",
    )
    assert inspection.pending_versions == ()

    assert database.transaction_calls == 2


def test_inspect_reports_pending_migrations() -> None:
    """Applied and pending versions are reported in packaged order."""

    migration_001 = _make_migration(
        "001_initial.sql",
        _INITIAL_MIGRATION_SQL,
    )
    migration_002 = _make_migration(
        "002_second.sql",
        _SECOND_MIGRATION_SQL,
    )
    migration_003 = _make_migration(
        "003_third.sql",
        _THIRD_MIGRATION_SQL,
    )
    resources = _migration_resources(
        (migration_003.filename, migration_003.sql_text),
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        (migration_001.filename, migration_001.sql_text),
        (migration_002.filename, migration_002.sql_text),
    )
    connections = [
        FakeConnection(
            rows=[
                (
                    True,
                    True,
                )
            ]
        ),
        FakeConnection(
            rows=[
                (
                    migration_001.version,
                    migration_001.checksum,
                )
            ]
        ),
    ]
    runner, database = _make_runner(
        resources,
        connections,
    )

    inspection = runner.inspect()

    assert inspection.initialised is True
    assert inspection.current is False
    assert inspection.packaged_versions == (
        "001",
        "002",
        "003",
    )
    assert inspection.applied_versions == ("001",)
    assert inspection.pending_versions == (
        "002",
        "003",
    )

    assert database.transaction_calls == 2


def test_inspect_does_not_modify_migration_state() -> None:
    """Migration inspection performs only read operations."""

    migration_001 = _make_migration(
        "001_initial.sql",
        _INITIAL_MIGRATION_SQL,
    )
    migration_002 = _make_migration(
        "002_second.sql",
        _SECOND_MIGRATION_SQL,
    )
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        (migration_001.filename, migration_001.sql_text),
        (migration_002.filename, migration_002.sql_text),
    )
    connections = [
        FakeConnection(
            rows=[
                (
                    True,
                    True,
                )
            ]
        ),
        FakeConnection(
            rows=[
                (
                    migration_001.version,
                    migration_001.checksum,
                )
            ]
        ),
    ]
    runner, database = _make_runner(
        resources,
        connections,
    )

    runner.inspect()

    assert database.transaction_calls == 2

    bootstrap_inspection_cursor = _cursor_for(
        database,
        0,
    )
    ledger_cursor = _cursor_for(
        database,
        1,
    )

    assert len(bootstrap_inspection_cursor.executions) == 1
    assert len(ledger_cursor.executions) == 1

    bootstrap_query = _query_text(bootstrap_inspection_cursor.executions[0].query)
    ledger_query = _query_text(ledger_cursor.executions[0].query)

    assert "information_schema.schemata" in bootstrap_query
    assert "information_schema.tables" in bootstrap_query
    assert bootstrap_inspection_cursor.executions[0].parameters == (
        "dataio",
        "dataio",
    )

    assert "SELECT version, checksum" in ledger_query
    assert ledger_cursor.executions[0].parameters is None

    all_query_text = "\n".join(
        _query_text(execution.query)
        for connection in database.used_connections
        for execution in connection.fake_cursor.executions
    )

    assert "CREATE SCHEMA" not in all_query_text
    assert "CREATE TABLE" not in all_query_text
    assert "ALTER TABLE" not in all_query_text
    assert "INSERT INTO" not in all_query_text
    assert "DROP " not in all_query_text


@pytest.mark.parametrize(
    ("schema_exists", "ledger_exists"),
    [
        (
            True,
            False,
        ),
        (
            False,
            True,
        ),
    ],
)
def test_inspect_rejects_inconsistent_bootstrap_state(
    schema_exists: bool,
    ledger_exists: bool,
) -> None:
    """Partial bootstrap state is treated as migration corruption."""

    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        ("001_initial.sql", _INITIAL_MIGRATION_SQL),
    )
    connections = [
        FakeConnection(
            rows=[
                (
                    schema_exists,
                    ledger_exists,
                )
            ]
        ),
    ]
    runner, database = _make_runner(
        resources,
        connections,
    )

    with pytest.raises(
        MigrationStateError,
        match=r"bootstrap state is inconsistent",
    ):
        runner.inspect()

    assert database.transaction_calls == 1


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [
            (
                True,
                True,
            ),
            (
                True,
                True,
            ),
        ],
    ],
)
def test_inspect_rejects_unexpected_bootstrap_result_count(
    rows: list[tuple[object, ...]],
) -> None:
    """Bootstrap inspection must return exactly one PostgreSQL row."""

    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        ("001_initial.sql", _INITIAL_MIGRATION_SQL),
    )
    connections = [
        FakeConnection(
            rows=rows,
        ),
    ]
    runner, _ = _make_runner(
        resources,
        connections,
    )

    with pytest.raises(
        MigrationStateError,
        match=r"unexpected number of rows",
    ):
        runner.inspect()


@pytest.mark.parametrize(
    "row",
    [
        (True,),
        (
            True,
            True,
            False,
        ),
    ],
)
def test_inspect_rejects_unexpected_bootstrap_row_shape(
    row: tuple[object, ...],
) -> None:
    """Bootstrap inspection must return schema and ledger existence only."""

    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        ("001_initial.sql", _INITIAL_MIGRATION_SQL),
    )
    connections = [
        FakeConnection(
            rows=[
                row,
            ]
        ),
    ]
    runner, _ = _make_runner(
        resources,
        connections,
    )

    with pytest.raises(
        MigrationStateError,
        match=r"unexpected row shape",
    ):
        runner.inspect()


@pytest.mark.parametrize(
    "row",
    [
        (
            1,
            True,
        ),
        (
            True,
            1,
        ),
        (
            "true",
            True,
        ),
        (
            True,
            None,
        ),
    ],
)
def test_inspect_rejects_non_boolean_bootstrap_state(
    row: tuple[object, ...],
) -> None:
    """Bootstrap catalogue observations must be genuine boolean values."""

    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        ("001_initial.sql", _INITIAL_MIGRATION_SQL),
    )
    connections = [
        FakeConnection(
            rows=[
                row,
            ]
        ),
    ]
    runner, _ = _make_runner(
        resources,
        connections,
    )

    with pytest.raises(
        MigrationStateError,
        match=r"must be boolean",
    ):
        runner.inspect()


def test_inspect_validates_applied_migration_checksums() -> None:
    """Inspection validates ledger identity before reporting migration state."""

    migration = _make_migration(
        "001_initial.sql",
        _INITIAL_MIGRATION_SQL,
    )
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        (migration.filename, migration.sql_text),
    )
    connections = [
        FakeConnection(
            rows=[
                (
                    True,
                    True,
                )
            ]
        ),
        FakeConnection(
            rows=[
                (
                    migration.version,
                    "f" * 64,
                )
            ]
        ),
    ]
    runner, database = _make_runner(
        resources,
        connections,
    )

    with pytest.raises(
        MigrationChecksumMismatchError,
        match=r"Checksum mismatch.*001_initial\.sql",
    ):
        runner.inspect()

    assert database.transaction_calls == 2


# ---------------------------------------------------------------------
# IDEMPOTENT REPLAY
# ---------------------------------------------------------------------


def test_migrate_is_noop_when_all_migrations_are_applied() -> None:
    migration_001 = _make_migration(
        "001_initial.sql",
        _INITIAL_MIGRATION_SQL,
    )
    migration_002 = _make_migration(
        "002_second.sql",
        _SECOND_MIGRATION_SQL,
    )
    resources = _migration_resources(
        ("000_bootstrap.sql", _BOOTSTRAP_SQL),
        (migration_001.filename, migration_001.sql_text),
        (migration_002.filename, migration_002.sql_text),
    )
    connections = [
        FakeConnection(),
        FakeConnection(
            rows=[
                (
                    migration_001.version,
                    migration_001.checksum,
                ),
                (
                    migration_002.version,
                    migration_002.checksum,
                ),
            ]
        ),
    ]
    runner, database = _make_runner(
        resources,
        connections,
    )

    applied_versions = runner.migrate()

    assert applied_versions == []
    assert database.transaction_calls == 2
    assert len(database.used_connections) == 2

    bootstrap_cursor = _cursor_for(
        database,
        0,
    )
    ledger_cursor = _cursor_for(
        database,
        1,
    )

    assert len(bootstrap_cursor.executions) == 1
    assert len(ledger_cursor.executions) == 1
