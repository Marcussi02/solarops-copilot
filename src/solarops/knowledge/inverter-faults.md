# Inverter faults and trips

## Why inverters matter most

Central and string inverters convert DC from the arrays into AC for the grid, and they are the most common cause of lost energy on a solar farm. A farm is usually built from many inverter blocks, so losing one block removes a fixed share of output. A sudden step down to a steady fraction of normal output, such as losing one of eight blocks, points to an inverter or its medium-voltage transformer rather than weather.

## Grid voltage and frequency trips

Inverters must disconnect when grid voltage or frequency moves outside their protection settings, and reconnect only after conditions have been stable for a set time. Trips affecting a whole farm at once, or several farms in the same area, usually follow a grid disturbance rather than an equipment defect. Check the fault log for over-voltage, under-voltage, over-frequency or anti-islanding events and whether the inverter reconnected automatically.

## Insulation resistance and ground faults

Before starting each morning, inverters measure the insulation resistance of the DC array to earth. Moisture from dew or rain, damaged cable insulation, water in connectors or junction boxes, or a cracked module backsheet can cause an isolation or ground-fault alarm. The inverter then refuses to start, which shows as a block missing from the morning ramp. If the fault clears as the array dries, record the recurrence and schedule insulation testing of the affected strings.

## Over-temperature and derating

Inverters reduce output when internal temperatures get too high, which is called derating. Blocked air filters, failed cooling fans, high ambient temperatures and poor shelter ventilation are common causes. Derating shows as output flattening during the hottest part of a clear day while irradiance stays high. Cleaning filters and checking fans should come before replacing power electronics.

## Communication loss versus zero output

A missing SCADA value is not the same as zero generation. If data stops arriving, first check the plant controller, data logger and communications path. Output reported as exactly zero during daylight with other farms nearby generating normally means the farm is really not exporting, and points to a trip at the inverters, the substation or the connection point. Always confirm with the site before dispatching a technician.

## Hardware failures

IGBT power module failures, DC contactor faults, capacitor ageing and control board faults usually need the manufacturer or an authorised service partner. Record the fault code, the time and the inverter serial number, and follow the manufacturer's documented procedure. Isolation and restart of inverters must follow site switching procedures and electrical safety rules.
