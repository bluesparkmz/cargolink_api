"""
Notificações in-app com escopo por perfil.

Regra Driver:
- mostra somente assuntos operacionais do motorista/viagem;
- nunca mostra propostas, negociações, pagamentos, carteira,
  comissão ou requisições financeiras.
"""

from fastapi import HTTPException, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Query, Session

from controllers.realtime_events import emit_to_user
from models.models import Notification, User


DRIVER_EXACT_NOTIFICATION_TYPES = {
    "rating.created",
    "message.created",
    "transport.cancelled",
}


def _notification_query(db: Session, user: User) -> Query:
    query = db.query(Notification).filter(Notification.user_id == user.id)

    if user.user_type == "motorista":
        query = query.filter(
            or_(
                Notification.notification_type.like("trip.%"),
                Notification.notification_type.like("driver.%"),
                Notification.notification_type.in_(
                    DRIVER_EXACT_NOTIFICATION_TYPES
                ),
            )
        )

    return query


def create_notification(
    db: Session,
    *,
    user_id: int,
    title: str,
    body: str,
    notification_type: str | None = None,
    payload: dict | None = None,
) -> Notification:
    notification = Notification(
        user_id=user_id,
        title=title,
        body=body,
        notification_type=notification_type,
        payload=payload,
    )
    db.add(notification)
    db.flush()
    return notification


def emit_notification(notification: Notification) -> None:
    emit_to_user(
        notification.user_id,
        {
            "type": "notification.created",
            "notification": notification,
        },
    )


def list_notifications(
    db: Session,
    user: User,
    *,
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[Notification]:
    query = _notification_query(db, user)
    if unread_only:
        query = query.filter(Notification.read.is_(False))
    return (
        query.order_by(Notification.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def count_unread(db: Session, user: User) -> int:
    query = _notification_query(db, user).filter(
        Notification.read.is_(False)
    )
    return query.with_entities(func.count(Notification.id)).scalar() or 0


def mark_notification_read(
    db: Session,
    user: User,
    notification_id: int,
) -> Notification:
    notification = (
        _notification_query(db, user)
        .filter(Notification.id == notification_id)
        .first()
    )
    if notification is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notificação não encontrada",
        )
    notification.read = True
    db.commit()
    db.refresh(notification)
    return notification


def mark_all_read(db: Session, user: User) -> int:
    query = _notification_query(db, user).filter(
        Notification.read.is_(False)
    )
    updated = query.update(
        {Notification.read: True},
        synchronize_session=False,
    )
    db.commit()
    return updated
