import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def root():
    return ROOT


@pytest.fixture(scope="session")
def recorded():
    return json.loads((ROOT / "fixtures/model_outputs/extraction_3540.json").read_text())


@pytest.fixture(scope="session")
def county():
    return json.loads((ROOT / "fixtures/county_facts_3540.json").read_text())


@pytest.fixture(scope="session")
def cited_lines():
    return json.loads((ROOT / "fixtures/cited_lines.json").read_text())


def have_postgres() -> bool:
    return shutil.which("initdb") is not None and shutil.which("pg_ctl") is not None
