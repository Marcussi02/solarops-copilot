# NEM market data used by SolarOps

## Dispatch intervals and NEM time

The National Electricity Market runs on 5-minute dispatch intervals, and energy has been settled on the same 5-minute basis since October 2021. AEMO timestamps each interval by its end time in NEM time, which is Australian Eastern Standard Time, fixed at UTC+10 with no daylight saving. SolarOps stores all timestamps in UTC and converts to NEM time only when writing answers.

## Regions

The NEM is divided into five price regions: NSW1 for New South Wales including the ACT, QLD1 for Queensland, VIC1 for Victoria, SA1 for South Australia and TAS1 for Tasmania. Western Australia and the Northern Territory are not part of the NEM. Almost all utility-scale solar capacity is in NSW, Queensland, Victoria and South Australia.

## DUIDs and facilities

Every generating unit registered in the NEM has a dispatchable unit identifier, called a DUID. A single solar farm can have one or several DUIDs, for example separate stages of the same site. SolarOps groups DUIDs into facilities using the Open Electricity registry, and reports output per facility by summing its units.

## SCADA readings

The DISPATCH_UNIT_SCADA files on AEMO's NEMWeb publish the SCADA value of each unit in MW at the end of every 5-minute interval. The value is an instantaneous snapshot of output as measured at the connection, not an energy total. SolarOps estimates energy in MWh by multiplying each 5-minute reading by one twelfth of an hour. SCADA values can occasionally be slightly negative at night because of auxiliary loads, and missing values mean no data, not zero output.

## Marginal loss factors

Marginal loss factors scale a generator's output to reflect transmission losses between its connection point and the regional reference node. They change a farm's revenue each financial year but do not change the measured MW that SolarOps analyses. Remote farms at the end of long lines often have lower loss factors.

## Data freshness

A new SCADA file is published shortly after each interval ends. SolarOps polls NEMWeb every 5 minutes and reports how many minutes old the latest ingested interval is through the health endpoint. A lag well above 15 minutes suggests a stalled ingest queue, an AEMO publishing delay or a failing worker, and should be checked against the dead-letter queue alarm.
