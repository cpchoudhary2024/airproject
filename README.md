---
title: PurpleAir Air Quality Analyzer
emoji: 🌬️
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: EPA-corrected PurpleAir PM2.5 analysis & reports
tags:
  - air-quality
  - pm25
  - purpleair
  - environmental-health
  - epa
  - data-analysis
---

# 🌬️ PurpleAir Air Quality Analyzer

**Turn a raw PurpleAir CSV/XLSX export into a peer-review-grade air-quality analysis in seconds.**
Upload one file and get EPA-corrected PM2.5 trends, sensor-health validation, pollution-event
detection, and downloadable regulatory- and community-ready PDF reports — all computed in your
session. Nothing is persisted after your session ends.

👉 **[Open the live app](https://cpchoudhary2024-airquality-analyzer.hf.space/)**

---

## Why it goes beyond a government dashboard

Public dashboards (EPA AirNow, PurpleAir's map, state AQ portals) show you a *number*. This platform
shows you the **evidence chain behind the number** — the correction applied, the sensor's health, the
data completeness, and the uncertainty — so results stand up in research, regulatory, and community
settings.

| Capability | This platform | Typical gov / public dashboard |
|---|---|---|
| **EPA Barkjohn (2021) humidity correction** | ✅ applied + auditable formula | ⚠️ varies / hidden |
| **Multi-formula comparison** (Barkjohn · LRAPA · AQ&U) | ✅ side-by-side | ❌ |
| **Dual-channel A/B agreement & drift detection** | ✅ | ❌ |
| **Data-quality score & completeness audit** | ✅ JHU/MIT quality score | ❌ |
| **STL trend/seasonality decomposition** | ✅ | ❌ |
| **Pollution-event detection & ranking** | ✅ | Partial |
| **House-vs-control comparison** | ✅ up to 10 sites | ❌ |
| **Publication + plain-language community PDFs** | ✅ | ❌ |
| **Global timezone-aware daily grouping** | ✅ | Local only |
| **Your data leaves your session?** | ❌ never persisted | Varies |

## What you get

- **PM2.5 analysis** — daily-mean calendar (WHO 15 / EPA 35 thresholds), temporal trend, concentration
  distribution, diurnal cycle, and 1h/24h/7d rolling medians.
- **Sensor validation** — dual-sensor A/B agreement, drift detection, and a Sensor Health Index (CV).
- **Quality assurance** — data-completeness audit, gap analysis, and a transparent quality score.
- **Pollution events** — automatic detection, duration, peak concentration, and top-event ranking.
- **Reports** — a full **Research Report** (methodology + statistics) and a jargon-free **Community
  Report** for residents, plus CSV/JSON data exports and high-resolution chart PNGs.
- **Comparison mode** — benchmark multiple homes/sites against a control, with multi-year overlays.

## How to use

1. Export your sensor data from PurpleAir as a **CSV or XLSX** (0-/2-/10-minute averages all work).
2. Open the app, optionally enter a Device ID / Location / Timezone.
3. Drop the file in — the dashboard, charts, and downloadable reports generate automatically.

## Methods & standards

PM2.5 corrections follow **Barkjohn et al. (2021)**; health context uses the **WHO 2021 Air Quality
Guidelines** and the **US EPA PM NAAQS**; trend separation uses **STL decomposition (Cleveland et al.,
1990)**. Full citations appear in-app under *Methods & Standards*.

## Stack

FastAPI · pandas / NumPy / SciPy · statsmodels (STL) · Matplotlib + Plotly · ReportLab · Docker on
Hugging Face Spaces.

---

*Developed for environmental-health research and education. Outputs support — but do not replace —
regulatory monitoring or clinical decision-making. Results are subject to sensor limitations and
site-specific conditions.*
