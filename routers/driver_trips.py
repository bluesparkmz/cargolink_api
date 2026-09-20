"""
Rotas do app motorista — viagens, GPS e paragens.
Sem carteira, pagamentos ou publicação de cargas.
"""

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.orm import Session

from constants import STOP_TYPES, TRIP_GROUP_COMPLETED, TRIP_GROUP_IN_PROGRESS
from controllers.driver_trips_controller import (
    add_driver_location,
    add_trip_stop,
    arrive_driver_pickup_trip,
    confirm_driver_loaded_trip,
    end_driver_trip,
    get_driver_trip_detail,
    list_driver_locations,
    list_driver_trips,
    list_trip_stops,
    resume_trip_stop,
    start_driver_pickup_trip,
    start_driver_trip,
)
from deps import get_current_user
from database import get_db
from models.models import User
from schemas.schemas import (
    StopTypeItem,
    TripDriverDetailResponse,
    TripDriverListItem,
    TripLocationCreateRequest,
    TripLocationResponse,
    TripPickupStartRequest,
    TripStartRequest,
    TripStopCreateRequest,
    TripStopResumeRequest,
    TripStopResponse,
)

router = APIRouter()


@router.get("/stops/types", response_model=list[StopTypeItem])
def list_stop_types():
    """Tipos de paragem: abastecimento, descanso, mecânica, outros."""
    return [StopTypeItem(**item) for item in STOP_TYPES]


@router.get("", response_model=list[TripDriverListItem])
def list_trips(
    group: str | None = Query(
        None,
        description=f"Filtrar: {TRIP_GROUP_IN_PROGRESS} ou {TRIP_GROUP_COMPLETED}",
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Minhas viagens — em andamento ou concluídas."""
    return list_driver_trips(db, current_user, group=group)


@router.get("/{trip_id}", response_model=TripDriverDetailResponse)
def get_trip_detail(
    trip_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Detalhe da viagem com progresso e dados do cliente."""
    return get_driver_trip_detail(db, current_user, trip_id)


@router.patch("/{trip_id}/start-pickup", response_model=TripDriverDetailResponse)
def start_pickup_trip(
    trip_id: int,
    data: TripPickupStartRequest | None = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Sair para carregar / a caminho da origem da carga."""
    return start_driver_pickup_trip(db, current_user, trip_id, data)


@router.patch("/{trip_id}/arrive-pickup", response_model=TripDriverDetailResponse)
def arrive_pickup_trip(
    trip_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Chegada ao local de carregamento / origem."""
    return arrive_driver_pickup_trip(db, current_user, trip_id)


@router.patch("/{trip_id}/confirm-loaded", response_model=TripDriverDetailResponse)
def confirm_loaded_trip(
    trip_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Confirmar que a carga foi carregada no camião."""
    return confirm_driver_loaded_trip(db, current_user, trip_id)


@router.patch("/{trip_id}/start", response_model=TripDriverDetailResponse)
def start_trip(
    trip_id: int,
    data: TripStartRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Iniciar viagem de entrega."""
    return start_driver_trip(db, current_user, trip_id, data)


@router.patch("/{trip_id}/end", response_model=TripDriverDetailResponse)
def end_trip(
    trip_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Encerrar viagem / confirmar chegada ao destino."""
    return end_driver_trip(db, current_user, trip_id)


@router.post("/{trip_id}/locations", response_model=TripLocationResponse, status_code=201)
def post_location(
    trip_id: int,
    data: TripLocationCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Enviar localização GPS em tempo real."""
    return add_driver_location(db, current_user, trip_id, data)


@router.get("/{trip_id}/locations", response_model=list[TripLocationResponse])
def get_locations(
    trip_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Histórico de localização da viagem."""
    return list_driver_locations(db, current_user, trip_id)


@router.post("/{trip_id}/stops", response_model=TripStopResponse, status_code=201)
def create_stop(
    trip_id: int,
    data: TripStopCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Registar paragem na viagem."""
    return add_trip_stop(db, current_user, trip_id, data)


@router.get("/{trip_id}/stops", response_model=list[TripStopResponse])
def get_stops(
    trip_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Listar paragens da viagem."""
    return list_trip_stops(db, current_user, trip_id)


@router.patch("/{trip_id}/stops/{stop_id}/resume", response_model=TripStopResponse)
def resume_stop(
    trip_id: int,
    stop_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Motorista retoma viagem — regista hora de saída da paragem."""
    return resume_trip_stop(db, current_user, trip_id, stop_id)
