"""
Configuration view helpers for mxm-dataio.

mxm-dataio does not resolve runtime configuration itself. Callers provide an
already-resolved MXMConfig, normally from RuntimeContext.config, and this
module exposes focused read-only views over DataIO-owned configuration.

Typical composition
-------------------
    context = resolve_runtime_context(identity)
    cfg = dataio_view(context.config)

The origin, layering, environment selection, and runtime resolution of the
configuration are outside mxm-dataio's responsibility.
"""

from __future__ import annotations

from mxm.config import MXMConfig, make_view


def dataio_view(cfg: MXMConfig, *, resolve: bool = True) -> MXMConfig:
    """Return the `dataio` subtree (read-only view)."""
    return make_view(cfg, "dataio", resolve=resolve)


__all__ = [
    "dataio_view",
]
