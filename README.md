---
title: PurpleAir Air Quality Analyzer
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
license: other
short_description: EPA-corrected PurpleAir PM2.5 QA/QC analysis and reporting
tags:
  - air-quality
  - pm25
  - purpleair
  - atmospheric-science
  - environmental-data
---

# PurpleAir Air Quality Analyzer

A web app that turns raw PurpleAir sensor exports into corrected PM2.5 estimates, checks the two internal sensor channels against each other, and produces report PDF, Word and CSV reports.

Live app: https://cpchoudhary2024-airquality-analyzer.hf.space/

## Why raw PurpleAir data needs processing

PurpleAir monitors use Plantower PMS5003 optical counters. These measure scattered light, not particle mass, which causes four problems:

1. **Humidity bias.** Above about 50% relative humidity, fine particles take up water and swell. They scatter more light per unit mass, so the raw `cf_1` and `atm` values over-report PM2.5 on humid days.
2. **Channel faults.** Each monitor has two laser counters, A and B. Dust, insects, aging or electrical problems usually hit one channel first. Looking at a single channel hides the fault.
3. **Gaps.** Power cuts and Wi-Fi dropouts leave holes in the record. If nobody checks completeness, a gap can be mistaken for clean air.
4. **Uncertainty.** A single uncorrected number says nothing about how far the two channels disagree or how large the correction error is.

The app applies the EPA-adopted correction, checks that the two channels agree, tests trends with corrections for autocorrelation, and records a fingerprint of each run so results can be reproduced.

## Methods

### Calibration

By default the app uses the US-wide correction from Barkjohn et al. (2021), which the EPA uses on the AirNow Fire and Smoke Map:

$$\text{PM}_{2.5,\text{corrected}} = 0.524 \cdot \text{PA}_{\text{cf1}} - 0.0862 \cdot \text{RH} + 5.75 \quad [\mu\text{g/m}^3]$$

Here $\text{PA}_{\text{cf1}}$ is the raw `cf_1` PM2.5 reading ($\mu\text{g/m}^3$) and $\text{RH}$ is relative humidity (%). Negative results are clipped to zero. When RH is missing or invalid, the raw value is kept and flagged so corrected and uncorrected values are never silently mixed.

Two regional models are available for comparison:

* LRAPA (Lane Regional Air Protection Agency): $\text{PM}_{2.5} = \max(0, 0.5 \cdot \text{PM} - 0.66)$
* AQ&U (University of Utah): $\text{PM}_{2.5} = \max(0, 0.778 \cdot \text{PM} + 2.65)$

### AQI

The PM2.5 sub-index follows the EPA AQI Technical Assistance Document (EPA-454/B-24-002) and the 2024 NAAQS revision:

$$I = \frac{I_{\text{high}} - I_{\text{low}}}{C_{\text{high}} - C_{\text{low}}} (C_{\text{trunc}} - C_{\text{low}}) + I_{\text{low}}$$

| PM2.5 ($\mu\text{g/m}^3$) | AQI | Category |
| :--- | :--- | :--- |
| 0.0 to 9.0 | 0 to 50 | Good |
| 9.1 to 35.4 | 51 to 100 | Moderate |
| 35.5 to 55.4 | 101 to 150 | Unhealthy for Sensitive Groups |
| 55.5 to 125.4 | 151 to 200 | Unhealthy |
| 125.5 to 225.4 | 201 to 300 | Very Unhealthy |
| 225.5 to 325.4 | 301 to 500 | Hazardous |

Concentrations are truncated to one decimal before lookup, so 9.05 $\mu\text{g/m}^3$ becomes 9.0 (AQI 50, Good). This stops floating-point noise from pushing a value across a category boundary. Values above 325.4 $\mu\text{g/m}^3$ are capped at 500 ("Beyond the AQI").

Daily AQI comes from the 24-hour mean concentration, not from averaging hourly AQI values, because the index is not linear in concentration.

### Health guidelines used

| Guideline | PM2.5 ($\mu\text{g/m}^3$) |
| :--- | :--- |
| WHO 2021, 24-hour | 15.0 |
| WHO 2021, annual | 5.0 |
| US EPA primary NAAQS, 24-hour | 35.0 |
| US EPA primary NAAQS, annual (2024) | 9.0 |

### Sensor QA/QC

Channel agreement is measured with an inter-channel coefficient of variation:

$$\text{CV} = \frac{\text{Mean Absolute Difference}}{\text{Mean Concentration}} \times 100\%$$

A CV under 10% is good agreement, 10% to 15% is acceptable, and 15% or more flags possible contamination or optical wear.

A Bland-Altman analysis reports the mean bias and 95% limits of agreement ($\bar{d} \pm 1.96 \cdot s_d$), which shows whether the channels drift apart as concentration rises. A 7-day rolling median of $(A - B)$ separates slow calibration drift from short-term noise.

Completeness is the ratio of observed to expected records, based on the median sampling interval. The quality score combines it with validity:

$$\text{Quality Score} = 0.4 \times \text{Validity} + 0.6 \times \text{Coverage}$$

### Events and trends

A reading counts as an excursion only when both conditions hold:

$$\text{Excursion} \iff (C_t > \text{Median}_{2\text{h}} + 3\sigma_{2\text{h}}) \land (C_t \ge 15\,\mu\text{g/m}^3)$$

The 15 $\mu\text{g/m}^3$ floor keeps small wobbles in clean air from being reported as events.

Trends use the Mann-Kendall test with the Hamed and Rao (1998) variance correction for autocorrelation, a Theil-Sen slope and Pettitt change-point detection. Annual slopes are only reported when the record covers at least 365 days.

For several sensors, a difference-in-differences comparison measures each site's excess over a chosen control site, using daily means and Newey-West (HAC) standard errors.

### Reproducibility ID

Each run gets a SHA-256 fingerprint built from the uploaded file's bytes and the versions of the methods used:

$$\text{Repro-ID} = \text{SHA256}(\text{SHA256}(\text{Input Bytes}) \mathbin{\Vert} \text{Frozen Methods})[0:16]$$

The ID appears on the dashboard and in every PDF and Word report. The same file analyzed with the same methods always gives the same ID.

## Outputs

* **Technical report (PDF):** descriptive statistics, STL seasonal decomposition, Bland-Altman agreement, drift, diurnal patterns and your own notes.
* **Community report (PDF):** a plain-language summary that compares local exposure with health guidelines.
* **Multi-house comparison (PDF):** rolling medians, diurnal profiles and difference-in-differences excess against a control site.
* **Word report (DOCX):** an editable version for theses, filings or internal documents.
* **Data files (CSV/JSON):** the cleaned time series with raw and corrected PM2.5, QA/QC flags and summary statistics.

## Running it yourself

### Project layout

```
.
├── Dockerfile
├── LICENSE
├── README.md
├── requirements.txt
└── app/
    ├── __init__.py
    ├── main.py                 # FastAPI routes and security headers
    ├── analysis.py             # Analysis pipeline and report builders
    ├── visual_explanations.py  # Chart explanation text
    ├── static/
    │   ├── app.js              # Dashboard logic and Plotly charts
    │   └── styles.css
    └── templates/
        └── index.html
```

### Local install

Requires Python 3.11 or later.

```bash
git clone https://github.com/cpchoudhary2024/airproject.git
cd airproject

python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt

uvicorn app.main:app --port 7860
```

Then open `http://localhost:7860` and upload a PurpleAir CSV export.

### Docker

```bash
docker build -t purpleair-analyzer .
docker run -p 7860:7860 purpleair-analyzer
```

## References

* Barkjohn, K. K., Gantt, B., and Clements, A. L. (2021). Development and application of a United States-wide correction for PM2.5 data collected with the PurpleAir sensor. *Atmospheric Measurement Techniques*, 14(6), 4617-4637. doi:10.5194/amt-14-4617-2021
* Cleveland, R. B., Cleveland, W. S., McRae, J. E., and Terpenning, I. (1990). STL: A Seasonal-Trend Decomposition Procedure Based on Loess. *Journal of Official Statistics*, 6(1), 3-73.
* Hamed, K. H., and Rao, A. R. (1998). A modified Mann-Kendall trend test for autocorrelated data. *Journal of Hydrology*, 204(1-4), 182-196.
* U.S. Environmental Protection Agency (2024). Technical Assistance Document for the Reporting of Daily Air Quality: the Air Quality Index (AQI). EPA-454/B-24-002.
* World Health Organization (2021). WHO global air quality guidelines: particulate matter (PM2.5 and PM10), ozone, nitrogen dioxide, sulfur dioxide and carbon monoxide. Geneva: World Health Organization.

## License

Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved. The source is published so the methods can be reviewed and checked. See [LICENSE](LICENSE).
