from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, cast

import pytest
from mxm_config import load_config
from omegaconf import DictConfig, OmegaConf
from omegaconf.errors import ReadonlyConfigError

from mxm_dataio.config.config import dataio_http_view, dataio_paths_view, dataio_view


def _load_cfg_from_repo_yaml(
    mxm_config_home: Callable[[str, str], Path],
) -> DictConfig:
    # Mirror mxm_dataio/config/*.yaml into MXM_CONFIG_HOME/mxm-dataio/
    mxm_config_home("mxm-dataio", "mxm_dataio")
    cfg = cast(
        DictConfig,
        load_config(package="mxm-dataio", env="dev", profile="default"),
    )
    assert isinstance(cfg, DictConfig)
    return cfg


def test_dataio_view_mapping_readonly_and_identity(
    mxm_config_home: Callable[[str, str], Path],
) -> None:
    cfg = _load_cfg_from_repo_yaml(mxm_config_home)

    view = cast(DictConfig, dataio_view(cfg))  # resolve=True by default
    assert isinstance(view, DictConfig)

    # The view should be the same underlying subtree (no deep copy).
    assert view is cfg.mxm_dataio  # type: ignore[attr-defined]

    # Basic expected subtrees present
    for key in ("paths", "http", "cache"):
        assert key in view

    # Read-only enforced
    with pytest.raises(ReadonlyConfigError):
        view.paths.root = "/tmp/override"  # type: ignore[attr-defined]


def test_dataio_paths_view_has_core_paths_and_is_readonly(
    mxm_config_home: Callable[[str, str], Path],
) -> None:
    cfg = _load_cfg_from_repo_yaml(mxm_config_home)
    pview = cast(DictConfig, dataio_paths_view(cfg))  # resolved

    # Core keys exist and are non-empty strings
    for key in ("root", "db_path", "responses_dir"):
        assert key in pview
        val = pview[key]
        assert isinstance(val, str) and len(val) > 0

    # Composition includes env/profile suffixes (sanity)
    assert "/dev/dataio/default" in pview.root  # type: ignore[attr-defined]

    # Read-only enforced
    with pytest.raises(ReadonlyConfigError):
        pview.db_path = "/tmp/x.sqlite"  # type: ignore[attr-defined]


def test_dataio_http_view_defaults_resolve_and_copy_safety(
    mxm_config_home: Callable[[str, str], Path],
) -> None:
    cfg = _load_cfg_from_repo_yaml(mxm_config_home)
    hview = cast(DictConfig, dataio_http_view(cfg))  # resolved

    # Expected knobs exist with sensible types
    assert isinstance(hview.timeout_s, (int, float))  # type: ignore[attr-defined]
    assert "headers" in hview and "retries" in hview

    # Converting to a dict allows local mutation without affecting cfg
    params = cast(Dict[str, Any], OmegaConf.to_container(hview, resolve=True))
    old_timeout = params.get("timeout_s")
    params["timeout_s"] = 999  # local change

    hview2 = cast(DictConfig, dataio_http_view(cfg))
    assert hview2.timeout_s == old_timeout  # type: ignore[attr-defined]

    # Read-only enforced on the view itself
    with pytest.raises(ReadonlyConfigError):
        hview.timeout_s = 999  # type: ignore[attr-defined]
