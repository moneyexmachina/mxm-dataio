"""Tests for the public mxm.dataio package API."""

import mxm.dataio as dataio
from mxm.dataio import DataIO, compose_dataio
from mxm.dataio.composition import compose_dataio as composition_compose_dataio
from mxm.dataio.runtime import DataIO as RuntimeDataIO


def test_public_imports_resolve_to_canonical_objects() -> None:
    """Top-level imports expose the canonical DataIO objects."""

    assert DataIO is RuntimeDataIO
    assert compose_dataio is composition_compose_dataio


def test_public_api_is_explicit() -> None:
    """The package advertises only its composed capability boundary."""

    assert set(dataio.__all__) == {
        "DataIO",
        "compose_dataio",
    }
