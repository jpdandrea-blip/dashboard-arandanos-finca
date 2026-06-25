"""
Dashboard Meteorologico - Finca Leon Rouges
Lee datos desde Supabase (PostgreSQL).
Hosteado en Hugging Face Spaces — sin hibernacion.
"""
import os
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import streamlit as st
from supabase import create_client

# ── Supabase ──────────────────────────────────────────────────────────────────
SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON = os.environ.get("SUPABASE_ANON_KEY", "")   # anon key (solo lectura)
STATION       = "Finca Leon Rouges"

@st.cache_resource
def get_supabase():
    if not SUPABASE_URL or not SUPABASE_ANON:
        st.error("Variables SUPABASE_URL y SUPABASE_ANON_KEY no configuradas.")
        st.stop()
    return create_client(SUPABASE_URL, SUPABASE_ANON)


# ── Config pagina ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Estacion Finca Leon Rouges",
    page_icon="🫐",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .metric-card {
        background: linear-gradient(135deg, #1e3a1e 0%, #2d5a2d 100%);
        border-radius: 12px; padding: 18px 20px; color: white;
        text-align: center; border: 1px solid #3a7a3a;
    }
    .metric-card .value { font-size: 2.0rem; font-weight: 700; line-height: 1.1; }
    .metric-card .label { font-size: 0.78rem; opacity: 0.85; margin-top: 4px;
        text-transform: uppercase; letter-spacing: 0.05em; }
    .metric-card .delta { font-size: 0.82rem; margin-top: 6px; opacity: 0.75; }
    .alert-frost { background: #5c1a1a; border-left: 4px solid #ff4444;
        padding: 10px 14px; border-radius: 6px; margin: 8px 0; }
    .alert-info  { background: #1a3a5c; border-left: 4px solid #4488ff;
        padding: 10px 14px; border-radius: 6px; margin: 8px 0; }
    [data-testid="stSidebar"] { background-color: #0f1f0f; }
    h1, h2, h3 { color: #7fcf7f; }
</style>
""", unsafe_allow_html=True)


# ── Carga de datos ────────────────────────────────────────────────────────────
@st.cache_data(ttl=300)
def db_date_range() -> tuple[datetime, datetime]:
    sb = get_supabase()
    row = sb.rpc("date_range_mediciones", {"p_estacion": STATION}).execute()
    if row.data and row.data[0]:
        d = row.data[0]
        return datetime.fromisoformat(d["min_ts"]), datetime.fromisoformat(d["max_ts"])
    today = datetime.now()
    return today - timedelta(days=7), today


@st.cache_data(ttl=300)
def load_daily(date_from: str, date_to: str) -> pd.DataFrame:
    """Carga datos diarios via funcion SQL en Supabase (rapido, cualquier rango)."""
    sb = get_supabase()
    resp = sb.rpc("daily_stats", {
        "p_from":    date_from,
        "p_to":      date_to,
        "p_estacion": STATION,
    }).execute()
    if not resp.data:
        return pd.DataFrame()
    df = pd.DataFrame(resp.data)
    df["fecha"] = pd.to_datetime(df["fecha"])
    df["horas_frio_acum"] = df["horas_frio"].cumsum()
    df["gdd"] = ((df["temp_max"] + df["temp_min"]) / 2 - 7).clip(lower=0)
    df["gdd_acum"] = df["gdd"].cumsum()
    return df


@st.cache_data(ttl=300)
def load_raw(date_from: str, date_to: str, max_rows: int = 8000) -> pd.DataFrame:
    """Carga datos crudos de 15 min. Limitado a max_rows para performance."""
    sb = get_supabase()
    resp = (
        sb.table("mediciones")
        .select("timestamp,temperatura_c,humedad_pct,lluvia_mm,radiacion_solar_wm2,vel_viento_kmh,vel_rafaga_kmh,dir_viento_grados,presion_hpa")
        .eq("estacion", STATION)
        .gte("timestamp", date_from)
        .lte("timestamp", date_to)
        .order("timestamp")
        .limit(max_rows)
        .execute()
    )
    if not resp.data:
        return pd.DataFrame()
    df = pd.DataFrame(resp.data)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["fecha"] = df["timestamp"].dt.date
    df["hora"]  = df["timestamp"].dt.hour
    df["horas_frio"] = (df["temperatura_c"] < 7.0).astype(float) * 0.25
    df["helada"]     = df["temperatura_c"] < 0.0
    return df


# ── Helpers ───────────────────────────────────────────────────────────────────
COLORS = {
    "temp_max": "#ff6b6b", "temp_min": "#4ecdc4", "temp_avg": "#ffd93d",
    "lluvia": "#4a90d9",   "humedad": "#a8e6cf",  "rad": "#ffb347",
    "viento": "#c3a6ff",   "horas_frio": "#74c0fc", "gdd": "#69db7c",
}
PLOTLY_LAYOUT = dict(
    paper_bgcolor="#111", plot_bgcolor="#1a1a1a",
    font=dict(color="#ccc", size=12),
    legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(size=11)),
    margin=dict(l=10, r=10, t=40, b=10),
    xaxis=dict(gridcolor="#2a2a2a", showgrid=True),
    yaxis=dict(gridcolor="#2a2a2a", showgrid=True),
)

def metric_card(label, value, delta="", color="#7fcf7f"):
    return f"""<div class="metric-card">
        <div class="value" style="color:{color}">{value}</div>
        <div class="label">{label}</div>
        {"<div class='delta'>" + delta + "</div>" if delta else ""}
    </div>"""

def fmt(v, decimals=1, unit=""):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:.{decimals}f}{unit}"


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🫐 Finca Leon Rouges")
    st.caption("Estacion meteorologica Pegasus")
    st.divider()

    db_min, db_max = db_date_range()

    st.markdown("### Periodo de analisis")
    preset = st.radio("Rango rapido",
        ["Ultima semana", "Ultimas 2 semanas", "Ultimo mes", "Todo"], index=0)
    if preset == "Ultima semana":
        d_from, d_to = db_max.date() - timedelta(days=7),  db_max.date()
    elif preset == "Ultimas 2 semanas":
        d_from, d_to = db_max.date() - timedelta(days=14), db_max.date()
    elif preset == "Ultimo mes":
        d_from, d_to = db_max.date() - timedelta(days=30), db_max.date()
    else:
        d_from, d_to = db_min.date(), db_max.date()

    col_f, col_t = st.columns(2)
    with col_f:
        d_from = st.date_input("Desde", value=d_from, min_value=db_min.date(), max_value=db_max.date())
    with col_t:
        d_to   = st.date_input("Hasta", value=d_to,   min_value=db_min.date(), max_value=db_max.date())

    st.divider()
    st.markdown("### Umbrales criticos")
    umbral_helada = st.number_input("Helada (°C)",          value=0.0,  step=0.5)
    umbral_frio   = st.number_input("Horas frio bajo (°C)", value=7.0,  step=0.5)
    umbral_lluvia = st.number_input("Lluvia alerta (mm/dia)",value=20.0, step=5.0)

    st.divider()
    st.caption(f"DB actualizada: {db_max.strftime('%d/%m/%Y %H:%M')}")
    if st.button("Actualizar datos", use_container_width=True):
        st.cache_data.clear()
        st.rerun()


# ── Cargar datos ──────────────────────────────────────────────────────────────
date_from_str = d_from.strftime("%Y-%m-%d 00:00:00")
date_to_str   = d_to.strftime(  "%Y-%m-%d 23:59:59")

df_daily = load_daily(date_from_str, date_to_str)
if df_daily.empty:
    st.error("Sin datos para el periodo seleccionado.")
    st.stop()

dias_rango = (d_to - d_from).days
df_raw = load_raw(date_from_str, date_to_str) if dias_rango <= 60 else pd.DataFrame()


# ── Header ────────────────────────────────────────────────────────────────────
st.title("🫐 Dashboard Meteorologico — Finca Leon Rouges")
st.caption(
    f"Periodo: **{d_from.strftime('%d/%m/%Y')}** al **{d_to.strftime('%d/%m/%Y')}**  "
    f"· {len(df_daily)} dias analizados"
)

# ── Alertas ───────────────────────────────────────────────────────────────────
t_min_periodo = df_daily["temp_min"].min()
dias_lluvia_alta = df_daily[df_daily["lluvia_total"] >= umbral_lluvia]

if t_min_periodo < umbral_helada:
    st.markdown(f'<div class="alert-frost">⚠️ ALERTA HELADA: Minima de {t_min_periodo:.1f}°C en el periodo (umbral {umbral_helada}°C)</div>', unsafe_allow_html=True)
if not dias_lluvia_alta.empty:
    st.markdown(f'<div class="alert-info">🌧️ {len(dias_lluvia_alta)} dias con lluvia ≥ {umbral_lluvia:.0f} mm</div>', unsafe_allow_html=True)


# ── KPIs ──────────────────────────────────────────────────────────────────────
st.markdown("### Resumen del periodo")
k1, k2, k3, k4, k5, k6, k7 = st.columns(7)

t_min      = df_daily["temp_min"].min()
t_max      = df_daily["temp_max"].max()
lluvia_tot = df_daily["lluvia_total"].sum()
horas_frio = df_daily["horas_frio"].sum()
gdd_tot    = df_daily["gdd"].sum()
helada_hrs = df_daily["horas_helada"].sum()
dias_lluvia = (df_daily["lluvia_total"] > 0).sum()

# Ultima lectura (raw si disponible, sino del ultimo dia diario)
if not df_raw.empty:
    ultimo = df_raw.iloc[-1]
    t_actual = ultimo["temperatura_c"]
    h_actual = ultimo["humedad_pct"]
else:
    t_actual = df_daily.iloc[-1]["temp_avg"]
    h_actual = df_daily.iloc[-1]["humedad_avg"]

with k1:
    color = "#ff6b6b" if (t_actual or 99) < 5 else "#ffd93d" if (t_actual or 99) < 12 else "#7fcf7f"
    st.markdown(metric_card("Temp. reciente", fmt(t_actual, 1, "°C"), f"Min {fmt(t_min,1)}  Max {fmt(t_max,1)}", color), unsafe_allow_html=True)
with k2:
    st.markdown(metric_card("Humedad", fmt(h_actual, 0, "%"), f"Prom periodo {fmt(df_daily['humedad_avg'].mean(), 0)}%", "#a8e6cf"), unsafe_allow_html=True)
with k3:
    color_ll = "#ff6b6b" if lluvia_tot > umbral_lluvia * len(df_daily) else "#4a90d9"
    st.markdown(metric_card("Lluvia total", fmt(lluvia_tot, 1, " mm"), f"{dias_lluvia} dias con lluvia", color_ll), unsafe_allow_html=True)
with k4:
    st.markdown(metric_card("Horas de frio", fmt(horas_frio, 1, " h"), f"T < {umbral_frio}°C", "#74c0fc"), unsafe_allow_html=True)
with k5:
    color_he = "#ff4444" if helada_hrs > 0 else "#7fcf7f"
    st.markdown(metric_card("Horas helada", fmt(helada_hrs, 1, " h"), f"T < {umbral_helada}°C", color_he), unsafe_allow_html=True)
with k6:
    st.markdown(metric_card("GDD acum.", fmt(gdd_tot, 0), f"Base {umbral_frio}°C", "#69db7c"), unsafe_allow_html=True)
with k7:
    rad_max = df_daily["rad_max"].max()
    st.markdown(metric_card("Rad. solar max", fmt(rad_max, 0, " w/m²"), f"Prom {fmt(df_daily['rad_avg'].mean(), 0)}", "#ffb347"), unsafe_allow_html=True)


# ── Temperatura ───────────────────────────────────────────────────────────────
st.divider()
st.markdown("### Temperatura")

tabs = ["Diario (min/max/avg)"]
if not df_raw.empty:
    tabs.append("Cada 15 minutos")
tab_views = st.tabs(tabs)

with tab_views[0]:
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df_daily["fecha"], y=df_daily["temp_max"],
        name="Max", line=dict(color=COLORS["temp_max"], width=2)))
    fig.add_trace(go.Scatter(x=df_daily["fecha"], y=df_daily["temp_min"],
        name="Min", line=dict(color=COLORS["temp_min"], width=2),
        fill="tonexty", fillcolor="rgba(78,205,196,0.12)"))
    fig.add_trace(go.Scatter(x=df_daily["fecha"], y=df_daily["temp_avg"],
        name="Promedio", line=dict(color=COLORS["temp_avg"], width=2, dash="dot")))
    fig.add_hline(y=umbral_helada, line_color="#ff4444", line_dash="dash",
                  annotation_text=f"Helada ({umbral_helada}°C)", annotation_position="top left")
    fig.add_hline(y=umbral_frio, line_color="#74c0fc", line_dash="dash",
                  annotation_text=f"Frio ({umbral_frio}°C)", annotation_position="bottom left")
    fig.update_layout(**PLOTLY_LAYOUT, title="Temperatura diaria (°C)", yaxis_title="°C", height=350)
    st.plotly_chart(fig, use_container_width=True)

if not df_raw.empty and len(tab_views) > 1:
    with tab_views[1]:
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(x=df_raw["timestamp"], y=df_raw["temperatura_c"],
            name="Temp 15 min", line=dict(color=COLORS["temp_avg"], width=1), mode="lines"))
        fig2.add_hline(y=umbral_helada, line_color="#ff4444", line_dash="dash")
        fig2.update_layout(**PLOTLY_LAYOUT, title="Temperatura cada 15 min (°C)", yaxis_title="°C", height=350)
        st.plotly_chart(fig2, use_container_width=True)
elif dias_rango > 60 and len(tab_views) == 1:
    st.info("Vista 15 min disponible para rangos ≤ 60 dias. Selecciona un rango mas corto.")


# ── Precipitacion + Humedad ───────────────────────────────────────────────────
col_ll, col_hum = st.columns(2)

with col_ll:
    st.markdown("### Precipitacion")
    colors_lluvia = ["#ff4444" if v >= umbral_lluvia else "#4a90d9" for v in df_daily["lluvia_total"]]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=df_daily["fecha"], y=df_daily["lluvia_total"],
        name="Lluvia diaria", marker_color=colors_lluvia,
        hovertemplate="%{x}<br>%{y:.1f} mm<extra></extra>"))
    fig.add_hline(y=umbral_lluvia, line_color="#ff4444", line_dash="dash",
                  annotation_text=f"Alerta ({umbral_lluvia:.0f} mm)")
    fig.update_layout(**PLOTLY_LAYOUT, title="Lluvia diaria (mm)", yaxis_title="mm", height=320)
    st.plotly_chart(fig, use_container_width=True)

with col_hum:
    st.markdown("### Humedad relativa")
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df_daily["fecha"], y=df_daily["humedad_max"],
        name="Max", line=dict(color="#a8e6cf", width=1.5)))
    fig.add_trace(go.Scatter(x=df_daily["fecha"], y=df_daily["humedad_avg"],
        name="Promedio", line=dict(color="#2dce89", width=2),
        fill="tonexty", fillcolor="rgba(168,230,207,0.15)"))
    fig.add_hline(y=80, line_color="#ffb347", line_dash="dash",
                  annotation_text="Riesgo fungico (80%)")
    fig.update_layout(**PLOTLY_LAYOUT, title="Humedad (%) — riesgo enfermedades",
                      yaxis_title="%", yaxis_range=[0, 105], height=320)
    st.plotly_chart(fig, use_container_width=True)


# ── Radiacion solar ───────────────────────────────────────────────────────────
st.markdown("### Radiacion solar")
fig = go.Figure()
fig.add_trace(go.Bar(x=df_daily["fecha"], y=df_daily["rad_max"],
    name="Max diaria", marker_color="#ffb347", opacity=0.7))
fig.add_trace(go.Scatter(x=df_daily["fecha"], y=df_daily["rad_avg"],
    name="Promedio", line=dict(color="#ffd93d", width=2), mode="lines+markers", marker_size=5))
fig.update_layout(**PLOTLY_LAYOUT, title="Radiacion solar (w/m²)", yaxis_title="w/m²", height=300)
st.plotly_chart(fig, use_container_width=True)


# ── Horas frio + GDD ──────────────────────────────────────────────────────────
st.markdown("### Horas de frio y Grados-Dia Acumulados (GDD)")
st.caption("**Horas de frio** = horas con T < 7°C · **GDD** = base 7°C (relevante en temporada activa)")

fig = make_subplots(specs=[[{"secondary_y": True}]])
fig.add_trace(go.Bar(x=df_daily["fecha"], y=df_daily["horas_frio"],
    name="Horas frio/dia", marker_color=COLORS["horas_frio"], opacity=0.8), secondary_y=False)
fig.add_trace(go.Scatter(x=df_daily["fecha"], y=df_daily["horas_frio_acum"],
    name="H. frio acum.", line=dict(color="#228be6", width=2.5)), secondary_y=True)
fig.add_trace(go.Scatter(x=df_daily["fecha"], y=df_daily["gdd_acum"],
    name="GDD acum.", line=dict(color=COLORS["gdd"], width=2.5, dash="dot")), secondary_y=True)
fig.update_layout(**PLOTLY_LAYOUT, title="Horas de frio y GDD", height=320)
fig.update_yaxes(title_text="Horas/dia", secondary_y=False, gridcolor="#2a2a2a")
fig.update_yaxes(title_text="Acumulado",  secondary_y=True,  gridcolor="rgba(0,0,0,0)")
st.plotly_chart(fig, use_container_width=True)


# ── Viento ────────────────────────────────────────────────────────────────────
col_vv, col_dir = st.columns([2, 1])

with col_vv:
    st.markdown("### Velocidad de viento")
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df_daily["fecha"], y=df_daily["rafaga_max"],
        name="Rafaga max", line=dict(color=COLORS["viento"], width=1.5)))
    fig.add_trace(go.Scatter(x=df_daily["fecha"], y=df_daily["viento_avg"],
        name="Viento prom.", line=dict(color="#845ef7", width=2.5),
        fill="tonexty", fillcolor="rgba(195,166,255,0.15)"))
    fig.update_layout(**PLOTLY_LAYOUT, title="Velocidad viento (km/h)", yaxis_title="km/h", height=300)
    st.plotly_chart(fig, use_container_width=True)

with col_dir:
    st.markdown("### Rosa de vientos")
    if not df_raw.empty:
        df_wind = df_raw[df_raw["vel_viento_kmh"] > 0.5].dropna(subset=["dir_viento_grados", "vel_viento_kmh"])
        if not df_wind.empty:
            puntos = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSO","SO","OSO","O","ONO","NO","NNO"]
            df_wind = df_wind.copy()
            df_wind["punto"] = pd.cut(df_wind["dir_viento_grados"] % 360,
                bins=[i * 22.5 for i in range(17)], labels=puntos, include_lowest=True)
            conteo = df_wind.groupby("punto", observed=True)["vel_viento_kmh"].mean().reindex(puntos, fill_value=0)
            fig_wind = go.Figure(go.Barpolar(r=conteo.values, theta=puntos,
                marker_color=px.colors.sequential.Plasma[:len(puntos)]))
            fig_wind.update_layout(paper_bgcolor="#111", plot_bgcolor="#1a1a1a",
                font=dict(color="#ccc"), polar=dict(bgcolor="#1a1a1a",
                radialaxis=dict(visible=True, color="#555")),
                showlegend=False, margin=dict(l=20,r=20,t=40,b=10),
                height=300, title="Vel. media por direccion (km/h)")
            st.plotly_chart(fig_wind, use_container_width=True)
        else:
            st.info("Sin datos de viento.")
    else:
        st.info("Rosa de vientos disponible para rangos ≤ 60 dias.")


# ── Presion ───────────────────────────────────────────────────────────────────
if not df_raw.empty:
    st.markdown("### Presion atmosferica")
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df_raw["timestamp"], y=df_raw["presion_hpa"],
        line=dict(color="#f06595", width=1.5), name="Presion", mode="lines"))
    fig.update_layout(**PLOTLY_LAYOUT, title="Presion atmosferica (hPa)", yaxis_title="hPa", height=280)
    st.plotly_chart(fig, use_container_width=True)


# ── Tabla diaria ──────────────────────────────────────────────────────────────
st.divider()
st.markdown("### Tabla resumen diario")

show_cols = {
    "fecha": "Fecha", "temp_min": "T min (°C)", "temp_max": "T max (°C)",
    "temp_avg": "T prom (°C)", "lluvia_total": "Lluvia (mm)",
    "humedad_avg": "Humedad (%)", "rad_max": "Rad max (w/m²)",
    "viento_avg": "Viento (km/h)", "horas_frio": "H. frio", "gdd": "GDD",
}
df_show = df_daily[[c for c in show_cols if c in df_daily.columns]].copy()
df_show.columns = [show_cols[c] for c in df_show.columns]
df_show["Fecha"] = df_show["Fecha"].dt.strftime("%d/%m/%Y")
numeric_cols = [c for c in df_show.columns if c != "Fecha"]

st.dataframe(
    df_show.style.format({c: "{:.1f}" for c in numeric_cols})
           .background_gradient(subset=["T min (°C)"], cmap="Blues_r")
           .background_gradient(subset=["T max (°C)"], cmap="Reds")
           .background_gradient(subset=["Lluvia (mm)"], cmap="Blues")
           .background_gradient(subset=["H. frio"],    cmap="PuBu"),
    use_container_width=True,
    height=min(400, 40 + 36 * len(df_show)),
)

csv = df_show.to_csv(index=False).encode("utf-8")
st.download_button("Descargar CSV", data=csv,
    file_name=f"finca_leon_rouges_{d_from}_{d_to}.csv", mime="text/csv")


# ── Footer ────────────────────────────────────────────────────────────────────
st.divider()
st.caption(
    f"Datos: Estacion Pegasus · Finca Leon Rouges, Tucuman  |  "
    f"Registros cada 15 min  |  Actualizacion automatica cada 15 min via GitHub Actions"
)
