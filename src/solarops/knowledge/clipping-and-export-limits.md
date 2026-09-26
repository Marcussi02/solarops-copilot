# Inverter clipping and export limits

## DC to AC ratio

Solar farms are usually built with more DC module capacity than AC inverter capacity, a design choice expressed as the DC to AC ratio or inverter loading ratio. Ratios of around 1.2 to 1.4 are common. Oversizing the DC side increases energy in the mornings, afternoons and cloudy periods, at the cost of losing some energy in the middle of clear days.

## What clipping looks like

When the arrays could produce more than the inverters can convert, output is held at the inverter AC limit. This is called clipping, and it shows as a flat top on the output curve around midday on clear days. Clipping is expected behaviour and not a fault, although unusually long clipping can indicate a mismatch between the registered capacity and the real AC limit.

## Connection point and export limits

A farm's connection agreement sets the maximum power it may export, and the plant controller enforces it at the point of connection. When the export limit is below the combined inverter capacity, output flattens at that limit even with inverters available. Reactive power requirements can also reduce the active power available at full apparent power.

## Telling clipping apart from curtailment

Clipping and export limiting produce a flat top at the same level every clear day, close to the farm's AC or export capacity. Curtailment produces a ceiling that changes from interval to interval and can sit far below capacity, tracking dispatch targets or prices. A flat top well below usual peak output on a clear day is more likely curtailment or an inverter block out of service than clipping.

## Effect on the performance index

Because the SolarOps expected output is capped at registered capacity, clipping near capacity has little effect on the index. If the registered capacity is higher than the real export limit, the farm will appear to underperform on bright days. Comparing peak output with registered capacity over several clear days reveals this.
