import os
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_trip_tracking.db")

from schemas.schemas import (
    TripLocationResponse,
    TripPickupStartRequest,
)
from controllers.trips_controller import _should_store_trip_location


def test_pickup_estimate_contract_accepts_map_values():
    payload = TripPickupStartRequest(
        pickup_distance_km=14.7,
        pickup_estimated_time="22 min",
    )

    assert payload.pickup_distance_km == 14.7
    assert payload.pickup_estimated_time == "22 min"


def test_location_contract_exposes_trip_phase():
    payload = TripLocationResponse.model_validate(
        {
            "id": 1,
            "trip_id": 7,
            "latitude": -25.9653,
            "longitude": 32.5892,
            "speed": 34,
            "phase": "indo_carregar",
            "created_at": datetime.now(timezone.utc),
        }
    )

    assert payload.phase == "indo_carregar"


def test_first_location_of_new_phase_is_always_stored():
    previous = type(
        "Location",
        (),
        {
            "phase": "indo_carregar",
            "latitude": -25.9653,
            "longitude": 32.5892,
            "created_at": datetime.now(timezone.utc),
        },
    )()

    assert _should_store_trip_location(
        previous,
        previous.latitude,
        previous.longitude,
        "viagem_iniciada",
    ) is True
