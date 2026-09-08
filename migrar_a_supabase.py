"""
Migracion unica: sube todos los datos de SQLite a Supabase.
Ejecutar una sola vez despues de crear la tabla en Supabase.

Uso: python migrar_a_supabase.py
"""
import sqlite3
import logging
import sys
from pathlib import Path
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent))
from supabase_client import get_client
from pegasus_arandanos import DB_PATH, STATION

BATCH_SIZE = 500


def migrar():
    client = get_client()
    if not client:
        log.error("No hay credenciales Supabase. Verificar .env")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    total = conn.execute("SELECT COUNT(*) FROM mediciones").fetchone()[0]
    log.info(f"Total registros en SQLite: {total:,}")

    offset = 0
    insertados = 0

    while True:
        rows = conn.execute(
            """SELECT timestamp, estacion, presion_hpa, lluvia_mm,
                      vel_viento_kmh, dir_viento_grados, vel_rafaga_kmh,
                      dir_rafaga_grados, temperatura_c, humedad_pct,
                      radiacion_solar_wm2, bateria_vcc
               FROM mediciones ORDER BY timestamp LIMIT ? OFFSET ?""",
            (BATCH_SIZE, offset)
        ).fetchall()

        if not rows:
            break

        records = []
        for r in rows:
            try:
                ts = datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S").isoformat()
                records.append({
                    "timestamp":           ts,
                    "estacion":            r["estacion"],
                    "presion_hpa":         r["presion_hpa"],
                    "lluvia_mm":           r["lluvia_mm"],
                    "vel_viento_kmh":      r["vel_viento_kmh"],
                    "dir_viento_grados":   r["dir_viento_grados"],
                    "vel_rafaga_kmh":      r["vel_rafaga_kmh"],
                    "dir_rafaga_grados":   r["dir_rafaga_grados"],
                    "temperatura_c":       r["temperatura_c"],
                    "humedad_pct":         r["humedad_pct"],
                    "radiacion_solar_wm2": r["radiacion_solar_wm2"],
                    "bateria_vcc":         r["bateria_vcc"],
                })
            except Exception as e:
                log.warning(f"Fila ignorada: {e}")

        if records:
            client.table("mediciones").upsert(
                records, on_conflict="timestamp,estacion"
            ).execute()
            insertados += len(records)

        offset += BATCH_SIZE
        log.info(f"Progreso: {min(offset, total):,}/{total:,} ({min(offset,total)/total*100:.1f}%)")

    conn.close()
    log.info(f"Migracion completa: {insertados:,} registros subidos a Supabase.")


if __name__ == "__main__":
    migrar()
