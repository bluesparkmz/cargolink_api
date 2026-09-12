from fastapi import APIRouter, Depends, File, Form, Response, UploadFile, status
from sqlalchemy.orm import Session

from controllers.trip_evidence_controller import (
    delete_trip_evidence,
    evidence_summary,
    finalize_evidence_stage,
    list_trip_evidence,
    upload_trip_evidence,
)
from database import get_db
from deps import get_current_user
from models.models import User

router = APIRouter()


@router.get("/{trip_id}")
def list_evidence(
    trip_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return list_trip_evidence(db, current_user, trip_id)


@router.get("/{trip_id}/summary")
def summary(
    trip_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return evidence_summary(db, current_user, trip_id)


@router.post("/{trip_id}/upload", status_code=201)
def upload(
    trip_id: int,
    evidence_type: str = Form(...),
    file: UploadFile = File(...),
    notes: str | None = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return upload_trip_evidence(
        db,
        current_user,
        trip_id,
        evidence_type=evidence_type,
        file=file,
        notes=notes,
    )


@router.post("/{trip_id}/finalize/{stage}")
def finalize(
    trip_id: int,
    stage: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return finalize_evidence_stage(db, current_user, trip_id, stage)


@router.delete("/{trip_id}/{evidence_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(
    trip_id: int,
    evidence_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    delete_trip_evidence(db, current_user, trip_id, evidence_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
