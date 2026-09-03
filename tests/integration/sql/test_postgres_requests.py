"""PostgreSQL integration tests for DataIO request SQL/schema compatibility.

These tests exercise the real migrated PostgreSQL schema. They prove that the
request SQL adapter matches that schema, that multiple request occurrences may
share one logical request hash, that session/source relational integrity is
enforced, and that request JSON and conflict semantics behave correctly through
real PostgreSQL.

Detailed SQL construction, row validation, logical-identity computation, and
transaction non-ownership are tested separately by the request unit tests.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from psycopg.errors import ForeignKeyViolation

from mxm.dataio.models import (
    CacheMode,
    Request,
    RequestMethod,
    Session,
    SessionMode,
)
from mxm.dataio.sql.postgres import PostgresDatabase
from mxm.dataio.sql.requests import (
    RequestConflictError,
    fetch_request_by_id,
    fetch_requests_by_hash,
    insert_request,
)
from mxm.dataio.sql.sessions import insert_session
from mxm.types import JSONLike, JSONObj

pytestmark = pytest.mark.postgres


def _session(
    *,
    session_id: str,
    source: str = "test-source",
    started_at: datetime | None = None,
) -> Session:
    """Construct one deterministic parent DataIO session."""

    return Session(
        id=session_id,
        source=source,
        mode=SessionMode.SYNC,
        started_at=(
            started_at
            if started_at is not None
            else datetime(
                2026,
                9,
                2,
                10,
                0,
                tzinfo=UTC,
            )
        ),
    )


def _request(
    *,
    request_id: str,
    session_id: str,
    source: str = "test-source",
    kind: str = "prices",
    cache_mode: CacheMode = CacheMode.DEFAULT,
    method: RequestMethod = RequestMethod.GET,
    params: JSONObj | None = None,
    body: JSONLike | None = None,
    created_at: datetime | None = None,
    ttl_seconds: float | None = 300.0,
    as_of_bucket: str | None = None,
    cache_tag: str | None = None,
) -> Request:
    """Construct one deterministic request occurrence."""

    return Request(
        id=request_id,
        session_id=session_id,
        source=source,
        kind=kind,
        cache_mode=cache_mode,
        method=method,
        params=params,
        body=body,
        created_at=(
            created_at
            if created_at is not None
            else datetime(
                2026,
                9,
                2,
                10,
                5,
                tzinfo=UTC,
            )
        ),
        ttl_seconds=ttl_seconds,
        as_of_bucket=as_of_bucket,
        cache_tag=cache_tag,
    )


def test_requests_round_trip_through_postgres(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Complete request state round-trips through the migrated schema."""

    database = migrated_postgres_database

    session = _session(
        session_id="session-round-trip",
    )

    request = _request(
        request_id="request-round-trip",
        session_id=session.id,
        method=RequestMethod.POST,
        cache_mode=CacheMode.REVALIDATE,
        params={
            "symbol": "ES",
            "fields": [
                "open",
                "close",
            ],
        },
        body={
            "options": {
                "adjusted": False,
            },
        },
        ttl_seconds=600.0,
        as_of_bucket="2026-09-02T10",
        cache_tag="vendor-v1",
    )

    with database.transaction() as connection:
        insert_session(
            connection,
            schema=database.schema,
            session=session,
        )

        insert_request(
            connection,
            schema=database.schema,
            request=request,
        )

    with database.transaction() as connection:
        persisted_request = fetch_request_by_id(
            connection,
            schema=database.schema,
            request_id=request.id,
        )

        missing_request = fetch_request_by_id(
            connection,
            schema=database.schema,
            request_id="missing-request",
        )

    assert persisted_request == request
    assert missing_request is None


def test_equivalent_logical_requests_persist_as_distinct_occurrences(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Distinct Q occurrences sharing one H are independently persisted."""

    database = migrated_postgres_database

    session = _session(
        session_id="session-shared-hash",
    )

    first = _request(
        request_id="request-1",
        session_id=session.id,
        cache_mode=CacheMode.DEFAULT,
        ttl_seconds=300.0,
        created_at=datetime(
            2026,
            9,
            2,
            10,
            5,
            tzinfo=UTC,
        ),
        params={
            "symbol": "ES",
        },
    )

    second = _request(
        request_id="request-2",
        session_id=session.id,
        cache_mode=CacheMode.BYPASS,
        ttl_seconds=None,
        created_at=datetime(
            2026,
            9,
            2,
            10,
            6,
            tzinfo=UTC,
        ),
        params={
            "symbol": "ES",
        },
    )

    assert first.id != second.id
    assert first.hash == second.hash

    with database.transaction() as connection:
        insert_session(
            connection,
            schema=database.schema,
            session=session,
        )

        insert_request(
            connection,
            schema=database.schema,
            request=first,
        )

        insert_request(
            connection,
            schema=database.schema,
            request=second,
        )

    with database.transaction() as connection:
        persisted_requests = fetch_requests_by_hash(
            connection,
            schema=database.schema,
            request_hash=first.hash,
        )

    assert persisted_requests == {
        first.id: first,
        second.id: second,
    }


def test_request_source_must_match_session_source(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """The real composite foreign key enforces session/request source identity."""

    database = migrated_postgres_database

    session = _session(
        session_id="session-source-integrity",
        source="source-a",
    )

    valid_request = _request(
        request_id="request-valid-source",
        session_id=session.id,
        source="source-a",
        params={
            "symbol": "ES",
        },
    )

    invalid_request = _request(
        request_id="request-invalid-source",
        session_id=session.id,
        source="source-b",
        params={
            "symbol": "ES",
        },
    )

    with database.transaction() as connection:
        insert_session(
            connection,
            schema=database.schema,
            session=session,
        )

        insert_request(
            connection,
            schema=database.schema,
            request=valid_request,
        )

    with pytest.raises(ForeignKeyViolation):
        with database.transaction() as connection:
            insert_request(
                connection,
                schema=database.schema,
                request=invalid_request,
            )

    with database.transaction() as connection:
        persisted_valid = fetch_request_by_id(
            connection,
            schema=database.schema,
            request_id=valid_request.id,
        )

        persisted_invalid = fetch_request_by_id(
            connection,
            schema=database.schema,
            request_id=invalid_request.id,
        )

    assert persisted_valid == valid_request
    assert persisted_invalid is None


def test_request_persistence_is_idempotent_and_rejects_identity_conflicts(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Identical Q state is idempotent while conflicting Q state is rejected."""

    database = migrated_postgres_database

    session = _session(
        session_id="session-request-conflict",
    )

    request = _request(
        request_id="request-stable-identity",
        session_id=session.id,
        params={
            "symbol": "ES",
        },
    )

    with database.transaction() as connection:
        insert_session(
            connection,
            schema=database.schema,
            session=session,
        )

        insert_request(
            connection,
            schema=database.schema,
            request=request,
        )

    # Replaying the identical occurrence is allowed.
    with database.transaction() as connection:
        insert_request(
            connection,
            schema=database.schema,
            request=request,
        )

    conflicting_request = replace(
        request,
        kind="settlements",
    )

    with pytest.raises(
        RequestConflictError,
        match=r"Persisted request conflicts.*request-stable-identity",
    ):
        with database.transaction() as connection:
            insert_request(
                connection,
                schema=database.schema,
                request=conflicting_request,
            )

    with database.transaction() as connection:
        persisted_request = fetch_request_by_id(
            connection,
            schema=database.schema,
            request_id=request.id,
        )

    assert persisted_request == request


def test_request_json_round_trips_through_postgres(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Nested JSON values survive Psycopg/PostgreSQL JSONB round-tripping."""

    database = migrated_postgres_database

    session = _session(
        session_id="session-json",
    )

    request = _request(
        request_id="request-json",
        session_id=session.id,
        params={
            "symbol": "ES",
            "fields": [
                "open",
                "high",
                "low",
                "close",
            ],
            "options": {
                "adjusted": False,
                "limit": 10,
            },
        },
        body={
            "nested": [
                1,
                "two",
                True,
                None,
                {
                    "depth": 2,
                },
            ],
        },
    )

    with database.transaction() as connection:
        insert_session(
            connection,
            schema=database.schema,
            session=session,
        )

        insert_request(
            connection,
            schema=database.schema,
            request=request,
        )

    with database.transaction() as connection:
        persisted_request = fetch_request_by_id(
            connection,
            schema=database.schema,
            request_id=request.id,
        )

    assert persisted_request == request
    assert persisted_request is not None
    assert persisted_request.params == request.params
    assert persisted_request.body == request.body
