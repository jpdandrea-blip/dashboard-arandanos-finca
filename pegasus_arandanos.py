#!/usr/bin/env python3
"""
Pegasus Weather Station - Finca Leon Rouges
Descarga datos meteorologicos relevantes para produccion de arandanos
y los almacena en una base de datos SQLite.

Variables descargadas:
  - Temperatura de Aire exterior (°C)
  - Humedad de Aire exterior (%)
  - Lluvia Caida (mm)
  - Radiacion Solar (w/m2)
  - Velocidad y Direccion de Viento
  - Presion Atmosferica (hPa)
"""

import requests
from bs4 import BeautifulSoup
import sqlite3
import re
import sys
import logging
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(
            open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
        ),
        logging.FileHandler(
            Path(__file__).parent / "pegasus_arandanos.log",
            encoding="utf-8"
        ),
    ],
)
log = logging.getLogger(__name__)

# ── Configuracion ────────────────────────────────────────────────────────────
BASE_URL    = "http://recursoshidricos.hopto.org:1002"
USERNAME    = "jpdrea"
PASSWORD    = "Arandanos"
DB_PATH     = Path(__file__).parent / "pegasus_arandanos.db"
EQUIPO_ID   = "15"
STATION     = "Finca Leon Rouges"


# ── Base de datos ─────────────────────────────────────────────────────────────
def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mediciones (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp           TEXT    NOT NULL,
            estacion            TEXT    NOT NULL,
            presion_hpa         REAL,
            lluvia_mm           REAL,
            vel_viento_kmh      REAL,
            dir_viento_grados   REAL,
            vel_rafaga_kmh      REAL,
            dir_rafaga_grados   REAL,
            temperatura_c       REAL,
            humedad_pct         REAL,
            radiacion_solar_wm2 REAL,
            bateria_vcc         REAL,
            fecha_descarga      TEXT    DEFAULT (datetime('now','localtime')),
            UNIQUE(timestamp, estacion)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS descargas (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha_ejecucion       TEXT    DEFAULT (datetime('now','localtime')),
            desde                 TEXT,
            hasta                 TEXT,
            registros_insertados  INTEGER DEFAULT 0,
            registros_duplicados  INTEGER DEFAULT 0,
            error                 TEXT
        )
    """)
    # Indices utiles para consultas de produccion
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_mediciones_timestamp
        ON mediciones(timestamp)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_mediciones_estacion_ts
        ON mediciones(estacion, timestamp)
    """)
    conn.commit()
    return conn


# ── Utilidades de parsing ─────────────────────────────────────────────────────
def parse_float(s: str):
    """Convierte string con coma decimal a float. Retorna None si invalido."""
    if not s or s.strip() in ("", "-", "sin valor"):
        return None
    try:
        return float(s.strip().replace(".", "").replace(",", "."))
    except ValueError:
        return None


def get_hidden_fields(soup: BeautifulSoup) -> dict:
    """Extrae los campos ocultos de ASP.NET necesarios para el POST."""
    result = {}
    for name in [
        "__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION",
        "ScriptManager1_HiddenField", "__EVENTTARGET", "__EVENTARGUMENT",
    ]:
        el = soup.find("input", {"name": name})
        result[name] = el["value"] if el and el.get("value") else ""
    return result


# ── Login ─────────────────────────────────────────────────────────────────────
def login(session: requests.Session) -> None:
    """Inicia sesion en Pegasus. Lanza Exception si falla."""
    log.info("Iniciando sesion en Pegasus...")
    r = session.get(f"{BASE_URL}/Login.aspx", timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    hidden = get_hidden_fields(soup)

    text_inputs = soup.find_all("input", {"type": "text"})
    pass_inputs = soup.find_all("input", {"type": "password"})
    if not text_inputs or not pass_inputs:
        raise RuntimeError("No se encontraron campos de usuario/contrasena en Login.aspx")

    user_field = text_inputs[0]["name"]
    pass_field  = pass_inputs[0]["name"]

    data = {
        **hidden,
        user_field: USERNAME,
        pass_field: PASSWORD,
    }

    # Detectar boton de envio (image button "ingresar")
    btn = (
        soup.find("input", {"type": "submit"}) or
        soup.find("input", {"type": "image"}) or
        soup.find("input", id=re.compile(r"btn|login|ingresar", re.I))
    )
    if btn:
        btn_name = btn.get("name", "")
        if btn.get("type") == "image":
            data[f"{btn_name}.x"] = "50"
            data[f"{btn_name}.y"] = "15"
        elif btn_name:
            data[btn_name] = btn.get("value", "Ingresar")

    r = session.post(
        f"{BASE_URL}/Login.aspx", data=data,
        timeout=30, allow_redirects=True
    )
    r.raise_for_status()

    if "Login" in r.url or "login" in r.url.lower():
        raise RuntimeError("Login fallido. Verificar credenciales.")

    log.info(f"Login exitoso -> {r.url}")


# ── Descarga de historico ─────────────────────────────────────────────────────
def _post_historico(
    session: requests.Session,
    date_from: str,
    date_to: str,
    soup: BeautifulSoup,
    eventtarget: str = "",
    eventargument: str = "",
    click_ver: bool = False,
) -> BeautifulSoup:
    """Realiza un POST a Historico.aspx y devuelve el soup resultante."""
    hidden = get_hidden_fields(soup)
    data = {
        **hidden,
        "__EVENTTARGET":  eventtarget,
        "__EVENTARGUMENT": eventargument,
        "DropDownList1": EQUIPO_ID,
        "DropDownList3": EQUIPO_ID,
        "DropDownList2": "0",       # Todos los sensores
        "TextBox1": date_from,
        "TextBox2": date_to,
    }
    if click_ver:
        data["ImageButton1.x"] = "10"
        data["ImageButton1.y"] = "10"

    r = session.post(f"{BASE_URL}/Historico.aspx", data=data, timeout=60)
    r.raise_for_status()
    return BeautifulSoup(r.text, "lxml")


def parse_table(soup: BeautifulSoup) -> list[list[str]]:
    """Extrae filas de datos de la tabla GridView1."""
    table = soup.find("table", id="GridView1")
    if not table:
        return []
    rows = []
    for tr in table.find_all("tr")[1:]:   # saltar encabezado
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) >= 11:
            rows.append(cells)
    return rows


def fetch_all_pages(
    session: requests.Session,
    date_from: str,
    date_to: str,
) -> list[list[str]]:
    """Descarga todas las paginas del historico para el rango dado."""
    log.info(f"Descargando historico: {date_from} al {date_to}")

    # Primera carga de la pagina (GET para obtener viewstate)
    r = session.get(f"{BASE_URL}/Historico.aspx", timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    # Primer POST para ver los datos
    soup = _post_historico(session, date_from, date_to, soup, click_ver=True)

    all_rows: list[list[str]] = []
    page = 1

    while True:
        rows = parse_table(soup)
        if not rows:
            log.warning(f"Pagina {page}: sin datos en la tabla.")
            break

        all_rows.extend(rows)
        log.info(f"  Pagina {page}: {len(rows)} registros (acum: {len(all_rows)})")

        # Verificar si hay pagina siguiente
        next_link = soup.find("a", string="Siguiente")
        if not next_link:
            break

        page += 1
        soup = _post_historico(
            session, date_from, date_to, soup,
            eventtarget="GridView1",
            eventargument="Page$Next",
        )

    return all_rows


# ── Guardado en DB ────────────────────────────────────────────────────────────
def save_rows(conn: sqlite3.Connection, rows: list[list[str]]) -> tuple[int, int]:
    """Inserta filas en la DB. Devuelve (insertados, duplicados)."""
    inserted = duplicates = 0

    for cells in rows:
        try:
            # Timestamp: "14/05/2026 0:00:00"
            ts = datetime.strptime(cells[0], "%d/%m/%Y %H:%M:%S").strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            record = (
                ts, STATION,
                parse_float(cells[1]),    # presion_hpa
                parse_float(cells[2]),    # lluvia_mm
                parse_float(cells[3]),    # vel_viento_kmh
                parse_float(cells[4]),    # dir_viento_grados
                parse_float(cells[5]),    # vel_rafaga_kmh
                parse_float(cells[6]),    # dir_rafaga_grados
                parse_float(cells[7]),    # temperatura_c
                parse_float(cells[8]),    # humedad_pct
                parse_float(cells[9]),    # radiacion_solar_wm2
                parse_float(cells[10]),   # bateria_vcc
            )
            conn.execute(
                """INSERT OR IGNORE INTO mediciones
                   (timestamp, estacion, presion_hpa, lluvia_mm,
                    vel_viento_kmh, dir_viento_grados, vel_rafaga_kmh,
                    dir_rafaga_grados, temperatura_c, humedad_pct,
                    radiacion_solar_wm2, bateria_vcc)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                record,
            )
            if conn.execute("SELECT changes()").fetchone()[0] > 0:
                inserted += 1
            else:
                duplicates += 1
        except Exception as e:
            log.error(f"Error en fila {cells[:2]}: {e}")

    conn.commit()
    return inserted, duplicates


# ── Funcion principal ─────────────────────────────────────────────────────────
def run(date_from: str = None, date_to: str = None) -> tuple[int, int, str | None]:
    """
    Descarga y almacena datos meteorologicos.

    Args:
        date_from: Fecha inicio DD/MM/YYYY (default: 7 dias atras)
        date_to:   Fecha fin   DD/MM/YYYY (default: hoy)

    Returns:
        (registros_insertados, duplicados, error_o_None)
    """
    now = datetime.now()
    if date_to is None:
        date_to = now.strftime("%d/%m/%Y")
    if date_from is None:
        date_from = (now - timedelta(days=7)).strftime("%d/%m/%Y")

    conn = init_db(DB_PATH)
    inserted = duplicates = 0
    error_msg = None

    try:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; PegasusBot/1.0)"})

        login(session)
        rows = fetch_all_pages(session, date_from, date_to)

        if rows:
            inserted, duplicates = save_rows(conn, rows)
            log.info(f"DB: {inserted} nuevos, {duplicates} duplicados -> {DB_PATH}")
        else:
            log.warning("No se obtuvieron registros para el rango indicado.")

    except Exception as exc:
        error_msg = str(exc)
        log.error(f"Error general: {exc}", exc_info=True)

    finally:
        conn.execute(
            """INSERT INTO descargas (desde, hasta, registros_insertados, registros_duplicados, error)
               VALUES (?,?,?,?,?)""",
            (date_from, date_to, inserted, duplicates, error_msg),
        )
        conn.commit()
        conn.close()

    return inserted, duplicates, error_msg


# ── Consultas utiles para arandanos ─────────────────────────────────────────
def resumen_semanal(db_path: Path = DB_PATH):
    """Imprime un resumen semanal con las variables clave para arandanos."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT
            DATE(timestamp)                     AS fecha,
            ROUND(MIN(temperatura_c), 1)        AS temp_min_c,
            ROUND(MAX(temperatura_c), 1)        AS temp_max_c,
            ROUND(AVG(temperatura_c), 1)        AS temp_prom_c,
            SUM(lluvia_mm)                      AS lluvia_total_mm,
            ROUND(AVG(humedad_pct), 1)          AS humedad_prom_pct,
            ROUND(MAX(radiacion_solar_wm2), 0)  AS rad_max_wm2,
            ROUND(AVG(vel_viento_kmh), 1)       AS viento_prom_kmh,
            -- Horas de frio (temp < 7°C, relevante para dormancia)
            SUM(CASE WHEN temperatura_c < 7.0 THEN 0.25 ELSE 0 END) AS horas_frio
        FROM mediciones
        WHERE timestamp >= DATE('now', '-7 days')
          AND estacion = ?
        GROUP BY DATE(timestamp)
        ORDER BY fecha
    """, (STATION,)).fetchall()
    conn.close()

    print(f"\n{'='*70}")
    print(f"RESUMEN SEMANAL - {STATION}")
    print(f"{'='*70}")
    print(f"{'Fecha':<12} {'T_min':>6} {'T_max':>6} {'T_avg':>6} "
          f"{'Lluvia':>7} {'Humedad':>8} {'Rad_max':>8} {'Viento':>7} {'H_frio':>7}")
    print(f"{'-'*70}")
    for r in rows:
        print(
            f"{r['fecha']:<12} {r['temp_min_c'] or '-':>6} {r['temp_max_c'] or '-':>6} "
            f"{r['temp_prom_c'] or '-':>6} {r['lluvia_total_mm'] or 0:>7.1f} "
            f"{r['humedad_prom_pct'] or '-':>8} {r['rad_max_wm2'] or '-':>8} "
            f"{r['viento_prom_kmh'] or '-':>7} {r['horas_frio'] or 0:>7.1f}"
        )
    print(f"{'='*70}\n")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Descarga datos de Pegasus (Finca Leon Rouges) para arandanos"
    )
    parser.add_argument("--desde", help="Fecha inicio DD/MM/YYYY")
    parser.add_argument("--hasta", help="Fecha fin DD/MM/YYYY")
    parser.add_argument("--resumen", action="store_true",
                        help="Mostrar resumen semanal despues de la descarga")
    args = parser.parse_args()

    inserted, duplicates, error = run(date_from=args.desde, date_to=args.hasta)

    print(f"\nResultado: {inserted} registros nuevos, {duplicates} duplicados.")
    if error:
        print(f"Error: {error}")
        sys.exit(1)

    if args.resumen or not error:
        resumen_semanal()
