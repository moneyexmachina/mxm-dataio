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

from typing import Protocol, runtime_checkable

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
