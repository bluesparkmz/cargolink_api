import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_driver_active_trip.db")

from constants import (
    TRIP_GROUP_COMPLETED,
    TRIP_GROUP_IN_PROGRESS,
    TRIP_GROUP_STATUSES,
    TRIP_STATUS_STARTED,
    TRIP_STATUS_WAITING_CLIENT,
)
from controllers.trips_controller import _ensure_driver_has_no_other_active_trip


class FakeTripQuery:
    def __init__(self, result):
        self.result = result

    def filter(self, *args):
        return self

    def order_by(self, *args):
        return self

    def first(self):
        return self.result


class FakeDb:
    def __init__(self, result):
        self.result = result

    def query(self, model):
        return FakeTripQuery(self.result)


def test_waiting_client_is_completed_for_driver_and_does_not_block_work():
    assert TRIP_STATUS_WAITING_CLIENT in TRIP_GROUP_STATUSES[TRIP_GROUP_COMPLETED]
    assert TRIP_STATUS_WAITING_CLIENT not in TRIP_GROUP_STATUSES[TRIP_GROUP_IN_PROGRESS]


def test_other_active_trip_blocks_new_start():
    driver = SimpleNamespace(id=5)
    active_trip = SimpleNamespace(id=44, status=TRIP_STATUS_STARTED)

    with pytest.raises(HTTPException) as exc:
        _ensure_driver_has_no_other_active_trip(FakeDb(active_trip), driver, trip_id=50)

    assert exc.value.status_code == 409
    assert "#44" in exc.value.detail


def test_driver_without_active_trip_can_start():
    _ensure_driver_has_no_other_active_trip(FakeDb(None), SimpleNamespace(id=5), trip_id=50)
