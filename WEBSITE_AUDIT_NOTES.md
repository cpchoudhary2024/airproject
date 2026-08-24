# PurpleAir Air-Quality Analyzer — Website Audit Notes

Living document for the full accuracy/defensibility audit of the deployed web app.
Goal: the website must be 100% defensible among researchers and the public.
Method: **independent recomputation** (plain pandas / first principles), not the app's own
tests; read code, verify numbers, view every visual, read every narrative.

- **Deployed == local:** local HEAD, `origin/main`, `huggingface/main` all = `f932ec9`
  (`app/` diff empty). Auditing local `app/` audits the live site.
- **App file:** `app/analysis.py` (~6,036 lines), `app/main.py` (~1,139), `app/static/app.js`
  (~2,228), `app/templates/index.html` (~650), `app/visual_explanations.py` (~598).
- **Trusted reference implementation:** `compute_and_plot.py` (root, untracked) — independently
  verified to reproduce `evidence.json` byte-for-byte from raw data. Use as a cross-check oracle.

---

## Correction formulas (verified ✔)
Source of truth: Barkjohn et al., AMT 14, 4617 (2021), Eq. 10.
- **EPA/Barkjohn:** `0.524·PA_cf1 − 0.0862·RH + 5.75`, clipped at 0.  ✔ correct, matches pipeline.
- **LRAPA:** `0.5·PA_cf1 − 0.66`, clipped at 0.  ✔ matches published LRAPA.
- **AQ&U:** `0.778·PA_cf1 + 2.65`, clipped at 0.  ✔ matches published AQ&U (Utah).
- Coefficients are single constants; displayed formula/worked example render from the same
  constants, so shown ≠ computed cannot happen. ✔
- **Channel selection** (`choose_channels`): A/B kept in the primary's calibration family
  (cf_1 vs atm), primary prefers cf_1. ✔ (this was a prior bug, now fixed.)

## AQI (methodological limitation — must stay illustrative)
- Breakpoints = **EPA 2024-updated** table (Good 0.0–9.0, Moderate 9.1–35.4, …). Current & correct.
- Value calc truncates to 0.1 grid per EPA TAD; NaN/negative → "Unknown" (not 0 into averages).
- **KNOWN LIMITATION (user-confirmed):** US AQI is defined on a **24-hour average** from
  **FEM/FRM reference monitors**, not per-reading low-cost optical data. AQI is a *nonlinear*
  transform of concentration, so **averaging AQI values is improper** (avg-of-AQI ≠ AQI-of-avg).
  → AUDIT ACTION: confirm the app computes AQI from **daily-average concentration** for any
    daily/calendar display, never averages per-reading AQI, and labels AQI as illustrative.
- Extreme-range breakpoints (>225 µg/m³) split 301-400 / 401-500; EPA 2024 may define a single
  301-500 segment (225.5–325.4). Only affects wildfire-level values, never residential data.
  → AUDIT ACTION: verify against EPA 2024 TAD; low severity (out of range for this app's use).

---

## Feature inventory (to audit)
Numeric/analytic:
- [ ] corrections (EPA/LRAPA/AQ&U) — formulas ✔; verify applied to cf_1, RH handling, clip
- [ ] QC: A/B agreement filter (EPA 5 µg/m³ AND 61% rule), coverage, completeness
- [ ] descriptive stats / exceedances (WHO 5/15, EPA 9/35)
- [ ] AQI + calendar heatmap
- [ ] pollution-event detection (`detect_events`)
- [ ] trend test (`build_trend_test`) — Mann-Kendall + Hamed-Rao, no bogus annualisation
- [ ] diurnal pattern (`build_diurnal_pattern`) — hourly grouping done correctly
- [ ] seasonal / rolling medians / STL decomposition
- [ ] sensor drift (`build_sensor_drift`)
- [ ] uncertainty (`build_uncertainty`)
- [ ] exposure metrics (`build_exposure_metrics`)
- [ ] Compare Houses / difference-in-differences (`compute_did`) — daily means, HAC SE
- [ ] radar profiles (data-quality; temporal)
Outputs:
- [ ] every chart (`build_report_figures`) — render & view
- [ ] narratives (`build_narrative_summary`, `build_report_markdown`, `build_anomaly_report`)
- [ ] PDF reports (community / research / comparison)
Robustness:
- [ ] edge cases: missing columns, single channel, timezones, partial days, tiny/huge files

---

## Authoritative sources consulted
- EPA/AirNow AQI breakpoints (current, post-2024): https://aqs.epa.gov/aqsweb/documents/codetables/aqi_breakpoints.html
- EPA "Final Updates to the AQI for PM" fact sheet (2024): https://www.epa.gov/system/files/documents/2024-02/pm-naaqs-air-quality-index-fact-sheet.pdf
- Confirmed: **AQI = max of pollutant sub-indices** (PM2.5, PM10, O3, CO, SO2, NO2); real-time AQI uses **NowCast** (weighted ~12-h avg); daily AQI uses the **24-h average**.
- Authoritative current PM2.5 (24-h) breakpoints:
  0.0–9.0→0–50 · 9.1–35.4→51–100 · 35.5–55.4→101–150 · 55.5–125.4→151–200 ·
  125.5–225.4→201–300 · **225.5–325.4→301–500 (single segment)** · 325.5+→501–999.

## Findings log (severity: BLOCKER / HIGH / MEDIUM / LOW / OK)
Each verified by independent recomputation and/or authoritative source.

- **F1 — HIGH (defensibility): AQI is labeled "AQI" but only PM2.5 is measured.** The US AQI is
  multi-pollutant (max of sub-indices); a PM2.5 sensor can only yield the **PM2.5 sub-index**.
  Calling it "AQI"/"Air Quality Index" overstates it. Also: "current AQI" from one reading and
  "average AQI" from a period mean are not how EPA reports AQI (NowCast for real-time; 24-h avg
  for daily). Daily calendar AQI IS computed from daily-mean concentration (correct basis).
  → FIX: relabel to "PM2.5 AQI (sub-index)" everywhere (UI, narrative, PDF); add caveat that the
    official multi-pollutant AQI may be higher, and that real-time AQI uses NowCast. Keep values.
- **F2 — LOW (confirmed, out of range): extreme AQI breakpoints wrong.** App maps 225.5–325.4→301–400
  and 325.5+→401–500. EPA: 225.5–325.4→**301–500**, 325.5+→501–999. Only affects PM2.5>225 µg/m³
  (never residential). → FIX table to match EPA.
- **F3 — VERIFY/disclosure: EPA A/B agreement exclusion (5 µg/m³ AND 61%) is NOT applied.** Cleaning
  keeps rows on valid_timestamp & valid_pm25 only; A/B are averaged and agreement is reported as a
  health metric, not used to exclude. Legitimate design ONLY IF the app does not claim to apply the
  EPA A/B screen. → CHECK narrative/methods claims; if claimed, it's HIGH.
- **F4 — VERIFY/disclosure: LOD/√2 (~0.707) substitution for values <1 µg/m³** (raw and corrected).
  Recognized left-censoring method, but differs from the report pipeline (clip at 0) and slightly
  raises clean-air means; a reviewer recomputing plainly gets lower numbers. → CHECK it is disclosed
  in methods; if undisclosed, MEDIUM.

- **F5 — MEDIUM/HIGH (overclaim): "Research-Grade" + "appropriate for regulatory submissions".**
  `build_report_markdown` title (L920) and quality narrative (L471/L5640) claim low-cost sensor data
  is regulatory-grade when quality_score≥90. Overstates what a corrected low-cost optical sensor
  supports. → FIX: soften to "community/screening-grade; complements, not replaces, reference monitors."
- **F6 — MEDIUM (mislabel): quality score wrongly described.** Report exec summary (L929) labels the
  headline score "(percentage of valid readings)", but `summary['quality_score']` is the composite
  `0.4·Validity + 0.6·Coverage` (L5631). The % of valid readings is a *different* number (validity_score).
  → FIX: relabel to "composite quality score (0.4×validity + 0.6×coverage)".
- **F3 (downgraded) — LOW/MEDIUM: no EPA A/B exclusion filter.** Confirmed the app does NOT falsely
  claim it (QA/QC §3.4 lists only range/timestamp/dup checks; §3.5 discloses channel averaging). It is
  a *weaker* QC than the report pipeline, not a lie. → Optional: add A/B screen, or note the limitation.

- **F7 — HIGH (defensibility): "pollution events" over-detected & over-attributed.** Real-data run
  (291796, 40 days) flags **120 "Spike" episodes**: median duration **2 min**, median peak **7.2 µg/m³**
  (below WHO *annual* guideline), some peaks **0.1 µg/m³**. The rule `> rolling_median + 3σ` (2 h window)
  has **no absolute floor**, so it fires on sensor jitter in clean periods; the narrative then attributes
  spikes to "traffic, cooking, or burning". Reporting 120 "pollution episodes" is alarmist/indefensible.
  → FIX: require a spike to ALSO reach a meaningful absolute level (and/or min duration); soften the
    source attribution to "statistical excursions (not necessarily pollution)".

## Cross-check result (real data, sensor 291796, 40 days)
- App corrected mean **9.73** == independent recompute **9.728**. Correction pipeline CORRECT & consistent. ✔
- LOD/√2 substitution raises the mean by **+0.012 µg/m³** — numerically negligible (F4 = disclosure only).
- quality_score 79.8 = 0.4·100(validity) + 0.6·66.4(coverage) — confirms F6 mislabel.

## Verified OK (independently)
- Correction formulas (EPA/LRAPA/AQ&U) + channel-family selection. ✔ (app mean 9.73 == recompute 9.728)
- AQI computed as AQI-of-mean and daily-AQI-from-daily-mean (NOT average-of-AQI); 0.1-grid truncation. ✔
- Trend test: Mann-Kendall + ties + Hamed-Rao autocorrelation correction + Theil-Sen + Pettitt;
  annualisation gated on record length (past "bogus per-year" bug fixed). ✔
- Event detection MECHANICS: time-based 2h window (not sample count), in-progress episode closed,
  half-open slicing. ✔  (but detection THRESHOLD is the F7 problem)
- Compare Houses / DiD: daily-mean unit of analysis, paired-t + OLS w/ HAC/Newey-West SEs. ✔

## FIXES APPLIED (all verified; awaiting redeploy)
- **F1** AQI relabelled → "PM2.5 AQI (sub-index)" on web tiles, compare table, CSV label; added
  footnote (multi-pollutant max + NowCast caveat); PDF recommendation & AQI definition aligned.
- **F2** AQI breakpoint table fixed → 225.5–325.4 = 301–500 (verified AQI(300)=449, matches EPA).
- **F4** LOD/√2 substitution now disclosed in Methods §3.5 (numerical impact ~+0.01 µg/m³ noted).
- **F5** removed "Research-Grade"/"regulatory submissions" overclaims (report title, quality narrative,
  radar caption, PDF footer, app.js tiles) → "community/screening-grade, complements not replaces".
- **F6** quality score relabelled from "(percentage of valid readings)" → composite 0.4·validity+0.6·coverage.
- **F7** spike detector now requires ≥15 µg/m³ absolute floor → events 120→30 (all peak ≥15, median 24.6);
  narrative no longer attributes spikes to specific sources.
- **F8** (new) WHO annual mislabel fixed: "WHO annual (15)" → "WHO 24-hour (15)" (2 spots). Annual = 5.
- **F9** (new) STL chart relabelled "Pollution Events" → "Residual anomalies (2σ)" so it no longer
  collides with the headline pollution-episode count (30 vs 34 were both called "Pollution Events").

## Additional OK (verified this pass)
- Diurnal: input is hourly-resampled first (L5261) → per-day-hour equal weight; past 2-min-grouping bug fixed. ✔
- Drift chart: breaks at real gaps, thresholds labelled. ✔  Uncertainty: CV⊕RMSE quadrature, conservative,
  single-channel labelled. ✔
- Edge cases: single-channel / no-humidity → correction falls back to raw, labelled honestly; missing
  timestamp & 2-row file do not crash. ✔

## NOT exhaustively covered (be honest)
- Rendered charts not visually inspected yet (web uses Plotly from the verified data dict; PDF uses
  matplotlib `build_report_figures`). Data feeding them is largely verified; pixels not eyeballed.
- Not read line-by-line: exposure metrics, radar profile internals, seasonal, rolling-medians,
  and the full multi-page PDF layout code (spot-checked, not exhaustively).
- ALL 8 static/PDF charts now viewed and OK: diurnal (UTC-honest), drift (gaps enforced), STL
  (F9 label fixed), channel_ab (R²=0.996, 1:1+fit), bland-altman (bias/±1.96SD/proportional trend —
  textbook), radar (values match diurnal, UTC), rolling_medians (physical gaps enforced), weekly_heatmap
  (sequential single-hue, WHO-15 marked, peak cell outlined). No new chart defects.
- Web (Plotly) charts render client-side from the SAME verified data dict; not opened in a live browser.
