from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from controllers.trip_stops_controller import (
    complete_trip_stop,
    create_trip_stop,
    get_trip_financial_summary,
    list_stop_categories,
    list_trip_stops,
    start_trip_stop,
)
from database import get_db
from deps import get_current_user
from models.models import User


router = APIRouter()


class TripStopCreateRequest(BaseModel):
    category: str = Field(min_length=2, max_length=80)
    location_name: str = Field(min_length=2, max_length=180)
    description: str = Field(min_length=3, max_length=1000)


@router.get("/categories")
def categories():
    return list_stop_categories()


@router.get("/{trip_id}")
def stops(
    trip_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return list_trip_stops(db, current_user, trip_id)


@router.post("/{trip_id}")
def create_stop(
    trip_id: int,
    payload: TripStopCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return create_trip_stop(
        db,
        current_user,
        trip_id,
        category=payload.category,
        location_name=payload.location_name,
        description=payload.description,
    )


@router.patch("/{trip_id}/{stop_id}/start")
def start_stop(
    trip_id: int,
    stop_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return start_trip_stop(
        db,
        current_user,
        trip_id,
        stop_id,
    )


@router.patch("/{trip_id}/{stop_id}/complete")
def complete_stop(
    trip_id: int,
    stop_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return complete_trip_stop(
        db,
        current_user,
        trip_id,
        stop_id,
    )


@router.get("/{trip_id}/financial-summary")
def financial_summary(
    trip_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return get_trip_financial_summary(
        db,
        current_user,
        trip_id,
    )
