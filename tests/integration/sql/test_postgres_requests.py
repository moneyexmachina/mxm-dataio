"""PostgreSQL integration tests for DataIO Request persistence.

These tests exercise the real migrated PostgreSQL schema and Psycopg boundary.

They prove that:

- complete Request state round-trips through PostgreSQL;
- multiple Request occurrences may share one logical-question hash across
  different resolution contexts;
- Request persistence has no parent grouping dependency;
- Request JSON params survive PostgreSQL JSONB persistence;
- Request identity is idempotent while conflicting occurrence state is rejected;
- canonical MXM timestamps survive the real PostgreSQL timestamp boundary.

Detailed SQL construction, malformed-row handling, logical-question hash
validation, timestamp-boundary validation, and transaction non-ownership are
tested separately by the Request unit tests.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from mxm.dataio.models import (
    CacheMode,
    Request,
)
from mxm.dataio.sql.postgres import PostgresDatabase
from mxm.dataio.sql.requests import (
    RequestConflictError,
    fetch_request_by_id,
    fetch_requests_by_hash,
    insert_request,
)
from mxm.types import JSONObj
from mxm.types.timestamps import (
    TSNSScalar,
    ts_ns_from_str,
)

pytestmark = pytest.mark.postgres


def _request(
    *,
    request_id: str,
    source: str = "test-source",
    kind: str = "prices",
    cache_mode: CacheMode = CacheMode.DEFAULT,
    params: JSONObj | None = None,
    ttl_seconds: float | None = 300.0,
    as_of_bucket: str | None = None,
    cache_tag: str | None = None,
    created_at: TSNSScalar | None = None,
) -> Request:
    """Construct one deterministic Request occurrence."""

    return Request(
        id=request_id,
        source=source,
        kind=kind,
        cache_mode=cache_mode,
        params=params,
        ttl_seconds=ttl_seconds,
        as_of_bucket=as_of_bucket,
        cache_tag=cache_tag,
        created_at=(
            created_at
            if created_at is not None
            else ts_ns_from_str("2026-09-03T10:05:00.654321000Z")
        ),
    )


def test_requests_round_trip_through_postgres(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Complete Request state round-trips through the migrated schema."""

    database = migrated_postgres_database

    request = _request(
        request_id="request-round-trip",
        source="example-source",
        kind="databento.timeseries.get_range",
        cache_mode=CacheMode.ONLY_IF_CACHED,
        ttl_seconds=600.0,
        as_of_bucket="2026-09-03T10",
        cache_tag="vendor-v1",
        params={
            "symbol": "ES",
            "fields": [
                "open",
                "close",
            ],
            "options": {
                "adjusted": False,
            },
        },
        created_at=ts_ns_from_str("2026-09-03T10:05:00.654321000Z"),
    )

    with database.transaction() as connection:
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


def test_equivalent_questions_persist_as_distinct_occurrences_across_contexts(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """The same logical question may occur in different resolution contexts."""

    database = migrated_postgres_database

    first = _request(
        request_id="request-1",
        source="source-a",
        cache_mode=CacheMode.DEFAULT,
        ttl_seconds=300.0,
        as_of_bucket="bucket-a",
        cache_tag="vendor-v1",
        created_at=ts_ns_from_str("2026-09-03T10:05:00.000000000Z"),
        params={
            "symbol": "ES",
        },
    )

    second = _request(
        request_id="request-2",
        source="source-b",
        cache_mode=CacheMode.BYPASS,
        ttl_seconds=None,
        as_of_bucket="bucket-b",
        cache_tag="vendor-v2",
        created_at=ts_ns_from_str("2026-09-03T10:06:00.000000000Z"),
        params={
            "symbol": "ES",
        },
    )

    assert first.id != second.id
    assert first.source != second.source
    assert first.hash == second.hash

    with database.transaction() as connection:
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


def test_request_has_no_parent_grouping_dependency(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """A Request persists directly without a parent grouping record."""

    database = migrated_postgres_database

    request = _request(
        request_id="request-without-parent",
        params={
            "symbol": "ES",
        },
    )

    with database.transaction() as connection:
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


def test_request_persistence_is_idempotent_and_rejects_identity_conflicts(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Identical Q state is idempotent while conflicting Q state is rejected."""

    database = migrated_postgres_database

    request = _request(
        request_id="request-stable-identity",
        params={
            "symbol": "ES",
        },
    )

    with database.transaction() as connection:
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
        match=r"Persisted Request conflicts.*request-stable-identity",
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
    """Nested logical Request params survive PostgreSQL JSONB round-tripping."""

    database = migrated_postgres_database

    request = _request(
        request_id="request-json",
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
