"""
Constantes da aplicação Fretix.
"""

# Tipos de carga (ecrã "Cadastrar nova carga")
LOAD_TYPES = [
    {"id": "areia", "label": "Areia", "image": "/static/load-types/areea.png"},
    {"id": "cimento", "label": "Cimento", "image": "/static/load-types/cimento.png"},
    {"id": "cascalho", "label": "Cascalho", "image": "/static/load-types/area.png"},
    {"id": "combustivel", "label": "Combustível", "image": "/static/load-types/Asset_combustivel_qz_server.png"},
    {"id": "ferro", "label": "Ferro", "image": "/static/load-types/ferro.png"},
    {"id": "madeira", "label": "Madeira", "image": "/static/load-types/area.png"},
    {"id": "graos", "label": "Grãos", "image": "/static/load-types/area.png"},
    {"id": "mercadoria_geral", "label": "Mercadoria geral", "image": "/static/load-types/Asset_geral_qz_server.png"},
    {"id": "outro", "label": "Outro", "icon": "ellipsis-horizontal"},
]

LOAD_TYPE_IDS = {item["id"] for item in LOAD_TYPES}

LOAD_TYPE_LABELS = {item["id"]: item["label"] for item in LOAD_TYPES}

# Aliases aceites pelo app (normalizados para o id canonico)
LOAD_TYPE_ALIASES = {
    "mercadoria": "mercadoria_geral",
    "mercadoria-geral": "mercadoria_geral",
}

# Carga completa / meia carga (badge no detalhe)
LOAD_FILL_TYPES = [
    {"id": "completa", "label": "Carga completa"},
    {"id": "meia_carga", "label": "Meia carga"},
]
LOAD_FILL_IDS = {item["id"] for item in LOAD_FILL_TYPES}
LOAD_FILL_LABELS = {item["id"]: item["label"] for item in LOAD_FILL_TYPES}

# Velocidade média para estimativa de tempo de rota (km/h)
ROUTE_AVG_SPEED_KMH_MIN = 50
ROUTE_AVG_SPEED_KMH_MAX = 60

MAX_LOAD_IMAGES = 5

# Unidades de peso aceites
WEIGHT_UNITS = ["ton", "kg"]

# ---------------------------------------------------------------------------
# Estados da viagem
# ---------------------------------------------------------------------------

TRIP_STATUS_WAITING = "aguardando_inicio"
TRIP_STATUS_EN_ROUTE_PICKUP = "indo_carregar"
TRIP_STATUS_ARRIVED_PICKUP = "chegou_origem"
TRIP_STATUS_LOADED = "carregado"
TRIP_STATUS_STARTED = "viagem_iniciada"
TRIP_STATUS_WAITING_CLIENT = "aguardando_cliente"
TRIP_STATUS_COMPLETED = "concluida"

# Estados em que o motorista está operacionalmente ocupado. Uma viagem que
# aguarda apenas a confirmação do cliente já não bloqueia uma nova recolha.
TRIP_EXECUTION_STATUSES = [
    TRIP_STATUS_EN_ROUTE_PICKUP,
    TRIP_STATUS_ARRIVED_PICKUP,
    TRIP_STATUS_LOADED,
    TRIP_STATUS_STARTED,
]

# GPS da viagem: controla quantos pontos entram no historico da rota.
TRIP_LOCATION_MIN_INTERVAL_SECONDS = 10
TRIP_LOCATION_MIN_DISTANCE_METERS = 50
TRIP_LOCATION_HEARTBEAT_SECONDS = 120

# Filtros do app motorista (Minhas Viagens)
TRIP_GROUP_IN_PROGRESS = "em_andamento"
TRIP_GROUP_COMPLETED = "concluidas"

TRIP_GROUP_STATUSES = {
    TRIP_GROUP_IN_PROGRESS: [
        TRIP_STATUS_WAITING,
        TRIP_STATUS_EN_ROUTE_PICKUP,
        TRIP_STATUS_ARRIVED_PICKUP,
        TRIP_STATUS_LOADED,
        TRIP_STATUS_STARTED,
    ],
    TRIP_GROUP_COMPLETED: [TRIP_STATUS_WAITING_CLIENT, TRIP_STATUS_COMPLETED],
}

# Tipos de paragem durante a viagem
STOP_TYPES = [
    {"id": "abastecimento", "label": "Abastecimento"},
    {"id": "descanso", "label": "Descanso"},
    {"id": "mecanica", "label": "Mecânica"},
    {"id": "outros", "label": "Outros"},
]

STOP_TYPE_IDS = {item["id"] for item in STOP_TYPES}

# ---------------------------------------------------------------------------
# Status de carga — espelham os estados da viagem correspondentes
# ---------------------------------------------------------------------------

LOAD_STATUS_AVAILABLE = "disponivel"
LOAD_STATUS_ACCEPTED = "aceite"
LOAD_STATUS_EN_ROUTE_PICKUP = TRIP_STATUS_EN_ROUTE_PICKUP   # "indo_carregar"
LOAD_STATUS_ARRIVED_PICKUP = TRIP_STATUS_ARRIVED_PICKUP      # "chegou_origem"
LOAD_STATUS_LOADED = TRIP_STATUS_LOADED                      # "carregado"
LOAD_STATUS_IN_TRANSIT = "em_viagem"
LOAD_STATUS_WAITING_CLIENT = "aguardando_cliente"            # motorista chegou, aguarda cliente
LOAD_STATUS_COMPLETED = "concluida"
LOAD_STATUS_CANCELLED = "cancelada"

LOAD_ACTIVE_STATUSES = [
    LOAD_STATUS_ACCEPTED,
    LOAD_STATUS_EN_ROUTE_PICKUP,
    LOAD_STATUS_ARRIVED_PICKUP,
    LOAD_STATUS_LOADED,
    LOAD_STATUS_IN_TRANSIT,
    LOAD_STATUS_WAITING_CLIENT,
]

# ---------------------------------------------------------------------------
# Status exibidos no feed de atividades do cliente
# ---------------------------------------------------------------------------

ACTIVITY_IN_PROGRESS = "em_andamento"
ACTIVITY_NEGOTIATING = "em_negociacao"
ACTIVITY_COMPLETED = "concluida"

# ---------------------------------------------------------------------------
# Status de propostas e negociacoes
# ---------------------------------------------------------------------------

PROPOSAL_STATUS_PENDING = "pendente"
PROPOSAL_STATUS_NEGOTIATING = "em_negociacao"
PROPOSAL_STATUS_ACCEPTED = "aceite"
PROPOSAL_STATUS_REJECTED = "recusada"
PROPOSAL_OPEN_STATUSES = [PROPOSAL_STATUS_PENDING, PROPOSAL_STATUS_NEGOTIATING]

NEGOTIATION_STATUS_PENDING = "pendente"
NEGOTIATION_STATUS_ACCEPTED = "aceite"
NEGOTIATION_STATUS_REJECTED = "recusada"
NEGOTIATION_STATUS_REPLACED = "substituida"

# ---------------------------------------------------------------------------
# Veículos
# ---------------------------------------------------------------------------

VEHICLE_STATUS_AVAILABLE = "disponivel"
VEHICLE_STATUS_UNAVAILABLE = "indisponivel"
VEHICLE_STATUS_MAINTENANCE = "manutencao"
VEHICLE_STATUS_INACTIVE = "inativo"
VEHICLE_STATUSES = {
    VEHICLE_STATUS_AVAILABLE,
    VEHICLE_STATUS_UNAVAILABLE,
    VEHICLE_STATUS_MAINTENANCE,
    VEHICLE_STATUS_INACTIVE,
}

# ---------------------------------------------------------------------------
# Utilizadores
# ---------------------------------------------------------------------------

USER_TYPE_USUARIO = "usuario"
USER_TYPE_CLIENT = "cliente"
USER_TYPE_COMPANY = "empresa"
USER_TYPE_DRIVER = "motorista"
USER_STATUS_PENDING = "pendente"
USER_STATUS_ACTIVE = "ativo"

# ---------------------------------------------------------------------------
# Carteira e pagamentos
# ---------------------------------------------------------------------------

TRANSACTION_TYPE_DEPOSIT = "deposito"
TRANSACTION_TYPE_WITHDRAWAL = "levantamento"
TRANSACTION_TYPE_TRANSPORT_PAYMENT = "transport_payment"
TRANSACTION_TYPE_ESCROW_RECEIVED = "escrow_received"
PAYMENT_METHOD_MPESA = "mpesa"
PAYMENT_METHOD_WALLET = "wallet"
PAYMENT_STATUS_PENDING = "pendente"
PAYMENT_STATUS_COMPLETED = "concluido"
PAYMENT_STATUS_FAILED = "falhou"
TRANSACTION_STATUS_PENDING = "pendente"
TRANSACTION_STATUS_COMPLETED = "concluido"

# ---------------------------------------------------------------------------
# Tipos de documento (perfil)
# ---------------------------------------------------------------------------

DOCUMENT_TYPES = [
    {"id": "bi", "label": "Bilhete de Identidade"},
    {"id": "carta", "label": "Carta de condução"},
    {"id": "licenca", "label": "Licença / Alvará"},
    {"id": "nuit", "label": "NUIT / Documento fiscal"},
    {"id": "outro", "label": "Outro"},
]
DOCUMENT_TYPE_IDS = {item["id"] for item in DOCUMENT_TYPES}

DOCUMENT_STATUS_PENDING = "pendente"
DOCUMENT_STATUS_APPROVED = "aprovado"
DOCUMENT_STATUS_REJECTED = "rejeitado"

# ---------------------------------------------------------------------------
# Segurança de uploads: extensões e MIME types bloqueados
# ---------------------------------------------------------------------------

BLOCKED_FILE_EXTENSIONS = {
    # Linguagens de programação e scripting
    ".py", ".js", ".ts", ".jsx", ".tsx", ".php", ".php3", ".php4", ".php5", ".php6", ".php7", ".php8",
    ".phtml", ".phar", ".pl", ".rb", ".go", ".rs", ".c", ".cpp", ".java", ".cs", ".vb",
    ".sh", ".bash", ".zsh", ".ksh", ".csh", ".bat", ".cmd", ".com", ".exe", ".scr",
    # Web e documento executável
    ".html", ".htm", ".xml", ".svg", ".css", ".less", ".scss", ".sass",
    # Ficheiros de desenvolvimento
    ".asp", ".aspx", ".jsp", ".jspx", ".cgi", ".fcgi", ".wsgi",
    # Ficheiros de sistema
    ".dll", ".so", ".dylib", ".class", ".jar",
    # Scripts de dados
    ".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".config",
    # Executáveis comprimidos
    ".zip", ".tar", ".gz", ".rar", ".7z", ".bz2",
}

BLOCKED_CONTENT_TYPES = {
    # Executáveis
    "application/x-msdownload",
    "application/x-msdos-program",
    "application/x-executable",
    "application/x-elf",
    "application/x-object",
    "application/x-sharedlib",
    "application/x-shellscript",
    "application/x-perl",
    "application/x-python",
    # Web/Script
    "text/html",
    "application/xhtml+xml",
    "text/javascript",
    "application/javascript",
    "application/x-javascript",
    "text/css",
    "image/svg+xml",
    "application/xml",
    "text/xml",
    "application/x-php",
    # Arquivo
    "application/zip",
    "application/x-tar",
    "application/gzip",
    "application/x-rar-compressed",
    "application/x-7z-compressed",
}
