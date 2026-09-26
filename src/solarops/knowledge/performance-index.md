# Performance index: how SolarOps scores a farm

## What the performance index means

The performance index compares what a solar farm actually exported with what the weather says it should have produced at the same 5-minute interval. It is the ratio actual MW divided by expected MW. An index of 1.0 means the farm is doing exactly what the simple weather model predicts, 0.8 means it is producing 80% of expectation, and values below 0.6 are flagged as underperforming by default. The index is a screening signal for operators, not a contractual performance ratio.

## How expected output is calculated

Expected power scales with global horizontal irradiance (GHI) relative to standard test conditions of 1000 W/m². The formula is expected MW = capacity MW × GHI / 1000 × 0.8, capped at the registered capacity. The 0.8 is a fixed performance ratio that absorbs typical temperature, soiling, inverter, cabling and transformer losses. Capacity comes from the facility registry and GHI comes from Open-Meteo for the farm's coordinates.

## When a farm is not scored

No index is produced when irradiance is below 200 W/m², when no weather observation lies within 30 minutes of the interval, or when expected output is under 5% of capacity. At dawn, dusk and night the ratio is dominated by inverter start-up thresholds, tracker stow and row shading, and would produce false alarms. This follows the irradiance filtering used in IEC 61724-style performance-ratio calculations. Farms with no score appear with a null index rather than a zero.

## Matching weather to each interval

Each 5-minute SCADA reading is paired with the weather observation nearest in time, within plus or minus 30 minutes. Current conditions are polled every 15 minutes, and missed periods are backfilled from hourly history stored at the middle of its averaging hour. Nearest-in-time matching stops a sunny afternoon average being applied at dusk, which previously caused false underperformance alerts.

## Known limitations of the model

The model uses horizontal irradiance, so single-axis tracking farms can look better than 1.0 in the morning and afternoon, when their panels see more plane-of-array irradiance than GHI suggests. Modelled irradiance cannot see a single cloud over one farm, so short dips under passing cloud are expected. The model does not know about economic curtailment, network constraints, inverter clipping or export limits, so a low index is a prompt to investigate, not proof of a fault. See the curtailment and alarm triage guides before raising a work order.
