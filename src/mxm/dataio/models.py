"""Core data models for mxm-dataio.

This module defines the minimal and deterministic structures used to
represent all external I/O interactions within the MXM ecosystem.

Each interaction is represented by a three-level hierarchy:

    Session → Request → Response

A Session groups multiple Requests under a common logical run
(e.g., a daily data fetch, a broker connection, or a streaming
subscription).  Each Request records the intent and parameters of an
external call, while each Response captures the corresponding outcome.

The models are dependency-light, serializable, and future-proof for
asynchronous or streaming communication patterns.

Caching and volatility
----------------------
Requests and Responses now include optional caching metadata
(`cache_mode`, `ttl_seconds`, `as_of_bucket`, `fetched_at`) which
allow `DataIoSession` to distinguish between volatile and stable
sources, control cache reuse policies, and persist provenance for
every collected payload.  These fields are informational only; all
policy logic lives in the runtime API layer.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

from mxm.types import JSONLike, JSONMap, JSONObj

# --------------------------------------------------------------------------- #
# Utility helpers
# --------------------------------------------------------------------------- #


def _utcnow() -> datetime:
    """Return the current UTC timestamp with explicit tzinfo."""
    return datetime.now(tz=UTC)


def _uuid() -> str:
    """Generate a unique identifier as a string."""
    return str(uuid.uuid4())


def _json_dumps(data: JSONLike) -> str:
    """Deterministically serialize a Python object to JSON."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class SessionMode(str, Enum):
    """Operational mode of a session."""

    SYNC = "sync"
    ASYNC = "async"
    BATCH = "batch"


class RequestMethod(str, Enum):
    """Generalized method or verb for external I/O requests."""

    GET = "GET"
    POST = "POST"
    SEND = "SEND"
    SUBSCRIBE = "SUBSCRIBE"
    COMMAND = "COMMAND"


class CacheMode(str, Enum):
    """Policy under which one request occurrence uses cached observations."""

    DEFAULT = "default"
    ONLY_IF_CACHED = "only_if_cached"
    BYPASS = "bypass"
    REVALIDATE = "revalidate"
    NEVER = "never"


class ResponseStatus(str, Enum):
    """Canonical response status values."""

    OK = "ok"
    ERROR = "error"
    PARTIAL = "partial"
    STREAM_OPEN = "stream_open"
    STREAM_MESSAGE = "stream_message"
    STREAM_CLOSED = "stream_closed"
    ACK = "ack"
    NACK = "nack"


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Session:
    """Logical ingestion or I/O session grouping multiple requests."""

    source: str
    mode: SessionMode = SessionMode.SYNC
    id: str = field(default_factory=_uuid)
    started_at: datetime = field(default_factory=_utcnow)
    ended_at: datetime | None = None

    def end(self) -> None:
        """Mark the session as completed."""
        self.ended_at = _utcnow()


@dataclass(frozen=True, slots=True)
class Request:
    """One immutable occurrence of an external I/O request."""

    session_id: str
    source: str
    kind: str
    cache_mode: CacheMode

    method: RequestMethod = RequestMethod.GET
    params: JSONObj | None = None
    body: JSONLike | None = None
    id: str = field(default_factory=_uuid)
    created_at: datetime = field(default_factory=_utcnow)
    ttl_seconds: float | None = None
    as_of_bucket: str | None = None
    cache_tag: str | None = None
    hash: str = field(init=False)

    def __post_init__(self) -> None:
        """Compute the deterministic logical-request identity."""

        base: JSONLike = {
            "source": self.source,
            "kind": self.kind,
            "method": self.method.value,
            "params": self.params,
            "body": self.body,
            "as_of_bucket": self.as_of_bucket,
            "cache_tag": self.cache_tag,
        }

        object.__setattr__(
            self,
            "hash",
            hashlib.sha256(_json_dumps(base).encode()).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class Response:
    """One immutable external response observation.

    ``id`` identifies this external observation.

    ``request_id`` identifies the request occurrence that actually contacted
    the external source and produced this observation.

    ``payload_checksum`` identifies the exact payload bytes presented by the
    external source at the adapter boundary. Distinct observations may
    legitimately reference identical payload bytes and therefore share one
    payload checksum.

    Optional metadata describes generic properties of the payload or
    acquisition. Source- or transport-specific metadata belongs in
    ``adapter_meta``.
    """

    request_id: str
    status: ResponseStatus
    payload_checksum: str
    size_bytes: int

    id: str = field(default_factory=_uuid)
    sequence: int | None = None

    created_at: datetime = field(default_factory=_utcnow)
    fetched_at: datetime = field(default_factory=_utcnow)

    media_type: str | None = None
    encoding: str | None = None
    elapsed_ms: int | None = None
    adapter_meta: JSONObj | None = None

    @classmethod
    def from_bytes(
        cls,
        request_id: str,
        status: ResponseStatus,
        data: bytes,
        sequence: int | None = None,
    ) -> "Response":
        """Create an observation from exact external payload bytes."""

        return cls(
            request_id=request_id,
            status=status,
            payload_checksum=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            sequence=sequence,
        )

    @classmethod
    def from_adapter_result(
        cls,
        request_id: str,
        status: ResponseStatus,
        result: "AdapterResult",
        sequence: int | None = None,
    ) -> "Response":
        """Create an observation from an adapter result.

        The payload checksum and size are derived from the exact bytes at the
        adapter boundary. Generic acquisition metadata is copied onto the
        observation; source-specific metadata remains opaque to DataIO.
        """

        return cls(
            request_id=request_id,
            status=status,
            payload_checksum=hashlib.sha256(result.data).hexdigest(),
            size_bytes=len(result.data),
            sequence=sequence,
            media_type=result.media_type,
            encoding=result.encoding,
            elapsed_ms=result.elapsed_ms,
            adapter_meta=(
                dict(result.adapter_meta) if result.adapter_meta is not None else None
            ),
        )

    def verify_payload(self, data: bytes) -> bool:
        """Return whether bytes match this observation's payload identity."""

        return hashlib.sha256(data).hexdigest() == self.payload_checksum


@dataclass(frozen=True, slots=True)
class AdapterResult:
    """Exact external payload plus generic acquisition metadata.

    ``data`` contains the exact payload bytes presented by the external source
    at the adapter boundary. DataIO does not semantically transform these
    bytes before deriving payload identity or persisting them.

    Generic metadata may describe the representation or acquisition where
    applicable. Transport-, protocol-, device-, or source-specific facts
    belong in ``adapter_meta``.

    Examples of adapter-specific metadata include HTTP status and headers,
    filesystem path and inode, broker request IDs, Kafka offsets, or device
    identifiers. DataIO assigns no semantics to those values.
    """

    data: bytes

    media_type: str | None = None
    encoding: str | None = None
    elapsed_ms: int | None = None
    adapter_meta: JSONObj | None = None

    def meta_dict(self) -> JSONMap:
        """Return generic metadata as JSON for legacy persistence callers.

        This compatibility helper may be removed when the legacy filesystem
        sidecar persistence path is retired.
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
