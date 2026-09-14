from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from constants import NEGOTIATION_STATUS_ACCEPTED, PROPOSAL_STATUS_ACCEPTED
from controllers.financial_settings import get_financial_setting
from models.models import (
    Client,
    LoadProposal,
    PaymentInstallment,
    PaymentPlan,
    ProposalNegotiation,
    Trip,
)

PAYMENT_MODE_INTEGRAL = "integral"
PAYMENT_MODE_SPLIT_50_50 = "parcelas_50_50"
PAYMENT_MODE_AFTER_UNLOADING = "prazo_apos_descarga"
PAYMENT_MODE_FROM_LOADING = "prazo_desde_carregamento"

PAYMENT_MODES = {
    PAYMENT_MODE_INTEGRAL,
    PAYMENT_MODE_SPLIT_50_50,
    PAYMENT_MODE_AFTER_UNLOADING,
    PAYMENT_MODE_FROM_LOADING,
}

DEFERRED_PAYMENT_MODES = {
    PAYMENT_MODE_AFTER_UNLOADING,
    PAYMENT_MODE_FROM_LOADING,
}

MONEY = Decimal("0.01")


def money(value: Decimal | float | int | str | None) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY, rounding=ROUND_HALF_UP)


def now_utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def accepted_transport_amount(db: Session, proposal: LoadProposal) -> Decimal:
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
        return money(accepted_negotiation.amount)
    if proposal.proposed_value is None:
        return Decimal("0.00")
    return money(proposal.proposed_value)


def _client_user_id_for_proposal(db: Session, proposal: LoadProposal) -> int:
    if proposal.load is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A proposta não possui carga associada.",
        )

    client = db.query(Client).filter(Client.id == proposal.load.client_id).first()
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cliente da carga não encontrado.",
        )
    return client.user_id


def _build_installments(db: Session, plan: PaymentPlan) -> None:
    if plan.mode == PAYMENT_MODE_SPLIT_50_50:
        first = money(plan.contract_amount * Decimal("0.50"))
        second = money(plan.contract_amount - first)
        db.add(
            PaymentInstallment(
                plan_id=plan.id,
                sequence=1,
                kind="primeira_parcela_50",
                percent=Decimal("50.00"),
                amount=first,
                paid_amount=Decimal("0.00"),
                status="pendente",
            )
        )
        db.add(
            PaymentInstallment(
                plan_id=plan.id,
                sequence=2,
                kind="segunda_parcela_50",
                percent=Decimal("50.00"),
                amount=second,
                paid_amount=Decimal("0.00"),
                status="pendente",
            )
        )
        return

    kind = {
        PAYMENT_MODE_INTEGRAL: "pagamento_integral",
        PAYMENT_MODE_AFTER_UNLOADING: "pagamento_apos_descarga",
        PAYMENT_MODE_FROM_LOADING: "pagamento_durante_prazo",
    }.get(plan.mode, "pagamento_integral")

    db.add(
        PaymentInstallment(
            plan_id=plan.id,
            sequence=1,
            kind=kind,
            percent=Decimal("100.00"),
            amount=money(plan.contract_amount),
            paid_amount=Decimal("0.00"),
            status="pendente",
        )
    )


def ensure_payment_plan_for_proposal(
    db: Session,
    proposal: LoadProposal,
    *,
    trip: Trip | None = None,
) -> PaymentPlan:
    existing = db.query(PaymentPlan).filter(PaymentPlan.load_id == proposal.load_id).first()
    if existing is not None:
        if trip is not None and existing.trip_id is None:
            existing.trip_id = trip.id
        if existing.proposal_id is None:
            existing.proposal_id = proposal.id
        return existing

    if proposal.status != PROPOSAL_STATUS_ACCEPTED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A proposta precisa estar aceite.",
        )
    if proposal.company_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A proposta aceite não possui empresa.",
        )

    load = proposal.load
    if load is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Carga da proposta não encontrada.",
        )

    contract_amount = accepted_transport_amount(db, proposal)
    if contract_amount <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Valor contratado inválido.",
        )

    mode = getattr(load, "payment_mode", None) or PAYMENT_MODE_INTEGRAL
    if mode not in PAYMENT_MODES:
        mode = PAYMENT_MODE_INTEGRAL

    term_days = getattr(load, "payment_term_days", None)
    if mode in DEFERRED_PAYMENT_MODES:
        term_days = int(term_days or 7)
    else:
        term_days = None

    commission_percent = money(
        get_financial_setting(
            db,
            "fretix_commission_percent",
            Decimal("30.00"),
        )
    )
    commission_amount = money(
        contract_amount * commission_percent / Decimal("100")
    )
    company_net_entitlement = money(contract_amount - commission_amount)

    plan = PaymentPlan(
        load_id=load.id,
        proposal_id=proposal.id,
        trip_id=trip.id if trip else None,
        client_user_id=_client_user_id_for_proposal(db, proposal),
        company_id=proposal.company_id,
        mode=mode,
        term_days=term_days,
        contract_amount=contract_amount,
        commission_percent=commission_percent,
        commission_amount=commission_amount,
        company_net_entitlement=company_net_entitlement,
        client_paid_total=Decimal("0.00"),
        company_escrow_balance=Decimal("0.00"),
        company_released_total=Decimal("0.00"),
        commission_collected=Decimal("0.00"),
        status="pendente",
    )
    db.add(plan)
    db.flush()
    _build_installments(db, plan)
    db.flush()
    return plan


def ensure_payment_plan_for_trip(db: Session, trip: Trip) -> PaymentPlan:
    existing = db.query(PaymentPlan).filter(PaymentPlan.load_id == trip.load_id).first()
    if existing is not None:
        if existing.trip_id is None:
            existing.trip_id = trip.id
        return existing

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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plano de pagamento indisponível: proposta aceite não encontrada.",
        )
    return ensure_payment_plan_for_proposal(db, proposal, trip=trip)


def get_plan_for_load(db: Session, load_id: int) -> PaymentPlan | None:
    return db.query(PaymentPlan).filter(PaymentPlan.load_id == load_id).first()


def _installments(db: Session, plan_id: int) -> list[PaymentInstallment]:
    return (
        db.query(PaymentInstallment)
        .filter(PaymentInstallment.plan_id == plan_id)
        .order_by(PaymentInstallment.sequence.asc())
        .all()
    )


def client_remaining(plan: PaymentPlan) -> Decimal:
    return max(
        Decimal("0.00"),
        money(plan.contract_amount) - money(plan.client_paid_total),
    )


def company_remaining_entitlement(plan: PaymentPlan) -> Decimal:
    return max(
        Decimal("0.00"),
        money(plan.company_net_entitlement)
        - money(plan.company_released_total)
        - money(plan.company_escrow_balance),
    )


def apply_payment_to_installments(
    db: Session,
    plan: PaymentPlan,
    amount: Decimal,
    *,
    paid_at: datetime | None = None,
) -> None:
    remaining = money(amount)
    timestamp = paid_at or now_utc_naive()

    for installment in _installments(db, plan.id):
        if remaining <= 0:
            break
        unpaid = max(
            Decimal("0.00"),
            money(installment.amount) - money(installment.paid_amount),
        )
        if unpaid <= 0:
            continue

        allocation = min(unpaid, remaining)
        installment.paid_amount = money(
            money(installment.paid_amount) + allocation
        )
        remaining = money(remaining - allocation)

        if money(installment.paid_amount) >= money(installment.amount):
            installment.status = "pago"
            installment.paid_at = timestamp
        else:
            installment.status = "parcial"


def _refresh_plan_status(plan: PaymentPlan) -> None:
    remaining = client_remaining(plan)
    if remaining <= 0:
        plan.status = "pago"
    elif money(plan.client_paid_total) > 0:
        plan.status = "parcial"
    else:
        plan.status = "pendente"


def allocate_regular_client_payment(
    db: Session,
    plan: PaymentPlan,
    amount: Decimal,
) -> dict:
    amount = money(amount)
    remaining = client_remaining(plan)
    if amount <= 0 or amount > remaining:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Valor de pagamento inválido.",
        )

    rows = _installments(db, plan.id)
    target = next(
        (row for row in rows if money(row.paid_amount) < money(row.amount)),
        None,
    )
    target_sequence = target.sequence if target is not None else 1

    company_capacity = company_remaining_entitlement(plan)
    remaining_commission = max(
        Decimal("0.00"),
        money(plan.commission_amount) - money(plan.commission_collected),
    )

    immediate_release = plan.mode == PAYMENT_MODE_SPLIT_50_50

    if immediate_release:
        selected_installment = int(
            get_financial_setting(
                db,
                "split_commission_installment",
                Decimal("1.00"),
            )
        )
        if selected_installment not in {1, 2}:
            selected_installment = 1

        should_collect_now = (
            target_sequence == selected_installment
            or (
                target_sequence > selected_installment
                and remaining_commission > 0
            )
        )

        commission_credit = (
            min(amount, remaining_commission)
            if should_collect_now
            else Decimal("0.00")
        )

        company_credit = min(
            money(amount - commission_credit),
            company_capacity,
        )

        gap = money(amount - company_credit - commission_credit)
        if gap > 0 and remaining_commission > commission_credit:
            commission_credit = min(
                remaining_commission,
                money(commission_credit + gap),
            )
            company_credit = min(
                money(amount - commission_credit),
                company_capacity,
            )

        plan.company_released_total = money(
            money(plan.company_released_total) + company_credit
        )
    else:
        company_credit = min(amount, company_capacity)
        commission_credit = money(amount - company_credit)
        plan.company_escrow_balance = money(
            money(plan.company_escrow_balance) + company_credit
        )

    plan.client_paid_total = money(
        money(plan.client_paid_total) + amount
    )
    plan.commission_collected = money(
        money(plan.commission_collected) + commission_credit
    )

    apply_payment_to_installments(db, plan, amount)
    _refresh_plan_status(plan)

    return {
        "company_credit": company_credit,
        "commission_credit": commission_credit,
        "immediate_release": immediate_release,
        "target_installment_sequence": target_sequence,
    }

def record_direct_fuel_client_payment(
    db: Session,
    plan: PaymentPlan,
    amount: Decimal,
) -> None:
    amount = money(amount)
    remaining = client_remaining(plan)

    if amount <= 0 or amount > remaining:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Valor do combustível excede o valor restante do transporte.",
        )

    total_after_release = money(money(plan.company_released_total) + amount)
    if total_after_release > money(plan.company_net_entitlement):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="O adiantamento ultrapassa o valor líquido da transportadora.",
        )

    plan.client_paid_total = money(money(plan.client_paid_total) + amount)
    plan.company_released_total = total_after_release
    apply_payment_to_installments(db, plan, amount)
    _refresh_plan_status(plan)


def release_company_escrow_amount(
    plan: PaymentPlan,
    amount: Decimal,
) -> None:
    amount = money(amount)
    escrow = money(plan.company_escrow_balance)

    if amount <= 0 or amount > escrow:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Saldo em retenção insuficiente para a libertação.",
        )

    plan.company_escrow_balance = money(escrow - amount)
    plan.company_released_total = money(
        money(plan.company_released_total) + amount
    )


def next_regular_payment(
    db: Session,
    plan: PaymentPlan,
    trip: Trip | None,
) -> dict:
    remaining = client_remaining(plan)
    if remaining <= 0:
        return {
            "available": False,
            "amount": Decimal("0.00"),
            "reason": "Pagamento completo.",
            "installment": None,
        }

    rows = _installments(db, plan.id)
    target = next(
        (row for row in rows if money(row.paid_amount) < money(row.amount)),
        None,
    )
    if target is None:
        return {
            "available": False,
            "amount": Decimal("0.00"),
            "reason": "Pagamento completo.",
            "installment": None,
        }

    due_amount = money(money(target.amount) - money(target.paid_amount))

    if (
        plan.mode == PAYMENT_MODE_SPLIT_50_50
        and target.sequence == 2
        and not (trip and trip.client_confirmed_at)
    ):
        return {
            "available": False,
            "amount": due_amount,
            "reason": (
                "A segunda parcela fica disponível depois "
                "da confirmação da entrega pelo cliente."
            ),
            "installment": target,
        }

    return {
        "available": True,
        "amount": min(due_amount, remaining),
        "reason": None,
        "installment": target,
    }


def start_payment_deadline_for_trip_event(
    db: Session,
    trip_id: int,
    event: str,
) -> PaymentPlan | None:
    trip = db.query(Trip).filter(Trip.id == trip_id).first()
    if trip is None:
        return None

    plan = get_plan_for_load(db, trip.load_id)
    if plan is None:
        try:
            plan = ensure_payment_plan_for_trip(db, trip)
        except HTTPException:
            return None

    if plan.deadline_started_at is not None:
        return plan

    should_start = (
        plan.mode == PAYMENT_MODE_FROM_LOADING and event == "loading"
    ) or (
        plan.mode == PAYMENT_MODE_AFTER_UNLOADING and event == "unloading"
    )
    if not should_start:
        return plan

    base_time = now_utc_naive()
    if event == "loading" and trip.loaded_at:
        base_time = trip.loaded_at.replace(tzinfo=None)
    if event == "unloading" and trip.arrived_at:
        base_time = trip.arrived_at.replace(tzinfo=None)

    days = int(plan.term_days or 7)
    plan.deadline_started_at = base_time
    plan.due_at = base_time + timedelta(days=days)

    for installment in _installments(db, plan.id):
        if installment.status != "pago":
            installment.due_at = plan.due_at

    db.commit()
    db.refresh(plan)
    return plan


def serialize_payment_plan(
    db: Session,
    plan: PaymentPlan,
    *,
    trip: Trip | None = None,
) -> dict:
    rows = _installments(db, plan.id)
    now = now_utc_naive()

    remaining_seconds = None
    remaining_days = None
    overdue = False

    if plan.due_at is not None:
        seconds = int((plan.due_at - now).total_seconds())
        remaining_seconds = max(0, seconds)
        remaining_days = max(0, math.ceil(remaining_seconds / 86400))
        overdue = seconds < 0 and client_remaining(plan) > 0

    next_info = next_regular_payment(db, plan, trip)

    return {
        "id": plan.id,
        "load_id": plan.load_id,
        "proposal_id": plan.proposal_id,
        "trip_id": plan.trip_id,
        "mode": plan.mode,
        "term_days": plan.term_days,
        "status": plan.status,
        "contract_amount": float(money(plan.contract_amount)),
        "client_paid_total": float(money(plan.client_paid_total)),
        "client_remaining": float(client_remaining(plan)),
        "company_net_entitlement": float(money(plan.company_net_entitlement)),
        "company_escrow_balance": float(money(plan.company_escrow_balance)),
        "company_released_total": float(money(plan.company_released_total)),
        "commission_percent": float(money(plan.commission_percent)),
        "commission_amount": float(money(plan.commission_amount)),
        "commission_collected": float(money(plan.commission_collected)),
        "split_commission_installment": int(
            get_financial_setting(
                db,
                "split_commission_installment",
                Decimal("1.00"),
            )
        ),
        "deadline_started_at": plan.deadline_started_at,
        "due_at": plan.due_at,
        "remaining_seconds_to_pay": remaining_seconds,
        "remaining_days_to_pay": remaining_days,
        "overdue": overdue,
        "next_payment": {
            "available": bool(next_info["available"]),
            "amount": float(money(next_info["amount"])),
            "reason": next_info["reason"],
            "installment_id": (
                next_info["installment"].id
                if next_info["installment"]
                else None
            ),
        },
        "installments": [
            {
                "id": item.id,
                "sequence": item.sequence,
                "kind": item.kind,
                "percent": float(money(item.percent)),
                "amount": float(money(item.amount)),
                "paid_amount": float(money(item.paid_amount)),
                "remaining_amount": float(
                    max(
                        Decimal("0.00"),
                        money(item.amount) - money(item.paid_amount),
                    )
                ),
                "status": item.status,
                "due_at": item.due_at,
                "paid_at": item.paid_at,
            }
            for item in rows
        ],
        "currency": "MT",
    }
