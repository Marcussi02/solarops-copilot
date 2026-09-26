# Curtailment and dispatch in the NEM

## Semi-scheduled solar farms

Most utility-scale solar farms in the National Electricity Market are classified as semi-scheduled generators. They bid their output into the market and receive a dispatch target every 5 minutes. When a farm is not constrained, it may generate up to its available output, which AEMO forecasts through the unconstrained intermittent generation forecast (UIGF). When a semi-dispatch cap applies, the farm must not exceed its dispatch target for that interval.

## Economic curtailment at negative prices

Solar farms typically offer energy at low or negative prices because their fuel is free, but many are not willing to generate at any price. When the regional price falls below the price at which a farm has offered its output, the farm is dispatched down and curtails itself. This is common around midday in South Australia and Victoria, when rooftop and utility solar exceed demand and prices go negative. Economic curtailment is a commercial decision, not an equipment fault.

## Network constraints and runbacks

AEMO uses constraint equations to keep power flows within the limits of transmission lines, transformers and system strength. When a constraint binds, affected generators receive lower dispatch targets regardless of price. Planned or forced network outages, voltage limits and system strength requirements in weak parts of the grid are common causes. Some connections also have runback schemes that automatically reduce a farm's output when a nearby element trips.

## How curtailment looks in the data

Curtailed output usually appears as a flat or stepped ceiling well below capacity on a clear day, often starting and ending on 5-minute interval boundaries. Several farms in the same region curtailing at the same time points to prices or a regional constraint, while one farm alone points to a local constraint, an export limit or a fault. Because the SolarOps performance index only knows the weather, curtailed intervals score low and can be flagged as underperforming. Check dispatch targets and regional prices before treating a low index as a fault.

## Confirming curtailment

Compare SCADA output with the farm's dispatch target and the regional price for the same interval. If output tracks a target that sits below available capacity, the farm was curtailed. If the target is at or above availability and output is still low, curtailment is ruled out and the equipment should be investigated. Recording curtailed energy separately keeps availability and performance reporting honest.
