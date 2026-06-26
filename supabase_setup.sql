-- ============================================================
-- Ejecutar en Supabase > SQL Editor (una sola vez)
-- ============================================================

-- Tabla principal
CREATE TABLE IF NOT EXISTS mediciones (
    id                  BIGSERIAL PRIMARY KEY,
    timestamp           TIMESTAMPTZ NOT NULL,
    estacion            TEXT        NOT NULL,
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
    fecha_descarga      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(timestamp, estacion)
);

CREATE INDEX IF NOT EXISTS idx_ts         ON mediciones(timestamp);
CREATE INDEX IF NOT EXISTS idx_estacion_ts ON mediciones(estacion, timestamp);

-- RLS: lectura publica, escritura solo con service_role key
ALTER TABLE mediciones ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "public_read" ON mediciones;
CREATE POLICY "public_read" ON mediciones FOR SELECT USING (true);

-- ============================================================
-- Funcion: rango de fechas disponibles (para el sidebar del dashboard)
-- ============================================================
CREATE OR REPLACE FUNCTION date_range_mediciones(p_estacion TEXT DEFAULT 'Finca Leon Rouges')
RETURNS TABLE (min_ts TEXT, max_ts TEXT)
LANGUAGE sql STABLE AS $$
    SELECT
        MIN(timestamp)::TEXT,
        MAX(timestamp)::TEXT
    FROM mediciones
    WHERE estacion = p_estacion;
$$;

-- ============================================================
-- Funcion: agregado diario (para los graficos del dashboard)
-- ============================================================
CREATE OR REPLACE FUNCTION daily_stats(
    p_from    TEXT,
    p_to      TEXT,
    p_estacion TEXT DEFAULT 'Finca Leon Rouges'
)
RETURNS TABLE (
    fecha        DATE,
    temp_min     REAL,
    temp_max     REAL,
    temp_avg     REAL,
    humedad_avg  REAL,
    humedad_max  REAL,
    lluvia_total REAL,
    rad_max      REAL,
    rad_avg      REAL,
    viento_avg   REAL,
    rafaga_max   REAL,
    horas_frio   REAL,
    horas_helada REAL,
    presion_avg  REAL
)
LANGUAGE sql STABLE AS $$
    SELECT
        DATE(timestamp)                                                        AS fecha,
        MIN(temperatura_c)::REAL,
        MAX(temperatura_c)::REAL,
        AVG(temperatura_c)::REAL,
        AVG(humedad_pct)::REAL,
        MAX(humedad_pct)::REAL,
        SUM(lluvia_mm)::REAL,
        MAX(radiacion_solar_wm2)::REAL,
        AVG(radiacion_solar_wm2)::REAL,
        AVG(vel_viento_kmh)::REAL,
        MAX(vel_rafaga_kmh)::REAL,
        SUM(CASE WHEN temperatura_c < 7 THEN 0.25 ELSE 0 END)::REAL,
        SUM(CASE WHEN temperatura_c < 0 THEN 0.25 ELSE 0 END)::REAL,
        AVG(presion_hpa)::REAL
    FROM mediciones
    WHERE estacion = p_estacion
      AND timestamp BETWEEN p_from::TIMESTAMPTZ AND p_to::TIMESTAMPTZ
      AND temperatura_c IS NOT NULL
    GROUP BY DATE(timestamp)
    ORDER BY DATE(timestamp);
$$;
