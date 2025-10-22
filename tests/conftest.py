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
    package_name: str,
    package_module: str,
    package_config_rel: str = "config",
) -> Path:
    """
    Create MXM_CONFIG_HOME/<package_name>/ that mirrors the package's config directory.
    Prefer a symlink; fall back to copying if symlink fails (e.g., Windows without perms).
    """
    target_dir = tmp_root / package_name
    target_dir.mkdir(parents=True, exist_ok=True)

    # Resolve the package's on-disk config directory:
    # e.g., importlib.resources.files("mxm_dataio") / "config"
    src_dir = pkg_files(package_module) / package_config_rel
    src_path = Path(str(src_dir))  # convert Traversable to a real filesystem path

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
