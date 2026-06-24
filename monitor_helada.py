"""
monitor_helada.py
Monitorea temperatura en Finca Leon Rouges y alerta via Telegram cuando T <= 2 C.
Se ejecuta cada 15 minutos via Tarea Programada de Windows.

Logica de alertas:
  - T <= UMBRAL_ALERTA  → alerta inmediata, luego recordatorio cada HORAS_SILENCIO
  - T >  UMBRAL_FIN     → mensaje de temperatura recuperada, reset del evento
  - Guarda estado en monitor_state.json para sobrevivir reinicios
  - Pushea DB a GitHub cada HORAS_PUSH_GITHUB horas (actualiza dashboard Streamlit)
"""
import os
import json
import sqlite3
import subprocess
import requests
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Cargar .env
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        if _line.strip() and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            Path(__file__).parent / "monitor_helada.log",
            encoding="utf-8"
        ),
    ],
)
log = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent))
from pegasus_arandanos import run as descargar_datos, DB_PATH, STATION

# ── Configuracion ─────────────────────────────────────────────────────────────
UMBRAL_ALERTA      = 2.0   # C  — alertar cuando T <= este valor
UMBRAL_FIN         = 4.0   # C  — desactivar alerta cuando T > este valor
HORAS_SILENCIO     = 4     # h  — esperar antes de re-alertar si sigue fria
HORAS_PUSH_GITHUB  = 4     # h  — pushear DB a GitHub cada N horas (actualiza dashboard)
DATOS_ANTIGUOS_MIN = 45    # min — avisar si los datos tienen mas de X minutos

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

STATE_FILE = Path(__file__).parent / "monitor_state.json"


# ── Estado persistente ────────────────────────────────────────────────────────
def cargar_estado() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "en_alerta":       False,
        "ultima_alerta":   None,
        "temp_min_evento": None,
        "ultimo_push_gh":  None,
    }


def guardar_estado(estado: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(estado, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8"
    )


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(mensaje: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no configurados en .env")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       mensaje,
                "parse_mode": "HTML",
            },
            timeout=15,
        )
        r.raise_for_status()
        log.info("Telegram: mensaje enviado OK.")
        return True
    except Exception as e:
        log.warning(f"Telegram: error al enviar — {e}")
        return False


# ── DB ────────────────────────────────────────────────────────────────────────
def temperatura_reciente() -> tuple:
    """Devuelve (temp_actual, ts_lectura, temp_min_hora) de la DB local."""
    try:
        conn = sqlite3.connect(DB_PATH)
        # Lectura mas reciente
        row = conn.execute(
            """SELECT temperatura_c, timestamp FROM mediciones
               WHERE estacion = ? AND temperatura_c IS NOT NULL
               ORDER BY timestamp DESC LIMIT 1""",
            (STATION,)
        ).fetchone()
        # Minima de la ultima hora
        hace_1h = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        min_row = conn.execute(
            """SELECT MIN(temperatura_c) FROM mediciones
               WHERE estacion = ? AND timestamp >= ?""",
            (STATION, hace_1h)
        ).fetchone()
        conn.close()
        if row:
            ts = datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S")
            return row[0], ts, (min_row[0] if min_row and min_row[0] is not None else row[0])
    except Exception as e:
        log.warning(f"Error leyendo DB: {e}")
    return None, None, None


# ── GitHub push ───────────────────────────────────────────────────────────────
def push_github_si_corresponde(estado: dict, ahora: datetime) -> dict:
    """Pushea la DB a GitHub si pasaron HORAS_PUSH_GITHUB desde el ultimo push."""
    ultimo = estado.get("ultimo_push_gh")
    if ultimo:
        horas = (ahora - datetime.fromisoformat(ultimo)).total_seconds() / 3600
        if horas < HORAS_PUSH_GITHUB:
            return estado
    repo = Path(__file__).parent
    try:
        subprocess.run(["git", "add", "pegasus_arandanos.db"],
                       cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m",
             f"data: actualizar DB {ahora.strftime('%Y-%m-%d %H:%M')} [monitor]"],
            cwd=repo, capture_output=True
        )
        subprocess.run(
            ["git", "pull", "--rebase", "--autostash", "origin", "master"],
            cwd=repo, check=True, capture_output=True
        )
        subprocess.run(["git", "push", "origin", "master"],
                       cwd=repo, check=True, capture_output=True)
        log.info("DB pusheada a GitHub (actualizacion dashboard).")
        estado["ultimo_push_gh"] = ahora.isoformat()
    except subprocess.CalledProcessError as e:
        log.warning(f"Push GitHub: {e}")
    return estado


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ahora = datetime.now()

    # 1. Descargar datos recientes de Pegasus → DB
    date_from = (ahora - timedelta(hours=1)).strftime("%d/%m/%Y")
    date_to   = ahora.strftime("%d/%m/%Y")
    ins, dup, err = descargar_datos(date_from=date_from, date_to=date_to)
    if err:
        log.warning(f"Error Pegasus: {err}")
    else:
        log.info(f"Pegasus: {ins} nuevos, {dup} duplicados")

    # 2. Leer temperatura de la DB
    t_actual, ts, t_min_hora = temperatura_reciente()
    if t_actual is None:
        log.warning("No se pudo obtener temperatura — abortando.")
        return

    edad_min = (ahora - ts).total_seconds() / 60
    log.info(f"T={t_actual:.1f}C | min_1h={t_min_hora:.1f}C | datos hace {edad_min:.0f} min")

    if edad_min > DATOS_ANTIGUOS_MIN:
        log.warning(f"Datos con {edad_min:.0f} min de antiguedad — verificar conexion Pegasus.")

    # 3. Logica de alerta
    estado = cargar_estado()

    if t_actual <= UMBRAL_ALERTA:
        ultima_dt = (
            datetime.fromisoformat(estado["ultima_alerta"])
            if estado.get("ultima_alerta") else None
        )
        horas_desde = (ahora - ultima_dt).total_seconds() / 3600 if ultima_dt else 999
        t_min_ev = min(
            t_actual,
            estado.get("temp_min_evento") or t_actual,
            t_min_hora
        )
        estado["temp_min_evento"] = t_min_ev

        if not estado["en_alerta"] or horas_desde >= HORAS_SILENCIO:
            tipo = "ALERTA DE HELADA" if not estado["en_alerta"] else "Helada activa — recordatorio"
            icono = "🚨" if not estado["en_alerta"] else "🔁"
            msg = (
                f"{icono} <b>{tipo}</b>\n"
                f"📍 Finca Leon Rouges — {ahora.strftime('%d/%m/%Y %H:%M')}\n\n"
                f"🌡 Temperatura actual: <b>{t_actual:.1f}°C</b>\n"
                f"❄️ Mínima última hora: <b>{t_min_hora:.1f}°C</b>\n"
                f"📉 Mínima del evento: <b>{t_min_ev:.1f}°C</b>\n\n"
                f"Umbral de alerta: {UMBRAL_ALERTA}°C\n"
                f"⚠️ Verificar sistema antihelada."
            )
            send_telegram(msg)
            estado["en_alerta"] = True
            estado["ultima_alerta"] = ahora.isoformat()
        else:
            log.info(f"En alerta — T={t_actual:.1f}C, silencio {horas_desde:.1f}/{HORAS_SILENCIO}h")

    elif t_actual > UMBRAL_FIN and estado.get("en_alerta"):
        t_min_ev = estado.get("temp_min_evento") or t_actual
        msg = (
            f"✅ <b>Temperatura recuperada</b>\n"
            f"📍 Finca Leon Rouges — {ahora.strftime('%d/%m/%Y %H:%M')}\n\n"
            f"🌡 Temperatura actual: <b>{t_actual:.1f}°C</b>\n"
            f"📉 Mínima registrada en el evento: <b>{t_min_ev:.1f}°C</b>\n\n"
            f"Temperatura superó {UMBRAL_FIN}°C. Alerta desactivada."
        )
        send_telegram(msg)
        estado["en_alerta"] = False
        estado["temp_min_evento"] = None
        log.info("Alerta desactivada — temperatura recuperada.")

    else:
        log.info(f"T={t_actual:.1f}C — fuera de zona critica.")

    # 4. Push a GitHub cada HORAS_PUSH_GITHUB horas
    estado = push_github_si_corresponde(estado, ahora)
    guardar_estado(estado)


if __name__ == "__main__":
    main()
