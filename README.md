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

An environmental data processing pipeline and reporting interface for PurpleAir low-cost optical particulate monitors. Converts raw sensor exports into calibrated PM2.5 estimates with dual-channel quality control, time-series decomposition, spatial difference-in-differences comparisons, and cryptographic reproducibility records.

Live deployment: https://cpchoudhary2024-airquality-analyzer.hf.space/

## 1. Overview and Problem Statement

Low-cost optical particle counters (specifically the Plantower PMS5003 units deployed inside PurpleAir monitors) measure light scattering rather than gravimetric particle mass. Inherent physical limitations introduce systematic biases:

1. **Hygroscopic Growth:** Fine particles absorb ambient moisture under high relative humidity (RH > 50%), swelling and scattering disproportionately more light per unit mass. Raw sensor readings (`cf_1` and `atm`) systematically over-report PM2.5 under humid conditions.
2. **Channel Divergence:** PurpleAir hardware contains two internal laser counters (Channel A and Channel B). Contamination, insect intrusion, component aging, or electrical degradation often affect one sensor before the other. Single-channel evaluations cannot detect hardware faults.
3. **Temporal Incompleteness:** Offline periods, power outages, and intermittent Wi-Fi transmission create temporal sampling gaps. Without explicit completeness auditing, missing readings can be misinterpreted as low-pollution intervals.
4. **Uncertainty Propagation:** Uncalibrated point measurements fail to reflect measurement uncertainty stemming from inter-channel divergence and empirical correction residuals.

This platform resolves these issues through a reproducible, zero-external-dependency pipeline that applies US EPA-adopted correction equations, verifies dual-channel concordance, evaluates time-series trends with autocorrelation corrections, and compiles multi-format audit deliverables.

## 2. Mathematical and Methodological Framework

### 2.1 Calibration Corrections

The default calibration implements the nationwide empirical correction derived by Barkjohn et al. (2021) and adopted by the US EPA for the AirNow Fire and Smoke Map:

$$\text{PM}_{2.5,\text{corrected}} = 0.524 \cdot \text{PA}_{\text{cf1}} - 0.0862 \cdot \text{RH} + 5.75 \quad [\mu\text{g/m}^3]$$

Where:
* $\text{PA}_{\text{cf1}}$ is the raw PM2.5 concentration using the `cf_1` calibration family ($\mu\text{g/m}^3$).
* $\text{RH}$ is relative humidity (%).
* Physical bounds are enforced: negative corrected values are clipped at zero ($0.0\,\mu\text{g/m}^3$).
* When RH is missing or invalid, the raw concentration is preserved and explicitly flagged to avoid mixed-series distortion.

Alternative regional models available for comparative inspection:
* **LRAPA (Lane Regional Air Protection Agency):** $\text{PM}_{2.5} = \max(0, 0.5 \cdot \text{PM} - 0.66)$
* **AQ&U (University of Utah):** $\text{PM}_{2.5} = \max(0, 0.778 \cdot \text{PM} + 2.65)$

### 2.2 EPA AQI Breakpoints and TAD Truncation

The PM2.5 sub-index is calculated according to the EPA AQI Technical Assistance Document (TAD, EPA-454/B-24-002) and the revised 2024 National Ambient Air Quality Standards (NAAQS):

$$I = \frac{I_{\text{high}} - I_{\text{low}}}{C_{\text{high}} - C_{\text{low}}} (C_{\text{trunc}} - C_{\text{low}}) + I_{\text{low}}$$

| PM2.5 Concentration ($\mu\text{g/m}^3$) | AQI Range | Category |
| :--- | :--- | :--- |
| 0.0 to 9.0 | 0 to 50 | Good |
| 9.1 to 35.4 | 51 to 100 | Moderate |
| 35.5 to 55.4 | 101 to 150 | Unhealthy for Sensitive Groups (USG) |
| 55.5 to 125.4 | 151 to 200 | Unhealthy |
| 125.5 to 225.4 | 201 to 300 | Very Unhealthy |
| 225.5 to 325.4 | 301 to 500 | Hazardous |

* **Truncation Rule:** Concentrations are truncated to one decimal place before category indexing. For example, $9.05\,\mu\text{g/m}^3$ truncates to $9.0\,\mu\text{g/m}^3$ (AQI 50, Good), preventing category boundary jumping caused by binary floating-point representation.
* Concentrations above $325.4\,\mu\text{g/m}^3$ are capped at 500 ("Beyond the AQI").
* Daily calendar AQI is calculated strictly from daily 24-hour mean concentrations. Daily averaging of hourly AQI values is avoided due to the non-linear relationship between concentration and index values.

### 2.3 Environmental Health Guidelines

* **WHO 2021 24-Hour Guideline:** $15.0\,\mu\text{g/m}^3$
* **WHO 2021 Annual Guideline:** $5.0\,\mu\text{g/m}^3$
* **US EPA 24-Hour Primary NAAQS:** $35.0\,\mu\text{g/m}^3$
* **US EPA Annual Primary NAAQS (2024 Revision):** $9.0\,\mu\text{g/m}^3$

### 2.4 Sensor QA/QC and Drift Diagnostics

* **Channel Concordance:** Inter-channel coefficient of variation (CV):
  $$\text{CV} = \frac{\text{Mean Absolute Difference}}{\text{Mean Concentration}} \times 100\%$$
  CV < 10% indicates optimal dual-channel agreement; 10% to 15% indicates acceptable performance; CV >= 15% flags potential contamination or optical degradation.
* **Bland-Altman Agreement:** Computes mean bias and 95% limits of agreement ($\bar{d} \pm 1.96 \cdot s_d$) to detect proportional or concentration-dependent sensor divergence.
* **Sensor Drift Tracking:** 7-day rolling median of $(A - B)$ differences to separate systematic long-term calibration decay from short-term transient noise.
* **Data Completeness:** Ratio of observed records to expected records based on the median sampling interval. Composite Quality Score:
  $$\text{Quality Score} = 0.4 \times \text{Validity} + 0.6 \times \text{Coverage}$$

### 2.5 Event Detection and Statistical Testing

* **Episode Identification:** Excursions are flagged only when meeting two simultaneous criteria:
  $$\text{Excursion} \iff (C_t > \text{Median}_{2\text{h}} + 3\sigma_{2\text{h}}) \land (C_t \ge 15\,\mu\text{g/m}^3)$$
  This avoids false positives during clean-air baselines while isolating true physical episodes.
* **Trend Estimation:** Mann-Kendall test with Hamed & Rao (1998) variance correction for serial autocorrelation, paired with robust Theil-Sen slope estimation and Pettitt change-point detection. Annualized slopes are reported only when the record length spans at least 365 days.
* **Difference-in-Differences (DiD):** Evaluates multi-sensor excess against a designated baseline control using daily averages and Newey-West Heteroskedasticity and Autocorrelation Consistent (HAC) standard errors.

### 2.6 Cryptographic Provenance

Every execution computes a deterministic SHA-256 fingerprint combining the raw byte stream of the uploaded dataset and frozen algorithm method versions:

$$\text{Repro-ID} = \text{SHA256}(\text{SHA256}(\text{Input Bytes}) \mathbin{\Vert} \text{Frozen Methods})[0:16]$$

This hash is stamped on the dashboard and in all generated PDF and Word deliverables.

## 3. Repository Structure

```
.
├── Dockerfile                  # Container build configuration (Python 3.11-slim)
├── LICENSE                     # Project license and copyright
├── README.md                   # Technical documentation
├── requirements.txt            # Production runtime dependencies
├── requirements-dev.txt        # Development and testing dependencies
├── pytest.ini                  # Pytest configuration
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI backend, routing, security headers
│   ├── analysis.py             # Core analytical pipeline, math, PDF compilers
│   ├── visual_explanations.py  # Documentation strings for visualizations
│   ├── static/
│   │   ├── app.js              # Client-side UI logic, Plotly rendering, calendar
│   │   └── styles.css          # Interface stylesheets and responsive grid
│   └── templates/
│       └── index.html          # Dashboard HTML template
└── tests/
    ├── test_accuracy_audit.py  # Independent numerical recomputation tests
    ├── test_app.py             # API endpoints, security, and export verification
    └── test_epa_domain.py      # Regulatory breakpoint and Barkjohn math tests
```

## 4. Setup and Execution

### 4.1 Prerequisites

* Python 3.11 or later
* System libraries for Matplotlib Agg backend and ReportLab (included in standard Linux/macOS environments)

### 4.2 Local Installation

```bash
# Clone the repository
git clone https://github.com/cpchoudhary2024/PurpleAir-Data-Analysis.git
cd "PurpleAir-Data-Analysis"

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

### 4.3 Running the Application

```bash
uvicorn app.main:app --reload --port 7860
```

Access the interface at `http://localhost:7860`.

### 4.4 Docker Container Execution

```bash
docker build -t purpleair-analyzer .
docker run -p 7860:7860 purpleair-analyzer
```

## 5. Verification and Test Suite

The test suite validates calculations independently from the application code (first-principles recomputations):

```bash
# Run complete test suite (61 tests)
pytest -v

# Run EPA domain math and breakpoint boundary tests
pytest tests/test_epa_domain.py -v

# Run numerical recomputation and accuracy tests
pytest tests/test_accuracy_audit.py -v

# Run API endpoint and export tests
pytest tests/test_app.py -v
```

Expected result:
```
======================== 61 passed, 1 warning in ~25s ========================
```

## 6. Generated Deliverables

The application produces four structured exports:
1. **Research Technical Report (PDF):** Multi-page report containing descriptive statistics, STL seasonal decomposition, Bland-Altman agreement, sensor drift analysis, diurnal distributions, and user-supplied notes.
2. **Community Report (PDF):** Accessible summary designed for public participants and community science initiatives, mapping local exposure to health guidelines.
3. **Multi-House Comparison Report (PDF):** Comparative report summarizing rolling medians, diurnal profiles, and difference-in-differences excess relative to a designated control site.
4. **Editable Word Report (DOCX):** Formatted document for integration into regulatory filings, academic theses, or institutional documentation.
5. **Clean Data Products (CSV/JSON):** Validated time series with both raw and Barkjohn-corrected concentrations, QA/QC flags, and statistical summaries.

## 7. Primary References

* Barkjohn, K. K., Gantt, B., and Clements, A. L. (2021). Development and application of a United States-wide correction for PM2.5 data collected with the PurpleAir sensor. *Atmospheric Measurement Techniques*, 14(6), 4617-4637. doi:10.5194/amt-14-4617-2021
* Cleveland, R. B., Cleveland, W. S., McRae, J. E., and Terpenning, I. (1990). STL: A Seasonal-Trend Decomposition Procedure Based on Loess. *Journal of Official Statistics*, 6(1), 3-73.
* Hamed, K. H., and Rao, A. R. (1998). A modified Mann-Kendall trend test for autocorrelated data. *Journal of Hydrology*, 204(1-4), 182-196.
* U.S. Environmental Protection Agency (2024). Technical Assistance Document for the Reporting of Daily Air Quality: the Air Quality Index (AQI). EPA-454/B-24-002.
* World Health Organization (2021). WHO global air quality guidelines: particulate matter (PM2.5 and PM10), ozone, nitrogen dioxide, sulfur dioxide and carbon monoxide. Geneva: World Health Organization.

## 8. License

Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
Source published for academic review, scientific verification, and community research transparency. See [LICENSE](LICENSE).
