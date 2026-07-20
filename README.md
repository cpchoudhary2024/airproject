---
title: PurpleAir Air Quality Analyzer
emoji: 🌬️
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
license: other
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

**Turn a raw PurpleAir CSV/XLSX export into a documented, reproducible air-quality analysis in seconds.**
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
| **Data-quality score & completeness audit** | ✅ transparent, published scoring rule | ❌ |
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

## Scientific rigor (Tier 1–3)

Beyond the corrected numbers, every analysis now quantifies **how much to trust them**:

- **Measurement-uncertainty bands** — a 95% confidence band on the corrected trend, combining
  dual-channel disagreement with the Barkjohn correction RMSE (in quadrature). Honest uncertainty,
  not a single bare number.
- **Statistical trend test** — Mann-Kendall (Kendall τ + p), a Theil-Sen slope in µg/m³ per year with
  95% CI, and a Pettitt change-point test that says *whether* a change is real and *when* it started.
- **Difference-in-differences** — the Compare-Houses panel reports each house's excess vs the Control
  as a quantified **% ± 95% CI with a p-value**, not two eyeballed lines.
- **Exposure & health burden** — cumulative µg·hours, days over the WHO 15 / EPA 35 guidelines, and an
  optional (clearly-labelled, illustrative) WHO/GBD excess-risk estimate.
- **Reproducibility ID** — a SHA-256 fingerprint of the exact input data + method versions, stamped on
  the UI and every PDF so any result is auditable and reproducible.

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

## Licence & attribution

**Copyright (c) 2026 C. P. Choudhary. All rights reserved.** This source is published for
transparency and scientific verification, not for reuse: copying, redistribution, derivative
works, and commercial use require prior written permission. See [LICENSE](LICENSE).

The underlying science is public and freely usable — the EPA-adopted correction of
[Barkjohn et al. (2021)](https://doi.org/10.5194/amt-14-4617-2021), the WHO Air Quality
Guidelines, and the US EPA NAAQS are cited here, not owned.

---

*Developed for environmental-health research and education. Outputs support — but do not replace —
regulatory monitoring or clinical decision-making. Results are subject to sensor limitations and
site-specific conditions.*
