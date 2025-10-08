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
"""

from __future__ import annotations

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
    """

    session_id: str
    kind: str
    method: RequestMethod = RequestMethod.GET
    params: dict[str, Any] | None = None
    body: dict[str, Any] | None = None
    id: str = field(default_factory=_uuid)
    created_at: datetime = field(default_factory=_utcnow)
    hash: str = field(init=False)

    def __post_init__(self) -> None:
        """Compute a deterministic hash for the request."""
        serialized = _json_dumps({"params": self.params, "body": self.body})
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
    """

    request_id: str
    status: ResponseStatus = ResponseStatus.OK
    checksum: str | None = None
    path: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    id: str = field(default_factory=_uuid)
    sequence: int | None = None
    size_bytes: int | None = None

    @classmethod
    def from_bytes(
        cls,
        request_id: str,
        status: ResponseStatus,
        data: bytes,
        path: str,
        sequence: int | None = None,
    ) -> Response:
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

    def verify(self, data: bytes) -> bool:
        """Return True if the given data matches the stored checksum."""
        if self.checksum is None:
            return False
        return hashlib.sha256(data).hexdigest() == self.checksum
