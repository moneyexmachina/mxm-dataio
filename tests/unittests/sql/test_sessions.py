"""Unit tests for plain-SQL DataIO session persistence operations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal, Self, cast

import pytest
from psycopg import Connection, sql

from mxm.dataio.models import Session, SessionMode
from mxm.dataio.sql.postgres import PostgresRow
from mxm.dataio.sql.sessions import (
    SessionConflictError,
    SessionPersistenceError,
    fetch_latest_session_id,
    fetch_session_by_id,
    insert_session,
    mark_session_ended,
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
# Session fixtures
# ---------------------------------------------------------------------------


_STARTED_AT = datetime(2026, 9, 2, 9, 0, tzinfo=UTC)
_ENDED_AT = datetime(2026, 9, 2, 9, 30, tzinfo=UTC)


def _session(
    *,
    session_id: str = "session-1",
    source: str = "example-source",
    mode: SessionMode = SessionMode.SYNC,
    started_at: datetime = _STARTED_AT,
    ended_at: datetime | None = None,
) -> Session:
    """Construct one representative DataIO session."""

    return Session(
        id=session_id,
        source=source,
        mode=mode,
        started_at=started_at,
        ended_at=ended_at,
    )


def _session_row(
    session: Session,
) -> PostgresRow:
    """Encode one session as a PostgreSQL result row."""

    return (
        session.id,
        session.source,
        session.mode.value,
        session.started_at,
        session.ended_at,
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


# ---------------------------------------------------------------------------
# fetch_session_by_id
# ---------------------------------------------------------------------------


def test_fetch_session_by_id_returns_none_when_absent() -> None:
    """A missing session ID returns no persisted session."""

    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    session = fetch_session_by_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        session_id="session-1",
    )

    assert session is None
    assert connection.cursor_calls == 1


def test_fetch_session_by_id_reconstructs_session() -> None:
    """A persisted row reconstructs the complete Session object."""

    expected = _session(
        mode=SessionMode.BATCH,
        ended_at=_ENDED_AT,
    )

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _session_row(expected),
                ]
            ),
        ]
    )

    session = fetch_session_by_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        session_id=expected.id,
    )

    assert session == expected


def test_fetch_session_by_id_uses_configured_schema_and_identity() -> None:
    """Session lookup uses the caller-provided schema and requested ID."""

    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    fetch_session_by_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        session_id="session-1",
    )

    execution = _single_execution(connection)
    query_text = _query_text(execution.query)

    assert '"dataio_test_abc"."sessions"' in query_text
    assert '"public"."sessions"' not in query_text
    assert "WHERE id = %s" in query_text
    assert execution.parameters == ("session-1",)


def test_fetch_session_by_id_rejects_invalid_identifier_before_sql() -> None:
    """Invalid requested session identity fails before database access."""

    connection = FakeConnection()

    with pytest.raises(
        SessionPersistenceError,
        match=r"session_id must be non-empty text",
    ):
        fetch_session_by_id(
            _as_connection(connection),
            schema="dataio_test_abc",
            session_id="",
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


def test_fetch_session_by_id_rejects_multiple_database_rows() -> None:
    """One session ID cannot reconstruct from multiple persisted rows."""

    session = _session()

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _session_row(session),
                    _session_row(session),
                ]
            ),
        ]
    )

    with pytest.raises(
        SessionPersistenceError,
        match=r"multiple rows.*session-1",
    ):
        fetch_session_by_id(
            _as_connection(connection),
            schema="dataio_test_abc",
            session_id=session.id,
        )


_SESSION_ROW = _session_row(_session())


@pytest.mark.parametrize(
    (
        "row",
        "error_match",
    ),
    [
        (
            _row(*_SESSION_ROW[:-1]),
            r"unexpected row shape",
        ),
        (
            _row(*_SESSION_ROW, "extra"),
            r"unexpected row shape",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                0,
                123,
            ),
            r"id must be non-empty text",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                0,
                "",
            ),
            r"id must be non-empty text",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                1,
                123,
            ),
            r"source must be non-empty text",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                1,
                "",
            ),
            r"source must be non-empty text",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                2,
                "unknown-mode",
            ),
            r"mode is not recognised",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                3,
                "2026-09-02T09:00:00Z",
            ),
            r"started_at must be a datetime",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                3,
                datetime(
                    2026,
                    9,
                    2,
                    9,
                    0,
                ),
            ),
            r"started_at must be timezone-aware",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                4,
                "2026-09-02T09:30:00Z",
            ),
            r"ended_at must be a datetime",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                4,
                datetime(
                    2026,
                    9,
                    2,
                    9,
                    30,
                ),
            ),
            r"ended_at must be timezone-aware",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                4,
                datetime(
                    2026,
                    9,
                    2,
                    8,
                    59,
                    tzinfo=UTC,
                ),
            ),
            r"ended_at must not be before started_at",
        ),
    ],
)
def test_fetch_session_by_id_rejects_invalid_rows(
    row: PostgresRow,
    error_match: str,
) -> None:
    """Malformed persisted session rows cannot enter the domain."""

    connection = FakeConnection(
        [
            FakeCursor(rows=[row]),
        ]
    )

    with pytest.raises(
        SessionPersistenceError,
        match=error_match,
    ):
        fetch_session_by_id(
            _as_connection(connection),
            schema="dataio_test_abc",
            session_id="session-1",
        )


# ---------------------------------------------------------------------------
# fetch_latest_session_id
# ---------------------------------------------------------------------------


def test_fetch_latest_session_id_returns_none_when_source_has_no_sessions() -> None:
    """A source with no persisted sessions has no latest session ID."""

    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    session_id = fetch_latest_session_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        source="example-source",
    )

    assert session_id is None


def test_fetch_latest_session_id_returns_persisted_identity() -> None:
    """The latest-session query returns its persisted session ID."""

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    ("session-latest",),
                ]
            ),
        ]
    )

    session_id = fetch_latest_session_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        source="example-source",
    )

    assert session_id == "session-latest"


def test_fetch_latest_session_id_uses_source_and_deterministic_ordering() -> None:
    """Latest-session lookup is source-specific with deterministic ties."""

    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    fetch_latest_session_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        source="example-source",
    )

    execution = _single_execution(connection)
    query_text = _query_text(execution.query)

    assert '"dataio_test_abc"."sessions"' in query_text
    assert "WHERE source = %s" in query_text
    assert "ORDER BY started_at DESC, id DESC" in query_text
    assert "LIMIT 1" in query_text
    assert execution.parameters == ("example-source",)


def test_fetch_latest_session_id_rejects_invalid_source_before_sql() -> None:
    """Invalid source identity fails before database access."""

    connection = FakeConnection()

    with pytest.raises(
        SessionPersistenceError,
        match=r"source must be non-empty text",
    ):
        fetch_latest_session_id(
            _as_connection(connection),
            schema="dataio_test_abc",
            source="",
        )

    assert connection.cursor_calls == 0


@pytest.mark.parametrize(
    "rows",
    [
        [
            ("session-a",),
            ("session-b",),
        ],
        [
            ("session-a", "extra"),
        ],
        [
            (123,),
        ],
        [
            ("",),
        ],
    ],
)
def test_fetch_latest_session_id_rejects_invalid_result(
    rows: list[PostgresRow],
) -> None:
    """Malformed latest-session query results are rejected."""

    connection = FakeConnection(
        [
            FakeCursor(rows=rows),
        ]
    )

    with pytest.raises(SessionPersistenceError):
        fetch_latest_session_id(
            _as_connection(connection),
            schema="dataio_test_abc",
            source="example-source",
        )


# ---------------------------------------------------------------------------
# insert_session
# ---------------------------------------------------------------------------


def test_insert_session_encodes_complete_session_state() -> None:
    """One Session is encoded into the expected SQL values."""

    session = _session(
        mode=SessionMode.ASYNC,
        ended_at=_ENDED_AT,
    )

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _session_row(session),
                ]
            ),
        ]
    )

    insert_session(
        _as_connection(connection),
        schema="dataio_test_abc",
        session=session,
    )

    execution = connection.executions[0]
    query_text = _query_text(execution.query)

    assert '"dataio_test_abc"."sessions"' in query_text
    assert "ON CONFLICT (id) DO NOTHING" in query_text

    assert execution.parameters == (
        session.id,
        session.source,
        session.mode.value,
        session.started_at,
        session.ended_at,
    )


def test_insert_session_accepts_matching_persisted_state() -> None:
    """An identical existing session is an idempotent replay."""

    session = _session()

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _session_row(session),
                ]
            ),
        ]
    )

    insert_session(
        _as_connection(connection),
        schema="dataio_test_abc",
        session=session,
    )

    assert len(connection.executions) == 2
    assert "INSERT INTO" in _query_text(connection.executions[0].query)
    assert "SELECT" in _query_text(connection.executions[1].query)


def test_insert_session_rejects_conflicting_persisted_state() -> None:
    """One session ID cannot identify different session state."""

    requested = _session()

    persisted = replace(
        requested,
        source="different-source",
    )

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _session_row(persisted),
                ]
            ),
        ]
    )

    with pytest.raises(
        SessionConflictError,
        match=r"Persisted session conflicts.*session-1",
    ):
        insert_session(
            _as_connection(connection),
            schema="dataio_test_abc",
            session=requested,
        )


def test_insert_session_rejects_missing_state_after_insert() -> None:
    """A requested session must exist after insertion."""

    session = _session()

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(rows=[]),
        ]
    )

    with pytest.raises(
        SessionPersistenceError,
        match=r"not present after insertion.*session-1",
    ):
        insert_session(
            _as_connection(connection),
            schema="dataio_test_abc",
            session=session,
        )


@pytest.mark.parametrize(
    (
        "session",
        "error_match",
    ),
    [
        (
            _session(session_id=""),
            r"id must be non-empty text",
        ),
        (
            _session(source=""),
            r"source must be non-empty text",
        ),
        (
            _session(
                started_at=datetime(
                    2026,
                    9,
                    2,
                    9,
                    0,
                ),
            ),
            r"started_at must be timezone-aware",
        ),
        (
            _session(
                ended_at=datetime(
                    2026,
                    9,
                    2,
                    9,
                    30,
                ),
            ),
            r"ended_at must be timezone-aware",
        ),
        (
            _session(
                ended_at=datetime(
                    2026,
                    9,
                    2,
                    8,
                    59,
                    tzinfo=UTC,
                ),
            ),
            r"ended_at must not be before started_at",
        ),
    ],
)
def test_insert_session_rejects_invalid_state_before_sql(
    session: Session,
    error_match: str,
) -> None:
    """Invalid persistence invariants fail before database access."""

    connection = FakeConnection()

    with pytest.raises(
        SessionPersistenceError,
        match=error_match,
    ):
        insert_session(
            _as_connection(connection),
            schema="dataio_test_abc",
            session=session,
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


# ---------------------------------------------------------------------------
# mark_session_ended
# ---------------------------------------------------------------------------


def test_mark_session_ended_records_completion() -> None:
    """An open persisted session can be completed once."""

    open_session = _session()

    ended_session = replace(
        open_session,
        ended_at=_ENDED_AT,
    )

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _session_row(open_session),
                ]
            ),
            FakeCursor(),
            FakeCursor(
                rows=[
                    _session_row(ended_session),
                ]
            ),
        ]
    )

    mark_session_ended(
        _as_connection(connection),
        schema="dataio_test_abc",
        session_id=open_session.id,
        ended_at=_ENDED_AT,
    )

    assert len(connection.executions) == 3

    update_execution = connection.executions[1]
    update_query = _query_text(update_execution.query)

    assert '"dataio_test_abc"."sessions"' in update_query
    assert "SET ended_at = %s" in update_query
    assert "WHERE id = %s" in update_query
    assert "ended_at IS NULL" in update_query

    assert update_execution.parameters == (
        _ENDED_AT,
        open_session.id,
    )


def test_mark_session_ended_is_idempotent_for_same_timestamp() -> None:
    """Repeating the same completion timestamp performs no update."""

    ended_session = _session(
        ended_at=_ENDED_AT,
    )

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _session_row(ended_session),
                ]
            ),
        ]
    )

    mark_session_ended(
        _as_connection(connection),
        schema="dataio_test_abc",
        session_id=ended_session.id,
        ended_at=_ENDED_AT,
    )

    assert connection.cursor_calls == 1
    assert len(connection.executions) == 1

    query_text = _query_text(connection.executions[0].query)

    assert "SELECT" in query_text
    assert "UPDATE" not in query_text


def test_mark_session_ended_rejects_different_existing_completion() -> None:
    """A recorded session completion cannot be rewritten."""

    existing_end = datetime(
        2026,
        9,
        2,
        9,
        20,
        tzinfo=UTC,
    )

    ended_session = _session(
        ended_at=existing_end,
    )

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _session_row(ended_session),
                ]
            ),
        ]
    )

    with pytest.raises(
        SessionConflictError,
        match=r"already has a different completion time",
    ):
        mark_session_ended(
            _as_connection(connection),
            schema="dataio_test_abc",
            session_id=ended_session.id,
            ended_at=_ENDED_AT,
        )

    assert connection.cursor_calls == 1


def test_mark_session_ended_rejects_unknown_session() -> None:
    """A nonexistent session cannot be completed."""

    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    with pytest.raises(
        SessionPersistenceError,
        match=r"not persisted.*session-1",
    ):
        mark_session_ended(
            _as_connection(connection),
            schema="dataio_test_abc",
            session_id="session-1",
            ended_at=_ENDED_AT,
        )


def test_mark_session_ended_rejects_completion_before_start() -> None:
    """A session cannot complete before its persisted start time."""

    session = _session()

    invalid_end = datetime(
        2026,
        9,
        2,
        8,
        59,
        tzinfo=UTC,
    )

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _session_row(session),
                ]
            ),
        ]
    )

    with pytest.raises(
        SessionPersistenceError,
        match=r"ended_at must not be before started_at",
    ):
        mark_session_ended(
            _as_connection(connection),
            schema="dataio_test_abc",
            session_id=session.id,
            ended_at=invalid_end,
        )

    assert connection.cursor_calls == 1


def test_mark_session_ended_rejects_naive_timestamp_before_sql() -> None:
    """Completion timestamps must be timezone-aware."""

    connection = FakeConnection()

    with pytest.raises(
        SessionPersistenceError,
        match=r"ended_at must be timezone-aware",
    ):
        mark_session_ended(
            _as_connection(connection),
            schema="dataio_test_abc",
            session_id="session-1",
            ended_at=datetime(
                2026,
                9,
                2,
                9,
                30,
            ),
        )

    assert connection.cursor_calls == 0


def test_mark_session_ended_rejects_invalid_session_id_before_sql() -> None:
    """Completion requires a valid persisted session identity."""

    connection = FakeConnection()

    with pytest.raises(
        SessionPersistenceError,
        match=r"session_id must be non-empty text",
    ):
        mark_session_ended(
            _as_connection(connection),
            schema="dataio_test_abc",
            session_id="",
            ended_at=_ENDED_AT,
        )

    assert connection.cursor_calls == 0


def test_mark_session_ended_rejects_unexpected_post_update_state() -> None:
    """Completion must be visible with the requested value after updating."""

    open_session = _session()

    still_open_session = _session()

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _session_row(open_session),
                ]
            ),
            FakeCursor(),
            FakeCursor(
                rows=[
                    _session_row(still_open_session),
                ]
            ),
        ]
    )

    with pytest.raises(
        SessionConflictError,
        match=r"completion differs from requested value",
    ):
        mark_session_ended(
            _as_connection(connection),
            schema="dataio_test_abc",
            session_id=open_session.id,
            ended_at=_ENDED_AT,
        )


# ---------------------------------------------------------------------------
# Transaction ownership
# ---------------------------------------------------------------------------


def test_session_operations_do_not_control_transactions() -> None:
    """Session SQL helpers neither commit nor roll back transactions."""

    session = _session()

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _session_row(session),
                ]
            ),
        ]
    )

    insert_session(
        _as_connection(connection),
        schema="dataio_test_abc",
        session=session,
    )

    assert connection.commit_calls == 0
    assert connection.rollback_calls == 0
