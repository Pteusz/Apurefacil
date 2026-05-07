"""
modules/auth.py — Autenticação por JWT, magic link e senha.

Fluxos disponíveis:
  1. Magic link  → POST /auth/magic-link + GET /auth/verify/{token}
  2. Senha       → POST /auth/login
  3. Dev token   → POST /auth/dev-token (ENV=development apenas)
"""
import hmac
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

from db import get_db

# ── Configuração ──────────────────────────────────────────────────────────────

JWT_SECRET         = os.environ.get("JWT_SECRET", "dev-secret-troque-em-producao")
JWT_ALGORITHM      = "HS256"
JWT_EXPIRE_DAYS    = int(os.environ.get("JWT_EXPIRE_DAYS", "30"))
BONUS_CADASTRO     = int(os.environ.get("BONUS_CADASTRO", "3"))
SMTP_HOST          = os.environ.get("SMTP_HOST", "smtp.hostinger.com")
SMTP_PORT          = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER          = os.environ.get("SMTP_USER", "")
SMTP_PASS          = os.environ.get("SMTP_PASS", "")
EMAIL_FROM         = os.environ.get("EMAIL_FROM", SMTP_USER)
FRONTEND_URL       = os.environ.get("FRONTEND_URL", "https://apurei.com")
ENV                = os.environ.get("ENV", "production")
MAGIC_LINK_TTL_MIN = 15

# ── Admin ─────────────────────────────────────────────────────────────────────

ADMIN_EMAIL            = os.environ.get("ADMIN_EMAIL", "")
ADMIN_PASSWORD         = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_JWT_SECRET       = os.environ.get("ADMIN_JWT_SECRET", "admin-secret-troque-em-producao")
ADMIN_JWT_EXPIRE_HOURS = 8

security       = HTTPBearer(auto_error=False)
admin_security = HTTPBearer(auto_error=False)
pwd_context    = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── Senha ─────────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── Token ─────────────────────────────────────────────────────────────────────

def create_access_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None


# ── Dependency do FastAPI ─────────────────────────────────────────────────────

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    if not credentials:
        raise HTTPException(status_code=401, detail="Token de autenticação necessário")
    user_id = decode_token(credentials.credentials)
    if not user_id:
        raise HTTPException(status_code=401, detail="Token inválido ou expirado")
    return user_id


# ── Usuários ──────────────────────────────────────────────────────────────────

def get_user_by_email(email: str) -> Optional[dict]:
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM usuarios WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_id(user_id: str) -> Optional[dict]:
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM usuarios WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_full(user_id: str) -> Optional[dict]:
    """Retorna usuário com plano e créditos. Não expõe senha_hash."""
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT u.id, u.email, u.nome, u.plano, u.plano_renovacao, u.created_at,
                   COALESCE(c.saldo, 0) AS creditos
            FROM usuarios u
            LEFT JOIN creditos c ON c.user_id = u.id
            WHERE u.id = ?
            """,
            (user_id,),
        ).fetchone()
        if not row:
            return None
        user = dict(row)
        user["user_id"] = user["id"]   # alias para compatibilidade com o frontend
        return user
    finally:
        conn.close()


def create_user(email: str, nome: Optional[str] = None) -> dict:
    """Cria usuário e concede créditos de boas-vindas."""
    user_id = f"usr_{uuid.uuid4().hex[:12]}"
    now     = datetime.now(timezone.utc).isoformat()
    conn    = get_db()
    try:
        conn.execute(
            "INSERT INTO usuarios (id, email, nome, created_at) VALUES (?, ?, ?, ?)",
            (user_id, email, nome, now),
        )
        conn.execute(
            "INSERT INTO creditos (user_id, saldo) VALUES (?, ?)",
            (user_id, BONUS_CADASTRO),
        )
        conn.execute(
            "INSERT INTO transacoes_credito (id, user_id, delta, motivo, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, user_id, BONUS_CADASTRO, "bonus_cadastro", now),
        )
        conn.commit()
        return {"id": user_id, "email": email, "nome": nome, "plano": "trial",
                "plano_renovacao": None, "created_at": now, "creditos": BONUS_CADASTRO,
                "user_id": user_id}
    finally:
        conn.close()


def get_or_create_user(email: str) -> dict:
    """Retorna usuário existente ou cria novo com créditos de boas-vindas."""
    user = get_user_by_email(email)
    if user:
        return user
    return create_user(email)


def update_user_name(user_id: str, nome: str) -> dict:
    conn = get_db()
    try:
        conn.execute("UPDATE usuarios SET nome = ? WHERE id = ?", (nome, user_id))
        conn.commit()
    finally:
        conn.close()
    return get_user_full(user_id)


def change_password(user_id: str, current_password: Optional[str], new_password: str) -> None:
    """
    Define ou altera a senha do usuário.
    Se o usuário já tem senha, current_password é obrigatório e deve estar correto.
    Se não tem senha (só magic link até agora), define diretamente.
    """
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    existing_hash = user.get("senha_hash")
    if existing_hash:
        if not current_password:
            raise HTTPException(status_code=400, detail="Senha atual obrigatória")
        if not verify_password(current_password, existing_hash):
            raise HTTPException(status_code=400, detail="Senha atual incorreta")

    new_hash = hash_password(new_password)
    conn = get_db()
    try:
        conn.execute("UPDATE usuarios SET senha_hash = ? WHERE id = ?", (new_hash, user_id))
        conn.commit()
    finally:
        conn.close()


def login_with_password(email: str, password: str) -> Optional[dict]:
    """Autentica por email + senha. Retorna usuário ou None."""
    user = get_user_by_email(email)
    if not user:
        return None
    senha_hash = user.get("senha_hash")
    if not senha_hash:
        return None  # Usuário existe mas só tem magic link
    if not verify_password(password, senha_hash):
        return None
    return get_user_full(user["id"])


# ── Magic link ────────────────────────────────────────────────────────────────

def magic_link_send(email: str) -> None:
    """
    Cria ou recupera o usuário, gera token e envia email via SMTP.
    Em ENV=development e sem credenciais SMTP, imprime o link no stdout.

    Lê SMTP_USER / SMTP_PASS em tempo de execução (não no import) para
    garantir que alterações no docker-compose.yml sejam sempre refletidas.
    """
    # Leitura lazy — garante que o valor atual da variável de ambiente seja usado
    smtp_user  = os.environ.get("SMTP_USER", "")
    smtp_pass  = os.environ.get("SMTP_PASS", "")
    smtp_host  = os.environ.get("SMTP_HOST", "smtp.hostinger.com")
    smtp_port  = int(os.environ.get("SMTP_PORT", "465"))
    email_from = os.environ.get("EMAIL_FROM", smtp_user)
    env        = os.environ.get("ENV", "production")
    frontend   = os.environ.get("FRONTEND_URL", "https://apurei.com")

    user       = get_or_create_user(email)
    token      = secrets.token_urlsafe(32)
    expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=MAGIC_LINK_TTL_MIN)
    ).isoformat()

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO magic_links (token, user_id, expires_at, used) VALUES (?, ?, ?, 0)",
            (token, user["id"], expires_at),
        )
        conn.commit()
    finally:
        conn.close()

    link = f"{frontend}?magic={token}"

    if not smtp_user or not smtp_pass:
        # Modo desenvolvimento: exibe link no terminal
        print(f"\n[DEV] Magic link para {email}:\n{link}\n")
        if env == "production":
            raise HTTPException(
                status_code=503,
                detail="Serviço de email não configurado. Defina SMTP_USER e SMTP_PASS.",
            )
        return

    html = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px">
      <h2 style="font-size:20px;font-weight:600;color:#1a1a1a;margin-bottom:8px">
        Seu link de acesso
      </h2>
      <p style="color:#666;font-size:14px;margin-bottom:24px">
        Clique no botão abaixo para entrar no Apurei. O link expira em {MAGIC_LINK_TTL_MIN} minutos.
      </p>
      <a href="{link}"
         style="display:inline-block;padding:12px 24px;background:#1a1a1a;color:#fff;
                text-decoration:none;border-radius:8px;font-size:14px;font-weight:500">
        Entrar no Apurei
      </a>
      <p style="color:#999;font-size:12px;margin-top:24px">
        Se você não solicitou este acesso, ignore este email.
      </p>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Seu link de acesso — Apurei"
    msg["From"]    = email_from or smtp_user
    msg["To"]      = email
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [email], msg.as_string())


def magic_link_verify(token: str) -> Optional[dict]:
    """
    Valida o token do magic link. Retorna o usuário ou None se inválido/expirado.
    Marca o token como usado ao validar.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM magic_links WHERE token = ? AND used = 0",
            (token,),
        ).fetchone()

        if not row:
            return None

        link = dict(row)
        expires_at = datetime.fromisoformat(link["expires_at"])
        if datetime.now(timezone.utc) > expires_at:
            return None

        conn.execute(
            "UPDATE magic_links SET used = 1 WHERE token = ?",
            (token,),
        )
        conn.commit()

        return get_user_full(link["user_id"])
    finally:
        conn.close()


# ── Admin auth ────────────────────────────────────────────────────────────────

def login_admin(email: str, password: str) -> bool:
    """Valida credenciais de admin contra as variáveis de ambiente."""
    if not ADMIN_EMAIL or not ADMIN_PASSWORD:
        return False
    return (
        hmac.compare_digest(email, ADMIN_EMAIL)
        and hmac.compare_digest(password, ADMIN_PASSWORD)
    )


def create_admin_token() -> str:
    payload = {
        "sub":  "admin",
        "role": "admin",
        "exp":  datetime.now(timezone.utc) + timedelta(hours=ADMIN_JWT_EXPIRE_HOURS),
        "iat":  datetime.now(timezone.utc),
    }
    return jwt.encode(payload, ADMIN_JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_admin_token(token: str) -> bool:
    try:
        payload = jwt.decode(token, ADMIN_JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("role") == "admin"
    except JWTError:
        return False


async def get_current_admin(
    credentials: HTTPAuthorizationCredentials = Security(admin_security),
) -> str:
    if not credentials:
        raise HTTPException(status_code=401, detail="Token admin necessário")
    if not decode_admin_token(credentials.credentials):
        raise HTTPException(status_code=401, detail="Token admin inválido ou expirado")
    return "admin"

