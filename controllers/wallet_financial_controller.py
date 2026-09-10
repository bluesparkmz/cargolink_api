from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from constants import PAYMENT_METHOD_WALLET, PAYMENT_STATUS_COMPLETED
from controllers.mpesa_utils import normalize_msisdn
from models.models import (
    Company, FuelAdvance, Payment, Transaction, Trip, User, Vehicle, Wallet,
)

FUEL_ADVANCE_LIMIT_PERCENT = Decimal("50.00")
FUEL_ADVANCE_STATUS_WAITING_PAYOUT = "aprovado_aguardando_desembolso"
FUEL_ADVANCE_STATUS_PAID = "pago"
EXCLUDED_ADVANCE_STATUSES = {"rejeitado", "cancelado", "falhou"}


def _wallet(db: Session, user: User) -> Wallet:
    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
    if wallet is None:
        wallet = Wallet(user_id=user.id)
        db.add(wallet)
        db.commit()
        db.refresh(wallet)
    return wallet


def _held_client_amount(db: Session, user_id: int) -> Decimal:
    rows = (
        db.query(Payment)
        .filter(
            Payment.user_id == user_id,
            Payment.method == PAYMENT_METHOD_WALLET,
            Payment.status == PAYMENT_STATUS_COMPLETED,
        )
        .all()
    )
    total = Decimal("0")
    for payment in rows:
        meta = dict(payment.gateway_response or {})
        if meta.get("escrow_status") == "held":
            total += Decimal(str(payment.amount or 0))
    return total


def get_wallet_summary(db: Session, user: User) -> dict:
    wallet = _wallet(db, user)
    available = Decimal(str(wallet.available_balance or 0))
    pending = Decimal(str(wallet.pending_balance or 0))
    blocked = Decimal(str(wallet.blocked_balance or 0))

    if user.user_type == "cliente":
        frozen = _held_client_amount(db, user.id) + blocked
    else:
        frozen = pending + blocked

    return {
        "available_balance": float(available),
        "pending_balance": float(pending),
        "blocked_balance": float(blocked),
        "accounting_balance": float(available + frozen),
        "frozen_balance": float(frozen),
        "role": user.user_type,
        "currency": "MT",
    }


def _payment_by_reference(db: Session, reference: str | None) -> Payment | None:
    if not reference:
        return None
    return (
        db.query(Payment)
        .filter(Payment.external_reference == reference)
        .order_by(Payment.created_at.desc())
        .first()
    )


def get_wallet_transaction_details(
    db: Session,
    user: User,
    transaction_id: int,
) -> dict:
    wallet = _wallet(db, user)
    tx = (
        db.query(Transaction)
        .filter(
            Transaction.id == transaction_id,
            Transaction.wallet_id == wallet.id,
        )
        .first()
    )
    if tx is None:
        raise HTTPException(status_code=404, detail="Movimento não encontrado.")

    payment = _payment_by_reference(db, tx.reference)
    meta = dict(payment.gateway_response or {}) if payment else {}
    escrow = meta.get("escrow_status")

    load = payment.load if payment else None
    trip = load.trip if load is not None else None

    company = None
    company_id = meta.get("company_id")
    if company_id:
        company = db.query(Company).filter(Company.id == int(company_id)).first()
    elif trip is not None and trip.company_id:
        company = db.query(Company).filter(Company.id == trip.company_id).first()

    if tx.transaction_type == "transport_payment":
        if escrow == "held":
            funds_label = "Congelado na carteira Fretix"
            explanation = (
                "O valor foi retido na carteira Fretix e só será libertado "
                "à empresa depois da confirmação da entrega da carga."
            )
        elif escrow == "released":
            funds_label = "Libertado à transportadora"
            explanation = (
                "A entrega foi confirmada e o valor já foi libertado "
                "à transportadora."
            )
        else:
            funds_label = "Pagamento registado"
            explanation = "Pagamento de transporte registado."
    elif tx.transaction_type == "escrow_received":
        if escrow == "held" or tx.status in {"pendente", "pending"}:
            funds_label = "Em retenção"
            explanation = (
                "Este valor integra o saldo contabilístico, mas ainda não "
                "está disponível para levantamento. Será libertado após a "
                "confirmação da entrega, descontando adiantamentos pagos."
            )
        else:
            funds_label = "Disponível"
            explanation = (
                "O cliente confirmou a entrega e o valor foi libertado "
                "para o saldo disponível."
            )
    else:
        funds_label = tx.status
        explanation = tx.description or "Movimento registado na carteira."

    return {
        "id": tx.id,
        "transaction_type": tx.transaction_type,
        "amount": float(tx.amount),
        "status": tx.status,
        "reference": tx.reference,
        "description": tx.description,
        "created_at": tx.created_at,
        "viewer_role": user.user_type,
        "funds_status": escrow,
        "funds_label": funds_label,
        "explanation": explanation,
        "receipt_available": bool(
            user.user_type == "cliente"
            and tx.transaction_type == "transport_payment"
            and payment is not None
        ),
        "payment": ({
            "id": payment.id,
            "amount": float(payment.amount),
            "method": payment.method,
            "status": payment.status,
            "external_reference": payment.external_reference,
            "escrow_status": escrow,
            "held_at": meta.get("held_at"),
            "released_at": meta.get("released_at"),
        } if payment else None),
        "load": ({
            "id": load.id,
            "code": load.code,
            "load_name": load.load_name,
            "origin": load.origin,
            "destination": load.destination,
        } if load else None),
        "trip": ({
            "id": trip.id,
            "status": trip.status,
            "vehicle_id": trip.vehicle_id,
        } if trip else None),
        "company": ({
            "id": company.id,
            "company_name": company.company_name,
        } if company else None),
        "commission": {
            "percent": float(meta.get("commission_percent", 0) or 0),
            "amount": float(meta.get("commission_amount", 0) or 0),
            "company_net_amount": float(
                meta.get(
                    "company_net_amount",
                    payment.amount if payment else tx.amount,
                ) or 0
            ),
        },
        "currency": "MT",
    }


def _company(db: Session, user: User) -> Company:
    if user.user_type != "empresa":
        raise HTTPException(
            status_code=403,
            detail="Apenas empresas podem solicitar combustível.",
        )
    company = db.query(Company).filter(Company.user_id == user.id).first()
    if company is None:
        raise HTTPException(status_code=404, detail="Empresa não encontrada.")
    return company


def _trip(db: Session, company: Company, trip_id: int) -> Trip:
    trip = (
        db.query(Trip)
        .filter(Trip.id == trip_id, Trip.company_id == company.id)
        .first()
    )
    if trip is None:
        raise HTTPException(
            status_code=404,
            detail="Viagem não encontrada para esta empresa.",
        )
    return trip


def _transport_payment(db: Session, trip: Trip) -> Payment | None:
    return (
        db.query(Payment)
        .filter(
            Payment.load_id == trip.load_id,
            Payment.method == PAYMENT_METHOD_WALLET,
            Payment.status == PAYMENT_STATUS_COMPLETED,
        )
        .order_by(Payment.created_at.desc())
        .first()
    )


def _advance_totals(db: Session, trip_id: int) -> tuple[Decimal, Decimal]:
    rows = db.query(FuelAdvance).filter(FuelAdvance.trip_id == trip_id).all()
    reserved = Decimal("0")
    paid = Decimal("0")
    for row in rows:
        if row.status in EXCLUDED_ADVANCE_STATUSES:
            continue
        amount = Decimal(str(row.amount or 0))
        reserved += amount
        if row.status == FUEL_ADVANCE_STATUS_PAID:
            paid += amount
    return reserved, paid


def get_fuel_advance_eligibility(
    db: Session,
    user: User,
    trip_id: int,
) -> dict:
    company = _company(db, user)
    trip = _trip(db, company, trip_id)
    payment = _transport_payment(db, trip)

    reasons: list[str] = []
    net = Decimal("0")
    escrow = None

    if payment is None:
        reasons.append("O cliente ainda não pagou o transporte.")
    else:
        meta = dict(payment.gateway_response or {})
        escrow = meta.get("escrow_status")
        net = Decimal(str(meta.get("company_net_amount", payment.amount) or 0))
        if escrow != "held":
            reasons.append(
                "O pagamento precisa estar em retenção para permitir o adiantamento."
            )

    if trip.status in {"concluida", "cancelada", "cancelado"}:
        reasons.append("A viagem já está encerrada.")

    vehicle = None
    if trip.vehicle_id is None:
        reasons.append("É necessário atribuir um camião à viagem.")
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

    max_allowed = (
        net * FUEL_ADVANCE_LIMIT_PERCENT / Decimal("100")
    ).quantize(Decimal("0.01"))
    reserved, paid = _advance_totals(db, trip.id)
    remaining = max(Decimal("0"), max_allowed - reserved)

    if max_allowed > 0 and remaining <= 0:
        reasons.append("O limite de 50% já foi atingido.")

    return {
        "trip_id": trip.id,
        "load_id": trip.load_id,
        "client_paid": payment is not None,
        "escrow_status": escrow,
        "company_net_transport": float(net),
        "limit_percent": 50.0,
        "max_allowed_amount": float(max_allowed),
        "already_reserved_amount": float(reserved),
        "already_paid_amount": float(paid),
        "remaining_requestable_amount": float(remaining),
        "vehicle": ({
            "id": vehicle.id,
            "plate": vehicle.plate,
            "brand": vehicle.brand,
            "model_name": vehicle.model_name,
        } if vehicle else None),
        "can_request": not reasons and remaining > 0,
        "reasons": reasons,
        "currency": "MT",
    }


def create_fuel_advance_request(
    db: Session,
    user: User,
    trip_id: int,
    *,
    vehicle_id: int,
    amount: Decimal,
    mpesa_phone: str,
) -> dict:
    company = _company(db, user)
    trip = _trip(db, company, trip_id)
    eligibility = get_fuel_advance_eligibility(db, user, trip_id)

    if not eligibility["can_request"]:
        raise HTTPException(
            status_code=400,
            detail=" ".join(eligibility["reasons"]) or "Pedido indisponível.",
        )

    if trip.vehicle_id != vehicle_id:
        raise HTTPException(
            status_code=400,
            detail="Seleccione o camião atribuído a esta viagem.",
        )

    amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    remaining = Decimal(str(eligibility["remaining_requestable_amount"]))
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Valor inválido.")
    if amount > remaining:
        raise HTTPException(
            status_code=400,
            detail=f"O máximo disponível é {remaining:.2f} MT.",
        )

    phone = normalize_msisdn(mpesa_phone)

    advance = FuelAdvance(
        trip_id=trip.id,
        company_id=company.id,
        vehicle_id=vehicle_id,
        requested_by_user_id=user.id,
        amount=amount,
        mpesa_phone=phone,
        max_allowed_amount=Decimal(str(eligibility["max_allowed_amount"])),
        status=FUEL_ADVANCE_STATUS_WAITING_PAYOUT,
        approved_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(advance)
    db.commit()
    db.refresh(advance)

    return {
        "id": advance.id,
        "trip_id": advance.trip_id,
        "vehicle_id": advance.vehicle_id,
        "amount": float(advance.amount),
        "mpesa_phone": advance.mpesa_phone,
        "status": advance.status,
        "max_allowed_amount": float(advance.max_allowed_amount),
        "approved_at": advance.approved_at,
        "paid_at": advance.paid_at,
        "external_reference": advance.external_reference,
        "currency": "MT",
        "message": (
            "Pedido validado e aprovado. Aguardando desembolso M-Pesa. "
            "O envio automático exige a integração B2C/Disbursement."
        ),
    }


def list_fuel_advances(
    db: Session,
    user: User,
    trip_id: int,
) -> list[dict]:
    company = _company(db, user)
    trip = _trip(db, company, trip_id)
    rows = (
        db.query(FuelAdvance)
        .filter(FuelAdvance.trip_id == trip.id)
        .order_by(FuelAdvance.created_at.desc())
        .all()
    )
    return [
        {
            "id": row.id,
            "trip_id": row.trip_id,
            "vehicle_id": row.vehicle_id,
            "amount": float(row.amount),
            "mpesa_phone": row.mpesa_phone,
            "status": row.status,
            "max_allowed_amount": float(row.max_allowed_amount),
            "approved_at": row.approved_at,
            "paid_at": row.paid_at,
            "external_reference": row.external_reference,
            "created_at": row.created_at,
            "currency": "MT",
        }
        for row in rows
    ]


def get_paid_fuel_advance_total(db: Session, trip_id: int) -> Decimal:
    _reserved, paid = _advance_totals(db, trip_id)
    return paid
