"""Expected-versus-actual solar output.

A simple, explainable model: expected power scales with global horizontal
irradiance (GHI) relative to standard test conditions (1000 W/m²), multiplied by
a performance ratio that absorbs temperature, soiling, inverter and wiring losses.
"""

STC_IRRADIANCE_WM2 = 1000.0
DEFAULT_PERFORMANCE_RATIO = 0.80
# Below this share of capacity the ratio is too noisy to be meaningful (dawn, dusk, night).
MIN_EXPECTED_SHARE = 0.05


def expected_mw(
    capacity_mw: float, ghi_wm2: float | None, performance_ratio: float = DEFAULT_PERFORMANCE_RATIO
) -> float | None:
    if ghi_wm2 is None:
        return None
    raw = capacity_mw * max(ghi_wm2, 0.0) / STC_IRRADIANCE_WM2 * performance_ratio
    return min(raw, capacity_mw)


def performance_index(actual_mw: float, expected: float | None, capacity_mw: float) -> float | None:
    """actual / expected, or None when expected output is too small to judge."""
    if expected is None or expected < capacity_mw * MIN_EXPECTED_SHARE:
        return None
    return max(actual_mw, 0.0) / expected
