"""
Rotas de cargas: publicar, consultar, imagens e propostas.
"""

from fastapi import APIRouter, Depends, Query, File, UploadFile, Form
from sqlalchemy.orm import Session

from datetime import date
from typing import Annotated

from constants import LOAD_FILL_TYPES, LOAD_TYPES
from controllers.loads_controller import (
    accept_proposal,
    add_load_image,
    create_load,
    create_load_with_files,
    normalize_load_type,
    create_proposal,
    delete_load,
    get_load_detail_response,
    get_load_tracking,
    list_available_loads,
    list_load_proposals,
    list_my_loads,
    reject_proposal,
    update_load,
)
from deps import get_current_user
from database import get_db
from models.models import Load, User
from schemas.schemas import (
    LoadCreateRequest,
    LoadCreateRequestForm,
    LoadDetailResponse,
    LoadImageCreateRequest,
    LoadImageResponse,
    LoadListItem,
    LoadTrackingResponse,
    LoadProposalCreateRequest,
    LoadProposalResponse,
    LoadFillTypeItem,
    LoadTypeItem,
    LoadUpdateRequest,
    TripResponse,
)

router = APIRouter()


def _to_list_item(load: Load) -> LoadListItem:
    """Monta resposta resumida com dados da carga."""
    return LoadListItem(
        id=load.id,
        code=load.code,
        load_type=load.load_type,
        load_name=load.load_name,
        origin=load.origin,
        destination=load.destination,
        origin_lat=load.origin_lat,
        origin_lng=load.origin_lng,
        destination_lat=load.destination_lat,
        destination_lng=load.destination_lng,
        weight=load.weight,
        weight_unit=load.weight_unit,
        value=load.value,
        negotiable=load.negotiable,
        status=load.status,
        departure_date=load.departure_date,
        created_at=load.created_at,
    )


@router.get("/types", response_model=list[LoadTypeItem])
def list_load_types():
    """Catálogo de tipos de carga (passo 1 do app)."""
    return [LoadTypeItem(**item) for item in LOAD_TYPES]


@router.get("/fill-types", response_model=list[LoadFillTypeItem])
def list_load_fill_types():
    """Catálogo carga completa / meia carga."""
    return [LoadFillTypeItem(**item) for item in LOAD_FILL_TYPES]


@router.get("/payment-modes")
def list_payment_modes():
    return [
        {
            "id": "integral",
            "label": "Pagamento integral",
            "requires_days": False,
        },
        {
            "id": "parcelas_50_50",
            "label": "50% na saída + 50% após confirmação da entrega",
            "requires_days": False,
        },
        {
            "id": "prazo_apos_descarga",
            "label": "Pagar X dias depois da descarga",
            "requires_days": True,
        },
        {
            "id": "prazo_desde_carregamento",
            "label": "Pagar durante X dias desde o carregamento",
            "requires_days": True,
        },
    ]


@router.post("", response_model=LoadDetailResponse, status_code=201)
def publish_load(
    load_type: str = Form("mercadoria_geral"),
    origin: str = Form("Maputo"),
    destination: str = Form("Beira"),
    load_name: str = Form("Carga de teste"),
    description: str = Form("Descrição padrão de teste"),
    weight: float = Form(150),
    weight_unit: str = Form("ton"),
    volume: float = Form(25),
    value: float = Form(500000),
    negotiable: bool = Form(True),
    origin_lat: float = Form(-23.8245),
    origin_lng: float = Form(35.3075),
    destination_lat: float = Form(-19.8432),
    destination_lng: float = Form(34.8386),
    departure_date: date = Form("2026-06-15"),
    load_fill: str = Form("completa"),
    suggested_vehicle_type: str = Form("Camião"),
    instructions: str = Form("Carga frágil - manusejar com cuidado"),
    payment_mode: str = Form("integral"),
    payment_term_days: int | None = Form(None),
    images: Annotated[
        list[UploadFile] | None,
        File(description="Até 5 imagens (jpg, png)"),
    ] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cliente publica carga com multipart/form-data (dropdowns + upload de imagens)."""
    form_data = LoadCreateRequestForm(
        load_type=normalize_load_type(load_type),
        load_name=load_name,
        description=description,
        weight=weight,
        weight_unit=weight_unit,
        volume=volume,
        value=value,
        negotiable=negotiable,
        origin=origin,
        destination=destination,
        origin_lat=origin_lat,
        origin_lng=origin_lng,
        destination_lat=destination_lat,
        destination_lng=destination_lng,
        departure_date=departure_date,
        load_fill=load_fill,
        suggested_vehicle_type=suggested_vehicle_type,
        instructions=instructions,
        payment_mode=payment_mode,
        payment_term_days=payment_term_days,
    )

    return create_load_with_files(db, current_user, form_data, images)


@router.get("", response_model=list[LoadListItem])
def list_loads(
    status: str | None = Query(None, description="Filtrar por status"),
    load_type: str | None = Query(None, description="Tipo de carga"),
    origin: str | None = Query(None, description="Filtrar origem"),
    destination: str | None = Query(None, description="Filtrar destino"),
    q: str | None = Query(None, description="Pesquisa em origem, destino, nome ou código"),
    departure_date_from: date | None = Query(None),
    departure_date_to: date | None = Query(None),
    limit: int | None = Query(None, description="Limite de resultados"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Lista cargas com filtros (pesquisa e Filtros do app)."""
    return list_available_loads(
        db,
        current_user,
        status_filter=status,
        load_type=load_type,
        origin=origin,
        destination=destination,
        q=q,
        departure_date_from=departure_date_from,
        departure_date_to=departure_date_to,
        limit=limit,
    )


@router.get("/me", response_model=list[LoadListItem])
def list_my_loads_route(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Lista cargas ligadas ao utilizador autenticado."""
    return list_my_loads(db, current_user)


@router.get("/{load_id}/tracking", response_model=LoadTrackingResponse)
def track_load(
    load_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Rastreio da carga no mapa (GPS da viagem)."""
    data = get_load_tracking(db, current_user, load_id)
    return LoadTrackingResponse(**data)


@router.get("/{load_id}", response_model=LoadDetailResponse)
def get_load(
    load_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Detalhe completo: remetente, rota estimada, imagens."""
    return get_load_detail_response(db, load_id, current_user)


@router.patch("/{load_id}", response_model=LoadDetailResponse)
def patch_load(
    load_id: int,
    data: LoadUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cliente atualiza a própria carga."""
    return update_load(db, current_user, load_id, data)


@router.delete("/{load_id}", status_code=204)
def remove_load(
    load_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cliente cancela a carga (status cancelada)."""
    delete_load(db, current_user, load_id)


@router.post("/{load_id}/images", response_model=LoadImageResponse, status_code=201)
def add_image(
    load_id: int,
    data: LoadImageCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cliente adiciona imagem à carga (máx. 5 no total)."""
    return add_load_image(db, current_user, load_id, data)


@router.post("/{load_id}/proposals", response_model=LoadProposalResponse, status_code=201)
def submit_proposal(
    load_id: int,
    data: LoadProposalCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Empresa envia proposta para a carga."""
    return create_proposal(db, current_user, load_id, data)


@router.get("/{load_id}/proposals", response_model=list[LoadProposalResponse])
def get_proposals(
    load_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cliente lista propostas recebidas na carga."""
    return list_load_proposals(db, current_user, load_id)


@router.post("/{load_id}/proposals/{proposal_id}/accept", response_model=TripResponse)
def accept_load_proposal(
    load_id: int,
    proposal_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cliente aceita proposta e cria viagem."""
    return accept_proposal(db, current_user, load_id, proposal_id)


@router.post("/{load_id}/proposals/{proposal_id}/reject", response_model=LoadProposalResponse)
def reject_load_proposal(
    load_id: int,
    proposal_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cliente recusa proposta."""
    return reject_proposal(db, current_user, load_id, proposal_id)
