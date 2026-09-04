"""PostgreSQL timestamp representation bridge for mxm-dataio.

MXM models use the canonical timestamp representation defined by mxm-types:

    TSNSScalar == np.datetime64[ns]

Psycopg represents PostgreSQL ``timestamptz`` values as timezone-aware Python
``datetime`` objects.

This module owns the explicit representation bridge between those two forms
for mxm-dataio SQL persistence.

PostgreSQL ``timestamptz`` has microsecond precision. Canonical MXM timestamps
with finer-than-microsecond precision are rejected rather than silently
truncated.

This module defines representation conversion only. It does not acquire wall
clock time or assign temporal/domain semantics.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mxm.types.timestamps import (
    TSNSScalar,
    assert_not_nat,
    assert_ts_ns,
    ts_ns_from_int,
    ts_ns_to_int,
)

_POSTGRES_TIMESTAMP_NS = 1_000
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class SqlTimestampError(ValueError):
    """Raised when a timestamp cannot cross the PostgreSQL boundary exactly."""


def validate_sql_timestamp(
    value: object,
    *,
    field: str,
) -> TSNSScalar:
    """Require a persistable canonical MXM timestamp.

    PostgreSQL ``timestamptz`` stores timestamps with microsecond precision.
    Finer canonical timestamps are rejected rather than silently truncated.
    """

    try:
        timestamp = assert_not_nat(
            assert_ts_ns(value),
        )
    except (TypeError, ValueError) as err:
        raise SqlTimestampError(
            f"{field} must be a valid canonical MXM timestamp, got {value!r}"
        ) from err

    nanoseconds = ts_ns_to_int(timestamp)

    if nanoseconds % _POSTGRES_TIMESTAMP_NS != 0:
        raise SqlTimestampError(
            f"{field} cannot be represented exactly by PostgreSQL timestamptz: "
            f"timestamp must be aligned to microsecond precision, "
            f"got {timestamp!r}"
        )

    return timestamp


def ts_ns_to_db_datetime(
    value: TSNSScalar,
    *,
    field: str,
) -> datetime:
    """Convert one canonical MXM timestamp to Psycopg datetime form."""

    timestamp = validate_sql_timestamp(
        value,
        field=field,
    )

    nanoseconds = ts_ns_to_int(timestamp)
    microseconds = nanoseconds // _POSTGRES_TIMESTAMP_NS

    try:
        return _UNIX_EPOCH + timedelta(
            microseconds=microseconds,
        )
    except OverflowError as err:
        raise SqlTimestampError(
            f"{field} is outside the supported PostgreSQL/Python datetime "
            f"range: {timestamp!r}"
        ) from err


def db_datetime_to_ts_ns(
    value: object,
    *,
    field: str,
) -> TSNSScalar:
    """Convert one Psycopg ``timestamptz`` value to canonical MXM form."""

    if not isinstance(value, datetime):
        raise SqlTimestampError(f"{field} must be a datetime, got {value!r}")

    if value.tzinfo is None or value.utcoffset() is None:
        raise SqlTimestampError(f"{field} must be timezone-aware, got {value!r}")

    value_utc = value.astimezone(UTC)
    delta = value_utc - _UNIX_EPOCH

    total_microseconds = (
        delta.days * 86_400 + delta.seconds
    ) * 1_000_000 + delta.microseconds

    timestamp = ts_ns_from_int(total_microseconds * _POSTGRES_TIMESTAMP_NS)

    return validate_sql_timestamp(
        timestamp,
        field=field,
    )


def require_db_timestamp(
    value: object,
    *,
    field: str,
) -> TSNSScalar:
    """Require a persisted timestamp and convert it to canonical MXM form."""

    return db_datetime_to_ts_ns(
        value,
        field=field,
    )


def optional_db_timestamp(
    value: object,
    *,
    field: str,
) -> TSNSScalar | None:
    """Require a persisted timestamp or NULL."""

    if value is None:
        return None

    return require_db_timestamp(
        value,
        field=field,
    )
