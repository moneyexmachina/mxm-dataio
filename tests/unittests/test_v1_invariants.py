"""Executable architectural invariants for DataIO V1.

This suite records cross-model properties of the current V1 architecture that
are already stable independently of the unfinished DataIO runtime.

It deliberately does not exercise the legacy API, SQLite Store, adapter
registry, cache implementation, or runtime resolution machinery.

Repository behaviour, PostgreSQL constraints, timestamp representation, and
object-store behaviour are tested in their dedicated suites.

As the V1 runtime is rebuilt, this suite should grow to cover the remaining
cross-component invariants: reuse partitioning, TTL, cache modes, candidate
selection, Resolution creation, payload retrieval, and acquisition behaviour.
"""

from __future__ import annotations

from mxm.dataio.models import (
    CacheMode,
    Request,
    Resolution,
    ResolutionKind,
    Response,
    ResponseStatus,
    Session,
)
from mxm.types.timestamps import (
    TSNSScalar,
    ts_ns_from_str,
)

# ---------------------------------------------------------------------------
# Timestamp fixtures
# ---------------------------------------------------------------------------


def _ts(
    value: str,
) -> TSNSScalar:
    """Construct one deterministic canonical MXM timestamp."""

    return ts_ns_from_str(value)


# ---------------------------------------------------------------------------
# Logical-question identity
# ---------------------------------------------------------------------------


def test_equivalent_questions_share_hash_across_request_occurrences() -> None:
    """H identifies the logical question, not the Request occurrence."""

    first = Request(
        id="request-1",
        session_id="session-1",
        kind="databento.timeseries.get_range",
        params={
            "dataset": "GLBX.MDP3",
            "symbols": ["ES.c.0"],
        },
        created_at=_ts("2026-09-03T10:00:00.000000000Z"),
    )

    second = Request(
        id="request-2",
        session_id="session-2",
        kind="databento.timeseries.get_range",
        params={
            "dataset": "GLBX.MDP3",
            "symbols": ["ES.c.0"],
        },
        created_at=_ts("2026-09-03T11:00:00.000000000Z"),
    )

    assert first.id != second.id
    assert first.session_id != second.session_id
    assert first.created_at != second.created_at

    assert first.hash == second.hash


def test_session_context_does_not_participate_in_question_hash() -> None:
    """Source and cache context partition reuse, not logical-question identity."""

    first_session = Session(
        id="session-1",
        source="source-a",
        cache_mode=CacheMode.DEFAULT,
        ttl_seconds=60.0,
        as_of_bucket="bucket-a",
        cache_tag="vendor-v1",
        started_at=_ts("2026-09-03T10:00:00.000000000Z"),
    )

    second_session = Session(
        id="session-2",
        source="source-b",
        cache_mode=CacheMode.ONLY_IF_CACHED,
        ttl_seconds=3_600.0,
        as_of_bucket="bucket-b",
        cache_tag="vendor-v2",
        started_at=_ts("2026-09-03T11:00:00.000000000Z"),
    )

    first = Request(
        id="request-1",
        session_id=first_session.id,
        kind="prices",
        params={
            "symbol": "ES",
        },
        created_at=_ts("2026-09-03T10:01:00.000000000Z"),
    )

    second = Request(
        id="request-2",
        session_id=second_session.id,
        kind="prices",
        params={
            "symbol": "ES",
        },
        created_at=_ts("2026-09-03T11:01:00.000000000Z"),
    )

    assert first_session.source != second_session.source
    assert first_session.cache_mode != second_session.cache_mode
    assert first_session.ttl_seconds != second_session.ttl_seconds
    assert first_session.as_of_bucket != second_session.as_of_bucket
    assert first_session.cache_tag != second_session.cache_tag

    assert first.hash == second.hash


def test_question_bearing_fields_define_request_hash() -> None:
    """Changing kind or params changes logical-question identity."""

    baseline = Request(
        id="request-1",
        session_id="session-1",
        kind="prices",
        params={
            "symbol": "ES",
        },
        created_at=_ts("2026-09-03T10:00:00.000000000Z"),
    )

    different_kind = Request(
        id="request-2",
        session_id="session-1",
        kind="settlements",
        params={
            "symbol": "ES",
        },
        created_at=_ts("2026-09-03T10:00:00.000000000Z"),
    )

    different_params = Request(
        id="request-3",
        session_id="session-1",
        kind="prices",
        params={
            "symbol": "NQ",
        },
        created_at=_ts("2026-09-03T10:00:00.000000000Z"),
    )

    assert baseline.hash != different_kind.hash
    assert baseline.hash != different_params.hash


def test_question_hash_is_independent_of_parameter_key_order() -> None:
    """Equivalent JSON objects represent the same logical question."""

    first = Request(
        id="request-1",
        session_id="session-1",
        kind="prices",
        params={
            "symbol": "ES",
            "schema": "ohlcv-1h",
            "limit": 100,
        },
        created_at=_ts("2026-09-03T10:00:00.000000000Z"),
    )

    second = Request(
        id="request-2",
        session_id="session-1",
        kind="prices",
        params={
            "limit": 100,
            "schema": "ohlcv-1h",
            "symbol": "ES",
        },
        created_at=_ts("2026-09-03T10:01:00.000000000Z"),
    )

    assert first.hash == second.hash


# ---------------------------------------------------------------------------
# Request occurrence versus logical identity
# ---------------------------------------------------------------------------


def test_equivalent_questions_remain_distinct_request_occurrences() -> None:
    """Q identity remains distinct even when logical-question identity is equal."""

    first = Request(
        id="request-1",
        session_id="session-1",
        kind="prices",
        params={
            "symbol": "ES",
        },
        created_at=_ts("2026-09-03T10:00:00.000000000Z"),
    )

    second = Request(
        id="request-2",
        session_id="session-1",
        kind="prices",
        params={
            "symbol": "ES",
        },
        created_at=_ts("2026-09-03T10:01:00.000000000Z"),
    )

    assert first.hash == second.hash
    assert first.id != second.id


# ---------------------------------------------------------------------------
# Response identity versus Payload identity
# ---------------------------------------------------------------------------


def test_distinct_responses_may_share_exact_payload_identity() -> None:
    """R identifies an observation occurrence independently of payload P."""

    payload_checksum = "a" * 64

    first = Response(
        id="response-1",
        request_id="request-1",
        status=ResponseStatus.OK,
        payload_checksum=payload_checksum,
        size_bytes=17,
        created_at=_ts("2026-09-03T10:02:00.000000000Z"),
        fetched_at=_ts("2026-09-03T10:02:01.000000000Z"),
        adapter_meta={
            "vendor_request_id": "vendor-1",
        },
    )

    second = Response(
        id="response-2",
        request_id="request-2",
        status=ResponseStatus.OK,
        payload_checksum=payload_checksum,
        size_bytes=17,
        created_at=_ts("2026-09-03T10:03:00.000000000Z"),
        fetched_at=_ts("2026-09-03T10:03:01.000000000Z"),
        adapter_meta={
            "vendor_request_id": "vendor-2",
        },
    )

    assert first.id != second.id
    assert first.request_id != second.request_id

    assert first.payload_checksum == second.payload_checksum
    assert first.size_bytes == second.size_bytes

    assert first.adapter_meta != second.adapter_meta


# ---------------------------------------------------------------------------
# Resolution ontology
# ---------------------------------------------------------------------------


def test_one_response_can_represent_acquisition_and_later_reuse() -> None:
    """Reuse points a later Request to the existing acquired Response."""

    response = Response(
        id="response-1",
        request_id="request-1",
        status=ResponseStatus.OK,
        payload_checksum="b" * 64,
        size_bytes=12,
        created_at=_ts("2026-09-03T10:02:00.000000000Z"),
        fetched_at=_ts("2026-09-03T10:02:01.000000000Z"),
    )

    acquired = Resolution(
        request_id="request-1",
        response_id=response.id,
        kind=ResolutionKind.ACQUIRED,
        resolved_at=_ts("2026-09-03T10:03:00.000000000Z"),
    )

    reused = Resolution(
        request_id="request-2",
        response_id=response.id,
        kind=ResolutionKind.REUSED,
        resolved_at=_ts("2026-09-03T10:05:00.000000000Z"),
    )

    assert response.request_id == acquired.request_id
    assert response.request_id != reused.request_id

    assert acquired.response_id == response.id
    assert reused.response_id == response.id

    assert acquired.kind is ResolutionKind.ACQUIRED
    assert reused.kind is ResolutionKind.REUSED
