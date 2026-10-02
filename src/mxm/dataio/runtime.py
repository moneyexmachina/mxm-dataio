"""Runtime façade for the MXM DataIO capability."""

from __future__ import annotations

from collections.abc import Callable

import mxm.dataio.resolution as resolution
from mxm.dataio.adapters import Fetcher
from mxm.dataio.models import CacheMode
from mxm.dataio.payloads import PayloadStore
from mxm.dataio.sql.postgres import PostgresDatabase
from mxm.types import JSONObj
from mxm.types.timestamps import TSNSScalar

__all__ = [
    "DataIO",
]


class DataIO:
    """Composed DataIO capability."""

    def __init__(
        self,
        *,
        database: PostgresDatabase,
        payload_store: PayloadStore,
        timestamp_source: Callable[[], TSNSScalar],
    ) -> None:
        """Initialise one composed DataIO capability."""

        self._database = database
        self._payload_store = payload_store
        self._timestamp_source = timestamp_source

    def resolve(
        self,
        *,
        source: str,
        kind: str,
        params: JSONObj | None,
        adapter: Fetcher,
        cache_mode: CacheMode = CacheMode.DEFAULT,
        ttl_seconds: float | None = None,
        as_of_bucket: str | None = None,
        cache_tag: str | None = None,
    ) -> resolution.ResolveResult:
        """Resolve one external data request."""

        return resolution.resolve(
            database=self._database,
            payload_store=self._payload_store,
            timestamp_source=self._timestamp_source,
            source=source,
            kind=kind,
            params=params,
            adapter=adapter,
            cache_mode=cache_mode,
            ttl_seconds=ttl_seconds,
            as_of_bucket=as_of_bucket,
            cache_tag=cache_tag,
        )
