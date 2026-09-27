"""
Controller de empresas transportadoras: perfil, motoristas e propostas.
"""

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from models.models import Company, Driver, LoadProposal, Trip, User
from schemas.schemas import CompanyProfileUpdateRequest
from security import generate_random_password, hash_password
from controllers.realtime_events import emit_to_user


def require_company(user: User) -> None:
    """Garante que o utilizador autenticado e uma empresa transportadora."""
    if user.user_type != "empresa":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acesso permitido apenas para empresas transportadoras",
        )


def get_my_company(db: Session, user: User) -> Company:
    """Retorna perfil da empresa autenticada."""
    require_company(user)
    company = (
        db.query(Company)
        .options(joinedload(Company.user))
        .filter(Company.user_id == user.id)
        .first()
    )
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Perfil de empresa nao encontrado",
        )
    return company


def get_company_by_id(db: Session, company_id: int) -> Company:
    """Busca empresa por id com dados do utilizador."""
    company = (
        db.query(Company)
        .options(joinedload(Company.user))
        .filter(Company.id == company_id)
        .first()
    )
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Empresa nao encontrada",
        )
    return company


def list_companies(db: Session) -> list[Company]:
    """Lista empresas transportadoras registadas."""
    return db.query(Company).options(joinedload(Company.user)).all()


def update_my_company(db: Session, user: User, data: CompanyProfileUpdateRequest) -> Company:
    """Atualiza perfil da empresa autenticada."""
    company = get_my_company(db, user)
    fields = data.model_dump(exclude_unset=True)
    for field, value in fields.items():
        setattr(company, field, value)
    db.commit()
    db.refresh(company)
    return get_company_by_id(db, company.id)


def list_company_drivers(db: Session, user: User) -> list[Driver]:
    """Lista motoristas associados a empresa autenticada."""
    company = get_my_company(db, user)
    return (
        db.query(Driver)
        .options(joinedload(Driver.user))
        .filter(Driver.company_id == company.id)
        .order_by(Driver.created_at.desc())
        .all()
    )


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _get_driver_by_user_email(db: Session, email: str) -> Driver | None:
    """Busca motorista pelo email de login (conta tipo motorista)."""
    normalized = _normalize_email(email)
    return (
        db.query(Driver)
        .options(joinedload(Driver.user))
        .join(User, Driver.user_id == User.id)
        .filter(
            func.lower(User.email) == normalized,
            User.user_type == "motorista",
        )
        .first()
    )


def attach_driver_to_company(db: Session, user: User, email: str) -> Driver:
    """Associa motorista existente a empresa autenticada pelo email."""
    company = get_my_company(db, user)
    driver = _get_driver_by_user_email(db, email)
    if driver is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Motorista nao encontrado com este email",
        )
    if driver.company_id and driver.company_id != company.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Motorista ja pertence a outra empresa",
        )

    driver.company_id = company.id
    db.commit()
    db.refresh(driver)
    emit_to_user(user.id, {'type':'driver.attached','driver_id':driver.id,'company_id':company.id})
    return driver


def get_company_driver_by_email(db: Session, company: Company, email: str) -> Driver:
    """Motorista que ja pertence a empresa, identificado pelo email."""
    driver = _get_driver_by_user_email(db, email)
    if driver is None or driver.company_id != company.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Motorista nao encontrado nesta empresa com este email",
        )
    return driver


def detach_driver_from_company(db: Session, user: User, driver_id: int) -> None:
    """Remove motorista da empresa autenticada."""
    company = get_my_company(db, user)
    driver = db.query(Driver).filter(Driver.id == driver_id).first()
    if driver is None or driver.company_id != company.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Motorista nao encontrado nesta empresa",
        )
    _detach_driver_from_company(db, company, driver)
    emit_to_user(user.id, {'type':'driver.detached','driver_id':driver.id,'company_id':company.id})


def detach_driver_from_company_by_email(db: Session, user: User, email: str) -> None:
    """Remove motorista da empresa pelo email de login."""
    company = get_my_company(db, user)
    driver = get_company_driver_by_email(db, company, email)
    _detach_driver_from_company(db, company, driver)
    emit_to_user(user.id, {'type':'driver.detached','driver_id':driver.id,'company_id':company.id})


def _detach_driver_from_company(db: Session, company: Company, driver: Driver) -> None:
    driver.company_id = None
    for vehicle in driver.vehicles:
        if vehicle.company_id == company.id:
            vehicle.driver_id = None
    db.commit()


def list_company_proposals(db: Session, user: User) -> list[LoadProposal]:
    """Lista propostas enviadas pela empresa autenticada."""
    company = get_my_company(db, user)
    return (
        db.query(LoadProposal)
        .filter(LoadProposal.company_id == company.id)
        .order_by(LoadProposal.created_at.desc())
        .all()
    )


def list_company_trips(db: Session, user: User) -> list[Trip]:
    """Lista viagens da empresa autenticada."""
    company = get_my_company(db, user)
    return (
        db.query(Trip)
        .options(joinedload(Trip.load), joinedload(Trip.vehicle))
        .filter(Trip.company_id == company.id)
        .order_by(Trip.created_at.desc())
        .all()
    )


def create_driver_for_company(
    db: Session,
    user: User,
    name: str,
    email: str,
    phone: str,
    license_number: str | None = None,
    license_expiry = None,
    years_experience: int = 0,
) -> tuple[Driver, str]:
    """
    Cria novo motorista e o associa à empresa autenticada.
    Retorna tupla: (driver, temporary_password)
    """
    company = get_my_company(db, user)
    
    # Verifica se email já existe
    existing_user = db.query(User).filter(func.lower(User.email) == func.lower(email)).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email já registado no sistema",
        )
    
    # Verifica se telefone já existe
    existing_phone = db.query(User).filter(func.lower(User.phone) == func.lower(phone)).first()
    if existing_phone:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Telefone já registado no sistema",
        )
    
    # Gera senha temporária
    temporary_password = generate_random_password(12)
    password_hash = hash_password(temporary_password)
    
    # Cria novo utilizador tipo motorista
    new_user = User(
        name=name,
        email=email.lower(),
        phone=phone,
        password_hash=password_hash,
        user_type="motorista",
        status="ativo",
        verified=False,
        must_change_password=True,
    )
    db.add(new_user)
    db.flush()  # Força a geração do ID
    
    # Cria perfil de motorista
    new_driver = Driver(
        user_id=new_user.id,
        company_id=company.id,
        license_number=license_number,
        license_expiry=license_expiry,
        years_experience=years_experience,
    )
    db.add(new_driver)
    db.commit()
    db.refresh(new_driver)
    
    created_driver = (
        db.query(Driver)
        .options(joinedload(Driver.user))
        .filter(Driver.id == new_driver.id)
        .first()
    )
    emit_to_user(user.id, {'type':'driver.created','driver_id':created_driver.id,'company_id':company.id})
    return created_driver, temporary_password
