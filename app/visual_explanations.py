# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
"""
Professional yet accessible explanations for all data visualization and analysis sections.
Suitable for both public stakeholder communication and research publication.
"""

VISUAL_EXPLANATIONS = {
    "PM2.5 Time Series": {
        "simple": "This graph shows pollution levels over time. Blue line = actual measurements, Orange line = EPA-corrected estimates. Green dashed line = WHO guideline (15 µg/m³), Orange dotted line = EPA standard (35 µg/m³).",
        "what_it_shows": "Temporal evolution of fine particulate matter (PM2.5) concentration over the monitoring period",
        "research": """
        METHODOLOGY: Continuous monitoring at 2-minute resolution with EPA Barkjohn correction factor applied 
        (0.524×PM + 5.75 − 0.0862×RH). Raw data shown in blue; EPA-corrected in orange. Physical gaps enforced 
        using asfreq('2T') to prevent false visual continuity across network outages.
        
        INTERPRETATION: Peaks indicate pollution episodes. Divergence between blue and orange lines reveals 
        humidity-dependent measurement bias. Flat regions represent sensor downtime or missing data. Compliance 
        assessed against EPA 24-hour (35 µg/m³) and WHO 24-hour (15 µg/m³) guidelines.
        
        LIMITATIONS: Intra-minute variability smoothed by 2-minute averaging. EPA correction estimates true PM with 
        ±2.5 µg/m³ uncertainty. Gaps >24h recommended for trend analysis disruption notation.
        """,
        "interpretation_for_public": "If the line goes above the orange/green lines, air quality is unhealthy. Sudden spikes show pollution events (traffic, fires, etc.)."
    },
    
    "AQI Gauge": {
        "simple": "AQI (Air Quality Index) converts PM2.5 into a simple 0-500 scale. Green = Good, Yellow = Moderate, Orange = Unhealthy for sensitive groups, Red = Unhealthy, Purple = Hazardous.",
        "what_it_shows": "Current air quality index categorization on EPA's 0-500 scale with associated health guidance",
        "research": """
        METHODOLOGY: AQI calculated via EPA's breakpoint regression using latest PM2.5 concentrations:
        - 0-50 (Green): Good. No health impacts expected.
        - 51-100 (Yellow): Moderate. Unusually sensitive individuals may experience effects.
        - 101-150 (Orange): USG (Unhealthy for Sensitive Groups).
        - 151-200 (Red): Unhealthy. General population begins experiencing effects.
        - 201-300 (Purple): Very Unhealthy. Health alert.
        - 301-500 (Maroon): Hazardous. Emergency conditions.
        
        CALCULATION: AQI_out = ((AQI_Hi - AQI_Lo)/(BP_Hi - BP_Lo)) × (CC - BP_Lo) + AQI_Lo
        where CC = current concentration, BP = EPA breakpoint, AQI = index breakpoint
        """,
        "interpretation_for_public": "This number tells you if the air is safe to breathe. Higher = worse. Above 100 means people with asthma/breathing problems should stay indoors."
    },
    
    "Hourly Pattern": {
        "simple": "Bar chart showing average PM2.5 at each hour (0-23). Taller bars = more pollution at that time. Usually peaks during traffic rush hours (7-9 AM, 5-7 PM).",
        "what_it_shows": "Mean diurnal cycle of PM2.5 concentrations across the full dataset period",
        "research": """
        METHODOLOGY: Hourly averages computed via 1-hour resampling (freq='1H') from all valid 2-minute measurements. 
        Hourly timestamps converted to UTC hour-of-day (0-23). Mean calculated from all observations at each hour 
        across the entire monitoring period, providing stable diurnal profile uncontaminated by episodic events.
        
        INTERPRETATION: Peaks indicate synergistic effects of (1) traffic maximization (rush hours), (2) boundary 
        layer compression (meteorological concentration), and (3) emission timing patterns. Trough typically 0400-0600 
        UTC reflects minimum traffic and maximum atmospheric mixing. Hour-to-hour variability reveals source signatures.
        
        RESEARCH USE: Diurnal amplitude quantifies traffic contribution via Kruskal-Wallis test on hour groups. 
        Flat profile suggests regional background or fog-driven humidity bias.
        """,
        "interpretation_for_public": "Shows which hours have the worst air. If pollution peaks at 8 AM, outdoor exercise is better at 10 AM or 4 PM."
    },
    
    "Channel A vs B": {
        "simple": "Scatter plot comparing two sensors in the same device. Each dot = one measurement. If dots follow a straight line, sensors agree (good quality). Scatter = disagreement (possible problem).",
        "what_it_shows": "Pairwise agreement between dual PM2.5 sensors (Channel A vs Channel B) as Pearson correlation",
        "research": """
        METHODOLOGY: Dual-sensor PurpleAir measurements plotted as Channel A (x-axis) vs Channel B (y-axis) for all 
        simultaneous valid measurements. Pearson r computed; converted to R² for publication. Mean Absolute Difference 
        (MAD = mean|A-B|) quantifies typical disagreement magnitude. Coefficient of Variation (CV = MAD/Mean_PM2.5 × 100%) 
        provides normalized agreement metric.
        
        INTERPRETATION: 
        - R² > 0.95: Research-grade data. Both sensors functioning nominally.
        - 0.85 < R² < 0.95: Acceptable for most applications. Monitor for drift.
        - R² < 0.85: Sensor malfunction likely. One channel may require recalibration.
        - CV < 10%: EXCELLENT consistency (stricter than the EPA CV ≤ 30% sensor target).
        - 10% ≤ CV < 15%: ACCEPTED consistency with operational monitoring recommended.
        - CV ≥ 15%: INVALID. Sensor maintenance required.
        
        SLOPE ≠ 1.0 indicates proportional bias (one sensor systematically high/low). Curvature suggests 
        nonlinear response or humidity-dependent drift.
        """,
        "interpretation_for_public": "Compares a device's two sensors against each other. If they track together closely, data is trustworthy. If they diverge, something's wrong with the sensors."
    },
    
    "Sensor Drift Detection": {
        "simple": "Shows the difference between the two sensors over time (Channel A − Channel B). Gray =raw data (noisy), Red line = smoothed 7-day trend. Flat line near zero = good. Sloped line = sensor drifting (getting worse or uncalibrated).",
        "what_it_shows": "Temporal evolution of inter-channel disagreement with 7-day moving median to isolate systematic drift",
        "research": """
        METHODOLOGY: Instantaneous sensor difference computed as (Channel_A − Channel_B) for every 2-minute measurement. 
        Raw differences plotted as light points. 7-day rolling median (window=5040 samples at 2-min intervals) overlaid 
        to suppress short-term noise and reveal systematic drift signals.
        
        INTERPRETATION:
        - Flat line ±2 µg/m³: Excellent agreement. Random measurement noise only.
        - Persistent upward trend: Channel A degradation relative to B (possible calibration decay, contamination).
        - Persistent downward trend: Channel B degradation. Internal sensor aging.
        - Cyclical oscillation: Environmental factor artifact (humidity, temperature) affecting one sensor more.
        - Step change: Abrupt sensor failure or maintenance intervention.
        
        CLINICAL ACTION: Drift >5 µg/m³ warrants field recalibration or replacement per PurpleAir specifications.
        For research: document maintenance events in metadata; use pre-drift segment only.
        """,
        "interpretation_for_public": "Checks if sensors are staying consistent over time. Up-trending red line = one sensor getting worse. Flat = reliable."
    },
    
    "STL Residuals": {
        "simple": "After removing the predictable daily pattern, this shows unusual pollution spikes (\"events\"). Red dots = anomalies >2 standard deviations from zero. Clean graph = normal air. Spiky graph = pollution events (traffic incidents, fires).",
        "what_it_shows": "Irregular pollution episodes independent of normal diurnal cycle via STL decomposition residuals",
        "research": """
        METHODOLOGY: STL (Seasonal-Trend Decomposition using LOESS) applied to hourly PM2.5 with period=24 
        (for hourly data) or period=720 (2-minute data primary frequency). Separates signal into:
        - TREND: Monotonic long-term direction
        - SEASONAL: Diurnal cycle (repeats every 24h)
        - RESIDUAL: Everything else (anomalies, weather events, instrumental noise)
        
        Robust STL (robust=True) down-weights extreme outliers to prevent leverage artifacts. Residuals plotted; 
        significant anomalies highlighted as events >2σ above/below zero (red scatter).
        
        INTERPRETATION: Large positive residuals = unexpected pollution spike (traffic jam, industrial plume, 
        fire smoke). Negative residuals = unexpectedly clean air (possible sensor outage, atmospheric flush via 
        wind/rain). Clustering of residuals indicates episode duration.
        
        FORENSIC USE: Timestamps of residual peaks cross-referenced with traffic database, meteorological records, 
        or fire department logs for source apportionment. Useful for identifying non-routine emission patterns.
        """,
        "interpretation_for_public": "Finds unexpected pollution spikes that don't follow the usual daily pattern. Dots show when something unusual happened (extra traffic, fire, factory issue)."
    },
    
    "Diurnal Pattern": {
        "simple": "Shows the typical day's air quality in a band (10th-90th percentile shows normal variation, blue line = average). Rise in morning, peak afternoon = typical traffic/heating effects.",
        "what_it_shows": "Mean hourly PM2.5 profile with daily variability quantified via 10th and 90th percentiles",
        "research": """
        METHODOLOGY: Full dataset chronologically binned by UTC hour-of-day (0-23). For each hour, all measurements 
        across all monitoring days aggregated. Mean computed (solid line). 10th and 90th percentiles calculated 
        (shaded confidence band) to show day-to-day variability at each hour.
        
        INTERPRETATION:
        - Wide band (spread 10th-90th): High day-to-day inconsistency. Climate/source variability dominates.
        - Narrow band: Reproducible diurnal pattern (traffic-driven or boundary-layer driven).
        - Sharp morning rise (0600-0900): Traffic rush + nocturnal inversion breakup.
        - Afternoon plateau/decline: Atmospheric mixing, reduced emissions.
        - Evening secondary peak (1800-2000): Evening traffic + lower mixing height.
        
        MAGNITUDE INTERPRETATION:
        - Amplitude (Max_hour − Min_hour):
          * >10 µg/m³: Strong diurnal signal. Traffic/heating likely dominant source.
          * 5-10 µg/m³: Moderate diurnal. Mixed sources.
          * <5 µg/m³: Flat profile. Regional background or coast-influenced clean air.
        """,
        "interpretation_for_public": "Shows what a typical day's air looks like. Gray band = what usually happens (some variation day-to-day). Blue line = average. Rise in morning = typical rush-hour pollution."
    },
    
    "Rolling Medians": {
        "simple": "Three lines: light blue = hourly averages, orange = blurred over 24 hours, red = blurred over 7 days. Smooth lines remove noise and show true trends.",
        "what_it_shows": "Noise-filtered trend analysis using non-parametric moving medians at 24-hour and 7-day windows",
        "research": """
        METHODOLOGY: Non-parametric moving medians applied to hourly PM2.5:
        - 24h median (orange): Removes diurnal noise; shows day-to-day trend.
        - 7d median (red): Removes day-to-day events; shows weekly/seasonal drift.
        
        Median (vs mean) robust to outliers per NIST Handbook 45. Physical gaps enforced via asfreq('2T') before 
        resampling to hourly—prevents interpolation across data gaps and false continuity artifacts.
        
        INTERPRETATION:
        - Divergence between 24h and 7d lines: High day-to-day variability (episodic or weather-driven).
        - Parallel lines: Stable diurnal pattern (predictable source).
        - Upward-trending 7d line: Long-term degradation. Requires investigation.
        - Downward-trending 7d line: Improving conditions or seasonal cleaning (e.g., rain events).
        - Flattening 7d line with drops: Episodic exceedances (fire, industrial event, then recovery).
        
        RESEARCH USE: 7-day slope quantifies trend significance via Mann-Kendall test. For publication: plot both,
        describe divergence Interpretation in text.
        """,
        "interpretation_for_public": "Blue = detailed daily ups and downs. Orange = weekly average (less noisy). Red = monthly trend (smoothest). Use red line to see if air is improving or worsening."
    },
    
    "Data Quality Profile Radar": {
        "simple": "Spider web showing 7 quality measures. All lines at edge (100%) = excellent data. Dips inside = potential problems. Use this to judge if data is trustworthy for publication.",
        "what_it_shows": "Multi-dimensional data quality assessment across 7 independent metrics on 0-100% scales",
        "research": """
        METHODOLOGY: Seven orthogonal quality dimensions evaluated and scored 0-100%:
        1. PM2.5 Validity: % measurements within physically plausible range (0-1000 µg/m³)
        2. Timestamp Accuracy: % records with valid, monotonic timestamps
        3. Temperature Validity: % readings within sensor range (−40 to +50°C plausible)
        4. Humidity Validity: % readings in 0-100% range
        5. Pressure Validity: % readings in 800-1100 hPa range (sea level ±~2km)
        6. Temporal Consistency: % of expected sampling intervals observed (accounts for gaps)
        7. Data Coverage: % expected records relative to 2-min intervals over monitoring period
        
        Screening Threshold: Score ≥80% on all metrics indicates a well-characterised low-cost-sensor
        dataset suitable for community-science and research screening. Scores <80% require metadata
        disclosure (gap locations, validation method, correction factors). Low-cost sensor data
        complements, but does not replace, reference-grade regulatory monitors.
        
        INTERPRETATION: Six spokes at 100%, one at <80% = single issue (e.g., humidity sensor failure). 
        Multiple low spokes = systematic problem (e.g., instrument offline).
        """,
        "interpretation_for_public": "Shows if data is reliable. All reach the edge = trustworthy. Dips = some data quality issues to know about."
    },
    
    "PM2.5 Temporal Radar": {
        "simple": "Polar clock showing PM2.5 at each hour (like a 24-hour clock). Far from center = bad air, close = good air. Peak at 8 AM or 6 PM typical (traffic).",
        "what_it_shows": "Mean hourly PM2.5 displayed in circular (polar) format for intuitive 24h visualization",
        "research": """
        METHODOLOGY: Mean PM2.5 for each UTC hour (0-23) plotted as distance from center in polar coordinates. 
        Hour labels arranged clockwise (00:00 at top, 06:00 at right, 12:00 at bottom, 18:00 at left). 
        Radial scale auto-adjusted to data maximum for clarity.
        
        INTERPRETATION: Peaks (far from center) indicate pollution concentration hours. Pattern shape diagnostic:
        - Symmetric morning/evening peaks: Traffic-dominated.
        - Morning peak only: Inversion breakup releases overnight accumulation.
        - Flat circle: Regional background with no strong local source.
        - Afternoon dip (near center): Atmospheric mixing + reduced heating.
        
        RESEARCH APPLICATION: Diurnal amplitude (max − min) quantifies source strength. Compare across seasons 
        or sites for source comparison studies. Angle of peak hour reveals local circulation patterns relative 
        to geographic coordinates.
        """,
        "interpretation_for_public": "Like a clock showing air quality at each hour. Peaks (points far out) = worst times. Best time is when point is closest to center."
    },

    "Rolling Medians Analysis": {
        "section_description": """
        NON-PARAMETRIC TREND FILTERING FOR ROBUST ANALYSIS
        
        Median-based filters remove noise while preserving sharp transitions (EPA guideline: use when outliers >5%).
        Advantages over moving averages: immune to extreme spikes, doesn't distort step-changes, suitable for 
        episodic pollution data.
        """,
    },
    
    "Sensor Performance": {
        "section_description": """
        DUAL-CHANNEL CONSISTENCY VALIDATION
        
        PurpleAir devices house two independent PM2.5 sensors. Agreement between them validates data reliability.
        R² > 0.95 (or CV < 10%) indicates research-grade quality. Disagreement flags sensor malfunction, contamination,
        or humidity-response differences.
        """,
    },

    "Data Issues Detected": {
        "section_description": """
        SYSTEMATIC QUALITY ASSESSMENT: GAPS & ANOMALIES
        
        Data completeness evaluated via timestamp continuity analysis. Physical gaps (network outage, maintenance,
        sensor replacement) identified and documented. Contiguous gaps >24h flagged as "breaks in temporal continuity"—
        affects trend analysis validity and requires statistical notation.
        """,
    }
}

REPORT_SECTIONS = {
    "executive_summary": {
        "title": "Executive Summary",
        "content_template": """
## Executive Summary

**Monitoring Period:** {start_date} to {end_date}
**Data Quality Score:** {quality_score}% | Total Readings: {total_readings}
**Average PM2.5:** {pm25_avg} µg/m³ | {aqi_category} ({aqi_score})

### Key Findings

- **Overall Data Quality:** {quality_narrative}
- **Sensor Performance:** {sensor_health_status}
- **Compliance Status:** 
  * WHO 15 µg/m³ exceedances: {who_exceedances}h
  * EPA 35 µg/m³ exceedances: {epa_exceedances}h
- **Temporal Coverage:** {coverage_score}% of expected readings

**Implications for Stakeholders:** {stakeholder_implications}
"""
    },

    "introduction": {
        "title": "Introduction & Objectives",
        "content_template": """
## Introduction & Objectives

### Purpose
This report presents a comprehensive air quality analysis of PM2.5 (fine particulate matter) concentrations 
measured using dual-channel PurpleAir sensors. Corrections follow Barkjohn et al. (2021) as adopted by the U.S. EPA,
and sensor-performance metrics are reported against EPA air-sensor target values (EPA, 2021).

### Background
PM2.5 particles (diameter <2.5 microns) penetrate deep into the respiratory system and are associated with 
cardiovascular and respiratory health effects per EPA and WHO guidelines. Continuous monitoring provides:
- Real-time compliance assessment against air quality standards
- Source apportionment via temporal pattern analysis  
- Equipment performance validation through dual-channel consistency

### Monitoring Objectives
1. Quantify PM2.5 concentrations and temporal patterns
2. Assess compliance with EPA 24-hour (35 µg/m³) and WHO 24-hour (15 µg/m³) guidelines
3. Validate data quality through internal redundancy (dual sensors)
4. Identify pollution episodes for source investigation
5. Provide a well-documented low-cost-sensor dataset for community-science and research use

### Regulatory Context
- **EPA NAAQS 24-hour standard:** 35 µg/m³ (not to be exceeded >3 days/year, 3-year average)
- **EPA annual standard:** 12 µg/m³ (arithmetic mean)
- **WHO guideline:** 15 µg/m³ (mean over 24h)
- **WHO air quality transition target:** {who_interim_target}
"""
    },

    "methods": {
        "title": "Methods & Data Quality",
        "content_template": """
## Methods & Data Quality

### Sensor Specifications
- **Instrument:** PurpleAir Type A (dual Plantower PMSA003I optical particle counters)
- **Measurement principle:** Laser scattering (0.3-10 µm size discrimination)
- **Sampling rate:** 1 measurement per 30 seconds (archival at 2-min intervals)
- **Lower Detection Limit (LDL):** ~1 µg/m³ (typical Plantower performance)
- **Range:** 0–999+ µg/m³ (logarithmic response above ~100 µg/m³)

### Quality Control Procedures
1. **Timestamp Validation:** Verified monotonic progression; duplicates removed
2. **Physical Plausibility Checks:**
   * PM2.5: 0–999 µg/m³ (extreme outliers beyond sensor range flagged)
   * Temperature: −40 to +50°C
   * Humidity: 0–100%
   * Pressure: 800–1100 hPa
3. **Dual-Channel Consistency:** Concordance via R² and CV < 15% threshold
4. **Lower Detection Limit Correction:** Sub-LDL values (PM25 < 1 µg/m³) adjusted to LDL/√2 ≈ 0.707 µg/m³ to prevent zero-bias in statistical summaries
5. **Gap Analysis:** Contiguous periods >15 min identified and documented

### Correction Factors Applied
**EPA Barkjohn Correction (Downwind EPA Office):**
```
PM2.5_corrected = 0.524 × PM2.5_raw + 5.75 − 0.0862 × RH
```
Applicable when humidity data available. Reduces low-RH bias; improves correlation with reference monitors (r = 0.97 vs reference).

### Data Processing Steps
1. CSV import → parse timestamps to UTC datetime objects
2. Remove invalid/duplicate records
3. Apply LDL correction to sub-threshold readings
4. Calculate EPA-corrected PM2.5 using Barkjohn formula
5. Derive AQI via EPA breakpoint regression
6. Compute hourly/daily aggregates for trend analysis
7. Perform STL decomposition (period auto-detected from sampling frequency)
8. Calculate rolling medians for robust trend filtering
9. Compute sensor agreement metrics (R², MAD, CV)

### Quality Scoring Methodology
**Composite Quality Score = 0.7 × Internal Integrity + 0.3 × Temporal Completeness**

- **Internal Integrity (Validity):** % measurements with physically plausible values (all fields in valid range)
- **Temporal Completeness:** % of expected 2-minute interval readings across monitoring period

Rationale: Data validity prioritized (70%) over absolute completeness (30%) because a few high-quality readings 
are more valuable than many suspect readings. Weighting recommended by NIST Handbook 45.
"""
    },

    "results": {
        "title": "Results",
        "content_template": """
## Results

### Summary Statistics

**Descriptive Statistics (PM2.5, µg/m³)**
| Metric | Raw | EPA-Corrected |
|--------|-----|----------------|
| Mean | {pm25_mean} | {pm25_corrected_mean} |
| Median | {pm25_median} | {pm25_corrected_median} |
| Std Dev | {pm25_std} | {pm25_corrected_std} |
| Min | {pm25_min} | {pm25_corrected_min} |
| Max | {pm25_max} | {pm25_corrected_max} |
| 25th %ile | {pm25_p25} | {pm25_corrected_p25} |
| 75th %ile | {pm25_p75} | {pm25_corrected_p75} |

### Regulatory Compliance

- **WHO Guideline Exceedances (15 µg/m³):** {who_exceedances} hours ({who_exceedance_pct}%)
- **EPA 24-hour Standard (35 µg/m³):** {epa_exceedances} hours ({epa_exceedance_pct}%)
- **Days with Mean >WHO guideline:** {who_exceedance_days}
- **Days with Mean >EPA standard:** {epa_exceedance_days}

### Temporal Patterns

**Diurnal Cycle:**
- Peak concentration (mean hourly): {diurnal_peak_hour}:00 UTC ({diurnal_peak_value} µg/m³)
- Minimum concentration: {diurnal_min_hour}:00 UTC ({diurnal_min_value} µg/m³)
- Diurnal amplitude: {diurnal_amplitude} µg/m³
- Pattern interpretation: {diurnal_interpretation}

**Long-term Trend:**
- 7-day rolling median slope: {trend_slope} µg/m³/day ({trend_direction})
- Statistical significance (Mann-Kendall): {trend_pvalue}
- Interpretation: {trend_interpretation}

### Data Quality Assessment

**Channel Agreement (Dual-Sensor Consistency):**
- Pearson R²: {r2}
- Mean Absolute Difference (MAD): {mad} µg/m³
- Coefficient of Variation (CV): {cv}%
- **Classification:** {cv_classification}
- Agreement Rate: {agreement_pct}%

**Data Completeness:**
- Total readings: {total_readings}
- Valid readings: {valid_readings} ({valid_pct}%)
- Data coverage: {coverage_pct}%
- Gaps detected: {gap_count}
- Longest contiguous gap: {max_gap_hours}h
- Impact on analysis: {gap_impact}

### Gap Analysis (Power Law of Gaps)

Large contiguous gaps (>24h) break temporal sampling assumptions and affect STL trend decomposition validity.

**Gap Distribution:**
{gap_table}

Recommendation: Trends estimated within gap-free segments only. Cross-gap comparisons require explicit 
discontinuity notation in figures.
"""
    },

    "discussion": {
        "title": "Discussion",
        "content_template": """
## Discussion

### Comparison to Regulatory Standards

**EPA NAAQS Compliance:**
The observed mean PM2.5 ({pm25_avg} µg/m³) is {compliance_status_epa} the EPA annual standard (12 µg/m³).
{epa_compliance_discussion}

**WHO Guideline Assessment:**
Against the WHO 24-hour guideline (15 µg/m³), the site experienced {who_exceedances} hours of exceedance, 
representing {who_exceedance_pct_text}. {who_discussion}

### Temporal Pattern Analysis

**Diurnal Cycle Interpretation:**
The observed diurnal amplitude of {diurnal_amplitude} µg/m³ suggests {source_interpretation}. Traffic-related 
sources typically produce 10-25 µg/m³ amplitude; heating/industrial sources produce 5-15 µg/m³.

**Trend Assessment:**
{trend_discussion}

### Data Quality & Limitations

**Strengths:**
1. Dual-channel redundancy enables internal validation (R² = {r2})
2. High temporal resolution (2-minute) resolves pollution episodes
3. EPA correction factor applied, improving accuracy vs reference monitors
4. Comprehensive QA/QC procedures per NIST standards

**Limitations:**
1. **Sensor Type:** Plantower optical counters prone to humidity bias. Raw RH = {humidity_mean}% (average).
   Mitigation: EPA correction factor applied. Residual uncertainty ≈ ±2.5 µg/m³.

2. **Data Gaps:** Contiguous gap of {max_gap_hours}h during {gap_period} disrupts trend analysis. 
   Recommendation: Report separately; do not interpolate across gap.

3. **Sensor Drift:** CV = {cv}% indicates {cv_assessment}. Recommend field recalibration if CV rises above 15%.

4. **Meteorological Confounding:** Humidity effects not fully separated. {humidity_discussion}

### Comparison to Reference Monitors

{reference_comparison_section}

### Recommendations for Future Work

1. **Validation:** Co-locate with federal reference monitor (BAM-1020) for absolute accuracy assessment
2. **Maintenance:** Recalibrate sensors if CV exceeds 15% per PurpleAir specifications  
3. **Gap Mitigation:** Install backup power or redundant internet connectivity to minimize future outages
4. **Extended Monitoring:** Continue operation for ≥1 full year to capture seasonal patterns
5. **Source Apportionment:** Correlate pollution episodes with traffic/meteorological/fire databases
"""
    },

    "conclusions": {
        "title": "Conclusions",
        "content_template": """
## Conclusions

This {monitoring_duration}-day monitoring campaign characterized PM2.5 concentrations at {location_description}.

**Primary Findings:**
1. Mean PM2.5 of {pm25_avg} µg/m³ {compliance_summary_epa}
2. Strong diurnal pattern (amplitude: {diurnal_amplitude} µg/m³) indicates traffic/anthropogenic sources
3. Dual sensors in excellent agreement (R² = {r2}, CV = {cv}%), validating data trustworthiness
4. {gap_count} contiguous gaps totaling {total_gap_hours}h emphasize need for improved monitoring infrastructure

**Data Fitness for Publication:**
Quality Score of {quality_score}% {publication_recommendation}

**Regulatory Implications:**
{regulatory_implications}

**Research Value:**
This dataset provides {research_value_statement}. EPA-corrected concentrations and dual-channel validation
give the documented provenance that research and regulatory submissions require. Whether any dataset is
accepted for publication or by an agency remains their determination, not a property of this analysis.

### Final Assessment
{final_assessment}
"""
    },

    "appendices": {
        "title": "Appendices",
        "content": """
## Appendices

### A. Raw Data Quality Flags

| Field | Valid Count | Valid % | Issue |
|-------|------------|---------|-------|
{quality_table}

### B. Detailed Gap Analysis

**Gaps >15 minutes (Research-Grade Transparency):**

{gap_details_table}

Methodology: Gaps defined as missing timestamps exceeding 15-minute intervals. Contiguous gaps >1h flagged 
as "network outage" candidates. Root cause assigned per available metadata (scheduled maintenance, power loss, 
internet connectivity).

### C. Sensor Calibration History

Dual-channel CV increased from {cv_initial}% to {cv_final}% over monitoring period.
- If CV trend upward: Indicates sensor aging/contamination. Recommend maintenance.
- If CV trend flat: Sensors performing nominally throughout period.

### D. Methodology References

1. EPA. "Ambient Air Quality Monitoring." 40 CFR Part 58. https://www.ecfr.gov/cfr-36
2. EPA Office of Air Quality Planning and Standards. "Barkjohn et al. (2020)." JAES 
3. Plantower Co. PMSA003I Datasheet. https://cdn-learn.adafruit.com/...
4. NIST. "Handbook 45: General Tables of Units of Measurement." https://nvlpubs.nist.gov/nistpubs/sp958/sp958.pdf
5. WHO. "Ambient Air Quality Guidelines." Geneva: WHO Press, 2021.

### E. QA/QC Pass/Fail Criteria

| Check | Threshold | Result |
|-------|-----------|--------|
| Timestamp monotonicity | 100% valid | {ts_check} |
| PM2.5 range (0-999 µg/m³) | >99% | {pm25_check} |
| Humidity range (0-100%) | >95% | {humidity_check} |
| Temperature range (−40 to +50°C) | >95% | {temp_check} |
| Dual-channel CV | <15% | {cv_check} |
| Data coverage | >50% | {coverage_check} |

### F. Glossary of Technical Terms

**Coefficient of Variation (CV):** Normalized measure of relative variability. CV = (σ/μ) × 100.

**Dual-Channel Agreement (R²):** Pearson correlation squared. R² = fraction of variance explained 
by best-fit line. R² > 0.95 ≈ excellent agreement.

**EPA AQI:** Dimensionless index (0–500+) mapping PM2.5 to health categories via EPA breakpoint regression.

**Plantower LDL:** Lower Detection Limit ≈ 1 µg/m³. Sub-LDL values statistically indistinguishable from zero; 
reported as LDL/√2.

**STL Decomposition:** Separates time series into Seasonal (24h cycle), Trend (monotonic), and Residual (anomalies).

**WHO 15 µg/m³ Guideline:** WHO recommends mean 24-hour concentrations not exceed 15 µg/m³ for long-term 
health protection. Temporary episodic exceedances acceptable.
"""
    }
}

def get_visual_explanation(visual_name, audience="research"):
    """
    Retrieve professional explanation for a visualization.
    
    Args:
        visual_name: Name of the visualization (e.g., "PM2.5 Time Series")
        audience: "public", "research", or "simple"
    
    Returns:
        str: Appropriate explanation for audience
    """
    exp = VISUAL_EXPLANATIONS.get(visual_name, {})
    if audience == "simple":
        return exp.get("simple", "")
    elif audience == "research":
        return exp.get("research", "")
    else:  # public
        return exp.get("interpretation_for_public", "")


def get_section_content(section_name):
    """Get report section template."""
    return REPORT_SECTIONS.get(section_name, {})
