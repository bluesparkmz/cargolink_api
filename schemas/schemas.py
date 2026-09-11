"""
Schemas Pydantic CargoLink — validação de entrada e saída da API.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, computed_field

from constants import USER_STATUS_PENDING, USER_TYPE_USUARIO, VEHICLE_STATUS_AVAILABLE


# ---------------------------------------------------------------------------
# Autenticação
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    """Dados para criar nova conta (tipo definido depois no onboarding)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=2, max_length=150)
    email: EmailStr = Field(...)
    password: str = Field(..., min_length=6, max_length=128)
    phone: str | None = None


class CompleteOnboardingRequest(BaseModel):
    """Escolha do tipo de conta após registo."""

    choice: Literal["carga", "camioes"]


class LoginRequest(BaseModel):
    """Credenciais de login (email + senha)."""

    email: EmailStr
    password: str


class GoogleLoginRequest(BaseModel):
    """Token do Google recebido pelo app."""

    id_token: str = Field(..., min_length=20)


class PasswordChangeRequest(BaseModel):
    """Alterar senha (ecrã Segurança do perfil)."""

    current_password: str = Field(..., min_length=6, max_length=128)
    new_password: str = Field(..., min_length=6, max_length=128)


class TokenResponse(BaseModel):
    """Resposta com JWT após login ou registo."""

    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    """Dados públicos do utilizador autenticado."""

    id: int
    name: str
    email: str | None
    phone: str
    user_type: str
    status: str
    verified: bool
    profile_photo: str | None = None

    model_config = {"from_attributes": True}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def needs_onboarding(self) -> bool:
        return self.user_type == USER_TYPE_USUARIO or self.status == USER_STATUS_PENDING


# ---------------------------------------------------------------------------
# Utilizadores
# ---------------------------------------------------------------------------


class UserUpdateRequest(BaseModel):
    """Atualização dos dados base do utilizador."""

    name: str | None = Field(None, min_length=2, max_length=150)
    email: EmailStr | None = None
    profile_photo: str | None = None


class ClientProfileResponse(BaseModel):
    """Perfil cliente ligado ao utilizador."""

    id: int
    client_type: str
    company_name: str | None = None
    tax_id: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None

    model_config = {"from_attributes": True}


class DriverProfileResponse(BaseModel):
    """Perfil motorista ligado ao utilizador."""

    id: int
    company_id: int | None = None
    license_number: str | None = None
    license_expiry: date | None = None
    years_experience: int
    average_rating: float
    total_trips: int
    available: bool
    current_lat: float | None = None
    current_lng: float | None = None
    location_updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class CompanyProfileResponse(BaseModel):
    """Perfil da empresa transportadora."""

    id: int
    company_name: str
    tax_id: str | None = None
    license_number: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    average_rating: float
    total_trips: int
    verified: bool

    model_config = {"from_attributes": True}


class CompanyProfileUpdateRequest(BaseModel):
    """Atualizacao do perfil da empresa transportadora."""

    company_name: str | None = Field(None, min_length=2, max_length=150)
    tax_id: str | None = None
    license_number: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None


class UserProfileResponse(UserResponse):
    """Utilizador com perfil cliente ou motorista."""

    client: ClientProfileResponse | None = None
    company: CompanyProfileResponse | None = None
    driver: DriverProfileResponse | None = None


class ClientProfileUpdateRequest(BaseModel):
    """Atualização do perfil cliente."""

    client_type: str | None = None
    company_name: str | None = None
    tax_id: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None


class DriverProfileUpdateRequest(BaseModel):
    """Atualização do perfil motorista."""

    license_number: str | None = None
    license_expiry: date | None = None
    years_experience: int | None = None
    available: bool | None = None


# ---------------------------------------------------------------------------
# Clientes
# ---------------------------------------------------------------------------


class ClientUserSummary(BaseModel):
    """Dados públicos do utilizador ligado ao cliente."""

    id: int
    name: str
    phone: str
    email: str | None = None
    profile_photo: str | None = None
    verified: bool

    model_config = {"from_attributes": True}


class ClientDetailResponse(ClientProfileResponse):
    """Cliente com dados do utilizador."""

    user: ClientUserSummary


class ClientListItem(BaseModel):
    """Item resumido na listagem de clientes."""

    id: int
    user_id: int
    name: str
    client_type: str
    city: str | None = None
    company_name: str | None = None
    verified: bool

    model_config = {"from_attributes": True}


class ClientStatsResponse(BaseModel):
    """Cards Minhas atividades no perfil do cliente."""

    published_count: int
    in_progress_count: int
    completed_count: int
    average_rating: float | None = None
    rating_count: int = 0


class ConvertClientToCompanyRequest(BaseModel):
    """Converter perfil cliente para empresa transportadora."""

    company_name: str = Field(..., min_length=2, max_length=150)
    tax_id: str | None = None
    license_number: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None


class AvailabilityUpdateRequest(BaseModel):
    """Alterar disponibilidade do motorista."""

    available: bool


class LocationUpdateRequest(BaseModel):
    """Atualizar posição GPS no mapa."""

    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    sync_vehicles: bool = Field(
        True,
        description="Ao atualizar motorista, replica coords nos camiões disponíveis",
    )


# ---------------------------------------------------------------------------
# Motoristas
# ---------------------------------------------------------------------------


class DriverUserSummary(BaseModel):
    """Dados públicos do utilizador ligado ao motorista."""

    id: int
    name: str
    phone: str
    email: str | None = None
    profile_photo: str | None = None
    verified: bool

    model_config = {"from_attributes": True}


class DriverDetailResponse(DriverProfileResponse):
    """Motorista com dados do utilizador."""

    user: DriverUserSummary


class DriverListItem(BaseModel):
    """Item resumido na listagem de motoristas."""

    id: int
    user_id: int
    company_id: int | None = None
    name: str
    email: str | None = None
    average_rating: float
    total_trips: int
    available: bool
    profile_photo: str | None = None
    verified: bool
    current_lat: float | None = None
    current_lng: float | None = None
    location_updated_at: datetime | None = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Empresas transportadoras
# ---------------------------------------------------------------------------


class CompanyUserSummary(BaseModel):
    """Dados publicos do utilizador ligado a empresa."""

    id: int
    name: str
    phone: str
    email: str | None = None
    profile_photo: str | None = None
    verified: bool

    model_config = {"from_attributes": True}


class CompanyDetailResponse(CompanyProfileResponse):
    """Empresa com dados do utilizador."""

    user: CompanyUserSummary


class CompanyListItem(BaseModel):
    """Item resumido na listagem de empresas transportadoras."""

    id: int
    user_id: int
    company_name: str
    city: str | None = None
    state: str | None = None
    average_rating: float
    total_trips: int
    verified: bool

    model_config = {"from_attributes": True}


class CompanyDriverAttachRequest(BaseModel):
    """Associar motorista existente a empresa autenticada pelo email de login."""

    email: EmailStr


class CompanyDriverCreateRequest(BaseModel):
    """Criar novo motorista e associá-lo à empresa autenticada."""

    name: str = Field(..., min_length=2, max_length=150)
    email: EmailStr
    phone: str = Field(..., min_length=7, max_length=30)
    license_number: str = Field(..., min_length=5, max_length=100, description="Número da carta de condução (obrigatório)")
    license_expiry: date | None = None
    years_experience: int = Field(0, ge=0)


class CompanyDriverCreateResponse(BaseModel):
    """Resposta ao criar motorista com senha temporária."""

    id: int
    user_id: int
    name: str
    email: str | None
    phone: str
    company_id: int
    temporary_password: str
    average_rating: float
    total_trips: int
    available: bool

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Cargas
# ---------------------------------------------------------------------------


class LoadImageResponse(BaseModel):
    """Imagem associada à carga."""

    id: int
    image_url: str
    is_primary: bool

    model_config = {"from_attributes": True}


class LoadImageCreateRequest(BaseModel):
    """Adicionar imagem à carga."""

    image_url: str
    is_primary: bool = False


class LoadTypeItem(BaseModel):
    """Tipo de carga para o selector do app."""

    id: str
    label: str
    icon: str | None = None
    image: str | None = None


class LoadFillTypeItem(BaseModel):
    """Carga completa / meia carga."""

    id: str
    label: str


class LoadCreateRequest(BaseModel):
    """Publicar nova carga (passos 1–3 do app num único pedido)."""

    load_type: str = Field(..., min_length=2, max_length=100)
    load_name: str | None = None
    description: str | None = None
    weight: float | None = None
    weight_unit: str | None = "ton"
    volume: float | None = None
    value: float | None = None
    negotiable: bool = True
    origin: str = Field(..., min_length=2, max_length=150)
    destination: str = Field(..., min_length=2, max_length=150)
    origin_lat: float | None = None
    origin_lng: float | None = None
    destination_lat: float | None = None
    destination_lng: float | None = None
    departure_date: date | None = None
    load_fill: str | None = None
    suggested_vehicle_type: str | None = Field(None, max_length=150)
    instructions: str | None = None
    images: list[LoadImageCreateRequest] | None = None


class LoadCreateRequestForm(BaseModel):
    """Publicar nova carga via multipart/form-data (dropdowns)."""

    load_type: Literal[
        "areia",
        "cimento",
        "cascalho",
        "combustivel",
        "ferro",
        "madeira",
        "graos",
        "mercadoria_geral",
        "outro",
    ] = Field(default="mercadoria_geral", description="Tipo de carga")
    load_name: str = Field(default="Carga de teste", max_length=150)
    description: str = Field(default="Descrição de teste", description="Descrição da carga")
    weight: float = Field(default=150, ge=0, description="Peso")
    weight_unit: Literal["ton", "kg"] = Field(default="ton", description="Unidade de peso")
    volume: float = Field(default=25, ge=0, description="Volume")
    value: float = Field(default=500000, ge=0, description="Valor")
    negotiable: bool = Field(default=True, description="Negociável")
    origin: str = Field(default="Maputo", min_length=2, max_length=150, description="Origem")
    destination: str = Field(default="Beira", min_length=2, max_length=150, description="Destino")
    origin_lat: float = Field(default=-23.8245, ge=-90, le=90)
    origin_lng: float = Field(default=35.3075, ge=-180, le=180)
    destination_lat: float = Field(default=-19.8432, ge=-90, le=90)
    destination_lng: float = Field(default=34.8386, ge=-180, le=180)
    departure_date: date = Field(default="2026-06-15", description="Data de saída")
    load_fill: Literal["completa", "meia_carga"] = Field(default="completa", description="Tipo de carga")
    suggested_vehicle_type: str = Field(default="Camião", max_length=150)
    instructions: str = Field(default="Carga frágil - manusejar com cuidado", description="Instruções especiais")


class LoadUpdateRequest(BaseModel):
    """Atualizar carga existente."""

    load_type: str | None = None
    load_name: str | None = None
    description: str | None = None
    weight: float | None = None
    weight_unit: str | None = None
    volume: float | None = None
    value: float | None = None
    negotiable: bool | None = None
    origin: str | None = None
    destination: str | None = None
    origin_lat: float | None = None
    origin_lng: float | None = None
    destination_lat: float | None = None
    destination_lng: float | None = None
    departure_date: date | None = None
    load_fill: str | None = None
    suggested_vehicle_type: str | None = Field(None, max_length=150)
    instructions: str | None = None
    status: str | None = None


class LoadResponse(BaseModel):
    """Resposta padrão de uma carga."""

    id: int
    client_id: int
    code: str
    load_type: str
    load_name: str | None = None
    description: str | None = None
    weight: float | None = None
    weight_unit: str | None = None
    volume: float | None = None
    value: float | None = None
    negotiable: bool
    origin: str
    destination: str
    origin_lat: float | None = None
    origin_lng: float | None = None
    destination_lat: float | None = None
    destination_lng: float | None = None
    departure_date: date | None = None
    load_fill: str | None = None
    suggested_vehicle_type: str | None = None
    instructions: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class LoadSenderSummary(BaseModel):
    """Remetente (cliente) no detalhe da carga."""

    client_id: int
    user_id: int
    name: str
    phone: str
    email: str | None = None
    profile_photo: str | None = None
    verified: bool
    average_rating: float | None = None
    rating_count: int = 0


class LoadRouteEstimate(BaseModel):
    """Distância e tempo estimados (mapa da secção Rota)."""

    distance_km: float | None = None
    estimated_time_min: int | None = None
    estimated_time_label: str | None = None


class LoadDetailResponse(LoadResponse):
    """Detalhe completo para o ecrã Detalhes da Carga."""

    images: list[LoadImageResponse] = []
    load_type_label: str | None = None
    load_fill_label: str | None = None
    sender: LoadSenderSummary
    route: LoadRouteEstimate | None = None
    proposals_count: int = 0


class LoadListItem(BaseModel):
    """Item resumido na listagem de cargas."""

    id: int
    code: str
    load_type: str
    load_name: str | None = None
    origin: str
    destination: str
    origin_lat: float | None = None
    origin_lng: float | None = None
    destination_lat: float | None = None
    destination_lng: float | None = None
    weight: float | None = None
    weight_unit: str | None = None
    value: float | None = None
    negotiable: bool
    status: str
    departure_date: date | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class LoadProposalCreateRequest(BaseModel):
    """Proposta da empresa para uma carga."""

    proposed_value: float | None = None
    message: str | None = None
    driver_id: int | None = None
    vehicle_id: int | None = None


class LoadProposalResponse(BaseModel):
    """Proposta registada."""

    id: int
    load_id: int
    company_id: int | None = None
    driver_id: int | None = None
    vehicle_id: int | None = None
    proposed_value: float | None = None
    message: str | None = None
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ProposalLoadSummary(BaseModel):
    """Resumo da carga ligada a proposta."""

    id: int
    code: str
    load_type: str
    load_name: str | None = None
    origin: str
    destination: str
    value: float | None = None
    negotiable: bool = True
    status: str
    departure_date: date | None = None


class ProposalCompanySummary(BaseModel):
    """Resumo da empresa que fez a proposta."""

    id: int
    company_name: str
    average_rating: float = 0.0
    total_trips: int = 0
    verified: bool = False
    logo_url: str | None = None


class ProposalDriverSummary(BaseModel):
    """Resumo do motorista atribuido a proposta."""

    id: int
    name: str
    average_rating: float = 0.0
    total_trips: int = 0
    available: bool = True
    avatar_url: str | None = None


class ProposalVehicleSummary(BaseModel):
    """Resumo do veiculo atribuido a proposta."""

    id: int
    plate: str
    brand: str | None = None
    model_name: str | None = None
    vehicle_type: str | None = None
    tonnage_capacity: float | None = None
    status: str


class LoadProposalDetailResponse(LoadProposalResponse):
    """Proposta com dados para ecras de listagem e detalhe."""

    load: ProposalLoadSummary
    company: ProposalCompanySummary | None = None
    driver: ProposalDriverSummary | None = None
    vehicle: ProposalVehicleSummary | None = None


class ProposalNegotiationCreateRequest(BaseModel):
    """Criar contraproposta com novo valor."""

    amount: float = Field(..., gt=0)
    message: str | None = Field(None, max_length=1000)


class ProposalNegotiationResponse(BaseModel):
    """Item do historico de negociacao."""

    id: int
    proposal_id: int
    sender_id: int
    sender_name: str | None = None
    sender_type: str | None = None
    amount: float
    message: str | None = None
    status: str
    created_at: datetime


class ProposalNegotiationDetailResponse(BaseModel):
    """Proposta com historico de negociacao."""

    proposal: LoadProposalDetailResponse
    negotiations: list[ProposalNegotiationResponse]


class VehicleDriverAssignmentRequest(BaseModel):
    # Associa ou remove motorista de um camião da empresa.
    driver_id: int | None = None


# ---------------------------------------------------------------------------
# Viagens
# ---------------------------------------------------------------------------


class TripVehicleSummary(BaseModel):
    """Resumo do camiao usado na viagem."""

    id: int
    company_id: int | None = None
    driver_id: int | None = None
    plate: str
    brand: str | None = None
    model_name: str | None = None
    vehicle_type: str | None = None
    tonnage_capacity: float | None = None
    volume_capacity: float | None = None
    photo: str | None = None
    status: str
    current_lat: float | None = None
    current_lng: float | None = None
    location_updated_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TripLoadSummary(BaseModel):
    """Resumo da carga da viagem."""

    id: int
    client_id: int
    code: str
    load_type: str
    load_name: str | None = None
    description: str | None = None
    weight: float | None = None
    weight_unit: str | None = None
    volume: float | None = None
    value: float | None = None
    negotiable: bool
    origin: str
    destination: str
    origin_lat: float | None = None
    origin_lng: float | None = None
    destination_lat: float | None = None
    destination_lng: float | None = None
    departure_date: date | None = None
    load_fill: str | None = None
    suggested_vehicle_type: str | None = None
    instructions: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TripDriverLoadSummary(BaseModel):
    # Carga visível no app motorista, sem qualquer valor financeiro.

    id: int
    client_id: int
    code: str
    load_type: str
    load_name: str | None = None
    description: str | None = None
    weight: float | None = None
    weight_unit: str | None = None
    volume: float | None = None
    negotiable: bool
    origin: str
    destination: str
    origin_lat: float | None = None
    origin_lng: float | None = None
    destination_lat: float | None = None
    destination_lng: float | None = None
    departure_date: date | None = None
    load_fill: str | None = None
    suggested_vehicle_type: str | None = None
    instructions: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TripDriverSummary(BaseModel):
    """Resumo do motorista da viagem."""

    id: int
    user_id: int
    company_id: int | None = None
    license_number: str | None = None
    years_experience: int | None = 0
    average_rating: float | None = 0.0
    total_trips: int | None = 0
    available: bool | None = True
    current_lat: float | None = None
    current_lng: float | None = None
    location_updated_at: datetime | None = None
    # campos do utilizador ligados ao motorista
    name: str | None = None
    phone: str | None = None
    profile_photo: str | None = None

    model_config = {"from_attributes": True}


class TripActivityResponse(BaseModel):
    """Atividade/evento registado na viagem."""

    id: int
    trip_id: int
    event_type: str
    title: str
    description: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TripResponse(BaseModel):
    """Viagem após aceite de proposta."""

    id: int
    load_id: int
    company_id: int | None = None
    driver_id: int | None
    vehicle_id: int | None
    status: str
    origin: str | None = None
    destination: str | None = None
    origin_lat: float | None = None
    origin_lng: float | None = None
    destination_lat: float | None = None
    destination_lng: float | None = None
    en_route_pickup_at: datetime | None = None
    arrived_pickup_at: datetime | None = None
    loaded_at: datetime | None = None
    started_at: datetime | None = None
    arrived_at: datetime | None = None
    client_confirmed_at: datetime | None = None
    completed_at: datetime | None = None
    total_distance_km: float | None = None
    traveled_distance_km: float | None = None
    estimated_time: str | None = None
    created_at: datetime
    load: TripLoadSummary | None = None
    vehicle: TripVehicleSummary | None = None
    driver: TripDriverSummary | None = None
    activities: list[TripActivityResponse] = []

    model_config = {"from_attributes": True}


class TripAssignVehicleRequest(BaseModel):
    # Empresa atribui camiao com motorista a uma viagem aceite.
    vehicle_id: int = Field(..., gt=0)


class TripStartRequest(BaseModel):
    """Iniciar viagem (opcional: veículo e distância estimada)."""

    vehicle_id: int | None = None
    total_distance_km: float | None = None
    estimated_time: str | None = None


class TripLocationCreateRequest(BaseModel):
    """Ponto GPS durante a viagem."""

    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    speed: float | None = None
    traveled_distance_km: float | None = None


class TripLocationResponse(BaseModel):
    """Localização registada."""

    id: int
    trip_id: int
    latitude: float
    longitude: float
    speed: float | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------


class StopTypeItem(BaseModel):
    """Tipo de paragem para o selector do app."""

    id: str
    label: str


class TripStopCreateRequest(BaseModel):
    """Registar paragem na viagem."""

    stop_type: str
    location_name: str | None = None
    address: str | None = None
    notes: str | None = None
    latitude: float | None = Field(None, ge=-90, le=90)
    longitude: float | None = Field(None, ge=-180, le=180)
    stopped_at: datetime | None = None


class TripStopResponse(BaseModel):
    """Paragem registada."""

    id: int
    trip_id: int
    stop_type: str
    location_name: str | None = None
    address: str | None = None
    notes: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    stopped_at: datetime
    resumed_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TripStopResumeRequest(BaseModel):
    """Retomar viagem após paragem — regista hora de saída."""

    resumed_at: datetime | None = None


class TripDriverListItem(BaseModel):
    """Item da lista Minhas Viagens (motorista)."""

    id: int
    load_code: str
    origin: str
    destination: str
    client_name: str
    status: str
    started_at: datetime | None = None
    estimated_time: str | None = None
    departure_date: date | None = None
    created_at: datetime


class TripDriverDetailResponse(TripResponse):
    """Detalhe da viagem para o motorista."""

    load_code: str
    load_type: str
    origin: str
    destination: str
    client_name: str
    client_phone: str | None = None
    progress_percent: float | None = None
    load: TripDriverLoadSummary | None = None
    stops: list[TripStopResponse] = []


# ---------------------------------------------------------------------------
# Documentos
# ---------------------------------------------------------------------------


class DocumentTypeItem(BaseModel):
    """Tipo de documento para upload."""

    id: str
    label: str


class DocumentCreateRequest(BaseModel):
    """Registar documento (URL do ficheiro já enviado ao storage)."""

    document_type: str = Field(..., min_length=2, max_length=50)
    file_url: str = Field(..., min_length=10)
    notes: str | None = None


class DocumentResponse(BaseModel):
    """Documento do utilizador."""

    id: int
    user_id: int
    document_type: str
    file_url: str
    status: str
    notes: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Dashboard / estatísticas
# ---------------------------------------------------------------------------


class DashboardStatsResponse(BaseModel):
    """Contagens para os cards do ecrã inicial."""

    available_loads: int
    active_loads: int
    completed_this_month: int
    available_vehicles: int


# ---------------------------------------------------------------------------
# Veículos (camiões)
# ---------------------------------------------------------------------------


class VehicleListItem(BaseModel):
    """Camião disponível na listagem horizontal."""

    id: int
    company_id: int | None = None
    company_name: str | None = None
    driver_id: int | None = None
    plate: str
    brand: str | None = None
    model_name: str | None = None
    vehicle_type: str | None = None
    tonnage_capacity: float | None = None
    photo: str | None = None
    status: str
    current_lat: float | None = None
    current_lng: float | None = None
    location_updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class VehicleDetailResponse(VehicleListItem):
    """Detalhe do veículo com dados do motorista."""

    company_name: str | None = None
    driver_name: str | None = None
    driver_rating: float | None = None
    driver_photo: str | None = None


class VehicleCreateRequest(BaseModel):
    """Empresa regista novo camião."""

    driver_id: int | None = None
    driver_email: EmailStr | None = None
    plate: str = Field(..., min_length=3, max_length=50)
    brand: str | None = Field(None, max_length=100)
    model_name: str | None = Field(None, max_length=100)
    vehicle_type: str | None = Field(None, max_length=100)
    tonnage_capacity: float | None = Field(None, gt=0)
    photo: str | None = None
    status: str = VEHICLE_STATUS_AVAILABLE
    current_lat: float | None = Field(None, ge=-90, le=90)
    current_lng: float | None = Field(None, ge=-180, le=180)


class VehicleUpdateRequest(BaseModel):
    """Atualização parcial do veículo."""

    driver_id: int | None = None
    driver_email: EmailStr | None = None
    plate: str | None = Field(None, min_length=3, max_length=50)
    brand: str | None = None
    model_name: str | None = None
    vehicle_type: str | None = None
    tonnage_capacity: float | None = Field(None, gt=0)
    photo: str | None = None
    status: str | None = None
    current_lat: float | None = Field(None, ge=-90, le=90)
    current_lng: float | None = Field(None, ge=-180, le=180)


# ---------------------------------------------------------------------------
# Carteira
# ---------------------------------------------------------------------------


class WalletBalanceResponse(BaseModel):
    """Saldo da carteira com valores disponíveis e em retenção."""

    available_balance: float
    pending_balance: float
    blocked_balance: float
    accounting_balance: float = 0
    frozen_balance: float = 0
    role: str | None = None
    currency: str = "MT"


class WalletTransactionResponse(BaseModel):
    """Movimento no extrato."""

    id: int
    transaction_type: str
    amount: float
    status: str
    reference: str | None = None
    description: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class WalletDepositRequest(BaseModel):
    """Pedido de depósito via M-Pesa (+ no app)."""

    amount: float = Field(..., gt=0, le=5_000_000)
    phone: str | None = Field(None, min_length=8, max_length=30)
    method: Literal["mpesa"] = "mpesa"


class WalletDepositResponse(BaseModel):
    """Resposta após iniciar depósito."""

    payment_id: int
    transaction_id: int
    amount: float
    status: str
    external_reference: str
    phone: str
    message: str


class WalletDepositStatusResponse(BaseModel):
    """Estado actual de um depósito (polling)."""

    payment_id: int
    amount: float
    status: str
    external_reference: str
    phone: str
    message: str


# ---------------------------------------------------------------------------
# Notificações
# ---------------------------------------------------------------------------


class NotificationResponse(BaseModel):
    """Alerta in-app."""

    id: int
    title: str
    body: str
    notification_type: str | None = None
    read: bool
    payload: dict | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class UnreadCountResponse(BaseModel):
    """Contagem de não lidas (sino do app)."""

    count: int


# ---------------------------------------------------------------------------
# Mensagens
# ---------------------------------------------------------------------------


class MessageCreateRequest(BaseModel):
    """Enviar mensagem numa conversa da carga."""

    receiver_id: int = Field(..., gt=0)
    body: str = Field(..., min_length=1, max_length=5000)
    attachment: str | None = None


class MessageResponse(BaseModel):
    """Mensagem no chat."""

    id: int
    load_id: int
    sender_id: int | None
    receiver_id: int | None
    body: str
    attachment: str | None = None
    read: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class ConversationSummary(BaseModel):
    """Resumo para lista de conversas."""

    load_id: int
    load_code: str
    other_user_id: int
    other_user_name: str
    last_message: str | None = None
    last_message_at: datetime | None = None
    unread_count: int


class MessagesSummaryResponse(BaseModel):
    """Badge total de mensagens não lidas."""

    unread_count: int


# ---------------------------------------------------------------------------
# Atividades recentes (cliente)
# ---------------------------------------------------------------------------


class ActivityItem(BaseModel):
    """Item do feed Atividades recentes."""

    load_id: int
    code: str
    origin: str
    destination: str
    load_type: str
    weight: float | None = None
    weight_unit: str | None = None
    display_status: str
    activity_at: datetime
    trip_id: int | None = None


# ---------------------------------------------------------------------------
# Rastreio de carga
# ---------------------------------------------------------------------------


class LoadTrackingResponse(BaseModel):
    """Rastreio por carga — viagem e GPS."""

    load_id: int
    load_code: str
    load_status: str
    trip_id: int | None = None
    trip_status: str | None = None
    trackable: bool
    locations: list[TripLocationResponse] = []
    last_location: TripLocationResponse | None = None


# ---------------------------------------------------------------------------
# Avaliacoes
# ---------------------------------------------------------------------------


class RatingCreateRequest(BaseModel):
    """Avaliar um participante apos viagem concluida."""

    rated_user_id: int = Field(..., gt=0)
    score: int = Field(..., ge=1, le=5)
    comment: str | None = Field(None, max_length=1000)


class RatingResponse(BaseModel):
    """Avaliacao de uma viagem."""

    id: int
    trip_id: int
    rater_id: int | None = None
    rated_user_id: int | None = None
    score: int | None = None
    comment: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
