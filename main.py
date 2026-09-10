"""
Ponto de entrada da API CargoLink.
Inicializa FastAPI, rotas e tabelas na base de dados.
"""

from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.staticfiles import StaticFiles

from config import settings
from database import Base, engine
from routers.auth import router as auth_router
from routers.admin import router as admin_router
from routers.clients import router as clients_router
from routers.companies import router as companies_router
from routers.documents import router as documents_router
from routers.documentation import router as documentation_router
from routers.drivers import router as drivers_router
from routers.frontend_test import router as frontend_test_router
from routers.gps_router import router as gps_router
from routers.loads import router as loads_router
from routers.driver_trips import router as driver_trips_router
from routers.messages import router as messages_router
from routers.notifications import router as notifications_router
from routers.proposals import router as proposals_router
from routers.ratings import router as ratings_router
from routers.stats import router as stats_router
from routers.trips import router as trips_router
from routers.trip_stops import router as trip_stops_router
from routers.financial_settings import router as financial_settings_router
from routers.users import router as users_router
from routers.vehicles import router as vehicles_router
from routers.wallet import router as wallet_router
from routers.websocket_router import router as websocket_router

# Importa modelos para o SQLAlchemy registar todas as tabelas
import models.models  # noqa: F401

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Cria tabelas ao arrancar (em dev; em produção usar migrações Alembic)."""
    Base.metadata.create_all(bind=engine)
    # Garante colunas de tabelas existentes
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS push_token TEXT;"))
            conn.execute(text("ALTER TABLE trips ADD COLUMN IF NOT EXISTS en_route_pickup_at TIMESTAMP;"))
            conn.execute(text("ALTER TABLE trips ADD COLUMN IF NOT EXISTS arrived_pickup_at TIMESTAMP;"))
            conn.execute(text("ALTER TABLE trips ADD COLUMN IF NOT EXISTS loaded_at TIMESTAMP;"))
            conn.execute(text("ALTER TABLE trip_stops ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER;"))
            conn.execute(text("ALTER TABLE trip_stops ADD COLUMN IF NOT EXISTS created_by_type VARCHAR(30);"))
            conn.execute(text("ALTER TABLE trip_stops ADD COLUMN IF NOT EXISTS status VARCHAR(30);"))
            conn.execute(text("ALTER TABLE trip_stops ADD COLUMN IF NOT EXISTS started_at TIMESTAMP;"))
            conn.execute(text("ALTER TABLE trip_stops ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP;"))
            conn.execute(text("ALTER TABLE trip_stops ADD COLUMN IF NOT EXISTS delay_fee_per_24h NUMERIC(12,2) DEFAULT 3000;"))
            conn.commit()
    except Exception as e:
        logger.warning("Falha ao adicionar colunas via SQL (podem já existir): %s", e)

    logger.info(
        "CargoLink API — M-Pesa sandbox | auto_confirm=%s",
        settings.AUTO_CONFIRM_MPESA_DEPOSITS,
    )
    yield


app = FastAPI(
    title="CargoLink API",
    description="API para o projeto CargoLink",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount(
    "/uploads",
    StaticFiles(directory=f"{settings.STORAGE_DIR}/uploads", check_dir=False),
    name="uploads",
)

app.mount(
    "/static",
    StaticFiles(directory="static", check_dir=False),
    name="static",
)

# Cada router é registado aqui, um ficheiro por domínio
app.include_router(auth_router, prefix="/auth", tags=["Auth"])
app.include_router(users_router, prefix="/users", tags=["Users"])
app.include_router(clients_router, prefix="/clients", tags=["Clients"])
app.include_router(companies_router, prefix="/companies", tags=["Companies"])
app.include_router(documents_router, prefix="/documents", tags=["Documents"])
app.include_router(documentation_router, prefix="/documentation", tags=["Documentation"])
app.include_router(drivers_router, prefix="/drivers", tags=["Drivers"])
app.include_router(frontend_test_router, prefix="/frontend-test", tags=["Frontend Test"])
app.include_router(loads_router, prefix="/loads", tags=["Loads"])
app.include_router(proposals_router, prefix="/proposals", tags=["Proposals"])
app.include_router(ratings_router, prefix="/ratings", tags=["Ratings"])
app.include_router(trips_router, prefix="/trips", tags=["Trips"])
app.include_router(trip_stops_router, prefix="/trip-stops", tags=["Trip Stops"])
app.include_router(driver_trips_router, prefix="/driver/trips", tags=["Driver"])
app.include_router(stats_router, prefix="/stats", tags=["Stats"])
app.include_router(vehicles_router, prefix="/vehicles", tags=["Vehicles"])
app.include_router(wallet_router, prefix="/wallet", tags=["Wallet"])
app.include_router(gps_router, tags=["GPS Rastreamento"])
app.include_router(notifications_router, prefix="/notifications", tags=["Notifications"])
app.include_router(messages_router, prefix="/messages", tags=["Messages"])
app.include_router(websocket_router, tags=["Realtime"])
app.include_router(admin_router, prefix="/admin", tags=["Admin"])
app.include_router(financial_settings_router, prefix="/admin/financial-settings", tags=["Admin Financial Settings"])


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )

    def set_multipart_property(path: str, property_name: str, schema: dict) -> None:
        request_ref = (
            openapi_schema.get("paths", {})
            .get(path, {})
            .get("post", {})
            .get("requestBody", {})
            .get("content", {})
            .get("multipart/form-data", {})
            .get("schema", {})
            .get("$ref")
        )
        if not request_ref:
            return

        schema_name = request_ref.rsplit("/", 1)[-1]
        properties = (
            openapi_schema.get("components", {})
            .get("schemas", {})
            .get(schema_name, {})
            .setdefault("properties", {})
        )
        properties[property_name] = schema

    set_multipart_property(
        "/loads",
        "images",
        {
            "type": "array",
            "items": {"type": "string", "format": "binary"},
            "title": "Images",
            "description": "Até 5 imagens (jpg, png)",
        },
    )
    set_multipart_property(
        "/vehicles",
        "photo",
        {
            "type": "string",
            "format": "binary",
            "title": "Photo",
            "description": "Foto do camião (jpg, png)",
        },
    )

    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi


@app.get("/")
def root():
    """Health check simples."""
    return {"app": "CargoLink", "status": "ok"}
