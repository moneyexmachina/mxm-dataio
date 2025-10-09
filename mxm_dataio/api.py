"""High-level runtime API for mxm-dataio.

This module defines `DataIoSession`, a context-managed runtime controller
that orchestrates external I/O via registered adapters and persists a full
audit trail using the Store.

Responsibilities
---------------
- Create and finalize a persisted Session (models.Session)
- Construct deterministic Requests (models.Request)
- Resolve the correct adapter via the registry (by source name)
- Dispatch to adapter capabilities (Fetcher / Sender)
- Persist Responses and raw payloads (checksum-verified)
- Optional request-hash caching to avoid duplicate work

Notes
-----
* Streaming is intentionally left as a future extension; the method
  is defined with a clear exception for now (see TODO).
"""

from __future__ import annotations

import json
from types import TracebackType
from typing import Any, Optional, Type

from mxm_dataio.adapters import AdapterResult, Fetcher, Sender
from mxm_dataio.models import (
    Request,
    RequestMethod,
    Response,
    ResponseStatus,
    Session,
    SessionMode,
)
from mxm_dataio.registry import resolve_adapter
from mxm_dataio.store import Store


class DataIoSession:
    """Runtime context manager for MXM DataIO operations.

    A `DataIoSession` binds to a single adapter identified by `source`
    (registered via `mxm_dataio.registry`) and provides capability-checked
    operations (`fetch`, `send`). All metadata and payloads are persisted
    via `Store`, ensuring reproducible, auditable I/O.

    Parameters
    ----------
    source:
        The registry name of the external system (e.g., "justetf", "ibkr").
    cfg:
        Resolved configuration dict (from mxm-config) providing paths.
    store:
        Optional pre-initialised Store. If omitted, a per-config singleton
        instance is retrieved.
    mode:
        SessionMode flag ("sync" by default). Kept for future async/streaming.
    use_cache:
        If True, identical requests (by `Request.hash`) will reuse a previously
        stored Response when available.
    """

    def __init__(
        self,
        source: str,
        cfg: dict[str, Any],
        *,
        store: Optional[Store] = None,
        mode: SessionMode = SessionMode.SYNC,
        use_cache: bool = True,
    ) -> None:
        self.source = source
        self.cfg = cfg
        self.store = store or Store.get_instance(cfg)
        self.mode = mode
        self.use_cache = use_cache

        self._session: Optional[Session] = None

    # ------------------------------------------------------------------ #
    # Context management
    # ------------------------------------------------------------------ #

    def __enter__(self) -> "DataIoSession":
        """Start and persist a new Session record."""
        session = Session(source=self.source, mode=self.mode)
        self.store.insert_session(session)
        self._session = session
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        """Finalize the Session by setting ended_at."""

        _ = (exc_type, exc_val, exc_tb)

        if self._session is None:
            return
        self._session.end()
        self.store.mark_session_ended(self._session.id, self._session.ended_at)

    # ------------------------------------------------------------------ #
    # Request construction
    # ------------------------------------------------------------------ #

    def request(
        self,
        *,
        kind: str,
        params: Optional[dict[str, Any]] = None,
        method: RequestMethod = RequestMethod.GET,
        body: Optional[dict[str, Any]] = None,
    ) -> Request:
        """Create and persist a Request bound to the current session."""
        if self._session is None:
            raise RuntimeError(
                "DataIoSession must be entered before creating requests."
            )
        req = Request(
            session_id=self._session.id,
            kind=kind,
            method=method,
            params=params or {},
            body=body,
        )
        self.store.insert_request(req)
        return req

    # ------------------------------------------------------------------ #
    # Capability-dispatched operations
    # ------------------------------------------------------------------ #

    def fetch(self, request: Request) -> Response:
        """Perform a fetch via a Fetcher-capable adapter and persist the Response."""
        adapter = resolve_adapter(self.source)
        if not isinstance(adapter, Fetcher):
            raise TypeError(f"Adapter '{self.source}' does not support fetching.")

        if self.use_cache:
            cached = self._maybe_get_cached_response(request_hash=request.hash)
            if cached is not None:
                return cached

        result = adapter.fetch(request)  # bytes | AdapterResult
        data, meta = _extract_bytes_and_meta(result)
        path = self.store.write_payload(data)
        # Write sidecar metadata (if any)
        if meta:
            self.store.write_metadata(path.stem, meta)

        resp = Response.from_bytes(
            request_id=request.id,
            status=ResponseStatus.OK,
            data=data,
            path=str(path),
            sequence=None,
        )
        self.store.insert_response(resp)
        return resp

    def send(self, request: Request, payload: bytes | dict[str, Any]) -> Response:
        """Perform a send via a Sender-capable adapter and persist the Response.

        Supports adapters returning:
          - AdapterResult (data+meta)
          - bytes (raw)
          - dict (treated as JSON body and stored as payload)
        """
        adapter = resolve_adapter(self.source)
        if not isinstance(adapter, Sender):
            raise TypeError(f"Adapter '{self.source}' does not support sending.")

        if self.use_cache:
            cached = self._maybe_get_cached_response(request_hash=request.hash)
            if cached is not None:
                return cached

        payload_bytes = _ensure_bytes(payload)
        result = adapter.send(
            request, payload_bytes
        )  # AdapterResult | bytes | dict[str, Any]

        # Normalize result to bytes + optional meta
        if isinstance(result, AdapterResult):
            data, meta = result.data, result.meta_dict()
        elif isinstance(result, (bytes, bytearray, memoryview)):
            data, meta = bytes(result), None
        else:
            # Assume mapping metadata; persist as deterministic JSON payload (status quo).
            data = json.dumps(result, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
            meta = None

        path = self.store.write_payload(data)
        if meta:
            self.store.write_metadata(path.stem, meta)

        resp = Response.from_bytes(
            request_id=request.id,
            status=ResponseStatus.ACK,
            data=data,
            path=str(path),
            sequence=None,
        )
        self.store.insert_response(resp)
        return resp

    async def stream(self, request: Request) -> None:
        """(Planned) Perform streaming via a Streamer-capable adapter.

        TODO: Define Streamer to return an async iterator of bytes so we can
        iterate messages and persist them as sequenced Responses.
        """
        _ = request
        raise NotImplementedError(
            "Streaming will be implemented in a future iteration."
        )

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _maybe_get_cached_response(self, *, request_hash: str) -> Optional[Response]:
        """Return the most recent Response for a previously-seen request hash, if any."""
        return self.store.get_cached_response_by_request_hash(request_hash)


# --------------------------------------------------------------------------- #
# Module-level utilities
# --------------------------------------------------------------------------- #


def _extract_bytes_and_meta(
    obj: bytes | AdapterResult,
) -> tuple[bytes, dict[str, Any] | None]:
    """Normalize adapter returns to (bytes, optional_metadata)."""
    if isinstance(obj, AdapterResult):
        return obj.data, obj.meta_dict()
    # By type hint, anything else here is bytes.
    return obj, None


def _ensure_bytes(payload: bytes | dict[str, Any]) -> bytes:
    """Return payload as bytes; dict is deterministically JSON-encoded."""
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return bytes(payload)
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
