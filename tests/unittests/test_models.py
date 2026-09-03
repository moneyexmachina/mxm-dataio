import hashlib
from dataclasses import FrozenInstanceError

import pytest

from mxm.dataio.models import (
    AdapterResult,
    CacheMode,
    Request,
    RequestMethod,
    Resolution,
    ResolutionKind,
    Response,
    ResponseStatus,
    Session,
    SessionMode,
)
from mxm.types import JSONLike, JSONObj


def _request(
    *,
    session_id: str = "s1",
    kind: str = "fetch",
    method: RequestMethod = RequestMethod.GET,
    params: JSONObj | None = None,
    body: JSONLike | None = None,
) -> Request:
    """Construct one representative immutable request occurrence."""

    return Request(
        session_id=session_id,
        kind=kind,
        method=method,
        params=params,
        body=body,
    )


def test_request_hash_determinism() -> None:
    """Equivalent questions have the same deterministic identity."""

    params = {
        "symbol": "AAPL",
        "limit": 10,
    }

    first = _request(
        session_id="s1",
        params=params,
    )

    second = _request(
        session_id="s2",
        params=params,
    )

    assert first.id != second.id
    assert first.session_id != second.session_id
    assert first.hash == second.hash


@pytest.mark.parametrize(
    "second",
    [
        _request(
            session_id="s2",
            kind="different-kind",
        ),
        _request(
            session_id="s2",
            method=RequestMethod.POST,
        ),
        _request(
            session_id="s2",
            params={
                "x": 2,
            },
        ),
        _request(
            session_id="s2",
            body={
                "command": "different",
            },
        ),
    ],
)
def test_request_hash_changes_with_question_content(
    second: Request,
) -> None:
    """Every question-bearing field participates in request identity."""

    first = _request(
        session_id="s1",
        kind="fetch",
        method=RequestMethod.GET,
        params={
            "x": 1,
        },
        body=None,
    )

    assert first.hash != second.hash


def test_request_hash_excludes_occurrence_context() -> None:
    """Session membership does not participate in question identity."""

    first = _request(
        session_id="session-a",
        params={
            "symbol": "ES",
        },
    )

    second = _request(
        session_id="session-b",
        params={
            "symbol": "ES",
        },
    )

    assert first.session_id != second.session_id
    assert first.hash == second.hash


def test_request_is_immutable() -> None:
    """A request occurrence cannot be rewritten after construction."""

    request = _request()

    with pytest.raises(FrozenInstanceError):
        request.kind = "different-kind"  # type: ignore[misc]


def test_acquired_resolution_records_request_and_response_identity() -> None:
    """An acquired resolution records which observation satisfied the request."""

    resolution = Resolution(
        request_id="request-1",
        response_id="response-1",
        kind=ResolutionKind.ACQUIRED,
    )

    assert resolution.request_id == "request-1"
    assert resolution.response_id == "response-1"
    assert resolution.kind is ResolutionKind.ACQUIRED
    assert resolution.resolved_at.tzinfo is not None
    assert resolution.resolved_at.utcoffset() is not None


def test_reused_resolution_can_reference_existing_response() -> None:
    """A reused resolution may point to an observation acquired elsewhere."""

    resolution = Resolution(
        request_id="request-2",
        response_id="response-1",
        kind=ResolutionKind.REUSED,
    )

    assert resolution.request_id == "request-2"
    assert resolution.response_id == "response-1"
    assert resolution.kind is ResolutionKind.REUSED


def test_resolution_is_immutable() -> None:
    """A recorded request-resolution fact cannot be rewritten."""

    resolution = Resolution(
        request_id="request-1",
        response_id="response-1",
        kind=ResolutionKind.ACQUIRED,
    )

    with pytest.raises(FrozenInstanceError):
        resolution.kind = ResolutionKind.REUSED  # type: ignore[misc]


def test_response_from_bytes_derives_payload_identity() -> None:
    """Exact payload bytes determine checksum and size."""

    data = b"hello world"

    response = Response.from_bytes(
        request_id="request-1",
        status=ResponseStatus.OK,
        data=data,
    )

    assert response.verify_payload(data)
    assert not response.verify_payload(b"tampered")
    assert response.size_bytes == len(data)
    assert response.payload_checksum == hashlib.sha256(data).hexdigest()


def test_distinct_responses_may_share_payload_identity() -> None:
    """Distinct observations may contain byte-for-byte identical payloads."""

    data = b"identical external payload"

    first = Response.from_bytes(
        request_id="request-1",
        status=ResponseStatus.OK,
        data=data,
    )

    second = Response.from_bytes(
        request_id="request-2",
        status=ResponseStatus.OK,
        data=data,
    )

    assert first.id != second.id
    assert first.request_id != second.request_id

    assert first.payload_checksum == second.payload_checksum
    assert first.size_bytes == second.size_bytes


def test_response_from_adapter_result_preserves_generic_metadata() -> None:
    """Generic adapter-boundary metadata becomes observation metadata."""

    result = AdapterResult(
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

    response = Response.from_adapter_result(
        request_id="request-1",
        status=ResponseStatus.OK,
        result=result,
    )

    assert response.payload_checksum == hashlib.sha256(result.data).hexdigest()
    assert response.size_bytes == len(result.data)

    assert response.media_type == "application/json"
    assert response.encoding == "utf-8"
    assert response.elapsed_ms == 125

    assert response.adapter_meta == {
        "vendor_request_id": "abc-123",
        "transport": {
            "status": 200,
        },
    }


def test_response_is_immutable() -> None:
    """An external observation cannot be rewritten after construction."""

    response = Response.from_bytes(
        request_id="request-1",
        status=ResponseStatus.OK,
        data=b"payload",
    )

    with pytest.raises(FrozenInstanceError):
        response.status = ResponseStatus.ERROR  # type: ignore[misc]


def test_adapter_result_meta_dict_contains_generic_metadata() -> None:
    """The legacy metadata helper exposes only generic acquisition metadata."""

    result = AdapterResult(
        data=b"payload",
        media_type="text/plain",
        encoding="utf-8",
        elapsed_ms=17,
        adapter_meta={
            "device_id": "sensor-1",
        },
    )

    assert result.meta_dict() == {
        "media_type": "text/plain",
        "encoding": "utf-8",
        "elapsed_ms": 17,
        "adapter_meta": {
            "device_id": "sensor-1",
        },
    }


def test_session_end_sets_timestamp() -> None:
    session = Session(
        source="test",
        cache_mode=CacheMode.DEFAULT,
        mode=SessionMode.SYNC,
    )

    session.end()

    assert session.ended_at is not None
    assert session.ended_at >= session.started_at


def test_enum_roundtrip() -> None:
    assert SessionMode("async") == SessionMode.ASYNC
    assert RequestMethod("GET") == RequestMethod.GET
    assert CacheMode("default") == CacheMode.DEFAULT
    assert ResolutionKind("acquired") == ResolutionKind.ACQUIRED
    assert ResolutionKind("reused") == ResolutionKind.REUSED
    assert ResponseStatus("ok") == ResponseStatus.OK
    assert SessionMode("async") == SessionMode.ASYNC
    assert RequestMethod("GET") == RequestMethod.GET
    assert CacheMode("default") == CacheMode.DEFAULT
    assert ResponseStatus("ok") == ResponseStatus.OK
