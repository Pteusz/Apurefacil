"""
extrator_v2.py
==============
Extrator relativo de extratos bancários em PDF.
Princípio: fronteira = variação de consistência anterior (sem threshold fixo)

Bancos suportados: picpay, nubank, itau, caixa, bb, santander,
                   uber_digio, inter, bradesco, mercadopago

Schema de saída:
    {
        'total_lancamentos': int,
        'total_entradas'   : float,
        'total_saidas'     : float,
        'saldo'            : float,
        'lancamentos'      : [
            {
                'page'     : int,
                'date'     : str | None,
                'entrada'  : float | None,
                'saida'    : float | None,
                'descricao': str | None,
            }
        ]
    }
"""

import re
import fitz
from collections import Counter
from typing import Optional

DEC_RE = re.compile(r'\d{1,3}(?:\.\d{3})*,\d{2}')

# ══════════════════════════════════════════════════════════════════════════════
# ASSINATURAS — vocabulário geométrico de cada banco
# ══════════════════════════════════════════════════════════════════════════════

ASSINATURAS = {

    "picpay": {
        # invariante: font (Medium = cabeçalho, Regular = lançamento)
        # separador de bloco: font=Medium OU gap > 120% do ritmo dominante
        "ancora_x"          : (35, 60),    # coluna HH:MM
        "descricao_x"       : (235, 255),  # coluna origem/destino
        "descricao_delta_y" : 12,          # fragmentos dentro desse delta pertencem à âncora
        "valor_x"           : (500, 560),
        "sinal"             : "embedded",  # +R$ / −R$ embutido no span
    },

    "nubank": {
        # invariante: font + x0
        # Semibold em x0=120 = cabeçalho de subbloco (declara sinal)
        # Regular em x0=120 = lançamento individual
        # gap=45 + x0=58 = fronteira de bloco de dia
        "ancora_x"          : (115, 130),  # coluna tipo transação
        "bloco_x"           : (50, 68),    # coluna data do dia
        "descricao_x"       : (255, 275),  # coluna descrição
        "descricao_gap_max" : 50,
        "valor_x"           : (490, 560),
    },

    "itau": {
        # invariante: font_valor (Bold = valor monetário, Regular = tudo mais)
        # não há separador de bloco — data repete em cada linha
        # fragilidade remanescente: filtro textual para SALDO TOTAL / SALDO ANTERIOR
        "data_x"            : (25, 40),
        "descricao_x"       : (88, 110),
        "valor_x"           : (390, 560),
        "filtro_descricao"  : ["SALDO TOTAL", "SALDO ANTERIOR"],
    },

    "caixa": {
        # invariante: gap uniforme=20 + nr_doc
        # nr_doc=000000 → linha de saldo/rendimento, ignorar
        # fragilidade remanescente: filtro textual para SALDO DIA / REM BASICA / CRED JUROS
        "data_x"            : (25, 40),
        "nr_doc_x"          : (95, 115),
        "nr_doc_filtro"     : "000000",
        "descricao_x"       : (158, 178),
        "valor_x"           : (430, 470),
    },

    "bradesco": {
        # Font    : Tahoma Regular (lançamentos) | Tahoma-Bold (saldo — ignorar)
        # Colunas : data x0≈47 | tipo x0≈86 | complemento x0≈86 | docto x0≈212
        #           crédito x0≈315-345 | débito x0≈408-438 (prefixo "- ") | saldo x0≈513-531
        # Bloco   : 3 linhas por lançamento
        #   y+0,0  → tipo      (ex: "Visa Electron", "Transfe Pix")
        #   y+5,8  → docto + crédito/débito    ← âncora
        #   y+11,6 → complemento (estabelecimento / remetente)
        # Data    : DD/MM/YY, aparece só na linha-docto da 1ª transação do dia
        # Sinal   : presença do span de débito (prefixo "- ") = saída; crédito = entrada
        "data_x"       : (44, 52),
        "tipo_x"       : (83, 92),
        "complemento_x": (83, 92),
        "docto_x"      : (209, 217),
        "credito_x"    : (312, 348),
        "debito_x"     : (408, 438),
        "saldo_font"   : "Tahoma-Bold",
    },

    "mercadopago": {
        # Font    : ProximaNova-Regular (lançamentos) | font="" vazio (cabeçalhos/bold)
        # Colunas : data x0≈40 | descrição x0≈88 | ID op. x0≈197 | valor x0≈290-315
        # Âncora  : linha com data DD-MM-YYYY + ID numérico (12-15 dígitos) + "R$ ±valor"
        # Descrição: spans ProximaNova-Regular em x0≈88, ±30pt da âncora (tipo + nome)
        # Sinal   : "R$ -X" = saída; "R$ X" = entrada
        # Rendimentos: font="" em x0≈40, descrição "Rendimentos" — tratar como entrada
        "data_x"       : (37, 48),
        "descricao_x"  : (85, 95),
        "id_x"         : (193, 204),
        "valor_x"      : (288, 322),
        "descricao_gap": 30,
        "font_lancamento": "ProximaNova-Regular",
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# UTILITÁRIOS
# ══════════════════════════════════════════════════════════════════════════════

def _spans(page) -> list[dict]:
    out = []
    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                t = span["text"].strip()
                if not t:
                    continue
                out.append({
                    "text": t,
                    "x0"  : round(span["bbox"][0], 1),
                    "y0"  : round(span["bbox"][1], 1),
                    "x1"  : round(span["bbox"][2], 1),
                "y1"  : round(span["bbox"][3], 1),
                    "size": round(span["size"],    1),
                    "font": span["font"],
                })
    out.sort(key=lambda s: (s["y0"], s["x0"]))
    return out


def _agrupar_linhas(spans: list[dict], tol: float = 3.0) -> list[list[dict]]:
    if not spans:
        return []
    linhas, cur = [], [spans[0]]
    for s in spans[1:]:
        if s["y0"] - cur[-1]["y0"] > tol:
            linhas.append(cur)
            cur = [s]
        else:
            cur.append(s)
    linhas.append(cur)
    return linhas



def _bbox_da_linha(spans: list[dict]) -> dict:
    return {
        "x0": round(min(s["x0"] for s in spans), 2),
        "y0": round(min(s["y0"] for s in spans), 2),
        "x1": round(max(s["x1"] for s in spans), 2),
        "y1": round(max(s["y1"] for s in spans), 2),
    }


def _bbox_linhas_completas(
    ancora_spans: list[dict],
    frag_spans: list[dict],
    all_page_spans: list[dict],
    tol: float = 2.0,
) -> dict:
    """Bbox universal: cobre horizontalmente todos os spans dentro da faixa vertical do lançamento.

    Calcula a faixa vertical (y_top..y_bot) dos spans já atribuídos ao lançamento
    e seleciona todos os spans da página contidos nessa faixa, garantindo cobertura
    horizontal completa sem capturar elementos de outras linhas.

    y0 = elemento mais alto   →  y1 = elemento mais baixo
    x0 = elemento mais à esquerda  →  x1 = elemento mais à direita
    """
    base = ancora_spans + frag_spans
    if not base:
        return {"x0": 0, "y0": 0, "x1": 0, "y1": 0}

    y_top = min(s["y0"] for s in base) - tol
    y_bot = max(s["y1"] for s in base) + tol

    full_spans = [
        s for s in all_page_spans
        if s["y0"] >= y_top and s["y1"] <= y_bot
    ]
    return _bbox_da_linha(full_spans) if full_spans else _bbox_da_linha(base)


def _ritmo_dominante(linhas: list[list[dict]]) -> float:
    ys   = [l[0]["y0"] for l in linhas]
    gaps = [round(ys[i] - ys[i-1], 1) for i in range(1, len(ys)) if ys[i] - ys[i-1] > 0]
    return Counter(gaps).most_common(1)[0][0] if gaps else 20.0


def _extrair_valor(text: str) -> Optional[float]:
    m = DEC_RE.search(text)
    return float(m.group().replace(".", "").replace(",", ".")) if m else None


def _expandir_bboxes(lancamentos: list[dict], doc) -> None:
    """Reservado — cada extrator já computa o bbox a partir dos próprios spans."""
    pass


def _montar(lancamentos: list[dict]) -> dict:
    entradas = round(sum(l["entrada"] for l in lancamentos if l["entrada"]), 2)
    saidas   = round(sum(l["saida"]   for l in lancamentos if l["saida"]),   2)
    # limpar campos internos antes de retornar
    for l in lancamentos:
        l.pop("_y",          None)
        l.pop("_frags",      None)
        l.pop("_raw",        None)
        l.pop("_bbox_spans", None)
    return {
        "total_lancamentos": len(lancamentos),
        "total_entradas"   : entradas,
        "total_saidas"     : saidas,
        "saldo"            : round(entradas + saidas, 2),
        "lancamentos"      : lancamentos,
    }


# ══════════════════════════════════════════════════════════════════════════════
# EXTRATOR PICPAY
# ══════════════════════════════════════════════════════════════════════════════

def _extract_picpay(pdf_path: str) -> dict:
    sig      = ASSINATURAS["picpay"]
    HORA_RE  = re.compile(r"^\d{2}:\d{2}$")
    VALOR_RE = re.compile(r"([+−\-])R\$\s*(\d{1,3}(?:\.\d{3})*,\d{2})")
    DATA_RE  = re.compile(r"(\d{1,2}\s+de\s+\w+\s+\d{4})", re.I)
    FILTRO   = re.compile(
        r"^(Hora|Tipo|Origem\s*/\s*Destino|Forma de pagamento|Valor|"
        r"Com saldo|Saldo ao final|Saldo final|Documento emitido|PicPay|"
        r"CNPJ|Dias úteis|Extrato de conta|Per[ií]odo|Ag[eê]ncia|CPF)$", re.I
    )

    doc = fitz.open(pdf_path)
    lancamentos = []
    data_atual  = None

    for pn in range(len(doc)):
        all_spans = _spans(doc[pn])
        linhas    = _agrupar_linhas(all_spans)
        ritmo     = _ritmo_dominante(linhas)

        ancoras = []
        for linha in linhas:
            # capturar data de bloco
            for s in linha:
                if sig["ancora_x"][0] <= s["x0"] <= sig["ancora_x"][1]:
                    mm = DATA_RE.match(s["text"])
                    if mm:
                        data_atual = mm.group(1)

            ancora = next(
                (s for s in linha
                 if sig["ancora_x"][0] <= s["x0"] <= sig["ancora_x"][1]
                 and HORA_RE.match(s["text"])), None
            )
            if not ancora:
                continue

            m_val = next(
                (VALOR_RE.search(s["text"]) for s in linha
                 if VALOR_RE.search(s["text"])), None
            )
            if not m_val:
                continue

            # Ignorar transações canceladas
            tipo_s = next((s["text"] for s in linha if 115 <= s["x0"] <= 200), "")
            if "cancelado" in tipo_s.lower():
                continue

            ancoras.append({
                "ancora"     : ancora,
                "linha"      : linha,
                "m_val"      : m_val,
                "data"       : data_atual,
                "page"       : pn + 1,
                "_frags"     : [],
                "_bbox_spans": [],
            })

        if not ancoras:
            continue

        # Voronoi relativo para fragmentos de descrição multilinha (texto)
        dx0, dx1   = sig["descricao_x"]
        delta_max  = sig["descricao_delta_y"] + ritmo * 0.5
        desc_spans = [
            s for s in all_spans
            if dx0 <= s["x0"] <= dx1
            and not FILTRO.search(s["text"])
            and not DEC_RE.search(s["text"])
        ]
        ancora_ys = [a["ancora"]["y0"] for a in ancoras]
        for ds in desc_spans:
            dists = [abs(ds["y0"] - ay) for ay in ancora_ys]
            idx   = dists.index(min(dists))
            if min(dists) <= delta_max:
                ancoras[idx]["_frags"].append(ds)

        # Segundo passe para bbox: mesma proximidade mas sem restrição de coluna
        # (captura colunas como "Conta:", "Agência:" que ficam fora de descricao_x)
        bbox_spans_all = [s for s in all_spans if not FILTRO.search(s["text"])]
        for bs in bbox_spans_all:
            dists = [abs(bs["y0"] - ay) for ay in ancora_ys]
            idx   = dists.index(min(dists))
            if min(dists) <= delta_max:
                ancoras[idx]["_bbox_spans"].append(bs)

        for a in ancoras:
            m     = a["m_val"]
            sinal = -1 if m.group(1) in ("-", "−") else +1
            v     = float(m.group(2).replace(".", "").replace(",", "."))
            desc  = " ".join(
                s["text"] for s in sorted(a["_frags"], key=lambda s: s["y0"])
            ) or None

            lancamentos.append({
                "page"     : a["page"],
                "date"     : a["data"],
                "entrada"  : round(v,  2) if sinal == +1 else None,
                "saida"    : round(-v, 2) if sinal == -1 else None,
                "bbox"     : _bbox_linhas_completas(a["linha"], a["_bbox_spans"], all_spans),
                "descricao": desc,
            })

    _expandir_bboxes(lancamentos, doc)
    doc.close()
    return _montar(lancamentos)


# ══════════════════════════════════════════════════════════════════════════════
# EXTRATOR NUBANK
# ══════════════════════════════════════════════════════════════════════════════

def _extract_nubank(pdf_path: str) -> dict:
    sig      = ASSINATURAS["nubank"]
    DATA_RE  = re.compile(r"^\d{2}\s+\w{3}\s+\d{4}$", re.I)
    CABEC_RE = re.compile(r"^(Total de entradas|Total de saídas)$", re.I)

    doc = fitz.open(pdf_path)
    lancamentos = []
    data_atual  = None

    for pn in range(len(doc)):
        all_spans = _spans(doc[pn])
        linhas    = _agrupar_linhas(all_spans)

        sinal_atual    = +1
        ancoras_pagina = []   # índices globais das âncoras desta página

        for linha in linhas:
            # data de bloco
            data_s = next(
                (s for s in linha
                 if sig["bloco_x"][0] <= s["x0"] <= sig["bloco_x"][1]
                 and DATA_RE.match(s["text"])), None
            )
            if data_s:
                data_atual = data_s["text"]

            # cabeçalho de subbloco — declara sinal geometricamente
            cabec = next(
                (s for s in linha
                 if sig["ancora_x"][0] <= s["x0"] <= sig["ancora_x"][1]
                 and "Semibold" in s["font"]
                 and CABEC_RE.match(s["text"])), None
            )
            if cabec:
                sinal_atual = +1 if "entradas" in cabec["text"].lower() else -1
                continue

            # âncora de lançamento: Regular em x0≈120, sem número
            ancora = next(
                (s for s in linha
                 if sig["ancora_x"][0] <= s["x0"] <= sig["ancora_x"][1]
                 and "Regular" in s["font"]
                 and not DEC_RE.search(s["text"])), None
            )
            if not ancora:
                continue

            # valor: Regular, x0 > 490
            val_s = next(
                (s for s in linha
                 if s["x0"] >= sig["valor_x"][0]
                 and DEC_RE.search(s["text"])
                 and "Regular" in s["font"]), None
            )
            if not val_s:
                continue

            v = _extrair_valor(val_s["text"])
            if not v:
                continue

            dx0, dx1    = sig["descricao_x"]
            desc_inline = [s for s in linha if dx0 <= s["x0"] <= dx1 and not DEC_RE.search(s["text"])]
            idx_global  = len(lancamentos)

            lancamentos.append({
                "page"     : pn + 1,
                "date"     : data_atual,
                "entrada"  : round(v,  2) if sinal_atual == +1 else None,
                "saida"    : round(-v, 2) if sinal_atual == -1 else None,
                "bbox"     : _bbox_da_linha(linha),
                "descricao": None,
                "_y"       : linha[0]["y0"],
                "_frags"   : list(desc_inline),
                "_raw"     : ancora["text"][:80],
            })
            ancoras_pagina.append(idx_global)

        if not ancoras_pagina:
            continue

        # Voronoi direcional para fragmentos multilinha desta página
        ancora_ys    = [lancamentos[i]["_y"] for i in ancoras_pagina]
        ancora_y_set = set(ancora_ys)
        dx0, dx1     = sig["descricao_x"]

        desc_extra = [
            s for s in all_spans
            if dx0 <= s["x0"] <= dx1
            and not DEC_RE.search(s["text"])
            and "Regular" in s["font"]
            and s["y0"] not in ancora_y_set
        ]

        for ds in desc_extra:
            candidatos = []
            for idx_local, ay in enumerate(ancora_ys):
                dist = abs(ds["y0"] - ay)
                # abaixo da âncora: até gap_max
                if ds["y0"] >= ay and dist <= sig["descricao_gap_max"]:
                    candidatos.append((dist, idx_local))
                # acima da âncora: só se muito próximo (fragmento pré-âncora)
                elif ds["y0"] < ay and dist <= 12:
                    candidatos.append((dist, idx_local))
            if candidatos:
                melhor = min(candidatos, key=lambda x: x[0])
                lancamentos[ancoras_pagina[melhor[1]]]["_frags"].append(ds)

        # resolver descrições e bbox: primeiro→último elemento do lançamento
        for idx in ancoras_pagina:
            l = lancamentos[idx]
            l["descricao"] = " ".join(
                s["text"] for s in sorted(l["_frags"], key=lambda s: s["y0"])
            ) or l["_raw"]

            # âncora_spans: reconstrói a partir do bbox original (y0 da linha âncora)
            ancora_proxy = [{"y0": l["bbox"]["y0"], "y1": l["bbox"]["y1"],
                             "x0": l["bbox"]["x0"], "x1": l["bbox"]["x1"]}]
            l["bbox"] = _bbox_linhas_completas(ancora_proxy, l["_frags"], all_spans)

    _expandir_bboxes(lancamentos, doc)
    doc.close()
    return _montar(lancamentos)


# ══════════════════════════════════════════════════════════════════════════════
# EXTRATOR ITAÚ
# ══════════════════════════════════════════════════════════════════════════════

def _extract_itau(pdf_path: str) -> dict:
    sig     = ASSINATURAS["itau"]
    FILTRO  = re.compile("|".join(sig["filtro_descricao"]), re.I)
    DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")

    doc = fitz.open(pdf_path)
    lancamentos = []
    data_atual  = None

    for pn in range(len(doc)):
        all_spans = _spans(doc[pn])
        linhas    = _agrupar_linhas(all_spans)

        for linha in linhas:
            # data: x0≈30, Regular
            data_s = next(
                (s for s in linha
                 if sig["data_x"][0] <= s["x0"] <= sig["data_x"][1]
                 and DATE_RE.match(s["text"])), None
            )
            if data_s:
                data_atual = data_s["text"]

            # descrição: x0≈95, Regular, sem número
            dx0, dx1 = sig["descricao_x"]
            desc_s = next(
                (s for s in linha
                 if dx0 <= s["x0"] <= dx1
                 and "Regular" in s["font"]
                 and not DEC_RE.search(s["text"])), None
            )
            if not desc_s or FILTRO.search(desc_s["text"]):
                continue

            # valor: Bold, x0 > 390
            vx0, _ = sig["valor_x"]
            val_s = next(
                (s for s in linha
                 if "Bold" in s["font"]
                 and s["x0"] >= vx0
                 and DEC_RE.search(s["text"])), None
            )
            if not val_s:
                continue

            v = _extrair_valor(val_s["text"])
            if not v:
                continue

            sinal = -1 if val_s["text"].strip().startswith("-") else +1

            lancamentos.append({
                "page"     : pn + 1,
                "date"     : data_atual,
                "entrada"  : round(v,  2) if sinal == +1 else None,
                "saida"    : round(-v, 2) if sinal == -1 else None,
                "bbox"     : _bbox_da_linha(linha),
                "descricao": desc_s["text"],
            })

    _expandir_bboxes(lancamentos, doc)
    doc.close()
    return _montar(lancamentos)


# ══════════════════════════════════════════════════════════════════════════════
# EXTRATOR CAIXA
# ══════════════════════════════════════════════════════════════════════════════

def _extract_caixa(pdf_path: str) -> dict:
    sig     = ASSINATURAS["caixa"]
    DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
    VAL_CD  = re.compile(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*([CD])$")
    FILTRO  = re.compile(r"^(SALDO DIA|REM BASICA|CRED JUROS)$", re.I)

    doc = fitz.open(pdf_path)
    lancamentos = []

    for pn in range(len(doc)):
        all_spans = _spans(doc[pn])
        linhas    = _agrupar_linhas(all_spans)

        for linha in linhas:
            data_s = next(
                (s for s in linha
                 if sig["data_x"][0] <= s["x0"] <= sig["data_x"][1]
                 and DATE_RE.match(s["text"])), None
            )

            # filtrar linhas de saldo (nr_doc = 000000)
            nr_s = next(
                (s for s in linha
                 if sig["nr_doc_x"][0] <= s["x0"] <= sig["nr_doc_x"][1]), None
            )
            if nr_s and nr_s["text"] == sig["nr_doc_filtro"]:
                continue

            # descrição
            dx0, dx1 = sig["descricao_x"]
            desc_s = next(
                (s for s in linha
                 if dx0 <= s["x0"] <= dx1
                 and not DEC_RE.search(s["text"])), None
            )
            if not desc_s or FILTRO.search(desc_s["text"]):
                continue

            # valor com sufixo C/D
            vx0, vx1 = sig["valor_x"]
            val_s = next(
                (s for s in linha
                 if vx0 <= s["x0"] <= vx1
                 and VAL_CD.search(s["text"])), None
            )
            if not val_s:
                continue

            m     = VAL_CD.search(val_s["text"])
            v     = float(m.group(1).replace(".", "").replace(",", "."))
            sinal = +1 if m.group(2) == "C" else -1

            lancamentos.append({
                "page"     : pn + 1,
                "date"     : data_s["text"] if data_s else None,
                "entrada"  : round(v,  2) if sinal == +1 else None,
                "saida"    : round(-v, 2) if sinal == -1 else None,
                "bbox"     : _bbox_da_linha(linha),
                "descricao": desc_s["text"],
            })

    _expandir_bboxes(lancamentos, doc)
    doc.close()
    return _montar(lancamentos)


# ══════════════════════════════════════════════════════════════════════════════
# EXTRATOR BANCO DO BRASIL
# ══════════════════════════════════════════════════════════════════════════════

def _extract_bb(pdf_path: str) -> dict:
    """
    Estrutura: data x0=30 | lote x0=99 | doc x0=148 | histórico x0=265 | valor x0≈535 com (+/-)
    Âncora   : linha com data DD/MM/YYYY x0=30 + valor com sufixo (+) ou (-)
    Filtro   : Saldo do dia, BB Rende Fácil (doc=9903), Saldo Anterior, cabeçalho Histórico
    Descrição: Voronoi 1D — cada âncora define zona [mid_acima, mid_abaixo] e coleta
               todos os fragmentos em x0=265 que caem nessa zona, ordenados por y0.
    """
    FILTRO     = re.compile(
        r"^(saldo do dia|saldo anterior|bb rende f|rende facil|s\s*a\s*l\s*d\s*o|total aplic|histórico)",
        re.I,
    )
    FILTRO_DOC = {"9903"}
    DATE_RE    = re.compile(r"^\d{2}/\d{2}/\d{4}$")
    SINAL_RE   = re.compile(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*\(([+\-])\)")

    doc = fitz.open(pdf_path)
    lancamentos = []
    data_atual  = None

    for pn in range(len(doc)):
        all_spans = _spans(doc[pn])
        linhas    = _agrupar_linhas(all_spans)

        # ── 1. Identificar âncoras (linhas com data DD/MM/YYYY) ───────────────
        ancoras = []
        for linha in linhas:
            data_s = next(
                (s for s in linha if 25 <= s["x0"] <= 40 and DATE_RE.match(s["text"])),
                None,
            )
            if not data_s:
                continue

            doc_s       = next((s["text"] for s in linha if 143 <= s["x0"] <= 158), None)
            sinal_m     = SINAL_RE.search(" ".join(s["text"] for s in linha))
            hist_inline = next(
                (s for s in linha if 260 <= s["x0"] <= 275 and not DEC_RE.search(s["text"])),
                None,
            )

            ancoras.append({
                "y0"        : data_s["y0"],
                "date"      : data_s["text"],
                "doc"       : doc_s,
                "sinal_m"   : sinal_m,
                "hist_frags": [hist_inline] if hist_inline else [],
                "linha"     : linha,
                "page"      : pn + 1,
            })

        if not ancoras:
            continue

        # ── 2. Voronoi 1D: cada âncora define sua zona ────────────────────────
        n = len(ancoras)
        limites_sup = [0.0] * n
        limites_inf = [float("inf")] * n

        for i in range(n):
            if i > 0:
                mid = (ancoras[i - 1]["y0"] + ancoras[i]["y0"]) / 2
                limites_sup[i] = mid
                limites_inf[i - 1] = mid

        # ── 3. Coletar flutuantes e atribuir à zona correta ───────────────────
        hist_flutuantes = [
            next(s for s in linha if 260 <= s["x0"] <= 275)
            for linha in linhas
            if not any(25 <= s["x0"] <= 40 and DATE_RE.match(s["text"]) for s in linha)
            and any(260 <= s["x0"] <= 275 for s in linha)
        ]

        for fspan in hist_flutuantes:
            if FILTRO.search(fspan["text"]):
                continue
            for i, a in enumerate(ancoras):
                if limites_sup[i] <= fspan["y0"] <= limites_inf[i]:
                    a["hist_frags"].append(fspan)
                    break

        # ── 4. Montar lançamentos ─────────────────────────────────────────────
        for a in ancoras:
            txt_bloco = " ".join(s["text"] for s in a["linha"])

            if FILTRO.search(txt_bloco):
                continue
            if a["doc"] in FILTRO_DOC:
                continue
            if not a["sinal_m"]:
                continue

            m     = a["sinal_m"]
            v     = float(m.group(1).replace(".", "").replace(",", "."))
            sinal = +1 if m.group(2) == "+" else -1
            if v < 0.001:
                continue

            if a["date"]:
                data_atual = a["date"]

            frags_ord = sorted(a["hist_frags"], key=lambda s: s["y0"])
            descricao = " | ".join(
                s["text"] for s in frags_ord if not FILTRO.search(s["text"])
            ) or None

            bb_bbox = _bbox_da_linha(list(a["linha"]) + list(a["hist_frags"]))

            lancamentos.append({
                "page"     : a["page"],
                "date"     : data_atual,
                "entrada"  : round(v,  2) if sinal == +1 else None,
                "saida"    : round(-v, 2) if sinal == -1 else None,
                "bbox"     : bb_bbox,
                "descricao": descricao,
            })

    _expandir_bboxes(lancamentos, doc)
    doc.close()
    return _montar(lancamentos)


# ══════════════════════════════════════════════════════════════════════════════
# EXTRATOR SANTANDER
# ══════════════════════════════════════════════════════════════════════════════

def _extract_santander(pdf_path: str) -> dict:
    """
    Estrutura: data DD/MM x0=34 | descrição x0=65 | nr doc x0=309 | valor x0≈437-441
    Âncora   : linha com valor em x0=428-450 (ArialNarrow)
    Filtro   : seções fora de Movimentação ignoradas via flag em_movim persistente
    Data     : DD/MM sem ano — ano inferido do cabeçalho da primeira página
    Descrição: Voronoi 1D — cada âncora coleta flutuantes em x0=62-70 dentro de sua
               zona [mid_acima, mid_abaixo], ordenados por y0.
    """
    FILTRO = re.compile(
        r"^(saldo em|total de cr|total de d|dep[oó]sitos|outros cr|compras com|"
        r"pagamentos|provisão|saldo de conta|saldo anter|resgate cdb|aplicacao cdb|"
        r"vencimento cdb|mensalidade|limite sant|fale conosco|resumo|nome$|agência$|"
        r"conta corrente$|movimenta|data$|descrição$|nº documento|movimento|saldo \(|"
        r"saldo disp|período:|loja:|central|ouvidoria|libras|comprovantes|"
        r"contas de consumo|transferên|data canal|créditos contratados|limite da conta|"
        r"cet |saldos por período|compras com cartão|Renda Fixa|Aplicação|"
        r"Movimentação|Rendimento|CDB|Pacote|SERVICOS|Produtos|Valor da|Status|"
        r"Dia de|Índices|IBOVESPA|IGPM|INCC|INPC|IPCA|CDI|TR|POUPANCA|EURO|DOLAR|SALARIO)",
        re.I,
    )
    DATE_RE = re.compile(r"^\d{2}/\d{2}$")

    doc = fitz.open(pdf_path)

    # ── Inferir ano do cabeçalho (primeira página) ────────────────────────
    p0_text = doc[0].get_text()
    m_ano = re.search(
        r"(janeiro|fevereiro|mar[çc]o|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)/(\d{4})",
        p0_text, re.I,
    )
    ano_extrato = m_ano.group(2) if m_ano else "2025"

    lancamentos = []
    data_atual  = None
    em_movim    = False  # estado persistente entre páginas

    for pn in range(len(doc)):
        all_spans = _spans(doc[pn])
        linhas    = _agrupar_linhas(all_spans)

        # ── 1. Filtrar linhas dentro da seção Movimentação ────────────────
        linhas_movim = []
        for linha in linhas:
            txt = " ".join(s["text"] for s in linha)
            if "Movimentação" in txt or "SALDO EM" in txt:
                em_movim = True
            if any(x in txt for x in ["Renda Fixa", "Compras com Cartão de Débito",
                                       "Comprovantes", "Transferências entre"]):
                em_movim = False
            if em_movim:
                linhas_movim.append(linha)

        if not linhas_movim:
            continue

        # ── 2. Identificar âncoras ────────────────────────────────────────
        ancoras = []
        for linha in linhas_movim:
            val_s = next(
                (s for s in linha if 428 <= s["x0"] <= 450
                 and "Narrow" in s["font"] and DEC_RE.search(s["text"])),
                None,
            )
            if not val_s:
                continue

            data_s = next(
                (s for s in linha if 30 <= s["x0"] <= 42 and DATE_RE.match(s["text"])),
                None,
            )
            if data_s:
                data_atual = f"{data_s['text']}/{ano_extrato}"

            desc_s = next(
                (s for s in linha if 62 <= s["x0"] <= 70
                 and "Narrow" in s["font"] and not DEC_RE.search(s["text"])),
                None,
            )
            if not desc_s or FILTRO.search(desc_s["text"]):
                continue

            v = _extrair_valor(val_s["text"])
            if not v or v < 0.001:
                continue
            sinal = -1 if val_s["text"].rstrip().endswith("-") else +1

            ancoras.append({
                "y0"        : val_s["y0"],
                "date"      : data_atual,
                "desc_frags": [desc_s],
                "v"         : v,
                "sinal"     : sinal,
                "page"      : pn + 1,
                "linha"     : linha,
            })

        if not ancoras:
            continue

        # ── 3. Voronoi 1D: cada âncora define sua zona ────────────────────
        n = len(ancoras)
        limites_sup = [0.0] * n
        limites_inf = [float("inf")] * n
        for i in range(n):
            if i > 0:
                mid = (ancoras[i - 1]["y0"] + ancoras[i]["y0"]) / 2
                limites_sup[i] = mid
                limites_inf[i - 1] = mid

        # ── 4. Coletar flutuantes e atribuir à zona correta ───────────────
        flutuantes = [
            next(s for s in linha if 62 <= s["x0"] <= 70 and "Narrow" in s["font"])
            for linha in linhas_movim
            if not any(428 <= s["x0"] <= 450 and "Narrow" in s["font"] and DEC_RE.search(s["text"]) for s in linha)
            and any(62 <= s["x0"] <= 70 and "Narrow" in s["font"] and not DEC_RE.search(s["text"]) for s in linha)
        ]

        for fspan in flutuantes:
            if FILTRO.search(fspan["text"]):
                continue
            for i, a in enumerate(ancoras):
                if limites_sup[i] <= fspan["y0"] <= limites_inf[i]:
                    a["desc_frags"].append(fspan)
                    break

        # ── 5. Montar lançamentos ─────────────────────────────────────────
        for a in ancoras:
            frags_ord = sorted(a["desc_frags"], key=lambda s: s["y0"])
            descricao = " | ".join(
                s["text"] for s in frags_ord if not FILTRO.search(s["text"])
            ) or None

            base_bbox = _bbox_da_linha(list(a["linha"]) + list(a["desc_frags"]))

            lancamentos.append({
                "page"     : a["page"],
                "date"     : a["date"],
                "entrada"  : round(a["v"],  2) if a["sinal"] == +1 else None,
                "saida"    : round(-a["v"], 2) if a["sinal"] == -1 else None,
                "bbox"     : base_bbox,
                "descricao": descricao,
            })

    _expandir_bboxes(lancamentos, doc)
    doc.close()
    return _montar(lancamentos)


# ══════════════════════════════════════════════════════════════════════════════
# EXTRATOR UBER CONTA (DIGIO)
# ══════════════════════════════════════════════════════════════════════════════

def _extract_uber_digio(pdf_path: str) -> dict:
    """
    Estrutura: tipo x0=37 (Regular) | data x0=358 | valor x0=492
    Invariante: UberMoveText-Regular gap=23.8 uniforme
    Sinal    : prefixo "- R$" = débito; "R$" sem "- " = crédito
    Filtro   : font Medium/Bold = cabeçalhos e rodapés
    """
    FILTRO_FONT = re.compile(r"Medium|Bold", re.I)
    VALOR_RE    = re.compile(r"^(-\s*)?R\$\s*(\d{1,3}(?:\.\d{3})*,\d{2})$")
    DATE_RE     = re.compile(r"^\d{2}/\d{2}/\d{4}")
    FILTRO_TIPO = re.compile(
        r"^(Tipo|Descrição|Data do Lançamento|Valor|Saldo final|Saldo bloqueado|"
        r"Em \d|Entrou|Saiu|↑|↓|R\$\s*\d|Esse é|Exibindo|Olá|Atendimento|"
        r"Capitais|Demais|3004|0800|Extrato para|Total de|Saldo anterior)", re.I
    )

    doc = fitz.open(pdf_path)
    lancamentos = []

    for pn in range(len(doc)):
        all_spans = _spans(doc[pn])
        linhas    = _agrupar_linhas(all_spans)

        for linha in linhas:
            tipo_s = next(
                (s for s in linha if 33 <= s["x0"] <= 45
                 and not FILTRO_FONT.search(s["font"])
                 and not FILTRO_TIPO.search(s["text"])), None
            )
            if not tipo_s:
                continue

            data_s = next(
                (s for s in linha if 354 <= s["x0"] <= 365 and DATE_RE.match(s["text"])), None
            )
            if not data_s:
                continue

            val_s = next(
                (s for s in linha if 488 <= s["x0"] <= 500 and VALOR_RE.match(s["text"].strip())), None
            )
            if not val_s:
                continue

            m     = VALOR_RE.match(val_s["text"].strip())
            v     = float(m.group(2).replace(".", "").replace(",", "."))
            sinal = -1 if m.group(1) else +1

            lancamentos.append({
                "page"     : pn + 1,
                "date"     : data_s["text"][:10],
                "entrada"  : round(v,  2) if sinal == +1 else None,
                "saida"    : round(-v, 2) if sinal == -1 else None,
                "bbox"     : _bbox_da_linha(linha),
                "descricao": tipo_s["text"],
            })

    _expandir_bboxes(lancamentos, doc)
    doc.close()
    return _montar(lancamentos)


# ══════════════════════════════════════════════════════════════════════════════
# EXTRATOR BANCO INTER
# ══════════════════════════════════════════════════════════════════════════════

def _extract_inter(pdf_path: str) -> dict:
    """
    Estrutura: descrição x0=44 (FreeSans Regular) | valor x0>400 (FreeSansBold)
    Invariante: FreeSansBold x0=41 com data = fronteira de bloco
               FreeSans (Regular) x0=44 com tipo reconhecido = lançamento
    Sinal    : prefixo "-R$" = saída; "R$" sem "-" = entrada
    """
    FILTRO = re.compile(
        r"^(saldo do dia|saldo total|saldo disponível|saldo bloqueado|"
        r"período:|instituição:|cpf/cnpj|agência|conta:|banco inter|"
        r"fale com a gente|sac:|ouvidoria|deficiência|solicitado em|"
        r"total de entrada|total de saída|saldo final|valor$|saldo por transação)",
        re.I,
    )
    DATA_RE  = re.compile(r"^\d{1,2} de \w+ de \d{4}$", re.I)
    TIPO_RE  = re.compile(
        r"^(Pix enviado:|Pix recebido:|Compra no debito:|Pagamento efetuado:|Credito liberado:)",
        re.I,
    )
    VALOR_RE = re.compile(r"^(-)?R\$\s*(\d{1,3}(?:\.\d{3})*,\d{2})$")

    doc = fitz.open(pdf_path)
    lancamentos = []
    data_atual  = None

    for pn in range(len(doc)):
        all_spans = _spans(doc[pn])
        linhas    = _agrupar_linhas(all_spans)

        for linha in linhas:
            data_s = next(
                (s for s in linha if 38 <= s["x0"] <= 50
                 and "Bold" in s["font"] and DATA_RE.match(s["text"])), None
            )
            if data_s:
                data_atual = data_s["text"]

            txt = " ".join(s["text"] for s in linha)
            if FILTRO.search(txt):
                continue

            desc_s = next(
                (s for s in linha if 40 <= s["x0"] <= 52
                 and "Bold" not in s["font"] and TIPO_RE.match(s["text"])), None
            )
            if not desc_s:
                continue

            val_s = next(
                (s for s in linha if s["x0"] > 400
                 and "Bold" in s["font"] and VALOR_RE.match(s["text"].strip())), None
            )
            if not val_s:
                continue

            m     = VALOR_RE.match(val_s["text"].strip())
            v     = float(m.group(2).replace(".", "").replace(",", "."))
            sinal = -1 if m.group(1) else +1
            if v < 0.001:
                continue

            lancamentos.append({
                "page"     : pn + 1,
                "date"     : data_atual,
                "entrada"  : round(v,  2) if sinal == +1 else None,
                "saida"    : round(-v, 2) if sinal == -1 else None,
                "bbox"     : _bbox_da_linha(linha),
                "descricao": desc_s["text"],
            })

    _expandir_bboxes(lancamentos, doc)
    doc.close()
    return _montar(lancamentos)


# ══════════════════════════════════════════════════════════════════════════════
# EXTRATOR BRADESCO
# ══════════════════════════════════════════════════════════════════════════════

def _extract_bradesco(pdf_path: str) -> dict:
    """
    Estrutura (Bradesco Internet Banking → PDF):
      Font    : Tahoma Regular (lançamentos) | Tahoma-Bold (saldos — ignorar)
      Colunas : data x0≈47 (DD/MM/YY) | tipo x0≈86 | docto x0≈212
                crédito x0≈315-345 | débito x0≈408-438 (prefixo "- ") | saldo x0≈513 (Bold)
      Bloco   : cada lançamento ocupa 2-3 linhas verticais:
                  linha A: tipo ("Visa Electron", "Transfe Pix"…)          x0≈86
                  linha B: docto + valor (crédito OU débito)               ← âncora
                  linha C: complemento (estabelecimento / remetente)       x0≈86
      Data    : DD/MM/YY — aparece na linha B da 1ª transação do dia
      Sinal   : presença do span de débito (prefixo "- ") = saída; crédito = entrada
      Filtro  : Tahoma-Bold (saldos), cabeçalhos textuais, rodapés
    """
    sig = ASSINATURAS["bradesco"]

    FILTRO = re.compile(
        r"^(SALDO ANTERIOR|Total|Histórico|Data|Docto\.|Crédito \(R\$\)|Débito \(R\$\)|"
        r"Saldo \(R\$\)|Bradesco Internet Banking|Nome:|Extrato de:|Últimos Lançamentos|"
        r"Saldos Invest Fácil|Não há lançamentos|Os dados acima|Fone Fácil|"
        r"Capitais e Regiões|Demais Regiões|SAC|Ouvidoria|Cancelamento|Atendimento|"
        r"Se preferir|Comprovantes|CNPJ n|Av\. das|Encontre nossos|Data de geração|"
        r"Ag:|Conta:)",
        re.I,
    )
    DATE_RE  = re.compile(r"^\d{2}/\d{2}/\d{2}$")
    DOCTO_RE = re.compile(r"^\d{7}$")
    VALOR_RE = re.compile(r"^-?\s*\d{1,3}(?:\.\d{3})*,\d{2}$")

    doc = fitz.open(pdf_path)
    lancamentos = []
    data_atual  = None

    for pn in range(len(doc)):
        all_spans = _spans(doc[pn])
        linhas    = _agrupar_linhas(all_spans)

        # Pré-indexar spans por y0 para lookup de complemento
        spans_by_y: dict[float, list] = {}
        for s in all_spans:
            spans_by_y.setdefault(round(s["y0"], 1), []).append(s)

        # Rastrear última linha-tipo válida (linha A do bloco) — linha completa para bbox
        ultima_tipo: Optional[str] = None
        ultima_tipo_linha: Optional[list] = None

        for i, linha in enumerate(linhas):
            # Capturar data: x0≈47, formato DD/MM/YY
            data_s = next(
                (s for s in linha
                 if sig["data_x"][0] <= s["x0"] <= sig["data_x"][1]
                 and DATE_RE.match(s["text"])), None
            )
            if data_s:
                data_atual = data_s["text"]

            # Linha A — tipo: x0≈86, Tahoma Regular, sem número monetário
            tipo_s = next(
                (s for s in linha
                 if sig["tipo_x"][0] <= s["x0"] <= sig["tipo_x"][1]
                 and sig["saldo_font"] not in s["font"]
                 and not DEC_RE.search(s["text"])
                 and not FILTRO.search(s["text"])), None
            )
            if tipo_s:
                ultima_tipo       = tipo_s["text"]
                ultima_tipo_linha = linha  # guardar linha inteira para bbox

            # Linha B — âncora: presença de docto (x0≈212, 7 dígitos)
            docto_s = next(
                (s for s in linha
                 if sig["docto_x"][0] <= s["x0"] <= sig["docto_x"][1]
                 and DOCTO_RE.match(s["text"])), None
            )
            if not docto_s:
                continue

            # Valor débito: x0≈408-438, prefixo "- "
            debito_s = next(
                (s for s in linha
                 if sig["debito_x"][0] <= s["x0"] <= sig["debito_x"][1]
                 and VALOR_RE.match(s["text"].replace(" ", ""))
                 and s["text"].strip().startswith("-")), None
            )
            # Valor crédito: x0≈312-348, sem prefixo "-"
            credito_s = next(
                (s for s in linha
                 if sig["credito_x"][0] <= s["x0"] <= sig["credito_x"][1]
                 and VALOR_RE.match(s["text"].strip())
                 and not s["text"].strip().startswith("-")), None
            )

            if not debito_s and not credito_s:
                continue

            v = _extrair_valor((debito_s or credito_s)["text"])
            if not v or v < 0.001:
                continue

            sinal = -1 if debito_s else +1

            # Linha C — complemento: próxima linha com span em x0≈86, sem número
            complemento: Optional[str] = None
            comp_s = None
            if i + 1 < len(linhas):
                prox = linhas[i + 1]
                comp_s = next(
                    (s for s in prox
                     if sig["complemento_x"][0] <= s["x0"] <= sig["complemento_x"][1]
                     and sig["saldo_font"] not in s["font"]
                     and not DEC_RE.search(s["text"])
                     and not FILTRO.search(s["text"])), None
                )
                if comp_s:
                    complemento = comp_s["text"]

            # Só aceitar ultima_tipo_linha se estiver genuinamente acima desta âncora
            # (tipo fica ~6pt acima; comp vazado do lançamento anterior fica ~28pt acima)
            tipo_valido = (
                ultima_tipo_linha is not None
                and docto_s["y0"] - ultima_tipo_linha[0]["y0"] <= 15
            )

            descricao = " | ".join(filter(None, [
                ultima_tipo if tipo_valido else None,
                complemento,
            ])) or None

            # Bbox = união de todos os spans do bloco: A (tipo) + B (anchor) + C (comp)
            bloco_spans = list(ultima_tipo_linha if tipo_valido else []) + list(linha)
            if comp_s:
                bloco_spans.extend(linhas[i + 1])
            anchor_bbox = _bbox_da_linha(bloco_spans)

            # Consumir linha-tipo para não vazar para a próxima âncora
            ultima_tipo_linha = None

            lancamentos.append({
                "page"     : pn + 1,
                "date"     : data_atual,
                "entrada"  : round(v,  2) if sinal == +1 else None,
                "saida"    : round(-v, 2) if sinal == -1 else None,
                "bbox"     : anchor_bbox,
                "descricao": descricao,
            })

    _expandir_bboxes(lancamentos, doc)
    doc.close()
    return _montar(lancamentos)


# ══════════════════════════════════════════════════════════════════════════════
# EXTRATOR MERCADO PAGO
# ══════════════════════════════════════════════════════════════════════════════

def _extract_mercadopago(pdf_path: str) -> dict:
    """
    Estrutura (Mercado Pago — extrato PDF):
      Font    : ProximaNova-Regular (lançamentos) | font="" vazio (cabeçalhos)
      Colunas : data x0≈40 (DD-MM-YYYY) | descrição x0≈88 | ID op. x0≈197 | valor x0≈290-315
      Âncora  : linha com data + ID numérico (12-15 dígitos) + "R$ [±]valor"
      Descrição: spans ProximaNova-Regular em x0≈88, dentro de ±30pt da âncora
                 linha 1 = tipo ("Transferência Pix recebida", "Pagamento com QR Pix …")
                 linhas 2-N = nome do favorecido/estabelecimento
      Sinal   : "R$ -X,XX" = saída; "R$ X,XX" (sem menos) = entrada
      Rendimentos: font="" em x0≈40, descrição "Rendimentos" — tratado como entrada
    """
    sig = ASSINATURAS["mercadopago"]

    FILTRO = re.compile(
        r"^(Data|Descrição|ID da operação|Valor|Saldo|Entradas:|Saidas:|"
        r"Saldo inicial:|Saldo final:|DETALHE DOS MOVIMENTOS|EXTRATO DE CONTA|"
        r"CPF/CNPJ:|Agência:|Conta:|Periodo:|Você tem alguma dúvida|"
        r"Mercado Pago|SAC|Ouvidoria|Data de geração)",
        re.I,
    )
    DATE_RE  = re.compile(r"^\d{2}-\d{2}-\d{4}$")
    VALOR_RE = re.compile(r"^R\$\s*(-)?(\d{1,3}(?:\.\d{3})*,\d{2})$")
    ID_RE    = re.compile(r"^\d{12,15}$")

    doc = fitz.open(pdf_path)
    lancamentos = []

    for pn in range(len(doc)):
        all_spans = _spans(doc[pn])
        linhas    = _agrupar_linhas(all_spans)

        # Pré-indexar spans de descrição (ProximaNova-Regular, x0≈88)
        desc_spans = [
            s for s in all_spans
            if sig["descricao_x"][0] <= s["x0"] <= sig["descricao_x"][1]
            and s["font"] == sig["font_lancamento"]
            and not DEC_RE.search(s["text"])
            and not FILTRO.search(s["text"])
        ]

        # ── Passo 1: coletar âncoras da página ───────────────────────────────
        ancoras_pag = []
        for linha in linhas:
            data_s = next(
                (s for s in linha
                 if sig["data_x"][0] <= s["x0"] <= sig["data_x"][1]
                 and DATE_RE.match(s["text"])), None
            )
            if not data_s:
                continue

            id_s = next(
                (s for s in linha
                 if sig["id_x"][0] <= s["x0"] <= sig["id_x"][1]
                 and ID_RE.match(s["text"])), None
            )
            if not id_s:
                continue

            val_s = next(
                (s for s in linha
                 if sig["valor_x"][0] <= s["x0"] <= sig["valor_x"][1]
                 and VALOR_RE.match(s["text"].strip())), None
            )
            if not val_s:
                continue

            m = VALOR_RE.match(val_s["text"].strip())
            v = float(m.group(2).replace(".", "").replace(",", "."))
            if v < 0.001:
                continue

            ancoras_pag.append({
                "y0"   : data_s["y0"],
                "v"    : v,
                "sinal": -1 if m.group(1) else +1,
                "date" : data_s["text"],
                "linha": linha,
                "page" : pn + 1,
                "frags": [],
            })

        if not ancoras_pag:
            continue

        # ── Passo 2: Voronoi 1D — cada âncora recebe os desc_spans da sua zona ──
        n_mp = len(ancoras_pag)
        lim_sup = [0.0] * n_mp
        lim_inf = [float("inf")] * n_mp
        for i in range(n_mp):
            if i > 0:
                mid = (ancoras_pag[i - 1]["y0"] + ancoras_pag[i]["y0"]) / 2
                lim_sup[i] = mid
                lim_inf[i - 1] = mid

        for ds in desc_spans:
            for i, a in enumerate(ancoras_pag):
                if lim_sup[i] <= ds["y0"] <= lim_inf[i]:
                    a["frags"].append(ds)
                    break

        # ── Passo 3: montar lançamentos ───────────────────────────────────────
        for a in ancoras_pag:
            frags_ord = sorted(a["frags"], key=lambda s: s["y0"])
            descricao = " ".join(s["text"] for s in frags_ord) or None
            lancamentos.append({
                "page"     : a["page"],
                "date"     : a["date"],
                "entrada"  : round(a["v"],  2) if a["sinal"] == +1 else None,
                "saida"    : round(-a["v"], 2) if a["sinal"] == -1 else None,
                "bbox"     : _bbox_da_linha(a["linha"] + a["frags"]),
                "descricao": descricao,
            })

    _expandir_bboxes(lancamentos, doc)
    doc.close()
    return _montar(lancamentos)


# ══════════════════════════════════════════════════════════════════════════════
# CATÁLOGO E PONTO DE ENTRADA PÚBLICO
# ══════════════════════════════════════════════════════════════════════════════

BANCOS_SUPORTADOS: dict[str, dict] = {
    "picpay"      : {"fn": _extract_picpay,      "label": "PicPay"},
    "nubank"      : {"fn": _extract_nubank,      "label": "Nubank"},
    "itau"        : {"fn": _extract_itau,        "label": "Itaú"},
    "caixa"       : {"fn": _extract_caixa,       "label": "Caixa Econômica Federal"},
    "bb"          : {"fn": _extract_bb,          "label": "Banco do Brasil"},
    "santander"   : {"fn": _extract_santander,   "label": "Santander"},
    "uber_digio"  : {"fn": _extract_uber_digio,  "label": "Uber Conta (Digio)"},
    "inter"       : {"fn": _extract_inter,       "label": "Banco Inter"},
    "bradesco"    : {"fn": _extract_bradesco,    "label": "Bradesco"},
    "mercadopago" : {"fn": _extract_mercadopago, "label": "Mercado Pago"},
}


def extract(pdf_path: str, banco: str) -> dict:
    """
    Ponto de entrada público.

    Args:
        pdf_path : caminho para o PDF do extrato
        banco    : identificador do banco — ver list_bancos() para opções

    Returns:
        dict com 'lancamentos' e totais calculados

    Raises:
        ValueError: banco não suportado
    """
    key   = banco.lower().strip()
    entry = BANCOS_SUPORTADOS.get(key)
    if not entry:
        raise ValueError(
            f"Banco '{banco}' não suportado. "
            f"Disponíveis: {sorted(BANCOS_SUPORTADOS)}"
        )
    return entry["fn"](pdf_path)


def list_bancos() -> list[dict]:
    """Retorna lista de bancos suportados com chave e rótulo."""
    return [{"key": k, "label": v["label"]} for k, v in BANCOS_SUPORTADOS.items()]


# ══════════════════════════════════════════════════════════════════════════════
# SEÇÃO COMPAT — extract_with_signature (portado do bancario v1)
# ══════════════════════════════════════════════════════════════════════════════

def _inferir_sinal_generico(text: str, signal_logic: str) -> int:
    if signal_logic == "prefix_minus":
        return -1 if text.startswith("-") else +1
    if signal_logic == "suffix_minus":
        return -1 if text.rstrip().endswith("-") else +1
    if signal_logic == "suffix_cd":
        return +1 if text.rstrip().endswith("C") else -1
    if signal_logic == "embedded_plus_minus":
        m = re.search(r"([+−\-])R\$", text)
        return -1 if m and m.group(1) in ("-", "−") else +1
    if signal_logic == "suffix_parenthesis":
        return +1 if re.search(r"\(\+\)$", text) else -1
    return +1


def extract_with_signature(pdf_path: str, sig: dict) -> dict:
    """
    Extrai lançamentos usando assinatura definida pelo usuário.

    Suporta entradas multiline: linhas sem âncora/valor que estão entre duas
    linhas-âncora são tratadas como continuação do lançamento acima (Voronoi 1D).
    Os campos nomeados (campos_def) têm seus textos concatenados entre a linha
    âncora e todas as linhas de continuação associadas.
    """
    import json as _json

    try:
        anchor_re = re.compile(sig["anchor_pattern"])
    except re.error:
        anchor_re = re.compile(r".+")

    ax_min = float(sig["anchor_x_min"])
    ax_max = float(sig["anchor_x_max"])
    vx_min = float(sig["value_x_min"])
    vx_max = float(sig["value_x_max"])
    logic  = sig.get("signal_logic", "prefix_minus")

    # Campos nomeados pelo usuário — lista de {label, x_min, x_max}
    campos_def = sig.get("campos") or []
    if isinstance(campos_def, str):
        campos_def = _json.loads(campos_def)

    DATE_RE = re.compile(
        r"^\d{1,2}[-/]\d{2}[-/]\d{2,4}$|^\d{1,2}\s+de\s+\w+\s+de\s+\d{4}$", re.I
    )

    doc = fitz.open(pdf_path)
    lancamentos = []
    data_atual  = None

    for pn in range(len(doc)):
        all_page_spans = _spans(doc[pn])
        linhas = _agrupar_linhas(all_page_spans)
        ritmo_pag = _ritmo_dominante(linhas)

        # ── Passo 1: identificar linhas-âncora (têm âncora + valor) ──────────
        ancoras_pag: list[dict] = []
        for i, linha in enumerate(linhas):
            # Atualizar data_atual a partir de qualquer linha
            data_s = next((s for s in linha if DATE_RE.match(s["text"].strip())), None)
            if data_s:
                data_atual = data_s["text"]

            ancora = next(
                (s for s in linha if ax_min <= s["x0"] <= ax_max and anchor_re.match(s["text"])),
                None,
            )
            if not ancora:
                continue

            val_s = next(
                (s for s in linha if vx_min <= s["x0"] <= vx_max and DEC_RE.search(s["text"])),
                None,
            )
            if not val_s:
                continue

            m = DEC_RE.search(val_s["text"])
            if not m:
                continue

            v = float(m.group().replace(".", "").replace(",", "."))
            sinal = _inferir_sinal_generico(val_s["text"], logic)
            if v < 0.001:
                continue

            ancoras_pag.append({
                "i"           : i,
                "linha"       : linha,
                "ancora"      : ancora,
                "v"           : v,
                "sinal"       : sinal,
                "date"        : data_atual,
                "y0"          : ancora["y0"],
                "extra_linhas": [],  # linhas de continuação (multiline)
            })

        if not ancoras_pag:
            continue

        # ── Passo 2: Voronoi 1D — atribuir linhas de continuação à âncora mais próxima ──
        # Uma linha de continuação é qualquer linha que:
        #   (a) não é uma linha-âncora
        #   (b) está abaixo da âncora e acima da próxima âncora (zona Voronoi)
        indices_ancora = {a["i"] for a in ancoras_pag}
        n = len(ancoras_pag)
        # limite superior de cada âncora = ponto médio com a âncora anterior
        lim_inf = [float("inf")] * n
        for i in range(1, n):
            mid = (ancoras_pag[i - 1]["y0"] + ancoras_pag[i]["y0"]) / 2
            lim_inf[i - 1] = mid

        for j, linha in enumerate(linhas):
            if j in indices_ancora:
                continue
            y = linha[0]["y0"]
            for i, a in enumerate(ancoras_pag):
                if a["y0"] < y <= lim_inf[i]:
                    a["extra_linhas"].append(linha)
                    break

        # ── Passo 3: montar lançamentos ───────────────────────────────────────
        for a in ancoras_pag:
            todas_linhas = [a["linha"]] + a["extra_linhas"]

            # Coleta valores dos campos nomeados concatenando linhas de continuação
            campos_vals: dict | None = None
            if campos_def:
                campos_vals = {}
                for campo in campos_def:
                    cx_min = float(campo["x_min"])
                    cx_max = float(campo["x_max"])
                    label  = campo["label"]
                    partes: list[str] = []
                    for linha in todas_linhas:
                        span = next(
                            (s for s in linha
                             if cx_min <= s["x0"] <= cx_max and not DEC_RE.search(s["text"])),
                            None,
                        )
                        if span:
                            partes.append(span["text"])
                    if partes:
                        campos_vals[label] = " ".join(partes)

            # Descrição: usa campos nomeados se disponíveis; senão coleta texto
            # não-âncora e não-valor da linha principal (útil no preview antes de nomear)
            if campos_vals:
                descricao = " · ".join(v for v in campos_vals.values() if v)
            else:
                descricao = " ".join(
                    s["text"] for s in a["linha"]
                    if not DEC_RE.search(s["text"])
                    and not (ax_min <= s["x0"] <= ax_max and anchor_re.match(s["text"]))
                    and not (vx_min <= s["x0"] <= vx_max)
                ).strip() or a["ancora"]["text"]

            # Bbox cobre linha-âncora + linhas de continuação próximas,
            # clampado horizontalmente ao retângulo original do usuário (rect_x0/x1)
            # quando disponível, ou ao range âncora→valor como fallback.
            # Usa delta_bbox (≈ 1 ritmo) para excluir separadores de seção e
            # cabeçalhos de data que caem na zona Voronoi mas não pertencem ao lançamento.
            delta_bbox = ritmo_pag + 5
            linhas_bbox = [a["linha"]] + [
                l for l in a["extra_linhas"]
                if l and (l[0]["y0"] - a["y0"]) <= delta_bbox
            ]
            todos_spans = [s for l in linhas_bbox for s in l]
            raw_bbox = _bbox_da_linha(todos_spans)
            clamp_x0 = float(sig.get("rect_x0") or ax_min - 5)
            clamp_x1 = float(sig.get("rect_x1") or vx_max + 5)
            bbox = {
                "x0": max(raw_bbox["x0"], clamp_x0),
                "y0": raw_bbox["y0"],
                "x1": min(raw_bbox["x1"], clamp_x1),
                "y1": raw_bbox["y1"],
            }
            lancamento: dict = {
                "page"     : pn + 1,
                "date"     : a["date"],
                "entrada"  : round(a["v"],  2) if a["sinal"] == +1 else None,
                "saida"    : round(-a["v"], 2) if a["sinal"] == -1 else None,
                "bbox"     : bbox,
                "descricao": descricao,
            }
            if campos_vals:
                lancamento["campos"] = campos_vals

            lancamentos.append(lancamento)

    _expandir_bboxes(lancamentos, doc)
    doc.close()
    return _montar(lancamentos)
