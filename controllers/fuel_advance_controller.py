from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from constants import (
    PAYMENT_METHOD_WALLET,
    PAYMENT_STATUS_COMPLETED,
    TRANSACTION_STATUS_COMPLETED,
)
from controllers.notifications_controller import create_notification, emit_notification
from controllers.payment_plan_controller import (
    client_remaining,
    ensure_payment_plan_for_trip,
    money,
    record_direct_fuel_client_payment,
    release_company_escrow_amount,
)
from models.models import (
    Client,
    Company,
    FuelAdvance,
    Load,
    Payment,
    PaymentPlan,
    Transaction,
    Trip,
    User,
    Vehicle,
    Wallet,
)

FUEL_ADVANCE_LIMIT_PERCENT = Decimal("50.00")
EXCLUDED_ADVANCE_STATUSES = {"rejeitado", "cancelado", "falhou"}

STATUS_WAITING_CLIENT = "aguardando_cliente"
STATUS_PAID = "pago"
STATUS_REJECTED = "rejeitado"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _company(db: Session, user: User) -> Company:
    if user.user_type != "empresa":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Apenas empresas podem solicitar combustível.",
        )
    company = db.query(Company).filter(Company.user_id == user.id).first()
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Empresa não encontrada.",
        )
    return company


def _trip_for_company(db: Session, company: Company, trip_id: int) -> Trip:
    trip = (
        db.query(Trip)
        .filter(Trip.id == trip_id, Trip.company_id == company.id)
        .first()
    )
    if trip is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Viagem não encontrada para esta empresa.",
        )
    return trip


def _wallet_locked(db: Session, user_id: int) -> Wallet:
    wallet = (
        db.query(Wallet)
        .filter(Wallet.user_id == user_id)
        .with_for_update()
        .first()
    )
    if wallet is None:
        wallet = Wallet(user_id=user_id)
        db.add(wallet)
        db.flush()
    return wallet


def _advance_totals(db: Session, trip_id: int) -> tuple[Decimal, Decimal]:
    rows = db.query(FuelAdvance).filter(FuelAdvance.trip_id == trip_id).all()
    reserved = Decimal("0.00")
    paid = Decimal("0.00")
    for row in rows:
        if row.status in EXCLUDED_ADVANCE_STATUSES:
            continue
        amount = money(row.amount)
        reserved += amount
        if row.status == STATUS_PAID:
            paid += amount
    return money(reserved), money(paid)


def _plan_for_trip(db: Session, trip: Trip) -> PaymentPlan:
    return ensure_payment_plan_for_trip(db, trip)


def get_fuel_advance_eligibility(db: Session, user: User, trip_id: int) -> dict:
    company = _company(db, user)
    trip = _trip_for_company(db, company, trip_id)
    plan = _plan_for_trip(db, trip)

    reasons: list[str] = []
    if trip.status in {"concluida", "concluido", "cancelada", "cancelado"}:
        reasons.append("A viagem já está encerrada.")

    vehicle = None
    if trip.vehicle_id is None:
        reasons.append("Atribua um camião à viagem antes de solicitar combustível.")
    else:
        vehicle = (
            db.query(Vehicle)
            .filter(
                Vehicle.id == trip.vehicle_id,
                Vehicle.company_id == company.id,
            )
            .first()
        )
        if vehicle is None:
            reasons.append("O camião atribuído não pertence à empresa.")

    max_allowed = money(
        money(plan.company_net_entitlement)
        * FUEL_ADVANCE_LIMIT_PERCENT
        / Decimal("100")
    )
    reserved, paid = _advance_totals(db, trip.id)
    fuel_limit_remaining = max(
        Decimal("0.00"),
        money(max_allowed - reserved),
    )

    escrow_available = money(plan.company_escrow_balance)
    contract_remaining = client_remaining(plan)

    if plan.mode == "integral" and escrow_available > 0:
        route = "automatic_escrow"
        currently_available = min(fuel_limit_remaining, escrow_available)
    else:
        route = "client_approval"
        currently_available = min(fuel_limit_remaining, contract_remaining)

    if fuel_limit_remaining <= 0:
        reasons.append("O limite de 50% para combustível já foi atingido.")
    if contract_remaining <= 0 and route == "client_approval":
        reasons.append(
            "O contrato já está totalmente pago; não existe saldo por pagar "
            "para uma nova requisição de combustível."
        )
    if currently_available <= 0 and not reasons:
        reasons.append("Não existe valor disponível para este pedido.")

    return {
        "trip_id": trip.id,
        "load_id": trip.load_id,
        "payment_plan_id": plan.id,
        "funding_route": route,
        "payment_mode": plan.mode,
        "client_paid_total": float(money(plan.client_paid_total)),
        "client_remaining": float(contract_remaining),
        "company_escrow_balance": float(escrow_available),
        "company_net_transport": float(money(plan.company_net_entitlement)),
        "limit_percent": 50.0,
        "max_allowed_amount": float(max_allowed),
        "already_reserved_amount": float(reserved),
        "already_paid_amount": float(paid),
        "remaining_fuel_limit": float(fuel_limit_remaining),
        "remaining_requestable_amount": float(currently_available),
        "vehicle": (
            {
                "id": vehicle.id,
                "plate": vehicle.plate,
                "brand": vehicle.brand,
                "model_name": vehicle.model_name,
            }
            if vehicle
            else None
        ),
        "can_request": not reasons and currently_available > 0,
        "reasons": reasons,
        "currency": "MT",
    }

def _notify(db: Session, *, user_id: int, title: str, body: str, notification_type: str, payload: dict):
    return create_notification(
        db,
        user_id=user_id,
        title=title,
        body=body,
        notification_type=notification_type,
        payload=payload,
    )


def serialize_fuel_advance(row: FuelAdvance) -> dict:
    meta = dict(row.provider_response or {})
    return {
        "id": row.id,
        "trip_id": row.trip_id,
        "company_id": row.company_id,
        "vehicle_id": row.vehicle_id,
        "amount": float(row.amount),
        "status": row.status,
        "funding_source": meta.get("funding_source"),
        "reason": meta.get("reason"),
        "automatic": bool(meta.get("automatic")),
        "max_allowed_amount": float(row.max_allowed_amount),
        "approved_at": row.approved_at,
        "paid_at": row.paid_at,
        "external_reference": row.external_reference,
        "created_at": row.created_at,
        "currency": "MT",
    }


def create_fuel_advance_request(
    db: Session,
    user: User,
    trip_id: int,
    *,
    vehicle_id: int,
    amount: Decimal,
    reason: str | None = None,
    mpesa_phone: str | None = None,
) -> dict:
    company = _company(db, user)
    trip = _trip_for_company(db, company, trip_id)
    eligibility = get_fuel_advance_eligibility(db, user, trip_id)

    if not eligibility["can_request"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=" ".join(eligibility["reasons"]) or "Pedido indisponível.",
        )
    if trip.vehicle_id != vehicle_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Seleccione o camião atribuído a esta viagem.",
        )

    amount = money(amount)
    maximum = money(eligibility["remaining_requestable_amount"])
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Valor inválido.")
    if amount > maximum:
        raise HTTPException(
            status_code=400,
            detail=f"O máximo disponível neste momento é {maximum:.2f} MT.",
        )

    plan = _plan_for_trip(db, trip)
    funding_route = eligibility["funding_route"]
    meta = {
        "funding_source": funding_route,
        "reason": (reason or "").strip() or None,
        "client_user_id": plan.client_user_id,
        "payment_plan_id": plan.id,
        "automatic": funding_route == "automatic_escrow",
    }

    advance = FuelAdvance(
        trip_id=trip.id,
        company_id=company.id,
        vehicle_id=vehicle_id,
        requested_by_user_id=user.id,
        amount=amount,
        mpesa_phone=(mpesa_phone or ""),
        max_allowed_amount=money(eligibility["max_allowed_amount"]),
        status=STATUS_PAID if funding_route == "automatic_escrow" else STATUS_WAITING_CLIENT,
        provider_response=meta,
        approved_at=_now() if funding_route == "automatic_escrow" else None,
        paid_at=_now() if funding_route == "automatic_escrow" else None,
    )
    db.add(advance)
    db.flush()
    notifications = []

    if funding_route == "automatic_escrow":
        company_wallet = _wallet_locked(db, company.user_id)
        pending = money(company_wallet.pending_balance)
        if pending < amount:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "A carteira da empresa não possui saldo em retenção "
                    "suficiente para este adiantamento."
                ),
            )

        company_wallet.pending_balance = money(pending - amount)
        company_wallet.available_balance = money(
            money(company_wallet.available_balance) + amount
        )
        release_company_escrow_amount(plan, amount)

        reference = f"FUEL{uuid4().hex[:14].upper()}"
        advance.external_reference = reference
        db.add(
            Transaction(
                wallet_id=company_wallet.id,
                transaction_type="fuel_advance_release",
                amount=amount,
                status=TRANSACTION_STATUS_COMPLETED,
                reference=reference,
                description=f"Combustível libertado automaticamente da carga {trip.load_id}",
            )
        )

        notifications.append(
            _notify(
                db,
                user_id=company.user_id,
                title="Combustível libertado",
                body=(
                    f"{amount:.2f} MT foram libertados automaticamente "
                    "do valor em retenção para o saldo disponível da empresa."
                ),
                notification_type="fuel_request.auto_released",
                payload={
                    "fuel_advance_id": advance.id,
                    "trip_id": trip.id,
                    "load_id": trip.load_id,
                    "amount": float(amount),
                },
            )
        )
        notifications.append(
            _notify(
                db,
                user_id=plan.client_user_id,
                title="Adiantamento para combustível",
                body=(
                    f"A Fretix libertou {amount:.2f} MT do valor já pago "
                    "desta carga para combustível."
                ),
                notification_type="fuel_request.auto_released_info",
                payload={
                    "fuel_advance_id": advance.id,
                    "trip_id": trip.id,
                    "load_id": trip.load_id,
                    "amount": float(amount),
                },
            )
        )
    else:
        notifications.append(
            _notify(
                db,
                user_id=plan.client_user_id,
                title="Pedido de combustível",
                body=(
                    f"A transportadora solicitou {amount:.2f} MT para combustível. "
                    "O valor pago será abatido do transporte."
                ),
                notification_type="fuel_request.created",
                payload={
                    "fuel_advance_id": advance.id,
                    "trip_id": trip.id,
                    "load_id": trip.load_id,
                    "amount": float(amount),
                    "action_required": True,
                },
            )
        )

    db.commit()
    db.refresh(advance)
    for notification in notifications:
        db.refresh(notification)
        emit_notification(notification)
    return serialize_fuel_advance(advance)


def list_fuel_advances(db: Session, user: User, trip_id: int) -> list[dict]:
    company = _company(db, user)
    trip = _trip_for_company(db, company, trip_id)
    rows = (
        db.query(FuelAdvance)
        .filter(FuelAdvance.trip_id == trip.id)
        .order_by(FuelAdvance.created_at.desc())
        .all()
    )
    return [serialize_fuel_advance(row) for row in rows]


def _client_for_user(db: Session, user: User) -> Client:
    if user.user_type != "cliente":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Apenas clientes podem responder a requisições.",
        )
    client = db.query(Client).filter(Client.user_id == user.id).first()
    if client is None:
        raise HTTPException(status_code=404, detail="Cliente não encontrado.")
    return client


def _get_client_advance(
    db: Session,
    user: User,
    advance_id: int,
) -> tuple[FuelAdvance, Trip, PaymentPlan]:
    client = _client_for_user(db, user)
    advance = (
        db.query(FuelAdvance)
        .filter(FuelAdvance.id == advance_id)
        .with_for_update()
        .first()
    )
    if advance is None:
        raise HTTPException(status_code=404, detail="Requisição não encontrada.")

    trip = db.query(Trip).filter(Trip.id == advance.trip_id).first()
    if trip is None:
        raise HTTPException(status_code=404, detail="Viagem não encontrada.")
    load = db.query(Load).filter(Load.id == trip.load_id).first()
    if load is None or load.client_id != client.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esta requisição não pertence ao cliente.",
        )

    plan = _plan_for_trip(db, trip)
    return advance, trip, plan


def list_client_fuel_requests(db: Session, user: User) -> list[dict]:
    client = _client_for_user(db, user)
    trips = (
        db.query(Trip)
        .join(Load, Load.id == Trip.load_id)
        .filter(Load.client_id == client.id)
        .all()
    )
    trip_by_id = {row.id: row for row in trips}
    trip_ids = list(trip_by_id)
    if not trip_ids:
        return []

    rows = (
        db.query(FuelAdvance)
        .filter(FuelAdvance.trip_id.in_(trip_ids))
        .order_by(FuelAdvance.created_at.desc())
        .all()
    )

    company_ids = {row.company_id for row in rows}
    companies = (
        {
            row.id: row
            for row in db.query(Company)
            .filter(Company.id.in_(company_ids))
            .all()
        }
        if company_ids
        else {}
    )

    load_ids = {trip.load_id for trip in trips}
    loads = (
        {
            row.id: row
            for row in db.query(Load)
            .filter(Load.id.in_(load_ids))
            .all()
        }
        if load_ids
        else {}
    )

    result = []
    for row in rows:
        item = serialize_fuel_advance(row)
        trip = trip_by_id.get(row.trip_id)
        load = loads.get(trip.load_id) if trip else None
        company = companies.get(row.company_id)

        item["load_id"] = trip.load_id if trip else None
        item["load_code"] = load.code if load else None
        item["company_name"] = (
            company.company_name if company else "Transportadora"
        )
        item["requested_by_label"] = (
            company.company_name if company else "Transportadora"
        )
        item["request_source"] = "transportadora"
        item["action_required"] = row.status == STATUS_WAITING_CLIENT
        result.append(item)

    return result

def pay_client_fuel_request(db: Session, user: User, advance_id: int) -> dict:
    advance, trip, plan = _get_client_advance(db, user, advance_id)

    if advance.status == STATUS_PAID:
        return serialize_fuel_advance(advance)
    if advance.status != STATUS_WAITING_CLIENT:
        raise HTTPException(status_code=400, detail="Esta requisição já não pode ser paga.")

    amount = money(advance.amount)
    if amount > client_remaining(plan):
        raise HTTPException(status_code=400, detail="O pedido excede o valor restante do transporte.")

    company = db.query(Company).filter(Company.id == advance.company_id).first()
    if company is None:
        raise HTTPException(status_code=404, detail="Empresa não encontrada.")

    client_wallet = _wallet_locked(db, user.id)
    company_wallet = _wallet_locked(db, company.user_id)
    available = money(client_wallet.available_balance)
    if available < amount:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Saldo insuficiente. Disponível: {available:.2f} MT; "
                f"necessário: {amount:.2f} MT. Deposite na carteira e tente novamente."
            ),
        )

    client_wallet.available_balance = money(available - amount)
    company_wallet.available_balance = money(
        money(company_wallet.available_balance) + amount
    )
    record_direct_fuel_client_payment(db, plan, amount)

    reference = f"FUELC{uuid4().hex[:12].upper()}"
    payment = Payment(
        user_id=user.id,
        load_id=trip.load_id,
        method=PAYMENT_METHOD_WALLET,
        phone=None,
        amount=amount,
        status=PAYMENT_STATUS_COMPLETED,
        external_reference=reference,
        gateway_response={
            "provider": PAYMENT_METHOD_WALLET,
            "payment_plan_id": plan.id,
            "payment_kind": "fuel_advance_client",
            "fuel_advance_id": advance.id,
            "company_id": company.id,
            "trip_id": trip.id,
            "company_credit": float(amount),
            "commission_credit": 0.0,
            "escrow_status": "released_direct",
            "counts_toward_transport": True,
        },
    )
    db.add(payment)
    db.add(
        Transaction(
            wallet_id=client_wallet.id,
            transaction_type="transport_payment",
            amount=amount,
            status=TRANSACTION_STATUS_COMPLETED,
            reference=reference,
            description=f"Pagamento de combustível abatido do transporte da carga {trip.load_id}",
        )
    )
    db.add(
        Transaction(
            wallet_id=company_wallet.id,
            transaction_type="fuel_advance_received",
            amount=amount,
            status=TRANSACTION_STATUS_COMPLETED,
            reference=reference,
            description=f"Combustível pago pelo cliente para a carga {trip.load_id}",
        )
    )

    meta = dict(advance.provider_response or {})
    meta["funding_source"] = "client"
    meta["automatic"] = False
    meta["payment_reference"] = reference
    advance.provider_response = meta
    advance.status = STATUS_PAID
    advance.approved_at = _now()
    advance.paid_at = _now()
    advance.external_reference = reference

    company_notification = _notify(
        db,
        user_id=company.user_id,
        title="Combustível pago",
        body=(
            f"O cliente pagou {amount:.2f} MT para combustível. "
            "O valor já está disponível na carteira da empresa."
        ),
        notification_type="fuel_request.paid",
        payload={
            "fuel_advance_id": advance.id,
            "trip_id": trip.id,
            "load_id": trip.load_id,
            "amount": float(amount),
        },
    )
    client_notification = _notify(
        db,
        user_id=user.id,
        title="Combustível pago",
        body=(
            f"{amount:.2f} MT foram pagos para combustível e abatidos "
            "do valor total do transporte."
        ),
        notification_type="fuel_request.payment_completed",
        payload={
            "fuel_advance_id": advance.id,
            "trip_id": trip.id,
            "load_id": trip.load_id,
            "amount": float(amount),
        },
    )

    db.commit()
    db.refresh(advance)
    db.refresh(company_notification)
    db.refresh(client_notification)
    emit_notification(company_notification)
    emit_notification(client_notification)
    return serialize_fuel_advance(advance)


def reject_client_fuel_request(db: Session, user: User, advance_id: int) -> dict:
    advance, trip, _plan = _get_client_advance(db, user, advance_id)

    if advance.status == STATUS_REJECTED:
        return serialize_fuel_advance(advance)
    if advance.status != STATUS_WAITING_CLIENT:
        raise HTTPException(status_code=400, detail="Esta requisição já não pode ser recusada.")

    company = db.query(Company).filter(Company.id == advance.company_id).first()
    meta = dict(advance.provider_response or {})
    meta["rejected_at"] = _now().isoformat()
    meta["rejected_by_user_id"] = user.id
    advance.provider_response = meta
    advance.status = STATUS_REJECTED

    notification = None
    if company is not None:
        notification = _notify(
            db,
            user_id=company.user_id,
            title="Pedido de combustível recusado",
            body=(
                f"O cliente recusou o pedido de {money(advance.amount):.2f} MT "
                "para combustível."
            ),
            notification_type="fuel_request.rejected",
            payload={
                "fuel_advance_id": advance.id,
                "trip_id": trip.id,
                "load_id": trip.load_id,
                "amount": float(advance.amount),
            },
        )

    db.commit()
    db.refresh(advance)
    if notification is not None:
        db.refresh(notification)
        emit_notification(notification)
    return serialize_fuel_advance(advance)
