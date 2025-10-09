"""Extended tests for mxm_dataio.store.

These tests validate persistence, concurrency, and robustness aspects
of the Store class beyond basic functional coverage.
"""

from __future__ import annotations

import concurrent.futures
import itertools
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from mxm_dataio.models import Session
from mxm_dataio.store import Store

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def store_cfg(tmp_path: Path) -> dict[str, Any]:
    """Provide a temporary configuration dictionary for tests."""
    return {
        "paths": {
            "data_root": str(tmp_path),
            "db_path": str(tmp_path / "test.sqlite"),
            "responses_dir": str(tmp_path / "responses"),
        }
    }


@pytest.fixture()
def store(store_cfg: dict[str, Any]) -> Store:
    """Return a new Store instance for the temporary config."""
    return Store(store_cfg)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_store_persistence_across_instances(store_cfg: dict[str, Any]) -> None:
    """Data inserted by one Store instance should be visible to another."""
    store1 = Store.get_instance(store_cfg)
    session = Session(source="persist")
    store1.insert_session(session)

    # New instance reading the same DB
    store2 = Store(store_cfg)
    with store2.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM sessions WHERE id=?", (session.id,)
        ).fetchall()
    assert len(rows) == 1, "Session should persist across instances"


def test_singleton_thread_safety(store_cfg: dict[str, Any]) -> None:
    """get_instance should return the same object even under concurrency."""

    def create_store(_: object) -> Store:
        return Store.get_instance(store_cfg)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(create_store, itertools.repeat(None, 5)))

    first = results[0]
    assert all(s is first for s in results), "All threads should share one instance"


def test_datetime_roundtrip(store: Store) -> None:
    """Timestamps should be stored and retrievable as ISO UTC datetimes."""
    s = Session(source="timecheck")
    store.insert_session(s)

    with store.connect() as conn:
        row = conn.execute(
            "SELECT started_at FROM sessions WHERE id=?", (s.id,)
        ).fetchone()

    assert row is not None
    dt = datetime.fromisoformat(row[0])
    assert dt.tzinfo is not None, "Timestamp must preserve timezone info"


def test_missing_payload_raises(store: Store) -> None:
    """Attempting to read a non-existent payload should raise FileNotFoundError."""
    fake_checksum = "00" * 32
    with pytest.raises(FileNotFoundError):
        store.read_payload(fake_checksum)


def test_large_payload_roundtrip(store: Store) -> None:
    """Large binary payloads should write and read back correctly."""
    data = os.urandom(5 * 1024 * 1024)  # 5 MB
    path = store.write_payload(data)
    assert path.exists()
    out = store.read_payload(path.stem)
    assert out == data
    assert len(out) == len(data)


def test_indexes_created(store: Store) -> None:
    with store.connect() as conn:
        names = {row[1] for row in conn.execute("PRAGMA index_list('requests')")}
        assert "idx_requests_hash" in names
        assert "idx_requests_session" in names

        names = {row[1] for row in conn.execute("PRAGMA index_list('responses')")}
        assert "idx_responses_request" in names
        assert "idx_responses_created" in names
        assert "idx_responses_checksum" in names  # if you kept the optional one
