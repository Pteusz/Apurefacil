"""
modules/autodetect.py
=====================
Auto-detecção de banco baseada em catálogo de assinaturas.

Uma assinatura codifica as 5 variáveis geométricas/tipográficas que
identificam o layout de um extrato bancário:
    font_hints      : substrings da fonte dominante (case-insensitive)
    anchor_x_range  : faixa horizontal do elemento âncora
    anchor_re       : regex do padrão esperado na âncora
    value_x_range   : faixa horizontal do valor monetário
    value_pattern   : regex adicional no valor (opcional — ex: C/D da Caixa)
    desc_x_range    : faixa de descrição como discriminador extra (opcional)

Ponto de entrada público:
    detect(pdf_path)            → str | None
    detect_multi(pdf_paths)     → list[str | None]
"""

import re
import fitz
from typing import Optional

DEC_RE = re.compile(r'\d{1,3}(?:\.\d{3})*,\d{2}')

# ── Catálogo vanilla (derivado dos extratores em extrator_bancario.py) ────────

CATALOG: dict[str, dict] = {
    "nubank": {
        "label"         : "Nubank",
        "font_hints"    : ["graphik"],
        "anchor_x_range": (110.0, 140.0),
        "anchor_re"     : re.compile(
            r"^(transferência|compra|pagamento|saque|rendimento)", re.I
        ),
        "value_x_range" : (480.0, 530.0),
    },
    "bradesco": {
        "label"         : "Bradesco",
        "font_hints"    : [],
        "anchor_x_range": (200.0, 230.0),
        "anchor_re"     : re.compile(r"^\d{6,7}$"),
        "value_x_range" : (300.0, 450.0),
    },
    "picpay": {
        "label"         : "PicPay",
        "font_hints"    : ["aeonik"],
        "anchor_x_range": (35.0, 60.0),
        "anchor_re"     : re.compile(r"^\d{2}:\d{2}$"),
        "value_x_range" : (100.0, 450.0),
    },
    "bb": {
        "label"         : "Banco do Brasil",
        "font_hints"    : [],
        "anchor_x_range": (20.0, 50.0),
        "anchor_re"     : re.compile(r"^\d{2}/\d{2}/\d{4}$"),
        "value_x_range" : (300.0, 500.0),
        # sufixo (+) ou (-) distingue BB de Itaú
        "value_pattern" : re.compile(r"\d{1,3}(?:\.\d{3})*,\d{2}\s*\([+\-]\)"),
    },
    "itau": {
        "label"         : "Itaú",
        "font_hints"    : [],
        "anchor_x_range": (25.0, 40.0),
        "anchor_re"     : re.compile(r"^\d{2}/\d{2}/\d{4}$"),
        "value_x_range" : (200.0, 550.0),
        # descrição em x≈88-110 distingue Itaú do BB
        "desc_x_range"  : (88.0, 110.0),
    },
    "santander": {
        "label"         : "Santander",
        "font_hints"    : ["arialnarrow", "arial narrow"],
        "anchor_x_range": (58.0, 75.0),
        "anchor_re"     : re.compile(r"^[A-Za-z]{3,}"),
        "value_x_range" : (400.0, 490.0),
    },
    "caixa": {
        "label"         : "Caixa Econômica Federal",
        "font_hints"    : [],
        "anchor_x_range": (155.0, 180.0),
        "anchor_re"     : re.compile(r"^[A-Z]"),
        "value_x_range" : (430.0, 460.0),
        # sufixo C ou D distingue Caixa de outros
        "value_pattern" : re.compile(r"\d{1,3}(?:\.\d{3})*,\d{2}\s*[CD]$"),
    },
    "mercadopago": {
        "label"         : "Mercado Pago",
        "font_hints"    : [],
        "anchor_x_range": (35.0, 50.0),
        # hífens no formato DD-MM-YYYY distinguem MP de outros bancos com data
        "anchor_re"     : re.compile(r"^\d{2}-\d{2}-\d{4}$"),
        "value_x_range" : (285.0, 325.0),
    },
    "uber_digio": {
        "label"         : "Uber Conta (Digio)",
        "font_hints"    : [],
        # data+hora fica à direita da página (x≈358) — muito discriminante
        "anchor_x_range": (350.0, 370.0),
        "anchor_re"     : re.compile(r"^\d{2}/\d{2}/\d{4}"),
        "value_x_range" : (485.0, 510.0),
    },
    "inter": {
        "label"         : "Banco Inter",
        "font_hints"    : [],
        "anchor_x_range": (40.0, 52.0),
        "anchor_re"     : re.compile(
            r"^(Pix enviado:|Pix recebido:|Compra no debito:|"
            r"Pagamento efetuado:|Credito liberado:)",
            re.I,
        ),
        "value_x_range" : (415.0, 435.0),
    },
}

SCORE_THRESHOLD = 2   # pontuação mínima para aceitar um match


# ── Extração de spans para detecção ──────────────────────────────────────────

def _spans_para_deteccao(pdf_path: str, max_pages: int = 2) -> list[dict]:
    """Extrai spans das primeiras N páginas para análise de assinatura."""
    doc = fitz.open(pdf_path)
    spans: list[dict] = []
    for pn in range(min(max_pages, len(doc))):
        raw = doc[pn].get_text("dict")
        for block in raw["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    t = span["text"].strip()
                    if t:
                        spans.append({
                            "text": t,
                            "x0"  : round(span["bbox"][0], 1),
                            "font": span["font"].lower(),
                            "hv"  : bool(DEC_RE.search(t)),
                        })
    doc.close()
    return spans


# ── Pontuação de assinatura ───────────────────────────────────────────────────

def _pontuar(spans: list[dict], sig: dict) -> int:
    """
    Pontua o quanto um conjunto de spans se encaixa numa assinatura.
    Critérios:
      +2  se ≥ 2 spans âncora na faixa correta (muito discriminante)
      +1  se exatamente 1 span âncora
      +1  se ≥ 2 valores monetários na faixa de valor
      +1  se value_pattern extra bate
      +1  se font_hints bate em pelo menos um span
      +1  se desc_x_range tem descrições (distingue Itaú do BB)
    """
    score = 0
    ax0, ax1 = sig["anchor_x_range"]
    vx0, vx1 = sig["value_x_range"]

    anchors = [s for s in spans if ax0 <= s["x0"] <= ax1 and sig["anchor_re"].match(s["text"])]
    if len(anchors) >= 2:
        score += 2
    elif len(anchors) == 1:
        score += 1

    values = [s for s in spans if vx0 <= s["x0"] <= vx1 and s["hv"]]
    if len(values) >= 2:
        score += 1

    if "value_pattern" in sig:
        if any(sig["value_pattern"].search(s["text"]) for s in values):
            score += 1

    if sig.get("font_hints"):
        if any(h in s["font"] for h in sig["font_hints"] for s in spans):
            score += 1

    if "desc_x_range" in sig:
        dx0, dx1 = sig["desc_x_range"]
        descs = [s for s in spans if dx0 <= s["x0"] <= dx1 and not s["hv"] and len(s["text"]) > 2]
        if len(descs) >= 2:
            score += 1

    return score


# ── API pública ───────────────────────────────────────────────────────────────

def detect(pdf_path: str, user_signatures: list[dict] | None = None) -> Optional[str]:
    """
    Detecta o banco do extrato.

    1. Testa o catálogo vanilla
    2. Se `user_signatures` for fornecida, testa também as assinaturas do usuário
       (identificadas pelo campo 'id' em vez de banco_key)

    Retorna o banco_key (ex: "nubank") ou o id da assinatura do usuário,
    ou None se não encontrar match com pontuação suficiente.
    """
    try:
        spans = _spans_para_deteccao(pdf_path)
    except Exception:
        return None

    scores: dict[str, int] = {}

    # Testa catálogo vanilla
    for banco_key, sig in CATALOG.items():
        scores[banco_key] = _pontuar(spans, sig)

    # Testa assinaturas do usuário (formato diferente — usa os campos da tabela)
    if user_signatures:
        for assin in user_signatures:
            sig = _assinatura_db_para_sig(assin)
            scores[f"user:{assin['id']}"] = _pontuar(spans, sig)

    if not scores:
        return None

    best_key   = max(scores, key=lambda k: scores[k])
    best_score = scores[best_key]

    if best_score < SCORE_THRESHOLD:
        return None

    return best_key


def detect_multi(
    pdf_paths: list[str],
    user_signatures: list[dict] | None = None,
) -> list[Optional[str]]:
    """Detecta banco para cada PDF. Retorna lista na mesma ordem."""
    return [detect(p, user_signatures) for p in pdf_paths]


def _assinatura_db_para_sig(row: dict) -> dict:
    """Converte linha da tabela `assinaturas` no formato usado por _pontuar()."""
    return {
        "font_hints"    : [row["font_dominant"].lower()] if row.get("font_dominant") else [],
        "anchor_x_range": (float(row["anchor_x_min"]), float(row["anchor_x_max"])),
        "anchor_re"     : re.compile(row["anchor_pattern"]),
        "value_x_range" : (float(row["value_x_min"]), float(row["value_x_max"])),
    }


def label_para_banco(banco_key: str) -> str:
    """Retorna o nome amigável de um banco_key do catálogo vanilla."""
    if banco_key in CATALOG:
        return CATALOG[banco_key]["label"]
    return banco_key
