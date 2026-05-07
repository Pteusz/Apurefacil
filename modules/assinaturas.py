"""
modules/assinaturas.py
======================
CRUD de assinaturas de banco definidas por usuários + derivação a partir de
retângulo selecionado no editor de PDF.

Uma assinatura agrupa as 5 variáveis:
    font_dominant   : família tipográfica dominante
    anchor_x_range  : faixa horizontal do elemento âncora
    anchor_pattern  : regex derivada dos textos âncora
    value_x_range   : faixa horizontal do valor monetário
    signal_logic    : regra de sinal (prefix_minus, suffix_minus, suffix_cd,
                      embedded_plus_minus, suffix_parenthesis)
"""

import json
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from statistics import median
from typing import Optional

import fitz

from db import get_db

DEC_RE = re.compile(r'\d{1,3}(?:\.\d{3})*,\d{2}')

# ── Constantes de derivação ───────────────────────────────────────────────────

_SIGNAL_PATTERNS = [
    ("embedded_plus_minus", re.compile(r"[+−\-]R\$")),
    ("suffix_cd",           re.compile(r"\d,\d{2}\s*[CD]$")),
    ("suffix_parenthesis",  re.compile(r"\d{1,3}(?:\.\d{3})*,\d{2}\s*\([+\-]\)")),
    ("suffix_minus",        re.compile(r"\d,\d{2}-$")),
    ("prefix_minus",        re.compile(r"^-\d")),
]

_DATE_ANCHORS = [
    ("data_ddmmyyyy_hifen", re.compile(r"^\d{2}-\d{2}-\d{4}$")),
    ("data_ddmmyyyy",       re.compile(r"^\d{2}/\d{2}/\d{4}$")),
    ("data_ddmm",           re.compile(r"^\d{2}/\d{2}$")),
    ("hora",                re.compile(r"^\d{2}:\d{2}$")),
    ("docto",               re.compile(r"^\d{6,7}$")),
    ("data_longa",          re.compile(r"^\d{1,2}\s+de\s+\w+\s+de\s+\d{4}$", re.I)),
    ("hora_seg",            re.compile(r"^\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}$")),
]

_PATTERN_MAP = {
    "data_ddmmyyyy_hifen": r"^\d{2}-\d{2}-\d{4}$",
    "data_ddmmyyyy"      : r"^\d{2}/\d{2}/\d{4}$",
    "data_ddmm"          : r"^\d{2}/\d{2}$",
    "hora"               : r"^\d{2}:\d{2}$",
    "docto"              : r"^\d{6,7}$",
    "data_longa"         : r"^\d{1,2} de \w+ de \d{4}$",
    "hora_seg"           : r"^\d{2}/\d{2}/\d{4}",
}


# ── Extração de spans de uma área retangular ──────────────────────────────────

def _spans_na_area(
    pdf_path: str,
    page: int,
    x0: float, y0: float, x1: float, y1: float,
    margin: float = 1.0,
) -> list[dict]:
    """Retorna spans contidos dentro do retângulo fornecido.

    Margem mínima (1 px) apenas para absorver imprecisão de coordenada PDF vs
    tela — não extrapola o retângulo desenhado pelo usuário para os lados.
    """
    doc = fitz.open(pdf_path)
    # page é 1-indexed no contrato público
    pn  = max(0, page - 1)
    if pn >= len(doc):
        doc.close()
        return []

    raw = doc[pn].get_text("dict")
    spans: list[dict] = []
    for block in raw["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                t = span["text"].strip()
                if not t:
                    continue
                bx0, by0 = span["bbox"][0], span["bbox"][1]
                if (x0 - margin <= bx0 <= x1 + margin and
                        y0 - margin <= by0 <= y1 + margin):
                    spans.append({
                        "text": t,
                        "x0"  : round(bx0, 1),
                        "y0"  : round(by0, 1),
                        "font": span["font"].lower(),
                        "hv"  : bool(DEC_RE.search(t)),
                    })
    doc.close()
    spans.sort(key=lambda s: (s["y0"], s["x0"]))
    return spans


def _agrupar_linhas(spans: list[dict], threshold: float = 5.0) -> list[list[dict]]:
    if not spans:
        return []
    linhas, cur = [], [spans[0]]
    for s in spans[1:]:
        if s["y0"] - cur[-1]["y0"] > threshold:
            linhas.append(cur)
            cur = [s]
        else:
            cur.append(s)
    linhas.append(cur)
    return linhas


# ── Derivação da assinatura ───────────────────────────────────────────────────

def _detectar_colunas_texto(
    todos_os_spans: list[dict],
    anchor_x_min: float,
    anchor_x_max: float,
    rect_x1: float = float("inf"),
) -> list[dict]:
    """
    Agrupa os spans de texto não-numéricos por proximidade horizontal (gap > 20 px).

    Recebe TODOS os spans da área selecionada (incluindo linhas de continuação
    multiline que não possuem valor monetário próprio), garantindo que colunas
    como "Histórico" com quebra de linha sejam detectadas corretamente.

    rect_x1: borda direita do retângulo desenhado — o x_max de cada cluster é
    limitado a este valor, impedindo que colunas adjacentes (ex: saldo) sejam
    capturadas por causa da margem de padding interna.

    Retorna uma lista ordenada por posição X de todas as colunas de texto
    encontradas, cada uma com:
        x_min, x_max  — faixa horizontal do cluster
        exemplo       — texto mais longo encontrado nesse cluster
        is_anchor     — True se este cluster corresponde à coluna âncora

    A coluna âncora é identificada por sobreposição com [anchor_x_min, anchor_x_max].
    O frontend usa is_anchor=True para omitir essa coluna do formulário de nomeação.
    """
    # Coleta todos os spans não-numéricos com x0 e texto (inclui linhas de continuação)
    pontos: list[tuple[float, str]] = []
    for s in todos_os_spans:
        if not s["hv"]:
            pontos.append((s["x0"], s["text"]))

    if not pontos:
        return []

    pontos.sort(key=lambda p: p[0])

    # Agrupa por proximidade (gap > 20 px → nova coluna)
    grupos: list[list[tuple[float, str]]] = []
    cur: list[tuple[float, str]] = [pontos[0]]
    for x, txt in pontos[1:]:
        if x - cur[-1][0] > 20:
            grupos.append(cur)
            cur = [(x, txt)]
        else:
            cur.append((x, txt))
    grupos.append(cur)

    colunas = []
    for grupo in grupos:
        xs      = [x for x, _ in grupo]
        txts    = [t for _, t in grupo]
        x_min   = round(min(xs) - 5, 1)
        # x_max limitado à borda direita do retângulo — impede captura de colunas
        # adjacentes (ex: saldo no Itaú) que o usuário não incluiu na seleção
        x_max   = round(min(max(xs) + 25, rect_x1), 1)
        exemplo = max(txts, key=len)

        # Sobrepõe com o range da âncora?
        is_anchor = not (x_max < anchor_x_min or x_min > anchor_x_max)

        colunas.append({
            "x_min"    : x_min,
            "x_max"    : x_max,
            "exemplo"  : exemplo,
            "is_anchor": is_anchor,
        })
    return colunas


def _derivar_sinal(valores: list[str]) -> str:
    for nome, pat in _SIGNAL_PATTERNS:
        if any(pat.search(v) for v in valores):
            return nome
    return "prefix_minus"  # fallback


def _derivar_anchor_pattern(textos: list[str]) -> str:
    # Testa padrões canônicos
    for nome, pat in _DATE_ANCHORS:
        matches = sum(1 for t in textos if pat.match(t))
        if matches / max(len(textos), 1) >= 0.5:
            return _PATTERN_MAP.get(nome, pat.pattern)
    # Fallback: prefixo comum ou regex genérico
    if textos:
        prefixo = textos[0][:5]
        if all(t.startswith(prefixo) for t in textos):
            return r"^" + re.escape(prefixo)
    return r".+"


def extrair_de_retangulo(
    pdf_path: str,
    page: int,
    x0: float, y0: float, x1: float, y1: float,
) -> dict:
    """
    Deriva uma assinatura a partir de um retângulo desenhado pelo usuário.

    Retorna:
        {
            "assinatura": { font_dominant, anchor_x_min/max, anchor_pattern,
                            value_x_min/max, signal_logic },
            "preview_lancamentos": [ { page, descricao, entrada, saida } ]
        }
    """
    spans = _spans_na_area(pdf_path, page, x0, y0, x1, y1)
    if not spans:
        return {"assinatura": None, "preview_lancamentos": [], "erro": "Nenhum texto encontrado na área selecionada"}

    linhas = _agrupar_linhas(spans)
    # Filtra linhas sem valor (não são lançamentos)
    linhas_com_valor = [l for l in linhas if any(s["hv"] for s in l)]
    if len(linhas_com_valor) < 1:
        return {"assinatura": None, "preview_lancamentos": [], "erro": "Nenhum valor monetário encontrado. Selecione linhas com valores."}

    # ── Âncora: span mais à esquerda não-numérico ──────────────────────────
    anchor_xs  : list[float] = []
    anchor_txts: list[str]   = []
    for linha in linhas_com_valor:
        non_num = [s for s in linha if not s["hv"]]
        if non_num:
            anchor = min(non_num, key=lambda s: s["x0"])
            anchor_xs.append(anchor["x0"])
            anchor_txts.append(anchor["text"])

    if not anchor_xs:
        return {"assinatura": None, "preview_lancamentos": [], "erro": "Não foi possível identificar a âncora (coluna de identificação do lançamento)."}

    anchor_med     = median(anchor_xs)
    anchor_x_min   = round(anchor_med - 15, 1)
    anchor_x_max   = round(anchor_med + 25, 1)
    anchor_pattern = _derivar_anchor_pattern(anchor_txts)

    # ── Valor: span mais à direita numérico ───────────────────────────────
    value_xs  : list[float] = []
    value_txts: list[str]   = []
    for linha in linhas_com_valor:
        nums = [s for s in linha if s["hv"]]
        if nums:
            val_span = max(nums, key=lambda s: s["x0"])
            value_xs.append(val_span["x0"])
            value_txts.append(val_span["text"])

    value_med   = median(value_xs)
    value_x_min = round(value_med - 10, 1)
    value_x_max = round(value_med + 30, 1)
    signal_logic = _derivar_sinal(value_txts)

    # ── Fonte dominante ────────────────────────────────────────────────────
    fonts = Counter(s["font"].split(",")[0].split("-")[0] for s in spans)
    font_dominant = fonts.most_common(1)[0][0] if fonts else None

    assinatura = {
        "font_dominant" : font_dominant,
        "anchor_x_min"  : anchor_x_min,
        "anchor_x_max"  : anchor_x_max,
        "anchor_pattern": anchor_pattern,
        "value_x_min"   : value_x_min,
        "value_x_max"   : value_x_max,
        "signal_logic"  : signal_logic,
        "rect_x0"       : round(x0, 1),
        "rect_x1"       : round(x1, 1),
    }

    # ── Colunas de texto detectadas (para o usuário nomear no sidebar) ────
    # Passa TODOS os spans da área para capturar colunas de linhas de continuação
    # (ex: Histórico multiline no BB que não têm valor monetário próprio).
    # rect_x1 limita o x_max de cada cluster à borda direita do retângulo.
    colunas_detectadas = _detectar_colunas_texto(spans, anchor_x_min, anchor_x_max, rect_x1=x1)

    # ── Preview: roda extrator genérico no PDF inteiro ────────────────────
    from modules.extrator_bancario import extract_with_signature
    try:
        # Passa as colunas detectadas como campos temporários para que o preview
        # exiba o texto já separado por coluna (em vez de tudo mesclado em descricao).
        campos_preview = [
            {"label": c["exemplo"][:30], "x_min": c["x_min"], "x_max": c["x_max"]}
            for c in colunas_detectadas
            if not c.get("is_anchor")
        ]
        sig_preview = {**assinatura, "campos": campos_preview} if campos_preview else assinatura
        preview = extract_with_signature(pdf_path, sig_preview)
        lancamentos_preview = preview.get("lancamentos", [])
    except Exception:
        lancamentos_preview = []

    return {
        "assinatura"          : assinatura,
        "colunas_detectadas"  : colunas_detectadas,
        "preview_lancamentos" : lancamentos_preview,
    }


# ── Extração pontual (sem propagação de padrão) ───────────────────────────────

import re as _re

_SINGLE_DATE_RE = _re.compile(
    r'^\d{1,2}/\d{2}/\d{4}$'
    r'|^\d{2}-\d{2}-\d{4}$'
    r'|^\d{1,2}\s+de\s+\w+\s+de\s+\d{4}$'
    r'|^\d{1,2}/\d{2}$',
    _re.I,
)

_SAIDA_CTX = _re.compile(
    r'enviado|enviada|pagamento|debito|d[eé]bito|saque|compra|iof|tarifa',
    _re.I,
)
_ENTRADA_CTX = _re.compile(
    r'recebido|recebida|cr[eé]dito|dep[oó]sito|estorno|resgate|rendimento',
    _re.I,
)


def _parse_valor_num(texto: str) -> float:
    """Extrai o primeiro valor BRL de uma string e retorna como float positivo."""
    m = DEC_RE.search(texto)
    if not m:
        return 0.0
    return round(float(m.group().replace(".", "").replace(",", ".")), 2)


def extrair_area_simples(
    pdf_path: str,
    page: int,
    x0: float, y0: float, x1: float, y1: float,
) -> dict:
    """
    Extrai os dados de um único lançamento a partir de uma área selecionada.
    Sem derivação de assinatura, sem propagação — apenas lê o que está no bbox.

    Retorna:
        {
            "data"     : str | None,   (YYYY-MM-DD ou None se não detectada)
            "descricao": str,
            "entrada"  : float | None,
            "saida"    : float | None,
            "bbox"     : { x0, y0, x1, y1, page },
            "erro"     : str | None,
        }
    """
    from modules.apuracao import parse_datetime

    spans = _spans_na_area(pdf_path, page, x0, y0, x1, y1)
    if not spans:
        return {"erro": "Nenhum texto encontrado na área selecionada"}

    valor_spans = [s for s in spans if s["hv"]]
    texto_spans = [s for s in spans if not s["hv"]]

    if not valor_spans:
        return {"erro": "Nenhum valor monetário encontrado. Amplie a seleção."}

    # Valor: usa o span mais à direita com número (coluna de valor)
    val_span = max(valor_spans, key=lambda s: s["x0"])
    valor_num = _parse_valor_num(val_span["text"])

    # Sinal: detecta pelo formato do texto e depois pelo contexto descritivo
    signal = _derivar_sinal([s["text"] for s in valor_spans])
    descricao_ctx = " ".join(s["text"] for s in texto_spans)

    saida = None
    entrada = None
    negativo = (
        (signal == "prefix_minus"       and val_span["text"].startswith("-"))
        or (signal == "suffix_minus"    and val_span["text"].endswith("-"))
        or (signal == "suffix_cd"       and val_span["text"].upper().endswith("D"))
        or (signal == "embedded_plus_minus" and (
            "-" in val_span["text"] or "\u2212" in val_span["text"]
        ))
        or (signal == "prefix_minus"    and _SAIDA_CTX.search(descricao_ctx) is not None)
    )
    if negativo or _SAIDA_CTX.search(descricao_ctx):
        saida = valor_num
    elif _ENTRADA_CTX.search(descricao_ctx):
        entrada = valor_num
    else:
        entrada = valor_num   # default: considera entrada

    # Data: procura nos textos âncora
    data_iso = None
    descricao_parts = []
    for s in texto_spans:
        t = s["text"].strip()
        if _SINGLE_DATE_RE.match(t):
            if data_iso is None:
                dt = parse_datetime(t)
                data_iso = dt.strftime("%Y-%m-%d") if dt else None
        else:
            descricao_parts.append(t)

    descricao = " · ".join(descricao_parts).strip() or descricao_ctx.strip()

    return {
        "data"     : data_iso,
        "descricao": descricao,
        "entrada"  : entrada,
        "saida"    : saida,
        "bbox"     : {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "page": page},
        "erro"     : None,
    }


# ── CRUD ──────────────────────────────────────────────────────────────────────

def salvar(user_id: str, dados: dict) -> dict:
    """Persiste uma nova assinatura no banco de dados."""
    conn = get_db()
    try:
        now      = datetime.now(timezone.utc).isoformat()
        sig_id   = uuid.uuid4().hex[:16]

        # Serializa campos nomeados pelo usuário
        campos_raw  = dados.get("campos", [])
        campos_json = json.dumps(
            [c.model_dump() if hasattr(c, "model_dump") else dict(c) for c in campos_raw],
            ensure_ascii=False,
        )

        # Calcula layout_version (sequencial por bank_name + user)
        row = conn.execute(
            "SELECT MAX(layout_version) AS mv FROM assinaturas "
            "WHERE bank_name = ? AND created_by = ?",
            (dados["bank_name"], user_id),
        ).fetchone()
        layout_v = (row["mv"] or 0) + 1

        conn.execute(
            """INSERT INTO assinaturas
               (id, bank_name, font_dominant, anchor_x_min, anchor_x_max,
                anchor_pattern, value_x_min, value_x_max, signal_logic,
                source, layout_version, created_by, validated, created_at,
                campos_json, rect_x0, rect_x1)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sig_id,
                dados["bank_name"],
                dados.get("font_dominant"),
                dados["anchor_x_min"],
                dados["anchor_x_max"],
                dados["anchor_pattern"],
                dados["value_x_min"],
                dados["value_x_max"],
                dados.get("signal_logic", "prefix_minus"),
                "user",
                layout_v,
                user_id,
                0,
                now,
                campos_json,
                dados.get("rect_x0"),
                dados.get("rect_x1"),
            ),
        )
        conn.commit()
        return {
            **dados,
            "id"            : sig_id,
            "source"        : "user",
            "layout_version": layout_v,
            "created_at"    : now,
            "campos"        : json.loads(campos_json),
        }
    finally:
        conn.close()


def listar(user_id: str) -> list[dict]:
    """Retorna todas as assinaturas acessíveis ao usuário (próprias + vanilla)."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM assinaturas WHERE created_by = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["campos"] = json.loads(d.pop("campos_json") or "[]")
            result.append(d)
        return result
    finally:
        conn.close()


def deletar(sig_id: str, user_id: str) -> bool:
    """Remove uma assinatura do usuário. Retorna True se deletou."""
    conn = get_db()
    try:
        cur = conn.execute(
            "DELETE FROM assinaturas WHERE id = ? AND created_by = ?",
            (sig_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()

