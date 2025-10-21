"""Adapter interface definitions for mxm-dataio.

This module defines the canonical interface hierarchy for all adapters that
connect the MXM DataIO layer to external systems. Adapters translate between
the generic Request/Response model used internally and the specific protocols
used by each data source, broker, or stream.

Every adapter must satisfy :class:`MXMDataIoAdapter` and may additionally
implement one or more capability interfaces such as :class:`Fetcher`,
:class:`Sender`, or :class:`Streamer`.

Return semantics
----------------
All **synchronous** adapter operations return a metadata-rich
:class:`AdapterResult`. This replaces the historic "bytes only" return and
enables robust auditing (status, headers, URL, content-type, elapsed).

For **streaming**, adapters should expose an *async iterator* that yields
:class:`AdapterResult` chunks. This preserves transport metadata per message.

Example
-------
    from mxm_dataio.adapters import Fetcher, AdapterResult
    from mxm_dataio.models import Request

    class JustETFFetcher:
        source = "justetf"

        def fetch(self, request: Request) -> AdapterResult:
            # perform HTTP GET and return bytes + metadata
            ...

        def describe(self) -> str: ...
        def close(self) -> None: ...
"""

from typing import AsyncIterator, Protocol, runtime_checkable

from mxm_dataio.models import AdapterResult, Request


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
    """Capability interface for adapters that fetch data (e.g., HTTP GET).

    Implementations perform I/O to retrieve external data and must return an
    :class:`AdapterResult` containing the raw payload and transport metadata.
    """

    def fetch(self, request: Request) -> AdapterResult:
        """Execute the request and return a metadata-rich result."""
        ...


@runtime_checkable
class Sender(MXMDataIoAdapter, Protocol):
    """Capability interface for adapters that can send or post data."""

    def send(self, request: Request, payload: bytes) -> AdapterResult:
        """Send or post data and return a metadata-rich result."""
        ...


@runtime_checkable
class Streamer(MXMDataIoAdapter, Protocol):
    """Capability interface for adapters that produce asynchronous streams.

    Implementations should yield :class:`AdapterResult` items, one per message/
    event/chunk, preserving transport metadata alongside payload bytes.
    """

    async def stream(self, request: Request) -> AsyncIterator[AdapterResult]:
        """Yield a sequence of results for the given subscription/request."""
        ...
