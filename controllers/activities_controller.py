"""
Feed de atividades recentes do cliente.
"""

from sqlalchemy import func
from sqlalchemy.orm import Session

from constants import (
    ACTIVITY_COMPLETED,
    ACTIVITY_IN_PROGRESS,
    ACTIVITY_NEGOTIATING,
    LOAD_STATUS_AVAILABLE,
    LOAD_STATUS_COMPLETED,
    LOAD_STATUS_IN_TRANSIT,
    TRIP_STATUS_STARTED,
    TRIP_STATUS_WAITING,
    TRIP_STATUS_WAITING_CLIENT,
)
from controllers.clients_controller import get_my_client
from models.models import Load, LoadProposal, PaymentPlan, Trip, User, Company


def _display_status(load: Load, trip: Trip | None, pending_proposals: int) -> str:
    """Mapeia estado interno para rótulos do app."""
    if load.status == LOAD_STATUS_COMPLETED:
        return ACTIVITY_COMPLETED

    if load.status == LOAD_STATUS_IN_TRANSIT:
        return ACTIVITY_IN_PROGRESS

    if trip and trip.status in (
        TRIP_STATUS_STARTED,
        TRIP_STATUS_WAITING_CLIENT,
    ):
        return ACTIVITY_IN_PROGRESS

    if load.status == LOAD_STATUS_AVAILABLE and pending_proposals > 0:
        return ACTIVITY_NEGOTIATING

    if trip and trip.status == TRIP_STATUS_WAITING:
        return ACTIVITY_NEGOTIATING

    if load.status not in (LOAD_STATUS_COMPLETED,):
        return ACTIVITY_NEGOTIATING

    return ACTIVITY_NEGOTIATING


def list_client_activities(db: Session, user: User, *, limit: int = 20) -> list[dict]:
    """Atividades recentes das cargas do cliente autenticado."""
    client = get_my_client(db, user)
    loads = (
        db.query(Load)
        .filter(Load.client_id == client.id, Load.status != "cancelada")
        .order_by(Load.updated_at.desc())
        .limit(limit)
        .all()
    )

    items: list[dict] = []
    for load in loads:
        trip = db.query(Trip).filter(Trip.load_id == load.id).first()
        pending = (
            db.query(func.count(LoadProposal.id))
            .filter(LoadProposal.load_id == load.id, LoadProposal.status == "pendente")
            .scalar()
            or 0
        )

        plan = db.query(PaymentPlan).filter(PaymentPlan.load_id == load.id).first()
        contract_amount = float(plan.contract_amount) if plan else float(load.value or 0)
        paid_amount = float(plan.client_paid_total) if plan else 0.0
        remaining_amount = max(0.0, contract_amount - paid_amount)
        payment_percent = (
            min(100.0, round((paid_amount / contract_amount) * 100, 2))
            if contract_amount > 0
            else 0.0
        )
        if paid_amount <= 0:
            payment_status = "nao_pago"
        elif remaining_amount <= 0.009:
            payment_status = "pago"
        else:
            payment_status = "parcial"

        activity_at = load.updated_at
        if trip and trip.started_at:
            activity_at = trip.started_at
        elif trip:
            activity_at = trip.created_at

        items.append(
            {
                "load_id": load.id,
                "code": load.code,
                "origin": load.origin,
                "destination": load.destination,
                "load_type": load.load_type,
                "weight": float(load.weight) if load.weight is not None else None,
                "weight_unit": load.weight_unit,
                "display_status": _display_status(load, trip, pending),
                "activity_at": activity_at,
                "trip_id": trip.id if trip else None,
                "payment_mode": load.payment_mode or "integral",
                "payment_status": payment_status,
                "payment_percent": payment_percent,
                "paid_amount": paid_amount,
                "remaining_amount": remaining_amount,
            }
        )

    return items


def _payment_snapshot_for_load(db: Session, load: Load) -> dict:
    plan = db.query(PaymentPlan).filter(PaymentPlan.load_id == load.id).first()
    contract_amount = float(plan.contract_amount) if plan else float(load.value or 0)
    paid_amount = float(plan.client_paid_total) if plan else 0.0
    remaining_amount = max(0.0, contract_amount - paid_amount)
    payment_percent = (
        min(100.0, round((paid_amount / contract_amount) * 100, 2))
        if contract_amount > 0
        else 0.0
    )
    if paid_amount <= 0:
        payment_status = "nao_pago"
    elif remaining_amount <= 0.009:
        payment_status = "pago"
    else:
        payment_status = "parcial"

    return {
        "payment_mode": load.payment_mode or "integral",
        "payment_status": payment_status,
        "payment_percent": payment_percent,
        "paid_amount": paid_amount,
        "remaining_amount": remaining_amount,
    }


def _company_activity_status(proposal: LoadProposal, trip: Trip | None) -> str:
    if trip is not None:
        if trip.status in {"concluida", "concluido"}:
            return ACTIVITY_COMPLETED
        if trip.status in {
            "indo_carregar",
            "chegou_origem",
            "carregado",
            "viagem_iniciada",
            "aguardando_cliente",
        }:
            return ACTIVITY_IN_PROGRESS
        if trip.status == "aguardando_inicio":
            return ACTIVITY_NEGOTIATING

    if proposal.status in {"pendente", "em_negociacao"}:
        return ACTIVITY_NEGOTIATING
    if proposal.status == "aceite":
        return ACTIVITY_IN_PROGRESS
    return ACTIVITY_NEGOTIATING


def list_company_activities(db: Session, user: User, *, limit: int = 20) -> list[dict]:
    """Feed operacional da transportadora.

    O perfil Company e a fonte de verdade. Contas antigas/migradas podem
    conservar user_type diferente apesar de ja possuirem Company associado.
    """
    company = db.query(Company).filter(Company.user_id == user.id).first()
    if company is None:
        return []

    items_by_load: dict[int, dict] = {}

    trips = (
        db.query(Trip)
        .filter(Trip.company_id == company.id)
        .order_by(Trip.created_at.desc())
        .limit(limit * 2)
        .all()
    )
    for trip in trips:
        if trip.status in {"cancelada", "cancelado"}:
            continue

        load = db.query(Load).filter(Load.id == trip.load_id).first()
        if load is None:
            continue

        proposal = (
            db.query(LoadProposal)
            .filter(
                LoadProposal.load_id == load.id,
                LoadProposal.company_id == company.id,
            )
            .order_by(LoadProposal.created_at.desc())
            .first()
        )

        class _Fallback:
            status = "aceite"

        activity_at = (
            trip.completed_at
            or trip.started_at
            or trip.en_route_pickup_at
            or trip.created_at
            or load.updated_at
        )

        items_by_load[load.id] = {
            "load_id": load.id,
            "code": load.code,
            "origin": load.origin,
            "destination": load.destination,
            "load_type": load.load_type,
            "weight": float(load.weight) if load.weight is not None else None,
            "weight_unit": load.weight_unit,
            "display_status": _company_activity_status(
                proposal or _Fallback(),
                trip,
            ),
            "activity_at": activity_at,
            "trip_id": trip.id,
            "activity_source": "trip",
            "company_id": company.id,
            **_payment_snapshot_for_load(db, load),
        }

    proposals = (
        db.query(LoadProposal)
        .filter(LoadProposal.company_id == company.id)
        .order_by(LoadProposal.created_at.desc())
        .limit(limit * 2)
        .all()
    )
    for proposal in proposals:
        if proposal.status in {"recusada", "cancelada"}:
            continue
        load = proposal.load
        if load is None or load.id in items_by_load:
            continue

        items_by_load[load.id] = {
            "load_id": load.id,
            "code": load.code,
            "origin": load.origin,
            "destination": load.destination,
            "load_type": load.load_type,
            "weight": float(load.weight) if load.weight is not None else None,
            "weight_unit": load.weight_unit,
            "display_status": _company_activity_status(proposal, None),
            "activity_at": proposal.created_at,
            "trip_id": None,
            "activity_source": "proposal",
            "company_id": company.id,
            **_payment_snapshot_for_load(db, load),
        }

    items = list(items_by_load.values())
    items.sort(key=lambda row: row["activity_at"], reverse=True)
    return items[:limit]

