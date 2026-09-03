"""Plain-SQL persistence operations for DataIO response observations."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import datetime
from typing import cast

from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from mxm.dataio.models import Response, ResponseStatus
from mxm.dataio.sql.postgres import PostgresRow
from mxm.types import JSONObj

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ResponsePersistenceError(RuntimeError):
    """Response state cannot be represented safely at the SQL boundary."""


class ResponseConflictError(ResponsePersistenceError):
    """One response ID refers to conflicting persisted observation state."""


# ---------------------------------------------------------------------------
# Public lookup operations
# ---------------------------------------------------------------------------


def fetch_response_by_id(
    connection: Connection[PostgresRow],
    *,
    schema: str,
    response_id: str,
) -> Response | None:
    """Return one persisted response observation by identity."""

    _validate_non_empty_text(
        response_id,
        field_name="response_id",
    )

    query = sql.SQL(
        """
        SELECT
            id,
            request_id,
            status,
            sequence,
            created_at,
            fetched_at,
            payload_checksum,
            size_bytes,
            media_type,
            encoding,
            elapsed_ms,
            adapter_meta
        FROM {}.responses
        WHERE id = %s
        """
    ).format(
        sql.Identifier(schema),
    )

    with connection.cursor() as cursor:
        cursor.execute(
            query,
            (response_id,),
        )
        rows = cursor.fetchall()

    if not rows:
        return None

    if len(rows) != 1:
        raise ResponsePersistenceError(
            f"Response lookup returned multiple rows for response_id={response_id!r}"
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
    """Return observations acquired by one request occurrence.

    Results are keyed by response identity. Ordering of the returned mapping
    follows ``created_at, id`` for deterministic inspection, without assigning
    additional streaming semantics to ``sequence``.
    """

    _validate_non_empty_text(
        request_id,
        field_name="request_id",
    )

    query = sql.SQL(
        """
        SELECT
            id,
            request_id,
            status,
            sequence,
            created_at,
            fetched_at,
            payload_checksum,
            size_bytes,
            media_type,
            encoding,
            elapsed_ms,
            adapter_meta
        FROM {}.responses
        WHERE request_id = %s
        ORDER BY created_at, id
        """
    ).format(
        sql.Identifier(schema),
    )

    with connection.cursor() as cursor:
        cursor.execute(
            query,
            (request_id,),
        )
        rows = cursor.fetchall()

    responses: dict[str, Response] = {}

    for row in rows:
        response = _response_from_row(row)

        if response.id in responses:
            raise ResponsePersistenceError(
                "Response query returned duplicate observation identity: "
                f"{response.id!r}"
            )

        responses[response.id] = response

    return responses


def fetch_responses_by_payload_checksum(
    connection: Connection[PostgresRow],
    *,
    schema: str,
    payload_checksum: str,
) -> dict[str, Response]:
    """Return observations that reference one exact payload identity."""

    _validate_sha256(
        payload_checksum,
        field_name="payload_checksum",
    )

    query = sql.SQL(
        """
        SELECT
            id,
            request_id,
            status,
            sequence,
            created_at,
            fetched_at,
            payload_checksum,
            size_bytes,
            media_type,
            encoding,
            elapsed_ms,
            adapter_meta
        FROM {}.responses
        WHERE payload_checksum = %s
        ORDER BY created_at, id
        """
    ).format(
        sql.Identifier(schema),
    )

    with connection.cursor() as cursor:
        cursor.execute(
            query,
            (payload_checksum,),
        )
        rows = cursor.fetchall()

    responses: dict[str, Response] = {}

    for row in rows:
        response = _response_from_row(row)

        if response.id in responses:
            raise ResponsePersistenceError(
                "Payload lookup returned duplicate response observation: "
                f"{response.id!r}"
            )

        responses[response.id] = response

    return responses


# ---------------------------------------------------------------------------
# Public persistence operation
# ---------------------------------------------------------------------------


def insert_response(
    connection: Connection[PostgresRow],
    *,
    schema: str,
    response: Response,
) -> None:
    """Persist one immutable external response observation.

    Insertion is idempotent when the same response ID already represents
    identical state. The same response ID cannot be reused for different
    observation state.

    Transaction ownership remains with the caller.
    """

    _validate_response(
        response,
    )

    query = sql.SQL(
        """
        INSERT INTO {}.responses (
            id,
            request_id,
            status,
            sequence,
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
            %s,
            %s
        )
        ON CONFLICT (id) DO NOTHING
        """
    ).format(
        sql.Identifier(schema),
    )

    adapter_meta = (
        Jsonb(dict(response.adapter_meta))
        if response.adapter_meta is not None
        else None
    )

    with connection.cursor() as cursor:
        cursor.execute(
            query,
            (
                response.id,
                response.request_id,
                response.status.value,
                response.sequence,
                response.created_at,
                response.fetched_at,
                response.payload_checksum,
                response.size_bytes,
                response.media_type,
                response.encoding,
                response.elapsed_ms,
                adapter_meta,
            ),
        )

    persisted = fetch_response_by_id(
        connection,
        schema=schema,
        response_id=response.id,
    )

    if persisted is None:
        raise ResponsePersistenceError(
            f"Response is not present after insertion: response_id={response.id!r}"
        )

    if persisted != response:
        raise ResponseConflictError(
            "Persisted response conflicts with requested observation: "
            f"response_id={response.id!r}"
        )


# ---------------------------------------------------------------------------
# Row reconstruction
# ---------------------------------------------------------------------------


def _response_from_row(
    row: PostgresRow,
) -> Response:
    """Reconstruct and validate one Response from a PostgreSQL row."""

    if len(row) != 12:
        raise ResponsePersistenceError(
            "Response query returned unexpected row shape: "
            f"expected=12, actual={len(row)}"
        )

    (
        response_id,
        request_id,
        status_value,
        sequence,
        created_at,
        fetched_at,
        payload_checksum,
        size_bytes,
        media_type,
        encoding,
        elapsed_ms,
        adapter_meta,
    ) = row

    _validate_non_empty_text(
        response_id,
        field_name="id",
    )
    _validate_non_empty_text(
        request_id,
        field_name="request_id",
    )

    if not isinstance(
        status_value,
        str,
    ):
        raise ResponsePersistenceError("status must be text")

    try:
        status = ResponseStatus(
            status_value,
        )
    except ValueError as exc:
        raise ResponsePersistenceError(
            f"status is not recognised: {status_value!r}"
        ) from exc

    _validate_optional_non_negative_int(
        sequence,
        field_name="sequence",
    )

    _validate_datetime(
        created_at,
        field_name="created_at",
    )
    _validate_datetime(
        fetched_at,
        field_name="fetched_at",
    )

    _validate_sha256(
        payload_checksum,
        field_name="payload_checksum",
    )

    _validate_non_negative_int(
        size_bytes,
        field_name="size_bytes",
    )

    _validate_optional_text(
        media_type,
        field_name="media_type",
    )
    _validate_optional_text(
        encoding,
        field_name="encoding",
    )

    _validate_optional_non_negative_int(
        elapsed_ms,
        field_name="elapsed_ms",
    )

    _validate_json_object_or_none(
        adapter_meta,
        field_name="adapter_meta",
    )

    return Response(
        id=cast(str, response_id),
        request_id=cast(str, request_id),
        status=status,
        sequence=cast(int | None, sequence),
        created_at=cast(datetime, created_at),
        fetched_at=cast(datetime, fetched_at),
        payload_checksum=cast(str, payload_checksum),
        size_bytes=cast(int, size_bytes),
        media_type=cast(str | None, media_type),
        encoding=cast(str | None, encoding),
        elapsed_ms=cast(int | None, elapsed_ms),
        adapter_meta=cast(JSONObj | None, adapter_meta),
    )


# ---------------------------------------------------------------------------
# Persistence validation
# ---------------------------------------------------------------------------


def _validate_response(
    response: Response,
) -> None:
    """Validate Response state before crossing the SQL boundary."""

    _validate_non_empty_text(
        response.id,
        field_name="id",
    )
    _validate_non_empty_text(
        response.request_id,
        field_name="request_id",
    )

    _validate_optional_non_negative_int(
        response.sequence,
        field_name="sequence",
    )

    _validate_datetime(
        response.created_at,
        field_name="created_at",
    )
    _validate_datetime(
        response.fetched_at,
        field_name="fetched_at",
    )

    _validate_sha256(
        response.payload_checksum,
        field_name="payload_checksum",
    )

    _validate_non_negative_int(
        response.size_bytes,
        field_name="size_bytes",
    )

    _validate_optional_text(
        response.media_type,
        field_name="media_type",
    )
    _validate_optional_text(
        response.encoding,
        field_name="encoding",
    )

    _validate_optional_non_negative_int(
        response.elapsed_ms,
        field_name="elapsed_ms",
    )

    _validate_json_object_or_none(
        response.adapter_meta,
        field_name="adapter_meta",
    )


def _validate_non_empty_text(
    value: object,
    *,
    field_name: str,
) -> None:
    """Require non-empty text."""

    if (
        not isinstance(
            value,
            str,
        )
        or not value
    ):
        raise ResponsePersistenceError(f"{field_name} must be non-empty text")


def _validate_optional_text(
    value: object,
    *,
    field_name: str,
) -> None:
    """Require text or NULL."""

    if value is not None and not isinstance(
        value,
        str,
    ):
        raise ResponsePersistenceError(f"{field_name} must be text or NULL")


def _validate_sha256(
    value: object,
    *,
    field_name: str,
) -> None:
    """Require a canonical lowercase SHA-256 hexadecimal digest."""

    if (
        not isinstance(
            value,
            str,
        )
        or _SHA256_PATTERN.fullmatch(value) is None
    ):
        raise ResponsePersistenceError(
            f"{field_name} must be a 64-character lowercase hexadecimal SHA-256 value"
        )


def _validate_non_negative_int(
    value: object,
    *,
    field_name: str,
) -> None:
    """Require a non-negative integer."""

    if (
        not isinstance(
            value,
            int,
        )
        or isinstance(
            value,
            bool,
        )
        or value < 0
    ):
        raise ResponsePersistenceError(f"{field_name} must be a non-negative integer")


def _validate_optional_non_negative_int(
    value: object,
    *,
    field_name: str,
) -> None:
    """Require a non-negative integer or NULL."""

    if value is None:
        return

    _validate_non_negative_int(
        value,
        field_name=field_name,
    )


def _validate_datetime(
    value: object,
    *,
    field_name: str,
) -> None:
    """Require a timezone-aware datetime."""

    if not isinstance(
        value,
        datetime,
    ):
        raise ResponsePersistenceError(f"{field_name} must be a datetime")

    if value.tzinfo is None or value.utcoffset() is None:
        raise ResponsePersistenceError(f"{field_name} must be timezone-aware")


def _validate_json_object_or_none(
    value: object,
    *,
    field_name: str,
) -> None:
    """Require a JSON object or NULL."""

    if value is None:
        return

    if not isinstance(
        value,
        Mapping,
    ):
        raise ResponsePersistenceError(f"{field_name} must be a JSON object or NULL")

    mapping = cast(
        Mapping[object, object],
        value,
    )
    for key, item in mapping.items():
        if not isinstance(
            key,
            str,
        ):
            raise ResponsePersistenceError(f"{field_name} keys must be text")

        _validate_json_like(
            item,
            field_name=f"{field_name}.{key}",
        )


def _validate_json_like(
    value: object,
    *,
    field_name: str,
) -> None:
    """Require a recursively JSON-compatible value."""

    if value is None:
        return

    if isinstance(
        value,
        str,
    ):
        return

    if isinstance(
        value,
        bool,
    ):
        return

    if isinstance(
        value,
        int,
    ):
        return

    if isinstance(
        value,
        float,
    ):
        if not math.isfinite(value):
            raise ResponsePersistenceError(
                f"{field_name} must contain JSON-compatible values"
            )
        return

    if isinstance(
        value,
        Mapping,
    ):
        mapping = cast(
            Mapping[object, object],
            value,
        )

        for key, item in mapping.items():
            if not isinstance(
                key,
                str,
            ):
                raise ResponsePersistenceError(f"{field_name} keys must be text")

            _validate_json_like(
                item,
                field_name=f"{field_name}.{key}",
            )
        return

    if isinstance(
        value,
        list,
    ):
        items = cast(
            list[object],
            value,
        )

        for index, item in enumerate(items):
            _validate_json_like(
                item,
                field_name=f"{field_name}[{index}]",
            )
        return

    raise ResponsePersistenceError(f"{field_name} must contain JSON-compatible values")
