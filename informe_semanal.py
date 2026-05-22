"""
Informe Semanal Meteorologico - Finca Leon Rouges
Genera y envia por email un reporte con insights para produccion de arandanos.

Uso manual:  python informe_semanal.py
Automatico:  llamado por el Programador de Tareas cada lunes.
"""

import os
import sqlite3
import smtplib
import sys
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from pathlib import Path

import json
import pandas as pd

# Cargar .env si existe (credenciales locales, no van a GitHub)
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        if _line.strip() and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ── Importar descarga de datos ────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from pegasus_arandanos import run as descargar_datos, init_db, DB_PATH, STATION

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── Configuracion email ───────────────────────────────────────────────────────
EMAIL_ORIGEN  = "juanpidan99@gmail.com"
EMAIL_DESTINO = "jpdandrea@tierradearandanos.com.ar"
APP_PASSWORD  = os.environ.get("GMAIL_APP_PASSWORD", "")   # leer de .env local
SMTP_HOST     = "smtp.gmail.com"
SMTP_PORT     = 587


# ── Carga de datos ────────────────────────────────────────────────────────────
def cargar_semana(fecha_hasta: datetime) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Carga 7 dias de datos en crudo y agregados diarios."""
    fecha_desde = fecha_hasta - timedelta(days=7)
    desde_str = fecha_desde.strftime("%Y-%m-%d 00:00:00")
    hasta_str = fecha_hasta.strftime( "%Y-%m-%d 23:59:59")

    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql(
        """SELECT timestamp, temperatura_c, humedad_pct, lluvia_mm,
                  radiacion_solar_wm2, vel_viento_kmh, vel_rafaga_kmh, presion_hpa
           FROM mediciones
           WHERE timestamp BETWEEN ? AND ? AND estacion = ?
           ORDER BY timestamp""",
        conn, params=(desde_str, hasta_str, STATION),
    )
    conn.close()

    if df.empty:
        return df, pd.DataFrame()

    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["fecha"] = df["timestamp"].dt.date
    df["horas_frio"] = (df["temperatura_c"] < 7.0).astype(float) * 0.25
    df["helada"]     = df["temperatura_c"] < 0.0

    daily = df.groupby("fecha").agg(
        temp_min    =("temperatura_c",    "min"),
        temp_max    =("temperatura_c",    "max"),
        temp_avg    =("temperatura_c",    "mean"),
        humedad_avg =("humedad_pct",      "mean"),
        lluvia      =("lluvia_mm",        "sum"),
        rad_max     =("radiacion_solar_wm2","max"),
        rad_avg     =("radiacion_solar_wm2","mean"),
        viento_avg  =("vel_viento_kmh",   "mean"),
        horas_frio  =("horas_frio",       "sum"),
        horas_helada=("helada",           lambda x: x.sum() * 0.25),
    ).reset_index()

    daily["fecha"] = pd.to_datetime(daily["fecha"])
    daily["gdd"]   = ((daily["temp_max"] + daily["temp_min"]) / 2 - 7).clip(lower=0)
    return df, daily


def horas_frio_acumuladas(conn: sqlite3.Connection) -> float:
    """Horas de frio acumuladas desde el 1 de abril (inicio temporada)."""
    inicio = datetime.now().replace(month=4, day=1, hour=0, minute=0, second=0)
    if datetime.now().month < 4:
        inicio = inicio.replace(year=datetime.now().year - 1)
    row = conn.execute(
        """SELECT COUNT(*) * 0.25 FROM mediciones
           WHERE temperatura_c < 7 AND estacion = ?
             AND timestamp >= ?""",
        (STATION, inicio.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchone()
    return row[0] or 0.0


# ── Generacion de insights ────────────────────────────────────────────────────
def generar_insights(df: pd.DataFrame, daily: pd.DataFrame) -> list[dict]:
    """Genera alertas e insights automaticos basados en los datos de la semana."""
    insights = []

    if daily.empty:
        return [{"tipo": "warn", "titulo": "Sin datos", "cuerpo": "No hay registros para la semana analizada."}]

    t_min_sem = daily["temp_min"].min()
    t_max_sem = daily["temp_max"].max()
    lluvia_tot = daily["lluvia"].sum()
    horas_frio_sem = daily["horas_frio"].sum()
    horas_helada_sem = daily["horas_helada"].sum()
    gdd_sem = daily["gdd"].sum()
    dias_lluvia = (daily["lluvia"] > 0).sum()
    humedad_ok = daily["humedad_avg"].mean() > 5  # sensor funcionando

    # Helada
    if horas_helada_sem > 0:
        dias_helada = daily[daily["horas_helada"] > 0]["fecha"].dt.strftime("%d/%m").tolist()
        insights.append({
            "tipo": "peligro",
            "titulo": f"Helada registrada — {horas_helada_sem:.1f} h bajo 0°C",
            "cuerpo": f"Dias afectados: {', '.join(dias_helada)}. "
                      f"Minima absoluta: {t_min_sem:.1f}°C. "
                      "Verificar dano en yemas si la planta no estaba en dormancia completa.",
        })
    elif t_min_sem < 3.0:
        insights.append({
            "tipo": "alerta",
            "titulo": f"Temperatura muy baja — minima de {t_min_sem:.1f}°C",
            "cuerpo": "Sin helada registrada, pero proxima al umbral critico. "
                      "Controlar el pronostico de los proximos dias.",
        })

    # Horas de frio
    if horas_frio_sem >= 20:
        insights.append({
            "tipo": "ok",
            "titulo": f"Buena acumulacion de frio — {horas_frio_sem:.0f} h esta semana",
            "cuerpo": "Acumulacion util para completar los requerimientos de frio de la variedad. "
                      "Revisar el total acumulado desde abril para decidir el momento de aplicacion de cianamida.",
        })
    elif horas_frio_sem > 0:
        insights.append({
            "tipo": "info",
            "titulo": f"Horas de frio — {horas_frio_sem:.1f} h esta semana",
            "cuerpo": "Acumulacion moderada. Continuar monitoreando semanalmente.",
        })

    # Lluvia y riego
    if lluvia_tot == 0:
        insights.append({
            "tipo": "alerta",
            "titulo": "Sin precipitaciones — riego necesario",
            "cuerpo": f"0 mm en 7 dias. Con temperaturas de hasta {t_max_sem:.1f}°C y radiacion solar activa, "
                      "el deficit hidrico puede ser significativo. Verificar estado del sistema de riego.",
        })
    elif lluvia_tot < 10:
        insights.append({
            "tipo": "info",
            "titulo": f"Lluvia insuficiente — {lluvia_tot:.1f} mm en {dias_lluvia} dia{'s' if dias_lluvia > 1 else ''}",
            "cuerpo": "La precipitacion no cubre los requerimientos hidricos de la semana. "
                      "Complementar con riego segun estado del cultivo.",
        })
    else:
        insights.append({
            "tipo": "ok",
            "titulo": f"Lluvia adecuada — {lluvia_tot:.1f} mm acumulados",
            "cuerpo": f"Distribuida en {dias_lluvia} dias. Evaluar drenaje si hubo eventos mayores a 20 mm/dia.",
        })

    # Humedad y enfermedades
    if not humedad_ok:
        insights.append({
            "tipo": "alerta",
            "titulo": "Sensor de humedad con lecturas anormales",
            "cuerpo": "La humedad relativa muestra valores cercanos a 0%, lo que indica un posible "
                      "problema de calibracion o dano en el sensor. Revisar antes de la temporada de mayor riesgo fungico.",
        })
    elif daily["humedad_avg"].max() > 80:
        dias_humedo = daily[daily["humedad_avg"] > 80]["fecha"].dt.strftime("%d/%m").tolist()
        insights.append({
            "tipo": "alerta",
            "titulo": f"Humedad elevada — riesgo de Botrytis",
            "cuerpo": f"Dias con humedad media > 80%: {', '.join(dias_humedo)}. "
                      "Condiciones favorables para Botrytis cinerea. Evaluar aplicacion preventiva de fungicida.",
        })

    # GDD
    if gdd_sem > 0:
        insights.append({
            "tipo": "info",
            "titulo": f"Grados-Dia acumulados esta semana — {gdd_sem:.0f} GDD",
            "cuerpo": "Registro para seguimiento fenologico. En dormancia activa los GDD son bajos, "
                      "pero son clave para predecir brotacion y cosecha una vez iniciada la temporada.",
        })

    return insights


# ── HTML del email ────────────────────────────────────────────────────────────
# ── Narrativa analitica ───────────────────────────────────────────────────────
DIAS_ES = {0:"lun",1:"mar",2:"mie",3:"jue",4:"vie",5:"sab",6:"dom"}
MESES_ES = {1:"enero",2:"febrero",3:"marzo",4:"abril",5:"mayo",6:"junio",
            7:"julio",8:"agosto",9:"septiembre",10:"octubre",11:"noviembre",12:"diciembre"}

FENOLOGIA_CONFIG_PATH = Path(__file__).parent / "fenologia_config.json"

def cargar_fenologia_config() -> dict:
    """Lee el archivo de configuracion fenologica. Fallback a estimacion por mes."""
    try:
        with open(FENOLOGIA_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def etapa_fenologica(mes: int) -> str:
    """Etapa estimada por mes como fallback si no hay config."""
    if mes in (4, 5, 6, 7):   return "dormancia"
    if mes == 8:               return "pre-floracion"
    if mes == 9:               return "floracion"
    if mes == 10:              return "cuaje"
    if mes in (11, 12, 1):    return "maduracion"
    return "post-cosecha"

# Mapa de etapa_predominante del config a etapa interna
ETAPA_MAP = {
    "inicio_dormancia":  "dormancia",
    "dormancia":         "dormancia",
    "pre_floracion":     "pre-floracion",
    "floracion":         "floracion",
    "cuaje":             "cuaje",
    "maduracion":        "maduracion",
    "post_cosecha":      "post-cosecha",
    "pre_dormancia":     "dormancia",
}


def generar_narrativa(daily: pd.DataFrame, df_raw: pd.DataFrame,
                      horas_frio_acum: float, fecha_desde: datetime,
                      fecha_hasta: datetime,
                      fenologia: dict = None) -> str:
    """
    Genera texto narrativo con analisis y recomendaciones basadas en los datos reales
    y en el conocimiento agronomico del cuaderno NotebookLM 'Arandanos - variables climaticas'.
    Devuelve HTML listo para insertar en el email.
    """
    if daily.empty:
        return ""

    mes_actual = fecha_hasta.month
    etapa = etapa_fenologica(mes_actual)

    # ── Datos fenologicos del config (si disponible) ──────────────────────────
    req_horas_frio    = {}
    grupos_fenologia  = {}
    alerta_fenologica = False
    texto_alerta_fen  = ""
    variedades_avanz  = []
    lotes_avanz       = []
    vars_pre_dorm     = []
    fecha_med_fenol   = ""
    if fenologia:
        etapa             = ETAPA_MAP.get(fenologia.get("etapa_predominante", ""), etapa)
        req_horas_frio    = fenologia.get("requerimiento_horas_frio", {})
        grupos_fenologia  = fenologia.get("grupos", {})
        alerta_fenologica = fenologia.get("alerta_activa", False)
        texto_alerta_fen  = fenologia.get("texto_alerta", "")
        variedades_avanz  = grupos_fenologia.get("avanzado", {}).get("variedades", [])
        lotes_avanz       = grupos_fenologia.get("avanzado", {}).get("lotes", [])
        vars_pre_dorm     = grupos_fenologia.get("pre_dormancia", {}).get("variedades", [])
        fecha_med_fenol   = fenologia.get("fecha_medicion", "")

    t_min_sem    = daily["temp_min"].min()
    t_max_sem    = daily["temp_max"].max()
    lluvia_tot   = daily["lluvia"].sum()
    rad_max_sem  = daily["rad_max"].max()
    rad_avg_sem  = daily["rad_avg"].mean()
    hf_sem       = daily["horas_frio"].sum()
    he_sem       = daily["horas_helada"].sum()
    gdd_sem      = daily["gdd"].sum()
    dias_lluvia  = int((daily["lluvia"] > 0).sum())
    humedad_ok   = daily["humedad_avg"].mean() > 5

    # Dia mas frio
    idx_min = daily["temp_min"].idxmin()
    dia_mas_frio = daily.loc[idx_min, "fecha"].strftime("%d/%m")
    t_min_dia = daily.loc[idx_min, "temp_min"]

    # Dias con frio intenso (>4h bajo 7°C)
    dias_frio_intenso = daily[daily["horas_frio"] > 4]["fecha"].dt.strftime("%d/%m").tolist()

    # ETo aproximada (Hargreaves simplificado sin radiacion extraterrestre)
    # ETo ≈ 0.0023 * (Tmean + 17.8) * (Tmax - Tmin)^0.5 * Ra
    # Sin Ra usamos radiacion medida como proxy
    eto_diario = []
    for _, r in daily.iterrows():
        if pd.notna(r["temp_min"]) and pd.notna(r["temp_max"]) and r["rad_max"] > 0:
            tmean = (r["temp_max"] + r["temp_min"]) / 2
            # Conversion w/m2 a MJ/m2/dia (multiplicar por 0.0864)
            rs = r["rad_max"] * 0.0864
            eto = 0.0023 * (tmean + 17.8) * ((r["temp_max"] - r["temp_min"]) ** 0.5) * rs * 0.408
            eto_diario.append(max(0, eto))
    eto_sem = sum(eto_diario)
    deficit = max(0, eto_sem - lluvia_tot)

    # Humedad - dias con riesgo fungico
    if humedad_ok:
        dias_hum_alta = daily[daily["humedad_avg"] > 80]
        n_dias_hum_riesgo = len(dias_hum_alta)
    else:
        n_dias_hum_riesgo = -1  # sensor roto

    # Calcular semanas restantes hasta floracion
    if etapa == "dormancia":
        mes_floracion = 9
        semanas_floracion = max(0, int(((mes_floracion - mes_actual) * 4.3)))
    else:
        semanas_floracion = 0

    # ── Construccion del texto por secciones ──────────────────────────────────
    secciones = []

    # ---- 1. HORAS DE FRIO ----
    if etapa == "dormancia":
        if dias_frio_intenso:
            dias_frio_str = ", ".join(dias_frio_intenso)
            detalle_frio = (f"Los dias {dias_frio_str} registraron mas de 4 horas bajo 7&deg;C, "
                           f"con una minima de <strong>{t_min_dia:.1f}&deg;C el {dia_mas_frio}</strong>. "
                           f"En el NOA, temperaturas de hasta 10&ndash;12&deg;C tambien son fisiologicamente "
                           f"efectivas para acumular frio en Southern Highbush.")
        else:
            detalle_frio = (f"La semana fue relativamente templada: minima de {t_min_dia:.1f}&deg;C el "
                           f"{dia_mas_frio}. En el NOA, temperaturas de hasta 12&ndash;15&deg;C pueden "
                           f"contribuir a romper la endodormicion en estas variedades.")

        # Estado por variedad usando el config fenologico
        if req_horas_frio:
            vars_cumplidas    = [v for v, r in req_horas_frio.items() if horas_frio_acum >= r]
            vars_prox         = [v for v, r in req_horas_frio.items() if 0 < r - horas_frio_acum <= 100]
            vars_lejos        = [v for v, r in req_horas_frio.items() if r - horas_frio_acum > 100]
            req_min           = min(req_horas_frio.values())
            req_max           = max(req_horas_frio.values())

            if vars_cumplidas:
                cumplidas_str = ", ".join(vars_cumplidas)
                estado_acum = (f"Con <strong>{horas_frio_acum:.0f} horas acumuladas desde abril</strong>, "
                               f"las variedades <strong>{cumplidas_str}</strong> ya completaron su requerimiento. "
                               f"Si quedan variedades sin cubrir ({', '.join(vars_lejos) if vars_lejos else 'ninguna'}), "
                               f"continuar el monitoreo semanal.")
            elif vars_prox:
                prox_str = ", ".join(vars_prox)
                restante = min(req_horas_frio[v] for v in vars_prox) - horas_frio_acum
                estado_acum = (f"Con <strong>{horas_frio_acum:.0f} horas acumuladas</strong>, "
                               f"las variedades <strong>{prox_str}</strong> estan a menos de 100 horas de "
                               f"completar su requerimiento. Faltan ~{restante:.0f} horas para la mas proxima.")
            else:
                estado_acum = (f"Se llevan <strong>{horas_frio_acum:.0f} horas acumuladas</strong> desde abril "
                               f"(rango de variedades: {req_min}&ndash;{req_max} h). "
                               f"Acumulacion insuficiente puede derivar en floracion asincronica "
                               f"(desfase de 50&ndash;60 dias) y menor firmeza de fruto en temporada.")
        else:
            if horas_frio_acum >= 400:
                estado_acum = (f"Con <strong>{horas_frio_acum:.0f} horas acumuladas desde abril</strong>, "
                               f"las variedades de menor requerimiento ya completaron dormancia (300&ndash;400 h). "
                               f"Monitorear brotacion anticipada.")
            elif horas_frio_acum >= 200:
                estado_acum = (f"Se llevan <strong>{horas_frio_acum:.0f} horas acumuladas</strong> desde abril. "
                               f"Acumulacion en progreso para el rango Southern Highbush (200&ndash;500 h).")
            else:
                estado_acum = (f"Acumulado de <strong>{horas_frio_acum:.0f} horas</strong> desde abril. "
                               f"Fase temprana de acumulacion. Quedan varios meses de invierno en el NOA.")

        alerta_avanz_str = ""
        if variedades_avanz:
            alerta_avanz_str = (f" <strong style='color:#ffaa00'>Atencion:</strong> lotes "
                               f"{', '.join(lotes_avanz)} de {', '.join(variedades_avanz)} ya muestran "
                               f"R1&ndash;R3 (yemas activas), lo que puede indicar entrada irregular a dormancia.")

        decision = ("El momento exacto de aplicacion de cianamida hidrogenada (H&#8322;CN&#8322;) "
                   "depende de que la variedad haya completado su requerimiento de frio. "
                   "Aplicarla antes genera brotacion dispareja; tarde, se pierde la ventana de mercado. "
                   "En NOA la decision optima suele caer entre julio y agosto.")

        secciones.append({
            "num": "1", "titulo": "Horas de frio &mdash; variable critica de la semana",
            "color": "#74c0fc",
            "cuerpo": (f"Esta semana se acumularon <strong>{hf_sem:.1f} horas de frio</strong> (T &lt; 7&deg;C). "
                      f"{detalle_frio} {estado_acum}{alerta_avanz_str}"),
            "decision": decision,
        })
    else:
        secciones.append({
            "num": "1", "titulo": "Horas de frio &mdash; acumulado de temporada",
            "color": "#74c0fc",
            "cuerpo": (f"En etapa de <strong>{etapa}</strong>, las horas de frio ya no son "
                      f"la variable critica. El acumulado de la temporada fue de "
                      f"<strong>{horas_frio_acum:.0f} horas</strong> desde abril."),
            "decision": "Mantener registro para planificacion de la temporada siguiente.",
        })

    # ---- 2. RIESGO DE HELADA ----
    # Umbrales criticos por etapa fenologica (fuente: Ecofisiologia NOA + literatura)
    # R0-dormancia: tolera hasta -15°C / -28°C (tejido lignificado)
    # R1 yema hinchada: -12°C a -9°C
    # R2 boton visible: -5°C a -4°C
    # R3 flores abiertas: -4°C a -2.2°C
    # R4 plena floracion: limite precaucion -0.6°C
    # R4-R5 cuaje: 0°C = perdida total

    # Determinar umbral real segun etapa + deteccion de R en config
    if variedades_avanz:
        # Hay lotes con R1-R3 en dormancia: usar umbral de R1-R2
        umbral_critico = -9.0
        nota_umbral = (f"Atencion: lotes {', '.join(lotes_avanz)} de {', '.join(variedades_avanz)} "
                      f"muestran R1&ndash;R2 activo. En este estado el umbral critico baja a "
                      f"<strong>&minus;9&deg;C a &minus;4&deg;C</strong> (vs. &minus;28&deg;C en dormancia completa).")
    elif etapa == "dormancia":
        umbral_critico = -15.0
        nota_umbral = ("En dormancia completa (R0), la planta tolera hasta &minus;15&deg;C (madera) "
                      "sin sufrir danos estructurales.")
    elif etapa == "pre-floracion":
        umbral_critico = -4.0
        nota_umbral = "Boton visible (R2): umbral critico entre &minus;5&deg;C y &minus;4&deg;C."
    elif etapa == "floracion":
        umbral_critico = -0.6
        nota_umbral = ("Flor abierta (R3&ndash;R4): <strong>umbral critico &minus;2.2&deg;C a &minus;0.6&deg;C</strong>. "
                      "Cuaje (R4&ndash;R5): 0&deg;C destruye el ovario.")
    else:
        umbral_critico = -2.0
        nota_umbral = "Revisar el estado fenologico exacto para evaluar el impacto."

    if he_sem > 0:
        detalle_helada = (f"Se registraron <strong>{he_sem:.1f} horas bajo 0&deg;C</strong> "
                         f"(minima: <strong>{t_min_sem:.1f}&deg;C</strong> el {dia_mas_frio}).")
        if etapa == "dormancia" and not variedades_avanz:
            impacto = (f"En dormancia completa la planta tolera hasta &minus;15&deg;C a &minus;28&deg;C. "
                      f"Con una minima de {t_min_sem:.1f}&deg;C no hay dano esperado en tejido lignificado.")
            urgencia = ("Verificar que ningun lote haya entrado a R1&ndash;R2 anticipadamente: "
                       "en ese estado el umbral critico cae a &minus;9&deg;C. "
                       "Calibrar el sistema de aspersion anti-helada para floracion (agosto&ndash;septiembre).")
        elif variedades_avanz and t_min_sem <= umbral_critico:
            impacto = (f"<strong style='color:#ff4444'>RIESGO REAL:</strong> {nota_umbral} "
                      f"La minima de {t_min_sem:.1f}&deg;C puede haber danado yemas en estado R1&ndash;R2.")
            urgencia = ("Inspeccionar yemas en lotes avanzados. "
                       "Si el sistema de aspersion esta disponible, activarlo cuando T &le; 1&ndash;2&deg;C "
                       "(aprovecha calor latente de fusion: mantiene el ovario a 0&deg;C constante).")
        elif etapa in ("floracion", "pre-floracion"):
            impacto = (f"<strong style='color:#ff4444'>CRITICO:</strong> {nota_umbral} "
                      f"La minima de {t_min_sem:.1f}&deg;C puede haber afectado flores abiertas. "
                      "Evaluar perdida con recuento en parcelas representativas.")
            urgencia = ("Activar aspersion cuando T descienda a 1&ndash;2&deg;C. "
                       "NO apagar hasta que el sol derrita el hielo naturalmente.")
        else:
            impacto = (f"La minima de {t_min_sem:.1f}&deg;C estuvo bajo 0&deg;C. "
                      f"{nota_umbral} Evaluar impacto segun el estado exacto de cada lote.")
            urgencia = "Revisar el sistema antihelada y monitorear los lotes mas avanzados fenologicamente."

        secciones.append({
            "num": "2", "titulo": "Helada registrada &mdash; accion requerida",
            "color": "#ff4444",
            "cuerpo": f"{detalle_helada} {impacto}",
            "decision": urgencia,
        })
    elif t_min_sem < 3.0 and etapa in ("floracion", "pre-floracion"):
        secciones.append({
            "num": "2", "titulo": f"Riesgo de helada &mdash; minima de {t_min_sem:.1f}&deg;C en {etapa}",
            "color": "#ffaa00",
            "cuerpo": (f"Sin helada registrada, pero la minima de <strong>{t_min_sem:.1f}&deg;C el {dia_mas_frio}</strong> "
                      f"estuvo cerca del umbral critico. {nota_umbral} "
                      f"Cielo despejado + viento calmo + presion en baja es la firma tipica de helada radiativa en NOA."),
            "decision": ("Mantener el sistema de aspersion listo para activar desde 1&ndash;2&deg;C. "
                        "Monitorear el pronostico nocturno a diario durante la floracion."),
        })
    elif etapa == "dormancia":
        color_helada = "#2ecc71" if t_min_sem >= 0 else ("#ffaa00" if variedades_avanz else "#2ecc71")
        avanz_nota = f" {nota_umbral}" if variedades_avanz else ""
        secciones.append({
            "num": "2", "titulo": f"Riesgo de helada &mdash; minima de {t_min_sem:.1f}&deg;C",
            "color": color_helada,
            "cuerpo": (f"La minima de la semana fue <strong>{t_min_sem:.1f}&deg;C el {dia_mas_frio}</strong>.{avanz_nota} "
                      f"En dormancia completa el tejido lignificado tolera hasta &minus;15&deg;C. "
                      f"El riesgo aparece en <strong>agosto&ndash;septiembre</strong> cuando las flores abren."),
            "decision": (f"Verificar y calibrar el sistema antihelada ahora, con anticipacion. "
                        f"Faltan aproximadamente {semanas_floracion} semanas para la floracion en Tucuman."),
        })

    # ---- 3. ESTRES HIDRICO Y RIEGO ----
    # El arandano tiene 80-85% de raices en los primeros 15-30 cm, sin pelos radiculares.
    # Esto lo hace extremadamente sensible tanto al deficit como al encharcamiento (48h = muerte radicular).
    if eto_sem > 0:
        if deficit > 15:
            estado_riego = (f"Con solo <strong>{lluvia_tot:.1f} mm de lluvia</strong> y un ETo estimado de "
                           f"<strong>{eto_sem:.1f} mm</strong>, el deficit hidrico semanal fue de "
                           f"<strong>{deficit:.0f} mm</strong>. El cultivo dependio del riego.")
            decision_riego = (f"Verificar que el sistema de riego repuso al menos {deficit:.0f} mm en la semana. "
                             f"En {etapa}, el estres hidrico impacta en " +
                             ("tamano y firmeza de fruto (cracking al retomar riego bruscamente)."
                              if etapa in ("cuaje","maduracion")
                              else "diferenciacion floral y reservas para la proxima temporada."))
        elif deficit > 5:
            estado_riego = (f"Lluvia de <strong>{lluvia_tot:.1f} mm</strong> en "
                           f"{dias_lluvia} dia{'s' if dias_lluvia > 1 else ''}, "
                           f"ETo estimado {eto_sem:.1f} mm. Deficit moderado de <strong>{deficit:.0f} mm</strong>.")
            decision_riego = ("Complementar con riego segun humedad de suelo. "
                             "No asumir que la lluvia fue suficiente: la distribucion espacial puede ser desigual.")
        else:
            estado_riego = (f"La lluvia de <strong>{lluvia_tot:.1f} mm</strong> cubrio la mayor parte del "
                           f"ETo estimado ({eto_sem:.1f} mm). Semana favorable en terminos hidricos.")
            decision_riego = ("Monitorear humedad del suelo igualmente: lluvia intensa puede superar la "
                             "capacidad de infiltracion. El arandano no tolera encharcamiento mas de 48 h "
                             "(el sistema radicular no tiene pelos radiculares y es altamente superficial).")
    else:
        estado_riego = f"Se registraron <strong>{lluvia_tot:.1f} mm</strong> de lluvia en la semana."
        decision_riego = ("Verificar humedad del suelo con tensiometros. "
                         "El 80&ndash;85% de las raices del arandano estan en los primeros 15&ndash;30 cm: "
                         "sensible al deficit, pero tambien al exceso (muerte radicular en 48 h de anoxia).")

    secciones.append({
        "num": "3", "titulo": "Estres hidrico y riego",
        "color": "#4a90d9",
        "cuerpo": (f"{estado_riego} La radiacion solar de la semana: "
                  f"<strong>{rad_max_sem:.0f} w/m&sup2; maxima</strong>, "
                  f"{rad_avg_sem:.0f} w/m&sup2; promedio &mdash; "
                  f"{'alta demanda evaporativa' if rad_max_sem > 500 else 'demanda evaporativa moderada'}."),
        "decision": decision_riego,
    })

    # ---- 4. PRESION DE ENFERMEDADES FUNGICAS ----
    # Botrytis cinerea: optimo a 20-24°C + HR persistente > 80%. Coloniza flores senescentes
    # y frutos maduros. Se disemina via viento, lluvia y corrientes de aire.
    # Roya de la Hoja: requiere >36 h con HR >90% y T <20°C para germinar (referencia).
    humedad_avg_sem = daily["humedad_avg"].mean() if humedad_ok else 0

    if n_dias_hum_riesgo == -1:
        cuerpo_hongos = ("El sensor de humedad relativa muestra lecturas anormales (cercanas a 0%), "
                        "lo que indica un problema de <strong>calibracion o dano en el sensor</strong>. "
                        "Sin datos de HR validos no es posible evaluar riesgo de Botrytis cinerea, "
                        "Antracnosis ni Roya de la Hoja.")
        decision_hongos = ("<strong>Prioridad alta:</strong> Revisar y calibrar el sensor antes de la "
                          "floracion. La HR es la variable mas critica para modelar infeccion fungica.")
    elif n_dias_hum_riesgo >= 3:
        t_avg_sem = daily["temp_avg"].mean()
        riesgo_botrytis = "alto" if 18 <= t_avg_sem <= 26 else "moderado"
        cuerpo_hongos = (f"Se registraron <strong>{n_dias_hum_riesgo} dias con HR media &gt; 80%</strong> "
                        f"(HR promedio semanal: {humedad_avg_sem:.0f}%). "
                        f"Temperatura promedio {t_avg_sem:.1f}&deg;C: riesgo de Botrytis cinerea "
                        f"<strong>{riesgo_botrytis}</strong> (optimo 20&ndash;24&deg;C + HR elevada). "
                        f"La lluvia y el rocio nocturno son los principales vectores de diseminacion.")
        decision_hongos = (f"Evaluar aplicacion preventiva de fungicida (Captan, Iprodione o Fenhexamid). "
                          f"El modelo de Mills recomienda tratar dentro de las 24&ndash;48 h de un evento "
                          f"de infeccion posible ({n_dias_hum_riesgo} dias de riesgo esta semana). "
                          f"Priorizar los lotes en estado R3&ndash;R5 (flores abiertas y frutos).")
    elif n_dias_hum_riesgo > 0:
        cuerpo_hongos = (f"HR elevada en {n_dias_hum_riesgo} dia{'s' if n_dias_hum_riesgo > 1 else ''} "
                        f"(HR promedio: {humedad_avg_sem:.0f}%), sin alcanzar el umbral critico sostenido "
                        f"(&gt;80% por varios dias). Presion fungica moderada. "
                        f"Botrytis cinerea coloniza flores senescentes y frutos maduros; "
                        f"se disemina por viento y salpicadura de lluvia.")
        decision_hongos = ("Monitorear aparicion de sintomas en campo. "
                          "Si el pronostico incluye lluvia nocturna, evaluar aplicacion preventiva en lotes con frutos.")
    else:
        cuerpo_hongos = (f"HR promedio semanal: {humedad_avg_sem:.0f}%. "
                        "Humedad relativa dentro de rangos normales. Presion fungica baja esta semana.")
        decision_hongos = ("Sin accion inmediata. Continuar monitoreo regular de campo. "
                          "Revisar condiciones antes de la floracion (mayor vulnerabilidad en flores abiertas).")

    secciones.append({
        "num": "4", "titulo": "Presion de enfermedades fungicas",
        "color": "#a8e6cf",
        "cuerpo": cuerpo_hongos,
        "decision": decision_hongos,
    })

    # ---- 5. RADIACION SOLAR Y CALIDAD DE FRUTA ----
    # Radiacion PAR: sintesis de antocianinas (color) es foto-dependiente.
    # Alta rad. -> mayor Brix y relacion solidos/acidez.
    # Exceso de rad. con T >30°C: degradacion de pruina, ablandamiento prematuro.
    # T >30°C: cierre de estomas, fotosintesis cae, fruto pierde tamano y firmeza.
    dias_calor = int((daily["temp_max"] > 30).sum()) if not daily.empty else 0

    if etapa in ("maduracion", "cuaje"):
        if rad_avg_sem > 500:
            eval_rad = (f"Excelente radiacion solar (<strong>{rad_avg_sem:.0f} w/m&sup2; promedio</strong>). "
                       f"Alta PAR en las semanas previas a cosecha correlaciona con mayor acumulacion de "
                       f"azucares (&deg;Brix), mejor coloracion (sintesis de antocianinas foto-dependiente) "
                       f"y mayor firmeza de baya. La pruina (cera protectora del fruto) se preserva mejor "
                       f"sin radiacion extrema ni calor excesivo.")
            dec_rad = ("Anticipar fruta de alta calidad para mercado fresco. "
                      "Monitorear &deg;Brix en campo (&gt;12 objetivo) antes de decidir fecha de cosecha.")
        else:
            eval_rad = (f"Radiacion solar moderada ({rad_avg_sem:.0f} w/m&sup2; promedio). "
                       f"Semanas con baja PAR antes de cosecha reducen la acumulacion de azucares y "
                       f"pueden afectar la coloracion de la fruta (sintesis de antocianinas).")
            dec_rad = ("Considerar postergar cosecha si el pronostico indica mejora. "
                      "Medir &deg;Brix en campo antes de decidir; fruta cosechada con bajo &deg;Brix "
                      "no mejora en postcosecha.")
        if dias_calor > 0:
            dec_rad += (f" Atencion: {dias_calor} dia{'s' if dias_calor > 1 else ''} con maxima &gt;30&deg;C. "
                       "A esa temperatura los estomas se cierran, la fotosintesis cae y la fruta pierde "
                       "firmeza. Aumentar frecuencia de riego y considerar malla sombra si es recurrente.")
    else:
        if dias_calor > 0 and etapa not in ("dormancia",):
            nota_calor = (f" {dias_calor} dia{'s' if dias_calor > 1 else ''} con maxima &gt;30&deg;C: "
                         "los estomas se cierran y la fotosintesis se detiene hasta el rescaldo. "
                         "Verificar el riego en esos dias.")
        else:
            nota_calor = ""
        eval_rad = (f"Radiacion de <strong>{rad_max_sem:.0f} w/m&sup2; maxima</strong> "
                   f"y {rad_avg_sem:.0f} w/m&sup2; promedio. "
                   f"En {etapa} la PAR es clave para fotosintesis y acumulacion de reservas "
                   f"(carbohidratos que sosteran el vigor de brotacion primaveral).{nota_calor}")
        dec_rad = ("Sin accion directa en esta etapa. "
                  "Radiacion acumulada en dormancia influye en el vigor de brotacion y desarrollo de yemas.")

    secciones.append({
        "num": "5", "titulo": "Radiacion solar y calidad de fruta",
        "color": "#ffb347",
        "cuerpo": eval_rad,
        "decision": dec_rad,
    })

    # ---- 6. GDD Y FENOLOGIA ----
    # Rango para madurez de cosecha: 850-1300 GDD acumulados desde brotacion (base 7°C).
    # En dormancia los GDD son bajos por definicion.

    # Informacion fenologica del config
    if fenologia and fecha_med_fenol:
        fecha_med_str = datetime.strptime(fecha_med_fenol, "%Y-%m-%d").strftime("%d/%m/%Y") if fecha_med_fenol else ""
        desc_fenol = fenologia.get("descripcion", "")
        alerta_str = f" <strong style='color:#ffaa00'>Alerta:</strong> {texto_alerta_fen}" if alerta_fenologica else ""
        etapa_descrita = grupos_fenologia.get("R0_dominante", {}).get("etapa", etapa)
        nota_fenol = (f"Medicion fenologica del {fecha_med_str}: <em>{desc_fenol}</em>{alerta_str}")
    else:
        nota_fenol = ""

    if etapa == "dormancia":
        cuerpo_gdd = (f"Se acumularon <strong>{gdd_sem:.0f} GDD esta semana</strong> (base 7&deg;C). "
                     f"En dormancia, los GDD son bajos ya que las temperaturas medias rondan o caen "
                     f"bajo la base. Una vez iniciada la brotacion (agosto&ndash;septiembre en NOA), "
                     f"se necesitan entre <strong>850 y 1.300 GDD acumulados</strong> para que la "
                     f"fruta alcance madurez de cosecha.")
        if nota_fenol:
            cuerpo_gdd += f"<br><br><em style='color:#888;font-size:13px'>{nota_fenol}</em>"
        dec_gdd = ("Comenzar a registrar GDD desde el primer dia de brotacion (no desde 1 de julio). "
                  "Esto permite estimar la fecha de cosecha con &plusmn;5 dias de anticipacion, "
                  "facilitando la coordinacion de cuadrillas y transporte refrigerado.")
    elif etapa in ("pre-floracion", "floracion"):
        cuerpo_gdd = (f"<strong>{gdd_sem:.0f} GDD acumulados esta semana.</strong> "
                     f"Con los GDD desde brotacion se puede ajustar la ventana de floracion "
                     f"y planificar el ingreso de colmenas.")
        if nota_fenol:
            cuerpo_gdd += f"<br><br><em style='color:#888;font-size:13px'>{nota_fenol}</em>"
        dec_gdd = ("Coordinar ingreso de colmenas. Relacion optima: 5&ndash;8 colmenas fuertes por hectarea. "
                  "El arandano es autofertil pero la polinizacion cruzada mejora el tamano y firmeza del fruto.")
    else:
        cuerpo_gdd = (f"<strong>{gdd_sem:.0f} GDD acumulados esta semana.</strong> "
                     f"Acumulando desde brotacion, se necesitan 850&ndash;1.300 GDD para cosecha. "
                     f"Comparar el acumulado actual con el historico de la finca para estimar "
                     f"la fecha con &plusmn;5 dias de precision.")
        if nota_fenol:
            cuerpo_gdd += f"<br><br><em style='color:#888;font-size:13px'>{nota_fenol}</em>"
        dec_gdd = ("Coordinar cuadrillas, transporte refrigerado y precio forward con compradores "
                  "usando la fecha estimada. La cosecha oportuna es el principal driver de calidad.")

    secciones.append({
        "num": "6", "titulo": "Grados-Dia y fenologia del cultivo",
        "color": "#69db7c",
        "cuerpo": cuerpo_gdd,
        "decision": dec_gdd,
    })

    # ── HTML de la narrativa ───────────────────────────────────────────────────
    html_secciones = ""
    for s in secciones:
        html_secciones += f"""
        <div style="margin-bottom:28px">
          <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:10px">
            <span style="background:{s['color']};color:#000;font-weight:700;font-size:13px;
                         border-radius:50%;width:24px;height:24px;display:inline-flex;
                         align-items:center;justify-content:center;flex-shrink:0">
              {s['num']}
            </span>
            <span style="font-size:16px;font-weight:700;color:{s['color']}">
              {s['titulo']}
            </span>
          </div>
          <div style="font-size:14px;color:#ccc;line-height:1.7;margin-bottom:10px;
                      padding-left:34px">
            {s['cuerpo']}
          </div>
          <div style="font-size:13px;background:#1e1e1e;border-left:3px solid {s['color']};
                      border-radius:0 6px 6px 0;padding:10px 14px;margin-left:34px;
                      color:#bbb;line-height:1.6">
            <span style="color:{s['color']};font-weight:700">Decision que habilita: </span>
            {s['decision']}
          </div>
        </div>"""

    return f"""
    <div style="font-size:16px;font-weight:700;color:#7fcf7f;margin-bottom:18px;
                border-bottom:1px solid #2a2a2a;padding-bottom:10px">
      Analisis Agronomico &mdash; Semana del {fecha_desde.strftime('%d/%m')} al {fecha_hasta.strftime('%d/%m/%Y')}
    </div>
    <div style="font-size:12px;color:#555;margin-bottom:20px">
      Etapa fenologica actual: <strong style="color:#888">{etapa.upper()}</strong>
      &nbsp;&middot;&nbsp; {MESES_ES[fecha_hasta.month].capitalize()} en el NOA
    </div>
    {html_secciones}"""


COLORES_INSIGHT = {
    "peligro": ("#5c1a1a", "#ff4444", "&#9888;"),
    "alerta":  ("#3d2b00", "#ffaa00", "&#9888;"),
    "ok":      ("#0d2e1a", "#2ecc71", "&#10003;"),
    "info":    ("#0d1f3c", "#4a90d9", "&#9432;"),
    "warn":    ("#2a1a00", "#ff8800", "&#9888;"),
}

def fmt(v, dec=1, unit=""):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:.{dec}f}{unit}"


def construir_html(daily: pd.DataFrame, df_raw: pd.DataFrame, insights: list[dict],
                   fecha_desde: datetime, fecha_hasta: datetime,
                   horas_frio_acum: float, narrativa_html: str = "") -> str:

    semana_str = f"{fecha_desde.strftime('%d/%m/%Y')} al {fecha_hasta.strftime('%d/%m/%Y')}"
    generado   = datetime.now().strftime("%d/%m/%Y %H:%M")

    # KPIs
    t_min  = daily["temp_min"].min() if not daily.empty else None
    t_max  = daily["temp_max"].max() if not daily.empty else None
    lluvia = daily["lluvia"].sum()   if not daily.empty else 0
    hf_sem = daily["horas_frio"].sum() if not daily.empty else 0
    gdd    = daily["gdd"].sum()      if not daily.empty else 0
    he_sem = daily["horas_helada"].sum() if not daily.empty else 0

    color_tmin = "#ff4444" if (t_min or 99) < 0 else "#ffaa00" if (t_min or 99) < 3 else "#ffffff"
    color_he   = "#ff4444" if he_sem > 0 else "#2ecc71"

    # Tabla diaria
    filas_tabla = ""
    if not daily.empty:
        for _, r in daily.iterrows():
            fondo = "#2a1a1a" if r["horas_helada"] > 0 else "#1e2e1e" if r["horas_frio"] > 4 else "#1a1a1a"
            filas_tabla += f"""
            <tr style="background:{fondo}">
                <td style="padding:8px 12px;border-bottom:1px solid #2a2a2a;font-weight:600">
                    {r['fecha'].strftime('%a %d/%m')}
                </td>
                <td style="padding:8px 12px;border-bottom:1px solid #2a2a2a;color:#4ecdc4;text-align:center">
                    {fmt(r['temp_min'])}°
                </td>
                <td style="padding:8px 12px;border-bottom:1px solid #2a2a2a;color:#ff6b6b;text-align:center">
                    {fmt(r['temp_max'])}°
                </td>
                <td style="padding:8px 12px;border-bottom:1px solid #2a2a2a;color:#4a90d9;text-align:center">
                    {fmt(r['lluvia'])} mm
                </td>
                <td style="padding:8px 12px;border-bottom:1px solid #2a2a2a;color:#a8e6cf;text-align:center">
                    {fmt(r['humedad_avg'], 0)}%
                </td>
                <td style="padding:8px 12px;border-bottom:1px solid #2a2a2a;color:#ffb347;text-align:center">
                    {fmt(r['rad_max'], 0)}
                </td>
                <td style="padding:8px 12px;border-bottom:1px solid #2a2a2a;color:#74c0fc;text-align:center">
                    {fmt(r['horas_frio'], 1)} h
                </td>
                <td style="padding:8px 12px;border-bottom:1px solid #2a2a2a;color:#69db7c;text-align:center">
                    {fmt(r['gdd'], 0)}
                </td>
            </tr>"""

    # Tarjetas de insights
    cards_insights = ""
    for ins in insights:
        bg, color, icon = COLORES_INSIGHT.get(ins["tipo"], COLORES_INSIGHT["info"])
        cards_insights += f"""
        <div style="background:{bg};border-left:4px solid {color};border-radius:6px;
                    padding:14px 18px;margin-bottom:12px">
            <div style="color:{color};font-weight:700;font-size:15px;margin-bottom:4px">
                {icon} {ins['titulo']}
            </div>
            <div style="color:#ccc;font-size:13px;line-height:1.5">
                {ins['cuerpo']}
            </div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Informe Semanal - Finca Leon Rouges</title>
</head>
<body style="margin:0;padding:0;background:#0d0d0d;font-family:Arial,Helvetica,sans-serif;color:#e0e0e0">

<!-- Wrapper -->
<table width="100%" cellpadding="0" cellspacing="0" style="background:#0d0d0d;padding:20px 0">
<tr><td align="center">
<table width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%">

  <!-- HEADER -->
  <tr>
    <td style="background:linear-gradient(135deg,#0f2a0f,#1a4a1a);
               border-radius:12px 12px 0 0;padding:30px 32px;text-align:center">
      <div style="font-size:28px;margin-bottom:6px">&#127815;</div>
      <div style="font-size:22px;font-weight:700;color:#7fcf7f;letter-spacing:0.5px">
        Informe Semanal Meteorologico
      </div>
      <div style="font-size:14px;color:#aaa;margin-top:6px">
        Finca Leon Rouges &mdash; {semana_str}
      </div>
    </td>
  </tr>

  <!-- KPIs -->
  <tr>
    <td style="background:#141414;padding:24px 32px">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td width="25%" style="text-align:center;padding:0 6px">
            <div style="background:#1e1e1e;border:1px solid #2a2a2a;border-radius:10px;padding:16px 8px">
              <div style="font-size:24px;font-weight:700;color:{color_tmin}">{fmt(t_min)}°C</div>
              <div style="font-size:11px;color:#888;margin-top:4px;text-transform:uppercase">Temp minima</div>
              <div style="font-size:11px;color:#aaa;margin-top:2px">Max {fmt(t_max)}°C</div>
            </div>
          </td>
          <td width="25%" style="text-align:center;padding:0 6px">
            <div style="background:#1e1e1e;border:1px solid #2a2a2a;border-radius:10px;padding:16px 8px">
              <div style="font-size:24px;font-weight:700;color:#4a90d9">{fmt(lluvia, 1)} mm</div>
              <div style="font-size:11px;color:#888;margin-top:4px;text-transform:uppercase">Lluvia total</div>
            </div>
          </td>
          <td width="25%" style="text-align:center;padding:0 6px">
            <div style="background:#1e1e1e;border:1px solid #2a2a2a;border-radius:10px;padding:16px 8px">
              <div style="font-size:24px;font-weight:700;color:#74c0fc">{fmt(hf_sem, 1)} h</div>
              <div style="font-size:11px;color:#888;margin-top:4px;text-transform:uppercase">Horas de frio</div>
              <div style="font-size:11px;color:#aaa;margin-top:2px">Acum. temporada: {fmt(horas_frio_acum, 0)} h</div>
            </div>
          </td>
          <td width="25%" style="text-align:center;padding:0 6px">
            <div style="background:#1e1e1e;border:1px solid #2a2a2a;border-radius:10px;padding:16px 8px">
              <div style="font-size:24px;font-weight:700;color:{color_he}">{fmt(he_sem, 1)} h</div>
              <div style="font-size:11px;color:#888;margin-top:4px;text-transform:uppercase">Horas helada</div>
              <div style="font-size:11px;color:#aaa;margin-top:2px">GDD semana: {fmt(gdd, 0)}</div>
            </div>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- NARRATIVA ANALITICA -->
  <tr>
    <td style="background:#141414;padding:0 32px 24px">
      {narrativa_html}
    </td>
  </tr>

  <!-- ALERTAS RAPIDAS -->
  <tr>
    <td style="background:#141414;padding:0 32px 24px">
      <div style="font-size:16px;font-weight:700;color:#7fcf7f;margin-bottom:14px;
                  border-bottom:1px solid #2a2a2a;padding-bottom:10px">
        Alertas de la semana
      </div>
      {cards_insights}
    </td>
  </tr>

  <!-- TABLA DIARIA -->
  <tr>
    <td style="background:#141414;padding:0 32px 28px">
      <div style="font-size:16px;font-weight:700;color:#7fcf7f;margin-bottom:14px;
                  border-bottom:1px solid #2a2a2a;padding-bottom:10px">
        Resumen Diario
      </div>
      <table width="100%" cellpadding="0" cellspacing="0"
             style="border-radius:8px;overflow:hidden;font-size:13px">
        <tr style="background:#2a2a2a">
          <th style="padding:10px 12px;text-align:left;color:#aaa;font-weight:600">Dia</th>
          <th style="padding:10px 12px;color:#4ecdc4;font-weight:600">T min</th>
          <th style="padding:10px 12px;color:#ff6b6b;font-weight:600">T max</th>
          <th style="padding:10px 12px;color:#4a90d9;font-weight:600">Lluvia</th>
          <th style="padding:10px 12px;color:#a8e6cf;font-weight:600">Hum.</th>
          <th style="padding:10px 12px;color:#ffb347;font-weight:600">Rad.</th>
          <th style="padding:10px 12px;color:#74c0fc;font-weight:600">H.Frio</th>
          <th style="padding:10px 12px;color:#69db7c;font-weight:600">GDD</th>
        </tr>
        {filas_tabla}
      </table>
      <div style="font-size:11px;color:#555;margin-top:8px">
        T min/max en °C &middot; Rad. en w/m2 &middot; Horas frio = T &lt; 7°C &middot;
        GDD base 7°C
      </div>
    </td>
  </tr>

  <!-- FOOTER -->
  <tr>
    <td style="background:#0f2a0f;border-radius:0 0 12px 12px;
               padding:18px 32px;text-align:center">
      <div style="font-size:12px;color:#666">
        Generado el {generado} &middot; Estacion Pegasus &middot; Finca Leon Rouges, Tucuman
      </div>
      <div style="font-size:11px;color:#444;margin-top:4px">
        Datos actualizados cada lunes a las 7:00 AM
      </div>
    </td>
  </tr>

</table>
</td></tr>
</table>

</body>
</html>"""

    return html


# ── Sync a GitHub (para Streamlit Cloud) ─────────────────────────────────────
import subprocess

def sync_db_a_github(fecha: datetime) -> None:
    """
    Commitea la DB actualizada y la pushea a GitHub.
    Streamlit Community Cloud detecta el push y redespliega el dashboard.
    Requiere que el repo tenga un remote 'origin' configurado.
    """
    repo = Path(__file__).parent
    try:
        # Verificar que hay un remote configurado
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo, capture_output=True, text=True
        )
        if result.returncode != 0:
            log.warning("Git remote 'origin' no configurado. Omitiendo sync a GitHub.")
            return

        fecha_str = fecha.strftime("%Y-%m-%d")
        subprocess.run(["git", "add", "pegasus_arandanos.db", "fenologia_config.json"],
                       cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m",
                        f"data: actualizar DB y fenologia {fecha_str} [auto]"],
                       cwd=repo, capture_output=True)  # puede no haber cambios
        subprocess.run(["git", "push", "origin", "master"],
                       cwd=repo, check=True, capture_output=True)
        log.info("DB pusheada a GitHub. Streamlit Cloud redespliegara automaticamente.")
    except subprocess.CalledProcessError as e:
        log.warning(f"No se pudo pushear a GitHub: {e}")
    except Exception as e:
        log.warning(f"Error en sync_db_a_github: {e}")


# ── Envio de email ────────────────────────────────────────────────────────────
def enviar_email(html: str, fecha_hasta: datetime) -> None:
    semana_str = fecha_hasta.strftime("%d/%m/%Y")
    asunto = f"Informe Meteorologico Semanal - Finca Leon Rouges [{semana_str}]"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = asunto
    msg["From"]    = f"Estacion Meteorologica <{EMAIL_ORIGEN}>"
    msg["To"]      = EMAIL_DESTINO

    msg.attach(MIMEText(html, "html", "utf-8"))

    log.info(f"Enviando email a {EMAIL_DESTINO}...")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(EMAIL_ORIGEN, APP_PASSWORD)
        server.sendmail(EMAIL_ORIGEN, EMAIL_DESTINO, msg.as_string())

    log.info("Email enviado correctamente.")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ahora       = datetime.now()
    fecha_hasta = ahora
    fecha_desde = ahora - timedelta(days=7)

    # 1. Descargar datos frescos
    log.info("Descargando datos de la semana...")
    desde_fmt = fecha_desde.strftime("%d/%m/%Y")
    hasta_fmt = fecha_hasta.strftime("%d/%m/%Y")
    insertados, duplicados, error = descargar_datos(date_from=desde_fmt, date_to=hasta_fmt)
    if error:
        log.warning(f"Descarga con error: {error}")
    log.info(f"Descarga: {insertados} nuevos, {duplicados} duplicados")

    # 2. Cargar datos para el informe
    df_raw, daily = cargar_semana(fecha_hasta)
    if df_raw.empty:
        log.error("Sin datos para generar el informe.")
        sys.exit(1)

    # 3. Horas frio acumuladas en la temporada
    conn = sqlite3.connect(DB_PATH)
    hf_acum = horas_frio_acumuladas(conn)
    conn.close()

    # 4. Cargar config fenologico y generar insights + narrativa
    fenologia = cargar_fenologia_config()
    if fenologia:
        log.info(f"Fenologia cargada: {fenologia.get('etapa_predominante','?')} "
                 f"(medicion {fenologia.get('fecha_medicion','?')})")
    else:
        log.warning("No se encontro fenologia_config.json; usando estimacion por mes.")

    insights  = generar_insights(df_raw, daily)
    narrativa = generar_narrativa(daily, df_raw, hf_acum, fecha_desde, fecha_hasta,
                                  fenologia=fenologia)

    # 5. Construir HTML
    html = construir_html(daily, df_raw, insights, fecha_desde, fecha_hasta, hf_acum,
                          narrativa_html=narrativa)

    # 6. Guardar copia local
    ruta_html = Path(__file__).parent / f"informe_{ahora.strftime('%Y%m%d')}.html"
    ruta_html.write_text(html, encoding="utf-8")
    log.info(f"Informe guardado en {ruta_html}")

    # 7. Enviar email
    enviar_email(html, fecha_hasta)

    # 8. Pushear DB actualizada a GitHub (para Streamlit Community Cloud)
    sync_db_a_github(ahora)


if __name__ == "__main__":
    main()
