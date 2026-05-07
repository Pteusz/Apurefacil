"""
db.py — Conexão SQLite e migrations automáticas na inicialização.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("/var/data/estabilizei/db.sqlite")

_DDL = """
CREATE TABLE IF NOT EXISTS usuarios (
    id          TEXT PRIMARY KEY,
    email       TEXT UNIQUE NOT NULL,
    nome        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS magic_links (
    token       TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used        INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS creditos (
    user_id     TEXT PRIMARY KEY,
    saldo       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transacoes_credito (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    delta       INTEGER NOT NULL,
    motivo      TEXT NOT NULL,
    ref_externa TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pagamentos (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    tipo        TEXT NOT NULL,
    valor       REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pendente',
    ref_externa TEXT,
    gateway     TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config (
    chave       TEXT PRIMARY KEY,
    valor       TEXT NOT NULL,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS assinaturas (
    id              TEXT PRIMARY KEY,
    bank_name       TEXT NOT NULL,
    font_dominant   TEXT,
    anchor_x_min    REAL NOT NULL,
    anchor_x_max    REAL NOT NULL,
    anchor_pattern  TEXT NOT NULL,
    value_x_min     REAL NOT NULL,
    value_x_max     REAL NOT NULL,
    signal_logic    TEXT NOT NULL DEFAULT 'prefix_minus',
    source          TEXT NOT NULL DEFAULT 'user',
    layout_version  INTEGER NOT NULL DEFAULT 1,
    created_by      TEXT,
    validated       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    campos_json     TEXT
);
"""

# Migrations para colunas adicionadas após a criação inicial da tabela.
# ALTER TABLE não suporta IF NOT EXISTS no SQLite < 3.37 — usamos try/except.
_MIGRATIONS = [
    "ALTER TABLE usuarios ADD COLUMN senha_hash TEXT",
    "ALTER TABLE usuarios ADD COLUMN plano TEXT NOT NULL DEFAULT 'trial'",
    "ALTER TABLE usuarios ADD COLUMN plano_renovacao TEXT",
    # pagamentos — colunas adicionadas para rastreio de webhooks e assinaturas
    "ALTER TABLE pagamentos ADD COLUMN transaction_nsu TEXT",
    "ALTER TABLE pagamentos ADD COLUMN preapproval_id TEXT",
    "ALTER TABLE pagamentos ADD COLUMN expires_at TEXT",
    "ALTER TABLE pagamentos ADD COLUMN metadata TEXT",
    # assinaturas — colunas de descrição nomeadas pelo usuário
    "ALTER TABLE assinaturas ADD COLUMN campos_json TEXT",
    # assinaturas — limites do retângulo original desenhado pelo usuário
    "ALTER TABLE assinaturas ADD COLUMN rect_x0 REAL",
    "ALTER TABLE assinaturas ADD COLUMN rect_x1 REAL",
]

# Valores iniciais da tabela config (inseridos apenas se a chave não existir)
_CONFIG_SEED = {
    "bonus_cadastro":                 "3",
    "plano_essencial_creditos":       "15",
    "plano_essencial_preco_mensal":   "149",
    "plano_essencial_preco_anual":    "124",
    "plano_essencial_preco_avulso":   "9.90",
    "plano_profissional_creditos":    "35",
    "plano_profissional_preco_mensal":"289",
    "plano_profissional_preco_anual": "241",
    "plano_profissional_preco_avulso":"8.50",
    "plano_escritorio_creditos":      "70",
    "plano_escritorio_preco_mensal":  "479",
    "plano_escritorio_preco_anual":   "399",
    "plano_escritorio_preco_avulso":  "7.00",
    "avulso_quantidade":              "10",
    "avulso_preco":                   "99.00",
}


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Cria as tabelas e executa migrations. Chamado no startup do app."""
    conn = get_db()
    try:
        conn.executescript(_DDL)
        conn.commit()
        _run_migrations(conn)
        _seed_config(conn)
    finally:
        conn.close()


def _run_migrations(conn: sqlite3.Connection) -> None:
    for sql in _MIGRATIONS:
        try:
            conn.execute(sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Coluna já existe — migration já aplicada


def _seed_config(conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for chave, valor in _CONFIG_SEED.items():
        conn.execute(
            "INSERT OR IGNORE INTO config (chave, valor, updated_at) VALUES (?, ?, ?)",
            (chave, valor, now),
        )
    conn.commit()


# ── Config helpers ────────────────────────────────────────────────────────────

def get_config(chave: str, default: str = "") -> str:
    """Retorna o valor de uma chave de configuração."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT valor FROM config WHERE chave = ?", (chave,)
        ).fetchone()
        return row["valor"] if row else default
    finally:
        conn.close()


def set_config(chave: str, valor: str) -> None:
    """Salva ou atualiza uma chave de configuração."""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO config (chave, valor, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor, updated_at = excluded.updated_at",
            (chave, valor, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_all_config() -> dict:
    """Retorna todas as configurações como dicionário chave→valor."""
    conn = get_db()
    try:
        rows = conn.execute("SELECT chave, valor FROM config").fetchall()
        return {r["chave"]: r["valor"] for r in rows}
    finally:
        conn.close()

