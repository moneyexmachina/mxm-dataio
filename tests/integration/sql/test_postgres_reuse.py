"""PostgreSQL integration tests for DataIO reusable-Response lookup.

These tests exercise reuse candidate selection against the real migrated
PostgreSQL schema.

They prove that reusable Responses are discovered from their original
acquisition context:

    Request -> Response

Candidate eligibility requires matching:

- source;
- logical-question hash;
- as-of bucket;
- cache tag.

Only successful Responses are eligible. An optional freshness cutoff applies
to Response.fetched_at, and the newest eligible acquisition is selected
deterministically.

These tests also prove an important architectural exclusion: a historical
REUSED Resolution does not transport a Response into the reusing Request's
acquisition namespace.

Cache-mode decisions, TTL calculation, payload-store availability, Resolution
creation, and external acquisition belong to the runtime and are tested
separately.
"""

from __future__ import annotations

import hashlib

import pytest

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
from mxm.dataio.sql.resolutions import insert_resolution
from mxm.dataio.sql.responses import insert_response
from mxm.dataio.sql.reuse import fetch_reuse_candidate_response_id
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

    return hashlib.sha256(
        data,
    ).hexdigest()


def _request(
    *,
    request_id: str,
    source: str = "source-a",
    kind: str = "prices",
    cache_mode: CacheMode = CacheMode.DEFAULT,
    params: JSONObj | None = None,
    ttl_seconds: float | None = 300.0,
    as_of_bucket: str | None = "bucket-a",
    cache_tag: str | None = "vendor-v1",
    created_at: TSNSScalar | None = None,
) -> Request:
    """Construct one deterministic logical-question occurrence."""

    return Request(
        id=request_id,
        source=source,
        kind=kind,
        cache_mode=cache_mode,
        params=(
            params
            if params is not None
            else {
                "symbol": "ES",
            }
        ),
        ttl_seconds=ttl_seconds,
        as_of_bucket=as_of_bucket,
        cache_tag=cache_tag,
        created_at=(
            created_at
            if created_at is not None
            else ts_ns_from_str("2026-09-07T07:01:00.000000000Z")
        ),
    )


def _response(
    *,
    response_id: str,
    request_id: str,
    status: ResponseStatus = ResponseStatus.OK,
    payload: bytes | None = None,
    created_at: TSNSScalar | None = None,
    fetched_at: TSNSScalar | None = None,
) -> Response:
    """Construct one deterministic external Response observation."""

    response_payload = payload if payload is not None else response_id.encode()

    return Response(
        id=response_id,
        request_id=request_id,
        status=status,
        payload_checksum=_checksum(
            response_payload,
        ),
        size_bytes=len(
            response_payload,
        ),
        created_at=(
            created_at
            if created_at is not None
            else ts_ns_from_str("2026-09-07T07:02:00.000000000Z")
        ),
        fetched_at=(
            fetched_at
            if fetched_at is not None
            else ts_ns_from_str("2026-09-07T07:02:01.000000000Z")
        ),
    )


def _persist_acquisition(
    database: PostgresDatabase,
    *,
    request: Request,
    response: Response,
) -> None:
    """Persist one Request -> Response acquisition hierarchy."""

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


def _lookup(
    database: PostgresDatabase,
    *,
    source: str,
    request_hash: str,
    as_of_bucket: str | None,
    cache_tag: str | None,
    minimum_fetched_at: TSNSScalar | None = None,
) -> str | None:
    """Look up one reuse candidate through the real PostgreSQL boundary."""

    with database.transaction() as connection:
        return fetch_reuse_candidate_response_id(
            connection,
            schema=database.schema,
            source=source,
            request_hash=request_hash,
            as_of_bucket=as_of_bucket,
            cache_tag=cache_tag,
            minimum_fetched_at=minimum_fetched_at,
        )


# ---------------------------------------------------------------------------
# Basic candidate discovery
# ---------------------------------------------------------------------------


def test_reuse_lookup_returns_none_when_no_candidate_exists(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """An empty acquisition history produces no reusable Response."""

    database = migrated_postgres_database

    request = _request(
        request_id="current-request",
    )

    candidate = _lookup(
        database,
        source="source-a",
        request_hash=request.hash,
        as_of_bucket="bucket-a",
        cache_tag="vendor-v1",
    )

    assert candidate is None


def test_reuse_lookup_finds_matching_acquired_response(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """A successful acquisition in the same namespace is reusable."""

    database = migrated_postgres_database

    request = _request(
        request_id="request-acquiring",
        source="source-a",
        as_of_bucket="bucket-a",
        cache_tag="vendor-v1",
    )

    response = _response(
        response_id="response-acquired",
        request_id=request.id,
    )

    _persist_acquisition(
        database,
        request=request,
        response=response,
    )

    candidate = _lookup(
        database,
        source=request.source,
        request_hash=request.hash,
        as_of_bucket=request.as_of_bucket,
        cache_tag=request.cache_tag,
    )

    assert candidate == response.id


# ---------------------------------------------------------------------------
# Reuse namespace
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    (
        "lookup_source",
        "lookup_as_of_bucket",
        "lookup_cache_tag",
    ),
    [
        (
            "source-b",
            "bucket-a",
            "vendor-v1",
        ),
        (
            "source-a",
            "bucket-b",
            "vendor-v1",
        ),
        (
            "source-a",
            "bucket-a",
            "vendor-v2",
        ),
    ],
)
def test_reuse_lookup_requires_complete_namespace_match(
    migrated_postgres_database: PostgresDatabase,
    lookup_source: str,
    lookup_as_of_bucket: str | None,
    lookup_cache_tag: str | None,
) -> None:
    """Source, as-of bucket, and cache tag independently partition reuse."""

    database = migrated_postgres_database

    request = _request(
        request_id="request-acquiring",
        source="source-a",
        as_of_bucket="bucket-a",
        cache_tag="vendor-v1",
    )

    response = _response(
        response_id="response-acquired",
        request_id=request.id,
    )

    _persist_acquisition(
        database,
        request=request,
        response=response,
    )

    candidate = _lookup(
        database,
        source=lookup_source,
        request_hash=request.hash,
        as_of_bucket=lookup_as_of_bucket,
        cache_tag=lookup_cache_tag,
    )

    assert candidate is None


def test_reuse_lookup_matches_null_partition_coordinates(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """NULL as-of bucket and cache tag compare as opaque equality values."""

    database = migrated_postgres_database

    request = _request(
        request_id="request-null-partition",
        source="source-a",
        as_of_bucket=None,
        cache_tag=None,
    )

    response = _response(
        response_id="response-null-partition",
        request_id=request.id,
    )

    _persist_acquisition(
        database,
        request=request,
        response=response,
    )

    matching = _lookup(
        database,
        source="source-a",
        request_hash=request.hash,
        as_of_bucket=None,
        cache_tag=None,
    )

    mismatching = _lookup(
        database,
        source="source-a",
        request_hash=request.hash,
        as_of_bucket="bucket-a",
        cache_tag=None,
    )

    assert matching == response.id
    assert mismatching is None


def test_reuse_lookup_requires_same_logical_question_hash(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Reuse namespace matching cannot override logical-question identity."""

    database = migrated_postgres_database

    acquired_request = _request(
        request_id="request-es",
        params={
            "symbol": "ES",
        },
    )

    response = _response(
        response_id="response-es",
        request_id=acquired_request.id,
    )

    current_request = _request(
        request_id="request-nq",
        params={
            "symbol": "NQ",
        },
    )

    assert acquired_request.hash != current_request.hash

    _persist_acquisition(
        database,
        request=acquired_request,
        response=response,
    )

    candidate = _lookup(
        database,
        source=acquired_request.source,
        request_hash=current_request.hash,
        as_of_bucket=acquired_request.as_of_bucket,
        cache_tag=acquired_request.cache_tag,
    )

    assert candidate is None


# ---------------------------------------------------------------------------
# Response eligibility
# ---------------------------------------------------------------------------


def test_reuse_lookup_excludes_error_responses(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """A newer ERROR observation cannot displace an older reusable OK response."""

    database = migrated_postgres_database

    ok_request = _request(
        request_id="request-ok",
        created_at=ts_ns_from_str("2026-09-07T07:01:00.000000000Z"),
    )

    error_request = _request(
        request_id="request-error",
        created_at=ts_ns_from_str("2026-09-07T07:10:00.000000000Z"),
    )

    assert ok_request.hash == error_request.hash

    ok_response = _response(
        response_id="response-ok",
        request_id=ok_request.id,
        status=ResponseStatus.OK,
        created_at=ts_ns_from_str("2026-09-07T07:02:00.000000000Z"),
        fetched_at=ts_ns_from_str("2026-09-07T07:02:01.000000000Z"),
    )

    error_response = _response(
        response_id="response-error",
        request_id=error_request.id,
        status=ResponseStatus.ERROR,
        created_at=ts_ns_from_str("2026-09-07T07:11:00.000000000Z"),
        fetched_at=ts_ns_from_str("2026-09-07T07:11:01.000000000Z"),
    )

    with database.transaction() as connection:
        insert_request(
            connection,
            schema=database.schema,
            request=ok_request,
        )

        insert_request(
            connection,
            schema=database.schema,
            request=error_request,
        )

        insert_response(
            connection,
            schema=database.schema,
            response=ok_response,
        )

        insert_response(
            connection,
            schema=database.schema,
            response=error_response,
        )

    candidate = _lookup(
        database,
        source=ok_request.source,
        request_hash=ok_request.hash,
        as_of_bucket=ok_request.as_of_bucket,
        cache_tag=ok_request.cache_tag,
    )

    assert candidate == ok_response.id


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


def test_reuse_lookup_applies_inclusive_fetched_at_cutoff(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """A Response fetched exactly at the runtime cutoff remains eligible."""

    database = migrated_postgres_database

    cutoff = ts_ns_from_str("2026-09-07T07:30:00.123456000Z")

    stale_request = _request(
        request_id="request-stale",
        created_at=ts_ns_from_str("2026-09-07T07:10:00.000000000Z"),
    )

    boundary_request = _request(
        request_id="request-boundary",
        created_at=ts_ns_from_str("2026-09-07T07:20:00.000000000Z"),
    )

    assert stale_request.hash == boundary_request.hash

    stale_response = _response(
        response_id="response-stale",
        request_id=stale_request.id,
        fetched_at=ts_ns_from_str("2026-09-07T07:29:59.999999000Z"),
    )

    boundary_response = _response(
        response_id="response-boundary",
        request_id=boundary_request.id,
        fetched_at=cutoff,
    )

    with database.transaction() as connection:
        insert_request(
            connection,
            schema=database.schema,
            request=stale_request,
        )

        insert_request(
            connection,
            schema=database.schema,
            request=boundary_request,
        )

        insert_response(
            connection,
            schema=database.schema,
            response=stale_response,
        )

        insert_response(
            connection,
            schema=database.schema,
            response=boundary_response,
        )

    candidate = _lookup(
        database,
        source=stale_request.source,
        request_hash=stale_request.hash,
        as_of_bucket=stale_request.as_of_bucket,
        cache_tag=stale_request.cache_tag,
        minimum_fetched_at=cutoff,
    )

    assert candidate == boundary_response.id


def test_reuse_lookup_returns_none_when_all_candidates_are_stale(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """No Response older than the runtime cutoff is reusable."""

    database = migrated_postgres_database

    request = _request(
        request_id="request-stale",
    )

    response = _response(
        response_id="response-stale",
        request_id=request.id,
        fetched_at=ts_ns_from_str("2026-09-07T07:29:59.999999000Z"),
    )

    _persist_acquisition(
        database,
        request=request,
        response=response,
    )

    candidate = _lookup(
        database,
        source=request.source,
        request_hash=request.hash,
        as_of_bucket=request.as_of_bucket,
        cache_tag=request.cache_tag,
        minimum_fetched_at=ts_ns_from_str("2026-09-07T07:30:00.000000000Z"),
    )

    assert candidate is None


# ---------------------------------------------------------------------------
# Candidate ordering
# ---------------------------------------------------------------------------


def test_reuse_lookup_prefers_newest_fetched_response(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Response.fetched_at is the primary candidate ordering coordinate."""

    database = migrated_postgres_database

    first_request = _request(
        request_id="request-1",
    )

    second_request = _request(
        request_id="request-2",
    )

    first_response = _response(
        response_id="response-1",
        request_id=first_request.id,
        fetched_at=ts_ns_from_str("2026-09-07T07:20:00.000000000Z"),
    )

    second_response = _response(
        response_id="response-2",
        request_id=second_request.id,
        fetched_at=ts_ns_from_str("2026-09-07T07:21:00.000000000Z"),
    )

    assert first_request.hash == second_request.hash

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

    candidate = _lookup(
        database,
        source=first_request.source,
        request_hash=first_request.hash,
        as_of_bucket=first_request.as_of_bucket,
        cache_tag=first_request.cache_tag,
    )

    assert candidate == second_response.id


def test_reuse_lookup_uses_created_at_to_break_fetched_at_ties(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Response.created_at breaks equal fetched_at ties."""

    database = migrated_postgres_database

    first_request = _request(
        request_id="request-1",
    )

    second_request = _request(
        request_id="request-2",
    )

    fetched_at = ts_ns_from_str("2026-09-07T07:30:00.000000000Z")

    first_response = _response(
        response_id="response-z",
        request_id=first_request.id,
        created_at=ts_ns_from_str("2026-09-07T07:28:00.000000000Z"),
        fetched_at=fetched_at,
    )

    second_response = _response(
        response_id="response-a",
        request_id=second_request.id,
        created_at=ts_ns_from_str("2026-09-07T07:29:00.000000000Z"),
        fetched_at=fetched_at,
    )

    assert first_request.hash == second_request.hash

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

    candidate = _lookup(
        database,
        source=first_request.source,
        request_hash=first_request.hash,
        as_of_bucket=first_request.as_of_bucket,
        cache_tag=first_request.cache_tag,
    )

    # response-z has the lexically greater ID, so this specifically proves
    # created_at wins before the final ID tie-breaker.
    assert candidate == second_response.id


def test_reuse_lookup_uses_response_id_as_final_tie_breaker(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Response ID makes candidate selection deterministic after timestamp ties."""

    database = migrated_postgres_database

    first_request = _request(
        request_id="request-1",
    )

    second_request = _request(
        request_id="request-2",
    )

    created_at = ts_ns_from_str("2026-09-07T07:29:00.000000000Z")

    fetched_at = ts_ns_from_str("2026-09-07T07:30:00.000000000Z")

    first_response = _response(
        response_id="response-a",
        request_id=first_request.id,
        created_at=created_at,
        fetched_at=fetched_at,
    )

    second_response = _response(
        response_id="response-z",
        request_id=second_request.id,
        created_at=created_at,
        fetched_at=fetched_at,
    )

    assert first_request.hash == second_request.hash

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

    candidate = _lookup(
        database,
        source=first_request.source,
        request_hash=first_request.hash,
        as_of_bucket=first_request.as_of_bucket,
        cache_tag=first_request.cache_tag,
    )

    assert candidate == second_response.id


# ---------------------------------------------------------------------------
# Historical Resolution exclusion
# ---------------------------------------------------------------------------


def test_reused_resolution_does_not_transport_response_between_namespaces(
    migrated_postgres_database: PostgresDatabase,
) -> None:
    """Reuse history cannot make an acquisition discoverable in a new namespace."""

    database = migrated_postgres_database

    acquiring_request = _request(
        request_id="request-acquiring",
        source="source-a",
        as_of_bucket="bucket-a",
        cache_tag="vendor-v1",
    )

    response = _response(
        response_id="response-original",
        request_id=acquiring_request.id,
    )

    reusing_request = _request(
        request_id="request-reusing",
        source="source-a",
        as_of_bucket="bucket-b",
        cache_tag="vendor-v1",
        created_at=ts_ns_from_str("2026-09-07T08:01:00.000000000Z"),
    )

    assert acquiring_request.hash == reusing_request.hash
    assert acquiring_request.as_of_bucket != reusing_request.as_of_bucket

    acquired_resolution = Resolution(
        request_id=acquiring_request.id,
        response_id=response.id,
        kind=ResolutionKind.ACQUIRED,
        resolved_at=ts_ns_from_str("2026-09-07T07:03:00.000000000Z"),
    )

    reused_resolution = Resolution(
        request_id=reusing_request.id,
        response_id=response.id,
        kind=ResolutionKind.REUSED,
        resolved_at=ts_ns_from_str("2026-09-07T08:02:00.000000000Z"),
    )

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

        # The Resolution repository deliberately permits this structurally:
        # the Response was acquired by a different Request. Reuse eligibility
        # is not Resolution persistence's concern.
        insert_resolution(
            connection,
            schema=database.schema,
            resolution=reused_resolution,
        )

    original_namespace_candidate = _lookup(
        database,
        source=acquiring_request.source,
        request_hash=acquiring_request.hash,
        as_of_bucket=acquiring_request.as_of_bucket,
        cache_tag=acquiring_request.cache_tag,
    )

    reusing_namespace_candidate = _lookup(
        database,
        source=reusing_request.source,
        request_hash=reusing_request.hash,
        as_of_bucket=reusing_request.as_of_bucket,
        cache_tag=reusing_request.cache_tag,
    )

    assert original_namespace_candidate == response.id
    assert reusing_namespace_candidate is None
