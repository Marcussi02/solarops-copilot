-- Curtailment-aware scoring (performance view v3).
-- A farm producing well below what the sun allows is not necessarily faulty: AEMO
-- may have dispatched it down (semi-dispatch cap, network constraint) or it may be
-- self-curtailing at negative prices. Two sources, see solarops/dispatch.py:
--   region_prices  every 5 minutes (DispatchIS)        -> "likely_curtailed", live
--   unit_dispatch  once a day (Next_Day_Dispatch)      -> "curtailed" with MW lost
CREATE TABLE IF NOT EXISTS region_prices (
    region        text NOT NULL,
    interval_end  timestamptz NOT NULL,
    rrp           double precision NOT NULL,
    PRIMARY KEY (region, interval_end)
);

CREATE TABLE IF NOT EXISTS unit_dispatch (
    duid              text NOT NULL REFERENCES solar_units (duid),
    interval_end      timestamptz NOT NULL,
    total_cleared     double precision NOT NULL,  -- dispatch target, MW
    availability      double precision,           -- offered availability, MW
    uigf              double precision,           -- unconstrained forecast, MW
    semidispatch_cap  boolean NOT NULL,           -- target was binding
    PRIMARY KEY (duid, interval_end)
);
CREATE INDEX IF NOT EXISTS unit_dispatch_interval_idx ON unit_dispatch (interval_end);

ALTER TABLE region_prices ENABLE ROW LEVEL SECURITY;
ALTER TABLE unit_dispatch ENABLE ROW LEVEL SECURITY;

-- MW the market or network held back a facility at one interval: unconstrained
-- forecast minus dispatch target while a semi-dispatch cap was binding.
-- NULL means no dispatch data for that interval yet (it arrives the next day).
-- A function, so the view only runs it for rows that need it (see below).
CREATE OR REPLACE FUNCTION facility_curtailed_mw(code text, at timestamptz)
RETURNS double precision
LANGUAGE sql STABLE AS $$
    SELECT sum(CASE WHEN d.semidispatch_cap
                    THEN GREATEST(coalesce(d.uigf, d.availability, 0) - d.total_cleared, 0)
                    ELSE 0 END)
    FROM solar_units u
    JOIN unit_dispatch d ON d.duid = u.duid AND d.interval_end = at
    WHERE u.facility_code = code
$$;

-- v3 keeps every v2 column (in order) and appends rrp, curtailed_mw and status.
-- Curtailment is looked up lazily: queries that never select curtailed_mw or status
-- skip it, and status only looks it up for intervals scoring below 0.6. Measured on
-- 600k readings, this keeps the latest-interval lookups at v2 speed.
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
),
scored AS (
    SELECT f.*,
           w.ghi_wm2,
           LEAST(f.capacity_mw, f.capacity_mw * GREATEST(w.ghi_wm2, 0) / 1000.0 * 0.8)
               AS expected_mw,
           CASE
               WHEN w.ghi_wm2 IS NULL OR w.ghi_wm2 < 200
                 OR f.capacity_mw * w.ghi_wm2 / 1000.0 * 0.8 < f.capacity_mw * 0.05 THEN NULL
               ELSE GREATEST(f.actual_mw, 0)
                    / LEAST(f.capacity_mw, f.capacity_mw * w.ghi_wm2 / 1000.0 * 0.8)
           END AS performance_index,
           p.rrp
    FROM facility_output f
    LEFT JOIN LATERAL (
        SELECT ghi_wm2
        FROM weather_obs w
        WHERE w.facility_code = f.facility_code
          AND w.observed_at BETWEEN f.interval_end - interval '30 minutes'
                                AND f.interval_end + interval '30 minutes'
        ORDER BY abs(extract(epoch FROM w.observed_at - f.interval_end))
        LIMIT 1
    ) w ON true
    LEFT JOIN region_prices p ON p.region = f.region AND p.interval_end = f.interval_end
)
SELECT facility_code, facility_name, region, interval_end, actual_mw, capacity_mw,
       ghi_wm2, expected_mw, performance_index, rrp,
       facility_curtailed_mw(facility_code, interval_end) AS curtailed_mw,
       CASE
           WHEN performance_index IS NULL THEN NULL
           WHEN performance_index >= 0.6 THEN 'ok'
           ELSE (
               SELECT CASE
                          -- Ground truth: held back by more than 5% of capacity while capped.
                          WHEN c.mw > capacity_mw * 0.05 THEN 'curtailed'
                          -- Dispatch data shows no binding cap: a real shortfall.
                          WHEN c.mw IS NOT NULL THEN 'underperforming'
                          -- No dispatch data yet: negative prices suggest curtailment.
                          WHEN rrp < 0 THEN 'likely_curtailed'
                          ELSE 'underperforming'
                      END
               FROM (SELECT facility_curtailed_mw(facility_code, interval_end) AS mw) c
           )
       END AS status
FROM scored;
