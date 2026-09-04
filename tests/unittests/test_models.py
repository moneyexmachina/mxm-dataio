import hashlib
from dataclasses import FrozenInstanceError

import pytest

from mxm.dataio.models import (
    AdapterResult,
    CacheMode,
    Request,
    Resolution,
    ResolutionKind,
    Response,
    ResponseStatus,
    Session,
)
from mxm.types import JSONObj
from mxm.types.timestamps import TSNSScalar, ts_ns_from_int


def _ts(value: int) -> TSNSScalar:
    """Construct a deterministic canonical MXM timestamp for tests."""

    return ts_ns_from_int(value)


def _request(
    *,
    session_id: str = "s1",
    kind: str = "fetch",
    params: JSONObj | None = None,
    created_at: TSNSScalar | None = None,
) -> Request:
    """Construct one representative immutable Request occurrence."""

    return Request(
        session_id=session_id,
        kind=kind,
        params=params,
        created_at=created_at if created_at is not None else _ts(1),
    )


def test_request_hash_determinism() -> None:
    """Equivalent logical questions have the same deterministic identity."""

    params = {
        "symbol": "AAPL",
        "limit": 10,
    }

    first = _request(
        session_id="s1",
        params=params,
        created_at=_ts(1),
    )

    second = _request(
        session_id="s2",
        params=params,
        created_at=_ts(2),
    )

    assert first.id != second.id
    assert first.session_id != second.session_id
    assert first.created_at != second.created_at
    assert first.hash == second.hash


@pytest.mark.parametrize(
    "second",
    [
        _request(
            session_id="s2",
            kind="different-kind",
            params={"x": 1},
        ),
        _request(
            session_id="s2",
            kind="fetch",
            params={"x": 2},
        ),
    ],
)
def test_request_hash_changes_with_question_content(
    second: Request,
) -> None:
    """Every logical question-bearing field participates in Request identity."""

    first = _request(
        session_id="s1",
        kind="fetch",
        params={"x": 1},
    )

    assert first.hash != second.hash


def test_request_hash_is_independent_of_parameter_key_order() -> None:
    """Equivalent JSON objects have the same deterministic Request identity."""

    first = _request(
        params={
            "symbol": "ES",
            "start": "2026-01-01",
            "end": "2026-02-01",
        }
    )

    second = _request(
        params={
            "end": "2026-02-01",
            "start": "2026-01-01",
            "symbol": "ES",
        }
    )

    assert first.hash == second.hash


def test_request_hash_excludes_occurrence_context() -> None:
    """Session membership and occurrence metadata do not affect question identity."""

    first = _request(
        session_id="session-a",
        params={"symbol": "ES"},
        created_at=_ts(1),
    )

    second = _request(
        session_id="session-b",
        params={"symbol": "ES"},
        created_at=_ts(2),
    )

    assert first.session_id != second.session_id
    assert first.created_at != second.created_at
    assert first.hash == second.hash


def test_request_is_immutable() -> None:
    """A Request occurrence cannot be rewritten after construction."""

    request = _request()

    with pytest.raises(FrozenInstanceError):
        request.kind = "different-kind"  # type: ignore[misc]


def test_acquired_resolution_records_request_and_response_identity() -> None:
    """An acquired Resolution records which Response satisfied the Request."""

    resolved_at = _ts(10)

    resolution = Resolution(
        request_id="request-1",
        response_id="response-1",
        kind=ResolutionKind.ACQUIRED,
        resolved_at=resolved_at,
    )

    assert resolution.request_id == "request-1"
    assert resolution.response_id == "response-1"
    assert resolution.kind is ResolutionKind.ACQUIRED
    assert resolution.resolved_at == resolved_at


def test_reused_resolution_can_reference_existing_response() -> None:
    """A reused Resolution may point to a Response acquired elsewhere."""

    resolution = Resolution(
        request_id="request-2",
        response_id="response-1",
        kind=ResolutionKind.REUSED,
        resolved_at=_ts(10),
    )

    assert resolution.request_id == "request-2"
    assert resolution.response_id == "response-1"
    assert resolution.kind is ResolutionKind.REUSED


def test_resolution_is_immutable() -> None:
    """A recorded Request-resolution fact cannot be rewritten."""

    resolution = Resolution(
        request_id="request-1",
        response_id="response-1",
        kind=ResolutionKind.ACQUIRED,
        resolved_at=_ts(10),
    )

    with pytest.raises(FrozenInstanceError):
        resolution.kind = ResolutionKind.REUSED  # type: ignore[misc]


def test_response_from_bytes_derives_payload_identity() -> None:
    """Exact payload bytes determine checksum and size."""

    data = b"hello world"
    created_at = _ts(20)
    fetched_at = _ts(19)

    response = Response.from_bytes(
        request_id="request-1",
        status=ResponseStatus.OK,
        data=data,
        created_at=created_at,
        fetched_at=fetched_at,
    )

    assert response.verify_payload(data)
    assert not response.verify_payload(b"tampered")
    assert response.size_bytes == len(data)
    assert response.payload_checksum == hashlib.sha256(data).hexdigest()
    assert response.created_at == created_at
    assert response.fetched_at == fetched_at


def test_distinct_responses_may_share_payload_identity() -> None:
    """Distinct external replies may contain byte-for-byte identical payloads."""

    data = b"identical external payload"

    first = Response.from_bytes(
        request_id="request-1",
        status=ResponseStatus.OK,
        data=data,
        created_at=_ts(20),
        fetched_at=_ts(19),
    )

    second = Response.from_bytes(
        request_id="request-2",
        status=ResponseStatus.OK,
        data=data,
        created_at=_ts(30),
        fetched_at=_ts(29),
    )

    assert first.id != second.id
    assert first.request_id != second.request_id

    assert first.payload_checksum == second.payload_checksum
    assert first.size_bytes == second.size_bytes


def test_response_from_adapter_result_preserves_status_and_metadata() -> None:
    """Adapter classification and generic metadata become Response facts."""

    result = AdapterResult(
        status=ResponseStatus.OK,
        data=b'{"value": 42}',
        media_type="application/json",
        encoding="utf-8",
        elapsed_ms=125,
        adapter_meta={
            "vendor_request_id": "abc-123",
            "transport": {
                "status": 200,
            },
        },
    )

    created_at = _ts(20)
    fetched_at = _ts(19)

    response = Response.from_adapter_result(
        request_id="request-1",
        result=result,
        created_at=created_at,
        fetched_at=fetched_at,
    )

    assert response.status is ResponseStatus.OK

    assert response.payload_checksum == hashlib.sha256(result.data).hexdigest()
    assert response.size_bytes == len(result.data)

    assert response.created_at == created_at
    assert response.fetched_at == fetched_at

    assert response.media_type == "application/json"
    assert response.encoding == "utf-8"
    assert response.elapsed_ms == 125

    assert response.adapter_meta == {
        "vendor_request_id": "abc-123",
        "transport": {
            "status": 200,
        },
    }


def test_response_from_adapter_result_preserves_error_status() -> None:
    """Response status is assigned by the adapter rather than the runtime."""

    result = AdapterResult(
        status=ResponseStatus.ERROR,
        data=b'{"error": "rate limited"}',
    )

    response = Response.from_adapter_result(
        request_id="request-1",
        result=result,
        created_at=_ts(20),
        fetched_at=_ts(19),
    )

    assert response.status is ResponseStatus.ERROR


def test_response_is_immutable() -> None:
    """An external Response observation cannot be rewritten after construction."""

    response = Response.from_bytes(
        request_id="request-1",
        status=ResponseStatus.OK,
        data=b"payload",
        created_at=_ts(20),
        fetched_at=_ts(19),
    )

    with pytest.raises(FrozenInstanceError):
        response.status = ResponseStatus.ERROR  # type: ignore[misc]


def test_adapter_result_meta_dict_contains_generic_metadata() -> None:
    """The legacy metadata helper exposes generic acquisition metadata."""

    result = AdapterResult(
        status=ResponseStatus.OK,
        data=b"payload",
        media_type="text/plain",
        encoding="utf-8",
        elapsed_ms=17,
        adapter_meta={
            "vendor_request_id": "request-1",
        },
    )

    assert result.meta_dict() == {
        "media_type": "text/plain",
        "encoding": "utf-8",
        "elapsed_ms": 17,
        "adapter_meta": {
            "vendor_request_id": "request-1",
        },
    }


def test_session_end_uses_supplied_timestamp() -> None:
    """Session records the completion timestamp supplied by its runtime."""

    started_at = _ts(10)
    ended_at = _ts(20)

    session = Session(
        source="test",
        cache_mode=CacheMode.DEFAULT,
        started_at=started_at,
    )

    assert session.started_at == started_at
    assert session.ended_at is None

    session.end(ended_at=ended_at)

    assert session.ended_at == ended_at


def test_enum_roundtrip() -> None:
    """Persisted enum values reconstruct the canonical DataIO vocabulary."""

    assert CacheMode("default") is CacheMode.DEFAULT
    assert CacheMode("only_if_cached") is CacheMode.ONLY_IF_CACHED
    assert CacheMode("bypass") is CacheMode.BYPASS

    assert ResolutionKind("acquired") is ResolutionKind.ACQUIRED
    assert ResolutionKind("reused") is ResolutionKind.REUSED

    assert ResponseStatus("ok") is ResponseStatus.OK
    assert ResponseStatus("error") is ResponseStatus.ERROR
