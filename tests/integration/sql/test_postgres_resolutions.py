"""PostgreSQL integration tests for DataIO Resolution persistence.

These tests exercise the real migrated PostgreSQL schema and Psycopg boundary.

They prove that:

- acquired and reused Resolutions round-trip through PostgreSQL;
- canonical MXM timestamps survive the real PostgreSQL timestamp boundary;
- ACQUIRED and REUSED preserve their Request/Response provenance semantics;
- one Response may satisfy multiple Request occurrences;
- one Request occurrence has at most one final Resolution;
- Resolution.request_id is relationally constrained to a persisted Request;
- Resolution persistence is idempotent while conflicting identity is rejected.

Detailed SQL construction, malformed-row handling, timestamp-boundary
validation, structural relationship validation, and transaction non-ownership
are tested separately by the Resolution unit tests.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from psycopg.errors import ForeignKeyViolation

from mxm.dataio.models import (
    CacheMode,
    Request,
    Resolution,
    ResolutionKind,
    Response,
    ResponseStatus,
)
from mxm.dataio.sql.postgres import PostgresDatabase
from mxm.dataio.sql.requests import insert_request
from mxm.dataio.sql.resolutions import (
    ResolutionConflictError,
    ResolutionPersistenceError,
    fetch_resolution_by_request_id,
    fetch_resolutions_by_response_id,
    insert_resolution,
)
from mxm.dataio.sql.responses import insert_response
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

    return hashlib.sha256(
        data,
    ).hexdigest()


def _request(
    *,
    request_id: str,
    source: str = "test-source",
    cache_mode: CacheMode = CacheMode.DEFAULT,
    created_at: TSNSScalar | None = None,
) -> Request:
    """Construct one deterministic Request occurrence."""

    return Request(
        id=request_id,
        source=source,
        kind="prices",
        cache_mode=cache_mode,
        params={
            "symbol": "ES",
        },
        created_at=(
            created_at
            if created_at is not None
            else ts_ns_from_str("2026-09-03T10:01:00.234567000Z")
        ),
    )


def _response(
    *,
    response_id: str,
    request_id: str,
    payload: bytes = b"payload",
    created_at: TSNSScalar | None = None,
    fetched_at: TSNSScalar | None = None,
) -> Response:
    """Construct one deterministic external Response observation."""

    return Response(
        id=response_id,
        request_id=request_id,
        status=ResponseStatus.OK,
        payload_checksum=_checksum(
            payload,
        ),
        size_bytes=len(
            payload,
        ),
        created_at=(
            created_at
            if created_at is not None
            else ts_ns_from_str("2026-09-03T10:02:00.345678000Z")
        ),
        fetched_at=(
            fetched_at
            if fetched_at is not None
            else ts_ns_from_str("2026-09-03T10:02:01.456789000Z")
        ),
    )


def _resolution(
    *,
    request_id: str,
    response_id: str,
    kind: ResolutionKind,
    resolved_at: TSNSScalar | None = None,
) -> Resolution:
    """Construct one deterministic final Request Resolution."""

    return Resolution(
        request_id=request_id,
        response_id=response_id,
        kind=kind,
        resolved_at=(
            resolved_at
            if resolved_at is not None
            else ts_ns_from_str("2026-09-03T10:03:00.567890000Z")
        ),
    )


def _insert_acquired_observation(
    database: PostgresDatabase,
    *,
    request: Request,
    response: Response,
) -> None:
    """Persist one Request → Response acquisition hierarchy."""

    with database.transaction() as connection:
        insert_request(
            connection,
            schema=database.schema,
            request=request,
        )

        insert_response(
            connection,
            schema=database.schema,
            response=response,
        )


# ---------------------------------------------------------------------------
# Acquired Resolution persistence
# ---------------------------------------------------------------------------


def test_acquired_resolution_round_trips_through_postgres(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """An acquired Resolution survives real PostgreSQL persistence."""

    database = migrated_postgres_database

    request = _request(
        request_id="request-acquired",
    )

    response = _response(
        response_id="response-acquired",
        request_id=request.id,
        payload=b"fresh external observation",
    )

    resolution = _resolution(
        request_id=request.id,
        response_id=response.id,
        kind=ResolutionKind.ACQUIRED,
        resolved_at=ts_ns_from_str("2026-09-03T10:03:00.654321000Z"),
    )

    _insert_acquired_observation(
        database,
        request=request,
        response=response,
    )

    with database.transaction() as connection:
        insert_resolution(
            connection,
            schema=database.schema,
            resolution=resolution,
        )

    with database.transaction() as connection:
        persisted = fetch_resolution_by_request_id(
            connection,
            schema=database.schema,
            request_id=request.id,
        )

        missing = fetch_resolution_by_request_id(
            connection,
            schema=database.schema,
            request_id="missing-request",
        )

    assert persisted == resolution
    assert missing is None


# ---------------------------------------------------------------------------
# Reused Resolution persistence
# ---------------------------------------------------------------------------


def test_reused_resolution_references_existing_observation(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Q2 may resolve by reusing a Response acquired by Q1."""

    database = migrated_postgres_database

    acquiring_request = _request(
        request_id="request-1",
        source="source-a",
        cache_mode=CacheMode.DEFAULT,
        created_at=ts_ns_from_str("2026-09-03T10:01:00.000000000Z"),
    )

    response = _response(
        response_id="response-1",
        request_id=acquiring_request.id,
        payload=b"reusable observation",
        created_at=ts_ns_from_str("2026-09-03T10:02:00.000000000Z"),
        fetched_at=ts_ns_from_str("2026-09-03T10:02:01.000000000Z"),
    )

    reusing_request = _request(
        request_id="request-2",
        source="source-a",
        cache_mode=CacheMode.ONLY_IF_CACHED,
        created_at=ts_ns_from_str("2026-09-03T10:05:00.000000000Z"),
    )

    acquired_resolution = _resolution(
        request_id=acquiring_request.id,
        response_id=response.id,
        kind=ResolutionKind.ACQUIRED,
        resolved_at=ts_ns_from_str("2026-09-03T10:03:00.000000000Z"),
    )

    reused_resolution = _resolution(
        request_id=reusing_request.id,
        response_id=response.id,
        kind=ResolutionKind.REUSED,
        resolved_at=ts_ns_from_str("2026-09-03T10:06:00.000000000Z"),
    )

    assert acquiring_request.hash == reusing_request.hash
    assert response.request_id == acquiring_request.id
    assert response.request_id != reusing_request.id

    with database.transaction() as connection:
        insert_request(
            connection,
            schema=database.schema,
            request=acquiring_request,
        )

        insert_request(
            connection,
            schema=database.schema,
            request=reusing_request,
        )

        insert_response(
            connection,
            schema=database.schema,
            response=response,
        )

        insert_resolution(
            connection,
            schema=database.schema,
            resolution=acquired_resolution,
        )

        insert_resolution(
            connection,
            schema=database.schema,
            resolution=reused_resolution,
        )

    with database.transaction() as connection:
        persisted_acquired = fetch_resolution_by_request_id(
            connection,
            schema=database.schema,
            request_id=acquiring_request.id,
        )

        persisted_reused = fetch_resolution_by_request_id(
            connection,
            schema=database.schema,
            request_id=reusing_request.id,
        )

        by_response = fetch_resolutions_by_response_id(
            connection,
            schema=database.schema,
            response_id=response.id,
        )

    assert persisted_acquired == acquired_resolution
    assert persisted_reused == reused_resolution

    assert by_response == {
        acquired_resolution.request_id: acquired_resolution,
        reused_resolution.request_id: reused_resolution,
    }


# ---------------------------------------------------------------------------
# Resolution semantic consistency
# ---------------------------------------------------------------------------


def test_acquired_resolution_rejects_observation_from_different_request(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """ACQUIRED cannot falsely claim another Request's Response."""

    database = migrated_postgres_database

    first_request = _request(
        request_id="request-1",
    )

    second_request = _request(
        request_id="request-2",
        created_at=ts_ns_from_str("2026-09-03T10:01:01.000000000Z"),
    )

    response = _response(
        response_id="response-1",
        request_id=first_request.id,
    )

    invalid_resolution = _resolution(
        request_id=second_request.id,
        response_id=response.id,
        kind=ResolutionKind.ACQUIRED,
    )

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
            response=response,
        )

    with pytest.raises(
        ResolutionPersistenceError,
        match=r"Acquired Resolution must reference a Response acquired by the same",
    ):
        with database.transaction() as connection:
            insert_resolution(
                connection,
                schema=database.schema,
                resolution=invalid_resolution,
            )

    with database.transaction() as connection:
        persisted = fetch_resolution_by_request_id(
            connection,
            schema=database.schema,
            request_id=second_request.id,
        )

    assert persisted is None


def test_reused_resolution_rejects_observation_from_same_request(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """REUSED cannot describe a Response actually acquired by that Q."""

    database = migrated_postgres_database

    request = _request(
        request_id="request-1",
    )

    response = _response(
        response_id="response-1",
        request_id=request.id,
    )

    invalid_resolution = _resolution(
        request_id=request.id,
        response_id=response.id,
        kind=ResolutionKind.REUSED,
    )

    _insert_acquired_observation(
        database,
        request=request,
        response=response,
    )

    with pytest.raises(
        ResolutionPersistenceError,
        match=r"Reused Resolution must reference a Response acquired by a different",
    ):
        with database.transaction() as connection:
            insert_resolution(
                connection,
                schema=database.schema,
                resolution=invalid_resolution,
            )

    with database.transaction() as connection:
        persisted = fetch_resolution_by_request_id(
            connection,
            schema=database.schema,
            request_id=request.id,
        )

    assert persisted is None


# ---------------------------------------------------------------------------
# Resolution identity and relational integrity
# ---------------------------------------------------------------------------


def test_resolution_persistence_is_idempotent_and_rejects_identity_conflicts(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Identical Resolution state is idempotent; one Q has one final result."""

    database = migrated_postgres_database

    request = _request(
        request_id="request-resolution-conflict",
    )

    first_response = _response(
        response_id="response-1",
        request_id=request.id,
        payload=b"first observation",
    )

    second_response = _response(
        response_id="response-2",
        request_id=request.id,
        payload=b"second observation",
        created_at=ts_ns_from_str("2026-09-03T10:02:02.000000000Z"),
        fetched_at=ts_ns_from_str("2026-09-03T10:02:03.000000000Z"),
    )

    resolution = _resolution(
        request_id=request.id,
        response_id=first_response.id,
        kind=ResolutionKind.ACQUIRED,
    )

    with database.transaction() as connection:
        insert_request(
            connection,
            schema=database.schema,
            request=request,
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

        insert_resolution(
            connection,
            schema=database.schema,
            resolution=resolution,
        )

    # Replaying the identical historical fact is allowed.
    with database.transaction() as connection:
        insert_resolution(
            connection,
            schema=database.schema,
            resolution=resolution,
        )

    conflicting_resolution = replace(
        resolution,
        response_id=second_response.id,
    )

    with pytest.raises(
        ResolutionConflictError,
        match=r"Persisted Resolution conflicts.*request-resolution-conflict",
    ):
        with database.transaction() as connection:
            insert_resolution(
                connection,
                schema=database.schema,
                resolution=conflicting_resolution,
            )

    with database.transaction() as connection:
        persisted = fetch_resolution_by_request_id(
            connection,
            schema=database.schema,
            request_id=request.id,
        )

    assert persisted == resolution


def test_resolution_request_foreign_key_is_enforced(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """A Resolution cannot resolve a Request occurrence that does not exist."""

    database = migrated_postgres_database

    acquiring_request = _request(
        request_id="request-acquiring",
    )

    response = _response(
        response_id="response-existing",
        request_id=acquiring_request.id,
    )

    _insert_acquired_observation(
        database,
        request=acquiring_request,
        response=response,
    )

    resolution = _resolution(
        request_id="missing-request",
        response_id=response.id,
        kind=ResolutionKind.REUSED,
    )

    # The Resolution/Response relationship is structurally valid for REUSED:
    # response-existing was acquired by request-acquiring, not missing-request.
    # PostgreSQL must then reject the nonexistent Resolution.request_id.
    with pytest.raises(ForeignKeyViolation):
        with database.transaction() as connection:
            insert_resolution(
                connection,
                schema=database.schema,
                resolution=resolution,
            )

    with database.transaction() as connection:
        persisted = fetch_resolution_by_request_id(
            connection,
            schema=database.schema,
            request_id="missing-request",
        )

    assert persisted is None
