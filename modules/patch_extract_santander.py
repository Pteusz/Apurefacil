"""
patch_extract_santander.py
Aplicar no VPS:
    python3 patch_extract_santander.py

Substitui _extract_santander em extrator_bancario.py pela versão com Voronoi 1D.
Faz backup automático antes de qualquer alteração.
"""

import shutil
from pathlib import Path

PATH = Path("/opt/estabilizei-backend/modules/extrator_bancario.py")

NOVA_FUNCAO = '''\
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
        r"conta corrente$|movimenta|data$|descrição$|nº documento|movimento|saldo \\(|"
        r"saldo disp|período:|loja:|central|ouvidoria|libras|comprovantes|"
        r"contas de consumo|transferên|data canal|créditos contratados|limite da conta|"
        r"cet |saldos por período|compras com cartão|Renda Fixa|Aplicação|"
        r"Movimentação|Rendimento|CDB|Pacote|SERVICOS|Produtos|Valor da|Status|"
        r"Dia de|Índices|IBOVESPA|IGPM|INCC|INPC|IPCA|CDI|TR|POUPANCA|EURO|DOLAR|SALARIO)",
        re.I,
    )
    DATE_RE = re.compile(r"^\\d{2}/\\d{2}$")

    doc = fitz.open(pdf_path)

    # ── Inferir ano do cabeçalho (primeira página) ────────────────────────
    p0_text = doc[0].get_text()
    m_ano = re.search(
        r"(janeiro|fevereiro|mar[çc]o|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)/(\\d{4})",
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
                "desc_frags": [(val_s["y0"], desc_s["text"])],
                "v"         : v,
                "sinal"     : sinal,
                "page"      : pn + 1,
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
            (linha[0]["y0"], next(s["text"] for s in linha if 62 <= s["x0"] <= 70 and "Narrow" in s["font"]))
            for linha in linhas_movim
            if not any(428 <= s["x0"] <= 450 and "Narrow" in s["font"] and DEC_RE.search(s["text"]) for s in linha)
            and any(62 <= s["x0"] <= 70 and "Narrow" in s["font"] and not DEC_RE.search(s["text"]) for s in linha)
        ]

        for fy, ftxt in flutuantes:
            if FILTRO.search(ftxt):
                continue
            for i, a in enumerate(ancoras):
                if limites_sup[i] <= fy <= limites_inf[i]:
                    a["desc_frags"].append((fy, ftxt))
                    break

        # ── 5. Montar lançamentos ─────────────────────────────────────────
        for a in ancoras:
            frags_ord = sorted(a["desc_frags"], key=lambda x: x[0])
            descricao = " | ".join(
                t for _, t in frags_ord if not FILTRO.search(t)
            ) or None

            lancamentos.append({
                "page"     : a["page"],
                "date"     : a["date"],
                "entrada"  : round(a["v"],  2) if a["sinal"] == +1 else None,
                "saida"    : round(-a["v"], 2) if a["sinal"] == -1 else None,
                "bbox"     : _bbox_da_linha(a.get("linha", [{"x0":0,"y0":0,"x1":0,"y1":0}])),
                "descricao": descricao,
            })

    doc.close()
    return _montar(lancamentos)

'''

MARKER_START = "def _extract_santander(pdf_path: str) -> dict:"
MARKER_END   = (
    "# ══════════════════════════════════════════════════════════════════════════════\n"
    "# EXTRATOR UBER CONTA (DIGIO)"
)

def main():
    src = PATH.read_text(encoding="utf-8")

    if MARKER_START not in src:
        raise RuntimeError(f"Marcador de início não encontrado: {MARKER_START!r}")
    if MARKER_END not in src:
        raise RuntimeError(f"Marcador de fim não encontrado: {MARKER_END!r}")

    backup = PATH.with_suffix(".py.bak3")
    shutil.copy2(PATH, backup)
    print(f"Backup salvo em: {backup}")

    i_start = src.index(MARKER_START)
    i_end   = src.index(MARKER_END)

    novo_src = src[:i_start] + NOVA_FUNCAO + "\n" + src[i_end:]
    PATH.write_text(novo_src, encoding="utf-8")
    print("OK — _extract_santander substituída com sucesso.")

    import py_compile, tempfile, os
    tmp = tempfile.mktemp(suffix=".py")
    Path(tmp).write_text(novo_src, encoding="utf-8")
    try:
        py_compile.compile(tmp, doraise=True)
        print("Sintaxe: OK")
    except py_compile.PyCompileError as e:
        print(f"ERRO DE SINTAXE: {e}")
        print("Revertendo para backup...")
        shutil.copy2(backup, PATH)
        print("Revertido.")
    finally:
        os.unlink(tmp)

if __name__ == "__main__":
    main()
