from solarops.registry import parse_facilities


def unit(code, fueltech="solar_utility", status="operating", capacity=100.0):
    return {
        "code": code,
        "fueltech_id": fueltech,
        "status_id": status,
        "capacity_registered": capacity,
    }


PAYLOAD = {
    "data": [
        {
            "code": "BIGSF",
            "name": "Big Solar Farm",
            "network_id": "NEM",
            "network_region": "NSW1",
            "location": {"lat": -33.1, "lng": 147.2},
            "units": [
                unit("BIGSF1"),
                unit("BIGBA1", fueltech="battery_discharging"),
                unit("BIGSF2", capacity=0.2),  # auxiliary, below threshold
                unit("BIGSF3", status="committed"),  # not yet operating
            ],
        },
        {
            "code": "WASF",
            "name": "Western Solar",
            "network_id": "WEM",  # not in NEMWeb
            "network_region": "WEM",
            "location": {"lat": -31.0, "lng": 116.0},
            "units": [unit("WASF1")],
        },
        {
            "code": "NOLOC",
            "name": "No Location Solar",
            "network_id": "NEM",
            "network_region": "QLD1",
            "location": None,
            "units": [unit("NOLOC1")],
        },
    ]
}


def test_keeps_only_operating_nem_solar_with_location():
    units = parse_facilities(PAYLOAD)
    assert [u.duid for u in units] == ["BIGSF1"]
    u = units[0]
    assert (u.facility_code, u.region, u.capacity_mw) == ("BIGSF", "NSW1", 100.0)
    assert (u.latitude, u.longitude) == (-33.1, 147.2)


def test_capacity_threshold_is_configurable():
    duids = {u.duid for u in parse_facilities(PAYLOAD, min_capacity_mw=0.1)}
    assert duids == {"BIGSF1", "BIGSF2"}
