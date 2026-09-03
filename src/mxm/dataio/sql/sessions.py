"""Plain-SQL persistence operations for DataIO sessions.

This module owns the PostgreSQL representation of ``Session`` objects.

A session is an operational grouping of request occurrences for one external
source. It also records the effective DataIO handling context under which those
requests are resolved: cache policy, TTL, and opaque reuse-context
discriminators.

It does not define temporal semantics for the observations collected within it.

All functions operate on a caller-provided Psycopg connection. They do not
open, commit, or roll back transactions. Transaction ownership belongs to the
higher-level DataIO operation.
"""

from __future__ import annotations

import math
from datetime import datetime

from psycopg import Connection, sql

from mxm.dataio.models import CacheMode, Session, SessionMode
from mxm.dataio.sql.postgres import PostgresRow

type ExecutableQuery = sql.SQL | sql.Composed


class SessionPersistenceError(RuntimeError):
    """Base error for invalid or inconsistent persisted session state."""


class SessionConflictError(SessionPersistenceError):
    """Raised when one session ID identifies different session state."""


# ---------------------------------------------------------------------
# SESSION READS
# ---------------------------------------------------------------------


def fetch_session_by_id(
    connection: Connection[PostgresRow],
    *,
    schema: str,
    session_id: str,
) -> Session | None:
    """Return one persisted session by ID, if present.

    Args:
        connection:
            Active Psycopg connection owned by the caller.
        schema:
            PostgreSQL schema containing the ``sessions`` table.
        session_id:
            Session identifier to retrieve.

    Returns:
        The persisted session, or ``None`` when no such session exists.
    """

    _validate_identifier(
        session_id,
        field="session_id",
    )
    query = sql.SQL(
        """
        SELECT
            id,
            source,
            mode,
            cache_mode,
            ttl_seconds,
            as_of_bucket,
            cache_tag,
            started_at,
            ended_at
        FROM {}
        WHERE id = %s
        """
    ).format(
        sql.Identifier(
            schema,
            "sessions",
        )
    )
    rows = _fetch_rows(
        connection,
        query,
        (session_id,),
    )

    if not rows:
        return None

    if len(rows) != 1:
        raise SessionPersistenceError(
            "Session query returned multiple rows for "
            f"session_id {session_id!r}: {rows!r}"
        )

    return _session_from_row(rows[0])


def fetch_latest_session_id(
    connection: Connection[PostgresRow],
    *,
    schema: str,
    source: str,
) -> str | None:
    """Return the most recently started session ID for one source.

    Args:
        connection:
            Active Psycopg connection owned by the caller.
        schema:
            PostgreSQL schema containing the ``sessions`` table.
        source:
            External source whose latest session should be located.

    Returns:
        The latest session ID, or ``None`` when no session exists for the
        requested source.
    """

    _validate_identifier(
        source,
        field="source",
    )

    query = sql.SQL(
        """
        SELECT id
        FROM {}
        WHERE source = %s
        ORDER BY
            started_at DESC,
            id DESC
        LIMIT 1
        """
    ).format(
        sql.Identifier(
            schema,
            "sessions",
        )
    )

    rows = _fetch_rows(
        connection,
        query,
        (source,),
    )

    if not rows:
        return None

    if len(rows) != 1 or len(rows[0]) != 1:
        raise SessionPersistenceError(
            f"Latest-session query returned an unexpected result: {rows!r}"
        )

    return _require_text(
        rows[0][0],
        field="id",
    )


# ---------------------------------------------------------------------
# SESSION WRITES
# ---------------------------------------------------------------------


def insert_session(
    connection: Connection[PostgresRow],
    *,
    schema: str,
    session: Session,
) -> None:
    """Persist one session idempotently while rejecting identity conflicts.

    A session absent from the database is inserted.

    A session already present with identical values is accepted as an
    idempotent no-op.

    A session already present with different values raises
    ``SessionConflictError``.

    Session completion is normally recorded separately with
    ``mark_session_ended``.

    Args:
        connection:
            Active Psycopg connection owned by the caller.
        schema:
            PostgreSQL schema containing the ``sessions`` table.
        session:
            Session occurrence to persist.

    Raises:
        SessionConflictError:
            If the session ID already identifies different persisted state.
        SessionPersistenceError:
            If the requested session is absent after insertion or invalid for
            persistence.
    """

    _validate_session(session)
    query = sql.SQL(
        """
        INSERT INTO {} (
            id,
            source,
            mode,
            cache_mode,
            ttl_seconds,
            as_of_bucket,
            cache_tag,
            started_at,
            ended_at
        )
        VALUES (
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s
        )
        ON CONFLICT (id) DO NOTHING
        """
    ).format(
        sql.Identifier(
            schema,
            "sessions",
        )
    )
    with connection.cursor() as cursor:
        cursor.execute(
            query,
            (
                session.id,
                session.source,
                session.mode.value,
                session.cache_mode.value,
                session.ttl_seconds,
                session.as_of_bucket,
                session.cache_tag,
                session.started_at,
                session.ended_at,
            ),
        )

    persisted_session = fetch_session_by_id(
        connection,
        schema=schema,
        session_id=session.id,
    )

    if persisted_session is None:
        raise SessionPersistenceError(
            f"Session was not present after insertion: session_id={session.id!r}"
        )

    if persisted_session != session:
        raise SessionConflictError(
            "Persisted session conflicts with requested session for "
            f"session_id {session.id!r}: "
            f"persisted={persisted_session!r}, "
            f"requested={session!r}"
        )


def mark_session_ended(
    connection: Connection[PostgresRow],
    *,
    schema: str,
    session_id: str,
    ended_at: datetime,
) -> None:
    """Record completion of one persisted session.

    Session completion is monotonic:

    - an open session may acquire one ``ended_at`` timestamp;
    - repeating the identical completion is an idempotent no-op;
    - attempting to replace an existing completion timestamp raises
      ``SessionConflictError``.

    Args:
        connection:
            Active Psycopg connection owned by the caller.
        schema:
            PostgreSQL schema containing the ``sessions`` table.
        session_id:
            Session occurrence to complete.
        ended_at:
            Timestamp at which the session completed.

    Raises:
        SessionPersistenceError:
            If the session does not exist or timestamps are invalid.
        SessionConflictError:
            If the session has already been completed at a different time.
    """

    _validate_identifier(
        session_id,
        field="session_id",
    )

    _validate_datetime(
        ended_at,
        field="ended_at",
    )

    existing_session = fetch_session_by_id(
        connection,
        schema=schema,
        session_id=session_id,
    )

    if existing_session is None:
        raise SessionPersistenceError(
            f"Cannot end a session that is not persisted: session_id={session_id!r}"
        )

    if ended_at < existing_session.started_at:
        raise SessionPersistenceError(
            "Session ended_at must not be before started_at: "
            f"session_id={session_id!r}, "
            f"started_at={existing_session.started_at!r}, "
            f"ended_at={ended_at!r}"
        )

    if existing_session.ended_at is not None:
        if existing_session.ended_at == ended_at:
            return

        raise SessionConflictError(
            "Persisted session already has a different completion time: "
            f"session_id={session_id!r}, "
            f"persisted={existing_session.ended_at!r}, "
            f"requested={ended_at!r}"
        )

    query = sql.SQL(
        """
        UPDATE {}
        SET ended_at = %s
        WHERE id = %s
          AND ended_at IS NULL
        """
    ).format(
        sql.Identifier(
            schema,
            "sessions",
        )
    )

    with connection.cursor() as cursor:
        cursor.execute(
            query,
            (
                ended_at,
                session_id,
            ),
        )

    persisted_session = fetch_session_by_id(
        connection,
        schema=schema,
        session_id=session_id,
    )

    if persisted_session is None:
        raise SessionPersistenceError(
            f"Session disappeared while recording completion: session_id={session_id!r}"
        )

    if persisted_session.ended_at != ended_at:
        raise SessionConflictError(
            "Persisted session completion differs from requested value: "
            f"session_id={session_id!r}, "
            f"persisted={persisted_session.ended_at!r}, "
            f"requested={ended_at!r}"
        )


# ---------------------------------------------------------------------
# PRIVATE QUERY HELPERS
# ---------------------------------------------------------------------


def _fetch_rows(
    connection: Connection[PostgresRow],
    query: ExecutableQuery,
    parameters: tuple[object, ...] | None = None,
) -> list[PostgresRow]:
    """Execute one query and return all result rows."""

    with connection.cursor() as cursor:
        if parameters is None:
            cursor.execute(query)
        else:
            cursor.execute(
                query,
                parameters,
            )

        return cursor.fetchall()


# ---------------------------------------------------------------------
# ROW RECONSTRUCTION
# ---------------------------------------------------------------------


def _session_from_row(
    row: PostgresRow,
) -> Session:
    """Reconstruct one validated session from a database row."""

    if len(row) != 9:
        raise SessionPersistenceError(
            f"Session query returned an unexpected row shape: {row!r}"
        )

    session_id = _require_text(
        row[0],
        field="id",
    )

    source = _require_text(
        row[1],
        field="source",
    )

    mode_text = _require_text(
        row[2],
        field="mode",
    )

    cache_mode_text = _require_text(
        row[3],
        field="cache_mode",
    )

    ttl_seconds = _optional_non_negative_float(
        row[4],
        field="ttl_seconds",
    )

    as_of_bucket = _optional_text(
        row[5],
        field="as_of_bucket",
    )

    cache_tag = _optional_text(
        row[6],
        field="cache_tag",
    )

    started_at = _require_datetime(
        row[7],
        field="started_at",
    )

    ended_at = _optional_datetime(
        row[8],
        field="ended_at",
    )

    try:
        mode = SessionMode(mode_text)
    except ValueError as err:
        raise SessionPersistenceError(
            f"Persisted session mode is not recognised: {mode_text!r}"
        ) from err

    try:
        cache_mode = CacheMode(cache_mode_text)
    except ValueError as err:
        raise SessionPersistenceError(
            f"Persisted session cache_mode is not recognised: {cache_mode_text!r}"
        ) from err

    session = Session(
        id=session_id,
        source=source,
        mode=mode,
        cache_mode=cache_mode,
        ttl_seconds=ttl_seconds,
        as_of_bucket=as_of_bucket,
        cache_tag=cache_tag,
        started_at=started_at,
        ended_at=ended_at,
    )

    _validate_session(session)

    return session


# ---------------------------------------------------------------------
# VALIDATION
# ---------------------------------------------------------------------


def _validate_session(
    session: Session,
) -> None:
    """Validate persistence-level session invariants."""

    _validate_identifier(
        session.id,
        field="id",
    )

    _validate_identifier(
        session.source,
        field="source",
    )

    _validate_optional_non_negative_float(
        session.ttl_seconds,
        field="ttl_seconds",
    )

    _validate_optional_text(
        session.as_of_bucket,
        field="as_of_bucket",
    )

    _validate_optional_text(
        session.cache_tag,
        field="cache_tag",
    )

    _validate_datetime(
        session.started_at,
        field="started_at",
    )

    if session.ended_at is not None:
        _validate_datetime(
            session.ended_at,
            field="ended_at",
        )

        if session.ended_at < session.started_at:
            raise SessionPersistenceError(
                "Session ended_at must not be before started_at: "
                f"session_id={session.id!r}, "
                f"started_at={session.started_at!r}, "
                f"ended_at={session.ended_at!r}"
            )


def _validate_identifier(
    value: object,
    *,
    field: str,
) -> None:
    """Require a non-empty text identifier."""

    if (
        not isinstance(
            value,
            str,
        )
        or not value
    ):
        raise SessionPersistenceError(
            f"Session {field} must be non-empty text, got {value!r}"
        )


def _require_text(
    value: object,
    *,
    field: str,
) -> str:
    """Require a non-empty persisted text value."""

    if (
        not isinstance(
            value,
            str,
        )
        or not value
    ):
        raise SessionPersistenceError(
            f"Persisted session {field} must be non-empty text, got {value!r}"
        )

    return value


def _validate_datetime(
    value: object,
    *,
    field: str,
) -> datetime:
    """Require a timezone-aware datetime."""

    if not isinstance(
        value,
        datetime,
    ):
        raise SessionPersistenceError(
            f"Session {field} must be a datetime, got {value!r}"
        )

    if value.tzinfo is None or value.utcoffset() is None:
        raise SessionPersistenceError(
            f"Session {field} must be timezone-aware, got {value!r}"
        )

    return value


def _require_datetime(
    value: object,
    *,
    field: str,
) -> datetime:
    """Require a timezone-aware persisted datetime."""

    if not isinstance(
        value,
        datetime,
    ):
        raise SessionPersistenceError(
            f"Persisted session {field} must be a datetime, got {value!r}"
        )

    if value.tzinfo is None or value.utcoffset() is None:
        raise SessionPersistenceError(
            f"Persisted session {field} must be timezone-aware, got {value!r}"
        )

    return value


def _optional_datetime(
    value: object,
    *,
    field: str,
) -> datetime | None:
    """Require a timezone-aware persisted datetime or NULL."""

    if value is None:
        return None

    return _require_datetime(
        value,
        field=field,
    )


def _validate_optional_non_negative_float(
    value: object,
    *,
    field: str,
) -> None:
    """Require a finite non-negative numeric value or NULL."""

    if value is None:
        return

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise SessionPersistenceError(
            f"Session {field} must be a finite non-negative number or NULL, "
            f"got {value!r}"
        )


def _optional_non_negative_float(
    value: object,
    *,
    field: str,
) -> float | None:
    """Require a persisted finite non-negative numeric value or NULL."""

    if value is None:
        return None

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise SessionPersistenceError(
            f"Persisted session {field} must be a finite non-negative number "
            f"or NULL, got {value!r}"
        )

    return float(value)


def _validate_optional_text(
    value: object,
    *,
    field: str,
) -> None:
    """Require text or NULL."""

    if value is not None and not isinstance(value, str):
        raise SessionPersistenceError(
            f"Session {field} must be text or NULL, got {value!r}"
        )


def _optional_text(
    value: object,
    *,
    field: str,
) -> str | None:
    """Require persisted text or NULL."""

    if value is None:
        return None

    if not isinstance(value, str):
        raise SessionPersistenceError(
            f"Persisted session {field} must be text or NULL, got {value!r}"
        )

    return value
