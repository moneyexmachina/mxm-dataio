"""Unit tests for the DataIO composition boundary."""

from __future__ import annotations

from mxm.dataio.composition import compose_dataio
from mxm.dataio.payloads import PayloadStore
from mxm.dataio.runtime import DataIO
from mxm.dataio.sql.postgres import PostgresDatabase
from mxm.types.timestamps import TSNSScalar, ts_ns_from_str


class InMemoryPayloadStore:
    """Minimal payload store used to verify dependency composition."""

    def __init__(self) -> None:
        self._payloads: dict[str, bytes] = {}

    def put(
        self,
        checksum: str,
        data: bytes,
    ) -> None:
        self._payloads[checksum] = data

    def get(
        self,
        checksum: str,
    ) -> bytes:
        return self._payloads[checksum]


def test_compose_dataio_assembles_supplied_dependencies() -> None:
    """Composition retains the exact application-provided dependencies."""

    database = PostgresDatabase(
        "postgresql://mxm@localhost/mxm_dev",
    )
    payload_store: PayloadStore = InMemoryPayloadStore()

    def timestamp_source() -> TSNSScalar:
        return ts_ns_from_str("2026-10-01T10:00:00.000000Z")

    dataio = compose_dataio(
        database=database,
        payload_store=payload_store,
        timestamp_source=timestamp_source,
    )

    assert isinstance(dataio, DataIO)
    assert dataio._database is database
    assert dataio._payload_store is payload_store
    assert dataio._timestamp_source is timestamp_source
