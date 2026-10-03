import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

# The bundled configs are found relative to the package, never to the shell's
# working directory: `pytest tests/` from the repository root used to fail five
# venue tests on a missing configs/ directory, which looked like a code fault.
CONFIGS = PACKAGE_ROOT / "configs"


def config_path(name: str) -> str:
    """One bundled config's path."""
    return str(CONFIGS / name)


def config_paths() -> list[str]:
    """Every bundled config, sorted, as `glob` would have them."""
    return sorted(str(p) for p in CONFIGS.glob("*.json"))

from btc5m.config import config_from_dict
from btc5m.data import synthetic
from btc5m.features import build_features


@pytest.fixture(scope="session")
def cfg():
    return config_from_dict()


@pytest.fixture(scope="session")
def series():
    return synthetic(4000, seed=101)


@pytest.fixture(scope="session")
def features(series, cfg):
    return build_features(series, cfg)
