from __future__ import annotations

from pathlib import Path
from typing import Callable, cast

import pytest
from mxm_config import load_config
from omegaconf import DictConfig
from omegaconf.errors import ReadonlyConfigError

from mxm_dataio.config.config import dataio_view


def _load_cfg_from_repo_yaml(
    mxm_config_home: Callable[[str, str], Path],
    *,
    env: str = "dev",
    profile: str = "default",
) -> DictConfig:
    # Mirror mxm_dataio/config/*.yaml into MXM_CONFIG_HOME/mxm-dataio/
    mxm_config_home("mxm-dataio", "mxm_dataio")
    cfg = cast(DictConfig, load_config(package="mxm-dataio", env=env, profile=profile))
    assert isinstance(cfg, DictConfig)
    return cfg


def test_dataio_view_mapping_readonly_and_identity(
    mxm_config_home: Callable[[str, str], Path],
) -> None:
    cfg = _load_cfg_from_repo_yaml(mxm_config_home)

    view = cast(DictConfig, dataio_view(cfg))  # resolve=True by default
    assert isinstance(view, DictConfig)

    # The view should be the same underlying subtree (no deep copy).
    assert view is cfg.dataio  # type: ignore[attr-defined]

    # Basic expected subtrees present
    assert "paths" in view
    # cache may be present if you added it; if not, this line can be relaxed
    assert "cache" in view or not hasattr(view, "cache")

    # Read-only enforced
    with pytest.raises(ReadonlyConfigError):
        view.paths.root = "/tmp/override"  # type: ignore[attr-defined]


def test_dataio_paths_have_core_fields_and_are_readonly(
    mxm_config_home: Callable[[str, str], Path],
) -> None:
    cfg = _load_cfg_from_repo_yaml(mxm_config_home, env="dev", profile="default")
    dview = cast(DictConfig, dataio_view(cfg))  # resolved

    # Core keys exist and are non-empty strings
    assert isinstance(dview.paths.root, str) and dview.paths.root  # type: ignore[attr-defined]
    assert isinstance(dview.paths.db_path, str) and dview.paths.db_path  # type: ignore[attr-defined]
    assert isinstance(dview.paths.responses_dir, str) and dview.paths.responses_dir  # type: ignore[attr-defined]

    # Composition includes env/profile suffixes (sanity)
    assert "/dev/dataio/default" in dview.paths.root  # type: ignore[attr-defined]
    assert dview.paths.db_path.endswith("/dataio.sqlite")  # type: ignore[attr-defined]
    assert dview.paths.responses_dir.endswith("/responses")  # type: ignore[attr-defined]

    # Read-only enforced on the paths view
    with pytest.raises(ReadonlyConfigError):
        dview.paths.db_path = "/tmp/x.sqlite"  # type: ignore[attr-defined]


def test_profile_overrides_paths_when_research_profile_selected(
    mxm_config_home: Callable[[str, str], Path],
) -> None:
    # Only relevant if you used Option B in profile.yaml
    cfg = _load_cfg_from_repo_yaml(mxm_config_home, env="dev", profile="research")
    dview = cast(DictConfig, dataio_view(cfg))

    assert dview.paths.db_path.endswith("dataio_research.sqlite")  # type: ignore[attr-defined]
    assert dview.paths.responses_dir.endswith("responses_research")  # type: ignore[attr-defined]


def test_env_overrides_cache_use_cache(
    mxm_config_home: Callable[[str, str], Path],
) -> None:
    cfg_dev = _load_cfg_from_repo_yaml(mxm_config_home, env="dev", profile="default")
    cfg_prod = _load_cfg_from_repo_yaml(mxm_config_home, env="prod", profile="default")

    d_dev = cast(DictConfig, dataio_view(cfg_dev))
    d_prod = cast(DictConfig, dataio_view(cfg_prod))

    assert bool(d_dev.cache.use_cache) is True  # type: ignore[attr-defined]
    assert bool(d_prod.cache.use_cache) is False  # type: ignore[attr-defined]
