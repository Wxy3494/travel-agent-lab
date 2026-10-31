from datetime import date, timedelta

import pytest

from app.config import Settings
from app.schemas import Constraints
from app.service import TravelService
from app.storage import Repository


@pytest.fixture
def constraints():
    return Constraints(origin="广州", cities=["广州"], start_date=date.today() + timedelta(days=2),
                       days=2, people=2, budget=6000)


@pytest.fixture
def service(tmp_path):
    settings = Settings(db_path=str(tmp_path / "trips.sqlite3"))
    return TravelService(settings, Repository(settings.db_path))
