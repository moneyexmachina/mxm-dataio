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
- Persist AdapterResults: payload bytes (checksum-verified) + sidecar metadata
- Optional request-hash caching to avoid duplicate work

Notes
-----
* Streaming is intentionally left as a future extension; the method is defined
  with a clear exception for now (see TODO).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Any, Optional, Type

from mxm_config import MXMConfig

from mxm_dataio.adapters import Fetcher, Sender
from mxm_dataio.models import (
    AdapterResult,
    Request,
    RequestMethod,
    Response,
    ResponseStatus,
    Session,
    SessionMode,
)
from mxm_dataio.registry import resolve_adapter
from mxm_dataio.store import Store

# --------------------------------------------------------------------------- #
# Cache mode enumeration
# --------------------------------------------------------------------------- #


class CacheMode(str, Enum):
    """Policy controlling cache usage and persistence."""

    DEFAULT = "default"  # Use cache if valid, else fetch
    ONLY_IF_CACHED = "only_if_cached"  # Never hit network
    BYPASS = "bypass"  # Always hit network, persist new
    REVALIDATE = "revalidate"  # Use ETag/If-Modified-Since when supported
    NEVER = "never"  # Do not use or store cache (ephemeral)


# --------------------------------------------------------------------------- #
# DataIoSession
# --------------------------------------------------------------------------- #


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
        Configuration dict (from mxm-config) providing paths.
    store:
        Optional pre-initialised Store. If omitted, a per-config singleton
        instance is retrieved.
    mode:
        SessionMode flag ("sync" by default). Kept for future async/streaming.
    cache_mode:
        One of CacheMode. Controls cache usage and persistence.
    ttl:
        Optional time-to-live in seconds for cached responses.
    use_cache:
        Deprecated shim for backward compatibility.
    cache_store:
        Optional ephemeral cache layer. If provided, it is checked before the
        archival Store and written through after successful fetches. Expected
        to expose a minimal interface: get(key, ttl) → bytes | None; put(key, data) → Path.
    """

    def __init__(
        self,
        source: str,
        cfg: MXMConfig,
        *,
        store: Optional[Store] = None,
        cache_store: Optional[Any] = None,
        mode: SessionMode = SessionMode.SYNC,
        cache_mode: CacheMode | str = CacheMode.DEFAULT,
        ttl: float | None = None,
        as_of_bucket: str | None = None,
        cache_tag: str | None = None,
        use_cache: Optional[bool] = None,
    ) -> None:
        self.source = source
        self.cfg = cfg
        self.store = store or Store.get_instance(cfg)
        self.cache_store = cache_store
        self.mode = mode
        self.ttl = ttl
        self.as_of_bucket = as_of_bucket
        self.cache_tag = cache_tag
        self.cache_mode = CacheMode(cache_mode)

        # Backward-compatibility shim
        if use_cache is not None:
            self.cache_mode = CacheMode.DEFAULT if use_cache else CacheMode.BYPASS

        self._session: Optional[Session] = None

    # ------------------------------------------------------------------ #
    # Context management
    # ------------------------------------------------------------------ #

    def __enter__(self) -> "DataIoSession":
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
            cache_mode=self.cache_mode.value,
            ttl_seconds=self.ttl,
            as_of_bucket=self.as_of_bucket,
            cache_tag=self.cache_tag,
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

        # 1) Try ephemeral cache_store first (if any) and policy allows
        if self.cache_store is not None and self.cache_mode not in (
            CacheMode.BYPASS,
            CacheMode.NEVER,
        ):
            cached_bytes = self.cache_store.get(request.hash, ttl=self.ttl)
            if cached_bytes is not None:
                # Serve from ephemeral cache without touching archive
                return Response.from_bytes(
                    request_id=request.id,
                    status=ResponseStatus.OK,
                    data=cached_bytes,
                    path="<cache>",
                )

        # 2) Try archive store cache if policy allows
        cached_resp: Response | None = None
        if self.cache_mode not in (CacheMode.BYPASS, CacheMode.NEVER):
            cached_resp = self.store.get_cached_response_by_request_hash_and_bucket(
                request_hash=request.hash,
                as_of_bucket=request.as_of_bucket,
            )
            if cached_resp is not None:
                if self.cache_mode == CacheMode.ONLY_IF_CACHED:
                    # Use it regardless of TTL
                    return cached_resp

                # DEFAULT / REVALIDATE: enforce TTL if provided
                if self.ttl is not None:
                    age = (
                        datetime.now(timezone.utc) - cached_resp.created_at
                    ).total_seconds()
                    if age <= self.ttl:
                        return cached_resp
                    # else stale → ignore and refetch
                else:
                    # No TTL → treat as fresh
                    return cached_resp

        # 3) If ONLY_IF_CACHED and we didn't return above, it's a miss → raise
        if self.cache_mode == CacheMode.ONLY_IF_CACHED:
            raise RuntimeError(
                f"Cache miss for request hash={request.hash} bucket={request.as_of_bucket!r}"
            )

        # 4) Policy requires a fresh fetch (BYPASS/NEVER or stale/miss)
        result: AdapterResult = adapter.fetch(request)

        # Write-through to ephemeral cache (if present and allowed)
        if self.cache_store is not None and self.cache_mode not in (
            CacheMode.NEVER,
            CacheMode.ONLY_IF_CACHED,
        ):
            try:
                self.cache_store.put(request.hash, result.data)
            except Exception:
                # Cache is best-effort; don't fail the fetch path
                pass

        # Build Response and persist to archive unless policy says NEVER
        if self.cache_mode == CacheMode.NEVER:
            # Ephemeral-only response; don't write bytes/metadata/row to archive
            resp = Response.from_adapter_result(
                request_id=request.id,
                status=ResponseStatus.OK,
                result=result,
                path="<ephemeral>",
                sequence=None,
            )
            resp.cache_mode = self.cache_mode.value
            resp.ttl_seconds = self.ttl
            resp.as_of_bucket = request.as_of_bucket
            resp.cache_tag = request.cache_tag
            return resp

        # Normal archival persistence
        resp = persist_result_as_response(
            store=self.store,
            request_id=request.id,
            status=ResponseStatus.OK,
            result=result,
            cache_mode=self.cache_mode.value,
            ttl_seconds=self.ttl,
            as_of_bucket=request.as_of_bucket,
            cache_tag=request.cache_tag,
        )
        self.store.insert_response(resp)
        return resp

    def send(self, request: Request, payload: bytes | dict[str, Any]) -> Response:
        """Perform a send via a Sender-capable adapter and persist the Response."""
        adapter = resolve_adapter(self.source)
        if not isinstance(adapter, Sender):
            raise TypeError(f"Adapter '{self.source}' does not support sending.")

        # Sending operations are normally non-cacheable
        if self.cache_mode == CacheMode.NEVER:
            payload_bytes = _ensure_bytes(payload)
            result: AdapterResult = adapter.send(request, payload_bytes)
            return Response.from_adapter_result(
                request_id=request.id,
                status=ResponseStatus.ACK,
                result=result,
                path="<ephemeral>",
            )

        if self.cache_mode == CacheMode.ONLY_IF_CACHED:
            cached = self._maybe_get_cached_response(request)
            if cached is not None:
                return cached
            raise RuntimeError("Cache miss under ONLY_IF_CACHED mode for send().")

        payload_bytes = _ensure_bytes(payload)
        result: AdapterResult = adapter.send(request, payload_bytes)
        resp = persist_result_as_response(
            store=self.store,
            request_id=request.id,
            status=ResponseStatus.ACK,
            result=result,
            cache_mode=self.cache_mode.value,
            ttl_seconds=self.ttl,
            as_of_bucket=request.as_of_bucket,
            cache_tag=request.cache_tag,
        )
        self.store.insert_response(resp)
        return resp

    async def stream(self, request: Request) -> None:
        _ = request
        raise NotImplementedError(
            "Streaming will be implemented in a future iteration."
        )

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _maybe_get_cached_response(self, request: Request) -> Optional[Response]:
        """Return the most recent Response for a previously-seen request hash and bucket."""
        try:
            return self.store.get_cached_response_by_request_hash_and_bucket(
                request.hash, request.as_of_bucket
            )
        except AttributeError:
            # fallback for older store schema
            return self.store.get_cached_response_by_request_hash(request.hash)


# --------------------------------------------------------------------------- #
# Module-level utilities
# --------------------------------------------------------------------------- #


def persist_result_as_response(
    *,
    store: Store,
    request_id: str,
    status: ResponseStatus,
    result: AdapterResult,
    sequence: int | None = None,
    cache_mode: str | None = None,
    ttl_seconds: float | None = None,
    as_of_bucket: str | None = None,
    cache_tag: str | None = None,
) -> Response:
    """Persist an AdapterResult (bytes + metadata) and return a Response row."""
    path = store.write_payload(result.data)
    meta = result.meta_dict()
    if meta:
        stem = Path(path).stem if hasattr(path, "stem") else Path(str(path)).stem
        store.write_metadata(stem, meta)

    resp = Response.from_adapter_result(
        request_id=request_id,
        status=status,
        result=result,
        path=str(path),
        sequence=sequence,
    )
    resp.cache_mode = cache_mode
    resp.ttl_seconds = ttl_seconds
    resp.as_of_bucket = as_of_bucket
    resp.cache_tag = cache_tag
    return resp


def _ensure_bytes(payload: bytes | dict[str, Any]) -> bytes:
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return bytes(payload)
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
