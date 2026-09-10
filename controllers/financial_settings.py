from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from models.models import FinancialSetting


DEFAULT_DELAY_FEE_PER_24H = Decimal("3000.00")
DEFAULT_FRETIX_COMMISSION_PERCENT = Decimal("30.00")


def get_financial_setting(
    db: Session,
    key: str,
    default: Decimal,
) -> Decimal:
    row = (
        db.query(FinancialSetting)
        .filter(FinancialSetting.key == key)
        .first()
    )
    if row is None:
        return default
    return Decimal(str(row.value))


def get_delay_fee_per_24h(db: Session) -> Decimal:
    return get_financial_setting(
        db,
        "delay_fee_per_24h",
        DEFAULT_DELAY_FEE_PER_24H,
    )


def get_fretix_commission_percent(db: Session) -> Decimal:
    return get_financial_setting(
        db,
        "fretix_commission_percent",
        DEFAULT_FRETIX_COMMISSION_PERCENT,
    )


def get_financial_settings_payload(db: Session) -> dict:
    return {
        "delay_fee_per_24h": float(get_delay_fee_per_24h(db)),
        "fretix_commission_percent": float(
            get_fretix_commission_percent(db)
        ),
        "delay_grace_hours": 24,
        "delay_billing_unit_hours": 1,
    }


def update_financial_settings(
    db: Session,
    *,
    delay_fee_per_24h: Decimal | None = None,
    fretix_commission_percent: Decimal | None = None,
) -> dict:
    updates = {
        "delay_fee_per_24h": delay_fee_per_24h,
        "fretix_commission_percent": fretix_commission_percent,
    }

    for key, value in updates.items():
        if value is None:
            continue

        row = (
            db.query(FinancialSetting)
            .filter(FinancialSetting.key == key)
            .first()
        )
        if row is None:
            row = FinancialSetting(key=key, value=value)
            db.add(row)
        else:
            row.value = value

    db.commit()
    return get_financial_settings_payload(db)
