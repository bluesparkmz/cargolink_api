from __future__ import annotations

from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from controllers.financial_settings import (
    get_financial_settings_payload,
    update_financial_settings,
)
from database import get_db
from deps import get_current_user
from models.models import User

router = APIRouter()


class FinancialSettingsUpdate(BaseModel):
    delay_fee_per_24h: Decimal | None = Field(default=None, ge=0, le=1000000)
    fretix_commission_percent: Decimal | None = Field(default=None, ge=0, le=100)
    split_commission_installment: int | None = Field(
        default=None,
        ge=1,
        le=2,
        description="1 = comissão na primeira parcela; 2 = na segunda parcela",
    )


def _require_admin(user: User) -> None:
    if user.user_type != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Apenas a administração pode alterar estas definições.",
        )


@router.get("")
def get_settings(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin(current_user)
    return get_financial_settings_payload(db)


@router.patch("")
def update_settings(
    payload: FinancialSettingsUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin(current_user)
    if (
        payload.delay_fee_per_24h is None
        and payload.fretix_commission_percent is None
        and payload.split_commission_installment is None
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Informe pelo menos uma definição para actualizar.",
        )
    return update_financial_settings(
        db,
        delay_fee_per_24h=payload.delay_fee_per_24h,
        fretix_commission_percent=payload.fretix_commission_percent,
        split_commission_installment=payload.split_commission_installment,
    )
