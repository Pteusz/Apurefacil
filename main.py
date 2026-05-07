"""
main.py — Estabilizei Backend
FastAPI app com todas as rotas.

Auth:
  POST   /auth/magic-link             → envia magic link por email
  GET    /auth/verify/{token}         → valida token, retorna JWT
  POST   /auth/login                  → login com email + senha
  GET    /auth/me                     → dados do usuário (plano + créditos)
  PATCH  /auth/me                     → atualizar nome
  POST   /auth/change-password        → definir ou alterar senha
  POST   /auth/dev-token              → JWT direto (ENV=development apenas)

Upload / Sessão / Créditos: inalterados.

Configuração:
  Variáveis de ambiente lidas de .env (via python-dotenv).
  Em produção, o .env fica no servidor ao lado do docker-compose.yml
  e é injetado no container via env_file — nunca entra na imagem Docker.
  Para alterar qualquer config (SMTP, JWT, etc.): edite .env e reinicie
  o container com `docker-compose restart` (sem rebuild).
"""
# ── Carrega .env antes de qualquer import que leia os.environ ─────────────────
from dotenv import load_dotenv
load_dotenv()   # no-op se as variáveis já estiverem no ambiente (Docker/produção)

import hashlib
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aiofiles
import pdfplumber
from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from db import get_all_config, get_db, init_db, set_config
from modules import auth
from modules.auth import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    create_access_token,
    create_admin_token,
    get_current_admin,
    get_current_user,
    get_or_create_user,
    get_user_full,
    login_admin,
    login_with_password,
    magic_link_send,
    magic_link_verify,
)
from modules.creditos import (
    creditar,
    debitar,
    get_extrato,
    get_saldo,
)
from modules.pagamentos import (
    aprovar_pagamento,
    criar_assinatura_mp,
    criar_checkout_ip,
    get_pagamento,
    listar_pagamentos,
    processar_webhook_ip,
    processar_webhook_mp,
    reprovar_pagamento,
)
from modules.sessao import (
    BancoConflito,
    aplicar_flag_grupo,
    calcular_apuracao,
    calcular_estado,
    calcular_laudo,
    criar_sessao,
    get_sessao,
    get_sessao_raw,
    listar_sessoes,
    mutate_session,
    patch_sessao,
    remover_sessao,
    salvar_snapshot_apuracao,
)
from schemas import (
    AdminCreditosRequest,
    AdminConfigRequest,
    AdminLoginRequest,
    AdminPlanoRequest,
    AssinarPlanoMPRequest,
    ChangePasswordRequest,
    CheckoutIPRequest,
    CriarSessaoRequest,
    DevTokenRequest,
    ExtrairAssinaturaRequest,
    LoginRequest,
    MagicLinkRequest,
    MutateRequest,
    PatchFlagGrupoRequest,
    PatchSessaoRequest,
    SalvarAssinaturaRequest,
    UpdateMeRequest,
)
from storage import UPLOADS_DIR

ENV = os.environ.get("ENV", "production")

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title       = "Estabilizei Backend",
    description = "Motor de apuração de renda — API principal",
    version     = "1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins  = ["*"],   # Restringir em produção para front.estabilizei.com.br
    allow_methods  = ["*"],
    allow_headers  = ["*"],
)

class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    """Força revalidação no browser para arquivos HTML, JS e CSS."""
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.endswith(('.html', '.js', '.css')) or path in ('/', ''):
            response.headers['Cache-Control'] = 'no-cache, must-revalidate'
        return response

app.add_middleware(NoCacheStaticMiddleware)


@app.on_event("startup")
async def startup():
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    init_db()


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/ping")
async def ping():
    return {"status": "ok", "service": "estabilizei-backend", "version": "1.0.0", "env": ENV}


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.post("/auth/dev-token", summary="[DEV] Gera JWT direto por email")
async def dev_token(body: DevTokenRequest):
    if ENV != "development":
        raise HTTPException(status_code=403, detail="Disponível apenas em ambiente de desenvolvimento")
    user  = get_or_create_user(body.email)
    token = create_access_token(user["id"])
    return {"token": token, "user": get_user_full(user["id"])}


@app.post("/auth/magic-link", summary="Envia magic link por email")
async def magic_link_send_route(body: MagicLinkRequest):
    try:
        magic_link_send(body.email)
        return {"ok": True, "message": "Link de acesso enviado para o seu email"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao enviar email: {e}")


@app.get("/auth/verify/{token}", summary="Valida magic link e retorna JWT")
async def magic_link_verify_route(token: str):
    user = magic_link_verify(token)
    if not user:
        raise HTTPException(status_code=400, detail="Link inválido ou expirado")
    jwt_token = create_access_token(user["id"])
    return {"token": jwt_token, "user": user}


@app.post("/auth/login", summary="Login com email e senha")
async def login_route(body: LoginRequest):
    user = login_with_password(body.email, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Email ou senha incorretos")
    token = create_access_token(user["id"])
    return {"token": token, "user": user}


@app.get("/auth/me", summary="Dados do usuário autenticado (com plano e créditos)")
async def auth_me(user_id: str = Depends(get_current_user)):
    user = get_user_full(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    return user


@app.patch("/auth/me", summary="Atualizar nome do usuário")
async def update_me(body: UpdateMeRequest, user_id: str = Depends(get_current_user)):
    user = auth.update_user_name(user_id, body.nome.strip())
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    return user


@app.post("/auth/change-password", summary="Definir ou alterar senha")
async def change_password_route(
    body    : ChangePasswordRequest,
    user_id : str = Depends(get_current_user),
):
    auth.change_password(user_id, body.current_password, body.new_password)
    return {"ok": True, "message": "Senha atualizada com sucesso"}


# ── Upload ────────────────────────────────────────────────────────────────────

@app.post("/upload", summary="Recebe PDF e retorna file_id")
async def upload_pdf(
    file    : UploadFile = File(...),
    user_id : str        = Depends(get_current_user),
):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Apenas arquivos PDF são aceitos")

    fid     = uuid.uuid4().hex[:12]
    dest    = UPLOADS_DIR / f"{fid}.pdf"
    content = await file.read()

    async with aiofiles.open(dest, "wb") as f:
        await f.write(content)

    try:
        with pdfplumber.open(str(dest)) as pdf:
            total_pages = len(pdf.pages)
    except Exception:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Arquivo PDF inválido ou corrompido")

    return {
        "file_id"    : fid,
        "filename"   : file.filename,
        "total_pages": total_pages,
    }


# ── Sessão ────────────────────────────────────────────────────────────────────

@app.post("/sessao", summary="Cria sessão — processa um ou mais PDFs e classifica lançamentos")
async def criar_sessao_route(
    body    : CriarSessaoRequest,
    user_id : str = Depends(get_current_user),
):
    # Valida existência de cada arquivo antes de debitar crédito
    pdf_paths: list[str] = []
    for fid in body.file_ids:
        pdf_path = UPLOADS_DIR / f"{fid}.pdf"
        if not pdf_path.exists():
            raise HTTPException(
                status_code=404,
                detail=f"PDF não encontrado — faça upload primeiro (file_id: {fid})",
            )
        pdf_paths.append(str(pdf_path))

    saldo = get_saldo(user_id)
    if saldo < 1:
        raise HTTPException(
            status_code=402,
            detail=f"Créditos insuficientes. Saldo atual: {saldo}",
        )

    try:
        sessao = await criar_sessao(
            user_id      = user_id,
            file_ids     = body.file_ids,
            pdf_paths    = pdf_paths,
            session_name = body.session_name,
            config       = body.config.model_dump(),
            bancos       = body.bancos,
        )
    except BancoConflito as e:
        raise HTTPException(
            status_code = 422,
            detail      = {
                "error"   : "banco_conflito",
                "file_idx": e.file_idx,
                "file_id" : e.file_id,
                "banco"   : e.banco,
            },
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao processar PDF: {e}")

    debitar(user_id, 1, f"apuracao:{sessao['session_id']}")
    return sessao


@app.get("/sessao", summary="Lista sessões do usuário")
async def listar_sessoes_route(user_id: str = Depends(get_current_user)):
    return await listar_sessoes(user_id)


@app.get("/sessao/{session_id}", summary="Estado atual da sessão")
async def get_sessao_route(session_id: str, user_id: str = Depends(get_current_user)):
    try:
        return await get_sessao(user_id, session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Sessão não encontrada")


@app.get("/sessao/{session_id}/raw", summary="JSON completo com versions[] (debug/auditoria)")
async def get_sessao_raw_route(session_id: str, user_id: str = Depends(get_current_user)):
    try:
        return await get_sessao_raw(user_id, session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Sessão não encontrada")


@app.post("/sessao/{session_id}/mutate", summary="Aplica mutação de estado (portão único)")
async def mutate_route(
    session_id : str,
    body       : MutateRequest,
    user_id    : str = Depends(get_current_user),
):
    try:
        return await mutate_session(user_id, session_id, body.model_dump())
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Sessão não encontrada")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/sessao/{session_id}/estado", summary="Estado completo: sessao + apuracao + laudo com 1x apurar()")
async def estado_route(session_id: str, user_id: str = Depends(get_current_user)):
    try:
        return await calcular_estado(user_id, session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Sessão não encontrada")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/sessao/{session_id}/apuracao", summary="Cálculo on-demand (nunca persistido)")
async def apuracao_route(session_id: str, user_id: str = Depends(get_current_user)):
    try:
        return await calcular_apuracao(user_id, session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Sessão não encontrada")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/sessao/{session_id}/laudo", summary="Laudo estruturado para renderização (nunca persistido)")
async def laudo_route(session_id: str, user_id: str = Depends(get_current_user)):
    try:
        return await calcular_laudo(user_id, session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Sessão não encontrada")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.patch(
    "/apuracao/{session_id}/grupo/{grupo_id}/flag",
    summary="Override de flag de grupo pelo contador — re-executa apuração e retorna resultado completo",
)
async def patch_grupo_flag(
    session_id: str,
    grupo_id  : str,
    body      : PatchFlagGrupoRequest,
    user_id   : str = Depends(get_current_user),
):
    from modules.apuracao import FREQ_MIN_MESES, FREQ_MIN_CONSERVADOR

    _FLAGS_VALIDAS = {
        'estavel', 'ruido', 'sem_historico',
        'auto_transferencia', 'renda_circular', 'ignorar',
    }
    if body.flag not in _FLAGS_VALIDAS:
        raise HTTPException(
            status_code=422,
            detail=f"Flag inválida: '{body.flag}'. Valores aceitos: {sorted(_FLAGS_VALIDAS)}",
        )

    # Carrega sessão para obter modo e validar
    try:
        sessao_raw = await get_sessao_raw(user_id, session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Sessão não encontrada")

    # Valida transições proibidas antes de persistir
    if body.flag == 'sem_historico':
        # Bloquear se o grupo tem aparições suficientes para ter histórico
        try:
            resultado_atual = await calcular_apuracao(user_id, session_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        modo     = sessao_raw.get("config", {}).get("modo", "padrao")
        freq_min = FREQ_MIN_CONSERVADOR if modo == "conservador" else FREQ_MIN_MESES

        grupo_info = None
        for item in (
            resultado_atual.get("composicao", [])
            + resultado_atual.get("inconclusivos", [])
            + resultado_atual.get("excluidos", [])
        ):
            if item.get("grupo_id") == grupo_id:
                grupo_info = item
                break

        if grupo_info is not None:
            aparicoes = grupo_info.get("aparicoes", 0)
            if aparicoes >= freq_min:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Flag 'sem_historico' inválida: grupo aparece em {aparicoes} meses "
                        f"(mínimo para ter histórico: {freq_min}). "
                        "Só é possível marcar como 'sem_historico' grupos com histórico insuficiente."
                    ),
                )

    # Persiste override
    try:
        await aplicar_flag_grupo(user_id, session_id, grupo_id, body.flag)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Sessão não encontrada")

    # Re-executa, persiste snapshot e retorna resultado completo atualizado
    try:
        resultado = await calcular_apuracao(user_id, session_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    await salvar_snapshot_apuracao(
        user_id    = user_id,
        session_id = session_id,
        resultado  = resultado,
        trigger    = "patch_grupo_flag",
    )
    return resultado


@app.patch("/sessao/{session_id}", summary="Atualiza metadados da sessão (nome, fixado)")
async def patch_sessao_route(
    session_id : str,
    body       : PatchSessaoRequest,
    user_id    : str = Depends(get_current_user),
):
    if body.session_name is None and body.pinned is None:
        raise HTTPException(status_code=400, detail="Nenhum campo para atualizar")
    try:
        return await patch_sessao(user_id, session_id, body.model_dump(exclude_none=True))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Sessão não encontrada")


@app.delete("/sessao/{session_id}", summary="Remove sessão")
async def remover_sessao_route(session_id: str, user_id: str = Depends(get_current_user)):
    removed = await remover_sessao(user_id, session_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Sessão não encontrada")
    return {"removed": True, "session_id": session_id}


# ── Servir PDF original ───────────────────────────────────────────────────────

@app.get("/upload/{file_id}/pdf", summary="Serve o PDF original para o editor de overlays")
async def serve_pdf(file_id: str, user_id: str = Depends(get_current_user)):
    pdf_path = UPLOADS_DIR / f"{file_id}.pdf"
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF não encontrado")
    return FileResponse(str(pdf_path), media_type="application/pdf")


@app.post("/upload/{file_id}/extrair-area",
          summary="Extrai dados de um único lançamento a partir de área selecionada (sem propagação de padrão)")
async def extrair_area_route(
    file_id : str,
    body    : ExtrairAssinaturaRequest,
    user_id : str = Depends(get_current_user),
):
    pdf_path = UPLOADS_DIR / f"{file_id}.pdf"
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF não encontrado")

    from modules.assinaturas import extrair_area_simples
    resultado = extrair_area_simples(
        str(pdf_path),
        body.page,
        body.x0, body.y0, body.x1, body.y1,
    )
    if resultado.get("erro"):
        raise HTTPException(status_code=422, detail=resultado["erro"])
    return resultado


# ── Files ─────────────────────────────────────────────────────────────────────

@app.delete("/files/cleanup", summary="Remove PDFs mais antigos que max_age_days")
async def cleanup_old_files(
    max_age_days : int = 30,
    user_id      : str = Depends(get_current_user),
):
    cutoff  = time.time() - max_age_days * 86400
    removed = 0
    for f in UPLOADS_DIR.glob("*.pdf"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                removed += 1
        except OSError:
            pass
    return {"removed": removed, "max_age_days": max_age_days}


# ── Créditos ──────────────────────────────────────────────────────────────────

@app.get("/creditos", summary="Saldo e extrato de créditos")
async def creditos_route(user_id: str = Depends(get_current_user)):
    return {
        "user_id": user_id,
        "saldo"  : get_saldo(user_id),
        "extrato": get_extrato(user_id),
    }


# ── Pagamentos ────────────────────────────────────────────────────────────────

@app.post("/pagamentos/mp/assinar", summary="Inicia assinatura mensal Mercado Pago")
async def assinar_mp_route(
    body    : AssinarPlanoMPRequest,
    user_id : str = Depends(get_current_user),
):
    """Cria preapproval no MP e retorna {init_point, pagamento_id}."""
    plano = body.plano.lower()
    if plano not in ("essencial", "profissional", "escritorio"):
        raise HTTPException(status_code=400, detail="Plano inválido")
    user = auth.get_user_full(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    return await criar_assinatura_mp(user_id, user["email"], plano)


@app.post("/pagamentos/ip/checkout", summary="Inicia checkout InfinityPay (avulso ou anual)")
async def checkout_ip_route(
    body    : CheckoutIPRequest,
    user_id : str = Depends(get_current_user),
):
    """Cria registro de pagamento e retorna {checkout_url, pagamento_id}."""
    user = auth.get_user_full(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    plano_atual = (user.get("plano") or "trial").lower()
    return await criar_checkout_ip(
        user_id     = user_id,
        plano_atual = plano_atual,
        tipo        = body.tipo,
        user_nome   = user.get("nome") or "",
        user_email  = user.get("email") or "",
        plano       = body.plano,
    )


@app.get("/pagamentos/{pagamento_id}/status", summary="Status de um pagamento (polling)")
async def pagamento_status_route(
    pagamento_id : str,
    user_id      : str = Depends(get_current_user),
):
    """Retorna status atual do pagamento. Usado pelo polling no obrigado.html."""
    pag = get_pagamento(pagamento_id, user_id)
    if not pag:
        raise HTTPException(status_code=404, detail="Pagamento não encontrado")
    return {
        "pagamento_id": pag["id"],
        "status":       pag["status"],
        "tipo":         pag["tipo"],
        "gateway":      pag["gateway"],
        "created_at":   pag["created_at"],
    }


@app.get("/pagamentos", summary="Lista pagamentos do usuário")
async def listar_pagamentos_route(user_id: str = Depends(get_current_user)):
    return listar_pagamentos(user_id)


# ── Webhooks ──────────────────────────────────────────────────────────────────

@app.post("/webhooks/mercadopago", summary="Webhook Mercado Pago")
async def webhook_mp_route(payload: dict):
    result = await processar_webhook_mp(payload)
    return JSONResponse(result)


@app.post("/webhooks/infinitypay", summary="Webhook InfinityPay")
async def webhook_ip_route(payload: dict):
    result = processar_webhook_ip(payload)
    return JSONResponse(result)


# Mantém rotas antigas por compatibilidade caso MP já tenha o URL registrado
@app.post("/creditos/webhook/mercadopago", summary="Webhook MP (legado)")
async def webhook_mp_legado(payload: dict):
    result = await processar_webhook_mp(payload)
    return JSONResponse(result)


@app.post("/creditos/webhook/infinitypay", summary="Webhook IP (legado)")
async def webhook_ip_legado(payload: dict):
    result = processar_webhook_ip(payload)
    return JSONResponse(result)


# ── Planos config (público) ───────────────────────────────────────────────────

def _build_planos_config(c: dict) -> dict:
    """Monta o payload de configuração de planos a partir da tabela config."""
    return {
        "essencial": {
            "creditos":     int(c.get("plano_essencial_creditos",       "15")),
            "preco_mensal": float(c.get("plano_essencial_preco_mensal", "149")),
            "preco_anual":  float(c.get("plano_essencial_preco_anual",  "124")),
            "preco_avulso": float(c.get("plano_essencial_preco_avulso", "9.90")),
        },
        "profissional": {
            "creditos":     int(c.get("plano_profissional_creditos",       "35")),
            "preco_mensal": float(c.get("plano_profissional_preco_mensal", "289")),
            "preco_anual":  float(c.get("plano_profissional_preco_anual",  "241")),
            "preco_avulso": float(c.get("plano_profissional_preco_avulso", "8.50")),
        },
        "escritorio": {
            "creditos":     int(c.get("plano_escritorio_creditos",       "70")),
            "preco_mensal": float(c.get("plano_escritorio_preco_mensal", "479")),
            "preco_anual":  float(c.get("plano_escritorio_preco_anual",  "399")),
            "preco_avulso": float(c.get("plano_escritorio_preco_avulso", "7.00")),
        },
        "avulso": {
            "quantidade": int(c.get("avulso_quantidade", "10")),
            "preco":      float(c.get("avulso_preco",    "99.00")),
        },
    }


@app.get("/planos/config", summary="Configuração pública de planos e preços")
async def planos_config_route():
    return _build_planos_config(get_all_config())


@app.get("/bancos", summary="Lista bancos suportados (vanilla + assinaturas do usuário)")
async def bancos_route(user_id: str = Depends(get_current_user)):
    from modules.extrator_bancario import list_bancos
    from modules import autodetect
    from modules.assinaturas import listar as listar_assinaturas

    vanilla = list_bancos()

    user_sigs = listar_assinaturas(user_id)
    user_entries = [
        {
            "key"       : f"user:{s['id']}",
            "label"     : s["bank_name"],
            "source"    : "user",
            "id"        : s["id"],
            "created_at": s["created_at"],
        }
        for s in user_sigs
    ]

    return {"vanilla": vanilla, "user": user_entries}


# ── Assinaturas ───────────────────────────────────────────────────────────────

@app.post("/assinaturas/extrair", summary="Deriva assinatura a partir de retângulo no PDF")
async def extrair_assinatura_route(
    body    : ExtrairAssinaturaRequest,
    user_id : str = Depends(get_current_user),
):
    from modules.assinaturas import extrair_de_retangulo

    pdf_path = UPLOADS_DIR / f"{body.file_id}.pdf"
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF não encontrado")

    resultado = extrair_de_retangulo(
        str(pdf_path), body.page,
        body.x0, body.y0, body.x1, body.y1,
    )
    if resultado.get("assinatura") is None:
        raise HTTPException(status_code=422, detail=resultado.get("erro", "Não foi possível derivar assinatura"))
    return resultado


@app.get("/assinaturas", summary="Lista assinaturas salvas pelo usuário")
async def listar_assinaturas_route(user_id: str = Depends(get_current_user)):
    from modules.assinaturas import listar as listar_assinaturas
    return listar_assinaturas(user_id)


@app.post("/assinaturas", summary="Salva nova assinatura")
async def salvar_assinatura_route(
    body    : SalvarAssinaturaRequest,
    user_id : str = Depends(get_current_user),
):
    from modules.assinaturas import salvar
    return salvar(user_id, body.model_dump())


@app.delete("/assinaturas/{sig_id}", summary="Remove assinatura do usuário")
async def deletar_assinatura_route(
    sig_id  : str,
    user_id : str = Depends(get_current_user),
):
    from modules.assinaturas import deletar
    if not deletar(sig_id, user_id):
        raise HTTPException(status_code=404, detail="Assinatura não encontrada")
    return {"removed": True, "id": sig_id}


# ── Admin ─────────────────────────────────────────────────────────────────────

@app.post("/admin/login", summary="Login do painel administrativo")
async def admin_login_route(body: AdminLoginRequest):
    if not ADMIN_EMAIL or not ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="Admin não configurado no servidor")
    if not login_admin(body.email, body.password):
        raise HTTPException(status_code=401, detail="Credenciais inválidas")
    return {"token": create_admin_token()}


@app.get("/admin/dashboard", summary="Métricas do painel admin")
async def admin_dashboard(_: str = Depends(get_current_admin)):
    conn = get_db()
    try:
        now        = datetime.now(timezone.utc)
        inicio_mes = f"{now.year}-{now.month:02d}-01"

        total_usuarios  = conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
        usuarios_ativos = conn.execute(
            "SELECT COUNT(*) FROM usuarios WHERE plano != 'trial'"
        ).fetchone()[0]
        creditos_mes = conn.execute(
            "SELECT COALESCE(SUM(delta), 0) FROM transacoes_credito "
            "WHERE delta > 0 AND created_at >= ? AND motivo != 'bonus_cadastro'",
            (inicio_mes,),
        ).fetchone()[0]
        receita_mes = conn.execute(
            "SELECT COALESCE(SUM(valor), 0) FROM pagamentos "
            "WHERE status = 'confirmado' AND created_at >= ?",
            (inicio_mes,),
        ).fetchone()[0]
        return {
            "total_usuarios":        total_usuarios,
            "usuarios_ativos":       usuarios_ativos,
            "creditos_vendidos_mes": int(creditos_mes),
            "receita_estimada_mes":  float(receita_mes),
        }
    finally:
        conn.close()


@app.get("/admin/config", summary="Lê configurações globais (admin)")
async def admin_config_get(_: str = Depends(get_current_admin)):
    c = get_all_config()
    return {
        "bonus_cadastro": int(c.get("bonus_cadastro", "3")),
        **_build_planos_config(c),
    }


@app.patch("/admin/config", summary="Salva configurações globais (admin)")
async def admin_config_update(
    body: AdminConfigRequest,
    _:    str = Depends(get_current_admin),
):
    updates = body.model_dump(exclude_none=True)
    for chave, valor in updates.items():
        set_config(chave, str(valor))
    return {"ok": True, "updated": list(updates.keys())}


@app.get("/admin/usuarios", summary="Lista usuários com paginação e busca")
async def admin_usuarios(
    page:   int = Query(1,  ge=1),
    limit:  int = Query(20, ge=1, le=100),
    search: str = Query(""),
    _:      str = Depends(get_current_admin),
):
    conn   = get_db()
    like   = f"%{search}%"
    offset = (page - 1) * limit
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM usuarios WHERE email LIKE ? OR COALESCE(nome,'') LIKE ?",
            (like, like),
        ).fetchone()[0]
        rows = conn.execute(
            """SELECT u.id, u.email, u.nome, u.plano, u.plano_renovacao, u.created_at,
                      COALESCE(c.saldo, 0) AS creditos
               FROM usuarios u
               LEFT JOIN creditos c ON c.user_id = u.id
               WHERE u.email LIKE ? OR COALESCE(u.nome,'') LIKE ?
               ORDER BY u.created_at DESC
               LIMIT ? OFFSET ?""",
            (like, like, limit, offset),
        ).fetchall()
        return {"total": total, "page": page, "items": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/admin/usuarios/{user_id}", summary="Detalhe de usuário com extrato de créditos")
async def admin_usuario_detail(
    user_id: str,
    _:       str = Depends(get_current_admin),
):
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT u.id, u.email, u.nome, u.plano, u.plano_renovacao, u.created_at,
                      COALESCE(c.saldo, 0) AS creditos
               FROM usuarios u
               LEFT JOIN creditos c ON c.user_id = u.id
               WHERE u.id = ?""",
            (user_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Usuário não encontrado")
        user           = dict(row)
        user["extrato"] = get_extrato(user_id, limit=10)
        return user
    finally:
        conn.close()


@app.post("/admin/usuarios/{user_id}/creditos", summary="Adiciona ou remove créditos de um usuário")
async def admin_creditos(
    user_id: str,
    body:    AdminCreditosRequest,
    _:       str = Depends(get_current_admin),
):
    if body.delta == 0:
        raise HTTPException(status_code=400, detail="Delta não pode ser zero")
    motivo = f"admin:{body.motivo}"
    try:
        if body.delta > 0:
            return creditar(user_id, body.delta, motivo)
        else:
            return debitar(user_id, abs(body.delta), motivo)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/admin/usuarios/{user_id}/plano", summary="Altera plano de um usuário")
async def admin_plano(
    user_id: str,
    body:    AdminPlanoRequest,
    _:       str = Depends(get_current_admin),
):
    planos_validos = ("trial", "essencial", "profissional", "escritorio")
    if body.plano not in planos_validos:
        raise HTTPException(status_code=400, detail=f"Plano inválido: {body.plano}")
    conn = get_db()
    try:
        if not conn.execute("SELECT id FROM usuarios WHERE id = ?", (user_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Usuário não encontrado")
        conn.execute(
            "UPDATE usuarios SET plano = ? WHERE id = ?", (body.plano, user_id)
        )
        conn.commit()
        if body.creditos is not None:
            conn.execute(
                "UPDATE creditos SET saldo = ? WHERE user_id = ?",
                (body.creditos, user_id),
            )
            conn.commit()
    finally:
        conn.close()
    return get_user_full(user_id)


@app.get("/admin/pagamentos", summary="Lista pagamentos com filtros (admin)")
async def admin_pagamentos(
    page:    int = Query(1,     ge=1),
    limit:   int = Query(20,    ge=1, le=100),
    gateway: str = Query("all"),
    status:  str = Query("all"),
    _:       str = Depends(get_current_admin),
):
    conn       = get_db()
    conditions = []
    params: list = []

    if gateway != "all":
        conditions.append("p.gateway = ?")
        params.append(gateway)
    if status != "all":
        conditions.append("p.status = ?")
        params.append(status)

    where  = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    offset = (page - 1) * limit

    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM pagamentos p {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT p.*, u.email AS user_email, u.nome AS user_nome
                FROM pagamentos p
                LEFT JOIN usuarios u ON u.id = p.user_id
                {where}
                ORDER BY p.created_at DESC
                LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
        return {"total": total, "page": page, "items": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/admin/pagamentos/{pagamento_id}/aprovar", summary="Aprova pagamento manualmente")
async def admin_aprovar(
    pagamento_id: str,
    _:            str = Depends(get_current_admin),
):
    try:
        return aprovar_pagamento(pagamento_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/admin/pagamentos/{pagamento_id}/reprovar", summary="Reprova pagamento manualmente")
async def admin_reprovar(
    pagamento_id: str,
    _:            str = Depends(get_current_admin),
):
    try:
        return reprovar_pagamento(pagamento_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Frontend (static files — deve ser o último mount) ─────────────────────────

_front_dir = Path(__file__).parent / "front"

def _app_js_version() -> str:
    """Retorna hash curto do app.js para cache busting."""
    p = _front_dir / "app.js"
    try:
        return hashlib.md5(p.read_bytes()).hexdigest()[:10]
    except Exception:
        return "0"

if _front_dir.exists():

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/index.html", response_class=HTMLResponse, include_in_schema=False)
    async def serve_index():
        html = (_front_dir / "index.html").read_text(encoding="utf-8")
        v    = _app_js_version()
        html = html.replace('src="app.js"', f'src="app.js?v={v}"')
        return HTMLResponse(
            content=html,
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    app.mount("/", StaticFiles(directory=str(_front_dir), html=True), name="front")

