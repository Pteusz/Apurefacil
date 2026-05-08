"""
modules/sessao.py — Gestão de sessões de apuração.

Responsabilidades:
  - Criar sessão: scanner → classificação inicial → persiste versions[0]
  - Servir estado atual: lê versions[-1] de cada lançamento
  - Receber mutações: portão único, append-only em versions[]
  - Computar apuração on-demand: converte estado → chama apurar()
  - Não armazena nunca o resultado de apuração (sempre derivado)
"""
import asyncio
import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from modules.scanner_legacy import extract_all_pages
from modules.extrator_bancario import extract as extract_banco, extract_with_signature
from modules import autodetect
from modules.assinaturas import listar as listar_assinaturas
from modules.apuracao import (
    apurar,
    montar_laudo,
    ConfigApuracao,
    is_conta_propria,
    parse_date,
    parse_datetime,
    normalizar,
    agrupar_por_pagador,
    LEVENSHTEIN_THRESHOLD,
)
from rapidfuzz import fuzz as _rfuzz
from storage import ensure_user_dir, read_session, write_session, write_lock, list_user_sessions, session_exists, delete_session


# ── Exceções ──────────────────────────────────────────────────────────────────

class BancoConflito(Exception):
    """
    Levantada quando o banco declarado pelo usuário não produz lançamentos
    no PDF fornecido — indica conflito de assinatura/layout.
    """
    def __init__(self, file_idx: int, file_id: str, banco: str):
        self.file_idx = file_idx
        self.file_id  = file_id
        self.banco    = banco
        super().__init__(f"Assinatura do banco '{banco}' não reconhecida no arquivo {file_id}")


# ── IDs determinísticos (seção 12) ────────────────────────────────────────────

def _gerar_lanc_id(data: str, descricao: str, valor: float, counter: int = 0) -> str:
    """Hash dos campos imutáveis. Colisões tratadas com sufixo incremental."""
    raw = f"{data}|{(descricao or '')[:30]}|{valor}"
    if counter > 0:
        raw += f"|{counter}"
    return "lanc_" + hashlib.sha1(raw.encode()).hexdigest()[:8]


def _normalizar_data(raw_date: str) -> str:
    """Normaliza data para 'YYYY-MM-DD'. Retorna raw se falhar."""
    dt = parse_datetime(raw_date)
    if dt:
        return dt.strftime("%Y-%m-%d")
    return raw_date


# ── Classificação inicial ─────────────────────────────────────────────────────

def _classificar_lancamento(
    lanc: Dict,
    filtros_norm: List[str],
    numeros_proprios: List[str],
) -> Dict[str, Any]:
    """
    Classifica um lançamento bruto do scanner para o estado inicial (versions[0]).

    Regras:
      saída negativa → active: False, flag: despesa_recorrente
      entrada + conta própria → active: False, flag: auto_transferencia
      entrada válida → active: True, flag: renda_recorrente
      sem valor → active: False, flag: renda_recorrente
    """
    entrada = lanc.get("entrada") or 0.0
    saida   = lanc.get("saida")   or 0.0
    origem  = lanc.get("descricao") or lanc.get("origem_destino") or lanc.get("tipo_transacao") or ""
    data    = lanc.get("date") or ""
    # Campos nomeados pelo usuário (presentes apenas em assinaturas com campos definidos)
    campos  = lanc.get("campos") or None

    def _base(extra: Dict) -> Dict:
        result = {**extra, "comentario": None}
        if campos:
            result["campos"] = campos
        return result

    if saida < 0:
        return _base({
            "valor"    : round(saida, 2),
            "descricao": origem,
            "data"     : _normalizar_data(data),
            "active"   : False,
            "flag"     : "despesa_recorrente",
        })

    if entrada > 0:
        proprio, _ = is_conta_propria(origem, numeros_proprios, filtros_norm)
        if proprio:
            return _base({
                "valor"    : round(entrada, 2),
                "descricao": origem,
                "data"     : _normalizar_data(data),
                "active"   : False,
                "flag"     : "auto_transferencia",
            })
        return _base({
            "valor"    : round(entrada, 2),
            "descricao": origem,
            "data"     : _normalizar_data(data),
            "active"   : True,
            "flag"     : "renda_recorrente",
        })

    # Lançamento sem valor monetário (metadado escapou do filtro)
    return _base({
        "valor"    : 0.0,
        "descricao": origem,
        "data"     : _normalizar_data(data),
        "active"   : False,
        "flag"     : "renda_recorrente",
    })


# ── Construção do JSON de sessão ──────────────────────────────────────────────

def _montar_sessao(
    session_id:   str,
    session_name: str,
    user_id:      str,
    lancamentos_brutos: List[Dict],
    config:       Dict,
) -> Dict:
    """
    Constrói o JSON de sessão a partir dos lançamentos brutos do scanner.
    Persiste cada lançamento como versions[0] com op: original.
    """
    filtros_norm     = [f for f in config.get("contas_proprias", []) if f.strip()]
    numeros_proprios = [n.strip() for n in config.get("numeros_contas_proprias", []) if n.strip()]

    now = datetime.now(timezone.utc).isoformat()
    ids_usados: Dict[str, int] = {}
    meses: Dict[str, Dict] = {}

    for lanc in lancamentos_brutos:
        data = lanc.get("date") or ""
        mes  = parse_date(data)
        if not mes:
            continue

        state = _classificar_lancamento(lanc, filtros_norm, numeros_proprios)

        # Coordenadas do parser — persistidas em versions[0]
        raw_bbox  = lanc.get("bbox")
        page_num  = lanc.get("page") or lanc.get("_page")  # v6/scanner usa 'page'
        file_idx  = lanc.get("_file_idx", 0)
        if raw_bbox and page_num is not None:
            state["bbox"] = {
                "file_idx": file_idx,
                "page"    : page_num,
                "x0"      : raw_bbox["x0"],
                "y0"      : raw_bbox["y0"],
                "x1"      : raw_bbox["x1"],
                "y1"      : raw_bbox["y1"],
            }
        else:
            state["bbox"] = None
            # Fallback: persiste page/file_idx mesmo sem coords para o editor navegar
            if page_num is not None:
                state["page"]     = page_num
                state["file_idx"] = file_idx

        # ID determinístico com deduplicação
        base_id = _gerar_lanc_id(data, state["descricao"], state["valor"])
        count   = ids_usados.get(base_id, 0)
        lanc_id = base_id if count == 0 else _gerar_lanc_id(data, state["descricao"], state["valor"], count)
        ids_usados[base_id] = count + 1

        if mes not in meses:
            meses[mes] = {}

        meses[mes][lanc_id] = {
            "versions": [
                {
                    "v"        : 1,
                    "op"       : "original",
                    "source"   : "parser",
                    "timestamp": now,
                    "state"    : state,
                }
            ]
        }

    return {
        "session_id"  : session_id,
        "session_name": session_name,
        "created_at"  : now,
        "user_id"     : user_id,
        "config"      : {
            "contas_proprias"        : filtros_norm,
            "numeros_contas_proprias": numeros_proprios,
            "filtros_ativos"         : [],
            "modo"                   : config.get("modo", "padrao"),
            "periodo"                : {
                "inicio": min(meses.keys()) if meses else None,
                "fim"   : max(meses.keys()) if meses else None,
            },
        },
        "meses": meses,
    }


# ── API pública ───────────────────────────────────────────────────────────────

async def criar_sessao(
    user_id:      str,
    file_ids:     List[str],
    pdf_paths:    List[str],
    session_name: str,
    config:       Dict,
    bancos:       Optional[List[Optional[str]]] = None,
) -> Dict:
    """
    Pipeline completo de criação de sessão:
      1. Scanner extrai lançamentos de cada PDF (na ordem de pdf_paths)
         - Se bancos[i] for informado explicitamente, usa esse banco
         - Caso contrário, tenta auto-detectar o banco:
             * banco vanilla → extrator_bancario.extract(pdf, banco)
             * assinatura do usuário (user:{id}) → extract_with_signature
             * não detectado → scanner_legacy chord-based como fallback
      2. Lançamentos de todos os arquivos são mesclados com _file_idx
      3. Classificação inicial define active + flag por lançamento
      4. Persiste como versions[0] no JSON de sessão
    """
    lancamentos_brutos: List[Dict] = []
    bancos_detectados:  List[Optional[str]] = []

    # Carrega assinaturas do usuário uma só vez para reutilizar na detecção
    try:
        user_sigs = await asyncio.to_thread(listar_assinaturas, user_id)
    except Exception:
        user_sigs = []

    # Indexa assinaturas por id para acesso rápido
    user_sigs_by_id = {s["id"]: s for s in user_sigs}

    for file_idx, pdf_path in enumerate(pdf_paths):
        banco_explicito = (bancos[file_idx] if bancos and file_idx < len(bancos) else None)

        if banco_explicito:
            # Banco informado pelo usuário explicitamente — usa direto
            banco_usado = banco_explicito
            if banco_explicito.startswith("user:"):
                sig_id = banco_explicito[5:]
                sig    = user_sigs_by_id.get(sig_id)
                if sig:
                    extraction = await asyncio.to_thread(extract_with_signature, pdf_path, sig)
                else:
                    extraction = {"lancamentos": []}
            else:
                extraction = await asyncio.to_thread(extract_banco, pdf_path, banco_explicito)

        else:
            # Tenta auto-detecção
            banco_detectado = await asyncio.to_thread(
                autodetect.detect, pdf_path, user_sigs or None
            )

            if banco_detectado and banco_detectado.startswith("user:"):
                sig_id = banco_detectado[5:]
                sig    = user_sigs_by_id.get(sig_id)
                if sig:
                    banco_usado = banco_detectado
                    extraction  = await asyncio.to_thread(extract_with_signature, pdf_path, sig)
                else:
                    banco_usado = None
                    extraction  = await asyncio.to_thread(extract_all_pages, pdf_path)
            elif banco_detectado and banco_detectado in autodetect.CATALOG:
                banco_usado = banco_detectado
                extraction  = await asyncio.to_thread(extract_banco, pdf_path, banco_detectado)
            else:
                banco_usado = None
                extraction  = await asyncio.to_thread(extract_all_pages, pdf_path)

        bancos_detectados.append(banco_usado)

        for lanc in extraction.get("lancamentos", []):
            lancamentos_brutos.append({
                **lanc,
                "_file_idx": file_idx,
            })

    session_id              = uuid.uuid4().hex[:16]
    sessao                  = _montar_sessao(session_id, session_name, user_id, lancamentos_brutos, config)
    sessao["file_ids"]      = file_ids
    sessao["file_id"]       = file_ids[0]   # backward compat (editor de PDF legado)
    sessao["bancos_detectados"] = bancos_detectados

    await ensure_user_dir(user_id)
    await write_session(user_id, session_id, sessao)

    return _session_view(sessao)


async def get_sessao(user_id: str, session_id: str) -> Dict:
    """Retorna visão atual da sessão (versions[-1].state por lançamento)."""
    sessao = await read_session(user_id, session_id)
    return _session_view(sessao)


async def get_sessao_raw(user_id: str, session_id: str) -> Dict:
    """Retorna JSON completo incluindo versions[] — para debug/auditoria."""
    return await read_session(user_id, session_id)


async def listar_sessoes(user_id: str) -> List[Dict]:
    return await list_user_sessions(user_id)


async def remover_sessao(user_id: str, session_id: str) -> bool:
    return await delete_session(user_id, session_id)


async def patch_sessao(user_id: str, session_id: str, updates: Dict) -> Dict:
    """
    Atualiza metadados da sessão: session_name e/ou pinned.
    Usa write_lock para consistência. Não toca em lançamentos.
    """
    async with write_lock:
        sessao = await read_session(user_id, session_id)
        if "session_name" in updates and updates["session_name"] is not None:
            sessao["session_name"] = updates["session_name"].strip()
        if "pinned" in updates and updates["pinned"] is not None:
            sessao["pinned"] = bool(updates["pinned"])
        await write_session(user_id, session_id, sessao)
    return {
        "session_id"  : sessao["session_id"],
        "session_name": sessao["session_name"],
        "pinned"      : sessao.get("pinned", False),
    }


async def mutate_session(user_id: str, session_id: str, mutation: Dict) -> Dict:
    """
    Portão único de mutação (seção 10 do documento).
    Toda mutação é serializada via write_lock e persiste como novo snapshot.
    Nunca sobrescreve versions[] existentes.

    Retorna {sessao, apuracao, laudo} em uma única resposta — eliminando os dois
    GETs extras que o frontend faria a seguir e chamando apurar() apenas uma vez.
    """
    async with write_lock:
        sessao = await read_session(user_id, session_id)
        sessao = _apply_mutation(sessao, mutation)
        await write_session(user_id, session_id, sessao)

    # Uma única chamada a apurar() produz tanto apuracao quanto laudo
    lancamentos = _session_to_lancamentos(sessao)
    config_dict = sessao.get("config", {})
    config = ConfigApuracao(
        contas_proprias         = config_dict.get("contas_proprias", []),
        numeros_contas_proprias = config_dict.get("numeros_contas_proprias", []),
        modo                    = config_dict.get("modo", "padrao"),
        grupos_override         = _resolve_grupos_override(
            config_dict.get("grupos_override_log", []),
            config_dict.get("grupos_override"),
        ),
    )
    apuracao_result = await asyncio.to_thread(apurar, lancamentos, config)

    titular        = config.contas_proprias[0] if config.contas_proprias else sessao.get("session_name", "—")
    chaves         = sorted(apuracao_result.get("totais_por_mes", {}).keys())
    periodo_inicio = _fmt_mes(chaves[0])  if chaves else "—"
    periodo_fim    = _fmt_mes(chaves[-1]) if chaves else "—"
    laudo_result   = montar_laudo(apuracao_result, titular, periodo_inicio, periodo_fim)
    laudo_result["fontes"] = _completar_fontes_com_inativos(
        sessao,
        laudo_result.get("fontes", []),
        apuracao_result.get("excluidos", []),
    )

    return {
        "sessao"  : _session_view(sessao, apuracao_result.get("lancamentos_grupo")),
        "apuracao": apuracao_result,
        "laudo"   : laudo_result,
    }


async def calcular_apuracao(user_id: str, session_id: str) -> Dict:
    """
    Cálculo on-demand de apuração.
    Lê estado atual (versions[-1].active) → filtra ativos → chama apurar().
    Resultado nunca é persistido — use salvar_snapshot_apuracao() para fixar um ponto no tempo.
    """
    sessao      = await read_session(user_id, session_id)
    lancamentos = _session_to_lancamentos(sessao)
    config_dict = sessao.get("config", {})
    config      = ConfigApuracao(
        contas_proprias         = config_dict.get("contas_proprias", []),
        numeros_contas_proprias = config_dict.get("numeros_contas_proprias", []),
        modo                    = config_dict.get("modo", "padrao"),
        grupos_override         = _resolve_grupos_override(
            config_dict.get("grupos_override_log", []),
            config_dict.get("grupos_override"),
        ),
    )
    result = await asyncio.to_thread(apurar, lancamentos, config)
    return result


async def aplicar_flag_grupo(
    user_id   : str,
    session_id: str,
    grupo_id  : str,
    flag      : str,
) -> None:
    """
    Persiste o override de flag para um grupo como entrada append-only em grupos_override_log.
    Nunca sobrescreve — cada decisão do contador é registrada com timestamp e versão.
    Re-execução de apurar() é feita pelo caller após esta chamada.
    """
    async with write_lock:
        sessao = await read_session(user_id, session_id)
        config = sessao.setdefault("config", {})
        log    = config.setdefault("grupos_override_log", [])
        log.append({
            "v"        : len(log) + 1,
            "grupo_id" : grupo_id,
            "flag"     : flag,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source"   : "api",
            "user_id"  : user_id,
        })
        await write_session(user_id, session_id, sessao)


async def salvar_snapshot_apuracao(
    user_id   : str,
    session_id: str,
    resultado : Dict,
    trigger   : str,
) -> Dict:
    """
    Persiste o resultado de apuração como entrada append-only em apuracao_snapshots[].
    Cada snapshot registra o estado exato do log de overrides no momento (grupos_override_at_v),
    permitindo reconstruir quais decisões estavam ativas naquele ponto no tempo.
    """
    async with write_lock:
        sessao    = await read_session(user_id, session_id)
        snapshots = sessao.setdefault("apuracao_snapshots", [])
        config    = sessao.get("config", {})
        snapshot  = {
            "v"              : len(snapshots) + 1,
            "timestamp"      : datetime.now(timezone.utc).isoformat(),
            "trigger"        : trigger,
            "source"         : "api",
            "user_id"        : user_id,
            "config_snapshot": {
                "modo"                : config.get("modo", "padrao"),
                "grupos_override_at_v": len(config.get("grupos_override_log", [])),
            },
            "resultado"      : resultado,
        }
        snapshots.append(snapshot)
        await write_session(user_id, session_id, sessao)
    return snapshot


_MESES_ABR = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']

def _fmt_mes(ym: str) -> str:
    try:
        y, m = ym.split('-')
        return f"{_MESES_ABR[int(m) - 1]}/{y}"
    except Exception:
        return ym


async def calcular_laudo(user_id: str, session_id: str, _resultado: Optional[Dict] = None) -> Dict:
    """
    Cálculo on-demand do laudo estruturado.
    Chama apurar() → montar_laudo() com titular e período derivados da sessão.
    Resultado nunca é persistido.

    Aceita _resultado já calculado para evitar chamada dupla a apurar() quando
    chamado junto com calcular_apuracao() (ex.: calcular_estado).
    """
    sessao      = await read_session(user_id, session_id)
    config_dict = sessao.get("config", {})
    config      = ConfigApuracao(
        contas_proprias         = config_dict.get("contas_proprias", []),
        numeros_contas_proprias = config_dict.get("numeros_contas_proprias", []),
        modo                    = config_dict.get("modo", "padrao"),
        grupos_override         = _resolve_grupos_override(
            config_dict.get("grupos_override_log", []),
            config_dict.get("grupos_override"),
        ),
    )

    if _resultado is None:
        lancamentos = _session_to_lancamentos(sessao)
        _resultado  = await asyncio.to_thread(apurar, lancamentos, config)

    titular = (
        config.contas_proprias[0]
        if config.contas_proprias
        else sessao.get('session_name', '—')
    )

    chaves         = sorted(_resultado.get('totais_por_mes', {}).keys())
    periodo_inicio = _fmt_mes(chaves[0])  if chaves else '—'
    periodo_fim    = _fmt_mes(chaves[-1]) if chaves else '—'

    return montar_laudo(_resultado, titular, periodo_inicio, periodo_fim)


async def calcular_estado(user_id: str, session_id: str) -> Dict:
    """
    Retorna {sessao, apuracao, laudo} com uma única execução de apurar().
    Equivalente ao que o mutate já faz, mas para leitura pura (sem mutação).
    Usado pelo frontend ao selecionar/abrir uma sessão.
    """
    sessao      = await read_session(user_id, session_id)
    lancamentos = _session_to_lancamentos(sessao)
    config_dict = sessao.get("config", {})
    config      = ConfigApuracao(
        contas_proprias         = config_dict.get("contas_proprias", []),
        numeros_contas_proprias = config_dict.get("numeros_contas_proprias", []),
        modo                    = config_dict.get("modo", "padrao"),
        grupos_override         = _resolve_grupos_override(
            config_dict.get("grupos_override_log", []),
            config_dict.get("grupos_override"),
        ),
    )
    apuracao_result = await asyncio.to_thread(apurar, lancamentos, config)

    titular        = config.contas_proprias[0] if config.contas_proprias else sessao.get("session_name", "—")
    chaves         = sorted(apuracao_result.get("totais_por_mes", {}).keys())
    periodo_inicio = _fmt_mes(chaves[0])  if chaves else "—"
    periodo_fim    = _fmt_mes(chaves[-1]) if chaves else "—"
    laudo_result   = montar_laudo(apuracao_result, titular, periodo_inicio, periodo_fim)
    laudo_result["fontes"] = _completar_fontes_com_inativos(
        sessao,
        laudo_result.get("fontes", []),
        apuracao_result.get("excluidos", []),
    )

    return {
        "sessao"  : _session_view(sessao, apuracao_result.get("lancamentos_grupo")),
        "apuracao": apuracao_result,
        "laudo"   : laudo_result,
    }


# ── Helpers internos ──────────────────────────────────────────────────────────


def _completar_fontes_com_inativos(
    sessao: Dict,
    fontes_ativas: List[Dict],
    excluidos_sistema: Optional[List[Dict]] = None,
) -> List[Dict]:
    """
    Garante que o resultado sempre contenha todos os grupos da sessão —
    ativos, inativos e excluídos pelo sistema. Cada grupo preserva seu estado.

    O cálculo (apurar) roda apenas sobre os lançamentos ativos — isso não muda.
    O resultado final inclui grupos que não participaram da conta para que a
    tela os exiba com estado desligado ou com o motivo de exclusão do sistema.
    """
    grupo_ids_ativos = {f.get("grupo_id", "") for f in fontes_ativas}

    # Coleta entradas inativas (valor > 0, active=False)
    entradas_inativas = []
    for mes, lancs in sessao.get("meses", {}).items():
        for lanc_data in lancs.values():
            versions = lanc_data.get("versions", [])
            if not versions:
                continue
            state = versions[-1]["state"]
            valor = state.get("valor", 0) or 0.0
            if valor <= 0:
                continue
            if state.get("active", True):
                continue
            entradas_inativas.append({
                "origem_destino": state.get("descricao") or "",
                "descricao"     : state.get("descricao") or "",
            })

    if not entradas_inativas:
        return fontes_ativas

    # Agrupa pelo mesmo Levenshtein usado em apurar()
    grupos = agrupar_por_pagador(entradas_inativas)

    display_cache = sessao.get("config", {}).get("grupos_display_cache", {})

    fontes_inativas = []
    for grupo_id, lancs in grupos.items():
        if grupo_id in grupo_ids_ativos:
            continue  # já está no resultado ativo
        # Usa a descrição original (não normalizada) como label do grupo
        pagador = lancs[0].get("descricao") or grupo_id
        cache   = display_cache.get(grupo_id, {})
        fontes_inativas.append({
            "grupo_id"        : grupo_id,
            "pagador"         : pagador,
            "renda_base"      : cache.get("renda_base", 0.0),
            "participacao_pct": 0.0,
            "cv_pct"          : cache.get("cv_pct"),
            "regularidade"    : cache.get("regularidade", "0/0"),
            "aparicoes"       : 0,
            "active"          : False,
            "faixa_mensal"    : {"min": 0.0, "max": 0.0},
        })

    # ── Grupos excluídos pelo sistema ────────────────────────────────────────
    # Distintos dos inativos (active=False pelo usuário): estes têm active=True
    # mas foram descartados pelo cálculo (auto_transferencia, circular, etc.).
    # Persistimos os grupo_ids para que o toggle saiba acionar grupos_override.
    fontes_excluidas_sistema: List[Dict] = []
    if excluidos_sistema:
        grupos_override_atual = _resolve_grupos_override(
            sessao.get("config", {}).get("grupos_override_log", []),
            sessao.get("config", {}).get("grupos_override"),
        )
        grupo_ids_inativos = {f.get("grupo_id", "") for f in fontes_inativas}

        # Monta lista de entradas para agrupar_por_pagador — mesmo Levenshtein
        # que o apurar() usa. Cada item carrega o motivo para recuperar depois.
        entradas_excl = []
        motivo_por_desc: Dict[str, str] = {}
        for ex in excluidos_sistema:
            pagador = ex.get("pagador") or ""
            if not pagador:
                continue
            motivo_por_desc[normalizar(pagador)] = ex.get("motivo") or "desconhecido"
            entradas_excl.append({"origem_destino": pagador, "descricao": pagador})

        grupos_excl = agrupar_por_pagador(entradas_excl) if entradas_excl else {}

        # Resolve motivo dominante por grupo após o agrupamento Levenshtein
        ids_sistema: List[str] = []
        vistos_agrupados: Dict[str, Dict] = {}
        for gid, lancs in grupos_excl.items():
            if gid in grupo_ids_ativos or gid in grupo_ids_inativos:
                continue
            pagador = lancs[0].get("descricao") or gid
            # Motivo: usa o do primeiro lançamento que bater exato, senão o mais comum
            motivo = motivo_por_desc.get(gid) or next(
                (motivo_por_desc[normalizar(l["descricao"])]
                 for l in lancs if normalizar(l["descricao"]) in motivo_por_desc),
                "desconhecido"
            )
            ids_sistema.append(gid)
            vistos_agrupados[gid] = {"pagador": pagador, "motivo": motivo}

        # Salva na sessão para que o toggle saiba quais são system_excluded
        sessao.setdefault("config", {})["grupos_excluidos_sistema"] = ids_sistema

        cache_disp = sessao.get("config", {}).get("grupos_display_cache", {})
        for gid, meta in vistos_agrupados.items():
            # Se já foi forçado estável pelo usuário, não exibe mais como excluído
            if grupos_override_atual.get(gid) == "estavel":
                continue
            cache = cache_disp.get(gid, {})
            fontes_excluidas_sistema.append({
                "grupo_id"        : gid,
                "pagador"         : meta["pagador"],
                "renda_base"      : cache.get("renda_base", 0.0),
                "participacao_pct": 0.0,
                "cv_pct"          : cache.get("cv_pct"),
                "regularidade"    : cache.get("regularidade", "0/0"),
                "aparicoes"       : 0,
                "active"          : False,
                "system_excluded" : True,
                "motivo_exclusao" : meta["motivo"],
                "faixa_mensal"    : {"min": 0.0, "max": 0.0},
            })

    return fontes_ativas + fontes_inativas + fontes_excluidas_sistema

def _session_view(sessao: Dict, lancamentos_grupo: Optional[Dict] = None) -> Dict:
    """
    Projeta a sessão para a visão atual:
    cada lançamento mostra apenas versions[-1].state + metadata leve.

    lancamentos_grupo: mapa {lanc_id → {grupo_id, motivo}} produzido por apurar().
    Quando presente, injeta grupo_id e grupo_motivo no state de cada lançamento
    sem mutar o dado persistido — são campos derivados da apuração corrente.
    """
    meses_view: Dict[str, List[Dict]] = {}
    for mes, lancs in sessao.get("meses", {}).items():
        lista = []
        for lanc_id, lanc_data in lancs.items():
            versions = lanc_data.get("versions", [])
            if not versions:
                continue
            state = versions[-1]["state"]
            if lancamentos_grupo and lanc_id in lancamentos_grupo:
                state = dict(state)  # cópia rasa — não mutar o dado persistido
                state["grupo_id"]     = lancamentos_grupo[lanc_id]["grupo_id"]
                state["grupo_motivo"] = lancamentos_grupo[lanc_id]["motivo"]
            lista.append({
                "lanc_id"       : lanc_id,
                "state"         : state,
                "versions_count": len(versions),
            })
        # Ordena por data dentro do mês
        lista.sort(key=lambda l: l["state"].get("data") or "")
        meses_view[mes] = lista

    raw_file_id  = sessao.get("file_id")
    raw_file_ids = sessao.get("file_ids") or ([raw_file_id] if raw_file_id else [])

    # Constrói a view de config: resolve o log para o dict atual e não expõe o log bruto
    # (o log completo está disponível via GET /sessao/{id}/raw)
    config_raw  = sessao.get("config", {})
    config_view = {k: v for k, v in config_raw.items() if k != "grupos_override_log"}
    config_view["grupos_override"] = _resolve_grupos_override(
        config_raw.get("grupos_override_log", []),
        config_raw.get("grupos_override"),
    )

    return {
        "session_id"       : sessao["session_id"],
        "session_name"     : sessao["session_name"],
        "created_at"       : sessao["created_at"],
        "file_id"          : raw_file_id,
        "file_ids"         : raw_file_ids,
        "config"           : config_view,
        "meses"            : meses_view,
        "pinned"           : sessao.get("pinned", False),
        "bancos_detectados": sessao.get("bancos_detectados", []),
    }


def _session_to_lancamentos(sessao: Dict) -> List[Dict]:
    """
    Converte sessão → lista de lançamentos no formato esperado por apurar().
    Mapeamento: active → incluido.
    """
    lancamentos = []
    for mes, lancs in sessao.get("meses", {}).items():
        for lanc_id, lanc_data in lancs.items():
            versions = lanc_data.get("versions", [])
            if not versions:
                continue
            state = versions[-1]["state"]
            valor = state.get("valor", 0) or 0.0
            lancamentos.append({
                "id"            : lanc_id,
                "date"          : state.get("data"),
                "entrada"       : valor if valor > 0 else None,
                "saida"         : valor if valor < 0 else None,
                "descricao"     : state.get("descricao"),
                "origem_destino": state.get("descricao"),
                "incluido"      : state.get("active", True),
                "grupo"         : state.get("flag"),
            })
    return lancamentos


# Flags que implicam active: False automaticamente
_FLAGS_INATIVAS = {"auto_transferencia", "despesa_recorrente", "ignorar",
                   "ruido", "sem_historico", "renda_circular", "renda_duplicada"}
# Flags que implicam active: True automaticamente
_FLAGS_ATIVAS   = {"renda_recorrente", "renda_eventual", "forcar_estavel", "estavel"}


def _resolve_grupos_override(
    log: List[Dict],
    legacy_dict: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """
    Reduz grupos_override_log ao estado atual: flag mais recente por grupo_id.
    Migração suave: se log vazio e legacy_dict existe, usa o dict legado sem reescrever.
    """
    if not log and legacy_dict:
        return dict(legacy_dict)
    result: Dict[str, str] = {}
    for entry in log:
        result[entry["grupo_id"]] = entry["flag"]
    return result


def _apply_create_lanc(sessao: Dict, mutation: Dict) -> Dict:
    """
    Cria um novo lançamento manual a partir de área selecionada no editor.
    Gera lanc_id único e insere em meses[mes][lanc_id].
    """
    params    = mutation.get("params", {})
    source    = mutation.get("source", "api")
    timestamp = mutation.get("timestamp") or datetime.now(timezone.utc).isoformat()

    data      = (params.get("data") or "").strip()
    descricao = (params.get("descricao") or "").strip()
    valor     = round(float(params.get("valor") or 0), 2)
    active    = bool(params.get("active", valor > 0))
    flag      = params.get("flag") or ("renda_recorrente" if valor > 0 else "despesa_recorrente")
    bbox      = params.get("bbox")

    # Determina mês pelo campo data ("YYYY-MM-DD" → "YYYY-MM")
    mes = data[:7] if len(data) >= 7 else None
    if not mes:
        return sessao  # sem data não é possível categorizar

    lanc_id = "lanc_m_" + uuid.uuid4().hex[:8]

    state: Dict[str, Any] = {
        "valor"     : valor,
        "descricao" : descricao,
        "data"      : _normalizar_data(data) if data else None,
        "active"    : active,
        "flag"      : flag,
        "comentario": None,
    }
    if bbox:
        state["bbox"] = bbox

    new_lanc = {
        "versions": [{
            "v"        : 1,
            "op"       : "create_lanc",
            "source"   : source,
            "timestamp": timestamp,
            "state"    : state,
        }]
    }

    if mes not in sessao.setdefault("meses", {}):
        sessao["meses"][mes] = {}
    sessao["meses"][mes][lanc_id] = new_lanc
    return sessao


def _apply_toggle_grupo(sessao: Dict, mutation: Dict) -> Dict:
    """
    Ativa ou desativa em massa todos os lançamentos de entrada que pertençam
    ao grupo identificado por 'grupo_id' (chave normalizada usada pela apuração).
    O 'grupo_id' é normalizar(descricao), garantindo que o matching seja idêntico
    ao agrupamento Levenshtein da apuração — sem ambiguidade de substring.
    """
    params    = mutation.get("params", {})
    source    = mutation.get("source", "api")
    timestamp = mutation.get("timestamp") or datetime.now(timezone.utc).isoformat()

    grupo_id      = (params.get("grupo_id") or "").strip()
    active        = bool(params.get("active", True))
    display_cache = params.get("display_cache")  # valores estéticos opcionais

    if not grupo_id:
        return sessao

    # Quando o usuário ativa um grupo excluído pelo sistema (auto_transferencia, circular etc.),
    # adiciona grupos_override='estavel' para que apurar() force a inclusão.
    # Quando desativa, remove o override (adiciona 'ignorar' ao log) se existia.
    grupos_excluidos_sistema = sessao.get("config", {}).get("grupos_excluidos_sistema", [])
    if grupo_id in grupos_excluidos_sistema:
        log = sessao.setdefault("config", {}).setdefault("grupos_override_log", [])
        log.append({
            "v"        : len(log) + 1,
            "grupo_id" : grupo_id,
            "flag"     : "estavel" if active else "ignorar",
            "timestamp": timestamp,
            "source"   : source,
        })

    # Quando desativa, persiste o cache de exibição (renda_base, cv_pct, regularidade)
    # para que o card continue mostrando os valores da última vez que estava ativo.
    # Puramente estético — não interfere no cálculo.
    if not active and display_cache:
        cache_map = sessao.setdefault("config", {}).setdefault("grupos_display_cache", {})
        cache_map[grupo_id] = {
            "renda_base"  : display_cache.get("renda_base"),
            "cv_pct"      : display_cache.get("cv_pct"),
            "regularidade": display_cache.get("regularidade"),
        }

    for mes_lancs in sessao.get("meses", {}).values():
        for lanc_id, lanc in mes_lancs.items():
            versions = lanc.get("versions", [])
            if not versions:
                continue

            last_state = versions[-1]["state"]

            # Ignora saídas
            val = last_state.get("valor", 0) or 0
            if val < 0:
                continue

            # Matching por similaridade Levenshtein — idêntico ao agrupar_por_pagador
            desc_norm  = normalizar(last_state.get("descricao") or "")
            similarity = _rfuzz.ratio(desc_norm, grupo_id) / 100.0
            if similarity < LEVENSHTEIN_THRESHOLD:
                continue

            new_state = dict(last_state)
            new_state["active"] = active
            new_version = {
                "v"        : len(versions) + 1,
                "op"       : "toggle_grupo",
                "source"   : source,
                "timestamp": timestamp,
                "state"    : new_state,
            }
            lanc["versions"].append(new_version)

    return sessao


def _apply_mutation(sessao: Dict, mutation: Dict) -> Dict:
    """
    Aplica uma operação de mutação ao JSON de sessão.
    Sempre adiciona novo snapshot em versions[] — nunca sobrescreve.
    """
    op        = mutation.get("op")
    target    = mutation.get("target")  # lanc_id
    params    = mutation.get("params", {})
    source    = mutation.get("source", "api")
    timestamp = mutation.get("timestamp") or datetime.now(timezone.utc).isoformat()

    if not op:
        return sessao

    # create_lanc não precisa de target — cria novo lançamento
    if op == "create_lanc":
        return _apply_create_lanc(sessao, mutation)

    # toggle_grupo não precisa de target — afeta todos do grupo
    if op == "toggle_grupo":
        return _apply_toggle_grupo(sessao, mutation)

    if not target:
        return sessao

    # Localiza o lançamento pelo ID
    for mes, lancs in sessao.get("meses", {}).items():
        if target not in lancs:
            continue

        versions = lancs[target]["versions"]
        if not versions:
            break

        last_state = dict(versions[-1]["state"])  # cópia

        if op == "toggle_active":
            last_state["active"] = params.get("active", not last_state.get("active", True))

        elif op == "set_flag":
            flag = params.get("flag")
            if flag:
                last_state["flag"] = flag
                if flag in _FLAGS_INATIVAS:
                    last_state["active"] = False
                elif flag in _FLAGS_ATIVAS:
                    last_state["active"] = True

        elif op == "edit_field":
            allowed = {"valor", "descricao", "data"}
            for field, val in params.items():
                if field in allowed:
                    last_state[field] = val

        elif op == "add_comentario":
            last_state["comentario"] = params.get("comentario")

        elif op == "edit_bbox":
            # Ajuste manual do bounding box pelo usuário no editor de PDF
            keys = ("x0", "y0", "x1", "y1")
            if all(k in params for k in keys) and last_state.get("bbox"):
                last_state["bbox"] = {
                    **last_state["bbox"],
                    "x0": float(params["x0"]),
                    "y0": float(params["y0"]),
                    "x1": float(params["x1"]),
                    "y1": float(params["y1"]),
                }

        elif op == "reset":
            # Volta ao estado original (versions[0])
            last_state = dict(versions[0]["state"])
            op = "reset"

        new_version = {
            "v"        : len(versions) + 1,
            "op"       : op,
            "source"   : source,
            "timestamp": timestamp,
            "state"    : last_state,
        }
        lancs[target]["versions"].append(new_version)
        break

    return sessao

