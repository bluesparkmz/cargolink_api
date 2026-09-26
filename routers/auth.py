"""
Rotas de autenticação: registo, login e perfil atual.
"""

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from controllers.auth_controller import (
    authenticate_google_user,
    authenticate_user,
    change_password,
    complete_onboarding,
    create_user_token,
    create_verification_code,
    register_user,
    reset_user_password,
    user_for_email,
    verify_user_email,
)
from deps import get_current_user
from database import get_db
from models.models import User
from schemas.schemas import (
    CompleteOnboardingRequest,
    EmailCodeConfirmRequest,
    EmailCodeRequest,
    GoogleLoginRequest,
    LoginRequest,
    PasswordChangeRequest,
    PasswordResetConfirmRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from services.email_service import send_otp_email

router = APIRouter()


@router.post("/register", response_model=TokenResponse, status_code=201)
def register(
    data: RegisterRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Regista novo utilizador (pendente) e devolve token JWT."""
    user = register_user(db, data)
    code = create_verification_code(db, user, "email_verification")
    background_tasks.add_task(
        send_otp_email, user.email, code, "email_verification"
    )
    token = create_user_token(user)
    return TokenResponse(access_token=token)


@router.post("/verify-email/resend")
def resend_verification(
    data: EmailCodeRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    user = user_for_email(db, str(data.email), required=False)
    if user is not None and not user.verified:
        code = create_verification_code(db, user, "email_verification")
        background_tasks.add_task(
            send_otp_email, user.email, code, "email_verification"
        )
    return {"message": "Se a conta existir, o código será enviado"}


@router.post("/verify-email/confirm", response_model=TokenResponse)
def confirm_email(data: EmailCodeConfirmRequest, db: Session = Depends(get_db)):
    user = verify_user_email(db, str(data.email), data.code)
    return TokenResponse(access_token=create_user_token(user))


@router.post("/password-reset/request")
def request_password_reset(
    data: EmailCodeRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    user = user_for_email(db, str(data.email), required=False)
    if user is not None:
        code = create_verification_code(db, user, "password_reset")
        background_tasks.add_task(send_otp_email, user.email, code, "password_reset")
    return {"message": "Se a conta existir, o código será enviado"}


@router.post("/password-reset/confirm")
def confirm_password_reset(
    data: PasswordResetConfirmRequest, db: Session = Depends(get_db)
):
    reset_user_password(db, str(data.email), data.code, data.new_password)
    return {"message": "Senha alterada com sucesso"}


@router.post("/complete-onboarding", response_model=UserResponse)
def onboarding(
    data: CompleteOnboardingRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Define tipo de conta: carga (cliente) ou camioes (empresa)."""
    return complete_onboarding(db, current_user, data)


@router.post("/login", response_model=TokenResponse)
def login(data: LoginRequest, db: Session = Depends(get_db)):
    """Autentica por email e senha; devolve token JWT."""
    user = authenticate_user(db, data.email, data.password)
    token = create_user_token(user)
    return TokenResponse(access_token=token)


@router.post("/google", response_model=TokenResponse)
def google_login(data: GoogleLoginRequest, db: Session = Depends(get_db)):
    """Autentica com id_token do Google; devolve token JWT local."""
    user = authenticate_google_user(db, data.id_token)
    token = create_user_token(user)
    return TokenResponse(access_token=token)


@router.get("/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)):
    """Devolve dados do utilizador autenticado (requer Bearer token)."""
    return current_user


@router.patch("/password")
def update_password(
    data: PasswordChangeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Altera senha (ecrã Segurança do perfil)."""
    change_password(db, current_user, data)
    return {"message": "Senha alterada com sucesso"}
