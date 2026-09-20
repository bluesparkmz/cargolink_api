"""
Controller de viagens: estados e localização GPS.
"""

from datetime import datetime, timezone
from decimal import Decimal
from math import atan2, cos, radians, sin, sqrt

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload

from constants import (
    LOAD_STATUS_ACCEPTED,
    LOAD_STATUS_ARRIVED_PICKUP,
    LOAD_STATUS_EN_ROUTE_PICKUP,
    LOAD_STATUS_IN_TRANSIT,
    LOAD_STATUS_LOADED,
    LOAD_STATUS_COMPLETED,
    LOAD_STATUS_WAITING_CLIENT,
    TRIP_LOCATION_HEARTBEAT_SECONDS,
    TRIP_LOCATION_MIN_DISTANCE_METERS,
    TRIP_LOCATION_MIN_INTERVAL_SECONDS,
    TRIP_EXECUTION_STATUSES,
    TRIP_STATUS_ARRIVED_PICKUP,
    TRIP_STATUS_COMPLETED,
    TRIP_STATUS_EN_ROUTE_PICKUP,
    TRIP_STATUS_LOADED,
    TRIP_STATUS_STARTED,
    TRIP_STATUS_WAITING,
    TRIP_STATUS_WAITING_CLIENT,
    VEHICLE_STATUS_AVAILABLE,
    VEHICLE_STATUS_UNAVAILABLE,
)
from controllers.notifications_controller import create_notification, emit_notification
from controllers.realtime_events import emit_to_rooms
from controllers.wallet_transport_controller import release_transport_escrow_for_trip
from models.models import Client, Company, Driver, Load, Trip, TripActivity, TripEvidenceStage, TripLocation, User, Vehicle
from schemas.schemas import TripLocationCreateRequest, TripPickupStartRequest, TripStartRequest


def log_trip_activity(
    db: Session,
    trip: Trip,
    event_type: str,
    title: str,
    description: str | None = None,
    latitude: Decimal | None = None,
    longitude: Decimal | None = None,
) -> TripActivity:
    """Regista uma nova atividade cronologica no historico da viagem."""
    activity = TripActivity(
        trip_id=trip.id,
        event_type=event_type,
        title=title,
        description=description,
        latitude=latitude,
        longitude=longitude,
    )
    db.add(activity)
    db.commit()
    db.refresh(activity)
    return activity


def _serialize_trip(trip: Trip) -> dict:
    """Converte um objeto Trip ORM num dicionario com dados completos de carga, camiao e motorista."""
    load = trip.load
    vehicle = trip.vehicle
    driver = trip.driver

    load_data = None
    if load is not None:
        load_data = {
            "id": load.id,
            "client_id": load.client_id,
            "code": load.code,
            "load_type": load.load_type,
            "load_name": load.load_name,
            "description": load.description,
            "weight": float(load.weight) if load.weight is not None else None,
            "weight_unit": load.weight_unit,
            "volume": float(load.volume) if load.volume is not None else None,
            "value": float(load.value) if load.value is not None else None,
            "negotiable": load.negotiable,
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
        }

    vehicle_data = None
    if vehicle is not None:
        vehicle_data = {
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
            "created_at": vehicle.created_at,
        }

    driver_data = None
    if driver is not None:
        driver_user = driver.user if hasattr(driver, "user") else None
        driver_data = {
            "id": driver.id,
            "user_id": driver.user_id,
            "company_id": driver.company_id,
            "license_number": driver.license_number,
            "years_experience": driver.years_experience,
            "average_rating": float(driver.average_rating) if driver.average_rating is not None else 0.0,
            "total_trips": driver.total_trips,
            "available": driver.available,
            "current_lat": float(driver.current_lat) if driver.current_lat is not None else None,
            "current_lng": float(driver.current_lng) if driver.current_lng is not None else None,
            "location_updated_at": driver.location_updated_at,
            "name": driver_user.name if driver_user else None,
            "phone": driver_user.phone if driver_user else None,
            "profile_photo": driver_user.profile_photo if driver_user else None,
        }

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
        for act in sorted((trip.activities or []), key=lambda item: (item.created_at, item.id))
    ]

    return {
        "id": trip.id,
        "load_id": trip.load_id,
        "company_id": trip.company_id,
        "driver_id": trip.driver_id,
        "vehicle_id": trip.vehicle_id,
        "status": trip.status,
        "origin": load.origin if load else None,
        "destination": load.destination if load else None,
        "origin_lat": float(load.origin_lat) if load and load.origin_lat is not None else None,
        "origin_lng": float(load.origin_lng) if load and load.origin_lng is not None else None,
        "destination_lat": float(load.destination_lat) if load and load.destination_lat is not None else None,
        "destination_lng": float(load.destination_lng) if load and load.destination_lng is not None else None,
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
        "pickup_distance_km": float(trip.pickup_distance_km) if trip.pickup_distance_km is not None else None,
        "pickup_estimated_time": trip.pickup_estimated_time,
        "created_at": trip.created_at,
        "load": load_data,
        "vehicle": vehicle_data,
        "driver": driver_data,
        "activities": activities_data,
    }


def get_trip_detail(db: Session, trip_id: int) -> Trip:
    """Busca viagem por id."""
    trip = (
        db.query(Trip)
        .options(
            joinedload(Trip.load),
            joinedload(Trip.vehicle),
            joinedload(Trip.driver).joinedload(Driver.user),
            joinedload(Trip.activities),
        )
        .filter(Trip.id == trip_id)
        .first()
    )
    if trip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Viagem não encontrada")
    return trip


def user_can_access_trip(db: Session, user: User, trip: Trip) -> None:
    """Verifica se utilizador é o motorista ou o cliente da carga."""
    if user.user_type == "motorista":
        driver = db.query(Driver).filter(Driver.user_id == user.id).first()
        if driver and trip.driver_id == driver.id:
            return
    if user.user_type == "cliente":
        client = db.query(Client).filter(Client.user_id == user.id).first()
        load = db.query(Load).filter(Load.id == trip.load_id).first()
        if client and load and load.client_id == client.id:
            return
    if user.user_type == "empresa":
        company = db.query(Company).filter(Company.user_id == user.id).first()
        if company and trip.company_id == company.id:
            return
    if user.user_type == "admin":
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sem acesso a esta viagem")


# Mantém alias privado para não quebrar imports existentes
_user_can_access_trip = user_can_access_trip


def _sync_live_location(
    trip: Trip,
    driver: Driver,
    latitude: Decimal,
    longitude: Decimal,
) -> None:
    """Mantem motorista e camiao da viagem na ultima posicao GPS recebida."""
    now = datetime.now(timezone.utc)
    driver.current_lat = latitude
    driver.current_lng = longitude
    driver.location_updated_at = now

    if trip.vehicle is not None:
        trip.vehicle.current_lat = latitude
        trip.vehicle.current_lng = longitude
        trip.vehicle.location_updated_at = now


def _distance_meters(
    lat1: Decimal,
    lng1: Decimal,
    lat2: Decimal,
    lng2: Decimal,
) -> float:
    """Calcula distancia aproximada entre dois pontos GPS."""
    earth_radius_m = 6371000
    phi1 = radians(float(lat1))
    phi2 = radians(float(lat2))
    delta_phi = radians(float(lat2 - lat1))
    delta_lambda = radians(float(lng2 - lng1))
    a = sin(delta_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(delta_lambda / 2) ** 2
    return earth_radius_m * 2 * atan2(sqrt(a), sqrt(1 - a))


def _seconds_since(created_at: datetime | None) -> float | None:
    if created_at is None:
        return None
    now = datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        return (now.replace(tzinfo=None) - created_at).total_seconds()
    return (now - created_at).total_seconds()


def _latest_trip_location(db: Session, trip_id: int) -> TripLocation | None:
    return (
        db.query(TripLocation)
        .filter(TripLocation.trip_id == trip_id)
        .order_by(TripLocation.created_at.desc())
        .first()
    )


def _should_store_trip_location(
    last_location: TripLocation | None,
    latitude: Decimal,
    longitude: Decimal,
    phase: str,
) -> bool:
    if last_location is None:
        return True

    # Conserva sempre o primeiro ponto de cada fase, mesmo que a mudança de
    # estado aconteça poucos segundos depois do último heartbeat.
    if last_location.phase != phase:
        return True

    elapsed_seconds = _seconds_since(last_location.created_at)
    if elapsed_seconds is not None and elapsed_seconds >= TRIP_LOCATION_HEARTBEAT_SECONDS:
        return True

    if elapsed_seconds is not None and elapsed_seconds < TRIP_LOCATION_MIN_INTERVAL_SECONDS:
        return False

    distance = _distance_meters(
        last_location.latitude,
        last_location.longitude,
        latitude,
        longitude,
    )
    return distance >= TRIP_LOCATION_MIN_DISTANCE_METERS


def _trip_event_rooms(trip: Trip) -> set[str]:
    rooms = {f"trip:{trip.id}", f"load:{trip.load_id}"}
    if trip.company_id:
        rooms.add(f"company:{trip.company_id}")
    if trip.driver_id:
        rooms.add(f"driver:{trip.driver_id}")
    return rooms


def _trip_user_ids(db: Session, trip: Trip) -> set[int]:
    user_ids: set[int] = set()
    load = db.query(Load).filter(Load.id == trip.load_id).first()
    if load:
        client = db.query(Client).filter(Client.id == load.client_id).first()
        if client:
            user_ids.add(client.user_id)
    if trip.company_id:
        company = db.query(Company).filter(Company.id == trip.company_id).first()
        if company:
            user_ids.add(company.user_id)
    if trip.driver_id:
        driver = db.query(Driver).filter(Driver.id == trip.driver_id).first()
        if driver:
            user_ids.add(driver.user_id)
    return user_ids


def _ensure_driver_has_no_other_active_trip(
    db: Session,
    driver: Driver,
    trip_id: int,
) -> None:
    """Impede o motorista de executar duas cargas ao mesmo tempo."""
    active_trip = (
        db.query(Trip)
        .filter(
            Trip.id != trip_id,
            Trip.driver_id == driver.id,
            Trip.status.in_(TRIP_EXECUTION_STATUSES),
        )
        .order_by(Trip.created_at.desc())
        .first()
    )
    if active_trip is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Conclua primeiro a viagem em andamento "
                f"(#{active_trip.id}) para iniciar esta carga."
            ),
        )


def _trip_status_event_payload(db: Session, trip: Trip) -> dict:
    """Monta payload realtime com contexto de motorista/veiculo para cliente e empresa."""
    driver = db.query(Driver).options(joinedload(Driver.user)).filter(Driver.id == trip.driver_id).first()
    vehicle = db.query(Vehicle).filter(Vehicle.id == trip.vehicle_id).first() if trip.vehicle_id else None
    load = db.query(Load).filter(Load.id == trip.load_id).first()
    client_id = load.client_id if load else None

    return {
        "type": "trip.status_changed",
        "trip_id": trip.id,
        "load_id": trip.load_id,
        "client_id": client_id,
        "company_id": trip.company_id,
        "status": trip.status,
        "en_route_pickup_at": trip.en_route_pickup_at,
        "arrived_pickup_at": trip.arrived_pickup_at,
        "loaded_at": trip.loaded_at,
        "started_at": trip.started_at,
        "arrived_at": trip.arrived_at,
        "completed_at": trip.completed_at,
        "pickup_distance_km": float(trip.pickup_distance_km) if trip.pickup_distance_km is not None else None,
        "pickup_estimated_time": trip.pickup_estimated_time,
        "estimated_time": trip.estimated_time,
        "load": {
            "id": load.id,
            "code": load.code,
            "load_type": load.load_type,
            "load_name": load.load_name,
            "origin": load.origin,
            "destination": load.destination,
            "departure_date": load.departure_date,
            "status": load.status,
        }
        if load
        else None,
        "driver": {
            "id": driver.id,
            "name": driver.user.name if driver and driver.user else None,
            "phone": driver.user.phone if driver and driver.user else None,
            "current_lat": driver.current_lat if driver else None,
            "current_lng": driver.current_lng if driver else None,
        }
        if driver
        else None,
        "vehicle": {
            "id": vehicle.id,
            "plate": vehicle.plate,
            "brand": vehicle.brand,
            "model_name": vehicle.model_name,
            "vehicle_type": vehicle.vehicle_type,
            "photo": vehicle.photo,
            "status": vehicle.status,
            "current_lat": vehicle.current_lat,
            "current_lng": vehicle.current_lng,
        }
        if vehicle
        else None,
    }


def _emit_trip_status_changed(
    db: Session,
    trip: Trip,
    *,
    title: str,
    body: str,
    notification_type: str,
) -> None:
    notifications = [
        create_notification(
            db,
            user_id=user_id,
            title=title,
            body=body,
            notification_type=notification_type,
            payload={"trip_id": trip.id, "load_id": trip.load_id, "status": trip.status},
        )
        for user_id in _trip_user_ids(db, trip)
    ]
    db.commit()
    for notification in notifications:
        db.refresh(notification)
        emit_notification(notification)
    emit_to_rooms(_trip_event_rooms(trip), _trip_status_event_payload(db, trip))


def list_my_trips(db: Session, user: User) -> list[dict]:
    """Lista viagens do motorista ou do cliente com dados da carga, do camiao e do motorista."""
    options = [
        joinedload(Trip.load),
        joinedload(Trip.vehicle),
        joinedload(Trip.driver).joinedload(Driver.user),
    ]

    trips: list[Trip] = []
    if user.user_type == "motorista":
        driver = db.query(Driver).filter(Driver.user_id == user.id).first()
        if driver is not None:
            trips = (
                db.query(Trip)
                .options(*options)
                .filter(Trip.driver_id == driver.id)
                .order_by(Trip.created_at.desc())
                .all()
            )

    elif user.user_type == "cliente":
        client = db.query(Client).filter(Client.user_id == user.id).first()
        if client is not None:
            trips = (
                db.query(Trip)
                .options(*options)
                .join(Load, Trip.load_id == Load.id)
                .filter(Load.client_id == client.id)
                .order_by(Trip.created_at.desc())
                .all()
            )

    elif user.user_type == "empresa":
        company = db.query(Company).filter(Company.user_id == user.id).first()
        if company is not None:
            trips = (
                db.query(Trip)
                .options(*options)
                .filter(Trip.company_id == company.id)
                .order_by(Trip.created_at.desc())
                .all()
            )

    return [_serialize_trip(t) for t in trips]



def assign_vehicle_to_trip(db: Session, user: User, trip_id: int, vehicle_id: int) -> dict:
    # Empresa atribui camiao e motorista a uma viagem aceite.
    if user.user_type != 'empresa':
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Apenas a empresa transportadora pode atribuir o camiao')
    company = db.query(Company).filter(Company.user_id == user.id).first()
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Perfil de empresa nao encontrado')
    trip = get_trip_detail(db, trip_id)
    if trip.company_id != company.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Esta viagem pertence a outra empresa')
    if trip.load is None or trip.load.status != LOAD_STATUS_ACCEPTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Esta carga foi cancelada ou ja nao esta disponivel para atribuicao de camiao',
        )
    if trip.status != TRIP_STATUS_WAITING:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='O camiao so pode ser atribuido antes do motorista iniciar a recolha')

    vehicle = db.query(Vehicle).options(joinedload(Vehicle.driver).joinedload(Driver.user)).filter(Vehicle.id == vehicle_id).first()
    if vehicle is None or vehicle.company_id != company.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Camiao invalido para esta empresa')
    if vehicle.status != VEHICLE_STATUS_AVAILABLE:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='O camiao selecionado nao esta disponivel')
    if vehicle.driver_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Este camiao nao tem motorista atribuido. Atribua um motorista antes de usar o camiao nesta carga.')

    driver = vehicle.driver or db.query(Driver).filter(Driver.id == vehicle.driver_id).first()
    if driver is None or driver.company_id != company.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Motorista invalido para este camiao')
    if not driver.available:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='O motorista deste camiao nao esta disponivel')

    if db.query(Trip).filter(Trip.id != trip.id, Trip.vehicle_id == vehicle.id, Trip.status.in_(TRIP_EXECUTION_STATUSES)).first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Este camiao ja esta atribuido a outra viagem ativa')
    if db.query(Trip).filter(Trip.id != trip.id, Trip.driver_id == driver.id, Trip.status.in_(TRIP_EXECUTION_STATUSES)).first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='O motorista deste camiao ja esta atribuido a outra viagem ativa')

    trip.vehicle_id = vehicle.id
    trip.driver_id = driver.id
    db.commit(); db.refresh(trip)
    load = db.query(Load).filter(Load.id == trip.load_id).first()
    code = load.code if load else f'#{trip.load_id}'
    rota = f' {load.origin} -> {load.destination}.' if load else ''
    log_trip_activity(db, trip, event_type='trip_assigned', title='Camiao e motorista atribuidos', description=f'Camiao {vehicle.plate} atribuido a viagem.')

    notification = create_notification(db, user_id=driver.user_id, title='Nova carga atribuida', body=f'Foi-lhe atribuida a carga {code}.{rota}', notification_type='trip.assigned', payload={'trip_id':trip.id,'load_id':trip.load_id,'vehicle_id':vehicle.id,'driver_id':driver.id})
    db.commit(); db.refresh(notification); emit_notification(notification)
    emit_to_rooms(_trip_event_rooms(trip), {'type':'trip.assigned','trip_id':trip.id,'load_id':trip.load_id,'company_id':trip.company_id,'driver_id':driver.id,'vehicle_id':vehicle.id,'status':trip.status})
    return _serialize_trip(get_trip_detail(db, trip.id))


def start_pickup_trip(
    db: Session,
    user: User,
    trip_id: int,
    data: TripPickupStartRequest | None = None,
) -> Trip:
    """Motorista inicia deslocamento para o local de carregamento (origem)."""
    if user.user_type != "motorista":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Apenas motoristas podem iniciar deslocamento para coleta",
        )

    trip = get_trip_detail(db, trip_id)
    driver = db.query(Driver).filter(Driver.user_id == user.id).first()
    if driver is None or trip.driver_id != driver.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Viagem de outro motorista")

    _ensure_driver_has_no_other_active_trip(db, driver, trip.id)

    if trip.status != TRIP_STATUS_WAITING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Viagem precisa estar aguardando início para sair para coleta",
        )

    trip.status = TRIP_STATUS_EN_ROUTE_PICKUP
    trip.en_route_pickup_at = datetime.now(timezone.utc)
    if data is not None:
        if data.pickup_distance_km is not None:
            trip.pickup_distance_km = Decimal(str(data.pickup_distance_km))
        if data.pickup_estimated_time:
            trip.pickup_estimated_time = data.pickup_estimated_time.strip()

    load = db.query(Load).filter(Load.id == trip.load_id).first()
    if load:
        load.status = LOAD_STATUS_EN_ROUTE_PICKUP

    driver.available = False
    if trip.vehicle is not None:
        trip.vehicle.status = VEHICLE_STATUS_UNAVAILABLE

    db.commit()
    db.refresh(trip)

    log_trip_activity(
        db,
        trip,
        event_type=TRIP_STATUS_EN_ROUTE_PICKUP,
        title="Indo Carregar",
        description=(
            "Motorista a caminho do local de coleta/origem da carga."
            + (f" Previsão de chegada: {trip.pickup_estimated_time}." if trip.pickup_estimated_time else "")
        ),
        latitude=driver.current_lat,
        longitude=driver.current_lng,
    )

    _emit_trip_status_changed(
        db,
        trip,
        title="Motorista a caminho da coleta",
        body="O motorista iniciou o deslocamento para o local de carregamento.",
        notification_type="trip.en_route_pickup",
    )
    return get_trip_detail(db, trip.id)


def arrive_pickup_trip(db: Session, user: User, trip_id: int) -> Trip:
    """Motorista confirma chegada ao local de carregamento (origem)."""
    if user.user_type != "motorista":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Apenas motoristas podem confirmar chegada ao carregamento",
        )

    trip = get_trip_detail(db, trip_id)
    driver = db.query(Driver).filter(Driver.user_id == user.id).first()
    if driver is None or trip.driver_id != driver.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Viagem de outro motorista")

    _ensure_driver_has_no_other_active_trip(db, driver, trip.id)

    if trip.status != TRIP_STATUS_EN_ROUTE_PICKUP:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Motorista precisa estar a caminho da coleta para confirmar chegada",
        )

    trip.status = TRIP_STATUS_ARRIVED_PICKUP
    trip.arrived_pickup_at = datetime.now(timezone.utc)

    load = db.query(Load).filter(Load.id == trip.load_id).first()
    if load:
        load.status = LOAD_STATUS_ARRIVED_PICKUP

    db.commit()
    db.refresh(trip)

    log_trip_activity(
        db,
        trip,
        event_type=TRIP_STATUS_ARRIVED_PICKUP,
        title="Chegou à Origem",
        description="Motorista chegou ao local de carregamento.",
        latitude=driver.current_lat,
        longitude=driver.current_lng,
    )

    _emit_trip_status_changed(
        db,
        trip,
        title="Motorista chegou ao carregamento",
        body="O motorista chegou ao local de coleta da carga.",
        notification_type="trip.arrived_pickup",
    )
    return get_trip_detail(db, trip.id)


def confirm_loaded_trip(db: Session, user: User, trip_id: int) -> Trip:
    """Motorista confirma que a carga foi totalmente carregada no camião."""
    if user.user_type != "motorista":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Apenas motoristas podem confirmar carregamento",
        )

    trip = get_trip_detail(db, trip_id)
    driver = db.query(Driver).filter(Driver.user_id == user.id).first()
    if driver is None or trip.driver_id != driver.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Viagem de outro motorista")

    if trip.status != TRIP_STATUS_ARRIVED_PICKUP:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Carga só pode ser confirmada como carregada após a chegada ao local de coleta",
        )

    pickup_evidence = (
        db.query(TripEvidenceStage)
        .filter(
            TripEvidenceStage.trip_id == trip.id,
            TripEvidenceStage.stage == "pickup",
            TripEvidenceStage.finalized_at.isnot(None),
        )
        .first()
    )
    if pickup_evidence is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Finalize primeiro a prova de carregamento "
                "com 3 a 5 fotografias."
            ),
        )

    trip.status = TRIP_STATUS_LOADED
    trip.loaded_at = datetime.now(timezone.utc)

    load = db.query(Load).filter(Load.id == trip.load_id).first()
    if load:
        load.status = LOAD_STATUS_LOADED

    db.commit()
    db.refresh(trip)

    log_trip_activity(
        db,
        trip,
        event_type=TRIP_STATUS_LOADED,
        title="Carga Carregada",
        description="Carga carregada no veículo com sucesso e pronta para viagem de entrega.",
        latitude=driver.current_lat,
        longitude=driver.current_lng,
    )

    _emit_trip_status_changed(
        db,
        trip,
        title="Carga Carregada",
        body="A carga foi carregada no camião e está pronta para a viagem de entrega.",
        notification_type="trip.loaded",
    )

    from controllers.payment_plan_controller import (
        start_payment_deadline_for_trip_event,
    )
    start_payment_deadline_for_trip_event(
        db,
        trip.id,
        "loading",
    )
    return get_trip_detail(db, trip.id)


def start_trip(db: Session, user: User, trip_id: int, data: TripStartRequest) -> Trip:
    """Motorista inicia a viagem de entrega."""
    if user.user_type != "motorista":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Apenas motoristas podem iniciar viagem",
        )

    trip = get_trip_detail(db, trip_id)
    driver = db.query(Driver).filter(Driver.user_id == user.id).first()
    if driver is None or trip.driver_id != driver.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Viagem de outro motorista")

    _ensure_driver_has_no_other_active_trip(db, driver, trip.id)

    if trip.status not in (TRIP_STATUS_LOADED, TRIP_STATUS_WAITING):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Viagem não pode ser iniciada neste estado",
        )

    if data.vehicle_id:
        vehicle = db.query(Vehicle).filter(Vehicle.id == data.vehicle_id).first()
        if vehicle is None or vehicle.company_id != trip.company_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Veículo inválido para este motorista",
            )
        if vehicle.driver_id is not None and vehicle.driver_id != driver.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Veiculo atribuido a outro motorista",
            )
        trip.vehicle_id = data.vehicle_id

    trip.status = TRIP_STATUS_STARTED
    trip.started_at = datetime.now(timezone.utc)
    if data.total_distance_km is not None:
        trip.total_distance_km = Decimal(str(data.total_distance_km))
    if data.estimated_time:
        trip.estimated_time = data.estimated_time

    load = db.query(Load).filter(Load.id == trip.load_id).first()
    if load:
        load.status = LOAD_STATUS_IN_TRANSIT

    db.commit()
    db.refresh(trip)

    log_trip_activity(
        db,
        trip,
        event_type=TRIP_STATUS_STARTED,
        title="Viagem de Entrega Iniciada",
        description="Motorista iniciou o transporte da carga em direção ao destino.",
        latitude=driver.current_lat,
        longitude=driver.current_lng,
    )

    _emit_trip_status_changed(
        db,
        trip,
        title="Viagem de entrega iniciada",
        body="O motorista iniciou a viagem de entrega da carga.",
        notification_type="trip.started",
    )
    return get_trip_detail(db, trip.id)


def arrive_trip(db: Session, user: User, trip_id: int) -> Trip:
    """Motorista confirma chegada ao destino."""
    if user.user_type != "motorista":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Apenas motoristas podem confirmar chegada",
        )

    trip = get_trip_detail(db, trip_id)
    driver = db.query(Driver).filter(Driver.user_id == user.id).first()
    if driver is None or trip.driver_id != driver.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Viagem de outro motorista")

    if trip.status != TRIP_STATUS_STARTED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Viagem deve estar em curso para confirmar chegada",
        )

    delivery_evidence = (
        db.query(TripEvidenceStage)
        .filter(
            TripEvidenceStage.trip_id == trip.id,
            TripEvidenceStage.stage == "delivery",
            TripEvidenceStage.finalized_at.isnot(None),
        )
        .first()
    )
    if delivery_evidence is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Finalize primeiro as provas de entrega: "
                "3 a 5 fotografias e o comprovativo POD."
            ),
        )

    trip.status = TRIP_STATUS_WAITING_CLIENT
    trip.arrived_at = datetime.now(timezone.utc)

    load = db.query(Load).filter(Load.id == trip.load_id).first()
    if load:
        load.status = LOAD_STATUS_WAITING_CLIENT

    # Para o motorista, o trabalho termina na entrega. A confirmação posterior
    # do cliente mantém o acerto comercial, mas não bloqueia uma nova carga.
    driver.available = True
    if trip.vehicle is not None:
        trip.vehicle.status = VEHICLE_STATUS_AVAILABLE

    db.commit()
    db.refresh(trip)

    log_trip_activity(
        db,
        trip,
        event_type=TRIP_STATUS_WAITING_CLIENT,
        title="Chegou ao Destino",
        description="Motorista chegou ao local de entrega e aguarda confirmação do cliente.",
        latitude=driver.current_lat,
        longitude=driver.current_lng,
    )

    _emit_trip_status_changed(
        db,
        trip,
        title="Motorista chegou ao destino",
        body="A carga chegou ao destino. Aguarda confirmação do cliente.",
        notification_type="trip.arrived",
    )

    from controllers.payment_plan_controller import (
        start_payment_deadline_for_trip_event,
    )
    start_payment_deadline_for_trip_event(
        db,
        trip.id,
        "unloading",
    )
    return get_trip_detail(db, trip.id)


def confirm_delivery(db: Session, user: User, trip_id: int) -> Trip:
    """Cliente confirma entrega concluída."""
    if user.user_type != "cliente":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Apenas clientes podem confirmar entrega",
        )

    trip = get_trip_detail(db, trip_id)
    client = db.query(Client).filter(Client.user_id == user.id).first()
    load = db.query(Load).filter(Load.id == trip.load_id).first()
    if client is None or load is None or load.client_id != client.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Viagem de outro cliente")

    if trip.status != TRIP_STATUS_WAITING_CLIENT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Aguarde confirmação de chegada do motorista",
        )

    now = datetime.now(timezone.utc)
    trip.status = TRIP_STATUS_COMPLETED
    trip.client_confirmed_at = now
    trip.completed_at = now
    load.status = LOAD_STATUS_COMPLETED

    if trip.driver_id:
        driver = db.query(Driver).filter(Driver.id == trip.driver_id).first()
        if driver:
            driver.total_trips += 1
    if trip.company_id:
        company = db.query(Company).filter(Company.id == trip.company_id).first()
        if company:
            company.total_trips += 1

    db.commit()
    db.refresh(trip)

    log_trip_activity(
        db,
        trip,
        event_type=TRIP_STATUS_COMPLETED,
        title="Entrega Concluída",
        description="A entrega foi confirmada pelo cliente.",
    )

    _emit_trip_status_changed(
        db,
        trip,
        title="Entrega confirmada",
        body="A entrega foi confirmada pelo cliente.",
        notification_type="trip.completed",
    )
    # Wallet: liberta o valor retido após confirmação do cliente.
    release_transport_escrow_for_trip(db, trip)
    return get_trip_detail(db, trip.id)


def _record_gps_point(
    db: Session,
    trip: Trip,
    driver: Driver,
    data: TripLocationCreateRequest,
) -> TripLocation:
    """Lógica partilhada: atualiza posicao ao vivo e persiste ponto GPS se necessario."""
    if data.traveled_distance_km is not None:
        trip.traveled_distance_km = Decimal(str(data.traveled_distance_km))

    latitude = Decimal(str(data.latitude))
    longitude = Decimal(str(data.longitude))
    _sync_live_location(trip, driver, latitude, longitude)

    last_location = _latest_trip_location(db, trip.id)
    if not _should_store_trip_location(last_location, latitude, longitude, trip.status):
        db.commit()
        return last_location

    location = TripLocation(
        trip_id=trip.id,
        latitude=latitude,
        longitude=longitude,
        speed=Decimal(str(data.speed)) if data.speed is not None else None,
        phase=trip.status,
    )
    db.add(location)
    db.commit()
    db.refresh(location)
    return location


def add_trip_location(
    db: Session, user: User, trip_id: int, data: TripLocationCreateRequest
) -> TripLocation:
    """Regista ponto GPS durante a viagem."""
    if user.user_type != "motorista":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Apenas motoristas enviam localização",
        )

    trip = get_trip_detail(db, trip_id)
    driver = db.query(Driver).filter(Driver.user_id == user.id).first()
    if driver is None or trip.driver_id != driver.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Viagem de outro motorista")

    if trip.status not in (
        TRIP_STATUS_EN_ROUTE_PICKUP,
        TRIP_STATUS_ARRIVED_PICKUP,
        TRIP_STATUS_LOADED,
        TRIP_STATUS_STARTED,
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Localização só durante deslocamento ou viagem em curso",
        )

    return _record_gps_point(db, trip, driver, data)


def list_trip_locations(db: Session, user: User, trip_id: int) -> list[TripLocation]:
    """Lista pontos GPS da viagem."""
    trip = get_trip_detail(db, trip_id)
    user_can_access_trip(db, user, trip)
    return (
        db.query(TripLocation)
        .filter(TripLocation.trip_id == trip_id)
        .order_by(TripLocation.created_at.asc())
        .all()
    )
