from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from mxm.config import DefaultsMode, install_config, load_config
from omegaconf import DictConfig
from omegaconf.errors import ReadonlyConfigError

from mxm.dataio.config.config import dataio_view

APP_ID = "dataio"
SHIPPED_PACKAGE = "mxm.dataio"


def _install_dataio_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Install the shipped dataio config into a temporary MXM_CONFIG_HOME.
    """
    monkeypatch.setenv("MXM_CONFIG_HOME", str(tmp_path))

    install_config(
        app_id=APP_ID,
        mode=DefaultsMode.shipped,
        shipped_package=SHIPPED_PACKAGE,
    )


def _load_cfg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    env: str = "dev",
    profile: str = "default",
) -> DictConfig:
    """
    Install shipped config into a temp MXM_CONFIG_HOME and load a merged config.
    """
    _install_dataio_config(tmp_path, monkeypatch)

    cfg = cast(
        DictConfig,
        load_config(
            package=APP_ID,
            env=env,
            profile=profile,
        ),
    )
    assert isinstance(cfg, DictConfig)
    return cfg


def test_dataio_view_mapping_readonly_and_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _load_cfg(tmp_path, monkeypatch=monkeypatch)

    view = cast(DictConfig, dataio_view(cfg))
    assert isinstance(view, DictConfig)

    # The view should be the same underlying subtree (no deep copy).
    assert view is cfg.dataio  # type: ignore[attr-defined]

    # Basic expected subtrees present
    assert "paths" in view
    # cache may or may not be present depending on your YAMLs
    assert "cache" in view or not hasattr(view, "cache")

    # Read-only enforced
    with pytest.raises(ReadonlyConfigError):
        view.paths.root = "/tmp/override"  # type: ignore[attr-defined]


def test_dataio_paths_have_core_fields_and_are_readonly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _load_cfg(tmp_path, monkeypatch=monkeypatch, env="dev", profile="default")
    dview = cast(DictConfig, dataio_view(cfg))

    # Core keys exist and are non-empty strings
    assert isinstance(dview.paths.root, str) and dview.paths.root  # type: ignore[attr-defined]
    assert isinstance(dview.paths.db_path, str) and dview.paths.db_path  # type: ignore[attr-defined]
    assert isinstance(dview.paths.responses_dir, str) and dview.paths.responses_dir  # type: ignore[attr-defined]

    # Composition includes env/profile suffixes (sanity checks – adjust as needed
    # to match your actual YAML structure).
    assert "dev" in dview.paths.root  # type: ignore[attr-defined]
    assert "dataio" in dview.paths.root  # type: ignore[attr-defined]
    assert dview.paths.db_path.endswith("dataio.sqlite")  # type: ignore[attr-defined]
    assert dview.paths.responses_dir.endswith("responses")  # type: ignore[attr-defined]

    # Read-only enforced on the paths view
    with pytest.raises(ReadonlyConfigError):
        dview.paths.db_path = "/tmp/x.sqlite"  # type: ignore[attr-defined]


def test_profile_overrides_paths_when_research_profile_selected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _load_cfg(
        tmp_path,
        monkeypatch=monkeypatch,
        env="dev",
        profile="research",
    )
    dview = cast(DictConfig, dataio_view(cfg))

    # Adjust suffixes to whatever you actually use in profile.yaml
    assert dview.paths.db_path.endswith("dataio_research.sqlite")  # type: ignore[attr-defined]
    assert dview.paths.responses_dir.endswith("responses_research")  # type: ignore[attr-defined]


def test_env_overrides_cache_use_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_dev = _load_cfg(
        tmp_path,
        monkeypatch=monkeypatch,
        env="dev",
        profile="default",
    )
    cfg_prod = _load_cfg(
        tmp_path,
        monkeypatch=monkeypatch,
        env="prod",
        profile="default",
    )

    d_dev = cast(DictConfig, dataio_view(cfg_dev))
    d_prod = cast(DictConfig, dataio_view(cfg_prod))

    # From environment.yaml: dev → use_cache = true, prod → use_cache = false
    assert bool(d_dev.cache.use_cache) is True  # type: ignore[attr-defined]
    assert bool(d_prod.cache.use_cache) is False  # type: ignore[attr-defined]
