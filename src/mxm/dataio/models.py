"""Core data models for mxm-dataio.

mxm-dataio is a durable cache around costly external data acquisitions.

It records:

    Request
        one occurrence of a logical external data question and the resolution
        context under which it is satisfied

    Resolution
        whether that request was satisfied by a newly acquired response
        or by reusing an existing cached response

    Response
        one actual reply acquired from the external source

    AdapterResult
        the exact payload bytes and operational result produced by the
        source adapter

Payload bytes themselves are persisted separately through PayloadStore and
are identified by their SHA-256 checksum.

DataIO deliberately does not interpret source data semantically. Parsing,
normalization, dataset semantics, and MXM epistemic state belong to layers
above mxm-dataio.

Timestamps use the canonical MXM timestamp representation from mxm-types.
Models do not acquire wall-clock time themselves; timestamps are supplied
explicitly by the runtime that creates or updates the records.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from enum import Enum

from mxm.types import JSONLike, JSONMap, JSONObj
from mxm.types.timestamps import TSNSScalar

# --------------------------------------------------------------------------- #
# Utility helpers
# --------------------------------------------------------------------------- #


def _uuid() -> str:
    """Generate a unique identifier as a string."""

    return str(uuid.uuid4())


def _json_dumps(data: JSONLike) -> str:
    """Deterministically serialize a JSON-compatible value."""

    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
    )


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class CacheMode(str, Enum):
    """Policy governing reuse of previously acquired responses.

    DEFAULT
        Reuse the newest eligible cached response when one exists.
        Otherwise acquire and persist a new response.

    ONLY_IF_CACHED
        Reuse the newest eligible cached response when one exists.
        Otherwise fail without contacting the external source.

    BYPASS
        Do not consider cached responses. Always acquire and persist a new
        response.
    """

    DEFAULT = "default"
    ONLY_IF_CACHED = "only_if_cached"
    BYPASS = "bypass"


class ResponseStatus(str, Enum):
    """Operational classification of an acquired external reply.

    The source adapter assigns this classification.

    OK
        The external interaction produced a complete valid answer to the
        logical request. DataIO does not interpret the semantic content of
        that answer; a valid "no data" reply may therefore still be OK.

    ERROR
        The external interaction produced a reply that does not constitute a
        valid reusable answer to the request.

    Only OK responses are eligible for cache reuse.
    """

    OK = "ok"
    ERROR = "error"


class ResolutionKind(str, Enum):
    """How one Request occurrence was satisfied."""

    ACQUIRED = "acquired"
    REUSED = "reused"


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Request:
    """One immutable occurrence of a logical external data question.

    ``id`` identifies this individual Request occurrence.

    ``source`` identifies the external source adapter namespace and forms part
    of the reuse namespace.

    ``kind`` identifies the logical vendor operation, for example
    ``"databento.timeseries.get_range"``.

    ``params`` contains the canonical logical arguments to that operation.
    Transport details such as HTTP method, query-string versus request-body
    placement, SDK invocation shape, authentication, and pagination belong to
    the source adapter and are deliberately absent from Request.

    ``hash`` identifies the logical question itself. It is derived only from
    ``kind`` and ``params``.

    ``cache_mode`` and ``ttl_seconds`` record the caller-supplied reuse policy
    for this Request occurrence.

    ``as_of_bucket`` and ``cache_tag`` are opaque caller-supplied reuse
    partition coordinates. DataIO compares them for equality but assigns no
    temporal, versioning, or domain semantics to them.

    Resolution context deliberately does not participate in the question hash.

    ``created_at`` records when this Request occurrence was created. The
    runtime supplies the timestamp.
    """

    source: str
    kind: str
    cache_mode: CacheMode
    created_at: TSNSScalar

    params: JSONObj | None = None
    ttl_seconds: float | None = None
    as_of_bucket: str | None = None
    cache_tag: str | None = None

    id: str = field(default_factory=_uuid)
    hash: str = field(init=False)

    def __post_init__(self) -> None:
        """Compute the deterministic logical-question identity."""

        base: JSONLike = {
            "kind": self.kind,
            "params": self.params,
        }

        object.__setattr__(
            self,
            "hash",
            hashlib.sha256(
                _json_dumps(base).encode(),
            ).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class Resolution:
    """Immutable record of how one Request occurrence was satisfied.

    ``request_id`` identifies the Request occurrence being resolved.

    ``response_id`` identifies the Response whose payload satisfied that
    Request.

    ``kind`` records whether the Response was newly acquired for this Request
    or reused from an earlier acquisition.

    One Request occurrence has at most one final Resolution, so ``request_id``
    is sufficient as the persistence identity.

    For ACQUIRED:

        Response.request_id == Resolution.request_id

    For REUSED:

        Response.request_id != Resolution.request_id

    ``resolved_at`` is supplied by the runtime.
    """

    request_id: str
    response_id: str
    kind: ResolutionKind
    resolved_at: TSNSScalar


@dataclass(frozen=True, slots=True)
class Response:
    """One immutable reply actually acquired from an external source.

    ``id`` identifies this individual external response occurrence.

    ``request_id`` identifies the Request occurrence that actually contacted
    the external source and acquired this Response.

    ``status`` is the generic operational classification assigned by the
    source adapter.

    ``payload_checksum`` identifies the exact payload bytes acquired at the
    adapter boundary. Distinct Responses may legitimately reference the same
    checksum when repeated external calls return byte-for-byte identical data.

    ``fetched_at`` records when the external response was acquired. It carries
    cache-freshness meaning.

    ``created_at`` records when the Response record was created.

    Both timestamps are supplied explicitly by the runtime.

    Generic representation and acquisition metadata are represented directly.
    Source- or protocol-specific facts remain opaque in ``adapter_meta``.
    """

    request_id: str
    status: ResponseStatus
    payload_checksum: str
    size_bytes: int
    created_at: TSNSScalar
    fetched_at: TSNSScalar

    id: str = field(default_factory=_uuid)

    media_type: str | None = None
    encoding: str | None = None
    elapsed_ms: int | None = None
    adapter_meta: JSONObj | None = None

    @classmethod
    def from_bytes(
        cls,
        *,
        request_id: str,
        status: ResponseStatus,
        data: bytes,
        created_at: TSNSScalar,
        fetched_at: TSNSScalar,
    ) -> Response:
        """Create a Response from exact external payload bytes."""

        return cls(
            request_id=request_id,
            status=status,
            payload_checksum=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            created_at=created_at,
            fetched_at=fetched_at,
        )

    @classmethod
    def from_adapter_result(
        cls,
        *,
        request_id: str,
        result: AdapterResult,
        created_at: TSNSScalar,
        fetched_at: TSNSScalar,
    ) -> Response:
        """Create a Response from one source-adapter acquisition result."""

        return cls(
            request_id=request_id,
            status=result.status,
            payload_checksum=hashlib.sha256(result.data).hexdigest(),
            size_bytes=len(result.data),
            created_at=created_at,
            fetched_at=fetched_at,
            media_type=result.media_type,
            encoding=result.encoding,
            elapsed_ms=result.elapsed_ms,
            adapter_meta=(
                dict(result.adapter_meta) if result.adapter_meta is not None else None
            ),
        )

    def verify_payload(self, data: bytes) -> bool:
        """Return whether bytes match this Response's payload identity."""

        return hashlib.sha256(data).hexdigest() == self.payload_checksum


@dataclass(frozen=True, slots=True)
class AdapterResult:
    """Result of one actual external acquisition performed by an adapter.

    ``status`` is the adapter's generic operational classification of the
    external reply.

    ``data`` contains the exact payload bytes presented to DataIO at the
    adapter boundary. DataIO does not semantically transform these bytes before
    deriving payload identity or persisting them.

    Generic representation and acquisition metadata may be provided directly.

    Source-, SDK-, or protocol-specific metadata belongs in ``adapter_meta``.
    DataIO persists those values but assigns no semantics to them.
    """

    status: ResponseStatus
    data: bytes

    media_type: str | None = None
    encoding: str | None = None
    elapsed_ms: int | None = None
    adapter_meta: JSONObj | None = None

    def meta_dict(self) -> JSONMap:
        """Return generic metadata for legacy persistence callers.

        This compatibility helper can be removed when the legacy DataIO runtime
        and filesystem sidecar persistence path have been retired.
        """

        result: JSONMap = {}

        if self.media_type is not None:
            result["media_type"] = self.media_type

        if self.encoding is not None:
            result["encoding"] = self.encoding

        if self.elapsed_ms is not None:
            result["elapsed_ms"] = self.elapsed_ms

        if self.adapter_meta is not None:
            result["adapter_meta"] = dict(self.adapter_meta)

        return result
