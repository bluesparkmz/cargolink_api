import os
from datetime import date, datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_driver_trip_detail.db")

from controllers.driver_trips_controller import build_trip_detail
from schemas.schemas import TripDriverDetailResponse


def test_driver_trip_detail_matches_response_contract():
    now = datetime.now(timezone.utc)
    user = SimpleNamespace(name="Motorista Teste", phone="840000000", profile_photo=None)
    client_user = SimpleNamespace(name="Cliente Teste", phone="850000000")
    client = SimpleNamespace(user=client_user)
    load = SimpleNamespace(
        id=10,
        client_id=20,
        client=client,
        code="CRG-001",
        load_type="geral",
        load_name="Carga de teste",
        description=None,
        weight=10,
        weight_unit="ton",
        volume=20,
        negotiable=True,
        origin="Maputo",
        destination="Matola",
        origin_lat=-25.9,
        origin_lng=32.5,
        destination_lat=-25.8,
        destination_lng=32.4,
        departure_date=date.today(),
        load_fill="completa",
        suggested_vehicle_type="camião",
        instructions=None,
        status="em_transporte",
        created_at=now,
        updated_at=now,
    )
    vehicle = SimpleNamespace(
        id=30,
        company_id=40,
        driver_id=50,
        plate="ABC-12-34",
        brand="Marca",
        model_name="Modelo",
        vehicle_type="camião",
        tonnage_capacity=20,
        volume_capacity=40,
        photo=None,
        status="em_viagem",
        current_lat=None,
        current_lng=None,
        location_updated_at=None,
        created_at=now,
    )
    driver = SimpleNamespace(
        id=50,
        user_id=60,
        company_id=40,
        license_number="LIC-001",
        years_experience=5,
        average_rating=5,
        total_trips=1,
        available=False,
        current_lat=None,
        current_lng=None,
        location_updated_at=None,
        user=user,
    )
    trip = SimpleNamespace(
        id=1,
        load_id=10,
        company_id=40,
        driver_id=50,
        vehicle_id=30,
        status="aguardando_inicio",
        en_route_pickup_at=None,
        arrived_pickup_at=None,
        loaded_at=None,
        started_at=None,
        arrived_at=None,
        client_confirmed_at=None,
        completed_at=None,
        total_distance_km=None,
        traveled_distance_km=None,
        estimated_time=None,
        pickup_distance_km=12.5,
        pickup_estimated_time="18 min",
        created_at=now,
        load=load,
        company=SimpleNamespace(id=40, company_name="Transportadora"),
        vehicle=vehicle,
        driver=driver,
        activities=[],
        stops=[],
    )

    payload = build_trip_detail(trip)
    validated = TripDriverDetailResponse.model_validate(payload)

    assert validated.vehicle is not None
    assert validated.vehicle.created_at == now
    assert validated.load is not None
    assert validated.load.negotiable is True
    assert validated.pickup_distance_km == 12.5
    assert validated.pickup_estimated_time == "18 min"
