"""Entrega assíncrona de notificações através do Expo Push Service."""

from concurrent.futures import ThreadPoolExecutor
import logging
import os

import httpx

from database import SessionLocal
from models.models import User, UserPushToken

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="fretix-push")
_EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def _is_expo_token(token: str) -> bool:
    return token.startswith("ExponentPushToken[") or token.startswith("ExpoPushToken[")


def _driver_may_receive(notification_type: str | None) -> bool:
    value = notification_type or ""
    return (
        value.startswith("trip.")
        or value.startswith("driver.")
        or value in {"rating.created", "message.created", "transport.cancelled"}
    )


def _send_push(
    *,
    user_id: int,
    notification_id: int,
    title: str,
    body: str,
    notification_type: str | None,
    payload: dict,
) -> None:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user is None or (
            user.user_type == "motorista" and not _driver_may_receive(notification_type)
        ):
            return

        devices = db.query(UserPushToken).filter(
            UserPushToken.user_id == user_id,
            UserPushToken.active.is_(True),
        ).all()
        devices = [device for device in devices if _is_expo_token(device.token)]
        if not devices:
            return

        data = dict(payload)
        data.update(
            {
                "notification_id": notification_id,
                "notification_type": notification_type,
            }
        )
        messages = [
            {
                "to": device.token,
                "title": title,
                "body": body,
                "data": data,
                "sound": "default",
                "priority": "high",
                "channelId": "fretix-default",
            }
            for device in devices
        ]
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        access_token = os.getenv("EXPO_ACCESS_TOKEN")
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"

        response = httpx.post(_EXPO_PUSH_URL, json=messages, headers=headers, timeout=12)
        response.raise_for_status()
        tickets = response.json().get("data", [])
        if isinstance(tickets, dict):
            tickets = [tickets]
        for device, ticket in zip(devices, tickets):
            if ticket.get("status") == "error" and ticket.get("details", {}).get("error") == "DeviceNotRegistered":
                device.active = False
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Falha ao enviar notificação push para user_id=%s", user_id)
    finally:
        db.close()


def queue_push_notification(**notification: object) -> None:
    """Agenda o envio sem atrasar nem fazer falhar a operação principal."""
    _executor.submit(_send_push, **notification)
