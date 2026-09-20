from __future__ import annotations

import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from config import settings
from controllers.notifications_controller import create_notification, emit_notification
from controllers.realtime_events import emit_to_rooms
from models.models import (
    Client,
    Company,
    Driver,
    Load,
    Trip,
    TripActivity,
    TripEvidence,
    TripEvidenceStage,
    User,
)

EVIDENCE_PICKUP = "pickup_photo"
EVIDENCE_DELIVERY = "delivery_photo"
EVIDENCE_POD = "proof_of_delivery"
EVIDENCE_TYPES = {EVIDENCE_PICKUP, EVIDENCE_DELIVERY, EVIDENCE_POD}

STAGE_PICKUP = "pickup"
STAGE_DELIVERY = "delivery"

IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
POD_TYPES = {**IMAGE_TYPES, "application/pdf": ".pdf"}

MIN_STAGE_PHOTOS = 3
MAX_STAGE_PHOTOS = 5
MAX_POD_FILES = 1


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _trip(db: Session, trip_id: int) -> Trip:
    trip = db.query(Trip).filter(Trip.id == trip_id).first()
    if trip is None:
        raise HTTPException(status_code=404, detail="Viagem não encontrada.")
    return trip


def _access_role(db: Session, user: User, trip: Trip) -> str:
    if user.user_type == "admin":
        return "admin"

    if user.user_type == "motorista":
        driver = db.query(Driver).filter(Driver.user_id == user.id).first()
        if driver and driver.id == trip.driver_id:
            return "motorista"

    if user.user_type == "empresa":
        company = db.query(Company).filter(Company.user_id == user.id).first()
        if company and company.id == trip.company_id:
            return "empresa"

    if user.user_type == "cliente":
        client = db.query(Client).filter(Client.user_id == user.id).first()
        load = db.query(Load).filter(Load.id == trip.load_id).first()
        if client and load and client.id == load.client_id:
            return "cliente"

    raise HTTPException(status_code=403, detail="Sem acesso às provas desta viagem.")


def _require_uploader(db: Session, user: User, trip: Trip) -> str:
    role = _access_role(db, user, trip)
    if role not in {"motorista", "admin"}:
        raise HTTPException(
            status_code=403,
            detail="Apenas o motorista atribuído pode enviar provas da viagem.",
        )
    return role


def _stage_for_type(evidence_type: str) -> str:
    return STAGE_PICKUP if evidence_type == EVIDENCE_PICKUP else STAGE_DELIVERY


def _stage_record(db: Session, trip_id: int, stage: str) -> TripEvidenceStage | None:
    return (
        db.query(TripEvidenceStage)
        .filter(
            TripEvidenceStage.trip_id == trip_id,
            TripEvidenceStage.stage == stage,
        )
        .first()
    )


def _assert_stage_open(db: Session, trip_id: int, stage: str) -> None:
    row = _stage_record(db, trip_id, stage)
    if row is not None and row.finalized_at is not None:
        raise HTTPException(status_code=409, detail="Esta fase de provas já foi finalizada.")


def _count(db: Session, trip_id: int, evidence_type: str) -> int:
    return (
        db.query(TripEvidence)
        .filter(
            TripEvidence.trip_id == trip_id,
            TripEvidence.evidence_type == evidence_type,
        )
        .count()
    )


def _validate_upload(file: UploadFile, evidence_type: str) -> str:
    if evidence_type not in EVIDENCE_TYPES:
        raise HTTPException(status_code=400, detail="Tipo de prova inválido.")

    allowed = POD_TYPES if evidence_type == EVIDENCE_POD else IMAGE_TYPES
    content_type = (file.content_type or "").lower()
    extension = allowed.get(content_type)
    if not extension:
        detail = (
            "Proof of Delivery deve ser JPG, PNG, WEBP ou PDF."
            if evidence_type == EVIDENCE_POD
            else "As fotografias devem ser JPG, PNG ou WEBP."
        )
        raise HTTPException(status_code=400, detail=detail)
    return extension


def serialize_evidence(row: TripEvidence) -> dict:
    return {
        "id": row.id,
        "trip_id": row.trip_id,
        "load_id": row.load_id,
        "evidence_type": row.evidence_type,
        "file_url": row.file_url,
        "mime_type": row.mime_type,
        "original_name": row.original_name,
        "uploaded_by_user_id": row.uploaded_by_user_id,
        "notes": row.notes,
        "created_at": row.created_at,
    }


def upload_trip_evidence(
    db: Session,
    user: User,
    trip_id: int,
    *,
    evidence_type: str,
    file: UploadFile,
    notes: str | None = None,
) -> dict:
    trip = _trip(db, trip_id)
    _require_uploader(db, user, trip)

    stage = _stage_for_type(evidence_type)
    _assert_stage_open(db, trip.id, stage)
    extension = _validate_upload(file, evidence_type)

    if evidence_type in {EVIDENCE_PICKUP, EVIDENCE_DELIVERY}:
        if _count(db, trip.id, evidence_type) >= MAX_STAGE_PHOTOS:
            raise HTTPException(
                status_code=400,
                detail=f"Máximo de {MAX_STAGE_PHOTOS} fotografias nesta fase.",
            )
    elif _count(db, trip.id, EVIDENCE_POD) >= MAX_POD_FILES:
        raise HTTPException(status_code=400, detail="Já existe um Proof of Delivery para esta viagem.")

    upload_dir = Path(settings.STORAGE_DIR) / "uploads" / "trip-evidence" / str(trip.id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{evidence_type}_{uuid.uuid4().hex}{extension}"
    target = upload_dir / filename

    file.file.seek(0)
    with target.open("wb") as destination:
        shutil.copyfileobj(file.file, destination)

    row = TripEvidence(
        trip_id=trip.id,
        load_id=trip.load_id,
        evidence_type=evidence_type,
        file_url=f"/uploads/trip-evidence/{trip.id}/{filename}",
        mime_type=file.content_type,
        original_name=file.filename,
        uploaded_by_user_id=user.id,
        notes=(notes or "").strip() or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    emit_to_rooms(
        {f"trip:{trip.id}", f"load:{trip.load_id}"},
        {
            "type": "trip.evidence_uploaded",
            "trip_id": trip.id,
            "load_id": trip.load_id,
            "evidence_id": row.id,
            "evidence_type": evidence_type,
        },
    )
    return serialize_evidence(row)


def list_trip_evidence(db: Session, user: User, trip_id: int) -> list[dict]:
    trip = _trip(db, trip_id)
    _access_role(db, user, trip)
    rows = (
        db.query(TripEvidence)
        .filter(TripEvidence.trip_id == trip.id)
        .order_by(TripEvidence.created_at.asc())
        .all()
    )
    return [serialize_evidence(row) for row in rows]


def evidence_summary(db: Session, user: User, trip_id: int) -> dict:
    trip = _trip(db, trip_id)
    _access_role(db, user, trip)

    pickup_count = _count(db, trip.id, EVIDENCE_PICKUP)
    delivery_count = _count(db, trip.id, EVIDENCE_DELIVERY)
    pod_count = _count(db, trip.id, EVIDENCE_POD)
    pickup_stage = _stage_record(db, trip.id, STAGE_PICKUP)
    delivery_stage = _stage_record(db, trip.id, STAGE_DELIVERY)

    return {
        "trip_id": trip.id,
        "load_id": trip.load_id,
        "pickup": {
            "photo_count": pickup_count,
            "min_photos": MIN_STAGE_PHOTOS,
            "max_photos": MAX_STAGE_PHOTOS,
            "ready_to_finalize": MIN_STAGE_PHOTOS <= pickup_count <= MAX_STAGE_PHOTOS,
            "finalized": bool(pickup_stage and pickup_stage.finalized_at),
            "finalized_at": pickup_stage.finalized_at if pickup_stage else None,
        },
        "delivery": {
            "photo_count": delivery_count,
            "proof_of_delivery_count": pod_count,
            "min_photos": MIN_STAGE_PHOTOS,
            "max_photos": MAX_STAGE_PHOTOS,
            "proof_of_delivery_required": True,
            "ready_to_finalize": (
                MIN_STAGE_PHOTOS <= delivery_count <= MAX_STAGE_PHOTOS and pod_count >= 1
            ),
            "finalized": bool(delivery_stage and delivery_stage.finalized_at),
            "finalized_at": delivery_stage.finalized_at if delivery_stage else None,
        },
    }


def _recipient_user_ids(db: Session, trip: Trip) -> list[int]:
    recipients: list[int] = []
    if trip.company_id:
        company = db.query(Company).filter(Company.id == trip.company_id).first()
        if company:
            recipients.append(company.user_id)

    load = db.query(Load).filter(Load.id == trip.load_id).first()
    if load:
        client = db.query(Client).filter(Client.id == load.client_id).first()
        if client:
            recipients.append(client.user_id)
    return list(dict.fromkeys(recipients))


def finalize_evidence_stage(db: Session, user: User, trip_id: int, stage: str) -> dict:
    trip = _trip(db, trip_id)
    _require_uploader(db, user, trip)

    if stage not in {STAGE_PICKUP, STAGE_DELIVERY}:
        raise HTTPException(status_code=400, detail="Fase inválida. Use pickup ou delivery.")

    existing = _stage_record(db, trip.id, stage)
    if existing is not None and existing.finalized_at is not None:
        return evidence_summary(db, user, trip.id)

    if stage == STAGE_PICKUP:
        count = _count(db, trip.id, EVIDENCE_PICKUP)
        if count < MIN_STAGE_PHOTOS:
            raise HTTPException(
                status_code=400,
                detail=f"Envie pelo menos {MIN_STAGE_PHOTOS} fotografias da carga antes da saída.",
            )
    else:
        count = _count(db, trip.id, EVIDENCE_DELIVERY)
        pod_count = _count(db, trip.id, EVIDENCE_POD)
        if count < MIN_STAGE_PHOTOS:
            raise HTTPException(
                status_code=400,
                detail=f"Envie pelo menos {MIN_STAGE_PHOTOS} fotografias da descarga.",
            )
        if pod_count < 1:
            raise HTTPException(status_code=400, detail="Envie o Proof of Delivery antes de concluir.")

    if existing is None:
        existing = TripEvidenceStage(
            trip_id=trip.id,
            stage=stage,
            finalized_by_user_id=user.id,
            finalized_at=_now(),
        )
        db.add(existing)
    else:
        existing.finalized_by_user_id = user.id
        existing.finalized_at = _now()

    title = "Fotos da carga recebidas" if stage == STAGE_PICKUP else "Comprovação da entrega recebida"
    body = (
        "O motorista enviou as fotografias da carga antes da saída."
        if stage == STAGE_PICKUP
        else "O motorista enviou as fotografias da descarga e o Proof of Delivery."
    )
    event_type = (
        "trip.pickup_evidence_finalized"
        if stage == STAGE_PICKUP
        else "trip.delivery_evidence_finalized"
    )

    notifications = [
        create_notification(
            db,
            user_id=user_id,
            title=title,
            body=body,
            notification_type=event_type,
            payload={"trip_id": trip.id, "load_id": trip.load_id, "stage": stage},
        )
        for user_id in _recipient_user_ids(db, trip)
    ]

    # A finalização, e não cada fotografia individual, representa o marco
    # operacional que cliente e empresa devem ver no histórico da viagem.
    activity = TripActivity(
        trip_id=trip.id,
        event_type=event_type,
        title=(
            "Prova de Recolha Enviada"
            if stage == STAGE_PICKUP
            else "Prova de Entrega e POD Enviados"
        ),
        description=body,
    )
    db.add(activity)

    # Fase, notificações e atividade são persistidas de forma atómica.
    db.commit()
    db.refresh(activity)
    for notification in notifications:
        db.refresh(notification)
        emit_notification(notification)

    emit_to_rooms(
        {f"trip:{trip.id}", f"load:{trip.load_id}"},
        {
            "type": event_type,
            "trip_id": trip.id,
            "load_id": trip.load_id,
            "stage": stage,
            "activity": {
                "id": activity.id,
                "event_type": activity.event_type,
                "title": activity.title,
                "description": activity.description,
                "created_at": activity.created_at,
            },
        },
    )
    return evidence_summary(db, user, trip.id)


def delete_trip_evidence(db: Session, user: User, trip_id: int, evidence_id: int) -> None:
    trip = _trip(db, trip_id)
    _require_uploader(db, user, trip)
    row = (
        db.query(TripEvidence)
        .filter(TripEvidence.id == evidence_id, TripEvidence.trip_id == trip.id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Prova não encontrada.")

    _assert_stage_open(db, trip.id, _stage_for_type(row.evidence_type))
    target = Path(settings.STORAGE_DIR) / row.file_url.lstrip("/")
    if target.exists():
        try:
            target.unlink()
        except OSError:
            pass
    db.delete(row)
    db.commit()
