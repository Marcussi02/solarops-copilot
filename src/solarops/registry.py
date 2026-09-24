"""Solar farm registry from the Open Electricity facilities API.

Maps AEMO unit codes (DUIDs) to facility name, region, coordinates and capacity.
"""

from dataclasses import dataclass

from .http import get_json

FACILITIES_URL = "https://api.openelectricity.org.au/v4/facilities/"
SOLAR_FUELTECH = "solar_utility"


@dataclass(frozen=True)
class SolarUnit:
    duid: str
    facility_code: str
    facility_name: str
    region: str
    latitude: float
    longitude: float
    capacity_mw: float


def parse_facilities(payload: dict, min_capacity_mw: float = 1.0) -> list[SolarUnit]:
    """Keep operating utility-scale solar units in the NEM that have a location."""
    units: list[SolarUnit] = []
    for facility in payload.get("data", []):
        if facility.get("network_id") != "NEM":
            continue  # NEMWeb SCADA only covers the NEM (not Western Australia's WEM)
        location = facility.get("location") or {}
        lat, lng = location.get("lat"), location.get("lng")
        if lat is None or lng is None:
            continue
        for unit in facility.get("units", []):
            capacity = unit.get("capacity_registered") or 0
            if (
                unit.get("fueltech_id") == SOLAR_FUELTECH
                and unit.get("status_id") == "operating"
                and capacity >= min_capacity_mw
            ):
                units.append(
                    SolarUnit(
                        duid=unit["code"],
                        facility_code=facility["code"],
                        facility_name=facility["name"],
                        region=facility.get("network_region", ""),
                        latitude=float(lat),
                        longitude=float(lng),
                        capacity_mw=float(capacity),
                    )
                )
    return units


def fetch_solar_units(api_key: str | None, min_capacity_mw: float = 1.0) -> list[SolarUnit]:
    """Requires a free Open Electricity API key (https://platform.openelectricity.org.au)."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    return parse_facilities(get_json(FACILITIES_URL, headers=headers), min_capacity_mw)
