"""Policy-aware recovery of reusable DataIO Responses and payloads.

This module applies the current Request's reuse policy above the relational
candidate lookup.

It deliberately does not decide whether a cache miss should trigger external
acquisition or fail the larger resolution attempt. Both ``DEFAULT`` and
``ONLY_IF_CACHED`` attempt reuse here; their different miss behavior belongs
to the resolution workflow.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

from mxm.dataio.models import CacheMode, Request, Response
from mxm.dataio.payloads import (
    PayloadIntegrityError,
    PayloadNotFoundError,
    PayloadStore,
    require_payload_matches_checksum,
)
from mxm.dataio.sql.postgres import PostgresDatabase
from mxm.dataio.sql.responses import fetch_response_by_id
from mxm.dataio.sql.reuse import (
    ReuseLookupError,
    fetch_reuse_candidate_response_id,
)
from mxm.types.timestamps import (
    TSNSScalar,
    ts_ns_from_int,
    ts_ns_to_int,
)

_MICROSECONDS_PER_SECOND = 1_000_000
_NANOSECONDS_PER_MICROSECOND = 1_000


def find_reusable_response(
    *,
    database: PostgresDatabase,
    payload_store: PayloadStore,
    request: Request,
) -> tuple[Response, bytes] | None:
    """Return the newest usable Response and its exact payload, if available.

    ``BYPASS`` disables candidate lookup. Other cache modes use the Request's
    acquisition namespace and optional TTL-derived freshness cutoff.

    Candidate metadata is selected and reconstructed in one short PostgreSQL
    transaction. Payload retrieval occurs only after that transaction closes.

    Missing or corrupt payload bytes make the selected candidate unusable.
    Other payload-store and PostgreSQL failures propagate.
    """

    if request.cache_mode is CacheMode.BYPASS:
        return None

    minimum_fetched_at = _minimum_fetched_at(
        request,
    )

    with database.transaction() as connection:
        response_id = fetch_reuse_candidate_response_id(
            connection,
            schema=database.schema,
            source=request.source,
            request_hash=request.hash,
            as_of_bucket=request.as_of_bucket,
            cache_tag=request.cache_tag,
            minimum_fetched_at=minimum_fetched_at,
        )

        if response_id is None:
            return None

        response = fetch_response_by_id(
            connection,
            schema=database.schema,
            response_id=response_id,
        )

        if response is None:
            raise ReuseLookupError(
                "Reuse candidate Response was not present during reconstruction: "
                f"response_id={response_id!r}"
            )

    try:
        data = payload_store.get(
            response.payload_checksum,
        )

        require_payload_matches_checksum(
            response.payload_checksum,
            data,
        )
    except (PayloadNotFoundError, PayloadIntegrityError):
        return None

    return response, data


def _minimum_fetched_at(
    request: Request,
) -> TSNSScalar | None:
    """Return the current Request's optional inclusive freshness cutoff."""

    if request.ttl_seconds is None:
        return None

    ttl_microseconds = int(
        (
            Decimal(str(request.ttl_seconds)) * _MICROSECONDS_PER_SECOND
        ).to_integral_value(
            rounding=ROUND_FLOOR,
        )
    )
    cutoff_nanoseconds = (
        ts_ns_to_int(
            request.created_at,
        )
        - ttl_microseconds * _NANOSECONDS_PER_MICROSECOND
    )
    cutoff_nanoseconds = (
        -(-cutoff_nanoseconds // _NANOSECONDS_PER_MICROSECOND)
        * _NANOSECONDS_PER_MICROSECOND
    )

    return ts_ns_from_int(
        cutoff_nanoseconds,
    )


__all__ = [
    "find_reusable_response",
]
