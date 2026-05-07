"""
Executa na VPS:
    python3 apply_patch.py
"""
import re, shutil, sys
from pathlib import Path

TARGET = Path("/opt/estabilizei-backend/modules/extrator_bancario.py")

NEW_FUNC = r'''def _extract_picpay(pdf_path: str) -> dict:
    HORA_RE        = re.compile(r"^\d{2}:\d{2}$")
    VALOR_RE       = re.compile(r"([+−\-])R\$\s*(\d{1,3}(?:\.\d{3})*,\d{2})")
    DATA_RE        = re.compile(r"(\d{1,2}\s+de\s+\w+\s+\d{4})", re.I)
    FILTRO_DESC_RE = re.compile(
        r"^(Hora|Tipo|Origem\s*/\s*Destino|Forma de pagamento|Valor|Com saldo|"
        r"Saldo ao final|Saldo final|Documento emitido|PicPay|CNPJ|Dias úteis|"
        r"Extrato de conta|Per[ií]odo|Ag[eê]ncia|CPF)$",
        re.I,
    )

    doc = fitz.open(pdf_path)
    lancamentos = []
    data_atual  = None

    for pn in range(len(doc)):
        spans  = _extrair_spans(doc[pn])
        linhas = _agrupar_linhas(spans)

        # — 1ª passagem: coletar âncoras (hora) com valor na mesma linha lógica
        ancoras = []
        for linha in linhas:
            for s in linha:
                if 35 <= s["x0"] <= 55:
                    mm = DATA_RE.match(s["text"])
                    if mm:
                        data_atual = mm.group(1)

            ancora = next(
                (s for s in linha if 35 <= s["x0"] <= 60 and HORA_RE.match(s["text"])),
                None,
            )
            if not ancora:
                continue

            m_val = next(
                (VALOR_RE.search(s["text"]) for s in linha if VALOR_RE.search(s["text"])),
                None,
            )
            if not m_val:
                continue

            ancoras.append({
                "ancora"    : ancora,
                "linha"     : linha,
                "m_val"     : m_val,
                "data"      : data_atual,
                "page"      : pn + 1,
                "desc_spans": [],
            })

        if not ancoras:
            continue

        # — 2ª passagem: coletar spans de descrição (x0≈243, tolerância 8pt)
        desc_spans = [
            s for s in spans
            if abs(s["x0"] - 243.0) < 8
            and not s["hv"]
            and not FILTRO_DESC_RE.search(s["text"])
        ]

        # — 3ª passagem: Voronoi 1D — cada span vai para a âncora com y mais próxima
        ancora_ys = [a["ancora"]["y0"] for a in ancoras]
        for ds in desc_spans:
            dists = [abs(ds["y0"] - ay) for ay in ancora_ys]
            ancoras[dists.index(min(dists))]["desc_spans"].append(ds)

        # — montar lançamentos
        for a in ancoras:
            m_val = a["m_val"]
            sinal = -1 if m_val.group(1) in ("-", "−") else +1
            v     = float(m_val.group(2).replace(".", "").replace(",", "."))

            desc_sorted = sorted(a["desc_spans"], key=lambda s: s["y0"])
            descricao   = " ".join(s["text"] for s in desc_sorted) or None

            lancamentos.append({
                "page"     : a["page"],
                "date"     : a["data"],
                "entrada"  : round(v, 2) if sinal == +1 else None,
                "saida"    : round(-v, 2) if sinal == -1 else None,
                "descricao": descricao,
                "bbox"     : _bbox_da_linha(a["linha"]),
                "_raw"     : (" ".join(s["text"] for s in a["linha"]))[:80],
            })

    doc.close()
    return _montar_resultado(lancamentos)
'''

NOVA_SECAO4 = (
    "# " + "═" * 78 + "\n"
    "# SEÇÃO 4 — Extrator PicPay  (v2 — Voronoi 1D para descrição multilinha)\n"
    "#\n"
    "# Estrutura: hora HH:MM x≈44 | tipo x≈121 | origem/destino x≈243 | valor embutido (+/-R$)\n"
    "# Âncora   : hora HH:MM em x≈44\n"
    "# Sinal    : prefixo '+R$' ou '−R$' embutido no span do valor\n"
    "# Data     : herdada do header 'DD de mês YYYY Saldo ao final do dia'\n"
    "# Descrição: Voronoi 1D — todos os spans em x≈243, não-numéricos, não-header,\n"
    "#             são atribuídos à âncora (hora) cuja y é mais próxima da sua y.\n"
    "#             Resolve multilinhas que aparecem ANTES e DEPOIS do y da âncora.\n"
    "# " + "═" * 78 + "\n\n"
    + NEW_FUNC
)

SECAO4_RE = re.compile(
    r"# ═+\s*\n# SEÇÃO 4 — Extrator PicPay.*?(?=\n# ═+\s*\n# SEÇÃO 5)",
    re.DOTALL,
)

if not TARGET.exists():
    print(f"ERRO: arquivo não encontrado em {TARGET}")
    sys.exit(1)

src = TARGET.read_text(encoding="utf-8")

if not SECAO4_RE.search(src):
    print("ERRO: padrão da SEÇÃO 4 não encontrado.")
    sys.exit(1)

backup = TARGET.with_suffix(".py.bak")
shutil.copy2(TARGET, backup)
print(f"Backup salvo em: {backup}")

# lambda evita que \d dentro de NEW_FUNC seja interpretado como backreference
novo = SECAO4_RE.sub(lambda _: NOVA_SECAO4, src)
TARGET.write_text(novo, encoding="utf-8")
print(f"Patch aplicado em: {TARGET}")
print("Feito.")
