"""
Controller de autenticação: registo, login e perfil.
Sem lógica de carteira ou pagamentos.
"""

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta

from fastapi import HTTPException, status
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import settings
from constants import (
    USER_STATUS_ACTIVE,
    USER_STATUS_PENDING,
    USER_TYPE_CLIENT,
    USER_TYPE_COMPANY,
    USER_TYPE_USUARIO,
)
from security import create_access_token, hash_password, verify_password
from models.models import AuthVerificationCode, Client, Company, User, Wallet
from schemas.schemas import CompleteOnboardingRequest, PasswordChangeRequest, RegisterRequest


def _raise_duplicate_registration(exc: IntegrityError) -> None:
    message = str(getattr(exc, "orig", exc)).lower()
    if "users_telefone_key" in message or "telefone" in message:
        detail = "Telefone jÃ¡ registado"
    elif "users_email_key" in message or "email" in message:
        detail = "Email jÃ¡ registado"
    else:
        detail = "Dados jÃ¡ registados"
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail) from exc


def register_user(db: Session, data: RegisterRequest) -> User:
    """
    Cria utilizador genérico em estado pendente.
    O tipo (cliente ou empresa) é definido no onboarding.
    """
    phone = (data.phone or "").strip()
    email = str(data.email).strip().lower()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email já registado",
        )

    if db.query(User).filter(User.phone == phone).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Telefone jÃ¡ registado",
        )

    user = User(
        name=data.name,
        email=email,
        phone=phone,
        password_hash=hash_password(data.password),
        user_type=USER_TYPE_USUARIO,
        status=USER_STATUS_PENDING,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        _raise_duplicate_registration(exc)
    db.add(Wallet(user_id=user.id))

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _raise_duplicate_registration(exc)
    db.refresh(user)
    if user.user_type != USER_TYPE_USUARIO or user.status != USER_STATUS_PENDING:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Falha ao criar conta pendente",
        )
    return user


def _code_hash(user_id: int, purpose: str, code: str) -> str:
    payload = f"{user_id}:{purpose}:{code}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), payload, hashlib.sha256).hexdigest()


def create_verification_code(db: Session, user: User, purpose: str) -> str:
    """Invalida códigos anteriores e cria um OTP de seis dígitos."""
    now = datetime.utcnow()
    db.query(AuthVerificationCode).filter(
        AuthVerificationCode.user_id == user.id,
        AuthVerificationCode.purpose == purpose,
        AuthVerificationCode.consumed_at.is_(None),
    ).update({"consumed_at": now}, synchronize_session=False)
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.add(
        AuthVerificationCode(
            user_id=user.id,
            purpose=purpose,
            code_hash=_code_hash(user.id, purpose, code),
            expires_at=now + timedelta(minutes=int(os.getenv("OTP_EXPIRE_MINUTES", "10"))),
        )
    )
    db.commit()
    return code


def user_for_email(db: Session, email: str, *, required: bool = True) -> User | None:
    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if required and user is None:
        raise HTTPException(status_code=404, detail="Conta não encontrada")
    return user


def consume_verification_code(
    db: Session, user: User, purpose: str, code: str
) -> None:
    record = (
        db.query(AuthVerificationCode)
        .filter(
            AuthVerificationCode.user_id == user.id,
            AuthVerificationCode.purpose == purpose,
            AuthVerificationCode.consumed_at.is_(None),
        )
        .order_by(AuthVerificationCode.created_at.desc(), AuthVerificationCode.id.desc())
        .first()
    )
    now = datetime.utcnow()
    if record is None or record.expires_at < now or record.attempts >= 5:
        raise HTTPException(status_code=400, detail="Código inválido ou expirado")

    record.attempts += 1
    valid = hmac.compare_digest(
        record.code_hash, _code_hash(user.id, purpose, code)
    )
    if not valid:
        db.commit()
        raise HTTPException(status_code=400, detail="Código inválido ou expirado")

    record.consumed_at = now
    db.commit()


def verify_user_email(db: Session, email: str, code: str) -> User:
    user = user_for_email(db, email)
    if user.verified:
        return user
    consume_verification_code(db, user, "email_verification", code)
    user.verified = True
    db.commit()
    db.refresh(user)
    return user


def reset_user_password(db: Session, email: str, code: str, password: str) -> None:
    user = user_for_email(db, email)
    consume_verification_code(db, user, "password_reset", code)
    user.password_hash = hash_password(password)
    db.commit()


def complete_onboarding(db: Session, user: User, data: CompleteOnboardingRequest) -> User:
    """Define o tipo de conta (cliente ou empresa) após registo pendente."""
    if not user.verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Verifique o seu email antes de continuar",
        )
    if user.user_type != USER_TYPE_USUARIO and user.status != USER_STATUS_PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Conta já configurada",
        )

    if user.user_type in (USER_TYPE_CLIENT, USER_TYPE_COMPANY):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Conta já configurada",
        )

    if data.choice == "carga":
        user.user_type = USER_TYPE_CLIENT
        db.add(Client(user_id=user.id, client_type="individual"))
    else:
        user.user_type = USER_TYPE_COMPANY
        db.add(Company(user_id=user.id, company_name=user.name))

    user.status = USER_STATUS_ACTIVE

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _raise_duplicate_registration(exc)
    db.refresh(user)
    return user


def authenticate_user(db: Session, email: str, password: str) -> User:
    """
    Valida credenciais por email e senha.
    Devolve o utilizador ou lança 401.
    """
    user = db.query(User).filter(User.email == email.strip().lower()).first()

    if user is None or not verify_password(password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email ou senha incorretos",
        )

    if user.status not in (USER_STATUS_ACTIVE, USER_STATUS_PENDING):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Conta inativa ou suspensa",
        )

    if not user.verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="EMAIL_NOT_VERIFIED",
        )

    return user


def _verify_google_token(id_token: str) -> dict:
    if not settings.GOOGLE_CLIENT_IDS:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Google login nao configurado",
        )

    last_error: Exception | None = None
    for client_id in settings.GOOGLE_CLIENT_IDS:
        try:
            return google_id_token.verify_oauth2_token(
                id_token,
                google_requests.Request(),
                client_id,
            )
        except ValueError as exc:
            last_error = exc

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token Google invalido",
    ) from last_error


def authenticate_google_user(db: Session, token: str) -> User:
    """
    Valida id_token do Google e devolve/cria utilizador local.
    Novas contas entram como cliente individual.
    """
    payload = _verify_google_token(token)
    email = payload.get("email")
    email_verified = payload.get("email_verified")
    google_sub = payload.get("sub")

    if not email or not google_sub or email_verified is not True:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Conta Google sem email verificado",
        )

    user = db.query(User).filter(User.email == email).first()
    if user:
        if user.status not in (USER_STATUS_ACTIVE, USER_STATUS_PENDING):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Conta inativa ou suspensa",
            )
        return user

    user = User(
        name=payload.get("name") or email.split("@", 1)[0],
        email=email,
        phone=f"google:{google_sub}"[:30],
        password_hash=hash_password(secrets.token_urlsafe(32)),
        user_type=USER_TYPE_USUARIO,
        status=USER_STATUS_PENDING,
        profile_photo=payload.get("picture"),
        verified=True,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        _raise_duplicate_registration(exc)

    db.add(Wallet(user_id=user.id))

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _raise_duplicate_registration(exc)

    db.refresh(user)
    return user


def create_user_token(user: User) -> str:
    """Gera JWT com o id do utilizador no campo sub."""
    return create_access_token({"sub": str(user.id)})


def change_password(db: Session, user: User, data: PasswordChangeRequest) -> None:
    """Altera senha após validar a atual."""
    if not verify_password(data.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Senha atual incorreta",
        )
    if data.current_password == data.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A nova senha deve ser diferente da atual",
        )

    user.password_hash = hash_password(data.new_password)
    db.commit()
