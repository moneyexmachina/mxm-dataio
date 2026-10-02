"""PostgreSQL integration tests for DataIO Response persistence.

These tests exercise the real migrated PostgreSQL schema and Psycopg boundary.

They prove that:

- complete Response observation state survives PostgreSQL persistence;
- canonical MXM timestamps survive the real PostgreSQL timestamp boundary;
- distinct Response observations may reference one identical payload;
- Response.request_id is relationally constrained to a persisted acquiring
  Request;
- Response persistence is idempotent while conflicting identity is rejected;
- nested adapter metadata survives PostgreSQL JSONB persistence.

Detailed SQL construction, malformed-row handling, timestamp-boundary
validation, payload/checksum validation, and transaction non-ownership are
tested separately by the Response unit tests.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from psycopg.errors import ForeignKeyViolation

from mxm.dataio.models import (
    CacheMode,
    Request,
    Response,
    ResponseStatus,
)
from mxm.dataio.sql.postgres import PostgresDatabase
from mxm.dataio.sql.requests import insert_request
from mxm.dataio.sql.responses import (
    ResponseConflictError,
    fetch_response_by_id,
    fetch_responses_by_payload_checksum,
    insert_response,
)
from mxm.types import JSONObj
from mxm.types.timestamps import (
    TSNSScalar,
    ts_ns_from_str,
)

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _checksum(
    data: bytes,
) -> str:
    """Return the SHA-256 identity of exact payload bytes."""

    return hashlib.sha256(data).hexdigest()


def _request(
    *,
    request_id: str,
    source: str = "test-source",
    cache_mode: CacheMode = CacheMode.DEFAULT,
    ttl_seconds: float | None = 300.0,
    as_of_bucket: str | None = None,
    cache_tag: str | None = None,
    created_at: TSNSScalar | None = None,
) -> Request:
    """Construct one deterministic parent Request occurrence."""

    return Request(
        id=request_id,
        source=source,
        kind="prices",
        cache_mode=cache_mode,
        params={
            "symbol": "ES",
        },
        ttl_seconds=ttl_seconds,
        as_of_bucket=as_of_bucket,
        cache_tag=cache_tag,
        created_at=(
            created_at
            if created_at is not None
            else ts_ns_from_str("2026-09-03T08:01:00.234567000Z")
        ),
    )


def _response(
    *,
    response_id: str,
    request_id: str,
    payload: bytes = b"payload",
    status: ResponseStatus = ResponseStatus.OK,
    created_at: TSNSScalar | None = None,
    fetched_at: TSNSScalar | None = None,
    media_type: str | None = None,
    encoding: str | None = None,
    elapsed_ms: int | None = None,
    adapter_meta: JSONObj | None = None,
) -> Response:
    """Construct one deterministic external Response observation."""

    return Response(
        id=response_id,
        request_id=request_id,
        status=status,
        created_at=(
            created_at
            if created_at is not None
            else ts_ns_from_str("2026-09-03T08:02:00.345678000Z")
        ),
        fetched_at=(
            fetched_at
            if fetched_at is not None
            else ts_ns_from_str("2026-09-03T08:02:01.456789000Z")
        ),
        payload_checksum=_checksum(
            payload,
        ),
        size_bytes=len(
            payload,
        ),
        media_type=media_type,
        encoding=encoding,
        elapsed_ms=elapsed_ms,
        adapter_meta=adapter_meta,
    )


def _insert_parent_request(
    database: PostgresDatabase,
    *,
    request: Request,
) -> None:
    """Persist the parent Request occurrence."""

    with database.transaction() as connection:
        insert_request(
            connection,
            schema=database.schema,
            request=request,
        )


# ---------------------------------------------------------------------------
# Response persistence
# ---------------------------------------------------------------------------


def test_response_round_trips_through_postgres(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Complete Response observation state survives PostgreSQL persistence."""

    database = migrated_postgres_database

    request = _request(
        request_id="request-response-round-trip",
        source="example-source",
        cache_mode=CacheMode.ONLY_IF_CACHED,
        ttl_seconds=600.0,
        as_of_bucket="2026-09-03T08",
        cache_tag="vendor-v1",
    )

    response = _response(
        response_id="response-round-trip",
        request_id=request.id,
        payload=b'{"price": 123.45}',
        status=ResponseStatus.OK,
        created_at=ts_ns_from_str("2026-09-03T08:02:00.123456000Z"),
        fetched_at=ts_ns_from_str("2026-09-03T08:02:01.654321000Z"),
        media_type="application/json",
        encoding="utf-8",
        elapsed_ms=137,
        adapter_meta={
            "source_request_id": "abc-123",
            "transport": {
                "status": 200,
            },
        },
    )

    _insert_parent_request(
        database,
        request=request,
    )

    with database.transaction() as connection:
        insert_response(
            connection,
            schema=database.schema,
            response=response,
        )

    with database.transaction() as connection:
        persisted = fetch_response_by_id(
            connection,
            schema=database.schema,
            response_id=response.id,
        )

    assert persisted == response


def test_distinct_observations_can_share_payload_identity(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Real PostgreSQL preserves multiple R observations referencing one P."""

    database = migrated_postgres_database

    first_request = _request(
        request_id="request-1",
        created_at=ts_ns_from_str("2026-09-03T08:01:00.000000000Z"),
    )

    second_request = _request(
        request_id="request-2",
        created_at=ts_ns_from_str("2026-09-03T08:01:01.000000000Z"),
    )

    payload = b"identical external payload"

    first_response = _response(
        response_id="response-1",
        request_id=first_request.id,
        payload=payload,
        created_at=ts_ns_from_str("2026-09-03T08:02:00.000000000Z"),
        fetched_at=ts_ns_from_str("2026-09-03T08:02:01.000000000Z"),
    )

    second_response = _response(
        response_id="response-2",
        request_id=second_request.id,
        payload=payload,
        created_at=ts_ns_from_str("2026-09-03T08:03:00.000000000Z"),
        fetched_at=ts_ns_from_str("2026-09-03T08:03:01.000000000Z"),
    )

    assert first_request.id != second_request.id
    assert first_request.hash == second_request.hash

    assert first_response.id != second_response.id
    assert first_response.payload_checksum == second_response.payload_checksum

    with database.transaction() as connection:
        insert_request(
            connection,
            schema=database.schema,
            request=first_request,
        )

        insert_request(
            connection,
            schema=database.schema,
            request=second_request,
        )

        insert_response(
            connection,
            schema=database.schema,
            response=first_response,
        )

        insert_response(
            connection,
            schema=database.schema,
            response=second_response,
        )

    with database.transaction() as connection:
        persisted = fetch_responses_by_payload_checksum(
            connection,
            schema=database.schema,
            payload_checksum=first_response.payload_checksum,
        )

    assert persisted == {
        first_response.id: first_response,
        second_response.id: second_response,
    }


def test_response_request_foreign_key_is_enforced(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """A Response cannot refer to an acquiring Request that does not exist."""

    database = migrated_postgres_database

    response = _response(
        response_id="response-orphan",
        request_id="missing-request",
    )

    with pytest.raises(ForeignKeyViolation):
        with database.transaction() as connection:
            insert_response(
                connection,
                schema=database.schema,
                response=response,
            )

    with database.transaction() as connection:
        persisted = fetch_response_by_id(
            connection,
            schema=database.schema,
            response_id=response.id,
        )

    assert persisted is None


def test_response_persistence_is_idempotent_and_rejects_identity_conflicts(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Identical R state is idempotent while conflicting R state is rejected."""

    database = migrated_postgres_database

    request = _request(
        request_id="request-response-conflict",
    )

    response = _response(
        response_id="response-stable",
        request_id=request.id,
        payload=b"original payload",
    )

    _insert_parent_request(
        database,
        request=request,
    )

    with database.transaction() as connection:
        insert_response(
            connection,
            schema=database.schema,
            response=response,
        )

    # Replaying the identical immutable observation is allowed.
    with database.transaction() as connection:
        insert_response(
            connection,
            schema=database.schema,
            response=response,
        )

    conflicting_response = replace(
        response,
        payload_checksum=_checksum(
            b"different payload",
        ),
        size_bytes=len(
            b"different payload",
        ),
    )

    with pytest.raises(
        ResponseConflictError,
        match=r"Persisted Response conflicts.*response-stable",
    ):
        with database.transaction() as connection:
            insert_response(
                connection,
                schema=database.schema,
                response=conflicting_response,
            )

    with database.transaction() as connection:
        persisted = fetch_response_by_id(
            connection,
            schema=database.schema,
            response_id=response.id,
        )

    assert persisted == response


def test_response_adapter_metadata_round_trips_through_jsonb(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Nested generic adapter metadata survives real PostgreSQL JSONB."""

    database = migrated_postgres_database

    request = _request(
        request_id="request-response-json",
    )

    response = _response(
        response_id="response-json",
        request_id=request.id,
        payload=b"measurement",
        adapter_meta={
            "source_request_id": "abc-123",
            "channel": 4,
            "measurement": {
                "quality": "good",
                "flags": [
                    1,
                    2,
                    None,
                    True,
                ],
            },
        },
    )

    _insert_parent_request(
        database,
        request=request,
    )

    with database.transaction() as connection:
        insert_response(
            connection,
            schema=database.schema,
            response=response,
        )

    with database.transaction() as connection:
        persisted = fetch_response_by_id(
            connection,
            schema=database.schema,
            response_id=response.id,
        )

    assert persisted is not None
    assert persisted == response
    assert persisted.adapter_meta == response.adapter_meta
