"""
Cliente Supabase compartido para todos los scripts del sistema.
Lee credenciales desde variables de entorno (.env local o GitHub Secrets).
"""
import os
from pathlib import Path

# Cargar .env si existe
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        if _line.strip() and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")   # service_role para escritura


def get_client():
    """Devuelve cliente Supabase o None si no hay credenciales."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        from supabase import create_client
        return create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Supabase: no se pudo conectar — {e}")
        return None
