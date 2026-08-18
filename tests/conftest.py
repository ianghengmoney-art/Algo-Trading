import pytest

from gcfp.config import DEFAULT_PARAMS
from gcfp.data.fixtures import ANCHOR_DATE, FixtureAdapter
from gcfp.data.registry import capability_gate

SUBJECTS = [
    "MATURE", "FASTPROF", "BURNER", "BANKCO", "REITCO", "INSURCO",
    "ADRCO", "RERATED", "BADQUAL", "STALECO", "FLAGGED",
]


@pytest.fixture
def params():
    return DEFAULT_PARAMS


@pytest.fixture
def adapter():
    return FixtureAdapter()


@pytest.fixture
def as_of():
    return ANCHOR_DATE


@pytest.fixture
def gate(adapter):
    return capability_gate(adapter, symbols=SUBJECTS)
