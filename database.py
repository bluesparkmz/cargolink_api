"""
Ligação à base de dados PostgreSQL via SQLAlchemy.
Fornece sessão por pedido HTTP (dependency injection).
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from config import settings


def _database_url() -> str:
    """Usa o driver psycopg2 instalado mesmo se o provedor indicar psycopg v3."""
    url = settings.DATABASE_URL
    if not url:
        raise RuntimeError("DATABASE_URL não está configurada")
    if url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql+psycopg://", "postgresql+psycopg2://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg2://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url

# Motor de ligação à base de dados
engine = create_engine(_database_url(), pool_pre_ping=True)

# Fábrica de sessões (uma sessão por pedido)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Classe base para todos os modelos ORM."""

    pass


def get_db():
    """
    Gera uma sessão de base de dados e fecha-a no fim do pedido.
    Usado como dependência FastAPI.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
