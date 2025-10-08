"""Unit tests for mxm_dataio.registry.

Covers registration, resolution, unregistration, clearing, and introspection
of adapter instances. Uses a lightweight dummy adapter that implements the
MXMDataIoAdapter protocol minimally.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from mxm_dataio import registry
from mxm_dataio.adapters import MXMDataIoAdapter

# --------------------------------------------------------------------------- #
# Fixtures and Dummy Adapters
# --------------------------------------------------------------------------- #


class DummyAdapter:
    """Simple adapter implementing the MXMDataIoAdapter protocol."""

    source = "dummy"

    def describe(self) -> str:
        return "Dummy adapter"

    def close(self) -> None:
        pass


class AnotherAdapter:
    source = "another"

    def describe(self) -> str:
        return "Another adapter"

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    """Ensure registry is cleared before and after each test."""
    registry.clear_registry()
    yield
    registry.clear_registry()


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_register_and_resolve_success() -> None:
    """Adapters can be registered and resolved successfully."""
    adapter = DummyAdapter()
    registry.register("dummy", adapter)
    resolved = registry.resolve_adapter("dummy")

    assert resolved is adapter
    assert isinstance(resolved, MXMDataIoAdapter)
    assert "dummy" in registry.list_registered()


def test_register_duplicate_raises_value_error() -> None:
    """Registering the same adapter name twice raises ValueError."""
    adapter = DummyAdapter()
    registry.register("dummy", adapter)
    with pytest.raises(ValueError):
        registry.register("dummy", adapter)


def test_resolve_adapter_success() -> None:
    """resolve_adapter returns the correct adapter instance."""
    adapter = DummyAdapter()
    registry.register("dummy", adapter)
    resolved = registry.resolve_adapter("dummy")
    assert resolved.describe() == "Dummy adapter"


def test_resolve_adapter_missing_raises_key_error() -> None:
    """resolve_adapter raises KeyError for unknown adapter names."""
    with pytest.raises(KeyError):
        registry.resolve_adapter("unknown")


def test_unregister_removes_adapter() -> None:
    """unregister removes an adapter from the registry."""
    adapter = DummyAdapter()
    registry.register("dummy", adapter)
    registry.unregister("dummy")
    assert "dummy" not in registry.list_registered()

    with pytest.raises(KeyError):
        registry.resolve_adapter("dummy")


def test_clear_registry_empties_all_entries() -> None:
    """clear_registry removes all registered adapters."""
    registry.register("dummy", DummyAdapter())
    registry.register("another", AnotherAdapter())
    assert len(registry.list_registered()) == 2

    registry.clear_registry()
    assert registry.list_registered() == []


def test_list_registered_returns_sorted_names() -> None:
    """list_registered returns adapter names in sorted order."""
    registry.register("zeta", DummyAdapter())
    registry.register("alpha", AnotherAdapter())

    names = registry.list_registered()
    assert names == ["alpha", "zeta"]


def test_describe_registry_output_contains_registered_names() -> None:
    """describe_registry produces human-readable listing of adapters."""
    registry.register("dummy", DummyAdapter())
    output = registry.describe_registry()

    assert "dummy" in output
    assert "Dummy adapter" in output

    registry.clear_registry()
    assert "(no adapters registered)" in registry.describe_registry()
