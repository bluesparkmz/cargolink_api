from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from constants import (
    NEGOTIATION_STATUS_ACCEPTED,
    PAYMENT_METHOD_WALLET,
    PAYMENT_STATUS_COMPLETED,
    PROPOSAL_STATUS_ACCEPTED,
    TRANSACTION_STATUS_COMPLETED,
    TRANSACTION_STATUS_PENDING,
    TRANSACTION_TYPE_ESCROW_RECEIVED,
    TRANSACTION_TYPE_TRANSPORT_PAYMENT,
)
from controllers.notifications_controller import create_notification, emit_notification
from controllers.wallet_controller import get_or_create_wallet
from controllers.financial_settings import get_financial_setting
from models.models import (
    Client,
    Company,
    FretixCommission,
    LoadProposal,
    Payment,
    ProposalNegotiation,
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


def _get_accepted_transport_amount(db: Session, proposal: LoadProposal) -> Decimal:
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
        return Decimal(str(accepted_negotiation.amount))
    if proposal.proposed_value is None:
        return Decimal("0")
    return Decimal(str(proposal.proposed_value))


def _get_existing_wallet_transport_payment(
    db: Session,
    *,
    user_id: int,
    load_id: int,
) -> Payment | None:
    return (
        db.query(Payment)
        .filter(
            Payment.user_id == user_id,
            Payment.load_id == load_id,
            Payment.method == PAYMENT_METHOD_WALLET,
            Payment.status == PAYMENT_STATUS_COMPLETED,
        )
        .order_by(Payment.created_at.desc())
        .first()
    )


def _transport_payment_payload(
    *,
    proposal: LoadProposal,
    trip: Trip | None,
    payment: Payment | None,
    amount: Decimal,
    wallet: Wallet,
) -> dict:
    metadata = dict(payment.gateway_response or {}) if payment else {}
    escrow_status = metadata.get("escrow_status") if payment else None

    if payment and escrow_status == "held":
        message = "Pagamento efectuado. O valor está em retenção até à conclusão da entrega."
    elif payment and escrow_status == "released":
        message = "Pagamento concluído e valor libertado para a transportadora."
    else:
        message = "Pagamento ainda não efectuado."

    return {
        "paid": payment is not None,
        "payment_id": payment.id if payment else None,
        "proposal_id": proposal.id,
        "load_id": proposal.load_id,
        "trip_id": trip.id if trip else metadata.get("trip_id"),
        "amount": float(amount),
        "commission_percent": float(metadata.get("commission_percent", 0) or 0),
        "commission_amount": float(metadata.get("commission_amount", 0) or 0),
        "company_net_amount": float(metadata.get("company_net_amount", amount) or amount),
        "status": payment.status if payment else "nao_pago",
        "escrow_status": escrow_status,
        "available_balance": float(wallet.available_balance or Decimal("0")),
        "currency": "MT",
        "message": message,
    }


def _get_client_proposal_for_payment(
    db: Session,
    user: User,
    proposal_id: int,
) -> tuple[LoadProposal, Client, Trip | None, Decimal]:
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
            detail="Esta proposta não pertence a uma carga do cliente autenticado.",
        )

    if proposal.status != PROPOSAL_STATUS_ACCEPTED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A proposta precisa estar aceite antes do pagamento.",
        )

    if proposal.company_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A proposta aceite não possui empresa transportadora.",
        )

    amount = _get_accepted_transport_amount(db, proposal)
    if amount <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A proposta aceite não possui um valor válido para pagamento.",
        )

    trip = db.query(Trip).filter(Trip.load_id == proposal.load_id).first()
    return proposal, client, trip, amount


def get_transport_payment_status(db: Session, user: User, proposal_id: int) -> dict:
    proposal, _client, trip, amount = _get_client_proposal_for_payment(
        db, user, proposal_id
    )
    wallet = get_or_create_wallet(db, user)
    payment = _get_existing_wallet_transport_payment(
        db,
        user_id=user.id,
        load_id=proposal.load_id,
    )
    return _transport_payment_payload(
        proposal=proposal,
        trip=trip,
        payment=payment,
        amount=amount,
        wallet=wallet,
    )


def pay_accepted_proposal_from_wallet(
    db: Session,
    user: User,
    proposal_id: int,
) -> dict:
    proposal, _client, trip, amount = _get_client_proposal_for_payment(
        db, user, proposal_id
    )

    company = db.query(Company).filter(Company.id == proposal.company_id).first()
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Empresa transportadora não encontrada.",
        )

    client_wallet = _get_or_create_wallet_locked(db, user.id)

    existing = _get_existing_wallet_transport_payment(
        db,
        user_id=user.id,
        load_id=proposal.load_id,
    )
    if existing is not None:
        return _transport_payment_payload(
            proposal=proposal,
            trip=trip,
            payment=existing,
            amount=amount,
            wallet=client_wallet,
        )

    company_wallet = _get_or_create_wallet_locked(db, company.user_id)

    available = client_wallet.available_balance or Decimal("0")
    if available < amount:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Saldo insuficiente. Disponível: {available:.2f} MT; "
                f"necessário: {amount:.2f} MT."
            ),
        )

    commission_percent = get_financial_setting(
        db,
        "fretix_commission_percent",
        Decimal("30.00"),
    )
    commission_amount = (
        amount * commission_percent / Decimal("100")
    ).quantize(Decimal("0.01"))
    company_net_amount = amount - commission_amount

    reference = f"FW{uuid4().hex[:16].upper()}"

    client_wallet.available_balance = available - amount
    company_wallet.pending_balance = (
        company_wallet.pending_balance or Decimal("0")
    ) + company_net_amount

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
            "escrow_status": "held",
            "commission_percent": float(commission_percent),
            "commission_amount": float(commission_amount),
            "company_net_amount": float(company_net_amount),
            "held_at": datetime.now(timezone.utc).isoformat(),
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

    db.add(
        Transaction(
            wallet_id=company_wallet.id,
            transaction_type=TRANSACTION_TYPE_ESCROW_RECEIVED,
            amount=company_net_amount,
            status=TRANSACTION_STATUS_PENDING,
            reference=reference,
            description=(
                f"Pagamento em retenção da carga "
                f"{proposal.load.code if proposal.load else proposal.load_id}"
            ),
        )
    )

    db.flush()

    db.add(
        FretixCommission(
            payment_id=payment.id,
            trip_id=trip.id if trip else None,
            proposal_id=proposal.id,
            base_amount=amount,
            commission_percent=commission_percent,
            commission_amount=commission_amount,
            status="registada",
        )
    )

    company_notification = create_notification(
        db,
        user_id=company.user_id,
        title="Pagamento recebido",
        body=(
            f"O cliente pagou {amount:.2f} MT. "
            f"Comissão Fretix: {commission_amount:.2f} MT "
            f"({commission_percent:.2f}%). "
            f"Valor líquido em retenção: {company_net_amount:.2f} MT."
        ),
        notification_type="wallet.payment_held",
        payload={
            "proposal_id": proposal.id,
            "load_id": proposal.load_id,
            "trip_id": trip.id if trip else None,
            "amount": float(amount),
            "commission_percent": float(commission_percent),
            "commission_amount": float(commission_amount),
            "company_net_amount": float(company_net_amount),
        },
    )

    try:
        db.commit()
        db.refresh(payment)
        db.refresh(client_wallet)
        db.refresh(company_wallet)
        db.refresh(company_notification)
    except Exception:
        db.rollback()
        raise

    emit_notification(company_notification)

    return _transport_payment_payload(
        proposal=proposal,
        trip=trip,
        payment=payment,
        amount=amount,
        wallet=client_wallet,
    )


def release_transport_escrow_for_trip(db: Session, trip: Trip) -> bool:
    payment = (
        db.query(Payment)
        .filter(
            Payment.load_id == trip.load_id,
            Payment.method == PAYMENT_METHOD_WALLET,
            Payment.status == PAYMENT_STATUS_COMPLETED,
        )
        .order_by(Payment.created_at.desc())
        .first()
    )
    if payment is None:
        return False

    metadata = dict(payment.gateway_response or {})
    if metadata.get("escrow_status") != "held":
        return False

    company_id = metadata.get("company_id") or trip.company_id
    if not company_id:
        logger.error(
            "Escrow sem company_id: payment_id=%s trip_id=%s",
            payment.id,
            trip.id,
        )
        return False

    company = db.query(Company).filter(Company.id == int(company_id)).first()
    if company is None:
        logger.error(
            "Empresa do escrow não encontrada: payment_id=%s company_id=%s",
            payment.id,
            company_id,
        )
        return False

    company_wallet = _get_or_create_wallet_locked(db, company.user_id)
    gross_amount = Decimal(str(payment.amount))
    amount = Decimal(
        str(metadata.get("company_net_amount", payment.amount))
    )
    pending = company_wallet.pending_balance or Decimal("0")

    if pending < amount:
        logger.error(
            "Saldo pendente insuficiente para libertar escrow: "
            "payment_id=%s pending=%s amount=%s",
            payment.id,
            pending,
            amount,
        )
        db.rollback()
        return False

    company_wallet.pending_balance = pending - amount
    company_wallet.available_balance = (
        company_wallet.available_balance or Decimal("0")
    ) + amount

    company_transaction = (
        db.query(Transaction)
        .filter(
            Transaction.wallet_id == company_wallet.id,
            Transaction.reference == payment.external_reference,
            Transaction.transaction_type == TRANSACTION_TYPE_ESCROW_RECEIVED,
        )
        .order_by(Transaction.created_at.desc())
        .first()
    )
    if company_transaction is not None:
        company_transaction.status = TRANSACTION_STATUS_COMPLETED
        company_transaction.description = (
            f"Pagamento libertado após conclusão da carga {trip.load_id}"
        )

    metadata["escrow_status"] = "released"
    metadata["released_at"] = datetime.now(timezone.utc).isoformat()
    metadata["trip_id"] = trip.id
    payment.gateway_response = metadata

    company_notification = create_notification(
        db,
        user_id=company.user_id,
        title="Pagamento libertado",
        body=(
            f"{amount:.2f} MT foram libertados para o seu saldo disponível "
            "após a confirmação da entrega."
        ),
        notification_type="wallet.payment_released",
        payload={
            "trip_id": trip.id,
            "load_id": trip.load_id,
            "amount": float(amount),
        },
    )

    client_notification = None
    if payment.user_id:
        client_notification = create_notification(
            db,
            user_id=payment.user_id,
            title="Pagamento concluído",
            body=(
                "A entrega foi confirmada e o pagamento foi libertado "
                "à transportadora."
            ),
            notification_type="wallet.payment_completed",
            payload={
                "trip_id": trip.id,
                "load_id": trip.load_id,
                "amount": float(amount),
            },
        )

    try:
        db.commit()
        db.refresh(company_wallet)
        db.refresh(company_notification)
        if client_notification is not None:
            db.refresh(client_notification)
    except Exception:
        db.rollback()
        raise

    emit_notification(company_notification)
    if client_notification is not None:
        emit_notification(client_notification)

    return True
