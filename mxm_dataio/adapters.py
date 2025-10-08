"""Adapter interface definitions for mxm-dataio.

This module defines the canonical interface hierarchy for all adapters that
connect the MXM DataIO layer to external systems. Adapters translate between
the generic Request/Response model used internally and the specific protocols
used by each data source, broker, or stream.

Every adapter must inherit from :class:`MXMDataIoAdapter` and may additionally
implement one or more capability interfaces such as :class:`Fetcher`,
:class:`Sender`, or :class:`Streamer`.

Example
-------
    from mxm_dataio.adapters import Fetcher
    from mxm_dataio.models import Request

    class JustETFFetcher:
        source = "justetf"

        def fetch(self, request: Request) -> bytes:
            # perform HTTP GET and return raw bytes
            ...

        def describe(self) -> str:
            return "Fetch ETF data from JustETF"

        def close(self) -> None:
            pass
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from mxm_dataio.models import Request


@runtime_checkable
class MXMDataIoAdapter(Protocol):
    """Base protocol for all MXM DataIO adapters.

    Each adapter represents a logical connection to a specific external system.
    It must expose a unique ``source`` identifier and implement descriptive and
    teardown methods.

    Attributes
    ----------
    source:
        Canonical identifier for the external system (e.g., ``"justetf"``).
    """

    source: str

    # Optional descriptive / lifecycle methods
    def describe(self) -> str:
        """Return a human-readable description of the adapter."""
        ...

    def close(self) -> None:
        """Release any held resources (e.g., sessions or sockets)."""
        ...


@runtime_checkable
class Fetcher(MXMDataIoAdapter, Protocol):
    """Capability interface for adapters that can fetch data.

    Implementations should perform the necessary I/O to retrieve external data
    and return the raw bytes of the response.
    """

    def fetch(self, request: Request) -> bytes:
        """Perform the external I/O and return raw response bytes."""
        ...


@runtime_checkable
class Sender(MXMDataIoAdapter, Protocol):
    """Capability interface for adapters that can send or post data."""

    def send(self, request: Request, payload: bytes) -> dict[str, str]:
        """Send or post data to an external system and return a metadata map."""
        ...


@runtime_checkable
class Streamer(MXMDataIoAdapter, Protocol):
    """Capability interface for adapters that can stream data asynchronously."""

    async def stream(self, request: Request) -> None:
        """Subscribe to a continuous data stream."""
        ...


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
