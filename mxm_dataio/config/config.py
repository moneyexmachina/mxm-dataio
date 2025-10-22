"""
Config access helpers for mxm-dataio.

This module provides **focused, read-only views** over the package’s config
subtrees, built on top of `mxm_config.make_view`. It intentionally performs
no I/O at import time—callers should obtain a global `MXMConfig` via
`mxm_config.load_config(...)` and then pass that object to the helpers here.

Typical usage
-------------
    from mxm_config import load_config
    from mxm_dataio.config import dataio_view, dataio_paths_view, dataio_http_view

    cfg = load_config(package="mxm-dataio", env="dev", profile="default")
    dio = dataio_view(cfg)               # mxm_dataio subtree (read-only)
    db_path = dataio_paths_view(cfg).db_path
    timeout = dataio_http_view(cfg).timeout_s

Notes
-----
- These helpers **require** the new config shape with a package subtree:
      mxm_dataio: { paths: {...}, http: {...}, ... }
- Views are **read-only** and may be `resolve=True` for convenience at the
  boundary. If you need to mutate derived parameters, convert to a plain dict:
      from omegaconf import OmegaConf
      params = OmegaConf.to_container(dataio_http_view(cfg), resolve=True)
"""

from __future__ import annotations

from mxm_config import MXMConfig, make_view


def dataio_view(cfg: MXMConfig, *, resolve: bool = True) -> MXMConfig:
    """Return the `dataio` subtree (read-only view)."""
    return make_view(cfg, "dataio", resolve=resolve)


__all__ = [
    "dataio_view",
]
