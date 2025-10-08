"""Global adapter registry for mxm-dataio.

This module maintains a process-local mapping between source identifiers
(e.g. "justetf", "ibkr") and concrete adapter instances implementing one or
more MXMDataIoAdapter capabilities.

The registry enables dynamic resolution of adapters at runtime so that
high-level components (such as IngestSession) can remain protocol-agnostic.

Usage
-----
    from mxm_dataio.registry import register, resolve_adapter, list_registered
    from mxm_dataio.adapters import Fetcher

    class DummyFetcher:
        source = "dummy"
        def fetch(self, request): return b"ok"
        def describe(self): return "Dummy fetcher"
        def close(self): pass

    register("dummy", DummyFetcher())
    adapter = resolve_adapter("dummy")
    assert isinstance(adapter, Fetcher)
"""

from __future__ import annotations

from mxm_dataio.adapters import MXMDataIoAdapter

# --------------------------------------------------------------------------- #
# Internal registry store
# --------------------------------------------------------------------------- #

_REGISTRY: dict[str, MXMDataIoAdapter] = {}


# --------------------------------------------------------------------------- #
# Registration API
# --------------------------------------------------------------------------- #


def register(name: str, adapter: MXMDataIoAdapter) -> None:
    """Register an adapter instance under a unique source name.

    Parameters
    ----------
    name:
        Canonical identifier for the external system (e.g., "justetf").
    adapter:
        Instance implementing one or more MXMDataIoAdapter capabilities.

    Raises
    ------
    ValueError
        If an adapter with the same name is already registered.
    """
    if name in _REGISTRY:
        raise ValueError(f"Adapter '{name}' is already registered.")
    _REGISTRY[name] = adapter


def unregister(name: str) -> None:
    """Remove a previously registered adapter."""
    _REGISTRY.pop(name, None)


def resolve_adapter(name: str) -> MXMDataIoAdapter:
    """Return the adapter registered for the given source name.

    Parameters
    ----------
    name:
        Source identifier to look up.

    Returns
    -------
    MXMDataIoAdapter
        The registered adapter instance.

    Raises
    ------
    KeyError
        If no adapter has been registered under the given name.
    """
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"No adapter registered for source '{name}'.") from exc


def list_registered() -> list[str]:
    """Return a sorted list of registered adapter names."""
    return sorted(_REGISTRY.keys())


def clear_registry() -> None:
    """Clear all registered adapters (useful for testing)."""
    _REGISTRY.clear()


# --------------------------------------------------------------------------- #
# Introspection / Debug helpers
# --------------------------------------------------------------------------- #


def describe_registry() -> str:
    """Return a formatted string listing all registered adapters."""
    if not _REGISTRY:
        return "(no adapters registered)"

    lines = ["Registered adapters:"]
    for name, adapter in sorted(_REGISTRY.items()):
        desc = (
            adapter.describe() if hasattr(adapter, "describe") else "(no description)"
        )
        lines.append(f"  • {name:15s} → {desc}")
    return "\n".join(lines)
