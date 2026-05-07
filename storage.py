"""
storage.py — Leitura e escrita de sessões JSON no filesystem.

Toda mutação de estado passa pelo write_lock para serializar escritas
por processo. Limitação conhecida: não protege múltiplos workers.
Solução pós-MVP: fcntl ou migração de sessões para SQLite.
"""
import asyncio
import json
from pathlib import Path
from typing import List, Optional

DATA_DIR     = Path("/var/data/estabilizei")
SESSIONS_DIR = DATA_DIR / "sessions"
UPLOADS_DIR  = DATA_DIR / "uploads"

# Lock por processo — serializa todas as escritas de sessão
write_lock = asyncio.Lock()


def _session_path(user_id: str, session_id: str) -> Path:
    return SESSIONS_DIR / user_id / f"{session_id}.json"


def _ensure_dirs() -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


_ensure_dirs()


# ── Operações síncronas (usadas dentro do write_lock ou em to_thread) ─────────

def _read_session_sync(user_id: str, session_id: str) -> dict:
    path = _session_path(user_id, session_id)
    if not path.exists():
        raise FileNotFoundError(f"Sessão {session_id} não encontrada")
    return json.loads(path.read_text(encoding="utf-8"))


def _ensure_user_dir(user_id: str) -> None:
    """Garante que o diretório do usuário existe. Chamada uma vez na criação da sessão."""
    (SESSIONS_DIR / user_id).mkdir(parents=True, exist_ok=True)


def _write_session_sync(user_id: str, session_id: str, data: dict) -> None:
    path = _session_path(user_id, session_id)
    # JSON compacto: ~3x menor e ~10x mais rápido de serializar que indent=2
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ── API assíncrona pública ────────────────────────────────────────────────────

async def ensure_user_dir(user_id: str) -> None:
    """Garante que o diretório do usuário existe. Chamar uma vez antes da primeira escrita."""
    await asyncio.to_thread(_ensure_user_dir, user_id)


async def read_session(user_id: str, session_id: str) -> dict:
    return await asyncio.to_thread(_read_session_sync, user_id, session_id)


async def write_session(user_id: str, session_id: str, data: dict) -> None:
    await asyncio.to_thread(_write_session_sync, user_id, session_id, data)


async def session_exists(user_id: str, session_id: str) -> bool:
    return _session_path(user_id, session_id).exists()


async def list_user_sessions(user_id: str) -> List[dict]:
    user_dir = SESSIONS_DIR / user_id
    if not user_dir.exists():
        return []
    sessoes = []
    for f in sorted(user_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            sessoes.append({
                "session_id"  : data.get("session_id"),
                "session_name": data.get("session_name"),
                "created_at"  : data.get("created_at"),
                "periodo"     : data.get("config", {}).get("periodo"),
                "pinned"      : data.get("pinned", False),
            })
        except Exception:
            pass
    # Sessões fixadas primeiro — sort estável preserva ordem por mtime dentro de cada grupo
    sessoes.sort(key=lambda s: not s.get("pinned", False))
    return sessoes


async def delete_session(user_id: str, session_id: str) -> bool:
    path = _session_path(user_id, session_id)
    if path.exists():
        path.unlink()
        return True
    return False

