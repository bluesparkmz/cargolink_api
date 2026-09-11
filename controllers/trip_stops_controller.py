from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from constants import NEGOTIATION_STATUS_ACCEPTED, PROPOSAL_STATUS_ACCEPTED
from controllers.financial_settings import (
    get_delay_fee_per_24h,
    get_fretix_commission_percent,
)
from controllers.notifications_controller import (
    create_notification,
    emit_notification,
)
from controllers.realtime_events import emit_to_user
from models.models import (
    AdditionalCharge,
    Client,
    Company,
    Driver,
    LoadProposal,
    ProposalNegotiation,
    Trip,
    TripStop,
    User,
)

STOP_STATUS_SCHEDULED = "programada"
STOP_STATUS_ACTIVE = "em_andamento"
STOP_STATUS_COMPLETED = "concluida"

CATEGORY_LABELS = {
    "regularizacao_carga": "Regularização da carga",
    "pagamento_direitos": "Pagamento de direitos",
    "fiscalizacao_documentacao": "Fiscalização / documentação",
    "outra": "Outra",
}

CLOSED_TRIP_STATUSES = {"concluida", "cancelada", "cancelado"}


def _now_naive_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _get_trip(db: Session, trip_id: int) -> Trip:
    trip = db.query(Trip).filter(Trip.id == trip_id).first()
    if trip is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Viagem não encontrada.",
        )
    return trip


def _get_client_for_trip(db: Session, trip: Trip) -> Client | None:
    if trip.load is None:
        return None
    return (
        db.query(Client)
        .filter(Client.id == trip.load.client_id)
        .first()
    )


def _get_company_for_trip(db: Session, trip: Trip) -> Company | None:
    if trip.company_id is None:
        return None
    return (
        db.query(Company)
        .filter(Company.id == trip.company_id)
        .first()
    )


def _get_driver_for_trip(db: Session, trip: Trip) -> Driver | None:
    if trip.driver_id is None:
        return None
    return (
        db.query(Driver)
        .filter(Driver.id == trip.driver_id)
        .first()
    )


def _assert_trip_access(
    db: Session,
    user: User,
    trip: Trip,
) -> str:
    if user.user_type == "admin":
        return "admin"

    if user.user_type == "cliente":
        client = (
            db.query(Client)
            .filter(Client.user_id == user.id)
            .first()
        )
        if client and trip.load and trip.load.client_id == client.id:
            return "cliente"

    if user.user_type == "empresa":
        company = (
            db.query(Company)
            .filter(Company.user_id == user.id)
            .first()
        )
        if company and trip.company_id == company.id:
            return "empresa"

    if user.user_type == "motorista":
        driver = (
            db.query(Driver)
            .filter(Driver.user_id == user.id)
            .first()
        )
        if driver and trip.driver_id == driver.id:
            return "motorista"

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Sem acesso a esta viagem.",
    )


def _notify_users(
    db: Session,
    user_ids: list[int],
    *,
    title: str,
    body: str,
    notification_type: str,
    payload: dict,
) -> list:
    notifications = []
    for user_id in sorted(set(uid for uid in user_ids if uid)):
        notification = create_notification(
            db,
            user_id=user_id,
            title=title,
            body=body,
            notification_type=notification_type,
            payload=payload,
        )
        notifications.append(notification)
    return notifications


def _emit_notifications(notifications: list) -> None:
    for notification in notifications:
        emit_notification(notification)


def _emit_stop_event(
    trip: Trip,
    *,
    event_type: str,
    stop_payload: dict,
    client_user_id: int | None,
    company_user_id: int | None,
    driver_user_id: int | None,
) -> None:
    event = {
        "type": event_type,
        "trip_id": trip.id,
        "load_id": trip.load_id,
        "stop": stop_payload,
    }

    for user_id in {
        client_user_id,
        company_user_id,
        driver_user_id,
    }:
        if not user_id:
            continue

        user_event = event
        if driver_user_id and user_id == driver_user_id:
            user_event = {
                **event,
                "stop": _sanitize_stop_payload_for_driver(
                    stop_payload
                ),
            }

        emit_to_user(user_id, user_event)


def _delay_calculation(
    stop: TripStop,
    *,
    now: datetime | None = None,
) -> dict:
    now = now or _now_naive_utc()

    if stop.started_at is None:
        return {
            "elapsed_seconds": 0,
            "elapsed_hours": 0.0,
            "free_remaining_seconds": 24 * 3600,
            "chargeable_hours": 0,
            "fee_active": False,
            "delay_fee_amount": Decimal("0.00"),
        }

    end_at = stop.completed_at or now
    elapsed_seconds = max(
        0,
        int((end_at - stop.started_at).total_seconds()),
    )

    grace_seconds = 24 * 3600
    excess_seconds = max(0, elapsed_seconds - grace_seconds)

    # A partir do momento em que ultrapassa 24h, cobra por hora iniciada.
    chargeable_hours = (
        math.ceil(excess_seconds / 3600)
        if excess_seconds > 0
        else 0
    )

    rate_24h = Decimal(str(stop.delay_fee_per_24h or 3000))
    hourly_rate = rate_24h / Decimal("24")
    fee = (
        hourly_rate * Decimal(chargeable_hours)
    ).quantize(Decimal("0.01"))

    return {
        "elapsed_seconds": elapsed_seconds,
        "elapsed_hours": round(elapsed_seconds / 3600, 2),
        "free_remaining_seconds": max(
            0,
            grace_seconds - elapsed_seconds,
        ),
        "chargeable_hours": chargeable_hours,
        "fee_active": chargeable_hours > 0,
        "delay_fee_amount": fee,
    }


def _sync_additional_charge(
    db: Session,
    stop: TripStop,
) -> tuple[AdditionalCharge | None, bool]:
    calc = _delay_calculation(stop)
    amount = calc["delay_fee_amount"]

    charge = (
        db.query(AdditionalCharge)
        .filter(AdditionalCharge.stop_id == stop.id)
        .first()
    )

    created = False

    if amount <= 0:
        return charge, created

    if charge is None:
        charge = AdditionalCharge(
            trip_id=stop.trip_id,
            stop_id=stop.id,
            charge_type="delay_fee",
            amount=amount,
            status="pendente_pagamento",
            description=(
                f"Taxa de demora — "
                f"{CATEGORY_LABELS.get(stop.stop_type, stop.stop_type)}"
            ),
        )
        db.add(charge)
        created = True
    else:
        charge.amount = amount
        charge.description = (
            f"Taxa de demora — "
            f"{CATEGORY_LABELS.get(stop.stop_type, stop.stop_type)}"
        )

    return charge, created


def _stop_payload(
    stop: TripStop,
    charge: AdditionalCharge | None = None,
) -> dict:
    calc = _delay_calculation(stop)

    return {
        "id": stop.id,
        "trip_id": stop.trip_id,
        "category": stop.stop_type,
        "category_label": CATEGORY_LABELS.get(
            stop.stop_type,
            stop.stop_type,
        ),
        "location_name": stop.location_name,
        "description": stop.notes,
        "created_by_user_id": stop.created_by_user_id,
        "created_by_type": stop.created_by_type,
        "status": stop.status,
        "started_at": stop.started_at,
        "completed_at": stop.completed_at,
        "created_at": stop.created_at,
        "delay_grace_hours": 24,
        "delay_fee_per_24h": float(stop.delay_fee_per_24h),
        "hourly_rate": float(
            Decimal(str(stop.delay_fee_per_24h or 3000))
            / Decimal("24")
        ),
        "elapsed_seconds": calc["elapsed_seconds"],
        "elapsed_hours": calc["elapsed_hours"],
        "free_remaining_seconds": calc[
            "free_remaining_seconds"
        ],
        "chargeable_hours": calc["chargeable_hours"],
        "fee_active": calc["fee_active"],
        "delay_fee_amount": float(
            calc["delay_fee_amount"]
        ),
        "additional_charge_id": charge.id if charge else None,
        "additional_charge_status": (
            charge.status if charge else None
        ),
    }



_DRIVER_HIDDEN_STOP_FIELDS = {
    "delay_fee_per_24h",
    "hourly_rate",
    "chargeable_hours",
    "delay_fee_amount",
    "additional_charge_id",
    "additional_charge_status",
}


def _sanitize_stop_payload_for_driver(payload: dict) -> dict:
    result = dict(payload)
    for key in _DRIVER_HIDDEN_STOP_FIELDS:
        result.pop(key, None)

    elapsed = int(result.get("elapsed_seconds") or 0)
    result["grace_period_exceeded"] = elapsed > 24 * 3600
    result["overtime_seconds"] = max(0, elapsed - 24 * 3600)
    return result


def _stop_payload_for_role(
    stop: TripStop,
    role: str,
    charge: AdditionalCharge | None = None,
) -> dict:
    payload = _stop_payload(stop, charge)
    if role == "motorista":
        return _sanitize_stop_payload_for_driver(payload)
    return payload


def list_stop_categories() -> list[dict]:
    return [
        {"value": key, "label": label}
        for key, label in CATEGORY_LABELS.items()
    ]


def create_trip_stop(
    db: Session,
    user: User,
    trip_id: int,
    *,
    category: str,
    location_name: str,
    description: str,
) -> dict:
    trip = _get_trip(db, trip_id)
    actor = _assert_trip_access(db, user, trip)

    if actor not in {"empresa", "motorista"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Apenas a empresa ou o motorista podem "
                "adicionar uma paragem."
            ),
        )

    if category not in CATEGORY_LABELS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Categoria de paragem inválida.",
        )

    if trip.status in CLOSED_TRIP_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Não é possível adicionar paragens a uma viagem concluída.",
        )

    if trip.driver_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "A viagem precisa ter um motorista atribuído "
                "antes de adicionar a paragem."
            ),
        )

    active = (
        db.query(TripStop)
        .filter(
            TripStop.trip_id == trip.id,
            TripStop.status == STOP_STATUS_ACTIVE,
        )
        .first()
    )
    if actor == "motorista" and active is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Já existe uma paragem em andamento nesta viagem.",
        )

    rate = get_delay_fee_per_24h(db)

    stop = TripStop(
        trip_id=trip.id,
        stop_type=category,
        location_name=location_name.strip(),
        notes=description.strip(),
        created_by_user_id=user.id,
        created_by_type=actor,
        status=(
            STOP_STATUS_ACTIVE
            if actor == "motorista"
            else STOP_STATUS_SCHEDULED
        ),
        started_at=(
            _now_naive_utc()
            if actor == "motorista"
            else None
        ),
        delay_fee_per_24h=rate,
    )
    db.add(stop)
    db.flush()

    client = _get_client_for_trip(db, trip)
    company = _get_company_for_trip(db, trip)
    driver = _get_driver_for_trip(db, trip)

    client_user_id = (
        client.user_id if client is not None else None
    )
    company_user_id = (
        company.user_id if company is not None else None
    )
    driver_user_id = (
        driver.user_id if driver is not None else None
    )

    payload = _stop_payload(stop)

    if actor == "motorista":
        title = "Paragem para regularização"
        body = (
            f"O motorista parou em {stop.location_name}. "
            f"Motivo: {stop.notes}"
        )
        recipients = [
            client_user_id,
            company_user_id,
        ]
        notification_type = "trip.stop_started"
    else:
        title = "Nova paragem programada"
        body = (
            f"A empresa adicionou uma paragem em "
            f"{stop.location_name}: {stop.notes}"
        )
        recipients = [
            client_user_id,
            driver_user_id,
        ]
        notification_type = "trip.stop_scheduled"

    notifications = _notify_users(
        db,
        recipients,
        title=title,
        body=body,
        notification_type=notification_type,
        payload={
            "trip_id": trip.id,
            "load_id": trip.load_id,
            "stop_id": stop.id,
            "category": stop.stop_type,
            "location_name": stop.location_name,
        },
    )

    db.commit()
    db.refresh(stop)
    for notification in notifications:
        db.refresh(notification)

    _emit_notifications(notifications)
    payload = _stop_payload(stop)
    _emit_stop_event(
        trip,
        event_type=notification_type,
        stop_payload=payload,
        client_user_id=client_user_id,
        company_user_id=company_user_id,
        driver_user_id=driver_user_id,
    )
    return _stop_payload_for_role(stop, actor)


def start_trip_stop(
    db: Session,
    user: User,
    trip_id: int,
    stop_id: int,
) -> dict:
    trip = _get_trip(db, trip_id)
    actor = _assert_trip_access(db, user, trip)

    if actor != "motorista":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Apenas o motorista pode iniciar a paragem.",
        )

    stop = (
        db.query(TripStop)
        .filter(
            TripStop.id == stop_id,
            TripStop.trip_id == trip.id,
        )
        .first()
    )
    if stop is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Paragem não encontrada.",
        )

    if stop.status == STOP_STATUS_ACTIVE:
        return _stop_payload_for_role(
            stop,
            actor,
            _sync_additional_charge(db, stop)[0],
        )

    if stop.status != STOP_STATUS_SCHEDULED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Esta paragem não pode ser iniciada.",
        )

    active = (
        db.query(TripStop)
        .filter(
            TripStop.trip_id == trip.id,
            TripStop.status == STOP_STATUS_ACTIVE,
            TripStop.id != stop.id,
        )
        .first()
    )
    if active is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Já existe outra paragem em andamento.",
        )

    stop.status = STOP_STATUS_ACTIVE
    stop.started_at = _now_naive_utc()

    client = _get_client_for_trip(db, trip)
    company = _get_company_for_trip(db, trip)
    driver = _get_driver_for_trip(db, trip)

    recipients = [
        client.user_id if client else None,
        company.user_id if company else None,
    ]

    notifications = _notify_users(
        db,
        recipients,
        title="Regularização iniciada",
        body=(
            f"O motorista chegou a {stop.location_name} "
            f"e iniciou a paragem: {stop.notes}"
        ),
        notification_type="trip.stop_started",
        payload={
            "trip_id": trip.id,
            "load_id": trip.load_id,
            "stop_id": stop.id,
        },
    )

    db.commit()
    db.refresh(stop)
    for notification in notifications:
        db.refresh(notification)

    _emit_notifications(notifications)
    payload = _stop_payload(stop)
    _emit_stop_event(
        trip,
        event_type="trip.stop_started",
        stop_payload=payload,
        client_user_id=client.user_id if client else None,
        company_user_id=company.user_id if company else None,
        driver_user_id=driver.user_id if driver else None,
    )
    return _stop_payload_for_role(stop, actor)


def complete_trip_stop(
    db: Session,
    user: User,
    trip_id: int,
    stop_id: int,
) -> dict:
    trip = _get_trip(db, trip_id)
    actor = _assert_trip_access(db, user, trip)

    if actor != "motorista":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Apenas o motorista pode concluir a paragem.",
        )

    stop = (
        db.query(TripStop)
        .filter(
            TripStop.id == stop_id,
            TripStop.trip_id == trip.id,
        )
        .first()
    )
    if stop is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Paragem não encontrada.",
        )

    if stop.status == STOP_STATUS_COMPLETED:
        charge, _ = _sync_additional_charge(db, stop)
        return _stop_payload_for_role(
            stop,
            actor,
            charge,
        )

    if stop.status != STOP_STATUS_ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A paragem precisa estar em andamento.",
        )

    stop.completed_at = _now_naive_utc()
    stop.status = STOP_STATUS_COMPLETED

    charge, charge_created = _sync_additional_charge(
        db,
        stop,
    )
    payload = _stop_payload(stop, charge)

    client = _get_client_for_trip(db, trip)
    company = _get_company_for_trip(db, trip)
    driver = _get_driver_for_trip(db, trip)

    fee = Decimal(str(payload["delay_fee_amount"]))

    body = (
        f"A regularização em {stop.location_name} foi concluída. "
        f"Duração: {payload['elapsed_hours']:.2f}h."
    )
    if fee > 0:
        body += (
            f" Taxa de demora acumulada: "
            f"{fee:.2f} MT."
        )

    notifications = _notify_users(
        db,
        [
            client.user_id if client else None,
            company.user_id if company else None,
        ],
        title="Paragem concluída",
        body=body,
        notification_type="trip.stop_completed",
        payload={
            "trip_id": trip.id,
            "load_id": trip.load_id,
            "stop_id": stop.id,
            "delay_fee_amount": float(fee),
        },
    )

    db.commit()
    db.refresh(stop)
    if charge is not None:
        db.refresh(charge)
    for notification in notifications:
        db.refresh(notification)

    _emit_notifications(notifications)
    payload = _stop_payload(stop, charge)
    _emit_stop_event(
        trip,
        event_type="trip.stop_completed",
        stop_payload=payload,
        client_user_id=client.user_id if client else None,
        company_user_id=company.user_id if company else None,
        driver_user_id=driver.user_id if driver else None,
    )
    return _stop_payload_for_role(stop, actor, charge)


def list_trip_stops(
    db: Session,
    user: User,
    trip_id: int,
) -> list[dict]:
    trip = _get_trip(db, trip_id)
    role = _assert_trip_access(db, user, trip)

    stops = (
        db.query(TripStop)
        .filter(TripStop.trip_id == trip.id)
        .order_by(TripStop.created_at.asc())
        .all()
    )

    result = []
    new_charge_notifications = []

    client = _get_client_for_trip(db, trip)
    company = _get_company_for_trip(db, trip)

    for stop in stops:
        charge, created = _sync_additional_charge(
            db,
            stop,
        )
        result.append(
            _stop_payload_for_role(
                stop,
                role,
                charge,
            )
        )

        if created and charge is not None:
            new_charge_notifications.extend(
                _notify_users(
                    db,
                    [
                        client.user_id if client else None,
                        company.user_id if company else None,
                    ],
                    title="Taxa de demora activa",
                    body=(
                        f"A paragem em {stop.location_name} "
                        f"ultrapassou 24 horas. "
                        f"A taxa de demora começou a ser calculada."
                    ),
                    notification_type="trip.delay_fee_started",
                    payload={
                        "trip_id": trip.id,
                        "load_id": trip.load_id,
                        "stop_id": stop.id,
                        "delay_fee_amount": float(
                            charge.amount
                        ),
                    },
                )
            )

    db.commit()
    for notification in new_charge_notifications:
        db.refresh(notification)
    _emit_notifications(new_charge_notifications)

    return result


def _accepted_transport_amount(
    db: Session,
    trip: Trip,
) -> tuple[Decimal, LoadProposal | None]:
    proposal = (
        db.query(LoadProposal)
        .filter(
            LoadProposal.load_id == trip.load_id,
            LoadProposal.status == PROPOSAL_STATUS_ACCEPTED,
        )
        .order_by(LoadProposal.created_at.desc())
        .first()
    )

    if proposal is None:
        return Decimal("0.00"), None

    accepted_negotiation = (
        db.query(ProposalNegotiation)
        .filter(
            ProposalNegotiation.proposal_id == proposal.id,
            ProposalNegotiation.status == NEGOTIATION_STATUS_ACCEPTED,
        )
        .order_by(ProposalNegotiation.created_at.desc())
        .first()
    )

    if accepted_negotiation is not None:
        return Decimal(
            str(accepted_negotiation.amount)
        ), proposal

    return Decimal(
        str(proposal.proposed_value or 0)
    ), proposal


def get_trip_financial_summary(
    db: Session,
    user: User,
    trip_id: int,
) -> dict:
    trip = _get_trip(db, trip_id)
    role = _assert_trip_access(db, user, trip)

    if role == "motorista":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Informação financeira da viagem não está "
                "disponível para o motorista."
            ),
        )

    # Actualiza custos de demora antes de resumir.
    stops = (
        db.query(TripStop)
        .filter(TripStop.trip_id == trip.id)
        .all()
    )
    for stop in stops:
        _sync_additional_charge(db, stop)

    db.flush()

    charges = (
        db.query(AdditionalCharge)
        .filter(AdditionalCharge.trip_id == trip.id)
        .order_by(AdditionalCharge.created_at.asc())
        .all()
    )

    additional_total = sum(
        (
            Decimal(str(charge.amount or 0))
            for charge in charges
        ),
        Decimal("0.00"),
    )

    transport_amount, proposal = _accepted_transport_amount(
        db,
        trip,
    )
    commission_percent = get_fretix_commission_percent(db)
    commission_amount = (
        transport_amount
        * commission_percent
        / Decimal("100")
    ).quantize(Decimal("0.01"))
    company_net_transport = (
        transport_amount - commission_amount
    )

    cargo_declared_value = Decimal(
        str(trip.load.value or 0)
    ) if trip.load else Decimal("0.00")

    db.commit()

    return {
        "trip_id": trip.id,
        "load_id": trip.load_id,
        "proposal_id": proposal.id if proposal else None,
        "viewer_role": role,
        "currency": "MT",
        "cargo_declared_value": float(
            cargo_declared_value
        ),
        "transport_amount": float(
            transport_amount
        ),
        "additional_costs": float(
            additional_total
        ),
        "client_total_cost": float(
            transport_amount + additional_total
        ),
        "fretix_commission_percent": float(
            commission_percent
        ),
        "fretix_commission_amount": float(
            commission_amount
        ),
        "company_net_transport": float(
            company_net_transport
        ),
        "company_expected_total": float(
            company_net_transport + additional_total
        ),
        "additional_charges": [
            {
                "id": charge.id,
                "stop_id": charge.stop_id,
                "charge_type": charge.charge_type,
                "amount": float(charge.amount),
                "status": charge.status,
                "description": charge.description,
                "created_at": charge.created_at,
                "updated_at": charge.updated_at,
            }
            for charge in charges
        ],
    }
