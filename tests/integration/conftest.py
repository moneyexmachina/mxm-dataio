"""Shared fixtures for disposable PostgreSQL integration tests.

These fixtures provide a real PostgreSQL engine using Testcontainers and one
uniquely named disposable PostgreSQL schema per test.

The integration-test environment is deliberately independent of MXM runtime
configuration, secrets, machine identity, and operational infrastructure.
mxm-dataio is a library package: its component integration tests prove that its
PostgreSQL implementation works against a real PostgreSQL engine, not that an
MXM application can resolve and compose the operational database.

Application-level acceptance of runtime configuration, secrets resolution, and
real MXM infrastructure belongs in application packages such as
mxm-moneymachine.

Tests never operate on the package's conventional ``dataio`` schema.
"""

from __future__ import annotations

import re
from collections.abc import Generator
from uuid import uuid4

import pytest
from psycopg import sql
from testcontainers.community.postgres import PostgresContainer

from mxm.dataio.sql.migration_runner import MigrationRunner
from mxm.dataio.sql.postgres import PostgresDatabase

_POSTGRES_IMAGE = "postgres:16"

_TEST_SCHEMA_PATTERN = re.compile(
    r"^dataio_test_[0-9a-f]{12}$",
    flags=re.ASCII,
)


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer]:
    """Provide one disposable PostgreSQL server for the test session.

    The container owns an isolated PostgreSQL instance created solely for the
    current test session. ``driver=None`` requests a native PostgreSQL URL
    suitable for Psycopg 3 rather than a SQLAlchemy driver-qualified URL.
    """

    with PostgresContainer(
        _POSTGRES_IMAGE,
        driver=None,
    ) as container:
        yield container


@pytest.fixture(scope="session")
def configured_postgres_database(
    postgres_container: PostgresContainer,
) -> PostgresDatabase:
    """Provide a PostgreSQL boundary backed by the disposable test server.

    This boundary is only a connection target from which individual tests
    derive isolated schema boundaries. Tests never create or modify the
    conventional ``dataio`` schema itself.
    """

    database = PostgresDatabase(
        postgres_container.get_connection_url(),
        schema="dataio",
    )

    if not database.check_connection():
        raise RuntimeError(
            "Disposable PostgreSQL test container failed connectivity check."
        )

    return database


@pytest.fixture
def postgres_database(
    configured_postgres_database: PostgresDatabase,
) -> Generator[PostgresDatabase]:
    """Provide an unmigrated database boundary for one disposable schema.

    Each test receives a unique schema on the session-scoped disposable
    PostgreSQL server. The schema does not exist when yielded, allowing
    migration tests to prove creation from an empty starting point.

    The schema is dropped during teardown, including after test failures.
    """

    schema = f"dataio_test_{uuid4().hex[:12]}"

    _require_safe_test_schema(schema)

    database = configured_postgres_database.with_schema(schema)

    try:
        yield database
    finally:
        _drop_test_schema(database)


@pytest.fixture
def migrated_postgres_database(
    postgres_database: PostgresDatabase,
) -> PostgresDatabase:
    """Provide a disposable PostgreSQL schema with all migrations applied."""

    runner = MigrationRunner(postgres_database)

    applied_versions = runner.migrate()

    if "001" not in applied_versions:
        raise RuntimeError(
            "Expected initial DataIO migration '001' to be applied to a "
            "new disposable PostgreSQL schema, "
            f"got {applied_versions!r}."
        )

    return postgres_database


def _require_safe_test_schema(
    schema: str,
) -> None:
    """Reject any schema name outside the generated test-schema form."""

    if _TEST_SCHEMA_PATTERN.fullmatch(schema) is None:
        raise RuntimeError(
            "Refusing to operate on an unsafe PostgreSQL integration-test "
            f"schema: {schema!r}."
        )

    if schema == "dataio":
        raise RuntimeError("Refusing to operate on the conventional dataio schema.")


def _drop_test_schema(
    database: PostgresDatabase,
) -> None:
    """Drop only the validated disposable schema owned by one test."""

    _require_safe_test_schema(database.schema)

    query = sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
        sql.Identifier(database.schema)
    )

    with database.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query)
