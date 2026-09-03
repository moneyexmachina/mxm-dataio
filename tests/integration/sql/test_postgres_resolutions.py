"""PostgreSQL integration tests for DataIO Resolution persistence.

These tests exercise the real migrated PostgreSQL schema. They prove that the
Resolution SQL adapter matches that schema, that acquired and reused
Resolutions preserve the intended Request/Response semantics, that one Response
may satisfy multiple Request occurrences, and that Resolution identity,
foreign-key, and conflict semantics behave correctly through real PostgreSQL.

Detailed SQL construction, row validation, and transaction non-ownership are
tested separately by the Resolution unit tests.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from psycopg.errors import ForeignKeyViolation

from mxm.dataio.models import (
    CacheMode,
    Request,
    Resolution,
    ResolutionKind,
    Response,
    ResponseStatus,
    Session,
    SessionMode,
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
from mxm.dataio.sql.sessions import insert_session

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


def _session(
    *,
    session_id: str,
    source: str = "test-source",
    cache_mode: CacheMode = CacheMode.DEFAULT,
) -> Session:
    """Construct one deterministic parent DataIO Session."""

    return Session(
        id=session_id,
        source=source,
        cache_mode=cache_mode,
        mode=SessionMode.SYNC,
        started_at=datetime(
            2026,
            9,
            3,
            10,
            0,
            tzinfo=UTC,
        ),
    )


def _request(
    *,
    request_id: str,
    session_id: str,
    created_at: datetime | None = None,
) -> Request:
    """Construct one deterministic Request occurrence."""

    return Request(
        id=request_id,
        session_id=session_id,
        kind="prices",
        params={
            "symbol": "ES",
        },
        created_at=(
            created_at
            if created_at is not None
            else datetime(
                2026,
                9,
                3,
                10,
                1,
                tzinfo=UTC,
            )
        ),
    )


def _response(
    *,
    response_id: str,
    request_id: str,
    payload: bytes = b"payload",
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
        created_at=datetime(
            2026,
            9,
            3,
            10,
            2,
            tzinfo=UTC,
        ),
        fetched_at=datetime(
            2026,
            9,
            3,
            10,
            2,
            1,
            tzinfo=UTC,
        ),
    )


def _resolution(
    *,
    request_id: str,
    response_id: str,
    kind: ResolutionKind,
    resolved_at: datetime | None = None,
) -> Resolution:
    """Construct one deterministic final Request Resolution."""

    return Resolution(
        request_id=request_id,
        response_id=response_id,
        kind=kind,
        resolved_at=(
            resolved_at
            if resolved_at is not None
            else datetime(
                2026,
                9,
                3,
                10,
                3,
                tzinfo=UTC,
            )
        ),
    )


def _insert_acquired_observation(
    database: PostgresDatabase,
    *,
    session: Session,
    request: Request,
    response: Response,
) -> None:
    """Persist one Session → Request → Response acquisition hierarchy."""

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

    session = _session(
        session_id="session-acquired",
    )

    request = _request(
        request_id="request-acquired",
        session_id=session.id,
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
    )

    _insert_acquired_observation(
        database,
        session=session,
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
    """Q2 may resolve by reusing an observation acquired by Q1."""

    database = migrated_postgres_database

    acquiring_session = _session(
        session_id="session-acquiring",
        source="source-a",
        cache_mode=CacheMode.DEFAULT,
    )

    acquiring_request = _request(
        request_id="request-1",
        session_id=acquiring_session.id,
        created_at=datetime(
            2026,
            9,
            3,
            10,
            1,
            tzinfo=UTC,
        ),
    )

    response = _response(
        response_id="response-1",
        request_id=acquiring_request.id,
        payload=b"reusable observation",
    )

    reusing_session = _session(
        session_id="session-reusing",
        source="source-a",
        cache_mode=CacheMode.ONLY_IF_CACHED,
    )

    reusing_request = _request(
        request_id="request-2",
        session_id=reusing_session.id,
        created_at=datetime(
            2026,
            9,
            3,
            10,
            5,
            tzinfo=UTC,
        ),
    )

    acquired_resolution = _resolution(
        request_id=acquiring_request.id,
        response_id=response.id,
        kind=ResolutionKind.ACQUIRED,
        resolved_at=datetime(
            2026,
            9,
            3,
            10,
            3,
            tzinfo=UTC,
        ),
    )

    reused_resolution = _resolution(
        request_id=reusing_request.id,
        response_id=response.id,
        kind=ResolutionKind.REUSED,
        resolved_at=datetime(
            2026,
            9,
            3,
            10,
            6,
            tzinfo=UTC,
        ),
    )

    assert acquiring_request.hash == reusing_request.hash
    assert response.request_id == acquiring_request.id
    assert response.request_id != reusing_request.id

    with database.transaction() as connection:
        insert_session(
            connection,
            schema=database.schema,
            session=acquiring_session,
        )

        insert_session(
            connection,
            schema=database.schema,
            session=reusing_session,
        )

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
    """ACQUIRED cannot falsely claim another Request's observation."""

    database = migrated_postgres_database

    session = _session(
        session_id="session-invalid-acquired",
    )

    first_request = _request(
        request_id="request-1",
        session_id=session.id,
    )

    second_request = _request(
        request_id="request-2",
        session_id=session.id,
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
    """REUSED cannot describe an observation actually acquired by that Q."""

    database = migrated_postgres_database

    session = _session(
        session_id="session-invalid-reused",
    )

    request = _request(
        request_id="request-1",
        session_id=session.id,
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
        session=session,
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

    session = _session(
        session_id="session-resolution-conflict",
    )

    request = _request(
        request_id="request-resolution-conflict",
        session_id=session.id,
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
    )

    resolution = _resolution(
        request_id=request.id,
        response_id=first_response.id,
        kind=ResolutionKind.ACQUIRED,
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
        match=r"Persisted resolution conflicts.*request-resolution-conflict",
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

    session = _session(
        session_id="session-resolution-fk",
    )

    acquiring_request = _request(
        request_id="request-acquiring",
        session_id=session.id,
    )

    response = _response(
        response_id="response-existing",
        request_id=acquiring_request.id,
    )

    _insert_acquired_observation(
        database,
        session=session,
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
