"""
modules/pagamentos.py — Criação e processamento de pagamentos.

Mercado Pago : mensalidades (preapproval/assinatura mensal, cartão)
InfinityPay  : créditos avulsos + planos anuais (PIX, checkout hospedado)

Fluxo Mercado Pago (mensal):
  1. Backend chama POST /preapproval na API do MP com preapproval_plan_id + external_reference
  2. Retorna init_point → frontend redireciona o usuário
  3. MP envia webhook subscription_preapproval (ativação) ou payment (renovação)
  4. Backend ativa plano e credita créditos mensais

Fluxo InfinityPay (avulso + anual):
  1. Backend cria registro interno e monta link de checkout
  2. Frontend redireciona → usuário paga no checkout IP
  3. IP redireciona para obrigado.html e envia webhook
  4. Backend valida e credita/ativa
"""
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import HTTPException

from db import get_db
from modules.creditos import creditar

# ─── Configuração ─────────────────────────────────────────────────────────────

MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN", "")
FRONTEND_URL    = os.environ.get("FRONTEND_URL", "https://apurei.com")

# IDs de plano do Mercado Pago (preapproval_plan_id)
MP_PLAN_IDS = {
    "essencial":    os.environ.get("MP_PLAN_ESSENCIAL",    "6987f6eec50141d5b56116b5f8dc5504"),
    "profissional": os.environ.get("MP_PLAN_PROFISSIONAL", "626edb1c7ad148f0b37f18217229d761"),
    "escritorio":   os.environ.get("MP_PLAN_ESCRITORIO",   "feb6ae56d1ca422d9b1f77c591419b5b"),
}

# Créditos mensais inclusos por plano
PLAN_CREDITS = {
    "essencial":    15,
    "profissional": 35,
    "escritorio":   70,
}

# Preço do pacote avulso (10 créditos) conforme plano do usuário
AVULSO_VALOR = {
    "trial":        99.00,
    "essencial":    99.00,
    "profissional": 85.00,
    "escritorio":   70.00,
}

# Preços anuais por plano (10 meses cobrado = 2 meses grátis)
ANUAL_VALOR = {
    "essencial":    1490.00,
    "profissional": 2890.00,
    "escritorio":   4790.00,
}

# InfinityPay — sem API key, autenticação por handle (tag pública do merchant)
INFINITYPAY_TAG       = os.environ.get("INFINITYPAY_TAG", "estabilizei")
INFINITYPAY_LINKS_URL = "https://api.infinitepay.io/invoices/public/checkout/links"
BACKEND_URL           = os.environ.get("BACKEND_URL", "https://backend.estabilizei.com.br")


# ─── Helpers internos ─────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expires_at(days: int = 32) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _criar_pagamento(
    user_id:        str,
    tipo:           str,
    valor:          float,
    gateway:        str,
    ref_externa:    Optional[str] = None,
    preapproval_id: Optional[str] = None,
) -> dict:
    """Cria registro de pagamento com status=pendente. Retorna dict com id e dados básicos."""
    pag_id = uuid.uuid4().hex
    now    = _now()
    conn   = get_db()
    try:
        conn.execute(
            """INSERT INTO pagamentos
               (id, user_id, tipo, valor, status, ref_externa, gateway, created_at, preapproval_id)
               VALUES (?, ?, ?, ?, 'pendente', ?, ?, ?, ?)""",
            (pag_id, user_id, tipo, valor, ref_externa, gateway, now, preapproval_id),
        )
        conn.commit()
        return {
            "id":      pag_id,
            "tipo":    tipo,
            "valor":   valor,
            "status":  "pendente",
            "gateway": gateway,
        }
    finally:
        conn.close()


def _atualizar_status(
    pagamento_id: str,
    status:       str,
    metadata:     Optional[dict] = None,
) -> None:
    conn = get_db()
    try:
        conn.execute(
            "UPDATE pagamentos SET status = ?, metadata = ? WHERE id = ?",
            (status, json.dumps(metadata) if metadata else None, pagamento_id),
        )
        conn.commit()
    finally:
        conn.close()


def _ativar_plano(user_id: str, plano: str, expires_at: str) -> None:
    """Ativa/renova plano do usuário e credita os créditos mensais inclusos."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE usuarios SET plano = ?, plano_renovacao = ? WHERE id = ?",
            (plano, expires_at, user_id),
        )
        conn.commit()
    finally:
        conn.close()

    creditos = PLAN_CREDITS.get(plano, 0)
    if creditos > 0:
        creditar(user_id, creditos, f"plano:{plano}")


# ─── Consultas públicas ────────────────────────────────────────────────────────

def get_pagamento(pagamento_id: str, user_id: Optional[str] = None) -> Optional[dict]:
    conn = get_db()
    try:
        if user_id:
            row = conn.execute(
                "SELECT * FROM pagamentos WHERE id = ? AND user_id = ?",
                (pagamento_id, user_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM pagamentos WHERE id = ?", (pagamento_id,)
            ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def listar_pagamentos(user_id: str, limit: int = 20) -> list:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM pagamentos WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ─── Criação de checkout — Mercado Pago ───────────────────────────────────────

async def criar_assinatura_mp(user_id: str, email: str, plano: str) -> dict:
    """
    Cria preapproval no Mercado Pago e retorna {pagamento_id, init_point}.
    O usuário deve ser redirecionado para init_point para assinar.
    """
    plan_id = MP_PLAN_IDS.get(plano)
    if not plan_id:
        raise HTTPException(status_code=400, detail=f"Plano inválido: {plano}")
    if not MP_ACCESS_TOKEN:
        raise HTTPException(status_code=503, detail="Pagamentos temporariamente indisponíveis")
    if not email:
        raise HTTPException(status_code=400, detail="E-mail do usuário necessário para criar assinatura")

    # Cria registro interno antes da chamada externa para ter o ID
    pag = _criar_pagamento(user_id, f"mp_mensal_{plano}", 0.0, "mercadopago")

    back_url = (
        f"{FRONTEND_URL}/obrigado.html"
        f"?payment_id={pag['id']}&plano={plano}&gateway=mp"
    )

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            "https://api.mercadopago.com/preapproval",
            headers={
                "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
                "Content-Type":  "application/json",
            },
            json={
                "preapproval_plan_id": plan_id,
                "reason":              f"Estabilizei — {plano.capitalize()} Mensal",
                "external_reference":  f"{user_id}|{pag['id']}",
                "payer_email":         email,
                "back_url":            back_url,
                "status":              "pending",
            },
        )

    if resp.status_code not in (200, 201):
        mp_error = resp.text
        _atualizar_status(pag["id"], "erro_criacao", {"mp_response": mp_error})
        raise HTTPException(
            status_code=502,
            detail=f"Erro ao criar assinatura no Mercado Pago: [{resp.status_code}] {mp_error}",
        )

    data           = resp.json()
    preapproval_id = data.get("id")
    init_point     = data.get("init_point")

    conn = get_db()
    try:
        conn.execute(
            "UPDATE pagamentos SET preapproval_id = ?, ref_externa = ? WHERE id = ?",
            (preapproval_id, preapproval_id, pag["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "pagamento_id": pag["id"],
        "init_point":   init_point,
        "plano":        plano,
    }


# ─── Criação de checkout — InfinityPay ────────────────────────────────────────

async def criar_checkout_ip(
    user_id:     str,
    plano_atual: str,
    tipo:        str,
    user_nome:   str  = "",
    user_email:  str  = "",
    plano:       Optional[str] = None,
) -> dict:
    """
    Cria link de pagamento na InfinityPay via endpoint público.
    Não requer API key — autenticação é feita pelo handle (INFINITYPAY_TAG).

    tipo: 'avulso' | 'anual'
    plano: obrigatório quando tipo='anual'
    """
    if tipo == "avulso":
        valor     = AVULSO_VALOR.get(plano_atual, 99.00)
        tipo_str  = "ip_avulso"
        descricao = "Créditos avulsos — 10 apurações"
    elif tipo == "anual" and plano:
        valor     = ANUAL_VALOR.get(plano, 0.0)
        tipo_str  = f"ip_anual_{plano}"
        descricao = f"Estabilizei {plano.capitalize()} — Plano Anual"
    else:
        raise HTTPException(status_code=400, detail="Tipo ou plano inválido")

    pag       = _criar_pagamento(user_id, tipo_str, valor, "infinitypay")
    order_nsu = f"EST-{pag['id']}"

    conn = get_db()
    try:
        conn.execute(
            "UPDATE pagamentos SET ref_externa = ? WHERE id = ?",
            (order_nsu, pag["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    redirect_url = f"{FRONTEND_URL}/obrigado.html?payment_id={pag['id']}&gateway=ip"
    webhook_url  = f"{BACKEND_URL}/webhooks/infinitypay"

    payload = {
        "handle":       INFINITYPAY_TAG,
        "order_nsu":    order_nsu,
        "webhook_url":  webhook_url,
        "redirect_url": redirect_url,
        "customer": {
            "name":  user_nome  or "Cliente",
            "email": user_email or "",
        },
        "items": [
            {
                "quantity":    1,
                "price":       int(round(valor * 100)),   # centavos
                "description": descricao,
            }
        ],
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            INFINITYPAY_LINKS_URL,
            headers={"Content-Type": "application/json"},
            json=payload,
        )

    if resp.status_code not in (200, 201):
        _atualizar_status(pag["id"], "erro_criacao", {"ip_response": resp.text})
        raise HTTPException(status_code=502, detail="Erro ao criar link de pagamento na InfinityPay")

    data         = resp.json()
    checkout_url = data.get("url")
    invoice_slug = data.get("invoice_slug")

    # Salva invoice_slug para referência futura (polling direto à API se necessário)
    if invoice_slug:
        conn = get_db()
        try:
            conn.execute(
                "UPDATE pagamentos SET metadata = ? WHERE id = ?",
                (json.dumps({"invoice_slug": invoice_slug}), pag["id"]),
            )
            conn.commit()
        finally:
            conn.close()

    return {
        "pagamento_id": pag["id"],
        "checkout_url": checkout_url,
        "order_nsu":    order_nsu,
        "valor":        valor,
    }


# ─── Webhook: Mercado Pago ────────────────────────────────────────────────────

async def processar_webhook_mp(payload: dict) -> dict:
    """
    Processa notificações do Mercado Pago.
    Suporta eventos: subscription_preapproval (ativação/cancelamento) e payment (renovação).
    Payload pode chegar via JSON body ou via query string — lidar com ambos.
    """
    topic  = payload.get("type") or payload.get("topic", "")
    data   = payload.get("data", {})
    obj_id = (data.get("id") if isinstance(data, dict) else None) or payload.get("id")

    if not obj_id:
        return {"ok": True, "skipped": "sem_id"}

    if topic in ("subscription_preapproval", "preapproval"):
        return await _processar_preapproval_mp(str(obj_id))

    if topic == "payment":
        return await _processar_payment_mp(str(obj_id))

    return {"ok": True, "skipped": f"topic_ignorado:{topic}"}


async def _processar_preapproval_mp(preapproval_id: str) -> dict:
    """Busca preapproval na API do MP e ativa/pausa/cancela plano."""
    if not MP_ACCESS_TOKEN:
        return {"ok": False, "error": "MP_ACCESS_TOKEN ausente"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"https://api.mercadopago.com/preapproval/{preapproval_id}",
            headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
        )

    if resp.status_code != 200:
        return {"ok": False, "error": f"MP retornou {resp.status_code}"}

    data    = resp.json()
    status  = data.get("status")           # authorized | paused | cancelled
    ext_ref = data.get("external_reference", "")

    # Identifica usuário — cascata de 3 tentativas
    user_id = None

    # 1. external_reference = "user_id|pagamento_id"
    if "|" in ext_ref:
        user_id = ext_ref.split("|")[0]
    elif ext_ref:
        user_id = ext_ref

    # 2. preapproval_id já registrado em pagamentos
    if not user_id:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT user_id FROM pagamentos WHERE preapproval_id = ?",
                (preapproval_id,),
            ).fetchone()
            user_id = row["user_id"] if row else None
        finally:
            conn.close()

    # 3. Fallback: payer_email
    if not user_id:
        payer_email = data.get("payer_email")
        if payer_email:
            conn = get_db()
            try:
                row = conn.execute(
                    "SELECT id FROM usuarios WHERE email = ?", (payer_email,)
                ).fetchone()
                user_id = row["id"] if row else None
            finally:
                conn.close()

    if not user_id:
        return {"ok": False, "error": "usuario_nao_identificado"}

    # Identifica plano pelo preapproval_plan_id
    plan_id = data.get("preapproval_plan_id", "")
    plano   = next((k for k, v in MP_PLAN_IDS.items() if v == plan_id), None)

    if status == "authorized" and plano:
        next_payment = data.get("next_payment_date")
        if next_payment:
            try:
                expires = (
                    datetime.fromisoformat(next_payment.replace("Z", "+00:00"))
                    + timedelta(days=2)
                ).isoformat()
            except Exception:
                expires = _expires_at(32)
        else:
            expires = _expires_at(32)

        _ativar_plano(user_id, plano, expires)

        # Atualiza status do pagamento
        pagamento_id = ext_ref.split("|")[1] if "|" in ext_ref else None
        if pagamento_id:
            _atualizar_status(pagamento_id, "confirmado", {"preapproval_id": preapproval_id})
        else:
            conn = get_db()
            try:
                row = conn.execute(
                    "SELECT id FROM pagamentos WHERE preapproval_id = ? AND user_id = ?",
                    (preapproval_id, user_id),
                ).fetchone()
                if row:
                    _atualizar_status(row["id"], "confirmado")
            finally:
                conn.close()

        return {"ok": True, "acao": "plano_ativado", "plano": plano, "user_id": user_id}

    if status == "cancelled":
        # Mantém expires_at — não corta acesso imediatamente
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT id FROM pagamentos WHERE preapproval_id = ? AND user_id = ?",
                (preapproval_id, user_id),
            ).fetchone()
            if row:
                _atualizar_status(row["id"], "cancelado")
        finally:
            conn.close()
        return {"ok": True, "acao": "assinatura_cancelada", "user_id": user_id}

    return {"ok": True, "skipped": f"status:{status}"}


async def _processar_payment_mp(payment_id: str) -> dict:
    """Processa cobrança mensal recorrente do Mercado Pago (renovação)."""
    if not MP_ACCESS_TOKEN:
        return {"ok": False, "error": "MP_ACCESS_TOKEN ausente"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"https://api.mercadopago.com/v1/payments/{payment_id}",
            headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
        )

    if resp.status_code != 200:
        return {"ok": False, "error": f"MP retornou {resp.status_code}"}

    data   = resp.json()
    status = data.get("status")
    if status != "approved":
        return {"ok": True, "skipped": f"status:{status}"}

    # Identifica usuário
    ext_ref  = data.get("external_reference", "")
    metadata = data.get("metadata") or {}
    sub_id   = (
        metadata.get("preapproval_id")
        or metadata.get("subscription_id")
        or data.get("preapproval_id")
    )

    user_id = None
    if "|" in ext_ref:
        user_id = ext_ref.split("|")[0]
    elif ext_ref:
        user_id = ext_ref

    if not user_id and sub_id:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT user_id FROM pagamentos WHERE preapproval_id = ?", (sub_id,)
            ).fetchone()
            user_id = row["user_id"] if row else None
        finally:
            conn.close()

    if not user_id:
        return {"ok": False, "error": "usuario_nao_identificado"}

    # Descobre plano atual do usuário
    conn = get_db()
    try:
        row   = conn.execute("SELECT plano FROM usuarios WHERE id = ?", (user_id,)).fetchone()
        plano = row["plano"] if row else None
    finally:
        conn.close()

    if plano and plano != "trial":
        expires = _expires_at(32)
        _ativar_plano(user_id, plano, expires)
        # Registra transação de renovação
        pag = _criar_pagamento(
            user_id,
            f"mp_renovacao_{plano}",
            float(data.get("transaction_amount", 0)),
            "mercadopago",
            ref_externa=payment_id,
        )
        _atualizar_status(pag["id"], "confirmado", {"payment_id": payment_id})
        return {"ok": True, "acao": "plano_renovado", "plano": plano, "user_id": user_id}

    return {"ok": True, "skipped": "plano_nao_identificado"}


# ─── Webhook: InfinityPay ─────────────────────────────────────────────────────

def processar_webhook_ip(payload: dict) -> dict:
    """
    Valida e processa webhook da InfinityPay.

    Campos esperados: order_nsu, paid, paid_amount, transaction_nsu, receipt_url
    """
    paid        = payload.get("paid")
    order_nsu   = payload.get("order_nsu", "")
    paid_amount = float(payload.get("paid_amount") or 0)
    tx_nsu      = payload.get("transaction_nsu", "")

    if not paid:
        return {"ok": True, "skipped": "nao_pago"}

    if not order_nsu.startswith("EST-"):
        return {"ok": False, "error": "order_nsu_invalido"}

    pagamento_id = order_nsu[4:]   # Remove prefixo "EST-"
    pag          = get_pagamento(pagamento_id)

    if not pag:
        return {"ok": False, "error": "pagamento_nao_encontrado"}

    if pag["status"] != "pendente":
        return {"ok": True, "skipped": "ja_processado"}

    # Proteção contra duplicatas via transaction_nsu
    if tx_nsu:
        conn = get_db()
        try:
            existing = conn.execute(
                "SELECT id FROM pagamentos WHERE transaction_nsu = ?", (tx_nsu,)
            ).fetchone()
            if existing:
                return {"ok": True, "skipped": "duplicata"}
            conn.execute(
                "UPDATE pagamentos SET transaction_nsu = ? WHERE id = ?",
                (tx_nsu, pagamento_id),
            )
            conn.commit()
        finally:
            conn.close()

    # Valida valor (tolerância ±R$0,01)
    if pag["valor"] > 0 and abs(paid_amount - pag["valor"]) > 0.01:
        return {
            "ok":    False,
            "error": f"valor_divergente:{paid_amount}!={pag['valor']}",
        }

    user_id  = pag["user_id"]
    tipo     = pag["tipo"]
    metadata = {
        "order_nsu":      order_nsu,
        "transaction_nsu": tx_nsu,
        "receipt_url":    payload.get("receipt_url"),
    }

    _atualizar_status(pagamento_id, "confirmado", metadata)

    if tipo == "ip_avulso":
        creditar(user_id, 10, "creditos_avulsos", ref_externa=order_nsu)
        return {"ok": True, "acao": "creditos_creditados", "quantidade": 10, "user_id": user_id}

    if tipo.startswith("ip_anual_"):
        plano   = tipo.replace("ip_anual_", "")
        expires = _expires_at(366)
        _ativar_plano(user_id, plano, expires)
        return {"ok": True, "acao": "plano_anual_ativado", "plano": plano, "user_id": user_id}

    return {"ok": True, "skipped": f"tipo_desconhecido:{tipo}"}


# ─── Aprovação/reprovação manual (admin) ──────────────────────────────────────

def aprovar_pagamento(pagamento_id: str) -> dict:
    """
    Aprova manualmente um pagamento pendente.
      ip_avulso         → credita N créditos (avulso_quantidade da config)
      ip_anual_{plano}  → ativa plano anual (+366 dias)
      mp_mensal_{plano} → ativa plano mensal (+32 dias)
      mp_renovacao_*    → renova plano (+32 dias)
    """
    from db import get_config  # import local para evitar circular

    pag = get_pagamento(pagamento_id)
    if not pag:
        raise ValueError("Pagamento não encontrado")
    if pag["status"] == "confirmado":
        raise ValueError("Pagamento já confirmado")

    user_id = pag["user_id"]
    tipo    = pag["tipo"]

    if tipo == "ip_avulso":
        qtd = int(get_config("avulso_quantidade", "10"))
        creditar(user_id, qtd, "admin:avulso_aprovado", ref_externa=pagamento_id)
    elif tipo.startswith("ip_anual_"):
        plano = tipo.replace("ip_anual_", "")
        _ativar_plano(user_id, plano, _expires_at(366))
    elif tipo.startswith("mp_mensal_"):
        plano = tipo.replace("mp_mensal_", "")
        _ativar_plano(user_id, plano, _expires_at(32))
    elif tipo.startswith("mp_renovacao_"):
        plano = tipo.replace("mp_renovacao_", "")
        _ativar_plano(user_id, plano, _expires_at(32))

    _atualizar_status(pagamento_id, "confirmado", {"aprovado_manualmente": True})
    return {"ok": True, "tipo": tipo, "user_id": user_id}


def reprovar_pagamento(pagamento_id: str) -> dict:
    """Reprova manualmente um pagamento (só muda status, não reverte créditos)."""
    pag = get_pagamento(pagamento_id)
    if not pag:
        raise ValueError("Pagamento não encontrado")
    _atualizar_status(pagamento_id, "reprovado", {"reprovado_manualmente": True})
    return {"ok": True, "pagamento_id": pagamento_id}

