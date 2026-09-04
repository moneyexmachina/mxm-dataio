"""PostgreSQL integration tests for DataIO Session persistence.

These tests exercise the real migrated PostgreSQL schema and Psycopg boundary.

They prove that:

- canonical MXM Session timestamps round-trip through PostgreSQL timestamptz;
- complete Session cache context survives persistence;
- latest-Session selection has the intended PostgreSQL ordering semantics;
- Session persistence is idempotent while identity conflicts are rejected;
- Session completion is monotonic and durable.

Detailed SQL construction, malformed-row handling, timestamp boundary
validation, and transaction non-ownership are tested separately by the
Session unit tests.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from mxm.dataio.models import CacheMode, Session
from mxm.dataio.sql.postgres import PostgresDatabase
from mxm.dataio.sql.sessions import (
    SessionConflictError,
    fetch_latest_session_id,
    fetch_session_by_id,
    insert_session,
    mark_session_ended,
)
from mxm.types.timestamps import TSNSScalar, ts_ns_from_str

pytestmark = pytest.mark.postgres


def _session(
    *,
    session_id: str,
    started_at: TSNSScalar,
    source: str = "test-source",
    cache_mode: CacheMode = CacheMode.DEFAULT,
    ttl_seconds: float | None = 300.0,
    as_of_bucket: str | None = None,
    cache_tag: str | None = None,
    ended_at: TSNSScalar | None = None,
) -> Session:
    """Construct one deterministic DataIO Session."""

    return Session(
        id=session_id,
        source=source,
        cache_mode=cache_mode,
        ttl_seconds=ttl_seconds,
        as_of_bucket=as_of_bucket,
        cache_tag=cache_tag,
        started_at=started_at,
        ended_at=ended_at,
    )


def test_sessions_round_trip_through_postgres(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Complete Session state survives the real PostgreSQL boundary."""

    database = migrated_postgres_database

    session = _session(
        session_id="session-round-trip",
        source="example-source",
        cache_mode=CacheMode.ONLY_IF_CACHED,
        ttl_seconds=600.0,
        as_of_bucket="2026-09-03",
        cache_tag="vendor-v2",
        started_at=ts_ns_from_str("2026-09-03T09:00:00.123456000Z"),
        ended_at=ts_ns_from_str("2026-09-03T09:30:00.654321000Z"),
    )

    with database.transaction() as connection:
        insert_session(
            connection,
            schema=database.schema,
            session=session,
        )

    with database.transaction() as connection:
        persisted_session = fetch_session_by_id(
            connection,
            schema=database.schema,
            session_id=session.id,
        )

        missing_session = fetch_session_by_id(
            connection,
            schema=database.schema,
            session_id="missing-session",
        )

    assert persisted_session == session
    assert missing_session is None


def test_latest_session_selection_uses_postgres_ordering(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Latest-Session lookup is source-specific and deterministically ordered."""

    database = migrated_postgres_database

    early = _session(
        session_id="session-early",
        source="source-a",
        started_at=ts_ns_from_str("2026-09-03T08:00:00.000000000Z"),
    )

    same_time_a = _session(
        session_id="session-a",
        source="source-a",
        started_at=ts_ns_from_str("2026-09-03T10:00:00.000000000Z"),
    )

    same_time_b = _session(
        session_id="session-b",
        source="source-a",
        started_at=ts_ns_from_str("2026-09-03T10:00:00.000000000Z"),
    )

    other_source = _session(
        session_id="session-other-source",
        source="source-b",
        started_at=ts_ns_from_str("2026-09-03T11:00:00.000000000Z"),
    )

    with database.transaction() as connection:
        for session in (
            same_time_a,
            early,
            other_source,
            same_time_b,
        ):
            insert_session(
                connection,
                schema=database.schema,
                session=session,
            )

    with database.transaction() as connection:
        latest_source_a = fetch_latest_session_id(
            connection,
            schema=database.schema,
            source="source-a",
        )

        latest_source_b = fetch_latest_session_id(
            connection,
            schema=database.schema,
            source="source-b",
        )

        missing_source = fetch_latest_session_id(
            connection,
            schema=database.schema,
            source="missing-source",
        )

    # The two source-a Sessions have identical started_at values.
    # The persistence query deliberately breaks the tie by ID descending.
    assert latest_source_a == same_time_b.id
    assert latest_source_b == other_source.id
    assert missing_source is None


def test_session_persistence_is_idempotent_and_rejects_context_conflicts(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Identical Session state is idempotent; cache context is immutable."""

    database = migrated_postgres_database

    session = _session(
        session_id="session-stable-identity",
        source="source-a",
        cache_mode=CacheMode.DEFAULT,
        ttl_seconds=300.0,
        as_of_bucket="2026-09-03",
        cache_tag="vendor-v1",
        started_at=ts_ns_from_str("2026-09-03T09:00:00.000000000Z"),
    )

    with database.transaction() as connection:
        insert_session(
            connection,
            schema=database.schema,
            session=session,
        )

    # Replaying the identical persisted fact is allowed.
    with database.transaction() as connection:
        insert_session(
            connection,
            schema=database.schema,
            session=session,
        )

    conflicting_session = replace(
        session,
        cache_mode=CacheMode.BYPASS,
    )

    with pytest.raises(
        SessionConflictError,
        match=r"Persisted Session conflicts.*session-stable-identity",
    ):
        with database.transaction() as connection:
            insert_session(
                connection,
                schema=database.schema,
                session=conflicting_session,
            )

    with database.transaction() as connection:
        persisted_session = fetch_session_by_id(
            connection,
            schema=database.schema,
            session_id=session.id,
        )

    assert persisted_session == session


def test_session_completion_is_persisted_and_monotonic(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Completion mutates lifecycle state without changing cache context."""

    database = migrated_postgres_database

    session = _session(
        session_id="session-completion",
        source="source-a",
        cache_mode=CacheMode.ONLY_IF_CACHED,
        ttl_seconds=None,
        as_of_bucket="2026-09-03",
        cache_tag="vendor-v1",
        started_at=ts_ns_from_str("2026-09-03T09:00:00.000000000Z"),
    )

    ended_at = ts_ns_from_str("2026-09-03T09:30:00.123456000Z")

    with database.transaction() as connection:
        insert_session(
            connection,
            schema=database.schema,
            session=session,
        )

    with database.transaction() as connection:
        mark_session_ended(
            connection,
            schema=database.schema,
            session_id=session.id,
            ended_at=ended_at,
        )

    with database.transaction() as connection:
        persisted_session = fetch_session_by_id(
            connection,
            schema=database.schema,
            session_id=session.id,
        )

    assert persisted_session == replace(
        session,
        ended_at=ended_at,
    )

    # Repeating the same historical fact is an idempotent no-op.
    with database.transaction() as connection:
        mark_session_ended(
            connection,
            schema=database.schema,
            session_id=session.id,
            ended_at=ended_at,
        )

    different_end = ts_ns_from_str("2026-09-03T09:31:00.123456000Z")

    with pytest.raises(
        SessionConflictError,
        match=r"already has a different completion time",
    ):
        with database.transaction() as connection:
            mark_session_ended(
                connection,
                schema=database.schema,
                session_id=session.id,
                ended_at=different_end,
            )

    with database.transaction() as connection:
        persisted_after_conflict = fetch_session_by_id(
            connection,
            schema=database.schema,
            session_id=session.id,
        )

    assert persisted_after_conflict == replace(
        session,
        ended_at=ended_at,
    )
