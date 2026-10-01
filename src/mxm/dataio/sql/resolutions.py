"""Plain-SQL persistence operations for DataIO Request resolutions.

This module owns the PostgreSQL representation of ``Resolution`` objects.

A Resolution records the immutable fact of how one Request occurrence was
satisfied and which Response observation supplied its result.

One Request occurrence has at most one final Resolution. ``request_id`` is
therefore the persistence identity of a Resolution.

Resolution semantics distinguish two cases:

- ``acquired``: the referenced Response was acquired by the Request being
  resolved;
- ``reused``: the referenced Response was acquired by a different Request
  occurrence and reused to satisfy this one.

This module validates that structural relationship between Resolution and
Response. It does not determine whether reuse was permitted by the acquiring
Request's cache policy, TTL, source, as-of bucket, cache tag, or other runtime
decision inputs.

Model timestamps use the canonical MXM ``TSNSScalar`` representation.
PostgreSQL stores timestamps as ``timestamptz``. The representation bridge
between those forms lives in ``sql_timestamps``; this module translates
timestamp-boundary failures into Resolution persistence errors.

All functions operate on a caller-provided Psycopg connection. They do not
open, commit, or roll back transactions. Transaction ownership belongs to the
higher-level DataIO operation.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from psycopg import Connection, sql

from mxm.dataio.models import Resolution, ResolutionKind
from mxm.dataio.sql.postgres import PostgresRow
from mxm.dataio.sql.sql_timestamps import (
    SqlTimestampError,
    require_db_timestamp,
    ts_ns_to_db_datetime,
    validate_sql_timestamp,
)
from mxm.types.timestamps import TSNSScalar

type ExecutableQuery = sql.SQL | sql.Composed


class ResolutionPersistenceError(RuntimeError):
    """Base error for invalid or inconsistent persisted Resolution state."""


class ResolutionConflictError(ResolutionPersistenceError):
    """Raised when one Request occurrence identifies different Resolutions."""


# ---------------------------------------------------------------------
# RESOLUTION READS
# ---------------------------------------------------------------------


def fetch_resolution_by_request_id(
    connection: Connection[PostgresRow],
    *,
    schema: str,
    request_id: str,
) -> Resolution | None:
    """Return the final Resolution for one Request occurrence, if present."""

    _validate_identifier(
        request_id,
        field="request_id",
    )

    query = sql.SQL(
        """
        SELECT
            request_id,
            response_id,
            kind,
            resolved_at
        FROM {}
        WHERE request_id = %s
        """
    ).format(
        sql.Identifier(
            schema,
            "resolutions",
        )
    )

    rows = _fetch_rows(
        connection,
        query,
        (request_id,),
    )

    if not rows:
        return None

    if len(rows) != 1:
        raise ResolutionPersistenceError(
            "Resolution query returned multiple rows for "
            f"request_id {request_id!r}: {rows!r}"
        )

    return _resolution_from_row(
        rows[0],
    )


def fetch_resolutions_by_response_id(
    connection: Connection[PostgresRow],
    *,
    schema: str,
    response_id: str,
) -> dict[str, Resolution]:
    """Return Resolutions satisfied by one Response observation.

    The returned mapping is keyed by Request occurrence ID.

    Multiple Resolutions may legitimately reference one Response. This is the
    relational representation of Response reuse.

    Results are deterministically ordered by resolution time and then Request
    ID before being reconstructed into the mapping.
    """

    _validate_identifier(
        response_id,
        field="response_id",
    )

    query = sql.SQL(
        """
        SELECT
            request_id,
            response_id,
            kind,
            resolved_at
        FROM {}
        WHERE response_id = %s
        ORDER BY
            resolved_at,
            request_id
        """
    ).format(
        sql.Identifier(
            schema,
            "resolutions",
        )
    )

    rows = _fetch_rows(
        connection,
        query,
        (response_id,),
    )

    return _resolutions_from_rows(
        rows,
    )


# ---------------------------------------------------------------------
# RESOLUTION WRITES
# ---------------------------------------------------------------------


def insert_resolution(
    connection: Connection[PostgresRow],
    *,
    schema: str,
    resolution: Resolution,
) -> None:
    """Persist one final Request Resolution idempotently.

    A Resolution absent from the database is inserted.

    A Resolution already present for the same Request occurrence with
    identical state is accepted as an idempotent no-op.

    A different Resolution already present for the same Request occurrence
    raises ``ResolutionConflictError``.

    Before insertion, the referenced Response relationship is validated:

    - ``acquired`` requires the Response to have been acquired by the same
      Request occurrence;
    - ``reused`` requires the Response to have been acquired by a different
      Request occurrence.

    Reuse eligibility itself is deliberately outside this repository.

    Raises:
        ResolutionConflictError:
            If the Request occurrence already has a different Resolution.
        ResolutionPersistenceError:
            If the Resolution is invalid, the referenced Response does not
            exist, its acquisition relationship contradicts ``kind``, or the
            requested Resolution is absent after insertion.
    """

    _validate_resolution(
        resolution,
    )

    _validate_resolution_response_relationship(
        connection,
        schema=schema,
        resolution=resolution,
    )

    query = sql.SQL(
        """
        INSERT INTO {} (
            request_id,
            response_id,
            kind,
            resolved_at
        )
        VALUES (
            %s,
            %s,
            %s,
            %s
        )
        ON CONFLICT (request_id) DO NOTHING
        """
    ).format(
        sql.Identifier(
            schema,
            "resolutions",
        )
    )

    with connection.cursor() as cursor:
        cursor.execute(
            query,
            (
                resolution.request_id,
                resolution.response_id,
                resolution.kind.value,
                _ts_ns_to_db_datetime(
                    resolution.resolved_at,
                    field="resolved_at",
                ),
            ),
        )

    persisted_resolution = fetch_resolution_by_request_id(
        connection,
        schema=schema,
        request_id=resolution.request_id,
    )

    if persisted_resolution is None:
        raise ResolutionPersistenceError(
            "Resolution was not present after insertion: "
            f"request_id={resolution.request_id!r}"
        )

    if persisted_resolution != resolution:
        raise ResolutionConflictError(
            "Persisted Resolution conflicts with requested Resolution for "
            f"request_id {resolution.request_id!r}: "
            f"persisted={persisted_resolution!r}, "
            f"requested={resolution!r}"
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
            cursor.execute(
                query,
            )
        else:
            cursor.execute(
                query,
                parameters,
            )

        return cursor.fetchall()


def _fetch_response_request_id(
    connection: Connection[PostgresRow],
    *,
    schema: str,
    response_id: str,
) -> str | None:
    """Return the Request occurrence that acquired one Response."""

    query = sql.SQL(
        """
        SELECT request_id
        FROM {}
        WHERE id = %s
        """
    ).format(
        sql.Identifier(
            schema,
            "responses",
        )
    )

    rows = _fetch_rows(
        connection,
        query,
        (response_id,),
    )

    if not rows:
        return None

    if len(rows) != 1 or len(rows[0]) != 1:
        raise ResolutionPersistenceError(
            "Response lookup returned an unexpected result for "
            f"response_id {response_id!r}: {rows!r}"
        )

    return _require_response_request_id(
        rows[0][0],
    )


# ---------------------------------------------------------------------
# ROW RECONSTRUCTION
# ---------------------------------------------------------------------


def _resolutions_from_rows(
    rows: Sequence[PostgresRow],
) -> dict[str, Resolution]:
    """Reconstruct Resolutions while rejecting duplicate Request identities."""

    resolutions: dict[str, Resolution] = {}

    for row in rows:
        resolution = _resolution_from_row(
            row,
        )

        if resolution.request_id in resolutions:
            raise ResolutionPersistenceError(
                "Resolution query returned duplicate Request occurrence ID "
                f"{resolution.request_id!r}"
            )

        resolutions[resolution.request_id] = resolution

    return resolutions


def _resolution_from_row(
    row: PostgresRow,
) -> Resolution:
    """Reconstruct one validated Resolution from a PostgreSQL row."""

    if len(row) != 4:
        raise ResolutionPersistenceError(
            f"Resolution query returned an unexpected row shape: {row!r}"
        )

    request_id = _require_text(
        row[0],
        field="request_id",
    )

    response_id = _require_text(
        row[1],
        field="response_id",
    )

    kind_text = _require_text(
        row[2],
        field="kind",
    )

    resolved_at = _require_db_timestamp(
        row[3],
        field="resolved_at",
    )

    try:
        kind = ResolutionKind(
            kind_text,
        )
    except ValueError as err:
        raise ResolutionPersistenceError(
            f"Persisted Resolution kind is not recognised: {kind_text!r}"
        ) from err

    resolution = Resolution(
        request_id=request_id,
        response_id=response_id,
        kind=kind,
        resolved_at=resolved_at,
    )

    _validate_resolution(
        resolution,
    )

    return resolution


# ---------------------------------------------------------------------
# CROSS-RECORD SEMANTICS
# ---------------------------------------------------------------------


def _validate_resolution_response_relationship(
    connection: Connection[PostgresRow],
    *,
    schema: str,
    resolution: Resolution,
) -> None:
    """Validate Resolution kind against Response acquisition provenance."""

    acquiring_request_id = _fetch_response_request_id(
        connection,
        schema=schema,
        response_id=resolution.response_id,
    )

    if acquiring_request_id is None:
        raise ResolutionPersistenceError(
            "Resolution references a Response that is not persisted: "
            f"response_id={resolution.response_id!r}"
        )

    if resolution.kind is ResolutionKind.ACQUIRED:
        if acquiring_request_id != resolution.request_id:
            raise ResolutionPersistenceError(
                "Acquired Resolution must reference a Response acquired by "
                "the same Request occurrence: "
                f"request_id={resolution.request_id!r}, "
                f"response_id={resolution.response_id!r}, "
                f"response_request_id={acquiring_request_id!r}"
            )

        return

    if resolution.kind is ResolutionKind.REUSED:
        if acquiring_request_id == resolution.request_id:
            raise ResolutionPersistenceError(
                "Reused Resolution must reference a Response acquired by "
                "a different Request occurrence: "
                f"request_id={resolution.request_id!r}, "
                f"response_id={resolution.response_id!r}"
            )

        return

    raise ResolutionPersistenceError(
        f"Unsupported Resolution kind: {resolution.kind!r}"
    )


# ---------------------------------------------------------------------
# TIMESTAMP BOUNDARY ERROR TRANSLATION
# ---------------------------------------------------------------------


def _validate_timestamp(
    value: object,
    *,
    field: str,
) -> TSNSScalar:
    """Validate one Resolution timestamp for PostgreSQL persistence."""

    try:
        return validate_sql_timestamp(
            value,
            field=f"Resolution {field}",
        )
    except SqlTimestampError as err:
        raise ResolutionPersistenceError(str(err)) from err


def _ts_ns_to_db_datetime(
    value: TSNSScalar,
    *,
    field: str,
) -> datetime:
    """Convert one Resolution timestamp to the Psycopg representation."""

    try:
        return ts_ns_to_db_datetime(
            value,
            field=f"Resolution {field}",
        )
    except SqlTimestampError as err:
        raise ResolutionPersistenceError(str(err)) from err


def _require_db_timestamp(
    value: object,
    *,
    field: str,
) -> TSNSScalar:
    """Convert one required persisted Resolution timestamp."""

    try:
        return require_db_timestamp(
            value,
            field=f"Persisted Resolution {field}",
        )
    except SqlTimestampError as err:
        raise ResolutionPersistenceError(str(err)) from err


# ---------------------------------------------------------------------
# VALIDATION
# ---------------------------------------------------------------------


def _validate_resolution(
    resolution: Resolution,
) -> None:
    """Validate persistence-level Resolution invariants."""

    _validate_identifier(
        resolution.request_id,
        field="request_id",
    )

    _validate_identifier(
        resolution.response_id,
        field="response_id",
    )

    _validate_timestamp(
        resolution.resolved_at,
        field="resolved_at",
    )


def _validate_identifier(
    value: object,
    *,
    field: str,
) -> None:
    """Require a non-empty text identifier."""

    if not isinstance(value, str) or not value:
        raise ResolutionPersistenceError(
            f"Resolution {field} must be non-empty text, got {value!r}"
        )


def _require_text(
    value: object,
    *,
    field: str,
) -> str:
    """Require a non-empty persisted text value."""

    if not isinstance(value, str) or not value:
        raise ResolutionPersistenceError(
            f"Persisted Resolution {field} must be non-empty text, got {value!r}"
        )

    return value


def _require_response_request_id(
    value: object,
) -> str:
    """Require the persisted acquiring Request identity of a Response."""

    if not isinstance(value, str) or not value:
        raise ResolutionPersistenceError(
            f"Persisted Response request_id must be non-empty text, got {value!r}"
        )

    return value
