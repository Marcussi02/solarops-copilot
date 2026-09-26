# Irradiance and weather

## GHI and plane-of-array irradiance

Global horizontal irradiance (GHI) is the total solar power per square metre arriving on a flat horizontal surface, measured in W/m². Plane-of-array (POA) irradiance is what arrives on the tilted or tracking module surface, and is what the panels actually convert. On clear days trackers see considerably more POA than GHI early and late in the day. Site pyranometers usually measure both, while weather models usually provide GHI.

## Modelled weather from Open-Meteo

SolarOps uses Open-Meteo, a free weather service, for shortwave radiation, air temperature and cloud cover at each farm's coordinates. These are model and satellite-based estimates for a grid cell, not measurements from the site's own sensors. They are good at capturing clear days and widespread overcast, but cannot resolve an individual cloud passing over one farm.

## Clouds and variability

Broken cloud makes output swing sharply from one 5-minute interval to the next, while hourly irradiance values average those swings out. Short dips below expectation under passing cloud are therefore normal and do not indicate a fault. Several neighbouring farms dropping together is strong evidence of weather rather than equipment.

## Temperature effects

Module efficiency falls as cell temperature rises, typically by around 0.3 to 0.4% per degree Celsius above 25 °C for crystalline silicon. On hot summer days cells can run 20 to 30 degrees hotter than the air, so clear hot days often score slightly lower than clear cool days. The fixed performance ratio in the SolarOps model does not adjust for temperature.

## Low sun, dawn and dusk

Near sunrise and sunset irradiance is low, inverters are starting up or shutting down, and tracker backtracking and row shading dominate. Ratios of actual to expected output are unreliable in these conditions. This is why SolarOps only scores intervals with at least 200 W/m² of irradiance.
