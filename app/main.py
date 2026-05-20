from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .analysis import analyze_dataset, build_comparison_pdf

APP_ROOT = Path(__file__).resolve().parent
# DATA_DIR env var lets any deployment (HF Spaces, Docker, Vercel) set the writable path.
# Falls back to a local data directory for development.
DATA_ROOT = Path(os.environ.get("DATA_DIR", str(APP_ROOT / "data")))
DATA_ROOT.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="PurpleAir Local Analyzer")

app.mount("/static", StaticFiles(directory=str(APP_ROOT / "static")), name="static")


_UUID_DIR_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _is_uuid_dir(dir_path: Path) -> bool:
    return bool(_UUID_DIR_RE.match(dir_path.name))


def _parse_int_env(var_name: str) -> int | None:
    raw = os.getenv(var_name)
    if raw is None or raw.strip() == "":
        return None

    try:
        return int(raw)
    except ValueError:
        return None


def _env_truthy(var_name: str) -> bool:
    raw = os.getenv(var_name)
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _cleanup_data_root(*, keep_dirs: set[Path] | None = None) -> None:
    """Optionally delete older analysis job folders under app/data.

    By default this keeps a small number of the most recent UUID job folders to
    prevent unbounded growth. You can override by setting either:
    - PAIR_ANALYZER_MAX_JOBS: keep only the N most-recent job folders
    - PAIR_ANALYZER_MAX_AGE_DAYS: delete job folders older than N days

    Note: this deletes entire job folders, which will make old download links invalid.
    """

    keep_dirs = keep_dirs or set()

    if _env_truthy("PAIR_ANALYZER_DISABLE_CLEANUP"):
        return

    # Default behavior: keep a small recent history (safe for iterative use).
    default_max_jobs = 5

    max_jobs = _parse_int_env("PAIR_ANALYZER_MAX_JOBS")
    max_age_days = _parse_int_env("PAIR_ANALYZER_MAX_AGE_DAYS")

    if max_jobs is None and max_age_days is None:
        max_jobs = default_max_jobs

    if max_jobs is not None and max_jobs < 0:
        return
    if max_age_days is not None and max_age_days < 0:
        return

    job_dirs: list[Path] = [p for p in DATA_ROOT.iterdir() if p.is_dir() and _is_uuid_dir(p)]
    if not job_dirs:
        return

    # Newest first
    job_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    to_delete: set[Path] = set()

    if max_jobs is not None:
        to_delete.update(job_dirs[max_jobs:])

    if max_age_days is not None:
        cutoff = time.time() - (max_age_days * 86400)
        for job_dir in job_dirs:
            try:
                if job_dir.stat().st_mtime < cutoff:
                    to_delete.add(job_dir)
            except FileNotFoundError:
                continue

    # Never delete explicitly kept dirs.
    to_delete.difference_update(keep_dirs)

    # Delete oldest first (more predictable for logs)
    for job_dir in sorted(to_delete, key=lambda p: p.stat().st_mtime):
        shutil.rmtree(job_dir, ignore_errors=True)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    index_path = APP_ROOT / "templates" / "index.html"
    return FileResponse(path=str(index_path), media_type="text/html")


async def _run_analysis_job(file: UploadFile, *, generate_outputs: bool = True, metadata: dict | None = None) -> tuple[str, Path, dict, dict]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing file name")
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in {".csv", ".xlsx"}:
        raise HTTPException(status_code=400, detail="Only .csv or .xlsx supported")

    file_id = str(uuid.uuid4())
    job_dir = DATA_ROOT / file_id
    job_dir.mkdir(parents=True, exist_ok=True)

    raw_path = job_dir / f"raw{ext}"
    with raw_path.open("wb") as f:
        f.write(await file.read())

    try:
        result, outputs = analyze_dataset(raw_path, job_dir, generate_outputs=generate_outputs, metadata=metadata)
    except Exception as exc:
        import traceback
        raise HTTPException(status_code=500, detail=f"Analysis failed: {exc}\n{traceback.format_exc()}")

    # Store summary payload with outputs for reuse or debugging.
    summary_data = result.copy()
    summary_data["outputs"] = outputs
    with (job_dir / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary_data, f, ensure_ascii=True)

    return file_id, job_dir, result, outputs


@app.post("/api/analyze")
async def analyze(
    file: UploadFile = File(...),
    device_id: str = Form(""),
    location: str = Form(""),
    timezone: str = Form("UTC"),
) -> dict:
    metadata = {"device_id": device_id.strip(), "location": location.strip(), "timezone": timezone.strip()}
    file_id, job_dir, result, outputs = await _run_analysis_job(file, metadata=metadata)

    # Store metadata in summary.json for comparison report use
    _cleanup_data_root(keep_dirs={job_dir})

    return {"file_id": file_id, "result": result, "outputs": outputs}


@app.post("/api/analyze-multi")
async def analyze_multi(files: list[UploadFile] = File(...)) -> dict:
    if not files or len(files) < 2:
        raise HTTPException(status_code=400, detail="Upload at least 2 files")
    if len(files) > 10:
        raise HTTPException(status_code=400, detail="Upload up to 10 files")

    analyses: list[dict] = []
    keep_dirs: set[Path] = set()

    for file in files:
        file_id, job_dir, result, outputs = await _run_analysis_job(file, generate_outputs=False)
        keep_dirs.add(job_dir)

        # Return a comparison-focused payload (still includes ids for downloads).
        analyses.append(
            {
                "file_id": file_id,
                "filename": file.filename,
                "summary": result.get("summary", {}),
                "timeseries": result.get("charts", {}).get("timeseries", {}),
                "daily_doy": result.get("charts", {}).get("daily_doy", []),
                "outputs": outputs,
            }
        )

    # Run cleanup once so we never delete jobs from this batch.
    _cleanup_data_root(keep_dirs=keep_dirs)

    return {"analyses": analyses}


@app.get("/api/download/{file_id}/report_word")
def export_to_word(file_id: str) -> FileResponse:
    """Export analysis report as editable Word document (.docx)"""
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor
    from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
    import pandas as pd
    
    job_dir = DATA_ROOT / file_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="Analysis not found")
    
    # Load summary.json
    summary_path = job_dir / "summary.json"
    if not summary_path.exists():
        raise HTTPException(status_code=404, detail="Summary data not found")
    
    try:
        with summary_path.open("r", encoding="utf-8") as f:
            summary_data = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read summary: {str(e)}")
    
    try:
        # Create Word document
        doc = Document()
        
        # Get summary data
        result = summary_data.get("result", {})
        summary = result.get("summary", summary_data.get("summary", {}))
        detected = result.get("detected", summary_data.get("detected", {}))
        date_range = summary.get("date_range", {})
        
        # Add title
        title = doc.add_heading('Air Quality Analysis Report', 0)
        title.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        
        # Add subtitle with date
        subtitle = doc.add_paragraph('Professional Environmental Data Analysis')
        subtitle.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        subtitle_format = subtitle.runs[0].font
        subtitle_format.size = Pt(12)
        subtitle_format.color.rgb = RGBColor(100, 100, 100)
        
        # Date range
        date_p = doc.add_paragraph()
        date_p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        date_p.add_run(f"Analysis Period: {date_range.get('start', 'N/A')} to {date_range.get('end', 'N/A')}")
        
        doc.add_paragraph()  # Spacing
        
        # ===== 1. EXECUTIVE SUMMARY =====
        doc.add_heading('1. Executive Summary', level=1)
        
        summary_section = doc.add_paragraph()
        summary_section.add_run(f"Quality Score: ").bold = True
        summary_section.add_run(f"{summary.get('quality_score', 'N/A')}%")
        
        doc.add_paragraph(f"Period: {date_range.get('start', 'N/A')} to {date_range.get('end', 'N/A')}")
        doc.add_paragraph(f"Quality Score: {summary.get('quality_score', 'N/A')}% | Total Readings: {summary.get('total_readings', 'N/A')}")
        doc.add_paragraph(
            f"Average PM2.5: {summary.get('pm25_average', 'N/A')} µg/m³ | "
            f"AQI: {summary.get('aqi_average', 'N/A')} ({summary.get('aqi_category', 'N/A')})"
        )
        
        if summary.get('quality_narrative'):
            doc.add_paragraph(summary['quality_narrative'], style='List Bullet')
        
        # ===== 2. KEY METRICS TABLE =====
        doc.add_heading('2. Key Metrics Summary', level=1)
        table = doc.add_table(rows=1, cols=2)
        table.style = 'Table Grid'
        hdr_cells = table.rows[0].cells
        hdr_cells[0].text = 'Metric'
        hdr_cells[1].text = 'Value'
        
        metrics = [
            ('Current AQI', f"{summary.get('aqi_current', 'N/A')} - {summary.get('aqi_category', 'N/A')}"),
            ('Average AQI', summary.get('aqi_average', 'N/A')),
            ('AQI 10th Percentile', summary.get('aqi_10pct', 'N/A')),
            ('AQI 90th Percentile', summary.get('aqi_90pct', 'N/A')),
            ('Average PM2.5 (µg/m³)', summary.get('pm25_average', 'N/A')),
            ('EPA-Corrected Average', summary.get('pm25_average_epa_corrected', 'N/A')),
            ('PM2.5 10th Percentile', summary.get('pm25_10pct', 'N/A')),
            ('PM2.5 90th Percentile', summary.get('pm25_90pct', 'N/A')),
            ('Quality Score', f"{summary.get('quality_score', 'N/A')}%"),
            ('Validity Score', f"{summary.get('validity_score', 'N/A')}%"),
            ('Coverage Score', f"{summary.get('coverage_score', 'N/A')}%"),
            ('Sensor Health (CV)', f"{summary.get('sensor_health_cv', 'N/A')}%"),
            ('Channel Agreement (R²)', summary.get('channel_agreement_r2', 'N/A')),
            ('Channel Agreement Rate', f"{summary.get('channel_agreement_pct', 'N/A')}%"),
            ('Total Valid Readings', summary.get('total_readings', 'N/A')),
        ]
        
        for metric_name, metric_value in metrics:
            row_cells = table.add_row().cells
            row_cells[0].text = str(metric_name)
            row_cells[1].text = str(metric_value)
        
        doc.add_paragraph()  # Spacing
        
        # ===== 3. VISUALIZATIONS =====
        doc.add_heading('3. Key Visualizations', level=1)
        
        # Add charts if they exist
        charts_to_add = [
            ('fig_radar_quality.png', 'Data Quality Profile Radar', 
             'Multi-dimensional quality assessment evaluating PM2.5 validity, timestamp accuracy, sensor validity, '
             'temporal consistency, and data coverage. Scores ≥80% on all metrics indicate research-grade data reliability.'),
            ('fig_channel_ab.png', 'Channel A vs B Correlation',
             'Validates instrument health by comparing dual-sensor measurements. Shows Pearson correlation coefficient and '
             'mean absolute difference. High agreement (R² > 0.85) indicates excellent data quality.'),
            ('fig_diurnal.png', 'Hourly Diurnal Pattern',
             'PM2.5 aggregated by hour of day showing typical daily cycle. Mean (bold line) with 10th-90th percentile bands. '
             'Peaks align with traffic rush hours and emission source activity.'),
            ('fig_drift.png', 'Sensor Drift Detection',
             'Time-series of channel differences with 7-day rolling median. Flat line near zero indicates excellent agreement. '
             'Persistent slope indicates sensor drift requiring recalibration.'),
            ('fig_rolling_medians.png', 'Rolling Median Trends',
             '24-hour and 7-day moving medians filter noise while preserving signal transitions. Shows underlying air quality trend.'),
            ('fig_radar_pm25_temporal.png', 'PM2.5 Temporal Radar (24-Hour)',
             'Hourly average PM2.5 in polar format with 00:00 at top. Distance from center = concentration. Reveals diurnal patterns.'),
            ('fig_stl_residuals.png', 'STL Decomposition Residuals',
             'Pollution events independent of regular diurnal cycle. Red points highlight anomalies >2σ from baseline. '
             'Identifies non-routine pollution sources.'),
        ]
        
        for filename, chart_title, chart_description in charts_to_add:
            chart_path = job_dir / filename
            if chart_path.exists():
                doc.add_heading(chart_title, level=2)
                doc.add_paragraph(chart_description, style='List Bullet')
                try:
                    doc.add_picture(str(chart_path), width=Inches(5.5))
                except Exception as e:
                    doc.add_paragraph(f"[Chart image: {filename}]", style='List Bullet')
                doc.add_paragraph()  # Spacing
        
        # ===== 4. METHODS & DATA QUALITY =====
        doc.add_heading('4. Methods & Data Quality Framework', level=1)
        
        doc.add_heading('Sensor Validation', level=2)
        doc.add_paragraph(
            'All measurements checked against physically plausible ranges:\n'
            '• PM2.5: 0–1000 µg/m³\n'
            '• Temperature: -40 to +140°F\n'
            '• Humidity: 0–100%\n'
            '• Pressure: 800–1100 hPa',
            style='List Bullet'
        )
        
        doc.add_heading('Data Processing', level=2)
        doc.add_paragraph(
            'Processing pipeline:\n'
            '• Timestamps validated for monotonic progression\n'
            '• Duplicates removed\n'
            '• EPA correction factors applied (0.534×PM + 5.604 − 0.0844×RH when RH available)\n'
            '• Detection limit correction applied (values <1 µg/m³ corrected to ~0.707 µg/m³)',
            style='List Bullet'
        )
        
        doc.add_heading('Quality Scoring', level=2)
        doc.add_paragraph(
            'Quality Score Formula: Q = (0.7 × Validity Score) + (0.3 × Coverage Score)\n'
            'This formula prioritizes data integrity (70% weight) while accounting for temporal gaps and completeness (30% weight).',
            style='List Bullet'
        )
        
        doc.add_heading('Analytical Methods', level=2)
        doc.add_paragraph(
            'Temporal Pattern Analysis: Data aggregated by hour of day to identify diurnal cycles. Mean and percentiles (10th, 90th) '
            'computed to show typical variability.',
            style='List Bullet'
        )
        doc.add_paragraph(
            'Trend Decomposition: STL decomposition isolates trend (monotonic direction), seasonality (24-hour cycle), and residuals '
            '(anomalies). Distinguishes cyclical patterns from baseline shifts.',
            style='List Bullet'
        )
        doc.add_paragraph(
            'Rolling Medians: 24-hour and 7-day moving medians applied to filter short-term noise while preserving signal transitions. '
            'Median (vs mean) resists extreme outliers.',
            style='List Bullet'
        )
        
        # ===== 5. SENSOR PERFORMANCE =====
        doc.add_heading('5. Sensor Performance Analysis', level=1)
        
        if summary.get('channel_agreement_r2'):
            doc.add_paragraph(f"Channel Agreement (R²): {summary.get('channel_agreement_r2', 'N/A')}")
            doc.add_paragraph(f"Mean Absolute Difference: {summary.get('channel_agreement_mad', 'N/A')} µg/m³")
            doc.add_paragraph(f"Agreement Rate: {summary.get('channel_agreement_pct', 'N/A')}%")
            
            r2_val = float(str(summary.get('channel_agreement_r2', '0')).split()[0])
            if r2_val > 0.85:
                status_para = doc.add_paragraph()
                status_para.add_run("✓ Status: Excellent performance - channels are highly consistent.").font.bold = True
            elif r2_val > 0.70:
                status_para = doc.add_paragraph()
                status_para.add_run("⚠ Status: Good performance - minor drift detected.").font.bold = True
            else:
                status_para = doc.add_paragraph()
                status_para.add_run("✗ Status: Poor channel agreement - recalibration recommended.").font.bold = True
        
        doc.add_paragraph(f"Sensor Health (Coefficient of Variation): {summary.get('sensor_health_cv', 'N/A')}%")
        if summary.get('sensor_health_cv'):
            try:
                cv = float(str(summary.get('sensor_health_cv', '0')).split()[0])
                if cv < 10:
                    doc.add_paragraph("✓ Excellent consistency (CV < 10%)", style='List Bullet')
                elif cv < 15:
                    doc.add_paragraph("✓ Acceptable consistency (CV 10-15%)", style='List Bullet')
                else:
                    doc.add_paragraph("⚠ High variability detected (CV > 15%)", style='List Bullet')
            except:
                pass
        
        # ===== 6. DETECTED DATA COLUMNS =====
        if detected:
            doc.add_heading('6. Detected Data Columns', level=1)
            for param, info in detected.items():
                if isinstance(info, dict):
                    p = doc.add_paragraph()
                    p.add_run(f"{param.upper()}: ").bold = True
                    if isinstance(info.get('primary'), dict):
                        p.add_run(
                            f"{info['primary'].get('name', 'Unknown')} "
                            f"(Confidence: {info['primary'].get('confidence', 'N/A')})"
                        )
        
        # ===== 7. REGULATORY COMPARISON =====
        doc.add_heading('7. Comparison to Standards', level=1)
        doc.add_paragraph(
            'EPA 24-Hour Standard: 35 µg/m³ - Unhealthy for sensitive groups when exceeded.',
            style='List Bullet'
        )
        doc.add_paragraph(
            'WHO Guideline: 15 µg/m³ - More stringent target for long-term health protection.',
            style='List Bullet'
        )
        
        avg_pm25 = summary.get('pm25_average', 0)
        if avg_pm25:
            try:
                avg_val = float(str(avg_pm25).split()[0])
                if avg_val > 35:
                    doc.add_paragraph(
                        f"⚠ Alert: Average PM2.5 ({avg_val:.1f} µg/m³) exceeds EPA 24-hour standard.",
                        style='List Bullet'
                    )
                elif avg_val > 15:
                    doc.add_paragraph(
                        f"⚠ Notice: Average PM2.5 ({avg_val:.1f} µg/m³) exceeds WHO guideline.",
                        style='List Bullet'
                    )
            except:
                pass
        
        # ===== 8. KEY FINDINGS =====
        doc.add_heading('8. Key Findings & Anomalies', level=1)
        
        # Try to load anomalies from pollution_events
        try:
            events_path = job_dir / "pollution_events.csv"
            if events_path.exists():
                events_df = pd.read_csv(events_path)
                if len(events_df) > 0:
                    doc.add_paragraph(f"Detected {len(events_df)} pollution events:", style='List Bullet')
                    for idx, row in events_df.head(10).iterrows():
                        event_desc = f"Event {idx+1}: {row.get('timestamp', 'Unknown time')} - {row.get('note', 'Pollution spike detected')}"
                        doc.add_paragraph(event_desc, style='List Bullet')
                    if len(events_df) > 10:
                        doc.add_paragraph(f"... and {len(events_df) - 10} more events (see pollution_events.csv)", style='List Bullet')
                else:
                    doc.add_paragraph("No major pollution events detected.", style='List Bullet')
        except Exception as e:
            doc.add_paragraph("Data quality assessment completed without anomaly detection.", style='List Bullet')
        
        # Add gap analysis if available
        if summary.get('gap_type'):
            doc.add_heading('Data Gaps Analysis', level=2)
            doc.add_paragraph(f"Gap Pattern: {summary.get('gap_type', 'Unknown')}", style='List Bullet')
            doc.add_paragraph(f"Maximum Contiguous Gap: {summary.get('max_contiguous_gap', 'N/A')} hours", style='List Bullet')
            doc.add_paragraph(
                'Note: Contiguous gaps (long stretches) are more damaging to trend decomposition than '
                'stochastic gaps (random blips), as they break temporal sampling assumptions.',
                style='List Bullet'
            )
        
        doc.add_paragraph()  # Spacing
        
        # ===== FOOTER =====
        doc.add_paragraph()
        footer = doc.add_paragraph(
            "Report generated by PurpleAir Local Analyzer - A professional environmental data analysis platform. "
            "Data quality and analysis methodologies adhere to EPA guidance and peer-reviewed environmental science standards."
        )
        footer_format = footer.runs[0].font
        footer_format.size = Pt(9)
        footer_format.color.rgb = RGBColor(150, 150, 150)
        footer.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        
        # Save Word document
        word_path = job_dir / "report_analysis.docx"
        doc.save(word_path)
        
        return FileResponse(path=str(word_path), filename="report_analysis.docx")
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Word export failed: {str(e)}")


@app.get("/api/download/{file_id}/{kind}")
def download(file_id: str, kind: str) -> FileResponse:
    job_dir = DATA_ROOT / file_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="Unknown analysis id")

    # First, try to look up the actual filename from summary.json outputs mapping
    summary_path = job_dir / "summary.json"
    if summary_path.exists():
        try:
            with summary_path.open("r", encoding="utf-8") as f:
                summary = json.load(f)
                if "outputs" in summary and kind in summary["outputs"]:
                    actual_filename = summary["outputs"][kind]
                    file_path = job_dir / actual_filename
                    if file_path.exists():
                        return FileResponse(path=str(file_path), filename=actual_filename)
        except Exception as e:
            pass  # Fall through to other methods

    # Try multiple file extensions (pdf, csv, json, docx, xlsx)
    for ext in ['.pdf', '.docx', '.csv', '.json', '.xlsx']:
        file_path = job_dir / f"{kind}{ext}"
        if file_path.exists():
            return FileResponse(path=str(file_path), filename=f"{kind}{ext}")
    
    # If not found with extensions, try exact name
    file_path = job_dir / kind
    if file_path.exists():
        return FileResponse(path=str(file_path), filename=kind)
    
    raise HTTPException(status_code=404, detail=f"Download not found: {kind}")


@app.post("/api/refine-analysis/{file_id}")
async def refine_analysis(file_id: str, date_from: str, date_to: str) -> dict:
    """Reanalyze data for a specific timeframe."""
    import pandas as pd
    
    job_dir = DATA_ROOT / file_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="Unknown analysis id")
    
    # Find the raw data file
    raw_files = list(job_dir.glob("raw.*"))
    if not raw_files:
        raise HTTPException(status_code=404, detail="Original data file not found")
    
    raw_path = raw_files[0]
    
    try:
        # Parse date strings to timestamps
        ts_from = pd.Timestamp(date_from)
        ts_to = pd.Timestamp(date_to)
        window = (ts_from, ts_to)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {str(e)}")
    
    # Create a new subdirectory for this refined analysis
    date_suffix = f"{date_from.replace('T', '-').replace(':', '')[:13]}_{date_to.replace('T', '-').replace(':', '')[:13]}"
    refine_dir = job_dir / f"refined_{date_suffix}"
    refine_dir.mkdir(parents=True, exist_ok=True)
    
    # Run analysis with the specified time window
    try:
        result, outputs = analyze_dataset(raw_path, job_dir, window=window, output_dir=refine_dir)

        # Store the refined analysis result
        summary_data = result.copy()
        summary_data["outputs"] = outputs
        summary_data["refinement_window"] = {"start": date_from, "end": date_to}
        with (refine_dir / "summary.json").open("w", encoding="utf-8") as f:
            json.dump(summary_data, f, ensure_ascii=True)

        return {"file_id": file_id, "result": result, "outputs": outputs, "refine_dir": str(refine_dir.name)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Refinement failed: {str(e)}")


@app.post("/api/generate-report/{file_id}")
async def generate_custom_report(file_id: str, payload: dict) -> FileResponse:
    """Regenerate PDF with custom notes and stored analysis parameters."""
    from .analysis import build_report_pdf

    job_dir = DATA_ROOT / file_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="Analysis not found")

    summary_path = job_dir / "summary.json"
    if not summary_path.exists():
        raise HTTPException(status_code=404, detail="Summary data not found")

    try:
        with summary_path.open("r", encoding="utf-8") as f:
            stored = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read summary: {e}")

    custom_notes = payload.get("custom_notes") or []

    # Retrieve stored PDF params
    pdf_params = stored.get("_pdf_params", {})
    tz_label = pdf_params.get("tz_label", "UTC")
    channel_agreement = pdf_params.get("channel_agreement")
    gap_analysis = pdf_params.get("gap_analysis", {})
    metadata = pdf_params.get("metadata") or {}

    summary = stored.get("summary", {})

    # quality_rows is stored under tables.quality
    quality_rows = (stored.get("tables") or {}).get("quality") or []

    # anomalies are at top level
    anomalies = stored.get("anomalies") or []

    # radar_profile is stored under charts.radar_pattern
    radar_profile = (stored.get("charts") or {}).get("radar_pattern")

    # highest_events is stored under tables.highest_events
    highest_events = (stored.get("tables") or {}).get("highest_events") or []

    # Figures must be List[Tuple[str, Path]] — same format build_report_figures returns.
    # Map each saved PNG to its (title, path) tuple; skip files that don't exist.
    figure_map = [
        ("Channel A vs B",      "fig_channel_ab.png"),
        ("Diurnal pattern",     "fig_diurnal.png"),
        ("Sensor drift",        "fig_drift.png"),
        ("Rolling medians",     "fig_rolling_medians.png"),
        ("PM2.5 Temporal Radar","fig_radar_pm25_temporal.png"),
        ("STL Residuals",       "fig_stl_residuals.png"),
    ]
    figures = [(title, job_dir / fn) for title, fn in figure_map if (job_dir / fn).exists()]

    report_path = job_dir / "report_custom.pdf"
    try:
        build_report_pdf(
            report_path,
            summary,
            quality_rows,
            anomalies,
            figures,
            channel_agreement=channel_agreement,
            gap_analysis=gap_analysis,
            radar_profile=radar_profile,
            metadata=metadata,
            custom_notes=custom_notes,
            tz_label=tz_label,
            highest_events=highest_events,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {e}")

    return FileResponse(path=str(report_path), filename="report_analysis.pdf", media_type="application/pdf")


@app.post("/api/compare-report")
async def generate_compare_report(payload: dict) -> FileResponse:
    """Generate and download a comprehensive comparison PDF report."""
    file_ids: list[str] = payload.get("file_ids", [])
    filenames: list[str] = payload.get("filenames", [])

    if len(file_ids) < 2:
        raise HTTPException(status_code=400, detail="Provide at least 2 file_ids")

    analyses: list[dict] = []
    for fid, fname in zip(file_ids, filenames):
        job_dir = DATA_ROOT / fid
        summary_path = job_dir / "summary.json"
        if not summary_path.exists():
            raise HTTPException(status_code=404, detail=f"Analysis not found: {fid}")
        try:
            with summary_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            # summary.json stores result at top level (no nested "result" key)
            analyses.append({"summary": data.get("summary", {})})
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to read analysis {fid}: {e}")

    if not filenames or len(filenames) < len(file_ids):
        filenames = [f"File {i+1}" for i in range(len(file_ids))]

    # Write comparison PDF to a temp dir associated with the first job
    out_dir = DATA_ROOT / file_ids[0]
    report_path = out_dir / "comparison_report.pdf"
    try:
        build_comparison_pdf(report_path, analyses, filenames)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {str(e)}")

    return FileResponse(path=str(report_path), filename="comparison_report.pdf", media_type="application/pdf")
