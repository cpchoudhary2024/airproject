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

# PurpleAir Air Quality Analyzer

Converts a raw PurpleAir sensor export into a documented, reproducible PM2.5 analysis:
EPA-adopted humidity correction, dual-channel sensor validation, completeness auditing,
quantified uncertainty, and regulatory- and community-ready reports. All computation occurs
in-session; nothing is persisted after the session ends.

**Live:** https://cpchoudhary2024-airquality-analyzer.hf.space/

---

## 1. Executive Summary & Problem Statement

### The engineering challenge

Low-cost optical particle sensors have made dense, community-scale air monitoring possible
for the first time. They have also made it easy to publish numbers that do not mean what
they appear to mean.

A PurpleAir sensor does not measure mass. It counts light-scattering events and infers
PM2.5 from an assumed particle size distribution and refractive index. The resulting `cf_1`
reading systematically **over-reports** ambient PM2.5, and the bias is humidity-dependent:
hygroscopic growth makes particles scatter more light without adding proportional mass.
Reporting a raw sensor value against a regulatory threshold overstates the exceedance.

Three further problems determine whether a community dataset is defensible:

**Sensor health is unobservable from a single channel.** PurpleAir units carry two
co-located laser counters. Divergence between them is the primary indicator of fouling,
partial blockage, or laser degradation. A single-channel analysis cannot detect a failing
sensor and will report its output as fact.

**Completeness is usually unstated.** A month with 40% data capture and a month with 98%
produce equally confident-looking monthly means. Without a coverage audit, a gap caused by
a power outage during a pollution episode is indistinguishable from clean air.

**A number without an interval invites over-reading.** Community monitoring frequently
drives advocacy or health decisions. A corrected concentration carries uncertainty from
both channel disagreement and the correction equation itself, and reporting a bare point
estimate conceals it.

### Technical objective

Produce the **evidence chain behind the number** — the correction applied and its
coefficients, the sensor's health, the data completeness, and the propagated uncertainty —
so results survive scrutiny in research, regulatory, and community settings.

### Compliance and risk-mitigation impact

Outputs support community air monitoring programmes, exposure characterisation, and
house-versus-control comparisons where a quantified difference (with a p-value and a
confidence interval) is required rather than two visually compared lines. The reproducibility
fingerprint allows any published result to be re-derived from the same input.

---

## 2. Regulatory & Industry Standards Alignment

### Barkjohn, Gantt & Clements (2021) — the EPA-adopted correction

*Atmospheric Measurement Techniques* 14, 4617–4637, **Equation 10**.
doi:10.5194/amt-14-4617-2021

This is the US-wide PurpleAir correction adopted by EPA for the AirNow Fire and Smoke Map:

```
PM2.5_corrected = 0.524 × PA_cf1 − 0.0862 × RH + 5.75        [µg/m³]
```

No coefficient is refitted. The published constants are the single source of truth in the
codebase: every displayed formula, worked example, and reproducibility record renders from
the same constants the data path uses, so the number the application computes and the
number it claims to compute cannot drift apart.

### EPA AQI Technical Assistance Document

PM2.5 breakpoints (2024 revision), with the mandated **truncation** of concentration to one
decimal place before the piecewise-linear index is applied:

| PM2.5 (µg/m³) | AQI | Category |
|---|---|---|
| 0.0 – 9.0 | 0 – 50 | Good |
| 9.1 – 35.4 | 51 – 100 | Moderate |
| 35.5 – 55.4 | 101 – 150 | Unhealthy for Sensitive Groups |
| 55.5 – 125.4 | 151 – 200 | Unhealthy |
| 125.5 – 225.4 | 201 – 300 | Very Unhealthy |
| 225.5 – 325.4 | 301 – 500 | Hazardous |

Above 325.4 µg/m³ the index is capped at 500 ("Beyond the AQI").

### Health and regulatory reference values

| Standard | Value | Averaging period |
|---|---|---|
| **WHO 2021 Air Quality Guideline** | 5 µg/m³ | Annual |
| **WHO 2021 Air Quality Guideline** | 15 µg/m³ | 24-hour |
| **US EPA PM2.5 NAAQS (2024 primary)** | 9 µg/m³ | Annual |
| **US EPA PM2.5 NAAQS** | 35 µg/m³ | 24-hour |

### Alternate corrections, reported side by side

| Correction | Formula | Origin |
|---|---|---|
| **Barkjohn (EPA)** | `0.524 × PM − 0.0862 × RH + 5.75` | US-wide, EPA-adopted |
| **LRAPA** | `0.5 × PM − 0.66` | Lane Regional Air Protection Agency |
| **AQ&U** | `0.778 × PM + 2.65` | University of Utah |

All three clip at zero: a negative mass concentration is unphysical.

### Analytical method references

- **STL decomposition** — Cleveland et al. (1990), for trend and seasonality separation
- **Mann-Kendall** — non-parametric monotonic trend test
- **Theil-Sen** — resistant slope estimator
- **Pettitt** — non-parametric change-point detection
- **WHO/GBD log-linear exposure-response** — RR ≈ 1.08 per 10 µg/m³, used only for a
  clearly-labelled illustrative excess-risk estimate

---

## 3. Technical Methodology & Mathematical Framework

### Pipeline

```
PurpleAir export (CSV / XLSX; 0-, 2-, or 10-minute averages)
        ↓
Column detection (name score + range score + timestamp score)
        ↓
Channel selection (A/B held in the same calibration family)
        ↓
EPA Barkjohn correction  →  AQI  →  daily / diurnal / seasonal aggregation
        ↓
QA/QC: A/B agreement · drift · completeness · gap classification
        ↓
Uncertainty band · Mann-Kendall / Theil-Sen / Pettitt · difference-in-differences
        ↓
Research PDF · Community PDF · CSV/JSON export · SHA-256 reproducibility ID
```

### Column detection

Headers vary across PurpleAir export versions and firmware. Columns are identified by a
composite score rather than a fixed schema:

- **Name score** — 1.0 exact match, 0.7 substring, 0.5 token match. Only tokens of ≥ 3
  characters count toward the token tier; one- and two-character tokens (the `a`/`b`
  channel suffixes) would otherwise match almost any header, e.g. `a` inside `private`.
- **Range score** — fraction of values inside a physically plausible range for the field.
- **Timestamp score** — fraction parsing as valid datetimes.

Channel A and B are held in the **same calibration family** as the primary column (`cf_1`
versus `atm`). Mixing families would compare a `cf_1` primary against `atm` channels, so
the A/B agreement statistic would describe two different calibrations rather than two
physical sensors.

### AQI computation

Concentration is truncated toward zero to one decimal place before the breakpoint lookup,
per the EPA AQI TAD:

```
AQI = ((AQI_hi − AQI_lo) / (C_hi − C_lo)) × (C_trunc − C_lo) + AQI_lo
```

Truncation is not cosmetic. The published breakpoints are edge-to-edge on a 0.1 grid
(… 9.0 | 9.1 …), so an untruncated value such as 9.05 falls between two bins and matches
none. That case previously returned AQI 0 / "Unknown", which — being zero — pulled
downstream AQI averages *downwards* and made air quality appear better than it was. The
truncation is also guarded against binary-float error, so 35.5 does not become 35.4 and
misreport the category.

### Pollution event detection

An excursion is flagged when it is **both** statistically anomalous and health-relevant:

```
spike  ⟺  (value > rolling_median + 3σ)  AND  (value ≥ 15 µg/m³)
```

The rolling baseline uses a **time-based 2-hour window**, not a fixed sample count. A
`rolling(12)` window means 24 minutes on a 2-minute export but 12 hours on an hourly one,
so the same physical episode was detected differently depending on which averaging interval
the user happened to download.

The 15 µg/m³ absolute floor (the WHO 24-hour guideline) is required because during clean
periods σ is tiny, and a bare 3σ rule fires on ordinary sensor jitter — flagging
sub-µg/m³ "spikes" that are noise, not pollution.

### Uncertainty propagation

Combined in quadrature from channel disagreement and the correction's own residual error:

```
u_total = √( (CV_channel × PM)² + RMSE_Barkjohn² )        RMSE = 3.0 µg/m³
```

The Barkjohn RMSE basis is **24-hour averages** (Barkjohn et al. 2021). Reported as a 95%
confidence band on the corrected trend.

### Trend and change detection

- **Mann-Kendall** — Kendall τ with p-value; non-parametric, no distributional assumption
- **Theil-Sen** — slope in µg/m³ per year with 95% CI; resistant to outliers
- **Pettitt** — change-point test identifying *whether* a shift is real and *when* it began
- **STL** — trend/seasonal/residual separation

### Difference-in-differences

House-versus-control comparison reports each site's excess relative to the control as a
quantified **percentage ± 95% CI with a p-value**, rather than two visually compared lines.

### Coverage and sampling

```
coverage = (actual records / expected records) × 100
expected = period_duration / median_sampling_interval
```

The sampling interval is the **median** positive timestamp gap, so data gaps and duplicated
timestamps do not distort the inferred interval.

### Model limitations and physical assumptions

- **The Barkjohn correction was developed for ambient and smoke-influenced conditions.**
  It degrades outside its fitted domain, particularly at very high concentrations and at
  extreme humidity.
- **Optical sensors infer mass from scattering.** They assume a particle size distribution
  and refractive index; unusual aerosol composition violates that assumption.
- **Correction requires humidity.** Missing RH yields NaN, never a silently uncorrected
  value — mixing corrected and uncorrected values in one series without disclosure would be
  worse than a gap.
- **Negative corrected values are clipped at zero.** On cool, clean, humid readings the
  humidity term can drive the raw result below zero.
- **A/B agreement detects divergence, not accuracy.** Two channels can drift together.
- **The excess-risk estimate is illustrative**, based on a log-linear WHO/GBD
  exposure-response relationship at population scale. It is not a clinical or individual
  risk figure.
- **This is not regulatory monitoring.** Outputs support, but do not replace, reference-grade
  Federal Reference Method or Federal Equivalent Method instrumentation.

### Verification

`tests/test_epa_domain.py` provides **18 passing tests** validating the domain math against
published references rather than against current output:

- Barkjohn coefficients pinned to Eq. 10 (guards against silent refitting)
- Two independent hand-computed reference points: 11.92 µg/m³ and 55.133 µg/m³
- Vectorised path agrees with the scalar worked-example path shown to users
- Negative clipping and NaN propagation on missing humidity
- LRAPA and AQ&U published forms
- Full EPA breakpoint table and every category edge (0, 9.0, 9.1, 35.4, 35.5, 55.4, 55.5, 125.5, 225.5)
- **Truncation not rounding** — 9.05 → AQI 50, not 51
- Binary-float robustness at the 35.5 edge
- Monotonicity across a 3,400-point concentration grid
- 500 cap beyond the defined index; Unknown on invalid and negative input
- Linear interpolation against the hand-computed EPA formula
- WHO 5 / EPA 9 / EPA 35 guideline anchors land in the expected categories

Full suite: **59 passing**.

### Reproducibility fingerprint

A SHA-256 hash of the exact input data plus method versions is stamped on the interface and
into every generated PDF, so any published result can be traced to the input that produced
it.

---

## 4. Data Schema & Engineering Units

### Inputs

| Field | Definition | Units |
|---|---|---|
| `PA_cf1` | Raw PurpleAir PM2.5, cf_1 calibration | µg/m³ |
| `PA_atm` | Raw PurpleAir PM2.5, atmospheric calibration | µg/m³ |
| `pm2.5_a` / `pm2.5_b` | Channel A / B PM2.5 | µg/m³ |
| `humidity` | Relative humidity | % (0–100) |
| `temperature` | Ambient temperature | °C or °F (detected) |
| `pressure` | Barometric pressure | hPa |
| `timestamp` | Observation time | ISO 8601 / epoch |

Accepted export intervals: 0-, 2-, and 10-minute averages, CSV or XLSX.

### Outputs

| Variable | Definition | Units |
|---|---|---|
| `pm25_corrected` | EPA Barkjohn-corrected PM2.5 | µg/m³ |
| `pm25_lrapa` / `pm25_aqu` | Alternate corrections | µg/m³ |
| `aqi` | EPA Air Quality Index | dimensionless, 0–500 |
| `category` | AQI category label | text |
| `u_total` | Combined 1σ measurement uncertainty | µg/m³ |
| `ci95_low` / `ci95_high` | 95% confidence band on the trend | µg/m³ |
| `channel_agreement` | A/B correlation and mean absolute difference | r, µg/m³ |
| `sensor_health_index` | Coefficient of variation between channels | % |
| `coverage_score` | Actual ÷ expected records | % |
| `sampling_minutes` | Median timestamp gap | minutes |
| `theil_sen_slope` | Trend slope with 95% CI | µg/m³ per year |
| `kendall_tau` / `p_value` | Mann-Kendall statistic and significance | dimensionless |
| `pettitt_changepoint` | Detected change-point date | date |
| `did_excess_pct` | House-vs-control excess ± 95% CI | % |
| `cumulative_exposure` | Integrated exposure burden | µg·hours |
| `days_over_who15` / `days_over_epa35` | Days above guideline | count |
| `repro_hash` | Reproducibility fingerprint | SHA-256 hex |

### Public data sources referenced

| Source | Use |
|---|---|
| **PurpleAir** | Sensor data export (user-supplied) |
| **US EPA AQI Technical Assistance Document** | Breakpoint table and truncation rule |
| **US EPA NAAQS** | Regulatory reference concentrations |
| **WHO 2021 Air Quality Guidelines** | Health-based reference values |
| **Barkjohn et al. (2021), AMT 14** | Correction coefficients and RMSE |

---

## 5. Verification & Reproduction Instructions

### Requirements

Python 3.11 or later.

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
pip install -r requirements-dev.txt
```

### Run the test suite

```bash
pytest -q
```

Expected: **59 passing**.

Domain math only:

```bash
pytest tests/test_epa_domain.py -v
```

Accuracy audit:

```bash
pytest tests/test_accuracy_audit.py -v
```

### Run the application

```bash
uvicorn app.main:app --reload --port 7860
```

Open `http://localhost:7860`, optionally enter a Device ID, Location, and Timezone, then
upload a PurpleAir CSV or XLSX export. The dashboard, charts, and downloadable reports
generate automatically.

### Container

```bash
docker build -t airquality-analyzer .
docker run -p 7860:7860 airquality-analyzer
```

### Generate reports from the command line

```bash
python compute_and_plot.py            # analysis + figures
python build_reports.py               # research PDF
python build_reports_docx.py          # DOCX deliverables
python build_defense_notes.py         # methods and defense notes
```

### Data handling

Nothing is persisted after a session ends. Raw sensor exports and any resident-identifying
report are excluded from version control.

---

## License and attribution

**Copyright © 2026 C. P. Choudhary. All rights reserved.** This source is published for
transparency and scientific verification, not for reuse: copying, redistribution,
derivative works, and commercial use require prior written permission. See
[LICENSE](LICENSE).

The underlying science is public and freely usable — the EPA-adopted correction of
[Barkjohn et al. (2021)](https://doi.org/10.5194/amt-14-4617-2021), the WHO Air Quality
Guidelines, and the US EPA NAAQS are cited here, not owned.

## Disclaimer

Developed for environmental-health research and education. Outputs support — but do not
replace — regulatory monitoring or clinical decision-making. Results are subject to sensor
limitations and site-specific conditions.
