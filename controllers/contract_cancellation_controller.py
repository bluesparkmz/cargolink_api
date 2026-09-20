from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from constants import PAYMENT_STATUS_COMPLETED, TRANSACTION_STATUS_COMPLETED
from controllers.notifications_controller import create_notification, emit_notification
from controllers.payment_plan_controller import money
from models.models import (
    Client,
    Company,
    Driver,
    FretixCommission,
    FuelAdvance,
    Load,
    LoadProposal,
    Payment,
    PaymentInstallment,
    PaymentPlan,
    ProposalNegotiation,
    Transaction,
    Trip,
    TripActivity,
    User,
    Vehicle,
    Wallet,
)

CANNOT_CANCEL_TRIP_STATUSES = {
    "carregado",
    "viagem_iniciada",
    "aguardando_cliente",
    "concluida",
    "concluido",
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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


def _assert_not_loaded(trip: Trip | None) -> None:
    if trip is None:
        return
    if trip.loaded_at is not None or trip.status in CANNOT_CANCEL_TRIP_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "O transporte já não pode ser cancelado porque a carga "
                "já foi carregada ou a viagem já avançou."
            ),
        )


def _refund_contract(
    db: Session,
    *,
    load: Load,
    trip: Trip,
    plan: PaymentPlan | None,
    reason: str,
    actor_user_id: int,
) -> Decimal:
    if plan is None:
        return Decimal("0.00")

    refund_amount = money(plan.client_paid_total)
    if refund_amount <= 0:
        db.query(PaymentInstallment).filter(
            PaymentInstallment.plan_id == plan.id
        ).delete(synchronize_session=False)
        db.delete(plan)
        return Decimal("0.00")

    client_wallet = _wallet_locked(db, plan.client_user_id)
    company = db.query(Company).filter(Company.id == plan.company_id).first()
    company_wallet = _wallet_locked(db, company.user_id) if company else None

    # Cliente recebe TUDO o que já pagou no contrato.
    client_wallet.available_balance = money(
        money(client_wallet.available_balance) + refund_amount
    )

    # Reverte o valor líquido já colocado na carteira da transportadora
    # (inclui parcelas 50/50 e combustível). Se já foi usado, o saldo pode
    # ficar negativo: passa a representar obrigação da empresa à Fretix.
    company_net_to_reverse = money(
        money(plan.company_escrow_balance) + money(plan.company_released_total)
    )
    if company_wallet is not None and company_net_to_reverse > 0:
        pending = money(company_wallet.pending_balance)
        pending_debit = min(pending, money(plan.company_escrow_balance))
        company_wallet.pending_balance = money(pending - pending_debit)

        remaining_debit = money(company_net_to_reverse - pending_debit)
        if remaining_debit > 0:
            company_wallet.available_balance = money(
                money(company_wallet.available_balance) - remaining_debit
            )

        db.add(
            Transaction(
                wallet_id=company_wallet.id,
                transaction_type="contract_refund_debit",
                amount=company_net_to_reverse,
                status=TRANSACTION_STATUS_COMPLETED,
                reference=f"REFD{uuid4().hex[:12].upper()}",
                description=(
                    f"Estorno do transporte cancelado da carga {load.code}. "
                    f"Motivo: {reason}"
                ),
            )
        )

    refund_reference = f"REF{uuid4().hex[:14].upper()}"
    db.add(
        Transaction(
            wallet_id=client_wallet.id,
            transaction_type="contract_refund_credit",
            amount=refund_amount,
            status=TRANSACTION_STATUS_COMPLETED,
            reference=refund_reference,
            description=(
                f"Reembolso do transporte cancelado da carga {load.code}. "
                f"Motivo: {reason}"
            ),
        )
    )

    payments = (
        db.query(Payment)
        .filter(
            Payment.load_id == load.id,
            Payment.status == PAYMENT_STATUS_COMPLETED,
        )
        .all()
    )
    for payment in payments:
        meta = dict(payment.gateway_response or {})
        if meta.get("payment_plan_id") != plan.id:
            continue
        meta.update(
            {
                "refunded": True,
                "refunded_at": _now().isoformat(),
                "refund_reason": reason,
                "refund_actor_user_id": actor_user_id,
                "refund_reference": refund_reference,
            }
        )
        payment.gateway_response = meta
        payment.status = "reembolsado"

    for commission in (
        db.query(FretixCommission)
        .filter(FretixCommission.proposal_id == plan.proposal_id)
        .all()
    ):
        commission.status = "estornada"

    for advance in db.query(FuelAdvance).filter(FuelAdvance.trip_id == trip.id).all():
        meta = dict(advance.provider_response or {})
        meta["cancelled_at"] = _now().isoformat()
        meta["cancel_reason"] = reason
        meta["contract_refunded"] = True
        advance.provider_response = meta
        advance.status = "reembolsado" if advance.status == "pago" else "cancelado"

    db.query(PaymentInstallment).filter(
        PaymentInstallment.plan_id == plan.id
    ).delete(synchronize_session=False)
    db.delete(plan)
    return refund_amount


def cancel_transport_contract(
    db: Session,
    user: User,
    load_id: int,
    *,
    reason: str,
) -> dict:
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise HTTPException(
            status_code=400,
            detail="Informe o motivo do cancelamento com pelo menos 5 caracteres.",
        )

    load = db.query(Load).filter(Load.id == load_id).with_for_update().first()
    if load is None:
        raise HTTPException(status_code=404, detail="Carga não encontrada.")

    client = db.query(Client).filter(Client.id == load.client_id).first()
    if client is None:
        raise HTTPException(status_code=404, detail="Cliente da carga não encontrado.")

    trip = (
        db.query(Trip)
        .filter(Trip.load_id == load.id)
        .order_by(Trip.created_at.desc(), Trip.id.desc())
        .with_for_update()
        .first()
    )
    company = None
    if user.user_type == "empresa":
        company = db.query(Company).filter(Company.user_id == user.id).first()

    if user.user_type == "cliente" and client.user_id == user.id:
        actor_side = "cliente"
    elif company is not None:
        involved = bool(trip and trip.company_id == company.id)
        if not involved:
            involved = (
                db.query(LoadProposal)
                .filter(
                    LoadProposal.load_id == load.id,
                    LoadProposal.company_id == company.id,
                    LoadProposal.status.in_(["pendente", "em_negociacao", "aceite"]),
                )
                .first()
                is not None
            )
        if not involved:
            raise HTTPException(
                status_code=403,
                detail="Esta empresa não participa nesta carga.",
            )
        actor_side = "empresa"
    else:
        raise HTTPException(
            status_code=403,
            detail="Apenas o cliente ou a empresa envolvida pode cancelar.",
        )

    _assert_not_loaded(trip)

    refunded = Decimal("0.00")
    affected_company_user_id = None

    if trip is not None:
        current_company = (
            db.query(Company).filter(Company.id == trip.company_id).first()
            if trip.company_id
            else None
        )
        if current_company is not None:
            affected_company_user_id = current_company.user_id

        if actor_side == "empresa" and company is not None and trip.company_id != company.id:
            raise HTTPException(status_code=403, detail="A viagem pertence a outra empresa.")

        plan = db.query(PaymentPlan).filter(PaymentPlan.load_id == load.id).first()
        refunded = _refund_contract(
            db,
            load=load,
            trip=trip,
            plan=plan,
            reason=reason,
            actor_user_id=user.id,
        )

        accepted = (
            db.query(LoadProposal)
            .filter(
                LoadProposal.load_id == load.id,
                LoadProposal.status == "aceite",
            )
            .all()
        )
        for proposal in accepted:
            proposal.status = "cancelada"

        if accepted:
            (
                db.query(ProposalNegotiation)
                .filter(
                    ProposalNegotiation.proposal_id.in_([p.id for p in accepted]),
                    ProposalNegotiation.status == "pendente",
                )
                .update({"status": "cancelada"}, synchronize_session=False)
            )

        if trip.driver_id:
            driver = db.query(Driver).filter(Driver.id == trip.driver_id).first()
            if driver is not None:
                driver.available = True
        if trip.vehicle_id:
            vehicle = db.query(Vehicle).filter(Vehicle.id == trip.vehicle_id).first()
            if vehicle is not None and vehicle.status not in {"manutencao", "inativo"}:
                vehicle.status = "disponivel"

        trip.status = "cancelada"
        trip.driver_id = None
        trip.vehicle_id = None
        db.add(
            TripActivity(
                trip_id=trip.id,
                event_type="transport_cancelled",
                title="Transporte cancelado",
                description=reason,
            )
        )
    else:
        query = db.query(LoadProposal).filter(
            LoadProposal.load_id == load.id,
            LoadProposal.status.in_(["pendente", "em_negociacao"]),
        )
        if actor_side == "empresa" and company is not None:
            query = query.filter(LoadProposal.company_id == company.id)
        proposals = query.all()
        for proposal in proposals:
            proposal.status = "cancelada"
        if proposals:
            (
                db.query(ProposalNegotiation)
                .filter(
                    ProposalNegotiation.proposal_id.in_([p.id for p in proposals]),
                    ProposalNegotiation.status == "pendente",
                )
                .update({"status": "cancelada"}, synchronize_session=False)
            )

    # O contrato foi cancelado, não a publicação. A carga volta ao mercado.
    load.status = "disponivel"

    notifications = [
        create_notification(
            db,
            user_id=client.user_id,
            title="Transporte cancelado",
            body=(
                f"O transporte da carga {load.code} foi cancelado. "
                + (
                    f"{refunded:.2f} MT foram reembolsados para a sua carteira. "
                    if refunded > 0
                    else ""
                )
                + "A carga voltou a ficar disponível para novas propostas."
            ),
            notification_type="transport.cancelled",
            payload={
                "load_id": load.id,
                "reason": reason,
                "refunded_amount": float(refunded),
                "reopened": True,
            },
        )
    ]

    if affected_company_user_id and affected_company_user_id != user.id:
        notifications.append(
            create_notification(
                db,
                user_id=affected_company_user_id,
                title="Transporte cancelado",
                body=f"O transporte da carga {load.code} foi cancelado.",
                notification_type="transport.cancelled",
                payload={
                    "load_id": load.id,
                    "reason": reason,
                    "refunded_amount": float(refunded),
                    "reopened": True,
                },
            )
        )

    db.commit()
    for notification in notifications:
        db.refresh(notification)
        emit_notification(notification)

    return {
        "ok": True,
        "load_id": load.id,
        "status": load.status,
        "cancelled_by": actor_side,
        "reason": reason,
        "refunded_amount": float(refunded),
        "reopened": True,
        "currency": "MT",
    }
