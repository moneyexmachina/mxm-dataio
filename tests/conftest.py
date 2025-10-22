from __future__ import annotations

import os
import shutil
from importlib.resources import files as pkg_files  # Python 3.11+
from pathlib import Path
from typing import Callable

import pytest
from _pytest.monkeypatch import MonkeyPatch  # type: ignore[import-not-found]


def _mirror_pkg_config(
    tmp_root: Path,
    package_name: str,  # e.g. "mxm-dataio"  (hyphen)
    package_module: str,  # e.g. "mxm_dataio"  (underscore)
    package_config_rel: str = "config",
) -> Path:
    """
    Create MXM_CONFIG_HOME/<package_name>/ by mirroring YAMLs from this repo.

    Preferred source:
        <repo_root>/<package_module>/<package_config_rel>/
    Fallback:
        importlib.resources.files(<package_module>)/<package_config_rel>/

    Also ensures MXM_CONFIG_HOME/machine.yaml exists for ${paths.data_root_base}.
    """
    target_dir = tmp_root / package_name
    target_dir.mkdir(parents=True, exist_ok=True)

    # Resolve repo root (tests/conftest.py -> tests/ -> repo root)
    repo_root = Path(__file__).resolve().parents[1]

    # Prefer in-repo configs under the *module* directory
    repo_cfg = repo_root / package_module / package_config_rel  # <-- IMPORTANT
    if repo_cfg.exists() and any(
        p.suffix.lower() == ".yaml" for p in repo_cfg.iterdir()
    ):
        src_path = repo_cfg
    else:
        # Fallback to installed package resources
        src_path = Path(str(pkg_files(package_module) / package_config_rel))

    # Mirror *.yaml into MXM_CONFIG_HOME/<package_name>/
    for p in src_path.iterdir():
        if p.suffix.lower() != ".yaml":
            continue
        dst = target_dir / p.name
        try:
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            os.symlink(p, dst)
        except (OSError, NotImplementedError):
            shutil.copy2(p, dst)

    # Ensure MXM_CONFIG_HOME/machine.yaml exists for ${paths.data_root_base}
    machine_yaml = tmp_root / "machine.yaml"
    if not machine_yaml.exists():
        machine_yaml.write_text(
            "paths:\n  data_root_base: /tmp/mxm\n", encoding="utf-8"
        )

    return target_dir


@pytest.fixture
def mxm_config_home(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> Callable[[str, str], Path]:
    """
    Provide a function to map a package's in-repo config dir into MXM_CONFIG_HOME.

    Usage in tests:
        home_for = mxm_config_home
        home_for("mxm-dataio", "mxm_dataio")
        # Now load_config(package="mxm-dataio", ...) reads from repo YAMLs (no install).
    """

    def _make(package_name: str, package_module: str) -> Path:
        home = tmp_path
        _mirror_pkg_config(home, package_name, package_module)
        monkeypatch.setenv("MXM_CONFIG_HOME", str(home))
        return home

    return _make
