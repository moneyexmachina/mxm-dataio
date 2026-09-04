"""Plain-SQL persistence operations for DataIO Response observations.

This module owns the PostgreSQL representation of ``Response`` objects.

A Response represents one actual external reply occurrence acquired by one
Request.

``Response.id`` identifies the external reply occurrence.

``Response.request_id`` identifies the Request occurrence that actually
acquired that reply from the external source. A later Request that reuses this
Response refers to it through a Resolution; reuse never fabricates another
Response.

``Response.payload_checksum`` identifies the exact immutable payload bytes.
Different Response observations may legitimately reference the same payload
identity when an external source returns byte-for-byte identical replies.

Model timestamps use the canonical MXM ``TSNSScalar`` representation.
PostgreSQL stores timestamps as ``timestamptz``. The representation bridge
between those forms lives in ``sql_timestamps``; this module translates
timestamp-boundary failures into Response persistence errors.

All functions operate on a caller-provided Psycopg connection. They do not
open, commit, or roll back transactions. Transaction ownership belongs to the
higher-level DataIO operation.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from datetime import datetime
from typing import cast

from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from mxm.dataio.models import Response, ResponseStatus
from mxm.dataio.sql.postgres import PostgresRow
from mxm.dataio.sql.sql_timestamps import (
    SqlTimestampError,
    require_db_timestamp,
    ts_ns_to_db_datetime,
    validate_sql_timestamp,
)
from mxm.types import JSONObj
from mxm.types.timestamps import TSNSScalar

type ExecutableQuery = sql.SQL | sql.Composed


_SHA256_PATTERN = re.compile(
    r"^[0-9a-f]{64}$",
    flags=re.ASCII,
)


class ResponsePersistenceError(RuntimeError):
    """Base error for invalid or inconsistent persisted Response state."""


class ResponseConflictError(ResponsePersistenceError):
    """Raised when one Response ID identifies different observation state."""


# ---------------------------------------------------------------------
# RESPONSE READS
# ---------------------------------------------------------------------


def fetch_response_by_id(
    connection: Connection[PostgresRow],
    *,
    schema: str,
    response_id: str,
) -> Response | None:
    """Return one persisted Response observation by ID, if present."""

    _validate_identifier(
        response_id,
        field="response_id",
    )

    query = sql.SQL(
        """
        SELECT
            id,
            request_id,
            status,
            created_at,
            fetched_at,
            payload_checksum,
            size_bytes,
            media_type,
            encoding,
            elapsed_ms,
            adapter_meta
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

    if len(rows) != 1:
        raise ResponsePersistenceError(
            "Response query returned multiple rows for "
            f"response_id {response_id!r}: {rows!r}"
        )

    return _response_from_row(
        rows[0],
    )


def fetch_responses_by_request_id(
    connection: Connection[PostgresRow],
    *,
    schema: str,
    request_id: str,
) -> dict[str, Response]:
    """Return Responses actually acquired by one Request occurrence.

    Results are keyed by Response ID and deterministically ordered by
    ``created_at, id``.

    This lookup concerns acquisition provenance only. A Request that reused a
    pre-existing Response is related to that Response through ``Resolution``,
    not through ``Response.request_id``.
    """

    _validate_identifier(
        request_id,
        field="request_id",
    )

    query = sql.SQL(
        """
        SELECT
            id,
            request_id,
            status,
            created_at,
            fetched_at,
            payload_checksum,
            size_bytes,
            media_type,
            encoding,
            elapsed_ms,
            adapter_meta
        FROM {}
        WHERE request_id = %s
        ORDER BY
            created_at,
            id
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
        (request_id,),
    )

    return _responses_from_rows(
        rows,
        duplicate_error="Response query returned duplicate Response ID",
    )


def fetch_responses_by_payload_checksum(
    connection: Connection[PostgresRow],
    *,
    schema: str,
    payload_checksum: str,
) -> dict[str, Response]:
    """Return Response observations referencing one exact payload identity."""

    _validate_hash(
        payload_checksum,
        field="payload_checksum",
    )

    query = sql.SQL(
        """
        SELECT
            id,
            request_id,
            status,
            created_at,
            fetched_at,
            payload_checksum,
            size_bytes,
            media_type,
            encoding,
            elapsed_ms,
            adapter_meta
        FROM {}
        WHERE payload_checksum = %s
        ORDER BY
            created_at,
            id
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
        (payload_checksum,),
    )

    return _responses_from_rows(
        rows,
        duplicate_error="Payload lookup returned duplicate Response ID",
    )


# ---------------------------------------------------------------------
# RESPONSE WRITES
# ---------------------------------------------------------------------


def insert_response(
    connection: Connection[PostgresRow],
    *,
    schema: str,
    response: Response,
) -> None:
    """Persist one immutable external Response observation idempotently.

    A Response absent from the database is inserted.

    A Response already present with the same ID and identical state is accepted
    as an idempotent no-op.

    A Response already present with the same ID but different state raises
    ``ResponseConflictError``.

    Different Responses may legitimately reference the same immutable payload
    checksum.
    """

    _validate_response(
        response,
    )

    query = sql.SQL(
        """
        INSERT INTO {} (
            id,
            request_id,
            status,
            created_at,
            fetched_at,
            payload_checksum,
            size_bytes,
            media_type,
            encoding,
            elapsed_ms,
            adapter_meta
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
            %s,
            %s,
            %s
        )
        ON CONFLICT (id) DO NOTHING
        """
    ).format(
        sql.Identifier(
            schema,
            "responses",
        )
    )

    adapter_meta_value = (
        Jsonb(response.adapter_meta) if response.adapter_meta is not None else None
    )

    with connection.cursor() as cursor:
        cursor.execute(
            query,
            (
                response.id,
                response.request_id,
                response.status.value,
                _ts_ns_to_db_datetime(
                    response.created_at,
                    field="created_at",
                ),
                _ts_ns_to_db_datetime(
                    response.fetched_at,
                    field="fetched_at",
                ),
                response.payload_checksum,
                response.size_bytes,
                response.media_type,
                response.encoding,
                response.elapsed_ms,
                adapter_meta_value,
            ),
        )

    persisted_response = fetch_response_by_id(
        connection,
        schema=schema,
        response_id=response.id,
    )

    if persisted_response is None:
        raise ResponsePersistenceError(
            f"Response was not present after insertion: response_id={response.id!r}"
        )

    if persisted_response != response:
        raise ResponseConflictError(
            "Persisted Response conflicts with requested observation for "
            f"response_id {response.id!r}: "
            f"persisted={persisted_response!r}, "
            f"requested={response!r}"
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


def _responses_from_rows(
    rows: Sequence[PostgresRow],
    *,
    duplicate_error: str,
) -> dict[str, Response]:
    """Reconstruct Responses while rejecting duplicate observation IDs."""

    responses: dict[str, Response] = {}

    for row in rows:
        response = _response_from_row(
            row,
        )

        if response.id in responses:
            raise ResponsePersistenceError(f"{duplicate_error} {response.id!r}")

        responses[response.id] = response

    return responses


def _response_from_row(
    row: PostgresRow,
) -> Response:
    """Reconstruct one validated Response from a PostgreSQL row."""

    if len(row) != 11:
        raise ResponsePersistenceError(
            f"Response query returned an unexpected row shape: {row!r}"
        )

    response_id = _require_text(
        row[0],
        field="id",
    )

    request_id = _require_text(
        row[1],
        field="request_id",
    )

    status_text = _require_text(
        row[2],
        field="status",
    )

    created_at = _require_db_timestamp(
        row[3],
        field="created_at",
    )

    fetched_at = _require_db_timestamp(
        row[4],
        field="fetched_at",
    )

    payload_checksum = _require_hash(
        row[5],
        field="payload_checksum",
    )

    size_bytes = _require_non_negative_int(
        row[6],
        field="size_bytes",
    )

    media_type = _optional_text(
        row[7],
        field="media_type",
    )

    encoding = _optional_text(
        row[8],
        field="encoding",
    )

    elapsed_ms = _optional_non_negative_int(
        row[9],
        field="elapsed_ms",
    )

    adapter_meta = _optional_json_object(
        row[10],
        field="adapter_meta",
    )

    try:
        status = ResponseStatus(
            status_text,
        )
    except ValueError as err:
        raise ResponsePersistenceError(
            f"Persisted Response status is not recognised: {status_text!r}"
        ) from err

    response = Response(
        id=response_id,
        request_id=request_id,
        status=status,
        created_at=created_at,
        fetched_at=fetched_at,
        payload_checksum=payload_checksum,
        size_bytes=size_bytes,
        media_type=media_type,
        encoding=encoding,
        elapsed_ms=elapsed_ms,
        adapter_meta=adapter_meta,
    )

    _validate_response(
        response,
    )

    return response


# ---------------------------------------------------------------------
# TIMESTAMP BOUNDARY ERROR TRANSLATION
# ---------------------------------------------------------------------


def _validate_timestamp(
    value: object,
    *,
    field: str,
) -> TSNSScalar:
    """Validate one Response timestamp for PostgreSQL persistence."""

    try:
        return validate_sql_timestamp(
            value,
            field=f"Response {field}",
        )
    except SqlTimestampError as err:
        raise ResponsePersistenceError(str(err)) from err


def _ts_ns_to_db_datetime(
    value: TSNSScalar,
    *,
    field: str,
) -> datetime:
    """Convert one Response timestamp to the Psycopg representation."""

    try:
        return ts_ns_to_db_datetime(
            value,
            field=f"Response {field}",
        )
    except SqlTimestampError as err:
        raise ResponsePersistenceError(str(err)) from err


def _require_db_timestamp(
    value: object,
    *,
    field: str,
) -> TSNSScalar:
    """Convert one required persisted Response timestamp."""

    try:
        return require_db_timestamp(
            value,
            field=f"Persisted Response {field}",
        )
    except SqlTimestampError as err:
        raise ResponsePersistenceError(str(err)) from err


# ---------------------------------------------------------------------
# VALIDATION
# ---------------------------------------------------------------------


def _validate_response(
    response: Response,
) -> None:
    """Validate persistence-level Response observation invariants."""

    _validate_identifier(
        response.id,
        field="id",
    )

    _validate_identifier(
        response.request_id,
        field="request_id",
    )

    _validate_timestamp(
        response.created_at,
        field="created_at",
    )

    _validate_timestamp(
        response.fetched_at,
        field="fetched_at",
    )

    _validate_hash(
        response.payload_checksum,
        field="payload_checksum",
    )

    _validate_non_negative_int(
        response.size_bytes,
        field="size_bytes",
    )

    _validate_optional_text(
        response.media_type,
        field="media_type",
    )

    _validate_optional_text(
        response.encoding,
        field="encoding",
    )

    _validate_optional_non_negative_int(
        response.elapsed_ms,
        field="elapsed_ms",
    )

    _validate_json_object_or_none(
        response.adapter_meta,
        field="adapter_meta",
    )


def _validate_identifier(
    value: object,
    *,
    field: str,
) -> None:
    """Require a non-empty text identifier."""

    if not isinstance(value, str) or not value:
        raise ResponsePersistenceError(
            f"Response {field} must be non-empty text, got {value!r}"
        )


def _require_text(
    value: object,
    *,
    field: str,
) -> str:
    """Require a non-empty persisted text value."""

    if not isinstance(value, str) or not value:
        raise ResponsePersistenceError(
            f"Persisted Response {field} must be non-empty text, got {value!r}"
        )

    return value


def _validate_hash(
    value: object,
    *,
    field: str,
) -> None:
    """Require a lowercase hexadecimal SHA-256 digest."""

    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ResponsePersistenceError(
            f"Response {field} must be a 64-character lowercase hexadecimal "
            f"SHA-256 value, got {value!r}"
        )


def _require_hash(
    value: object,
    *,
    field: str,
) -> str:
    """Require and return one persisted SHA-256 digest."""

    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ResponsePersistenceError(
            f"Persisted Response {field} must be a 64-character lowercase "
            f"hexadecimal SHA-256 value, got {value!r}"
        )

    return value


def _validate_non_negative_int(
    value: object,
    *,
    field: str,
) -> None:
    """Require a non-negative integer."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ResponsePersistenceError(
            f"Response {field} must be a non-negative integer, got {value!r}"
        )


def _require_non_negative_int(
    value: object,
    *,
    field: str,
) -> int:
    """Require and return a persisted non-negative integer."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ResponsePersistenceError(
            f"Persisted Response {field} must be a non-negative integer, got {value!r}"
        )

    return value


def _validate_optional_non_negative_int(
    value: object,
    *,
    field: str,
) -> None:
    """Require a non-negative integer or NULL."""

    if value is None:
        return

    _validate_non_negative_int(
        value,
        field=field,
    )


def _optional_non_negative_int(
    value: object,
    *,
    field: str,
) -> int | None:
    """Require and return a persisted non-negative integer or NULL."""

    if value is None:
        return None

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ResponsePersistenceError(
            f"Persisted Response {field} must be a non-negative integer "
            f"or NULL, got {value!r}"
        )

    return value


def _validate_optional_text(
    value: object,
    *,
    field: str,
) -> None:
    """Require text or NULL."""

    if value is not None and not isinstance(value, str):
        raise ResponsePersistenceError(
            f"Response {field} must be text or NULL, got {value!r}"
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
        raise ResponsePersistenceError(
            f"Persisted Response {field} must be text or NULL, got {value!r}"
        )

    return value


# ---------------------------------------------------------------------
# JSON VALIDATION
# ---------------------------------------------------------------------


def _optional_json_object(
    value: object,
    *,
    field: str,
) -> JSONObj | None:
    """Require a persisted JSON object or NULL."""

    if value is None:
        return None

    _validate_json_object(
        value,
        field=field,
        persisted=True,
    )

    return cast(
        JSONObj,
        value,
    )


def _validate_json_object_or_none(
    value: object,
    *,
    field: str,
) -> None:
    """Require a JSON object or NULL."""

    if value is None:
        return

    _validate_json_object(
        value,
        field=field,
        persisted=False,
    )


def _validate_json_object(
    value: object,
    *,
    field: str,
    persisted: bool,
) -> None:
    """Require a JSON object with recursively JSON-compatible values."""

    prefix = "Persisted Response" if persisted else "Response"

    if not isinstance(value, dict):
        raise ResponsePersistenceError(
            f"{prefix} {field} must be a JSON object, got {value!r}"
        )

    raw = cast(
        dict[object, object],
        value,
    )

    for key, item in raw.items():
        if not isinstance(key, str):
            raise ResponsePersistenceError(
                f"{prefix} {field} must contain only string keys, got {key!r}"
            )

        _validate_json_like(
            item,
            field=f"{field}.{key}",
            prefix=prefix,
        )


def _validate_json_like(
    value: object,
    *,
    field: str,
    prefix: str,
) -> None:
    """Require recursively JSON-compatible data."""

    if value is None:
        return

    if isinstance(value, str | bool | int):
        return

    if isinstance(value, float):
        if not math.isfinite(value):
            raise ResponsePersistenceError(
                f"{prefix} {field} must contain JSON-compatible values, got {value!r}"
            )

        return

    if isinstance(value, list):
        raw_items = cast(
            list[object],
            value,
        )

        for index, item in enumerate(raw_items):
            _validate_json_like(
                item,
                field=f"{field}[{index}]",
                prefix=prefix,
            )

        return

    if isinstance(value, dict):
        raw = cast(
            dict[object, object],
            value,
        )

        for key, item in raw.items():
            if not isinstance(key, str):
                raise ResponsePersistenceError(
                    f"{prefix} {field} JSON object must contain only "
                    f"string keys, got {key!r}"
                )

            _validate_json_like(
                item,
                field=f"{field}.{key}",
                prefix=prefix,
            )

        return

    raise ResponsePersistenceError(
        f"{prefix} {field} must contain JSON-compatible values, got {value!r}"
    )
