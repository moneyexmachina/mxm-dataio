"""Plain-SQL persistence operations for DataIO sessions.

This module owns the PostgreSQL representation of ``Session`` objects.

A Session is the stable source and cache context under which Request
occurrences are resolved.

Model timestamps use the canonical MXM ``TSNSScalar`` representation.
PostgreSQL stores timestamps as ``timestamptz``. The representation bridge
between those forms lives in ``sql_timestamps``; this module translates
timestamp-boundary failures into Session persistence errors.

All functions operate on a caller-provided Psycopg connection. They do not
open, commit, or roll back transactions. Transaction ownership belongs to the
higher-level DataIO operation.
"""

from __future__ import annotations

import math
from datetime import datetime

from psycopg import Connection, sql

from mxm.dataio.models import CacheMode, Session
from mxm.dataio.sql.postgres import PostgresRow
from mxm.dataio.sql.sql_timestamps import (
    SqlTimestampError,
    optional_db_timestamp,
    require_db_timestamp,
    ts_ns_to_db_datetime,
    validate_sql_timestamp,
)
from mxm.types.timestamps import TSNSScalar

type ExecutableQuery = sql.SQL | sql.Composed


class SessionPersistenceError(RuntimeError):
    """Base error for invalid or inconsistent persisted Session state."""


class SessionConflictError(SessionPersistenceError):
    """Raised when one Session ID identifies different Session state."""


# ---------------------------------------------------------------------
# SESSION READS
# ---------------------------------------------------------------------


def fetch_session_by_id(
    connection: Connection[PostgresRow],
    *,
    schema: str,
    session_id: str,
) -> Session | None:
    """Return one persisted Session by ID, if present."""

    _validate_identifier(
        session_id,
        field="session_id",
    )

    query = sql.SQL(
        """
        SELECT
            id,
            source,
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
    """Return the most recently started Session ID for one source."""

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
    """Persist one Session idempotently while rejecting identity conflicts.

    A Session absent from the database is inserted.

    A Session already present with identical values is accepted as an
    idempotent no-op.

    A Session already present with different values raises
    ``SessionConflictError``.

    Session completion is normally recorded separately with
    ``mark_session_ended``.
    """

    _validate_session(session)

    query = sql.SQL(
        """
        INSERT INTO {} (
            id,
            source,
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
                session.cache_mode.value,
                session.ttl_seconds,
                session.as_of_bucket,
                session.cache_tag,
                _ts_ns_to_db_datetime(
                    session.started_at,
                    field="started_at",
                ),
                (
                    _ts_ns_to_db_datetime(
                        session.ended_at,
                        field="ended_at",
                    )
                    if session.ended_at is not None
                    else None
                ),
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
            "Persisted Session conflicts with requested Session for "
            f"session_id {session.id!r}: "
            f"persisted={persisted_session!r}, "
            f"requested={session!r}"
        )


def mark_session_ended(
    connection: Connection[PostgresRow],
    *,
    schema: str,
    session_id: str,
    ended_at: TSNSScalar,
) -> None:
    """Record completion of one persisted Session.

    Session completion is monotonic:

    - an open Session may acquire one ``ended_at`` timestamp;
    - repeating the identical completion is an idempotent no-op;
    - attempting to replace an existing completion timestamp raises
      ``SessionConflictError``.
    """

    _validate_identifier(
        session_id,
        field="session_id",
    )

    _validate_timestamp(
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
            f"Cannot end a Session that is not persisted: session_id={session_id!r}"
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
            "Persisted Session already has a different completion time: "
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
                _ts_ns_to_db_datetime(
                    ended_at,
                    field="ended_at",
                ),
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
            "Persisted Session completion differs from requested value: "
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
    """Reconstruct one validated Session from a PostgreSQL row."""

    if len(row) != 8:
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

    cache_mode_text = _require_text(
        row[2],
        field="cache_mode",
    )

    ttl_seconds = _optional_non_negative_float(
        row[3],
        field="ttl_seconds",
    )

    as_of_bucket = _optional_text(
        row[4],
        field="as_of_bucket",
    )

    cache_tag = _optional_text(
        row[5],
        field="cache_tag",
    )

    started_at = _require_db_timestamp(
        row[6],
        field="started_at",
    )

    ended_at = _optional_db_timestamp(
        row[7],
        field="ended_at",
    )

    try:
        cache_mode = CacheMode(cache_mode_text)
    except ValueError as err:
        raise SessionPersistenceError(
            f"Persisted Session cache_mode is not recognised: {cache_mode_text!r}"
        ) from err

    session = Session(
        id=session_id,
        source=source,
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
# TIMESTAMP BOUNDARY ERROR TRANSLATION
# ---------------------------------------------------------------------


def _validate_timestamp(
    value: object,
    *,
    field: str,
) -> TSNSScalar:
    """Validate one Session timestamp for PostgreSQL persistence."""

    try:
        return validate_sql_timestamp(
            value,
            field=f"Session {field}",
        )
    except SqlTimestampError as err:
        raise SessionPersistenceError(str(err)) from err


def _ts_ns_to_db_datetime(
    value: TSNSScalar,
    *,
    field: str,
) -> datetime:
    """Convert one Session timestamp to the Psycopg representation."""

    try:
        return ts_ns_to_db_datetime(
            value,
            field=f"Session {field}",
        )
    except SqlTimestampError as err:
        raise SessionPersistenceError(str(err)) from err


def _require_db_timestamp(
    value: object,
    *,
    field: str,
) -> TSNSScalar:
    """Convert one required persisted Session timestamp."""

    try:
        return require_db_timestamp(
            value,
            field=f"Persisted Session {field}",
        )
    except SqlTimestampError as err:
        raise SessionPersistenceError(str(err)) from err


def _optional_db_timestamp(
    value: object,
    *,
    field: str,
) -> TSNSScalar | None:
    """Convert one nullable persisted Session timestamp."""

    try:
        return optional_db_timestamp(
            value,
            field=f"Persisted Session {field}",
        )
    except SqlTimestampError as err:
        raise SessionPersistenceError(str(err)) from err


# ---------------------------------------------------------------------
# VALIDATION
# ---------------------------------------------------------------------


def _validate_session(
    session: Session,
) -> None:
    """Validate persistence-level Session invariants."""

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

    _validate_timestamp(
        session.started_at,
        field="started_at",
    )

    if session.ended_at is not None:
        _validate_timestamp(
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

    if not isinstance(value, str) or not value:
        raise SessionPersistenceError(
            f"Session {field} must be non-empty text, got {value!r}"
        )


def _require_text(
    value: object,
    *,
    field: str,
) -> str:
    """Require a non-empty persisted text value."""

    if not isinstance(value, str) or not value:
        raise SessionPersistenceError(
            f"Persisted Session {field} must be non-empty text, got {value!r}"
        )

    return value


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
            f"Persisted Session {field} must be a finite non-negative number "
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
            f"Persisted Session {field} must be text or NULL, got {value!r}"
        )

    return value
