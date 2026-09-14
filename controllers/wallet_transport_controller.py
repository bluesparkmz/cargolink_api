from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from constants import (
    PAYMENT_METHOD_WALLET,
    PAYMENT_STATUS_COMPLETED,
    PROPOSAL_STATUS_ACCEPTED,
    TRANSACTION_STATUS_COMPLETED,
    TRANSACTION_STATUS_PENDING,
    TRANSACTION_TYPE_ESCROW_RECEIVED,
    TRANSACTION_TYPE_TRANSPORT_PAYMENT,
)
from controllers.notifications_controller import create_notification, emit_notification
from controllers.payment_plan_controller import (
    allocate_regular_client_payment,
    client_remaining,
    ensure_payment_plan_for_proposal,
    ensure_payment_plan_for_trip,
    money,
    next_regular_payment,
    serialize_payment_plan,
)
from controllers.wallet_controller import get_or_create_wallet
from models.models import (
    AdditionalCharge,
    Client,
    Company,
    FretixCommission,
    FuelAdvance,
    Load,
    LoadProposal,
    Payment,
    PaymentPlan,
    Transaction,
    Trip,
    User,
    Wallet,
)

logger = logging.getLogger(__name__)


def _get_or_create_wallet_locked(db: Session, user_id: int) -> Wallet:
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


def _get_client_proposal_for_payment(
    db: Session,
    user: User,
    proposal_id: int,
) -> tuple[LoadProposal, Client, Trip | None, PaymentPlan]:
    if user.user_type != "cliente":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Apenas o cliente pode pagar a proposta.",
        )

    client = db.query(Client).filter(Client.user_id == user.id).first()
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Perfil do cliente não encontrado.",
        )

    proposal = db.query(LoadProposal).filter(LoadProposal.id == proposal_id).first()
    if proposal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Proposta não encontrada.",
        )

    if proposal.load is None or proposal.load.client_id != client.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esta proposta não pertence ao cliente autenticado.",
        )

    if proposal.status != PROPOSAL_STATUS_ACCEPTED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A proposta precisa estar aceite antes do pagamento.",
        )

    trip = db.query(Trip).filter(Trip.load_id == proposal.load_id).first()
    plan = ensure_payment_plan_for_proposal(db, proposal, trip=trip)
    return proposal, client, trip, plan


def _payment_payload(
    db: Session,
    *,
    proposal: LoadProposal,
    trip: Trip | None,
    plan: PaymentPlan,
    wallet: Wallet,
) -> dict:
    summary = serialize_payment_plan(db, plan, trip=trip)
    return {
        "paid": summary["client_remaining"] <= 0,
        "partially_paid": (
            summary["client_paid_total"] > 0
            and summary["client_remaining"] > 0
        ),
        "proposal_id": proposal.id,
        "load_id": proposal.load_id,
        "trip_id": trip.id if trip else plan.trip_id,
        "amount": summary["contract_amount"],
        "paid_amount": summary["client_paid_total"],
        "remaining_amount": summary["client_remaining"],
        "payment_mode": summary["mode"],
        "payment_plan": summary,
        "next_payment_amount": summary["next_payment"]["amount"],
        "payment_available": summary["next_payment"]["available"],
        "payment_block_reason": summary["next_payment"]["reason"],
        "available_balance": float(wallet.available_balance or Decimal("0")),
        "currency": "MT",
    }


def get_transport_payment_status(db: Session, user: User, proposal_id: int) -> dict:
    proposal, _client, trip, plan = _get_client_proposal_for_payment(
        db, user, proposal_id
    )
    wallet = get_or_create_wallet(db, user)
    db.flush()
    return _payment_payload(
        db,
        proposal=proposal,
        trip=trip,
        plan=plan,
        wallet=wallet,
    )


def _maybe_register_contract_commission(
    db: Session,
    plan: PaymentPlan,
    payment: Payment,
) -> None:
    if money(plan.commission_collected) < money(plan.commission_amount):
        return

    existing = (
        db.query(FretixCommission)
        .filter(FretixCommission.proposal_id == plan.proposal_id)
        .first()
    )
    if existing is not None:
        return

    db.add(
        FretixCommission(
            payment_id=payment.id,
            trip_id=plan.trip_id,
            proposal_id=plan.proposal_id,
            base_amount=money(plan.contract_amount),
            commission_percent=money(plan.commission_percent),
            commission_amount=money(plan.commission_amount),
            status="registada",
        )
    )


def pay_accepted_proposal_from_wallet(
    db: Session,
    user: User,
    proposal_id: int,
) -> dict:
    proposal, _client, trip, plan = _get_client_proposal_for_payment(
        db, user, proposal_id
    )

    company = db.query(Company).filter(Company.id == proposal.company_id).first()
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Empresa transportadora não encontrada.",
        )

    payment_info = next_regular_payment(db, plan, trip)
    if not payment_info["available"]:
        if client_remaining(plan) <= 0:
            wallet = get_or_create_wallet(db, user)
            return _payment_payload(
                db,
                proposal=proposal,
                trip=trip,
                plan=plan,
                wallet=wallet,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=payment_info["reason"] or "Pagamento indisponível.",
        )

    amount = money(payment_info["amount"])
    client_wallet = _get_or_create_wallet_locked(db, user.id)
    company_wallet = _get_or_create_wallet_locked(db, company.user_id)

    available = money(client_wallet.available_balance)
    if available < amount:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Saldo insuficiente. Disponível: {available:.2f} MT; "
                f"necessário: {amount:.2f} MT."
            ),
        )

    allocation = allocate_regular_client_payment(db, plan, amount)
    company_credit = money(allocation["company_credit"])
    commission_credit = money(allocation["commission_credit"])
    immediate_release = bool(allocation.get("immediate_release"))

    client_wallet.available_balance = money(available - amount)

    if company_credit > 0:
        if immediate_release:
            company_wallet.available_balance = money(
                money(company_wallet.available_balance) + company_credit
            )
        else:
            company_wallet.pending_balance = money(
                money(company_wallet.pending_balance) + company_credit
            )

    reference = f"FW{uuid4().hex[:16].upper()}"
    payment = Payment(
        user_id=user.id,
        load_id=proposal.load_id,
        method=PAYMENT_METHOD_WALLET,
        phone=None,
        amount=amount,
        status=PAYMENT_STATUS_COMPLETED,
        external_reference=reference,
        gateway_response={
            "provider": PAYMENT_METHOD_WALLET,
            "proposal_id": proposal.id,
            "company_id": company.id,
            "trip_id": trip.id if trip else None,
            "payment_plan_id": plan.id,
            "payment_kind": (
                payment_info["installment"].kind
                if payment_info["installment"]
                else "transport_payment"
            ),
            "installment_sequence": allocation.get(
                "target_installment_sequence"
            ),
            "company_credit": float(company_credit),
            "commission_credit": float(commission_credit),
            "escrow_status": (
                "released_immediate"
                if immediate_release
                else ("held" if company_credit > 0 else "commission_only")
            ),
            "released_at": (
                datetime.now(timezone.utc).isoformat()
                if immediate_release
                else None
            ),
        },
    )
    db.add(payment)

    db.add(
        Transaction(
            wallet_id=client_wallet.id,
            transaction_type=TRANSACTION_TYPE_TRANSPORT_PAYMENT,
            amount=amount,
            status=TRANSACTION_STATUS_COMPLETED,
            reference=reference,
            description=(
                f"Pagamento do transporte da carga "
                f"{proposal.load.code if proposal.load else proposal.load_id}"
            ),
        )
    )

    if company_credit > 0:
        db.add(
            Transaction(
                wallet_id=company_wallet.id,
                transaction_type=(
                    "transport_payment_received"
                    if immediate_release
                    else TRANSACTION_TYPE_ESCROW_RECEIVED
                ),
                amount=company_credit,
                status=(
                    TRANSACTION_STATUS_COMPLETED
                    if immediate_release
                    else TRANSACTION_STATUS_PENDING
                ),
                reference=reference,
                description=(
                    f"Parcela disponível da carga "
                    f"{proposal.load.code if proposal.load else proposal.load_id}"
                    if immediate_release
                    else (
                        f"Valor em retenção da carga "
                        f"{proposal.load.code if proposal.load else proposal.load_id}"
                    )
                ),
            )
        )

    db.flush()
    _maybe_register_contract_commission(db, plan, payment)

    company_notification = create_notification(
        db,
        user_id=company.user_id,
        title="Parcela disponível" if immediate_release else "Pagamento recebido",
        body=(
            (
                f"O cliente pagou {amount:.2f} MT. "
                f"{company_credit:.2f} MT já estão disponíveis na carteira"
                + (
                    f" e {commission_credit:.2f} MT foram cobrados de comissão."
                    if commission_credit > 0
                    else "."
                )
            )
            if immediate_release
            else (
                f"O cliente pagou {amount:.2f} MT da carga. "
                f"Saldo do contrato: {client_remaining(plan):.2f} MT."
            )
        ),
        notification_type="payment.installment_paid",
        payload={
            "proposal_id": proposal.id,
            "load_id": proposal.load_id,
            "trip_id": trip.id if trip else None,
            "amount": float(amount),
            "company_credit": float(company_credit),
            "commission_credit": float(commission_credit),
            "immediate_release": immediate_release,
            "remaining_amount": float(client_remaining(plan)),
            "payment_plan_id": plan.id,
        },
    )

    db.commit()
    db.refresh(payment)
    db.refresh(client_wallet)
    db.refresh(company_wallet)
    db.refresh(company_notification)
    emit_notification(company_notification)

    if (
        not immediate_release
        and trip is not None
        and trip.client_confirmed_at
    ):
        release_transport_escrow_for_trip(db, trip)

    return _payment_payload(
        db,
        proposal=proposal,
        trip=trip,
        plan=plan,
        wallet=client_wallet,
    )

def release_transport_escrow_for_trip(db: Session, trip: Trip) -> bool:
    try:
        plan = ensure_payment_plan_for_trip(db, trip)
    except HTTPException:
        return False

    amount = money(plan.company_escrow_balance)
    if amount <= 0:
        return False

    company = db.query(Company).filter(Company.id == plan.company_id).first()
    if company is None:
        return False

    company_wallet = _get_or_create_wallet_locked(db, company.user_id)
    pending = money(company_wallet.pending_balance)
    if pending < amount:
        logger.error(
            "Saldo pendente insuficiente: trip=%s pending=%s amount=%s",
            trip.id,
            pending,
            amount,
        )
        db.rollback()
        return False

    company_wallet.pending_balance = money(pending - amount)
    company_wallet.available_balance = money(
        money(company_wallet.available_balance) + amount
    )
    plan.company_escrow_balance = Decimal("0.00")
    plan.company_released_total = money(
        money(plan.company_released_total) + amount
    )

    payments = (
        db.query(Payment)
        .filter(
            Payment.load_id == trip.load_id,
            Payment.method == PAYMENT_METHOD_WALLET,
            Payment.status == PAYMENT_STATUS_COMPLETED,
        )
        .all()
    )
    refs: list[str] = []
    for payment in payments:
        metadata = dict(payment.gateway_response or {})
        if metadata.get("payment_plan_id") == plan.id:
            metadata["escrow_status"] = "released"
            metadata["released_at"] = datetime.now(timezone.utc).isoformat()
            payment.gateway_response = metadata
            if payment.external_reference:
                refs.append(payment.external_reference)

    if refs:
        (
            db.query(Transaction)
            .filter(
                Transaction.wallet_id == company_wallet.id,
                Transaction.reference.in_(refs),
                Transaction.transaction_type == TRANSACTION_TYPE_ESCROW_RECEIVED,
                Transaction.status == TRANSACTION_STATUS_PENDING,
            )
            .update(
                {
                    "status": TRANSACTION_STATUS_COMPLETED,
                    "description": (
                        f"Valor libertado após confirmação da entrega "
                        f"da carga {trip.load_id}"
                    ),
                },
                synchronize_session=False,
            )
        )

    company_notification = create_notification(
        db,
        user_id=company.user_id,
        title="Pagamento libertado",
        body=(
            f"{amount:.2f} MT foram libertados para o saldo disponível "
            "da empresa."
        ),
        notification_type="wallet.payment_released",
        payload={
            "trip_id": trip.id,
            "load_id": trip.load_id,
            "amount": float(amount),
            "payment_plan_id": plan.id,
        },
    )

    client_notification = create_notification(
        db,
        user_id=plan.client_user_id,
        title="Pagamento actualizado",
        body=(
            "A entrega foi confirmada e o valor elegível foi libertado "
            "à transportadora."
        ),
        notification_type="wallet.payment_completed",
        payload={
            "trip_id": trip.id,
            "load_id": trip.load_id,
            "amount": float(amount),
            "payment_plan_id": plan.id,
        },
    )

    db.commit()
    db.refresh(company_notification)
    db.refresh(client_notification)
    emit_notification(company_notification)
    emit_notification(client_notification)
    return True


def _assert_finance_access(
    db: Session,
    user: User,
    load: Load,
    trip: Trip | None,
) -> None:
    if user.user_type == "admin":
        return

    if user.user_type == "cliente":
        client = db.query(Client).filter(Client.user_id == user.id).first()
        if client and client.id == load.client_id:
            return

    if user.user_type == "empresa" and trip is not None:
        company = db.query(Company).filter(Company.user_id == user.id).first()
        if company and company.id == trip.company_id:
            return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Sem acesso ao histórico financeiro desta carga.",
    )


def get_load_financial_history(
    db: Session,
    user: User,
    load_id: int,
) -> dict:
    if user.user_type == "motorista":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Informação financeira não está disponível para o motorista.",
        )

    load = db.query(Load).filter(Load.id == load_id).first()
    if load is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Carga não encontrada.",
        )

    trip = db.query(Trip).filter(Trip.load_id == load_id).first()
    _assert_finance_access(db, user, load, trip)

    plan = db.query(PaymentPlan).filter(PaymentPlan.load_id == load_id).first()
    plan_payload = serialize_payment_plan(db, plan, trip=trip) if plan else None

    payments = (
        db.query(Payment)
        .filter(
            Payment.load_id == load_id,
            Payment.status == PAYMENT_STATUS_COMPLETED,
        )
        .order_by(Payment.created_at.asc())
        .all()
    )

    fuel_rows = []
    charges = []
    if trip is not None:
        fuel_rows = (
            db.query(FuelAdvance)
            .filter(FuelAdvance.trip_id == trip.id)
            .order_by(FuelAdvance.created_at.asc())
            .all()
        )
        charges = (
            db.query(AdditionalCharge)
            .filter(AdditionalCharge.trip_id == trip.id)
            .order_by(AdditionalCharge.created_at.asc())
            .all()
        )

    return {
        "load_id": load.id,
        "load_code": load.code,
        "trip_id": trip.id if trip else None,
        "payment_plan": plan_payload,
        "payments": [
            {
                "id": row.id,
                "amount": float(row.amount),
                "method": row.method,
                "status": row.status,
                "reference": row.external_reference,
                "payment_kind": dict(row.gateway_response or {}).get("payment_kind"),
                "created_at": row.created_at,
            }
            for row in payments
        ],
        "fuel_requests": [
            {
                "id": row.id,
                "amount": float(row.amount),
                "status": row.status,
                "funding_source": dict(row.provider_response or {}).get("funding_source"),
                "created_at": row.created_at,
                "paid_at": row.paid_at,
            }
            for row in fuel_rows
        ],
        "additional_charges": [
            {
                "id": row.id,
                "amount": float(row.amount),
                "status": row.status,
                "description": row.description,
                "created_at": row.created_at,
            }
            for row in charges
        ],
        "currency": "MT",
    }
