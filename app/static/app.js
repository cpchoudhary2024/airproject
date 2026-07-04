const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('file-input');
const loading = document.getElementById('loading');
const dashboard = document.getElementById('dashboard');

const overviewCards = document.getElementById('overview-cards');
const detectedColumns = document.getElementById('detected-columns');
const downloadButtonsPublic = document.getElementById('download-buttons-public');
const downloadButtonsResearch = document.getElementById('download-buttons-research');
const chartDownloads = document.getElementById('chart-downloads');
const anomalyList = document.getElementById('anomaly-list');
const comparisonSection = document.getElementById('comparison-section');
const comparisonResults = document.getElementById('comparison-results');
const comparisonMeta = document.getElementById('comparison-meta');
const comparisonTable = document.getElementById('comparison-table');
const compareRunButton = document.getElementById('compare-run');
const compareSelection = document.getElementById('compare-selection');
const compareFilePicker = document.getElementById('compare-file-picker');
const compareAddBtn = document.getElementById('compare-add-btn');
const compareAddControlBtn = document.getElementById('compare-add-control-btn');
const compareFileList = document.getElementById('compare-file-list');
const compareClearBtn = document.getElementById('compare-clear');
const compareDownloadReportBtn = document.getElementById('compare-download-report');
const deviceIdInput = document.getElementById('device-id-input');
const locationInput = document.getElementById('location-input');
const narrativeText = document.getElementById('narrative-text');
const narrativeSummary = document.getElementById('narrative-summary');
const correctionSelect = document.getElementById('correction-select');

// State for comparison house list (one-by-one selection, max 10).
// Each entry: { file: File, label: string, isControl: boolean }
let compareFiles = [];
let compareFileIds = [];  // file_ids returned from server after comparison runs (control-first order)
let compareLabels = [];   // display labels aligned with compareFileIds (control-first order)
let _pendingControl = false;  // true while the next picked file should become the Control House

// Timezone applied to the current analysis (IANA name or 'UTC'). All chart
// timestamps from the backend are already converted to this zone, so axis
// labels must reflect it rather than hard-coding 'UTC'.
let currentTzLabel = 'UTC';
function _tzIsUtc() { return !currentTzLabel || currentTzLabel === 'UTC'; }
// Short, human-friendly zone name for axis titles, e.g. 'New York', 'Kolkata'.
function _tzShort() {
  if (_tzIsUtc()) return 'UTC';
  return currentTzLabel.split('/').pop().replace(/_/g, ' ');
}
// "Date/Time (UTC)" vs "Date/Time (New York local time)"
function _tzDateAxis() {
  return _tzIsUtc() ? 'Date/Time (UTC)' : `Date/Time (${_tzShort()} local time)`;
}
// "Hour of Day (UTC)" vs "Hour of Day (New York local time)"
function _tzHourAxis() {
  return _tzIsUtc() ? 'Hour of Day (UTC)' : `Hour of Day (${_tzShort()} local time)`;
}
function _tzHourTag() { return _tzIsUtc() ? 'UTC' : _tzShort(); }

// State for custom notes and current analysis
let customNotes = [];   // [{heading, content}, ...]
let currentFileId = null;

// Verify required DOM elements
const requiredElements = {
  dropzone, fileInput, loading, dashboard,
  overviewCards, detectedColumns, downloadButtonsPublic, downloadButtonsResearch, chartDownloads, anomalyList,
  comparisonSection, comparisonResults, comparisonMeta, comparisonTable,
  compareRunButton, compareSelection
};

const missingElements = Object.entries(requiredElements)
  .filter(([key, el]) => !el)
  .map(([key]) => key);

if (missingElements.length > 0) {
  console.error('Missing required DOM elements:', missingElements);
}

const chartLayouts = {
  base: {
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: '#fafaf8',
    font: { family: 'Space Grotesk, sans-serif', color: '#333', size: 11 },
    margin: { t: 50, r: 80, b: 60, l: 70 },
    autosize: true,
    height: 480,
    legend: {
      x: 1.02,
      y: 1,
      xanchor: 'left',
      yanchor: 'top',
      bgcolor: 'rgba(255,255,255,0.8)',
      bordercolor: '#ddd',
      borderwidth: 1,
      font: { size: 10 }
    },
    hovermode: 'x unified',
    xaxis: {
      showgrid: true,
      gridwidth: 1,
      gridcolor: '#f0f0f0',
      zeroline: false
    },
    yaxis: {
      showgrid: true,
      gridwidth: 1,
      gridcolor: '#f0f0f0',
      zeroline: false
    }
  },
};

const chartConfig = {
  responsive: true,
  displaylogo: false,
  toImageButtonOptions: {
    format: 'png',
    filename: 'chart_export',
    height: 1200,
    width: 1600,
    scale: 2
  }
};

function showLoading(isLoading) {
  loading.classList.toggle('hidden', !isLoading);
}

function setLoadingText(text) {
  if (!loading) return;
  const p = loading.querySelector('p');
  if (p) p.textContent = text;
}

function showDashboard() {
  dashboard.classList.remove('hidden');
  dashboard.scrollIntoView({ behavior: 'smooth' });
}

function buildOverviewCards(summary) {
  if (!overviewCards) {
    console.error('Overview cards container not found');
    return;
  }
  
  overviewCards.innerHTML = '';
  const cards = [
    {
      title: 'Current AQI',
      value: summary.aqi_current,
      note: summary.aqi_category,
      color: summary.aqi_color,
    },
    {
      title: 'Average AQI',
      value: summary.aqi_average,
      note: 'Period average',
      color: summary.aqi_color,
    },
    {
      title: 'Average PM2.5',
      value: `${summary.pm25_average} µg/m³`,
      note: 'Observed PM2.5',
      color: '#1f1c16',
    },
    {
      title: 'Quality Score',
      value: `${summary.quality_score}%`,
      note: `Validity: ${summary.validity_score}% | Coverage: ${summary.coverage_score}%`,
      color: summary.quality_score >= 80 ? '#00a651' : summary.quality_score >= 70 ? '#f6aa1c' : '#ff4444',
      formula: '0.4×Validity + 0.6×Coverage (Research-Grade Penalized Completeness)',
    },
    {
      title: 'Sensor Health (CV)',
      value: summary.sensor_health_cv !== null && summary.sensor_health_cv !== undefined ? `${summary.sensor_health_cv}%` : 'N/A',
      note: summary.sensor_health_status || 'Research-Grade Check',
      color: summary.sensor_health_cv < 10 ? '#00a651' : summary.sensor_health_cv < 15 ? '#f6aa1c' : '#ff4444',
    },
    {
      title: 'Channel Agreement',
      value: summary.channel_agreement_pct ? `${summary.channel_agreement_pct}%` : 'N/A',
      note: `R² consistency | Coeff. of Variation: ${summary.sensor_health_cv}%`,
      color: summary.channel_agreement_pct > 85 ? '#00a651' : '#1f1c16',
    },
    {
      title: 'Date Range',
      value: summary.date_range.start ? `${summary.date_range.start.split('T')[0]} to ${summary.date_range.end.split('T')[0]}` : 'N/A',
      note: `${summary.total_readings} readings`,
      color: '#1f1c16',
    },
    {
      title: 'Timezone',
      value: summary.tz_label && summary.tz_label !== 'UTC' ? summary.tz_label.split('/').pop().replace('_', ' ') : 'UTC',
      note: summary.tz_label || 'UTC — all timestamps in Coordinated Universal Time',
      color: summary.tz_label && summary.tz_label !== 'UTC' ? '#1f7a8c' : '#6d6256',
    },
  ];

  cards.forEach((card) => {
    const div = document.createElement('div');
    div.className = 'card';
    div.innerHTML = `
      <p class="card-title">${card.title}</p>
      <div class="card-value" style="color: ${card.color}">${card.value}</div>
      ${card.formula ? `<p class="card-formula">${card.formula}</p>` : ''}
      <p class="card-note">${card.note}</p>
    `;
    overviewCards.appendChild(div);
  });

  // Display quality narrative if available
  const qualityNarrative = document.getElementById('quality-narrative');
  if (qualityNarrative && summary.quality_narrative) {
    qualityNarrative.classList.remove('hidden');
    qualityNarrative.innerHTML = `
      <div class="narrative-box">
        <h3>Data Quality Context</h3>
        <p class="narrative-text">${summary.quality_narrative}</p>
      </div>
    `;
  }
}

function buildDataCompleteness(dc) {
  const el = document.getElementById('data-completeness');
  if (!el) return;
  if (!dc || !Array.isArray(dc.items)) { el.innerHTML = ''; return; }

  const levelRank = { required: 0, important: 1, recommended: 2, optional: 3 };
  const items = [...dc.items].sort((a, b) =>
    (a.present === b.present ? 0 : a.present ? 1 : -1) ||
    (levelRank[a.level] ?? 9) - (levelRank[b.level] ?? 9));

  // Banner when an important/required input is missing
  let banner = '';
  if (dc.missing_important && dc.missing_important.length) {
    banner = `<div class="dc-banner warn">⚠ This file is missing ${dc.missing_important.length} important input(s): `
      + `<strong>${dc.missing_important.map(escapeHtmlSafe).join(', ')}</strong>. See impact below.</div>`;
  } else {
    banner = `<div class="dc-banner ok">✓ All required and important inputs are present.</div>`;
  }

  const rows = items.map(it => {
    const ok = !!it.present;
    const icon = ok ? '✓' : (it.level === 'optional' ? '–' : '✗');
    const cls = ok ? 'present' : (it.level === 'required' || it.level === 'important' ? 'missing' : 'absent');
    const found = ok && it.found ? `<span class="dc-found">${escapeHtmlSafe(it.found)}</span>` : '';
    return `
      <div class="dc-row ${cls}">
        <span class="dc-icon">${icon}</span>
        <div class="dc-main">
          <div class="dc-name">${escapeHtmlSafe(it.name)} <span class="dc-level ${it.level}">${it.level}</span> ${found}</div>
          ${ok ? '' : `<div class="dc-impact">${escapeHtmlSafe(it.impact)}</div>`}
        </div>
        <span class="dc-status ${cls}">${ok ? 'Present' : 'Missing'}</span>
      </div>`;
  }).join('');

  el.innerHTML = banner + `<div class="dc-list">${rows}</div>`;
}

function escapeHtmlSafe(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function buildDetectedColumns(detected) {
  if (!detectedColumns) {
    console.error('Detected columns container not found');
    return;
  }
  
  detectedColumns.innerHTML = '';
  Object.entries(detected).forEach(([key, value]) => {
    const card = document.createElement('div');
    card.className = 'detected-card';
    const primary = value.primary ? `${value.primary.name} (${value.primary.confidence})` : 'Not found';
    const dual = value.dual ? 'Dual channels detected' : 'Single channel';
    card.innerHTML = `
      <h3>${key.toUpperCase()}</h3>
      <p><strong>Primary:</strong> ${primary}</p>
      <p><strong>Status:</strong> ${dual}</p>
    `;
    detectedColumns.appendChild(card);
  });
}

function buildTables(tables) {
  const statsContainer = document.getElementById('table-stats');
  const exceedContainer = document.getElementById('table-exceedances');
  const highestContainer = document.getElementById('table-highest-events');

  if (!statsContainer || !exceedContainer) {
    console.error('One or more table containers not found');
    return;
  }

  statsContainer.innerHTML = `<div class="table-scroll">${buildTable(tables.stats)}</div>`;
  exceedContainer.innerHTML = `
    <table>
      <tr><th>WHO 15 µg/m³ hours</th><td>${tables.exceedances.who_15}</td></tr>
      <tr><th>EPA 35 µg/m³ hours</th><td>${tables.exceedances.epa_35}</td></tr>
    </table>
  `;

  if (highestContainer) {
    if (tables.highest_events && tables.highest_events.length > 0) {
      highestContainer.innerHTML =
        `<div class="event-type-legend">` +
          `<span class="event-type-badge spike">Spike</span>` +
          `<span class="event-type-desc">Short, sharp PM2.5 surge (typically minutes–2 hrs) caused by a nearby source such as traffic, cooking, or burning.</span>` +
          `<span class="event-type-badge sustained">Sustained</span>` +
          `<span class="event-type-desc">Prolonged elevated PM2.5 lasting 3+ hrs, often indicating regional pollution, wildfires, or industrial activity.</span>` +
        `</div>` +
        `<p class="event-range-note">` +
          `<strong>PM2.5 Range</strong> shows the Min – Max µg/m³ recorded during the event window, giving a sense of how far concentrations varied. ` +
          `<strong>Duration (hh:mm)</strong> is shown as hours:minutes (e.g. 03:30 = 3 hours 30 minutes).` +
        `</p>` +
        `<div class="table-scroll">${buildTable(tables.highest_events)}</div>`;
    } else {
      highestContainer.innerHTML = '<p class="empty">No significant pollution events detected.</p>';
    }
  }
}

function buildTable(rows) {
  if (!rows || rows.length === 0) {
    return '<p class="empty">No data available.</p>';
  }
  const headers = Object.keys(rows[0]);
  const headerRow = headers.map((h) => `<th>${h}</th>`).join('');
  const body = rows
    .map((row) => {
      const cells = headers.map((h) => `<td>${row[h] ?? ''}</td>`).join('');
      return `<tr>${cells}</tr>`;
    })
    .join('');
  return `<table><thead><tr>${headerRow}</tr></thead><tbody>${body}</tbody></table>`;
}

function buildDownloads(fileId, outputs) {
  const downloadResearch = document.getElementById('download-buttons-research');
  const downloadPublic   = document.getElementById('download-buttons-public');

  if (!downloadResearch || !downloadPublic) {
    console.error('Download containers not found');
    return;
  }

  downloadResearch.innerHTML = '';
  downloadPublic.innerHTML   = '';

  // ── Community Report card ────────────────────────────────────────────────
  const communityCard = document.getElementById('community-report-card');
  if (communityCard) {
    const hasReport = !!(outputs.research_report_pdf || outputs.report_pdf || outputs.public_report_pdf);
    communityCard.style.display = hasReport ? 'block' : 'none';

    if (hasReport) {
      // Pre-fill cr-inputs from the main upload form so the user sees their values
      const crDeviceEl  = document.getElementById('cr-device-id');
      const crLocationEl = document.getElementById('cr-location');
      if (crDeviceEl && deviceIdInput && deviceIdInput.value.trim() && !crDeviceEl.value.trim()) {
        crDeviceEl.value = deviceIdInput.value.trim();
      }
      if (crLocationEl && locationInput && locationInput.value.trim() && !crLocationEl.value.trim()) {
        crLocationEl.value = locationInput.value.trim();
      }

      // Wire button — replace to clear any old listener
      const oldBtn = document.getElementById('btn-community-report');
      if (oldBtn) {
        const btn = oldBtn.cloneNode(true);
        oldBtn.parentNode.replaceChild(btn, oldBtn);
        btn.addEventListener('click', async () => {
          btn.disabled = true;
          btn.textContent = 'Generating…';
          try {
            const crDeviceId = (document.getElementById('cr-device-id')?.value || '').trim();
            const crLocation  = (document.getElementById('cr-location')?.value  || '').trim();
            // Always regenerate via POST so device_id/location are embedded fresh
            const fd = new FormData();
            fd.append('device_id', crDeviceId);
            fd.append('location',  crLocation);
            const resp = await fetch(`/api/community_report/${fileId}`, { method: 'POST', body: fd });
            if (!resp.ok) throw new Error('generation failed');
            const blob = await resp.blob();
            const url  = URL.createObjectURL(blob);
            const a    = document.createElement('a');
            a.href = url;
            a.download = `community_air_quality_report_${new Date().toISOString().split('T')[0]}.pdf`;
            document.body.appendChild(a); a.click(); document.body.removeChild(a);
            URL.revokeObjectURL(url);
          } catch (e) {
            alert('Could not generate the community report. Please try again.');
          } finally {
            btn.disabled = false;
            btn.textContent = 'Download Report';
          }
        });
      }
    }
  }
  // ─────────────────────────────────────────────────────────────────────────
  
  // Research report — POST with custom notes so they are embedded in the PDF
  if (outputs.research_report_pdf || outputs.report_pdf) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn primary';
    btn.textContent = 'Research Report PDF';
    btn.title = 'Complete research-grade analysis report (includes your custom notes)';
    btn.addEventListener('click', () => downloadReportWithNotes(fileId));
    downloadResearch.appendChild(btn);
  }
  
  // Word export (for editing)
  const wordBtn = document.createElement('a');
  wordBtn.href = `/api/download/${fileId}/report_word`;
  wordBtn.className = 'btn secondary';
  wordBtn.textContent = 'Report as Word (.docx)';
  wordBtn.title = 'Editable Word document - add notes and details easily';
  downloadResearch.appendChild(wordBtn);
  
  // Data exports for research section
  const researchDataDownloads = [
    { key: 'cleaned_data', label: 'Quality-Flagged Dataset (CSV)' },
    { key: 'epa_corrected', label: 'EPA-Corrected PM2.5 (CSV)' },
    { key: 'pollution_events', label: 'Anomaly Detection Log (CSV)' },
  ];
  
  researchDataDownloads.forEach((item) => {
    if (!outputs[item.key]) return;
    const btn = document.createElement('a');
    btn.href = `/api/download/${fileId}/${item.key}`;
    btn.className = 'btn secondary';
    btn.textContent = item.label;
    downloadResearch.appendChild(btn);
  });
  
  // Public section - simple exports
  const publicDataDownloads = [
    { key: 'hourly_summary', label: 'Hourly Summary Data (CSV)' },
    { key: 'daily_aqi', label: 'Daily AQI Data (CSV)' },
  ];
  
  publicDataDownloads.forEach((item) => {
    if (!outputs[item.key]) return;
    const btn = document.createElement('a');
    btn.href = `/api/download/${fileId}/${item.key}`;
    btn.className = 'btn secondary';
    btn.textContent = item.label;
    downloadPublic.appendChild(btn);
  });
  
  if (downloadPublic.children.length === 0) {
    const msg = document.createElement('p');
    msg.textContent = 'Data exports being prepared...';
    msg.style.color = '#999';
    msg.style.fontSize = '0.9rem';
    downloadPublic.appendChild(msg);
  }
}

function buildChartDownloads() {
  if (!chartDownloads) {
    console.error('Chart downloads container not found');
    return;
  }
  
  chartDownloads.innerHTML = '';
  
  // HTML format export
  const htmlButton = document.createElement('button');
  htmlButton.className = 'btn secondary';
  htmlButton.type = 'button';
  htmlButton.textContent = '📊 Download All Charts (HD)'; 
  htmlButton.style.fontWeight = 'bold';
  htmlButton.style.gridColumn = 'span 2';
  
  htmlButton.addEventListener('click', async () => {
    htmlButton.disabled = true;
    const originalText = htmlButton.textContent;
    htmlButton.textContent = 'Rendering HD export...';
    
    try {
      const chartsToExport = [
        { id: 'chart-timeseries', label: 'PM2.5 Temporal Trend' },
        { id: 'chart-distribution', label: 'PM2.5 Concentration Distribution' },
        { id: 'chart-hourly', label: 'Diurnal Cycle' },
        { id: 'chart-channel', label: 'Dual Sensor Comparison' },
        { id: 'chart-heatmap', label: 'Rolling Medians' },
        { id: 'chart-humidity', label: 'Sensor Drift' },
      ];
      
      let htmlContent = `
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>Air Quality Analysis Charts</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
    .page { page-break-after: always; background: white; padding: 20px; margin: 10px 0; border-radius: 8px; }
    h1 { color: #1f1c16; margin-bottom: 10px; }
    .chart-container { margin: 30px 0; text-align: center; }
    img { max-width: 100%; height: auto; border-radius: 4px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
  </style>
</head>
<body>
  <h1>Air Quality Analysis - High Resolution Charts</h1>
  <p>Generated: ${new Date().toLocaleString()}</p>
`;
      
      for (const chart of chartsToExport) {
        const element = document.getElementById(chart.id);
        if (element && element.data && element.data.length > 0) {
          try {
            const dataUrl = await exportChartHD(element);
            const base64Data = dataUrl.split(',')[1];
            htmlContent += `
  <div class="page">
    <div class="chart-container">
      <h2>${chart.label}</h2>
      <img src="data:image/png;base64,${base64Data}" alt="${chart.label}">
    </div>
  </div>
`;
          } catch (err) {
            console.error(`Failed to export ${chart.label}:`, err);
          }
        }
      }
      
      htmlContent += `</body></html>`;
      
      const blob = new Blob([htmlContent], { type: 'text/html' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `Air-Quality-Charts-HD-${new Date().toISOString().split('T')[0]}.html`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (error) {
      console.error('Export failed:', error);
      alert('Failed to generate HD export: ' + error.message);
    } finally {
      htmlButton.disabled = false;
      htmlButton.textContent = originalText;
    }
  });
  
  chartDownloads.appendChild(htmlButton);
}

function buildAnomalies(anomalies) {
  if (!anomalyList) {
    console.error('Anomaly list container not found');
    return;
  }
  
  anomalyList.innerHTML = '';
  if (!anomalies || anomalies.length === 0) {
    anomalyList.innerHTML = '<li>No major anomalies detected.</li>';
    return;
  }
  anomalies.forEach((note) => {
    const li = document.createElement('li');
    li.textContent = note;
    anomalyList.appendChild(li);
  });
}

function populateChartDescriptions(chartDescriptions, dateRange) {
  if (!chartDescriptions) return;
  
  // Map of chart IDs to description keys
  const chartMappings = {
    'timeseries': 'timeseries',
    'hourly': 'hourly_pattern',
    'heatmap': 'rolling_median',
    'channel': 'channel_comparison',
    'humidity': 'sensor_drift',
  };
  
  // Populate each chart with description
  Object.entries(chartMappings).forEach(([chartId, descKey]) => {
    const desc = chartDescriptions[descKey];
    if (!desc) return;
    
    // Set simple terms explanation
    const descElement = document.getElementById(`desc-${chartId}`);
    if (descElement) {
      descElement.innerHTML = `
        <div class="chart-description">
          <strong style="display: block; color: #1f7a8c; margin-bottom: 6px;">📖 In Simple Terms:</strong>
          <p>${desc.explanation}</p>
          <div style="margin-top: 10px; padding-top: 10px; border-top: 1px solid #e0e0e0;">
            <strong style="color: #1f7a8c; font-size: 0.85rem;">What to look for:</strong>
            <p style="margin: 4px 0 0 0; font-size: 0.8rem; color: #666;">${desc.look_for}</p>
          </div>
        </div>
      `;
    }
    
    // Set tooltip for info icon
    const tooltipElement = document.getElementById(`tooltip-${chartId}`);
    if (tooltipElement) {
      tooltipElement.innerHTML = `
        <strong>${desc.title}</strong>
        <p>${desc.explanation}</p>
        <p><em style="color: #1f7a8c;">💡 ${desc.look_for}</em></p>
      `;
    }
    
    // Set date range if available
    const dateRangeElement = document.getElementById(`range-${chartId}`);
    if (dateRangeElement && dateRange) {
      const startDate = new Date(dateRange.start).toLocaleDateString();
      const endDate = new Date(dateRange.end).toLocaleDateString();
      dateRangeElement.innerHTML = `📅 ${startDate} to ${endDate}`;
    }
  });
  
  // Setup info icon click handlers
  const infoButtons = document.querySelectorAll('.btn-icon.info-icon');
  infoButtons.forEach(btn => {
    btn.addEventListener('click', (e) => {
      const chartName = btn.dataset.chart;
      const tooltip = document.getElementById(`tooltip-${chartName}`);
      if (tooltip) {
        tooltip.style.display = tooltip.style.display === 'none' ? 'block' : 'none';
      }
    });
  });
}

function renderCharts(charts) {
  try {
    // Validate charts object
    if (!charts) {
      console.error('Charts data is missing');
      throw new Error('No charts data provided');
    }

    console.log('Rendering charts with data:', Object.keys(charts));

    // Chart container validation (chart-map excluded — removed as always-empty placeholder)
    const chartContainers = [
      'chart-timeseries', 'chart-distribution', 'chart-hourly', 'chart-heatmap',
      'chart-channel', 'chart-humidity'
    ];
    const missingCharts = chartContainers.filter(id => !document.getElementById(id));
    if (missingCharts.length > 0) {
      console.error('Missing chart containers:', missingCharts);
      return;
    }

    // Validate timeseries data
    if (!charts.timeseries || !charts.timeseries.timestamps || !charts.timeseries.pm25) {
      console.error('Missing timeseries data. Available keys:', Object.keys(charts.timeseries || {}));
      throw new Error('Incomplete timeseries data');
    }

    console.log('Timeseries data valid. Points:', charts.timeseries.timestamps.length);

    // Chart 1: Time series
    console.log('Rendering timeseries...');
    Plotly.newPlot(
      'chart-timeseries',
      [
        {
          x: charts.timeseries.timestamps,
          y: charts.timeseries.pm25,
          mode: 'lines',
          name: 'Observed PM2.5',
          line: { color: '#1f7a8c', width: 1.5 },
        },
        {
          x: charts.timeseries.timestamps,
          y: charts.timeseries.pm25_corrected,
          mode: 'lines',
          name: 'EPA-Corrected',
          line: { color: '#f25c54', width: 1.5 },
        },
        {
          x: charts.timeseries.timestamps,
          y: Array(charts.timeseries.timestamps.length).fill(charts.timeseries.who_line),
          mode: 'lines',
          name: 'WHO Guideline (15)',
          line: { color: '#00a651', width: 1, dash: 'dash' },
          hovertemplate: 'WHO: 15 µg/m³<extra></extra>',
        },
        {
          x: charts.timeseries.timestamps,
          y: Array(charts.timeseries.timestamps.length).fill(charts.timeseries.epa_line),
          mode: 'lines',
          name: 'EPA Standard (35)',
          line: { color: '#f6aa1c', width: 1, dash: 'dot' },
          hovertemplate: 'EPA: 35 µg/m³<extra></extra>',
        },
      ],
      {
        ...chartLayouts.base,
        title: 'PM2.5 Temporal Trend',
        xaxis: { title: _tzDateAxis(), ...chartLayouts.base.xaxis },
        yaxis: { title: 'PM2.5 Concentration (µg/m³)', ...chartLayouts.base.yaxis },
      },
      chartConfig
    );
    console.log('✓ Timeseries rendered');

    // Chart 2: PM2.5 Concentration Distribution (exposure histogram)
    console.log('Rendering PM2.5 distribution...');
    {
      const vals = (charts.timeseries.pm25_corrected || charts.timeseries.pm25 || [])
        .filter(v => v !== null && v !== undefined && isFinite(v) && v >= 0);
      const distEl = document.getElementById('chart-distribution');
      if (vals.length > 0 && distEl) {
        const pctBelowWho = (100 * vals.filter(v => v <= 15).length / vals.length).toFixed(1);
        const pctBelowEpa = (100 * vals.filter(v => v <= 35).length / vals.length).toFixed(1);
        Plotly.newPlot(
          'chart-distribution',
          [
            {
              x: vals,
              type: 'histogram',
              histnorm: 'percent',
              marker: { color: '#1f7a8c', line: { color: '#fff', width: 0.5 } },
              opacity: 0.85,
              name: '% of readings',
              hovertemplate: 'PM2.5 %{x} µg/m³<br>%{y:.1f}% of readings<extra></extra>',
            },
          ],
          {
            ...chartLayouts.base,
            title: `PM2.5 Concentration Distribution — ${pctBelowWho}% ≤ WHO 15, ${pctBelowEpa}% ≤ EPA 35`,
            bargap: 0.02,
            xaxis: { title: 'PM2.5 (µg/m³, EPA-corrected)', ...chartLayouts.base.xaxis },
            yaxis: { title: '% of readings', ...chartLayouts.base.yaxis },
            shapes: [
              { type: 'line', x0: 15, x1: 15, yref: 'paper', y0: 0, y1: 1, line: { color: '#00a651', width: 1.5, dash: 'dash' } },
              { type: 'line', x0: 35, x1: 35, yref: 'paper', y0: 0, y1: 1, line: { color: '#f6aa1c', width: 1.5, dash: 'dot' } },
            ],
            annotations: [
              { x: 15, yref: 'paper', y: 1.02, text: 'WHO 15', showarrow: false, font: { size: 10, color: '#00a651' } },
              { x: 35, yref: 'paper', y: 1.02, text: 'EPA 35', showarrow: false, font: { size: 10, color: '#f6aa1c' } },
            ],
          },
          chartConfig
        );
      } else if (distEl) {
        distEl.innerHTML = '<p class="empty">No PM2.5 data available for distribution.</p>';
      }
    }
    console.log('✓ Distribution rendered');

    // Chart 3: Diurnal Cycle — mean line with 10th–90th percentile band
    console.log('Rendering diurnal cycle...');
    {
      const dp = charts.diurnal_pattern;
      const hourlyEl = document.getElementById('chart-hourly');
      if (dp && dp.hours && dp.mean && hourlyEl) {
        const hrs = dp.hours;
        const traces = [];
        // Percentile band (p90 upper, p10 lower) drawn as a filled area
        if (dp.p90 && dp.p10) {
          traces.push({
            x: hrs, y: dp.p90, mode: 'lines', line: { width: 0 },
            name: '90th pct', hoverinfo: 'skip', showlegend: false,
          });
          traces.push({
            x: hrs, y: dp.p10, mode: 'lines', line: { width: 0 },
            fill: 'tonexty', fillcolor: 'rgba(31,122,140,0.15)',
            name: '10th–90th percentile', hoverinfo: 'skip',
          });
        }
        traces.push({
          x: hrs, y: dp.mean, mode: 'lines+markers',
          line: { color: '#1f7a8c', width: 2.2 },
          marker: { size: 4, color: '#1f7a8c' },
          name: 'Mean PM2.5',
          hovertemplate: `Hour %{x}:00 ${_tzHourTag()}<br>Mean: %{y:.1f} µg/m³<extra></extra>`,
        });
        Plotly.newPlot(
          'chart-hourly',
          traces,
          {
            ...chartLayouts.base,
            title: 'Diurnal Cycle — Typical PM2.5 by Hour of Day',
            xaxis: { title: _tzHourAxis(), dtick: 3, ...chartLayouts.base.xaxis },
            yaxis: { title: 'PM2.5 (µg/m³)', rangemode: 'tozero', ...chartLayouts.base.yaxis },
            shapes: [
              { type: 'line', xref: 'paper', x0: 0, x1: 1, y0: 15, y1: 15, line: { color: '#00a651', width: 1, dash: 'dash' } },
            ],
            annotations: [
              { xref: 'paper', x: 1, y: 15, text: 'WHO 15', showarrow: false, xanchor: 'left', font: { size: 10, color: '#00a651' } },
            ],
          },
          chartConfig
        );
      } else if (hourlyEl) {
        hourlyEl.innerHTML = '<p class="empty">No diurnal pattern data available.</p>';
      }
    }
    console.log('✓ Diurnal cycle rendered');

    // Chart 4: Rolling Medians (1h + 24h + 7d)
    console.log('Rendering rolling medians...');
    if (charts.rolling_median && charts.rolling_median.timestamps && charts.rolling_median.pm25) {
      const rmTraces = [
        {
          x: charts.rolling_median.timestamps,
          y: charts.rolling_median.pm25,
          mode: 'lines',
          name: 'Hourly PM2.5',
          line: { color: 'rgba(31, 122, 140, 0.18)', width: 0.6 },
          hovertemplate: '%{x}<br>Hourly: %{y:.1f} µg/m³<extra></extra>',
        },
      ];
      // 1-hour rolling median: fine-grained smoothing over sub-hourly noise
      if (charts.rolling_median.median_1h && charts.rolling_median.median_1h_timestamps) {
        rmTraces.push({
          x: charts.rolling_median.median_1h_timestamps,
          y: charts.rolling_median.median_1h,
          mode: 'lines',
          name: '1h Median',
          line: { color: '#4c956c', width: 1.0 },
          hovertemplate: '%{x}<br>1h Median: %{y:.1f} µg/m³<extra></extra>',
        });
      }
      rmTraces.push(
        {
          x: charts.rolling_median.timestamps,
          y: charts.rolling_median.median_24h,
          mode: 'lines',
          name: '24h Median',
          line: { color: '#f25c54', width: 1.5 },
          hovertemplate: '%{x}<br>24h: %{y:.1f} µg/m³<extra></extra>',
        },
        {
          x: charts.rolling_median.timestamps,
          y: charts.rolling_median.median_7d,
          mode: 'lines',
          name: '7d Median',
          line: { color: '#f6aa1c', width: 2.0, dash: 'dash' },
          hovertemplate: '%{x}<br>7d: %{y:.1f} µg/m³<extra></extra>',
        }
      );
      Plotly.newPlot(
        'chart-heatmap',
        rmTraces,
        {
          ...chartLayouts.base,
          title: 'Rolling Medians — 1h · 24h · 7d Smoothed Trend',
          xaxis: { title: _tzDateAxis(), ...chartLayouts.base.xaxis },
          yaxis: { title: 'PM2.5 Concentration (µg/m³)', ...chartLayouts.base.yaxis },
        },
        chartConfig
      );
    } else {
      document.getElementById('chart-heatmap').innerHTML = '<p class="empty">Rolling median analysis available in research report</p>';
    }
    console.log('✓ Rolling medians rendered');

    // Chart 5: Channel scatter (using channel_series data)
    console.log('Rendering channel comparison...');
    if (charts.channel_series && charts.channel_series.a && charts.channel_series.b) {
      const r2 = charts.channel_series.r2;
      const title = r2 ? `Dual Sensor Comparison (R2 = ${r2})` : 'Dual Sensor Comparison';
      
      Plotly.newPlot(
        'chart-channel',
        [
          {
            x: charts.channel_series.a,
            y: charts.channel_series.b,
            mode: 'markers',
            marker: { color: '#4c956c', size: 4, opacity: 0.6, line: { width: 0 } },
            name: 'Paired Measurements',
            hovertemplate: 'Channel A: %{x:.1f}<br>Channel B: %{y:.1f}<extra></extra>',
          },
        ],
        {
          ...chartLayouts.base,
          title: title,
          xaxis: { title: 'Channel A PM2.5 (µg/m³)', ...chartLayouts.base.xaxis },
          yaxis: { title: 'Channel B PM2.5 (µg/m³)', ...chartLayouts.base.yaxis },
        },
        chartConfig
      );
    } else {
      document.getElementById('chart-channel').innerHTML = '<p class="empty">No dual-channel data available</p>';
    }
    console.log('✓ Channel comparison rendered');

    // Chart 6: Sensor Drift Detection
    console.log('Rendering sensor drift...');
    if (charts.sensor_drift && charts.sensor_drift.timestamps && charts.sensor_drift.diff) {
      Plotly.newPlot(
        'chart-humidity',
        [
          {
            x: charts.sensor_drift.timestamps,
            y: charts.sensor_drift.diff,
            mode: 'markers',
            marker: { color: '#4c956c', size: 4, opacity: 0.5 },
            name: 'Channel Difference',
            hovertemplate: '%{x}<br>A - B: %{y:.2f} µg/m³<extra></extra>',
          },
          {
            x: charts.sensor_drift.timestamps,
            y: charts.sensor_drift.rolling,
            mode: 'lines',
            line: { color: '#f25c54', width: 2 },
            name: '7d Rolling Median',
            hovertemplate: '%{x}<br>Drift: %{y:.2f} µg/m³<extra></extra>',
          },
        ],
        {
          ...chartLayouts.base,
          title: 'Sensor Drift Detection (Channel A - Channel B)',
          xaxis: { title: _tzDateAxis(), ...chartLayouts.base.xaxis },
          yaxis: { title: 'Difference (µg/m³)', ...chartLayouts.base.yaxis },
        },
        chartConfig
      );
    } else {
      document.getElementById('chart-humidity').innerHTML = '<p class="empty">Sensor drift analysis available in research report</p>';
    }
    console.log('✓ Sensor drift rendered');

    // Calendar — daily-mean PM2.5, coloured by WHO/EPA thresholds (computed client-side)
    if (charts.calendar && charts.calendar.days && charts.calendar.days.length > 0) {
      const dailyPm = _dailyMeanPm25(charts.timeseries);
      renderPM25CalendarSVG(charts.calendar.days, dailyPm);
    } else {
      const el = document.getElementById('chart-calendar');
      if (el) el.innerHTML = '<p class="empty" style="padding:2rem;text-align:center;color:#999;">Calendar requires at least 7 days of data.</p>';
    }

    // Correction comparison chart
    if (charts.correction_comparison && charts.correction_comparison.timestamps) {
      const cc = charts.correction_comparison;
      Plotly.newPlot('chart-correction', [
        { x: cc.timestamps, y: cc.barkjohn, name: 'EPA Barkjohn', line: { color: '#f25c54', width: 1.2 }, type: 'scatter', mode: 'lines' },
        { x: cc.timestamps, y: cc.lrapa,    name: 'LRAPA',        line: { color: '#1f7a8c', width: 1.2 }, type: 'scatter', mode: 'lines' },
        { x: cc.timestamps, y: cc.aqu,      name: 'AQ&U',         line: { color: '#f6aa1c', width: 1.2 }, type: 'scatter', mode: 'lines' },
      ], {
        ...chartLayouts.base,
        title: 'PM2.5 by Correction Formula',
        xaxis: { title: 'Date' },
        yaxis: { title: 'PM2.5 (µg/m³)' },
        legend: { orientation: 'h', y: -0.2 },
      }, chartConfig);
    }

    // Force Plotly to fill containers correctly after all charts are rendered
    setTimeout(() => {
      ['chart-timeseries','chart-distribution','chart-hourly','chart-heatmap',
       'chart-channel','chart-humidity','chart-correction'].forEach(id => {
        const el = document.getElementById(id);
        if (el && el.data) Plotly.Plots.resize(el);
      });
    }, 100);

  } catch (error) {
    console.error('Chart rendering failed:', error);
    console.error('Error stack:', error.stack);
    
    // Try to identify which chart failed
    const chartIds = ['chart-timeseries', 'chart-distribution', 'chart-hourly', 'chart-heatmap', 'chart-channel', 'chart-humidity'];
    chartIds.forEach(id => {
      const el = document.getElementById(id);
      if (el && !el.innerHTML) {
        el.innerHTML = `<p class="empty">Error rendering ${id}. Check console.</p>`;
      }
    });
    
    alert(`Failed to render charts: ${error.message}. Check browser console (F12) for full details.`);
  }
}

function setupChartResize() {
  const chartIds = [
    'chart-timeseries',
    'chart-distribution',
    'chart-hourly',
    'chart-heatmap',
    'chart-channel',
    'chart-humidity',
    'chart-compare-timeseries',
  ];

  window.addEventListener('resize', () => {
    chartIds.forEach((id) => {
      const element = document.getElementById(id);
      if (element && element.data) {
        Plotly.Plots.resize(element);
      }
    });
  });
}

function _safeNumber(value) {
  if (value === null || value === undefined) return null;
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
}

function _formatMaybeNumber(value, decimals = 1) {
  const num = _safeNumber(value);
  if (num === null) return 'N/A';
  return num.toFixed(decimals);
}

function _formatDelta(delta, decimals = 1) {
  const num = _safeNumber(delta);
  if (num === null) return '';
  const sign = num > 0 ? '+' : '';
  return `${sign}${num.toFixed(decimals)}`;
}

function _findBestWorst(analyses, key, mode) {
  // mode: 'min' | 'max'
  const values = analyses
    .map((a, idx) => ({ idx, filename: a.filename || a.file_id, value: _safeNumber(a.summary?.[key]) }))
    .filter((x) => x.value !== null);
  if (!values.length) return null;
  values.sort((a, b) => (mode === 'min' ? a.value - b.value : b.value - a.value));
  return { best: values[0], worst: values[values.length - 1] };
}

function buildComparison(analyses) {
  if (!comparisonSection || !comparisonTable || !comparisonResults) return;
  comparisonResults.classList.remove('hidden');

  const baseline = analyses[0]?.summary || {};

  const rows = analyses.map((a, index) => {
    const s = a.summary || {};
    const start = s.date_range?.start ? String(s.date_range.start).split('T')[0] : 'N/A';
    const end = s.date_range?.end ? String(s.date_range.end).split('T')[0] : 'N/A';

    const basePmCorr = _safeNumber(baseline.pm25_average_epa_corrected ?? baseline.pm25_average);
    const thisPmCorr = _safeNumber(s.pm25_average_epa_corrected ?? s.pm25_average);
    const baseAqi = _safeNumber(baseline.aqi_average);
    const thisAqi = _safeNumber(s.aqi_average);
    const baseQs = _safeNumber(baseline.quality_score);
    const thisQs = _safeNumber(s.quality_score);

    // Difference-in-differences excess vs the control (statistically quantified).
    let didCell = index === 0 ? 'Control' : '—';
    const did = a.did;
    if (index !== 0 && did && did.excess_pct !== null && did.excess_pct !== undefined) {
      const sign = did.excess_pct > 0 ? '+' : '';
      const ci = did.excess_ci_pct ? ` [${did.excess_ci_pct[0]}, ${did.excess_ci_pct[1]}]` : '';
      const sig = did.significant ? `, p=${did.p_value}` : ` (n.s., p=${did.p_value})`;
      didCell = `${sign}${did.excess_pct}%${ci}${sig}`;
    }

    return {
      File: a.filename || a.file_id,
      'Start': start,
      'End': end,
      'Avg PM2.5 (EPA corr.)': _formatMaybeNumber(thisPmCorr, 1),
      'Δ PM2.5 vs File 1': index === 0 ? '' : _formatDelta(thisPmCorr !== null && basePmCorr !== null ? thisPmCorr - basePmCorr : null, 1),
      'Excess vs Control (95% CI)': didCell,
      'Avg AQI': _formatMaybeNumber(thisAqi, 0),
      'Δ AQI vs File 1': index === 0 ? '' : _formatDelta(thisAqi !== null && baseAqi !== null ? thisAqi - baseAqi : null, 0),
      'Quality Score (%)': _formatMaybeNumber(thisQs, 0),
      'Δ Quality vs File 1': index === 0 ? '' : _formatDelta(thisQs !== null && baseQs !== null ? thisQs - baseQs : null, 0),
    };
  });

  const bestWorstPm = _findBestWorst(analyses, 'pm25_average_epa_corrected', 'min') || _findBestWorst(analyses, 'pm25_average', 'min');
  const bestWorstQs = _findBestWorst(analyses, 'quality_score', 'max');

  const highlights = [];
  if (bestWorstPm) highlights.push(`Lowest avg PM2.5: ${bestWorstPm.best.filename} (${bestWorstPm.best.value.toFixed(1)} µg/m³)`);
  if (bestWorstPm) highlights.push(`Highest avg PM2.5: ${bestWorstPm.worst.filename} (${bestWorstPm.worst.value.toFixed(1)} µg/m³)`);
  if (bestWorstQs) highlights.push(`Best quality score: ${bestWorstQs.best.filename} (${bestWorstQs.best.value.toFixed(0)}%)`);
  if (bestWorstQs) highlights.push(`Worst quality score: ${bestWorstQs.worst.filename} (${bestWorstQs.worst.value.toFixed(0)}%)`);

  comparisonTable.innerHTML = `${highlights.length ? `<p class="group-note">${highlights.join(' · ')}</p>` : ''}${buildTable(rows)}`;

  // Determine overlap window (informational only)
  const starts = analyses
    .map((a) => a.summary?.date_range?.start)
    .filter(Boolean)
    .map((x) => new Date(x));
  const ends = analyses
    .map((a) => a.summary?.date_range?.end)
    .filter(Boolean)
    .map((x) => new Date(x));

  if (comparisonMeta) {
    if (starts.length && ends.length) {
      const overlapStart = new Date(Math.max(...starts.map((d) => d.getTime())));
      const overlapEnd = new Date(Math.min(...ends.map((d) => d.getTime())));
      if (overlapStart.getTime() <= overlapEnd.getTime()) {
        comparisonMeta.textContent = `Overlap window (all files): ${overlapStart.toLocaleDateString()} to ${overlapEnd.toLocaleDateString()}`;
      } else {
        comparisonMeta.textContent = 'No overlapping time window across all files (comparisons are still valid but cover different periods).';
      }
    } else {
      comparisonMeta.textContent = '';
    }
  }

  // Overlay timeseries
  const traces = analyses.map((a, idx) => {
    const ts = a.timeseries || {};
    const x = ts.timestamps || [];
    const y = ts.pm25_corrected || ts.pm25 || [];
    return {
      x,
      y,
      type: 'scatter',
      mode: 'lines',
      name: a.filename || `File ${idx + 1}`,
      line: { width: 2 },
    };
  });

  const firstTs = analyses[0]?.timeseries || {};
  const whoLine = _safeNumber(firstTs.who_line) ?? 15;
  const epaLine = _safeNumber(firstTs.epa_line) ?? 35;

  const layout = {
    ...chartLayouts.base,
    title: 'PM2.5 Trend Comparison (EPA-corrected where available)',
    xaxis: { ...chartLayouts.base.xaxis, title: 'Time' },
    yaxis: { ...chartLayouts.base.yaxis, title: 'PM2.5 (µg/m³)' },
    hovermode: 'x unified',
    shapes: [
      {
        type: 'line',
        xref: 'paper',
        x0: 0,
        x1: 1,
        yref: 'y',
        y0: whoLine,
        y1: whoLine,
        line: { color: '#00a651', width: 1, dash: 'dot' },
      },
      {
        type: 'line',
        xref: 'paper',
        x0: 0,
        x1: 1,
        yref: 'y',
        y0: epaLine,
        y1: epaLine,
        line: { color: '#ff4444', width: 1, dash: 'dot' },
      },
    ],
    annotations: [
      {
        xref: 'paper',
        x: 1,
        yref: 'y',
        y: whoLine,
        text: `WHO ${whoLine}`,
        showarrow: false,
        xanchor: 'left',
        font: { size: 10, color: '#00a651' },
      },
      {
        xref: 'paper',
        x: 1,
        yref: 'y',
        y: epaLine,
        text: `EPA ${epaLine}`,
        showarrow: false,
        xanchor: 'left',
        font: { size: 10, color: '#ff4444' },
      },
    ],
  };

  Plotly.newPlot('chart-compare-timeseries', traces, layout, chartConfig);

  // 24h + 1h rolling-median overlays with Control House highlighted
  buildMedianComparisonCharts(analyses);

  // Multi-year overlay: only show if files span different years
  const yearOverlayContainer = document.getElementById('year-overlay-container');
  if (analyses && yearOverlayContainer) {
    const yearGroups = {};
    analyses.forEach(a => {
      if (a.daily_doy) {
        a.daily_doy.forEach(d => {
          if (!yearGroups[d.year]) yearGroups[d.year] = [];
          yearGroups[d.year].push(d);
        });
      }
    });
    const years = Object.keys(yearGroups).sort();
    if (years.length >= 2) {
      yearOverlayContainer.classList.remove('hidden');
      const colors = ['#1f7a8c','#f25c54','#f6aa1c','#4c956c','#8F3F97','#FF7E00'];
      const overlayTraces = years.map((yr, i) => {
        const pts = yearGroups[yr].filter(d => d.pm25 !== null).sort((a,b) => a.doy - b.doy);
        return {
          x: pts.map(d => d.doy),
          y: pts.map(d => d.pm25),
          name: String(yr),
          mode: 'lines',
          line: { color: colors[i % colors.length], width: 1.8 },
          type: 'scatter',
        };
      });
      Plotly.newPlot('chart-year-overlay', overlayTraces, {
        ...chartLayouts.base,
        title: 'Year-over-Year PM2.5 (EPA Corrected)',
        xaxis: { title: 'Day of Year', range: [1, 366] },
        yaxis: { title: 'PM2.5 (µg/m³)' },
        legend: { orientation: 'h', y: -0.2 },
      }, chartConfig);
    } else {
      yearOverlayContainer.classList.add('hidden');
    }
  }
}

// Distinct, color-blind-friendly palette for non-control houses
const HOUSE_PALETTE = ['#1f7a8c', '#e07a5f', '#f6aa1c', '#4c956c', '#8f3f97', '#ff7e00', '#2a9d8f', '#c1121f', '#6a4c93'];

function _medianTraces(analyses, kind) {
  // kind: '24h' uses rolling_median.timestamps + median_24h
  //       '1h'  uses rolling_median.median_1h_timestamps + median_1h
  let houseColorIdx = 0;
  const traces = [];
  analyses.forEach((a) => {
    const rm = a.rolling_median || {};
    let x, y;
    if (kind === '24h') {
      x = rm.timestamps || [];
      y = rm.median_24h || [];
    } else {
      x = rm.median_1h_timestamps || [];
      y = rm.median_1h || [];
    }
    if (!x.length || !y.length) return;
    const name = a.label || a.filename || 'House';
    if (a.is_control) {
      traces.push({
        x, y, type: 'scatter', mode: 'lines',
        name: `${name} (Control)`,
        line: { color: '#0a1f47', width: 3.2 },
        hovertemplate: `<b>${name}</b><br>%{x}<br>%{y:.1f} µg/m³<extra></extra>`,
      });
    } else {
      const color = HOUSE_PALETTE[houseColorIdx % HOUSE_PALETTE.length];
      houseColorIdx += 1;
      traces.push({
        x, y, type: 'scatter', mode: 'lines',
        name,
        line: { color, width: 1.6 },
        opacity: 0.92,
        hovertemplate: `<b>${name}</b><br>%{x}<br>%{y:.1f} µg/m³<extra></extra>`,
      });
    }
  });
  // Draw control last so its bold line sits on top
  traces.sort((t1, t2) => (/(Control)/.test(t1.name) ? 1 : 0) - (/(Control)/.test(t2.name) ? 1 : 0));
  return traces;
}

function _medianLayout(title) {
  return {
    ...chartLayouts.base,
    title,
    xaxis: { ...chartLayouts.base.xaxis, title: 'Time' },
    yaxis: { ...chartLayouts.base.yaxis, title: 'PM2.5 (µg/m³)' },
    hovermode: 'x unified',
    shapes: [
      { type: 'line', xref: 'paper', x0: 0, x1: 1, yref: 'y', y0: 15, y1: 15, line: { color: '#00a651', width: 1, dash: 'dot' } },
      { type: 'line', xref: 'paper', x0: 0, x1: 1, yref: 'y', y0: 35, y1: 35, line: { color: '#ff4444', width: 1, dash: 'dot' } },
    ],
    annotations: [
      { xref: 'paper', x: 1, yref: 'y', y: 15, text: 'WHO 15', showarrow: false, xanchor: 'left', font: { size: 10, color: '#00a651' } },
      { xref: 'paper', x: 1, yref: 'y', y: 35, text: 'EPA 35', showarrow: false, xanchor: 'left', font: { size: 10, color: '#ff4444' } },
    ],
  };
}

function buildMedianComparisonCharts(analyses) {
  const c24 = document.getElementById('compare-24h-container');
  const c1h = document.getElementById('compare-1h-container');

  const t24 = _medianTraces(analyses, '24h');
  if (c24) {
    if (t24.length) {
      c24.classList.remove('hidden');
      Plotly.newPlot('chart-compare-median-24h', t24,
        _medianLayout('24-Hour Median PM2.5 — Control House vs Others'), chartConfig);
    } else {
      c24.classList.add('hidden');
    }
  }

  const t1h = _medianTraces(analyses, '1h');
  if (c1h) {
    if (t1h.length) {
      c1h.classList.remove('hidden');
      Plotly.newPlot('chart-compare-median-1h', t1h,
        _medianLayout('1-Hour Median PM2.5 — Control House vs Others'), chartConfig);
    } else {
      c1h.classList.add('hidden');
    }
  }
}

async function handleSingleFile(file) {
  if (!file) return;
  setLoadingText('Analyzing data and building your dashboard...');
  showLoading(true);
  const formData = new FormData();
  formData.append('file', file);
  if (deviceIdInput && deviceIdInput.value.trim()) {
    formData.append('device_id', deviceIdInput.value.trim());
  }
  if (locationInput && locationInput.value.trim()) {
    formData.append('location', locationInput.value.trim());
  }
  const tzSelect = document.getElementById('timezone-select');
  formData.append('timezone', tzSelect ? tzSelect.value : 'UTC');

  try {
    const response = await fetch('/api/analyze', { method: 'POST', body: formData });
    if (!response.ok) {
      showLoading(false);
      let errMsg = 'Upload failed. Please check your file and try again.';
      try {
        const errData = await response.json();
        if (errData.detail) errMsg = 'Error: ' + errData.detail;
      } catch (_) {}
      alert(errMsg);
      return;
    }
    const payload = await response.json();
    const { result, outputs, file_id } = payload;
    currentFileId = file_id;
    initCustomNotes();  // resets customNotes=[] and clears the note cards list

    currentTzLabel = (result.summary && result.summary.tz_label) || 'UTC';
    buildOverviewCards(result.summary);
    buildDataCompleteness(result.data_completeness);
    buildDetectedColumns(result.detected);
    renderCharts(result.charts);
    populateChartDescriptions(result.chart_descriptions, result.summary.date_range);
    buildChartDownloads();
    buildTables(result.tables);
    buildDownloads(file_id, outputs);
    buildAnomalies(result.anomalies);
    setupHDDownloads(file_id);

    // Narrative summary
    if (result.summary && result.summary.narrative_summary && narrativeText && narrativeSummary) {
      narrativeText.textContent = result.summary.narrative_summary;
      narrativeSummary.classList.remove('hidden');
    }
    showLoading(false);
    showDashboard();

    if (comparisonSection) comparisonSection.classList.remove('hidden');
    if (comparisonResults) comparisonResults.classList.add('hidden');
    // Reset comparison state for new analysis
    compareFiles = [];
    compareFileIds = [];
    _updateCompareUI();
    
    // Setup timeframe selector with the current file_id and data range
    setupTimeframeSelector(file_id, result.summary);
  } catch (error) {
    console.error('Error processing file:', error);
    showLoading(false);
    alert(`Error processing file: ${error.message}`);
  }
}

async function handleCompareRun() {
  if (compareFiles.length < 2) {
    alert('Add a Control House and at least one other house to compare.');
    return;
  }
  if (compareFiles.length > 10) {
    alert('Maximum 10 houses allowed for comparison.');
    return;
  }
  if (_controlIndex() < 0) {
    alert('Please designate a Control House before comparing.');
    return;
  }

  // Reorder so the Control House is always first (index 0 = control_index)
  const ctlIdx = _controlIndex();
  const ordered = [compareFiles[ctlIdx], ...compareFiles.filter((_, i) => i !== ctlIdx)];

  const formData = new FormData();
  ordered.forEach((entry) => formData.append('files', entry.file));
  formData.append('labels', JSON.stringify(ordered.map(e => e.label)));
  formData.append('control_index', '0');

  setLoadingText(`Comparing ${compareFiles.length} houses… This may take a moment for large datasets.`);
  showLoading(true);
  if (compareRunButton) compareRunButton.disabled = true;

  const controller = new AbortController();
  const timeoutMs = 10 * 60 * 1000; // 10 minutes
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch('/api/analyze-multi', {
      method: 'POST',
      body: formData,
      signal: controller.signal,
    });
    if (!response.ok) {
      showLoading(false);
      alert('Compare failed. Please check your files and try again.');
      return;
    }

    const payload = await response.json();
    const analyses = payload.analyses || [];
    if (analyses.length < 2) {
      showLoading(false);
      alert('Comparison requires at least 2 valid analyses.');
      return;
    }

    // Capture file_ids + labels (control-first order) for comparison report download
    compareFileIds = analyses.map(a => a.file_id).filter(Boolean);
    compareLabels  = analyses.map(a => a.label || a.filename || '');
    buildComparison(analyses);
    showLoading(false);
    showDashboard();
    comparisonSection?.scrollIntoView({ behavior: 'smooth' });
  } catch (error) {
    console.error('Error comparing files:', error);
    showLoading(false);
    if (error?.name === 'AbortError') {
      alert('Comparison is taking too long and was stopped. Try fewer files, a smaller date range, or CSV instead of XLSX.');
    } else {
      alert(`Error comparing files: ${error.message}`);
    }
  } finally {
    window.clearTimeout(timeoutId);
    if (compareRunButton) compareRunButton.disabled = false;
  }
}

async function exportChartHD(chartEl) {
  const isPolar = chartEl.data && chartEl.data.some(t => t.type === 'scatterpolar' || t.type === 'indicator');
  // Deep-clone the current layout so restore is always accurate
  const origLayout = JSON.parse(JSON.stringify(chartEl.layout || {}));

  // Axis titles can be stored as plain strings OR as {text, font} objects by Plotly internally
  const resolveTitle = (t) => {
    if (!t) return {};
    if (typeof t === 'string') return { text: t, font: { size: 15 } };
    return { ...t, font: { size: 15 } };
  };

  const exportPatch = isPolar
    ? { margin: { l: 90, r: 90, t: 110, b: 110 } }
    : {
        margin: { l: 130, r: 270, t: 90, b: 120 },
        legend: {
          x: 1.02, xanchor: 'left', y: 1, yanchor: 'top',
          bgcolor: 'rgba(255,255,255,0.97)',
          bordercolor: '#bbb', borderwidth: 1,
          font: { size: 13 },
        },
        xaxis: { ...(origLayout.xaxis || {}), title: resolveTitle(origLayout.xaxis?.title) },
        yaxis: { ...(origLayout.yaxis || {}), title: resolveTitle(origLayout.yaxis?.title) },
        font: { size: 14 },
      };

  await Plotly.relayout(chartEl, exportPatch);

  const dataUrl = await Plotly.toImage(chartEl, {
    format: 'png',
    width:  isPolar ? 1400 : 2800,
    height: isPolar ? 1400 : 1400,
    scale: 2,
  });

  // Restore exactly the original layout
  await Plotly.relayout(chartEl, origLayout);

  return dataUrl;
}

function setupHDDownloads(fileId) {
  const hdButtons = document.querySelectorAll('.btn-icon.download-hd');

  hdButtons.forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.preventDefault();
      const chartName = btn.dataset.chart;
      const chartElement = document.getElementById(`chart-${chartName}`);

      if (!chartElement || !chartElement.data) {
        alert(`Chart "${chartName}" is not ready. Run an analysis first.`);
        return;
      }

      btn.disabled = true;
      btn.style.opacity = '0.5';
      const origText = btn.querySelector('span')?.textContent || '';
      if (btn.querySelector('span')) btn.querySelector('span').textContent = '...';

      try {
        const dataUrl = await exportChartHD(chartElement);
        const link = document.createElement('a');
        link.href = dataUrl;
        link.download = `${chartName}-hd-${new Date().toISOString().split('T')[0]}.png`;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
      } catch (error) {
        console.error(`Failed to download HD ${chartName}:`, error);
        alert(`Failed to download chart: ${error.message}`);
      } finally {
        btn.disabled = false;
        btn.style.opacity = '1';
        if (btn.querySelector('span')) btn.querySelector('span').textContent = origText;
      }
    });
  });
}

function setupDropzone() {
  if (!dropzone || !fileInput) {
    console.error('Dropzone or file input not found');
    return;
  }

  dropzone.addEventListener('dragover', (event) => {
    event.preventDefault();
    dropzone.classList.add('dragging');
  });
  dropzone.addEventListener('dragleave', () => {
    dropzone.classList.remove('dragging');
  });
  dropzone.addEventListener('drop', (event) => {
    event.preventDefault();
    dropzone.classList.remove('dragging');
    const files = event.dataTransfer.files;
    if (files && files.length > 1) {
      alert('Please drop a single file to analyze. Use the Compare option after analysis to compare multiple files.');
    }
    handleSingleFile(files?.[0]);
  });
  fileInput.addEventListener('change', (event) => {
    const files = event.target.files;
    if (files && files.length > 1) {
      alert('Please select a single file to analyze. Use the Compare option after analysis to compare multiple files.');
    }
    handleSingleFile(files?.[0]);
  });
}

function _controlIndex() {
  return compareFiles.findIndex(e => e.isControl);
}

function _escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function _updateCompareUI() {
  const count = compareFiles.length;
  const hasControl = _controlIndex() >= 0;
  // House count = non-control entries
  const houseCount = count - (hasControl ? 1 : 0);

  if (compareSelection) {
    let msg = `${count} / 10 houses selected.`;
    if (!hasControl) msg += ' Add a Control House.';
    else if (houseCount < 1) msg += ' Add at least 1 more house to compare.';
    else msg += ' Ready to compare.';
    compareSelection.textContent = msg;
  }
  // Need a control + at least 1 other house (2 total)
  if (compareRunButton) compareRunButton.disabled = !(hasControl && houseCount >= 1);
  if (compareClearBtn) compareClearBtn.style.display = count > 0 ? '' : 'none';

  if (compareFileList) {
    const emptyNote = document.getElementById('compare-empty-note');
    Array.from(compareFileList.querySelectorAll('.compare-file-row')).forEach(el => el.remove());

    if (count === 0) {
      if (emptyNote) emptyNote.style.display = '';
    } else {
      if (emptyNote) emptyNote.style.display = 'none';
      compareFiles.forEach((entry, idx) => {
        const row = document.createElement('div');
        row.className = 'compare-file-row' + (entry.isControl ? ' is-control' : '');
        const badge = entry.isControl
          ? '<span class="compare-role-badge control">CONTROL</span>'
          : `<span class="compare-role-badge house">${_escapeHtml(entry.label)}</span>`;
        row.innerHTML = `
          ${badge}
          <input type="text" class="compare-name-input" data-idx="${idx}" value="${_escapeHtml(entry.label)}" maxlength="40" title="Display name for this house" />
          <span class="compare-file-name" title="${_escapeHtml(entry.file.name)}">${_escapeHtml(entry.file.name)}</span>
          <span class="compare-file-size">${(entry.file.size / 1024).toFixed(0)} KB</span>
          ${entry.isControl ? '' : `<button class="btn-set-control" data-idx="${idx}" title="Make this the Control House">Set as Control</button>`}
          <button class="btn-remove-file" data-idx="${idx}" title="Remove this house">✕</button>
        `;
        compareFileList.appendChild(row);
      });
    }
  }

  // Add-control button: only available if no control yet
  if (compareAddControlBtn) {
    const disable = hasControl || count >= 10;
    compareAddControlBtn.style.opacity = disable ? '0.5' : '1';
    compareAddControlBtn.style.pointerEvents = disable ? 'none' : '';
  }
  if (compareAddBtn) {
    compareAddBtn.style.opacity = count >= 10 ? '0.5' : '1';
    compareAddBtn.style.pointerEvents = count >= 10 ? 'none' : '';
  }
}

function setupCompareControls() {
  if (!compareRunButton || !compareSelection) return;

  // "+ Control House" — next picked file becomes the control
  if (compareAddControlBtn && compareFilePicker) {
    compareAddControlBtn.addEventListener('click', () => {
      if (_controlIndex() >= 0) { alert('A Control House is already set. Use "Set as Control" on a row to change it.'); return; }
      if (compareFiles.length >= 10) { alert('Maximum 10 houses allowed.'); return; }
      _pendingControl = true;
      compareFilePicker.click();
    });
  }

  // "+ Add House" — next picked file becomes a regular house
  if (compareAddBtn && compareFilePicker) {
    compareAddBtn.addEventListener('click', () => {
      if (compareFiles.length >= 10) { alert('Maximum 10 houses allowed.'); return; }
      _pendingControl = false;
      compareFilePicker.click();
    });
  }

  // File picker: one file at a time
  if (compareFilePicker) {
    compareFilePicker.addEventListener('change', () => {
      const file = compareFilePicker.files?.[0];
      compareFilePicker.value = ''; // reset so same path can be re-added
      if (!file) { _pendingControl = false; return; }
      if (compareFiles.length >= 10) {
        alert('Maximum 10 houses allowed.');
        _pendingControl = false;
        return;
      }
      const makeControl = _pendingControl && _controlIndex() < 0;
      // Auto-label: Control House, or House N (count of existing non-control houses + 1)
      const houseNum = compareFiles.filter(e => !e.isControl).length + 1;
      compareFiles.push({
        file,
        label: makeControl ? 'Control House' : `House ${houseNum}`,
        isControl: makeControl,
      });
      _pendingControl = false;
      _updateCompareUI();
    });
  }

  // Row interactions: rename, set-control, remove
  if (compareFileList) {
    // Live rename via the text input
    compareFileList.addEventListener('input', (e) => {
      const inp = e.target.closest('.compare-name-input');
      if (!inp) return;
      const idx = parseInt(inp.dataset.idx, 10);
      if (!isNaN(idx) && compareFiles[idx]) {
        compareFiles[idx].label = inp.value;
      }
    });

    compareFileList.addEventListener('click', (e) => {
      const setCtl = e.target.closest('.btn-set-control');
      if (setCtl) {
        const idx = parseInt(setCtl.dataset.idx, 10);
        if (!isNaN(idx) && compareFiles[idx]) {
          // Demote any existing control, relabel it as a house if it still says "Control House"
          compareFiles.forEach(en => {
            if (en.isControl) {
              en.isControl = false;
              if (en.label === 'Control House') en.label = 'House';
            }
          });
          compareFiles[idx].isControl = true;
          if (compareFiles[idx].label === '' || /^House(\s|$)/.test(compareFiles[idx].label)) {
            compareFiles[idx].label = 'Control House';
          }
          _updateCompareUI();
        }
        return;
      }
      const rm = e.target.closest('.btn-remove-file');
      if (rm) {
        const idx = parseInt(rm.dataset.idx, 10);
        if (!isNaN(idx) && idx >= 0 && idx < compareFiles.length) {
          compareFiles.splice(idx, 1);
          _updateCompareUI();
        }
      }
    });
  }

  // Clear all
  if (compareClearBtn) {
    compareClearBtn.addEventListener('click', () => {
      compareFiles = [];
      compareFileIds = [];
      _pendingControl = false;
      _updateCompareUI();
      if (comparisonResults) comparisonResults.classList.add('hidden');
    });
  }

  compareRunButton.addEventListener('click', () => {
    handleCompareRun();
  });
}

async function downloadComparisonReport() {
  if (compareFileIds.length < 2) {
    alert('Run a comparison first to generate the report.');
    return;
  }
  const btn = compareDownloadReportBtn;
  if (btn) { btn.disabled = true; btn.textContent = 'Generating report…'; }
  try {
    const response = await fetch('/api/compare-report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        file_ids: compareFileIds,
        filenames: compareLabels,
        labels: compareLabels,
      }),
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({ detail: 'Unknown error' }));
      throw new Error(err.detail || `HTTP ${response.status}`);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `comparison_report_${new Date().toISOString().split('T')[0]}.pdf`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  } catch (err) {
    alert(`Failed to download report: ${err.message}`);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '📄 Download Comparison Report (PDF)'; }
  }
}

async function downloadCommunityComparisonReport() {
  if (compareFileIds.length < 2) {
    alert('Run a comparison first to generate the report.');
    return;
  }
  const btn = document.getElementById('compare-download-community');
  if (btn) { btn.disabled = true; btn.textContent = 'Generating…'; }
  try {
    const response = await fetch('/api/community_report_compare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        file_ids: compareFileIds,   // control-first order
        labels: compareLabels,
        device_id: (deviceIdInput?.value || '').trim(),
        location: (locationInput?.value || '').trim(),
      }),
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({ detail: 'Unknown error' }));
      throw new Error(err.detail || `HTTP ${response.status}`);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `community_report_with_comparison_${new Date().toISOString().split('T')[0]}.pdf`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  } catch (err) {
    alert(`Failed to download community report: ${err.message}`);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🏡 Community Report + Comparison (PDF)'; }
  }
}

function setupTimeframeSelector(fileId, summary) {
  const dateFrom = document.getElementById('date-from');
  const dateTo = document.getElementById('date-to');
  const btnRefineAnalysis = document.getElementById('btn-refine-analysis');
  const refineLoading = document.getElementById('refine-loading');

  if (!dateFrom || !dateTo || !btnRefineAnalysis) {
    console.warn('Timeframe selector elements not found');
    return;
  }

  // Set min and max dates based on available data
  const dateRange = summary.date_range;
  if (dateRange && dateRange.start && dateRange.end) {
    const start = new Date(dateRange.start);
    const end = new Date(dateRange.end);
    
    // Format for datetime-local input (YYYY-MM-DDTHH:mm)
    const startStr = start.toISOString().slice(0, 16);
    const endStr = end.toISOString().slice(0, 16);
    
    dateFrom.min = startStr;
    dateFrom.max = endStr;
    dateTo.min = startStr;
    dateTo.max = endStr;
    
    // Set default values
    dateFrom.value = startStr;
    dateTo.value = endStr;
  }

  btnRefineAnalysis.addEventListener('click', async () => {
    const fromValue = dateFrom.value;
    const toValue = dateTo.value;

    if (!fromValue || !toValue) {
      alert('Please select both start and end dates');
      return;
    }

    if (new Date(fromValue) >= new Date(toValue)) {
      alert('Start date must be before end date');
      return;
    }

    // Show loading state
    refineLoading.classList.remove('hidden');
    btnRefineAnalysis.disabled = true;

    try {
      const response = await fetch(`/api/refine-analysis/${fileId}?date_from=${encodeURIComponent(fromValue)}&date_to=${encodeURIComponent(toValue)}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
      });

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || 'Refinement failed');
      }

      const payload = await response.json();
      const { result, outputs } = payload;

      // Update all dashboard sections with refined data
      currentTzLabel = (result.summary && result.summary.tz_label) || 'UTC';
      buildOverviewCards(result.summary);
      buildDataCompleteness(result.data_completeness);
      buildDetectedColumns(result.detected);
      renderCharts(result.charts);
      buildChartDownloads();
      buildTables(result.tables);
      buildDownloads(fileId, outputs);
      buildAnomalies(result.anomalies);

      alert(`Analysis successfully refined for ${fromValue.split('T')[0]} to ${toValue.split('T')[0]}`);
    } catch (error) {
      console.error('Refinement error:', error);
      alert(`Refinement failed: ${error.message}`);
    } finally {
      refineLoading.classList.add('hidden');
      btnRefineAnalysis.disabled = false;
    }
  });
}

// ─── PM2.5 Daily-Mean Calendar SVG ───────────────────────────────────────────
// Coloured by WHO (15) / EPA (35) 24-hour concentration thresholds — never AQI,
// since a PM2.5-only sensor cannot produce a valid multi-pollutant AQI.

function _pm25Color(v) {
  if (v === null || v === undefined || !isFinite(v)) return '#e8e8e8';
  if (v <= 15)  return '#2ca25f';   // within WHO guideline
  if (v <= 35)  return '#ffd92f';   // above WHO, within EPA 24h
  if (v <= 55)  return '#fc8d59';   // above EPA 24h
  if (v <= 150) return '#e34a33';   // high
  return '#7e0023';                 // very high
}

function _pm25Category(v) {
  if (v === null || v === undefined || !isFinite(v)) return 'No data';
  if (v <= 15)  return 'Within WHO guideline (≤15)';
  if (v <= 35)  return 'Above WHO, within EPA 24h (15–35)';
  if (v <= 55)  return 'Above EPA 24h standard (35–55)';
  if (v <= 150) return 'High (55–150)';
  return 'Very high (>150)';
}

// Compute date → daily-mean PM2.5 from the timeseries (EPA-corrected preferred).
function _dailyMeanPm25(timeseries) {
  const out = {};
  if (!timeseries || !timeseries.timestamps) return out;
  const ts = timeseries.timestamps;
  const ys = timeseries.pm25_corrected || timeseries.pm25 || [];
  const acc = {};
  for (let i = 0; i < ts.length; i++) {
    const v = ys[i];
    if (v === null || v === undefined || !isFinite(v)) continue;
    const day = String(ts[i]).slice(0, 10);
    if (!acc[day]) acc[day] = { sum: 0, n: 0 };
    acc[day].sum += v; acc[day].n += 1;
  }
  for (const d in acc) out[d] = acc[d].sum / acc[d].n;
  return out;
}

function renderPM25CalendarSVG(days, dailyPm) {
  const container = document.getElementById('chart-calendar');
  if (!container) return;
  dailyPm = dailyPm || {};

  const CELL = 20, GAP = 4, STEP = CELL + GAP;
  const DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const MONTH_NAMES = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const LEFT_PAD = 42, TOP_PAD = 42, BOTTOM_PAD = 52;

  // Resolve each day's PM2.5: prefer client-computed daily mean, fall back to backend pm25.
  const valOf = d => {
    const v = (d.date in dailyPm) ? dailyPm[d.date] : (d.pm25 ?? null);
    return (v === null || v === undefined || !isFinite(v)) ? null : v;
  };

  const sorted = [...days].sort((a, b) => a.date < b.date ? -1 : 1);
  const nWeeks = sorted.length > 0 ? (Math.max(...sorted.map(d => d.week_seq)) + 1) : 0;
  if (nWeeks === 0) {
    container.innerHTML = '<p class="empty" style="padding:2rem;text-align:center;color:#999;">No calendar data available.</p>';
    return;
  }

  const svgW = Math.max(LEFT_PAD + nWeeks * STEP + 24, 300);
  const svgH = TOP_PAD + 7 * STEP + BOTTOM_PAD;
  const latestDay = sorted.filter(d => valOf(d) !== null).slice(-1)[0];

  const monthLabels = [];
  let lastMonth = null, lastLabelX = -999;
  sorted.forEach(d => {
    const mo = d.date.slice(0, 7);
    if (mo !== lastMonth) {
      const x = LEFT_PAD + d.week_seq * STEP;
      const monthIdx = parseInt(d.date.slice(5, 7), 10) - 1;
      const label = MONTH_NAMES[monthIdx] + ' \'' + d.date.slice(2, 4);
      if (x - lastLabelX >= 30) { monthLabels.push({ x, label }); lastLabelX = x; }
      lastMonth = mo;
    }
  });

  let svgParts = [
    `<svg xmlns="http://www.w3.org/2000/svg" width="${svgW}" height="${svgH}" ` +
    `style="font-family:Space Grotesk,sans-serif;display:block;">`
  ];
  DAY_LABELS.forEach((label, i) => {
    svgParts.push(
      `<text x="${LEFT_PAD - 6}" y="${TOP_PAD + i * STEP + CELL - 5}" ` +
      `text-anchor="end" font-size="11" fill="#666">${label}</text>`
    );
  });
  monthLabels.forEach(({ x, label }) => {
    svgParts.push(`<text x="${x + 2}" y="${TOP_PAD - 10}" font-size="11" font-weight="600" fill="#444">${label}</text>`);
  });

  sorted.forEach(d => {
    const cx = LEFT_PAD + d.week_seq * STEP;
    const cy = TOP_PAD + d.weekday * STEP;
    const v = valOf(d);
    const color = _pm25Color(v);
    const isLatest = latestDay && d.date === latestDay.date;
    const stroke = isLatest ? '#1f1c16' : '#e0dbd0';
    const sw = isLatest ? 2 : 0.5;
    svgParts.push(
      `<rect class="aqi-cal-cell" x="${cx}" y="${cy}" width="${CELL}" height="${CELL}" rx="3" ry="3" ` +
      `fill="${color}" stroke="${stroke}" stroke-width="${sw}" ` +
      `data-date="${d.date}" data-pm="${v === null ? '' : v.toFixed(1)}" data-cat="${_pm25Category(v)}" />`
    );
  });

  // Legend — PM2.5 concentration bands
  const legendItems = [
    { label: '≤15 (WHO)', color: '#2ca25f' },
    { label: '15–35 (EPA)', color: '#ffd92f' },
    { label: '35–55', color: '#fc8d59' },
    { label: '55–150', color: '#e34a33' },
    { label: '>150', color: '#7e0023' },
    { label: 'No data', color: '#e8e8e8' },
  ];
  const legendY = TOP_PAD + 7 * STEP + 18;
  let lx = LEFT_PAD;
  legendItems.forEach(({ label, color }) => {
    svgParts.push(`<rect x="${lx}" y="${legendY}" width="13" height="13" rx="2" fill="${color}" stroke="#ccc" stroke-width="0.7"/>`);
    svgParts.push(`<text x="${lx + 17}" y="${legendY + 10}" font-size="10.5" fill="#555">${label}</text>`);
    lx += label.length * 6.2 + 26;
  });

  svgParts.push('</svg>');

  const lv = latestDay ? valOf(latestDay) : null;
  const dataNote = latestDay
    ? `Daily mean PM2.5 (µg/m³) from uploaded data — latest: <strong>${latestDay.date}</strong>, <strong>${lv === null ? 'N/A' : lv.toFixed(1)} µg/m³</strong> (${_pm25Category(lv)})`
    : 'Daily mean PM2.5 (µg/m³) from uploaded data';

  container.innerHTML =
    `<div class="aqi-cal-scroll">${svgParts.join('')}</div>` +
    `<p class="aqi-cal-note">${dataNote}</p>`;

  let tip = document.getElementById('aqi-cal-tip');
  if (!tip) {
    tip = document.createElement('div');
    tip.id = 'aqi-cal-tip';
    tip.className = 'aqi-cal-tip';
    document.body.appendChild(tip);
  }
  container.querySelectorAll('.aqi-cal-cell').forEach(cell => {
    cell.addEventListener('mouseenter', () => {
      const pm = cell.dataset.pm !== '' ? cell.dataset.pm : 'N/A';
      tip.innerHTML = `<strong>${cell.dataset.date}</strong><br>PM2.5: ${pm} µg/m³ — ${cell.dataset.cat}`;
      tip.style.display = 'block';
    });
    cell.addEventListener('mousemove', (e) => {
      tip.style.left = (e.pageX + 14) + 'px';
      tip.style.top  = (e.pageY - 40) + 'px';
    });
    cell.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
  });
}

// ─── Custom Notes ─────────────────────────────────────────────────────────────

function initCustomNotes() {
  customNotes = [];
  const list = document.getElementById('custom-notes-list');
  if (list) list.innerHTML = '';
}

function addNoteCard() {
  const list = document.getElementById('custom-notes-list');
  if (!list) return;
  const idx = customNotes.length;
  customNotes.push({ heading: '', content: '' });

  const card = document.createElement('div');
  card.className = 'note-card';
  card.dataset.idx = idx;
  card.innerHTML = `
    <div class="note-card-header">
      <span class="note-card-num">Note ${idx + 1}</span>
      <button type="button" class="note-remove-btn" title="Remove this note">✕</button>
    </div>
    <input type="text" class="note-heading-input" placeholder="Section heading (e.g. Site Observations)" maxlength="120" value="" />
    <textarea class="note-content-input" placeholder="Enter your notes, observations, or commentary here. Use new lines for paragraphs." rows="4"></textarea>
  `;

  card.querySelector('.note-heading-input').addEventListener('input', e => {
    customNotes[idx].heading = e.target.value;
  });
  card.querySelector('.note-content-input').addEventListener('input', e => {
    customNotes[idx].content = e.target.value;
  });
  card.querySelector('.note-remove-btn').addEventListener('click', () => {
    customNotes[idx] = null;
    card.remove();
  });

  list.appendChild(card);
}

// ─── Download report with notes via POST ─────────────────────────────────────

async function downloadReportWithNotes(fileId) {
  const notes = customNotes.filter(n => n !== null && (n.heading.trim() || n.content.trim()));
  try {
    const resp = await fetch(`/api/generate-report/${fileId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ custom_notes: notes }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      alert('Report generation failed: ' + (err.detail || resp.statusText));
      return;
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'report_analysis.pdf';
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 1000);
  } catch (e) {
    alert('Download error: ' + e.message);
  }
}

// ─── Init ─────────────────────────────────────────────────────────────────────

setupDropzone();
setupCompareControls();
setupChartResize();
_updateCompareUI();

// Bind the add-note button once on page load — not inside handleSingleFile
// so it's always functional regardless of upload flow.
(function bindAddNoteBtn() {
  const btn = document.getElementById('add-note-btn');
  if (btn) btn.addEventListener('click', addNoteCard);
})();

if (compareDownloadReportBtn) {
  compareDownloadReportBtn.addEventListener('click', downloadComparisonReport);
}

const compareDownloadCommunityBtn = document.getElementById('compare-download-community');
if (compareDownloadCommunityBtn) {
  compareDownloadCommunityBtn.addEventListener('click', downloadCommunityComparisonReport);
}

if (correctionSelect) {
  correctionSelect.addEventListener('change', () => {
    const val = correctionSelect.value;
    document.querySelectorAll('.formula-row').forEach(row => row.classList.remove('active-row'));
    document.querySelectorAll('.formula-name').forEach(n => n.classList.remove('active-formula'));
    const activeRow = document.getElementById(`formula-${val}`);
    if (activeRow) {
      activeRow.classList.add('active-row');
      activeRow.querySelector('.formula-name').classList.add('active-formula');
    }
  });
}
