"""
patch_extract_bb.py
Aplicar no VPS:
    python3 patch_extract_bb.py

Substitui _extract_bb em extrator_bancario.py pela versão com Voronoi 1D.
Faz backup automático antes de qualquer alteração.
"""

import shutil
from pathlib import Path

PATH = Path("/opt/estabilizei-backend/modules/extrator_bancario.py")

NOVA_FUNCAO = '''\
def _extract_bb(pdf_path: str) -> dict:
    """
    Estrutura: data x0=30 | lote x0=99 | doc x0=148 | histórico x0=265 | valor x0≈535 com (+/-)
    Âncora   : linha com data DD/MM/YYYY x0=30 + valor com sufixo (+) ou (-)
    Filtro   : Saldo do dia, BB Rende Fácil (doc=9903), Saldo Anterior, cabeçalho Histórico
    Descrição: Voronoi 1D — cada âncora define zona [mid_acima, mid_abaixo] e coleta
               todos os fragmentos em x0=265 que caem nessa zona, ordenados por y0.
    """
    FILTRO     = re.compile(
        r"^(saldo do dia|saldo anterior|bb rende f|rende facil|s\\s*a\\s*l\\s*d\\s*o|total aplic|histórico)",
        re.I,
    )
    FILTRO_DOC = {"9903"}
    DATE_RE    = re.compile(r"^\\d{2}/\\d{2}/\\d{4}$")
    SINAL_RE   = re.compile(r"(\\d{1,3}(?:\\.\\d{3})*,\\d{2})\\s*\\(([+\\-])\\)")

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
                "hist_frags": [(data_s["y0"], hist_inline["text"])] if hist_inline else [],
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
            (linha[0]["y0"], next(s["text"] for s in linha if 260 <= s["x0"] <= 275))
            for linha in linhas
            if not any(25 <= s["x0"] <= 40 and DATE_RE.match(s["text"]) for s in linha)
            and any(260 <= s["x0"] <= 275 for s in linha)
        ]

        for fy, ftxt in hist_flutuantes:
            if FILTRO.search(ftxt):
                continue
            for i, a in enumerate(ancoras):
                if limites_sup[i] <= fy <= limites_inf[i]:
                    a["hist_frags"].append((fy, ftxt))
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

            frags_ord = sorted(a["hist_frags"], key=lambda x: x[0])
            descricao = " | ".join(
                t for _, t in frags_ord if not FILTRO.search(t)
            ) or None

            lancamentos.append({
                "page"     : a["page"],
                "date"     : data_atual,
                "entrada"  : round(v,  2) if sinal == +1 else None,
                "saida"    : round(-v, 2) if sinal == -1 else None,
                "bbox"     : _bbox_da_linha(a["linha"]),
                "descricao": descricao,
            })

    doc.close()
    return _montar(lancamentos)

'''

MARKER_START = "def _extract_bb(pdf_path: str) -> dict:"
MARKER_END   = (
    "# ══════════════════════════════════════════════════════════════════════════════\n"
    "# EXTRATOR SANTANDER"
)

def main():
    src = PATH.read_text(encoding="utf-8")

    if MARKER_START not in src:
        raise RuntimeError(f"Marcador de início não encontrado: {MARKER_START!r}")
    if MARKER_END not in src:
        raise RuntimeError(f"Marcador de fim não encontrado: {MARKER_END!r}")

    # Backup
    backup = PATH.with_suffix(".py.bak2")
    shutil.copy2(PATH, backup)
    print(f"Backup salvo em: {backup}")

    i_start = src.index(MARKER_START)
    i_end   = src.index(MARKER_END)

    novo_src = src[:i_start] + NOVA_FUNCAO + "\n" + src[i_end:]
    PATH.write_text(novo_src, encoding="utf-8")

    print("OK — _extract_bb substituída com sucesso.")
    print(f"Arquivo: {PATH}")

    # Verificação rápida de sintaxe
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
