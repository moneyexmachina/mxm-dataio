"""Unit tests for policy-aware reusable Response recovery."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import cast

import pytest
from psycopg import Connection

import mxm.dataio.reuse as reuse
from mxm.dataio.models import CacheMode, Request, Response, ResponseStatus
from mxm.dataio.payloads import (
    PayloadNotFoundError,
    PayloadStore,
    PayloadStoreError,
    compute_payload_checksum,
)
from mxm.dataio.sql.postgres import PostgresDatabase, PostgresRow
from mxm.types.timestamps import (
    TSNSScalar,
    ts_ns_from_int,
    ts_ns_from_str,
    ts_ns_to_int,
)

_CREATED_AT = ts_ns_from_str("2026-10-01T10:00:00.000000000Z")
_FETCHED_AT = ts_ns_from_str("2026-10-01T09:59:00.000000000Z")
_PAYLOAD = b"reusable external payload"


class FakeDatabase:
    """Minimal transaction boundary recording transaction lifetime."""

    schema = "dataio_test_abc"

    def __init__(self) -> None:
        self.transaction_calls = 0
        self.transaction_active = False
        self.connection = cast(
            Connection[PostgresRow],
            object(),
        )

    @contextmanager
    def transaction(
        self,
    ) -> Generator[Connection[PostgresRow]]:
        self.transaction_calls += 1
        self.transaction_active = True

        try:
            yield self.connection
        finally:
            self.transaction_active = False


class FakePayloadStore:
    """Payload store recording retrieval and its transaction context."""

    def __init__(
        self,
        *,
        database: FakeDatabase,
        data: bytes = _PAYLOAD,
        error: PayloadStoreError | None = None,
    ) -> None:
        self._database = database
        self._data = data
        self._error = error
        self.get_calls: list[str] = []

    def put(
        self,
        checksum: str,
        data: bytes,
    ) -> None:
        _ = checksum, data
        raise AssertionError("Reuse recovery must not write payloads")

    def get(
        self,
        checksum: str,
    ) -> bytes:
        assert self._database.transaction_active is False
        self.get_calls.append(checksum)

        if self._error is not None:
            raise self._error

        return self._data


@dataclass
class LookupRecord:
    """Arguments captured from one relational candidate lookup."""

    connection: Connection[PostgresRow] | None = None
    schema: str | None = None
    source: str | None = None
    request_hash: str | None = None
    as_of_bucket: str | None = None
    cache_tag: str | None = None
    minimum_fetched_at: TSNSScalar | None = None
    calls: int = 0


def _request(
    *,
    cache_mode: CacheMode = CacheMode.DEFAULT,
    ttl_seconds: float | None = None,
    created_at: TSNSScalar = _CREATED_AT,
) -> Request:
    """Construct one deterministic current Request."""

    return Request(
        id="request-current",
        source="source-a",
        kind="prices",
        cache_mode=cache_mode,
        params={
            "symbol": "ES",
        },
        ttl_seconds=ttl_seconds,
        as_of_bucket="bucket-a",
        cache_tag="vendor-v1",
        created_at=created_at,
    )


def _response() -> Response:
    """Construct one reusable historical Response."""

    return Response(
        id="response-candidate",
        request_id="request-historical",
        status=ResponseStatus.OK,
        payload_checksum=compute_payload_checksum(
            _PAYLOAD,
        ),
        size_bytes=len(
            _PAYLOAD,
        ),
        created_at=_FETCHED_AT,
        fetched_at=_FETCHED_AT,
    )


def _as_database(
    database: FakeDatabase,
) -> PostgresDatabase:
    return cast(
        PostgresDatabase,
        database,
    )


def _as_payload_store(
    payload_store: FakePayloadStore,
) -> PayloadStore:
    return cast(
        PayloadStore,
        payload_store,
    )


def _install_candidate_lookup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    record: LookupRecord,
    response_id: str | None,
) -> None:
    """Install one recording SQL candidate lookup."""

    def candidate_lookup(
        connection: Connection[PostgresRow],
        *,
        schema: str,
        source: str,
        request_hash: str,
        as_of_bucket: str | None,
        cache_tag: str | None,
        minimum_fetched_at: TSNSScalar | None = None,
    ) -> str | None:
        record.connection = connection
        record.schema = schema
        record.source = source
        record.request_hash = request_hash
        record.as_of_bucket = as_of_bucket
        record.cache_tag = cache_tag
        record.minimum_fetched_at = minimum_fetched_at
        record.calls += 1
        return response_id

    monkeypatch.setattr(
        reuse,
        "fetch_reuse_candidate_response_id",
        candidate_lookup,
    )


def _install_response_lookup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    database: FakeDatabase,
    response: Response,
) -> None:
    """Install one deterministic Response reconstruction."""

    def response_lookup(
        connection: Connection[PostgresRow],
        *,
        schema: str,
        response_id: str,
    ) -> Response | None:
        assert database.transaction_active is True
        assert connection is database.connection
        _ = schema
        assert response_id == response.id
        return response

    monkeypatch.setattr(
        reuse,
        "fetch_response_by_id",
        response_lookup,
    )


def _find(
    *,
    database: FakeDatabase,
    payload_store: FakePayloadStore,
    request: Request,
) -> tuple[Response, bytes] | None:
    return reuse.find_reusable_response(
        database=_as_database(database),
        payload_store=_as_payload_store(payload_store),
        request=request,
    )


def test_bypass_performs_no_candidate_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    payload_store = FakePayloadStore(
        database=database,
    )
    record = LookupRecord()
    _install_candidate_lookup(
        monkeypatch,
        record=record,
        response_id="response-candidate",
    )

    result = _find(
        database=database,
        payload_store=payload_store,
        request=_request(
            cache_mode=CacheMode.BYPASS,
            ttl_seconds=60.0,
        ),
    )

    assert result is None
    assert record.calls == 0
    assert database.transaction_calls == 0
    assert payload_store.get_calls == []


@pytest.mark.parametrize(
    "cache_mode",
    [
        CacheMode.DEFAULT,
        CacheMode.ONLY_IF_CACHED,
    ],
)
def test_reuse_without_ttl_uses_no_freshness_cutoff(
    monkeypatch: pytest.MonkeyPatch,
    cache_mode: CacheMode,
) -> None:
    database = FakeDatabase()
    payload_store = FakePayloadStore(
        database=database,
    )
    request = _request(
        cache_mode=cache_mode,
        ttl_seconds=None,
    )
    record = LookupRecord()
    _install_candidate_lookup(
        monkeypatch,
        record=record,
        response_id=None,
    )

    result = _find(
        database=database,
        payload_store=payload_store,
        request=request,
    )

    assert result is None
    assert record.minimum_fetched_at is None
    assert record.source == request.source
    assert record.request_hash == request.hash
    assert record.as_of_bucket == request.as_of_bucket
    assert record.cache_tag == request.cache_tag


@pytest.mark.parametrize(
    "cache_mode",
    [
        CacheMode.DEFAULT,
        CacheMode.ONLY_IF_CACHED,
    ],
)
def test_reuse_with_ttl_uses_same_freshness_cutoff(
    monkeypatch: pytest.MonkeyPatch,
    cache_mode: CacheMode,
) -> None:
    database = FakeDatabase()
    payload_store = FakePayloadStore(
        database=database,
    )
    record = LookupRecord()
    _install_candidate_lookup(
        monkeypatch,
        record=record,
        response_id=None,
    )

    result = _find(
        database=database,
        payload_store=payload_store,
        request=_request(
            cache_mode=cache_mode,
            ttl_seconds=90.25,
        ),
    )

    assert result is None
    assert record.minimum_fetched_at == ts_ns_from_str("2026-10-01T09:58:29.750000000Z")


def test_sub_microsecond_ttl_allows_no_whole_microseconds_of_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    payload_store = FakePayloadStore(
        database=database,
    )
    record = LookupRecord()
    _install_candidate_lookup(
        monkeypatch,
        record=record,
        response_id=None,
    )

    result = _find(
        database=database,
        payload_store=payload_store,
        request=_request(
            ttl_seconds=0.0000009995,
        ),
    )

    assert result is None
    assert record.minimum_fetched_at == _CREATED_AT


def test_exact_one_microsecond_ttl_allows_one_microsecond(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    payload_store = FakePayloadStore(
        database=database,
    )
    record = LookupRecord()
    _install_candidate_lookup(
        monkeypatch,
        record=record,
        response_id=None,
    )

    result = _find(
        database=database,
        payload_store=payload_store,
        request=_request(
            ttl_seconds=0.000001,
        ),
    )

    assert result is None
    assert record.minimum_fetched_at == ts_ns_from_str("2026-10-01T09:59:59.999999000Z")


def test_ttl_slightly_above_one_microsecond_allows_one_microsecond(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    payload_store = FakePayloadStore(
        database=database,
    )
    record = LookupRecord()
    _install_candidate_lookup(
        monkeypatch,
        record=record,
        response_id=None,
    )

    result = _find(
        database=database,
        payload_store=payload_store,
        request=_request(
            ttl_seconds=0.0000014,
        ),
    )

    assert result is None
    assert record.minimum_fetched_at == ts_ns_from_str("2026-10-01T09:59:59.999999000Z")


def test_cutoff_from_nanosecond_created_at_rounds_up_to_microsecond(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    payload_store = FakePayloadStore(
        database=database,
    )
    record = LookupRecord()
    _install_candidate_lookup(
        monkeypatch,
        record=record,
        response_id=None,
    )

    result = _find(
        database=database,
        payload_store=payload_store,
        request=_request(
            ttl_seconds=0.000001,
            created_at=ts_ns_from_int(
                ts_ns_to_int(_CREATED_AT) + 999,
            ),
        ),
    )

    assert result is None
    assert record.minimum_fetched_at == _CREATED_AT


def test_matching_candidate_returns_response_and_exact_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    payload_store = FakePayloadStore(
        database=database,
    )
    response = _response()
    record = LookupRecord()
    _install_candidate_lookup(
        monkeypatch,
        record=record,
        response_id=response.id,
    )
    _install_response_lookup(
        monkeypatch,
        database=database,
        response=response,
    )

    result = _find(
        database=database,
        payload_store=payload_store,
        request=_request(),
    )

    assert result == (
        response,
        _PAYLOAD,
    )
    assert record.connection is database.connection
    assert database.transaction_active is False
    assert payload_store.get_calls == [
        response.payload_checksum,
    ]


def test_no_candidate_returns_none_without_payload_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    payload_store = FakePayloadStore(
        database=database,
    )
    record = LookupRecord()
    _install_candidate_lookup(
        monkeypatch,
        record=record,
        response_id=None,
    )

    result = _find(
        database=database,
        payload_store=payload_store,
        request=_request(),
    )

    assert result is None
    assert database.transaction_calls == 1
    assert payload_store.get_calls == []


def test_missing_candidate_response_raises_reuse_lookup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    payload_store = FakePayloadStore(
        database=database,
    )
    record = LookupRecord()
    _install_candidate_lookup(
        monkeypatch,
        record=record,
        response_id="response-missing",
    )

    def missing_response_lookup(
        connection: Connection[PostgresRow],
        *,
        schema: str,
        response_id: str,
    ) -> Response | None:
        assert database.transaction_active is True
        assert connection is database.connection
        _ = schema
        assert response_id == "response-missing"
        return None

    monkeypatch.setattr(
        reuse,
        "fetch_response_by_id",
        missing_response_lookup,
    )

    with pytest.raises(
        reuse.ReuseLookupError,
        match=r"Response was not present.*response-missing",
    ):
        _find(
            database=database,
            payload_store=payload_store,
            request=_request(),
        )

    assert database.transaction_active is False
    assert payload_store.get_calls == []


def test_missing_candidate_payload_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    payload_store = FakePayloadStore(
        database=database,
        error=PayloadNotFoundError("missing"),
    )
    response = _response()
    record = LookupRecord()
    _install_candidate_lookup(
        monkeypatch,
        record=record,
        response_id=response.id,
    )
    _install_response_lookup(
        monkeypatch,
        database=database,
        response=response,
    )

    result = _find(
        database=database,
        payload_store=payload_store,
        request=_request(),
    )

    assert result is None


def test_corrupt_candidate_payload_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    payload_store = FakePayloadStore(
        database=database,
        data=b"corrupt payload",
    )
    response = _response()
    record = LookupRecord()
    _install_candidate_lookup(
        monkeypatch,
        record=record,
        response_id=response.id,
    )
    _install_response_lookup(
        monkeypatch,
        database=database,
        response=response,
    )

    result = _find(
        database=database,
        payload_store=payload_store,
        request=_request(),
    )

    assert result is None


def test_generic_payload_store_failure_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    failure = PayloadStoreError("backend unavailable")
    payload_store = FakePayloadStore(
        database=database,
        error=failure,
    )
    response = _response()
    record = LookupRecord()
    _install_candidate_lookup(
        monkeypatch,
        record=record,
        response_id=response.id,
    )
    _install_response_lookup(
        monkeypatch,
        database=database,
        response=response,
    )

    with pytest.raises(PayloadStoreError) as caught:
        _find(
            database=database,
            payload_store=payload_store,
            request=_request(),
        )

    assert caught.value is failure


def test_database_failure_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    payload_store = FakePayloadStore(
        database=database,
    )
    failure = RuntimeError("database unavailable")

    def failing_lookup(
        connection: Connection[PostgresRow],
        *,
        schema: str,
        source: str,
        request_hash: str,
        as_of_bucket: str | None,
        cache_tag: str | None,
        minimum_fetched_at: TSNSScalar | None = None,
    ) -> str | None:
        _ = (
            connection,
            schema,
            source,
            request_hash,
            as_of_bucket,
            cache_tag,
            minimum_fetched_at,
        )
        raise failure

    monkeypatch.setattr(
        reuse,
        "fetch_reuse_candidate_response_id",
        failing_lookup,
    )

    with pytest.raises(RuntimeError) as caught:
        _find(
            database=database,
            payload_store=payload_store,
            request=_request(),
        )

    assert caught.value is failure
    assert database.transaction_active is False
    assert payload_store.get_calls == []
