import pytest

from backend.ingest.loader import build_dataset
from backend.ranker import analyze, build_report
from backend.synthetic.generate import generate


@pytest.fixture(scope="session")
def restaurant():
    return generate(seed=42)


@pytest.fixture(scope="session")
def dataset(restaurant):
    return build_dataset(restaurant.raw_files(), restaurant.profile)


@pytest.fixture(scope="session")
def analysis(dataset):
    return analyze(dataset)


@pytest.fixture(scope="session")
def report(analysis):
    return build_report(analysis)
