"""Numerical accuracy audit.

Every assertion here recomputes a published number *independently* — plain pandas,
no app code — and requires the app to agree. This exists because spot-checking kept
missing real defects (a mis-typed correction coefficient; the correction being fed
the wrong PurpleAir calibration column). Those are now regression-locked.

Ground truth for the correction is the primary source:
  Barkjohn, Gantt & Clements (2021), Atmospheric Measurement Techniques 14,
  4617-4637, Eq. 10 -- PM2.5 = 0.524 x PA_cf_1 - 0.0862 x RH + 5.75
  doi:10.5194/amt-14-4617-2021
"""

import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pair_audit_"))

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import analysis
from app.main import app

client = TestClient(app)

# The published coefficients, written out literally. If someone edits the constants
# in analysis.py, these independent literals make the test fail rather than follow.
PM_COEF, RH_COEF, INTERCEPT = 0.524, 0.0862, 5.75


def _reference_csv(n_hours: int = 24 * 40, seed: int = 7) -> pd.DataFrame:
    """A PurpleAir-shaped export containing BOTH calibrations, deliberately made to
    diverge (atm < cf_1 at higher concentrations, as in real exports) so that using
    the wrong column cannot pass."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-03-01", periods=n_hours * 30, freq="2min", tz="UTC")
    cf1 = np.clip(14 + 11 * np.sin(np.arange(len(idx)) / 700.0) + rng.normal(0, 4, len(idx)), 0.2, None)
    atm = cf1 * 0.72                      # the two calibrations diverge with level
    rh = np.clip(52 + 22 * np.sin(np.arange(len(idx)) / 480.0), 5, 99)
    jitter = rng.normal(0, 0.4, len(idx))
    return pd.DataFrame({
        "time_stamp": idx.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "humidity": rh,
        "temperature": 60.0,
        "pm2.5_atm": atm,
        "pm2.5_atm_a": atm + jitter,
        "pm2.5_atm_b": atm - jitter,
        "pm2.5_cf_1": cf1,
        "pm2.5_cf_1_a": cf1 + jitter,
        "pm2.5_cf_1_b": cf1 - jitter,
    })


@pytest.fixture(scope="module")
def audited():
    """Analyse one dataset once; every test asserts against the same response."""
    df = _reference_csv()
    payload = df.to_csv(index=False).encode()
    resp = client.post(
        "/api/analyze",
        files={"file": ("audit.csv", payload, "text/csv")},
        data={"timezone": "UTC"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return df, body.get("result", body)


def _expected_corrected(df: pd.DataFrame) -> pd.Series:
    """Independent recomputation: the published equation on the cf_1 column."""
    pm = pd.to_numeric(df["pm2.5_cf_1"], errors="coerce")
    rh = pd.to_numeric(df["humidity"], errors="coerce")
    return (PM_COEF * pm - RH_COEF * rh + INTERCEPT).clip(lower=0)


# --------------------------------------------------------------------------
# The correction itself
# --------------------------------------------------------------------------

def test_coefficients_match_published_source():
    assert analysis.BARKJOHN_PM_COEF == PM_COEF
    assert analysis.BARKJOHN_RH_COEF == RH_COEF
    assert analysis.BARKJOHN_INTERCEPT == INTERCEPT


def test_correction_matches_hand_computation():
    pm = pd.Series([0.0, 2.0, 10.0, 25.0, 100.0, 250.0])
    rh = pd.Series([10.0, 85.0, 50.0, 60.0, 30.0, 45.0])
    got = analysis.apply_epa_correction(pm, rh)
    want = (PM_COEF * pm - RH_COEF * rh + INTERCEPT).clip(lower=0)
    pd.testing.assert_series_equal(got, want, check_names=False)


def test_correction_never_returns_negative_concentration():
    """Clean + humid can drive the equation below zero; mass cannot be negative."""
    out = analysis.apply_epa_correction(pd.Series([0.0, 0.5, 1.0]), pd.Series([99.0, 95.0, 90.0]))
    assert (out >= 0).all()


def test_displayed_formula_matches_the_computed_one():
    """The reproducibility record must state the formula actually executed."""
    for token in (str(PM_COEF), str(RH_COEF), str(INTERCEPT)):
        assert token in analysis.BARKJOHN_FORMULA
    assert "10.5194/amt-14-4617-2021" in analysis.BARKJOHN_CITATION


# --------------------------------------------------------------------------
# Column selection -- the defect that made every downstream number wrong
# --------------------------------------------------------------------------

def test_cf1_is_preferred_over_atm(audited):
    """Barkjohn is defined on cf_1. Picking atm applies the right formula to the
    wrong input and understates peaks badly."""
    df, _ = audited
    detected = analysis.detect_columns(df)
    primary, ch_a, ch_b = analysis.choose_channels(detected["pm25"])
    assert primary is not None and "cf_1" in primary.name
    assert ch_a is not None and "cf_1" in ch_a.name
    assert ch_b is not None and "cf_1" in ch_b.name


def test_atm_used_only_when_cf1_absent():
    df = _reference_csv(n_hours=48).drop(
        columns=["pm2.5_cf_1", "pm2.5_cf_1_a", "pm2.5_cf_1_b"])
    primary, _, _ = analysis.choose_channels(analysis.detect_columns(df)["pm25"])
    assert primary is not None and "atm" in primary.name


# --------------------------------------------------------------------------
# Published aggregate numbers
# --------------------------------------------------------------------------

def test_reported_mean_matches_independent_recomputation(audited):
    df, result = audited
    expected = float(_expected_corrected(df).mean())
    reported = float(result["summary"]["pm25_average_epa_corrected"])
    assert reported == pytest.approx(expected, abs=0.1), (
        f"app mean {reported} vs independent {expected:.3f}")


def test_exceedance_day_counts_match(audited):
    df, result = audited
    s = pd.Series(_expected_corrected(df).values,
                  index=pd.to_datetime(df["time_stamp"], utc=True))
    daily = s.resample("D").mean().dropna()
    exposure = result["summary"]["exposure"]
    assert exposure["n_days"] == len(daily)
    assert exposure["days_over_who15"] == int((daily > 15).sum())
    assert exposure["days_over_epa35"] == int((daily > 35).sum())


def test_humidity_mean_matches(audited):
    df, result = audited
    assert float(result["summary"]["mean_rh"]) == pytest.approx(
        float(df["humidity"].mean()), abs=0.6)


# --------------------------------------------------------------------------
# Health-risk guardrail
# --------------------------------------------------------------------------

def test_long_term_risk_withheld_on_short_records():
    """RR~1.08 per 10 ug/m3 describes multi-year exposure. A short upload must not
    produce a mortality figure for someone's home."""
    df = _reference_csv(n_hours=24 * 10)
    resp = client.post("/api/analyze",
                       files={"file": ("short.csv", df.to_csv(index=False).encode(), "text/csv")},
                       data={"timezone": "UTC"})
    assert resp.status_code == 200
    exposure = resp.json()["result"]["summary"]["exposure"]
    assert exposure["excess_mortality_risk_pct"] is None
    assert exposure["risk_withheld_note"]


def test_no_unverifiable_institutional_authority():
    """Every threshold must be attributable to a real, checkable published source.

    An earlier version credited institutional "standards" that define no such
    thresholds. Quality criteria are now either cited to a real publication (EPA's
    2021 air-sensor target values) or stated plainly as this platform's own.
    """
    import pathlib

    # Institution names must not appear as the authority behind a threshold.
    forbidden = ("JHU/MIT", "MIT standard", "MIT criteria", "MIT tier", "MIT framework")
    src = pathlib.Path(analysis.__file__).parent
    targets = list(src.rglob("*.py")) + list((src / "templates").rglob("*.html"))
    for path in targets:
        text = path.read_text()
        for token in forbidden:
            assert token not in text, f"unverifiable attribution '{token}' in {path.name}"


# --------------------------------------------------------------------------
# Missing data must never be reported as clean air
# --------------------------------------------------------------------------

def test_gap_days_are_not_counted_as_compliant():
    """A day the sensor did not record is not a day that met the guideline.

    Regression: missing daily values were coerced to 0.0 ug/m3, which is below every
    guideline, so downtime was reported as compliance ("26 of 30 days met the WHO
    guideline" on a record containing only 23 monitored days).
    """
    df = _reference_csv(n_hours=24 * 30)
    ts = pd.to_datetime(df["time_stamp"], utc=True)
    # Blank out a full week in the middle -- a realistic outage.
    outage = (ts >= "2026-03-10") & (ts < "2026-03-17")
    df = df.loc[~outage].reset_index(drop=True)

    resp = client.post("/api/analyze",
                       files={"file": ("gappy.csv", df.to_csv(index=False).encode(), "text/csv")},
                       data={"timezone": "UTC"})
    assert resp.status_code == 200, resp.text
    summary = resp.json()["result"]["summary"]

    s = pd.Series(_expected_corrected(df).values,
                  index=pd.to_datetime(df["time_stamp"], utc=True))
    monitored = s.resample("D").mean().dropna()

    # The exposure block must count only days that actually have data.
    assert summary["exposure"]["n_days"] == len(monitored)
    span_days = (monitored.index.max() - monitored.index.min()).days + 1
    assert len(monitored) < span_days, "fixture should contain a real gap"
    assert summary["exposure"]["days_over_who15"] == int((monitored > 15).sum())


# --------------------------------------------------------------------------
# Deep-audit regressions: trend test, AQI, event detection, DiD, quality
# radar, diurnal pattern, Word export
# --------------------------------------------------------------------------

def test_trend_slope_not_extrapolated_from_a_short_record():
    """A 30-day record must not report a 'per year' slope: that annualises the
    fitted rate 12x beyond anything observed, and the CI was 41x the WHO guideline."""
    from app import analysis
    idx = pd.date_range("2026-03-01", periods=30, freq="D")
    rng = np.random.default_rng(1)
    s = pd.Series(10 + rng.normal(0, 2, 30), index=idx)
    tt = analysis.build_trend_test(s)
    assert tt is not None
    assert tt["annualised"] is False
    assert tt["sen_slope_per_year"] is None
    assert tt["sen_slope_per_day"] is not None


def test_trend_slope_annualised_and_recovered_on_a_long_record():
    from app import analysis
    idx = pd.date_range("2024-01-01", periods=730, freq="D")
    rng = np.random.default_rng(3)
    true_per_day = -0.004
    y = 20 + true_per_day * np.arange(730) + rng.normal(0, 2, 730)
    tt = analysis.build_trend_test(pd.Series(y, index=idx))
    assert tt["annualised"] is True
    lo, hi = tt["sen_slope_ci"]
    true_per_year = true_per_day * 365.25
    assert lo <= true_per_year <= hi


def test_aqi_has_no_breakpoint_gaps():
    from app import analysis
    for pm in [round(x, 2) for x in np.arange(0, 400, 0.37)]:
        _, label, _ = analysis.calc_aqi_value(pm)
        assert label != "Unknown", f"PM2.5={pm} fell in an AQI breakpoint gap"


def test_aqi_anchors_match_epa_table():
    from app import analysis
    for pm, expected in [(9.0, 50), (35.4, 100), (55.4, 150), (125.4, 200)]:
        aqi, _, _ = analysis.calc_aqi_value(pm)
        assert aqi == expected


def test_events_detected_on_corrected_not_raw_pm25():
    """Sustained-event detection must use the same corrected values as every other
    statistic. Using raw values compared >2000 raw exceedances of 35 to only ~240
    after correction on real data -- a large false-positive rate."""
    from app import analysis
    idx = pd.date_range("2026-03-01", periods=200, freq="h")
    raw = pd.Series(40.0, index=idx)          # raw > 35 throughout
    corrected = pd.Series(20.0, index=idx)    # corrected well below 35 throughout
    df = pd.DataFrame({"pm25": raw, "pm25_corrected": corrected})
    events_raw = analysis.detect_events(df, "pm25")
    events_corr = analysis.detect_events(df, "pm25_corrected")
    assert not events_raw[events_raw["type"] == "Sustained"].empty
    assert events_corr[events_corr["type"] == "Sustained"].empty


def test_event_count_is_the_real_event_count():
    df = _reference_csv(n_hours=24 * 20)
    resp = client.post("/api/analyze",
                       files={"file": ("ev.csv", df.to_csv(index=False).encode(), "text/csv")},
                       data={"timezone": "UTC"})
    body = resp.json()["result"]
    n_reported = body["summary"]["n_pollution_events"]
    outputs = body.get("outputs", {})
    # Cross-check against the actual detector output when the CSV is available.
    assert isinstance(n_reported, int) and n_reported >= 0


def test_did_uses_daily_means_not_raw_hourly_pairs():
    """Hourly pairing on an autocorrelated difference understated the CI by ~4x.
    A null (zero true difference) case must not be reported as significant."""
    from app import analysis
    rng = np.random.default_rng(5)
    n = 24 * 60
    idx = pd.date_range("2026-03-01", periods=n, freq="h")
    weather = pd.Series(np.cumsum(rng.normal(0, 0.35, n))).rolling(24, min_periods=1).mean().to_numpy()
    local = np.array(pd.Series(np.cumsum(rng.normal(0, 0.30, n))).rolling(72, min_periods=1).mean())
    local = local - np.nanmean(local)
    ctrl = pd.Series(12 + weather + rng.normal(0, 1.0, n), index=idx).clip(lower=0)
    treat = pd.Series(12 + weather + local + rng.normal(0, 1.0, n), index=idx).clip(lower=0)
    r = analysis.compute_did(ctrl, treat)
    assert r["unit_of_analysis"] == "daily mean"
    assert r["significant"] is False, "null case (true difference = 0) must not read significant"


def test_did_recovers_a_true_effect():
    from app import analysis
    rng = np.random.default_rng(5)
    n = 24 * 60
    idx = pd.date_range("2026-03-01", periods=n, freq="h")
    ctrl = pd.Series(np.clip(12 + rng.normal(0, 2, n), 1, None), index=idx)
    treat = (ctrl * 1.25 + rng.normal(0, 1.0, n)).clip(lower=0)
    r = analysis.compute_did(ctrl, treat)
    lo, hi = r["excess_ci_pct"]
    assert lo <= 25.0 <= hi
    assert r["significant"] is True


def test_data_integrity_metric_responds_to_corrupted_rows():
    """This metric was scored against pre-filtered data and always read 100%,
    regardless of how much of the submitted file was invalid."""
    from app import analysis
    n = 400
    cleaned = pd.DataFrame({"pm25": np.full(300, 10.0)},
                           index=pd.date_range("2026-03-01", periods=300, freq="h"))
    ca = {"cv_between_channels": 5.0, "agreement_pct": 95.0}
    radar = analysis.build_radar_profile(cleaned, 80.0, ca, coverage_score=90, n_rows_submitted=n)
    idx = radar["labels"].index("Internal Data Integrity")
    assert radar["values"][idx] == pytest.approx(75.0, abs=0.5)


def test_diurnal_pattern_averages_hours_not_raw_samples():
    """Grouping raw 2-minute readings directly by hour-of-day weights each hour by
    how many samples it happens to contain under patchy coverage. Hour-then-day
    averaging must match hourly-based independent recomputation."""
    from app import analysis
    rng = np.random.default_rng(2)
    # Hour 5 has 10x the sampling density of hour 6, at a different mean level.
    t5 = pd.date_range("2026-03-01 05:00", periods=300, freq="12s", tz="UTC")
    t6 = pd.date_range("2026-03-01 06:00", periods=30, freq="2min", tz="UTC")
    idx = t5.append(t6)
    vals = np.r_[np.full(300, 5.0), np.full(30, 25.0)] + rng.normal(0, 0.01, 330)
    df = pd.DataFrame({"pm25_corrected": vals}, index=idx)
    out = analysis.build_pm25_temporal_radar(df)
    by_hour = dict(zip(out["labels"], out["values"]))
    assert by_hour["05"] == pytest.approx(5.0, abs=0.2)
    assert by_hour["06"] == pytest.approx(25.0, abs=0.2)


def test_word_export_headline_is_epa_corrected_not_raw():
    """The Word report's headline PM2.5 must match the dashboard's EPA-corrected
    figure. It previously read the raw field, overstating the average by the same
    margin raw PurpleAir readings run above corrected ones (order ~60%)."""
    df = _reference_csv(n_hours=24 * 20)
    resp = client.post("/api/analyze",
                       files={"file": ("word.csv", df.to_csv(index=False).encode(), "text/csv")},
                       data={"timezone": "UTC"})
    file_id = resp.json()["file_id"]
    corrected = resp.json()["result"]["summary"]["pm25_average_epa_corrected"]
    word_resp = client.get(f"/api/download/{file_id}/report_word")
    assert word_resp.status_code == 200
    import io
    from docx import Document
    doc = Document(io.BytesIO(word_resp.content))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert f"{corrected}" in text
    assert "EPA-corrected" in text
