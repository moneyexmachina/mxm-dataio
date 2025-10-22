"""Tests for Store sidecar metadata read/write."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mxm_config import MXMConfig, make_subconfig

from mxm_dataio.store import Store

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def store_cfg_view(tmp_path: Path) -> MXMConfig:
    """
    Provide a temporary **dataio view** with only the paths Store needs.
    """
    return make_subconfig(
        {
            "paths": {
                "root": str(tmp_path),
                "db_path": str(tmp_path / "dataio.sqlite"),
                "responses_dir": str(tmp_path / "responses"),
            }
        }
    )


@pytest.fixture()
def store(store_cfg_view: MXMConfig) -> Store:
    return Store.get_instance(store_cfg_view)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_write_and_read_metadata_roundtrip(store: Store) -> None:
    data = b"payload-for-metadata"
    path = store.write_payload(data)
    checksum = path.stem

    meta_in = {"content_type": "application/json", "status": 200, "elapsed_ms": 12}
    meta_path = store.write_metadata(checksum, meta_in)
    assert meta_path.exists()

    meta_out = store.read_metadata(checksum)
    assert meta_out == meta_in


def test_write_metadata_is_idempotent_no_overwrite(store: Store) -> None:
    data = b"x"
    checksum = store.write_payload(data).stem

    # First write
    first_meta = {"a": 1}
    meta_path = store.write_metadata(checksum, first_meta)
    text1 = meta_path.read_text(encoding="utf-8")

    # Second write with different content should not overwrite existing file
    second_meta = {"a": 2, "b": 3}
    meta_path2 = store.write_metadata(checksum, second_meta)
    assert meta_path2 == meta_path
    text2 = meta_path.read_text(encoding="utf-8")

    assert text2 == text1
    assert json.loads(text2) == first_meta


def test_read_metadata_missing_raises(store: Store) -> None:
    missing_checksum = "0" * 64
    with pytest.raises(FileNotFoundError):
        _ = store.read_metadata(missing_checksum)


def test_metadata_filename_matches_checksum(store: Store) -> None:
    data = b"abc"
    checksum = store.write_payload(data).stem

    meta = {"ok": True}
    meta_path = store.write_metadata(checksum, meta)

    expected = store.responses_dir / f"{checksum}.meta.json"
    assert meta_path == expected
    assert meta_path.exists()


def test_metadata_unicode_and_deterministic_json(store: Store) -> None:
    data = b"u"
    checksum = store.write_payload(data).stem

    # Note: keys will be sorted and JSON minified
    meta = {"zeta": "Ωmega", "alpha": "äöü"}
    meta_path = store.write_metadata(checksum, meta)

    # Verify deterministic serialization (sorted keys, minified spacing)
    text = meta_path.read_text(encoding="utf-8")
    assert text == '{"alpha":"äöü","zeta":"Ωmega"}'

    # Roundtrip still matches the original dict values
    loaded = json.loads(text)
    assert loaded == meta
