"""Opt-in external data clients for the reference-monitor validation and
pollution-rose features.

Security / privacy posture (see README):
  • Fixed, hard-coded hostnames only — no user-supplied URLs — so these routes
    cannot be turned into an SSRF primitive.
  • Only the sensor's coordinates and date window are ever sent outward. PM2.5
    data never leaves the server.
  • Hard timeouts on every request; all network/parse failures collapse into a
    single ExternalError with a generic, user-safe message.
  • API keys are read from the environment only, never from the request.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

import httpx

# Fixed endpoints (never built from user input).
OPENAQ_BASE = "https://api.openaq.org/v3"
OPENMETEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"

# OpenAQ canonical parameter id for PM2.5.
PM25_PARAMETER_ID = 2

_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
_MAX_RADIUS_M = 25000  # OpenAQ hard cap
_HOURS_PAGE_SIZE = 1000
# Safety cap on pagination: 20 pages x 1000 = 20,000 hours (~2.3 years). No realistic
# PurpleAir analysis window needs more; this only bounds worst-case request count.
_HOURS_MAX_PAGES = 20


class ExternalError(Exception):
    """User-safe error for any external-data failure (no internals leaked)."""


def _validate_coords(lat: Any, lon: Any) -> Tuple[float, float]:
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        raise ExternalError("This dataset has no usable sensor coordinates.")
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        raise ExternalError("Sensor coordinates are out of range.")
    if lat_f == 0.0 and lon_f == 0.0:
        raise ExternalError("Sensor coordinates are missing (0, 0).")
    return round(lat_f, 5), round(lon_f, 5)


def _clamp_dates(start_date: str, end_date: str) -> Tuple[str, str]:
    """Accept only YYYY-MM-DD; anything else is rejected."""
    import datetime as _dt

    def _p(d: str) -> str:
        return _dt.date.fromisoformat(str(d)[:10]).isoformat()

    try:
        s, e = _p(start_date), _p(end_date)
    except Exception:
        raise ExternalError("Invalid date range.")
    return (s, e) if s <= e else (e, s)


# ── Open-Meteo: historical hourly wind (no API key) ──────────────────────────
def fetch_openmeteo_wind(lat: Any, lon: Any, start_date: str, end_date: str) -> Dict[str, List]:
    lat_f, lon_f = _validate_coords(lat, lon)
    s, e = _clamp_dates(start_date, end_date)
    params = {
        "latitude": lat_f,
        "longitude": lon_f,
        "start_date": s,
        "end_date": e,
        "hourly": "wind_speed_10m,wind_direction_10m",
        "timezone": "UTC",
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(OPENMETEO_ARCHIVE, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError:
        raise ExternalError("Wind-data service is currently unavailable. Please try again later.")
    except ValueError:
        raise ExternalError("Wind-data service returned an unreadable response.")

    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        raise ExternalError("No wind data is available for this location and period.")
    return {
        "time": times,  # list of 'YYYY-MM-DDTHH:MM' (UTC)
        "speed": hourly.get("wind_speed_10m") or [],
        "direction": hourly.get("wind_direction_10m") or [],
        "units": (data.get("hourly_units") or {}).get("wind_speed_10m", "km/h"),
    }


# ── OpenAQ v3: nearest regulatory PM2.5 monitor + hourly series (API key) ─────
def openaq_configured() -> bool:
    return bool(os.environ.get("OPENAQ_API_KEY", "").strip())


def fetch_openaq_reference(lat: Any, lon: Any, start_date: str, end_date: str,
                           radius_m: int = _MAX_RADIUS_M) -> Dict[str, Any]:
    key = os.environ.get("OPENAQ_API_KEY", "").strip()
    if not key:
        raise ExternalError(
            "Reference-monitor lookup is not configured on this server "
            "(no OpenAQ API key). See the README to enable it."
        )
    lat_f, lon_f = _validate_coords(lat, lon)
    s, e = _clamp_dates(start_date, end_date)
    radius = max(1000, min(int(radius_m or _MAX_RADIUS_M), _MAX_RADIUS_M))
    headers = {"X-API-Key": key}

    try:
        with httpx.Client(timeout=_TIMEOUT, headers=headers) as client:
            loc_resp = client.get(
                f"{OPENAQ_BASE}/locations",
                params={
                    "coordinates": f"{lat_f},{lon_f}",
                    "radius": radius,
                    "parameters_id": PM25_PARAMETER_ID,
                    "limit": 1,
                    "order_by": "distance",
                    "sort_order": "asc",
                },
            )
            loc_resp.raise_for_status()
            results = (loc_resp.json() or {}).get("results") or []
            if not results:
                raise ExternalError(
                    f"No regulatory PM2.5 monitor was found within {radius // 1000} km of this sensor."
                )
            location = results[0]

            sensor_id = None
            for sensor in location.get("sensors", []) or []:
                param = sensor.get("parameter") or {}
                if param.get("id") == PM25_PARAMETER_ID or param.get("name") == "pm25":
                    sensor_id = sensor.get("id")
                    break
            if not sensor_id:
                raise ExternalError("The nearest monitor does not expose a PM2.5 sensor.")

            # Paginate through every page of hourly data in the requested window.
            #
            # A single request caps at _HOURS_PAGE_SIZE (1000) hours = ~42 days. Any
            # analysis period longer than that -- which includes any realistic
            # multi-month PurpleAir deployment -- would otherwise come back silently
            # truncated to an arbitrary, non-representative subset of the period,
            # biasing every downstream statistic (bias, RMSE, R^2, slope) with no
            # indication to the user that anything was cut off.
            rows: list = []
            truncated = False
            for page in range(1, _HOURS_MAX_PAGES + 1):
                meas_resp = client.get(
                    f"{OPENAQ_BASE}/sensors/{sensor_id}/hours",
                    params={
                        "datetime_from": f"{s}T00:00:00Z",
                        "datetime_to": f"{e}T23:59:59Z",
                        "limit": _HOURS_PAGE_SIZE,
                        "page": page,
                    },
                )
                meas_resp.raise_for_status()
                page_rows = (meas_resp.json() or {}).get("results") or []
                rows.extend(page_rows)
                if len(page_rows) < _HOURS_PAGE_SIZE:
                    break
            else:
                truncated = True
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code in (401, 403):
            raise ExternalError("Reference-monitor lookup is not authorized (check the OpenAQ API key).")
        raise ExternalError("Reference-monitor service is currently unavailable. Please try again later.")
    except httpx.HTTPError:
        raise ExternalError("Reference-monitor service is currently unavailable. Please try again later.")
    except ValueError:
        raise ExternalError("Reference-monitor service returned an unreadable response.")

    series: Dict[str, float] = {}
    for row in rows:
        period = row.get("period") or {}
        dt_from = (period.get("datetimeFrom") or {}).get("utc")
        value = row.get("value")
        if dt_from and value is not None:
            try:
                series[str(dt_from)[:13]] = float(value)  # 'YYYY-MM-DDTHH' UTC key
            except (TypeError, ValueError):
                continue
    if not series:
        raise ExternalError("The reference monitor returned no data for this period.")

    coords = location.get("coordinates") or {}
    return {
        "location_name": location.get("name") or "Regulatory monitor",
        "distance_m": location.get("distance"),
        "sensor_id": sensor_id,
        "provider": (location.get("provider") or {}).get("name"),
        "latitude": coords.get("latitude"),
        "longitude": coords.get("longitude"),
        "series": series,  # UTC-hour key ('YYYY-MM-DDTHH') -> pm25
        "truncated": truncated,  # True only if the pagination safety cap was hit
    }
