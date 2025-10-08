"""
Configuration loader for mxm-marketdata.

Requires env and profile to be set explicitly.
Defaults to env="dev", profile="default" for local development.
"""

import os

from mxm_config import load_config

ENV = os.getenv("MXM_ENV", "dev")
PROFILE = os.getenv("MXM_PROFILE", "default")

cfg = load_config("mxm-marketdata", env=ENV, profile=PROFILE)

# Optional shortcuts
paths = cfg.paths
params = cfg.parameters
