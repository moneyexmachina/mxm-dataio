"""Unit tests for plain-SQL DataIO request persistence operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Self, cast

import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from mxm.dataio.models import Request, RequestMethod
from mxm.dataio.sql.postgres import PostgresRow
from mxm.dataio.sql.requests import (
    RequestConflictError,
    RequestPersistenceError,
    fetch_request_by_id,
    fetch_requests_by_hash,
    insert_request,
)
from mxm.types import JSONLike, JSONObj

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
        """Enter the fake cursor context."""

        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        """Exit the fake cursor context."""

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
        """Return the next scripted cursor."""

        self.cursor_calls += 1

        if not self._cursors:
            raise AssertionError(
                "Unexpected cursor request: no scripted cursor remains"
            )

        return self._cursors.pop(0)

    def commit(self) -> None:
        """Record an unexpected commit request."""

        self.commit_calls += 1

    def rollback(self) -> None:
        """Record an unexpected rollback request."""

        self.rollback_calls += 1


def _as_connection(
    connection: FakeConnection,
) -> Connection[PostgresRow]:
    """Cast a fake connection to the production connection type."""

    return cast(
        Connection[PostgresRow],
        connection,
    )


# ---------------------------------------------------------------------------
# Request fixtures
# ---------------------------------------------------------------------------


_CREATED_AT = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)


def _request(
    *,
    request_id: str = "request-1",
    session_id: str = "session-1",
    kind: str = "prices",
    method: RequestMethod = RequestMethod.GET,
    params: JSONObj | None = None,
    body: JSONLike | None = None,
    created_at: datetime = _CREATED_AT,
) -> Request:
    """Construct one representative immutable request occurrence."""

    return Request(
        id=request_id,
        session_id=session_id,
        kind=kind,
        method=method,
        params=params,
        body=body,
        created_at=created_at,
    )


def _request_row(
    request: Request,
) -> PostgresRow:
    """Encode one Request as a PostgreSQL result row."""

    return (
        request.id,
        request.session_id,
        request.kind,
        request.method.value,
        request.params,
        request.body,
        request.hash,
        request.created_at,
    )


# ---------------------------------------------------------------------------
# PostgreSQL row and execution helpers
# ---------------------------------------------------------------------------


def _row(
    *values: object,
) -> PostgresRow:
    """Construct an arbitrary PostgreSQL result row."""

    return tuple(values)


def _row_with_value(
    row: PostgresRow,
    index: int,
    value: object,
) -> PostgresRow:
    """Return a PostgreSQL row with one value replaced."""

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
    """Render and normalise one composed SQL query."""

    return " ".join(query.as_string().split())


def _single_execution(
    connection: FakeConnection,
) -> Execution:
    """Return the sole recorded SQL operation."""

    assert len(connection.executions) == 1

    return connection.executions[0]


def _execution_parameters(
    execution: Execution,
) -> tuple[object, ...]:
    """Return typed parameters from one execute operation."""

    parameters = execution.parameters

    if not isinstance(
        parameters,
        tuple,
    ):
        raise AssertionError(f"Expected tuple execution parameters, got {parameters!r}")

    return cast(
        tuple[object, ...],
        parameters,
    )


# ---------------------------------------------------------------------------
# Question identity
# ---------------------------------------------------------------------------


def test_request_occurrences_can_share_question_identity() -> None:
    """Different occurrences may represent exactly the same question."""

    first = _request(
        request_id="request-1",
        session_id="session-1",
        params={
            "symbol": "ES",
        },
    )

    second = _request(
        request_id="request-2",
        session_id="session-2",
        params={
            "symbol": "ES",
        },
    )

    assert first.id != second.id
    assert first.session_id != second.session_id
    assert first.hash == second.hash


@pytest.mark.parametrize(
    "second",
    [
        _request(
            request_id="request-2",
            session_id="different-session",
        ),
        _request(
            request_id="request-2",
            created_at=datetime(
                2026,
                9,
                3,
                11,
                0,
                tzinfo=UTC,
            ),
        ),
    ],
)
def test_occurrence_metadata_does_not_change_question_hash(
    second: Request,
) -> None:
    """Occurrence identity and timing do not participate in question identity."""

    first = _request(
        request_id="request-1",
    )

    assert first.hash == second.hash


@pytest.mark.parametrize(
    "second",
    [
        _request(
            request_id="request-2",
            kind="settlements",
        ),
        _request(
            request_id="request-2",
            method=RequestMethod.POST,
        ),
        _request(
            request_id="request-2",
            params={
                "symbol": "DIFFERENT",
            },
        ),
        _request(
            request_id="request-2",
            body={
                "command": "different",
            },
        ),
    ],
)
def test_question_fields_change_request_hash(
    second: Request,
) -> None:
    """Changing one question-bearing field changes question identity."""

    first = _request(
        request_id="request-1",
    )

    assert first.hash != second.hash


# ---------------------------------------------------------------------------
# fetch_request_by_id
# ---------------------------------------------------------------------------


def test_fetch_request_by_id_returns_none_when_absent() -> None:
    """A missing request occurrence returns no persisted request."""

    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    request = fetch_request_by_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        request_id="request-1",
    )

    assert request is None
    assert connection.cursor_calls == 1


def test_fetch_request_by_id_reconstructs_request() -> None:
    """A persisted row reconstructs the complete Request object."""

    expected = _request(
        method=RequestMethod.POST,
        params={
            "symbol": "TEST",
            "limit": 10,
        },
        body={
            "query": "value",
        },
    )

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _request_row(expected),
                ]
            ),
        ]
    )

    request = fetch_request_by_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        request_id=expected.id,
    )

    assert request == expected


def test_fetch_request_by_id_uses_configured_schema_and_identity() -> None:
    """Request lookup uses the configured schema and occurrence ID."""

    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    fetch_request_by_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        request_id="request-1",
    )

    execution = _single_execution(connection)
    query_text = _query_text(execution.query)

    assert '"dataio_test_abc"."requests"' in query_text
    assert '"public"."requests"' not in query_text
    assert "WHERE id = %s" in query_text
    assert execution.parameters == ("request-1",)


def test_fetch_request_by_id_rejects_invalid_identifier_before_sql() -> None:
    """Invalid occurrence identity fails before database access."""

    connection = FakeConnection()

    with pytest.raises(
        RequestPersistenceError,
        match=r"request_id must be non-empty text",
    ):
        fetch_request_by_id(
            _as_connection(connection),
            schema="dataio_test_abc",
            request_id="",
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


def test_fetch_request_by_id_rejects_multiple_database_rows() -> None:
    """One occurrence ID cannot reconstruct from multiple persisted rows."""

    request = _request()

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _request_row(request),
                    _request_row(request),
                ]
            ),
        ]
    )

    with pytest.raises(
        RequestPersistenceError,
        match=r"multiple rows.*request-1",
    ):
        fetch_request_by_id(
            _as_connection(connection),
            schema="dataio_test_abc",
            request_id=request.id,
        )


_REQUEST_ROW = _request_row(_request())


@pytest.mark.parametrize(
    (
        "row",
        "error_match",
    ),
    [
        (
            _row(*_REQUEST_ROW[:-1]),
            r"unexpected row shape",
        ),
        (
            _row(*_REQUEST_ROW, "extra"),
            r"unexpected row shape",
        ),
        (
            _row_with_value(
                _REQUEST_ROW,
                0,
                123,
            ),
            r"id must be non-empty text",
        ),
        (
            _row_with_value(
                _REQUEST_ROW,
                1,
                123,
            ),
            r"session_id must be non-empty text",
        ),
        (
            _row_with_value(
                _REQUEST_ROW,
                2,
                "",
            ),
            r"kind must be non-empty text",
        ),
        (
            _row_with_value(
                _REQUEST_ROW,
                3,
                "NOT_A_METHOD",
            ),
            r"method is not recognised",
        ),
        (
            _row_with_value(
                _REQUEST_ROW,
                4,
                ["not-an-object"],
            ),
            r"params must be a JSON object",
        ),
        (
            _row_with_value(
                _REQUEST_ROW,
                5,
                object(),
            ),
            r"body must contain JSON-compatible values",
        ),
        (
            _row_with_value(
                _REQUEST_ROW,
                6,
                "not-a-hash",
            ),
            r"64-character lowercase hexadecimal",
        ),
        (
            _row_with_value(
                _REQUEST_ROW,
                7,
                "2026-09-03T10:00:00Z",
            ),
            r"created_at must be a datetime",
        ),
        (
            _row_with_value(
                _REQUEST_ROW,
                7,
                datetime(
                    2026,
                    9,
                    3,
                    10,
                    0,
                ),
            ),
            r"created_at must be timezone-aware",
        ),
    ],
)
def test_fetch_request_by_id_rejects_invalid_rows(
    row: PostgresRow,
    error_match: str,
) -> None:
    """Malformed persisted request rows cannot enter the domain."""

    connection = FakeConnection(
        [
            FakeCursor(rows=[row]),
        ]
    )

    with pytest.raises(
        RequestPersistenceError,
        match=error_match,
    ):
        fetch_request_by_id(
            _as_connection(connection),
            schema="dataio_test_abc",
            request_id="request-1",
        )


def test_fetch_request_by_id_rejects_hash_inconsistent_with_question() -> None:
    """Persisted hash must agree with the question-bearing fields."""

    row = _row_with_value(
        _REQUEST_ROW,
        6,
        "0" * 64,
    )

    connection = FakeConnection(
        [
            FakeCursor(rows=[row]),
        ]
    )

    with pytest.raises(
        RequestPersistenceError,
        match=r"hash is inconsistent with question identity",
    ):
        fetch_request_by_id(
            _as_connection(connection),
            schema="dataio_test_abc",
            request_id="request-1",
        )


# ---------------------------------------------------------------------------
# fetch_requests_by_hash
# ---------------------------------------------------------------------------


def test_fetch_requests_by_hash_returns_empty_mapping() -> None:
    """A question identity with no occurrences produces an empty mapping."""

    request = _request()

    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    requests = fetch_requests_by_hash(
        _as_connection(connection),
        schema="dataio_test_abc",
        request_hash=request.hash,
    )

    assert requests == {}


def test_fetch_requests_by_hash_preserves_occurrences_across_sessions() -> None:
    """Occurrences sharing a question hash remain independently represented."""

    first = _request(
        request_id="request-1",
        session_id="session-1",
        params={
            "symbol": "ES",
        },
        created_at=datetime(
            2026,
            9,
            3,
            10,
            0,
            tzinfo=UTC,
        ),
    )

    second = _request(
        request_id="request-2",
        session_id="session-2",
        params={
            "symbol": "ES",
        },
        created_at=datetime(
            2026,
            9,
            3,
            11,
            0,
            tzinfo=UTC,
        ),
    )

    assert first.hash == second.hash
    assert first.id != second.id
    assert first.session_id != second.session_id

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _request_row(first),
                    _request_row(second),
                ]
            ),
        ]
    )

    requests = fetch_requests_by_hash(
        _as_connection(connection),
        schema="dataio_test_abc",
        request_hash=first.hash,
    )

    assert requests == {
        first.id: first,
        second.id: second,
    }


def test_fetch_requests_by_hash_uses_hash_filter_and_deterministic_ordering() -> None:
    """Question lookup uses hash and deterministic occurrence ordering."""

    request = _request()

    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    fetch_requests_by_hash(
        _as_connection(connection),
        schema="dataio_test_abc",
        request_hash=request.hash,
    )

    execution = _single_execution(connection)
    query_text = _query_text(execution.query)

    assert '"dataio_test_abc"."requests"' in query_text
    assert "WHERE hash = %s" in query_text
    assert "ORDER BY created_at, id" in query_text
    assert execution.parameters == (request.hash,)


def test_fetch_requests_by_hash_rejects_invalid_hash_before_sql() -> None:
    """Question identity must be a valid SHA-256 value."""

    connection = FakeConnection()

    with pytest.raises(
        RequestPersistenceError,
        match=r"64-character lowercase hexadecimal",
    ):
        fetch_requests_by_hash(
            _as_connection(connection),
            schema="dataio_test_abc",
            request_hash="not-a-hash",
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


def test_fetch_requests_by_hash_rejects_duplicate_occurrence_identity() -> None:
    """A result set cannot contain one request occurrence twice."""

    request = _request()

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _request_row(request),
                    _request_row(request),
                ]
            ),
        ]
    )

    with pytest.raises(
        RequestPersistenceError,
        match=r"duplicate request occurrence.*request-1",
    ):
        fetch_requests_by_hash(
            _as_connection(connection),
            schema="dataio_test_abc",
            request_hash=request.hash,
        )


# ---------------------------------------------------------------------------
# insert_request: encoding
# ---------------------------------------------------------------------------


def test_insert_request_encodes_complete_request_state() -> None:
    """One Request is encoded into the expected SQL representation."""

    request = _request(
        method=RequestMethod.POST,
        params={
            "symbol": "TEST",
            "limit": 10,
        },
        body={
            "query": [
                "a",
                "b",
            ],
        },
    )

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _request_row(request),
                ]
            ),
        ]
    )

    insert_request(
        _as_connection(connection),
        schema="dataio_test_abc",
        request=request,
    )

    execution = connection.executions[0]
    query_text = _query_text(execution.query)

    assert '"dataio_test_abc"."requests"' in query_text
    assert '"public"."requests"' not in query_text
    assert "ON CONFLICT (id) DO NOTHING" in query_text

    parameters = _execution_parameters(execution)

    assert len(parameters) == 8

    assert parameters[0] == request.id
    assert parameters[1] == request.session_id
    assert parameters[2] == request.kind
    assert parameters[3] == request.method.value

    params_json = parameters[4]
    assert isinstance(
        params_json,
        Jsonb,
    )
    assert params_json.obj == request.params

    body_json = parameters[5]
    assert isinstance(
        body_json,
        Jsonb,
    )
    assert body_json.obj == request.body

    assert parameters[6] == request.hash
    assert parameters[7] == request.created_at


def test_insert_request_encodes_absent_json_values_as_null() -> None:
    """Absent params and body are passed to PostgreSQL as NULL."""

    request = _request(
        params=None,
        body=None,
    )

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _request_row(request),
                ]
            ),
        ]
    )

    insert_request(
        _as_connection(connection),
        schema="dataio_test_abc",
        request=request,
    )

    parameters = _execution_parameters(connection.executions[0])

    assert parameters[4] is None
    assert parameters[5] is None


def test_insert_request_uses_occurrence_identity_conflict_clause() -> None:
    """Persistence conflicts on Q identity, not question hash."""

    request = _request()

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _request_row(request),
                ]
            ),
        ]
    )

    insert_request(
        _as_connection(connection),
        schema="dataio_test_abc",
        request=request,
    )

    query_text = _query_text(connection.executions[0].query)

    assert "ON CONFLICT (id) DO NOTHING" in query_text
    assert "ON CONFLICT (hash)" not in query_text


# ---------------------------------------------------------------------------
# insert_request: persisted-state verification
# ---------------------------------------------------------------------------


def test_insert_request_accepts_matching_persisted_state() -> None:
    """Identical persisted occurrence state is an idempotent replay."""

    request = _request()

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _request_row(request),
                ]
            ),
        ]
    )

    insert_request(
        _as_connection(connection),
        schema="dataio_test_abc",
        request=request,
    )

    assert len(connection.executions) == 2
    assert "INSERT INTO" in _query_text(connection.executions[0].query)
    assert "SELECT" in _query_text(connection.executions[1].query)


def test_insert_request_rejects_conflicting_persisted_occurrence() -> None:
    """One request ID cannot identify two different occurrences."""

    requested = _request()

    persisted = _request(
        request_id=requested.id,
        kind="different-kind",
    )

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _request_row(persisted),
                ]
            ),
        ]
    )

    with pytest.raises(
        RequestConflictError,
        match=r"Persisted request conflicts.*request-1",
    ):
        insert_request(
            _as_connection(connection),
            schema="dataio_test_abc",
            request=requested,
        )


def test_insert_request_rejects_missing_state_after_insert() -> None:
    """The requested occurrence must exist after insertion."""

    request = _request()

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(rows=[]),
        ]
    )

    with pytest.raises(
        RequestPersistenceError,
        match=r"not present after insertion.*request-1",
    ):
        insert_request(
            _as_connection(connection),
            schema="dataio_test_abc",
            request=request,
        )


# ---------------------------------------------------------------------------
# insert_request: caller-state validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    (
        "request_occurrence",
        "error_match",
    ),
    [
        (
            _request(
                request_id="",
            ),
            r"id must be non-empty text",
        ),
        (
            _request(
                session_id="",
            ),
            r"session_id must be non-empty text",
        ),
        (
            _request(
                kind="",
            ),
            r"kind must be non-empty text",
        ),
        (
            _request(
                created_at=datetime(
                    2026,
                    9,
                    3,
                    10,
                    0,
                ),
            ),
            r"created_at must be timezone-aware",
        ),
    ],
)
def test_insert_request_rejects_invalid_state_before_sql(
    request_occurrence: Request,
    error_match: str,
) -> None:
    """Invalid persistence invariants fail before database access."""

    connection = FakeConnection()

    with pytest.raises(
        RequestPersistenceError,
        match=error_match,
    ):
        insert_request(
            _as_connection(connection),
            schema="dataio_test_abc",
            request=request_occurrence,
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


def test_insert_request_rejects_nested_identity_mutation_before_sql() -> None:
    """Mutation inside question-bearing JSON is detected before persistence."""

    request = _request(
        params={
            "symbol": "TEST",
        }
    )

    assert request.params is not None

    mutable_params = cast(
        dict[str, JSONLike],
        request.params,
    )
    mutable_params["symbol"] = "DIFFERENT"

    connection = FakeConnection()

    with pytest.raises(
        RequestPersistenceError,
        match=r"hash is inconsistent with question identity",
    ):
        insert_request(
            _as_connection(connection),
            schema="dataio_test_abc",
            request=request,
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


def test_insert_request_rejects_nested_non_json_params_before_sql() -> None:
    """Invalid nested params state cannot cross the persistence boundary."""

    request = _request(
        params={
            "bad": "initially-valid",
        }
    )

    assert request.params is not None

    mutable_params = cast(
        dict[str, object],
        request.params,
    )
    mutable_params["bad"] = object()

    connection = FakeConnection()

    with pytest.raises(
        RequestPersistenceError,
        match=r"params\.bad must contain JSON-compatible values",
    ):
        insert_request(
            _as_connection(connection),
            schema="dataio_test_abc",
            request=request,
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


# ---------------------------------------------------------------------------
# Transaction ownership
# ---------------------------------------------------------------------------


def test_request_operations_do_not_control_transactions() -> None:
    """Request SQL helpers neither commit nor roll back transactions."""

    request = _request()

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _request_row(request),
                ]
            ),
        ]
    )

    insert_request(
        _as_connection(connection),
        schema="dataio_test_abc",
        request=request,
    )

    assert connection.commit_calls == 0
    assert connection.rollback_calls == 0
