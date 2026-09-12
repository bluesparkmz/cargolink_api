"""
Rotas de carteira: saldo, extrato, depósitos, pagamentos,
requisições de combustível e histórico financeiro.
"""

from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from controllers.wallet_controller import (
    confirm_deposit,
    create_deposit,
    create_deposit_with_polling,
    get_deposit_status,
    list_wallet_transactions,
    process_mpesa_callback,
    sync_deposit_with_mpesa,
)
from controllers.wallet_transport_controller import (
    get_load_financial_history,
    get_transport_payment_status,
    pay_accepted_proposal_from_wallet,
)
from controllers.wallet_financial_controller import (
    get_wallet_summary,
    get_wallet_transaction_details,
)
from controllers.fuel_advance_controller import (
    create_fuel_advance_request,
    get_fuel_advance_eligibility,
    list_client_fuel_requests,
    list_fuel_advances,
    pay_client_fuel_request,
    reject_client_fuel_request,
)
from database import get_db
from deps import get_current_user
from models.models import User
from schemas.schemas import (
    WalletBalanceResponse,
    WalletDepositRequest,
    WalletDepositResponse,
    WalletDepositStatusResponse,
    WalletTransactionResponse,
)

router = APIRouter()


class FuelAdvanceRequest(BaseModel):
    vehicle_id: int = Field(..., gt=0)
    amount: float = Field(..., gt=0)
    reason: str | None = Field(None, max_length=500)
    # Mantido opcional por compatibilidade com versões antigas do app.
    # O dinheiro é libertado para a carteira Fretix da empresa; não há
    # desembolso M-Pesa directo neste passo.
    mpesa_phone: str | None = Field(None, max_length=30)


@router.get("/transactions", response_model=list[WalletTransactionResponse])
def get_transactions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = list_wallet_transactions(
        db,
        current_user,
        limit=limit,
        offset=offset,
    )
    return [
        WalletTransactionResponse(
            id=row.id,
            transaction_type=row.transaction_type,
            amount=float(row.amount),
            status=row.status,
            reference=row.reference,
            description=row.description,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.post(
    "/deposits",
    response_model=WalletDepositResponse,
    status_code=201,
)
async def request_deposit(
    data: WalletDepositRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await create_deposit(db, current_user, data)


@router.post(
    "/deposits-with-polling",
    response_model=WalletDepositResponse,
    status_code=201,
)
async def request_deposit_with_polling(
    data: WalletDepositRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await create_deposit_with_polling(
        db,
        current_user,
        data,
        max_wait_seconds=30,
    )


@router.post(
    "/deposits/{payment_id}/sync",
    response_model=WalletDepositStatusResponse,
)
def sync_deposit_route(
    payment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return sync_deposit_with_mpesa(db, current_user, payment_id)


@router.get(
    "/deposits/{payment_id}/status",
    response_model=WalletDepositStatusResponse,
)
def deposit_status_route(
    payment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return get_deposit_status(db, current_user, payment_id)


@router.post(
    "/deposits/{payment_id}/confirm",
    response_model=WalletDepositResponse,
)
def confirm_deposit_route(
    payment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return confirm_deposit(db, current_user, payment_id)


@router.get("/proposal-payments/{proposal_id}")
def proposal_payment_status_route(
    proposal_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return get_transport_payment_status(
        db,
        current_user,
        proposal_id,
    )


@router.post("/proposal-payments/{proposal_id}")
def pay_proposal_from_wallet_route(
    proposal_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return pay_accepted_proposal_from_wallet(
        db,
        current_user,
        proposal_id,
    )


@router.get("/load-finance/{load_id}")
def load_financial_history_route(
    load_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return get_load_financial_history(
        db,
        current_user,
        load_id,
    )


@router.get("/transactions/{transaction_id}")
def transaction_details_route(
    transaction_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return get_wallet_transaction_details(
        db,
        current_user,
        transaction_id,
    )


# Cliente: painel Requisições.
@router.get("/fuel-requests/client")
def client_fuel_requests_route(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return list_client_fuel_requests(
        db,
        current_user,
    )


@router.post("/fuel-requests/{advance_id}/pay")
def client_pay_fuel_request_route(
    advance_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return pay_client_fuel_request(
        db,
        current_user,
        advance_id,
    )


@router.post("/fuel-requests/{advance_id}/reject")
def client_reject_fuel_request_route(
    advance_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return reject_client_fuel_request(
        db,
        current_user,
        advance_id,
    )


# Empresa: pedido de adiantamento de combustível.
@router.get("/fuel-advances/{trip_id}/eligibility")
def fuel_advance_eligibility_route(
    trip_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return get_fuel_advance_eligibility(
        db,
        current_user,
        trip_id,
    )


@router.get("/fuel-advances/{trip_id}")
def fuel_advance_list_route(
    trip_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return list_fuel_advances(
        db,
        current_user,
        trip_id,
    )


@router.post("/fuel-advances/{trip_id}", status_code=201)
def fuel_advance_create_route(
    trip_id: int,
    data: FuelAdvanceRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return create_fuel_advance_request(
        db,
        current_user,
        trip_id,
        vehicle_id=data.vehicle_id,
        amount=Decimal(str(data.amount)),
        reason=data.reason,
        mpesa_phone=data.mpesa_phone,
    )


@router.get("", response_model=WalletBalanceResponse)
def get_balance(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    summary = get_wallet_summary(db, current_user)
    return WalletBalanceResponse(**summary)


@router.post("/mpesa-callback")
def mpesa_callback(payload: dict):
    return process_mpesa_callback(payload)
