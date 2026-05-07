"""
schemas.py — Modelos Pydantic de request/response compartilhados.
Os schemas do algoritmo (ConfigApuracao) ficam em modules/apuracao.py
por estarem acoplados ao motor de cálculo.
"""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, model_validator


# ── Auth ──────────────────────────────────────────────────────────────────────

class DevTokenRequest(BaseModel):
    email: str


class MagicLinkRequest(BaseModel):
    email: str


class LoginRequest(BaseModel):
    email:    str
    password: str


class UpdateMeRequest(BaseModel):
    nome: str


class ChangePasswordRequest(BaseModel):
    current_password: Optional[str] = None   # Obrigatório se já tiver senha definida
    new_password:     str


class TokenResponse(BaseModel):
    token: str
    user:  Dict[str, Any]


# ── Upload ────────────────────────────────────────────────────────────────────

class UploadResponse(BaseModel):
    file_id    : str
    filename   : str
    total_pages: int


# ── Sessão ────────────────────────────────────────────────────────────────────

class ConfigSessao(BaseModel):
    contas_proprias         : List[str] = []
    numeros_contas_proprias : List[str] = []
    modo                    : str       = 'padrao'


class CriarSessaoRequest(BaseModel):
    file_ids    : List[str] = []
    file_id     : Optional[str] = None   # backward compat — convertido para file_ids[0]
    session_name: str = "Nova apuração"
    config      : ConfigSessao = ConfigSessao()
    # Um banco por arquivo, na mesma ordem de file_ids.
    # None = usa o scanner genérico (chord-based) como fallback.
    bancos      : Optional[List[Optional[str]]] = None

    @model_validator(mode='after')
    def resolve_file_ids(self) -> 'CriarSessaoRequest':
        if self.file_id and not self.file_ids:
            self.file_ids = [self.file_id]
        if not self.file_ids:
            raise ValueError("Informe ao menos um file_id ou file_ids")
        return self


class MutateRequest(BaseModel):
    """
    Portão único de mutação (seção 10 do documento).

    Operações suportadas:
      toggle_active  → params: {active: bool}
      set_flag       → params: {flag: str}  (auto-ajusta active)
      edit_field     → params: {campo: valor, ...}
      add_comentario → params: {comentario: str}
      reset          → params: {} (volta a versions[0])
      create_lanc    → params: {data, descricao, valor, active, flag, bbox?}
                       target ignorado — gera novo lanc_id
    """
    op        : str
    target    : Optional[str] = None   # lanc_id — obrigatório exceto para create_lanc
    params    : Dict[str, Any] = {}
    source    : str = "api"
    timestamp : Optional[str] = None


class PatchSessaoRequest(BaseModel):
    """Atualiza metadados da sessão. Campos opcionais — envia apenas o que mudar."""
    session_name : Optional[str]  = None
    pinned       : Optional[bool] = None


# ── Créditos ──────────────────────────────────────────────────────────────────

class SaldoResponse(BaseModel):
    user_id: str
    saldo  : int


# ── Pagamentos ────────────────────────────────────────────────────────────────

class AssinarPlanoMPRequest(BaseModel):
    """Inicia assinatura mensal via Mercado Pago."""
    plano: str   # essencial | profissional | escritorio


class CheckoutIPRequest(BaseModel):
    """Inicia checkout InfinityPay para avulso ou plano anual."""
    tipo  : str            # avulso | anual
    plano : Optional[str] = None   # obrigatório quando tipo=anual


# ── Assinaturas ───────────────────────────────────────────────────────────────

class ExtrairAssinaturaRequest(BaseModel):
    """Deriva assinatura a partir de retângulo desenhado no editor de PDF."""
    file_id : str
    page    : int                   # 1-indexed
    x0      : float
    y0      : float
    x1      : float
    y1      : float


class CampoDescricao(BaseModel):
    """Define uma coluna de texto nomeada pelo usuário dentro de uma assinatura."""
    label : str
    x_min : float
    x_max : float


class SalvarAssinaturaRequest(BaseModel):
    """Persiste assinatura derivada ou editada manualmente pelo usuário."""
    bank_name      : str
    font_dominant  : Optional[str] = None
    anchor_x_min   : float
    anchor_x_max   : float
    anchor_pattern : str
    value_x_min    : float
    value_x_max    : float
    signal_logic   : str = "prefix_minus"
    campos         : List[CampoDescricao] = []
    rect_x0        : Optional[float] = None
    rect_x1        : Optional[float] = None


# ── Admin ─────────────────────────────────────────────────────────────────────

class PatchFlagGrupoRequest(BaseModel):
    """Override de flag de grupo pelo contador."""
    flag: str   # FlagGrupo: estavel | ruido | sem_historico | auto_transferencia | renda_circular | renda_duplicada | ignorar


# ── Admin ─────────────────────────────────────────────────────────────────────

class AdminLoginRequest(BaseModel):
    email:    str
    password: str


class AdminConfigRequest(BaseModel):
    """Campos editáveis via PATCH /admin/config. Todos opcionais."""
    bonus_cadastro:                  Optional[int]   = None
    plano_essencial_creditos:        Optional[int]   = None
    plano_essencial_preco_mensal:    Optional[float] = None
    plano_essencial_preco_anual:     Optional[float] = None
    plano_essencial_preco_avulso:    Optional[float] = None
    plano_profissional_creditos:     Optional[int]   = None
    plano_profissional_preco_mensal: Optional[float] = None
    plano_profissional_preco_anual:  Optional[float] = None
    plano_profissional_preco_avulso: Optional[float] = None
    plano_escritorio_creditos:       Optional[int]   = None
    plano_escritorio_preco_mensal:   Optional[float] = None
    plano_escritorio_preco_anual:    Optional[float] = None
    plano_escritorio_preco_avulso:   Optional[float] = None
    avulso_quantidade:               Optional[int]   = None
    avulso_preco:                    Optional[float] = None


class AdminCreditosRequest(BaseModel):
    delta:  int   # positivo = adicionar, negativo = remover
    motivo: str


class AdminPlanoRequest(BaseModel):
    plano:    str            # trial | essencial | profissional | escritorio
    creditos: Optional[int] = None  # se informado, substitui o saldo atual

