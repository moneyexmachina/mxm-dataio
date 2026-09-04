"""PostgreSQL integration tests for packaged DataIO migrations.

These tests execute the packaged migrations against a real disposable
PostgreSQL server provided by Testcontainers, using one isolated schema per
test.

They prove the DataIO interaction-ledger schema contract: expected tables and
PostgreSQL representations, Request-occurrence identity, Session/Request
ownership, Response acquisition provenance, Resolution relationships,
important relational constraints and indexes, migration-ledger state,
non-mutating inspection, idempotent replay, and isolation between test
schemas.

They also prove several architectural invariants encoded by the schema:

- Session owns source and cache/reuse context;
- Request owns one logical-question occurrence and references its Session;
- logical-question hash is not unique across Request occurrences or Sessions;
- Response represents an actual external reply acquired by one Request;
- distinct Responses may reference identical payload identity;
- operational vocabularies are stored rather than authored by PostgreSQL;
- Resolution kind is part of the closed persistence ontology;
- one Request occurrence has at most one final Resolution.

Higher-level DataIO persistence, reuse-policy decisions, and object-store
semantics are tested separately from the migration contract.
"""

from __future__ import annotations

import pytest
from psycopg import errors, sql

from mxm.dataio.sql.migration_runner import MigrationRunner
from mxm.dataio.sql.postgres import PostgresDatabase, PostgresRow

type ExecutableQuery = sql.SQL | sql.Composed

pytestmark = pytest.mark.postgres


_EXPECTED_TABLES = frozenset(
    {
        "schema_migrations",
        "sessions",
        "requests",
        "responses",
        "resolutions",
    }
)


_EXPECTED_INDEXES = frozenset(
    {
        "requests_session_id_idx",
        "requests_hash_created_at_idx",
        "responses_request_id_idx",
        "responses_payload_checksum_idx",
        "responses_fetched_at_idx",
        "resolutions_response_id_idx",
    }
)


# ---------------------------------------------------------------------------
# PostgreSQL inspection helpers
# ---------------------------------------------------------------------------


def _fetch_rows(
    database: PostgresDatabase,
    query: ExecutableQuery,
    parameters: tuple[object, ...] | None = None,
) -> list[PostgresRow]:
    """Execute one inspection query and return all rows."""

    with database.transaction() as connection:
        with connection.cursor() as cursor:
            if parameters is None:
                cursor.execute(query)
            else:
                cursor.execute(
                    query,
                    parameters,
                )

            return cursor.fetchall()


def _table_names(
    database: PostgresDatabase,
    *,
    schema: str,
) -> set[str]:
    """Return table names present in one PostgreSQL schema."""

    rows = _fetch_rows(
        database,
        sql.SQL(
            """
            SELECT tablename
            FROM pg_catalog.pg_tables
            WHERE schemaname = %s
            ORDER BY tablename
            """
        ),
        (schema,),
    )

    return {
        row[0]
        for row in rows
        if isinstance(
            row[0],
            str,
        )
    }


def _column_names(
    database: PostgresDatabase,
    *,
    table: str,
) -> set[str]:
    """Return column names for one table in the migrated schema."""

    rows = _fetch_rows(
        database,
        sql.SQL(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
            ORDER BY ordinal_position
            """
        ),
        (
            database.schema,
            table,
        ),
    )

    return {
        row[0]
        for row in rows
        if isinstance(
            row[0],
            str,
        )
    }


def _constraint_columns(
    database: PostgresDatabase,
    *,
    table: str,
    constraint_type: str,
) -> set[tuple[str, ...]]:
    """Return constrained column tuples for one table and constraint type."""

    rows = _fetch_rows(
        database,
        sql.SQL(
            """
            SELECT
                constraint_definition.oid,
                key_column.ordinality,
                attribute.attname
            FROM pg_catalog.pg_constraint AS constraint_definition
            JOIN pg_catalog.pg_class AS constrained_table
              ON constrained_table.oid = constraint_definition.conrelid
            JOIN pg_catalog.pg_namespace AS constrained_schema
              ON constrained_schema.oid = constrained_table.relnamespace
            JOIN unnest(constraint_definition.conkey)
                 WITH ORDINALITY
                 AS key_column(attnum, ordinality)
              ON TRUE
            JOIN pg_catalog.pg_attribute AS attribute
              ON attribute.attrelid = constrained_table.oid
             AND attribute.attnum = key_column.attnum
            WHERE constrained_schema.nspname = %s
              AND constrained_table.relname = %s
              AND constraint_definition.contype = %s
            ORDER BY
                constraint_definition.oid,
                key_column.ordinality
            """
        ),
        (
            database.schema,
            table,
            constraint_type,
        ),
    )

    columns_by_constraint: dict[int, list[str]] = {}

    for row in rows:
        constraint_oid = row[0]
        ordinality = row[1]
        column = row[2]

        if isinstance(constraint_oid, bool) or not isinstance(
            constraint_oid,
            int,
        ):
            raise AssertionError(
                f"PostgreSQL returned an invalid constraint OID: {constraint_oid!r}"
            )

        if isinstance(ordinality, bool) or not isinstance(
            ordinality,
            int,
        ):
            raise AssertionError(
                f"PostgreSQL returned an invalid constraint ordinality: {ordinality!r}"
            )

        if not isinstance(
            column,
            str,
        ):
            raise AssertionError(
                f"PostgreSQL returned a non-text constraint column: {column!r}"
            )

        columns_by_constraint.setdefault(
            constraint_oid,
            [],
        ).append(column)

    return {tuple(columns) for columns in columns_by_constraint.values()}


def _foreign_keys(
    database: PostgresDatabase,
    *,
    table: str,
) -> set[tuple[tuple[str, ...], str, tuple[str, ...]]]:
    """Return complete foreign-key relationships for one table."""

    rows = _fetch_rows(
        database,
        sql.SQL(
            """
            SELECT
                constraint_definition.oid,
                source_key.ordinality,
                source_attribute.attname,
                referenced_table.relname,
                referenced_attribute.attname
            FROM pg_catalog.pg_constraint AS constraint_definition
            JOIN pg_catalog.pg_class AS source_table
              ON source_table.oid = constraint_definition.conrelid
            JOIN pg_catalog.pg_namespace AS source_schema
              ON source_schema.oid = source_table.relnamespace
            JOIN pg_catalog.pg_class AS referenced_table
              ON referenced_table.oid = constraint_definition.confrelid
            JOIN unnest(constraint_definition.conkey)
                 WITH ORDINALITY
                 AS source_key(attnum, ordinality)
              ON TRUE
            JOIN unnest(constraint_definition.confkey)
                 WITH ORDINALITY
                 AS referenced_key(attnum, ordinality)
              ON referenced_key.ordinality = source_key.ordinality
            JOIN pg_catalog.pg_attribute AS source_attribute
              ON source_attribute.attrelid = source_table.oid
             AND source_attribute.attnum = source_key.attnum
            JOIN pg_catalog.pg_attribute AS referenced_attribute
              ON referenced_attribute.attrelid = referenced_table.oid
             AND referenced_attribute.attnum = referenced_key.attnum
            WHERE source_schema.nspname = %s
              AND source_table.relname = %s
              AND constraint_definition.contype = 'f'
            ORDER BY
                constraint_definition.oid,
                source_key.ordinality
            """
        ),
        (
            database.schema,
            table,
        ),
    )

    relationships: dict[
        int,
        tuple[list[str], str, list[str]],
    ] = {}

    for row in rows:
        constraint_oid = row[0]
        source_column = row[2]
        referenced_table = row[3]
        referenced_column = row[4]

        if isinstance(constraint_oid, bool) or not isinstance(
            constraint_oid,
            int,
        ):
            raise AssertionError(
                f"PostgreSQL returned invalid FK OID: {constraint_oid!r}"
            )

        if (
            not isinstance(
                source_column,
                str,
            )
            or not isinstance(
                referenced_table,
                str,
            )
            or not isinstance(
                referenced_column,
                str,
            )
        ):
            raise AssertionError(
                f"PostgreSQL returned an unexpected foreign-key row: {row!r}"
            )

        existing = relationships.get(
            constraint_oid,
        )

        if existing is None:
            relationships[constraint_oid] = (
                [source_column],
                referenced_table,
                [referenced_column],
            )
        else:
            source_columns, existing_table, referenced_columns = existing

            if existing_table != referenced_table:
                raise AssertionError(
                    "PostgreSQL returned one foreign-key constraint "
                    "referencing multiple tables."
                )

            source_columns.append(
                source_column,
            )
            referenced_columns.append(
                referenced_column,
            )

    return {
        (
            tuple(source_columns),
            referenced_table,
            tuple(referenced_columns),
        )
        for (
            source_columns,
            referenced_table,
            referenced_columns,
        ) in relationships.values()
    }


def _constraint_names(
    database: PostgresDatabase,
    *,
    table: str,
    constraint_type: str,
) -> set[str]:
    """Return names of constraints of one PostgreSQL constraint type."""

    rows = _fetch_rows(
        database,
        sql.SQL(
            """
            SELECT constraint_definition.conname
            FROM pg_catalog.pg_constraint AS constraint_definition
            JOIN pg_catalog.pg_class AS constrained_table
              ON constrained_table.oid = constraint_definition.conrelid
            JOIN pg_catalog.pg_namespace AS constrained_schema
              ON constrained_schema.oid = constrained_table.relnamespace
            WHERE constrained_schema.nspname = %s
              AND constrained_table.relname = %s
              AND constraint_definition.contype = %s
            ORDER BY constraint_definition.conname
            """
        ),
        (
            database.schema,
            table,
            constraint_type,
        ),
    )

    return {
        row[0]
        for row in rows
        if isinstance(
            row[0],
            str,
        )
    }


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------


def test_packaged_migrations_create_expected_dataio_schema(
    postgres_database: PostgresDatabase,
) -> None:
    """Packaged migrations create the complete current DataIO schema."""

    database = postgres_database
    runner = MigrationRunner(
        database,
    )

    assert database.schema.startswith("dataio_test_")
    assert database.schema != "dataio"

    applied_versions = runner.migrate()

    assert applied_versions == [
        "001",
    ]

    assert (
        _table_names(
            database,
            schema=database.schema,
        )
        == _EXPECTED_TABLES
    )

    assert _column_names(
        database,
        table="sessions",
    ) == {
        "id",
        "source",
        "cache_mode",
        "ttl_seconds",
        "as_of_bucket",
        "cache_tag",
        "started_at",
        "ended_at",
    }

    assert _column_names(
        database,
        table="requests",
    ) == {
        "id",
        "session_id",
        "kind",
        "params",
        "hash",
        "created_at",
    }

    assert _column_names(
        database,
        table="responses",
    ) == {
        "id",
        "request_id",
        "status",
        "created_at",
        "fetched_at",
        "payload_checksum",
        "size_bytes",
        "media_type",
        "encoding",
        "elapsed_ms",
        "adapter_meta",
    }

    assert _column_names(
        database,
        table="resolutions",
    ) == {
        "request_id",
        "response_id",
        "kind",
        "resolved_at",
    }

    jsonb_columns = _fetch_rows(
        database,
        sql.SQL(
            """
            SELECT
                table_name,
                column_name
            FROM information_schema.columns
            WHERE table_schema = %s
              AND data_type = 'jsonb'
            ORDER BY
                table_name,
                column_name
            """
        ),
        (database.schema,),
    )

    assert jsonb_columns == [
        (
            "requests",
            "params",
        ),
        (
            "responses",
            "adapter_meta",
        ),
    ]

    obsolete_columns = _fetch_rows(
        database,
        sql.SQL(
            """
            SELECT
                table_name,
                column_name
            FROM information_schema.columns
            WHERE table_schema = %s
              AND column_name IN (
                  'path',
                  'mode',
                  'method',
                  'body',
                  'sequence'
              )
            ORDER BY
                table_name,
                column_name
            """
        ),
        (database.schema,),
    )

    assert obsolete_columns == []


def test_packaged_migrations_create_expected_relational_constraints(
    postgres_database: PostgresDatabase,
) -> None:
    """The migrated schema enforces the DataIO interaction-ledger structure."""

    database = postgres_database

    MigrationRunner(
        database,
    ).migrate()

    assert _constraint_columns(
        database,
        table="sessions",
        constraint_type="p",
    ) == {
        ("id",),
    }

    assert _constraint_columns(
        database,
        table="requests",
        constraint_type="p",
    ) == {
        ("id",),
    }

    assert _constraint_columns(
        database,
        table="responses",
        constraint_type="p",
    ) == {
        ("id",),
    }

    assert _constraint_columns(
        database,
        table="resolutions",
        constraint_type="p",
    ) == {
        ("request_id",),
    }

    # Session identity is its primary key. Source is Session-owned context and
    # does not participate in the Request foreign-key relationship.
    assert (
        "id",
        "source",
    ) not in _constraint_columns(
        database,
        table="sessions",
        constraint_type="u",
    )

    assert _foreign_keys(
        database,
        table="requests",
    ) == {
        (
            ("session_id",),
            "sessions",
            ("id",),
        ),
    }

    assert _foreign_keys(
        database,
        table="responses",
    ) == {
        (
            ("request_id",),
            "requests",
            ("id",),
        ),
    }

    assert _foreign_keys(
        database,
        table="resolutions",
    ) == {
        (
            ("request_id",),
            "requests",
            ("id",),
        ),
        (
            ("response_id",),
            "responses",
            ("id",),
        ),
    }

    # Logical-question identity H is deliberately not unique. Multiple Request
    # occurrences, including Requests in different Sessions, may share H.
    assert ("hash",) not in _constraint_columns(
        database,
        table="requests",
        constraint_type="u",
    )

    # Payload identity is also deliberately not unique. Distinct external
    # Response observations may contain byte-for-byte identical payloads.
    assert ("payload_checksum",) not in _constraint_columns(
        database,
        table="responses",
        constraint_type="u",
    )

    assert "resolutions_kind_valid" in _constraint_names(
        database,
        table="resolutions",
        constraint_type="c",
    )

    index_rows = _fetch_rows(
        database,
        sql.SQL(
            """
            SELECT indexname
            FROM pg_catalog.pg_indexes
            WHERE schemaname = %s
            ORDER BY indexname
            """
        ),
        (database.schema,),
    )

    actual_indexes = {
        row[0]
        for row in index_rows
        if isinstance(
            row[0],
            str,
        )
    }

    assert _EXPECTED_INDEXES <= actual_indexes


# ---------------------------------------------------------------------------
# Request occurrence / logical-question identity
# ---------------------------------------------------------------------------


def test_equivalent_questions_can_be_distinct_occurrences_across_sessions(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """The same logical-question hash may occur in different Session contexts."""

    database = migrated_postgres_database

    with database.transaction() as connection:
        with connection.cursor() as cursor:
            session_insert = sql.SQL(
                """
                INSERT INTO {} (
                    id,
                    source,
                    cache_mode,
                    ttl_seconds,
                    as_of_bucket,
                    cache_tag,
                    started_at
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    CURRENT_TIMESTAMP
                )
                """
            ).format(
                sql.Identifier(
                    database.schema,
                    "sessions",
                )
            )

            cursor.execute(
                session_insert,
                (
                    "session-1",
                    "source-a",
                    "default",
                    300.0,
                    "bucket-a",
                    "vendor-v1",
                ),
            )

            cursor.execute(
                session_insert,
                (
                    "session-2",
                    "source-b",
                    "bypass",
                    None,
                    "bucket-b",
                    "vendor-v2",
                ),
            )

            request_insert = sql.SQL(
                """
                INSERT INTO {} (
                    id,
                    session_id,
                    kind,
                    params,
                    hash,
                    created_at
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    CURRENT_TIMESTAMP
                )
                """
            ).format(
                sql.Identifier(
                    database.schema,
                    "requests",
                )
            )

            question_hash = "a" * 64

            cursor.execute(
                request_insert,
                (
                    "request-1",
                    "session-1",
                    "example",
                    None,
                    question_hash,
                ),
            )

            cursor.execute(
                request_insert,
                (
                    "request-2",
                    "session-2",
                    "example",
                    None,
                    question_hash,
                ),
            )

    rows = _fetch_rows(
        database,
        sql.SQL(
            """
            SELECT
                id,
                session_id,
                hash
            FROM {}
            ORDER BY id
            """
        ).format(
            sql.Identifier(
                database.schema,
                "requests",
            )
        ),
    )

    assert rows == [
        (
            "request-1",
            "session-1",
            "a" * 64,
        ),
        (
            "request-2",
            "session-2",
            "a" * 64,
        ),
    ]


def test_request_must_reference_existing_session(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Every Request occurrence must belong to one persisted Session."""

    database = migrated_postgres_database

    with pytest.raises(
        errors.ForeignKeyViolation,
    ):
        with database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        """
                        INSERT INTO {} (
                            id,
                            session_id,
                            kind,
                            hash,
                            created_at
                        )
                        VALUES (
                            %s,
                            %s,
                            %s,
                            %s,
                            CURRENT_TIMESTAMP
                        )
                        """
                    ).format(
                        sql.Identifier(
                            database.schema,
                            "requests",
                        )
                    ),
                    (
                        "request-1",
                        "missing-session",
                        "example",
                        "a" * 64,
                    ),
                )


# ---------------------------------------------------------------------------
# Response / payload identity
# ---------------------------------------------------------------------------


def test_distinct_responses_can_share_payload_identity(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Distinct external observations may reference identical payload bytes."""

    database = migrated_postgres_database

    with database.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {} (
                        id,
                        source,
                        cache_mode,
                        started_at
                    )
                    VALUES (
                        'session-1',
                        'example-source',
                        'default',
                        CURRENT_TIMESTAMP
                    )
                    """
                ).format(
                    sql.Identifier(
                        database.schema,
                        "sessions",
                    )
                )
            )

            request_insert = sql.SQL(
                """
                INSERT INTO {} (
                    id,
                    session_id,
                    kind,
                    hash,
                    created_at
                )
                VALUES (
                    %s,
                    'session-1',
                    'example',
                    %s,
                    CURRENT_TIMESTAMP
                )
                """
            ).format(
                sql.Identifier(
                    database.schema,
                    "requests",
                )
            )

            cursor.execute(
                request_insert,
                (
                    "request-1",
                    "a" * 64,
                ),
            )

            cursor.execute(
                request_insert,
                (
                    "request-2",
                    "a" * 64,
                ),
            )

            response_insert = sql.SQL(
                """
                INSERT INTO {} (
                    id,
                    request_id,
                    status,
                    created_at,
                    fetched_at,
                    payload_checksum,
                    size_bytes
                )
                VALUES (
                    %s,
                    %s,
                    'ok',
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP,
                    %s,
                    %s
                )
                """
            ).format(
                sql.Identifier(
                    database.schema,
                    "responses",
                )
            )

            payload_checksum = "b" * 64

            cursor.execute(
                response_insert,
                (
                    "response-1",
                    "request-1",
                    payload_checksum,
                    12,
                ),
            )

            cursor.execute(
                response_insert,
                (
                    "response-2",
                    "request-2",
                    payload_checksum,
                    12,
                ),
            )

    rows = _fetch_rows(
        database,
        sql.SQL(
            """
            SELECT
                id,
                request_id,
                payload_checksum
            FROM {}
            ORDER BY id
            """
        ).format(
            sql.Identifier(
                database.schema,
                "responses",
            )
        ),
    )

    assert rows == [
        (
            "response-1",
            "request-1",
            "b" * 64,
        ),
        (
            "response-2",
            "request-2",
            "b" * 64,
        ),
    ]


# ---------------------------------------------------------------------------
# Persistence vocabulary
# ---------------------------------------------------------------------------


def test_operational_vocabularies_are_not_authored_by_sql(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """PostgreSQL stores open behavioural vocabulary without enumerating it."""

    database = migrated_postgres_database

    with database.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {} (
                        id,
                        source,
                        cache_mode,
                        started_at
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        CURRENT_TIMESTAMP
                    )
                    """
                ).format(
                    sql.Identifier(
                        database.schema,
                        "sessions",
                    )
                ),
                (
                    "session-open-vocabulary",
                    "source-open-vocabulary",
                    "future-cache-policy",
                ),
            )

            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {} (
                        id,
                        session_id,
                        kind,
                        hash,
                        created_at
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        %s,
                        CURRENT_TIMESTAMP
                    )
                    """
                ).format(
                    sql.Identifier(
                        database.schema,
                        "requests",
                    )
                ),
                (
                    "request-open-vocabulary",
                    "session-open-vocabulary",
                    "future-kind",
                    "b" * 64,
                ),
            )

            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {} (
                        id,
                        request_id,
                        status,
                        created_at,
                        fetched_at,
                        payload_checksum,
                        size_bytes
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP,
                        %s,
                        %s
                    )
                    """
                ).format(
                    sql.Identifier(
                        database.schema,
                        "responses",
                    )
                ),
                (
                    "response-open-vocabulary",
                    "request-open-vocabulary",
                    "future-response-status",
                    "c" * 64,
                    0,
                ),
            )


def test_resolution_kind_is_closed_persistence_ontology(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Resolution kind accepts only the ontology defined by DataIO."""

    database = migrated_postgres_database

    with database.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {} (
                        id,
                        source,
                        cache_mode,
                        started_at
                    )
                    VALUES (
                        'session-1',
                        'example-source',
                        'default',
                        CURRENT_TIMESTAMP
                    )
                    """
                ).format(
                    sql.Identifier(
                        database.schema,
                        "sessions",
                    )
                )
            )

            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {} (
                        id,
                        session_id,
                        kind,
                        hash,
                        created_at
                    )
                    VALUES (
                        'request-1',
                        'session-1',
                        'example',
                        %s,
                        CURRENT_TIMESTAMP
                    )
                    """
                ).format(
                    sql.Identifier(
                        database.schema,
                        "requests",
                    )
                ),
                ("d" * 64,),
            )

            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {} (
                        id,
                        request_id,
                        status,
                        created_at,
                        fetched_at,
                        payload_checksum,
                        size_bytes
                    )
                    VALUES (
                        'response-1',
                        'request-1',
                        'ok',
                        CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP,
                        %s,
                        0
                    )
                    """
                ).format(
                    sql.Identifier(
                        database.schema,
                        "responses",
                    )
                ),
                ("e" * 64,),
            )

    with pytest.raises(
        errors.CheckViolation,
    ):
        with database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        """
                        INSERT INTO {} (
                            request_id,
                            response_id,
                            kind,
                            resolved_at
                        )
                        VALUES (
                            'request-1',
                            'response-1',
                            'invented-third-kind',
                            CURRENT_TIMESTAMP
                        )
                        """
                    ).format(
                        sql.Identifier(
                            database.schema,
                            "resolutions",
                        )
                    )
                )


# ---------------------------------------------------------------------------
# Migration lifecycle
# ---------------------------------------------------------------------------


def test_migration_state_is_versioned_current_and_idempotent(
    postgres_database: PostgresDatabase,
) -> None:
    """Migration ledger, inspection, replay, and schema isolation are correct."""

    database = postgres_database
    runner = MigrationRunner(
        database,
    )

    discovered = {migration.version: migration for migration in runner.discover()}

    assert set(discovered) == {
        "000",
        "001",
    }

    initial_migration = discovered["001"]

    first_applied_versions = runner.migrate()

    assert first_applied_versions == [
        "001",
    ]

    ledger_rows = _fetch_rows(
        database,
        sql.SQL(
            """
            SELECT
                version,
                checksum
            FROM {}
            ORDER BY version
            """
        ).format(
            sql.Identifier(
                database.schema,
                "schema_migrations",
            )
        ),
    )

    assert ledger_rows == [
        (
            "001",
            initial_migration.checksum,
        ),
    ]

    inspection = runner.inspect()

    assert inspection.initialised is True
    assert inspection.applied_versions == ("001",)
    assert inspection.pending_versions == ()
    assert inspection.current is True

    replay_applied_versions = runner.migrate()

    assert replay_applied_versions == []

    replay_ledger_rows = _fetch_rows(
        database,
        sql.SQL(
            """
            SELECT
                version,
                checksum
            FROM {}
            ORDER BY version
            """
        ).format(
            sql.Identifier(
                database.schema,
                "schema_migrations",
            )
        ),
    )

    assert replay_ledger_rows == ledger_rows

    dataio_tables = _table_names(
        database,
        schema="dataio",
    )

    assert dataio_tables.isdisjoint(
        _EXPECTED_TABLES,
    )

    public_tables = _table_names(
        database,
        schema="public",
    )

    assert public_tables.isdisjoint(
        _EXPECTED_TABLES,
    )
