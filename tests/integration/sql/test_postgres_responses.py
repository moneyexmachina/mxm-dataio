"""PostgreSQL integration tests for DataIO response persistence."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from psycopg.errors import ForeignKeyViolation

from mxm.dataio.models import (
    CacheMode,
    Request,
    Response,
    ResponseStatus,
    Session,
    SessionMode,
)
from mxm.dataio.sql.postgres import PostgresDatabase
from mxm.dataio.sql.requests import insert_request
from mxm.dataio.sql.responses import (
    ResponseConflictError,
    fetch_response_by_id,
    fetch_responses_by_payload_checksum,
    insert_response,
)
from mxm.dataio.sql.sessions import insert_session
from mxm.types import JSONObj

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _checksum(
    data: bytes,
) -> str:
    """Return the SHA-256 identity of exact payload bytes."""

    return hashlib.sha256(data).hexdigest()


def _session(
    *,
    session_id: str,
    source: str = "test-source",
    cache_mode: CacheMode = CacheMode.DEFAULT,
    mode: SessionMode = SessionMode.SYNC,
    ttl_seconds: float | None = 300.0,
    as_of_bucket: str | None = None,
    cache_tag: str | None = None,
) -> Session:
    """Construct one deterministic parent DataIO Session."""

    return Session(
        id=session_id,
        source=source,
        cache_mode=cache_mode,
        mode=mode,
        ttl_seconds=ttl_seconds,
        as_of_bucket=as_of_bucket,
        cache_tag=cache_tag,
        started_at=datetime(
            2026,
            9,
            3,
            8,
            0,
            tzinfo=UTC,
        ),
    )


def _request(
    *,
    request_id: str,
    session_id: str,
) -> Request:
    """Construct one deterministic parent request occurrence."""

    return Request(
        id=request_id,
        session_id=session_id,
        kind="prices",
        params={
            "symbol": "ES",
        },
        created_at=datetime(
            2026,
            9,
            3,
            8,
            1,
            tzinfo=UTC,
        ),
    )


def _response(
    *,
    response_id: str,
    request_id: str,
    payload: bytes = b"payload",
    status: ResponseStatus = ResponseStatus.OK,
    sequence: int | None = None,
    media_type: str | None = None,
    encoding: str | None = None,
    elapsed_ms: int | None = None,
    adapter_meta: JSONObj | None = None,
) -> Response:
    """Construct one deterministic external response observation."""

    return Response(
        id=response_id,
        request_id=request_id,
        status=status,
        sequence=sequence,
        created_at=datetime(
            2026,
            9,
            3,
            8,
            2,
            tzinfo=UTC,
        ),
        fetched_at=datetime(
            2026,
            9,
            3,
            8,
            2,
            1,
            tzinfo=UTC,
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
    session: Session,
    request: Request,
) -> None:
    """Persist the Session → Request parent hierarchy."""

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


# ---------------------------------------------------------------------------
# Response persistence
# ---------------------------------------------------------------------------


def test_response_round_trips_through_postgres(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Complete observation state survives real PostgreSQL persistence."""

    database = migrated_postgres_database

    session = _session(
        session_id="session-response-round-trip",
        source="example-source",
        cache_mode=CacheMode.REVALIDATE,
        ttl_seconds=600.0,
        as_of_bucket="2026-09-03T08",
        cache_tag="vendor-v1",
    )

    request = _request(
        request_id="request-response-round-trip",
        session_id=session.id,
    )

    response = _response(
        response_id="response-round-trip",
        request_id=request.id,
        payload=b'{"price": 123.45}',
        status=ResponseStatus.OK,
        sequence=3,
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
        session=session,
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

    session = _session(
        session_id="session-shared-payload",
    )

    first_request = _request(
        request_id="request-1",
        session_id=session.id,
    )

    second_request = _request(
        request_id="request-2",
        session_id=session.id,
    )

    payload = b"identical external payload"

    first_response = _response(
        response_id="response-1",
        request_id=first_request.id,
        payload=payload,
    )

    second_response = _response(
        response_id="response-2",
        request_id=second_request.id,
        payload=payload,
    )

    assert first_request.id != second_request.id
    assert first_request.hash == second_request.hash

    assert first_response.id != second_response.id
    assert first_response.payload_checksum == second_response.payload_checksum

    with database.transaction() as connection:
        insert_session(
            connection,
            schema=database.schema,
            session=session,
        )

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
    """A Response cannot refer to a request occurrence that does not exist."""

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

    session = _session(
        session_id="session-response-conflict",
    )

    request = _request(
        request_id="request-response-conflict",
        session_id=session.id,
    )

    response = _response(
        response_id="response-stable",
        request_id=request.id,
        payload=b"original payload",
    )

    _insert_parent_request(
        database,
        session=session,
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
        match=r"Persisted response conflicts.*response-stable",
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

    session = _session(
        session_id="session-response-json",
    )

    request = _request(
        request_id="request-response-json",
        session_id=session.id,
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
        session=session,
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
