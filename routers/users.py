"""
Rotas de utilizadores: dados base e perfil geral.
"""

from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from controllers.users_controller import get_user_by_id, get_user_profile, update_user
from deps import get_current_user
from database import get_db
from models.models import User, UserPushToken
from schemas.schemas import (
    PushTokenRegistrationRequest,
    PushTokenRemovalRequest,
    UserProfileResponse,
    UserResponse,
    UserUpdateRequest,
)

router = APIRouter()


@router.get("/me/profile", response_model=UserProfileResponse)
def get_my_profile(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Perfil completo do utilizador autenticado (cliente ou motorista)."""
    return get_user_profile(db, current_user)


@router.patch("/me", response_model=UserResponse)
def update_my_user(
    data: UserUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Atualiza dados base do utilizador autenticado."""
    return update_user(db, current_user, data)


@router.get("/{user_id}", response_model=UserProfileResponse)
def get_user(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Consulta utilizador por id (requer autenticação)."""
    return get_user_by_id(db, user_id)


from pydantic import BaseModel

class PushTokenRequest(BaseModel):
    push_token: str

@router.patch("/me/push-token")
def update_push_token(
    data: PushTokenRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Atualiza o push token para notificações push nativas."""
    current_user.push_token = data.push_token
    db.commit()
    return {"status": "ok", "message": "Push token updated successfully"}


@router.post("/me/push-tokens")
def register_push_token(
    data: PushTokenRegistrationRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Regista ou reactiva este aparelho para notificações nativas."""
    device = db.query(UserPushToken).filter(UserPushToken.token == data.token).first()
    if device is None:
        device = UserPushToken(
            user_id=current_user.id,
            token=data.token,
            app_name=data.app_name,
            platform=data.platform,
        )
        db.add(device)
    else:
        device.user_id = current_user.id
        device.app_name = data.app_name
        device.platform = data.platform
        device.active = True
        device.last_seen_at = datetime.utcnow()
    db.commit()
    return {"status": "ok"}


@router.delete("/me/push-tokens")
def remove_push_token(
    data: PushTokenRemovalRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Desactiva o aparelho no logout sem afectar os restantes aparelhos."""
    db.query(UserPushToken).filter(
        UserPushToken.user_id == current_user.id,
        UserPushToken.token == data.token,
    ).update({UserPushToken.active: False}, synchronize_session=False)
    db.commit()
    return {"status": "ok"}
