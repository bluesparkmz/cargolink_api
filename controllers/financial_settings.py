from __future__ import annotations

from decimal import Decimal
from sqlalchemy.orm import Session
from models.models import FinancialSetting

DEFAULT_DELAY_FEE_PER_24H = Decimal("3000.00")
DEFAULT_FRETIX_COMMISSION_PERCENT = Decimal("30.00")
DEFAULT_SPLIT_COMMISSION_INSTALLMENT = Decimal("1.00")


def get_financial_setting(db: Session, key: str, default: Decimal) -> Decimal:
    row = db.query(FinancialSetting).filter(FinancialSetting.key == key).first()
    if row is None:
        return default
    return Decimal(str(row.value))


def get_delay_fee_per_24h(db: Session) -> Decimal:
    return get_financial_setting(db, "delay_fee_per_24h", DEFAULT_DELAY_FEE_PER_24H)


def get_fretix_commission_percent(db: Session) -> Decimal:
    return get_financial_setting(
        db,
        "fretix_commission_percent",
        DEFAULT_FRETIX_COMMISSION_PERCENT,
    )


def get_split_commission_installment(db: Session) -> int:
    value = int(
        get_financial_setting(
            db,
            "split_commission_installment",
            DEFAULT_SPLIT_COMMISSION_INSTALLMENT,
        )
    )
    return 2 if value == 2 else 1


def get_financial_settings_payload(db: Session) -> dict:
    installment = get_split_commission_installment(db)
    return {
        "delay_fee_per_24h": float(get_delay_fee_per_24h(db)),
        "fretix_commission_percent": float(get_fretix_commission_percent(db)),
        "split_commission_installment": installment,
        "split_commission_installment_label": (
            "segunda_parcela" if installment == 2 else "primeira_parcela"
        ),
        "delay_grace_hours": 24,
        "delay_billing_unit_hours": 1,
    }


def update_financial_settings(
    db: Session,
    *,
    delay_fee_per_24h: Decimal | None = None,
    fretix_commission_percent: Decimal | None = None,
    split_commission_installment: int | None = None,
) -> dict:
    updates: dict[str, Decimal | None] = {
        "delay_fee_per_24h": delay_fee_per_24h,
        "fretix_commission_percent": fretix_commission_percent,
        "split_commission_installment": (
            Decimal(str(split_commission_installment))
            if split_commission_installment is not None
            else None
        ),
    }
    for key, value in updates.items():
        if value is None:
            continue
        row = db.query(FinancialSetting).filter(FinancialSetting.key == key).first()
        if row is None:
            db.add(FinancialSetting(key=key, value=value))
        else:
            row.value = value
    db.commit()
    return get_financial_settings_payload(db)
