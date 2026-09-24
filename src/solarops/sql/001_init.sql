-- Solar farm registry (one row per AEMO generating unit)
CREATE TABLE IF NOT EXISTS solar_units (
    duid           text PRIMARY KEY,
    facility_code  text NOT NULL,
    facility_name  text NOT NULL,
    region         text NOT NULL,
    latitude       double precision NOT NULL,
    longitude      double precision NOT NULL,
    capacity_mw    double precision NOT NULL CHECK (capacity_mw > 0),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS solar_units_facility_idx ON solar_units (facility_code);

-- 5-minute SCADA output. The natural key makes re-ingesting a file a no-op.
CREATE TABLE IF NOT EXISTS scada_readings (
    duid          text NOT NULL REFERENCES solar_units (duid),
    interval_end  timestamptz NOT NULL,
    mw            double precision NOT NULL,
    PRIMARY KEY (duid, interval_end)
);
CREATE INDEX IF NOT EXISTS scada_readings_interval_idx ON scada_readings (interval_end);

-- Weather at each facility (Open-Meteo "current" conditions, 15-minute resolution)
CREATE TABLE IF NOT EXISTS weather_obs (
    facility_code    text NOT NULL,
    observed_at      timestamptz NOT NULL,
    ghi_wm2          double precision,
    temp_c           double precision,
    cloud_cover_pct  double precision,
    PRIMARY KEY (facility_code, observed_at)
);

-- Ledger of processed NEMWeb files: the idempotency record for the ingest pipeline
CREATE TABLE IF NOT EXISTS ingested_files (
    file_name     text PRIMARY KEY,
    interval_end  timestamptz NOT NULL,
    rows_total    integer NOT NULL,
    rows_solar    integer NOT NULL,
    ingested_at   timestamptz NOT NULL DEFAULT now()
);

-- Facility-level actual vs weather-expected output (performance ratio 0.8).
-- Weather is matched to the latest observation within the previous hour.
CREATE OR REPLACE VIEW facility_performance AS
WITH facility_output AS (
    SELECT u.facility_code,
           max(u.facility_name) AS facility_name,
           max(u.region)        AS region,
           r.interval_end,
           sum(r.mw)            AS actual_mw,
           sum(u.capacity_mw)   AS capacity_mw
    FROM scada_readings r
    JOIN solar_units u USING (duid)
    GROUP BY u.facility_code, r.interval_end
)
SELECT f.*,
       w.ghi_wm2,
       LEAST(f.capacity_mw, f.capacity_mw * GREATEST(w.ghi_wm2, 0) / 1000.0 * 0.8) AS expected_mw,
       CASE
           WHEN w.ghi_wm2 IS NULL
             OR f.capacity_mw * w.ghi_wm2 / 1000.0 * 0.8 < f.capacity_mw * 0.05 THEN NULL
           ELSE GREATEST(f.actual_mw, 0)
                / LEAST(f.capacity_mw, f.capacity_mw * w.ghi_wm2 / 1000.0 * 0.8)
       END AS performance_index
FROM facility_output f
LEFT JOIN LATERAL (
    SELECT ghi_wm2
    FROM weather_obs w
    WHERE w.facility_code = f.facility_code
      AND w.observed_at <= f.interval_end
      AND w.observed_at > f.interval_end - interval '1 hour'
    ORDER BY w.observed_at DESC
    LIMIT 1
) w ON true;
