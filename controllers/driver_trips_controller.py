"""
Controller do app motorista: viagens, GPS e paragens.
Sem negociação, pagamentos ou carteira.
"""

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload

from constants import (
    STOP_TYPE_IDS,
    TRIP_GROUP_COMPLETED,
    TRIP_GROUP_IN_PROGRESS,
    TRIP_GROUP_STATUSES,
    TRIP_STATUS_ARRIVED_PICKUP,
    TRIP_STATUS_EN_ROUTE_PICKUP,
    TRIP_STATUS_LOADED,
    TRIP_STATUS_STARTED,
)
from models.models import Client, Company, Driver, Load, Trip, TripLocation, TripStop, User
from schemas.schemas import TripLocationCreateRequest, TripStartRequest, TripStopCreateRequest


def require_driver(db: Session, user: User) -> Driver:
    """Garante utilizador motorista com perfil."""
    if user.user_type != "motorista":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acesso apenas para motoristas",
        )
    driver = db.query(Driver).filter(Driver.user_id == user.id).first()
    if driver is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Perfil motorista não encontrado",
        )
    return driver


def get_driver_trip(db: Session, driver: Driver, trip_id: int) -> Trip:
    """Busca viagem do motorista com carga, cliente, paragens e atividades."""
    trip = (
        db.query(Trip)
        .options(
            joinedload(Trip.load).joinedload(Load.client).joinedload(Client.user),
            joinedload(Trip.company),
            joinedload(Trip.vehicle),
            joinedload(Trip.driver).joinedload(Driver.user),
            joinedload(Trip.stops),
            joinedload(Trip.activities),
        )
        .filter(Trip.id == trip_id, Trip.driver_id == driver.id)
        .first()
    )
    if trip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Viagem não encontrada")
    return trip


def _calc_progress(trip: Trip) -> float | None:
    """Calcula percentagem percorrida com base na distância."""
    if trip.total_distance_km and trip.traveled_distance_km:
        total = float(trip.total_distance_km)
        traveled = float(trip.traveled_distance_km)
        if total > 0:
            return min(round((traveled / total) * 100, 1), 100.0)
    return None


def _sync_live_location(
    trip: Trip,
    driver: Driver,
    latitude: Decimal,
    longitude: Decimal,
) -> None:
    """Mantem a posicao atual sincronizada com o ultimo ponto GPS da viagem."""
    now = datetime.now(timezone.utc)
    driver.current_lat = latitude
    driver.current_lng = longitude
    driver.location_updated_at = now

    if trip.vehicle is not None:
        trip.vehicle.current_lat = latitude
        trip.vehicle.current_lng = longitude
        trip.vehicle.location_updated_at = now


def _serialize_stop(s: TripStop) -> dict:
    """Converte ORM TripStop para dict — evita DetachedInstanceError."""
    return {
        "id": s.id,
        "trip_id": s.trip_id,
        "stop_type": s.stop_type,
        "location_name": s.location_name,
        "address": s.address,
        "notes": s.notes,
        "latitude": float(s.latitude) if s.latitude is not None else None,
        "longitude": float(s.longitude) if s.longitude is not None else None,
        "stopped_at": s.stopped_at,
        "resumed_at": s.resumed_at,
        "created_at": s.created_at,
    }


def build_trip_list_item(trip: Trip) -> dict:
    # Item operacional completo para a lista Minhas Viagens.
    load = trip.load
    client_user = load.client.user if load and load.client else None
    company = trip.company
    vehicle = trip.vehicle

    return {
        "id": trip.id,
        "load_id": trip.load_id,
        "company_id": trip.company_id,
        "driver_id": trip.driver_id,
        "vehicle_id": trip.vehicle_id,
        "load_code": load.code if load else "",
        "load_type": load.load_type if load else "",
        "origin": load.origin if load else "",
        "destination": load.destination if load else "",
        "origin_lat": float(load.origin_lat) if load and load.origin_lat is not None else None,
        "origin_lng": float(load.origin_lng) if load and load.origin_lng is not None else None,
        "destination_lat": float(load.destination_lat) if load and load.destination_lat is not None else None,
        "destination_lng": float(load.destination_lng) if load and load.destination_lng is not None else None,
        "client_name": client_user.name if client_user else "",
        "client_phone": client_user.phone if client_user else None,
        "status": trip.status,
        "en_route_pickup_at": trip.en_route_pickup_at,
        "arrived_pickup_at": trip.arrived_pickup_at,
        "loaded_at": trip.loaded_at,
        "started_at": trip.started_at,
        "arrived_at": trip.arrived_at,
        "client_confirmed_at": trip.client_confirmed_at,
        "completed_at": trip.completed_at,
        "total_distance_km": float(trip.total_distance_km) if trip.total_distance_km is not None else None,
        "traveled_distance_km": float(trip.traveled_distance_km) if trip.traveled_distance_km is not None else None,
        "progress_percent": _calc_progress(trip),
        "estimated_time": trip.estimated_time,
        "departure_date": load.departure_date if load else None,
        "created_at": trip.created_at,
        "company": {
            "id": company.id,
            "company_name": company.company_name,
        } if company else None,
        "vehicle": {
            "id": vehicle.id,
            "plate": vehicle.plate,
            "brand": vehicle.brand,
            "model_name": vehicle.model_name,
            "vehicle_type": vehicle.vehicle_type,
            "photo": vehicle.photo,
            "status": vehicle.status,
            "current_lat": float(vehicle.current_lat) if vehicle.current_lat is not None else None,
            "current_lng": float(vehicle.current_lng) if vehicle.current_lng is not None else None,
            "location_updated_at": vehicle.location_updated_at,
        } if vehicle else None,
        "load": {
            "id": load.id,
            "client_id": load.client_id,
            "code": load.code,
            "load_type": load.load_type,
            "load_name": load.load_name,
            "description": load.description,
            "weight": float(load.weight) if load.weight is not None else None,
            "weight_unit": load.weight_unit,
            "volume": float(load.volume) if load.volume is not None else None,
            "origin": load.origin,
            "destination": load.destination,
            "origin_lat": float(load.origin_lat) if load.origin_lat is not None else None,
            "origin_lng": float(load.origin_lng) if load.origin_lng is not None else None,
            "destination_lat": float(load.destination_lat) if load.destination_lat is not None else None,
            "destination_lng": float(load.destination_lng) if load.destination_lng is not None else None,
            "departure_date": load.departure_date,
            "instructions": load.instructions,
            "status": load.status,
        } if load else None,
    }

def build_trip_detail(trip: Trip) -> dict:
    # Detalhe operacional completo para o ecrã do motorista.
    load = trip.load
    client_user = load.client.user if load and load.client else None
    company = trip.company
    vehicle = trip.vehicle
    driver = trip.driver

    activities_data = [
        {
            "id": act.id,
            "trip_id": act.trip_id,
            "event_type": act.event_type,
            "title": act.title,
            "description": act.description,
            "latitude": float(act.latitude) if act.latitude is not None else None,
            "longitude": float(act.longitude) if act.longitude is not None else None,
            "created_at": act.created_at,
        }
        for act in (trip.activities or [])
    ]
    stops_data = [_serialize_stop(s) for s in (trip.stops or [])]

    return {
        "id": trip.id,
        "load_id": trip.load_id,
        "company_id": trip.company_id,
        "driver_id": trip.driver_id,
        "vehicle_id": trip.vehicle_id,
        "status": trip.status,
        "en_route_pickup_at": trip.en_route_pickup_at,
        "arrived_pickup_at": trip.arrived_pickup_at,
        "loaded_at": trip.loaded_at,
        "started_at": trip.started_at,
        "arrived_at": trip.arrived_at,
        "client_confirmed_at": trip.client_confirmed_at,
        "completed_at": trip.completed_at,
        "total_distance_km": float(trip.total_distance_km) if trip.total_distance_km is not None else None,
        "traveled_distance_km": float(trip.traveled_distance_km) if trip.traveled_distance_km is not None else None,
        "estimated_time": trip.estimated_time,
        "created_at": trip.created_at,
        "load_code": load.code if load else "",
        "load_type": load.load_type if load else "",
        "origin": load.origin if load else "",
        "destination": load.destination if load else "",
        "origin_lat": float(load.origin_lat) if load and load.origin_lat is not None else None,
        "origin_lng": float(load.origin_lng) if load and load.origin_lng is not None else None,
        "destination_lat": float(load.destination_lat) if load and load.destination_lat is not None else None,
        "destination_lng": float(load.destination_lng) if load and load.destination_lng is not None else None,
        "client_name": client_user.name if client_user else "",
        "client_phone": client_user.phone if client_user else None,
        "progress_percent": _calc_progress(trip),
        "stops": stops_data,
        "activities": activities_data,
        "company": {
            "id": company.id,
            "company_name": company.company_name,
        } if company else None,
        "vehicle": {
            "id": vehicle.id,
            "company_id": vehicle.company_id,
            "driver_id": vehicle.driver_id,
            "plate": vehicle.plate,
            "brand": vehicle.brand,
            "model_name": vehicle.model_name,
            "vehicle_type": vehicle.vehicle_type,
            "tonnage_capacity": float(vehicle.tonnage_capacity) if vehicle.tonnage_capacity is not None else None,
            "volume_capacity": float(vehicle.volume_capacity) if vehicle.volume_capacity is not None else None,
            "photo": vehicle.photo,
            "status": vehicle.status,
            "current_lat": float(vehicle.current_lat) if vehicle.current_lat is not None else None,
            "current_lng": float(vehicle.current_lng) if vehicle.current_lng is not None else None,
            "location_updated_at": vehicle.location_updated_at,
        } if vehicle else None,
        "driver": {
            "id": driver.id,
            "user_id": driver.user_id,
            "company_id": driver.company_id,
            "license_number": driver.license_number,
            "years_experience": driver.years_experience,
            "average_rating": float(driver.average_rating) if driver.average_rating is not None else 0,
            "total_trips": driver.total_trips,
            "available": driver.available,
            "current_lat": float(driver.current_lat) if driver.current_lat is not None else None,
            "current_lng": float(driver.current_lng) if driver.current_lng is not None else None,
            "location_updated_at": driver.location_updated_at,
            "name": driver.user.name if driver.user else None,
            "phone": driver.user.phone if driver.user else None,
            "profile_photo": driver.user.profile_photo if driver.user else None,
        } if driver else None,
        "load": {
            "id": load.id,
            "client_id": load.client_id,
            "code": load.code,
            "load_type": load.load_type,
            "load_name": load.load_name,
            "description": load.description,
            "weight": float(load.weight) if load.weight is not None else None,
            "weight_unit": load.weight_unit,
            "volume": float(load.volume) if load.volume is not None else None,
            "origin": load.origin,
            "destination": load.destination,
            "origin_lat": float(load.origin_lat) if load.origin_lat is not None else None,
            "origin_lng": float(load.origin_lng) if load.origin_lng is not None else None,
            "destination_lat": float(load.destination_lat) if load.destination_lat is not None else None,
            "destination_lng": float(load.destination_lng) if load.destination_lng is not None else None,
            "departure_date": load.departure_date,
            "load_fill": load.load_fill,
            "suggested_vehicle_type": load.suggested_vehicle_type,
            "instructions": load.instructions,
            "status": load.status,
            "created_at": load.created_at,
            "updated_at": load.updated_at,
        } if load else None,
    }

def list_driver_trips(db: Session, user: User, group: str | None = None) -> list[dict]:
    """Lista viagens do motorista (em_andamento ou concluidas)."""
    driver = require_driver(db, user)
    query = (
        db.query(Trip)
        .join(Load, Trip.load_id == Load.id)
        .options(
            joinedload(Trip.load).joinedload(Load.client).joinedload(Client.user),
            joinedload(Trip.company),
            joinedload(Trip.vehicle),
        )
        .filter(Trip.driver_id == driver.id)
    )

    if group:
        if group not in TRIP_GROUP_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Grupo inválido. Use: {TRIP_GROUP_IN_PROGRESS}, {TRIP_GROUP_COMPLETED}",
            )
        query = query.filter(Trip.status.in_(TRIP_GROUP_STATUSES[group]))

    trips = query.order_by(Trip.created_at.desc()).all()
    return [build_trip_list_item(t) for t in trips]


def get_driver_trip_detail(db: Session, user: User, trip_id: int) -> dict:
    """Detalhe da viagem para o motorista."""
    driver = require_driver(db, user)
    trip = get_driver_trip(db, driver, trip_id)
    return build_trip_detail(trip)


def start_driver_pickup_trip(db: Session, user: User, trip_id: int) -> dict:
    """Motorista inicia deslocamento para coleta."""
    from controllers.trips_controller import start_pickup_trip

    start_pickup_trip(db, user, trip_id)
    driver = require_driver(db, user)
    return build_trip_detail(get_driver_trip(db, driver, trip_id))


def arrive_driver_pickup_trip(db: Session, user: User, trip_id: int) -> dict:
    """Motorista confirma chegada à coleta."""
    from controllers.trips_controller import arrive_pickup_trip

    arrive_pickup_trip(db, user, trip_id)
    driver = require_driver(db, user)
    return build_trip_detail(get_driver_trip(db, driver, trip_id))


def confirm_driver_loaded_trip(db: Session, user: User, trip_id: int) -> dict:
    """Motorista confirma carga carregada."""
    from controllers.trips_controller import confirm_loaded_trip

    confirm_loaded_trip(db, user, trip_id)
    driver = require_driver(db, user)
    return build_trip_detail(get_driver_trip(db, driver, trip_id))


def start_driver_trip(db: Session, user: User, trip_id: int, data: TripStartRequest) -> dict:
    """Motorista inicia viagem."""
    from controllers.trips_controller import start_trip

    start_trip(db, user, trip_id, data)
    driver = require_driver(db, user)
    return build_trip_detail(get_driver_trip(db, driver, trip_id))


def end_driver_trip(db: Session, user: User, trip_id: int) -> dict:
    """Motorista encerra viagem / confirma chegada ao destino."""
    from controllers.trips_controller import arrive_trip

    arrive_trip(db, user, trip_id)
    driver = require_driver(db, user)
    return build_trip_detail(get_driver_trip(db, driver, trip_id))


def add_driver_location(
    db: Session, user: User, trip_id: int, data: TripLocationCreateRequest
) -> TripLocation:
    """Envia GPS e opcionalmente atualiza distância percorrida.
    Aceita localização em qualquer estado ativo da viagem.
    """
    from controllers.trips_controller import _record_gps_point

    driver = require_driver(db, user)
    trip = get_driver_trip(db, driver, trip_id)

    if trip.status not in (
        TRIP_STATUS_EN_ROUTE_PICKUP,
        TRIP_STATUS_ARRIVED_PICKUP,
        TRIP_STATUS_LOADED,
        TRIP_STATUS_STARTED,
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Localização só durante viagem ou deslocamento em curso",
        )

    return _record_gps_point(db, trip, driver, data)


def list_driver_locations(db: Session, user: User, trip_id: int) -> list[TripLocation]:
    """Histórico GPS da viagem."""
    driver = require_driver(db, user)
    get_driver_trip(db, driver, trip_id)
    return (
        db.query(TripLocation)
        .filter(TripLocation.trip_id == trip_id)
        .order_by(TripLocation.created_at.asc())
        .all()
    )


def add_trip_stop(db: Session, user: User, trip_id: int, data: TripStopCreateRequest) -> TripStop:
    """Regista paragem (abastecimento, descanso, etc.)."""
    if data.stop_type not in STOP_TYPE_IDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Tipo de paragem inválido. Use: {', '.join(sorted(STOP_TYPE_IDS))}",
        )

    driver = require_driver(db, user)
    trip = get_driver_trip(db, driver, trip_id)

    if trip.status not in (TRIP_STATUS_STARTED,):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Paragens só durante viagem em curso",
        )

    stopped_at = data.stopped_at or datetime.now(timezone.utc)
    stop = TripStop(
        trip_id=trip_id,
        stop_type=data.stop_type,
        location_name=data.location_name,
        address=data.address,
        notes=data.notes,
        latitude=Decimal(str(data.latitude)) if data.latitude is not None else None,
        longitude=Decimal(str(data.longitude)) if data.longitude is not None else None,
        stopped_at=stopped_at,
    )
    db.add(stop)
    db.commit()
    db.refresh(stop)
    return stop


def resume_trip_stop(db: Session, user: User, trip_id: int, stop_id: int) -> TripStop:
    """Motorista retoma viagem — regista hora de saída da paragem."""
    driver = require_driver(db, user)
    get_driver_trip(db, driver, trip_id)  # valida ownership

    stop = (
        db.query(TripStop)
        .filter(TripStop.id == stop_id, TripStop.trip_id == trip_id)
        .first()
    )
    if stop is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Paragem não encontrada",
        )
    if stop.resumed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Paragem já foi encerrada",
        )

    stop.resumed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(stop)
    return stop


def list_trip_stops(db: Session, user: User, trip_id: int) -> list[TripStop]:
    """Lista paragens da viagem."""
    driver = require_driver(db, user)
    get_driver_trip(db, driver, trip_id)
    return (
        db.query(TripStop)
        .filter(TripStop.trip_id == trip_id)
        .order_by(TripStop.stopped_at.asc())
        .all()
    )
