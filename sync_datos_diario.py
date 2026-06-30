"""
Sync diario de datos Pegasus -> GitHub -> Streamlit Cloud
Sin email. Solo descarga los ultimos 2 dias y pushea la DB.
Programado: todos los dias a las 7:00 AM (excepto Lun/Vie que corre informe_semanal.py)
"""
import os
import subprocess
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        if _line.strip() and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent))
from pegasus_arandanos import run as descargar_datos, DB_PATH

def main():
    ahora = datetime.now()
    date_from = (ahora - timedelta(days=2)).strftime("%d/%m/%Y")
    date_to   = ahora.strftime("%d/%m/%Y")

    log.info(f"Descargando datos {date_from} -> {date_to}...")
    ins, dup, err = descargar_datos(date_from=date_from, date_to=date_to)
    if err:
        log.warning(f"Error en descarga: {err}")
    log.info(f"Descarga: {ins} nuevos, {dup} duplicados")

    repo = Path(__file__).parent
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo, capture_output=True, text=True
        )
        if result.returncode != 0:
            log.warning("Git remote 'origin' no configurado.")
            return

        fecha_str = ahora.strftime("%Y-%m-%d")
        subprocess.run(["git", "add", "pegasus_arandanos.db"],
                       cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", f"data: actualizar DB {fecha_str} [auto]"],
                       cwd=repo, capture_output=True)
        subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "master"],
                       cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "push", "origin", "master"],
                       cwd=repo, check=True, capture_output=True)
        log.info("DB pusheada a GitHub. Streamlit Cloud redespliegara automaticamente.")
    except subprocess.CalledProcessError as e:
        log.warning(f"No se pudo pushear a GitHub: {e}")
    except Exception as e:
        log.warning(f"Error en sync: {e}")

if __name__ == "__main__":
    main()
