"""Resolution workflow for one DataIO Request occurrence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from mxm.dataio.acquisition import acquire_response
from mxm.dataio.adapters import Fetcher
from mxm.dataio.models import (
    CacheMode,
    Request,
    Resolution,
    ResolutionKind,
    Response,
    ResponseStatus,
)
from mxm.dataio.payloads import PayloadStore
from mxm.dataio.reuse import find_reusable_response
from mxm.dataio.sql.postgres import PostgresDatabase
from mxm.dataio.sql.requests import insert_request
from mxm.dataio.sql.resolutions import insert_resolution
from mxm.dataio.sql.responses import insert_response
from mxm.types import JSONObj
from mxm.types.timestamps import TSNSScalar


@dataclass(frozen=True, slots=True)
class ResolveResult:
    """Complete outcome of one DataIO resolution attempt."""

    request: Request
    response: Response
    resolution: Resolution | None
    data: bytes


class CacheMissError(RuntimeError):
    """Raised when cache-only policy cannot resolve a Request."""


def resolve(
    *,
    database: PostgresDatabase,
    payload_store: PayloadStore,
    timestamp_source: Callable[[], TSNSScalar],
    source: str,
    kind: str,
    params: JSONObj | None,
    adapter: Fetcher,
    cache_mode: CacheMode,
    ttl_seconds: float | None = None,
    as_of_bucket: str | None = None,
    cache_tag: str | None = None,
) -> ResolveResult:
    """Run and durably record one Request-resolution attempt."""

    if adapter.source != source:
        raise ValueError(
            "Adapter source does not match requested source: "
            f"adapter.source={adapter.source!r}, source={source!r}"
        )

    request = Request(
        source=source,
        kind=kind,
        params=params,
        cache_mode=cache_mode,
        ttl_seconds=ttl_seconds,
        as_of_bucket=as_of_bucket,
        cache_tag=cache_tag,
        created_at=timestamp_source(),
    )

    with database.transaction() as connection:
        insert_request(
            connection,
            schema=database.schema,
            request=request,
        )

    reusable = find_reusable_response(
        database=database,
        payload_store=payload_store,
        request=request,
    )

    if reusable is not None:
        response, data = reusable
        resolution = Resolution(
            request_id=request.id,
            response_id=response.id,
            kind=ResolutionKind.REUSED,
            resolved_at=timestamp_source(),
        )

        with database.transaction() as connection:
            insert_resolution(
                connection,
                schema=database.schema,
                resolution=resolution,
            )

        return ResolveResult(
            request=request,
            response=response,
            resolution=resolution,
            data=data,
        )

    if cache_mode is CacheMode.ONLY_IF_CACHED:
        raise CacheMissError(
            "No reusable Response is available for cache-only Request: "
            f"request_id={request.id!r}, request_hash={request.hash!r}"
        )

    response, data = acquire_response(
        payload_store=payload_store,
        timestamp_source=timestamp_source,
        request=request,
        adapter=adapter,
    )

    resolution: Resolution | None = None

    if response.status is ResponseStatus.OK:
        resolution = Resolution(
            request_id=request.id,
            response_id=response.id,
            kind=ResolutionKind.ACQUIRED,
            resolved_at=timestamp_source(),
        )

    with database.transaction() as connection:
        insert_response(
            connection,
            schema=database.schema,
            response=response,
        )

        if resolution is not None:
            insert_resolution(
                connection,
                schema=database.schema,
                resolution=resolution,
            )

    return ResolveResult(
        request=request,
        response=response,
        resolution=resolution,
        data=data,
    )


__all__ = [
    "CacheMissError",
    "ResolveResult",
    "resolve",
]
