"""Regulatory-reference unit tests for the PM2.5 domain math.

Verifies ``app.analysis`` against published regulatory and peer-reviewed
references rather than against current code output. No production function is
modified by this suite.

Reference standards exercised here
----------------------------------
* **Barkjohn, Gantt & Clements (2021)**, *Atmospheric Measurement Techniques*
  14, 4617-4637, **Eq. 10** — the US-wide PurpleAir correction adopted by EPA
  for the AirNow Fire and Smoke Map:

      PM2.5_corrected = 0.524 x PA_cf1 - 0.0862 x RH + 5.75

* **EPA AQI Technical Assistance Document** (EPA-454/B-24-002) — PM2.5
  breakpoint table and the requirement that concentrations be **truncated** to
  one decimal place before the piecewise-linear index is applied.

Units
-----
``PA_cf1`` and all corrected concentrations are **µg/m³**.
``RH`` is **percent relative humidity (%)**, 0-100.
``AQI`` is dimensionless, 0-500.
"""

import math

import numpy as np
import pandas as pd
import pytest

from app.analysis import (
    AQI_BREAKPOINTS,
    BARKJOHN_INTERCEPT,
    BARKJOHN_PM_COEF,
    BARKJOHN_RH_COEF,
    apply_aqu_correction,
    apply_epa_correction,
    apply_lrapa_correction,
    barkjohn_corrected,
    calc_aqi_value,
)


# ── Barkjohn / EPA correction coefficients ───────────────────────────────────


def test_barkjohn_coefficients_match_published_equation_10():
    """Coefficients must equal Barkjohn et al. (2021) Eq. 10 exactly.

    Guards against silent refitting or transcription drift. These are
    regulatory-adopted constants, not tunable parameters.
    """
    assert BARKJOHN_PM_COEF == 0.524
    assert BARKJOHN_RH_COEF == 0.0862
    assert BARKJOHN_INTERCEPT == 5.75


def test_barkjohn_scalar_hand_computed():
    """Hand-computed reference point.

    PA_cf1 = 20.0 µg/m³, RH = 50 %:
        0.524*20.0 - 0.0862*50 + 5.75
      = 10.48 - 4.31 + 5.75
      = 11.92 µg/m³
    """
    assert barkjohn_corrected(20.0, 50.0) == pytest.approx(11.92, abs=1e-9)


def test_barkjohn_second_hand_computed_point():
    """Second independent reference point, high concentration.

    PA_cf1 = 100.0 µg/m³, RH = 35 %:
        0.524*100.0 - 0.0862*35 + 5.75
      = 52.40 - 3.017 + 5.75
      = 55.133 µg/m³
    """
    assert barkjohn_corrected(100.0, 35.0) == pytest.approx(55.133, abs=1e-9)


def test_correction_reduces_purpleair_overreading():
    """PurpleAir cf1 over-reads ambient PM2.5; the correction must pull it down.

    At 100 µg/m³ and typical 50 % RH the corrected value must be materially
    lower than the raw reading — the entire purpose of the adjustment.
    """
    raw = 100.0
    corrected = barkjohn_corrected(raw, 50.0)
    assert corrected < raw
    assert corrected == pytest.approx(0.524 * raw - 0.0862 * 50.0 + 5.75, abs=1e-9)


def test_series_correction_matches_scalar_path():
    """The vectorised path must agree with the scalar worked-example path.

    The scalar form is what gets shown to users as a worked example; if the two
    diverge, the report explains one number and plots another.
    """
    pm = pd.Series([5.0, 20.0, 55.0, 120.0])
    rh = pd.Series([30.0, 50.0, 65.0, 80.0])
    out = apply_epa_correction(pm, rh)
    for i in range(len(pm)):
        expected = max(0.0, barkjohn_corrected(pm.iloc[i], rh.iloc[i]))
        assert out.iloc[i] == pytest.approx(expected, abs=1e-9)


def test_negative_concentrations_are_clipped_to_zero():
    """A negative mass concentration is unphysical and must be clipped at 0.

    On cool, clean, humid readings the -0.0862*RH term can drive the raw result
    below zero. Hand check: PA_cf1 = 0, RH = 95 % gives
        0.524*0 - 0.0862*95 + 5.75 = -2.439 µg/m³ -> clipped to 0.0
    """
    raw = barkjohn_corrected(0.0, 95.0)
    assert raw < 0
    out = apply_epa_correction(pd.Series([0.0]), pd.Series([95.0]))
    assert out.iloc[0] == pytest.approx(0.0, abs=1e-12)
    assert (out >= 0).all()


def test_correction_propagates_nan_for_missing_humidity():
    """Missing RH must yield NaN, never a silently uncorrected value.

    Substituting the raw reading when RH is absent would mix corrected and
    uncorrected values in one series without disclosure.
    """
    out = apply_epa_correction(pd.Series([20.0]), pd.Series([np.nan]))
    assert bool(pd.isna(out.iloc[0]))


def test_alternate_corrections_match_published_forms():
    """LRAPA (0.5*PM - 0.66) and AQ&U (0.778*PM + 2.65), both clipped at 0."""
    assert apply_lrapa_correction(pd.Series([20.0])).iloc[0] == pytest.approx(9.34, abs=1e-9)
    assert apply_aqu_correction(pd.Series([20.0])).iloc[0] == pytest.approx(18.21, abs=1e-9)
    # LRAPA goes negative below 1.32 µg/m³ and must clip.
    assert apply_lrapa_correction(pd.Series([0.0])).iloc[0] == pytest.approx(0.0, abs=1e-12)


# ── EPA AQI breakpoints ──────────────────────────────────────────────────────


def test_aqi_breakpoint_table_matches_epa_pm25_table():
    """The breakpoint table must match the published EPA PM2.5 rows.

    Values are the 2024-revised PM2.5 breakpoints (µg/m³ -> AQI).
    """
    expected = [
        (0.0, 9.0, 0, 50),
        (9.1, 35.4, 51, 100),
        (35.5, 55.4, 101, 150),
        (55.5, 125.4, 151, 200),
        (125.5, 225.4, 201, 300),
        (225.5, 325.4, 301, 500),
    ]
    actual = [(lo, hi, alo, ahi) for lo, hi, alo, ahi, _label, _color in AQI_BREAKPOINTS]
    assert actual == expected


def test_aqi_breakpoint_edges_are_exact():
    """Category boundaries must return the published AQI endpoint values."""
    assert calc_aqi_value(0.0)[0] == 0
    assert calc_aqi_value(9.0)[0] == 50
    assert calc_aqi_value(9.1)[0] == 51
    assert calc_aqi_value(35.4)[0] == 100
    assert calc_aqi_value(35.5)[0] == 101
    assert calc_aqi_value(55.4)[0] == 150
    assert calc_aqi_value(55.5)[0] == 151
    assert calc_aqi_value(125.5)[0] == 201
    assert calc_aqi_value(225.5)[0] == 301


def test_aqi_categories_are_labelled_correctly():
    """Category labels must track the concentration, not just the index."""
    assert calc_aqi_value(5.0)[1] == "Good"
    assert calc_aqi_value(20.0)[1] == "Moderate"
    assert calc_aqi_value(40.0)[1] == "USG"
    assert calc_aqi_value(60.0)[1] == "Unhealthy"
    assert calc_aqi_value(150.0)[1] == "Very Unhealthy"
    assert calc_aqi_value(250.0)[1] == "Hazardous"


def test_aqi_uses_truncation_not_rounding():
    """EPA requires TRUNCATION to one decimal before applying breakpoints.

    9.05 µg/m³ truncates to 9.0 -> AQI 50 ("Good"). Rounding to 9.1 would give
    AQI 51 ("Moderate") and flip the reported category. Previously such
    between-grid values matched no bin and returned AQI 0 / "Unknown", which
    dragged downstream AQI averages downward.
    """
    aqi, label, _ = calc_aqi_value(9.05)
    assert aqi == 50
    assert label == "Good"


def test_aqi_truncation_is_robust_to_binary_float_error():
    """35.5 must not be truncated to 35.4 by float representation error.

    35.5 sits exactly on a category edge; a naive floor(x*10)/10 on the binary
    representation can yield 35.4 and misreport the category.
    """
    assert calc_aqi_value(35.5)[0] == 101
    assert calc_aqi_value(35.5)[1] == "USG"


def test_aqi_is_monotonic_non_decreasing():
    """AQI must never decrease as concentration rises, across all breakpoints."""
    grid = [round(x * 0.1, 1) for x in range(0, 3400)]
    values = [calc_aqi_value(c)[0] for c in grid]
    assert all(b >= a for a, b in zip(values, values[1:]))


def test_aqi_is_capped_at_500_beyond_the_index():
    """Above 325.4 µg/m³ EPA does not define the index; cap at 500.

    Returning "Unknown"/0 here would understate an extreme smoke episode.
    """
    assert calc_aqi_value(500.0)[0] == 500
    assert calc_aqi_value(5000.0)[0] == 500


def test_aqi_rejects_invalid_and_negative_input():
    """Missing or physically impossible values must return Unknown, not 0-as-Good."""
    assert calc_aqi_value(None)[1] == "Unknown"
    assert calc_aqi_value(float("nan"))[1] == "Unknown"
    assert calc_aqi_value(-5.0)[1] == "Unknown"


def test_aqi_linear_interpolation_within_a_category():
    """Interpolation must follow the EPA piecewise-linear formula exactly.

    Hand check at 22.2 µg/m³ in the Moderate bin (9.1-35.4 -> 51-100):
        ((100-51)/(35.4-9.1)) * (22.2-9.1) + 51
      = (49/26.3)*13.1 + 51 = 24.408... + 51 = 75.408... -> round -> 75
    """
    expected = round((49.0 / 26.3) * (22.2 - 9.1) + 51)
    assert calc_aqi_value(22.2)[0] == expected == 75


# ── WHO / EPA guideline anchors ──────────────────────────────────────────────


def test_who_and_epa_annual_guidelines_land_in_expected_categories():
    """Sanity-anchor the index against the two headline guideline values.

    WHO 2021 annual guideline is 5 µg/m³; the EPA 2024 annual primary NAAQS is
    9 µg/m³ — both must fall inside the "Good" band, with 9.0 at its top edge.
    """
    assert calc_aqi_value(5.0)[1] == "Good"
    assert calc_aqi_value(9.0)[0] == 50
    # The EPA 24-hour PM2.5 NAAQS of 35 µg/m³ sits just inside Moderate.
    assert calc_aqi_value(35.0)[1] == "Moderate"
    assert calc_aqi_value(35.5)[1] == "USG"
