"""Unit tests for the PostgreSQL timestamp representation bridge."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import numpy as np
import pytest

from mxm.dataio.sql.sql_timestamps import (
    SqlTimestampError,
    db_datetime_to_ts_ns,
    optional_db_timestamp,
    require_db_timestamp,
    ts_ns_to_db_datetime,
    validate_sql_timestamp,
)
from mxm.types.timestamps import (
    TSNSScalar,
    ts_ns_from_int,
    ts_ns_from_str,
    ts_ns_to_int,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_EPOCH = ts_ns_from_str("1970-01-01T00:00:00.000000000Z")
_MODERN = ts_ns_from_str("2026-09-03T09:30:00.123456000Z")
_PRE_EPOCH = ts_ns_from_str("1969-12-31T23:59:59.123456000Z")


# ---------------------------------------------------------------------------
# validate_sql_timestamp
# ---------------------------------------------------------------------------


def test_validate_sql_timestamp_accepts_microsecond_aligned_timestamp() -> None:
    """A canonical timestamp exactly representable by PostgreSQL is accepted."""

    assert (
        validate_sql_timestamp(
            _MODERN,
            field="created_at",
        )
        == _MODERN
    )


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-03T09:30:00Z",
        datetime(
            2026,
            9,
            3,
            9,
            30,
            tzinfo=UTC,
        ),
        123,
    ],
)
def test_validate_sql_timestamp_rejects_noncanonical_values(
    value: object,
) -> None:
    """The SQL boundary accepts only canonical MXM timestamp scalars."""

    with pytest.raises(
        SqlTimestampError,
        match=r"created_at must be a valid canonical MXM timestamp",
    ):
        validate_sql_timestamp(
            value,
            field="created_at",
        )


def test_validate_sql_timestamp_rejects_nat() -> None:
    """NaT cannot cross the persistence boundary."""

    value = np.datetime64("NaT", "ns")

    with pytest.raises(
        SqlTimestampError,
        match=r"created_at must be a valid canonical MXM timestamp",
    ):
        validate_sql_timestamp(
            value,
            field="created_at",
        )


def test_validate_sql_timestamp_rejects_sub_microsecond_precision() -> None:
    """PostgreSQL persistence never silently truncates nanoseconds."""

    value = ts_ns_from_int(
        ts_ns_to_int(_MODERN) + 1,
    )

    with pytest.raises(
        SqlTimestampError,
        match=r"cannot be represented exactly.*microsecond precision",
    ):
        validate_sql_timestamp(
            value,
            field="created_at",
        )


# ---------------------------------------------------------------------------
# TSNSScalar -> Psycopg datetime
# ---------------------------------------------------------------------------


def test_ts_ns_to_db_datetime_converts_epoch_exactly() -> None:
    """The Unix epoch maps exactly to UTC Python datetime."""

    result = ts_ns_to_db_datetime(
        _EPOCH,
        field="created_at",
    )

    assert result == datetime(
        1970,
        1,
        1,
        tzinfo=UTC,
    )


def test_ts_ns_to_db_datetime_preserves_microseconds() -> None:
    """Supported sub-second precision survives conversion exactly."""

    result = ts_ns_to_db_datetime(
        _MODERN,
        field="created_at",
    )

    assert result == datetime(
        2026,
        9,
        3,
        9,
        30,
        0,
        123456,
        tzinfo=UTC,
    )


def test_ts_ns_to_db_datetime_handles_pre_epoch_timestamp() -> None:
    """Negative epoch offsets are converted without rounding errors."""

    result = ts_ns_to_db_datetime(
        _PRE_EPOCH,
        field="created_at",
    )

    assert result == datetime(
        1969,
        12,
        31,
        23,
        59,
        59,
        123456,
        tzinfo=UTC,
    )


def test_ts_ns_to_db_datetime_returns_utc_aware_datetime() -> None:
    """The Psycopg-side representation is timezone-aware UTC."""

    result = ts_ns_to_db_datetime(
        _MODERN,
        field="created_at",
    )

    assert result.tzinfo is not None
    assert result.utcoffset() == timedelta(0)


def test_ts_ns_to_db_datetime_rejects_sub_microsecond_precision() -> None:
    """Conversion rejects values PostgreSQL cannot represent exactly."""

    value = ts_ns_from_int(
        ts_ns_to_int(_MODERN) + 1,
    )

    with pytest.raises(
        SqlTimestampError,
        match=r"cannot be represented exactly.*microsecond precision",
    ):
        ts_ns_to_db_datetime(
            value,
            field="created_at",
        )


# ---------------------------------------------------------------------------
# Psycopg datetime -> TSNSScalar
# ---------------------------------------------------------------------------


def test_db_datetime_to_ts_ns_converts_utc_datetime_exactly() -> None:
    """A UTC-aware Psycopg datetime becomes the corresponding MXM timestamp."""

    value = datetime(
        2026,
        9,
        3,
        9,
        30,
        0,
        123456,
        tzinfo=UTC,
    )

    result = db_datetime_to_ts_ns(
        value,
        field="created_at",
    )

    assert result == _MODERN


def test_db_datetime_to_ts_ns_normalises_non_utc_offset() -> None:
    """Equivalent aware datetimes map to the same canonical instant."""

    utc_plus_one = timezone(
        timedelta(hours=1),
    )

    value = datetime(
        2026,
        9,
        3,
        10,
        30,
        0,
        123456,
        tzinfo=utc_plus_one,
    )

    result = db_datetime_to_ts_ns(
        value,
        field="created_at",
    )

    assert result == _MODERN


def test_db_datetime_to_ts_ns_handles_pre_epoch_timestamp() -> None:
    """Pre-epoch Psycopg timestamps convert exactly."""

    value = datetime(
        1969,
        12,
        31,
        23,
        59,
        59,
        123456,
        tzinfo=UTC,
    )

    result = db_datetime_to_ts_ns(
        value,
        field="created_at",
    )

    assert result == _PRE_EPOCH


def test_db_datetime_to_ts_ns_rejects_naive_datetime() -> None:
    """Psycopg timestamp values must be timezone-aware."""

    value = datetime(
        2026,
        9,
        3,
        9,
        30,
    )

    with pytest.raises(
        SqlTimestampError,
        match=r"created_at must be timezone-aware",
    ):
        db_datetime_to_ts_ns(
            value,
            field="created_at",
        )


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-03T09:30:00Z",
        123,
        None,
    ],
)
def test_db_datetime_to_ts_ns_rejects_non_datetime_values(
    value: object,
) -> None:
    """The PostgreSQL side of the bridge requires Python datetime values."""

    with pytest.raises(
        SqlTimestampError,
        match=r"created_at must be a datetime",
    ):
        db_datetime_to_ts_ns(
            value,
            field="created_at",
        )


# ---------------------------------------------------------------------------
# Round-trip invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "timestamp",
    [
        _EPOCH,
        _PRE_EPOCH,
        ts_ns_from_str("2026-09-03T09:30:00.000000000Z"),
        _MODERN,
        ts_ns_from_str("2035-12-31T23:59:59.999999000Z"),
    ],
)
def test_mxm_timestamp_round_trip_is_exact(
    timestamp: TSNSScalar,
) -> None:
    """Every supported canonical timestamp survives the SQL bridge exactly."""

    database_value = ts_ns_to_db_datetime(
        timestamp,
        field="created_at",
    )

    reconstructed = db_datetime_to_ts_ns(
        database_value,
        field="created_at",
    )

    assert reconstructed == timestamp


def test_aware_datetime_round_trip_preserves_instant() -> None:
    """Non-UTC input round-trips to the same instant in canonical UTC form."""

    utc_plus_two = timezone(
        timedelta(hours=2),
    )

    original = datetime(
        2026,
        9,
        3,
        11,
        30,
        0,
        123456,
        tzinfo=utc_plus_two,
    )

    timestamp = db_datetime_to_ts_ns(
        original,
        field="created_at",
    )

    reconstructed = ts_ns_to_db_datetime(
        timestamp,
        field="created_at",
    )

    assert reconstructed == datetime(
        2026,
        9,
        3,
        9,
        30,
        0,
        123456,
        tzinfo=UTC,
    )

    assert reconstructed.timestamp() == original.timestamp()


# ---------------------------------------------------------------------------
# Required / optional read helpers
# ---------------------------------------------------------------------------


def test_require_db_timestamp_converts_valid_datetime() -> None:
    """The required-value helper delegates to the canonical conversion."""

    value = datetime(
        2026,
        9,
        3,
        9,
        30,
        0,
        123456,
        tzinfo=UTC,
    )

    assert (
        require_db_timestamp(
            value,
            field="created_at",
        )
        == _MODERN
    )


def test_optional_db_timestamp_returns_none_for_null() -> None:
    """PostgreSQL NULL remains None."""

    assert (
        optional_db_timestamp(
            None,
            field="ended_at",
        )
        is None
    )


def test_optional_db_timestamp_converts_present_value() -> None:
    """A present optional PostgreSQL timestamp is converted normally."""

    value = datetime(
        2026,
        9,
        3,
        9,
        30,
        0,
        123456,
        tzinfo=UTC,
    )

    assert (
        optional_db_timestamp(
            value,
            field="ended_at",
        )
        == _MODERN
    )
