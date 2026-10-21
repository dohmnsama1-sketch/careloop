import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from careloop.clock import Clock
from careloop.core import CareLoop
from careloop.seed import TZ, build
from careloop.store import Store

DEMO_NOW = "2026-10-23T08:05"


@pytest.fixture
def data_file(tmp_path):
    p = tmp_path / "careloop.json"
    p.write_text(json.dumps(build(datetime.fromisoformat(DEMO_NOW).replace(tzinfo=ZoneInfo(TZ)))))
    return p


@pytest.fixture
def clock():
    return Clock(TZ, frozen=DEMO_NOW)


@pytest.fixture
def care(data_file, clock):
    return CareLoop(Store(data_file), clock)
