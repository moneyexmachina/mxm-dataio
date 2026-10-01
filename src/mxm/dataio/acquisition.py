"""Generic acquisition of one external Response and its exact payload."""

from __future__ import annotations

from collections.abc import Callable

from mxm.dataio.adapters import Fetcher
from mxm.dataio.models import Request, Response
from mxm.dataio.payloads import PayloadStore
from mxm.types.timestamps import TSNSScalar


def acquire_response(
    *,
    payload_store: PayloadStore,
    timestamp_source: Callable[[], TSNSScalar],
    request: Request,
    adapter: Fetcher,
) -> tuple[Response, bytes]:
    """Acquire, store, and return one external Response and its exact bytes.

    The application-provided adapter must identify the same source as the
    Request. Adapter failures and payload-store failures propagate unchanged.
    Relational persistence and resolution policy are deliberately outside this
    capability.
    """

    if adapter.source != request.source:
        raise ValueError(
            "Adapter source does not match Request source: "
            f"adapter.source={adapter.source!r}, request.source={request.source!r}"
        )

    result = adapter.fetch(
        request,
    )

    fetched_at = timestamp_source()
    created_at = timestamp_source()

    response = Response.from_adapter_result(
        request_id=request.id,
        result=result,
        created_at=created_at,
        fetched_at=fetched_at,
    )

    payload_store.put(
        response.payload_checksum,
        result.data,
    )

    return response, result.data


__all__ = [
    "acquire_response",
]
