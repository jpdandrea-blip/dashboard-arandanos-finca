"""
monitor_helada_cloud.py
Corre en GitHub Actions cada 15 minutos — independiente de la PC.
Consulta Pegasus directamente, envia alertas via WhatsApp (CallMeBot) si T <= 2 C.

Secretos requeridos en GitHub (Settings > Secrets > Actions):
  PEGASUS_USER          — usuario Pegasus (ej: jpdrea)
  PEGASUS_PASS          — password Pegasus
  CALLMEBOT_RECIPIENTS  — JSON: [{"phone":"+549XXXXXXXXXX","apikey":"123456"}, ...]
"""
import os
import json
import logging
import sys
import urllib.parse
import requests
from datetime import datetime, timedelta
from pathlib import Path
from bs4 import BeautifulSoup
import re

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL        = "http://recursoshidricos.hopto.org:1002"
USERNAME        = os.environ.get("PEGASUS_USER", "")
PASSWORD        = os.environ.get("PEGASUS_PASS", "")
EQUIPO_ID       = "15"

UMBRAL_ALERTA   = 2.0   # C — alertar cuando T <= este valor
UMBRAL_FIN      = 4.0   # C — desactivar alerta cuando T > este valor
HORAS_SILENCIO  = 4     # h — esperar antes de re-alertar en el mismo evento

STATE_FILE = Path(__file__).parent / "monitor_state.json"


# ── Pegasus ───────────────────────────────────────────────────────────────────
def get_hidden_fields(soup):
    result = {}
    for name in ["__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION",
                 "ScriptManager1_HiddenField", "__EVENTTARGET", "__EVENTARGUMENT"]:
        el = soup.find("input", {"name": name})
        result[name] = el["value"] if el and el.get("value") else ""
    return result


def login(session):
    r = session.get(f"{BASE_URL}/Login.aspx", timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    hidden = get_hidden_fields(soup)
    text_inputs = soup.find_all("input", {"type": "text"})
    pass_inputs = soup.find_all("input", {"type": "password"})
    if not text_inputs or not pass_inputs:
        raise RuntimeError("Campos de login no encontrados.")
    data = {**hidden, text_inputs[0]["name"]: USERNAME, pass_inputs[0]["name"]: PASSWORD}
    btn = (soup.find("input", {"type": "submit"}) or
           soup.find("input", {"type": "image"}) or
           soup.find("input", id=re.compile(r"btn|login|ingresar", re.I)))
    if btn:
        n = btn.get("name", "")
        if btn.get("type") == "image":
            data[f"{n}.x"] = "50"; data[f"{n}.y"] = "15"
        elif n:
            data[n] = btn.get("value", "Ingresar")
    r = session.post(f"{BASE_URL}/Login.aspx", data=data, timeout=30, allow_redirects=True)
    r.raise_for_status()
    if "login" in r.url.lower():
        raise RuntimeError("Login fallido. Verificar credenciales.")
    log.info(f"Login OK -> {r.url}")


def obtener_ultima_temperatura(session):
    """Descarga la ultima hora de datos y devuelve (temp_actual, temp_min_hora, ts)."""
    ahora = datetime.now()
    date_from = (ahora - timedelta(hours=1)).strftime("%d/%m/%Y")
    date_to   = ahora.strftime("%d/%m/%Y")

    r = session.get(f"{BASE_URL}/Historico.aspx", timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    hidden = get_hidden_fields(soup)
    data = {
        **hidden,
        "__EVENTTARGET": "", "__EVENTARGUMENT": "",
        "DropDownList1": EQUIPO_ID, "DropDownList3": EQUIPO_ID,
        "DropDownList2": "0",
        "TextBox1": date_from, "TextBox2": date_to,
        "ImageButton1.x": "10", "ImageButton1.y": "10",
    }
    r = session.post(f"{BASE_URL}/Historico.aspx", data=data, timeout=60)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    table = soup.find("table", id="GridView1")
    if not table:
        log.warning("No se encontro tabla de datos en Pegasus.")
        return None, None, None

    temps = []
    ultima_ts = None
    for tr in table.find_all("tr")[1:]:
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) >= 8:
            try:
                ts_str = cells[0]
                temp_str = cells[7].replace(".", "").replace(",", ".")
                temp = float(temp_str)
                ts = datetime.strptime(ts_str, "%d/%m/%Y %H:%M:%S")
                temps.append((ts, temp))
            except (ValueError, IndexError):
                continue

    if not temps:
        return None, None, None

    temps.sort(key=lambda x: x[0])
    ultima_ts, ultima_temp = temps[-1]
    temp_min = min(t for _, t in temps)
    return ultima_temp, temp_min, ultima_ts


# ── WhatsApp CallMeBot ────────────────────────────────────────────────────────
def send_whatsapp(mensaje: str) -> int:
    """Envia mensaje a todos los destinatarios configurados. Retorna cantidad enviada."""
    raw = os.environ.get("CALLMEBOT_RECIPIENTS", "")
    if not raw:
        log.warning("CALLMEBOT_RECIPIENTS no configurado en secrets de GitHub.")
        return 0

    try:
        recipients = json.loads(raw)
    except json.JSONDecodeError:
        log.error("CALLMEBOT_RECIPIENTS no es JSON valido.")
        return 0

    sent = 0
    for r in recipients:
        phone  = r.get("phone", "")
        apikey = r.get("apikey", "")
        if not phone or not apikey:
            continue
        try:
            url = (
                f"https://api.callmebot.com/whatsapp.php"
                f"?phone={urllib.parse.quote(phone)}"
                f"&text={urllib.parse.quote(mensaje)}"
                f"&apikey={apikey}"
            )
            resp = requests.get(url, timeout=20)
            if resp.status_code == 200:
                log.info(f"WhatsApp enviado a {phone}")
                sent += 1
            else:
                log.warning(f"WhatsApp error {resp.status_code} para {phone}: {resp.text[:100]}")
        except Exception as e:
            log.warning(f"WhatsApp exception para {phone}: {e}")

    return sent


# ── Estado ────────────────────────────────────────────────────────────────────
def cargar_estado() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"en_alerta": False, "ultima_alerta": None, "temp_min_evento": None}


def guardar_estado(estado: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(estado, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8"
    )


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ahora = datetime.now()
    log.info(f"Monitor iniciado — {ahora.strftime('%d/%m/%Y %H:%M UTC')}")

    if not USERNAME or not PASSWORD:
        log.error("PEGASUS_USER / PEGASUS_PASS no configurados.")
        sys.exit(1)

    # Obtener temperatura
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; MonitorBot/1.0)"})
        login(session)
        t_actual, t_min_hora, ts_lectura = obtener_ultima_temperatura(session)
    except Exception as e:
        log.error(f"Error al consultar Pegasus: {e}")
        sys.exit(0)  # exit 0 para que GitHub Actions no lo marque como fallo

    if t_actual is None:
        log.warning("Sin datos de temperatura — abortando.")
        sys.exit(0)

    edad_min = (ahora - ts_lectura).total_seconds() / 60 if ts_lectura else 999
    log.info(f"T actual: {t_actual:.1f}C | min ultima hora: {t_min_hora:.1f}C | datos hace {edad_min:.0f} min")

    estado = cargar_estado()
    estado_inicial = json.dumps(estado, default=str)

    # ── Logica de alerta ──────────────────────────────────────────────────────
    if t_actual <= UMBRAL_ALERTA:
        ultima_dt = (datetime.fromisoformat(estado["ultima_alerta"])
                     if estado.get("ultima_alerta") else None)
        horas_desde = ((ahora - ultima_dt).total_seconds() / 3600
                       if ultima_dt else 999)
        t_min_ev = min(
            t_actual,
            t_min_hora,
            estado.get("temp_min_evento") or t_actual
        )
        estado["temp_min_evento"] = t_min_ev

        if not estado["en_alerta"] or horas_desde >= HORAS_SILENCIO:
            tipo  = "ALERTA DE HELADA" if not estado["en_alerta"] else "Helada activa - recordatorio"
            icono = "ALERTA" if not estado["en_alerta"] else "RECORDATORIO"
            msg = (
                f"{icono} - {tipo}\n"
                f"Finca Leon Rouges | {ahora.strftime('%d/%m/%Y %H:%M')}\n\n"
                f"Temperatura actual: {t_actual:.1f} C\n"
                f"Minima ultima hora: {t_min_hora:.1f} C\n"
                f"Minima del evento: {t_min_ev:.1f} C\n\n"
                f"Umbral de alerta: {UMBRAL_ALERTA} C\n"
                f"Verificar sistema antihelada."
            )
            enviados = send_whatsapp(msg)
            log.info(f"Alerta enviada a {enviados} destinatarios.")
            estado["en_alerta"] = True
            estado["ultima_alerta"] = ahora.isoformat()
        else:
            log.info(f"En alerta — silencio {horas_desde:.1f}/{HORAS_SILENCIO}h")

    elif t_actual > UMBRAL_FIN and estado.get("en_alerta"):
        t_min_ev = estado.get("temp_min_evento") or t_actual
        msg = (
            f"TEMPERATURA RECUPERADA\n"
            f"Finca Leon Rouges | {ahora.strftime('%d/%m/%Y %H:%M')}\n\n"
            f"Temperatura actual: {t_actual:.1f} C\n"
            f"Minima registrada en el evento: {t_min_ev:.1f} C\n\n"
            f"Temperatura supero {UMBRAL_FIN} C. Alerta desactivada."
        )
        enviados = send_whatsapp(msg)
        log.info(f"Recuperacion notificada a {enviados} destinatarios.")
        estado["en_alerta"] = False
        estado["temp_min_evento"] = None

    else:
        log.info(f"T={t_actual:.1f}C — sin accion requerida.")

    # Guardar estado solo si cambio
    estado_final = json.dumps(estado, default=str)
    if estado_final != estado_inicial:
        guardar_estado(estado)
        log.info("Estado actualizado en monitor_state.json")
    else:
        log.info("Estado sin cambios.")


if __name__ == "__main__":
    main()
