"""
modules/scanner.py — Renda Scanner v7.0 (chord-based algorithm)

Troca a arquitetura de 6-passes por detecção de lançamentos via chord fingerprinting,
substituindo pdfplumber por PyMuPDF (fitz) para extração posicional mais precisa.

Conceito de chord:
  Cada linha vira uma tupla de (x_bucket, peso_tipográfico, tem_valor).
  Lançamentos de um extrato têm o mesmo chord, descoberto por auto-repetição.

Fixes incorporados:
  FIX 1 — gap_dom * 2.5 para parar absorção de continuações em saltos de seção
  FIX 2 — decimal BR sempre com vírgula (invariante: decimal = vírgula)
  FIX 3 — peeling de data layer DD/MM antes de construir chord

Classificação n_val:
  lancamento     — 0 ou 1 valor na linha
  saldo_embutido — 2 valores: esquerda = transação, direita = saldo (invariante)
  metadata       — saldo_embutido com valor == 0 ou texto reconhecível
  descartado     — mais de 2 valores (tabelas de índice etc.)

Saída de extract_all_pages():
  {
    'total_lancamentos': int,
    'total_entradas':    float,
    'total_saidas':      float,
    'saldo':             float,
    'lancamentos':       List[Dict],
  }

Cada lançamento:
  {
    'page':       int,
    'zone':       None,
    'date':       str | None,
    'descricao':  str | None,
    'entrada':    float | None,
    'saida':      float | None,
    'saldo_apos': float | None,
    'bbox':       {'x0', 'y0', 'x1', 'y1'} | None,
    '_raw':       List[str],
  }
"""

import re
from collections import Counter
from typing import Dict, List, Optional

import fitz

# ── Regex ─────────────────────────────────────────────────────────────────────

# FIX 2: decimal BR sempre com vírgula
DEC_RE = re.compile(r'\d{1,3}(?:\.\d{3})*,\d{2}')

# FIX 3: âncora de dia DD/MM — pertence à camada de agrupamento, não à transação
DATE_ANCHOR_RE = re.compile(r'^\d{1,2}/\d{2}$')

# Data completa para herança de data entre lançamentos
DATE_RE = re.compile(
    r'^\d{1,2}\s+(?:de\s+)?[A-Za-zÀ-ɏ]{3,}(?:\s+de)?\s+\d{4}$'
    r'|^\d{1,2}/\d{2}/\d{4}$'
    r'|^\d{2}/\d{2}$',
    re.I,
)

# Sinal por contexto semântico
_SAIDA_RE = re.compile(
    r'enviado|enviada|pagamento|debito|débito|saque|compra|iof|tarifa|remetente',
    re.I,
)
_ENTRADA_RE = re.compile(
    r'recebido|recebida|crédito|credito|depósito|deposito|estorno|resgate'
    r'|rendimento|pix recebido|vencimento cdb|vencimento rdb',
    re.I,
)

# Metadados de saldo que não são lançamentos de movimentação
METADATA_RE = re.compile(
    r'\b(saldo\s*(dia|anterior|final|inicial)|rem\s*basica|cred\s*juros'
    r'|lançamentos\s*do\s*dia|saldo\s*em\b)',
    re.I,
)


# ── Extração de spans e agrupamento de linhas ─────────────────────────────────

def _extrair_spans(page) -> List[Dict]:
    """Extrai todos os spans de texto de uma página fitz com posição e fonte."""
    raw = page.get_text("dict")
    spans = []
    for block in raw["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                text = span["text"].strip()
                if not text:
                    continue
                spans.append({
                    "text": text,
                    "x0"  : round(span["bbox"][0], 1),
                    "y0"  : round(span["bbox"][1], 1),
                    "x1"  : round(span["bbox"][2], 1),
                    "y1"  : round(span["bbox"][3], 1),
                    "font": span["font"],
                })
    spans.sort(key=lambda s: (s["y0"], s["x0"]))
    return spans


def _agrupar_linhas(spans: List[Dict], threshold: float = 5.0) -> List[List[Dict]]:
    """Agrupa spans em linhas horizontais por proximidade vertical."""
    if not spans:
        return []
    lines, current = [], [spans[0]]
    for s in spans[1:]:
        if s["y0"] - current[-1]["y0"] > threshold:
            lines.append(current)
            current = [s]
        else:
            current.append(s)
    lines.append(current)
    return lines


# ── Chord ─────────────────────────────────────────────────────────────────────

def _tem_valor(txt: str) -> bool:
    return bool(DEC_RE.search(txt))


def _peso(font: str) -> str:
    """Retorna 'P' (pesado/bold) ou 'L' (leve/regular) com base no nome da fonte."""
    return "P" if any(p in font for p in ["Bold", "Semibold", "Medium", "F7", "F6", "Black"]) else "L"


def _pelar_data_layer(line: List[Dict]):
    """
    FIX 3 — Separa elemento DD/MM da linha antes de construir o chord.
    Retorna (data_anchor | None, line_sem_ancora).
    """
    if not line:
        return None, line
    if DATE_ANCHOR_RE.match(line[0]["text"].strip()):
        return line[0]["text"].strip(), line[1:]
    return None, line


def _chord_fn(line: List[Dict]):
    """
    Converte linha em chord: tupla de (x_bucket, peso, tem_valor).
    Aplica peeling de data layer antes (FIX 3).
    Retorna (chord_tuple, data_anchor | None).
    """
    da, le = _pelar_data_layer(line)
    seen, parts = [], []
    for s in le:
        k = (round(s["x0"] / 20) * 20, _peso(s["font"]), "V" if _tem_valor(s["text"]) else "_")
        if k not in seen:
            seen.append(k)
            parts.append(k)
    return tuple(parts), da


def _eh_lancamento(c: tuple) -> bool:
    """Chord válido de lançamento: ≥2 elementos, nenhum bold, último tem valor."""
    return len(c) >= 2 and not any(p == "P" for _, p, _ in c) and c[-1][2] == "V"


def _normalizar(c: tuple) -> tuple:
    """Remove dimensão de posição x do chord para comparação flexível."""
    return tuple((p, v) for _, p, v in c)


# ── Extração de valores com posição ──────────────────────────────────────────

def _extrair_valores(line: List[Dict]) -> List[Dict]:
    """
    Extrai todos os valores decimais da linha (sem data layer),
    ordenados por posição x crescente.
    """
    _, le = _pelar_data_layer(line)
    vals = []
    for s in le:
        for m in DEC_RE.finditer(s["text"]):
            try:
                f = float(m.group().replace(".", "").replace(",", "."))
                vals.append({"x": round(s["x0"], 1), "raw": m.group(), "float": f})
            except ValueError:
                pass
    return sorted(vals, key=lambda v: v["x"])


# ── Inferência de sinal ───────────────────────────────────────────────────────

def _inferir_sinal(descricao: str, raw_valor: str) -> int:
    """
    Retorna +1 (entrada) ou -1 (saída).

    Prioridade:
      1. Sufixo/prefixo explícito no campo de valor ('-', '+')
      2. Contexto semântico da descrição
      3. Default conservador: +1 (entrada)
    """
    if raw_valor:
        if raw_valor.endswith("-") or raw_valor.startswith("-"):
            return -1
        if raw_valor.startswith("+"):
            return +1
    txt = descricao or ""
    if _ENTRADA_RE.search(txt):
        return +1
    if _SAIDA_RE.search(txt):
        return -1
    return +1


# ── Classificação de n_val ────────────────────────────────────────────────────

def _classificar_linha(txt: str, n_val: int, valor_float: float) -> str:
    """
    Classifica o lançamento baseado no número de valores decimais.

    n_val > 2  → 'descartado'    (tabelas de índices etc.)
    n_val == 2 → 'metadata'      se valor == 0 ou texto bate METADATA_RE
    n_val == 2 → 'saldo_embutido' caso contrário
    n_val <= 1 → 'lancamento'

    Invariante posicional (n_val == 2):
      menor x = valor da transação, maior x = saldo após (universal)
    """
    if n_val > 2:
        return "descartado"
    if n_val == 2:
        if abs(valor_float) < 0.001:
            return "metadata"
        if METADATA_RE.search(txt):
            return "metadata"
        return "saldo_embutido"
    return "lancamento"


# ── Bbox dos spans ────────────────────────────────────────────────────────────

def _compute_bbox(bloco_linhas: List[List[Dict]]) -> Optional[Dict]:
    """Calcula bounding box de todos os spans do bloco."""
    all_spans = [s for line in bloco_linhas for s in line]
    if not all_spans:
        return None
    return {
        'x0': round(min(s["x0"] for s in all_spans), 2),
        'y0': round(min(s["y0"] for s in all_spans), 2),
        'x1': round(max(s["x1"] for s in all_spans), 2),
        'y1': round(max(s["y1"] for s in all_spans), 2),
    }


# ── Função principal ──────────────────────────────────────────────────────────

def extract_all_pages(pdf_path: str) -> Dict:
    """
    Ponto de entrada principal. Processa todas as páginas do PDF
    e retorna todos os lançamentos em lista plana.

    Chamado via asyncio.to_thread pois é CPU-bound.
    """
    doc = fitz.open(pdf_path)
    pages = range(len(doc))

    # ── 1. Descoberta do chord-alvo por auto-repetição ────────────────────────
    all_cd_ref = []
    for pn in pages:
        lines = _agrupar_linhas(_extrair_spans(doc[pn]))
        gaps = [0] + [
            round(lines[i][0]["y0"] - lines[i - 1][0]["y0"], 1)
            for i in range(1, len(lines))
        ]
        for i, l in enumerate(lines):
            c, _ = _chord_fn(l)
            all_cd_ref.append({"chord": c, "gap": gaps[i]})

    # Chords que aparecem em linhas consecutivas idênticas (= padrão repetido)
    auto_rep: Counter = Counter()
    for i in range(len(all_cd_ref) - 1):
        c = all_cd_ref[i]["chord"]
        if all_cd_ref[i + 1]["chord"] == c:
            auto_rep[c] += 1

    cands = [c for c in auto_rep if _eh_lancamento(c)]
    if not cands:
        freq: Counter = Counter(
            cd["chord"] for cd in all_cd_ref if _eh_lancamento(cd["chord"])
        )
        cands = [c for c, _ in freq.most_common(5)]
    if not cands:
        doc.close()
        return {
            'total_lancamentos': 0,
            'total_entradas'   : 0.0,
            'total_saidas'     : 0.0,
            'saldo'            : 0.0,
            'lancamentos'      : [],
        }

    chord_alvo = max(cands, key=lambda c: (auto_rep.get(c, 0), len(c)))
    norma_alvo = _normalizar(chord_alvo)

    # ── 2. Coleta de linhas de todas as páginas com gaps ──────────────────────
    all_cd = []
    for pn in pages:
        lines = _agrupar_linhas(_extrair_spans(doc[pn]))
        gaps = [0] + [
            round(lines[i][0]["y0"] - lines[i - 1][0]["y0"], 1)
            for i in range(1, len(lines))
        ]
        gap_vals = [g for g in gaps if g > 0]
        gap_dom = Counter(gap_vals).most_common(1)[0][0] if gap_vals else 10.0
        gap_sec = gap_dom * 2.5  # FIX 1: threshold de quebra de seção

        for i, l in enumerate(lines):
            c, da = _chord_fn(l)
            all_cd.append({
                "chord"  : c,
                "gap"    : gaps[i],
                "line"   : l,
                "da"     : da,       # data anchor DD/MM se presente
                "page"   : pn,
                "gap_sec": gap_sec,
            })

    # Conjunto de chords aceitos: mesma norma e ±1 elemento de comprimento
    aceitos = {
        c for c in set(cd["chord"] for cd in all_cd)
        if _eh_lancamento(c)
        and _normalizar(c) == norma_alvo
        and abs(len(c) - len(chord_alvo)) <= 1
    }

    # ── 3. Montagem de blocos e extração ──────────────────────────────────────
    all_tx: List[Dict] = []
    grand_e = grand_s = 0.0
    last_date: Optional[str] = None
    i = 0

    while i < len(all_cd):
        cd = all_cd[i]
        if cd["chord"] not in aceitos:
            i += 1
            continue

        # Absorve linhas de continuação
        bloco_linhas = [cd["line"]]
        j = i + 1
        while j < len(all_cd):
            prox = all_cd[j]
            # FIX 1: para absorção se gap indica quebra de seção
            if prox["gap"] > prox["gap_sec"]:
                break
            eh_cont = (
                len(prox["chord"]) == 1
                and prox["chord"][0][1] == "L"
                and prox["chord"][0][2] == "_"
                and prox["chord"] not in aceitos
            )
            if eh_cont:
                bloco_linhas.append(prox["line"])
                j += 1
            else:
                break

        # Extração de dados da linha principal
        linha_principal = bloco_linhas[0]
        da              = cd["da"]
        continuacoes    = [" ".join(s["text"] for s in l) for l in bloco_linhas[1:]]
        txt_completo    = " ".join(s["text"] for s in linha_principal)
        vals            = _extrair_valores(linha_principal)
        n_val           = len(vals)

        # Classificação
        valor_principal_float = vals[0]["float"] if vals else 0.0
        cls = _classificar_linha(txt_completo, n_val, valor_principal_float)

        if cls in ("descartado", "metadata"):
            i = j
            continue

        # Separação valor / saldo pela posição x (rightmost = saldo, invariante)
        if n_val == 0:
            valor_tx  = None
            raw_tx    = None
            saldo_emb = None
        elif n_val == 1:
            valor_tx  = vals[0]["float"]
            raw_tx    = vals[0]["raw"]
            saldo_emb = None
        else:  # n_val == 2 (saldo_embutido)
            valor_tx  = vals[0]["float"]   # menor x → valor da transação
            raw_tx    = vals[0]["raw"]
            saldo_emb = vals[-1]["float"]  # maior x → saldo após

        # Aplicar sinal ao valor
        sinal = _inferir_sinal(txt_completo, raw_tx or "")
        if valor_tx is not None:
            valor_tx = round(abs(valor_tx) * sinal, 2)

        # Mapear para contrato: entrada (>0) ou saida (<0)
        entrada = round(valor_tx, 2) if valor_tx is not None and valor_tx > 0 else None
        saida   = round(valor_tx, 2) if valor_tx is not None and valor_tx < 0 else None

        # Data: da layer DD/MM → texto da linha → herança do último lançamento
        date_str = da
        if not date_str:
            date_str = next(
                (s["text"].strip() for s in linha_principal
                 if DATE_RE.match(s["text"].strip())),
                None,
            )
        if not date_str and last_date:
            date_str = last_date
        if date_str:
            last_date = date_str

        # Descrição: tokens sem decimal e sem data, deduplicados
        desc_parts = [
            s["text"].strip() for s in linha_principal
            if not DEC_RE.search(s["text"])
            and not DATE_RE.match(s["text"].strip())
            and s["text"].strip() not in ("-", "|", "")
            and len(s["text"].strip()) > 1
        ]
        descricao = " ".join(dict.fromkeys(desc_parts)) or None

        if entrada:
            grand_e += entrada
        if saida:
            grand_s += saida

        all_tx.append({
            'page'      : cd["page"] + 1,
            'zone'      : None,
            'date'      : date_str,
            'descricao' : descricao,
            'entrada'   : entrada,
            'saida'     : saida,
            'saldo_apos': round(saldo_emb, 2) if saldo_emb is not None else None,
            'bbox'      : _compute_bbox(bloco_linhas),
            '_raw'      : [txt_completo] + continuacoes,
        })
        i = j

    doc.close()

    return {
        'total_lancamentos': len(all_tx),
        'total_entradas'   : round(grand_e, 2),
        'total_saidas'     : round(grand_s, 2),
        'saldo'            : round(grand_e + grand_s, 2),
        'lancamentos'      : all_tx,
    }
