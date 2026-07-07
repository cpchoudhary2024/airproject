"""End-to-end tests for the PurpleAir analyzer API.

Covers the core analysis path (real numbers come out) and the Phase 0 security
hardening (path traversal, upload cap, download allowlist, headers). Uses a
self-contained synthetic PurpleAir-style CSV so no data files are required.
"""

import os
import tempfile

# Isolate all job output into a throwaway dir before the app is imported
# (DATA_ROOT is resolved at import time).
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pair_tests_"))

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

_VALID_UUID = "11111111-1111-4111-8111-111111111111"


def _make_csv(days: int = 5, freq_min: int = 10) -> bytes:
    n = int(days * 24 * 60 / freq_min)
    idx = pd.date_range("2025-01-01", periods=n, freq=f"{freq_min}min", tz="UTC")
    rng = np.random.default_rng(0)
    base = np.clip(12 + 6 * np.sin(np.arange(n) / 6.0) + rng.normal(0, 2, n), 0, None)
    df = pd.DataFrame({
        "time_stamp": idx.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pm2.5_atm": base.round(2),
        "pm2.5_atm_a": (base * 0.98).round(2),
        "pm2.5_atm_b": (base * 1.02).round(2),
        "humidity": (50 + 10 * np.sin(np.arange(n) / 12.0)).round(1),
        "temperature": (60 + 8 * np.sin(np.arange(n) / 24.0)).round(1),
    })
    return df.to_csv(index=False).encode()


def _analyze(csv: bytes, timezone: str | None = None) -> dict:
    data = {"timezone": timezone} if timezone else {}
    r = client.post("/api/analyze", data=data,
                    files={"file": ("data.csv", csv, "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def test_timezone_as_recorded_keeps_wall_clock():
    """AS_RECORDED must not shift timestamps: the analysis window starts at the
    file's own first wall-clock time (2025-01-01 00:00)."""
    s = _analyze(_make_csv(), timezone="AS_RECORDED")["result"]["summary"]
    assert s["tz_label"] == "As recorded"
    assert s["date_range"]["start"].startswith("2025-01-01T00:00")


def test_timezone_as_recorded_with_zone_name():
    """A user-named zone (e.g. EDT) is carried as a display label — timestamps
    still unshifted."""
    r = client.post(
        "/api/analyze",
        data={"timezone": "AS_RECORDED", "timezone_label": "EDT"},
        files={"file": ("data.csv", _make_csv(), "text/csv")},
    )
    assert r.status_code == 200, r.text
    s = r.json()["result"]["summary"]
    assert s["tz_label"] == "EDT (as recorded)"
    assert s["date_range"]["start"].startswith("2025-01-01T00:00")


def test_timezone_conversion_shifts_wall_clock():
    """Selecting a real timezone converts UTC -> local (New York is UTC-5 in Jan)."""
    s = _analyze(_make_csv(), timezone="America/New_York")["result"]["summary"]
    assert s["tz_label"] == "America/New_York"
    assert s["date_range"]["start"].startswith("2024-12-31T19:00")


def test_analyze_returns_core_numbers():
    body = _analyze(_make_csv())
    assert body["file_id"]
    s = body["result"]["summary"]
    assert s["pm25_average"] is not None
    assert s["pm25_average_epa_corrected"] is not None
    assert 0 <= s["quality_score"] <= 100
    # Additive scientific-rigor payload is present and inert-safe.
    for key in ("exposure", "uncertainty", "repro"):
        assert key in s
    assert s["repro"]["repro_id"]


def test_reproducibility_hash_is_stable():
    csv = _make_csv()
    a = _analyze(csv)["result"]["summary"]["repro"]["repro_id"]
    b = _analyze(csv)["result"]["summary"]["repro"]["repro_id"]
    assert a == b  # same bytes + methods -> same id


def test_reject_bad_extension():
    r = client.post("/api/analyze", files={"file": ("data.txt", b"x", "text/plain")})
    assert r.status_code == 400


def test_empty_file_rejected():
    r = client.post("/api/analyze", files={"file": ("data.csv", b"", "text/csv")})
    assert r.status_code == 400


def test_upload_size_cap(monkeypatch):
    monkeypatch.setenv("PAIR_ANALYZER_MAX_UPLOAD_MB", "1")
    big = b"a,b\n" + b"1,2\n" * (300 * 1024)  # > 1 MB
    r = client.post("/api/analyze", files={"file": ("big.csv", big, "text/csv")})
    assert r.status_code == 413


def test_download_rejects_bad_file_id():
    assert client.get("/api/download/not-a-uuid/report").status_code == 404
    # well-formed UUID but no such job
    assert client.get(f"/api/download/{_VALID_UUID}/report").status_code == 404


def test_download_valid_kind_and_charset():
    fid = _analyze(_make_csv())["file_id"]
    assert client.get(f"/api/download/{fid}/epa_corrected").status_code == 200
    # kind carrying path characters is refused
    assert client.get(f"/api/download/{fid}/..%2f..%2fmain").status_code == 404
    assert client.get(f"/api/download/{fid}/no_such_kind").status_code == 404


def test_error_responses_do_not_leak_tracebacks():
    r = client.post("/api/analyze", files={"file": ("data.txt", b"x", "text/plain")})
    assert "Traceback" not in r.text and "File \"" not in r.text


def test_security_headers_present():
    h = client.get("/").headers
    assert h.get("x-content-type-options") == "nosniff"
    assert h.get("x-frame-options") == "DENY"
    assert "content-security-policy" in h


def test_word_export_content_matches_analysis():
    """The .docx must state the as-recorded timezone and the quality-score
    formula the code actually computes (0.4/0.6) — guards against the report
    drifting from the analysis again."""
    import io
    from docx import Document

    r = client.post(
        "/api/analyze",
        data={"timezone": "AS_RECORDED", "timezone_label": "EDT"},
        files={"file": ("data.csv", _make_csv(), "text/csv")},
    )
    fid = r.json()["file_id"]
    doc_resp = client.get(f"/api/download/{fid}/report_word")
    assert doc_resp.status_code == 200
    text = "\n".join(p.text for p in Document(io.BytesIO(doc_resp.content)).paragraphs)
    assert "EDT local time" in text and "no timezone conversion" in text
    assert "0.4 × Validity" in text and "0.6 × Coverage" in text
    assert "0.7" not in text.split("Quality Score Formula")[1][:120]


def test_compare_flow_and_pdf():
    """Two-file comparison returns per-house payloads and a comparison PDF."""
    files = [("files", ("a.csv", _make_csv(), "text/csv")),
             ("files", ("b.csv", _make_csv(days=4), "text/csv"))]
    r = client.post("/api/analyze-multi", files=files, data={"control_index": "0"})
    assert r.status_code == 200, r.text
    analyses = r.json()["analyses"]
    assert len(analyses) == 2 and analyses[0]["is_control"]

    pdf = client.post("/api/compare-report", json={
        "file_ids": [a["file_id"] for a in analyses],
        "labels": ["Control House", "House 1"],
    })
    assert pdf.status_code == 200
    assert pdf.content[:5] == b"%PDF-"


def test_community_report_pdf_valid():
    fid = _analyze(_make_csv())["file_id"]
    r = client.get(f"/api/download/{fid}/public_report_pdf")
    assert r.status_code == 200
    assert r.content[:5] == b"%PDF-" and len(r.content) > 10_000
