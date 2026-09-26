-- v2 of facility_performance.
-- 1. Weather is matched to the observation *nearest in time* within +/-30 minutes,
--    instead of the latest one in the previous hour. Hourly backfill values are
--    stored at the middle of their averaging hour, so this pairs each 5-minute
--    reading with the irradiance that actually applied, and stops a sunny
--    afternoon average being used at dusk.
-- 2. Only score when irradiance >= 200 W/m2 (as IEC 61724-style performance-ratio
--    filters do). At low sun, inverter start-up, tracker stow and shading make the
--    ratio meaningless and produce false "underperforming" alerts.
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
           WHEN w.ghi_wm2 IS NULL OR w.ghi_wm2 < 200
             OR f.capacity_mw * w.ghi_wm2 / 1000.0 * 0.8 < f.capacity_mw * 0.05 THEN NULL
           ELSE GREATEST(f.actual_mw, 0)
                / LEAST(f.capacity_mw, f.capacity_mw * w.ghi_wm2 / 1000.0 * 0.8)
       END AS performance_index
FROM facility_output f
LEFT JOIN LATERAL (
    SELECT ghi_wm2
    FROM weather_obs w
    WHERE w.facility_code = f.facility_code
      AND w.observed_at BETWEEN f.interval_end - interval '30 minutes'
                            AND f.interval_end + interval '30 minutes'
    ORDER BY abs(extract(epoch FROM w.observed_at - f.interval_end))
    LIMIT 1
) w ON true;
