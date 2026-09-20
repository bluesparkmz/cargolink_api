"""
Controller de cargas: publicar, listar, imagens e propostas.
"""

import math
import shutil
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from config import settings
from constants import (
    BLOCKED_CONTENT_TYPES,
    BLOCKED_FILE_EXTENSIONS,
    LOAD_ACTIVE_STATUSES,
    LOAD_FILL_IDS,
    LOAD_FILL_LABELS,
    LOAD_STATUS_ACCEPTED,
    LOAD_STATUS_AVAILABLE,
    LOAD_STATUS_IN_TRANSIT,
    LOAD_TYPE_IDS,
    LOAD_TYPE_LABELS,
    MAX_LOAD_IMAGES,
    NEGOTIATION_STATUS_PENDING,
    NEGOTIATION_STATUS_REJECTED,
    PROPOSAL_OPEN_STATUSES,
    PROPOSAL_STATUS_ACCEPTED,
    PROPOSAL_STATUS_REJECTED,
    ROUTE_AVG_SPEED_KMH_MAX,
    ROUTE_AVG_SPEED_KMH_MIN,
    TRIP_STATUS_ARRIVED_PICKUP,
    TRIP_STATUS_EN_ROUTE_PICKUP,
    TRIP_STATUS_LOADED,
    TRIP_STATUS_STARTED,
    TRIP_STATUS_WAITING_CLIENT,
    WEIGHT_UNITS,
)
from controllers.trips_controller import _user_can_access_trip, log_trip_activity
from controllers.notifications_controller import create_notification, emit_notification
from controllers.realtime_events import emit_to_rooms
from models.models import (
    Client,
    Company,
    Driver,
    Load,
    LoadImage,
    LoadProposal,
    ProposalNegotiation,
    Rating,
    Trip,
    TripLocation,
    User,
    Vehicle,
)
from schemas.schemas import (
    LoadCreateRequest,
    LoadCreateRequestForm,
    LoadDetailResponse,
    LoadImageCreateRequest,
    LoadImageResponse,
    LoadProposalCreateRequest,
    LoadRouteEstimate,
    LoadSenderSummary,
    LoadUpdateRequest,
)

ALLOWED_LOAD_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png"}
LOAD_UPLOAD_DIR = "uploads/loads"


def _validate_upload_file_security(file: UploadFile, allowed_types: dict) -> None:
    """Valida segurança do ficheiro: bloqueia tipos executáveis e perigosos."""
    # Verificar MIME type bloqueado
    content_type = (file.content_type or "").lower()
    if content_type in BLOCKED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Tipo de ficheiro não permitido: {content_type}",
        )
    
    # Verificar extensão bloqueada
    if file.filename:
        filename_lower = file.filename.lower()
        for ext in BLOCKED_FILE_EXTENSIONS:
            if filename_lower.endswith(ext):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Extensão de ficheiro não permitida: {ext}",
                )


def _generate_load_code() -> str:
    """Gera código único para a carga."""
    return f"CL-{uuid.uuid4().hex[:8].upper()}"


def _save_load_image(file: UploadFile) -> str:
    """Guarda imagem da carga no volume persistente e devolve URL pública."""
    # Validar segurança do ficheiro
    _validate_upload_file_security(file, ALLOWED_LOAD_IMAGE_TYPES)
    
    extension = ALLOWED_LOAD_IMAGE_TYPES.get(file.content_type or "")
    if not extension:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Imagem invÃ¡lida. Envie apenas ficheiros JPG ou PNG.",
        )

    upload_dir = Path(settings.STORAGE_DIR) / LOAD_UPLOAD_DIR
    upload_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{uuid.uuid4().hex}{extension}"
    file_path = upload_dir / filename

    file.file.seek(0)
    with file_path.open("wb") as destination:
        shutil.copyfileobj(file.file, destination)

    return f"/{LOAD_UPLOAD_DIR}/{filename}"


def normalize_load_type(load_type: str) -> str:
    """Converte aliases legados para o id canonico do catalogo."""
    from constants import LOAD_TYPE_ALIASES

    return LOAD_TYPE_ALIASES.get(load_type.strip(), load_type.strip())


def _validate_load_type(load_type: str) -> str:
    """Valida tipo de carga contra catálogo do app e devolve o id canonico."""
    normalized = normalize_load_type(load_type)
    if normalized not in LOAD_TYPE_IDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Tipo de carga inválido. Use um de: {', '.join(sorted(LOAD_TYPE_IDS))}",
        )
    return normalized


def _validate_weight_unit(weight_unit: str | None) -> None:
    """Valida unidade de peso."""
    if weight_unit and weight_unit not in WEIGHT_UNITS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unidade de peso inválida. Use: {', '.join(WEIGHT_UNITS)}",
        )


def _validate_load_fill(load_fill: str | None) -> None:
    """Valida tipo completa / meia carga."""
    if load_fill and load_fill not in LOAD_FILL_IDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Tipo de volume inválido. Use: {', '.join(sorted(LOAD_FILL_IDS))}",
        )


PAYMENT_MODES = {
    "integral",
    "parcelas_50_50",
    "prazo_apos_descarga",
    "prazo_desde_carregamento",
}
DEFERRED_PAYMENT_MODES = {
    "prazo_apos_descarga",
    "prazo_desde_carregamento",
}


def _validate_payment_terms(
    payment_mode: str,
    payment_term_days: int | None,
) -> int | None:
    if payment_mode not in PAYMENT_MODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Modalidade de pagamento inválida. Use integral, "
                "parcelas_50_50, prazo_apos_descarga ou "
                "prazo_desde_carregamento."
            ),
        )

    if payment_mode in DEFERRED_PAYMENT_MODES:
        days = int(payment_term_days or 0)
        if days < 1 or days > 365:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Informe o prazo entre 1 e 365 dias.",
            )
        return days

    return None


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Distância em linha recta entre dois pontos (aproximação de rota)."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


def _estimate_route(load: Load, trip: Trip | None) -> LoadRouteEstimate | None:
    """Calcula distância e tempo para a secção Rota do app."""
    distance_km: float | None = None

    if trip and trip.total_distance_km is not None:
        distance_km = float(trip.total_distance_km)
    elif (
        load.origin_lat is not None
        and load.origin_lng is not None
        and load.destination_lat is not None
        and load.destination_lng is not None
    ):
        straight = _haversine_km(
            float(load.origin_lat),
            float(load.origin_lng),
            float(load.destination_lat),
            float(load.destination_lng),
        )
        distance_km = round(straight * 1.15, 1)

    if distance_km is None:
        return None

    if trip and trip.estimated_time:
        return LoadRouteEstimate(
            distance_km=distance_km,
            estimated_time_label=trip.estimated_time,
        )

    hours_min = distance_km / ROUTE_AVG_SPEED_KMH_MAX
    hours_max = distance_km / ROUTE_AVG_SPEED_KMH_MIN
    h_min, m_min = int(hours_min), int((hours_min % 1) * 60)
    h_max, m_max = int(hours_max), int((hours_max % 1) * 60)
    if m_min >= 30:
        h_min += 1
    if m_max >= 30:
        h_max += 1
    label = f"{h_min}h" if h_min == h_max else f"{h_min}h - {h_max}h"
    avg_minutes = int((hours_min + hours_max) / 2 * 60)

    return LoadRouteEstimate(
        distance_km=distance_km,
        estimated_time_min=avg_minutes,
        estimated_time_label=label,
    )


def _client_ratings(db: Session, user_id: int) -> tuple[float | None, int]:
    """Média e total de avaliações recebidas pelo cliente."""
    avg, count = (
        db.query(func.avg(Rating.score), func.count(Rating.id))
        .filter(Rating.rated_user_id == user_id, Rating.score.isnot(None))
        .first()
    ) or (None, 0)
    return (round(float(avg), 1) if avg is not None else None, int(count or 0))


def build_load_detail_response(db: Session, load: Load) -> LoadDetailResponse:
    """Monta resposta completa do ecrã Detalhes da Carga."""
    if not load.client or not load.client.user:
        client = (
            db.query(Client)
            .options(joinedload(Client.user))
            .filter(Client.id == load.client_id)
            .first()
        )
        load.client = client

    user = load.client.user
    avg_rating, rating_count = _client_ratings(db, user.id)
    trip = db.query(Trip).filter(Trip.load_id == load.id).first()
    proposals_count = (
        db.query(func.count(LoadProposal.id)).filter(LoadProposal.load_id == load.id).scalar() or 0
    )

    return LoadDetailResponse(
        id=load.id,
        client_id=load.client_id,
        code=load.code,
        load_type=load.load_type,
        load_name=load.load_name,
        description=load.description,
        weight=float(load.weight) if load.weight is not None else None,
        weight_unit=load.weight_unit,
        volume=float(load.volume) if load.volume is not None else None,
        value=float(load.value) if load.value is not None else None,
        negotiable=load.negotiable,
        origin=load.origin,
        destination=load.destination,
        origin_lat=float(load.origin_lat) if load.origin_lat is not None else None,
        origin_lng=float(load.origin_lng) if load.origin_lng is not None else None,
        destination_lat=float(load.destination_lat) if load.destination_lat is not None else None,
        destination_lng=float(load.destination_lng) if load.destination_lng is not None else None,
        departure_date=load.departure_date,
        load_fill=load.load_fill,
        suggested_vehicle_type=load.suggested_vehicle_type,
        instructions=load.instructions,
        payment_mode=load.payment_mode or "integral",
        payment_term_days=load.payment_term_days,
        status=load.status,
        created_at=load.created_at,
        updated_at=load.updated_at,
        images=[LoadImageResponse.model_validate(img) for img in load.images],
        load_type_label=LOAD_TYPE_LABELS.get(load.load_type, load.load_type),
        load_fill_label=LOAD_FILL_LABELS.get(load.load_fill) if load.load_fill else None,
        sender=LoadSenderSummary(
            client_id=load.client_id,
            user_id=user.id,
            name=user.name,
            phone=user.phone,
            email=user.email,
            profile_photo=user.profile_photo,
            verified=user.verified,
            average_rating=avg_rating,
            rating_count=rating_count,
        ),
        route=_estimate_route(load, trip),
        proposals_count=proposals_count,
    )


def _user_has_load_relation(db: Session, user: User, load: Load) -> bool:
    """Verifica se o utilizador tem relação com a carga (cliente, empresa ou motorista)."""
    if user.user_type == "admin":
        return True

    if user.user_type == "cliente":
        client = db.query(Client).filter(Client.user_id == user.id).first()
        return client is not None and load.client_id == client.id

    trip = db.query(Trip).filter(Trip.load_id == load.id).first()

    if user.user_type == "motorista":
        driver = db.query(Driver).filter(Driver.user_id == user.id).first()
        if not driver:
            return False
        if trip and trip.driver_id == driver.id:
            return True
        return (
            db.query(LoadProposal)
            .filter(LoadProposal.load_id == load.id, LoadProposal.driver_id == driver.id)
            .first()
            is not None
        )

    if user.user_type == "empresa":
        company = db.query(Company).filter(Company.user_id == user.id).first()
        if not company:
            return False
        if trip and trip.company_id == company.id:
            return True
        return (
            db.query(LoadProposal)
            .filter(LoadProposal.load_id == load.id, LoadProposal.company_id == company.id)
            .first()
            is not None
        )

    return False


def _ensure_can_view_load(db: Session, user: User, load: Load) -> None:
    """Permite visualização dos detalhes de qualquer carga pública para utilizadores autenticados."""
    return


def _related_load_ids_subquery(db: Session, user: User):
    """IDs de cargas ligadas ao utilizador autenticado."""
    if user.user_type == "cliente":
        client = db.query(Client).filter(Client.user_id == user.id).first()
        if not client:
            return None
        return db.query(Load.id).filter(Load.client_id == client.id)

    if user.user_type == "empresa":
        company = db.query(Company).filter(Company.user_id == user.id).first()
        if not company:
            return None
        trip_load_ids = db.query(Trip.load_id).filter(Trip.company_id == company.id)
        proposal_load_ids = db.query(LoadProposal.load_id).filter(
            LoadProposal.company_id == company.id
        )
        return db.query(Load.id).filter(
            Load.id.in_(trip_load_ids) | Load.id.in_(proposal_load_ids)
        )

    if user.user_type == "motorista":
        driver = db.query(Driver).filter(Driver.user_id == user.id).first()
        if not driver:
            return None
        trip_load_ids = db.query(Trip.load_id).filter(Trip.driver_id == driver.id)
        proposal_load_ids = db.query(LoadProposal.load_id).filter(
            LoadProposal.driver_id == driver.id
        )
        return db.query(Load.id).filter(
            Load.id.in_(trip_load_ids) | Load.id.in_(proposal_load_ids)
        )

    return None


def list_related_loads(
    db: Session,
    user: User,
    *,
    status_filter: str | None = None,
) -> list[Load]:
    """Lista cargas ligadas ao utilizador (cliente, empresa ou motorista)."""
    related_ids = _related_load_ids_subquery(db, user)
    if related_ids is None:
        return []

    query = db.query(Load).filter(Load.id.in_(related_ids))
    if status_filter:
        query = query.filter(Load.status == status_filter)
    return query.order_by(Load.created_at.desc()).all()


def get_load_detail_response(db: Session, load_id: int, user: User | None = None) -> LoadDetailResponse:
    """Detalhe completo da carga para a API."""
    load = get_load_detail(db, load_id)
    if user is not None:
        _ensure_can_view_load(db, user, load)
    return build_load_detail_response(db, load)


def get_client_or_403(db: Session, user: User) -> Client:
    """Obtém perfil cliente do utilizador autenticado."""
    if user.user_type != "cliente":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Apenas clientes podem gerir cargas",
        )
    client = db.query(Client).filter(Client.user_id == user.id).first()
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Perfil cliente não encontrado",
        )
    return client


def get_driver_or_403(db: Session, user: User) -> Driver:
    """Obtém perfil motorista do utilizador autenticado."""
    if user.user_type != "motorista":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Apenas motoristas podem enviar propostas",
        )
    driver = db.query(Driver).filter(Driver.user_id == user.id).first()
    if driver is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Perfil motorista não encontrado",
        )
    return driver


def get_company_or_403(db: Session, user: User) -> Company:
    """Obtém perfil da empresa transportadora autenticada."""
    if user.user_type != "empresa":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Apenas empresas transportadoras podem enviar propostas",
        )
    company = db.query(Company).filter(Company.user_id == user.id).first()
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Perfil de empresa nÃ£o encontrado",
        )
    return company


def _validate_company_trip_resources(
    db: Session, company: Company, driver_id: int | None, vehicle_id: int | None
) -> None:
    if driver_id is None or vehicle_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Informe o motorista e o camiÃ£o da empresa para esta proposta",
        )

    driver = db.query(Driver).filter(Driver.id == driver_id).first()
    if driver is None or driver.company_id != company.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Motorista invÃ¡lido para esta empresa",
        )

    vehicle = db.query(Vehicle).filter(Vehicle.id == vehicle_id).first()
    if vehicle is None or vehicle.company_id != company.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="CamiÃ£o invÃ¡lido para esta empresa",
        )
    if vehicle.driver_id is not None and vehicle.driver_id != driver.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Este camiÃ£o estÃ¡ atribuido a outro motorista",
        )


def get_load_detail(db: Session, load_id: int) -> Load:
    """Busca carga com imagens e cliente."""
    load = (
        db.query(Load)
        .options(
            joinedload(Load.images),
            joinedload(Load.client).joinedload(Client.user),
        )
        .filter(Load.id == load_id)
        .first()
    )
    if load is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Carga não encontrada")
    return load


def _attach_images(db: Session, load: Load, images: list[LoadImageCreateRequest]) -> None:
    """Associa imagens à carga (máximo 5)."""
    if len(images) > MAX_LOAD_IMAGES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Máximo de {MAX_LOAD_IMAGES} imagens por carga",
        )

    has_primary = any(img.is_primary for img in images)
    for index, img_data in enumerate(images):
        is_primary = img_data.is_primary or (index == 0 and not has_primary)
        if is_primary:
            for existing in load.images:
                existing.is_primary = False
        db.add(
            LoadImage(
                load_id=load.id,
                image_url=img_data.image_url,
                is_primary=is_primary,
            )
        )


def create_load(db: Session, user: User, data: LoadCreateRequest) -> LoadDetailResponse:
    """Cliente publica nova carga (com imagens opcionais no mesmo pedido)."""
    client = get_client_or_403(db, user)
    data.load_type = _validate_load_type(data.load_type)
    _validate_weight_unit(data.weight_unit)
    _validate_load_fill(data.load_fill)
    data.payment_term_days = _validate_payment_terms(
        data.payment_mode,
        data.payment_term_days,
    )

    images = data.images or []
    if len(images) > MAX_LOAD_IMAGES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Máximo de {MAX_LOAD_IMAGES} imagens por carga",
        )

    payload = data.model_dump(exclude={"images"})
    load = Load(
        client_id=client.id,
        code=_generate_load_code(),
        **payload,
    )
    db.add(load)
    db.flush()

    if images:
        _attach_images(db, load, images)

    db.commit()
    return get_load_detail_response(db, load.id)


def create_load_with_files(
    db: Session,
    user: User,
    data: LoadCreateRequestForm,
    image_files: list[UploadFile] | None = None,
) -> LoadDetailResponse:
    """Cliente publica nova carga via multipart/form-data com files de imagens."""
    client = get_client_or_403(db, user)
    data.load_type = _validate_load_type(data.load_type)
    _validate_weight_unit(data.weight_unit)
    _validate_load_fill(data.load_fill)
    data.payment_term_days = _validate_payment_terms(
        data.payment_mode,
        data.payment_term_days,
    )

    if image_files and len(image_files) > MAX_LOAD_IMAGES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Máximo de {MAX_LOAD_IMAGES} imagens por carga",
        )

    payload = data.model_dump()
    load = Load(
        client_id=client.id,
        code=_generate_load_code(),
        **payload,
    )
    db.add(load)
    db.flush()

    if image_files:
        images = []
        for idx, file in enumerate(image_files):
            image_url = _save_load_image(file)
            image_obj = LoadImage(
                load_id=load.id,
                image_url=image_url,
                is_primary=(idx == 0),
            )
            images.append(image_obj)
        db.add_all(images)

    db.commit()
    return get_load_detail_response(db, load.id)


def list_available_loads(
    db: Session,
    user: User,
    *,
    status_filter: str | None = None,
    load_type: str | None = None,
    origin: str | None = None,
    destination: str | None = None,
    payment_mode: str | None = None,
    q: str | None = None,
    departure_date_from: date | None = None,
    departure_date_to: date | None = None,
    limit: int | None = None,
) -> list[Load]:
    """Lista cargas públicas (disponíveis, em trânsito, etc.) para qualquer utilizador autenticado."""
    query = db.query(Load)

    if status_filter:
        query = query.filter(Load.status == status_filter)

    if load_type:
        query = query.filter(Load.load_type == load_type)
    if origin:
        query = query.filter(Load.origin.ilike(f"%{origin}%"))
    if destination:
        query = query.filter(Load.destination.ilike(f"%{destination}%"))
    if payment_mode:
        query = query.filter(Load.payment_mode == payment_mode)
    if q:
        pattern = f"%{q}%"
        query = query.filter(
            (Load.origin.ilike(pattern))
            | (Load.destination.ilike(pattern))
            | (Load.load_name.ilike(pattern))
            | (Load.code.ilike(pattern))
        )
    if departure_date_from:
        query = query.filter(Load.departure_date >= departure_date_from)
    if departure_date_to:
        query = query.filter(Load.departure_date <= departure_date_to)

    query = query.order_by(Load.created_at.desc())

    # Se limit for passado ou se a consulta for sem filtro de status, limita a 20 por padrão
    effective_limit = limit if limit is not None else (20 if status_filter is None else None)
    if effective_limit:
        query = query.limit(effective_limit)

    return query.all()


def get_load_tracking(db: Session, user: User, load_id: int) -> dict:
    """Rastreio GPS da carga via viagem associada."""
    load = get_load_detail(db, load_id)
    trip = db.query(Trip).filter(Trip.load_id == load_id).first()

    if user.user_type == "cliente":
        client = db.query(Client).filter(Client.user_id == user.id).first()
        if not client or load.client_id != client.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sem acesso")
    elif user.user_type == "motorista":
        driver = db.query(Driver).filter(Driver.user_id == user.id).first()
        if not driver:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sem acesso")
        allowed = trip is not None and trip.driver_id == driver.id
        if not allowed:
            allowed = (
                db.query(LoadProposal)
                .filter(LoadProposal.load_id == load_id, LoadProposal.driver_id == driver.id)
                .first()
                is not None
            )
        if not allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sem acesso")
    elif user.user_type == "empresa":
        company = db.query(Company).filter(Company.user_id == user.id).first()
        if not company:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sem acesso")
        allowed = trip is not None and trip.company_id == company.id
        if not allowed:
            allowed = (
                db.query(LoadProposal)
                .filter(LoadProposal.load_id == load_id, LoadProposal.company_id == company.id)
                .first()
                is not None
            )
        if not allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sem acesso")
    elif user.user_type != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sem acesso")
    trackable = trip is not None and trip.status in (
        TRIP_STATUS_EN_ROUTE_PICKUP,
        TRIP_STATUS_ARRIVED_PICKUP,
        TRIP_STATUS_LOADED,
        TRIP_STATUS_STARTED,
        TRIP_STATUS_WAITING_CLIENT,
    )
    locations: list[TripLocation] = []
    last_location = None

    if trip:
        _user_can_access_trip(db, user, trip)
        locations = (
            db.query(TripLocation)
            .filter(TripLocation.trip_id == trip.id)
            .order_by(TripLocation.created_at.asc())
            .all()
        )
        if locations:
            last_location = locations[-1]

    return {
        "load_id": load.id,
        "load_code": load.code,
        "load_status": load.status,
        "trip_id": trip.id if trip else None,
        "trip_status": trip.status if trip else None,
        "trackable": trackable,
        "locations": locations,
        "last_location": last_location,
    }


def list_my_loads(db: Session, user: User) -> list[Load]:
    """Lista cargas ligadas ao utilizador autenticado (cliente, empresa ou motorista)."""
    return list_related_loads(db, user)


def update_load(db: Session, user: User, load_id: int, data: LoadUpdateRequest) -> LoadDetailResponse:
    """Cliente atualiza a própria carga."""
    client = get_client_or_403(db, user)
    load = get_load_detail(db, load_id)
    if load.client_id != client.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Carga de outro cliente")

    fields = data.model_dump(exclude_unset=True)
    if "load_type" in fields:
        fields["load_type"] = _validate_load_type(fields["load_type"])
    if "weight_unit" in fields:
        _validate_weight_unit(fields.get("weight_unit"))
    if "load_fill" in fields:
        _validate_load_fill(fields.get("load_fill"))

    if "payment_mode" in fields or "payment_term_days" in fields:
        if load.status != LOAD_STATUS_AVAILABLE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "A modalidade de pagamento não pode ser alterada "
                    "depois da carga deixar de estar disponível."
                ),
            )
        next_mode = fields.get(
            "payment_mode",
            load.payment_mode or "integral",
        )
        next_days = fields.get(
            "payment_term_days",
            load.payment_term_days,
        )
        fields["payment_term_days"] = _validate_payment_terms(
            next_mode,
            next_days,
        )

    for field, value in fields.items():
        setattr(load, field, value)

    db.commit()
    return get_load_detail_response(db, load_id)


def delete_load(db: Session, user: User, load_id: int) -> None:
    client = get_client_or_403(db, user)
    load = get_load_detail(db, load_id)
    if load.client_id != client.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Carga de outro cliente",
        )

    trip = db.query(Trip).filter(Trip.load_id == load.id).first()
    if trip is not None and trip.status not in {"cancelada", "cancelado"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Esta carga já possui um transporte aceite. "
                "Use o cancelamento do transporte para garantir o reembolso."
            ),
        )

    if load.status != LOAD_STATUS_AVAILABLE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Use o cancelamento do transporte para esta carga.",
        )

    pending = (
        db.query(LoadProposal)
        .filter(
            LoadProposal.load_id == load.id,
            LoadProposal.status.in_(["pendente", "em_negociacao"]),
        )
        .all()
    )
    for proposal in pending:
        proposal.status = "cancelada"

    if pending:
        (
            db.query(ProposalNegotiation)
            .filter(
                ProposalNegotiation.proposal_id.in_([p.id for p in pending]),
                ProposalNegotiation.status == NEGOTIATION_STATUS_PENDING,
            )
            .update({"status": "cancelada"}, synchronize_session=False)
        )

    load.status = "cancelada"
    db.commit()

def add_load_image(
    db: Session, user: User, load_id: int, data: LoadImageCreateRequest
) -> LoadImage:
    """Cliente adiciona imagem à carga (respeita limite de 5)."""
    client = get_client_or_403(db, user)
    load = get_load_detail(db, load_id)
    if load.client_id != client.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Carga de outro cliente")

    if len(load.images) >= MAX_LOAD_IMAGES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Máximo de {MAX_LOAD_IMAGES} imagens por carga",
        )

    if data.is_primary:
        for img in load.images:
            img.is_primary = False

    image = LoadImage(load_id=load.id, image_url=data.image_url, is_primary=data.is_primary)
    db.add(image)
    db.commit()
    db.refresh(image)
    return image


def create_proposal(
    db: Session, user: User, load_id: int, data: LoadProposalCreateRequest
) -> LoadProposal:
    """Motorista envia proposta para carga disponível."""
    company = get_company_or_403(db, user)
    _validate_company_trip_resources(db, company, data.driver_id, data.vehicle_id)
    load = get_load_detail(db, load_id)

    if load.status != LOAD_STATUS_AVAILABLE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Carga não está disponível para propostas",
        )

    exists = (
        db.query(LoadProposal)
        .filter(
            LoadProposal.load_id == load_id,
            LoadProposal.company_id == company.id,
            ~LoadProposal.status.in_(["recusada", "cancelada"]),
        )
        .first()
    )
    if exists:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Já enviou proposta para esta carga",
        )

    if data.proposed_value is not None and load.value is not None:
        proposed = Decimal(str(data.proposed_value))
        client_value = Decimal(str(load.value))
        if proposed < client_value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"A proposta deve ser pelo menos {client_value:.2f} MT "
                    f"(valor indicado pelo dono da carga)."
                ),
            )

    proposal = LoadProposal(
        load_id=load_id,
        company_id=company.id,
        driver_id=data.driver_id,
        vehicle_id=data.vehicle_id,
        proposed_value=Decimal(str(data.proposed_value)) if data.proposed_value else None,
        message=data.message,
    )
    db.add(proposal)
    client = db.query(Client).filter(Client.id == load.client_id).first()
    notification = None
    if client:
        notification = create_notification(
            db,
            user_id=client.user_id,
            title="Nova proposta recebida",
            body=f"{company.company_name} enviou uma proposta para a carga {load.code}.",
            notification_type="proposal.created",
            payload={"load_id": load.id, "proposal_id": None, "company_id": company.id},
        )
    db.commit()
    db.refresh(proposal)
    if notification:
        notification.payload = {
            "load_id": load.id,
            "proposal_id": proposal.id,
            "company_id": company.id,
        }
        db.commit()
        db.refresh(notification)
        emit_notification(notification)
    emit_to_rooms(
        {f"load:{load.id}", f"client:{load.client_id}", f"company:{company.id}"},
        {
            "type": "proposal.created",
            "load_id": load.id,
            "proposal_id": proposal.id,
            "company_id": company.id,
            "status": proposal.status,
        },
    )
    return proposal


def list_load_proposals(db: Session, user: User, load_id: int) -> list[LoadProposal]:
    """Cliente vê propostas da sua carga."""
    client = get_client_or_403(db, user)
    load = get_load_detail(db, load_id)
    if load.client_id != client.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Carga de outro cliente")

    return (
        db.query(LoadProposal)
        .filter(LoadProposal.load_id == load_id)
        .order_by(LoadProposal.created_at.desc())
        .all()
    )


def accept_proposal(db: Session, user: User, load_id: int, proposal_id: int) -> Trip:
    """Cliente aceita proposta e cria viagem."""
    client = get_client_or_403(db, user)
    load = get_load_detail(db, load_id)
    if load.client_id != client.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Carga de outro cliente")

    if load.status != LOAD_STATUS_AVAILABLE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Carga já não está disponível",
        )

    proposal = (
        db.query(LoadProposal)
        .filter(LoadProposal.id == proposal_id, LoadProposal.load_id == load_id)
        .first()
    )
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposta não encontrada")
    if proposal.status not in PROPOSAL_OPEN_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Proposta já foi processada",
        )

    existing_trip = db.query(Trip).filter(Trip.load_id == load_id).first()
    if existing_trip and existing_trip.status not in {"cancelada", "cancelado"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Esta carga já tem viagem associada",
        )

    proposal.status = PROPOSAL_STATUS_ACCEPTED
    (
        db.query(ProposalNegotiation)
        .filter(
            ProposalNegotiation.proposal_id == proposal.id,
            ProposalNegotiation.status == NEGOTIATION_STATUS_PENDING,
        )
        .update({"status": NEGOTIATION_STATUS_REJECTED}, synchronize_session=False)
    )
    other_proposals = (
        db.query(LoadProposal)
        .filter(
            LoadProposal.load_id == load_id,
            LoadProposal.id != proposal_id,
            LoadProposal.status.in_(PROPOSAL_OPEN_STATUSES),
        )
        .all()
    )
    for other in other_proposals:
        other.status = PROPOSAL_STATUS_REJECTED
    if other_proposals:
        (
            db.query(ProposalNegotiation)
            .filter(
                ProposalNegotiation.proposal_id.in_([item.id for item in other_proposals]),
                ProposalNegotiation.status == NEGOTIATION_STATUS_PENDING,
            )
            .update({"status": NEGOTIATION_STATUS_REJECTED}, synchronize_session=False)
        )

    load.status = LOAD_STATUS_ACCEPTED
    # A proposta aceite define apenas a empresa vencedora.
    # O camião e o motorista serão escolhidos manualmente pela empresa.
    if existing_trip is not None:
        # Trip.load_id é UNIQUE. Quando uma carga cancelada volta ao mercado,
        # reutilizamos o registo da viagem, mas limpamos dados operacionais
        # da viagem anterior para não misturar GPS, provas, paragens,
        # actividades e requisições de combustível com o novo contrato.
        from models.models import (
            FuelAdvance,
            TripActivity,
            TripEvidence,
            TripEvidenceStage,
            TripLocation,
            TripStop,
        )

        db.query(TripLocation).filter(
            TripLocation.trip_id == existing_trip.id
        ).delete(synchronize_session=False)
        db.query(TripEvidenceStage).filter(
            TripEvidenceStage.trip_id == existing_trip.id
        ).delete(synchronize_session=False)
        db.query(TripEvidence).filter(
            TripEvidence.trip_id == existing_trip.id
        ).delete(synchronize_session=False)
        db.query(TripStop).filter(
            TripStop.trip_id == existing_trip.id
        ).delete(synchronize_session=False)
        db.query(FuelAdvance).filter(
            FuelAdvance.trip_id == existing_trip.id
        ).delete(synchronize_session=False)
        db.query(TripActivity).filter(
            TripActivity.trip_id == existing_trip.id
        ).delete(synchronize_session=False)

        trip = existing_trip
        trip.company_id = proposal.company_id
        trip.driver_id = None
        trip.vehicle_id = None
        trip.status = "aguardando_inicio"
        trip.en_route_pickup_at = None
        trip.arrived_pickup_at = None
        trip.loaded_at = None
        trip.started_at = None
        trip.arrived_at = None
        trip.client_confirmed_at = None
        trip.completed_at = None
        trip.total_distance_km = None
        trip.traveled_distance_km = None
        trip.estimated_time = None
        trip.pickup_distance_km = None
        trip.pickup_estimated_time = None
    else:
        trip = Trip(
            load_id=load_id,
            company_id=proposal.company_id,
            driver_id=None,
            vehicle_id=None,
        )
        db.add(trip)

    db.flush()

    from controllers.payment_plan_controller import (
        ensure_payment_plan_for_proposal,
    )

    ensure_payment_plan_for_proposal(
        db,
        proposal,
        trip=trip,
    )

    db.commit()
    db.refresh(trip)

    log_trip_activity(
        db,
        trip,
        event_type="proposta_aceita",
        title="Proposta Aceite",
        description=f"A proposta para a carga {load.code} foi aceite. Viagem criada.",
    )
    targets: set[int] = set()
    if proposal.company_id:
        company = db.query(Company).filter(Company.id == proposal.company_id).first()
        if company:
            targets.add(company.user_id)
    notifications = [
        create_notification(
            db,
            user_id=user_id,
            title="Proposta aceite",
            body=f"A proposta para a carga {load.code} foi aceite. Atribua um camião para iniciar o transporte.",
            notification_type="proposal.accepted",
            payload={"load_id": load.id, "proposal_id": proposal.id, "trip_id": trip.id},
        )
        for user_id in targets
    ]
    db.commit()
    for notification in notifications:
        db.refresh(notification)
        emit_notification(notification)
    emit_to_rooms(
        {
            f"load:{load.id}",
            f"trip:{trip.id}",
            f"company:{proposal.company_id}" if proposal.company_id else "",
        }
        - {""},
        {
            "type": "proposal.accepted",
            "load_id": load.id,
            "proposal_id": proposal.id,
            "trip_id": trip.id,
            "status": proposal.status,
        },
    )
    return trip


def reject_proposal(db: Session, user: User, load_id: int, proposal_id: int) -> LoadProposal:
    """Cliente recusa proposta."""
    client = get_client_or_403(db, user)
    load = get_load_detail(db, load_id)
    if load.client_id != client.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Carga de outro cliente")

    proposal = (
        db.query(LoadProposal)
        .filter(LoadProposal.id == proposal_id, LoadProposal.load_id == load_id)
        .first()
    )
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposta não encontrada")
    if proposal.status not in PROPOSAL_OPEN_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Proposta já foi processada",
        )

    proposal.status = PROPOSAL_STATUS_REJECTED
    (
        db.query(ProposalNegotiation)
        .filter(
            ProposalNegotiation.proposal_id == proposal.id,
            ProposalNegotiation.status == NEGOTIATION_STATUS_PENDING,
        )
        .update({"status": NEGOTIATION_STATUS_REJECTED}, synchronize_session=False)
    )
    db.commit()
    db.refresh(proposal)
    targets: set[int] = set()
    if proposal.company_id:
        company = db.query(Company).filter(Company.id == proposal.company_id).first()
        if company:
            targets.add(company.user_id)
    notifications = [
        create_notification(
            db,
            user_id=user_id,
            title="Proposta recusada",
            body=f"A proposta para a carga {load.code} foi recusada.",
            notification_type="proposal.rejected",
            payload={"load_id": load.id, "proposal_id": proposal.id},
        )
        for user_id in targets
    ]
    db.commit()
    for notification in notifications:
        db.refresh(notification)
        emit_notification(notification)
    emit_to_rooms(
        {f"load:{load.id}", f"company:{proposal.company_id}" if proposal.company_id else ""}
        - {""},
        {
            "type": "proposal.rejected",
            "load_id": load.id,
            "proposal_id": proposal.id,
            "status": proposal.status,
        },
    )
    return proposal
