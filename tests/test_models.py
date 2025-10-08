import hashlib

from mxm_dataio.models import (
    Request,
    RequestMethod,
    Response,
    ResponseStatus,
    Session,
    SessionMode,
)


def test_request_hash_determinism() -> None:
    params = {"symbol": "AAPL", "limit": 10}
    r1 = Request(session_id="s1", kind="fetch", params=params)
    r2 = Request(session_id="s1", kind="fetch", params=params)
    assert r1.hash == r2.hash


def test_request_hash_changes() -> None:
    r1 = Request(session_id="s1", kind="fetch", params={"x": 1})
    r2 = Request(session_id="s1", kind="fetch", params={"x": 2})
    assert r1.hash != r2.hash


def test_response_from_bytes_and_verify() -> None:
    data = b"hello world"
    r = Response.from_bytes(
        request_id="r1",
        status=ResponseStatus.OK,
        data=data,
        path="/tmp/x.bin",
    )
    assert r.verify(data)
    assert not r.verify(b"tampered")
    assert r.size_bytes == len(data)
    assert r.checksum == hashlib.sha256(data).hexdigest()


def test_session_end_sets_timestamp() -> None:
    s = Session(source="test", mode=SessionMode.SYNC)
    s.end()
    assert s.ended_at is not None
    assert s.ended_at >= s.started_at


def test_enum_roundtrip() -> None:
    assert SessionMode("async") == SessionMode.ASYNC
    assert RequestMethod("GET") == RequestMethod.GET
    assert ResponseStatus("ok") == ResponseStatus.OK
