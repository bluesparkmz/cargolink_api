"""
Rotas de empresas transportadoras.
"""

from fastapi import APIRouter, Depends, Query
from pydantic import EmailStr
from sqlalchemy.orm import Session

from controllers.activities_controller import list_company_activities

from controllers.companies_controller import (
    attach_driver_to_company,
    create_driver_for_company,
    detach_driver_from_company,
    detach_driver_from_company_by_email,
    get_company_by_id,
    get_my_company,
    list_companies,
    list_company_drivers,
    list_company_proposals,
    list_company_trips,
    update_my_company,
)
from database import get_db
from deps import get_current_user
from models.models import Company, Driver, LoadProposal, User
from schemas.schemas import (
    ActivityItem,
    CompanyDetailResponse,
    CompanyDriverAttachRequest,
    CompanyDriverCreateRequest,
    CompanyDriverCreateResponse,
    CompanyListItem,
    CompanyProfileUpdateRequest,
    DriverListItem,
    LoadProposalResponse,
    TripResponse,
)

router = APIRouter()


def _to_list_item(company: Company) -> CompanyListItem:
    return CompanyListItem(
        id=company.id,
        user_id=company.user_id,
        company_name=company.company_name,
        city=company.city,
        state=company.state,
        average_rating=float(company.average_rating),
        total_trips=company.total_trips,
        verified=company.verified,
    )


def _driver_to_list_item(driver: Driver) -> DriverListItem:
    return DriverListItem(
        id=driver.id,
        user_id=driver.user_id,
        company_id=driver.company_id,
        name=driver.user.name,
        email=driver.user.email,
        average_rating=float(driver.average_rating),
        total_trips=driver.total_trips,
        available=driver.available,
        profile_photo=driver.user.profile_photo,
        verified=driver.user.verified,
        current_lat=float(driver.current_lat) if driver.current_lat is not None else None,
        current_lng=float(driver.current_lng) if driver.current_lng is not None else None,
        location_updated_at=driver.location_updated_at,
    )


def _proposal_to_response(proposal: LoadProposal) -> LoadProposalResponse:
    return LoadProposalResponse(
        id=proposal.id,
        load_id=proposal.load_id,
        company_id=proposal.company_id,
        driver_id=proposal.driver_id,
        vehicle_id=proposal.vehicle_id,
        proposed_value=float(proposal.proposed_value) if proposal.proposed_value else None,
        message=proposal.message,
        status=proposal.status,
        created_at=proposal.created_at,
    )


def _trip_to_response(trip) -> TripResponse:
    return TripResponse.model_validate(trip)


@router.get("/me/activities", response_model=list[ActivityItem])
def get_my_activities(
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return list_company_activities(db, current_user, limit=limit)


@router.get("/me", response_model=CompanyDetailResponse)
def get_me(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Perfil da empresa autenticada."""
    return get_my_company(db, current_user)


@router.patch("/me", response_model=CompanyDetailResponse)
def update_me(
    data: CompanyProfileUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Atualiza perfil da empresa autenticada."""
    return update_my_company(db, current_user, data)


@router.get("/me/drivers", response_model=list[DriverListItem])
def get_my_drivers(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Lista motoristas da empresa autenticada."""
    return [_driver_to_list_item(driver) for driver in list_company_drivers(db, current_user)]


@router.post("/me/drivers", response_model=DriverListItem)
def attach_driver(
    data: CompanyDriverAttachRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Associa motorista existente a empresa pelo email de login."""
    driver = attach_driver_to_company(db, current_user, str(data.email))
    return _driver_to_list_item(driver)


@router.post("/me/drivers/create", response_model=CompanyDriverCreateResponse)
def create_driver(
    data: CompanyDriverCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cria novo motorista e o associa à empresa autenticada."""
    driver, temporary_password = create_driver_for_company(
        db,
        current_user,
        name=data.name,
        email=data.email,
        phone=data.phone,
        license_number=data.license_number,
        license_expiry=data.license_expiry,
        years_experience=data.years_experience,
    )
    return CompanyDriverCreateResponse(
        id=driver.id,
        user_id=driver.user_id,
        name=driver.user.name,
        email=driver.user.email,
        phone=driver.user.phone,
        company_id=driver.company_id,
        temporary_password=temporary_password,
        average_rating=float(driver.average_rating),
        total_trips=driver.total_trips,
        available=driver.available,
    )


@router.delete("/me/drivers", status_code=204)
def detach_driver_by_email(
    email: EmailStr = Query(..., description="Email de login do motorista"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove motorista da empresa pelo email."""
    detach_driver_from_company_by_email(db, current_user, str(email))


@router.delete("/me/drivers/{driver_id}", status_code=204)
def detach_driver(
    driver_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove motorista da empresa pelo id interno."""
    detach_driver_from_company(db, current_user, driver_id)


@router.get("/me/proposals", response_model=list[LoadProposalResponse])
def get_my_proposals(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Lista propostas enviadas pela empresa autenticada."""
    return [_proposal_to_response(p) for p in list_company_proposals(db, current_user)]


@router.get("/me/trips", response_model=list[TripResponse])
def get_my_trips(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Lista viagens da empresa autenticada."""
    return [_trip_to_response(trip) for trip in list_company_trips(db, current_user)]


@router.get("", response_model=list[CompanyListItem])
def list_all(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Lista empresas transportadoras registadas."""
    return [_to_list_item(company) for company in list_companies(db)]


@router.get("/{company_id}", response_model=CompanyDetailResponse)
def get_by_id(
    company_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Consulta empresa transportadora por id."""
    return get_company_by_id(db, company_id)
