import json
import time

import duckdb
import pytest

from generator.generate import build


@pytest.fixture(scope="session")
def built(tmp_path_factory):
    """Full-size dataset generated once into a temp folder (the app's own data/ is never touched)."""
    d = tmp_path_factory.mktemp("data")
    db_path, truth_path = d / "test.duckdb", d / "truth.json"
    t0 = time.time()
    build(db_path=db_path, truth_path=truth_path)
    seconds = time.time() - t0
    con = duckdb.connect(str(db_path))
    with open(truth_path) as f:
        truth = json.load(f)
    yield {"con": con, "truth": truth, "seconds": seconds}
    con.close()


@pytest.fixture(scope="session")
def con(built):
    return built["con"]


@pytest.fixture(scope="session")
def truth(built):
    return built["truth"]
