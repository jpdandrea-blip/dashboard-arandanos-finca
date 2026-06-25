"""
monitor_helada_cloud.py
Corre en GitHub Actions cada 15 minutos — independiente de la PC.
- Descarga datos de Pegasus y los guarda en Supabase
- Envia alertas WhatsApp via UltraMsg si T <= 2 C

Secrets en GitHub (Settings > Secrets > Actions):
  PEGASUS_USER       — usuario Pegasus
  PEGASUS_PASS       — password Pegasus
  SUPABASE_URL       — URL del proyecto Supabase
  SUPABASE_KEY       — service_role key de Supabase
  ULTRAMSG_INSTANCE  — ID de instancia UltraMsg (ej: "instance12345")
  ULTRAMSG_TOKEN     — token de UltraMsg
  ULTRAMSG_PHONES    — numeros separados por coma: "5491150000000,5491160000000"
"""
import os
import json
import logging
import sys
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
BASE_URL          = "http://recursoshidricos.hopto.org:1002"
USERNAME          = os.environ.get("PEGASUS_USER", "")
PASSWORD          = os.environ.get("PEGASUS_PASS", "")
EQUIPO_ID         = "15"
STATION           = "Finca Leon Rouges"

UMBRAL_ALERTA     = 2.0   # C — alerta cuando T <= este valor
UMBRAL_FIN        = 4.0   # C — desactivar alerta cuando T > este valor
HORAS_SILENCIO    = 4     # h — esperar antes de re-alertar

SUPABASE_URL      = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY      = os.environ.get("SUPABASE_KEY", "")  # service_role
ULTRAMSG_INSTANCE = os.environ.get("ULTRAMSG_INSTANCE", "")
ULTRAMSG_TOKEN    = os.environ.get("ULTRAMSG_TOKEN", "")
ULTRAMSG_PHONES   = os.environ.get("ULTRAMSG_PHONES", "")  # "549XXXXXXXXXX,549XXXXXXXXXX"

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
        raise RuntimeError("Login fallido.")
    log.info(f"Login OK -> {r.url}")


def parse_float(s):
    if not s or s.strip() in ("", "-", "sin valor"):
        return None
    try:
        return float(s.strip().replace(".", "").replace(",", "."))
    except ValueError:
        return None


def descargar_ultima_hora(session):
    """Descarga la ultima hora de Pegasus. Devuelve lista de dicts."""
    ahora     = datetime.now()
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
        return []

    records = []
    for tr in table.find_all("tr")[1:]:
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 11:
            continue
        try:
            ts = datetime.strptime(cells[0], "%d/%m/%Y %H:%M:%S").isoformat()
            records.append({
                "timestamp":           ts,
                "estacion":            STATION,
                "presion_hpa":         parse_float(cells[1]),
                "lluvia_mm":           parse_float(cells[2]),
                "vel_viento_kmh":      parse_float(cells[3]),
                "dir_viento_grados":   parse_float(cells[4]),
                "vel_rafaga_kmh":      parse_float(cells[5]),
                "dir_rafaga_grados":   parse_float(cells[6]),
                "temperatura_c":       parse_float(cells[7]),
                "humedad_pct":         parse_float(cells[8]),
                "radiacion_solar_wm2": parse_float(cells[9]),
                "bateria_vcc":         parse_float(cells[10]),
            })
        except Exception:
            continue
    return records


# ── Supabase ──────────────────────────────────────────────────────────────────
def guardar_en_supabase(records: list) -> int:
    if not SUPABASE_URL or not SUPABASE_KEY or not records:
        return 0
    try:
        from supabase import create_client
        client = create_client(SUPABASE_URL, SUPABASE_KEY)
        client.table("mediciones").upsert(
            records, on_conflict="timestamp,estacion"
        ).execute()
        log.info(f"Supabase: {len(records)} registros guardados.")
        return len(records)
    except Exception as e:
        log.warning(f"Supabase: error al guardar — {e}")
        return 0


# ── WhatsApp UltraMsg ─────────────────────────────────────────────────────────
def send_whatsapp(mensaje: str) -> int:
    if not ULTRAMSG_INSTANCE or not ULTRAMSG_TOKEN or not ULTRAMSG_PHONES:
        log.warning("UltraMsg no configurado (ULTRAMSG_INSTANCE / TOKEN / PHONES).")
        return 0
    phones = [p.strip() for p in ULTRAMSG_PHONES.split(",") if p.strip()]
    sent = 0
    for phone in phones:
        try:
            resp = requests.post(
                f"https://api.ultramsg.com/{ULTRAMSG_INSTANCE}/messages/chat",
                data={
                    "token": ULTRAMSG_TOKEN,
                    "to":    f"{phone}@c.us",
                    "body":  mensaje,
                },
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=20,
            )
            if resp.status_code == 200 and resp.json().get("sent") == "true":
                log.info(f"WhatsApp enviado a {phone}")
                sent += 1
            else:
                log.warning(f"WhatsApp error para {phone}: {resp.text[:120]}")
        except Exception as e:
            log.warning(f"WhatsApp exception para {phone}: {e}")
    return sent


# ── Estado persistente ────────────────────────────────────────────────────────
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
    log.info(f"Monitor iniciado — {ahora.strftime('%d/%m/%Y %H:%M')}")

    if not USERNAME or not PASSWORD:
        log.error("PEGASUS_USER / PEGASUS_PASS no configurados.")
        sys.exit(1)

    # 1. Descargar datos de Pegasus
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; MonitorBot/1.0)"})
        login(session)
        records = descargar_ultima_hora(session)
    except Exception as e:
        log.error(f"Error al consultar Pegasus: {e}")
        sys.exit(0)  # exit 0 para que GitHub Actions no lo marque como fallo

    if not records:
        log.warning("Sin datos de Pegasus.")
        sys.exit(0)

    # 2. Guardar en Supabase (actualiza el dashboard)
    guardar_en_supabase(records)

    # 3. Temperatura mas reciente
    records_ord = sorted(records, key=lambda r: r["timestamp"])
    ultimo = records_ord[-1]
    t_actual = ultimo.get("temperatura_c")
    ts_str   = ultimo.get("timestamp", "")

    if t_actual is None:
        log.warning("temperatura_c es None en el ultimo registro.")
        sys.exit(0)

    temps = [r["temperatura_c"] for r in records_ord if r.get("temperatura_c") is not None]
    t_min_hora = min(temps) if temps else t_actual

    try:
        ts_dt = datetime.fromisoformat(ts_str)
        edad_min = (ahora - ts_dt).total_seconds() / 60
    except Exception:
        edad_min = 0

    log.info(f"T actual: {t_actual:.1f}C | min hora: {t_min_hora:.1f}C | datos hace {edad_min:.0f} min")

    # 4. Logica de alerta
    estado = cargar_estado()
    estado_inicial = json.dumps(estado, default=str)

    if t_actual <= UMBRAL_ALERTA:
        ultima_dt = (datetime.fromisoformat(estado["ultima_alerta"])
                     if estado.get("ultima_alerta") else None)
        horas_desde = ((ahora - ultima_dt).total_seconds() / 3600
                       if ultima_dt else 999)
        t_min_ev = min(t_actual, t_min_hora, estado.get("temp_min_evento") or t_actual)
        estado["temp_min_evento"] = t_min_ev

        if not estado["en_alerta"] or horas_desde >= HORAS_SILENCIO:
            tipo  = "ALERTA DE HELADA" if not estado["en_alerta"] else "Helada activa - recordatorio"
            icono = "🚨" if not estado["en_alerta"] else "🔁"
            msg = (
                f"{icono} *{tipo}*\n"
                f"📍 Finca Leon Rouges | {ahora.strftime('%d/%m/%Y %H:%M')}\n\n"
                f"🌡 Temperatura actual: *{t_actual:.1f} °C*\n"
                f"❄️ Mínima última hora: *{t_min_hora:.1f} °C*\n"
                f"📉 Mínima del evento: *{t_min_ev:.1f} °C*\n\n"
                f"Umbral de alerta: {UMBRAL_ALERTA} °C\n"
                f"⚠️ Verificar sistema antihelada."
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
            f"✅ *Temperatura recuperada*\n"
            f"📍 Finca Leon Rouges | {ahora.strftime('%d/%m/%Y %H:%M')}\n\n"
            f"🌡 Temperatura actual: *{t_actual:.1f} °C*\n"
            f"📉 Mínima del evento: *{t_min_ev:.1f} °C*\n\n"
            f"Temperatura superó {UMBRAL_FIN} °C. Alerta desactivada."
        )
        enviados = send_whatsapp(msg)
        log.info(f"Recuperacion notificada a {enviados} destinatarios.")
        estado["en_alerta"] = False
        estado["temp_min_evento"] = None

    else:
        log.info(f"T={t_actual:.1f}C — sin accion requerida.")

    # Guardar estado solo si cambio
    if json.dumps(estado, default=str) != estado_inicial:
        guardar_estado(estado)
        log.info("Estado actualizado.")


if __name__ == "__main__":
    main()
