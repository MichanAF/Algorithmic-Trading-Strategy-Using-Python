import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
