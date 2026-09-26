# Triage runbook for an underperformance alert

## Step 1: check the data first

Confirm the alert is based on fresh data by checking the data lag on the health endpoint and the time of the flagged interval. Check whether readings are missing rather than low, since gaps come from communications or ingestion problems, not from the farm. Check that a weather observation exists close to the interval, because a stale or missing irradiance value leads to a wrong expectation.

## Step 2: rule out weather

Look at neighbouring farms in the same region at the same interval. If most of them are also below expectation, the cause is probably cloud, smoke, dust or an error in the modelled irradiance, and the alert can usually be closed with a note. A single farm low while its neighbours are normal deserves investigation.

## Step 3: rule out curtailment and constraints

Check the regional price and the farm's dispatch target for the interval. Negative prices or a binding network constraint explain a flat ceiling below capacity and are not equipment faults. Record the curtailed energy separately so it does not distort availability reporting.

## Step 4: read the shape of the output

A clean step down to a stable fraction of normal output suggests one or more inverter blocks or a medium-voltage transformer out of service. Exactly zero output in daylight suggests a trip at the substation or connection point, or a grid disturbance. Output that is low in the morning and recovers as the day warms up points to insulation resistance faults from dew. A slow decline over weeks points to soiling or degradation, and part-day losses point to trackers or shading.

## Step 5: escalate with evidence

Contact the site or control room with the farm name, the intervals affected, the actual and expected MW, and the checks already completed. Ask for inverter, tracker and protection fault logs for the same time. Create a work order only once weather and curtailment have been ruled out, and close the loop by recording the root cause so repeated alerts can be recognised.
