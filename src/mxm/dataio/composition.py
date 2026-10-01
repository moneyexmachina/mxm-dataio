"""Composition root for the MXM DataIO capability."""

from __future__ import annotations

from collections.abc import Callable

from mxm.dataio.payloads import PayloadStore
from mxm.dataio.runtime import DataIO
from mxm.dataio.sql.postgres import PostgresDatabase
from mxm.types.timestamps import TSNSScalar

__all__ = [
    "compose_dataio",
]


def compose_dataio(
    *,
    database: PostgresDatabase,
    payload_store: PayloadStore,
    timestamp_source: Callable[[], TSNSScalar],
) -> DataIO:
    """Compose DataIO from concrete application-provided dependencies."""

    return DataIO(
        database=database,
        payload_store=payload_store,
        timestamp_source=timestamp_source,
    )
