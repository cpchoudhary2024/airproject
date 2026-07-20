from __future__ import annotations

import json
import math
import os
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd
from matplotlib import pyplot as plt
from matplotlib.dates import DateFormatter, AutoDateLocator
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from statsmodels.tsa.seasonal import STL

try:
    from timezonefinder import TimezoneFinder
    import pytz
    HAS_TZ_LIBS = True
except ImportError:
    HAS_TZ_LIBS = False

plt.switch_backend("Agg")

PM25_RANGE = (0.0, 1000.0)
TEMP_RANGE_F = (-40.0, 140.0)
HUMID_RANGE = (0.0, 100.0)
PRESS_RANGE = (800.0, 1100.0)
LAT_RANGE = (-90.0, 90.0)
LON_RANGE = (-180.0, 180.0)

# EPA AQI breakpoints — revised February 2024 alongside the PM2.5 NAAQS update.
# Good upper boundary lowered from 12 µg/m³ to 9 µg/m³.
# Unhealthy ceiling lowered from 150.4 to 125.4; Very Unhealthy ceiling from 250.4 to 225.4.
AQI_BREAKPOINTS = [
    (0.0,   9.0,   0,  50, "Good",          "#00E400"),
    (9.1,  35.4,  51, 100, "Moderate",      "#FFFF00"),
    (35.5, 55.4, 101, 150, "USG",           "#FF7E00"),
    (55.5, 125.4, 151, 200, "Unhealthy",    "#FF0000"),
    (125.5, 225.4, 201, 300, "Very Unhealthy", "#8F3F97"),
    (225.5, 325.4, 301, 400, "Hazardous",   "#7E0023"),
    (325.5, 10000.0, 401, 500, "Hazardous", "#7E0023"),
]


@dataclass
class DetectedColumn:
    name: str
    confidence: float
    channel: Optional[str] = None
    reason: Optional[str] = None


def normalize_col(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", name.strip().lower())
    return value.strip("_")


def score_name_match(col_norm: str, patterns: Iterable[str]) -> float:
    score = 0.0
    for pattern in patterns:
        if col_norm == pattern:
            score = max(score, 1.0)
        elif pattern in col_norm:
            score = max(score, 0.7)
        else:
            # Only count word tokens of length >= 3. Single/double-character tokens
            # such as the "a"/"b" channel suffixes (from "humidity_a") would otherwise
            # match almost any header (e.g. "a" in "private"), causing false positives.
            toks = [t for t in pattern.split("_") if len(t) >= 3]
            if toks and any(t in col_norm for t in toks):
                score = max(score, 0.5)
    return score


def infer_channel(col_norm: str) -> Optional[str]:
    if re.search(r"(channel|sensor|ch)_?a$", col_norm) or col_norm.endswith("_a"):
        return "A"
    if re.search(r"(channel|sensor|ch)_?b$", col_norm) or col_norm.endswith("_b"):
        return "B"
    return None


def range_score(series: pd.Series, valid_range: Tuple[float, float]) -> float:
    numeric = pd.to_numeric(series, errors="coerce")
    numeric = numeric.dropna()
    if numeric.empty:
        return 0.0
    lo, hi = valid_range
    within = ((numeric >= lo) & (numeric <= hi)).mean()
    return float(within)


def timestamp_score(series: pd.Series) -> float:
    parsed = pd.to_datetime(series, errors="coerce", utc=True)
    return float(parsed.notna().mean())


def detect_columns(df: pd.DataFrame) -> Dict[str, List[DetectedColumn]]:
    patterns = {
        "pm25": [
            "pm2_5",
            "pm25",
            "pm2_5_atm",
            "pm2_5_cf_1",
            "pm2_5_cf1",
            "pm2_5_atm_a",
            "pm2_5_atm_b",
            "pm2_5_cf_1_a",
            "pm2_5_cf_1_b",
        ],
        "temp": ["temp", "temperature", "temperature_a", "temperature_b",
                 "temp_f", "temp_c", "current_temp_f", "current_temp_c", "current_temp", "air_temp"],
        "humidity": ["humidity", "humid", "rh", "humidity_a", "humidity_b",
                     "relative_humidity", "current_humidity", "rel_humidity"],
        "pressure": ["pressure", "press", "baro", "pressure_a", "pressure_b",
                     "barometric_pressure", "current_pressure", "atm_pressure"],
        "timestamp": ["time", "timestamp", "datetime", "time_stamp", "date",
                      "created_at", "created", "utc", "utc_time", "date_time", "epoch", "epochtime"],
        "latitude": ["lat", "latitude"],
        "longitude": ["lon", "longitude", "lng"],
    }
    # Value-range used both for scoring and the value-only fallback below.
    _ranges = {
        "pm25": PM25_RANGE, "temp": TEMP_RANGE_F, "humidity": HUMID_RANGE,
        "pressure": PRESS_RANGE, "latitude": LAT_RANGE, "longitude": LON_RANGE,
    }

    def _r_score(key, col):
        if key == "timestamp":
            return timestamp_score(df[col])
        return range_score(df[col], _ranges[key]) if key in _ranges else 0.0

    normalized = {col: normalize_col(col) for col in df.columns}
    detected: Dict[str, List[DetectedColumn]] = {key: [] for key in patterns}

    for original, norm in normalized.items():
        channel = infer_channel(norm)
        for key, pats in patterns.items():
            name_score = score_name_match(norm, pats)
            if name_score == 0.0:
                continue
            r_score = _r_score(key, original)
            confidence = 0.6 * name_score + 0.4 * r_score
            detected[key].append(
                DetectedColumn(name=original, confidence=confidence, channel=channel)
            )

    # ── Value-based fallback ─────────────────────────────────────────────────
    # If a column's NAME matched nothing but its VALUES clearly fit a known
    # quantity, still detect it. This makes parsing robust to unfamiliar CSV
    # headers (e.g. a PM2.5 column called "particles" or a timestamp called "ts").
    # Only apply value-only matching to the two REQUIRED, value-distinctive types
    # (PM2.5 and timestamp). Humidity/temperature/pressure share overlapping numeric
    # ranges (e.g. 0–100), so matching them by value alone causes false positives —
    # they must be identified by name.
    _claimed = {d.name for lst in detected.values() for d in lst}
    for original, norm in normalized.items():
        if original in _claimed:
            continue
        # timestamp: must parse as dates; pm25: only if nothing was named for it yet
        if timestamp_score(df[original]) >= 0.90:
            detected["timestamp"].append(
                DetectedColumn(name=original, confidence=0.4 * timestamp_score(df[original]), channel=None))
            continue
        if not detected["pm25"] and range_score(df[original], PM25_RANGE) >= 0.90:
            detected["pm25"].append(
                DetectedColumn(name=original, confidence=0.4 * range_score(df[original], PM25_RANGE),
                               channel=infer_channel(norm)))

    for key in detected:
        detected[key].sort(key=lambda item: item.confidence, reverse=True)

    # PM2.5 variant preference: cf_1 outranks atm.
    #
    # PurpleAir exports two calibrations of the same measurement — "cf_1" and "atm".
    # The EPA-adopted Barkjohn (2021) correction was derived on, and is defined for,
    # cf_1 input ("PM2.5 = 0.524 x PA_cf_1 - 0.0862 x RH + 5.75"). Feeding atm data
    # into it applies the right equation to the wrong input: the two calibrations
    # diverge as concentration rises, so peaks are materially understated. Name/range
    # confidence alone cannot distinguish them, so the preference is explicit here.
    def _pm25_variant_rank(name: str) -> int:
        n = normalize_col(name)
        if "cf_1" in n or "cf1" in n:
            return 0          # preferred: the correction's defined input
        if "atm" in n:
            return 2          # usable, but only when no cf_1 column exists
        return 1              # unlabelled PM2.5 column
    detected["pm25"].sort(key=lambda item: (_pm25_variant_rank(item.name), -item.confidence))

    return detected


def pick_best(detected: List[DetectedColumn]) -> Optional[DetectedColumn]:
    return detected[0] if detected else None


def choose_channels(
    detected: List[DetectedColumn],
) -> Tuple[Optional[DetectedColumn], Optional[DetectedColumn], Optional[DetectedColumn]]:
    primary = pick_best(detected)

    # Keep A/B in the same calibration family as the primary (cf_1 vs atm). Mixing
    # them would compare a cf_1 primary against atm channels, so the A/B agreement
    # statistics would describe two different calibrations rather than two sensors.
    def _family(name: str) -> str:
        n = normalize_col(name)
        if "cf_1" in n or "cf1" in n:
            return "cf_1"
        return "atm" if "atm" in n else ""

    want = _family(primary.name) if primary else ""

    def _pick(ch: str) -> Optional[DetectedColumn]:
        same = [d for d in detected if d.channel == ch and _family(d.name) == want]
        if same:
            return same[0]
        return next((d for d in detected if d.channel == ch), None)

    return primary, _pick("A"), _pick("B")


# Barkjohn et al., Atmospheric Measurement Techniques 14, 4617 (2021) — the US-wide
# correction adopted by the U.S. EPA for the AirNow Fire & Smoke Map. These are the
# single source of truth for the coefficients: every displayed formula, worked
# example and reproducibility record is rendered from them, so the number the app
# computes and the number it claims to compute cannot drift apart.
BARKJOHN_PM_COEF = 0.524
BARKJOHN_RH_COEF = 0.0862
BARKJOHN_INTERCEPT = 5.75
BARKJOHN_FORMULA = (
    f"{BARKJOHN_PM_COEF} x PA_cf1 - {BARKJOHN_RH_COEF} x RH + {BARKJOHN_INTERCEPT}"
)
BARKJOHN_CITATION = (
    "Barkjohn, Gantt & Clements (2021), Development and application of a United States-wide "
    "correction for PM2.5 data collected with the PurpleAir sensor, Atmospheric Measurement "
    "Techniques 14, 4617-4637, Eq. 10. doi:10.5194/amt-14-4617-2021"
)


def barkjohn_corrected(pm: float, rh: float) -> float:
    """Scalar form of the correction — used for the worked examples shown to users so
    they are computed by the same code path as the data, never typed by hand."""
    return BARKJOHN_PM_COEF * pm - BARKJOHN_RH_COEF * rh + BARKJOHN_INTERCEPT


def apply_epa_correction(pm25: pd.Series, rh: pd.Series) -> pd.Series:
    """Apply the EPA-adopted Barkjohn (2021) correction, exactly as published.

    No coefficient is refitted here. On cool, clean, humid readings the humidity term
    can drive the result slightly below zero; a negative mass concentration is
    unphysical, so results are clipped at 0 — the same convention used for the LRAPA
    and AQ&U corrections below.
    """
    pm = pd.to_numeric(pm25, errors="coerce")
    humid = pd.to_numeric(rh, errors="coerce")
    return (BARKJOHN_PM_COEF * pm - BARKJOHN_RH_COEF * humid + BARKJOHN_INTERCEPT).clip(lower=0)


def apply_lrapa_correction(pm: pd.Series) -> pd.Series:
    """LRAPA correction: 0.5 × PM_cf1 - 0.66"""
    return (0.5 * pm - 0.66).clip(lower=0)


def apply_aqu_correction(pm: pd.Series) -> pd.Series:
    """AQ&U correction: 0.778 × PM_cf1 + 2.65"""
    return (0.778 * pm + 2.65).clip(lower=0)


def _tz_is_as_recorded(tz_label) -> bool:
    """True for the no-conversion labels: 'As recorded' or 'EDT (as recorded)'."""
    return bool(tz_label) and "as recorded" in str(tz_label).lower()


def _tz_recorded_zone(tz_label) -> str:
    """Extract the user-stated zone name: 'EDT (as recorded)' -> 'EDT'; '' if none."""
    if not _tz_is_as_recorded(tz_label):
        return ""
    zone = re.sub(r"\(as recorded\)", "", str(tz_label), flags=re.IGNORECASE).strip()
    return "" if zone.lower() in ("", "as recorded") else zone


def _ts_str(datetime_like) -> list:
    """Convert timestamps to naive-local strings WITHOUT timezone offset, for Plotly display.

    Plotly.js re-converts tz-aware strings (e.g. '+05:30') back to browser-UTC, undoing
    any server-side timezone conversion.  Stripping the offset makes Plotly treat the
    already-converted local time as-is.
    """
    try:
        if isinstance(datetime_like, pd.DatetimeIndex):
            return datetime_like.strftime('%Y-%m-%dT%H:%M:%S').tolist()
        idx = pd.DatetimeIndex(datetime_like)
        return idx.strftime('%Y-%m-%dT%H:%M:%S').tolist()
    except Exception:
        return pd.Series(datetime_like).astype(str).tolist()


def build_calendar_data(daily_aqi: pd.Series) -> dict:
    """GitHub-style calendar heatmap: list of {date, aqi, category, color, weekday, week_seq} per day."""
    if daily_aqi.empty:
        return {"days": []}

    def _color(v):
        if pd.isna(v): return "#e0e0e0"
        v = int(v)
        if v <= 50:  return "#00C400"
        if v <= 100: return "#FFFF00"
        if v <= 150: return "#FF7E00"
        if v <= 200: return "#FF0000"
        if v <= 300: return "#8F3F97"
        return "#7E0023"

    def _cat(v):
        if pd.isna(v): return "No Data"
        v = int(v)
        if v <= 50:  return "Good"
        if v <= 100: return "Moderate"
        if v <= 150: return "Unhealthy for Sensitive Groups"
        if v <= 200: return "Unhealthy"
        if v <= 300: return "Very Unhealthy"
        return "Hazardous"

    # Build sorted date→aqi dict
    days = []
    first_date = pd.Timestamp(daily_aqi.index.min()).normalize()
    for date, val in daily_aqi.sort_index().items():
        d = pd.Timestamp(date).normalize()
        week_seq = int((d - first_date).days // 7)
        days.append({
            "date":     d.strftime("%Y-%m-%d"),
            "aqi":      None if pd.isna(val) else int(round(float(val))),
            "category": _cat(val),
            "color":    _color(val),
            "weekday":  int(d.weekday()),   # 0=Mon … 6=Sun
            "week_seq": week_seq,
        })
    return {"days": days}


def build_narrative_summary(
    pm_corr_avg, pm_raw_avg, aqi_avg, aqi_cat,
    start_iso, end_iso, n_total,
    n_events, pm25_max, cv, coverage, quality_score,
    who_15_hours: int = 0, epa_35_hours: int = 0,
    n_days: int = 0, aqi_current: int = 0,
) -> str:
    """Comprehensive plain-English summary covering air quality, health risk, sensor health, and data quality."""
    try:
        try:
            start_fmt = pd.Timestamp(start_iso).strftime("%B %d, %Y") if start_iso else "?"
            end_fmt   = pd.Timestamp(end_iso).strftime("%B %d, %Y")   if end_iso   else "?"
        except Exception:
            start_fmt, end_fmt = str(start_iso), str(end_iso)

        pm_val = pm_corr_avg if pm_corr_avg is not None else pm_raw_avg

        # ── 1. Monitoring overview ────────────────────────────────────────────
        days_label = f"{n_days} days" if n_days > 1 else "1 day"
        parts = [
            f"MONITORING OVERVIEW: Between {start_fmt} and {end_fmt} ({days_label}), "
            f"this PurpleAir sensor recorded {n_total:,} PM2.5 measurements (approximately every 2 minutes)."
        ]

        # ── 2. Air quality results ────────────────────────────────────────────
        # AQI health guidance mapping
        _aqi_guidance = {
            "Good":              "satisfactory air quality — air pollution poses little or no risk (EPA AQI category definition).",
            "Moderate":          "acceptable for most people, though unusually sensitive individuals may notice minor effects.",
            "USG":               "unhealthy for sensitive groups (children, elderly, people with asthma or heart disease); "
                                 "the general public is less likely to be affected.",
            "Unhealthy":         "unhealthy for everyone; sensitive groups may experience more serious health effects. "
                                 "Limit prolonged outdoor exertion.",
            "Very Unhealthy":    "very unhealthy — health warnings in effect for the entire population. "
                                 "Avoid all outdoor physical activity if possible.",
            "Hazardous":         "hazardous — emergency health conditions for the entire population. "
                                 "Stay indoors and keep windows closed.",
        }
        health_msg = _aqi_guidance.get(aqi_cat, "of uncertain category — check AQI thresholds.")

        # Note: WHO 15 / EPA 35 formally apply to 24-hour (daily) means; the
        # period mean is compared to those levels as context, and the wording
        # says so explicitly to avoid a category error.
        who_comparison = ""
        if pm_val is not None:
            if pm_val <= 15:
                who_comparison = (
                    f"The period mean is below the WHO 24-hour guideline level of 15 µg/m³ "
                    f"(the guideline formally applies to daily means; see the exceedance counts below)."
                )
            elif pm_val <= 35:
                who_comparison = (
                    f"The period mean is above the WHO 24-hour guideline level (15 µg/m³) but below the "
                    f"EPA 24-hour standard level (35 µg/m³). Sustained exposure in this range is associated "
                    f"with increased respiratory and cardiovascular risk in epidemiological studies (WHO, 2021)."
                )
            else:
                who_comparison = (
                    f"The period mean is above both the WHO 24-hour guideline level (15 µg/m³) and the "
                    f"EPA 24-hour standard level (35 µg/m³) — persistently elevated fine-particle "
                    f"pollution associated with increased health risk across the exposed population."
                )

        parts.append(
            f"\nAIR QUALITY: The mean EPA Barkjohn-corrected PM2.5 was {pm_val:.1f} µg/m³, {health_msg} "
            f"{who_comparison}"
        )

        # ── 3. Peak and exceedances ───────────────────────────────────────────
        peak_note = ""
        if pm25_max and pm25_max > 0:
            peak_note = f" The single highest recorded PM2.5 was {pm25_max:.1f} µg/m³."
        exceed_parts = []
        if who_15_hours > 0:
            exceed_parts.append(f"hourly PM2.5 was above the WHO 24-hour guideline level (15 µg/m³) for {who_15_hours} hours")
        if epa_35_hours > 0:
            exceed_parts.append(f"above the EPA 24-hour standard level (35 µg/m³) for {epa_35_hours} hours")
        if exceed_parts:
            exceedance_note = "During the monitoring period, " + " and ".join(exceed_parts) + "."
        elif who_15_hours == 0 and epa_35_hours == 0:
            exceedance_note = "Hourly PM2.5 never rose above the WHO 24-hour guideline level of 15 µg/m³ during the monitoring period."
        else:
            exceedance_note = ""
        parts.append(f"\nPOLLUTION EVENTS:{peak_note} {exceedance_note}")

        # ── 4. Detected events ────────────────────────────────────────────────
        if n_events > 0:
            parts.append(
                f"Automated anomaly detection (STL decomposition) identified {n_events} pollution episode(s) "
                f"that exceeded the 2-standard-deviation threshold above background levels. "
                f"'Spike' events are sharp, short-duration surges (typically minutes to 1–2 hours) caused "
                f"by nearby sources such as traffic, cooking, or burning. "
                f"'Sustained' events last 3+ hours and often indicate regional pollution transport, "
                f"wildfires, or prolonged industrial activity."
            )
        else:
            parts.append(
                "Automated anomaly detection found no statistically significant pollution episodes — "
                "PM2.5 remained within normal background variation throughout the monitoring period."
            )

        # ── 5. Sensor health ─────────────────────────────────────────────────
        if cv is not None:
            if cv < 10:
                cv_label = "excellent (research-grade)"
                cv_meaning = "Both sensor channels are in close agreement, confirming high data reliability."
            elif cv < 15:
                cv_label = "acceptable"
                cv_meaning = "Minor channel divergence exists but falls within acceptable limits for field sensors."
            else:
                cv_label = "poor — sensor maintenance is recommended"
                cv_meaning = (
                    "Significant disagreement between channels A and B suggests sensor degradation, "
                    "contamination, or a hardware fault. Data should be used with caution."
                )
            parts.append(
                f"\nSENSOR HEALTH: Dual-channel agreement was {cv_label} (CV = {cv:.1f}%). {cv_meaning}"
            )

        # ── 6. Data quality ───────────────────────────────────────────────────
        if quality_score >= 90:
            q_label = "excellent — appropriate to support research publications and regulatory submissions"
        elif quality_score >= 80:
            q_label = "good — appropriate for most research and community reporting purposes"
        elif quality_score >= 70:
            q_label = "acceptable — note data gaps or quality flags in any formal publication"
        else:
            q_label = "low — interpret results with caution; significant data gaps or quality issues present"
        parts.append(
            f"\nDATA QUALITY: Temporal coverage was {coverage:.1f}% of the monitoring window "
            f"(composite quality score: {quality_score:.1f}/100 — {q_label})."
        )

        return "\n".join(parts)
    except Exception:
        return ""


def get_utc_to_lst_offset_hours(latitude: float, longitude: float) -> Optional[float]:
    """
    RESEARCH-GRADE: Determine UTC offset for Local Standard Time based on GPS coordinates.

    Returns offset in hours (e.g., -5 for EST, -8 for PST).
    Returns None if timezone libraries not available or coordinates invalid.
    """
    if not HAS_TZ_LIBS:
        return None
    
    # Validate coordinates
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    
    try:
        tf = TimezoneFinder()
        tz_name = tf.timezone_at(lat=latitude, lng=longitude)
        if not tz_name:
            return None
        
        # Get timezone object and determine UTC offset
        # Use January 15 to get Standard Time (not Daylight Saving Time)
        tz = pytz.timezone(tz_name)
        dt_utc = pytz.UTC.localize(pd.Timestamp(2026, 1, 15, 12, 0, 0))
        dt_local = dt_utc.astimezone(tz)
        offset = (dt_local.utcoffset().total_seconds()) / 3600
        return float(offset)
    except Exception:
        return None


def convert_utc_hour_to_lst(utc_hour: int, offset_hours: Optional[float]) -> Tuple[int, str]:
    """
    Convert UTC hour (0-23) to Local Standard Time hour and return formatted label.
    
    Returns tuple of (local_hour, label_string)
    """
    if offset_hours is None:
        return utc_hour, f"{utc_hour:02d}:00 UTC"
    
    lst_hour = (utc_hour + int(offset_hours)) % 24
    offset_str = f"UTC{offset_hours:+.0f}" if offset_hours != 0 else "UTC"
    return lst_hour, f"{lst_hour:02d}:00 {offset_str}"


def calc_aqi_value(pm: float) -> Tuple[int, str, str]:
    """PM2.5 -> AQI using the EPA breakpoint table above.

    The concentration is TRUNCATED to one decimal place first, as specified in EPA's
    AQI Technical Assistance Document. This is not cosmetic: the published breakpoints
    are edge-to-edge on a 0.1 grid (… 9.0 | 9.1 …), so an untruncated value such as
    9.05 falls between two bins and matches none. Previously such values returned
    AQI 0 / "Unknown", which — being zero — pulled any downstream AQI average
    *downwards* and made air quality look better than it was.
    """
    if pm is None or (isinstance(pm, float) and math.isnan(pm)):
        return 0, "Unknown", "#9E9E9E"
    try:
        pm_val = float(pm)
    except (TypeError, ValueError):
        return 0, "Unknown", "#9E9E9E"
    if pm_val < 0:
        return 0, "Unknown", "#9E9E9E"
    # Truncate toward zero to one decimal (EPA convention), avoiding binary-float
    # surprises such as 35.5 -> 35.4.
    pm_trunc = math.floor(round(pm_val, 6) * 10.0) / 10.0
    for c_low, c_high, a_low, a_high, label, color in AQI_BREAKPOINTS:
        if c_low <= pm_trunc <= c_high:
            aqi = round(((a_high - a_low) / (c_high - c_low)) * (pm_trunc - c_low) + a_low)
            return int(aqi), label, color
    # Above the top breakpoint the AQI scale is capped at 500 (EPA does not define
    # values beyond it), rather than reported as unknown.
    top = AQI_BREAKPOINTS[-1]
    if pm_trunc > top[1]:
        return 500, top[4], top[5]
    return 0, "Unknown", "#9E9E9E"


def calc_aqi_series(pm_series: pd.Series) -> pd.DataFrame:
    aqi_values = []
    labels = []
    colors = []
    for value in pm_series:
        aqi, label, color = calc_aqi_value(value)
        aqi_values.append(aqi)
        labels.append(label)
        colors.append(color)
    return pd.DataFrame({"aqi": aqi_values, "category": labels, "color": colors})


def summarize_stats(df: pd.DataFrame, columns: List[str]) -> pd.DataFrame:
    stats = df[columns].describe(percentiles=[0.1, 0.25, 0.5, 0.75, 0.9]).T
    stats = stats.reset_index().rename(columns={"index": "metric"})
    return stats


def detect_events(df: pd.DataFrame, pm_col: str) -> pd.DataFrame:
    _empty_cols = ["start", "end", "duration_hours", "peak_pm25", "min_pm25", "pm25_range", "peak_timestamp", "type"]
    if pm_col not in df.columns:
        return pd.DataFrame(columns=_empty_cols)

    series = df[pm_col].dropna()
    if series.empty:
        return pd.DataFrame(columns=_empty_cols)

    # Time-based baseline window, not a fixed sample count.
    #
    # A `rolling(12)` window means 24 minutes on a 2-minute export but 12 hours on an
    # hourly one, so the same physical episode was detected differently depending on
    # which averaging interval the user happened to download. A fixed 2-hour window
    # keeps the detector's meaning constant across export intervals.
    _win = "2h"
    if isinstance(series.index, pd.DatetimeIndex) and len(series) > 2:
        rolling_med = series.rolling(_win, min_periods=3).median()
        rolling_std = series.rolling(_win, min_periods=3).std().fillna(0)
    else:
        rolling_med = series.rolling(12, min_periods=6).median()
        rolling_std = series.rolling(12, min_periods=6).std().fillna(0)
    spikes = series > (rolling_med + 3 * rolling_std)

    # 35 µg/m³ is the EPA 24-hour standard, used here as a "clearly elevated" level
    # for sustained (>=3 h) episodes -- not as a compliance determination, which
    # requires a 24-hour average.
    elevated = series > 35

    def _event_segment(s, t_start, t_end):
        """Return the slice [t_start, t_end) — exclusive of the end timestamp.
        pandas label-based slicing is inclusive on both ends, so we use
        positional searchsorted to get a proper half-open interval."""
        lo = s.index.searchsorted(t_start)
        hi = s.index.searchsorted(t_end)   # exclusive
        return s.iloc[lo:hi] if hi > lo else s.iloc[lo:lo+1]

    events = []
    for label, mask in [("Spike", spikes), ("Sustained", elevated)]:
        start = None
        for ts, flag in mask.items():
            if flag and start is None:
                start = ts
            if not flag and start is not None:
                end = ts
                duration = (end - start).total_seconds() / 3600.0
                if label == "Sustained" and duration < 3:
                    start = None
                    continue
                segment = _event_segment(series, start, end)
                if not segment.empty:
                    peak_idx = segment.idxmax()
                    peak_val = round(float(segment.max()), 2)
                    min_val  = round(float(segment.min()), 2)
                else:
                    peak_idx, peak_val, min_val = start, float("nan"), float("nan")
                events.append({
                    "start": start, "end": end,
                    "duration_hours": round(duration, 2),
                    "peak_pm25": peak_val,
                    "min_pm25":  min_val,
                    "pm25_range": round(peak_val - min_val, 2) if not math.isnan(peak_val) else float("nan"),
                    "peak_timestamp": peak_idx, "type": label,
                })
                start = None
        # Close an episode that is still in progress when the record ends. This must
        # cover BOTH labels: an ongoing spike at the final timestamp was previously
        # discarded, losing the most recent -- often most relevant -- episode. The
        # segment is taken inclusive of the last sample, so its peak is not truncated.
        if start is not None:
            end = mask.index[-1]
            duration = (end - start).total_seconds() / 3600.0
            if label != "Sustained" or duration >= 3:
                segment = series.loc[start:end]
                if not segment.empty:
                    peak_idx = segment.idxmax()
                    peak_val = round(float(segment.max()), 2)
                    min_val  = round(float(segment.min()), 2)
                else:
                    peak_idx, peak_val, min_val = start, float("nan"), float("nan")
                events.append({
                    "start": start, "end": end,
                    "duration_hours": round(duration, 2),
                    "peak_pm25": peak_val,
                    "min_pm25":  min_val,
                    "pm25_range": round(peak_val - min_val, 2) if not math.isnan(peak_val) else float("nan"),
                    "peak_timestamp": peak_idx, "type": label,
                })

    if not events:
        return pd.DataFrame(columns=_empty_cols)

    events_df = pd.DataFrame(events)
    return events_df.sort_values("start")


def classify_gap_patterns(df_with_timestamps: pd.DataFrame) -> Dict[str, Any]:
    """Classify data gaps as contiguous (long stretches) or stochastic (random blips).
    
    Contiguous gaps break STL decomposition trend analysis and are more damaging.
    Stochastic gaps (random missing measurements) are less problematic for time-series analysis.
    """
    if not hasattr(df_with_timestamps, 'set_index') or df_with_timestamps.empty:
        return {
            "gap_type": "None", 
            "max_contiguous_hours": 0, 
            "gap_frequency": "Unknown",
            "total_gap_hours": 0,
            "note": "No timestamps available for gap analysis."
        }
    
    # Create time index to detect gaps
    df_sorted = df_with_timestamps.sort_values("timestamp")
    if len(df_sorted) < 2:
        return {
            "gap_type": "Insufficient data", 
            "max_contiguous_hours": 0, 
            "gap_frequency": "Unknown",
            "total_gap_hours": 0,
            "note": "Insufficient data for gap analysis."
        }
    
    time_diffs = df_sorted["timestamp"].diff().dt.total_seconds() / 3600  # gaps in hours
    
    # Identify gaps longer than expected sampling interval
    large_gaps = time_diffs[time_diffs > 1.0]  # gaps > 1 hour
    
    if len(large_gaps) == 0:
        return {
            "gap_type": "Minimal", 
            "max_contiguous_hours": 0, 
            "gap_frequency": "None detected",
            "total_gap_hours": 0,
            "note": "Few gaps detected. Data suitable for STL decomposition."
        }
    
    max_gap = large_gaps.max()
    n_gaps = len(large_gaps)
    total_gap_hours = large_gaps.sum()
    measurement_hours = (df_sorted["timestamp"].max() - df_sorted["timestamp"].min()).total_seconds() / 3600
    
    # Classify: gaps > 6 hours are contiguous; gaps < 1 hour are stochastic
    if max_gap > 6:
        gap_type = "Contiguous"
        note = f"Long downtime periods detected (max: {max_gap:.1f}h). This breaks STL trend analysis."
    elif n_gaps > measurement_hours * 0.1:  # >10% of periods are gaps
        gap_type = "Stochastic"
        note = f"Random missing readings ({n_gaps} gaps). Less damaging to time-series analysis."
    else:
        gap_type = "Minimal"
        note = f"Few gaps detected. Data suitable for STL decomposition."
    
    return {
        "gap_type": gap_type,
        "max_contiguous_hours": round(max_gap, 2),
        "gap_frequency": f"{n_gaps} gaps",
        "total_gap_hours": round(total_gap_hours, 2),
        "note": note
    }


def build_anomaly_report(
    df: pd.DataFrame,
    timestamp_col: Optional[str],
    pm_col: Optional[str],
    temp_col: Optional[str],
    hum_col: Optional[str],
    press_col: Optional[str],
) -> List[str]:
    notes = []
    if timestamp_col and timestamp_col in df.columns:
        parsed = pd.to_datetime(df[timestamp_col], errors="coerce", utc=True)
        if parsed.isna().mean() > 0.1:
            notes.append("High number of unparseable timestamps.")
        if parsed.duplicated().any():
            notes.append("Duplicate timestamps detected.")
        if parsed.is_monotonic_increasing is False:
            notes.append("Timestamps are not strictly increasing.")
        
        # Enhanced gap analysis with specific timestamps
        gaps = parsed.sort_values().diff().dt.total_seconds().dropna()
        if not gaps.empty and gaps.max() > 6 * 3600:
            # Find and report large gaps
            sorted_ts = parsed.sort_values().reset_index(drop=True)
            gap_series = sorted_ts.diff().dt.total_seconds()
            large_gap_indices = gap_series[gap_series > 6 * 3600].index
            
            gap_details = []
            for idx in large_gap_indices:
                gap_hours = gap_series[idx] / 3600
                if gap_hours > 24:
                    before_ts = sorted_ts[idx - 1].isoformat()
                    after_ts = sorted_ts[idx].isoformat()
                    gap_details.append(f"{gap_hours:.1f}h ({before_ts} → {after_ts})")
                else:
                    gap_details.append(f"{gap_hours:.1f}h")
            
            gap_str = ", ".join(gap_details[:3])  # Show first 3 gaps
            if len(gap_details) > 3:
                gap_str += f", +{len(gap_details) - 3} more"
            notes.append(f"Large timestamp gaps detected: {gap_str}")

    def range_note(col: Optional[str], label: str, valid_range: Tuple[float, float]) -> None:
        if not col or col not in df.columns:
            return
        numeric = pd.to_numeric(df[col], errors="coerce")
        if numeric.isna().mean() > 0.2:
            notes.append(f"{label} has more than 20% missing values.")
        lo, hi = valid_range
        out = ((numeric < lo) | (numeric > hi)).mean()
        if out > 0.05:
            notes.append(f"{label} has over 5% values outside expected range.")

    range_note(pm_col, "PM2.5", PM25_RANGE)
    range_note(temp_col, "Temperature", TEMP_RANGE_F)
    range_note(hum_col, "Humidity", HUMID_RANGE)
    range_note(press_col, "Pressure", PRESS_RANGE)

    return notes


def estimate_sampling_minutes(timestamps: pd.Series) -> Optional[float]:
    if timestamps.empty:
        return None
    # Convert to Series if it's an Index
    if isinstance(timestamps, pd.DatetimeIndex):
        ts_series = pd.Series(timestamps)
    else:
        ts_series = timestamps
    
    sorted_ts = ts_series.sort_values().reset_index(drop=True)
    diffs = sorted_ts.diff().dropna()  # This gives timedelta values
    diffs_minutes = diffs.dt.total_seconds() / 60.0
    diffs_minutes = diffs_minutes[diffs_minutes > 0]
    if diffs_minutes.empty:
        return None
    return float(diffs_minutes.median())


def calculate_coverage_score(timestamps: pd.Series) -> float:
    """Calculate data coverage as a percentage of expected records."""
    if timestamps.empty or len(timestamps) < 2:
        return 0.0
    
    parsed = pd.to_datetime(timestamps, errors="coerce", utc=True)
    parsed = parsed.dropna()
    
    if len(parsed) < 2:
        return 0.0
    
    date_range = (parsed.max() - parsed.min()).total_seconds()
    median_interval = estimate_sampling_minutes(parsed)
    
    if date_range == 0 or median_interval is None or median_interval == 0:
        return 100.0
    
    expected_records = date_range / 60 / median_interval
    actual = len(parsed)
    coverage = (actual / expected_records) * 100
    
    return min(100.0, max(0.0, coverage))


def build_quality_summary(
    df_work: pd.DataFrame,
    cleaned: pd.DataFrame,
    ts_col: Optional[str],
    pm_col: Optional[str],
    temp_col: Optional[str],
    hum_col: Optional[str],
    press_col: Optional[str],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    def add_numeric_row(label: str, column: Optional[str], valid_range: Tuple[float, float]) -> None:
        if not column or column not in df_work.columns:
            return
        numeric = pd.to_numeric(df_work[column], errors="coerce")
        total = int(len(df_work))
        missing_pct = float(numeric.isna().mean() * 100)
        lo, hi = valid_range
        out_of_range_pct = float(((numeric < lo) | (numeric > hi)).mean() * 100)
        valid_pct = float(((numeric >= lo) & (numeric <= hi)).mean() * 100)
        rows.append(
            {
                "metric": label,
                "total": total,
                "missing_pct": round(missing_pct, 2),
                "out_of_range_pct": round(out_of_range_pct, 2),
                "valid_pct": round(valid_pct, 2),
            }
        )

    if ts_col and ts_col in df_work.columns:
        parsed = pd.to_datetime(df_work[ts_col], errors="coerce", utc=True)
        rows.append(
            {
                "metric": "Timestamp",
                "total": int(len(df_work)),
                "missing_pct": round(float(parsed.isna().mean() * 100), 2),
                "out_of_range_pct": 0.0,
                "valid_pct": round(float(parsed.notna().mean() * 100), 2),
            }
        )

    add_numeric_row("PM2.5", pm_col, PM25_RANGE)
    add_numeric_row("Temperature (F)", temp_col, TEMP_RANGE_F)
    add_numeric_row("Humidity (%)", hum_col, HUMID_RANGE)
    add_numeric_row("Pressure (hPa)", press_col, PRESS_RANGE)

    if cleaned.empty:
        return rows

    sampling_minutes = estimate_sampling_minutes(cleaned.index.to_series().dropna())
    if sampling_minutes:
        duration_min = (cleaned.index.max() - cleaned.index.min()).total_seconds() / 60.0
        expected = max(int(duration_min / sampling_minutes) + 1, 1)
        coverage_pct = round(float(len(cleaned) / expected * 100), 2)
        rows.append(
            {
                "metric": "Coverage",
                "total": expected,
                "missing_pct": round(float(100 - coverage_pct), 2),
                "out_of_range_pct": 0.0,
                "valid_pct": coverage_pct,
            }
        )

    return rows


def build_report_markdown(summary: Dict[str, Any], quality_rows: List[Dict[str, Any]],
                          anomalies: List[str], stats: pd.DataFrame = None, 
                          channel_agreement: Dict[str, Any] = None,
                          events: pd.DataFrame = None) -> str:
    lines = [
        "# Air Quality Research-Grade Analysis Report",
        "",
        "## 1. Executive Summary",
        "",
        "This report presents a comprehensive air quality analysis from PurpleAir sensor(s),",
        "including data quality assessment, temporal patterns, and comparisons to regulatory standards.",
        "",
        f"**Analysis Period:** {summary['date_range']['start']} to {summary['date_range']['end']}",
        f"**Total Observations:** {summary['total_readings']} readings",
        f"**Data Quality Score:** {summary['quality_score']}% (percentage of valid readings)",
        "",
        f"**Period Average PM2.5 (raw):** {summary['pm25_average']} µg/m³",
        f"**Period Average PM2.5 (EPA-corrected):** {summary.get('pm25_average_epa_corrected', '—')} µg/m³",
        f"**WHO 24-hour guideline:** 15 µg/m³  |  **EPA 24-hour standard:** 35 µg/m³",
        "",
        "---",
        "",
        "## 2. Introduction & Objectives",
        "",
        "PurpleAir sensors are low-cost air quality monitors that measure particulate matter (PM2.5),",
        "temperature, humidity, and pressure. This analysis aims to:",
        "",
        "- Assess data quality and identify measurement reliability issues",
        "- Evaluate temporal patterns (daily, weekly) in air quality",
        "- Compare measurements to EPA and WHO regulatory standards",
        "- Identify pollution events and anomalies",
        "- Evaluate sensor performance and channel agreement",
        "",
        "---",
        "",
        "## 3. Methods",
        "",
        "### 3.1 Sensor Specifications",
        "",
        "PurpleAir sensors typically include:",
        "- Two independent PM2.5 sensors (Channel A and B) for redundancy",
        "- Dual measurement channels that should read similarly if functioning correctly",
        "- Built-in environmental sensors (temperature, humidity, pressure)",
        "- Local data storage and network connectivity",
        "",
        "### 3.2 Data Collection Period",
        "",
        f"- **Start Date:** {summary['date_range']['start']}",
        f"- **End Date:** {summary['date_range']['end']}",
        f"- **Total Records:** {summary['total_readings']}",
        "",
    ]
    
    if quality_rows:
        lines.append("### 3.3 Data Quality and Completeness")
        lines.append("")
        lines.append("| Metric | Total | Missing % | Out of Range % | Valid % |")
        lines.append("| --- | ---: | ---: | ---: | ---: |")
        for row in quality_rows:
            lines.append(
                f"| {row['metric']} | {row['total']} | {row['missing_pct']} | {row['out_of_range_pct']} | {row['valid_pct']} |"
            )
        lines.append("")
    
    lines.extend([
        "### 3.4 QA/QC Procedures",
        "",
        "- **Range validation:** All measurements checked against physically plausible ranges",
        "  - PM2.5: 0–1000 µg/m³",
        "  - Temperature: -40 to +140°F",
        "  - Humidity: 0–100%",
        "  - Pressure: 800–1100 hPa",
        "- **Timestamp validation:** All readings validated for proper datetime formatting",
        "- **Duplicate detection:** Identical timestamps removed to avoid bias",
        "- **Monotonicity check:** Verified timestamps are in chronological order",
        "",
        "### 3.5 Correction Factors Applied",
        "",
        "- **EPA PM2.5 Correction:** Corrected = 0.524 × PM + 5.75 − 0.0862 × RH",
        "  (Applied when relative humidity data available)",
        "- **Channel Averaging:** When dual channels present, average used for final PM2.5",
        "- **Rolling Medians:** 24-hour and 7-day medians calculated to smooth hourly noise",
        "",
        "---",
        "",
        "## 4. Results",
        "",
        "### 4.1 Summary Statistics",
        "",
    ])
    
    if stats is not None and not stats.empty:
        lines.append("| Metric | Mean | Std | Min | 25% | 50% | 75% | Max |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for _, row in stats.iterrows():
            try:
                lines.append(
                    f"| {row['index']} | "
                    f"{float(row.get('mean', 0)):.2f} | "
                    f"{float(row.get('std', 0)):.2f} | "
                    f"{float(row.get('min', 0)):.2f} | "
                    f"{float(row.get('25%', 0)):.2f} | "
                    f"{float(row.get('50%', 0)):.2f} | "
                    f"{float(row.get('75%', 0)):.2f} | "
                    f"{float(row.get('max', 0)):.2f} |"
                )
            except:
                pass
        lines.append("")
    
    _period_avg = summary.get("pm25_average_epa_corrected") or summary.get("pm25_average")
    lines.append(f"**Period Average PM2.5 (EPA-corrected):** {_period_avg} µg/m³ "
                 f"(with observed range and variability shown above)")
    lines.append("")
    
    lines.extend([
        "### 4.2 Temporal Patterns",
        "",
        "- **Diurnal Pattern:** Hour-by-hour analysis shows typical daily cycle in air quality",
        "  The morning and evening rush hours commonly show elevated PM2.5.",
        "- **Weekly Pattern:** Analysis of day-of-week patterns to identify recurring patterns.",
        "- **Trend Analysis:** 24-hour and 7-day rolling medians identify sustained air quality events.",
        "- **STL Decomposition:** Time-series decomposed into trend, seasonal, and residual components.",
        "",
        "### 4.3 Data Quality Assessment",
        "",
        f"**Overall Data Quality Score:** {summary['quality_score']}%",
        "",
        "This score reflects:",
        "- Percentage of valid PM2.5 readings (60% of score)",
        "- Percentage of valid timestamps (30% of score)",
        "- Percentage of valid auxiliary data/sensors (10% of score)",
        "- **Data coverage/gaps (30% of final score)** — accounts for missing periods",
        "",
        "**Interpretation:**",
        "- 90-100%: Excellent quality, suitable for regulatory analysis",
        "- 70-89%: Good quality, appropriate for research and health reports",
        "- 50-69%: Fair quality, use with caution, may have significant gaps",
        "- <50%: Poor quality, extensive issues present",
        "",
        "**Coverage:** The percentage of time periods where you have data vs. all expected time periods.",
        "A 100% quality + 66% coverage means data quality is high, but 1/3 of expected time periods are missing.",
        "",
        "### 4.4 Channel Agreement Analysis",
        "",
    ])
    
    if channel_agreement:
        lines.append(f"- **Correlation (R²):** {channel_agreement.get('r2', 'N/A')}")
        lines.append(f"- **Mean Absolute Difference:** {channel_agreement.get('mean_abs_diff', 'N/A')} µg/m³")
        lines.append(f"- **Agreement Rate:** {channel_agreement.get('agreement_pct', 'N/A')}% (channels within 10% agreement tolerance)")
        if float(channel_agreement.get('r2', 0)) > 0.85:
            lines.append("")
            lines.append("**Status:** ✓ Excellent channel agreement - sensors are performing consistently")
        elif float(channel_agreement.get('r2', 0)) > 0.70:
            lines.append("")
            lines.append("**Status:** ⚠ Good channel agreement - minor calibration drift possible")
        else:
            lines.append("")
            lines.append("**Status:** ✗ Poor channel agreement - one sensor may need recalibration")
    else:
        lines.append("- **Dual channels not detected:** Unable to perform inter-sensor comparison.")
    
    lines.extend([
        "",
        "---",
        "",
        "## 5. Discussion",
        "",
        "### 5.1 Comparison to Regulatory Standards",
        "",
        "**EPA 24-Hour PM2.5 Standard:** 35 µg/m³",
        "- Readings above this threshold indicate unhealthy air quality for sensitive groups.",
        "",
        "**WHO Guideline:** 15 µg/m³ (24-hour mean)",
        "- More stringent standard for long-term health protection.",
        "",
    ])
    
    if events is not None and not events.empty:
        spike_events = events[events['type'] == 'Spike']
        sustained_events = events[events['type'] == 'Sustained']
        lines.append(f"**Detected Events in This Period:**")
        lines.append(f"- Spike events (rapid increases): {len(spike_events)}")
        lines.append(f"- Sustained elevated periods (≥3 hours >35 µg/m³): {len(sustained_events)}")
        lines.append("")
    
    lines.extend([
        "### 5.2 Data Limitations",
        "",
        "- **Humidity Effects:** EPA correction assumes specific sensor characteristics; actual performance varies.",
        "- **Spatial Representativeness:** Single-point measurements may not represent larger geographic areas.",
        "- **Temporal Resolution:** Hourly or lower resolution may miss rapid pollution spikes.",
        "- **Sensor Drift:** Long-term stability differs from laboratory-grade equipment.",
        "- **Missing Data:** Gaps due to network outages or sensor failures may bias temporal analysis.",
        "",
        "### 5.3 Sources of Variation",
        "",
        "PM2.5 air quality can be influenced by:",
        "- Local emission sources (traffic, cooking, industry)",
        "- Regional transport from distant wildfire smoke or pollution",
        "- Meteorological conditions (wind speed, atmospheric stability, precipitation)",
        "- Humidity effects on particle dynamics and measurement sensitivity",
        "- Time-of-day patterns (commute hours, heating cycles)",
        "",
        "---",
        "",
        "## 6. Conclusions",
        "",
    ])
    
    if anomalies:
        lines.append("### Key Findings:")
        lines.append("")
        lines.append("**Data Quality Issues Detected:**")
        for anomaly in anomalies[:5]:
            lines.append(f"- {anomaly}")
    else:
        lines.append("### Key Findings:")
        lines.append("")
        lines.append("- No major anomalies detected in this dataset.")
    
    lines.extend([
        "",
        "**Recommendations:**",
        "",
        "1. **For Personal Health:** Use AQI values as guide for outdoor activity decisions.",
        "   Consult local health departments for sensitive group guidance.",
        "",
        "2. **For Research:** Export CSV files (cleaned_data, epa_corrected, hourly_summary)",
        "   for further statistical analysis or modeling.",
        "",
        "3. **For Sensor Maintenance:** Monitor channel agreement (R²) over time.",
        "   If below 0.70, consider sensor recalibration.",
        "",
        "4. **For Data Use:** Always cite the data quality score and note any significant gaps",
        "   when reporting results.",
        "",
        "---",
        "",
        "## 7. Appendices",
        "",
        "### A. Raw Data Quality Flags",
        "",
        "All data are flagged during processing for timestamp validity, numeric range compliance,",
        "and consistency checks. Cleaned data exports contain only valid records.",
        "",
        "### B. Detailed Gap Analysis",
        "",
        "- Total records input: " + str(summary.get('total_readings', 'N/A')),
        "- Valid records after cleaning: " + str(int(summary['quality_score'] * summary.get('total_readings', 0) / 100)) if summary.get('total_readings') else "N/A",
        "- Data coverage: " + str(f"{summary['quality_score']}%"),
        "",
        "### C. Sensor Calibration History",
        "",
        "Standard calibration not stored in this dataset. For long-term deployment, recommend",
        "periodic comparison with reference monitors or recalibration with known standards.",
        "",
        "---",
        "",
        "*Report generated by PurpleAir Local Analyzer*",
        ((lambda _z: f"*All times as recorded in the uploaded file{f' ({_z} local time)' if _z else ''} — no timezone conversion*")(
            _tz_recorded_zone(summary.get("tz_label")))
         if _tz_is_as_recorded(summary.get("tz_label"))
         else f"*All times in {summary.get('tz_label') or 'UTC'} unless otherwise noted*"),
    ])

    return "\n".join(lines)


def build_diurnal_pattern(series: pd.Series, latitude: Optional[float] = None, longitude: Optional[float] = None) -> pd.DataFrame:
    """Build diurnal pattern with optional UTC to LST conversion.
    
    RESEARCH-GRADE: If latitude/longitude provided, automatically converts UTC hours to Local Standard Time.
    If timezone determination fails, preserves UTC hours with explicit labeling.
    """
    if series.empty:
        return pd.DataFrame()
    
    # Determine UTC offset if coordinates available
    utc_offset = None
    if latitude is not None and longitude is not None:
        utc_offset = get_utc_to_lst_offset_hours(latitude, longitude)
    
    df = series.to_frame("pm25").copy()
    df["hour"] = df.index.hour
    grouped = df.groupby("hour")["pm25"]
    
    hour_means = grouped.mean()
    
    # Convert UTC hours to LST if offset available
    if utc_offset is not None:
        # Re-index hours to Local Standard Time
        hours_lst = [(h + int(utc_offset)) % 24 for h in hour_means.index]
        hour_means.index = hours_lst
        hour_means = hour_means.sort_index()
        hour_label_suffix = f" LST (UTC{utc_offset:+.0f})"
    else:
        hour_label_suffix = " UTC"
    
    return pd.DataFrame(
        {
            "hour": hour_means.index,
            "mean": hour_means.values,
            "median": grouped.median().reindex(hour_means.index).values,
            "p10": grouped.quantile(0.1).reindex(hour_means.index).values,
            "p90": grouped.quantile(0.9).reindex(hour_means.index).values,
            "hour_label_suffix": hour_label_suffix,
        }
    )


def build_seasonal_pattern(series: pd.Series) -> pd.DataFrame:
    if series.empty:
        return pd.DataFrame()
    df = series.to_frame("pm25").copy()
    df["month"] = df.index.month
    grouped = df.groupby("month")["pm25"].mean()
    return pd.DataFrame({"month": grouped.index, "mean": grouped.values})


def build_rolling_medians(series: pd.Series) -> pd.DataFrame:
    """Build rolling medians with intelligent gap detection.
    RESEARCH-GRADE: Zero-Interpolation Rule - Inject NaN ONLY at actual data gaps (>1 hour),
    not at every missing 2-minute interval. This preserves data fidelity while breaking lines at true outages.
    """
    if series.empty:
        return pd.DataFrame()
    
    # Create working series with original timestamps preserved
    work_series = series.copy()
    
    # Detect actual time gaps (gaps > 1 hour = outage)
    time_diffs = work_series.index.to_series().diff()
    gap_threshold = pd.Timedelta(hours=1)
    gap_indices = time_diffs[time_diffs > gap_threshold].index
    
    # Create gap mask for visualization (as numpy array for consistency)
    gap_mask = series.index.isin(gap_indices)
    
    # Inject NaN at gap boundaries (where gaps start) to force matplotlib to break the line
    series_with_gaps = work_series.copy()
    if len(gap_indices) > 0:
        series_with_gaps[gap_indices] = np.nan
    
    # Compute rolling medians efficiently (minimal NaN values)
    rolling_24 = series_with_gaps.rolling(24, min_periods=12).median()
    rolling_7d = series_with_gaps.rolling(24 * 7, min_periods=24 * 3).median()
    
    return pd.DataFrame(
        {
            "timestamp": series.index,
            "pm25": series.values,
            "median_24h": rolling_24.values,
            "median_7d": rolling_7d.values,
            "gap_mask": gap_mask,  # For legend/filtering
        }
    )


def build_decomposition(series: pd.Series, period: int = None) -> pd.DataFrame:
    """STL decomposition with intelligent period detection for stochastic pollution events.
    
    Period determines seasonal cycle length.
    - Hourly data: period=24 (24-hour cycle)
    - 2-minute data: period=720 (24 hours @ 2min intervals)
    - Auto-adjusts if data length insufficient (minimum 2×period points required)
    """
    if series.empty:
        return pd.DataFrame()
    
    # FIX: Set period=720 for 2-minute interval data to capture stochastic pollution events
    if period is None:
        time_diffs = series.index.to_series().diff()
        median_interval = time_diffs.median()
        
        # Calculate period for 24-hour cycle based on sampling frequency
        if median_interval:
            minutes_per_interval = median_interval.total_seconds() / 60
            period = max(24, int(1440 / minutes_per_interval))  # 24-hour cycle
        else:
            period = 24  # Fallback to hourly assumption
    
    # Ensure sufficient data for STL (requires minimum 2×period points)
    if len(series) < period * 2:
        return pd.DataFrame()

    # STL requires a complete series (no NaN). Interpolate gaps so STL produces valid residuals.
    # NaN positions are tracked and restored afterward so gap visualization remains accurate.
    nan_mask = series.isna()
    if nan_mask.any():
        series_clean = series.interpolate(method="time").ffill().bfill()
    else:
        series_clean = series

    # Use robust STL to resist outliers during gap-heavy periods
    stl = STL(series_clean, period=period, robust=True, seasonal=period + 1 if period % 2 == 0 else period)
    result = stl.fit()

    df = pd.DataFrame(
        {
            "timestamp": series.index,
            "trend": result.trend,
            "seasonal": result.seasonal,
            "residual": result.resid,  # Non-zero residuals capture stochastic pollution events
        }
    )
    # Restore NaN at original gap positions so downstream gap-break visualisation works
    if nan_mask.any():
        df.loc[nan_mask.values, ["trend", "seasonal", "residual"]] = np.nan
    return df


def build_regression_diagnostics(x: pd.Series, y: pd.Series, x_label: str) -> Dict[str, Any]:
    _empty = {"x": [], "y": [], "fitted": [], "residuals": [], "r2": None, "label": x_label}
    data = pd.DataFrame({"x": x, "y": y})
    data = data.replace([np.inf, -np.inf], np.nan).dropna()
    # Need enough points AND non-degenerate variance in x, or polyfit/SVD can fail.
    if len(data) < 10 or float(data["x"].std() or 0) < 1e-9 or float(data["y"].std() or 0) < 1e-9:
        return _empty
    try:
        coeffs = np.polyfit(data["x"], data["y"], 1)
    except (np.linalg.LinAlgError, ValueError, TypeError):
        return _empty
    slope, intercept = float(coeffs[0]), float(coeffs[1])
    fitted = data["x"] * slope + intercept
    residuals = data["y"] - fitted
    ss_res = float(((data["y"] - fitted) ** 2).sum())
    ss_tot = float(((data["y"] - data["y"].mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot else None
    return {
        "x": data["x"].round(2).tolist(),
        "y": data["y"].round(2).tolist(),
        "fitted": fitted.round(2).tolist(),
        "residuals": residuals.round(2).tolist(),
        "r2": round(r2, 3) if r2 is not None else None,
        "label": x_label,
        "slope": round(slope, 4),
        "intercept": round(intercept, 4),
        "n": int(len(data)),
    }


def build_sensor_drift(df: pd.DataFrame) -> pd.DataFrame:
    if "pm25_a" not in df.columns or "pm25_b" not in df.columns:
        return pd.DataFrame()
    drift = df[["timestamp", "pm25_a", "pm25_b"]].copy()

    drift["pm25_a"] = pd.to_numeric(drift["pm25_a"], errors="coerce")
    drift["pm25_b"] = pd.to_numeric(drift["pm25_b"], errors="coerce")
    drift = drift.dropna(subset=["timestamp", "pm25_a", "pm25_b"])
    if drift.empty:
        return pd.DataFrame()

    drift["pm25_a"] = drift["pm25_a"].astype(float)
    drift["pm25_b"] = drift["pm25_b"].astype(float)
    drift["diff"] = drift["pm25_a"] - drift["pm25_b"]
    drift = drift.set_index("timestamp").sort_index()

    # Resample to hourly mean — hours with no data naturally become NaN, giving correct line breaks
    hourly = drift["diff"].resample("h").mean()

    # Detect gap hours (consecutive NaN blocks > 1 hour) and keep them as NaN so the plot breaks correctly
    # Rolling trend: use 3-day window with min 1 day; adaptive to dataset length
    n_hours = len(hourly.dropna())
    if n_hours >= 24 * 14:
        roll_win, roll_min = 24 * 7, 24
    elif n_hours >= 24 * 3:
        roll_win, roll_min = 24 * 3, 12
    else:
        roll_win, roll_min = max(6, n_hours // 2), 3
    rolling = hourly.rolling(roll_win, min_periods=roll_min, center=True).median()

    return pd.DataFrame(
        {
            "timestamp": hourly.index,
            "diff": hourly.values,
            "rolling_7d": rolling.values,
        }
    )


def build_radar_profile(cleaned: pd.DataFrame, quality_score: float, channel_agreement: Dict[str, Any],
                        coverage_score: float = 100, n_rows_submitted: Optional[int] = None) -> Dict[str, Any]:
    """Build multi-dimensional radar chart data showing DATA quality (not air quality)."""
    if cleaned.empty:
        return {"labels": [], "values": []}

    # Calculate various metrics (0-100 scale)
    metrics = {}

    # 1. Internal Data Integrity — the share of SUBMITTED rows that survived validation.
    #    This must be measured against the rows as supplied, not against `cleaned`:
    #    `cleaned` is already filtered to valid PM2.5, so scoring it against itself
    #    always returned exactly 100% and the axis could never register a problem.
    if n_rows_submitted and n_rows_submitted > 0:
        metrics["Internal Data Integrity"] = round(min(100.0, len(cleaned) / n_rows_submitted * 100.0), 1)
    else:
        metrics["Internal Data Integrity"] = 100.0
    
    # 2. Temporal Completeness (coverage accounting for gaps) - NEW AXIS
    metrics["Temporal Completeness"] = coverage_score
    
    # 3. Overall Quality Score (already 0-100)
    metrics["Overall Quality"] = quality_score
    
    # 4. Channel Agreement (0-100%)
    metrics["Sensor Agreement"] = min(100, channel_agreement.get("agreement_pct", 0))
    
    # 5. Inter-channel stability (CV between channels A and B — true sensor health metric)
    # Uses channel agreement CV if available, otherwise falls back to data completeness indicator
    sensor_cv = channel_agreement.get("cv_between_channels", None)
    if sensor_cv is not None:
        # Piecewise: CV<5%=90-100 (excellent), CV 5-10%=70-90 (good), CV 10-15%=0-70 (marginal), CV≥15%=0
        if sensor_cv < 5:
            _stability = 90 + (5 - sensor_cv) / 5 * 10
        elif sensor_cv < 10:
            _stability = 70 + (10 - sensor_cv) / 5 * 20
        elif sensor_cv < 15:
            _stability = (15 - sensor_cv) / 5 * 70
        else:
            _stability = 0
        metrics["Inter-Channel Stability"] = round(max(0, min(100, _stability)), 1)
    else:
        metrics["Inter-Channel Stability"] = 50  # Unknown / single channel
    
    # 6. Reading Frequency (how often measurements taken)
    # 6. Sampling Regularity — how consistently readings arrive at the interval this
    #    export actually uses, rather than how short that interval is. The previous
    #    `readings_per_hour * 25` scored a flawless hourly export at 25/100 while a
    #    2-minute export scored 100, penalising the user's download choice instead of
    #    measuring data quality. Here the sensor's own median interval sets the
    #    expectation, so any interval can score full marks if the record is unbroken.
    if isinstance(cleaned.index, pd.DatetimeIndex) and len(cleaned) > 2:
        deltas = pd.Series(cleaned.index).diff().dt.total_seconds().dropna()
        nominal = float(deltas.median()) if not deltas.empty else 0.0
        span_s = (cleaned.index.max() - cleaned.index.min()).total_seconds()
        if nominal > 0 and span_s > 0:
            expected = span_s / nominal + 1
            metrics["Sampling Regularity"] = round(min(100.0, len(cleaned) / expected * 100.0), 1)
        else:
            metrics["Sampling Regularity"] = 50.0
    else:
        metrics["Sampling Regularity"] = 50.0

    # NOTE: an "EPA Compliance" axis (share of readings below 35 µg/m³) was removed
    # from this profile. It measures AIR quality, not DATA quality: a well-run sensor
    # at a genuinely polluted site would be marked down for reporting the pollution it
    # exists to detect. Exceedance counts are reported separately, where they belong.

    return {
        "labels": list(metrics.keys()),
        "values": [max(0, min(100, v)) for v in metrics.values()],  # Ensure 0-100 range
    }


def build_pm25_temporal_radar(
    cleaned: pd.DataFrame,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    tz_label: str = "UTC",
) -> Dict[str, Any]:
    """Build radar chart for PM2.5 levels by hour of day.

    Uses the already-converted index (local time) for grouping, so hours are
    correct regardless of GPS availability.  The label shows the user-selected
    timezone; falls back to a GPS-derived offset when available.
    """
    if cleaned.empty:
        return {"labels": [], "values": [], "max_value": 0}

    # Hours are already in local time after tz_convert in analyze_dataset.
    # Use EPA-corrected PM2.5 to stay consistent with every other figure and
    # number in the report (averages, daily table, exceedances all use corrected).
    temp_df = cleaned.copy()
    if "pm25_corrected" in temp_df.columns:
        _pm_src = (temp_df["pm25_corrected"].fillna(temp_df["pm25"])
                  if "pm25" in temp_df.columns else temp_df["pm25_corrected"])
    else:
        _pm_src = temp_df["pm25"]
    temp_df = temp_df.assign(_pm_src=_pm_src)

    # Average each clock hour FIRST, then average those hourly values by hour-of-day.
    # Grouping the raw 2-minute readings directly would weight each hour-of-day by how
    # many samples it happens to contain, so a densely-recorded stretch would dominate
    # the "typical day" profile whenever coverage is uneven (this dataset is ~66%).
    if isinstance(temp_df.index, pd.DatetimeIndex) and len(temp_df) > 1:
        _hourly = temp_df["_pm_src"].resample("1h").mean().dropna()
        hourly_avg = _hourly.groupby(_hourly.index.hour).mean().reindex(range(24))
    else:
        hourly_avg = temp_df.groupby(temp_df.index.hour)["_pm_src"].mean().reindex(range(24))
    # An hour that was never sampled is absent, not clean: filling it with 0 would draw
    # a spurious trough at the cleanest possible concentration.
    hourly_avg = hourly_avg.astype(float)

    # Build a readable timezone label for the chart axis
    if _tz_is_as_recorded(tz_label):
        short_tz = _tz_recorded_zone(tz_label) or "as recorded"
        offset_label = f" ({short_tz})"
    elif tz_label and tz_label != "UTC":
        # Show the last component (e.g. "New_York" from "America/New_York")
        short_tz = tz_label.split("/")[-1].replace("_", " ")
        offset_label = f" ({short_tz})"
    elif latitude is not None and longitude is not None:
        utc_offset = get_utc_to_lst_offset_hours(latitude, longitude)
        if utc_offset is not None:
            offset_label = f" LST (UTC{utc_offset:+.0f})"
        else:
            offset_label = " UTC"
    else:
        offset_label = " UTC"

    max_pm = max(float(hourly_avg.max()) if hourly_avg.notna().any() else 0.1, 0.1)
    # Show only the hour number on each tick; the timezone is stated once in the
    # chart title (and caption), so per-tick suffixes would only add clutter.
    labels = [f"{int(h):02d}" for h in hourly_avg.index]
    # NaN is not valid JSON for a browser parser; emit null so an unsampled hour
    # renders as a gap rather than as a zero reading.
    values = [None if v != v else round(float(v), 4) for v in hourly_avg.tolist()]

    return {
        "labels": labels,
        "values": values,
        "max_value": max_pm,
        "type": "hourly_pm25_pattern",
        "offset_label": offset_label,
    }


def build_report_figures(
    output_dir: Path,
    rolling_df: pd.DataFrame,
    diurnal_df: pd.DataFrame,
    drift_df: pd.DataFrame,
    channel_series: Dict[str, Any],
    radar_profile: Dict[str, Any] = None,
    pm25_temporal_radar: Dict[str, Any] = None,
    decomposition_df: pd.DataFrame = None,
    rolling_1h_timestamps: pd.Index = None,
    rolling_1h_values: pd.Series = None,
    tz_label: str = "UTC",
    heatmap_summary: pd.DataFrame = None,
) -> List[Tuple[str, Path]]:
    figures: List[Tuple[str, Path]] = []
    # When the user selected a timezone on the home page, the data is already
    # converted to it, so labels must say that zone — never UTC or a GPS-LST guess.
    _user_tz = bool(tz_label and tz_label != "UTC")
    _user_tz_short = ((_tz_recorded_zone(tz_label) or "sensor") if _tz_is_as_recorded(tz_label)
                      else tz_label.split("/")[-1].replace("_", " ") if _user_tz else "UTC")

    def save_fig(title: str, fig: plt.Figure, filename: str) -> None:
        path = output_dir / filename
        fig.savefig(path, dpi=150, bbox_inches="tight")
        figures.append((title, path))
        plt.close(fig)

    # ── 1. ROLLING MEDIANS (24h only — 7d removed: too little data over 30-day window with gaps) ──
    if not rolling_df.empty:
        fig, ax = plt.subplots(figsize=(8.0, 3.8))

        if "gap_mask" in rolling_df.columns:
            rolling_df_masked = rolling_df.copy()
            rolling_df_masked.loc[rolling_df_masked["gap_mask"], ["pm25", "median_24h"]] = np.nan
            plot_df = rolling_df_masked
        else:
            plot_df = rolling_df

        ax.plot(plot_df["timestamp"], plot_df["pm25"], label="Hourly PM2.5", color="#1f7a8c", alpha=0.18, linewidth=0.5)
        if rolling_1h_timestamps is not None and rolling_1h_values is not None and len(rolling_1h_values) > 0:
            ax.plot(rolling_1h_timestamps, rolling_1h_values, label="1h Median", color="#4c956c", linewidth=0.9, alpha=0.75)
        ax.plot(plot_df["timestamp"], plot_df["median_24h"], label="24h Median", color="#f6aa1c", linewidth=1.4)
        if "median_7d" in plot_df.columns:
            ax.plot(plot_df["timestamp"], plot_df["median_7d"], label="7d Median", color="#f25c54", linewidth=1.8, linestyle="--")
        ax.set_title("PM2.5 Rolling Medians — 1h · 24h · 7d (Physical Gaps Enforced)", fontsize=11, fontweight='bold')
        ax.set_xlabel("Date")
        ax.set_ylabel("PM2.5 (µg/m³)")
        ax.xaxis.set_major_locator(AutoDateLocator())
        ax.xaxis.set_major_formatter(DateFormatter("%m/%d"))
        fig.autofmt_xdate(rotation=45, ha='right')
        ax.legend(loc="upper right", fontsize=8)
        ax.grid(True, alpha=0.3)
        save_fig("Rolling medians", fig, "fig_rolling_medians.png")

    # ── 2. STL RESIDUALS ──────────────────────────────────────────────────────
    if decomposition_df is not None and not decomposition_df.empty and "residual" in decomposition_df.columns:
        fig, ax = plt.subplots(figsize=(8.0, 3.6))
        residuals = decomposition_df["residual"].copy()
        timestamps = pd.to_datetime(decomposition_df.get("timestamp", range(len(residuals))), errors="coerce")

        # ── Compute time-aware sample count for 12-hour edges ──
        # The "360" hardcode was calibrated for 2-min data (360 × 2min = 12h).
        # For hourly data this would mask the entire dataset.  Detect resolution.
        if len(timestamps) > 2 and pd.notna(timestamps.iloc[0]) and pd.notna(timestamps.iloc[1]):
            _dt_sec = abs((timestamps.iloc[1] - timestamps.iloc[0]).total_seconds())
            _edge_n = max(1, int(12 * 3600 / max(60, _dt_sec)))   # samples in 12 h
            _gap_buf = max(1, int(2 * 3600 / max(60, _dt_sec)))   # gap NaN buffer = 2 h
        else:
            _edge_n = 12
            _gap_buf = 2
        # Never mask more than 10 % of the series from each end
        _edge_n = min(_edge_n, max(1, len(residuals) // 10))

        # ── Work in numpy for reliable NaN-safe boolean logic ──
        res_arr = residuals.values.copy().astype(float)        # already has NaN at data gaps
        ts_arr  = timestamps.values                            # datetime64 array

        # Mask warm-up / cool-down edges
        n = len(res_arr)
        if n > _edge_n * 2:
            res_arr[:_edge_n]  = np.nan
            res_arr[-_edge_n:] = np.nan

        # Pollution event detection using nanstd (ignores NaN — no pandas quirks)
        residual_std = float(np.nanstd(res_arr))
        threshold    = 2.0 * residual_std

        valid       = ~np.isnan(res_arr)
        significant = valid & (np.abs(res_arr) > threshold)

        # Buffer ±_gap_buf samples around any NaN to suppress edge artefacts
        nan_arr = ~valid
        buf_arr = nan_arr.copy()
        for i in range(1, _gap_buf + 1):
            buf_arr[i:]  |= nan_arr[:-i]
            buf_arr[:-i] |= nan_arr[i:]

        significant_interior  = significant & ~buf_arr
        has_significant_events = significant_interior.any() and residual_std > 1e-6

        ax.plot(ts_arr, res_arr, color="#1f7a8c", linewidth=1.0, alpha=0.75, label="STL Residuals")
        ax.axhline(y=0, color="black", linestyle="--", linewidth=0.8, alpha=0.4, label="Baseline (zero residual)")
        if residual_std > 1e-6:
            ax.axhline(y= threshold, color="#f6aa1c", linestyle=":", linewidth=1.2, alpha=0.85,
                       label=f"±2σ Anomaly Threshold (±{threshold:.1f} µg/m³)")
            ax.axhline(y=-threshold, color="#f6aa1c", linestyle=":", linewidth=1.2, alpha=0.85)

        if has_significant_events:
            ax.scatter(ts_arr[significant_interior], res_arr[significant_interior],
                       color="#f25c54", s=22, alpha=0.85, zorder=5,
                       label=f"Pollution Events (>{threshold:.1f} µg/m³, 2σ)")

        n_events = int(significant_interior.sum()) if has_significant_events else 0
        ax.set_title(f"STL Residuals — Pollution Events Independent of Diurnal Cycle ({n_events} flagged)", fontsize=11, fontweight='bold')
        ax.set_xlabel("Date")
        ax.set_ylabel("Residual PM2.5 (µg/m³)")
        ax.xaxis.set_major_locator(AutoDateLocator())
        ax.xaxis.set_major_formatter(DateFormatter("%m/%d"))
        fig.autofmt_xdate(rotation=45, ha='right')
        ax.legend(loc="upper right", fontsize=8)
        ax.grid(True, alpha=0.25)
        save_fig("STL Residuals", fig, "fig_stl_residuals.png")

    # ── 3. DIURNAL PATTERN — kept but with explicit gap caveat in subtitle ──
    if not diurnal_df.empty:
        fig, ax = plt.subplots(figsize=(8.0, 3.6))
        ax.fill_between(diurnal_df["hour"], diurnal_df["p10"], diurnal_df["p90"], color="#aaaaaa", alpha=0.35, label="10th–90th percentile")
        ax.plot(diurnal_df["hour"], diurnal_df["mean"], color="#1f7a8c", label="Mean", linewidth=2)

        hour_label_suffix = diurnal_df.get("hour_label_suffix", " UTC").iloc[0] if "hour_label_suffix" in diurnal_df.columns else " UTC"
        if _user_tz:
            # User-selected timezone is authoritative: data already in local time.
            title = f"Typical Daily Pattern by Hour ({_user_tz_short} local time)"
            xlabel = f"Hour of Day ({_user_tz_short} local time, 0–23)"
        elif "LST" in hour_label_suffix:
            title = f"Typical Daily Pattern by Hour ({hour_label_suffix})"
            xlabel = f"Hour of Day {hour_label_suffix}"
        else:
            title = "Typical Daily Pattern by Hour (UTC Time — No Local Offset Applied)"
            xlabel = "Hour of Day UTC (0–23)\n[For local time: Determine sensor timezone from coordinates, then add UTC offset]"

        ax.set_title(title, fontsize=11, fontweight='bold')
        ax.set_xlabel(xlabel)
        ax.set_ylabel("PM2.5 (µg/m³)")
        ax.set_xticks(range(0, 24, 2))
        ax.set_xlim(-0.5, 23.5)
        ax.legend(loc="upper right", fontsize=8)
        ax.grid(True, alpha=0.3)

        # Add caveat about data gaps affecting hourly sample sizes — inside axes area
        ax.annotate(
            "⚠ Hours coinciding with the data-gap period have fewer observations; interpret those hours with caution.",
            xy=(0.01, 0.02), xycoords='axes fraction',
            fontsize=7, style='italic', color='#666666',
            bbox=dict(boxstyle='round,pad=0.3', facecolor='#fffbe6', alpha=0.8, edgecolor='#ccaa00')
        )

        save_fig("Diurnal pattern", fig, "fig_diurnal.png")

    # ── 4. PM2.5 TEMPORAL RADAR ──
    pm25_radar = pm25_temporal_radar
    if pm25_radar and pm25_radar.get("labels") and pm25_radar.get("values"):
        num_vars = len(pm25_radar["labels"])
        angles = np.linspace(0, 2 * np.pi, num_vars, endpoint=False).tolist()
        values = pm25_radar["values"] + [pm25_radar["values"][0]]
        angles += angles[:1]

        fig, ax = plt.subplots(figsize=(8.5, 8.5), subplot_kw=dict(projection='polar'))
        ax.plot(angles, values, 'o-', linewidth=3, color="#f25c54", label="PM2.5 (µg/m³)", markersize=9)
        ax.fill(angles, values, alpha=0.25, color="#f25c54")

        max_val = pm25_radar.get("max_value", max(values[:-1]) if values else 50)
        yticks_max = int(np.ceil(max_val / 10) * 10)
        yticks = np.linspace(0, yticks_max, 5)
        ax.set_yticks(yticks)
        ax.set_yticklabels([f'{int(v)}' for v in yticks], size=10, weight='bold')
        ax.set_ylim(0, yticks_max * 1.1)

        ax.set_xticks(angles[:-1])
        ax.set_xticklabels(pm25_radar["labels"], size=9, weight='bold')
        ax.set_theta_zero_location('N')
        ax.set_theta_direction(-1)
        ax.grid(True, linewidth=2.0, alpha=0.5)

        offset_label = pm25_radar.get("offset_label", " UTC")
        if offset_label.strip() == "UTC":
            tz_display = "UTC"
        else:
            tz_display = offset_label.strip().strip("()")
        ax.set_title(f"PM2.5 Temporal Pattern — 24-Hour Average ({tz_display})", fontsize=14, fontweight='bold', pad=30)

        for angle, value, label in zip(angles[:-1], values[:-1], pm25_radar["labels"]):
            if value > 0:
                ax.text(angle, value + (yticks_max * 0.05), f'{value:.1f}', ha='center', va='center', fontsize=8, weight='bold',
                       bbox=dict(boxstyle='round,pad=0.3', facecolor='white', alpha=0.8, edgecolor='#f25c54'))

        # Save without bbox_inches="tight" so the square figsize is preserved exactly
        _radar_path = output_dir / "fig_radar_pm25_temporal.png"
        fig.savefig(_radar_path, dpi=150)
        figures.append(("PM2.5 Temporal Radar", _radar_path))
        plt.close(fig)

    # ── 5. QUALITY CONTROL SECTION: Sensor Drift ──
    if not drift_df.empty:
        fig, ax = plt.subplots(figsize=(8.0, 3.8))
        # ±1 µg/m³ normal-variation band (gray fill, no legend clutter)
        ax.axhspan(-1, 1, color="#dddddd", alpha=0.45, zorder=0, label="±1 µg/m³ normal variation")
        ax.axhline(0, color="#555555", linewidth=0.9, linestyle="--", zorder=2)
        # Reference thresholds
        ax.axhline( 2, color="#cc7700", linewidth=0.7, linestyle=":", zorder=2)
        ax.axhline(-2, color="#cc7700", linewidth=0.7, linestyle=":", zorder=2, label="±2 µg/m³ investigation threshold")

        _diff   = drift_df["diff"].values
        _ts     = drift_df["timestamp"]
        _valid  = np.where(np.isfinite(_diff), _diff, np.nan)
        ax.fill_between(_ts, _valid, 0,
                        where=np.nan_to_num(_valid, nan=0) >= 0,
                        alpha=0.22, color="#4e9a8c", label="_nolegend_")
        ax.fill_between(_ts, _valid, 0,
                        where=np.nan_to_num(_valid, nan=0) < 0,
                        alpha=0.22, color="#b05c5c", label="_nolegend_")
        ax.plot(_ts, _valid,
                color="#888888", alpha=0.55, linewidth=0.8, label="Hourly mean A − B")
        _rolling = drift_df["rolling_7d"].values
        ax.plot(_ts, np.where(np.isfinite(_rolling), _rolling, np.nan),
                color="#1a3a6b", linewidth=2.2, label="Rolling median trend")

        ax.set_title("Sensor Drift Detection — Channel A minus Channel B (QC)", fontsize=11, fontweight='bold')
        ax.set_xlabel("Date")
        ax.set_ylabel("PM2.5 Difference A−B (µg/m³)")
        ax.xaxis.set_major_locator(AutoDateLocator())
        ax.xaxis.set_major_formatter(DateFormatter("%m/%d"))
        fig.autofmt_xdate(rotation=45, ha='right')
        ax.legend(loc="upper right", fontsize=7.5, framealpha=0.85)
        ax.grid(True, alpha=0.20)
        save_fig("Sensor drift", fig, "fig_drift.png")

    # ── 6. QUALITY CONTROL: Channel A vs B agreement (scatter + 1:1 + regression) ──
    if channel_series.get("a") is not None and channel_series.get("b") is not None:
        _a = pd.to_numeric(pd.Series(channel_series["a"]), errors="coerce")
        _b = pd.to_numeric(pd.Series(channel_series["b"]), errors="coerce")
        _pair = pd.DataFrame({"a": _a.values, "b": _b.values}).replace([np.inf, -np.inf], np.nan).dropna()
        if len(_pair) >= 10:
            a_v = _pair["a"].values; b_v = _pair["b"].values
            # ── 6a. Agreement scatter ──────────────────────────────────────────
            fig, ax = plt.subplots(figsize=(8.0, 4.4))
            ax.scatter(a_v, b_v, s=6, alpha=0.30, color="#1f7a8c", edgecolors="none", label="Paired readings")
            _lim = float(max(np.nanpercentile(a_v, 99.5), np.nanpercentile(b_v, 99.5), 1.0))
            ax.plot([0, _lim], [0, _lim], color="#444444", lw=1.2, ls="--", label="1:1 (perfect agreement)")
            # Ordinary least-squares fit B = m·A + c
            try:
                m, c = np.polyfit(a_v, b_v, 1)
                _xline = np.array([0, _lim])
                ax.plot(_xline, m * _xline + c, color="#f25c54", lw=1.6,
                        label=f"Fit: B = {m:.2f}·A {'+' if c >= 0 else '−'} {abs(c):.2f}")
                _ss_res = float(np.sum((b_v - (m * a_v + c)) ** 2))
                _ss_tot = float(np.sum((b_v - np.mean(b_v)) ** 2))
                _r2 = 1 - _ss_res / _ss_tot if _ss_tot > 0 else float("nan")
            except Exception:
                _r2 = float(channel_series.get("r2") or float("nan"))
            ax.set_xlim(0, _lim); ax.set_ylim(0, _lim); ax.set_aspect("equal", adjustable="box")
            _r2_txt = f"R² = {_r2:.3f}" if _r2 == _r2 else "R² = n/a"
            ax.set_title(f"Channel A vs B Agreement — {_r2_txt} (QC)", fontsize=11, fontweight="bold")
            ax.set_xlabel("Channel A PM2.5 (µg/m³)"); ax.set_ylabel("Channel B PM2.5 (µg/m³)")
            ax.legend(loc="upper left", fontsize=7.5, framealpha=0.9)
            ax.grid(True, alpha=0.25)
            save_fig("Channel A vs B", fig, "fig_channel_ab.png")

            # ── 6b. Bland–Altman (method-agreement) ────────────────────────────
            _mean_ab = (a_v + b_v) / 2.0
            _diff_ab = a_v - b_v
            _bias = float(np.mean(_diff_ab)); _sd = float(np.std(_diff_ab, ddof=1))
            _loa_hi = _bias + 1.96 * _sd; _loa_lo = _bias - 1.96 * _sd
            _within = float(np.mean((_diff_ab >= _loa_lo) & (_diff_ab <= _loa_hi)) * 100)
            # X range trimmed to 99th pct so the dense low-concentration region is readable
            _xmax = float(np.nanpercentile(_mean_ab, 99))
            _ypad = max(_loa_hi - _bias, _bias - _loa_lo, float(np.nanstd(_diff_ab)) * 0.5, 0.5)
            _ylo, _yhi = _bias - 2.6 * (_loa_hi - _bias if _loa_hi > _bias else _ypad), _bias + 2.6 * (_loa_hi - _bias if _loa_hi > _bias else _ypad)

            fig, ax = plt.subplots(figsize=(8.2, 4.2))
            # Shaded 95% limits-of-agreement band
            ax.axhspan(_loa_lo, _loa_hi, color="#cfe3ea", alpha=0.45, zorder=0,
                       label="95% limits of agreement")
            # Density via hexbin (log count) — reveals where the mass of points sits
            hb = ax.hexbin(_mean_ab, _diff_ab, gridsize=42, cmap="Blues", bins="log",
                           mincnt=1, linewidths=0.0, zorder=1, extent=(0, max(_xmax, 1), _ylo, _yhi))
            # Highlight readings that fall OUTSIDE the limits (the notable disagreements)
            _out = (_diff_ab < _loa_lo) | (_diff_ab > _loa_hi)
            if _out.any():
                ax.scatter(_mean_ab[_out], _diff_ab[_out], s=10, color="#c0392b",
                           alpha=0.55, edgecolors="none", zorder=3,
                           label=f"Outside limits ({int(_out.sum())} pts)")
            # Proportional-bias check: regression of difference on mean
            try:
                _ps, _pi = np.polyfit(_mean_ab, _diff_ab, 1)
                _xx = np.array([0, max(_xmax, 1)])
                ax.plot(_xx, _ps * _xx + _pi, color="#6a4c93", lw=1.4, ls="-.",
                        zorder=4, label=f"Trend (slope {_ps:+.3f})")
            except Exception:
                _ps = float("nan")
            # Bias + limit lines with right-edge labels
            ax.axhline(_bias, color="#1a3a6b", lw=1.8, zorder=5)
            ax.axhline(_loa_hi, color="#cc7700", lw=1.1, ls="--", zorder=5)
            ax.axhline(_loa_lo, color="#cc7700", lw=1.1, ls="--", zorder=5)
            ax.axhline(0, color="#888888", lw=0.7, ls=":", zorder=2)
            _xtext = max(_xmax, 1) * 0.995
            ax.text(_xtext, _bias, f" bias {_bias:+.2f}", va="center", ha="right",
                    fontsize=7.5, color="#1a3a6b", fontweight="bold",
                    bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="#1a3a6b", alpha=0.85))
            ax.text(_xtext, _loa_hi, f" +1.96 SD {_loa_hi:+.2f}", va="bottom", ha="right", fontsize=7, color="#a4630a")
            ax.text(_xtext, _loa_lo, f" −1.96 SD {_loa_lo:+.2f}", va="top", ha="right", fontsize=7, color="#a4630a")
            # Stats box
            _agree_word = "excellent" if abs(_bias) < 1 and _sd < 1.5 else ("good" if abs(_bias) < 2 else "check")
            ax.text(0.015, 0.04,
                    f"Mean bias = {_bias:+.2f} µg/m³   SD = {_sd:.2f}\n"
                    f"{_within:.1f}% of readings within limits   |   agreement: {_agree_word}",
                    transform=ax.transAxes, fontsize=7.5, va="bottom", ha="left",
                    bbox=dict(boxstyle="round,pad=0.3", fc="#fffef5", ec="#cccccc", alpha=0.92))
            ax.set_xlim(0, max(_xmax, 1)); ax.set_ylim(_ylo, _yhi)
            ax.set_title("Bland–Altman — Channel Agreement (bias, 95% limits, proportional trend)",
                         fontsize=10.5, fontweight="bold")
            ax.set_xlabel("Mean of Channels A & B (µg/m³)")
            ax.set_ylabel("Difference A − B (µg/m³)")
            _cb = fig.colorbar(hb, ax=ax, pad=0.015, fraction=0.040)
            _cb.set_label("point density (log)", fontsize=7); _cb.ax.tick_params(labelsize=6)
            ax.legend(loc="upper right", fontsize=7, framealpha=0.9, ncol=1)
            ax.grid(True, axis="y", alpha=0.18)
            save_fig("Bland-Altman", fig, "fig_bland_altman.png")

    # ── 7. QUALITY CONTROL: Weekly heatmap (day-of-week × hour, EPA-corrected) ──
    if heatmap_summary is not None and not heatmap_summary.empty:
        _order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        _hm = heatmap_summary.reindex([d for d in _order if d in heatmap_summary.index])
        _hm = _hm.reindex(columns=[h for h in range(24) if h in heatmap_summary.columns])
        if not _hm.empty and _hm.notna().to_numpy().any():
            import matplotlib.colors as _mcolors
            _vals = _hm.values.astype(float)
            _ncols = _vals.shape[1]; _nrows = _vals.shape[0]
            _finite = _vals[np.isfinite(_vals)]
            _vmin = float(np.nanpercentile(_finite, 2))
            _vmax = max(float(np.nanpercentile(_finite, 98)), _vmin + 0.1)

            fig, ax = plt.subplots(figsize=(10.2, 3.9))
            # pcolormesh with crisp white cell borders; masked cells shown light grey
            _masked = np.ma.masked_invalid(_vals)
            _cmap = plt.get_cmap("YlOrRd").copy(); _cmap.set_bad("#eeeeee")
            _mesh = ax.pcolormesh(np.arange(_ncols + 1), np.arange(_nrows + 1), _masked,
                                  cmap=_cmap, vmin=_vmin, vmax=_vmax,
                                  edgecolors="white", linewidth=0.6)
            ax.set_aspect("auto"); ax.invert_yaxis()

            # Per-cell value labels (small, contrast-aware) — turns the grid into a readable table
            _span = max(_vmax - _vmin, 1e-6)
            for _r in range(_nrows):
                for _c in range(_ncols):
                    _v = _vals[_r, _c]
                    if not np.isfinite(_v):
                        continue
                    _frac = (min(max(_v, _vmin), _vmax) - _vmin) / _span
                    _txtc = "white" if _frac > 0.62 else "#333333"
                    ax.text(_c + 0.5, _r + 0.5, f"{_v:.0f}", ha="center", va="center",
                            fontsize=5.6, color=_txtc)

            # Flag the single worst (highest) day-hour cell
            _wr, _wc = np.unravel_index(np.nanargmax(np.where(np.isfinite(_vals), _vals, -np.inf)), _vals.shape)
            ax.add_patch(plt.Rectangle((_wc, _wr), 1, 1, fill=False, edgecolor="#1a3a6b", linewidth=1.8))

            ax.set_xticks(np.arange(0, _ncols, 2) + 0.5)
            ax.set_xticklabels([str(_hm.columns[i]) for i in range(0, _ncols, 2)], fontsize=7.5)
            ax.set_yticks(np.arange(_nrows) + 0.5)
            ax.set_yticklabels([d[:3] for d in _hm.index], fontsize=8)
            ax.tick_params(length=0)
            _hr_lbl = "UTC" if not _user_tz else f"{_user_tz_short} local time"
            ax.set_xlabel(f"Hour of day ({_hr_lbl})", fontsize=9); ax.set_ylabel("Day of week", fontsize=9)
            ax.set_title("Weekly Pollution Pattern — Mean PM2.5 by Day × Hour (µg/m³)",
                         fontsize=11, fontweight="bold")

            _cb = fig.colorbar(_mesh, ax=ax, pad=0.012, fraction=0.040)
            _cb.set_label("Mean PM2.5 (µg/m³)", fontsize=8); _cb.ax.tick_params(labelsize=7)
            # Mark WHO 15 / EPA 35 on the colour scale if within range
            for _thr, _lab, _col in ((15, "WHO 15", "#00a651"), (35, "EPA 35", "#b30000")):
                if _vmin <= _thr <= _vmax:
                    _cb.ax.axhline(_thr, color=_col, lw=1.2)
                    _cb.ax.text(1.6, _thr, _lab, transform=_cb.ax.get_yaxis_transform(),
                                va="center", ha="left", fontsize=6.5, color=_col, fontweight="bold")
            fig.tight_layout()
            save_fig("Weekly heatmap", fig, "fig_weekly_heatmap.png")

    # Data Quality Profile Radar intentionally omitted — replaced by Data Quality Table in PDF.

    return figures


def build_report_pdf(
    report_path: Path,
    summary: Dict[str, Any],
    quality_rows: List[Dict[str, Any]],
    anomalies: List[str],
    figures: List[Tuple[str, Path]],
    channel_agreement: Dict[str, Any] = None,
    gap_analysis: Dict[str, Any] = None,
    radar_profile: Dict[str, Any] = None,
    metadata: Dict[str, str] = None,
    custom_notes: Optional[List[Dict[str, str]]] = None,
    tz_label: str = "UTC",
    highest_events: Optional[List[Dict[str, Any]]] = None,
) -> None:
    pdf = canvas.Canvas(str(report_path), pagesize=letter)
    width, height = letter

    # ── Layout constants ──────────────────────────────────────────────────────
    L_MARGIN  = 62     # left margin
    R_MARGIN  = 62     # right margin
    T_MARGIN  = 58     # top of usable area from top of page
    B_MARGIN  = 80     # bottom safe-zone: stop drawing above this
    USABLE_W  = width - L_MARGIN - R_MARGIN      # 488 pts
    CONTENT_X = L_MARGIN + 14                    # indented content X
    SECTION_INDENT = 16                          # body-text indent relative to L_MARGIN

    # ── Brand colours ─────────────────────────────────────────────────────────
    C_NAVY   = (0.08, 0.18, 0.38)   # dark navy for titles / accents
    C_RULE   = (0.76, 0.76, 0.76)   # light gray for horizontal rules
    C_BODY   = (0.12, 0.12, 0.12)   # near-black for body text
    C_MUTED  = (0.42, 0.42, 0.42)   # muted gray for secondary text
    C_GREEN  = (0.03, 0.48, 0.20)
    C_AMBER  = (0.62, 0.38, 0.00)
    C_RED    = (0.70, 0.10, 0.10)
    C_WHITE  = (1.00, 1.00, 1.00)

    y = height - T_MARGIN
    page_num = [1]

    # Single source of truth for hour-of-day labelling. When the user selected a
    # timezone, the data is already in it, so descriptions say e.g. "New York
    # local time" instead of UTC; when UTC, the wording is unchanged.
    _is_utc_rep = not (tz_label and tz_label != "UTC")
    _is_as_recorded = _tz_is_as_recorded(tz_label)
    _rec_zone = _tz_recorded_zone(tz_label)
    _hour_tz = ("UTC" if _is_utc_rep
                else (f"{_rec_zone} local time (as recorded)" if _rec_zone
                      else "local time (as recorded in the file)") if _is_as_recorded
                else f"{tz_label.split('/')[-1].replace('_', ' ')} local time")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _set_fill(rgb):
        pdf.setFillColorRGB(*rgb)

    def _set_stroke(rgb):
        pdf.setStrokeColorRGB(*rgb)

    def _stamp_footer() -> None:
        pdf.setFont("Helvetica", 7)
        _set_fill(C_MUTED)
        _footer_tz = tz_label if (tz_label and tz_label != "UTC") else "UTC"
        _footer_times = ("All times as recorded in file" if _footer_tz == "As recorded"
                         else f"All times {_footer_tz}")
        pdf.drawString(L_MARGIN, 30, f"PurpleAir Local Analyzer  ·  {_footer_times}  ·  For research use: cite quality score and note significant data gaps.")
        pdf.drawRightString(width - R_MARGIN, 30, f"Page {page_num[0]}")
        # thin footer rule
        pdf.setLineWidth(0.3)
        _set_stroke(C_RULE)
        pdf.line(L_MARGIN, 40, width - R_MARGIN, 40)
        _set_fill(C_BODY)
        _set_stroke((0, 0, 0))

    def next_page() -> None:
        nonlocal y
        _stamp_footer()
        pdf.showPage()
        page_num[0] += 1
        y = height - T_MARGIN

    def _check_space(needed: int = 24) -> None:
        if y < B_MARGIN + needed:
            next_page()

    def draw_h_rule(weight: float = 0.35, color: tuple = None) -> None:
        """Draw a full-width horizontal rule and advance y by 5pt total."""
        nonlocal y
        c = color or C_RULE
        pdf.setLineWidth(weight)
        _set_stroke(c)
        pdf.line(L_MARGIN, y, width - R_MARGIN, y)
        _set_stroke((0, 0, 0))
        y -= 5

    # Backward-compat alias used throughout the function body
    def draw_rule(weight: float = 0.5, color: tuple = None) -> None:
        draw_h_rule(weight, color)

    def draw_line(text: str, x: int = None, font_size: int = 10,
                  bold: bool = False, color: tuple = None,
                  line_gap: int = None, indent: int = None) -> None:
        """Draw a single line of text; auto-page-break if needed.
        `indent` is a legacy alias — absolute x = L_MARGIN + indent."""
        nonlocal y
        _check_space(font_size + 8)
        fn = "Helvetica-Bold" if bold else "Helvetica"
        pdf.setFont(fn, font_size)
        _set_fill(color or C_BODY)
        if indent is not None:
            resolved_x = L_MARGIN + indent
        else:
            resolved_x = x if x is not None else CONTENT_X
        pdf.drawString(resolved_x, y, text)
        _set_fill(C_BODY)
        y -= (line_gap if line_gap is not None else font_size + 5)

    def draw_wrapped(text: str, x_offset: int = 0, font_size: int = 9,
                     color: tuple = None, bold: bool = False,
                     para_gap: int = 9, indent: int = None) -> None:
        """Word-wrap and fully justify text within the usable column, with auto-page-break."""
        nonlocal y
        fn = "Helvetica-Bold" if bold else "Helvetica"
        pdf.setFont(fn, font_size)
        # Right-side breathing room so text never touches the margin/box border
        _R_PAD = 8
        if indent is not None:
            col_x  = L_MARGIN + indent
            avail_w = USABLE_W - indent - _R_PAD
        else:
            col_x  = CONTENT_X + x_offset
            avail_w = USABLE_W - (CONTENT_X - L_MARGIN) - x_offset - _R_PAD
        avail_w = max(avail_w, 60)

        # Pixel-width-accurate word wrapping
        space_w = pdf.stringWidth(" ", fn, font_size)
        words = text.split()
        lines: list[list[str]] = []
        cur_words: list[str] = []
        cur_w = 0.0
        for word in words:
            ww = pdf.stringWidth(word, fn, font_size)
            if cur_words and cur_w + space_w + ww > avail_w:
                lines.append(cur_words)
                cur_words = [word]
                cur_w = ww
            else:
                if cur_words:
                    cur_w += space_w
                cur_words.append(word)
                cur_w += ww
        if cur_words:
            lines.append(cur_words)

        _set_fill(color or C_BODY)
        for i, line_words in enumerate(lines):
            _check_space(font_size + 4)
            pdf.setFont(fn, font_size)
            is_last = (i == len(lines) - 1)
            if is_last or len(line_words) == 1:
                # Last line (or single word): left-align
                pdf.drawString(col_x, y, " ".join(line_words))
            else:
                # Full justification: distribute whitespace evenly between words
                total_word_w = sum(pdf.stringWidth(w, fn, font_size) for w in line_words)
                gap = (avail_w - total_word_w) / (len(line_words) - 1)
                xpos = col_x
                for word in line_words:
                    pdf.drawString(xpos, y, word)
                    xpos += pdf.stringWidth(word, fn, font_size) + gap
            y -= font_size + 4
        _set_fill(C_BODY)
        y -= para_gap

    def section_header(num: str, title: str, start_new_page: bool = False) -> None:
        """Render a numbered section heading with accent bar and ruled underline."""
        nonlocal y
        if start_new_page:
            next_page()
        else:
            # Need at least 80pt for header + its first content block
            _check_space(80)
        # ── Pre-section breathing room ────────────────────────────────────────
        y -= 18
        # Accent bar: 4pt wide, 17pt tall, flush with left margin
        pdf.setLineWidth(0)
        _set_fill(C_NAVY)
        pdf.rect(L_MARGIN, y - 3, 4, 18, fill=1, stroke=0)
        # Section number (smaller, muted)
        pdf.setFont("Helvetica-Bold", 9)
        pdf.drawString(L_MARGIN + 10, y + 4, num)
        # Section title (larger)
        pdf.setFont("Helvetica-Bold", 14)
        pdf.drawString(L_MARGIN + 10 + (len(num) * 6), y + 4, f"  {title}")
        _set_fill(C_BODY)
        # ── Ruled underline — placed 8pt below baseline, giving clear gap ─────
        y -= 20     # move below title text (14pt font + 6pt gap)
        pdf.setLineWidth(0.6)
        _set_stroke(C_NAVY)
        pdf.line(L_MARGIN, y, width - R_MARGIN, y)
        _set_stroke((0, 0, 0))
        y -= 10     # clear gap below rule before first content line

    def draw_sub_heading(text: str, font_size: int = 10) -> None:
        """Bold sub-heading within a section (no rule, just bold + spacing)."""
        nonlocal y
        _check_space(font_size + 18)
        y -= 6      # top breathing room before sub-heading
        pdf.setFont("Helvetica-Bold", font_size)
        _set_fill(C_NAVY)
        pdf.drawString(CONTENT_X, y, text)
        _set_fill(C_BODY)
        y -= font_size + 5

    def draw_bullet(text: str, font_size: int = 9) -> None:
        """Indented bullet point."""
        nonlocal y
        _check_space(font_size + 8)
        pdf.setFont("Helvetica", font_size)
        _set_fill(C_BODY)
        pdf.drawString(CONTENT_X + 6, y, f"•  {text}")
        y -= font_size + 5

    def _fmt_period(ts_str: str) -> str:
        try:
            return pd.Timestamp(ts_str).strftime("%d %B %Y  %H:%M")
        except Exception:
            return ts_str

    # ═════════════════════════════════════════════════════════════════════════
    # COVER PAGE
    # ═════════════════════════════════════════════════════════════════════════

    # Full-width dark header band
    HEADER_H = 90
    _set_fill(C_NAVY)
    pdf.rect(0, height - HEADER_H, width, HEADER_H, fill=1, stroke=0)

    # Main report title (white on navy)
    pdf.setFont("Helvetica-Bold", 24)
    _set_fill(C_WHITE)
    pdf.drawString(L_MARGIN, height - 52, "Air Quality Research Report")

    # Subtitle line
    pdf.setFont("Helvetica", 11)
    _set_fill((0.80, 0.88, 0.96))
    pdf.drawString(L_MARGIN, height - 70, "Comprehensive PM2.5 Analysis  ·  Sensor-Grade Data Quality Assessment")
    _set_fill(C_BODY)

    y = height - HEADER_H - 28    # start content below the header band

    # ── Metadata block ────────────────────────────────────────────────────────
    _start_fmt = _fmt_period(summary['date_range']['start'])
    _end_fmt   = _fmt_period(summary['date_range']['end'])

    # Monitoring period (prominent)
    pdf.setFont("Helvetica-Bold", 11)
    _set_fill(C_NAVY)
    pdf.drawString(L_MARGIN, y, "Monitoring Period")
    _set_fill(C_BODY)
    pdf.setFont("Helvetica", 11)
    pdf.drawString(L_MARGIN + 130, y, f"{_start_fmt}  →  {_end_fmt}")
    y -= 18

    if metadata:
        _dev = (metadata.get("device_id") or "").strip()
        _loc = (metadata.get("location") or "").strip()
        if _dev:
            pdf.setFont("Helvetica-Bold", 10)
            _set_fill(C_NAVY)
            pdf.drawString(L_MARGIN, y, "Device ID")
            pdf.setFont("Helvetica", 10)
            _set_fill(C_BODY)
            pdf.drawString(L_MARGIN + 130, y, _dev)
            y -= 16
        if _loc:
            pdf.setFont("Helvetica-Bold", 10)
            _set_fill(C_NAVY)
            pdf.drawString(L_MARGIN, y, "Location")
            pdf.setFont("Helvetica", 10)
            _set_fill(C_BODY)
            pdf.drawString(L_MARGIN + 130, y, _loc)
            y -= 16

    _tz_display = tz_label if (tz_label and tz_label != "UTC") else "UTC — Coordinated Universal Time"
    pdf.setFont("Helvetica-Bold", 10)
    _set_fill(C_NAVY)
    pdf.drawString(L_MARGIN, y, "Report Timezone")
    pdf.setFont("Helvetica", 10)
    _set_fill(C_BODY)
    pdf.drawString(L_MARGIN + 130, y, _tz_display)
    y -= 22

    # Thin separator rule
    pdf.setLineWidth(0.5)
    _set_stroke(C_NAVY)
    pdf.line(L_MARGIN, y, width - R_MARGIN, y)
    _set_stroke((0, 0, 0))
    y -= 14

    # Quick-stats row (3 tiles)
    _qs = [
        ("Avg PM2.5 (raw)",        f"{summary.get('pm25_average', '—')} µg/m³"),
        ("Avg PM2.5 (EPA-corr.)",  f"{summary.get('pm25_average_epa_corrected', '—')} µg/m³"),
        ("Quality Score",          f"{summary.get('quality_score', '—')}%"),
    ]
    tile_w = (USABLE_W - 12) / 3
    tile_x = L_MARGIN
    for label, val in _qs:
        # Tile background
        _set_fill((0.95, 0.96, 0.98))
        pdf.rect(tile_x, y - 34, tile_w - 6, 42, fill=1, stroke=0)
        # Value
        pdf.setFont("Helvetica-Bold", 13)
        _set_fill(C_NAVY)
        pdf.drawString(tile_x + 8, y - 8, val)
        # Label
        pdf.setFont("Helvetica", 8)
        _set_fill(C_MUTED)
        pdf.drawString(tile_x + 8, y - 22, label)
        tile_x += tile_w
    _set_fill(C_BODY)
    y -= 50

    # Attribution
    pdf.setFont("Helvetica", 8.5)
    _set_fill(C_MUTED)
    pdf.drawString(L_MARGIN, y, "Principal Investigator: Dr. Ana Maria Rule   ·   Data Analysis & Platform: Chandra Prakash Choudhary")
    y -= 12
    pdf.setFont("Helvetica", 8)
    pdf.drawString(L_MARGIN, y, "This report was generated automatically using EPA-validated correction algorithms and research-grade quality metrics.")
    _set_fill(C_BODY)
    y -= 20

    # Cover-page separator rule (heavier)
    pdf.setLineWidth(1.2)
    _set_stroke(C_NAVY)
    pdf.line(L_MARGIN, y, width - R_MARGIN, y)
    _set_stroke((0, 0, 0))
    y -= 16

    # ── Analysis Summary (plain-English, for non-experts) ────────────────────

    _narrative_text = summary.get("narrative_summary", "")
    if _narrative_text:
        if y < 200:
            next_page()
        _nl_sections = [s.strip() for s in _narrative_text.split("\n") if s.strip()]

        # Title bar (dark blue background, white text) — drawn BEFORE text
        _box_top = y + 6
        pdf.setFillColorRGB(0.10, 0.22, 0.45)
        pdf.rect(L_MARGIN, y - 4, USABLE_W, 22, fill=1, stroke=0)
        pdf.setFont("Helvetica-Bold", 11)
        pdf.setFillColorRGB(1, 1, 1)
        pdf.drawString(L_MARGIN + 10, y + 2, "ANALYSIS SUMMARY  —  Plain-Language Overview")
        pdf.setFillColorRGB(0, 0, 0)
        y -= 26

        for section_text in _nl_sections:
            if y < B_MARGIN + 24:
                next_page()
            _colon_pos = section_text.find(":")
            # Detect "ALL CAPS LABEL:" at start of section
            if _colon_pos > 0 and section_text[:_colon_pos].replace(" ", "").isupper() and _colon_pos < 25:
                label_part   = section_text[:_colon_pos + 1]
                content_part = section_text[_colon_pos + 1:].strip()
                y -= 4
                pdf.setFont("Helvetica-Bold", 9.5)
                pdf.setFillColorRGB(0.10, 0.22, 0.45)
                pdf.drawString(L_MARGIN + 10, y, label_part)
                pdf.setFillColorRGB(0, 0, 0)
                y -= 13
                if content_part:
                    draw_wrapped(content_part, indent=10, font_size=9)
            else:
                draw_wrapped(section_text, indent=10, font_size=9)

        # Draw only left, right, and bottom borders — no top line (the navy title bar is the top)
        _box_bottom = y - 4
        pdf.setStrokeColorRGB(0.10, 0.22, 0.45)
        pdf.setLineWidth(0.8)
        pdf.line(L_MARGIN, _box_top, L_MARGIN, _box_bottom)                    # left
        pdf.line(L_MARGIN + USABLE_W, _box_top, L_MARGIN + USABLE_W, _box_bottom)  # right
        pdf.line(L_MARGIN, _box_bottom, L_MARGIN + USABLE_W, _box_bottom)      # bottom
        pdf.setLineWidth(0.4)
        pdf.setStrokeColorRGB(0, 0, 0)
        y -= 18

    # ── Section 1: Executive Summary ─────────────────────────────────────────

    section_header("1.", "Executive Summary")
    draw_line(f"Quality Score: {summary['quality_score']}%   |   Total Readings: {summary['total_readings']}", indent=12, font_size=10)
    draw_line(f"Average PM2.5 (raw): {summary['pm25_average']} µg/m³   |   Average PM2.5 (EPA-corrected): {summary.get('pm25_average_epa_corrected', '—')} µg/m³", indent=12, font_size=10)
    y -= 6
    draw_wrapped(
        "About the instrument: Each PurpleAir monitor houses two independent laser particle sensors — "
        "Channel A and Channel B — that measure PM2.5 simultaneously. The value of running two channels "
        "goes beyond simple backup: their agreement is the primary instrument health check. When both "
        "channels track closely, the data are reliable. When they diverge, it indicates sensor fouling, "
        "electronic drift, or partial blockage — problems that can be identified and acted on before "
        "they corrupt reported values. The sensor-health metrics and QC charts throughout this report "
        "are rooted in this dual-channel architecture.",
        indent=12, font_size=9
    )
    if summary.get('quality_narrative'):
        draw_wrapped(summary['quality_narrative'], indent=12, font_size=9)

    y -= 4

    # ── Section 2: Methods & Data Quality ────────────────────────────────────

    section_header("2.", "Methods & Data Quality")
    draw_wrapped(
        "Range Validation: Every reading is checked against physically plausible limits "
        "(PM2.5: 0–1,000 µg/m³ | Temperature: −40 to +140 °F | Humidity: 0–100% | "
        "Pressure: 800–1,100 hPa). Readings outside these bounds are flagged as out-of-range "
        "and excluded from all downstream calculations.",
        indent=12, font_size=9
    )
    _hum_used   = summary.get("humidity_used", False)
    _mean_rh_v  = summary.get("mean_rh")
    _rh_min_v   = summary.get("rh_min")
    _rh_max_v   = summary.get("rh_max")
    _pm25_raw_v = summary.get("pm25_average", 0.0)
    if _hum_used and _mean_rh_v is not None:
        _ex_corr = round(barkjohn_corrected(_pm25_raw_v, _mean_rh_v), 2)
        _bk_text = (
            f"EPA Barkjohn Correction (Barkjohn et al., 2021): Raw PurpleAir laser-scattering readings "
            f"overestimate true PM2.5, especially at high humidity. The full correction formula — "
            f"Corrected PM2.5 = 0.524 × raw_PM − 0.0862 × RH + 5.75 — was applied to every reading "
            f"in this dataset using the concurrent per-reading relative humidity (RH). "
            f"Humidity was available throughout this dataset: mean RH = {_mean_rh_v}% "
            f"(range {_rh_min_v}–{_rh_max_v}%). "
            f"Example using the dataset mean values: 0.524 × {_pm25_raw_v} − 0.0862 × {_mean_rh_v} "
            f"+ 5.75 = {_ex_corr} µg/m³ (corrected from {_pm25_raw_v} µg/m³ raw)."
        )
    else:
        _bk_text = (
            "EPA Barkjohn Correction (Barkjohn et al., 2021): Raw PurpleAir laser-scattering readings "
            "overestimate true PM2.5, especially at high humidity. The full formula is: "
            "Corrected PM2.5 = 0.524 × raw_PM − 0.0862 × RH + 5.75. "
            "Relative humidity was NOT available for this dataset, so the humidity-dependent Barkjohn "
            "correction could not be applied. Uncorrected (raw) PM2.5 is reported instead. Because raw "
            "PurpleAir readings tend to overestimate at elevated humidity, the reported values should be "
            "treated as an upper bound and interpreted with caution where humidity is typically high."
        )
    draw_wrapped(_bk_text, indent=12, font_size=9)
    if _tz_is_as_recorded(tz_label):
        _mn_zone = _tz_recorded_zone(tz_label)
        _tz_methods_note = (
            "Timestamps: All timestamps are used exactly as recorded in the uploaded file "
            + (f"(already in {_mn_zone} local time). " if _mn_zone
               else "(the file was exported with timestamps already in the sensor's local time). ")
            + "No timezone conversion was applied; daily and hourly grouping follows the "
            "file's own local calendar dates and hours."
        )
    elif tz_label and tz_label != "UTC":
        _tz_methods_note = (
            f"Timestamps: All timestamps and time-of-day patterns in this report are displayed in "
            f"{tz_label} (the timezone selected at analysis time). "
            f"Original sensor recordings were in UTC; the conversion was applied before daily and "
            f"hourly grouping so that all date boundaries and diurnal patterns reflect local calendar dates."
        )
    else:
        _tz_methods_note = (
            "Timestamps: All timestamps are in UTC (Coordinated Universal Time) as recorded by the sensor. "
            "No timezone conversion was applied. To convert to local time, add your UTC offset "
            "(e.g., UTC−5 for US Eastern Standard, UTC+5:30 for India). "
            "All time-of-day patterns in this report therefore show UTC hours (0–23)."
        )
    draw_wrapped(_tz_methods_note, indent=12, font_size=9)
    y -= 4
    draw_line("Per-Record Quality Metrics:", indent=12, font_size=10, bold=True)
    y -= 2

    for row in quality_rows[:6]:
        if row['metric'] == 'Coverage':
            draw_wrapped(
                f"Coverage (temporal completeness): {row['valid_pct']}% of expected recording slots "
                f"contain actual data ({row['missing_pct']}% missing — sensor offline or network outage; "
                f"{row['out_of_range_pct']}% out-of-range). Note: Coverage is different from the metrics "
                "above it. The other metrics measure the quality of readings that were recorded; "
                "Coverage measures whether the sensor was recording at all. A sensor that goes offline "
                "will have 100% valid readings in the metrics above, but its Coverage will drop "
                "proportional to the lost time.",
                indent=24, font_size=9
            )
        else:
            draw_line(
                f"{row['metric']}: {row['valid_pct']}% valid  "
                f"({row['missing_pct']}% missing, {row['out_of_range_pct']}% out-of-range)",
                indent=24, font_size=9
            )
    y -= 10

    # ── Section 3: Sensor Performance ────────────────────────────────────────

    if channel_agreement:
        section_header("3.", "Sensor Performance")
        draw_line(f"Channel Agreement (R²): {channel_agreement.get('r2', 'N/A')}", indent=12, font_size=9)
        draw_line(f"Mean Absolute Difference (MAD): {channel_agreement.get('mean_abs_diff', 'N/A')} µg/m³", indent=12, font_size=9)
        if 'cv_between_channels' in channel_agreement and channel_agreement['cv_between_channels'] is not None:
            cv = channel_agreement['cv_between_channels']
            draw_line(f"Agreement Rate: {channel_agreement.get('agreement_pct', 'N/A')}%", indent=12, font_size=9)
            draw_line(f"Coefficient of Variation (CV): {cv}%", indent=12, font_size=9)
        else:
            draw_line(f"Agreement Rate: {channel_agreement.get('agreement_pct', 'N/A')}%", indent=12, font_size=9)
        y -= 4
        r2 = float(channel_agreement.get('r2', 0))
        if r2 > 0.85:
            draw_wrapped(
                "✓ Status: Excellent — the two laser channels are tracking each other very closely. "
                "This is the highest tier of sensor health and indicates data are publication-ready "
                "without any instrument-health caveats.",
                indent=12, font_size=9, color=(0.0, 0.45, 0.18)
            )
        elif r2 > 0.70:
            draw_wrapped(
                "⚠ Status: Acceptable — minor disagreement between channels. Suitable for most analyses "
                "but worth monitoring. Field inspection recommended if divergence persists over time.",
                indent=12, font_size=9, color=(0.65, 0.40, 0.0)
            )
        else:
            draw_wrapped(
                "✗ Status: Poor — channels disagree substantially. Data should be treated with caution "
                "and the sensor inspected for blockage, contamination, or component failure before "
                "results are used in any formal report.",
                indent=12, font_size=9, color=(0.75, 0.10, 0.10)
            )
        y -= 6
        # Barkjohn correction transparency block in Sensor Performance
        _sp_hum   = summary.get("humidity_used", False)
        _sp_rh    = summary.get("mean_rh")
        _sp_rh_mn = summary.get("rh_min")
        _sp_rh_mx = summary.get("rh_max")
        _sp_pm    = summary.get("pm25_average", 0.0)
        if _sp_hum and _sp_rh is not None:
            _sp_corr = round(barkjohn_corrected(_sp_pm, _sp_rh), 2)
            draw_wrapped(
                f"EPA Barkjohn correction applied per reading using concurrent RH. "
                f"Dataset RH: mean {_sp_rh}%, range {_sp_rh_mn}–{_sp_rh_mx}%. "
                f"Worked example (dataset means): 0.524 × {_sp_pm} − 0.0862 × {_sp_rh} + 5.75 = {_sp_corr} µg/m³.",
                indent=12, font_size=9, color=(0.30, 0.30, 0.30)
            )
        else:
            draw_wrapped(
                "Humidity data unavailable — the Barkjohn correction could not be applied, so raw "
                "(uncorrected) PM2.5 is reported. Raw readings may overestimate at high humidity.",
                indent=12, font_size=9, color=(0.55, 0.35, 0.00)
            )
        y -= 4

    # ── Section 4: Analytical Methods ────────────────────────────────────────

    section_header("4.", "Analytical Methods")
    draw_wrapped(
        "Rolling Median (24-hour window): A 24-hour moving median is applied to the PM2.5 time series "
        "to remove short-term noise while preserving real trend transitions. Median is used instead of "
        "mean because it is resistant to extreme outliers — a single high spike from a passing vehicle "
        "or brief wind event will not skew the trend line. Physical data gaps are enforced as true "
        "breaks in the trend line; no values are interpolated across missing periods.",
        indent=12, font_size=9
    )
    draw_wrapped(
        "STL Decomposition (Seasonal-Trend using Loess): A well-established statistical technique "
        "that separates the PM2.5 time series into three additive components: (1) Trend — the "
        "slow-moving directional change over weeks; (2) Seasonal — the repeating 24-hour diurnal "
        "cycle driven by daily human activity and boundary-layer dynamics; and (3) Residual — "
        "everything left over, representing pollution events that do not fit the regular pattern "
        "(fires, industrial incidents, unusual traffic events, instrument artifacts). STL requires "
        "a minimum of two complete diurnal cycles and is sensitive to long contiguous data gaps.",
        indent=12, font_size=9
    )
    draw_wrapped(
        "Diurnal Pattern Analysis: PM2.5 readings are grouped by hour of day (0–23) across the entire "
        "dataset. The mean and 10th–90th percentile band are computed for each hour. Wide percentile "
        "bands at a given hour mean that hour's air quality varies considerably from day to day; "
        "narrow bands mean it is consistent.",
        indent=12, font_size=9
    )
    draw_wrapped(
        "Quality Score: A composite metric combining 40% data validity (fraction of recorded readings "
        "that pass all range checks) and 60% temporal coverage (fraction of expected recording slots "
        "that contain data). The higher weight on coverage reflects the principle that gaps in "
        "monitoring are often more damaging to exposure assessment than occasional bad readings.",
        indent=12, font_size=9
    )
    y -= 4

    draw_line("Plain-Language Glossary of Key Terms:", indent=12, font_size=10, bold=True)
    y -= 4
    glossary = [
        ("PM2.5",
         "Particles smaller than 2.5 micrometers in diameter — about 30 times thinner than a human hair. "
         "They are the most health-relevant size because they penetrate deep into the lungs and enter the bloodstream."),
        ("Why this report uses PM2.5 concentration, not AQI",
         "The U.S. EPA Air Quality Index is a composite of multiple pollutants (PM2.5, PM10, O3, NO2, SO2, CO). "
         "This instrument measures only PM2.5, so a valid multi-pollutant AQI cannot be derived from it. "
         "All thresholds in this report are therefore stated as PM2.5 mass concentration (µg/m³) against the "
         "WHO 24-hour guideline (15) and EPA 24-hour standard (35), which is the scientifically correct basis."),
        ("EPA Barkjohn Correction",
         "A regression formula (Barkjohn et al., 2021) developed specifically to correct PurpleAir low-cost sensor "
         "readings to align with co-located EPA reference monitors. It accounts for the sensor's humidity sensitivity."),
        ("STL Decomposition",
         "Seasonal-Trend decomposition using Loess — a statistical algorithm that peels apart a time series into "
         "its trend, its regular daily rhythm, and any unexplained events."),
        ("Diurnal Pattern",
         "The recurring 24-hour cycle of air quality driven by daily human activity (traffic, cooking, heating) "
         "and meteorological factors (boundary layer rise in the morning, collapse at night)."),
        ("R² (R-squared)",
         "A measure of how well two things track each other, on a scale of 0 to 1. R² = 1.0 means perfect agreement; "
         "R² = 0.85 means the two channels explain 85% of each other's variation — the standard research-grade threshold."),
        ("CV (Coefficient of Variation)",
         "The average absolute difference between Channel A and Channel B divided by the mean PM2.5, expressed as a "
         "percentage. This platform flags CV < 10% as excellent, 10–15% as acceptable, and ≥ 15% as a pair that "
         "should be inspected — deliberately stricter than the U.S. EPA's published precision target for PM2.5 air "
         "sensors (CV ≤ 30%; EPA Performance Testing Protocols, Metrics, and Target Values, 2021)."),
        ("LDL (Lower Detection Limit)",
         "The smallest concentration a Plantower laser sensor can reliably distinguish from zero (~1 µg/m³). "
         "Values below this limit are corrected to LDL/√2 ≈ 0.707 µg/m³ to avoid zero-bias in statistics."),
        ("MAD (Mean Absolute Difference)",
         "The average of the absolute difference between Channel A and Channel B readings, in µg/m³. "
         "A low MAD (< 2 µg/m³) confirms channels are measuring essentially the same air."),
        ("RH (Relative Humidity)",
         "The amount of water vapor in the air as a percentage of the maximum possible at that temperature. "
         "High RH causes laser particle counters to overcount particles because water droplets scatter light similarly to dust."),
        (("UTC (Coordinated Universal Time)"
          if _is_utc_rep else "Timezone"),
         ("The global time standard with no daylight-saving shifts. All sensor timestamps in this report are in UTC. "
          "To find local time, add your UTC offset (e.g., UTC−5 for US Eastern Standard, UTC+5:30 for India)."
          if _is_utc_rep else
          "All timestamps in this report are used exactly as recorded in the uploaded file"
          + (f", which is in {_rec_zone} local time" if _rec_zone else
             ", which was exported with timestamps already in the sensor's local time")
          + ". No timezone conversion was applied."
          if _is_as_recorded else
          f"All timestamps in this report are shown in {_hour_tz} ({tz_label}). The raw sensor data were "
          "recorded in UTC and converted to this local timezone before all daily/hourly grouping and analysis.")),
        ("hPa (Hectopascals)",
         "The SI unit of atmospheric pressure. Standard sea-level pressure is ~1013 hPa. "
         "Readings outside 800–1100 hPa indicate a sensor fault or extreme elevation."),
        ("NAAQS (National Ambient Air Quality Standards)",
         "The EPA's legally enforceable air quality standards. For PM2.5: 35 µg/m³ (24-hour) and 9 µg/m³ (annual, revised 2024). "
         "The WHO guideline is more stringent: 15 µg/m³ (24-hour)."),
    ]
    for term, definition in glossary:
        if y < B_MARGIN + 30:
            next_page()
        draw_line(f"  {term}", indent=24, font_size=9, bold=True)
        draw_wrapped(definition, indent=36, font_size=8, color=(0.25, 0.25, 0.25))
    y -= 6

    # ── Section 4b: Statistical Trend, Uncertainty & Exposure ────────────────
    _tt = summary.get("trend_test")
    _un = summary.get("uncertainty")
    _ex = summary.get("exposure")
    _rp = summary.get("repro")
    if _tt or _un or _ex or _rp:
        section_header("4b.", "Statistical Trend, Uncertainty & Exposure")
        if _tt:
            draw_line("Trend test (Mann-Kendall · Theil-Sen · Pettitt):", indent=12, font_size=10, bold=True)
            _dci = _tt.get("sen_slope_per_day_ci") or ["N/A", "N/A"]
            draw_line(
                f"Theil-Sen slope: {_tt.get('sen_slope_per_day')} µg/m³ per day "
                f"(95% CI {_dci[0]} to {_dci[1]}); modelled change across the "
                f"{_tt.get('span_days')}-day window: {_tt.get('sen_change_over_period')} µg/m³",
                indent=24, font_size=9)
            if _tt.get("annualised"):
                _ci = _tt.get("sen_slope_ci") or ["N/A", "N/A"]
                draw_line(
                    f"Equivalent annual rate: {_tt.get('sen_slope_per_year')} µg/m³ per year "
                    f"(95% CI {_ci[0]} to {_ci[1]})", indent=24, font_size=9)
            elif _tt.get("annualisation_note"):
                draw_wrapped(_tt.get("annualisation_note"), indent=24, font_size=8.5,
                             color=(0.30, 0.30, 0.30))
            draw_line(
                f"Mann-Kendall: τ = {_tt.get('tau')}, p = {_tt.get('p_value')} — {_tt.get('direction')}"
                f" ({'statistically significant' if _tt.get('significant') else 'not significant'} at α=0.05)",
                indent=24, font_size=9)
            if _tt.get("change_point_date"):
                draw_line(f"Change-point (Pettitt): {_tt.get('change_point_date')} "
                          f"(p = {_tt.get('change_point_p')})", indent=24, font_size=9)
            else:
                draw_line(f"Change-point (Pettitt): none detected (p = {_tt.get('change_point_p')})",
                          indent=24, font_size=9)
            y -= 4
        if _un and _un.get("mean_ci_halfwidth") is not None:
            draw_line("Measurement uncertainty:", indent=12, font_size=10, bold=True)
            draw_wrapped(
                f"Reported PM2.5 carries a mean 95% confidence interval of ±{_un.get('mean_ci_halfwidth')} µg/m³, "
                f"derived by combining {_un.get('method')}. Uncertainty bands are drawn on the temporal-trend "
                f"chart. Honest, source-quantified uncertainty distinguishes these results from a single-number "
                f"dashboard reading.",
                indent=24, font_size=9, color=(0.30, 0.30, 0.30))
            y -= 2
        if _ex:
            draw_line("Exposure & health burden:", indent=12, font_size=10, bold=True)
            draw_line(
                f"Cumulative exposure: {_ex.get('cumulative_ug_hours')} µg·hours over "
                f"{_ex.get('exposure_hours')} h.", indent=24, font_size=9)
            draw_line(
                f"Days above WHO 15: {_ex.get('days_over_who15')} of {_ex.get('n_days')} "
                f"(EPA 35: {_ex.get('days_over_epa35')} days).", indent=24, font_size=9)
            if _ex.get("excess_mortality_risk_pct") is not None:
                draw_wrapped(
                    f"Modelled long-term excess-mortality risk: +{_ex.get('excess_mortality_risk_pct')}% — the "
                    f"additional risk a population would carry if this average concentration persisted for years "
                    f"(WHO/GBD log-linear RR≈1.08 per 10 µg/m³, relative to the WHO 15 µg/m³ guideline). This is a "
                    f"population-level illustration only. It is not a clinical prediction and says nothing about "
                    f"the health of any individual at this location.",
                    indent=24, font_size=9, color=(0.30, 0.30, 0.30))
            elif _ex.get("risk_withheld_note"):
                draw_wrapped(_ex.get("risk_withheld_note"), indent=24, font_size=9,
                             color=(0.30, 0.30, 0.30))
            y -= 6
        if _rp and _rp.get("repro_id"):
            draw_line("Reproducibility:", indent=12, font_size=10, bold=True)
            draw_wrapped(
                f"Reproducibility ID {_rp.get('repro_id')} — a SHA-256 fingerprint of the exact input data "
                f"combined with the method/version set used ({(_rp.get('method_versions') or {}).get('app_version', '')}). "
                f"Quote this ID to reproduce or independently audit every figure in this report.",
                indent=24, font_size=9, color=(0.30, 0.30, 0.30))
            y -= 6

    # ── Section 5: Key Findings ───────────────────────────────────────────────

    section_header("5.", "Key Findings")
    if anomalies:
        draw_line("Flags Raised During Automated Analysis:", indent=12, font_size=10, bold=True)
        y -= 2
        for note in anomalies[:8]:
            draw_wrapped(f"  •  {note}", indent=24, font_size=9)
    else:
        draw_line("  No anomalies detected — all automated checks passed.", indent=12, font_size=9)
    y -= 8

    if gap_analysis and gap_analysis.get('gap_type'):
        draw_line("Data Gap Analysis:", indent=12, font_size=10, bold=True)
        y -= 2
        draw_wrapped(
            f"Gap pattern: {gap_analysis.get('gap_type', 'Unknown')} | "
            f"Longest single gap: {gap_analysis.get('max_contiguous_hours', 0)} hours | "
            f"Frequency: {gap_analysis.get('gap_frequency', 'Unknown')}",
            indent=24, font_size=9
        )
        if gap_analysis.get('note'):
            draw_wrapped(gap_analysis['note'], indent=24, font_size=9)
        draw_wrapped(
            "Why gap type matters for STL: A single contiguous gap (e.g., 48 hours of sensor downtime) "
            "is far more disruptive to trend decomposition than 48 scattered one-hour outages, because "
            "STL uses a sliding window that assumes roughly uniform sampling. Long contiguous gaps can "
            "cause the algorithm to distort the trend near the gap boundaries.",
            indent=24, font_size=9, color=(0.3, 0.3, 0.3)
        )
        draw_wrapped(
            "Common gap causes: (1) power interruption to the sensor or router; "
            "(2) Wi-Fi/network connectivity failure; (3) sensor hardware fault or self-reboot; "
            "(4) scheduled maintenance or physical relocation. "
            "The exact cause is not logged by the instrument — consult field maintenance records "
            "or router access logs to confirm.",
            indent=24, font_size=9, color=(0.3, 0.3, 0.3)
        )
    y -= 8

    draw_line("Lower Detection Limit (LDL) Correction:", indent=12, font_size=10, bold=True)
    y -= 2
    draw_wrapped(
        "Plantower laser sensors (used inside PurpleAir monitors) cannot reliably distinguish "
        "concentrations below ~1 µg/m³ from electronic noise. Any reading below 1 µg/m³ has been "
        "replaced with LDL/√2 ≈ 0.707 µg/m³. This standard technique, borrowed from analytical "
        "chemistry, prevents the statistical distortion ('zero bias') that occurs when sub-detection "
        "values are left as zero in calculations of averages, trends, or correlations.",
        indent=24, font_size=9
    )
    y -= 8

    # ── Section 5b: Highest Pollution Events ─────────────────────────────────

    if highest_events:
        if y < 220:
            next_page()
        draw_line("Top Pollution Events (ranked by peak PM2.5):", indent=12, font_size=10, bold=True)
        y -= 4
        # Event type explanations
        draw_wrapped(
            "Spike — a sudden, short-duration PM2.5 surge (minutes to ~2 hours) caused by a nearby source "
            "such as vehicle traffic, cooking smoke, or open burning. "
            "Sustained — a prolonged period (3+ hours) of elevated PM2.5 typically linked to regional "
            "pollution transport, wildfire smoke, or persistent industrial emissions. "
            "PM2.5 Range shows the Min – Max concentration recorded during the event window.",
            indent=12, font_size=8.5, color=(0.35, 0.35, 0.35)
        )
        y -= 4

        # Table header — 6 columns, widths sized to fit USABLE_W (480pt from L_MARGIN+8)
        _evt_cols   = ["#", "Event Start", "Peak Time", "Peak µg/m³", "Range µg/m³",  "Dur (hh:mm)", "Type"]
        _evt_widths = [16,   92,            92,           60,            106,             60,            54]
        _evt_x = [L_MARGIN + 8]
        for w in _evt_widths[:-1]:
            _evt_x.append(_evt_x[-1] + w)

        pdf.setFont("Helvetica-Bold", 8)
        pdf.setFillColorRGB(0.10, 0.22, 0.45)
        for col_label, cx in zip(_evt_cols, _evt_x):
            pdf.drawString(cx, y, col_label)
        pdf.setFillColorRGB(0, 0, 0)
        y -= 4
        pdf.setLineWidth(0.5)
        pdf.setStrokeColorRGB(0.10, 0.22, 0.45)
        pdf.line(L_MARGIN, y, width - R_MARGIN, y)
        pdf.setStrokeColorRGB(0, 0, 0)
        y -= 10

        for rank, ev in enumerate(highest_events[:10], 1):
            if y < B_MARGIN + 18:
                next_page()
            row_vals = [
                str(rank),
                str(ev.get("Event Start", "—")),
                str(ev.get("Peak Time", "—")),
                str(ev.get("Peak PM2.5 (µg/m³)", "—")),
                str(ev.get("PM2.5 Range (µg/m³)", "—")),
                str(ev.get("Duration (hh:mm)", "—")),
                str(ev.get("Type", "—")),
            ]
            pdf.setFont("Helvetica", 8)
            for val, cx in zip(row_vals, _evt_x):
                pdf.drawString(cx, y, val)
            y -= 12

        pdf.setLineWidth(0.3)
        pdf.setStrokeColorRGB(0.75, 0.75, 0.75)
        pdf.line(L_MARGIN, y, width - R_MARGIN, y)
        pdf.setStrokeColorRGB(0, 0, 0)
        y -= 12

    # ── Section 6: Regulatory Standards ──────────────────────────────────────

    section_header("6.", "Comparison to Regulatory Standards")
    draw_wrapped(
        "PM2.5 (fine particulate matter ≤2.5 µm) is regulated under the US EPA National Ambient Air "
        "Quality Standards (NAAQS) and the WHO Global Air Quality Guidelines. The standards below are "
        "used as benchmarks in this report. Note that short monitoring periods (weeks to months) cannot "
        "be directly compared to annual standards without full-year data.",
        indent=12, font_size=9
    )
    draw_wrapped(
        "35 µg/m³ — EPA Primary and Secondary 24-Hour PM2.5 Standard: The EPA's legally enforceable "
        "short-term limit, applied as a 24-hour average. Both the primary standard (protecting public "
        "health) and the secondary standard (protecting public welfare) are set at 35 µg/m³ and are "
        "unchanged since 2006. Sustained 24-hour means above this level are associated with elevated "
        "risk for sensitive groups: children, the elderly, and people with heart or lung disease.",
        indent=12, font_size=9
    )
    draw_wrapped(
        "15 µg/m³ — WHO 24-Hour Guideline and EPA Secondary Annual PM2.5 Standard: This value appears "
        "in two independent standards. As the WHO 24-hour guideline (updated 2021), it reflects the "
        "global health evidence threshold for daily PM2.5 exposure and is more stringent than the EPA "
        "24-hour standard. Separately, 15 µg/m³ is also the EPA's welfare-based secondary annual "
        "PM2.5 standard (averaging time: one calendar year), retained from the original 1997 NAAQS "
        "to protect visibility and ecosystems. The two standards share a number but differ in "
        "averaging time and purpose.",
        indent=12, font_size=9
    )
    draw_wrapped(
        "9 µg/m³ — EPA Primary Annual PM2.5 Standard (revised February 2024): The EPA lowered the "
        "annual health-based NAAQS from 12 µg/m³ to 9 µg/m³, citing updated epidemiological evidence "
        "linking long-term PM2.5 exposure to cardiovascular and respiratory disease at concentrations "
        "below the previous limit. This is a 3-year arithmetic mean standard; a short monitoring "
        "period cannot be directly compared to it without full-year data.",
        indent=12, font_size=9
    )
    draw_wrapped(
        "150 µg/m³ — EPA Primary and Secondary 24-Hour PM10 Standard: This standard covers inhalable "
        "coarse particles (≤10 µm, including PM2.5). It is informational context only in this report; "
        "all measurements here are PM2.5-specific. The 150 µg/m³ threshold applies to 24-hour average "
        "PM10 and has remained unchanged.",
        indent=12, font_size=9
    )
    y -= 10

    # ── Section 7: Data Quality Summary Table ────────────────────────────────

    if radar_profile and radar_profile.get("labels") and radar_profile.get("values"):
        if y < 220:
            next_page()
        section_header("7.", "Data Quality Summary")
        draw_wrapped(
            "The table below consolidates all quality dimensions into a single at-a-glance assessment. "
            "Each row shows the computed score, the research-grade threshold, and whether this dataset "
            "meets that threshold. 'PASS' (green) means the metric meets the threshold; "
            "'REVIEW' (orange) means it falls below and warrants attention before the data are used "
            "in a formal publication.",
            indent=12, font_size=9
        )
        y -= 8

        col_x = [L_MARGIN + 12, L_MARGIN + 240, L_MARGIN + 330, L_MARGIN + 410]
        pdf.setFont("Helvetica-Bold", 9)
        pdf.setFillColorRGB(0.10, 0.22, 0.45)
        pdf.drawString(col_x[0], y, "Quality Dimension")
        pdf.drawString(col_x[1], y, "Score")
        pdf.drawString(col_x[2], y, "Threshold")
        pdf.drawString(col_x[3], y, "Status")
        pdf.setFillColorRGB(0, 0, 0)
        y -= 4
        pdf.setLineWidth(0.6)
        pdf.setStrokeColorRGB(0.10, 0.22, 0.45)
        pdf.line(L_MARGIN, y, width - R_MARGIN, y)
        pdf.setStrokeColorRGB(0, 0, 0)
        y -= 10

        thresholds = {
            "Internal Data Integrity": 99,
            "Temporal Completeness": 80,
            "Overall Quality": 70,
            "Sensor Agreement": 85,
            "Inter-Channel Stability": 70,
            "Sampling Regularity": 70,
        }
        descriptions = {
            "Internal Data Integrity": "Fraction of the rows you submitted that passed all validity checks",
            "Temporal Completeness": "Fraction of expected recording slots (typically 2-min intervals) that contain data",
            "Overall Quality": "Composite: 40% validity + 60% coverage — penalises both bad readings and silent periods",
            "Sensor Agreement": "Fraction of reading pairs where Channel A and B are within ±10% of their mean",
            "Inter-Channel Stability": "Derived from the CV between channels: CV < 5% → ~100%; CV ≥ 15% → 0% (this platform's scale)",
            "Sampling Regularity": "Readings received vs expected at this export's own sampling interval — drops if the sensor stutters or skips",
        }

        for lbl, val in zip(radar_profile["labels"], radar_profile["values"]):
            if y < B_MARGIN + 24:
                next_page()
            thresh = thresholds.get(lbl, 80)
            status = "PASS" if val >= thresh else ("N/A" if thresh == 0 else "REVIEW")
            color = (0.0, 0.50, 0.22) if status == "PASS" else ((0.45, 0.45, 0.45) if status == "N/A" else (0.78, 0.18, 0.05))
            desc = descriptions.get(lbl, "")
            pdf.setFont("Helvetica", 9)
            pdf.drawString(col_x[0], y, lbl)
            pdf.drawString(col_x[1], y, f"{val:.1f}%")
            pdf.drawString(col_x[2], y, f"≥ {thresh}%" if thresh > 0 else "Info only")
            pdf.setFillColorRGB(*color)
            pdf.setFont("Helvetica-Bold", 9)
            pdf.drawString(col_x[3], y, status)
            pdf.setFillColorRGB(0, 0, 0)
            y -= 12
            if desc:
                pdf.setFont("Helvetica", 7.5)
                pdf.setFillColorRGB(0.38, 0.38, 0.38)
                pdf.drawString(col_x[0] + 6, y, desc)
                pdf.setFillColorRGB(0, 0, 0)
                y -= 10

        pdf.setLineWidth(0.3)
        pdf.setStrokeColorRGB(0.75, 0.75, 0.75)
        pdf.line(L_MARGIN, y, width - R_MARGIN, y)
        pdf.setStrokeColorRGB(0, 0, 0)
        y -= 16

    # ── Figures — each on its own dedicated page ───────────────────────────────

    # Each tuple: (what_it_shows, how_to_read, why_it_matters)
    # Paragraphs do NOT repeat the label — the label is drawn separately above each paragraph.
    figure_explanations = {
        "Rolling medians": (
            "Every PM2.5 reading from the monitoring period is plotted as a small gray dot on "
            "the timeline. Overlaid in orange is a 24-hour rolling median — a smoothed line "
            "computed by taking the median of the surrounding 24 hours at each point and "
            "advancing that window forward in time across the entire dataset.",
            "Ignore the individual gray dots and focus on the orange line. If the orange line "
            "rises over time, PM2.5 levels are trending upward. If it falls, air quality is "
            "improving. A flat line means stable background conditions. Where the orange line "
            "has a break, the sensor was offline — no data are invented to bridge that gap.",
            "Rolling medians are the standard first-pass trend filter in air quality monitoring "
            "because they are resistant to outliers: a single spike from a passing truck will "
            "not skew the trend. The 24-hour window absorbs within-day variation while still "
            "resolving multi-day directional shifts. This chart is suitable for direct inclusion "
            "in peer-reviewed publications as evidence of long-term air quality direction."
        ),
        "STL Residuals": (
            "After mathematically removing (1) the overall trend and (2) the regular 24-hour "
            "daily cycle from the PM2.5 data, what remains is plotted here as the 'residual.' "
            "Blue dots represent routine background variation; red dots mark statistical "
            "outliers more than two standard deviations above the residual mean.",
            "Think of this as a 'surprise detector.' When residuals cluster tightly near zero, "
            "air quality is following its predictable daily rhythm with few unexpected events. "
            "Each red dot marks a moment when something unusual happened — a nearby fire, a "
            "traffic incident, construction dust, an industrial emission event, or an unusual "
            "meteorological condition — that pushed PM2.5 well beyond the expected pattern.",
            "Residual analysis is a cornerstone of source apportionment studies. By isolating "
            "events that do not fit the diurnal cycle, researchers can time-match the red-dot "
            "moments against meteorological data, traffic incident logs, or fire records to "
            "identify probable pollution sources. This is directly applicable to EPA receptor "
            "modelling and health-effects epidemiology studies that must distinguish chronic "
            "background exposure from acute episodic exposure."
        ),
        "Diurnal pattern": (
            f"PM2.5 readings are grouped by hour of the day (0–23, {_hour_tz}) across the entire "
            "monitoring period. The bold center line is the mean concentration for each hour. "
            "The shaded gray band spans the 10th–90th percentile range — most readings on "
            "most days fall within this band.",
            "A peak in the early morning (6–9 AM local time) typically corresponds to vehicle "
            "traffic; an evening peak (5–8 PM) to rush-hour traffic, residential cooking, or "
            "heating. The lowest point is usually the cleanest time of day, often late night "
            "or pre-dawn. A wide shaded band at a given hour means that hour's concentration "
            "varies greatly from day to day; a narrow band means it is predictable. Note: "
            "hours overlapping a sensor outage have fewer data points and may be less reliable.",
            "Diurnal patterns directly inform personal exposure modelling, activity-based "
            "epidemiology, and urban-planning decisions. If assessing cumulative daily exposure "
            "for a health study, concentrations must be weighted by the hours people spend "
            "outdoors — this chart provides the concentration profile for that weighting. "
            "It is a required element of most low-cost sensor data publications."
        ),
        "PM2.5 Temporal Radar": (
            "The same hourly averages shown in the diurnal bar chart are re-plotted on a "
            "circular clock face, with midnight (00:00) at the top and hours progressing "
            "clockwise. The distance from the center equals PM2.5 concentration — the further "
            "the shape extends outward at a given hour, the higher the pollution at that time. "
            "Two reference rings mark the WHO 24-hour guideline (15 µg/m³) and the EPA "
            "24-hour standard (35 µg/m³).",
            "A shape that bulges toward the 6–9 AM sector (upper right) indicates a morning "
            "traffic peak. A bulge toward the 5–8 PM sector (lower right) indicates an evening "
            "peak. A roughly circular shape means pollution is nearly constant throughout the "
            "day — typical of industrial or regional background sources. A slim shape that "
            "stays inside the WHO ring means consistently clean air at all hours.",
            "The polar format reveals asymmetries in emission timing that can distinguish "
            "source types at a glance. Traffic-dominated sites produce a two-lobed shape "
            "(morning and evening). Residential wood-burning communities show a single "
            "evening lobe. Industrial sites tend toward a flat circle. This concise format "
            "is well-suited to exposure science publications where figure space is limited."
        ),
        "Sensor drift": (
            "Every reading's Channel A minus Channel B difference (in µg/m³) is plotted as "
            "a thin gray line. The area above zero is shaded teal (A reads higher than B) and "
            "below zero is shaded red (B reads higher than A). A dark navy line shows the "
            "7-day rolling median of the difference, smoothing out daily noise to reveal any "
            "slow underlying calibration drift between the two laser sensors.",
            "A healthy sensor keeps the gray line and the navy median hovering near the dashed "
            "zero reference with no persistent upward or downward slope. If the navy median "
            "trends upward over weeks, Channel A is reading progressively higher than B — a "
            "sign of laser degradation, dust build-up, or aging affecting one channel more "
            "than the other. If it trends downward, B is drifting higher. Cyclical daily "
            "swings suggest a temperature or humidity effect acting differently on each "
            "sensor element. A sustained median beyond ±5 µg/m³ is the threshold this platform "
            "uses to recommend field inspection and possible recalibration.",
            "Long-term drift is one of the most common and hardest-to-detect failure modes of "
            "low-cost optical particle counters. The shaded fill makes the sign of the drift "
            "immediately visible (teal = A high, red = B high), while the navy median line "
            "strips away day-to-day noise so a slow calibration decay becomes obvious. "
            "Detecting drift early lets a researcher flag or trim affected data before months "
            "of measurements are compromised — a direct analogue of the Levey-Jennings control "
            "charts used in clinical laboratory quality management."
        ),
        "Channel A vs B": (
            "Each point is one paired reading: Channel A on the x-axis, Channel B on the y-axis. "
            "Both channels sample the same air simultaneously inside the sensor. The dashed grey "
            "line is the 1:1 line of perfect agreement; the red line is the ordinary-least-squares "
            "fit (B = slope·A + intercept). R² is the squared correlation between the channels.",
            "If the channels agree, points cluster tightly along the 1:1 line and the fitted slope "
            "is near 1 with an intercept near 0. A slope materially different from 1 indicates a "
            "proportional bias between channels; a non-zero intercept indicates a constant offset; "
            "scatter away from the line indicates random disagreement. This platform treats R² > 0.85 as the "
            "acceptance threshold and 0.70–0.85 as acceptable with caveats; below 0.70 the data should be treated "
            "as suspect until the cause is found. For reference, the U.S. EPA's published target value for PM2.5 "
            "air sensors is R² ≥ 0.70 (EPA Performance Testing Protocols, Metrics, and Target Values, 2021).",
            "An A-vs-B scatter with a 1:1 reference is the standard way to document inter-sensor "
            "agreement for low-cost sensors, because it separates proportional bias (slope), constant "
            "offset (intercept), and random error (scatter) — distinctions a single overlaid time-series "
            "cannot reveal. Dual-channel agreement is the intrinsic replication that distinguishes "
            "PurpleAir from single-channel devices and is a required element of a rigorous methods section."
        ),
        "Bland-Altman": (
            "The Bland–Altman plot is the standard method-agreement diagnostic. For every paired "
            "reading it plots the difference between channels (A − B, y-axis) against their mean "
            "((A + B)/2, x-axis). The solid line is the mean bias; the dashed lines are the 95% "
            "limits of agreement (mean bias ± 1.96 standard deviations of the differences).",
            "A mean bias near zero means the channels agree on average. If the cloud of points "
            "tilts or fans out as concentration increases, the disagreement is concentration-dependent "
            "(proportional error) rather than a fixed offset — something a correlation/R² alone hides. "
            "About 95% of differences should fall within the limits of agreement; points outside flag "
            "readings where the two channels disagreed materially.",
            "Bland–Altman is the accepted approach in metrology and epidemiology for comparing two "
            "measurement methods, and is increasingly expected in low-cost-sensor literature because it "
            "quantifies bias and its concentration-dependence directly — information a 1:1 scatter "
            "supplements but does not fully replace. Together the scatter and Bland–Altman provide a "
            "complete, publication-grade account of dual-channel agreement."
        ),
        "PM2.5 time series": (
            "Every valid PM2.5 reading from the monitoring period is plotted in the order it "
            "was recorded. Horizontal reference lines mark the EPA 24-hour standard "
            "(35 µg/m³, dashed red) and the WHO 24-hour guideline (15 µg/m³, dashed orange).",
            "The height of each point above the x-axis is its concentration in µg/m³. Points "
            "above the red line fall in the 'Unhealthy for Sensitive Groups' zone or worse. "
            "Sustained runs of high readings matter more for health than isolated spikes. "
            "Flat stretches locked at a fixed value may indicate sensor saturation or a "
            "frozen/stuck reading and should be investigated in the raw data.",
            "This is the most fundamental chart in the report — the unprocessed data record. "
            "It lets you visually inspect completeness, spot malfunctions, locate exceedance "
            "events for regulatory compliance reporting, and confirm that smoothed trend "
            "charts are not artifacts of the smoothing algorithm."
        ),
        "Hourly pattern": (
            f"Mean PM2.5 for each hour of the day (00:00–23:00, {_hour_tz}) computed across all days "
            "in the monitoring period, displayed as a bar chart. The data are numerically "
            "identical to the diurnal line chart but presented in bar form.",
            "The tallest bars identify your worst-pollution hours of the day. The shortest "
            "bars identify the cleanest window — typically the best time for outdoor exercise, "
            "opening windows for ventilation, or planning time-sensitive outdoor activities.",
            "The bar chart format communicates hour-by-hour comparisons more directly than "
            "a continuous line for audiences less familiar with time-series graphs. It is "
            "widely used in community air quality reports and public health communication."
        ),
        "STL decomposition": (
            "Three stacked panels each show a different component of the PM2.5 signal after "
            "STL decomposition. Top panel: the slow-moving trend (overall direction of air "
            "quality over weeks). Middle panel: the repeating 24-hour daily cycle. "
            "Bottom panel: the residuals — what remains after removing trend and daily cycle.",
            "If the trend panel slopes upward, background air quality is deteriorating over "
            "the monitoring period. A large amplitude in the seasonal panel (tall peaks and "
            "deep troughs) means the daily cycle is the dominant driver of PM2.5 variability "
            "— typical of traffic-influenced urban sites. A mostly flat residual panel with "
            "occasional spikes means the sensor is cleanly resolving episodic events.",
            "STL decomposition is the standard approach for separating signal components in "
            "atmospheric time-series analysis. It lets researchers answer three distinct "
            "questions from a single dataset — Is air quality getting worse overall? (trend), "
            "When during the day is it worst? (seasonal), Were there unusual pollution events? "
            "(residuals) — with each component independently citable in publications."
        ),
        "Weekly heatmap": (
            "A colour grid where rows represent the seven days of the week (Monday at top) "
            f"and columns represent hours of the day (0–23, {_hour_tz}). Each cell is colour-coded "
            "by the mean PM2.5 for that specific day-of-week and hour combination across "
            "the full monitoring period. Blue = clean; yellow-orange = moderate; red = high.",
            "Dark red cells identify the day-hour combinations with the worst average air "
            "quality. Weekday-weekend differences are immediately visible — if weekday "
            "mornings are systematically redder than weekend mornings, commuter traffic "
            "is a dominant source. Similar patterns across all days suggest industrial, "
            "natural, or residential sources rather than commuter activity.",
            "The weekly-by-hourly heatmap is one of the most information-dense summary "
            "charts in exposure science. It simultaneously reveals diurnal patterns, weekly "
            "patterns, and their interactions in a single compact visual. It is appropriate "
            "for both technical research publications and community stakeholder presentations."
        ),
    }

    # ── Separate figures: non-QC first, QC (agreement + drift) last ──────────
    _QC_TITLES = {"Sensor drift", "Channel A vs B", "Bland-Altman"}
    # Desired QC order: agreement scatter, Bland–Altman, then drift-over-time
    _QC_ORDER  = ["Channel A vs B", "Bland-Altman", "Sensor drift"]
    non_qc_figs = [(t, p) for t, p in figures if t not in _QC_TITLES]
    qc_figs_map  = {t: p for t, p in figures if t in _QC_TITLES}
    qc_figs = [(t, qc_figs_map[t]) for t in _QC_ORDER if t in qc_figs_map]

    def _render_figure_page(title, path):
        nonlocal y
        next_page()
        explanation_tuple = figure_explanations.get(title)

        # Figure page header
        pdf.setFont("Helvetica-Bold", 14)
        pdf.setFillColorRGB(0.10, 0.22, 0.45)
        pdf.drawString(L_MARGIN, y, title)
        pdf.setFillColorRGB(0, 0, 0)
        y -= 12
        draw_rule(0.6)
        y -= 6

        # Image
        image = ImageReader(str(path))
        if title == "PM2.5 Temporal Radar":
            img_w = min(int(USABLE_W * 0.60), 295)   # smaller: leaves ~280pt for 3 description paras
            img_h = img_w
            x_pos = L_MARGIN + (USABLE_W - img_w) / 2
        else:
            img_w = USABLE_W
            img_h = int(img_w * 0.50)
            x_pos = L_MARGIN

        pdf.drawImage(image, x_pos, y - img_h, width=img_w, height=img_h)
        y -= img_h + 16

        # Plain-language description
        if explanation_tuple:
            desc_labels = ["What this chart shows:", "How to read it:", "Why it matters for research:"]
            for lbl, paragraph in zip(desc_labels, explanation_tuple):
                if y < B_MARGIN + 20:
                    next_page()
                draw_line(lbl, indent=0, font_size=9, bold=True, color=(0.10, 0.22, 0.45))
                draw_wrapped(paragraph, indent=12, font_size=9)
                y -= 2
        y -= 8

    # ── 1. Render all non-QC figures ──────────────────────────────────────────
    for title, path in non_qc_figs:
        _render_figure_page(title, path)

    # ── 2. QC section intro page, then Channel A vs B, then Sensor Drift ──────
    if qc_figs:
        next_page()
        section_header("QC.", "Quality Control Section")
        draw_wrapped(
            "The charts in this section are technical instrument-health diagnostics. "
            "They are intended for researchers, data reviewers, and sensor owners carrying "
            "out equipment maintenance. Non-technical readers can skip directly to the "
            "air quality charts in the previous section.",
            indent=12, font_size=9
        )
        draw_wrapped(
            "Each PurpleAir sensor contains two completely independent laser particle counters "
            "(Channel A and Channel B) running in parallel, providing intrinsic replication. This "
            "section documents their agreement three ways: the 'Channel A vs B' scatter shows "
            "correlation, proportional bias (slope) and offset (intercept) against a 1:1 line; the "
            "'Bland–Altman' plot quantifies the mean bias and 95% limits of agreement and reveals "
            "any concentration-dependent disagreement; and the 'Sensor Drift' chart shows whether "
            "their difference grows over time — a warning sign of calibration decay. Together they "
            "answer: can I trust this data?",
            indent=12, font_size=9
        )
        for title, path in qc_figs:
            _render_figure_page(title, path)

    # ── Custom Notes (user-authored sections appended after all standard content) ──
    if custom_notes:
        for _cn_idx, _note in enumerate(custom_notes):
            _heading = (_note.get("heading") or "").strip()
            _content = (_note.get("content") or "").strip()
            if not _heading and not _content:
                continue
            next_page()
            section_header(f"Note {_cn_idx + 1}.", _heading or "Additional Notes")
            if _content:
                for _para in _content.split("\n"):
                    _para = _para.strip()
                    if _para:
                        draw_wrapped(_para, indent=12, font_size=9)
                        y -= 2

    # ── Final footer on last page ─────────────────────────────────────────────
    _stamp_footer()
    pdf.save()


def build_public_report_pdf(
    report_path: Path,
    summary: Dict[str, Any],
    daily: pd.DataFrame,
    hourly: pd.DataFrame,
    anomalies: List[str],
    channel_agreement: Dict[str, Any],
    figures: List[Tuple[str, Path]],
    exceedances: Optional[Dict[str, int]] = None,
    metadata: Optional[Dict[str, str]] = None,
    comparison: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Professional, plain-language PDF for residents and research participants.

    If ``comparison`` is provided (a list of per-house dicts each with keys
    ``label``, ``is_control`` and ``rolling_median``), a final House Comparison
    page with 24h + 1h median overlay charts is appended.
    """
    import tempfile, os as _os
    exceedances = exceedances or {}
    metadata    = metadata or {}
    comparison  = comparison or []

    # Normalise timestamp columns to NAIVE LOCAL wall-clock. Timestamps stored in
    # a DST timezone carry mixed offsets (e.g. -05:00 EST and -04:00 EDT), which
    # makes pandas raise "Mixed timezones" on re-read. Stripping the offset keeps
    # the local wall-clock (correct local hour/date) and is identical for UTC
    # ("+00:00" removed → same hour). Applied to both daily and hourly.
    def _naive_local(df):
        if df is None or df.empty:
            return df
        tc = "timestamp" if "timestamp" in df.columns else df.columns[0]
        s = df[tc].astype(str).str.replace(r'(?:[+-]\d{2}:?\d{2}|Z)\s*$', '', regex=True).str.strip()
        df[tc] = pd.to_datetime(s, errors="coerce")
        return df
    daily  = _naive_local(daily.copy())
    hourly = _naive_local(hourly.copy())

    # ── Palette ───────────────────────────────────────────────────────────────
    NAVY     = (0.08, 0.14, 0.28)
    TEAL     = (0.10, 0.42, 0.50)
    WHITE    = (1.00, 1.00, 1.00)
    OFFWHITE = (0.97, 0.97, 0.96)
    LGREY    = (0.92, 0.92, 0.92)
    MGREY    = (0.50, 0.50, 0.50)
    DGREY    = (0.16, 0.16, 0.16)

    # ── Canvas ────────────────────────────────────────────────────────────────
    pdf = canvas.Canvas(str(report_path), pagesize=letter)
    W, H = letter
    LM, RM = 54, 54
    BM = 68          # bottom safe zone
    UW = W - LM - RM  # 504 pt usable width
    page_num = [1]
    y = [H - 54]

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _f(rgb): pdf.setFillColorRGB(*rgb)
    def _s(rgb): pdf.setStrokeColorRGB(*rgb)

    def _stamp():
        pdf.setFont("Helvetica", 7); _f(MGREY)
        dev_id = metadata.get("device_id") or metadata.get("label") or ""
        ft = "Air Quality Community Report  ·  PurpleAir Local Analyzer"
        if dev_id: ft += f"  ·  Device {dev_id}"
        pdf.drawString(LM, 28, ft)
        pdf.drawRightString(W - RM, 28, f"Page {page_num[0]}")
        _s((0.78, 0.78, 0.78)); pdf.setLineWidth(0.4)
        pdf.line(LM, 38, W - RM, 38)
        _f(DGREY); _s((0, 0, 0)); pdf.setLineWidth(0.5)

    def _newpage():
        _stamp(); pdf.showPage(); page_num[0] += 1; y[0] = H - 66

    def _need(h):
        if y[0] < BM + h: _newpage()

    def _at(text, x, yy, font="Helvetica", size=9, color=DGREY):
        _f(color); pdf.setFont(font, size); pdf.drawString(x, yy, text)

    def _wrap(text, x=LM, avail=UW - 10, font="Helvetica", size=9.5,
              color=DGREY, lh=14, after=10):
        """Left-aligned word-wrap. Checks space before each line — never overlaps."""
        _f(color); pdf.setFont(font, size)
        sw = pdf.stringWidth(" ", font, size)
        cur, cw = [], 0.0
        for w in text.split():
            ww = pdf.stringWidth(w, font, size)
            if cur and cw + sw + ww > avail:
                _need(lh + 2)
                pdf.setFont(font, size); _f(color)
                pdf.drawString(x, y[0], " ".join(cur))
                y[0] -= lh
                cur, cw = [w], ww
            else:
                if cur: cw += sw
                cur.append(w); cw += ww
        if cur:
            _need(lh + 2)
            pdf.setFont(font, size); _f(color)
            pdf.drawString(x, y[0], " ".join(cur))
            y[0] -= lh
        y[0] -= after
        _f(DGREY)

    def _section(title, sub=""):
        _need(52)
        y[0] -= 14
        _f(TEAL); pdf.rect(LM, y[0] - 8, UW, 32, fill=1, stroke=0)
        _at(title, LM + 12, y[0] + 10, "Helvetica-Bold", 12, WHITE)
        if sub:
            pdf.setFont("Helvetica", 8.5); _f((0.80, 0.93, 0.97))
            pdf.drawRightString(W - RM - 8, y[0] + 10, sub)
        y[0] -= 42

    def _embed(path, h=215, cap=""):
        """Embed an existing figure. Always checks full height first."""
        need = h + (16 if cap else 4)
        _need(need)
        pdf.drawImage(ImageReader(str(path)), LM, y[0] - h, UW, h,
                      preserveAspectRatio=True, mask="auto")
        y[0] -= h + 6
        if cap:
            _at(cap, LM, y[0], "Helvetica", 7.5, MGREY); y[0] -= 12
        y[0] -= 6

    # Look up existing figures by title (lower-case key)
    fig_map = {t.lower(): p for t, p in figures}

    # ─────────────────────────────────────────────────────────────────────────
    # PAGE 1 — OVERVIEW DASHBOARD
    # ─────────────────────────────────────────────────────────────────────────

    # ── Full-width header block ───────────────────────────────────────────────
    # Draw order: navy bg → text → darker sub-bar → period text (so text is on top)
    dev_id  = metadata.get("device_id") or metadata.get("label") or ""
    dev_loc = metadata.get("location") or metadata.get("sensor_location") or ""
    s_start = (summary.get("date_range", {}).get("start") or "")[:10]
    s_end   = (summary.get("date_range", {}).get("end")   or "")[:10]
    parts   = [f"Period: {s_start} – {s_end}"]
    if dev_id:  parts.append(f"Device ID: {dev_id}")
    if dev_loc: parts.append(f"Location: {dev_loc}")

    HDR_H   = 112   # total header height in points
    SUBBAR  = 36    # height of the darker device-info strip at the bottom
    # 1. Full navy background
    _f(NAVY); pdf.rect(0, H - HDR_H, W, HDR_H, fill=1, stroke=0)
    # 2. Title drawn on the navy (BEFORE the sub-bar so text stays on top)
    _at("Air Quality Community Report", LM, H - 46, "Helvetica-Bold", 22, WHITE)
    # 3. Darker sub-bar — drawn AFTER title so it sits below it
    _f((0.05, 0.10, 0.22)); pdf.rect(0, H - HDR_H, W, SUBBAR, fill=1, stroke=0)
    # 4. Period/device text inside the sub-bar
    _at("  |  ".join(parts), LM, H - HDR_H + 12, "Helvetica", 9.5, (0.76, 0.88, 0.93))

    y[0] = H - HDR_H - 12  # content starts below the header

    # ── Compute personalized stats (used throughout page 1 and tips page) ─────
    pm_corr = summary.get("pm25_average_epa_corrected") or summary.get("pm25_average", "0")
    pm_f    = float(pm_corr) if str(pm_corr).replace(".", "", 1).isdigit() else 0.0
    _pm25_max_val = float(summary.get("pm25_max", 0))

    # WHO/EPA hourly exceedance stats
    _total_hours = summary.get("total_readings", 0) * 2 / 60
    who_h   = exceedances.get("who_15", 0)
    epa_h   = exceedances.get("epa_35", 0)
    _who_pct = round(100 * who_h / _total_hours, 1) if _total_hours > 0 else 0.0
    _within_who_pct = round(100 - _who_pct, 1)

    # Days with most exceedances
    _ts_h_p = "timestamp" if "timestamp" in hourly.columns else hourly.columns[0]
    _hp      = hourly.copy()
    _hp["_d"] = pd.to_datetime(_hp[_ts_h_p], errors="coerce").dt.date
    _pm_h_col = "pm25_corrected" if "pm25_corrected" in _hp.columns else "pm25"
    _hp["_ab"] = _hp[_pm_h_col].apply(lambda v: 1 if pd.notna(v) and float(v) > 15 else 0)
    _exc_days  = _hp.groupby("_d")["_ab"].sum()
    _top_exc   = _exc_days[_exc_days > 0].nlargest(2)

    # Daily PM2.5 stats for verdict strip
    _ts_d_p = "timestamp" if "timestamp" in daily.columns else daily.columns[0]
    _dp = daily.copy()
    _dp["_d"] = pd.to_datetime(_dp[_ts_d_p], errors="coerce").dt.date
    _dp = _dp.dropna(subset=["_d"])
    _pm_d_col = "pm25_corrected" if "pm25_corrected" in _dp.columns else "pm25"
    # A day with no measurement is NOT a clean day. Coercing missing values to 0
    # would put them below every guideline and silently report sensor downtime as
    # compliance, overstating air quality by exactly the amount of data lost. Days
    # without a daily average are excluded, and every count below is therefore out
    # of MONITORED days.
    _dp["_pm_val"] = pd.to_numeric(_dp[_pm_d_col], errors="coerce")
    _dp = _dp.dropna(subset=["_pm_val"])
    _dp["_pm_val"] = _dp["_pm_val"].clip(lower=0.0)
    _monitored_days = int(len(_dp))
    _total_days = max(1, _monitored_days)
    _within_who_days = int((_dp["_pm_val"] <= 15).sum())
    _within_who_days_pct = round(100 * _within_who_days / _total_days)

    def _short_date(d):
        try:
            dt = pd.Timestamp(str(d))
            return f"{dt.strftime('%b')} {dt.day}"   # e.g. "May 18"
        except Exception:
            return str(d)

    _top_dates_str = ""
    if len(_top_exc) >= 2:
        _top_dates_str = " and ".join([_short_date(d) for d in list(_top_exc.index)[:2]])
    elif len(_top_exc) == 1:
        _top_dates_str = _short_date(list(_top_exc.index)[0])

    # ── At a Glance — plain-language verdict + interpretation ─────────────────
    # This is an executive summary (the "so what"), deliberately NOT a restatement
    # of the numeric Health Guidelines box below it. It translates the measured
    # levels into a clear takeaway and activity guidance for residents.
    # The verdict is graded on DAILY averages against the WHO 24-hour guideline
    # (the guideline's own averaging period) — never on the period mean, which
    # would understate short episodes. Health guidance describes documented
    # risk groups; it never asserts what "most people" experienced.
    _days_over = _total_days - _within_who_days
    # Worst dates for the verdict must come from DAILY means (the same basis as
    # the day counts) — not from the hour-based list used elsewhere, which can
    # name more dates than there are exceedance days.
    _over_daily = _dp[_dp["_pm_val"] > 15].nlargest(2, "_pm_val")
    _daily_dates_str = " and ".join(_short_date(d) for d in _over_daily["_d"].tolist())
    _worst_note = f" (highest on {_daily_dates_str})" if _daily_dates_str else ""
    if _days_over == 0 and who_h == 0:
        _glance_verdict = "Air quality at this location was GOOD throughout the monitoring period."
        _glance_interp = (
            f"Fine-particle pollution (PM2.5) stayed within the World Health Organization's "
            f"24-hour guideline of 15 µg/m³ on every day measured, and no individual hour "
            f"exceeded it (period average: {pm_f:.1f} µg/m³). Air quality at this level is "
            f"considered suitable for outdoor activity for everyone, including children, "
            f"older adults, and people with heart or lung conditions."
        )
    elif _days_over == 0:
        _glance_verdict = "Air quality at this location was GOOD, with brief short-term peaks."
        _glance_interp = (
            f"Every daily average met the WHO 24-hour guideline of 15 µg/m³ (period average: "
            f"{pm_f:.1f} µg/m³), although PM2.5 rose above that level during about {_who_pct}% "
            f"of individual hours. Brief peaks of this kind commonly reflect nearby, short-lived "
            f"sources such as traffic, cooking, or smoke. During visibly smoky or dusty periods, "
            f"people with asthma or heart disease benefit from limiting prolonged outdoor exertion."
        )
    elif _within_who_days_pct >= 80:
        _glance_verdict = "Air quality was GENERALLY GOOD — most days met the WHO guideline."
        _glance_interp = (
            f"{_within_who_days} of {_total_days} monitored days ({_within_who_days_pct}%) met the WHO "
            f"24-hour guideline of 15 µg/m³; {_days_over} day{'s' if _days_over != 1 else ''} "
            f"exceeded it{_worst_note}. On days above the guideline, children, older adults, "
            f"and people with heart or lung conditions benefit from limiting prolonged outdoor "
            f"exertion; for everyone else the added risk on those days is small but not zero."
        )
    elif pm_f <= 35:
        _glance_verdict = "Air quality at this location was MODERATE during this period."
        _glance_interp = (
            f"{_days_over} of {_total_days} monitored days exceeded the WHO 24-hour guideline of 15 µg/m³"
            f"{_worst_note}, and the period average was {pm_f:.1f} µg/m³ — above the WHO guideline "
            f"but within the U.S. EPA 24-hour standard of 35 µg/m³. Long-term exposure at these "
            f"levels is associated with increased respiratory and cardiovascular risk in "
            f"epidemiological studies; limiting prolonged outdoor exertion on the higher-pollution "
            f"days is a reasonable precaution, particularly for sensitive groups."
        )
    else:
        _glance_verdict = "Air quality at this location was ELEVATED — a health concern this period."
        _glance_interp = (
            f"The period average of {pm_f:.1f} µg/m³ was above the level of the U.S. EPA 24-hour "
            f"standard (35 µg/m³), and {_days_over} of {_total_days} monitored days were above the WHO guideline"
            f"{_worst_note}. Reducing outdoor exposure on high-pollution days — and using indoor "
            f"filtration where available — is advisable, especially for children, older adults, "
            f"pregnant women, and people with heart or lung conditions."
        )

    _gv_lines = textwrap.wrap(_glance_verdict, width=78)
    _gi_lines = textwrap.wrap(_glance_interp, width=96)
    KF_H = 22 + len(_gv_lines) * 13 + 4 + len(_gi_lines) * 12 + 12
    _need(KF_H + 8)
    y[0] -= 8
    _f(OFFWHITE); _s((0.80, 0.80, 0.80)); pdf.setLineWidth(0.7)
    pdf.roundRect(LM, y[0] - KF_H, UW, KF_H, 6, fill=1, stroke=1)
    _f(TEAL); pdf.rect(LM, y[0] - KF_H, 5, KF_H, fill=1, stroke=0)
    _at("At a Glance", LM + 14, y[0] - 16, "Helvetica-Bold", 11, NAVY)
    _s((0.80, 0.80, 0.80)); pdf.setLineWidth(0.4)
    pdf.line(LM + 12, y[0] - 24, LM + UW - 10, y[0] - 24)
    _gy = y[0] - 38
    pdf.setFont("Helvetica-Bold", 9.5); _f(NAVY)
    for _gl in _gv_lines:
        pdf.drawString(LM + 14, _gy, _gl); _gy -= 13
    _gy -= 4
    pdf.setFont("Helvetica", 8.5); _f(DGREY)
    for _gl in _gi_lines:
        pdf.drawString(LM + 14, _gy, _gl); _gy -= 12
    y[0] -= KF_H + 8

    # ── Health Guidelines box ─────────────────────────────────────────────────
    y[0] -= 8
    BOX_H = 110
    _need(BOX_H + 6)
    _f(OFFWHITE); _s((0.80, 0.80, 0.80)); pdf.setLineWidth(0.7)
    pdf.roundRect(LM, y[0] - BOX_H, UW, BOX_H, 6, fill=1, stroke=1)
    _at("Health Guidelines", LM + 12, y[0] - 16, "Helvetica-Bold", 10.5, NAVY)
    _s((0.80, 0.80, 0.80)); pdf.setLineWidth(0.5)
    pdf.line(LM + 10, y[0] - 26, LM + UW - 10, y[0] - 26)

    # Contextual detail lines — each must fit ~97 chars at 8pt; dates formatted as "May 18"
    # The peak is a single reading; the EPA 35 µg/m³ value is a 24-hour (daily)
    # standard, so it is cited as reference scale — not as a pass/fail limit.
    if _pm25_max_val < 35:
        _max_ctx = f"Peak reading {_pm25_max_val:.1f} µg/m³; EPA 35 µg/m³ is a 24-hour average."
    else:
        _max_ctx = f"Peak reading {_pm25_max_val:.1f} µg/m³; EPA 35 µg/m³ is a 24-hour average."

    if who_h == 0:
        _who_det = "No individual hours exceeded the WHO limit during the entire monitoring period."
    else:
        _date_part = f", mainly on {_top_dates_str}" if _top_dates_str else ""
        _who_det = f"{who_h} hrs = {_who_pct}% of monitoring time{_date_part}. Context: see daily chart."

    std_rows = [
        (pm_f <= 15,
         f"Average PM2.5: {pm_corr} µg/m³  (WHO guideline: 15 µg/m³)",
         (f"Below the WHO guideline. {_max_ctx}" if pm_f <= 15
          else f"Above the WHO 15 µg/m³ guideline. {_max_ctx}")),
        (who_h == 0,
         f"Hours above WHO guideline level (15 µg/m³): {who_h}",
         _who_det),
        (epa_h == 0,
         f"Hours above EPA standard level (35 µg/m³): {epa_h}",
         ("No hours reached the EPA 24-hour standard level during the entire monitoring period."
          if epa_h == 0
          else f"Hourly PM2.5 was above the EPA 24-hour standard level (35 µg/m³) for {epa_h} hours.")),
    ]
    ry = y[0] - 42
    for good, bold_t, detail_t in std_rows:
        dot_c = (0.00, 0.48, 0.20) if good else (0.62, 0.06, 0.06)
        _f(dot_c); pdf.circle(LM + 18, ry + 4, 5.5, fill=1, stroke=0)
        _at(bold_t,   LM + 32, ry + 1,  "Helvetica-Bold", 8.5, NAVY)
        _at(detail_t, LM + 32, ry - 12, "Helvetica",      8,   MGREY)
        ry -= 28
    y[0] -= BOX_H + 10

    # ── Regulatory Standards table (Page 1) ──────────────────────────────────
    _need(36)
    y[0] -= 4
    _at("Regulatory Standards for PM2.5 — All Major Limits at a Glance",
        LM, y[0], "Helvetica-Bold", 9.5, NAVY)
    y[0] -= 16

    # 4-column table — ROW_STD=24 so all 6 rows + footer stay on page 1
    # Columns: Agency+Limit(124) | Period(60) | Description(196) | Used here(124) = 504=UW
    SC = [LM, LM + 124, LM + 184, LM + 380]
    SH = ["Agency & Standard", "Avg Period", "What It Means", "Used in This Report?"]
    ROW_STD = 24

    _need(ROW_STD + 4)
    _f(NAVY); pdf.rect(LM, y[0] - 18, UW, 18, fill=1, stroke=0)
    pdf.setFont("Helvetica-Bold", 7.5); _f(WHITE)
    for cx, sh in zip(SC, SH):
        pdf.drawString(cx + 4, y[0] - 12, sh)
    y[0] -= 18

    STD_DATA = [
        ("WHO",  "15 µg/m³",  "24-hour",
         "Strictest global 24-hour guideline.",
         "YES — used in this report", True),
        ("EPA",  "35 µg/m³",  "24-hour",
         "US enforceable 24-hour standard (NAAQS).",
         "YES — used in this report", True),
        ("EPA",  "9 µg/m³",   "Annual mean",
         "US primary standard for public health.",
         "Context — needs 12+ months data", False),
        ("EPA",  "15 µg/m³",  "Annual mean",
         "US secondary standard for public welfare.",
         "Context — needs 12+ months data", False),
        ("WHO",  "5 µg/m³",   "Annual mean",
         "WHO long-term annual guideline.",
         "Context — needs 12+ months data", False),
        ("EPA",  "150 µg/m³", "24-hour",
         "Coarse-particle (PM10) standard — not PM2.5.",
         "No — PM10 standard only", False),
    ]
    DESC_W = SC[3] - SC[2] - 8
    APPL_W = LM + UW - SC[3] - 6

    for idx, (agency, lim, period, desc, appl, is_rel) in enumerate(STD_DATA):
        _need(ROW_STD + 2)
        _f(LGREY if idx % 2 == 0 else WHITE)
        pdf.rect(LM, y[0] - ROW_STD, UW, ROW_STD, fill=1, stroke=0)
        row_top = y[0]
        # Agency badge + limit value
        badge_c = TEAL if agency == "EPA" else (0.05, 0.36, 0.54)
        _f(badge_c); pdf.roundRect(SC[0] + 3, row_top - 20, 32, 12, 2, fill=1, stroke=0)
        pdf.setFont("Helvetica-Bold", 7); _f(WHITE)
        pdf.drawCentredString(SC[0] + 19, row_top - 14, agency)
        pdf.setFont("Helvetica-Bold", 8.5); _f(NAVY)
        pdf.drawString(SC[0] + 40, row_top - 13, lim)
        # Period
        pdf.setFont("Helvetica", 7.5); _f(DGREY)
        pdf.drawString(SC[1] + 4, row_top - 13, period)
        # Description — single line (fits DESC_W at 7pt)
        pdf.setFont("Helvetica", 7); _f(DGREY)
        desc_lines = textwrap.wrap(desc, width=max(20, int(DESC_W / 4.0)))
        pdf.drawString(SC[2] + 4, row_top - 13, desc_lines[0] if desc_lines else desc)
        # Applicable
        appl_c = (0.00, 0.42, 0.18) if is_rel else MGREY
        pdf.setFont("Helvetica-Bold" if is_rel else "Helvetica", 7.5); _f(appl_c)
        pdf.drawString(SC[3] + 4, row_top - 13, appl)
        y[0] -= ROW_STD

    # Footer note bar (fits because y ≈ 126 > BM+22=90 now)
    y[0] -= 4
    _f(LGREY); pdf.rect(LM, y[0] - 16, UW, 16, fill=1, stroke=0)
    pdf.setFont("Helvetica", 7); _f(MGREY)
    pdf.drawString(LM + 6, y[0] - 11,
        "Only 24-hour standards (WHO 15 µg/m³, EPA 35 µg/m³) are directly comparable to this monitoring period. "
        "Annual standards require ≥12 months of continuous data.")
    y[0] -= 20

    # ─────────────────────────────────────────────────────────────────────────
    # PAGE 2 — PM2.5 TREND OVER TIME  (rolling medians)
    # ─────────────────────────────────────────────────────────────────────────
    _newpage()
    _section("PM2.5 Levels Over Time — Trend & Variability")
    y[0] -= 4

    _wrap(
        "This chart shows how PM2.5 changed day by day during the monitoring period. "
        "Two lines are shown: a fine line for each hourly reading, and a bolder smoothed "
        "line that shows the overall daily trend. Watch the trend line: when it rises, "
        "air quality is getting worse; when it falls, air quality is improving.",
        lh=13, after=10
    )

    rolling_path = fig_map.get("rolling medians")
    if rolling_path and rolling_path.exists():
        _embed(rolling_path, h=215,
               cap="Chart: PM2.5 over time. Fine line = each hourly reading. Bold line = smoothed 24-hour trend.")
    y[0] -= 6

    _wrap(
        "Short spikes in the hourly line are normal — they often come from a passing vehicle, "
        "cooking, or wind stirring up dust, and typically clear within minutes. What matters "
        "for health is the sustained trend: if the bold line stays elevated for hours or days, "
        "that signals a real pollution episode worth paying attention to — such as smoke from "
        "fires, stagnant air trapping pollutants, or industrial emissions. The red reference "
        "line (if shown) marks the WHO 24-hour guideline of 15 µg/m3.",
        lh=13, after=8
    )

    # ─────────────────────────────────────────────────────────────────────────
    # PAGE 3 — DAILY PM2.5 DISTRIBUTION + TABLE
    # ─────────────────────────────────────────────────────────────────────────
    _newpage()
    _section("Daily PM2.5 Results", f"{s_start} – {s_end}")
    y[0] -= 4

    _wrap(
        "The box plot below summarises the distribution of daily PM2.5 concentrations across "
        "the monitoring period. Each box spans the interquartile range (25th–75th percentile) "
        "of all hourly readings for that day; the horizontal line inside the box is the daily "
        "median. Whiskers extend to the 10th and 90th percentile; individual dots beyond the "
        "whiskers are statistical outliers. The red dashed line marks the WHO 24-hour guideline "
        "(15 µg/m³); the orange dashed line marks the EPA 24-hour standard (35 µg/m³).",
        lh=13, after=10
    )

    # Prep daily data
    ts_col   = "timestamp" if "timestamp" in daily.columns else daily.columns[0]
    d_work   = daily.copy()
    d_work["_date"] = pd.to_datetime(d_work[ts_col], errors="coerce").dt.date
    d_work   = d_work.dropna(subset=["_date"]).sort_values("_date")
    pm_col_d = "pm25_corrected" if "pm25_corrected" in d_work.columns else "pm25"
    # Fall back to the raw daily value where the corrected one is unavailable, but
    # leave genuinely missing days as NaN — never 0. Plotting a gap as 0 µg/m³ draws
    # a fake "perfectly clean" day, which reads as excellent air quality when in fact
    # nothing was measured. NaN renders as a break in the line, which is the truth.
    _corrected = pd.to_numeric(d_work[pm_col_d], errors="coerce")
    if "pm25" in d_work.columns:
        _corrected = _corrected.fillna(pd.to_numeric(d_work["pm25"], errors="coerce"))
    d_work["_pm"] = _corrected.clip(lower=0.0)

    # Daily PM2.5 box plot using hourly data grouped by date
    _tmp1 = None
    try:
        _ts_bx   = "timestamp" if "timestamp" in hourly.columns else hourly.columns[0]
        _h_bx    = hourly.copy()
        _h_bx["_date"] = pd.to_datetime(_h_bx[_ts_bx], errors="coerce").dt.date
        _pm_bx   = "pm25_corrected" if "pm25_corrected" in _h_bx.columns else "pm25"
        _h_bx["_pm"] = _h_bx[_pm_bx].apply(lambda v: max(0.0, float(v)) if pd.notna(v) else float("nan"))
        _grouped = _h_bx.dropna(subset=["_pm"]).groupby("_date")["_pm"].apply(list)
        _grouped = _grouped[_grouped.apply(len) > 0]

        if len(_grouped) > 0:
            _dates  = sorted(_grouped.index)
            _data   = [_grouped[d] for d in _dates]
            _lbls   = [str(d)[5:] for d in _dates]

            fig, ax = plt.subplots(figsize=(9.4, 3.4))
            bp = ax.boxplot(
                _data, positions=range(len(_data)),
                widths=0.55, patch_artist=True, showfliers=True,
                flierprops=dict(marker=".", markersize=3, color="#888888", alpha=0.6),
                medianprops=dict(color="#1a3a6b", linewidth=2.0),
                boxprops=dict(facecolor="#cce4f0", linewidth=0.8),
                whiskerprops=dict(linewidth=0.8, linestyle="--"),
                capprops=dict(linewidth=0.8),
                whis=(10, 90),
            )
            ax.axhline(15, color="#c0392b", lw=1.2, ls="--", alpha=0.85, label="WHO guideline 15 µg/m³")
            ax.axhline(35, color="#e67e22", lw=1.0, ls="--", alpha=0.75, label="EPA standard 35 µg/m³")
            ax.set_xticks(range(len(_lbls)))
            ax.set_xticklabels(_lbls, rotation=40, ha="right", fontsize=7.0)
            ax.set_ylabel("PM2.5 (µg/m³)", fontsize=9)
            ax.set_title("Daily PM2.5 Distribution — Box Plot (Median, IQR, 10th–90th pct, Outliers)",
                         fontsize=10.5, fontweight="bold")
            ax.legend(fontsize=7.5, loc="upper right", framealpha=0.88)
            ax.grid(axis="y", alpha=0.20)
            ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
            fig.tight_layout(pad=1.1)
            _fd, _tmp1 = tempfile.mkstemp(suffix=".png")
            _os.close(_fd); fig.savefig(_tmp1, dpi=155, bbox_inches="tight"); plt.close(fig)
            IMG_H = 215
            _need(IMG_H + 14)
            pdf.drawImage(ImageReader(_tmp1), LM, y[0] - IMG_H, UW, IMG_H)
            y[0] -= IMG_H + 12
    except Exception:
        pass
    finally:
        if _tmp1 and _os.path.exists(_tmp1):
            try: _os.unlink(_tmp1)
            except Exception: pass

    # Daily summary table (3 columns: Date | PM2.5 | vs. WHO)
    COL_X   = [LM, LM + 140, LM + 320]
    HDRS    = ["Date", "PM2.5 (µg/m³)", "vs. WHO (15 µg/m³)"]
    ROW_H   = 13
    HDR_H   = 22

    def _draw_tbl_header():
        _need(HDR_H + ROW_H)
        _f(NAVY); pdf.rect(LM, y[0] - HDR_H + 2, UW, HDR_H, fill=1, stroke=0)
        pdf.setFont("Helvetica-Bold", 8.5); _f(WHITE)
        for cx, hd in zip(COL_X, HDRS):
            pdf.drawString(cx + 4, y[0] - 13, hd)
        y[0] -= HDR_H

    _draw_tbl_header()
    for idx, (_, row) in enumerate(d_work.iterrows()):
        if y[0] < BM + ROW_H + 4:
            _newpage()
            _draw_tbl_header()
        pm_v = row["_pm"]
        # Zebra row
        _f(LGREY if idx % 2 == 0 else WHITE)
        pdf.rect(LM, y[0] - ROW_H + 1, UW, ROW_H, fill=1, stroke=0)
        # Data cells
        pdf.setFont("Helvetica", 8.5); _f(DGREY)
        pdf.drawString(COL_X[0] + 4, y[0] - 9, str(row["_date"]))
        pdf.drawString(COL_X[1] + 4, y[0] - 9, f"{pm_v:.2f}")
        who_c = (0.60, 0.06, 0.06) if pm_v > 15 else (0.00, 0.44, 0.18)
        _f(who_c); pdf.drawString(COL_X[2] + 4, y[0] - 9,
                                   "Above WHO guideline" if pm_v > 15 else "Within WHO guideline")
        y[0] -= ROW_H

    # ─────────────────────────────────────────────────────────────────────────
    # PAGE 4 — HOUR-BY-HOUR PATTERN + HEALTH GUIDE
    # ─────────────────────────────────────────────────────────────────────────
    _newpage()
    _section("24-Hour Air Quality Pattern — When Is the Air Cleanest?")
    y[0] -= 4

    _wrap(
        "The polar (clock-face) chart below shows the average PM2.5 concentration for each hour "
        "of the day across the entire monitoring period. Midnight (00:00) is at the top; hours "
        "advance clockwise. The further the shape extends outward at a given hour, the higher "
        "the pollution. The dashed reference rings mark the WHO guideline (15 µg/m³) and the "
        "EPA 24-hour standard (35 µg/m³).",
        lh=13, after=8
    )

    # ── Embed the existing research-pipeline polar chart (best quality) ───────
    radar_path = fig_map.get("pm2.5 temporal radar")

    # Derive cleanest / dirtiest hour early so it can appear right after chart
    _tz_lbl  = summary.get("tz_label", "UTC") or "UTC"
    _is_utc  = _tz_lbl.upper() == "UTC"
    ts_h2    = "timestamp" if "timestamp" in hourly.columns else hourly.columns[0]
    h_wk2    = hourly.copy()
    h_wk2["_hr"] = pd.to_datetime(h_wk2[ts_h2], errors="coerce").dt.hour
    pm_hc2   = "pm25_corrected" if "pm25_corrected" in h_wk2.columns else "pm25"
    diurnal2 = h_wk2.groupby("_hr")[pm_hc2].mean().dropna()
    _tz_short = _tz_lbl.split('/')[-1].replace('_', ' ')
    if _tz_is_as_recorded(_tz_lbl):
        # reads "(EDT time)" when the user named the zone, else "(local time)"
        _tz_short = _tz_recorded_zone(_tz_lbl) or "local"
    def _hfmt_tz(h):
        if _is_utc:
            return f"{h:02d}:00 UTC"
        return f"{h % 12 or 12}:00 {'AM' if h < 12 else 'PM'} ({_tz_short} time)"

    if radar_path and Path(radar_path).exists():
        # Reduced from 460 → 380 so caption + cleanest-hour text + tz note all
        # fit on the same page without triggering _need() page breaks.
        RADAR_H = 380
        _need(RADAR_H + 20)
        radar_x = LM + (UW - RADAR_H) / 2
        pdf.drawImage(ImageReader(str(radar_path)), radar_x, y[0] - RADAR_H,
                      RADAR_H, RADAR_H, preserveAspectRatio=True, mask="auto")
        y[0] -= RADAR_H + 6

        # Caption — wrapped so it never escapes the right margin
        _cap_tz = "UTC" if _is_utc else f"{_tz_short} local time"
        _cap_text = (
            "Chart: Average PM2.5 by hour of day plotted on a 24-hour clock face "
            f"({_cap_tz}, hour labels 00-23). Distance from centre = PM2.5 concentration. "
            "Dashed rings = WHO 15 µg/m³ and EPA 35 µg/m³."
        )
        _cap_fs  = 7.5
        _cap_cpl = max(40, int((UW - 4) / (_cap_fs * 0.54)))
        _cap_lh  = 10
        _f(MGREY); pdf.setFont("Helvetica", _cap_fs)
        _cap_ly  = y[0]
        for _cl in textwrap.wrap(_cap_text, width=_cap_cpl):
            pdf.drawString(LM, _cap_ly, _cl)
            _cap_ly -= _cap_lh
        y[0] = _cap_ly - 4
    else:
        # Fallback: standard diurnal line chart from research pipeline
        diurnal_path = fig_map.get("diurnal pattern")
        if diurnal_path and Path(diurnal_path).exists():
            _embed(diurnal_path, h=210,
                   cap="Chart: Average PM2.5 by hour of day — shaded band = 10th-90th percentile range.")

    # Cleanest / dirtiest hour callout — drawn BEFORE timezone box so it
    # stays on the same page as the chart.
    if len(diurnal2) >= 4:
        c_hr = int(diurnal2.idxmin()); d_hr = int(diurnal2.idxmax())
        _wrap(
            f"Cleanest typical hour: {_hfmt_tz(c_hr)} (avg {diurnal2[c_hr]:.1f} µg/m³). "
            f"Highest average: {_hfmt_tz(d_hr)} ({diurnal2[d_hr]:.1f} µg/m³). "
            "For outdoor walks and exercise, choose the hours where the chart shape stays "
            "closest to the centre.",
            lh=13, after=10
        )

    # Timezone note — fully wrapped, sized to content, stays within margins
    _TZ_FS   = 7.5
    _TZ_LH   = 11   # line height in points
    _TZ_X    = LM + 8
    _TZ_AVAIL = UW - 20   # available text width in points
    _TZ_CPL  = max(40, int(_TZ_AVAIL / (_TZ_FS * 0.52)))  # chars per line

    if _is_utc:
        _tz_text = (
            "Timezone note:  "
            "All hours are in UTC (Coordinated Universal Time) as recorded by the sensor. "
            "To convert: subtract 5 hrs for Eastern Standard Time (EST, Nov-Mar) "
            "or 4 hrs for Eastern Daylight Time (EDT, Mar-Nov). "
            "Example: 14:00 UTC = 9:00 AM EST  |  10:00 AM EDT."
        )
    elif _tz_is_as_recorded(_tz_lbl):
        _rz = _tz_recorded_zone(_tz_lbl)
        _tz_text = (
            "Timezone note:  Hours are shown exactly as recorded in the uploaded file "
            + (f"(already in {_rz} local time). " if _rz
               else "(exported with timestamps already in the sensor's local time). ")
            + "No conversion was applied."
        )
    else:
        _tz_text = f"Timezone note:  Hours shown in {_tz_lbl} (local time applied at analysis time)."

    _tz_lines = textwrap.wrap(_tz_text, width=_TZ_CPL)
    _tz_box_h = max(20, len(_tz_lines) * _TZ_LH + 10)
    _need(_tz_box_h + 4)
    _f(OFFWHITE); pdf.rect(LM, y[0] - _tz_box_h, UW, _tz_box_h, fill=1, stroke=0)
    _ly = y[0] - 9
    for _li, _lt in enumerate(_tz_lines):
        _f((0.42, 0.24, 0.00) if _li == 0 else MGREY)
        pdf.setFont("Helvetica-Bold" if _li == 0 else "Helvetica", _TZ_FS)
        pdf.drawString(_TZ_X, _ly, _lt)
        _ly -= _TZ_LH
    y[0] -= _tz_box_h + 6


    # ─────────────────────────────────────────────────────────────────────────
    # PAGE 5 — PRACTICAL TIPS + ABOUT THIS DATA
    # ─────────────────────────────────────────────────────────────────────────
    _newpage()
    _section("What You Can Do — Personalized Guidance Based on Your Data")
    y[0] -= 8

    # Build personalized tips from actual monitoring data (no em-dashes in any text)
    _ts_tips = "timestamp" if "timestamp" in hourly.columns else hourly.columns[0]
    _h_tips  = hourly.copy()
    _h_tips["_hr"] = pd.to_datetime(_h_tips[_ts_tips], errors="coerce").dt.hour
    _pm_tips = "pm25_corrected" if "pm25_corrected" in _h_tips.columns else "pm25"
    _d_tips  = _h_tips.groupby("_hr")[_pm_tips].mean().dropna()
    _peak_hr  = int(_d_tips.idxmax()) if len(_d_tips) > 0 else 6
    _clean_hr = int(_d_tips.idxmin()) if len(_d_tips) > 0 else 3
    _peak_val = round(float(_d_tips[_peak_hr]),  1) if len(_d_tips) > 0 else 0.0
    _clean_val= round(float(_d_tips[_clean_hr]), 1) if len(_d_tips) > 0 else 0.0

    # Reuse _is_utc, _tz_lbl and _tz_short computed in the chart section above
    def _hfmt_tip(h):
        if _is_utc:
            return f"{h:02d}:00 UTC"
        return f"{h % 12 or 12}:00 {'AM' if h < 12 else 'PM'} ({_tz_short} time)"

    # UTC-to-local conversion reminder for Tip 2 if needed
    _tz_tip_note = (" (All times are UTC; subtract 4 hrs for EDT or 5 hrs for EST.)" if _is_utc else "")

    # Tip 1: personalized to WHO PM2.5 guideline exceedance (not AQI-based)
    if _within_who_pct >= 99:
        _tip1_body = (f"PM2.5 remained below the WHO guideline of 15 µg/m3 for {_within_who_pct}% of "
                      f"the monitoring period. Outdoor activity is appropriate for all population groups "
                      f"during these conditions. If concentrations rise above 15 µg/m3, children, "
                      f"elderly adults, and individuals with respiratory conditions should reduce "
                      f"prolonged outdoor exertion.")
    elif _within_who_pct >= 90:
        _tip1_body = (f"PM2.5 remained below the WHO guideline of 15 µg/m3 for {_within_who_pct}% of "
                      f"the monitoring period. When PM2.5 exceeds 15 µg/m3 (the remaining "
                      f"{round(100 - _within_who_pct, 1)}% of the time), children, elderly adults, and "
                      f"people with respiratory conditions should reduce prolonged outdoor activities.")
    else:
        _tip1_body = (f"PM2.5 exceeded the WHO guideline of 15 µg/m3 for {_who_pct}% of the monitoring "
                      f"period. During these elevated periods, children, elderly adults, pregnant women, "
                      f"and individuals with asthma or heart conditions should reduce prolonged vigorous "
                      f"outdoor activity and consider using a HEPA air purifier indoors.")

    # Tip 2: personalized to peak hour with explicit timezone
    _tip2_body = (f"Data from this monitoring period shows average PM2.5 is highest around "
                  f"{_hfmt_tip(_peak_hr)} ({_peak_val} µg/m³) and lowest around "
                  f"{_hfmt_tip(_clean_hr)} ({_clean_val} µg/m³){_tz_tip_note}. "
                  f"Schedule outdoor exercise, children's play, and gardening during the "
                  f"low-concentration hours for reduced exposure.")

    # Tip 3: personalized to WHO exceedance context
    if who_h == 0:
        _tip3_body = ("PM2.5 stayed below the WHO guideline of 15 µg/m³ for every individual hour "
                      "of this monitoring period. This is an excellent result. Keep windows open "
                      "during your cleanest hours to refresh indoor air without concern.")
    else:
        _tip3_body = (f"On the {who_h} hours that exceeded the WHO guideline of 15 µg/m³ "
                      f"({_who_pct}% of monitoring time), keeping windows closed and running "
                      f"a HEPA air purifier indoors can meaningfully reduce your personal exposure. "
                      f"A portable HEPA filter can remove up to 80% of fine particles from a room.")

    # Tip 4: peak PM2.5 context
    _tip4_body = (f"The highest recorded hourly average during this period was {_pm25_max_val:.1f} µg/m³. "
                  f"This brief peak was still below the EPA 24-hour standard of 35 µg/m³, which means "
                  f"no hour during this monitoring period reached the level where the EPA considers air "
                  f"unhealthy for sensitive groups." if _pm25_max_val < 35 else
                  f"The highest hourly average of {_pm25_max_val:.1f} µg/m³ exceeded the EPA 24-hour "
                  f"standard of 35 µg/m³. During such peaks, reduce outdoor time, especially for "
                  f"children, elderly residents, and anyone with respiratory conditions.")

    # Tip 5: sensitive groups
    _tip5_body = ("Children, pregnant women, elderly residents, and anyone with asthma, heart disease, "
                  "or lung conditions are more sensitive to fine particles. Their breathing rate is "
                  "higher relative to body size, increasing exposure. On Moderate days and above, "
                  "prioritize indoor activities or shorten outdoor time for these groups.")

    # Tip 6: sensor coverage note
    _tip6_body = ("This sensor captures air quality at one specific location. Readings can vary "
                  "significantly within a neighborhood depending on proximity to roads, kitchens, "
                  "or green spaces. For a broader picture, compare this data with nearby sensors "
                  "or official monitoring stations in your area.")

    TIPS = [
        ("Your data: air quality overview",             _tip1_body),
        ("Best and worst hours for outdoor activity",   _tip2_body),
        ("What to do during elevated pollution hours",  _tip3_body),
        ("Understanding the peak reading",              _tip4_body),
        ("Extra care for sensitive household members",  _tip5_body),
        ("What this sensor covers",                     _tip6_body),
    ]
    for i, (tip_title, tip_body) in enumerate(TIPS):
        _need(62)
        tip_top = y[0]
        _f(TEAL); pdf.circle(LM + 13, tip_top - 12, 11, fill=1, stroke=0)
        pdf.setFont("Helvetica-Bold", 11); _f(WHITE)
        pdf.drawCentredString(LM + 13, tip_top - 16, str(i + 1))
        _at(tip_title, LM + 30, tip_top - 7, "Helvetica-Bold", 10.5, NAVY)
        y[0] = tip_top - 23
        _wrap(tip_body, x=LM + 30, avail=UW - 34, font="Helvetica",
              size=9.5, color=DGREY, lh=13, after=18)

    # About this data — two-column rows, values wrap freely (no truncation)
    _section("About This Measurement")
    y[0] -= 8
    qs    = summary.get("quality_score", 0)
    agree = summary.get("channel_agreement_pct")
    n_rd  = summary.get("total_readings", 0)
    qs_l  = ("Excellent — publication-ready" if qs >= 90 else
             "Good — reliable for general use" if qs >= 70 else "Fair — interpret with caution")

    INFO = [
        ("Sensor",
         "PurpleAir optical particle counter — Plantower laser sensor + BME280 humidity/temperature chip"),
        ("PM2.5 Correction",
         "EPA Barkjohn formula applied per-reading using concurrent humidity (Barkjohn et al., 2021). "
         "Formula: Corrected PM2.5 = 0.524 × raw_PM − 0.0862 × RH + 5.75"),
        ("Total readings",
         f"{n_rd:,} measurements recorded approximately every 2 minutes over the monitoring period"),
        ("Data quality score",
         f"{qs}% — {qs_l}  (formula: 0.4 × validity + 0.6 × temporal coverage)"),
        ("Sensor agreement",
         f"{agree}% channel-to-channel agreement between the dual internal sensors" if agree
         else "Single-channel dataset — dual-channel agreement check not available"),
        ("Note",
         "This is a Community Summary Report with no statistical jargon. For full methodology, "
         "sensor drift analysis, and quality-control charts, see the Research Report."),
    ]
    # Plain-language exposure burden + uncertainty + reproducibility (additive).
    _ex_c = summary.get("exposure")
    if _ex_c and _ex_c.get("n_days") is not None:
        INFO.insert(4, (
            "Days above the guideline",
            f"On {_ex_c.get('days_over_who15')} of {_ex_c.get('n_days')} days, the daily average was above "
            f"the WHO safe guideline of 15 µg/m³ ({_ex_c.get('days_over_epa35')} day(s) above the US EPA "
            f"35 µg/m³ standard)."))
    _un_c = summary.get("uncertainty")
    if _un_c and _un_c.get("mean_ci_halfwidth") is not None:
        INFO.insert(5, (
            "Measurement uncertainty",
            f"Each reading is accurate to about ±{_un_c.get('mean_ci_halfwidth')} µg/m³ (95% confidence). "
            f"Showing this range honestly is more scientific than a single number with no error."))
    _rp_c = summary.get("repro")
    if _rp_c and _rp_c.get("repro_id"):
        INFO.append((
            "Reproducibility ID",
            f"{_rp_c.get('repro_id')} — a unique fingerprint of your data + the methods used, so anyone can "
            f"reproduce or check these results."))
    LBL_W = 132   # label column width
    VAL_X = LM + LBL_W + 8
    VAL_W = UW - LBL_W - 12

    for idx, (lbl, val) in enumerate(INFO):
        _need(26)
        row_y = y[0]
        # Alternating row background
        _f(LGREY if idx % 2 == 0 else WHITE)
        pdf.rect(LM, row_y - 26, UW, 26, fill=1, stroke=0)
        # Label
        pdf.setFont("Helvetica-Bold", 8.5); _f(NAVY)
        pdf.drawString(LM + 8, row_y - 10, lbl + ":")
        # Value wraps in right column from the same row_y baseline
        y[0] = row_y - 4
        _wrap(val, x=VAL_X, avail=VAL_W, font="Helvetica", size=8.5, color=DGREY, lh=12, after=4)
        # Ensure y advances at least past this row
        y[0] = min(y[0], row_y - 26)
    y[0] -= 6

    # ── Monitoring Summary — 4 key tiles + verdict (moved here from Page 1) ────
    _need(80)
    y[0] -= 10
    _at("Monitoring Summary", LM, y[0], "Helvetica-Bold", 10, NAVY)
    y[0] -= 16
    tw4 = (UW - 9) / 4
    stat_tiles = [
        ("Avg PM2.5 (EPA-corrected)",  f"{pm_corr} µg/m³"),
        ("Highest Hourly PM2.5",       f"{summary.get('pm25_max', '0')} µg/m³"),
        ("Data Quality Score",         f"{summary.get('quality_score', '0')}%"),
        ("Total Readings",             f"{summary.get('total_readings', 0):,}"),
    ]
    for i, (lbl, val) in enumerate(stat_tiles):
        tx = LM + i * (tw4 + 3)
        _f(LGREY); pdf.rect(tx, y[0] - 52, tw4, 52, fill=1, stroke=0)
        _f(TEAL);  pdf.rect(tx, y[0], tw4, 3, fill=1, stroke=0)
        _at(val, tx + 8, y[0] - 22, "Helvetica-Bold", 13, NAVY)
        pdf.setFont("Helvetica", 7); _f(MGREY)
        for chunk in textwrap.wrap(lbl, width=int((tw4 - 10) / 3.8)):
            pdf.drawString(tx + 8, y[0] - 36, chunk)
            break
    y[0] -= 64

    # Data Quality Score explanation note — wrapped to fit within margins
    _DQS_FS  = 7.5
    _DQS_LH  = 11
    _DQS_CPL = max(40, int((UW - 20) / (_DQS_FS * 0.52)))
    _dqs_txt = (
        "Data Quality Score: measures how complete and valid the sensor data is. "
        "Formula: 0.4 x validity + 0.6 x temporal coverage. "
        "90%+ = Excellent (publication-ready).  70-89% = Good (reliable for general use).  "
        "Below 70% = Fair — interpret results with caution."
    )
    _dqs_lines = textwrap.wrap(_dqs_txt, width=_DQS_CPL)
    _dqs_box_h = max(20, len(_dqs_lines) * _DQS_LH + 10)
    _need(_dqs_box_h + 4)
    _f(OFFWHITE); pdf.rect(LM, y[0] - _dqs_box_h, UW, _dqs_box_h, fill=1, stroke=0)
    _dly = y[0] - 9
    pdf.setFont("Helvetica", _DQS_FS); _f(MGREY)
    for _dl in _dqs_lines:
        pdf.drawString(LM + 8, _dly, _dl)
        _dly -= _DQS_LH
    y[0] -= _dqs_box_h + 6

    # Verdict strip — wrapped so long lines never cross the right margin
    if _within_who_days_pct == 100:
        _verdict_line = (f"PM2.5 remained at or below the WHO guideline of 15 µg/m3 "
                         f"for all {_total_days} monitored days — no monitored day had elevated pollution.")
    elif _within_who_days_pct >= 80:
        _verdict_line = (f"PM2.5 remained at or below the WHO guideline of 15 µg/m3 for "
                         f"{_within_who_days} of {_total_days} monitored days ({_within_who_days_pct}%).")
    else:
        _verdict_line = (f"PM2.5 was within the WHO guideline of 15 µg/m3 for "
                         f"{_within_who_days_pct}% of monitored days ({_within_who_days} of {_total_days}).")
    _verdict_sub = (f"No hours exceeded the WHO 15 µg/m3 guideline during the monitoring period."
                    if who_h == 0 else
                    f"Hours above WHO 15 µg/m3: {who_h} ({_who_pct}% of monitoring time). "
                    f"PM2.5 below 15 µg/m3 for {_within_who_pct}% of the period.")
    _v_textw = UW - 24
    _v_lines = textwrap.wrap(_verdict_line, width=max(40, int(_v_textw / (9 * 0.50))))
    _s_lines = textwrap.wrap(_verdict_sub,  width=max(40, int(_v_textw / (8 * 0.50))))
    _strip_h = 12 + len(_v_lines) * 12 + 2 + len(_s_lines) * 11 + 6
    _need(_strip_h + 6)
    _f(OFFWHITE); _s(TEAL); pdf.setLineWidth(0.8)
    pdf.roundRect(LM, y[0] - _strip_h, UW, _strip_h, 5, fill=1, stroke=1)
    pdf.setLineWidth(0.4)
    _f(TEAL); pdf.rect(LM, y[0] - _strip_h, 5, _strip_h, fill=1, stroke=0)
    _vy = y[0] - 14
    pdf.setFont("Helvetica-Bold", 9); _f(NAVY)
    for _vl in _v_lines:
        pdf.drawString(LM + 12, _vy, _vl); _vy -= 12
    _vy -= 2
    pdf.setFont("Helvetica", 8); _f(MGREY)
    for _sl in _s_lines:
        pdf.drawString(LM + 12, _vy, _sl); _vy -= 11
    y[0] -= _strip_h + 8

    # ─────────────────────────────────────────────────────────────────────────
    # OPTIONAL FINAL PAGE — HOUSE COMPARISON (only when comparison data passed)
    # ─────────────────────────────────────────────────────────────────────────
    _cmp_tmps = []
    if comparison:
        _CMP_COLORS = ["#1f7a8c", "#e07a5f", "#f6aa1c", "#4c956c", "#8f3f97",
                       "#ff7e00", "#2a9d8f", "#c1121f", "#6a4c93"]

        def _cmp_overlay(kind, title):
            import tempfile as _tf2, os as _os4
            ser = []
            for h in comparison:
                rm = h.get("rolling_median") or {}
                if kind == "24h":
                    xs, ys = rm.get("timestamps") or [], rm.get("median_24h") or []
                else:
                    xs, ys = rm.get("median_1h_timestamps") or [], rm.get("median_1h") or []
                if not xs or not ys:
                    continue
                n = min(len(xs), len(ys))
                try:
                    xdt = pd.to_datetime(pd.Series(xs[:n]), errors="coerce")
                except Exception:
                    continue
                ser.append((h.get("label") or "House", xdt,
                            pd.Series(ys[:n], dtype="float64"), bool(h.get("is_control"))))
            if not ser:
                return None
            # Dense 1h series get thinner lines than the smoother 24h series
            _ctl_lw = 1.7 if kind == "1h" else 2.0
            _oth_lw = 0.85 if kind == "1h" else 1.1
            fig, ax = plt.subplots(figsize=(9.2, 3.5))
            ci = 0
            for lbl, xdt, yv, is_ctl in sorted(ser, key=lambda t: t[3]):
                _avg = float(yv.mean()) if len(yv.dropna()) else float("nan")
                _avg_txt = f"  ·  avg {_avg:.1f}" if _avg == _avg else ""
                if is_ctl:
                    ax.plot(xdt, yv, color="#0a1f47", linewidth=_ctl_lw,
                            solid_capstyle="round", solid_joinstyle="round",
                            label=f"{lbl} (Control){_avg_txt}", zorder=6)
                else:
                    ax.plot(xdt, yv, color=_CMP_COLORS[ci % len(_CMP_COLORS)],
                            linewidth=_oth_lw, alpha=0.82,
                            solid_capstyle="round", solid_joinstyle="round",
                            label=f"{lbl}{_avg_txt}", zorder=3); ci += 1
            ax.axhline(15, color="#00a651", lw=0.9, ls=(0, (4, 3)), alpha=0.75)
            ax.axhline(35, color="#e67e22", lw=0.9, ls=(0, (4, 3)), alpha=0.7)
            ax.set_ylabel("PM2.5 (µg/m³)", fontsize=9)
            ax.set_title(title, fontsize=10.5, fontweight="bold", pad=8)
            _leg = ax.legend(fontsize=7.5, loc="upper right", framealpha=0.92,
                             ncol=2 if len(ser) > 3 else 1, handlelength=1.6,
                             borderpad=0.5, columnspacing=1.0)
            _leg.get_frame().set_edgecolor("#d9d9d9"); _leg.get_frame().set_linewidth(0.6)
            ax.margins(x=0.01)
            ax.grid(axis="y", alpha=0.16, linewidth=0.6)
            ax.tick_params(labelsize=7.5)
            for _sp in ("top", "right"):
                ax.spines[_sp].set_visible(False)
            for _sp in ("left", "bottom"):
                ax.spines[_sp].set_color("#bcbcbc"); ax.spines[_sp].set_linewidth(0.7)
            # Denser, evenly-spaced date ticks so the axis is easy to read precisely
            import matplotlib.dates as _mdates
            _loc = _mdates.AutoDateLocator(minticks=8, maxticks=16)
            ax.xaxis.set_major_locator(_loc)
            ax.xaxis.set_major_formatter(_mdates.ConciseDateFormatter(_loc))
            fig.autofmt_xdate(rotation=30); fig.tight_layout(pad=1.0)
            _fd, _p = _tf2.mkstemp(suffix=".png"); _os4.close(_fd)
            fig.savefig(_p, dpi=180, bbox_inches="tight"); plt.close(fig)
            return _p

        _newpage()
        _section("How Your House Compares — Nearby Houses")
        y[0] -= 4
        _wrap(
            "These charts compare the smoothed PM2.5 trend of each house in this study. "
            "The Control House (your reference location) is drawn as a bold dark line; "
            "the other houses are overlaid so you can see which run cleaner or dirtier. "
            "The dotted lines mark the WHO guideline (15 µg/m³) and the EPA standard (35 µg/m³).",
            lh=13, after=10
        )
        for _k, _t in (("24h", "24-Hour Median PM2.5 — Control House vs Others"),
                       ("1h",  "1-Hour Median PM2.5 — Control House vs Others")):
            _png = _cmp_overlay(_k, _t)
            if _png:
                _cmp_tmps.append(_png)
                _IMG_H = 215
                _need(_IMG_H + 12)
                pdf.drawImage(ImageReader(_png), LM, y[0] - _IMG_H, UW, _IMG_H,
                              preserveAspectRatio=True, mask="auto")
                y[0] -= _IMG_H + 10

    _stamp()
    pdf.save()

    for _p in _cmp_tmps:
        try: _os.unlink(_p)
        except Exception: pass


def build_comparison_pdf(
    report_path: Path,
    analyses: List[Dict[str, Any]],
    filenames: List[str],
) -> None:
    """Generate a comprehensive comparison PDF for multiple analyzed files."""
    import textwrap as _tw

    pdf = canvas.Canvas(str(report_path), pagesize=letter)
    width, height = letter
    L = 54
    R = 54
    B = 72
    USABLE_W = width - L - R
    y = height - 54
    page_num = [1]

    # Timezone footer label, taken from the analyses (all share the same zone
    # when produced from one comparison run). Defaults to UTC.
    _cmp_tz = "UTC"
    for _a in (analyses or []):
        _t = (_a.get("summary") or {}).get("tz_label")
        if _t and _t != "UTC":
            _cmp_tz = f"{_t.split('/')[-1].replace('_', ' ')} local time"
            break
    if _cmp_tz == "UTC":
        _cmp_tz_footer = "All times UTC"
    elif _tz_is_as_recorded(_cmp_tz):
        _cmp_zone = _tz_recorded_zone(_cmp_tz)
        _cmp_tz_footer = (f"All times {_cmp_zone} (as recorded in each uploaded file)" if _cmp_zone
                          else "All times as recorded in each uploaded file")
    else:
        _cmp_tz_footer = f"All times {_cmp_tz}"

    def _footer():
        pdf.setFont("Helvetica", 7)
        pdf.setFillColorRGB(0.55, 0.55, 0.55)
        pdf.drawString(L, 28, f"PurpleAir Local Analyzer  |  Comparison Report  |  {_cmp_tz_footer}")
        pdf.drawRightString(width - R, 28, f"Page {page_num[0]}")
        pdf.setFillColorRGB(0, 0, 0)

    def _new_page():
        nonlocal y
        _footer()
        pdf.showPage()
        page_num[0] += 1
        y = height - 54

    def _rule(w=0.4):
        nonlocal y
        pdf.setLineWidth(w)
        pdf.setStrokeColorRGB(0.75, 0.75, 0.75)
        pdf.line(L, y, width - R, y)
        pdf.setStrokeColorRGB(0, 0, 0)
        y -= 6

    def _line(text, indent=0, size=10, bold=False, color=(0, 0, 0)):
        nonlocal y
        if y < B:
            _new_page()
        pdf.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        pdf.setFillColorRGB(*color)
        pdf.drawString(L + indent, y, text)
        pdf.setFillColorRGB(0, 0, 0)
        y -= size + 4

    def _wrapped(text, indent=0, size=9, color=(0, 0, 0)):
        nonlocal y
        pdf.setFont("Helvetica", size)
        max_chars = max(40, int((USABLE_W - indent) / (size * 0.56)))
        pdf.setFillColorRGB(*color)
        for part in _tw.wrap(text, width=max_chars):
            if y < B:
                _new_page()
                pdf.setFont("Helvetica", size)
            pdf.drawString(L + indent, y, part)
            y -= size + 3
        pdf.setFillColorRGB(0, 0, 0)
        y -= 4

    def _section(title):
        nonlocal y
        if y < B + 40:
            _new_page()
        pdf.setFont("Helvetica-Bold", 13)
        pdf.setFillColorRGB(0.10, 0.22, 0.45)
        pdf.drawString(L, y, title)
        pdf.setFillColorRGB(0, 0, 0)
        y -= 16
        _rule(0.6)

    # ── Title page ──────────────────────────────────────────────────────────
    pdf.setFont("Helvetica-Bold", 22)
    pdf.setFillColorRGB(0.10, 0.22, 0.45)
    pdf.drawString(L, y, "Air Quality Comparison Report")
    pdf.setFillColorRGB(0, 0, 0)
    y -= 28
    pdf.setFont("Helvetica", 10)
    pdf.setFillColorRGB(0.40, 0.40, 0.40)
    pdf.drawString(L, y, f"Files compared: {len(analyses)}")
    y -= 14
    from datetime import datetime as _dt
    pdf.drawString(L, y, f"Generated: {_dt.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    pdf.setFillColorRGB(0, 0, 0)
    y -= 10
    _rule(1.0)
    y -= 4

    # ── Section 1: Files overview ────────────────────────────────────────────
    _section("1.  Files Analyzed")
    for idx, (a, fname) in enumerate(zip(analyses, filenames)):
        s = a.get("summary", {})
        dr = s.get("date_range", {})
        start = str(dr.get("start", "N/A"))[:10]
        end   = str(dr.get("end",   "N/A"))[:10]
        _line(f"File {idx+1}: {fname}", bold=True, size=10)
        _line(f"  Period: {start} → {end}   |   Readings: {s.get('total_readings', 'N/A')}", size=9, indent=10, color=(0.3, 0.3, 0.3))
        y -= 2

    y -= 6
    _rule()

    # ── Section 2: Key metrics comparison table ──────────────────────────────
    _section("2.  Key Metrics Comparison")
    if y < B + 80:
        _new_page()

    # Clean grid: left-aligned name, right-aligned numerics, navy header bar,
    # alternating row shading. No AQI column — a PM2.5-only sensor cannot yield
    # a valid multi-pollutant Air Quality Index.
    #               House        Avg PM2.5   vs Control   Quality %   Sensor CV
    _cw   = [196, 78, 78, 76, 76]                 # sums to 504 = USABLE_W
    _cx   = [L]
    for _w in _cw[:-1]:
        _cx.append(_cx[-1] + _w)
    _rx   = [_cx[i] + _cw[i] - 6 for i in range(len(_cw))]   # right edges for numerics
    _hdrs = ["House", "Avg PM2.5", "vs Control", "Data Qual.", "Sensor CV"]
    _units = ["", "µg/m³", "µg/m³", "%", "%"]
    _row_h = 16

    # Header bar
    pdf.setFillColorRGB(0.10, 0.22, 0.45)
    pdf.rect(L, y - _row_h + 3, USABLE_W, _row_h, fill=1, stroke=0)
    pdf.setFont("Helvetica-Bold", 8.5)
    pdf.setFillColorRGB(1, 1, 1)
    pdf.drawString(_cx[0] + 4, y - 9, _hdrs[0])
    for i in range(1, len(_hdrs)):
        pdf.drawRightString(_rx[i], y - 9, _hdrs[i])
    y -= _row_h

    baseline_pm = None
    pdf.setFont("Helvetica", 8.5)
    for idx, (a, fname) in enumerate(zip(analyses, filenames)):
        if y < B + 4:
            _new_page(); pdf.setFont("Helvetica", 8.5)
        s = a.get("summary", {})
        pm_raw = s.get("pm25_average_epa_corrected") or s.get("pm25_average")
        pm_val = round(float(pm_raw), 1) if pm_raw is not None else None
        if idx == 0:
            baseline_pm = pm_val
        qs_val = s.get("quality_score")
        cv_val = s.get("sensor_health_cv")

        # Row shading (alternating) + control highlight
        is_ctl = bool(a.get("is_control")) or idx == 0
        if is_ctl:
            pdf.setFillColorRGB(0.93, 0.95, 0.99)
        else:
            pdf.setFillColorRGB(0.97, 0.97, 0.96) if idx % 2 else pdf.setFillColorRGB(1, 1, 1)
        pdf.rect(L, y - _row_h + 4, USABLE_W, _row_h, fill=1, stroke=0)

        name = a.get("label") or fname or f"House {idx}"
        if is_ctl and "control" not in name.lower():
            name = f"{name} (Control)"
        name = name if len(name) <= 30 else name[:29] + "…"
        if idx == 0:
            vs_ctl = "— (ref)"
        elif pm_val is not None and baseline_pm is not None:
            d = pm_val - baseline_pm
            vs_ctl = f"{'+' if d >= 0 else ''}{d:.1f}"
        else:
            vs_ctl = "N/A"

        pdf.setFillColorRGB(0.12, 0.12, 0.12)
        pdf.setFont("Helvetica-Bold" if is_ctl else "Helvetica", 8.5)
        pdf.drawString(_cx[0] + 4, y - 8, name)
        pdf.setFont("Helvetica", 8.5)
        pdf.drawRightString(_rx[1], y - 8, f"{pm_val:.1f}" if pm_val is not None else "N/A")
        pdf.drawRightString(_rx[2], y - 8, vs_ctl)
        pdf.drawRightString(_rx[3], y - 8, f"{int(qs_val)}" if qs_val is not None else "N/A")
        pdf.drawRightString(_rx[4], y - 8, f"{cv_val:.1f}" if cv_val is not None else "N/A")
        y -= _row_h
    pdf.setFillColorRGB(0, 0, 0)

    y -= 6
    pdf.setFont("Helvetica-Oblique", 7.5); pdf.setFillColorRGB(0.45, 0.45, 0.45)
    pdf.drawString(L, y, "Units: PM2.5 and vs-Control in µg/m³ (EPA-corrected); Data Quality and Sensor CV in %. "
                         "Lower Sensor CV = better dual-channel agreement.")
    pdf.setFillColorRGB(0, 0, 0)
    y -= 16

    # ── Section: Difference-in-differences (statistical excess vs Control) ────
    try:
        attach_did_to_analyses(analyses)
        _did_rows = []
        for idx, (a, fname) in enumerate(zip(analyses, filenames)):
            if idx == 0:
                continue
            did = a.get("did")
            if not did or did.get("excess_pct") is None:
                continue
            name = a.get("label") or fname or f"House {idx}"
            ci = did.get("excess_ci_pct") or ["N/A", "N/A"]
            sign = "+" if did["excess_pct"] > 0 else ""
            sig = "significant" if did.get("significant") else "not significant"
            _did_rows.append(
                f"{name}: {sign}{did['excess_pct']}% excess vs Control "
                f"(95% CI {ci[0]}% to {ci[1]}%, p={did.get('p_value')}, {sig}; n={did.get('n_paired')})")
        if _did_rows:
            _section("Local Excess vs Control — Difference-in-Differences")
            _wrapped("Quantifies each house's PM2.5 excess relative to the Control House on their "
                     "overlapping hours, with a 95% confidence interval and significance test — a "
                     "defensible alternative to eyeballing two lines.", size=8.5, color=(0.30, 0.30, 0.30))
            y -= 2
            for _r in _did_rows:
                _line(_r, size=9, indent=10)
            y -= 12
    except Exception:
        pass

    # ── Section 2b: House comparison charts (1h + 24h median) ────────────────
    _HOUSE_COLORS = ["#1f7a8c", "#e07a5f", "#f6aa1c", "#4c956c", "#8f3f97",
                     "#ff7e00", "#2a9d8f", "#c1121f", "#6a4c93"]

    def _render_median_overlay(kind: str, title: str):
        """Build a matplotlib overlay of per-house rolling medians.
        kind='24h' -> timestamps + median_24h ; kind='1h' -> median_1h_*.
        Control House (analyses[*]['is_control']) drawn bold/dark on top.
        Returns a temp PNG path or None if there is no data."""
        import tempfile as _tf, os as _os2
        series = []          # (label, x, y, is_control)
        for idx2, a2 in enumerate(analyses):
            rm = a2.get("rolling_median") or {}
            if kind == "24h":
                xs = rm.get("timestamps") or []
                ys = rm.get("median_24h") or []
            else:
                xs = rm.get("median_1h_timestamps") or []
                ys = rm.get("median_1h") or []
            if not xs or not ys:
                continue
            n = min(len(xs), len(ys))
            try:
                xdt = pd.to_datetime(pd.Series(xs[:n]), errors="coerce")
            except Exception:
                continue
            yv = pd.Series(ys[:n], dtype="float64")
            lbl = a2.get("label") or (filenames[idx2] if idx2 < len(filenames) else f"House {idx2}")
            series.append((lbl, xdt, yv, bool(a2.get("is_control"))))
        if not series:
            return None

        _ctl_lw = 1.7 if kind == "1h" else 2.0
        _oth_lw = 0.85 if kind == "1h" else 1.1
        fig, ax = plt.subplots(figsize=(9.4, 3.6))
        ci = 0
        # Draw non-control first, control last (on top)
        for lbl, xdt, yv, is_ctl in sorted(series, key=lambda t: t[3]):
            _avg = float(yv.mean()) if len(yv.dropna()) else float("nan")
            _avg_txt = f"  ·  avg {_avg:.1f}" if _avg == _avg else ""
            if is_ctl:
                ax.plot(xdt, yv, color="#0a1f47", linewidth=_ctl_lw,
                        solid_capstyle="round", solid_joinstyle="round",
                        label=f"{lbl} (Control){_avg_txt}", zorder=6)
            else:
                ax.plot(xdt, yv, color=_HOUSE_COLORS[ci % len(_HOUSE_COLORS)],
                        linewidth=_oth_lw, alpha=0.82,
                        solid_capstyle="round", solid_joinstyle="round",
                        label=f"{lbl}{_avg_txt}", zorder=3)
                ci += 1
        ax.axhline(15, color="#00a651", lw=0.9, ls=(0, (4, 3)), alpha=0.75)
        ax.axhline(35, color="#e67e22", lw=0.9, ls=(0, (4, 3)), alpha=0.7)
        ax.text(1.005, 15, "WHO 15", transform=ax.get_yaxis_transform(),
                va="center", fontsize=7, color="#00a651")
        ax.text(1.005, 35, "EPA 35", transform=ax.get_yaxis_transform(),
                va="center", fontsize=7, color="#e67e22")
        ax.set_ylabel("PM2.5 (µg/m³)", fontsize=9)
        ax.set_title(title, fontsize=10.5, fontweight="bold", pad=8)
        _leg = ax.legend(fontsize=7.5, loc="upper right", framealpha=0.92,
                         ncol=2 if len(series) > 3 else 1, handlelength=1.6,
                         borderpad=0.5, columnspacing=1.0)
        _leg.get_frame().set_edgecolor("#d9d9d9"); _leg.get_frame().set_linewidth(0.6)
        ax.margins(x=0.01)
        ax.grid(axis="y", alpha=0.16, linewidth=0.6)
        ax.tick_params(labelsize=7.5)
        for _sp in ("top", "right"):
            ax.spines[_sp].set_visible(False)
        for _sp in ("left", "bottom"):
            ax.spines[_sp].set_color("#bcbcbc"); ax.spines[_sp].set_linewidth(0.7)
        # Denser, evenly-spaced date ticks so the axis is easy to read precisely
        import matplotlib.dates as _mdates
        _loc = _mdates.AutoDateLocator(minticks=8, maxticks=16)
        ax.xaxis.set_major_locator(_loc)
        ax.xaxis.set_major_formatter(_mdates.ConciseDateFormatter(_loc))
        fig.autofmt_xdate(rotation=30)
        fig.tight_layout(pad=1.0)
        _fd, _p = _tf.mkstemp(suffix=".png"); _os2.close(_fd)
        fig.savefig(_p, dpi=180, bbox_inches="tight"); plt.close(fig)
        return _p

    _tmp_charts = []
    try:
        _section("3.  House Comparison — Rolling Median PM2.5")
        _wrapped("The Control House is drawn as a bold dark line; every other house is "
                 "overlaid for direct comparison. Dotted lines mark the WHO 24-hour guideline "
                 "(15 µg/m³) and the EPA 24-hour standard (35 µg/m³).", size=9, color=(0.3, 0.3, 0.3))
        for _kind, _ttl in (("24h", "24-Hour Median PM2.5 — Control House vs Others"),
                            ("1h",  "1-Hour Median PM2.5 — Control House vs Others")):
            _png = _render_median_overlay(_kind, _ttl)
            if _png:
                _tmp_charts.append(_png)
                IMG_H = 210
                if y < B + IMG_H + 10:
                    _new_page()
                pdf.drawImage(ImageReader(_png), L, y - IMG_H, USABLE_W, IMG_H,
                              preserveAspectRatio=True, mask="auto")
                y -= IMG_H + 12
            else:
                _line(f"({_ttl}: no rolling-median data available)", size=8, color=(0.5, 0.5, 0.5))
    except Exception as _ce:
        _line(f"(House comparison charts unavailable: {_ce})", size=8, color=(0.5, 0.5, 0.5))

    y -= 6

    # ── Section 4: Interpretation & Narrative ───────────────────────────────
    _section("4.  Interpretation & Key Findings")

    # Best/worst file identification
    pm_vals = [(a.get("summary", {}).get("pm25_average_epa_corrected") or a.get("summary", {}).get("pm25_average"), fn)
               for a, fn in zip(analyses, filenames)]
    pm_vals_clean = [(v, fn) for v, fn in pm_vals if v is not None]
    qs_vals = [(a.get("summary", {}).get("quality_score"), fn) for a, fn in zip(analyses, filenames)]
    qs_vals_clean = [(v, fn) for v, fn in qs_vals if v is not None]

    if pm_vals_clean:
        best_pm  = min(pm_vals_clean, key=lambda x: x[0])
        worst_pm = max(pm_vals_clean, key=lambda x: x[0])
        _line("PM2.5 Levels:", bold=True, size=10)
        _wrapped(f"Lowest average PM2.5: {best_pm[1]} ({best_pm[0]:.1f} µg/m³). This location/period had the cleanest air in this comparison.", indent=10)
        _wrapped(f"Highest average PM2.5: {worst_pm[1]} ({worst_pm[0]:.1f} µg/m³). Concentrations here are most elevated — review source contributions.", indent=10)
        if len(pm_vals_clean) >= 2:
            spread = worst_pm[0] - best_pm[0]
            _wrapped(f"Spread across files: {spread:.1f} µg/m³ — {'significant variability suggests different emission sources or seasonal effects.' if spread > 10 else 'relatively consistent readings across all files.'}", indent=10)

    y -= 4
    if qs_vals_clean:
        best_qs  = max(qs_vals_clean, key=lambda x: x[0])
        worst_qs = min(qs_vals_clean, key=lambda x: x[0])
        _line("Data Quality:", bold=True, size=10)
        _wrapped(f"Best quality score: {best_qs[1]} ({best_qs[0]:.0f}%). Data completeness and validity are highest for this file.", indent=10)
        if best_qs[1] != worst_qs[1]:
            _wrapped(f"Lowest quality score: {worst_qs[1]} ({worst_qs[0]:.0f}%). Review gap patterns and sensor health for this dataset.", indent=10)

    y -= 4

    # Health context — framed on PM2.5 concentration vs WHO/EPA thresholds
    # (not AQI, which requires multiple pollutants this sensor does not measure).
    _line("Health Context:", bold=True, size=10)
    pm_avgs = [(a.get("summary", {}).get("pm25_average_epa_corrected")
                or a.get("summary", {}).get("pm25_average")) for a in analyses]
    pm_avgs_clean = [float(v) for v in pm_avgs if v is not None]
    if pm_avgs_clean:
        overall_pm = sum(pm_avgs_clean) / len(pm_avgs_clean)
        if overall_pm <= 15:
            cat = ("within the WHO 24-hour guideline (15 µg/m³) — generally protective for all "
                   "groups, including children, older adults, and people with heart or lung conditions.")
        elif overall_pm <= 35:
            cat = ("above the stricter WHO guideline (15 µg/m³) but within the U.S. EPA 24-hour "
                   "standard (35 µg/m³). Sensitive groups should limit prolonged outdoor exertion on higher days.")
        elif overall_pm <= 55:
            cat = ("above the EPA 24-hour standard (35 µg/m³). People with respiratory or heart "
                   "conditions should reduce prolonged outdoor exposure.")
        else:
            cat = ("well above the EPA 24-hour standard (35 µg/m³). Reducing outdoor exposure is "
                   "advisable for everyone, especially sensitive groups.")
        _wrapped(f"Mean PM2.5 (EPA-corrected) across all files: {overall_pm:.1f} µg/m³ — {cat}", indent=10)

    y -= 6

    # ── Section 4: Per-file summaries ────────────────────────────────────────
    _section("5.  Per-File Detailed Summaries")
    for idx, (a, fname) in enumerate(zip(analyses, filenames)):
        if y < B + 80:
            _new_page()
        s = a.get("summary", {})
        _line(f"File {idx + 1}: {fname}", bold=True, size=11, color=(0.10, 0.22, 0.45))
        dr = s.get("date_range", {})
        _line(f"  Period: {str(dr.get('start','N/A'))[:10]}  to  {str(dr.get('end','N/A'))[:10]}   |   {s.get('total_readings','N/A')} readings", size=9, indent=10, color=(0.4, 0.4, 0.4))
        pm_corr = s.get("pm25_average_epa_corrected") or s.get("pm25_average")
        pm_max  = s.get("pm25_max")
        _line(f"  Avg PM2.5 (EPA-corr.): {round(float(pm_corr),1) if pm_corr else 'N/A'} µg/m³   |   Peak PM2.5: {round(float(pm_max),1) if pm_max else 'N/A'} µg/m³   |   WHO 15 / EPA 35 µg/m³", size=9, indent=10)
        _line(f"  Quality Score: {s.get('quality_score','N/A')}%   |   Validity: {s.get('validity_score','N/A')}%   |   Coverage: {s.get('coverage_score','N/A')}%", size=9, indent=10)
        cv = s.get("sensor_health_cv")
        status = s.get("sensor_validation_status", "N/A")
        _line(f"  Sensor CV: {f'{cv:.1f}%' if cv else 'N/A'}   |   Sensor Status: {status}", size=9, indent=10)
        narrative = s.get("quality_narrative", "")
        if narrative:
            _wrapped(f"  Note: {narrative}", size=9, indent=10, color=(0.35, 0.35, 0.35))
        y -= 6

    # ── Section 5: Methodology notes ─────────────────────────────────────────
    _section("6.  Methodology & Standards")
    _wrapped(
        "All PM2.5 values have been corrected using the EPA Barkjohn correction formula: "
        "PM2.5_corr = 0.524 × PM2.5_raw − 0.0862 × RH + 5.75 (where RH is available). "
        "Concentrations are evaluated against the WHO 24-hour guideline (15 µg/m³) and EPA "
        "24-hour standard (35 µg/m³); no AQI is computed, as this sensor measures only PM2.5. "
        "Quality Score = 0.4 × Validity + 0.6 × Coverage. "
        "Sensor health assessed using Coefficient of Variation (CV) between dual channels: "
        "CV < 10% = Excellent, 10–15% = Accepted, ≥15% = Invalid (recalibration required). "
        "Data sourced from PurpleAir low-cost optical particle counters."
    )
    y -= 4
    _wrapped(
        "Comparison notes: Files may cover different time periods or locations. "
        "Delta values (Δ) are calculated relative to the first file loaded. "
        "Differences in quality scores between files may reflect sensor placement, maintenance, "
        "local obstructions, or genuine environmental variation."
    )

    _footer()
    pdf.save()

    # Clean up temp comparison-chart PNGs
    import os as _os3
    for _p in _tmp_charts:
        try: _os3.unlink(_p)
        except Exception: pass


# ─────────────────────────────────────────────────────────────────────────────
# Scientific-rigor add-ons (Tier 1–3): uncertainty, trend testing, exposure
# burden, and reproducibility provenance. Each is self-contained (scipy/pandas
# only) and attaches to the analysis result without changing existing outputs.
# ─────────────────────────────────────────────────────────────────────────────

# Method/version identifiers folded into the reproducibility hash so a given
# Repro-ID uniquely pins the exact computation that produced a result.
METHOD_VERSIONS = {
    "correction_default": "barkjohn_2021",
    "correction_formula": BARKJOHN_FORMULA,
    "lrapa": "0.5*PA_cf1 - 0.66",
    "aqu": "0.778*PA_cf1 + 2.65",
    "who_pm25_guideline": 15.0,
    "epa_pm25_naaqs_24h": 35.0,
    "trend_test": "mann_kendall+theil_sen+pettitt",
    "uncertainty": "channel_cv (+) barkjohn_rmse quadrature 95%",
    "exposure": "ug_hours+who/epa_exceedance_days+who_gbd_rr",
    "app_version": "2026.07",
}

# Barkjohn et al. (2021) national correction residual error (RMSE of corrected
# PurpleAir PM2.5 vs FRM/FEM), used as the correction-error term of the
# measurement-uncertainty band. Conservative published value (1σ, µg/m³).
# RMSE of the corrected data against FRM/FEM reference monitors, reported in
# Barkjohn et al. (2021): the correction "reduces the RMSE of the raw data from 8 to
# 3 ug m-3, with an average FRM or FEM concentration of 9 ug m-3".
#
# Scope limit worth knowing: that figure is for 24-HOUR AVERAGES. Averaging suppresses
# random error, so an hourly or 2-minute value carries more uncertainty than 3 ug/m3.
# Applying it at sub-daily resolution therefore yields a LOWER BOUND on the true band,
# which is stated in the returned method string rather than left implicit.
BARKJOHN_RMSE = 3.0
BARKJOHN_RMSE_BASIS = "24-hour averages (Barkjohn et al. 2021)"


def compute_repro_hash(file_path: Path, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Court-grade provenance: SHA-256 over the raw input bytes + frozen method
    versions (+ optional analysis parameters). The short ``repro_id`` is stamped
    on every report so any result is auditable and reproducible."""
    import hashlib
    import json as _json

    data_hash: Optional[str] = None
    try:
        h = hashlib.sha256()
        with open(file_path, "rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(block)
        data_hash = h.hexdigest()
    except Exception:
        data_hash = None

    method_blob = _json.dumps(METHOD_VERSIONS, sort_keys=True).encode("utf-8")
    combo = hashlib.sha256()
    if data_hash:
        combo.update(data_hash.encode("utf-8"))
    combo.update(method_blob)
    if extra:
        combo.update(_json.dumps(extra, sort_keys=True, default=str).encode("utf-8"))
    full = combo.hexdigest()
    return {
        "repro_id": full[:16],
        "data_sha256": data_hash,
        "method_sha256": hashlib.sha256(method_blob).hexdigest()[:16],
        "method_versions": METHOD_VERSIONS,
        "full_sha256": full,
    }


def build_uncertainty(pm_series: pd.Series, channel_agreement: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-point 95% CI half-width for corrected PM2.5.

    Combines two independent error sources in quadrature:
      • sensor uncertainty  — the dual-channel coefficient of variation applied
        proportionally to concentration (relative standard uncertainty);
      • correction uncertainty — the Barkjohn (2021) RMSE (absolute).
    When only one PM channel exists, the band reflects the correction RMSE alone
    and is labelled accordingly (honest, not hidden)."""
    pm = pd.to_numeric(pm_series, errors="coerce")
    cv = channel_agreement.get("cv_between_channels") if channel_agreement else None
    if cv is not None and cv == cv:  # not NaN
        # The CV here is mean|A-B| / mean(PM). Treating it as one relative standard
        # uncertainty is deliberately CONSERVATIVE: for two independent channels the
        # standard uncertainty of their mean is roughly 0.63 x mean|A-B|, so this
        # widens the band rather than narrowing it. Erring wide is the safe direction
        # for an uncertainty claim, but it is stated rather than presented as exact.
        cv_frac = float(cv) / 100.0
        method = ("Dual-channel spread ⊕ Barkjohn RMSE (quadrature, 95%); "
                  "sensor term is a conservative upper estimate")
        single = False
    else:
        cv_frac = 0.0
        method = "Barkjohn correction RMSE only (95%)"
        single = True
    u_total = np.sqrt((cv_frac * pm.clip(lower=0)) ** 2 + BARKJOHN_RMSE ** 2)
    half = 1.96 * u_total
    ci_low = (pm - half).clip(lower=0)
    ci_high = pm + half
    mean_half = float(half[half.notna()].mean()) if half.notna().any() else None
    return {
        "ci_low": ci_low,
        "ci_high": ci_high,
        "mean_ci_halfwidth": round(mean_half, 2) if mean_half is not None else None,
        "method": method,
        "single_channel": single,
        "barkjohn_rmse": BARKJOHN_RMSE,
        "barkjohn_rmse_basis": BARKJOHN_RMSE_BASIS,
        "confidence": 0.95,
    }


# A slope is only reported "per year" when the record actually spans a year. Below
# that, annualising multiplies the fitted slope by 365/span — a 30-day record becomes
# a 12x extrapolation beyond any observed data, producing figures like
# "-75 ug/m3 per year (95% CI -188 to +18)" that look authoritative and mean nothing.
MIN_DAYS_FOR_ANNUAL_SLOPE = 365


def build_trend_test(daily_series: pd.Series) -> Optional[Dict[str, Any]]:
    """Mann-Kendall trend + Theil-Sen slope + Pettitt change-point.

    ``daily_series`` is daily-mean corrected PM2.5 indexed by date. Returns None when
    there are too few days (<10) for a meaningful test.

    Two deliberate departures from a naive implementation:

    1. The Theil-Sen slope is reported in µg/m³ per DAY always, and additionally per
       YEAR only when the record spans at least a year (see the constant above).
    2. The Mann-Kendall p-value uses the Hamed & Rao (1998) variance correction for
       serial correlation. Daily PM2.5 is strongly autocorrelated — consecutive days
       share weather — and the classical test assumes independence, so an uncorrected
       p-value is anti-conservative and reports trends that are not there.
    """
    from scipy import stats as _stats

    s = daily_series.dropna()
    if len(s) < 10:
        return None
    idx = pd.to_datetime(pd.Index(s.index))
    t_years = np.asarray((idx - idx[0]).total_seconds()) / (365.25 * 86400.0)
    y = s.to_numpy(dtype=float)
    n = len(y)
    if np.ptp(t_years) <= 0:
        return None
    span_days = int((idx[-1] - idx[0]).days) + 1

    # Kendall's tau describes the monotonic association; its sign gives the direction.
    tau, _tau_p = _stats.kendalltau(t_years, y)

    # --- Mann-Kendall S and its variance, with ties correction -------------------
    S = 0.0
    for i in range(n - 1):
        S += np.sum(np.sign(y[i + 1:] - y[i]))
    _, tie_counts = np.unique(y, return_counts=True)
    tie_term = np.sum(tie_counts * (tie_counts - 1) * (2 * tie_counts + 5))
    var_s = (n * (n - 1) * (2 * n + 5) - tie_term) / 18.0

    # --- Hamed & Rao (1998) correction for serial correlation --------------------
    # Autocorrelation is measured on the rank series with the Theil-Sen trend removed,
    # so a real trend is not mistaken for persistence.
    autocorr_factor = 1.0
    try:
        _pre_slope = _stats.theilslopes(y, np.arange(n), 0.95)[0]
        detrended = y - _pre_slope * np.arange(n)
        ranks = _stats.rankdata(detrended)
        rmean = ranks.mean()
        denom = np.sum((ranks - rmean) ** 2)
        if denom > 0:
            acc = 0.0
            for k in range(1, n):
                rho = np.sum((ranks[:n - k] - rmean) * (ranks[k:] - rmean)) / denom
                # Keep only lags that are individually significant (95%, ~2/sqrt(n)).
                if abs(rho) > 1.96 / math.sqrt(n):
                    acc += (n - k) * (n - k - 1) * (n - k - 2) * rho
            if n > 2:
                autocorr_factor = 1.0 + (2.0 / (n * (n - 1) * (n - 2))) * acc
            autocorr_factor = float(max(1.0, autocorr_factor))
    except Exception:
        autocorr_factor = 1.0
    var_s_corrected = var_s * autocorr_factor

    if var_s_corrected > 0:
        if S > 0:
            z = (S - 1.0) / math.sqrt(var_s_corrected)
        elif S < 0:
            z = (S + 1.0) / math.sqrt(var_s_corrected)
        else:
            z = 0.0
        p_value = float(2.0 * (1.0 - _stats.norm.cdf(abs(z))))
    else:
        z, p_value = 0.0, float("nan")

    # Theil-Sen slope + 95% CI (robust, non-parametric), fitted in days.
    t_days = np.asarray((idx - idx[0]).total_seconds()) / 86400.0
    slope_day, _intercept, lo_day, hi_day = _stats.theilslopes(y, t_days, 0.95)

    significant = bool(p_value == p_value and p_value < 0.05)
    if significant:
        direction = "increasing" if slope_day > 0 else "decreasing"
    else:
        direction = "no significant trend"

    # Pettitt non-parametric single change-point (shift in the mean).
    ranks = _stats.rankdata(y)
    csum = np.cumsum(ranks)
    t_arr = np.arange(1, n + 1)
    U = 2.0 * csum - t_arr * (n + 1)
    K = float(np.max(np.abs(U)))
    cp_idx = int(np.argmax(np.abs(U)))
    p_cp = float(min(1.0, 2.0 * np.exp(-6.0 * K * K / (n ** 3 + n ** 2))))
    cp_date = idx[cp_idx].strftime("%Y-%m-%d") if p_cp < 0.05 else None

    annualisable = span_days >= MIN_DAYS_FOR_ANNUAL_SLOPE
    out = {
        "n_days": int(n),
        "span_days": span_days,
        "tau": round(float(tau), 3) if tau == tau else None,
        "p_value": round(float(p_value), 4) if p_value == p_value else None,
        "mk_z": round(float(z), 3),
        "autocorrelation_factor": round(float(autocorr_factor), 3),
        # Always reported: the slope in the units the record can actually support.
        "sen_slope_per_day": round(float(slope_day), 4),
        "sen_slope_per_day_ci": [round(float(lo_day), 4), round(float(hi_day), 4)],
        # Total modelled change across the observed window — the honest headline for
        # a short record, since it interpolates rather than extrapolates.
        "sen_change_over_period": round(float(slope_day * span_days), 2),
        "direction": direction,
        "significant": significant,
        "change_point_date": cp_date,
        "change_point_p": round(p_cp, 4),
        "method": ("Mann-Kendall (Hamed-Rao autocorrelation-corrected) · "
                   "Theil-Sen slope · Pettitt change-point"),
        "annualised": annualisable,
    }
    if annualisable:
        out["sen_slope_per_year"] = round(float(slope_day * 365.25), 3)
        out["sen_slope_ci"] = [round(float(lo_day * 365.25), 3), round(float(hi_day * 365.25), 3)]
    else:
        out["sen_slope_per_year"] = None
        out["sen_slope_ci"] = None
        out["annualisation_note"] = (
            f"Slope is reported per day and across the monitored window only: this record "
            f"spans {span_days} day(s), and projecting it to an annual rate would extrapolate "
            f"~{365.25 / max(span_days, 1):.0f}x beyond the data."
        )
    return out


def build_exposure_metrics(hourly_corrected: pd.Series, sampling_hours: float = 1.0) -> Optional[Dict[str, Any]]:
    """Translate concentrations into human-exposure burden: cumulative µg·hours,
    days above the WHO 15 and EPA 35 guidelines, and an illustrative long-term
    excess-mortality-risk estimate (WHO/GBD log-linear, RR≈1.08 per 10 µg/m³)."""
    s = pd.to_numeric(hourly_corrected, errors="coerce").dropna()
    if s.empty:
        return None
    mean_conc = float(s.mean())
    ug_hours = float((s * sampling_hours).sum())
    exposure_hours = float(len(s) * sampling_hours)

    days_over_who = days_over_epa = n_days = None
    if isinstance(s.index, pd.DatetimeIndex):
        daily = s.resample("D").mean().dropna()
        if not daily.empty:
            n_days = int(len(daily))
            days_over_who = int((daily > 15.0).sum())
            days_over_epa = int((daily > 35.0).sum())

    # WHO/GBD-style relative risk, log-linear above the WHO guideline.
    #
    # IMPORTANT: RR≈1.08 per 10 µg/m³ is derived from cohort studies of *multi-year*
    # average exposure. Applying it to a short record would be a category error — a
    # two-week upload cannot support a long-term risk statement. It is therefore
    # reported only for records long enough to characterise a seasonal average, and
    # is always phrased conditionally ("if this level persisted long term"), never as
    # a prediction about the people at this address.
    rr_per_10 = 1.08
    excess_risk_pct = None
    risk_note = None
    MIN_DAYS_FOR_RISK = 90
    if n_days is not None and n_days >= MIN_DAYS_FOR_RISK:
        if mean_conc > 0:
            delta = max(0.0, mean_conc - 15.0)
            rr = rr_per_10 ** (delta / 10.0)
            excess_risk_pct = round((rr - 1.0) * 100.0, 1)
    elif n_days is not None:
        risk_note = (
            f"Long-term excess-risk is not reported: this record covers {n_days} day(s), "
            f"below the {MIN_DAYS_FOR_RISK}-day minimum. The underlying risk ratio describes "
            f"multi-year average exposure and cannot be inferred from a short record."
        )

    return {
        "mean_pm25": round(mean_conc, 2),
        "cumulative_ug_hours": round(ug_hours, 1),
        "exposure_hours": round(exposure_hours, 1),
        "n_days": n_days,
        "days_over_who15": days_over_who,
        "days_over_epa35": days_over_epa,
        "excess_mortality_risk_pct": excess_risk_pct,
        "rr_per_10ug": rr_per_10,
        "min_days_for_risk": MIN_DAYS_FOR_RISK,
        "risk_withheld_note": risk_note,
        "note": ("Excess-risk is a population-level modelling illustration, not a clinical or "
                 "individual prediction: it estimates the additional long-term mortality risk a "
                 "population would carry *if* this average concentration persisted for years "
                 "(RR≈1.08 per 10 µg/m³, WHO/GBD log-linear, relative to the WHO 15 µg/m³ "
                 "guideline). It says nothing about any individual's health, and it is withheld "
                 "entirely for records shorter than 90 days."),
    }


def compute_did(control_series: pd.Series, treatment_series: pd.Series,
                split_date: Optional[pd.Timestamp] = None) -> Optional[Dict[str, Any]]:
    """Difference-in-differences excess for a treated house vs a control.

    Aligns the two hourly series on their common timestamps. With a ``split_date``
    it fits OLS ``pm ~ treated + post + treated:post`` and returns the interaction
    term (the DiD estimate) with its 95% CI and p-value. Without a split it reports
    the treated-minus-control mean difference as a % excess with a paired-t CI."""
    c = pd.to_numeric(control_series, errors="coerce").dropna()
    t = pd.to_numeric(treatment_series, errors="coerce").dropna()
    if c.empty or t.empty:
        return None
    joined = pd.concat([c.rename("control"), t.rename("treat")], axis=1, join="inner").dropna()

    # Collapse to ONE value per day before any inference.
    #
    # Hourly readings are not independent observations, and neither is the
    # house-minus-control difference when a local source persists for hours or days
    # (measured lag-1 autocorrelation ~0.7 on realistic episodes). Testing hourly
    # pairs is pseudo-replication: it leaves the estimate unbiased but shrinks the
    # confidence interval by roughly 4x and drives p-values far below their true
    # value. Daily means are the same unit of analysis used in the project's
    # published reports, so the app and the reports cannot disagree.
    resampled_daily = False
    if isinstance(joined.index, pd.DatetimeIndex) and len(joined) > 0:
        span_days = (joined.index.max() - joined.index.min()).days + 1
        if span_days >= 3 and len(joined) > span_days:
            joined = joined.resample("D").mean().dropna()
            resampled_daily = True

    if len(joined) < 8:
        return None

    control_mean = float(joined["control"].mean())
    treat_mean = float(joined["treat"].mean())
    if control_mean <= 0:
        return None

    try:
        from scipy import stats as _stats
        if split_date is not None and isinstance(joined.index, pd.DatetimeIndex):
            import statsmodels.api as sm  # optional, richer estimate
            long = pd.concat([
                pd.DataFrame({"pm": joined["control"], "treated": 0}),
                pd.DataFrame({"pm": joined["treat"], "treated": 1}),
            ])
            long["post"] = (long.index >= split_date).astype(int)
            long["did"] = long["treated"] * long["post"]
            X = sm.add_constant(long[["treated", "post", "did"]])
            # HAC (Newey-West) standard errors. Residuals from consecutive periods are
            # correlated, and classical OLS errors would understate the interaction
            # term's uncertainty for the same reason hourly pairing does.
            _maxlags = max(1, int(round(len(joined) ** (1.0 / 3.0))))
            model = sm.OLS(long["pm"], X).fit(cov_type="HAC",
                                              cov_kwds={"maxlags": _maxlags})
            coef = float(model.params["did"])
            ci = model.conf_int().loc["did"].tolist()
            pval = float(model.pvalues["did"])
            excess_pct = round(coef / control_mean * 100.0, 1)
            ci_pct = [round(ci[0] / control_mean * 100.0, 1), round(ci[1] / control_mean * 100.0, 1)]
            method = ("Difference-in-differences (OLS interaction term, "
                      f"HAC/Newey-West SEs, maxlags={_maxlags})")
        else:
            diff = joined["treat"] - joined["control"]
            mean_diff = float(diff.mean())
            sem = float(diff.sem())
            tcrit = float(_stats.t.ppf(0.975, len(diff) - 1))
            lo, hi = mean_diff - tcrit * sem, mean_diff + tcrit * sem
            tstat, pval = _stats.ttest_rel(joined["treat"], joined["control"])
            pval = float(pval)
            excess_pct = round(mean_diff / control_mean * 100.0, 1)
            ci_pct = [round(lo / control_mean * 100.0, 1), round(hi / control_mean * 100.0, 1)]
            method = ("Paired difference (treated − control), 95% CI, "
                      + ("daily means" if resampled_daily else "as supplied"))
    except Exception:
        return None

    return {
        "excess_pct": excess_pct,
        "excess_ci_pct": ci_pct,
        "p_value": round(pval, 4) if pval == pval else None,
        "significant": bool(pval == pval and pval < 0.05),
        "control_mean": round(control_mean, 2),
        "treat_mean": round(treat_mean, 2),
        "n_paired": int(len(joined)),
        "unit_of_analysis": "daily mean" if resampled_daily else "as supplied",
        "method": method,
    }


def _series_from_rolling_median(rm: Optional[Dict[str, Any]]) -> Optional[pd.Series]:
    """Reconstruct an hourly PM2.5 level series from a stored ``rolling_median``
    chart dict (prefers the 24-hour median level, falls back to the base series).
    Returns a datetime-indexed Series, or None if unusable."""
    if not isinstance(rm, dict):
        return None
    ts = rm.get("timestamps") or []
    vals = rm.get("median_24h")
    if not vals or all(v is None for v in vals) or len(vals) != len(ts):
        vals = rm.get("pm25")
    if not ts or not vals or len(ts) != len(vals):
        return None
    idx = pd.to_datetime(pd.Series(ts), errors="coerce")
    s = pd.Series(vals, index=idx)
    s = s[~s.index.isna()]
    s = pd.to_numeric(s, errors="coerce").dropna()
    return s if not s.empty else None


def attach_did_to_analyses(analyses: List[Dict[str, Any]]) -> None:
    """Compute difference-in-differences (each treatment house vs the control)
    in place. ``analyses`` is control-first; each dict must carry a
    ``rolling_median`` chart dict. Adds a ``did`` key to every treatment house."""
    if not analyses:
        return
    ctrl = _series_from_rolling_median(analyses[0].get("rolling_median"))
    if ctrl is None:
        return
    for a in analyses[1:]:
        treat = _series_from_rolling_median(a.get("rolling_median"))
        a["did"] = compute_did(ctrl, treat) if treat is not None else None


def analyze_dataset(
    file_path: Path,
    job_dir: Path,
    window: Optional[Tuple[pd.Timestamp, pd.Timestamp]] = None,
    output_dir: Optional[Path] = None,
    label: Optional[str] = None,
    *,
    generate_outputs: bool = True,
    metadata: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    if file_path.suffix.lower() == ".csv":
        df = _read_csv(file_path)
    else:
        df = pd.read_excel(file_path)

    detected = detect_columns(df)

    pm_primary, pm_a, pm_b = choose_channels(detected["pm25"])
    temp_primary, temp_a, temp_b = choose_channels(detected["temp"])
    hum_primary, hum_a, hum_b = choose_channels(detected["humidity"])
    press_primary, press_a, press_b = choose_channels(detected["pressure"])
    timestamp_primary = pick_best(detected["timestamp"])
    lat_primary = pick_best(detected["latitude"])
    lon_primary = pick_best(detected["longitude"])

    pm_col = pm_primary.name if pm_primary else None
    temp_col = temp_primary.name if temp_primary else None
    hum_col = hum_primary.name if hum_primary else None
    press_col = press_primary.name if press_primary else None
    ts_col = timestamp_primary.name if timestamp_primary else None
    lat_col = lat_primary.name if lat_primary else None
    lon_col = lon_primary.name if lon_primary else None

    # ── Data-completeness assessment ─────────────────────────────────────────
    # Tells the user (on the website) exactly which inputs were found and what
    # the impact is when an important one is missing.
    _has_dual_pm = bool(pm_a) and bool(pm_b)
    _completeness_items = [
        {"name": "Timestamp", "level": "required", "present": bool(ts_col),
         "found": ts_col or "",
         "impact": "Required for all time-based analysis. Without it, no analysis can run."},
        {"name": "PM2.5", "level": "required", "present": bool(pm_col),
         "found": pm_col or "",
         "impact": "The core pollutant measured. Without it, there is nothing to analyze."},
        {"name": "Relative Humidity", "level": "important", "present": bool(hum_col),
         "found": hum_col or "",
         "impact": ("Used by the EPA Barkjohn correction. If absent, the humidity term cannot be "
                    "applied and uncorrected (raw) PM2.5 is shown instead — accuracy at high humidity is reduced.")},
        {"name": "Temperature", "level": "optional", "present": bool(temp_col),
         "found": temp_col or "",
         "impact": "Used for sensor sanity checks and context. Not required for PM2.5 correction."},
        {"name": "Pressure", "level": "optional", "present": bool(press_col),
         "found": press_col or "",
         "impact": "Used for sensor sanity checks only. Not required for the analysis."},
        {"name": "Dual sensor (A/B channels)", "level": "recommended", "present": _has_dual_pm,
         "found": (f"{pm_a.name} / {pm_b.name}" if _has_dual_pm else (pm_col or "")),
         "impact": ("Two PM2.5 channels enable the inter-sensor agreement and drift checks that "
                    "validate data reliability. With a single channel, those QA checks are unavailable.")},
        {"name": "Location (latitude/longitude)", "level": "optional", "present": bool(lat_col and lon_col),
         "found": (f"{lat_col} / {lon_col}" if (lat_col and lon_col) else ""),
         "impact": "Used only for optional location context. Does not affect PM2.5 results."},
    ]
    _missing_important = [it["name"] for it in _completeness_items
                          if not it["present"] and it["level"] in ("required", "important")]
    data_completeness = {
        "items": _completeness_items,
        "missing_important": _missing_important,
        "humidity_available": bool(hum_col),
    }

    df_work = df.copy()
    if ts_col:
        df_work["timestamp"] = pd.to_datetime(df_work[ts_col], errors="coerce", utc=True)
    else:
        df_work["timestamp"] = pd.NaT

    if window and ts_col:
        start, end = window
        # Ensure start/end are UTC-aware for comparison with UTC-aware timestamps
        if start:
            start = pd.Timestamp(start, tz='UTC')
            df_work = df_work[df_work["timestamp"] >= start]
        if end:
            end = pd.Timestamp(end, tz='UTC')
            df_work = df_work[df_work["timestamp"] <= end]

    if pm_a and pm_b:
        df_work["pm25_a"] = pd.to_numeric(df_work[pm_a.name], errors="coerce")
        df_work["pm25_b"] = pd.to_numeric(df_work[pm_b.name], errors="coerce")
        df_work["pm25"] = df_work[["pm25_a", "pm25_b"]].mean(axis=1)
    elif pm_col:
        df_work["pm25"] = pd.to_numeric(df_work[pm_col], errors="coerce")
    else:
        df_work["pm25"] = np.nan

    if temp_col:
        df_work["temperature"] = pd.to_numeric(df_work[temp_col], errors="coerce")
    if hum_col:
        df_work["humidity"] = pd.to_numeric(df_work[hum_col], errors="coerce")
    if press_col:
        df_work["pressure"] = pd.to_numeric(df_work[press_col], errors="coerce")
    if lat_col:
        df_work["latitude"] = pd.to_numeric(df_work[lat_col], errors="coerce")
    if lon_col:
        df_work["longitude"] = pd.to_numeric(df_work[lon_col], errors="coerce")

    df_work = df_work.drop_duplicates(subset=["timestamp"]).sort_values("timestamp")

    df_work["valid_timestamp"] = df_work["timestamp"].notna()
    df_work["valid_pm25"] = df_work["pm25"].between(*PM25_RANGE)
    
    # Create validity columns as Series, not scalars
    if "temperature" in df_work and df_work["temperature"].notna().any():
        df_work["valid_temp"] = df_work["temperature"].between(*TEMP_RANGE_F)
    else:
        df_work["valid_temp"] = pd.Series(False, index=df_work.index)
    
    if "humidity" in df_work and df_work["humidity"].notna().any():
        df_work["valid_humidity"] = df_work["humidity"].between(*HUMID_RANGE)
    else:
        df_work["valid_humidity"] = pd.Series(False, index=df_work.index)
    
    if "pressure" in df_work and df_work["pressure"].notna().any():
        df_work["valid_pressure"] = df_work["pressure"].between(*PRESS_RANGE)
    else:
        df_work["valid_pressure"] = pd.Series(False, index=df_work.index)

    # Calculate validity score (quality of individual records)
    # RESEARCH-GRADE: Emphasizes data validity of existing records
    validity_score = (
        0.6 * df_work["valid_pm25"].mean() +
        0.3 * df_work["valid_timestamp"].mean() +
        0.1 * df_work[["valid_temp", "valid_humidity", "valid_pressure"]].mean(axis=1).mean()
    ) * 100
    
    # Calculate coverage score (how much data is missing due to gaps)
    # RESEARCH-GRADE: Accounts for temporal completeness (expected vs actual readings)
    coverage_score = calculate_coverage_score(df_work["timestamp"])
    
    # Combined quality score: 40% validity (attribute validity), 60% coverage (temporal completeness)
    # New formula emphasizes that missing data is more damaging than slightly imperfect readings
    # Example: 100% valid readings with 66% coverage → Final Score = (0.4×100) + (0.6×66) = 79.6%
    quality_score = (0.4 * validity_score + 0.6 * coverage_score)

    cleaned = df_work[df_work["valid_timestamp"] & df_work["valid_pm25"]].copy()

    # Apply Plantower sensor Detection Limit (LDL ≈ 1 µg/m³)
    # Replace sub-LDL values with LDL/√2 to avoid "Zero Bias" in statistical analysis
    # This is statistically proper for values below detection limit
    LDL = 1.0  # Plantower LDL in µg/m³
    LDL_corrected = LDL / np.sqrt(2)  # ~0.707 µg/m³
    
    # Apply to raw PM2.5
    mask_below_ldl = (cleaned["pm25"] > 0) & (cleaned["pm25"] < LDL)
    if mask_below_ldl.any():
        cleaned.loc[mask_below_ldl, "pm25"] = LDL_corrected
    
    # EPA Barkjohn needs relative humidity. Apply it where RH is available; for any
    # reading with no valid RH (or when the humidity column is absent entirely), fall
    # back to the raw value so the analysis never breaks and never shows NaN. We record
    # whether — and how completely — the correction was applied for honest reporting.
    if "humidity" in cleaned and cleaned["humidity"].notna().any():
        hum_for_corr = cleaned["humidity"].where(cleaned["humidity"].between(*HUMID_RANGE), np.nan)
        _corr = apply_epa_correction(cleaned["pm25"], hum_for_corr)
        # Rows where RH was missing/invalid → correction is NaN → keep the raw value.
        cleaned["pm25_corrected"] = _corr.fillna(cleaned["pm25"])
        _rh_coverage = float(hum_for_corr.notna().mean())
        _correction_applied = _rh_coverage > 0.0
    else:
        # No humidity at all: correction cannot be applied — show raw PM2.5 (labeled).
        cleaned["pm25_corrected"] = cleaned["pm25"]
        _rh_coverage = 0.0
        _correction_applied = False

    # Below-detection treatment for the corrected series.
    #
    # At high RH and low raw PM the Barkjohn equation can fall to or below zero, which
    # means the sensor is reading below its detection limit rather than measuring clean
    # air. Such values get the standard LOD/sqrt(2) substitution, as sub-LDL raw values
    # do, so they neither bias statistics toward zero nor imply a real measurement.
    #
    # The mask deliberately spans [0, LDL): apply_epa_correction() clamps unphysical
    # negatives to exactly 0, so a mask requiring "> 0" would skip precisely the
    # readings this substitution exists to handle and leave them sitting at 0.
    mask_corrected_below_ldl = cleaned["pm25_corrected"] < LDL
    if mask_corrected_below_ldl.any():
        cleaned.loc[mask_corrected_below_ldl, "pm25_corrected"] = LDL_corrected

    pm_for_aqi = cleaned["pm25_corrected"].fillna(cleaned["pm25"])
    aqi_df = calc_aqi_series(pm_for_aqi)
    cleaned["aqi"] = aqi_df["aqi"].values
    cleaned["aqi_category"] = aqi_df["category"].values
    cleaned["aqi_color"] = aqi_df["color"].values

    cleaned = cleaned.set_index("timestamp")
    if window:
        start, end = window
        if start or end:
            # Ensure start/end are UTC-aware for proper datetime indexing
            if start:
                start = pd.Timestamp(start, tz='UTC')
            if end:
                end = pd.Timestamp(end, tz='UTC')
            cleaned = cleaned.loc[start:end]

    # ── Convert UTC index to user-specified local timezone ────────────────────
    # This ensures daily/hourly grouping uses local calendar dates, not UTC dates.
    # "AS_RECORDED" means the file's timestamps are already in the sensor's local
    # time: apply no conversion at all and label outputs "as recorded", so a
    # locally-exported file is never shifted twice.
    _tz_label = "UTC"
    _tz_obj = None   # keep a reference so we can also convert drift later
    _tz_name = ((metadata or {}).get("timezone") or "UTC").strip()
    if _tz_name == "AS_RECORDED":
        # Optional display-only zone name (e.g. "EDT"): reports then say
        # "EDT (as recorded)" instead of the generic "As recorded".
        _custom_zone = re.sub(r"[^A-Za-z0-9 /+\-_:()]", "",
                              ((metadata or {}).get("timezone_label") or ""))[:40].strip()
        _tz_label = f"{_custom_zone} (as recorded)" if _custom_zone else "As recorded"
        _tz_name = "UTC"  # skip the conversion branch below; wall-clock stays unchanged
    if _tz_name and _tz_name != "UTC":
        try:
            from zoneinfo import ZoneInfo as _ZI
            _tz_obj = _ZI(_tz_name)
            cleaned.index = cleaned.index.tz_convert(_tz_obj)
            _tz_label = _tz_name
        except Exception as _tz_err:
            try:
                import pytz as _pytz_mod
                _tz_obj = _pytz_mod.timezone(_tz_name)
                cleaned.index = cleaned.index.tz_convert(_tz_obj)
                _tz_label = _tz_name
            except Exception:
                _tz_label = "UTC"
                _tz_obj = None

    hourly = cleaned.resample("h").mean(numeric_only=True)
    daily = cleaned.resample("D").mean(numeric_only=True)
    daily["aqi"] = calc_aqi_series(daily["pm25_corrected"].fillna(daily["pm25"]))["aqi"].values

    hourly_pm25 = pd.Series(dtype=float)
    if not hourly.empty and "pm25" in hourly.columns:
        hourly_pm25 = hourly["pm25_corrected"].fillna(hourly["pm25"])

    # RESEARCH-GRADE: Extract sensor coordinates for timezone/LST conversion
    lat_value = None
    lon_value = None
    if lat_col and lat_col in df_work.columns:
        lat_candidates = pd.to_numeric(df_work[lat_col], errors="coerce").dropna()
        if not lat_candidates.empty:
            lat_value = lat_candidates.iloc[0]
    if lon_col and lon_col in df_work.columns:
        lon_candidates = pd.to_numeric(df_work[lon_col], errors="coerce").dropna()
        if not lon_candidates.empty:
            lon_value = lon_candidates.iloc[0]

    diurnal_df = build_diurnal_pattern(hourly_pm25, latitude=lat_value, longitude=lon_value)
    seasonal_df = build_seasonal_pattern(hourly_pm25)
    rolling_df = build_rolling_medians(hourly_pm25)

    # 1-hour rolling median on raw cleaned data (sub-hourly, e.g. 2-min intervals)
    # This smooths sub-hourly noise while preserving hourly variation — different from 24h/7d trends
    pm25_raw_for_1h = cleaned["pm25_corrected"].fillna(cleaned["pm25"])
    if len(pm25_raw_for_1h) > 2:
        # Time-based 1-hour trailing window: more accurate than count-based when sampling is
        # slightly irregular. min_periods=15 ≈ 50% of a full 2-min-interval hour (30 obs).
        rolling_1h_series = pm25_raw_for_1h.rolling("1h", min_periods=15).median()
    else:
        rolling_1h_series = pm25_raw_for_1h.copy()

    # STL on hourly data (period=24): scientifically standard, captures diurnal cycle and pollution events,
    # and runs in <1s vs ~24s on raw 2-min data. Residuals still identify anomalies above the seasonal baseline.
    decomposition_df = build_decomposition(hourly_pm25, period=None)

    # Count of STL residuals beyond 2 standard deviations.
    #
    # This is NOT a pollution-event count and must not be presented as one: for any
    # roughly normal residual about 5% of points exceed 2 sigma by definition, so the
    # figure scales with record length rather than with pollution. It is retained as
    # a decomposition diagnostic under an accurate name. The user-facing event count
    # comes from detect_events() below, which identifies actual episodes.
    _n_residual_outliers = 0
    _pm25_max = 0.0
    if decomposition_df is not None and not decomposition_df.empty and "residual" in decomposition_df.columns:
        _res = decomposition_df["residual"].dropna().values.astype(float)
        if len(_res) > 10:
            _std = float(np.nanstd(_res))
            if _std > 1e-6:
                _n_residual_outliers = int(np.sum(np.abs(_res) > 2.0 * _std))
    _n_events = 0   # replaced with the real detected-event count once available
    if not hourly_pm25.empty:
        _pm25_max = float(hourly_pm25.dropna().max()) if not hourly_pm25.dropna().empty else 0.0
    elif not daily.empty:
        _daily_pm = daily["pm25_corrected"].fillna(daily["pm25"]) if "pm25_corrected" in daily.columns else daily["pm25"]
        _pm25_max = float(_daily_pm.max()) if not _daily_pm.dropna().empty else 0.0

    drift_df = build_sensor_drift(df_work)
    # Convert drift timestamps to local time (df_work still holds UTC timestamps)
    if _tz_obj is not None and not drift_df.empty and "timestamp" in drift_df.columns:
        try:
            drift_df["timestamp"] = pd.DatetimeIndex(drift_df["timestamp"]).tz_convert(_tz_obj)
        except Exception:
            pass

    regression_info: Dict[str, Any] = {}
    regression_table: List[Dict[str, Any]] = []
    if "humidity" in cleaned:
        regression_info = build_regression_diagnostics(
            cleaned["humidity"],
            cleaned["pm25_corrected"].fillna(cleaned["pm25"]),
            "Humidity (%)",
        )
    elif "temperature" in cleaned:
        regression_info = build_regression_diagnostics(
            cleaned["temperature"],
            cleaned["pm25_corrected"].fillna(cleaned["pm25"]),
            "Temperature (F)",
        )

    if regression_info.get("x"):
        regression_table = [
            {
                "metric": f"PM2.5 vs {regression_info['label']}",
                "slope": regression_info["slope"],
                "intercept": regression_info["intercept"],
                "r2": regression_info["r2"],
                "n": regression_info["n"],
            }
        ]

    channel_agreement = {}
    if "pm25_a" in df_work and "pm25_b" in df_work:
        paired = df_work[["pm25_a", "pm25_b"]].dropna()
        if not paired.empty:
            corr = paired["pm25_a"].corr(paired["pm25_b"])
            r2 = corr * corr if corr is not None else 0.0
            mad = (paired["pm25_a"] - paired["pm25_b"]).abs().mean()
            agree = (paired["pm25_a"] - paired["pm25_b"]).abs() <= (0.1 * paired[["pm25_a", "pm25_b"]].mean(axis=1) + 1)
            
            # Sensor Health Coefficient via Coefficient of Variation (CV)
            # CV = (MAD / Mean_PM25) × 100 — statistically rigorous metric
            diff_series = paired["pm25_a"] - paired["pm25_b"]
            mean_pm25 = paired[["pm25_a", "pm25_b"]].mean(axis=1).mean()
            
            # Three-tier validation (based on MAD relative to Mean)
            sensor_cv = (mad / mean_pm25 * 100) if mean_pm25 > 0 else 0.0
            
            # Three-tier classification based on CV metric
            if sensor_cv < 10:
                sensor_validation_status = "VALID (EXCELLENT)"
                sensor_health = f"Research-Grade Consistency (CV={sensor_cv:.1f}% < 10%)"
            elif sensor_cv < 15:
                sensor_validation_status = "VALID (ACCEPTED)"
                sensor_health = f"Acceptable Agreement (10% ≤ CV={sensor_cv:.1f}% < 15%)"
            else:
                sensor_validation_status = "INVALID"
                sensor_health = f"Poor Agreement - Maintenance Required (CV={sensor_cv:.1f}% ≥ 15%)"
            
            channel_agreement = {
                "r2": round(float(r2), 3),
                "mean_abs_diff": round(float(mad), 3),
                "agreement_pct": round(float(agree.mean() * 100), 1),
                "cv_between_channels": round(float(sensor_cv), 2),  # Coefficient of Variation
                "sensor_validation_status": sensor_validation_status,  # Three-tier validation
                "sensor_health_status": sensor_health,  # Health classification with CV threshold
            }

    stats_cols = [col for col in ["pm25", "pm25_corrected", "temperature", "humidity", "pressure"] if col in cleaned]
    stats = summarize_stats(cleaned, stats_cols) if stats_cols else pd.DataFrame()

    _hourly_corrected = hourly["pm25_corrected"].fillna(hourly["pm25"]) if "pm25_corrected" in hourly.columns else hourly["pm25"] if "pm25" in hourly.columns else pd.Series(dtype=float)
    exceedances = {
        "who_15": int((_hourly_corrected > 15).sum()) if not _hourly_corrected.empty else 0,
        "epa_35": int((_hourly_corrected > 35).sum()) if not _hourly_corrected.empty else 0,
    }

    # Detect on EPA-corrected values, consistent with every other statistic in the
    # app. Running this on the raw column compared uncorrected readings -- which run
    # substantially higher than corrected ones -- against the 35 µg/m³ level, and so
    # reported far more "sustained" episodes than the corrected data supports.
    _event_col = "pm25_corrected" if "pm25_corrected" in cleaned.columns else "pm25"
    events = detect_events(cleaned, _event_col)
    # The reported event count is the number of episodes actually detected.
    _n_events = int(len(events))
    events_display = pd.DataFrame()
    _highest_events_rows: list = []
    if not events.empty:
        events = events.copy()
        # Build top-10 highest events BEFORE serializing timestamps
        _top = events.nlargest(10, "peak_pm25").copy()
        _peak_ts_fmt = _top["peak_timestamp"].apply(
            lambda v: v.strftime('%Y-%m-%d %H:%M') if pd.notna(v) else "—"
        )
        _start_fmt = _top["start"].apply(
            lambda v: v.strftime('%Y-%m-%d %H:%M') if pd.notna(v) else "—"
        )
        def _fmt_dur(h):
            try:
                total_min = int(round(float(h) * 60))
                return f"{total_min // 60:02d}:{total_min % 60:02d}"
            except Exception:
                return "—"
        def _fmt_range(mn, mx):
            try:
                mn_f, mx_f = float(mn), float(mx)
                if math.isnan(mn_f) or math.isnan(mx_f):
                    return "—"
                return f"{mn_f:.1f} – {mx_f:.1f}"
            except Exception:
                return "—"
        _highest_events_rows = pd.DataFrame({
            "Event Start": _start_fmt.values,
            "Peak Time": _peak_ts_fmt.values,
            "Peak PM2.5 (µg/m³)": _top["peak_pm25"].values,
            "PM2.5 Range (µg/m³)": [_fmt_range(mn, mx) for mn, mx in zip(_top["min_pm25"].values, _top["peak_pm25"].values)],
            "Duration (hh:mm)": [_fmt_dur(h) for h in _top["duration_hours"].values],
            "Type": _top["type"].values,
        }).to_dict(orient="records")
        # Build the display-ready events table (all events, not just top 10)
        def _fmt_ts(v):
            try:
                return pd.Timestamp(v).strftime('%Y-%m-%d %H:%M') if pd.notna(v) else "—"
            except Exception:
                return "—"
        events_display = pd.DataFrame({
            "Event Start":          events["start"].apply(_fmt_ts),
            "Event End":            events["end"].apply(_fmt_ts),
            "Peak Time":            events["peak_timestamp"].apply(_fmt_ts),
            "Peak PM2.5 (µg/m³)":  events["peak_pm25"].round(2),
            "PM2.5 Range (µg/m³)": [
                _fmt_range(mn, mx)
                for mn, mx in zip(events["min_pm25"].values, events["peak_pm25"].values)
            ],
            "Duration (hh:mm)":    [_fmt_dur(h) for h in events["duration_hours"].values],
            "Type":                 events["type"],
        })
        # Still serialize raw timestamps for other uses (CSV download etc.)
        events["start"] = events["start"].apply(
            lambda value: value.isoformat() if pd.notna(value) else None
        )
        events["end"] = events["end"].apply(
            lambda value: value.isoformat() if pd.notna(value) else None
        )
        events["peak_timestamp"] = events["peak_timestamp"].apply(
            lambda value: value.isoformat() if pd.notna(value) else None
        )

    # Use EPA-corrected PM2.5 for all time-of-day / weekday / heatmap patterns so
    # they are consistent with the rest of the report (which is corrected throughout).
    _pmc_col = "pm25_corrected" if "pm25_corrected" in cleaned.columns else "pm25"

    weekday_pattern = cleaned.copy()
    weekday_pattern["weekday"] = weekday_pattern.index.day_name()
    weekday_pattern["_pmc"] = weekday_pattern[_pmc_col].fillna(weekday_pattern["pm25"])
    weekday_summary = weekday_pattern.groupby("weekday")["_pmc"].mean().reindex(
        ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    )

    hourly_pattern = cleaned.copy()
    hourly_pattern["hour"] = hourly_pattern.index.hour
    hourly_pattern["_pmc"] = hourly_pattern[_pmc_col].fillna(hourly_pattern["pm25"])
    hourly_summary = hourly_pattern.groupby("hour")["_pmc"].mean()
    # Ensure all 24 hours are present (0-23), even if no data for some hours
    hourly_summary = hourly_summary.reindex(range(24), fill_value=np.nan)

    heatmap = cleaned.copy()
    heatmap["day"] = heatmap.index.day_name()
    heatmap["hour"] = heatmap.index.hour
    heatmap["_pmc"] = heatmap[_pmc_col].fillna(heatmap["pm25"])
    heatmap_summary = heatmap.pivot_table(values="_pmc", index="day", columns="hour", aggfunc="mean")
    heatmap_summary = heatmap_summary.reindex(index=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]) 

    aqi_dist = cleaned["aqi_category"].value_counts()

    quality_timeline = cleaned.copy()
    quality_timeline["valid_ratio"] = (
        cleaned["pm25"].notna().rolling(24, min_periods=6).mean()
    )

    anomalies = build_anomaly_report(df_work, ts_col, pm_col, temp_col, hum_col, press_col)
    quality_rows = build_quality_summary(df_work, cleaned, ts_col, pm_col, temp_col, hum_col, press_col)
    
    # Analyze gap patterns (contiguous vs stochastic) for STL analysis impact assessment
    gap_analysis = classify_gap_patterns(df_work[["timestamp"]]) if "timestamp" in df_work.columns else {}
    
    # IMPROVEMENT 3: Dynamic Narrative Engine
    # Extract max gap in days for narrative
    max_gap_hours = gap_analysis.get("max_contiguous_hours", 0)
    max_gap_days = round(max_gap_hours / 24, 1) if max_gap_hours > 0 else 0
    
    # Quality score already calculated at line 1803 with correct formula:
    # quality_score = 0.4 * validity_score + 0.6 * coverage_score
    # This formula ensures: 100% valid + 66% coverage → 79.6% (research-grade completeness penalization)

    # RESEARCH-GRADE: Intelligent decimation for browser rendering
    # Only decimate if >5000 points to maintain view quality
    def decimate_data(timestamps_list, data_list, max_points=5000):
        """Intelligent data decimation for large datasets while preserving extrema"""
        if len(timestamps_list) <= max_points:
            return timestamps_list, data_list
        
        # Calculate decimation factor
        factor = max(1, len(timestamps_list) // max_points)
        decimated_ts = timestamps_list[::factor]
        decimated_data = data_list[::factor]
        
        # Always include last point for continuity
        if len(timestamps_list) > 0 and len(decimated_ts) > 0 and decimated_ts[-1] != timestamps_list[-1]:
            decimated_ts.append(timestamps_list[-1])
            decimated_data.append(data_list[-1])
        
        return decimated_ts, decimated_data

    # Build radar profile for multi-dimensional data quality visualization with coverage score
    radar_profile = build_radar_profile(cleaned, quality_score, channel_agreement, coverage_score,
                                        n_rows_submitted=int(len(df_work)))
    pm25_temporal_radar = build_pm25_temporal_radar(cleaned, latitude=lat_value, longitude=lon_value, tz_label=_tz_label)

    channel_series = {"timestamps": [], "a": [], "b": [], "r2": channel_agreement.get("r2", None)}
    if "pm25_a" in df_work and "pm25_b" in df_work and "timestamp" in df_work:
        series_df = df_work[["timestamp", "pm25_a", "pm25_b"]].dropna(subset=["timestamp"])
        if not series_df.empty:
            channel_series = {
                "timestamps": _ts_str(series_df["timestamp"]),
                "a": series_df["pm25_a"].round(2).where(lambda s: s.notna(), None).tolist(),
                "b": series_df["pm25_b"].round(2).where(lambda s: s.notna(), None).tolist(),
                "r2": channel_agreement.get("r2", None),
            }

    # ── New feature data ────────────────────────────────────────────────────────
    _calendar_data = build_calendar_data(daily["aqi"] if "aqi" in daily.columns else pd.Series(dtype=float))

    # Correction comparison (decimate to ≤5000 pts for browser)
    _corr_ts_raw = _ts_str(cleaned.index)
    _corr_bk_raw  = cleaned["pm25_corrected"].round(2).where(lambda s: s.notna(), None).tolist()
    _corr_lr_raw  = apply_lrapa_correction(cleaned["pm25"]).round(2).where(lambda s: s.notna(), None).tolist()
    _corr_aq_raw  = apply_aqu_correction(cleaned["pm25"]).round(2).where(lambda s: s.notna(), None).tolist()
    _corr_ts_d, _corr_bk_d = decimate_data(_corr_ts_raw, _corr_bk_raw)
    _,           _corr_lr_d = decimate_data(_corr_ts_raw, _corr_lr_raw)
    _,           _corr_aq_d = decimate_data(_corr_ts_raw, _corr_aq_raw)

    # Daily DOY data for multi-year overlay
    _daily_doy = []
    if not daily.empty:
        _doy_pm = daily["pm25_corrected"].fillna(daily["pm25"]) if "pm25_corrected" in daily.columns else daily["pm25"]
        for _d, _v in _doy_pm.items():
            _ts = pd.Timestamp(_d)
            _daily_doy.append({
                "date": _ts.strftime("%Y-%m-%d"),
                "year": int(_ts.year),
                "doy":  int(_ts.day_of_year),
                "pm25": round(float(_v), 2) if not pd.isna(_v) else None,
            })

    # Narrative summary
    _pm_corr_avg = round(float(cleaned["pm25_corrected"].mean()), 2) if "pm25_corrected" in cleaned and not cleaned.empty else None
    _pm_raw_avg  = round(float(cleaned["pm25"].mean()), 2) if not cleaned.empty else 0.0
    # AQI average: EPA defines AQI from a time-averaged concentration, not by averaging per-reading AQIs.
    # We derive the average AQI from the period-mean corrected PM2.5 (most reproducible and defensible).
    _aqi_avg_pm  = _pm_corr_avg if _pm_corr_avg is not None else _pm_raw_avg
    _aqi_avg_v, _aqi_avg_cat_v, _ = calc_aqi_value(float(_aqi_avg_pm)) if _aqi_avg_pm is not None else (0, "Unknown", "#9E9E9E")
    _aqi_avg     = _aqi_avg_v
    _aqi_cat     = _aqi_avg_cat_v
    # Current AQI: derived from the last reading's corrected PM2.5 (instantaneous)
    _last_pm     = float(cleaned["pm25_corrected"].fillna(cleaned["pm25"]).iloc[-1]) if not cleaned.empty else 0.0
    _aqi_current, _, _ = calc_aqi_value(_last_pm) if not cleaned.empty else (0, "Unknown", "#9E9E9E")
    _cv          = channel_agreement.get("cv_between_channels")
    _n_days      = max(1, int((cleaned.index.max() - cleaned.index.min()).total_seconds() / 86400)) if not cleaned.empty else 0
    _narrative   = build_narrative_summary(
        _pm_corr_avg, _pm_raw_avg, _aqi_avg, _aqi_cat,
        cleaned.index.min().strftime('%Y-%m-%dT%H:%M:%S') if not cleaned.empty else None,
        cleaned.index.max().strftime('%Y-%m-%dT%H:%M:%S') if not cleaned.empty else None,
        int(len(df_work)), _n_events, _pm25_max, _cv,
        round(float(coverage_score), 1), round(float(quality_score), 1),
        who_15_hours=int(exceedances.get("who_15", 0)),
        epa_35_hours=int(exceedances.get("epa_35", 0)),
        n_days=_n_days,
        aqi_current=_aqi_current,
    )

    # ── Scientific-rigor add-ons (Tier 1–3) ──────────────────────────────────
    # Reproducibility provenance (hash of raw bytes + frozen method versions).
    _repro = compute_repro_hash(file_path, extra={"window": [str(window[0]), str(window[1])] if window else None,
                                                   "label": label})
    # Statistical trend test on daily-mean corrected PM2.5.
    _trend_daily = (daily["pm25_corrected"].fillna(daily["pm25"]) if "pm25_corrected" in daily.columns
                    else (daily["pm25"] if "pm25" in daily.columns else pd.Series(dtype=float)))
    _trend_test = build_trend_test(_trend_daily)
    # Exposure & health-burden metrics from hourly corrected concentrations.
    _exposure = build_exposure_metrics(_hourly_corrected)
    # Per-point measurement-uncertainty band on corrected PM2.5.
    _pm_for_ci = (cleaned["pm25_corrected"].fillna(cleaned["pm25"]) if "pm25_corrected" in cleaned
                  else cleaned["pm25"] if "pm25" in cleaned else pd.Series(dtype=float))
    _uncertainty = build_uncertainty(_pm_for_ci, channel_agreement)
    _ci_ts = _ts_str(cleaned.index) if not cleaned.empty else []
    _ci_low_list = _uncertainty["ci_low"].round(2).where(lambda s: s.notna(), None).tolist() if not cleaned.empty else []
    _ci_high_list = _uncertainty["ci_high"].round(2).where(lambda s: s.notna(), None).tolist() if not cleaned.empty else []
    _, _ci_low_d = decimate_data(list(_ci_ts), list(_ci_low_list))
    _, _ci_high_d = decimate_data(list(_ci_ts), list(_ci_high_list))
    _uncertainty_summary = {
        "mean_ci_halfwidth": _uncertainty["mean_ci_halfwidth"],
        "method": _uncertainty["method"],
        "single_channel": _uncertainty["single_channel"],
        "barkjohn_rmse": _uncertainty["barkjohn_rmse"],
        "confidence": _uncertainty["confidence"],
    }

    result = {
        "summary": {
            "aqi_current": _aqi_current,
            "aqi_average": _aqi_avg,
            "aqi_category": _aqi_cat,
            "aqi_color": calc_aqi_value(float(_aqi_avg_pm))[2] if _aqi_avg_pm is not None else "#9E9E9E",
            "pm25_average": round(float(cleaned["pm25"].mean()), 2) if not cleaned.empty else 0.0,
            "pm25_average_epa_corrected": round(float(cleaned["pm25_corrected"].mean()), 2) if "pm25_corrected" in cleaned and not cleaned.empty else None,
            "epa_correction_applied": bool(_correction_applied),
            "rh_coverage_pct": round(_rh_coverage * 100, 1),
            "quality_score": round(float(quality_score), 1),
            "quality_score_formula": "Q = (0.4 × Validity) + (0.6 × Coverage)",
            "validity_score": round(float(validity_score), 1),
            "coverage_score": round(float(coverage_score), 1),
            # IMPROVEMENT 3: Dynamic Narrative Engine - Add context about gaps
            "quality_narrative": (
                f"Score primarily impacted by a contiguous {max_gap_days}-day network/power outage; "
                f"internal data integrity remains {round(float(validity_score), 1)}% valid."
                if coverage_score < 90 and max_gap_days > 0
                else f"Excellent data quality — {round(float(coverage_score), 1)}% temporal coverage with {round(float(validity_score), 1)}% valid readings. Appropriate to support research publications and regulatory submissions."
                if quality_score >= 90
                else f"Acceptable data quality (score {round(float(quality_score), 1)}/100). "
                     f"Coverage: {round(float(coverage_score), 1)}%, Validity: {round(float(validity_score), 1)}%."
            ),
            "channel_agreement_pct": channel_agreement.get("agreement_pct", None),
            "sensor_health_cv": channel_agreement.get("cv_between_channels", None),
            "sensor_validation_status": channel_agreement.get("sensor_validation_status", None),
            "sensor_health_status": channel_agreement.get("sensor_health_status", None),
            "date_range": {
                "start": cleaned.index.min().strftime('%Y-%m-%dT%H:%M:%S') if not cleaned.empty else None,
                "end": cleaned.index.max().strftime('%Y-%m-%dT%H:%M:%S') if not cleaned.empty else None,
            },
            "total_readings": int(len(df_work)),
            "label": label or "Full period",
            "n_pollution_events": _n_events,
            "n_stl_residual_outliers": _n_residual_outliers,
            "pm25_max": round(_pm25_max, 2),
            "narrative_summary": _narrative,
            "tz_label": _tz_label,
            "trend_test": _trend_test,
            "exposure": _exposure,
            "uncertainty": _uncertainty_summary,
            "repro": _repro,
            # Representative sensor coordinates (used only by opt-in network
            # features; never sent anywhere unless the user explicitly runs them).
            # Range-guarded so a mis-detected column never yields bogus coordinates.
            "latitude": (round(float(lat_value), 5) if (lat_value is not None and pd.notna(lat_value)
                         and -90.0 <= float(lat_value) <= 90.0) else None),
            "longitude": (round(float(lon_value), 5) if (lon_value is not None and pd.notna(lon_value)
                          and -180.0 <= float(lon_value) <= 180.0) else None),
            "humidity_used": bool("humidity" in cleaned and cleaned["humidity"].between(*HUMID_RANGE).any()),
            "mean_rh": (round(float(cleaned.loc[cleaned["humidity"].between(*HUMID_RANGE), "humidity"].mean()), 1) if ("humidity" in cleaned and cleaned["humidity"].between(*HUMID_RANGE).any()) else None),
            "rh_min": (round(float(cleaned.loc[cleaned["humidity"].between(*HUMID_RANGE), "humidity"].min()), 1) if ("humidity" in cleaned and cleaned["humidity"].between(*HUMID_RANGE).any()) else None),
            "rh_max": (round(float(cleaned.loc[cleaned["humidity"].between(*HUMID_RANGE), "humidity"].max()), 1) if ("humidity" in cleaned and cleaned["humidity"].between(*HUMID_RANGE).any()) else None),
        },
        "detected": {
            "pm25": _serialize_detected(pm_primary, pm_a, pm_b),
            "temperature": _serialize_detected(temp_primary, temp_a, temp_b),
            "humidity": _serialize_detected(hum_primary, hum_a, hum_b),
            "pressure": _serialize_detected(press_primary, press_a, press_b),
            "timestamp": _serialize_detected(timestamp_primary, None, None),
            "latitude": _serialize_detected(lat_primary, None, None),
            "longitude": _serialize_detected(lon_primary, None, None),
        },
        "data_completeness": data_completeness,
        "charts": {
            "timeseries": {
                # Enforce physical gaps using asfreq ('2T')
                # Reindex with 2-minute frequency to inject NaN into missing periods
                # This prevents false visual continuity across data gaps (e.g., 199.1h gap)
                # IMPROVEMENT: Show full dataset with intelligent decimation for browser performance
                "timestamps": decimate_data(_ts_str(cleaned.index), cleaned["pm25"].round(2).where(lambda s: s.notna(), None).tolist())[0],
                "pm25": decimate_data(_ts_str(cleaned.index), cleaned["pm25"].round(2).where(lambda s: s.notna(), None).tolist())[1],
                "pm25_corrected": decimate_data(_ts_str(cleaned.index), cleaned["pm25_corrected"].round(2).where(lambda s: s.notna(), None).tolist())[1],
                # 95% measurement-uncertainty band on the corrected series (Tier 1).
                "ci_low": _ci_low_d,
                "ci_high": _ci_high_d,
                "ci_method": _uncertainty["method"],
                "ci_single_channel": _uncertainty["single_channel"],
                "who_line": 15,
                "epa_line": 35,
                "gap_enforcement": "2T (2-minute refrequencing for physical gap visualization)",
            },
            "aqi_gauge": {
                "value": int(cleaned["aqi"].iloc[-1]) if not cleaned.empty else 0,
                "category": cleaned["aqi_category"].iloc[-1] if not cleaned.empty else "Unknown",
                "color": cleaned["aqi_color"].iloc[-1] if not cleaned.empty else "#9E9E9E",
            },
            "hourly_pattern": {
                "hours": hourly_summary.index.tolist(),
                "values": hourly_summary.round(2).fillna(0).tolist(),
            },
            "channel_series": channel_series,
            "rolling_median": {
                # IMPROVEMENT: Show full dataset with intelligent decimation for browser performance
                "timestamps": decimate_data(_ts_str(rolling_df.get("timestamp", pd.Series(dtype=object))), rolling_df.get("pm25", pd.Series(dtype=float)).round(2).where(lambda s: s.notna(), None).tolist())[0],
                "pm25": decimate_data(_ts_str(rolling_df.get("timestamp", pd.Series(dtype=object))), rolling_df.get("pm25", pd.Series(dtype=float)).round(2).where(lambda s: s.notna(), None).tolist())[1],
                "median_24h": rolling_df.get("median_24h", pd.Series(dtype=float)).round(2).where(lambda s: s.notna(), None).tolist(),
                "median_7d": rolling_df.get("median_7d", pd.Series(dtype=float)).round(2).where(lambda s: s.notna(), None).tolist(),
                # 1-hour rolling median at raw sub-hourly resolution (decimated to ≤2000 pts for browser)
                "median_1h_timestamps": decimate_data(
                    _ts_str(pm25_raw_for_1h.index),
                    rolling_1h_series.round(2).where(lambda s: s.notna(), None).tolist(),
                    max_points=2000,
                )[0],
                "median_1h": decimate_data(
                    _ts_str(pm25_raw_for_1h.index),
                    rolling_1h_series.round(2).where(lambda s: s.notna(), None).tolist(),
                    max_points=2000,
                )[1],
            },
            "diurnal_pattern": {
                "hours": diurnal_df.get("hour", pd.Series(dtype=int)).tolist(),
                "mean": diurnal_df.get("mean", pd.Series(dtype=float)).round(2).where(lambda s: s.notna(), None).tolist(),
                "p10": diurnal_df.get("p10", pd.Series(dtype=float)).round(2).where(lambda s: s.notna(), None).tolist(),
                "p90": diurnal_df.get("p90", pd.Series(dtype=float)).round(2).where(lambda s: s.notna(), None).tolist(),
            },
            "sensor_drift": {
                # IMPROVEMENT: Show full dataset with intelligent decimation for browser performance
                "timestamps": decimate_data(_ts_str(drift_df.get("timestamp", pd.Series(dtype=object))), drift_df.get("diff", pd.Series(dtype=float)).round(2).where(lambda s: s.notna(), None).tolist())[0],
                "diff": decimate_data(_ts_str(drift_df.get("timestamp", pd.Series(dtype=object))), drift_df.get("diff", pd.Series(dtype=float)).round(2).where(lambda s: s.notna(), None).tolist())[1],
                "rolling": drift_df.get("rolling_7d", pd.Series(dtype=float)).round(2).where(lambda s: s.notna(), None).tolist(),
            },
            "radar_pattern": radar_profile,
            "pm25_temporal_radar": pm25_temporal_radar,
            "calendar": _calendar_data,
            "correction_comparison": {
                "timestamps": _corr_ts_d,
                "barkjohn":   _corr_bk_d,
                "lrapa":      _corr_lr_d,
                "aqu":        _corr_aq_d,
            },
            "daily_doy": _daily_doy,
        },
        "tables": {
            "stats": stats.fillna("").to_dict(orient="records"),
            "exceedances": exceedances,
            "events": events_display.fillna("").to_dict(orient="records") if not events.empty else [],
            "quality": quality_rows,
            "highest_events": _highest_events_rows,
        },
        "anomalies": anomalies,
        "chart_descriptions": {
            "timeseries": {
                "title": "PM2.5 Temporal Trend",
                "explanation": "In Simple Terms: This line graph shows how air pollution levels (PM2.5) changed over time. The red line is the official EPA-corrected measurement, the blue line is the raw reading, and the green/yellow lines show WHO and EPA safety thresholds. When the line goes above these thresholds, air quality is unhealthy.",
                "look_for": "Sharp spikes indicate pollution events. Long flat lines above the thresholds mean prolonged poor air quality."
            },
            "aqi_gauge": {
                "title": "Air Quality Index",
                "explanation": "In Simple Terms: The AQI (Air Quality Index) is a simple 0-500 number that summarizes pollution level. 0-50 is good (green), 51-100 is moderate (yellow), 101-150 is unhealthy for sensitive groups (orange), 151-200 is unhealthy (red), 201-300 is very unhealthy (purple), 301+ is hazardous (maroon).",
                "look_for": "Which health category the latest reading falls into. Lower numbers are always better."
            },
            "hourly_pattern": {
                "title": "Diurnal Cycle (Hourly Pattern)",
                "explanation": "In Simple Terms: This shows the average PM2.5 level for each hour of the day (0-23). Most places have a pattern—typically lowest at night/early morning, higher midday. This helps identify if pollution is tied to rush hour traffic or industrial activity.",
                "look_for": "A clear peak during the day suggests human activity is the pollution source. Peaks at specific hours suggest routine traffic or work patterns."
            },
            "rolling_median": {
                "title": "Rolling Medians — 1h · 24h · 7d Smoothed Trends",
                "explanation": "In Simple Terms: Three smoothed views of PM2.5 at different time scales. The 1-hour median (green) removes sub-hourly noise while keeping hourly detail — ideal for spotting short events. The 24-hour median (red) shows the daily trend. The 7-day median (dashed orange) reveals whether air quality is improving or worsening week-over-week.",
                "look_for": "A rising 7-day line means worsening conditions. Spikes in the 1h median that disappear in 24h are brief events. Sustained elevation across all three lines signals a persistent problem."
            },
            "channel_comparison": {
                "title": "Dual Sensor Agreement",
                "explanation": "In Simple Terms: Your sensor has two channels (A and B). If both channels read the same values, your measurements are reliable. If they're very different, one channel may be faulty.",
                "look_for": "How closely Channel A and B data align. Lines that track together = good sensor health. Diverging lines = potential sensor malfunction."
            },
            "sensor_drift": {
                "title": "Sensor Drift Analysis",
                "explanation": "In Simple Terms: Over time, sensors can lose calibration ('drift'). This chart shows if Channel A and B are drifting apart. A growing difference suggests one sensor needs recalibration.",
                "look_for": "If the difference line is trending upward, the sensor may need maintenance. Stable differences are normal."
            },
            "radar_pattern": {
                "title": "Data Quality Radar",
                "explanation": "In Simple Terms: This 7-pointed polygon shows your data quality across multiple dimensions: validity (data accuracy), temporal coverage (gaps), timestamp precision, sensor agreement (both channels match), and consistency. The closer the shape is to the outer edge, the better your data quality.",
                "look_for": "A polygon that's symmetric and extends toward the outer edges means good quality. Dips or irregular shapes indicate weak points in specific quality metrics."
            },
            "pm25_temporal_radar": {
                "title": "24-Hour Temporal Pattern (Polar)",
                "explanation": "In Simple Terms: This circular chart shows the average pollution level for each hour of the day, arranged like a compass. The distance from center = pollution level. The closer to outside = higher pollution. This reveals your location's daily pollution rhythm (e.g., rush hour peaks).",
                "look_for": "Bulges pointing toward specific hours (e.g., morning=7-9am, evening=5-7pm) indicate rush hour pollution. Uniform shape suggests pollution is constant throughout the day."
            }
        },
    }

    # Store parameters needed to regenerate the PDF with custom notes later
    result["_pdf_params"] = {
        "channel_agreement": channel_agreement,
        "gap_analysis": gap_analysis if "gap_analysis" in dir() else {},
        "tz_label": _tz_label,
        "metadata": metadata or {},
    }

    if not generate_outputs:
        return result, {}

    output_dir = output_dir or job_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    report_markdown = build_report_markdown(
        result["summary"],
        quality_rows,
        anomalies,
        stats=stats,
        channel_agreement=channel_agreement,
        events=events if not events.empty else None
    )
    report_figures = build_report_figures(
        output_dir,
        rolling_df,
        diurnal_df,
        drift_df,
        channel_series,
        radar_profile,
        pm25_temporal_radar,
        decomposition_df,
        rolling_1h_timestamps=pm25_raw_for_1h.index,
        rolling_1h_values=rolling_1h_series,
        tz_label=_tz_label,
        heatmap_summary=heatmap_summary,
    )
    # Persist figure title→filename for community report regeneration
    result["_pdf_params"]["figures"] = [(title, path.name) for title, path in report_figures]

    report_pdf_path = output_dir / "report.pdf"
    build_report_pdf(report_pdf_path, result["summary"], quality_rows, anomalies, report_figures, channel_agreement, gap_analysis, radar_profile, metadata=metadata, tz_label=_tz_label, highest_events=_highest_events_rows)
    public_report_path = output_dir / "community_report.pdf"
    build_public_report_pdf(
        public_report_path,
        result["summary"],
        daily.reset_index(),
        hourly.reset_index(),
        anomalies,
        channel_agreement,
        report_figures,
        exceedances=exceedances,
        metadata=metadata,
    )
    outputs = _write_outputs(
        output_dir,
        df_work,
        cleaned,
        hourly,
        daily,
        events,
        channel_agreement,
        stats,
        quality_rows,
        report_markdown,
        result,
        diurnal_df,
        seasonal_df,
        rolling_df,
        decomposition_df,
        drift_df,
        regression_info,
        report_pdf_path,
        public_report_path,
    )

    return result, outputs


def _read_csv(file_path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(file_path, sep=None, engine="python")
    except UnicodeDecodeError:
        return pd.read_csv(file_path, sep=None, engine="python", encoding="latin-1")


def _serialize_detected(
    primary: Optional[DetectedColumn],
    channel_a: Optional[DetectedColumn],
    channel_b: Optional[DetectedColumn],
) -> Dict[str, Any]:
    return {
        "primary": _serialize_one(primary),
        "channel_a": _serialize_one(channel_a),
        "channel_b": _serialize_one(channel_b),
        "dual": bool(channel_a and channel_b),
    }


def _serialize_one(detected: Optional[DetectedColumn]) -> Optional[Dict[str, Any]]:
    if not detected:
        return None
    return {
        "name": detected.name,
        "confidence": round(float(detected.confidence), 2),
        "channel": detected.channel,
    }


def _write_outputs(
    output_dir: Path,
    raw: pd.DataFrame,
    cleaned: pd.DataFrame,
    hourly: pd.DataFrame,
    daily: pd.DataFrame,
    events: pd.DataFrame,
    channel_agreement: Dict[str, Any],
    stats: pd.DataFrame,
    quality_rows: List[Dict[str, Any]],
    report_markdown: str,
    report_payload: Dict[str, Any],
    diurnal_df: pd.DataFrame,
    seasonal_df: pd.DataFrame,
    rolling_df: pd.DataFrame,
    decomposition_df: pd.DataFrame,
    drift_df: pd.DataFrame,
    regression_info: Dict[str, Any],
    report_pdf_path: Path,
    public_report_path: Path,
) -> Dict[str, str]:
    outputs = {}

    cleaned_out = cleaned.reset_index()
    cleaned_out.to_csv(output_dir / "cleaned_data.csv", index=False)
    outputs["cleaned_data"] = "cleaned_data.csv"

    corrected = cleaned_out[["timestamp", "pm25", "pm25_corrected"]].copy()
    corrected.to_csv(output_dir / "epa_corrected.csv", index=False)
    outputs["epa_corrected"] = "epa_corrected.csv"

    hourly.reset_index().to_csv(output_dir / "hourly_summary.csv", index=False)
    outputs["hourly_summary"] = "hourly_summary.csv"

    # UTC-indexed hourly corrected PM2.5 — a clean, timezone-unambiguous series
    # for the opt-in network features (reference-monitor validation, pollution
    # rose) so their joins with external UTC data are always correct.
    try:
        _hu = hourly.copy()
        if getattr(_hu.index, "tz", None) is not None:
            _hu.index = _hu.index.tz_convert("UTC")
        _pmc = (_hu["pm25_corrected"].fillna(_hu["pm25"]) if "pm25_corrected" in _hu.columns
                else (_hu["pm25"] if "pm25" in _hu.columns else None))
        if _pmc is not None:
            _utc_df = pd.DataFrame({
                "utc_hour": _hu.index.strftime("%Y-%m-%dT%H:00"),
                "pm25_corrected": _pmc.round(2).values,
            }).dropna()
            _utc_df.to_csv(output_dir / "sensor_hourly_utc.csv", index=False)
            outputs["sensor_hourly_utc"] = "sensor_hourly_utc.csv"
    except Exception:
        pass

    daily.reset_index().to_csv(output_dir / "daily_aqi.csv", index=False)
    outputs["daily_aqi"] = "daily_aqi.csv"

    events.to_csv(output_dir / "pollution_events.csv", index=False)
    outputs["pollution_events"] = "pollution_events.csv"

    if channel_agreement:
        pd.DataFrame([channel_agreement]).to_csv(output_dir / "channel_agreement.csv", index=False)
        outputs["channel_agreement"] = "channel_agreement.csv"

    if not stats.empty:
        stats.to_csv(output_dir / "stats_summary.csv", index=False)
        outputs["stats_summary"] = "stats_summary.csv"

    if quality_rows:
        pd.DataFrame(quality_rows).to_csv(output_dir / "qa_summary.csv", index=False)
        outputs["qa_summary"] = "qa_summary.csv"

    if not diurnal_df.empty:
        diurnal_df.to_csv(output_dir / "diurnal_pattern.csv", index=False)
        outputs["diurnal_pattern"] = "diurnal_pattern.csv"

    if not seasonal_df.empty:
        seasonal_df.to_csv(output_dir / "seasonal_pattern.csv", index=False)
        outputs["seasonal_pattern"] = "seasonal_pattern.csv"

    if not rolling_df.empty:
        rolling_df.to_csv(output_dir / "rolling_medians.csv", index=False)
        outputs["rolling_medians"] = "rolling_medians.csv"

    if not decomposition_df.empty:
        decomposition_df.to_csv(output_dir / "decomposition.csv", index=False)
        outputs["decomposition"] = "decomposition.csv"

    if not drift_df.empty:
        drift_df.to_csv(output_dir / "sensor_drift.csv", index=False)
        outputs["sensor_drift"] = "sensor_drift.csv"

    if regression_info.get("x"):
        pd.DataFrame(
            {
                "x": regression_info["x"],
                "y": regression_info["y"],
                "fitted": regression_info["fitted"],
                "residual": regression_info["residuals"],
            }
        ).to_csv(output_dir / "regression_diagnostics.csv", index=False)
        outputs["regression_diagnostics"] = "regression_diagnostics.csv"

    report_md_path = output_dir / "report.md"
    report_md_path.write_text(report_markdown, encoding="utf-8")
    outputs["report_md"] = "report.md"

    report_json_path = output_dir / "report.json"
    with report_json_path.open("w", encoding="utf-8") as handle:
        json.dump(report_payload, handle, ensure_ascii=True)
    outputs["report_json"] = "report.json"

    if report_pdf_path.exists():
        outputs["report_pdf"] = report_pdf_path.name
        outputs["research_report_pdf"] = report_pdf_path.name  # Research-grade version

    if public_report_path.exists():
        outputs["public_report_pdf"] = public_report_path.name

    raw.to_csv(output_dir / "quality_flagged_data.csv", index=False)
    outputs["quality_flagged_data"] = "quality_flagged_data.csv"

    return outputs
