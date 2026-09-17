import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mmhedge.config import config_from_dict
from mmhedge.data import synthetic
from mmhedge.sizing import allocate


@pytest.fixture(scope="session")
def cfg():
    return config_from_dict({"capital_usd": 25_000.0})


@pytest.fixture(scope="session")
def alloc(cfg):
    return allocate(cfg)


@pytest.fixture(scope="session")
def series():
    return synthetic(24 * 200, seed=7)
