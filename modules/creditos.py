"""
modules/creditos.py — Gestão de créditos do usuário.

Webhooks de pagamento (Mercado Pago, InfinityPay):
  Estrutura presente, lógica de validação de assinatura fora de escopo.
  Cada provedor tem seu endpoint com stub documentado.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

from db import get_db


# ── Consulta ──────────────────────────────────────────────────────────────────

def get_saldo(user_id: str) -> int:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT saldo FROM creditos WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["saldo"] if row else 0
    finally:
        conn.close()


def get_extrato(user_id: str, limit: int = 20) -> list:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM transacoes_credito WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Mutação ───────────────────────────────────────────────────────────────────

def _registrar_transacao(
    conn,
    user_id:      str,
    delta:        int,
    motivo:       str,
    ref_externa:  Optional[str] = None,
) -> dict:
    """Atualiza saldo e registra transação. Levanta ValueError se saldo insuficiente."""
    row = conn.execute(
        "SELECT saldo FROM creditos WHERE user_id = ?", (user_id,)
    ).fetchone()

    saldo_atual = row["saldo"] if row else 0

    if delta < 0 and saldo_atual + delta < 0:
        raise ValueError(f"Saldo insuficiente: {saldo_atual} crédito(s)")

    novo_saldo = saldo_atual + delta
    now        = datetime.now(timezone.utc).isoformat()
    tx_id      = uuid.uuid4().hex

    if row:
        conn.execute(
            "UPDATE creditos SET saldo = ? WHERE user_id = ?",
            (novo_saldo, user_id),
        )
    else:
        conn.execute(
            "INSERT INTO creditos (user_id, saldo) VALUES (?, ?)",
            (user_id, novo_saldo),
        )

    conn.execute(
        "INSERT INTO transacoes_credito (id, user_id, delta, motivo, ref_externa, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tx_id, user_id, delta, motivo, ref_externa, now),
    )
    conn.commit()

    return {
        "id"         : tx_id,
        "user_id"    : user_id,
        "delta"      : delta,
        "saldo_apos" : novo_saldo,
        "motivo"     : motivo,
        "ref_externa": ref_externa,
        "created_at" : now,
    }


def debitar(user_id: str, quantidade: int = 1, motivo: str = "apuracao") -> dict:
    """Debita créditos para uso do sistema. Levanta HTTPException 402 se insuficiente."""
    conn = get_db()
    try:
        return _registrar_transacao(conn, user_id, -quantidade, motivo)
    except ValueError as e:
        raise HTTPException(status_code=402, detail=str(e))
    finally:
        conn.close()


def creditar(user_id: str, quantidade: int, motivo: str, ref_externa: Optional[str] = None) -> dict:
    """Credita créditos (pagamento confirmado, bônus, etc.)."""
    conn = get_db()
    try:
        return _registrar_transacao(conn, user_id, quantidade, motivo, ref_externa)
    finally:
        conn.close()


# ── Webhooks (estrutura presente, validação de assinatura TODO) ───────────────

def processar_webhook_mercadopago(payload: dict) -> dict:
    """
    TODO: implementar na próxima conversa junto com o plano de pagamentos.

    Fluxo esperado:
      1. Validar assinatura HMAC do header x-signature
      2. Identificar tipo do evento: payment.updated, payment.created
      3. Buscar pagamento via API do MP: GET /v1/payments/{id}
      4. Verificar status == "approved"
      5. Mapear valor para quantidade de créditos (tabela de planos)
      6. creditar(user_id, quantidade, "mercadopago", ref_externa=payment_id)

    Referência: https://www.mercadopago.com.br/developers/pt/docs/your-integrations/notifications/webhooks
    """
    action = payload.get("action")
    data   = payload.get("data", {})
    return {
        "recebido": True,
        "action"  : action,
        "data_id" : data.get("id"),
        "status"  : "webhook_nao_processado_ainda",
    }


def processar_webhook_infinitypay(payload: dict) -> dict:
    """
    TODO: implementar junto com o plano de pagamentos.

    Fluxo esperado:
      1. Validar token de autenticação do header
      2. Verificar status do pagamento
      3. Mapear para créditos
      4. creditar(user_id, quantidade, "infinitypay", ref_externa=tx_id)
    """
    return {
        "recebido": True,
        "status"  : "webhook_nao_processado_ainda",
    }
