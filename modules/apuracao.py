"""
modules/apuracao.py — Motor de Apuração de Renda

Base: algoritmo puro de 6 fases (v3.4).

Correções aplicadas (v3.3):
  [FIX-1] match_titular: fallback fuzzy no nome completo para capturar
          nicknames/grafias alternativas ("Paolla" vs "Pamella").
  [FIX-2] classificar_por_variancia: piso de frequência mínima (FREQ_MIN_MESES)
          antes da análise de CV — grupos raros não infam a mediana.
  [FIX-3] apurar Fase 6: subtrai circulares do historico antes de
          recalcular cv/seis/dez (estava detectando mas não removendo).
  [FIX-4] apurar: chama extrair_numeros_conta_titular internamente —
          não depende mais de o caller preencher numeros_contas_proprias.

Correções aplicadas (v3.4):
  [FIX-5] is_conta_propria: reconhece padrões de auto-investimento
          ("Resgate RDB/CDB", "Rendimento líquido", etc.) como conta
          própria na Fase 1, antes de chegarem na detecção circular.
  [FIX-6] CIRCULAR_WINDOW_HOURS reduzido de 72h para 24h — elimina falsos
          positivos de salário/consumo sem perder circulares reais.

v3.5:
  [ADD] modo 'liberal' em ConfigApuracao — ativa lógica pura de camadas
        emergentes (sem threshold fixo, organização por substrato/CV/intervalo).
  [ADD] modo 'conservador' aplica FREQ_MIN = 7 na Fase 4.

v3.6:
  [ADD-1] Fase 5b — detecção circular bilateral longitudinal: agrupa saídas
          por contraparte normalizada, cruza com grupos de entrada estáveis;
          se co-ocorrência >= CIRCULAR_LONGITUDINAL_FREQ (30 %) dos meses
          analisados → grupo marcado como 'circular_longitudinal' e excluído
          da composição final antes da Fase 6.

v3.7:
  [FIX-7] Fase 5b: cruzamento entrada/saída agora usa fuzzy (rapidfuzz.ratio,
          mesmo threshold do agrupamento) em vez de igualdade exata de string
          — evita miss silencioso por grafias alternativas ("Marta M Sales" vs
          "marta menezes sales").
  [FIX-8] Fase 5b: denominador da co-ocorrência corrigido de n_meses (total)
          para len(meses_entrada) — evita sub-detecção em grupos com presença
          esparsa (ex.: 4/10 meses de entrada = 40 %, não 4/15 = 26 %).
  [FIX-9] Fase 6: grupos com renda_base == 0 removidos de 'composicao' e
          movidos para novo campo 'inconclusivos' — mediana zero indica
          presença em < 50 % dos meses; não devem compor renda apurada.

v3.8:
  [FIX-10] Fase 6: totais_por_mes restaurado para incluir composicao +
           inconclusivos — representa fluxo real de entradas estáveis no
           período, não apenas as fontes com renda_base > 0.
  [ADD-2]  ConfigApuracao.circulares_longitudinais_manual: lista de nomes que
           o operador marca explicitamente como circulares quando não há saídas
           correspondentes no extrato (ex.: familiar que paga em espécie).
           Match fuzzy com mesmo threshold do agrupamento; motivo registrado
           como 'circular_longitudinal_manual'.
"""

import re
import statistics
import unicodedata
from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from typing import Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel
from rapidfuzz import fuzz as _rfuzz, process as _rprocess

# ── Tipos públicos ─────────────────────────────────────────────────────────────

FlagGrupo = Literal[
    'estavel',            # compõe renda_apurada
    'ruido',              # excluído — CV alto, contador pode promover
    'sem_historico',      # excluído — < FREQ_MIN aparições
    'auto_transferencia', # excluído Fase 1
    'renda_circular',     # excluído Fase 5 / 5b — fluxo circular (recebe e devolve)
    'renda_duplicada',    # excluído manual — lançamento duplicado no extrato
    'ignorar',            # excluído manual pelo contador
]

# Mapeamento override-flag → motivo de exclusão usado internamente
_OV_FLAG_TO_MOTIVO: Dict[str, str] = {
    'ruido'             : 'variancia',
    'sem_historico'     : 'sem_historico',
    'auto_transferencia': 'auto_transferencia',
    'renda_circular'    : 'circular_longitudinal_manual',
    'renda_duplicada'   : 'duplicado_manual',
}

# Mapeamento motivo → FlagGrupo para enriquecer excluidos
_MOTIVO_TO_FLAG: Dict[str, str] = {
    'auto_transferencia'          : 'auto_transferencia',
    'auto_investimento'           : 'auto_transferencia',
    'variancia'                   : 'ruido',
    'sem_historico'               : 'sem_historico',
    'circular'                    : 'renda_circular',
    'circular_longitudinal'       : 'renda_circular',
    'circular_longitudinal_manual': 'renda_circular',
    'duplicado_manual'            : 'renda_duplicada',
    'flag_usuario'                : 'ignorar',
}

# ── Constantes ────────────────────────────────────────────────────────────────

LEVENSHTEIN_THRESHOLD = 0.80
CIRCULAR_WINDOW_HOURS = 24
CIRCULAR_VALUE_TOL    = 0.01
PERCENTIL_PICO        = 90
MESES_BAIXA_CONFIANCA = 3

FREQ_MIN_MESES       = 3   # padrão
FREQ_MIN_CONSERVADOR = 7   # modo conservador

CIRCULAR_LONGITUDINAL_FREQ = 0.30  # >= 30 % dos meses → circular bilateral

GRUPO_IGNORAR        = 'ignorar'
GRUPO_FORCAR_ESTAVEL = 'forcar_estavel'

_RE_AUTO_INVESTIMENTO = re.compile(
    r'^(resgate\s+rdb|resgate\s+cdb|rendimento\s+l[ií]quido|'
    r'rend(?:imento)?\s+liq|aplica(?:cao|ção)\s+(?:rdb|cdb)|'
    r'resg(?:ate)?\s+aplic|juros\s+credit)',
    re.I,
)

TOKENS_GENERICOS = {
    'pix', 'ted', 'doc', 'transferencia', 'transferência',
    'recebido', 'enviado', 'pelo', 'para', 'de', 'via',
    'pagamento', 'credito', 'crédito', 'debito', 'débito',
    'banco', 'cred', 'deb', 'ag', 'cc', 'conta', 'cpf', 'cnpj',
}

_RE_INST_FINANCEIRA = re.compile(
    r'\b('
    r'itau|itaú|unibanco|bradesco|santander|inter|banco\s*inter|'
    r'c6|bco\s*c6|nubank|nu\s*pagamentos|pagbank|pagseguro|'
    r'mercado\s*pago|mercadopago|caixa|cef|sicoob|sicredi|'
    r'btg|xp|modal|safra|original|stone|picpay|next|neon|'
    r'will\s*bank|bs2|iti|agibank|banrisul|banpara|banese|'
    r'citibank|jp\s*morgan|hsbc|bnb|bndes|brb|daycoval|'
    r'digimais|dmcard|fator|fibra|gmac|guanabara|indusval|'
    r'jbs|luso\s*brasileiro|máxima|nk5|omni|pan|paraná\s*banco|'
    r'pine|rendimento|rodobens|senff|sofisa|triangulo|unicred|'
    r'votorantim|western\s*union|zip|zero|stark\s*bank|'
    r'delcred|pagueveloz|sants|nel3|wudi|boa\s*compra|'
    r'bco|s\.?\s*a\.?|ltda|ip\b|instituicao|instituição|pagamento|s/a'
    r')\b',
    re.I | re.U,
)

_RE_AGENCIA_CONTA = re.compile(
    r'agência\s*:?\s*[\d\-]+|agencia\s*:?\s*[\d\-]+|conta\s*:?\s*[\w\-]+',
    re.I,
)

_RE_TOKENS_GENERICOS = re.compile(
    r'\b(' + '|'.join(re.escape(t) for t in sorted(TOKENS_GENERICOS, key=len, reverse=True)) + r')\b',
    re.I | re.U,
)

MESES_PT = {
    'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
    'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12,
    'janeiro': 1, 'fevereiro': 2, 'março': 3, 'marco': 3, 'abril': 4,
    'maio': 5, 'junho': 6, 'julho': 7, 'agosto': 8, 'setembro': 9,
    'outubro': 10, 'novembro': 11, 'dezembro': 12,
}

_RE_DDMMYYYY        = re.compile(r'^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$')
_RE_DDMMYYYY_HIFEN  = re.compile(r'^(\d{1,2})-(\d{1,2})-(\d{4})$')
_RE_DDEMESYYYY = re.compile(
    r'^(\d{1,2})\s+(?:de\s+)?([A-Za-zÀ-ɏ]+)(?:\s+de)?\s+(\d{4})$', re.I
)
_RE_YYYYMMDD   = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')

_RE_CPF          = re.compile(r'\d{3}[\.\-]?\d{3}[\.\-]?\d{3}[\.\-]?\d{2}')
_RE_CNPJ         = re.compile(r'\d{2}[\.\-]?\d{3}[\.\-]?\d{3}[\.\-]?\d{4}[\.\-]?\d{2}')
_RE_CPF_MASK     = re.compile(r'[•\*x]{2,3}[\.\-]?\d{3}[\.\-]?\d{3}[\.\-][•\*x]{2,}', re.I)
_RE_NUMERO_CONTA = re.compile(r'[Cc]onta\s*:?\s*(?:\w+\s+)?([\d][\d\-]+[\d])', re.I)
_STOPWORDS_NOME  = {'de', 'da', 'do', 'das', 'dos', 'e', 'di', 'del', 'van', 'von'}


# ── Helpers de data ───────────────────────────────────────────────────────────

@lru_cache(maxsize=1024)
def parse_date(date_str: Optional[str]) -> Optional[str]:
    """Retorna 'YYYY-MM' ou None."""
    if not date_str:
        return None
    s = date_str.strip()
    m0 = _RE_YYYYMMDD.match(s)
    if m0:
        try:
            datetime(int(m0.group(1)), int(m0.group(2)), int(m0.group(3)))
            return f"{m0.group(1)}-{m0.group(2)}"
        except ValueError:
            return None
    m = _RE_DDMMYYYY.match(s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        if y is None:
            y = datetime.now().year
        elif len(y) == 2:
            y = 2000 + int(y)
        else:
            y = int(y)
        try:
            dt = datetime(y, mo, d)
            return f"{dt.year:04d}-{dt.month:02d}"
        except ValueError:
            return None
    mh = _RE_DDMMYYYY_HIFEN.match(s)
    if mh:
        d, mo, y = int(mh.group(1)), int(mh.group(2)), int(mh.group(3))
        try:
            dt = datetime(y, mo, d)
            return f"{dt.year:04d}-{dt.month:02d}"
        except ValueError:
            return None
    m2 = _RE_DDEMESYYYY.match(s)
    if m2:
        mes_str = m2.group(2).lower()
        mo = MESES_PT.get(mes_str[:3]) or MESES_PT.get(mes_str)
        y  = int(m2.group(3))
        if mo:
            return f"{y:04d}-{mo:02d}"
    return None


@lru_cache(maxsize=1024)
def parse_datetime(date_str: Optional[str]) -> Optional[datetime]:
    if not date_str:
        return None
    s = date_str.strip()
    m0 = _RE_YYYYMMDD.match(s)
    if m0:
        try: return datetime(int(m0.group(1)), int(m0.group(2)), int(m0.group(3)))
        except ValueError: return None
    m = _RE_DDMMYYYY.match(s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        if y is None: y = datetime.now().year
        elif len(y) == 2: y = 2000 + int(y)
        else: y = int(y)
        try: return datetime(y, mo, d)
        except ValueError: return None
    mh = _RE_DDMMYYYY_HIFEN.match(s)
    if mh:
        d, mo, y = int(mh.group(1)), int(mh.group(2)), int(mh.group(3))
        try: return datetime(y, mo, d)
        except ValueError: return None
    m2 = _RE_DDEMESYYYY.match(s)
    if m2:
        mes_str = m2.group(2).lower()
        mo = MESES_PT.get(mes_str[:3]) or MESES_PT.get(mes_str)
        y  = int(m2.group(3))
        if mo:
            try: return datetime(y, mo, int(m2.group(1)))
            except ValueError: return None
    return None


# ── Normalização ──────────────────────────────────────────────────────────────

@lru_cache(maxsize=4096)
def normalizar(texto: str) -> str:
    t = texto.lower().strip()
    t = _RE_CNPJ.sub(' ', t)
    t = _RE_CPF_MASK.sub(' ', t)
    t = _RE_CPF.sub(' ', t)
    t = _RE_AGENCIA_CONTA.sub(' ', t)
    t = _RE_INST_FINANCEIRA.sub(' ', t)
    t = _RE_TOKENS_GENERICOS.sub(' ', t)
    t = re.sub(r'[^\w\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def _fold_accents(s: str) -> str:
    return ''.join(
        c for c in unicodedata.normalize('NFD', s)
        if unicodedata.category(c) != 'Mn'
    )


def _tokenizar_nome(nome: str) -> List[str]:
    t = _fold_accents(nome.lower().strip())
    t = re.sub(r'[^\w\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return [tok for tok in t.split() if tok not in _STOPWORDS_NOME and len(tok) >= 1]


def _sobrenome_congruente(tok_origem: str, sob_filtro: str) -> bool:
    return sob_filtro.startswith(tok_origem)


def match_titular(nome_filtro: str, origem_norm: str) -> Tuple[bool, str]:
    """
    Verifica se origem_norm pertence ao mesmo titular que nome_filtro.

    Estratégia em duas camadas:
      1. Ancora estrutural: primeiro nome + pelo menos um sobrenome consecutivo.
      2. [FIX-1] Fallback fuzzy no nome completo normalizado.
    """
    tokens_filtro = _tokenizar_nome(nome_filtro)
    tokens_origem = _tokenizar_nome(origem_norm)

    if not tokens_filtro or not tokens_origem:
        return False, "tokens insuficientes"

    primeiro_nome  = tokens_filtro[0]
    sobrenomes_ref = tokens_filtro[1:]

    if primeiro_nome in tokens_origem:
        if not sobrenomes_ref:
            return True, "match por primeiro nome"
        anchor_idx  = tokens_origem.index(primeiro_nome)
        tokens_apos = tokens_origem[anchor_idx + 1:]
        confirmados = []
        for tok in tokens_apos:
            if any(_sobrenome_congruente(tok, sob) for sob in sobrenomes_ref):
                confirmados.append(tok)
            else:
                break
        if confirmados:
            return True, f"confirmados={confirmados}"

    norm_filtro = normalizar(nome_filtro)
    if norm_filtro and origem_norm:
        score = _rfuzz.ratio(origem_norm, norm_filtro) / 100.0
        if score >= 0.85:
            return True, f"fuzzy_nome_completo score={score:.2f}"

        sobs_filtro = sobrenomes_ref
        if len(sobs_filtro) >= 2:
            sobs_origem = tokens_origem[1:] if len(tokens_origem) > 1 else []
            matches_sob = sum(
                1 for s in sobs_filtro
                if any(_sobrenome_congruente(so, s) for so in sobs_origem)
            )
            if matches_sob >= 2:
                return True, f"sobrenomes_compartilhados={matches_sob}"

    return False, "sem match"


def extrai_numero_conta(origem: str) -> Optional[str]:
    m = _RE_NUMERO_CONTA.search(origem)
    return m.group(1).strip() if m else None


def is_conta_propria(
    origem: str,
    numeros_contas_proprias: List[str],
    filtros_norm: List[str],
) -> Tuple[bool, Optional[str]]:
    if _RE_AUTO_INVESTIMENTO.match(origem.strip()):
        return True, 'auto_investimento'
    num = extrai_numero_conta(origem)
    if num and num in numeros_contas_proprias:
        return True, 'auto_transferencia'
    norm_origem = normalizar(origem)
    for f_original in filtros_norm:
        if match_titular(f_original, norm_origem)[0]:
            return True, 'auto_transferencia'
    for f in filtros_norm:
        fn = normalizar(f)
        if not fn:
            continue
        if _rfuzz.ratio(norm_origem, fn) / 100.0 >= LEVENSHTEIN_THRESHOLD:
            return True, 'auto_transferencia'
    return False, None


# ── Extração de números de conta do titular ───────────────────────────────────

def extrair_numeros_conta_titular(
    lancamentos: List[Dict],
    titulares: List[str],
) -> List[str]:
    if not titulares:
        return []
    partes_por_titular = []
    for nome in titulares:
        partes = [p for p in nome.lower().split() if len(p) >= 3]
        if len(partes) >= 2:
            partes_por_titular.append(partes)
    if not partes_por_titular:
        return []
    numeros: set = set()
    for lanc in lancamentos:
        origem = (lanc.get('origem_destino') or '').lower()
        if not origem:
            continue
        tem_titular = any(
            all(p in origem for p in partes)
            for partes in partes_por_titular
        )
        if tem_titular:
            m = _RE_NUMERO_CONTA.search(lanc.get('origem_destino') or '')
            if m:
                numeros.add(m.group(1).strip())
    return list(numeros)


# ── Agrupamento ───────────────────────────────────────────────────────────────

def agrupar_por_pagador(entradas: List[Dict], threshold: float = LEVENSHTEIN_THRESHOLD) -> Dict[str, List[Dict]]:
    grupos: Dict[str, List[Dict]] = {}
    chaves: List[str] = []
    cutoff = threshold * 100

    for lanc in entradas:
        origem = lanc.get('origem_destino') or lanc.get('tipo_transacao') or 'desconhecido'
        norm   = normalizar(origem) or 'desconhecido'

        melhor_chave = None
        if chaves:
            resultado = _rprocess.extractOne(
                norm, chaves,
                scorer=_rfuzz.ratio,
                score_cutoff=cutoff,
            )
            if resultado:
                melhor_chave = resultado[0]

        if melhor_chave:
            grupos[melhor_chave].append({**lanc, '_norm': norm})
        else:
            grupos[norm] = [{**lanc, '_norm': norm}]
            chaves.append(norm)

    return grupos


# ── Variância longitudinal ────────────────────────────────────────────────────

def coeficiente_variacao(valores: List[float]) -> float:
    if not valores: return 0.0
    media = statistics.mean(valores)
    if media == 0: return 0.0
    return statistics.pstdev(valores) / media


def classificar_por_variancia(
    historico: Dict[str, Dict[str, float]],
    todos_meses: List[str],
    freq_min: int = FREQ_MIN_MESES,
) -> Dict[str, Dict]:
    resultado = {}

    for chave in historico:
        for mes in todos_meses:
            historico[chave].setdefault(mes, 0.0)

    grupo_frequente = {}
    grupo_raro      = {}
    for chave, h in historico.items():
        aparicoes = sum(1 for m in todos_meses if h[m] > 0)
        if aparicoes >= freq_min:
            grupo_frequente[chave] = h
        else:
            grupo_raro[chave] = h

    for chave, h in grupo_raro.items():
        valores = [h[m] for m in todos_meses]
        seis    = statistics.median(valores)
        resultado[chave] = {
            'classificacao'  : 'sem_historico',
            'cv'             : round(coeficiente_variacao(valores), 4),
            'threshold_cv'   : None,
            'valores_por_mes': dict(sorted(h.items())),
            'seis'           : round(seis, 2),
            'dez'            : 0.0,
            'aparicoes'      : sum(1 for m in todos_meses if h[m] > 0),
        }

    if not grupo_frequente:
        return resultado

    cvs = {
        chave: coeficiente_variacao([h[m] for m in todos_meses])
        for chave, h in grupo_frequente.items()
    }
    if not cvs:
        return resultado

    threshold_cv = statistics.median(cvs.values())
    todos_iguais = len(set(round(v, 6) for v in cvs.values())) == 1

    for chave, cv in cvs.items():
        h       = grupo_frequente[chave]
        valores = [h[m] for m in todos_meses]
        valores_ordenados = sorted(valores)
        seis    = statistics.median(valores)
        n       = len(valores_ordenados)
        idx     = (PERCENTIL_PICO / 100) * (n - 1)
        idx_baixo = int(idx)
        idx_alto  = min(idx_baixo + 1, n - 1)
        fracao    = idx - idx_baixo
        dez       = valores_ordenados[idx_baixo] * (1 - fracao) + valores_ordenados[idx_alto] * fracao
        estavel   = (cv <= threshold_cv) if todos_iguais else (cv < threshold_cv)
        resultado[chave] = {
            'classificacao'  : 'estavel' if estavel else 'ruido',
            'cv'             : round(cv, 4),
            'threshold_cv'   : round(threshold_cv, 4),
            'valores_por_mes': dict(sorted(h.items())),
            'seis'           : round(seis, 2),
            'dez'            : round(dez, 2),
            'aparicoes'      : sum(1 for m in todos_meses if h[m] > 0),
        }

    return resultado


# ── Detecção de fluxo circular ────────────────────────────────────────────────

def detectar_circulares(entradas, saidas, numeros_proprios, filtros_norm, janela_h=CIRCULAR_WINDOW_HOURS):
    saidas_parsed = []
    for s in saidas:
        origem_saida = s.get('origem_destino') or ''
        proprio, _   = is_conta_propria(origem_saida, numeros_proprios, filtros_norm)
        if proprio: continue
        dt  = parse_datetime(s.get('date'))
        val = abs(s.get('saida') or 0.0)
        if dt and val > 0:
            saidas_parsed.append((dt, val))
    circulares = set()
    for i, e in enumerate(entradas):
        dt_e  = parse_datetime(e.get('date'))
        val_e = e.get('entrada') or 0.0
        if not dt_e or val_e <= 0: continue
        for dt_s, val_s in saidas_parsed:
            diff_h = abs((dt_e - dt_s).total_seconds() / 3600)
            if diff_h <= janela_h:
                ratio = abs(val_e - val_s) / max(val_e, 0.01)
                if ratio <= CIRCULAR_VALUE_TOL:
                    circulares.add(i)
                    break
    return circulares


# ── Motor principal ───────────────────────────────────────────────────────────

def apurar(lancamentos_raw: List[Dict], config: "ConfigApuracao") -> Dict:
    """
    Aplica as 6 fases de apuração sobre os lançamentos.

    Modos disponíveis via config.modo — diferença APENAS na Fase 6 (renda_base)
    e no FREQ_MIN da Fase 4:

      'padrao'      → FREQ_MIN=3, renda_base = mediana c/ zeros  (seis)
      'conservador' → FREQ_MIN=7, renda_base = mediana c/ zeros  (seis)
      'moderado'    → FREQ_MIN=3, renda_base = média condicional (meses com valor > 0)
      'liberal'     → FREQ_MIN=3, renda_base = percentil 90      (dez)
    """
    # Frequência mínima: só conservador eleva o piso
    freq_min = FREQ_MIN_CONSERVADOR if config.modo == 'conservador' else FREQ_MIN_MESES

    # Overrides de grupo definidos pelo contador (grupo_id → FlagGrupo)
    grupos_override: Dict[str, str] = config.grupos_override or {}

    filtros_norm     = [f for f in config.contas_proprias if f.strip()]

    numeros_dinamicos = extrair_numeros_conta_titular(lancamentos_raw, filtros_norm)
    numeros_proprios  = list(set(
        [n.strip() for n in config.numeros_contas_proprias if n.strip()]
        + numeros_dinamicos
    ))

    lancamentos_ignorados:   List[Dict] = []
    lancamentos_processaveis: List[Dict] = []
    forcados_estaveis: set = set()

    for lanc in lancamentos_raw:
        incluido = lanc.get('incluido', True)
        grupo    = (lanc.get('grupo') or lanc.get('flag') or '').strip().lower()

        if not incluido or grupo == GRUPO_IGNORAR:
            lancamentos_ignorados.append(lanc)
        else:
            if grupo == GRUPO_FORCAR_ESTAVEL:
                forcados_estaveis.add(lanc.get('id') or id(lanc))
            lancamentos_processaveis.append(lanc)

    excluidos: List[Dict] = []

    for lanc in lancamentos_ignorados:
        origem = lanc.get('origem_destino') or lanc.get('tipo_transacao') or 'Desconhecido'
        excluidos.append({
            'grupo_id': normalizar(origem),
            'pagador' : origem,
            'valor'   : lanc.get('entrada', 0) or lanc.get('saida', 0),
            'data'    : lanc.get('date'),
            'motivo'  : 'flag_usuario',
            'flag'    : 'ignorar',
        })

    lancamentos_processaveis = [
        l for l in lancamentos_processaveis
        if l.get('origem_destino') or l.get('tipo_transacao')
    ]

    entradas_raw = [l for l in lancamentos_processaveis if (l.get('entrada') or 0.0) > 0]
    saidas_raw   = [l for l in lancamentos_processaveis if (l.get('saida')   or 0.0) < 0]

    todos_meses = sorted({
        parse_date(l.get('date'))
        for l in lancamentos_processaveis
        if parse_date(l.get('date'))
    })
    meses_analisados = len(todos_meses)

    # FASE 1 — Filtro de contas próprias
    entradas_validas: List[Dict] = []
    for lanc in entradas_raw:
        origem      = lanc.get('origem_destino') or lanc.get('tipo_transacao') or 'Desconhecido'
        norm_origem = normalizar(origem)

        # Contador pode promover grupo auto_transferencia → estavel
        if grupos_override.get(norm_origem) == 'estavel':
            entradas_validas.append(lanc)
            continue

        proprio, motivo = is_conta_propria(origem, numeros_proprios, filtros_norm)
        if proprio:
            excluidos.append({
                'grupo_id': norm_origem,
                'pagador' : origem,
                'valor'   : lanc.get('entrada', 0),
                'data'    : lanc.get('date'),
                'motivo'  : motivo,
                'flag'    : 'auto_transferencia',
            })
        else:
            entradas_validas.append(lanc)

    # FASE 2 — Agrupamento
    grupos = agrupar_por_pagador(entradas_validas, config.threshold_levenshtein)

    forcados_por_chave: set = set()
    for chave, lancs in grupos.items():
        for lanc in lancs:
            lanc_key = lanc.get('id') or id(lanc)
            if lanc_key in forcados_estaveis or any(
                (orig.get('id') or id(orig)) in forcados_estaveis
                for orig in lancamentos_raw
                if orig.get('origem_destino') == lanc.get('origem_destino')
                   and orig.get('date') == lanc.get('date')
                   and orig.get('entrada') == lanc.get('entrada')
            ):
                forcados_por_chave.add(chave)

    # FASE 3 — Granularização mensal
    historico:       Dict[str, Dict[str, float]] = {}
    label_por_grupo: Dict[str, str]              = {}
    for chave, lancs in grupos.items():
        soma_por_mes: Dict[str, float] = defaultdict(float)
        for l in lancs:
            mes = parse_date(l.get('date'))
            if mes: soma_por_mes[mes] += l.get('entrada', 0.0)
        historico[chave] = {mes: soma_por_mes.get(mes, 0.0) for mes in todos_meses}
        origens = [l.get('origem_destino') or l.get('tipo_transacao') or chave for l in lancs]
        label_por_grupo[chave] = max(set(origens), key=origens.count)

    # ── Overrides de grupo do contador (aplicado após agrupamento + labels) ──────
    # grupos_forcar_excluir: grupos a excluir mesmo que o algoritmo os estabilize
    grupos_forcar_excluir: Dict[str, str] = {}

    for chave, flag_ov in list(grupos_override.items()):
        if chave not in historico:
            continue
        if flag_ov == 'ignorar':
            # Remove do pipeline; adiciona a excluidos imediatamente
            for lanc in grupos.pop(chave, []):
                excluidos.append({
                    'grupo_id': chave,
                    'pagador' : label_por_grupo.get(chave, chave),
                    'valor'   : lanc.get('entrada', 0),
                    'data'    : lanc.get('date'),
                    'motivo'  : 'flag_usuario',
                    'flag'    : 'ignorar',
                })
            del historico[chave]
            label_por_grupo.pop(chave, None)
        elif flag_ov == 'estavel':
            # Força estável na Fase 4
            forcados_por_chave.add(chave)
        elif flag_ov in _OV_FLAG_TO_MOTIVO:
            # Remove eventual forçado anterior; guardamos para forçar exclusão pós-Fase 4
            forcados_por_chave.discard(chave)
            grupos_forcar_excluir[chave] = flag_ov

    # FASE 4 — Variância (freq_min varia conforme modo)
    historico_normal   = {k: v for k, v in historico.items() if k not in forcados_por_chave}
    historico_forcados = {k: v for k, v in historico.items() if k in forcados_por_chave}

    classificados = classificar_por_variancia(historico_normal, todos_meses, freq_min=freq_min)

    for chave, vals in historico_forcados.items():
        valores           = [vals.get(m, 0.0) for m in todos_meses]
        valores_ordenados = sorted(valores)
        seis              = statistics.median(valores) if valores else 0.0
        n                 = len(valores_ordenados)
        idx               = (PERCENTIL_PICO / 100) * (n - 1)
        idx_b             = int(idx)
        idx_a             = min(idx_b + 1, n - 1)
        fracao            = idx - idx_b
        dez               = (
            valores_ordenados[idx_b] * (1 - fracao) + valores_ordenados[idx_a] * fracao
            if n > 0 else 0.0
        )
        cv = coeficiente_variacao(valores)
        aparicoes = sum(1 for m in todos_meses if vals.get(m, 0.0) > 0)
        classificados[chave] = {
            'classificacao'  : 'forcado',
            'cv'             : round(cv, 4),
            'threshold_cv'   : None,
            'valores_por_mes': dict(sorted(vals.items())),
            'seis'           : round(seis, 2),
            'dez'            : round(dez, 2),
            'aparicoes'      : aparicoes,
        }

    estaveis = {
        k: v for k, v in classificados.items()
        if v['classificacao'] in ('estavel', 'forcado')
    }
    ruidosos = {
        k: v for k, v in classificados.items()
        if v['classificacao'] == 'ruido'
    }
    sem_historico = {
        k: v for k, v in classificados.items()
        if v['classificacao'] == 'sem_historico'
    }

    # Aplica forced-exclusions do contador: remove dos dicts de classificação
    # e adiciona a excluidos com o motivo correspondente ao flag escolhido.
    for chave in list(grupos_forcar_excluir):
        estaveis.pop(chave, None)
        ruidosos.pop(chave, None)
        sem_historico.pop(chave, None)

    for chave, flag_ov in grupos_forcar_excluir.items():
        motivo = _OV_FLAG_TO_MOTIVO[flag_ov]
        info   = classificados.get(chave, {})
        for lanc in grupos.get(chave, []):
            entry: Dict = {
                'grupo_id' : chave,
                'pagador'  : label_por_grupo.get(chave, chave),
                'valor'    : lanc.get('entrada', 0),
                'data'     : lanc.get('date'),
                'motivo'   : motivo,
                'flag'     : flag_ov,
                'aparicoes': info.get('aparicoes', 0),
            }
            if flag_ov == 'ruido':
                entry['cv'] = info.get('cv')
            excluidos.append(entry)

    for chave, info in ruidosos.items():
        for lanc in grupos.get(chave, []):
            excluidos.append({
                'grupo_id': chave,
                'pagador' : label_por_grupo.get(chave, chave),
                'valor'   : lanc.get('entrada', 0),
                'data'    : lanc.get('date'),
                'motivo'  : 'variancia',
                'cv'      : info['cv'],
                'aparicoes': info.get('aparicoes', 0),
                'flag'    : 'ruido',
            })

    for chave, info in sem_historico.items():
        for lanc in grupos.get(chave, []):
            excluidos.append({
                'grupo_id' : chave,
                'pagador'  : label_por_grupo.get(chave, chave),
                'valor'    : lanc.get('entrada', 0),
                'data'     : lanc.get('date'),
                'motivo'   : 'sem_historico',
                'aparicoes': info['aparicoes'],
                'flag'     : 'sem_historico',
            })

    # FASE 5 — Circular
    entradas_estaveis  = [l for chave in estaveis for l in grupos.get(chave, [])]
    idx_circulares_rel = detectar_circulares(
        entradas_estaveis, saidas_raw, numeros_proprios, filtros_norm,
        config.janela_circular_horas,
    )

    circulares_por_chave: Dict[str, set] = {}
    cursor = 0
    for chave in estaveis:
        lancs_grupo = grupos.get(chave, [])
        for j, lanc in enumerate(lancs_grupo):
            if cursor + j in idx_circulares_rel:
                circulares_por_chave.setdefault(chave, set()).add(j)
                excluidos.append({
                    'grupo_id': chave,
                    'pagador' : label_por_grupo.get(chave, chave),
                    'valor'   : lanc.get('entrada', 0),
                    'data'    : lanc.get('date'),
                    'motivo'  : 'circular',
                    'flag'    : 'renda_circular',
                })
        cursor += len(lancs_grupo)

    # FASE 5b — Detecção circular bilateral longitudinal
    # Agrupa saídas (excluindo contas próprias) por contraparte normalizada → meses
    saidas_por_contraparte: Dict[str, set] = {}
    for s in saidas_raw:
        proprio, _ = is_conta_propria(
            s.get('origem_destino') or '', numeros_proprios, filtros_norm
        )
        if proprio:
            continue
        mes_s = parse_date(s.get('date'))
        if not mes_s:
            continue
        chave_s = normalizar(s.get('origem_destino') or s.get('tipo_transacao') or '')
        if not chave_s:
            continue
        saidas_por_contraparte.setdefault(chave_s, set()).add(mes_s)

    # Cruza com grupos de entrada estáveis: co-ocorrência mensal >= 30 % dos meses
    # de aparição da entrada (denominador = meses_entrada, não n_meses total).
    # Match entre chave_e e chave_s via fuzzy com o mesmo threshold do agrupamento.
    chaves_saida = list(saidas_por_contraparte.keys())
    cutoff_long  = config.threshold_levenshtein * 100

    circular_longitudinal: set = set()
    if todos_meses:
        for chave_e in list(estaveis.keys()):
            if chave_e in circular_longitudinal:
                continue
            meses_entrada = {
                mes for mes in todos_meses
                if historico.get(chave_e, {}).get(mes, 0.0) > 0
            }
            if not meses_entrada:
                continue
            # Encontra todas as chaves de saída que fazem match fuzzy com chave_e
            matches_saida = _rprocess.extract(
                chave_e, chaves_saida,
                scorer=_rfuzz.ratio,
                score_cutoff=cutoff_long,
            ) if chaves_saida else []
            for match_chave_s, _score, _idx in matches_saida:
                meses_saida    = saidas_por_contraparte[match_chave_s]
                co_ocorrencias = len(meses_entrada & meses_saida)
                if co_ocorrencias / len(meses_entrada) >= CIRCULAR_LONGITUDINAL_FREQ:
                    circular_longitudinal.add(chave_e)
                    for lanc in grupos.get(chave_e, []):
                        excluidos.append({
                            'grupo_id'         : chave_e,
                            'pagador'          : label_por_grupo.get(chave_e, chave_e),
                            'valor'            : lanc.get('entrada', 0),
                            'data'             : lanc.get('date'),
                            'motivo'           : 'circular_longitudinal',
                            'contraparte_saida': match_chave_s,
                            'flag'             : 'renda_circular',
                        })
                    break  # um match já basta para excluir o grupo

    # Marcação manual pelo operador (casos sem saídas no extrato)
    manuais_norm = [normalizar(m) for m in config.circulares_longitudinais_manual if m.strip()]
    if manuais_norm:
        cutoff_manual = config.threshold_levenshtein * 100
        for chave_e in list(estaveis.keys()):
            if chave_e in circular_longitudinal:
                continue
            match = _rprocess.extractOne(
                chave_e, manuais_norm,
                scorer=_rfuzz.ratio,
                score_cutoff=cutoff_manual,
            )
            if match:
                circular_longitudinal.add(chave_e)
                for lanc in grupos.get(chave_e, []):
                    excluidos.append({
                        'grupo_id': chave_e,
                        'pagador' : label_por_grupo.get(chave_e, chave_e),
                        'valor'   : lanc.get('entrada', 0),
                        'data'    : lanc.get('date'),
                        'motivo'  : 'circular_longitudinal_manual',
                        'flag'    : 'renda_circular',
                    })

    # Remove grupos longitudinalmente circulares antes da Fase 6
    estaveis = {k: v for k, v in estaveis.items() if k not in circular_longitudinal}

    # FASE 6 — Composição
    estaveis_finais = {}
    for chave, info in estaveis.items():
        circ_idx    = circulares_por_chave.get(chave, set())
        lancs_grupo = grupos.get(chave, [])

        soma_sem_circ: Dict[str, float] = defaultdict(float)
        for j, lanc in enumerate(lancs_grupo):
            if j not in circ_idx:
                mes = parse_date(lanc.get('date'))
                if mes:
                    soma_sem_circ[mes] += lanc.get('entrada', 0.0)

        novo_historico  = {mes: soma_sem_circ.get(mes, 0.0) for mes in todos_meses}
        valores         = [novo_historico[m] for m in todos_meses]
        media           = statistics.mean(valores)
        cv_novo         = (statistics.pstdev(valores) / media) if media > 0 else 0.0
        valores_ord     = sorted(valores)
        n               = len(valores_ord)
        seis            = statistics.median(valores)
        idx_p           = (PERCENTIL_PICO / 100) * (n - 1)
        idx_b           = int(idx_p)
        idx_a           = min(idx_b + 1, n - 1)
        dez             = valores_ord[idx_b] * (1 - (idx_p - idx_b)) + valores_ord[idx_a] * (idx_p - idx_b)

        estaveis_finais[chave] = {
            **info,
            'cv'             : round(cv_novo, 4),
            'valores_por_mes': dict(sorted(novo_historico.items())),
            'seis'           : round(seis, 2),
            'dez'            : round(dez, 2),
        }

    composicao:     List[Dict] = []
    inconclusivos:  List[Dict] = []
    renda_total:    float      = 0.0
    totais_por_mes: Dict[str, float] = {mes: 0.0 for mes in todos_meses}

    for chave, info in estaveis_finais.items():
        # Fase 6 — medida de renda_base varia conforme modo
        if config.modo == 'moderado':
            vals_pos   = [v for v in info['valores_por_mes'].values() if v > 0]
            renda_base = round(statistics.mean(vals_pos), 2) if vals_pos else 0.0
        elif config.modo == 'liberal':
            renda_base = info['dez']
        else:  # padrao | conservador
            renda_base = info['seis']

        flag_ef = 'estavel'  # composicao e inconclusivos sempre têm flag estavel

        entrada = {
            'grupo_id'       : chave,
            'pagador'        : label_por_grupo.get(chave, chave),
            'renda_base'     : renda_base,
            'teto_organico'  : info['dez'],
            'cv'             : info['cv'],
            'classificacao'  : info['classificacao'],
            'aparicoes'      : info.get('aparicoes', 0),
            'valores_por_mes': info['valores_por_mes'],
            'flag'           : flag_ef,
        }

        # totais_por_mes reflete o fluxo real de entradas estáveis no período,
        # independente de renda_base — inclui composicao e inconclusivos.
        for mes, valor in info['valores_por_mes'].items():
            if mes in totais_por_mes:
                totais_por_mes[mes] = round(totais_por_mes[mes] + valor, 2)

        if renda_base == 0.0:
            # Grupo estável mas mediana zero (aparece em < 50 % dos meses):
            # não contribui para a renda apurada, vai para seção inconclusiva.
            inconclusivos.append({**entrada, 'inconclusivo': True})
        else:
            renda_total += renda_base
            composicao.append(entrada)

    composicao.sort(key=lambda c: c['renda_base'], reverse=True)
    inconclusivos.sort(key=lambda c: c['aparicoes'], reverse=True)

    return {
        'renda_apurada_mensal': round(renda_total, 2),
        'meses_analisados'    : meses_analisados,
        'baixa_confianca'     : meses_analisados < MESES_BAIXA_CONFIANCA,
        'composicao'          : composicao,
        'inconclusivos'       : inconclusivos,
        'totais_por_mes'      : dict(sorted(totais_por_mes.items())),
        'excluidos'           : excluidos,
    }


# ── Montagem de laudo ─────────────────────────────────────────────────────────

def montar_laudo(
    resultado: Dict,
    titular: str,
    periodo_inicio: str,
    periodo_fim: str,
) -> Dict:
    """
    Transforma o retorno bruto de apurar() numa estrutura completa pronta
    para renderização, calculando todos os campos derivados.
    """
    composicao       = resultado.get('composicao', [])
    inconclusivos    = resultado.get('inconclusivos', [])
    excluidos        = resultado.get('excluidos', [])
    totais_por_mes   = resultado.get('totais_por_mes', {})
    renda_apurada    = resultado.get('renda_apurada_mensal', 0.0)
    meses_analisados = resultado.get('meses_analisados', 0)

    # ── Cabeçalho ──────────────────────────────────────────────────────────────
    cabecalho = {
        'titular'         : titular,
        'periodo'         : f"{periodo_inicio} – {periodo_fim}",
        'meses_analisados': meses_analisados,
        'baixa_confianca' : resultado.get('baixa_confianca', False),
        'data_geracao'    : datetime.now().strftime('%d/%m/%Y'),
    }

    # ── Resumo ─────────────────────────────────────────────────────────────────
    if totais_por_mes:
        mes_mais_fraco  = min(totais_por_mes, key=totais_por_mes.get)
        mes_mais_forte  = max(totais_por_mes, key=totais_por_mes.get)
        amplitude_mensal = round(
            max(totais_por_mes.values()) - min(totais_por_mes.values()), 2
        )
    else:
        mes_mais_fraco   = None
        mes_mais_forte   = None
        amplitude_mensal = 0.0

    if composicao and renda_apurada:
        concentracao_principal = round(composicao[0]['renda_base'] / renda_apurada * 100, 1)
    else:
        concentracao_principal = 0.0

    resumo = {
        'renda_apurada_mensal'   : renda_apurada,
        'total_fontes'           : len(composicao),
        'concentracao_principal' : concentracao_principal,
        'mes_mais_fraco'         : mes_mais_fraco,
        'mes_mais_forte'         : mes_mais_forte,
        'amplitude_mensal'       : amplitude_mensal,
    }

    # ── Fontes enriquecidas ────────────────────────────────────────────────────
    fontes = []
    for fonte in composicao:
        valores_pos = [v for v in fonte.get('valores_por_mes', {}).values() if v > 0]
        fontes.append({
            **fonte,
            'regularidade'    : f"{fonte.get('aparicoes', 0)}/{meses_analisados}",
            'participacao_pct': round(fonte['renda_base'] / renda_apurada * 100, 1)
                                if renda_apurada else 0.0,
            'cv_pct'          : round(fonte.get('cv', 0.0) * 100, 1),
            'faixa_mensal'    : {
                'min': min(valores_pos) if valores_pos else 0.0,
                'max': max(valores_pos) if valores_pos else 0.0,
            },
        })

    # ── Exclusões agregadas ────────────────────────────────────────────────────
    por_motivo: Dict[str, int] = defaultdict(int)
    for ex in excluidos:
        motivo = ex.get('motivo') or 'desconhecido'
        por_motivo[motivo] += 1

    exclusoes = {
        'por_motivo'     : dict(por_motivo),
        'total_excluidos': len(excluidos),
    }

    return {
        'cabecalho'     : cabecalho,
        'resumo'        : resumo,
        'fontes'        : fontes,
        'exclusoes'     : exclusoes,
        'totais_por_mes': totais_por_mes,
        'inconclusivos' : inconclusivos,
    }


# ── Schema de configuração ────────────────────────────────────────────────────

class ConfigApuracao(BaseModel):
    contas_proprias              : List[str]      = []
    numeros_contas_proprias      : List[str]      = []
    threshold_levenshtein        : float          = LEVENSHTEIN_THRESHOLD
    janela_circular_horas        : int            = CIRCULAR_WINDOW_HOURS
    modo                         : str            = 'padrao'   # 'padrao' | 'conservador' | 'moderado' | 'liberal'
    circulares_longitudinais_manual: List[str]    = []
    # Nomes normalizados (ou substrings fuzzy-match >= threshold) de pagadores
    # que o operador sabe serem circulares mas que não aparecem como saídas no
    # extrato (ex.: recebe de familiar que paga em espécie ou outro banco).
    # Esses grupos são excluídos antes da Fase 6, exatamente como os detectados
    # automaticamente pela Fase 5b.
    grupos_override              : Dict[str, str] = {}
    # Override de flag por grupo (chave normalizada → FlagGrupo).
    # Persistido por sessão. Aplicado em apurar() após o agrupamento.
    #   'estavel'            → força grupo como estável na Fase 4
    #   'ignorar'            → exclui grupo antes da Fase 4 (motivo flag_usuario)
    #   outros               → força exclusão do grupo mesmo que algoritmo o estabilize

