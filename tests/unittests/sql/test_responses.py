"""Unit tests for plain-SQL DataIO Response persistence operations."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Literal, Self, cast

import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from mxm.dataio.models import Response, ResponseStatus
from mxm.dataio.sql.postgres import PostgresRow
from mxm.dataio.sql.responses import (
    ResponseConflictError,
    ResponsePersistenceError,
    fetch_response_by_id,
    fetch_responses_by_payload_checksum,
    fetch_responses_by_request_id,
    insert_response,
)
from mxm.types import JSONObj
from mxm.types.timestamps import (
    TSNSScalar,
    ts_ns_from_int,
    ts_ns_from_str,
    ts_ns_to_int,
)

type ExecutableQuery = sql.SQL | sql.Composed


# ---------------------------------------------------------------------------
# Fake PostgreSQL boundary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Execution:
    """One SQL operation issued through a fake cursor."""

    operation: Literal["execute"]
    query: ExecutableQuery
    parameters: object | None


class FakeCursor:
    """Scripted cursor recording SQL operations and returning fixed rows."""

    def __init__(
        self,
        *,
        rows: list[PostgresRow] | None = None,
    ) -> None:
        self._rows = list(rows or [])
        self._executions: list[Execution] = []
        self.fetchall_calls = 0

    def bind_executions(
        self,
        executions: list[Execution],
    ) -> None:
        """Bind this cursor to its connection's execution log."""

        self._executions = executions

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        _ = exc_type, exc_value, traceback
        return None

    def execute(
        self,
        query: ExecutableQuery,
        parameters: object | None = None,
    ) -> None:
        """Record one execute operation."""

        self._executions.append(
            Execution(
                operation="execute",
                query=query,
                parameters=parameters,
            )
        )

    def fetchall(self) -> list[PostgresRow]:
        """Return the scripted result rows."""

        self.fetchall_calls += 1
        return list(self._rows)


class FakeConnection:
    """Connection returning scripted cursors in invocation order."""

    def __init__(
        self,
        cursors: list[FakeCursor] | None = None,
    ) -> None:
        self._cursors = list(cursors or [])
        self.executions: list[Execution] = []
        self.cursor_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0

        for cursor in self._cursors:
            cursor.bind_executions(self.executions)

    def cursor(self) -> FakeCursor:
        self.cursor_calls += 1

        if not self._cursors:
            raise AssertionError(
                "Unexpected cursor request: no scripted cursor remains"
            )

        return self._cursors.pop(0)

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1


def _as_connection(
    connection: FakeConnection,
) -> Connection[PostgresRow]:
    return cast(
        Connection[PostgresRow],
        connection,
    )


# ---------------------------------------------------------------------------
# Timestamp fixtures and boundary helpers
# ---------------------------------------------------------------------------


_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_CREATED_AT = ts_ns_from_str("2026-09-03T08:00:00.123456000Z")

_FETCHED_AT = ts_ns_from_str("2026-09-03T08:00:01.654321000Z")


def _db_datetime(
    value: TSNSScalar,
) -> datetime:
    """Encode one microsecond-aligned MXM timestamp as fake Psycopg output."""

    nanoseconds = ts_ns_to_int(value)

    assert nanoseconds % 1_000 == 0

    return _UNIX_EPOCH + timedelta(
        microseconds=nanoseconds // 1_000,
    )


# ---------------------------------------------------------------------------
# Response fixtures
# ---------------------------------------------------------------------------


def _checksum(
    data: bytes = b"payload",
) -> str:
    """Return the SHA-256 identity of exact payload bytes."""

    return hashlib.sha256(data).hexdigest()


def _response(
    *,
    response_id: str = "response-1",
    request_id: str = "request-1",
    status: ResponseStatus = ResponseStatus.OK,
    created_at: TSNSScalar = _CREATED_AT,
    fetched_at: TSNSScalar = _FETCHED_AT,
    payload_checksum: str | None = None,
    size_bytes: int = 7,
    media_type: str | None = None,
    encoding: str | None = None,
    elapsed_ms: int | None = None,
    adapter_meta: JSONObj | None = None,
) -> Response:
    """Construct one representative immutable Response observation."""

    return Response(
        id=response_id,
        request_id=request_id,
        status=status,
        created_at=created_at,
        fetched_at=fetched_at,
        payload_checksum=(
            payload_checksum if payload_checksum is not None else _checksum()
        ),
        size_bytes=size_bytes,
        media_type=media_type,
        encoding=encoding,
        elapsed_ms=elapsed_ms,
        adapter_meta=adapter_meta,
    )


def _response_row(
    response: Response,
) -> PostgresRow:
    """Encode one Response as a fake Psycopg PostgreSQL result row."""

    return (
        response.id,
        response.request_id,
        response.status.value,
        _db_datetime(response.created_at),
        _db_datetime(response.fetched_at),
        response.payload_checksum,
        response.size_bytes,
        response.media_type,
        response.encoding,
        response.elapsed_ms,
        response.adapter_meta,
    )


# ---------------------------------------------------------------------------
# PostgreSQL row and execution helpers
# ---------------------------------------------------------------------------


def _row(
    *values: object,
) -> PostgresRow:
    return tuple(values)


def _row_with_value(
    row: PostgresRow,
    index: int,
    value: object,
) -> PostgresRow:
    values = list(row)

    if index < 0 or index >= len(values):
        raise AssertionError(
            "Test row replacement index is out of range: "
            f"index={index}, row_length={len(values)}"
        )

    values[index] = value

    return _row(*values)


def _query_text(
    query: ExecutableQuery,
) -> str:
    return " ".join(query.as_string().split())


def _single_execution(
    connection: FakeConnection,
) -> Execution:
    assert len(connection.executions) == 1
    return connection.executions[0]


def _execution_parameters(
    execution: Execution,
) -> tuple[object, ...]:
    parameters = execution.parameters

    if not isinstance(parameters, tuple):
        raise AssertionError(f"Expected tuple execution parameters, got {parameters!r}")

    return cast(
        tuple[object, ...],
        parameters,
    )


# ---------------------------------------------------------------------------
# Response / payload identity
# ---------------------------------------------------------------------------


def test_distinct_responses_can_share_payload_identity() -> None:
    """Different external observations may contain identical exact bytes."""

    payload_checksum = _checksum(b"same bytes")

    first = _response(
        response_id="response-1",
        request_id="request-1",
        payload_checksum=payload_checksum,
        size_bytes=len(b"same bytes"),
    )

    second = _response(
        response_id="response-2",
        request_id="request-2",
        payload_checksum=payload_checksum,
        size_bytes=len(b"same bytes"),
    )

    assert first.id != second.id
    assert first.request_id != second.request_id
    assert first.payload_checksum == second.payload_checksum


# ---------------------------------------------------------------------------
# fetch_response_by_id
# ---------------------------------------------------------------------------


def test_fetch_response_by_id_returns_none_when_absent() -> None:
    """A missing observation returns no persisted Response."""

    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    response = fetch_response_by_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        response_id="response-1",
    )

    assert response is None
    assert connection.cursor_calls == 1


def test_fetch_response_by_id_reconstructs_response() -> None:
    """A persisted row reconstructs the complete Response object."""

    expected = _response(
        status=ResponseStatus.ERROR,
        payload_checksum=_checksum(b"response bytes"),
        size_bytes=len(b"response bytes"),
        media_type="application/json",
        encoding="utf-8",
        elapsed_ms=125,
        adapter_meta={
            "vendor_request_id": "abc-123",
            "transport": {
                "status": 503,
            },
        },
    )

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _response_row(expected),
                ]
            ),
        ]
    )

    response = fetch_response_by_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        response_id=expected.id,
    )

    assert response == expected


def test_fetch_response_by_id_converts_database_timestamps_to_mxm_timestamps() -> None:
    """Psycopg datetime values reconstruct as canonical MXM timestamps."""

    expected = _response()

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _response_row(expected),
                ]
            ),
        ]
    )

    response = fetch_response_by_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        response_id=expected.id,
    )

    assert response is not None
    assert response.created_at == expected.created_at
    assert response.fetched_at == expected.fetched_at


def test_fetch_response_by_id_normalises_aware_database_timestamp_to_utc() -> None:
    """Equivalent database timezone representations reconstruct to one instant."""

    expected = _response(
        created_at=ts_ns_from_str("2026-09-03T08:00:00.000000000Z"),
    )

    row = _response_row(expected)

    utc_plus_one = timezone(
        timedelta(hours=1),
    )

    row = _row_with_value(
        row,
        3,
        datetime(
            2026,
            9,
            3,
            9,
            0,
            tzinfo=utc_plus_one,
        ),
    )

    connection = FakeConnection(
        [
            FakeCursor(rows=[row]),
        ]
    )

    response = fetch_response_by_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        response_id=expected.id,
    )

    assert response is not None
    assert response.created_at == expected.created_at


def test_fetch_response_by_id_uses_configured_schema_and_identity() -> None:
    """Response lookup uses the configured schema and observation ID."""

    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    fetch_response_by_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        response_id="response-1",
    )

    execution = _single_execution(connection)
    query_text = _query_text(execution.query)

    assert '"dataio_test_abc"."responses"' in query_text
    assert '"public"."responses"' not in query_text
    assert "WHERE id = %s" in query_text
    assert execution.parameters == ("response-1",)


def test_fetch_response_by_id_rejects_invalid_identifier_before_sql() -> None:
    """Invalid observation identity fails before database access."""

    connection = FakeConnection()

    with pytest.raises(
        ResponsePersistenceError,
        match=r"response_id must be non-empty text",
    ):
        fetch_response_by_id(
            _as_connection(connection),
            schema="dataio_test_abc",
            response_id="",
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


def test_fetch_response_by_id_rejects_multiple_database_rows() -> None:
    """One observation ID cannot reconstruct from multiple persisted rows."""

    response = _response()

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _response_row(response),
                    _response_row(response),
                ]
            ),
        ]
    )

    with pytest.raises(
        ResponsePersistenceError,
        match=r"multiple rows.*response-1",
    ):
        fetch_response_by_id(
            _as_connection(connection),
            schema="dataio_test_abc",
            response_id=response.id,
        )


_RESPONSE_ROW = _response_row(_response())


@pytest.mark.parametrize(
    (
        "row",
        "error_match",
    ),
    [
        (
            _row(*_RESPONSE_ROW[:-1]),
            r"unexpected row shape",
        ),
        (
            _row(*_RESPONSE_ROW, "extra"),
            r"unexpected row shape",
        ),
        (
            _row_with_value(
                _RESPONSE_ROW,
                0,
                123,
            ),
            r"id must be non-empty text",
        ),
        (
            _row_with_value(
                _RESPONSE_ROW,
                1,
                "",
            ),
            r"request_id must be non-empty text",
        ),
        (
            _row_with_value(
                _RESPONSE_ROW,
                2,
                "NOT_A_STATUS",
            ),
            r"status is not recognised",
        ),
        (
            _row_with_value(
                _RESPONSE_ROW,
                3,
                "2026-09-03T08:00:00Z",
            ),
            r"created_at must be a datetime",
        ),
        (
            _row_with_value(
                _RESPONSE_ROW,
                3,
                datetime(
                    2026,
                    9,
                    3,
                    8,
                    0,
                ),
            ),
            r"created_at must be timezone-aware",
        ),
        (
            _row_with_value(
                _RESPONSE_ROW,
                4,
                "2026-09-03T08:00:01Z",
            ),
            r"fetched_at must be a datetime",
        ),
        (
            _row_with_value(
                _RESPONSE_ROW,
                4,
                datetime(
                    2026,
                    9,
                    3,
                    8,
                    0,
                    1,
                ),
            ),
            r"fetched_at must be timezone-aware",
        ),
        (
            _row_with_value(
                _RESPONSE_ROW,
                5,
                "not-a-hash",
            ),
            r"64-character lowercase hexadecimal",
        ),
        (
            _row_with_value(
                _RESPONSE_ROW,
                6,
                -1,
            ),
            r"size_bytes must be a non-negative integer",
        ),
        (
            _row_with_value(
                _RESPONSE_ROW,
                6,
                True,
            ),
            r"size_bytes must be a non-negative integer",
        ),
        (
            _row_with_value(
                _RESPONSE_ROW,
                7,
                123,
            ),
            r"media_type must be text or NULL",
        ),
        (
            _row_with_value(
                _RESPONSE_ROW,
                8,
                123,
            ),
            r"encoding must be text or NULL",
        ),
        (
            _row_with_value(
                _RESPONSE_ROW,
                9,
                -1,
            ),
            r"elapsed_ms must be a non-negative integer",
        ),
        (
            _row_with_value(
                _RESPONSE_ROW,
                9,
                True,
            ),
            r"elapsed_ms must be a non-negative integer",
        ),
        (
            _row_with_value(
                _RESPONSE_ROW,
                10,
                ["not-an-object"],
            ),
            r"adapter_meta must be a JSON object",
        ),
        (
            _row_with_value(
                _RESPONSE_ROW,
                10,
                {
                    "bad": object(),
                },
            ),
            r"adapter_meta\.bad must contain JSON-compatible values",
        ),
    ],
)
def test_fetch_response_by_id_rejects_invalid_rows(
    row: PostgresRow,
    error_match: str,
) -> None:
    """Malformed persisted Response rows cannot enter the model."""

    connection = FakeConnection(
        [
            FakeCursor(rows=[row]),
        ]
    )

    with pytest.raises(
        ResponsePersistenceError,
        match=error_match,
    ):
        fetch_response_by_id(
            _as_connection(connection),
            schema="dataio_test_abc",
            response_id="response-1",
        )


# ---------------------------------------------------------------------------
# fetch_responses_by_request_id
# ---------------------------------------------------------------------------


def test_fetch_responses_by_request_id_returns_empty_mapping() -> None:
    """A Request with no acquired observations produces an empty mapping."""

    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    responses = fetch_responses_by_request_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        request_id="request-1",
    )

    assert responses == {}


def test_fetch_responses_by_request_id_preserves_distinct_observations() -> None:
    """Multiple Responses acquired by one Request remain independent."""

    first = _response(
        response_id="response-1",
        request_id="request-1",
        created_at=ts_ns_from_str("2026-09-03T08:00:00.000000000Z"),
    )

    second = _response(
        response_id="response-2",
        request_id="request-1",
        created_at=ts_ns_from_str("2026-09-03T08:01:00.000000000Z"),
        payload_checksum=_checksum(b"second payload"),
        size_bytes=len(b"second payload"),
    )

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _response_row(first),
                    _response_row(second),
                ]
            ),
        ]
    )

    responses = fetch_responses_by_request_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        request_id="request-1",
    )

    assert responses == {
        first.id: first,
        second.id: second,
    }


def test_fetch_responses_by_request_id_uses_filter_and_deterministic_ordering() -> None:
    """Request lookup filters by acquiring Q and orders observations."""

    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    fetch_responses_by_request_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        request_id="request-1",
    )

    execution = _single_execution(connection)
    query_text = _query_text(execution.query)

    assert '"dataio_test_abc"."responses"' in query_text
    assert "WHERE request_id = %s" in query_text
    assert "ORDER BY created_at, id" in query_text
    assert execution.parameters == ("request-1",)


def test_fetch_responses_by_request_id_rejects_invalid_identifier_before_sql() -> None:
    """Invalid Request occurrence identity fails before database access."""

    connection = FakeConnection()

    with pytest.raises(
        ResponsePersistenceError,
        match=r"request_id must be non-empty text",
    ):
        fetch_responses_by_request_id(
            _as_connection(connection),
            schema="dataio_test_abc",
            request_id="",
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


def test_fetch_responses_by_request_id_rejects_duplicate_response_identity() -> None:
    """One result set cannot contain the same Response twice."""

    response = _response()

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _response_row(response),
                    _response_row(response),
                ]
            ),
        ]
    )

    with pytest.raises(
        ResponsePersistenceError,
        match=r"duplicate Response ID.*response-1",
    ):
        fetch_responses_by_request_id(
            _as_connection(connection),
            schema="dataio_test_abc",
            request_id=response.request_id,
        )


# ---------------------------------------------------------------------------
# fetch_responses_by_payload_checksum
# ---------------------------------------------------------------------------


def test_fetch_responses_by_payload_checksum_returns_empty_mapping() -> None:
    """An unknown payload identity produces no observations."""

    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    responses = fetch_responses_by_payload_checksum(
        _as_connection(connection),
        schema="dataio_test_abc",
        payload_checksum=_checksum(),
    )

    assert responses == {}


def test_fetch_responses_by_payload_checksum_preserves_distinct_observations() -> None:
    """One exact payload may be referenced by distinct Response observations."""

    payload_checksum = _checksum(b"same bytes")

    first = _response(
        response_id="response-1",
        request_id="request-1",
        payload_checksum=payload_checksum,
        size_bytes=len(b"same bytes"),
    )

    second = _response(
        response_id="response-2",
        request_id="request-2",
        payload_checksum=payload_checksum,
        size_bytes=len(b"same bytes"),
    )

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _response_row(first),
                    _response_row(second),
                ]
            ),
        ]
    )

    responses = fetch_responses_by_payload_checksum(
        _as_connection(connection),
        schema="dataio_test_abc",
        payload_checksum=payload_checksum,
    )

    assert responses == {
        first.id: first,
        second.id: second,
    }


def test_fetch_responses_by_payload_checksum_uses_filter_and_ordering() -> None:
    """Payload lookup filters by P identity and orders observations."""

    payload_checksum = _checksum()

    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    fetch_responses_by_payload_checksum(
        _as_connection(connection),
        schema="dataio_test_abc",
        payload_checksum=payload_checksum,
    )

    execution = _single_execution(connection)
    query_text = _query_text(execution.query)

    assert '"dataio_test_abc"."responses"' in query_text
    assert "WHERE payload_checksum = %s" in query_text
    assert "ORDER BY created_at, id" in query_text
    assert execution.parameters == (payload_checksum,)


def test_fetch_responses_by_payload_checksum_rejects_invalid_hash_before_sql() -> None:
    """Payload identity must be a canonical SHA-256 value."""

    connection = FakeConnection()

    with pytest.raises(
        ResponsePersistenceError,
        match=r"64-character lowercase hexadecimal",
    ):
        fetch_responses_by_payload_checksum(
            _as_connection(connection),
            schema="dataio_test_abc",
            payload_checksum="not-a-hash",
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


def test_fetch_responses_by_payload_checksum_rejects_duplicate_response() -> None:
    """Payload lookup cannot contain the same Response twice."""

    response = _response()

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _response_row(response),
                    _response_row(response),
                ]
            ),
        ]
    )

    with pytest.raises(
        ResponsePersistenceError,
        match=r"duplicate Response ID.*response-1",
    ):
        fetch_responses_by_payload_checksum(
            _as_connection(connection),
            schema="dataio_test_abc",
            payload_checksum=response.payload_checksum,
        )


# ---------------------------------------------------------------------------
# insert_response: encoding
# ---------------------------------------------------------------------------


def test_insert_response_encodes_complete_observation_state() -> None:
    """One Response is encoded into the expected SQL representation."""

    response = _response(
        status=ResponseStatus.ERROR,
        payload_checksum=_checksum(b"response bytes"),
        size_bytes=len(b"response bytes"),
        media_type="application/json",
        encoding="utf-8",
        elapsed_ms=247,
        adapter_meta={
            "source_request_id": "source-123",
            "transport": {
                "status": 500,
            },
        },
    )

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _response_row(response),
                ]
            ),
        ]
    )

    insert_response(
        _as_connection(connection),
        schema="dataio_test_abc",
        response=response,
    )

    execution = connection.executions[0]
    query_text = _query_text(execution.query)

    assert '"dataio_test_abc"."responses"' in query_text
    assert '"public"."responses"' not in query_text
    assert "ON CONFLICT (id) DO NOTHING" in query_text

    parameters = _execution_parameters(execution)

    assert len(parameters) == 11

    assert parameters[0] == response.id
    assert parameters[1] == response.request_id
    assert parameters[2] == response.status.value
    assert parameters[3] == _db_datetime(response.created_at)
    assert parameters[4] == _db_datetime(response.fetched_at)
    assert parameters[5] == response.payload_checksum
    assert parameters[6] == response.size_bytes
    assert parameters[7] == response.media_type
    assert parameters[8] == response.encoding
    assert parameters[9] == response.elapsed_ms

    adapter_meta_json = parameters[10]

    assert isinstance(
        adapter_meta_json,
        Jsonb,
    )
    assert adapter_meta_json.obj == response.adapter_meta


def test_insert_response_encodes_absent_adapter_meta_as_null() -> None:
    """Absent adapter metadata is passed to PostgreSQL as NULL."""

    response = _response(
        adapter_meta=None,
    )

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _response_row(response),
                ]
            ),
        ]
    )

    insert_response(
        _as_connection(connection),
        schema="dataio_test_abc",
        response=response,
    )

    parameters = _execution_parameters(connection.executions[0])

    assert parameters[10] is None


def test_insert_response_conflicts_on_observation_identity_not_payload_identity() -> (
    None
):
    """Persistence conflicts on R identity, never on payload checksum."""

    response = _response()

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _response_row(response),
                ]
            ),
        ]
    )

    insert_response(
        _as_connection(connection),
        schema="dataio_test_abc",
        response=response,
    )

    query_text = _query_text(connection.executions[0].query)

    assert "ON CONFLICT (id) DO NOTHING" in query_text
    assert "ON CONFLICT (payload_checksum)" not in query_text


# ---------------------------------------------------------------------------
# insert_response: persisted-state verification
# ---------------------------------------------------------------------------


def test_insert_response_accepts_matching_persisted_state() -> None:
    """Identical persisted observation state is an idempotent replay."""

    response = _response()

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _response_row(response),
                ]
            ),
        ]
    )

    insert_response(
        _as_connection(connection),
        schema="dataio_test_abc",
        response=response,
    )

    assert len(connection.executions) == 2

    assert "INSERT INTO" in _query_text(connection.executions[0].query)

    assert "SELECT" in _query_text(connection.executions[1].query)


def test_insert_response_rejects_conflicting_persisted_observation() -> None:
    """One Response ID cannot identify two different external observations."""

    requested = _response(
        response_id="response-1",
        payload_checksum=_checksum(b"original payload"),
        size_bytes=len(b"original payload"),
    )

    persisted = _response(
        response_id=requested.id,
        payload_checksum=_checksum(b"different payload"),
        size_bytes=len(b"different payload"),
    )

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _response_row(persisted),
                ]
            ),
        ]
    )

    with pytest.raises(
        ResponseConflictError,
        match=r"Persisted Response conflicts.*response-1",
    ):
        insert_response(
            _as_connection(connection),
            schema="dataio_test_abc",
            response=requested,
        )


def test_insert_response_rejects_missing_state_after_insert() -> None:
    """The requested observation must exist after insertion."""

    response = _response()

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(rows=[]),
        ]
    )

    with pytest.raises(
        ResponsePersistenceError,
        match=r"not present after insertion.*response-1",
    ):
        insert_response(
            _as_connection(connection),
            schema="dataio_test_abc",
            response=response,
        )


# ---------------------------------------------------------------------------
# insert_response: caller-state validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    (
        "response_observation",
        "error_match",
    ),
    [
        (
            _response(
                response_id="",
            ),
            r"id must be non-empty text",
        ),
        (
            _response(
                request_id="",
            ),
            r"request_id must be non-empty text",
        ),
        (
            _response(
                payload_checksum="not-a-hash",
            ),
            r"64-character lowercase hexadecimal",
        ),
        (
            _response(
                size_bytes=-1,
            ),
            r"size_bytes must be a non-negative integer",
        ),
        (
            _response(
                elapsed_ms=-1,
            ),
            r"elapsed_ms must be a non-negative integer",
        ),
    ],
)
def test_insert_response_rejects_invalid_state_before_sql(
    response_observation: Response,
    error_match: str,
) -> None:
    """Invalid persistence invariants fail before database access."""

    connection = FakeConnection()

    with pytest.raises(
        ResponsePersistenceError,
        match=error_match,
    ):
        insert_response(
            _as_connection(connection),
            schema="dataio_test_abc",
            response=response_observation,
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


def test_insert_response_rejects_noncanonical_created_at_before_sql() -> None:
    """Response timestamps must use the canonical MXM representation."""

    invalid_timestamp = cast(
        TSNSScalar,
        "2026-09-03T08:00:00Z",
    )

    response = _response(
        created_at=invalid_timestamp,
    )

    connection = FakeConnection()

    with pytest.raises(
        ResponsePersistenceError,
        match=r"created_at must be a valid canonical MXM timestamp",
    ):
        insert_response(
            _as_connection(connection),
            schema="dataio_test_abc",
            response=response,
        )

    assert connection.cursor_calls == 0


def test_insert_response_rejects_noncanonical_fetched_at_before_sql() -> None:
    """Both Response timestamps independently cross the SQL timestamp boundary."""

    invalid_timestamp = cast(
        TSNSScalar,
        "2026-09-03T08:00:01Z",
    )

    response = _response(
        fetched_at=invalid_timestamp,
    )

    connection = FakeConnection()

    with pytest.raises(
        ResponsePersistenceError,
        match=r"fetched_at must be a valid canonical MXM timestamp",
    ):
        insert_response(
            _as_connection(connection),
            schema="dataio_test_abc",
            response=response,
        )

    assert connection.cursor_calls == 0


@pytest.mark.parametrize(
    "field",
    [
        "created_at",
        "fetched_at",
    ],
)
def test_insert_response_rejects_non_microsecond_aligned_timestamp(
    field: str,
) -> None:
    """Persistence never silently truncates Response nanosecond timestamps."""

    base = _CREATED_AT if field == "created_at" else _FETCHED_AT

    invalid_timestamp = ts_ns_from_int(
        ts_ns_to_int(base) + 1,
    )

    if field == "created_at":
        response = _response(
            created_at=invalid_timestamp,
        )
    else:
        response = _response(
            fetched_at=invalid_timestamp,
        )

    connection = FakeConnection()

    with pytest.raises(
        ResponsePersistenceError,
        match=rf"{field} cannot be represented exactly.*microsecond precision",
    ):
        insert_response(
            _as_connection(connection),
            schema="dataio_test_abc",
            response=response,
        )

    assert connection.cursor_calls == 0


def test_insert_response_rejects_nested_non_json_adapter_meta_before_sql() -> None:
    """Invalid nested adapter metadata cannot cross the persistence boundary."""

    response = _response(
        adapter_meta={
            "measurement": {
                "quality": "good",
            },
        }
    )

    assert response.adapter_meta is not None

    mutable_meta = cast(
        dict[str, object],
        response.adapter_meta,
    )

    mutable_meta["bad"] = object()

    connection = FakeConnection()

    with pytest.raises(
        ResponsePersistenceError,
        match=r"adapter_meta\.bad must contain JSON-compatible values",
    ):
        insert_response(
            _as_connection(connection),
            schema="dataio_test_abc",
            response=response,
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


def test_insert_response_rejects_non_finite_adapter_meta_float_before_sql() -> None:
    """Adapter metadata must remain representable by the DataIO JSON model."""

    response = _response(
        adapter_meta={
            "latency_ratio": float("nan"),
        }
    )

    connection = FakeConnection()

    with pytest.raises(
        ResponsePersistenceError,
        match=r"adapter_meta\.latency_ratio must contain JSON-compatible values",
    ):
        insert_response(
            _as_connection(connection),
            schema="dataio_test_abc",
            response=response,
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


# ---------------------------------------------------------------------------
# Transaction ownership
# ---------------------------------------------------------------------------


def test_response_operations_do_not_control_transactions() -> None:
    """Response SQL helpers neither commit nor roll back transactions."""

    response = _response()

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _response_row(response),
                ]
            ),
        ]
    )

    insert_response(
        _as_connection(connection),
        schema="dataio_test_abc",
        response=response,
    )

    assert connection.commit_calls == 0
    assert connection.rollback_calls == 0
