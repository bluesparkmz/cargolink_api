"""
Rotas de viagens: estados e localização em tempo real.
"""

from fastapi import APIRouter, Body, Depends
from sqlalchemy.orm import Session

from controllers.trips_controller import (
    assign_vehicle_to_trip,
    user_can_access_trip,
    add_trip_location,
    arrive_pickup_trip,
    arrive_trip,
    confirm_delivery,
    confirm_loaded_trip,
    get_trip_detail,
    list_my_trips,
    list_trip_locations,
    start_pickup_trip,
    start_trip,
)
from deps import get_current_user
from database import get_db
from models.models import User
from schemas.schemas import (
    TripAssignVehicleRequest,
    TripLocationCreateRequest,
    TripLocationResponse,
    TripPickupStartRequest,
    TripResponse,
    TripStartRequest,
)

router = APIRouter()


@router.get("/me", response_model=list[TripResponse])
def list_my_trips_route(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Lista viagens do motorista ou do cliente."""
    return list_my_trips(db, current_user)


@router.get("/{trip_id}", response_model=TripResponse)
def get_trip(
    trip_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Detalhe da viagem."""
    from controllers.trips_controller import _serialize_trip
    trip = get_trip_detail(db, trip_id)
    user_can_access_trip(db, current_user, trip)
    return _serialize_trip(trip)


@router.patch('/{trip_id}/assign-vehicle', response_model=TripResponse)
def assign_vehicle(trip_id: int, data: TripAssignVehicleRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return assign_vehicle_to_trip(db, current_user, trip_id, data.vehicle_id)


@router.patch("/{trip_id}/start-pickup", response_model=TripResponse)
def start_pickup(
    trip_id: int,
    data: TripPickupStartRequest | None = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Motorista inicia deslocamento para o local de carregamento (origem)."""
    return start_pickup_trip(db, current_user, trip_id, data)


@router.patch("/{trip_id}/arrive-pickup", response_model=TripResponse)
def arrive_pickup(
    trip_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Motorista confirma chegada ao local de carregamento (origem)."""
    return arrive_pickup_trip(db, current_user, trip_id)


@router.patch("/{trip_id}/confirm-loaded", response_model=TripResponse)
def confirm_loaded(
    trip_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Motorista confirma que a carga foi totalmente carregada no veículo."""
    return confirm_loaded_trip(db, current_user, trip_id)


@router.patch("/{trip_id}/start", response_model=TripResponse)
def start(
    trip_id: int,
    data: TripStartRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Motorista inicia viagem de entrega (status viagem_iniciada)."""
    return start_trip(db, current_user, trip_id, data)


@router.patch("/{trip_id}/arrive", response_model=TripResponse)
def arrive(
    trip_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Motorista confirma chegada (aguardando_cliente)."""
    return arrive_trip(db, current_user, trip_id)


@router.patch("/{trip_id}/confirm", response_model=TripResponse)
def confirm(
    trip_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cliente confirma entrega (concluida)."""
    return confirm_delivery(db, current_user, trip_id)


@router.post("/{trip_id}/locations", response_model=TripLocationResponse, status_code=201)
def post_location(
    trip_id: int,
    data: TripLocationCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Motorista envia localização GPS."""
    return add_trip_location(db, current_user, trip_id, data)


@router.get("/{trip_id}/locations", response_model=list[TripLocationResponse])
def get_locations(
    trip_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Histórico de localização da viagem."""
    return list_trip_locations(db, current_user, trip_id)
