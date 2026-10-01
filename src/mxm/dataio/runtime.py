"""Runtime façade for the MXM DataIO capability."""

from __future__ import annotations

from collections.abc import Callable

from mxm.dataio.payloads import PayloadStore
from mxm.dataio.sql.postgres import PostgresDatabase
from mxm.types.timestamps import TSNSScalar

__all__ = [
    "DataIO",
]


class DataIO:
    """Composed DataIO capability.

    This initial façade owns the concrete dependencies required by future
    DataIO operations. Operational methods are added in later implementation
    slices.
    """

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
