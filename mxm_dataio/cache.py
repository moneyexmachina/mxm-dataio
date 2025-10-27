"""
Cache layer interfaces and utilities for mxm-dataio.

This module introduces a *forward-compatible* abstraction for an ephemeral
cache that sits in front of the archival Store:

    DataIoSession
      ├── CacheStore (ephemeral; TTL/eviction; fast reuse)
      └── Store      (immutable audit trail; append-only)

Why a protocol?
---------------
We expose a minimal `CacheStoreProtocol` so `DataIoSession` can accept *any*
object that implements this interface (file cache, SQLite cache, Redis, memory,
etc.) without depending on a specific implementation. This keeps the runtime API
stable while allowing you to swap cache backends later with zero changes to the
session orchestration or archival persistence.

Contract & semantics
--------------------
- Keys are *deterministic* request hashes (already computed by Request).
- `get(key, ttl)` returns raw bytes if present and fresh; otherwise `None`.
- `put(key, data)` stores bytes and returns a Path to the cached artifact.
- The cache is *ephemeral*: eviction/TTL may remove entries at any time.
- The archive (Store) remains the canonical, append-only provenance store.

Usage (today)
-------------
You may pass a concrete cache store that implements the protocol into
`DataIoSession(cache_store=...)`. If `cache_store` is None, behavior remains
unchanged and only the archival Store is used.

Reference implementation
------------------------
We include a minimal `FileCacheStore` as a convenience for local development
and tests. It writes each entry to `<cache_dir>/<request_hash>.bin` and uses
file mtime to evaluate TTL freshness. You can replace it later with a more
sophisticated backend (e.g., SQLite, Redis) without changing `DataIoSession`.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class CacheStoreProtocol(Protocol):
    """Minimal interface for ephemeral cache stores.

    Implementations MUST be safe for repeated gets/puts on the same key.
    Concurrency, locking, and eviction policies are implementation-defined.
    """

    def get(self, key: str, ttl: float | None = None) -> Optional[bytes]:
        """Return cached bytes if present and within TTL; else None.

        Parameters
        ----------
        key:
            Deterministic cache key (typically Request.hash).
        ttl:
            Optional freshness window in seconds. If None, the implementation
            decides whether to consider the entry fresh (often "always fresh").
        """
        ...

    def put(self, key: str, data: bytes) -> Path:
        """Persist bytes for this key and return a filesystem path to the entry.

        Implementations may return a synthetic path (e.g., for in-memory caches),
        but SHOULD return a stable path where feasible for debugging/inspection.
        """
        ...


class FileCacheStore:
    """A tiny file-backed CacheStore for development and tests.

    Entries are stored as `<cache_dir>/<key>.bin`. TTL is evaluated against
    file mtime. This is intentionally simple; prefer a more robust backend
    for production (e.g., SQLite with metadata, or Redis).

    Parameters
    ----------
    cache_dir:
        Directory where cache artifacts are stored.
    default_ttl:
        Default TTL in seconds when the caller does not pass `ttl` to `get`.
        If None, entries are treated as "always fresh" unless caller supplies ttl.
    """

    def __init__(self, cache_dir: str | Path, default_ttl: float | None = None):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.default_ttl = default_ttl

    def _path(self, key: str) -> Path:
        # Avoid path traversal risk by restricting to a simple filename scheme.
        return self.cache_dir / f"{key}.bin"

    def get(self, key: str, ttl: float | None = None) -> Optional[bytes]:
        path = self._path(key)
        if not path.exists():
            return None

        eff_ttl = self.default_ttl if ttl is None else ttl
        if eff_ttl is not None:
            age = time.time() - path.stat().st_mtime
            if age > eff_ttl:
                return None

        try:
            return path.read_bytes()
        except FileNotFoundError:
            # Race or concurrent eviction; treat as cache miss.
            return None

    def put(self, key: str, data: bytes) -> Path:
        path = self._path(key)
        # Best-effort atomic-ish write: write temp then replace could be added if needed.
        path.write_bytes(data)
        return path


__all__ = ["CacheStoreProtocol", "FileCacheStore"]
