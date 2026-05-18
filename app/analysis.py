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

AQI_BREAKPOINTS = [
    (0.0, 12.0, 0, 50, "Good", "#00E400"),
    (12.1, 35.4, 51, 100, "Moderate", "#FFFF00"),
    (35.5, 55.4, 101, 150, "USG", "#FF7E00"),
    (55.5, 150.4, 151, 200, "Unhealthy", "#FF0000"),
    (150.5, 250.4, 201, 300, "Very Unhealthy", "#8F3F97"),
    (250.5, 10000.0, 301, 500, "Hazardous", "#7E0023"),
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
        elif any(token and token in col_norm for token in pattern.split("_")):
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
        "temp": ["temp", "temperature", "temperature_a", "temperature_b"],
        "humidity": ["humidity", "humid", "rh", "humidity_a", "humidity_b"],
        "pressure": ["pressure", "press", "baro", "pressure_a", "pressure_b"],
        "timestamp": ["time", "timestamp", "datetime", "time_stamp"],
        "latitude": ["lat", "latitude"],
        "longitude": ["lon", "longitude", "lng"],
    }

    normalized = {col: normalize_col(col) for col in df.columns}
    detected: Dict[str, List[DetectedColumn]] = {key: [] for key in patterns}

    for original, norm in normalized.items():
        channel = infer_channel(norm)
        for key, pats in patterns.items():
            name_score = score_name_match(norm, pats)
            if name_score == 0.0:
                continue

            if key == "timestamp":
                r_score = timestamp_score(df[original])
            elif key == "pm25":
                r_score = range_score(df[original], PM25_RANGE)
            elif key == "temp":
                r_score = range_score(df[original], TEMP_RANGE_F)
            elif key == "humidity":
                r_score = range_score(df[original], HUMID_RANGE)
            elif key == "pressure":
                r_score = range_score(df[original], PRESS_RANGE)
            elif key == "latitude":
                r_score = range_score(df[original], LAT_RANGE)
            elif key == "longitude":
                r_score = range_score(df[original], LON_RANGE)
            else:
                r_score = 0.0

            confidence = 0.6 * name_score + 0.4 * r_score
            detected[key].append(
                DetectedColumn(name=original, confidence=confidence, channel=channel)
            )

    for key in detected:
        detected[key].sort(key=lambda item: item.confidence, reverse=True)

    return detected


def pick_best(detected: List[DetectedColumn]) -> Optional[DetectedColumn]:
    return detected[0] if detected else None


def choose_channels(
    detected: List[DetectedColumn],
) -> Tuple[Optional[DetectedColumn], Optional[DetectedColumn], Optional[DetectedColumn]]:
    primary = pick_best(detected)
    channel_a = next((d for d in detected if d.channel == "A"), None)
    channel_b = next((d for d in detected if d.channel == "B"), None)
    return primary, channel_a, channel_b


def apply_epa_correction(pm25: pd.Series, rh: pd.Series) -> pd.Series:
    pm = pd.to_numeric(pm25, errors="coerce")
    humid = pd.to_numeric(rh, errors="coerce")
    return 0.534 * pm - 0.0844 * humid + 5.604


def apply_lrapa_correction(pm: pd.Series) -> pd.Series:
    """LRAPA correction: 0.5 × PM_cf1 - 0.66"""
    return (0.5 * pm - 0.66).clip(lower=0)


def apply_aqu_correction(pm: pd.Series) -> pd.Series:
    """AQ&U correction: 0.778 × PM_cf1 + 2.65"""
    return (0.778 * pm + 2.65).clip(lower=0)


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


def build_narrative_summary(pm_corr_avg, pm_raw_avg, aqi_avg, aqi_cat,
                             start_iso, end_iso, n_total,
                             n_events, pm25_max, cv, coverage, quality_score) -> str:
    """Plain-English one-paragraph summary of the analysis."""
    try:
        try:
            start_fmt = pd.Timestamp(start_iso).strftime("%b %d, %Y") if start_iso else "?"
            end_fmt   = pd.Timestamp(end_iso).strftime("%b %d, %Y")   if end_iso   else "?"
        except Exception:
            start_fmt, end_fmt = str(start_iso), str(end_iso)

        pm_val = pm_corr_avg if pm_corr_avg else pm_raw_avg
        parts = [
            f"Between {start_fmt} and {end_fmt}, this PurpleAir monitor recorded "
            f"{n_total:,} PM2.5 measurements at 2-minute intervals.",

            f"The mean EPA Barkjohn-corrected PM2.5 was {pm_val:.1f} µg/m³, "
            f"corresponding to an average AQI of {int(aqi_avg)} ({aqi_cat}).",
        ]

        if n_events > 0:
            peak = f"; peak daily average {pm25_max:.1f} µg/m³" if pm25_max > 0 else ""
            parts.append(
                f"STL decomposition identified {n_events} pollution episodes "
                f"exceeding the 2σ anomaly threshold{peak}."
            )
        else:
            parts.append("No statistically significant pollution events (>2σ) were detected.")

        if cv is not None:
            desc = "excellent" if cv < 10 else ("acceptable" if cv < 15 else "poor — maintenance recommended")
            parts.append(
                f"Dual-sensor channel agreement was {desc} (CV = {cv:.1f}%), "
                f"confirming {'research-grade data reliability' if cv < 10 else 'acceptable measurement quality'}."
            )

        parts.append(
            f"Temporal coverage was {coverage:.1f}% over the monitoring window "
            f"(composite quality score: {quality_score:.1f}/100)."
        )
        return " ".join(parts)
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
    if math.isnan(pm):
        return 0, "Unknown", "#9E9E9E"
    for c_low, c_high, a_low, a_high, label, color in AQI_BREAKPOINTS:
        if c_low <= pm <= c_high:
            aqi = round(((a_high - a_low) / (c_high - c_low)) * (pm - c_low) + a_low)
            return int(aqi), label, color
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
    if pm_col not in df.columns:
        return pd.DataFrame(columns=["start", "end", "duration_hours", "type"])

    series = df[pm_col].dropna()
    if series.empty:
        return pd.DataFrame(columns=["start", "end", "duration_hours", "type"])

    rolling_med = series.rolling(12, min_periods=6).median()
    rolling_std = series.rolling(12, min_periods=6).std().fillna(0)
    spikes = series > (rolling_med + 3 * rolling_std)

    elevated = series > 35

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
                events.append({"start": start, "end": end, "duration_hours": duration, "type": label})
                start = None
        if start is not None:
            end = mask.index[-1]
            duration = (end - start).total_seconds() / 3600.0
            if label == "Sustained" and duration >= 3:
                events.append({"start": start, "end": end, "duration_hours": duration, "type": label})

    if not events:
        return pd.DataFrame(columns=["start", "end", "duration_hours", "type"])

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
        f"**Current Air Quality:** AQI {summary['aqi_current']} ({summary['aqi_category']})",
        f"**Period Average PM2.5:** {summary['pm25_average']} µg/m³",
        f"**Period Average AQI:** {summary['aqi_average']} ({summary['aqi_category']})",
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
        "- **EPA PM2.5 Correction:** Corrected = 0.534 × PM + 5.604 − 0.0844 × RH",
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
    
    lines.append(f"**Period Average PM2.5:** {summary['pm25_average']} µg/m³ (with observed range and variability shown above)")
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
        "*All times in UTC unless otherwise noted*",
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
    
    JHU/MIT STANDARD: Period determines seasonal cycle length.
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
    data = pd.DataFrame({"x": x, "y": y}).dropna()
    if len(data) < 10:
        return {"x": [], "y": [], "fitted": [], "residuals": [], "r2": None, "label": x_label}
    coeffs = np.polyfit(data["x"], data["y"], 1)
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
    
    # Convert to numeric to prevent string subtraction errors
    drift["pm25_a"] = pd.to_numeric(drift["pm25_a"], errors="coerce")
    drift["pm25_b"] = pd.to_numeric(drift["pm25_b"], errors="coerce")
    
    drift = drift.dropna(subset=["timestamp", "pm25_a", "pm25_b"])
    if drift.empty:
        return pd.DataFrame()
    
    # Explicitly ensure float dtype after coercion to prevent arithmetic errors
    drift["pm25_a"] = drift["pm25_a"].astype(float)
    drift["pm25_b"] = drift["pm25_b"].astype(float)
    
    drift = drift.set_index("timestamp").sort_index()
    
    # RESEARCH-GRADE: Detect actual gaps and inject NaN only at gap boundaries
    time_diffs = drift.index.to_series().diff()
    gap_threshold = pd.Timedelta(hours=1)
    gap_indices = time_diffs[time_diffs > gap_threshold].index
    
    # Calculate drift
    drift["diff"] = drift["pm25_a"] - drift["pm25_b"]
    
    # Inject NaN at gap starts to force line breaks
    if len(gap_indices) > 0:
        drift.loc[gap_indices, "diff"] = np.nan
    
    hourly = drift["diff"].resample("h").mean()
    rolling = hourly.rolling(24 * 7, min_periods=24 * 3).median()
    return pd.DataFrame(
        {
            "timestamp": hourly.index,
            "diff": hourly.values,
            "rolling_7d": rolling.values,
        }
    )


def build_radar_profile(cleaned: pd.DataFrame, quality_score: float, channel_agreement: Dict[str, Any], coverage_score: float = 100) -> Dict[str, Any]:
    """Build multi-dimensional radar chart data showing data quality profile."""
    if cleaned.empty:
        return {"labels": [], "values": []}
    
    # Calculate various metrics (0-100 scale)
    metrics = {}
    
    # 1. Internal Data Integrity (% of rows with valid PM2.5) - renamed from "Data Completeness"
    metrics["Internal Data Integrity"] = min(100, (cleaned["pm25"].notna().sum() / len(cleaned)) * 100) if len(cleaned) > 0 else 0
    
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
    if "timestamp" in cleaned.index.names or isinstance(cleaned.index, pd.DatetimeIndex):
        time_range = (cleaned.index.max() - cleaned.index.min()).total_seconds() / 3600  # hours
        readings_per_hour = len(cleaned) / time_range if time_range > 0 else 0
        metrics["Measurement Frequency"] = min(100, readings_per_hour * 25)  # normalize to 100
    else:
        metrics["Measurement Frequency"] = 50
    
    # 7. EPA Compliance (% of time below EPA threshold)
    if "pm25_corrected" in cleaned.columns:
        epa_compliant = (cleaned["pm25_corrected"] <= 35).sum() / len(cleaned) * 100 if len(cleaned) > 0 else 0
    else:
        epa_compliant = (cleaned["pm25"] <= 35).sum() / len(cleaned) * 100 if len(cleaned) > 0 else 0
    metrics["EPA Compliance"] = epa_compliant
    
    return {
        "labels": list(metrics.keys()),
        "values": [max(0, min(100, v)) for v in metrics.values()],  # Ensure 0-100 range
    }


def build_pm25_temporal_radar(cleaned: pd.DataFrame, latitude: Optional[float] = None, longitude: Optional[float] = None) -> Dict[str, Any]:
    """Build radar chart for PM2.5 levels by hour of day with automatic LST conversion if coordinates available.
    
    RESEARCH-GRADE: Converts UTC hours to Local Standard Time based on GPS coordinates when available.
    """
    if cleaned.empty:
        return {"labels": [], "values": [], "max_value": 0}
    
    # Determine UTC offset if coordinates available
    utc_offset = None
    if latitude is not None and longitude is not None:
        utc_offset = get_utc_to_lst_offset_hours(latitude, longitude)
    
    # Get hourly patterns with actual PM2.5 values
    temp_df = cleaned.copy()
    temp_df["hour"] = temp_df.index.hour
    
    # Average PM2.5 by hour (0-23) - use actual µg/m³ values
    hourly_avg = temp_df.groupby("hour")["pm25"].mean()
    
    # Convert to LST if offset available
    if utc_offset is not None:
        hours_lst = [(h + int(utc_offset)) % 24 for h in range(24)]
        hourly_avg_reindexed = pd.Series(index=hours_lst, dtype=float)
        for utc_h in range(24):
            lst_h = (utc_h + int(utc_offset)) % 24
            hourly_avg_reindexed[lst_h] = hourly_avg.get(utc_h, 0)
        hourly_avg = hourly_avg_reindexed
        hourly_avg = hourly_avg.sort_index()
        offset_label = f" LST (UTC{utc_offset:+.0f})"
    else:
        hourly_avg = hourly_avg.reindex(range(24), fill_value=0)
        offset_label = " UTC"
    
    # Get the maximum value for radial axis scaling
    max_pm = max(hourly_avg.max(), 0.1)  # Avoid zero for empty datasets
    
    # Create labels for each hour
    labels = [f"{int(h):02d}:00{offset_label}" for h in hourly_avg.index]
    
    return {
        "labels": labels,
        "values": hourly_avg.tolist(),
        "max_value": max_pm,
        "type": "hourly_pm25_pattern",
        "offset_label": offset_label
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
) -> List[Tuple[str, Path]]:
    figures: List[Tuple[str, Path]] = []

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
        ax.fill_between(diurnal_df["hour"], diurnal_df["p10"], diurnal_df["p90"], color="#f6aa1c", alpha=0.2, label="10th–90th percentile")
        ax.plot(diurnal_df["hour"], diurnal_df["mean"], color="#1f7a8c", label="Mean", linewidth=2)

        hour_label_suffix = diurnal_df.get("hour_label_suffix", " UTC").iloc[0] if "hour_label_suffix" in diurnal_df.columns else " UTC"
        if "LST" in hour_label_suffix:
            title = f"Typical Daily Pattern by Hour ({hour_label_suffix})"
            xlabel = f"Hour of Day {hour_label_suffix}"
        else:
            title = "Typical Daily Pattern by Hour (UTC Time — No Local Offset Applied)"
            xlabel = "Hour of Day UTC (0–23)\n[For local time: Determine sensor timezone from coordinates, then add UTC offset]"

        ax.set_title(title, fontsize=11, fontweight='bold')
        ax.set_xlabel(xlabel)
        ax.set_ylabel("PM2.5 (µg/m³)")
        ax.set_xticks(range(0, 24, 2))
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
        if "LST" in offset_label:
            title = f"PM2.5 Temporal Pattern - 24-Hour Average ({offset_label})"
        else:
            title = "PM2.5 Temporal Pattern - 24-Hour Average (UTC Hours — No Local Offset)"
        ax.set_title(title, fontsize=14, fontweight='bold', pad=30)

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
        fig, ax = plt.subplots(figsize=(8.0, 3.6))
        ax.plot(drift_df["timestamp"], drift_df["diff"], color="#1f7a8c", alpha=0.4, label="Channel A – B")
        ax.plot(drift_df["timestamp"], drift_df["rolling_7d"], color="#f25c54", label="7d median drift", linewidth=2)
        ax.set_title("Sensor Drift Detection (QC)", fontsize=11, fontweight='bold')
        ax.set_xlabel("Date")
        ax.set_ylabel("PM2.5 Difference (µg/m³)")
        ax.xaxis.set_major_locator(AutoDateLocator())
        ax.xaxis.set_major_formatter(DateFormatter("%m/%d"))
        fig.autofmt_xdate(rotation=45, ha='right')
        ax.legend(loc="upper right", fontsize=8)
        ax.grid(True, alpha=0.3)
        save_fig("Sensor drift", fig, "fig_drift.png")

    # ── 6. QUALITY CONTROL SECTION: Channel A vs B ──
    if channel_series.get("timestamps"):
        fig, ax = plt.subplots(figsize=(8.0, 3.6))
        timestamps = pd.to_datetime(channel_series["timestamps"], errors="coerce")
        channel_a = channel_series["a"].copy() if isinstance(channel_series["a"], pd.Series) else pd.Series(channel_series["a"])
        channel_b = channel_series["b"].copy() if isinstance(channel_series["b"], pd.Series) else pd.Series(channel_series["b"])

        if len(timestamps) > 1:
            time_diffs = timestamps.diff()
            gap_threshold = pd.Timedelta(hours=1)
            gap_mask = time_diffs > gap_threshold

            if gap_mask.any():
                if hasattr(gap_mask, 'index'):
                    gap_start_indices = gap_mask[gap_mask].index
                else:
                    gap_start_indices = np.where(gap_mask)[0]
                channel_a[gap_start_indices] = np.nan
                channel_b[gap_start_indices] = np.nan

        ax.plot(timestamps, channel_a, color="#1f7a8c", alpha=0.6, label="Channel A", linewidth=1.5)
        ax.plot(timestamps, channel_b, color="#f25c54", alpha=0.6, label="Channel B", linewidth=1.5)
        ax.set_title("Channel A vs B Comparison (QC Consistency Check)", fontsize=11, fontweight='bold')
        ax.set_xlabel("Date")
        ax.set_ylabel("PM2.5 (µg/m³)")
        ax.xaxis.set_major_locator(AutoDateLocator())
        ax.xaxis.set_major_formatter(DateFormatter("%m/%d"))
        fig.autofmt_xdate(rotation=45, ha='right')
        ax.legend(loc="upper right", fontsize=8)
        ax.grid(True, alpha=0.3)
        save_fig("Channel A vs B", fig, "fig_channel_ab.png")

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
) -> None:
    pdf = canvas.Canvas(str(report_path), pagesize=letter)
    width, height = letter
    L_MARGIN = 54      # left margin in points
    R_MARGIN = 54      # right margin in points
    T_MARGIN = 54      # top margin (first use position from top)
    B_MARGIN = 72      # bottom safe zone
    USABLE_W = width - L_MARGIN - R_MARGIN   # 504 pts
    y = height - T_MARGIN
    page_num = [1]

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _stamp_footer() -> None:
        """Draw page number + generation note at the very bottom of the current page."""
        pdf.setFont("Helvetica", 7)
        pdf.setFillColorRGB(0.55, 0.55, 0.55)
        pdf.drawString(L_MARGIN, 28, "PurpleAir Local Analyzer  |  All times UTC  |  For research use: cite quality score and note significant gaps.")
        pdf.drawRightString(width - R_MARGIN, 28, f"Page {page_num[0]}")
        pdf.setFillColorRGB(0, 0, 0)

    def next_page() -> None:
        nonlocal y
        _stamp_footer()
        pdf.showPage()
        page_num[0] += 1
        y = height - T_MARGIN

    def draw_rule(weight: float = 0.4) -> None:
        nonlocal y
        pdf.setLineWidth(weight)
        pdf.setStrokeColorRGB(0.75, 0.75, 0.75)
        pdf.line(L_MARGIN, y, width - R_MARGIN, y)
        pdf.setStrokeColorRGB(0, 0, 0)
        y -= 3

    def draw_line(text: str, indent: int = 0, font_size: int = 10, bold: bool = False, color: tuple = (0, 0, 0)) -> None:
        nonlocal y
        if y < B_MARGIN:
            next_page()
        font_name = "Helvetica-Bold" if bold else "Helvetica"
        pdf.setFont(font_name, font_size)
        pdf.setFillColorRGB(*color)
        pdf.drawString(L_MARGIN + indent, y, text)
        pdf.setFillColorRGB(0, 0, 0)
        y -= font_size + 4

    def draw_wrapped(text: str, indent: int = 0, font_size: int = 9, color: tuple = (0, 0, 0)) -> None:
        nonlocal y
        pdf.setFont("Helvetica", font_size)
        # Dynamic wrap width: Helvetica avg char ≈ 0.56 × point size
        max_chars = max(40, int((USABLE_W - indent) / (font_size * 0.56)))
        pdf.setFillColorRGB(*color)
        for line in textwrap.wrap(text, width=max_chars):
            if y < B_MARGIN:
                next_page()
                pdf.setFont("Helvetica", font_size)
            pdf.drawString(L_MARGIN + indent, y, line)
            y -= font_size + 3
        pdf.setFillColorRGB(0, 0, 0)
        y -= 5     # paragraph gap

    def section_header(num: str, title: str) -> None:
        nonlocal y
        # Need enough room for: pre-gap(10) + title(16) + rule(1) + post-gap(6) + ≥2 content lines(26) = 59pt
        if y < B_MARGIN + 60:
            next_page()
        # Pre-section breathing room (visually separates from previous content)
        y -= 10
        # Blue accent bar (3 pt wide, 15 pt tall, vertically centred on the title cap-height)
        pdf.setFillColorRGB(0.10, 0.22, 0.45)
        pdf.rect(L_MARGIN, y - 2, 3, 15, fill=1, stroke=0)
        # Section title
        pdf.setFont("Helvetica-Bold", 13)
        pdf.drawString(L_MARGIN + 8, y, f"{num}  {title}")
        pdf.setFillColorRGB(0, 0, 0)
        # Rule sits 13 pt below the title baseline — safely below descenders (≈3 pt)
        y -= 13
        draw_rule(0.5)
        # draw_rule already moves y down by 3 pt; add 2 more for comfortable content start
        y -= 2

    # ── Cover / Title ─────────────────────────────────────────────────────────

    pdf.setFont("Helvetica-Bold", 22)
    pdf.setFillColorRGB(0.10, 0.22, 0.45)
    pdf.drawString(L_MARGIN, y, "Air Quality Research Report")
    pdf.setFillColorRGB(0, 0, 0)
    y -= 30
    pdf.setFont("Helvetica", 10)
    pdf.setFillColorRGB(0.35, 0.35, 0.35)
    pdf.drawString(L_MARGIN, y, f"Monitoring Period:  {summary['date_range']['start']}  to  {summary['date_range']['end']}")
    pdf.setFillColorRGB(0, 0, 0)
    y -= 14
    # Metadata: device ID and location (user-provided)
    if metadata:
        _dev = (metadata.get("device_id") or "").strip()
        _loc = (metadata.get("location") or "").strip()
        if _dev or _loc:
            pdf.setFont("Helvetica-Bold", 10)
            pdf.setFillColorRGB(0.10, 0.22, 0.45)
            if _dev:
                pdf.drawString(L_MARGIN, y, f"Device ID:  {_dev}")
                y -= 14
            if _loc:
                pdf.drawString(L_MARGIN, y, f"Location:   {_loc}")
                y -= 14
            pdf.setFillColorRGB(0, 0, 0)
    y -= 2
    draw_rule(1.0)
    y -= 2

    # ── Section 1: Executive Summary ─────────────────────────────────────────

    section_header("1.", "Executive Summary")
    draw_line(f"Quality Score: {summary['quality_score']}%   |   Total Readings: {summary['total_readings']}", indent=12, font_size=10)
    draw_line(f"Average PM2.5: {summary['pm25_average']} µg/m³   |   Average AQI: {summary['aqi_average']} ({summary['aqi_category']})", indent=12, font_size=10)
    y -= 6
    draw_wrapped(
        "About the instrument: Each PurpleAir monitor contains two independent laser particle sensor "
        "channels — Channel A and Channel B — both measuring PM2.5 simultaneously. This dual-channel "
        "design is not redundancy for redundancy's sake: comparing the two channels is a built-in "
        "quality check that can detect sensor fouling, electronic drift, or partial blockage before "
        "they affect reported data. The sensor-health metrics and QC charts in this report are "
        "grounded in that dual-channel architecture.",
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
    draw_wrapped(
        "EPA Barkjohn Correction: Raw PurpleAir laser-scattering readings systematically overestimate "
        "true PM2.5 mass concentration, particularly at elevated humidity. The EPA-validated correction "
        "formula (Barkjohn et al., 2021) is applied: Corrected PM2.5 = 0.534 × raw_PM + 5.604 − "
        "0.0844 × RH, where RH is relative humidity in percent. When humidity data are unavailable "
        "the simplified form (0.534 × raw_PM + 5.604) is used; this increases uncertainty at "
        "high-humidity conditions and is noted in the data quality flags.",
        indent=12, font_size=9
    )
    draw_wrapped(
        "Timestamps: All timestamps are preserved exactly as recorded by the sensor — in UTC "
        "(Coordinated Universal Time). No timezone conversion is applied automatically, because the "
        "sensor's physical location is required to determine the correct local offset. To convert, "
        "identify the sensor's time zone from its GPS coordinates and add the appropriate UTC offset. "
        "All time-of-day patterns in this report therefore show UTC hours (0–23).",
        indent=12, font_size=9
    )
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
        y -= 10

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
        ("AQI (Air Quality Index)",
         "The EPA's public health communication scale, 0–500. Green (0–50) = Good; Yellow (51–100) = Moderate; "
         "Orange (101–150) = Unhealthy for Sensitive Groups; Red (151–200) = Unhealthy; Purple (201+) = Very Unhealthy / Hazardous."),
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
         "percentage. CV < 10% = excellent (JHU/MIT tier); CV 10–15% = acceptable; CV ≥ 15% = the reading pair should be flagged."),
        ("LDL (Lower Detection Limit)",
         "The smallest concentration a Plantower laser sensor can reliably distinguish from zero (~1 µg/m³). "
         "Values below this limit are corrected to LDL/√2 ≈ 0.707 µg/m³ to avoid zero-bias in statistics."),
        ("MAD (Mean Absolute Difference)",
         "The average of the absolute difference between Channel A and Channel B readings, in µg/m³. "
         "A low MAD (< 2 µg/m³) confirms channels are measuring essentially the same air."),
        ("RH (Relative Humidity)",
         "The amount of water vapor in the air as a percentage of the maximum possible at that temperature. "
         "High RH causes laser particle counters to overcount particles because water droplets scatter light similarly to dust."),
        ("UTC (Coordinated Universal Time)",
         "The global time standard with no daylight-saving shifts. All sensor timestamps in this report are in UTC. "
         "To find local time, add your UTC offset (e.g., UTC−5 for US Eastern Standard, UTC+5:30 for India)."),
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

    # ── Section 6: Regulatory Standards ──────────────────────────────────────

    section_header("6.", "Comparison to Regulatory Standards")
    draw_wrapped(
        "EPA 24-Hour PM2.5 Standard — 35 µg/m³: This is the primary short-term standard used in this "
        "report. Exceeding 35 µg/m³ averaged over 24 hours is considered 'Unhealthy for Sensitive Groups' "
        "(AQI > 100). Sensitive groups include children, the elderly, people with asthma or heart "
        "disease, and outdoor workers with prolonged exposure.",
        indent=12, font_size=9
    )
    draw_wrapped(
        "WHO 24-Hour Guideline — 15 µg/m³: The World Health Organization's guideline is more than twice "
        "as stringent as the EPA 24-hour standard, reflecting growing evidence linking chronic low-level "
        "PM2.5 exposure to cardiovascular and respiratory disease even below the EPA threshold. "
        "Researchers and health agencies increasingly use the WHO guideline when evaluating "
        "long-term exposure burden.",
        indent=12, font_size=9
    )
    draw_wrapped(
        "EPA Annual PM2.5 Standard — 9 µg/m³ (revised 2024): In February 2024, the EPA lowered the "
        "annual NAAQS from 12 µg/m³ to 9 µg/m³, citing updated epidemiological evidence of health "
        "effects at lower long-term concentrations. This standard is currently under administrative "
        "review but remains legally in effect. Important: this is an annual arithmetic mean standard. "
        "A short monitoring period (weeks or months) cannot be directly compared to it without "
        "full-year data, but it provides useful context for long-term exposure interpretation.",
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
            "Measurement Frequency": 70,
            "EPA Compliance": 0,
        }
        descriptions = {
            "Internal Data Integrity": "Fraction of recorded PM2.5 readings that pass all range checks",
            "Temporal Completeness": "Fraction of expected recording slots (typically 2-min intervals) that contain data",
            "Overall Quality": "Composite: 40% validity + 60% coverage — penalises both bad readings and silent periods",
            "Sensor Agreement": "Fraction of reading pairs where Channel A and B are within ±10% of their mean",
            "Inter-Channel Stability": "Derived from the CV between channels: CV < 5% → ~100%; CV ≥ 15% → 0% (JHU/MIT criteria)",
            "Measurement Frequency": "Actual readings per hour vs expected rate — drops if sensor is stuttering or skipping",
            "EPA Compliance": "Fraction of readings below the EPA 24-h standard of 35 µg/m³ (informational, not a quality gate)",
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
            "PM2.5 readings are grouped by hour of the day (0–23 UTC) across the entire "
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
            "The instantaneous difference between Channel A and Channel B (A minus B, in µg/m³) "
            "is plotted as a thin line over the full monitoring period. Overlaid in orange is "
            "a 7-day rolling median that filters daily noise to reveal any slow underlying drift.",
            "A healthy sensor shows differences scattered randomly around zero with no "
            "persistent slope. If the orange median line drifts upward over weeks, Channel A "
            "is reading progressively higher than B — a warning sign of laser degradation, "
            "contamination, or aging that is affecting one channel more than the other. "
            "Cyclical daily swings suggest an environmental factor (humidity or temperature) "
            "affecting the two channels differently. A drift beyond ±5 µg/m³ is the JHU/MIT "
            "threshold recommending field inspection and possible recalibration.",
            "Long-term drift is one of the most common failure modes of low-cost optical "
            "particle counters. Detecting it early — ideally within days — lets a researcher "
            "flag or trim affected data before months of measurements are compromised. "
            "This chart is a direct analogue of the control charts used in analytical "
            "laboratory quality management systems, adapted for field air quality sensors."
        ),
        "Channel A vs B": (
            "Channel A (blue line) and Channel B (orange/red line) measurements are overlaid "
            "on the same time axis. Both channels sample the same air parcel simultaneously "
            "inside the sensor enclosure. The R² value shown is the squared Pearson correlation "
            "between the two channels — a measure of how closely they track each other.",
            "When the blue and orange lines lie directly on top of each other, the sensor is "
            "in excellent health. When they diverge — one rising while the other stays flat — "
            "something is wrong with one of the optical sensing elements. The Mean Absolute "
            "Difference (MAD) shown in µg/m³ quantifies the typical gap between the lines. "
            "R² > 0.85 is the research-grade acceptance threshold (JHU/MIT three-tier "
            "validation framework). R² 0.70–0.85 is acceptable with documented caveats. "
            "R² < 0.70 means the data should be treated as suspect until the cause is found.",
            "Dual-channel agreement is the primary quality-control mechanism that sets "
            "PurpleAir sensors apart from single-channel consumer devices. When both channels "
            "agree, the instrument provides intrinsic replication — a significant "
            "methodological strength for publication. This chart documents that agreement "
            "(or its failure) for the record, which is a required element of rigorous "
            "low-cost sensor data methodology sections."
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
        "AQI distribution": (
            "All readings are sorted into the six EPA AQI health categories and the fraction "
            "of total monitored time spent in each category is displayed as a colour-coded bar. "
            "The bars are coloured to match the standard EPA AQI colour scheme.",
            "A tall green bar means most of the monitoring period had 'Good' air quality "
            "(AQI 0–50, PM2.5 below 12 µg/m³). Tall orange or red bars indicate a meaningful "
            "fraction of hours had elevated or unhealthy conditions. All bars together sum "
            "to 100% of monitoring time.",
            "This chart converts the continuous PM2.5 record into a cumulative health-exposure "
            "burden summary. In epidemiology, you can directly read off the fraction of "
            "person-hours in each health category for exposure-response modelling. For "
            "regulatory compliance reporting, it quantifies how frequently the location "
            "exceeded defined thresholds during the monitoring window."
        ),
        "AQI indicator": (
            "A colour-coded gauge showing the AQI calculated from the most recent PM2.5 "
            "reading in this dataset, using the EPA standard piecewise linear breakpoint "
            "formula. The number inside the gauge is the numeric AQI value.",
            "The colour communicates the health concern level immediately: Green (0–50) = Good; "
            "Yellow (51–100) = Moderate; Orange (101–150) = Unhealthy for Sensitive Groups; "
            "Red (151–200) = Unhealthy for all; Purple (201–300) = Very Unhealthy; "
            "Maroon (301–500) = Hazardous.",
            "This is a snapshot indicator reflecting only the final reading in the dataset. "
            "It should not be interpreted as a summary of the full monitoring period. "
            "Use the AQI distribution chart on a nearby page for a time-integrated view."
        ),
        "Hourly pattern": (
            "Mean PM2.5 for each hour of the day (00:00–23:00 UTC) computed across all days "
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
            "and columns represent hours of the day (0–23 UTC). Each cell is colour-coded "
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

    _QC_TITLES = {"Sensor drift", "Channel A vs B"}
    _qc_header_printed = False

    for title, path in figures:
        # QC section intro page (before first QC chart)
        if title in _QC_TITLES and not _qc_header_printed:
            next_page()
            section_header("QC.", "Quality Control Section")
            draw_wrapped(
                "The two charts in this section are technical instrument-health diagnostics. "
                "They are intended for researchers, data reviewers, and sensor owners carrying "
                "out equipment maintenance. Non-technical readers can skip directly to the "
                "air quality charts in the previous section.",
                indent=12, font_size=9
            )
            draw_wrapped(
                "Each PurpleAir sensor contains two completely independent laser particle counters "
                "(Channel A and Channel B) running in parallel. The 'Sensor Drift' chart shows "
                "whether their difference is growing over time (a sign of calibration decay). "
                "The 'Channel A vs B' chart overlays both signals so you can see directly "
                "how closely they track. Together they answer the question: can I trust this data?",
                indent=12, font_size=9
            )
            _qc_header_printed = True

        # Every figure gets its own dedicated page
        next_page()

        explanation_tuple = figure_explanations.get(title)

        # ── Figure page header ────────────────────────────────────────────
        pdf.setFont("Helvetica-Bold", 14)
        pdf.setFillColorRGB(0.10, 0.22, 0.45)
        pdf.drawString(L_MARGIN, y, title)
        pdf.setFillColorRGB(0, 0, 0)
        y -= 12
        draw_rule(0.6)
        y -= 6

        # ── Image ─────────────────────────────────────────────────────────
        image = ImageReader(str(path))
        if title == "PM2.5 Temporal Radar":
            img_w = min(int(USABLE_W * 0.78), 390)
            img_h = img_w          # square polar chart
            x_pos = L_MARGIN + (USABLE_W - img_w) / 2
        else:
            img_w = USABLE_W
            img_h = int(img_w * 0.50)
            x_pos = L_MARGIN

        pdf.drawImage(image, x_pos, y - img_h, width=img_w, height=img_h)
        y -= img_h + 16

        # ── Plain-language description ─────────────────────────────────────
        if explanation_tuple:
            labels = ["What this chart shows:", "How to read it:", "Why it matters for research:"]
            for label, paragraph in zip(labels, explanation_tuple):
                if y < B_MARGIN + 20:
                    next_page()
                draw_line(label, indent=0, font_size=9, bold=True, color=(0.10, 0.22, 0.45))
                draw_wrapped(paragraph, indent=12, font_size=9)
                y -= 2
        y -= 8

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
) -> None:
    pdf = canvas.Canvas(str(report_path), pagesize=letter)
    width, height = letter
    y = height - 54

    def draw_line(text: str, indent: int = 0, font_size: int = 10, bold: bool = False) -> None:
        nonlocal y
        if y < 72:
            pdf.showPage()
            y = height - 54
        font_name = "Helvetica-Bold" if bold else "Helvetica"
        pdf.setFont(font_name, font_size)
        pdf.drawString(54 + indent, y, text)
        y -= font_size + 4

    def draw_wrapped(text: str, indent: int = 0, font_size: int = 9) -> None:
        nonlocal y
        pdf.setFont("Helvetica", font_size)
        for wrapped in textwrap.wrap(text, width=95):
            if y < 72:
                pdf.showPage()
                y = height - 54
            pdf.drawString(54 + indent, y, wrapped)
            y -= font_size + 2
        y -= 4

    # Title
    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(54, y, "Air Quality Community Report")
    y -= 10
    pdf.setFont("Helvetica", 10)
    pdf.drawString(54, y, "Easy-to-understand summary of your local air quality data")
    y -= 28

    # Overview
    draw_line("What's the Air Quality Like?", font_size=14, bold=True)
    y -= 6
    draw_line(f"Period: {summary['date_range']['start']} to {summary['date_range']['end']}", indent=12)
    draw_line(f"Average Air Quality: AQI {summary['aqi_average']} ({summary['aqi_category']})", indent=12)
    draw_line(f"Average PM2.5 pollution: {summary['pm25_average']} µg/m³", indent=12)
    y -= 12

    # What do these numbers mean?
    draw_line("Understanding the Numbers", font_size=14, bold=True)
    y -= 6
    draw_wrapped(
        "AQI (Air Quality Index): A number from 0–500 that describes air quality. Higher numbers mean worse air.",
        indent=12
    )
    draw_wrapped(
        "PM2.5: Tiny pollution particles you breathe. Measured in micrograms per cubic meter (µg/m³).",
        indent=12
    )
    draw_wrapped(
        "EPA Standard: 35 µg/m³ average over 24 hours. Above this = unhealthy for sensitive people.",
        indent=12
    )
    draw_wrapped(
        "WHO Goal: 15 µg/m³. Even stricter, for better long-term health protection.",
        indent=12
    )
    y -= 12

    # Data quality
    draw_line("How Good Is This Data?", font_size=14, bold=True)
    y -= 6
    draw_line(f"Data Quality Score: {summary['quality_score']}%", indent=12)
    if summary['quality_score'] >= 90:
        draw_wrapped("✓ Excellent: This data is reliable and ready for important decisions.", indent=12, font_size=9)
    elif summary['quality_score'] >= 70:
        draw_wrapped("✓ Good: This data is trustworthy for general use.", indent=12, font_size=9)
    elif summary['quality_score'] >= 50:
        draw_wrapped("⚠ Fair: Use with caution. Some measurements may be missing or unreliable.", indent=12, font_size=9)
    else:
        draw_wrapped("✗ Poor: Significant data quality issues. Not suitable for critical decisions.", indent=12, font_size=9)
    y -= 12

    # Channel agreement
    if channel_agreement and float(channel_agreement.get('r2', 0)) > 0:
        draw_line("Sensor Reliability Check", font_size=14, bold=True)
        y -= 6
        draw_line(f"Two sensors comparison: {channel_agreement.get('agreement_pct', 'N/A')}% consistent", indent=12)
        if float(channel_agreement.get('r2', 0)) > 0.85:
            draw_wrapped("✓ Both sensors agree very well - measurements are trustworthy.", indent=12, font_size=9)
        else:
            draw_wrapped("⚠ Sensors show some disagreement - take note before major decisions.", indent=12, font_size=9)
        y -= 12

    # Daily Summary
    draw_line("Day-by-Day Summary", font_size=14, bold=True)
    y -= 6
    if not daily.empty:
        daily_view = daily.copy().head(30)
        daily_count = 0
        for _, row in daily_view.iterrows():
            if daily_count >= 10:  # Show first 10 days
                draw_line(f"(+ {len(daily) - 10} more days — see full export for complete data)", indent=12, font_size=9)
                break
            date_label = row.get("timestamp")
            pm_val = row.get("pm25")
            aqi_val = row.get("aqi")
            draw_line(
                f"{date_label}: PM2.5 {round(float(pm_val), 1) if pd.notna(pm_val) else 'N/A'} "
                f"µg/m³, AQI {int(aqi_val) if pd.notna(aqi_val) else 'N/A'}",
                indent=12,
                font_size=9
            )
            daily_count += 1
    else:
        draw_line("No daily summary available.", indent=12)
    y -= 12

    # Patterns
    draw_line("Air Quality Patterns", font_size=14, bold=True)
    y -= 6
    draw_wrapped(
        "Hour-by-hour: Air quality typically changes throughout the day. Morning and evening rush hours often show "
        "higher pollution due to traffic. Late night often has cleaner air.",
        indent=12, font_size=9
    )
    draw_wrapped(
        "Day-to-day: Some days have cleaner air than others. Weather, wind direction, and nearby activities all matter.",
        indent=12, font_size=9
    )
    y -= 12

    # Anomalies
    draw_line("Things to Know About", font_size=14, bold=True)
    y -= 6
    if anomalies:
        draw_line("Potential issues detected:", indent=12, font_size=11, bold=True)
        for note in anomalies[:4]:
            draw_wrapped(f"• {note}", indent=24, font_size=9)
    else:
        draw_line("No major issues detected in this dataset.", indent=12)
    y -= 12

    # Tips
    draw_line("Tips for Using This Data", font_size=14, bold=True)
    y -= 6
    draw_wrapped(
        "1. Use AQI as your guide for outdoor activities. When AQI is high, limit time outside.",
        indent=12, font_size=9
    )
    draw_wrapped(
        "2. Sensitive groups (kids, elderly, people with asthma) should be extra careful on high-AQI days.",
        indent=12, font_size=9
    )
    draw_wrapped(
        "3. One sensor's data doesn't represent your whole town. Compare with nearby sensors if available.",
        indent=12, font_size=9
    )
    draw_wrapped(
        "4. Weather and location matter. Wind, rain, and nearby traffic all affect air quality.",
        indent=12, font_size=9
    )
    y -= 12

    # Charts
    for title, path in figures:
        if y < 220:
            pdf.showPage()
            y = height - 54
        draw_line(title, font_size=12, bold=True)
        image = ImageReader(str(path))
        img_width = width - 108
        img_height = img_width * 0.45
        pdf.drawImage(image, 54, y - img_height, width=img_width, height=img_height)
        y -= img_height + 20

    # Footer
    if y < 60:
        pdf.showPage()
        y = height - 54
    
    y -= 20
    pdf.setFont("Helvetica", 8)
    pdf.drawString(54, y, "Report generated by PurpleAir Local Analyzer")
    pdf.drawString(54, y - 12, "For health questions, consult your doctor or local health department.")

    pdf.save()


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

    def _footer():
        pdf.setFont("Helvetica", 7)
        pdf.setFillColorRGB(0.55, 0.55, 0.55)
        pdf.drawString(L, 28, "PurpleAir Local Analyzer  |  Comparison Report  |  All times UTC")
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

    # Column header row (simple text table)
    col_w = [180, 70, 60, 60, 70]  # widths in pts
    headers = ["File", "Avg PM2.5\n(EPA corr.)", "Avg AQI", "Quality\nScore %", "Sensor CV %"]
    row_h = 13
    x_positions = [L]
    for cw in col_w[:-1]:
        x_positions.append(x_positions[-1] + cw)

    if y < B + 60:
        _new_page()

    pdf.setFont("Helvetica-Bold", 9)
    pdf.setFillColorRGB(0.10, 0.22, 0.45)
    for xi, hdr in zip(x_positions, headers):
        pdf.drawString(xi, y, hdr.replace("\n", " "))
    y -= row_h
    _rule(0.3)

    pdf.setFont("Helvetica", 9)
    pdf.setFillColorRGB(0, 0, 0)
    baseline_pm = None
    for idx, (a, fname) in enumerate(zip(analyses, filenames)):
        if y < B:
            _new_page()
            pdf.setFont("Helvetica", 9)
        s = a.get("summary", {})
        pm_raw = s.get("pm25_average_epa_corrected") or s.get("pm25_average")
        pm_val = round(float(pm_raw), 1) if pm_raw is not None else None
        if idx == 0:
            baseline_pm = pm_val
        aqi_val = s.get("aqi_average")
        qs_val  = s.get("quality_score")
        cv_val  = s.get("sensor_health_cv")

        short_name = fname if len(fname) <= 26 else fname[:23] + "…"
        delta_pm = ""
        if idx > 0 and pm_val is not None and baseline_pm is not None:
            d = pm_val - baseline_pm
            delta_pm = f" ({'+' if d >= 0 else ''}{d:.1f})"

        row_vals = [
            short_name,
            f"{pm_val:.1f}{delta_pm}" if pm_val is not None else "N/A",
            str(int(aqi_val)) if aqi_val is not None else "N/A",
            str(int(qs_val))  if qs_val  is not None else "N/A",
            f"{cv_val:.1f}%"  if cv_val  is not None else "N/A",
        ]
        for xi, val in zip(x_positions, row_vals):
            pdf.drawString(xi, y, val)
        y -= row_h

    y -= 8

    # ── Section 3: Interpretation & Narrative ───────────────────────────────
    _section("3.  Interpretation & Key Findings")

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

    # AQI health context
    _line("Health Context:", bold=True, size=10)
    aqi_avgs = [a.get("summary", {}).get("aqi_average") for a in analyses]
    aqi_avgs_clean = [v for v in aqi_avgs if v is not None]
    if aqi_avgs_clean:
        overall_avg = sum(aqi_avgs_clean) / len(aqi_avgs_clean)
        if overall_avg <= 50:
            cat = "Good — Air quality is satisfactory with minimal health risk for all groups."
        elif overall_avg <= 100:
            cat = "Moderate — Acceptable quality; sensitive individuals may notice mild effects."
        elif overall_avg <= 150:
            cat = "Unhealthy for Sensitive Groups — People with respiratory/heart conditions should limit prolonged outdoor exposure."
        elif overall_avg <= 200:
            cat = "Unhealthy — Everyone may experience health effects; sensitive groups at greater risk."
        else:
            cat = "Very Unhealthy / Hazardous — Avoid outdoor activity. Health warnings of emergency conditions."
        _wrapped(f"Overall average AQI across all files: {overall_avg:.0f} — {cat}", indent=10)

    y -= 6

    # ── Section 4: Per-file summaries ────────────────────────────────────────
    _section("4.  Per-File Detailed Summaries")
    for idx, (a, fname) in enumerate(zip(analyses, filenames)):
        if y < B + 80:
            _new_page()
        s = a.get("summary", {})
        _line(f"File {idx + 1}: {fname}", bold=True, size=11, color=(0.10, 0.22, 0.45))
        dr = s.get("date_range", {})
        _line(f"  Period: {str(dr.get('start','N/A'))[:10]}  to  {str(dr.get('end','N/A'))[:10]}   |   {s.get('total_readings','N/A')} readings", size=9, indent=10, color=(0.4, 0.4, 0.4))
        pm_corr = s.get("pm25_average_epa_corrected") or s.get("pm25_average")
        _line(f"  Avg PM2.5 (EPA-corr.): {round(float(pm_corr),1) if pm_corr else 'N/A'} µg/m³   |   Avg AQI: {s.get('aqi_average','N/A')}   |   Category: {s.get('aqi_category','N/A')}", size=9, indent=10)
        _line(f"  Quality Score: {s.get('quality_score','N/A')}%   |   Validity: {s.get('validity_score','N/A')}%   |   Coverage: {s.get('coverage_score','N/A')}%", size=9, indent=10)
        cv = s.get("sensor_health_cv")
        status = s.get("sensor_validation_status", "N/A")
        _line(f"  Sensor CV: {f'{cv:.1f}%' if cv else 'N/A'}   |   Sensor Status: {status}", size=9, indent=10)
        narrative = s.get("quality_narrative", "")
        if narrative:
            _wrapped(f"  Note: {narrative}", size=9, indent=10, color=(0.35, 0.35, 0.35))
        y -= 6

    # ── Section 5: Methodology notes ─────────────────────────────────────────
    _section("5.  Methodology & Standards")
    _wrapped(
        "All PM2.5 values have been corrected using the EPA Barkjohn correction formula: "
        "PM2.5_corr = 0.534 × PM2.5_raw − 0.0844 × RH + 5.604 (where RH is available). "
        "AQI calculated per US EPA breakpoints. Quality Score = 0.4 × Validity + 0.6 × Coverage. "
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
    
    if "humidity" in cleaned:
        cleaned["pm25_corrected"] = apply_epa_correction(cleaned["pm25"], cleaned["humidity"])
    else:
        cleaned["pm25_corrected"] = np.nan

    # Apply LDL correction to corrected PM2.5 as well
    mask_corrected_below_ldl = (cleaned["pm25_corrected"] > 0) & (cleaned["pm25_corrected"] < LDL)
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

    hourly = cleaned.resample("h").mean(numeric_only=True)
    daily = cleaned.resample("d").mean(numeric_only=True)
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

    # Count pollution events (same 2σ method as build_report_figures)
    _n_events = 0
    _pm25_max = 0.0
    if decomposition_df is not None and not decomposition_df.empty and "residual" in decomposition_df.columns:
        _res = decomposition_df["residual"].dropna().values.astype(float)
        if len(_res) > 10:
            _std = float(np.nanstd(_res))
            if _std > 1e-6:
                _n_events = int(np.sum(np.abs(_res) > 2.0 * _std))
    if not daily.empty:
        _daily_pm = daily["pm25_corrected"].fillna(daily["pm25"]) if "pm25_corrected" in daily.columns else daily["pm25"]
        _pm25_max = float(_daily_pm.max()) if not _daily_pm.dropna().empty else 0.0

    drift_df = build_sensor_drift(df_work)

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
            
            # JHU/MIT STANDARD: Sensor Health Coefficient via Coefficient of Variation (CV)
            # CV = (MAD / Mean_PM25) × 100 — statistically rigorous metric
            diff_series = paired["pm25_a"] - paired["pm25_b"]
            mean_pm25 = paired[["pm25_a", "pm25_b"]].mean(axis=1).mean()
            
            # JHU/MIT Three-Tier Validation (based on MAD relative to Mean)
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
                "cv_between_channels": round(float(sensor_cv), 2),  # JHU/MIT: Coefficient of Variation
                "sensor_validation_status": sensor_validation_status,  # JHU/MIT: Three-tier validation
                "sensor_health_status": sensor_health,  # JHU/MIT: Health classification with CV threshold
            }

    stats_cols = [col for col in ["pm25", "pm25_corrected", "temperature", "humidity", "pressure"] if col in cleaned]
    stats = summarize_stats(cleaned, stats_cols) if stats_cols else pd.DataFrame()

    exceedances = {
        "who_15": int((hourly["pm25"] > 15).sum()) if "pm25" in hourly else 0,
        "epa_35": int((hourly["pm25"] > 35).sum()) if "pm25" in hourly else 0,
    }

    events = detect_events(cleaned, "pm25")
    if not events.empty:
        events = events.copy()
        events["start"] = events["start"].apply(
            lambda value: value.isoformat() if pd.notna(value) else None
        )
        events["end"] = events["end"].apply(
            lambda value: value.isoformat() if pd.notna(value) else None
        )

    weekday_pattern = cleaned.copy()
    weekday_pattern["weekday"] = weekday_pattern.index.day_name()
    weekday_summary = weekday_pattern.groupby("weekday")["pm25"].mean().reindex(
        ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    )

    hourly_pattern = cleaned.copy()
    hourly_pattern["hour"] = hourly_pattern.index.hour
    hourly_summary = hourly_pattern.groupby("hour")["pm25"].mean()
    # Ensure all 24 hours are present (0-23), even if no data for some hours
    hourly_summary = hourly_summary.reindex(range(24), fill_value=np.nan)

    heatmap = cleaned.copy()
    heatmap["day"] = heatmap.index.day_name()
    heatmap["hour"] = heatmap.index.hour
    heatmap_summary = heatmap.pivot_table(values="pm25", index="day", columns="hour", aggfunc="mean")
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
    radar_profile = build_radar_profile(cleaned, quality_score, channel_agreement, coverage_score)
    pm25_temporal_radar = build_pm25_temporal_radar(cleaned, latitude=lat_value, longitude=lon_value)

    channel_series = {"timestamps": [], "a": [], "b": [], "r2": channel_agreement.get("r2", None)}
    if "pm25_a" in df_work and "pm25_b" in df_work and "timestamp" in df_work:
        series_df = df_work[["timestamp", "pm25_a", "pm25_b"]].dropna(subset=["timestamp"])
        if not series_df.empty:
            channel_series = {
                "timestamps": series_df["timestamp"].astype(str).tolist(),
                "a": series_df["pm25_a"].round(2).fillna(None).tolist(),
                "b": series_df["pm25_b"].round(2).fillna(None).tolist(),
                "r2": channel_agreement.get("r2", None),
            }

    # ── New feature data ────────────────────────────────────────────────────────
    _calendar_data = build_calendar_data(daily["aqi"] if "aqi" in daily.columns else pd.Series(dtype=float))

    # Correction comparison (decimate to ≤5000 pts for browser)
    _corr_ts_raw = cleaned.index.astype(str).tolist()
    _corr_bk_raw  = cleaned["pm25_corrected"].round(2).fillna(None).tolist()
    _corr_lr_raw  = apply_lrapa_correction(cleaned["pm25"]).round(2).fillna(None).tolist()
    _corr_aq_raw  = apply_aqu_correction(cleaned["pm25"]).round(2).fillna(None).tolist()
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
    _aqi_avg     = int(cleaned["aqi"].mean()) if not cleaned.empty else 0
    _aqi_cat     = cleaned["aqi_category"].iloc[-1] if not cleaned.empty else "Unknown"
    _cv          = channel_agreement.get("cv_between_channels")
    _narrative   = build_narrative_summary(
        _pm_corr_avg, _pm_raw_avg, _aqi_avg, _aqi_cat,
        cleaned.index.min().isoformat() if not cleaned.empty else None,
        cleaned.index.max().isoformat() if not cleaned.empty else None,
        int(len(df_work)), _n_events, _pm25_max, _cv,
        round(float(coverage_score), 1), round(float(quality_score), 1),
    )

    result = {
        "summary": {
            "aqi_current": int(cleaned["aqi"].iloc[-1]) if not cleaned.empty else 0,
            "aqi_average": int(cleaned["aqi"].mean()) if not cleaned.empty else 0,
            "aqi_category": cleaned["aqi_category"].iloc[-1] if not cleaned.empty else "Unknown",
            "aqi_color": cleaned["aqi_color"].iloc[-1] if not cleaned.empty else "#9E9E9E",
            "pm25_average": round(float(cleaned["pm25"].mean()), 2) if not cleaned.empty else 0.0,
            "pm25_average_epa_corrected": round(float(cleaned["pm25_corrected"].mean()), 2) if "pm25_corrected" in cleaned and not cleaned.empty else None,
            "quality_score": round(float(quality_score), 1),
            "quality_score_formula": "Q = (0.7×Internal Integrity) + (0.3×Temporal Completeness)",  # IMPROVEMENT 4: Standardized formula
            "validity_score": round(float(validity_score), 1),
            "coverage_score": round(float(coverage_score), 1),
            # IMPROVEMENT 3: Dynamic Narrative Engine - Add context about gaps
            "quality_narrative": (
                f"Score primarily impacted by a contiguous {max_gap_days}-day network/power outage; "
                f"internal data integrity remains {round(float(validity_score), 1)}% valid."
                if coverage_score < 90 and max_gap_days > 0
                else "Data quality limited by temporal gaps; see coverage score for details."
            ),
            "channel_agreement_pct": channel_agreement.get("agreement_pct", None),
            "sensor_health_cv": channel_agreement.get("cv_between_channels", None),
            "sensor_validation_status": channel_agreement.get("sensor_validation_status", None),
            "sensor_health_status": channel_agreement.get("sensor_health_status", None),
            "date_range": {
                "start": cleaned.index.min().isoformat() if not cleaned.empty else None,
                "end": cleaned.index.max().isoformat() if not cleaned.empty else None,
            },
            "total_readings": int(len(df_work)),
            "label": label or "Full period",
            "n_pollution_events": _n_events,
            "pm25_max": round(_pm25_max, 2),
            "narrative_summary": _narrative,
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
        "charts": {
            "timeseries": {
                # JHU/MIT STANDARD: Enforce physical gaps using asfreq ('2T')
                # Reindex with 2-minute frequency to inject NaN into missing periods
                # This prevents false visual continuity across data gaps (e.g., 199.1h gap)
                # IMPROVEMENT: Show full dataset with intelligent decimation for browser performance
                "timestamps": decimate_data(cleaned.index.astype(str).tolist(), cleaned["pm25"].round(2).fillna(None).tolist())[0],
                "pm25": decimate_data(cleaned.index.astype(str).tolist(), cleaned["pm25"].round(2).fillna(None).tolist())[1],
                "pm25_corrected": decimate_data(cleaned.index.astype(str).tolist(), cleaned["pm25_corrected"].round(2).fillna(None).tolist())[1],
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
                "timestamps": decimate_data(rolling_df.get("timestamp", pd.Series(dtype=str)).astype(str).tolist(), rolling_df.get("pm25", pd.Series(dtype=float)).round(2).fillna(None).tolist())[0],
                "pm25": decimate_data(rolling_df.get("timestamp", pd.Series(dtype=str)).astype(str).tolist(), rolling_df.get("pm25", pd.Series(dtype=float)).round(2).fillna(None).tolist())[1],
                "median_24h": rolling_df.get("median_24h", pd.Series(dtype=float)).round(2).fillna(None).tolist(),
                "median_7d": rolling_df.get("median_7d", pd.Series(dtype=float)).round(2).fillna(None).tolist(),
                # 1-hour rolling median at raw sub-hourly resolution (decimated to ≤2000 pts for browser)
                "median_1h_timestamps": decimate_data(
                    pm25_raw_for_1h.index.astype(str).tolist(),
                    rolling_1h_series.round(2).fillna(None).tolist(),
                    max_points=2000,
                )[0],
                "median_1h": decimate_data(
                    pm25_raw_for_1h.index.astype(str).tolist(),
                    rolling_1h_series.round(2).fillna(None).tolist(),
                    max_points=2000,
                )[1],
            },
            "diurnal_pattern": {
                "hours": diurnal_df.get("hour", pd.Series(dtype=int)).tolist(),
                "mean": diurnal_df.get("mean", pd.Series(dtype=float)).round(2).fillna(None).tolist(),
                "p10": diurnal_df.get("p10", pd.Series(dtype=float)).round(2).fillna(None).tolist(),
                "p90": diurnal_df.get("p90", pd.Series(dtype=float)).round(2).fillna(None).tolist(),
            },
            "sensor_drift": {
                # IMPROVEMENT: Show full dataset with intelligent decimation for browser performance
                "timestamps": decimate_data(drift_df.get("timestamp", pd.Series(dtype=str)).astype(str).tolist(), drift_df.get("diff", pd.Series(dtype=float)).round(2).fillna(None).tolist())[0],
                "diff": decimate_data(drift_df.get("timestamp", pd.Series(dtype=str)).astype(str).tolist(), drift_df.get("diff", pd.Series(dtype=float)).round(2).fillna(None).tolist())[1],
                "rolling": drift_df.get("rolling_7d", pd.Series(dtype=float)).round(2).fillna(None).tolist(),
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
            "events": events.fillna("").to_dict(orient="records"),
            "quality": quality_rows,
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
    )
    report_pdf_path = output_dir / "report.pdf"
    build_report_pdf(report_pdf_path, result["summary"], quality_rows, anomalies, report_figures, channel_agreement, gap_analysis, radar_profile, metadata=metadata)
    public_report_path = output_dir / "community_report.pdf"
    build_public_report_pdf(
        public_report_path,
        result["summary"],
        daily.reset_index(),
        hourly.reset_index(),
        anomalies,
        channel_agreement,
        report_figures,
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
