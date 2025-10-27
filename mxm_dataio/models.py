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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

# --------------------------------------------------------------------------- #
# Utility helpers
# --------------------------------------------------------------------------- #


def _utcnow() -> datetime:
    """Return the current UTC timestamp with explicit tzinfo."""
    return datetime.now(tz=timezone.utc)


def _uuid() -> str:
    """Generate a unique identifier as a string."""
    return str(uuid.uuid4())


def _json_dumps(data: Any) -> str:
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
    as_of: datetime = field(default_factory=_utcnow)
    id: str = field(default_factory=_uuid)
    started_at: datetime = field(default_factory=_utcnow)
    ended_at: datetime | None = None

    def end(self) -> None:
        """Mark the session as completed."""
        self.ended_at = _utcnow()


@dataclass(slots=True)
class Request:
    """Represents a single external I/O request.

    Requests may represent read operations (e.g., data downloads),
    write operations (e.g., order placement), or control messages
    (e.g., subscribe/unsubscribe).  They are hashable and fully
    deterministic given identical parameters.

    Caching metadata
    ----------------
    The optional fields `cache_mode`, `ttl_seconds`, and `as_of_bucket`
    record the policy context under which the request was executed.
    They are also incorporated into the deterministic request hash,
    ensuring that time-bucketed or TTL-sensitive requests produce
    distinct fingerprints.
    """

    session_id: str
    kind: str
    method: RequestMethod = RequestMethod.GET
    params: dict[str, Any] | None = None
    body: dict[str, Any] | None = None
    id: str = field(default_factory=_uuid)
    created_at: datetime = field(default_factory=_utcnow)
    cache_mode: str | None = None
    ttl_seconds: float | None = None
    as_of_bucket: str | None = None
    cache_tag: str | None = None
    hash: str = field(init=False)

    def __post_init__(self) -> None:
        """Compute a deterministic hash for the request."""
        base = {
            "params": self.params,
            "body": self.body,
            "as_of_bucket": self.as_of_bucket,
            "cache_tag": self.cache_tag,
        }
        serialized = _json_dumps(base)
        self.hash = hashlib.sha256(
            f"{self.kind}:{self.method}:{serialized}".encode("utf-8")
        ).hexdigest()

    def to_json(self) -> str:
        """Return a JSON string representation of this request."""
        return _json_dumps(asdict(self))


@dataclass(slots=True)
class Response:
    """Represents the outcome of a single request.

    Responses may be one-off (for synchronous calls) or sequential
    (for streaming or asynchronous interactions).  Each response stores
    a checksum for integrity verification and can reference a file path
    to the persisted payload.

    Provenance and caching
    ----------------------
    Each response records its `fetched_at` timestamp and inherits the
    caching context (`cache_mode`, `ttl_seconds`, `as_of_bucket`) from
    the originating request.  These values enable reproducible
    re-validation and cache-audit workflows, but do not affect checksum
    computation or integrity verification.
    """

    request_id: str
    status: ResponseStatus = ResponseStatus.OK
    checksum: str | None = None
    path: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    id: str = field(default_factory=_uuid)
    sequence: int | None = None
    size_bytes: int | None = None

    # Provenance / caching metadata (taken from Request)
    fetched_at: datetime = field(default_factory=_utcnow)
    cache_mode: str | None = None
    ttl_seconds: float | None = None
    as_of_bucket: str | None = None
    cache_tag: str | None = None

    @classmethod
    def from_bytes(
        cls,
        request_id: str,
        status: ResponseStatus,
        data: bytes,
        path: str,
        sequence: int | None = None,
    ) -> "Response":
        """Create a Response object from raw bytes."""
        checksum = hashlib.sha256(data).hexdigest()
        return cls(
            request_id=request_id,
            status=status,
            checksum=checksum,
            path=path,
            sequence=sequence,
            size_bytes=len(data),
        )

    @classmethod
    def from_adapter_result(
        cls,
        request_id: str,
        status: ResponseStatus,
        result: "AdapterResult",
        path: str,
        sequence: int | None = None,
    ) -> "Response":
        """Create a Response from an AdapterResult (checksum/size derived from bytes)."""
        return cls.from_bytes(
            request_id=request_id,
            status=status,
            data=result.data,
            path=path,
            sequence=sequence,
        )

    def verify(self, data: bytes) -> bool:
        """Return True if the given data matches the stored checksum."""
        if self.checksum is None:
            return False
        return hashlib.sha256(data).hexdigest() == self.checksum


@dataclass(slots=True)
class AdapterResult:
    """Unified return envelope for adapters.

    Adapters may return either:
      • raw bytes (status quo), or
      • an AdapterResult carrying bytes + transport metadata.

    The `data` field contains the exact payload to persist under checksum.
    All other fields are optional metadata that can be stored as a sidecar
    JSON alongside the payload for inspection/replay.

    Fields
    ------
    data:
        Raw payload bytes from the external system (exact as received).
    content_type:
        MIME type if known (e.g., "application/json", "text/csv").
    encoding:
        Text encoding if applicable (e.g., "utf-8").
    transport_status:
        Transport-layer status code (e.g., HTTP status).
    url:
        Final request URL after redirects, if relevant.
    elapsed_ms:
        End-to-end elapsed time in milliseconds.
    headers:
        Flattened response headers (string-valued).
    adapter_meta:
        Free-form, source-specific metadata (rate limits, request id, etc.).
    """

    data: bytes
    content_type: str | None = None
    encoding: str | None = None
    transport_status: int | None = None
    url: str | None = None
    elapsed_ms: int | None = None
    headers: dict[str, str] | None = None
    adapter_meta: dict[str, Any] | None = None

    def meta_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict of all non-payload metadata."""
        return {
            k: v
            for k, v in {
                "content_type": self.content_type,
                "encoding": self.encoding,
                "transport_status": self.transport_status,
                "url": self.url,
                "elapsed_ms": self.elapsed_ms,
                "headers": self.headers,
                "adapter_meta": self.adapter_meta,
            }.items()
            if v is not None
        }
