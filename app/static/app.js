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
const compareFileList = document.getElementById('compare-file-list');
const compareClearBtn = document.getElementById('compare-clear');
const compareDownloadReportBtn = document.getElementById('compare-download-report');
const deviceIdInput = document.getElementById('device-id-input');
const locationInput = document.getElementById('location-input');
const narrativeText = document.getElementById('narrative-text');
const narrativeSummary = document.getElementById('narrative-summary');
const correctionSelect = document.getElementById('correction-select');

// State for comparison file list (one-by-one selection, max 10)
let compareFiles = [];
let compareFileIds = [];  // file_ids returned from server after comparison runs

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
  const eventsContainer = document.getElementById('table-events');
  const highestContainer = document.getElementById('table-highest-events');

  if (!statsContainer || !exceedContainer || !eventsContainer) {
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
  const eventsHtml = buildTable(tables.events);
  eventsContainer.innerHTML = eventsHtml.includes('<table>')
    ? `<div class="event-type-legend compact">` +
        `<span class="event-type-badge spike">Spike</span> sudden surge &nbsp;|&nbsp; ` +
        `<span class="event-type-badge sustained">Sustained</span> 3+ hrs elevated` +
      `</div><div class="table-scroll">${eventsHtml}</div>`
    : eventsHtml;

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
  const downloadPublic = document.getElementById('download-buttons-public');
  
  if (!downloadResearch || !downloadPublic) {
    console.error('Download containers not found');
    return;
  }
  
  downloadResearch.innerHTML = '';
  downloadPublic.innerHTML = '';
  
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
        { id: 'chart-aqi', label: 'Air Quality Index' },
        { id: 'chart-hourly', label: 'Diurnal Cycle' },
        { id: 'chart-channel', label: 'Sensor Comparison' },
        { id: 'chart-heatmap', label: 'Hourly Heatmap' },
        { id: 'chart-quality', label: 'Data Quality Radar' },
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
    'aqi': 'aqi_gauge',
    'hourly': 'hourly_pattern',
    'heatmap': 'rolling_median',
    'channel': 'channel_comparison',
    'humidity': 'sensor_drift',
    'aqi-dist': 'radar_pattern',
    'quality': 'pm25_temporal_radar',
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
      'chart-timeseries', 'chart-aqi', 'chart-hourly', 'chart-heatmap',
      'chart-channel', 'chart-humidity', 'chart-aqi-dist', 'chart-quality'
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
        xaxis: { title: 'Date/Time (UTC)', ...chartLayouts.base.xaxis },
        yaxis: { title: 'PM2.5 Concentration (µg/m³)', ...chartLayouts.base.yaxis },
      },
      chartConfig
    );
    console.log('✓ Timeseries rendered');

    // Chart 2: AQI Gauge
    console.log('Rendering AQI gauge...');
    Plotly.newPlot(
      'chart-aqi',
      [
        {
          type: 'indicator',
          mode: 'gauge+number+delta',
          value: charts.aqi_gauge.value,
          title: { text: 'Air Quality Index' },
          gauge: {
            axis: { range: [0, 500] },
            bar: { color: charts.aqi_gauge.color },
            steps: [
              { range: [0, 50], color: '#E8F5E9' },
              { range: [51, 100], color: '#FFF9C4' },
              { range: [101, 150], color: '#FFE0B2' },
              { range: [151, 200], color: '#FFCCBC' },
              { range: [201, 300], color: '#F3E5F5' },
              { range: [301, 500], color: '#FCE4EC' },
            ],
            threshold: {
              line: { color: 'red', width: 4 },
              thickness: 0.75,
              value: 150
            }
          },
          number: { font: { size: 24 } },
        },
      ],
      {
        ...chartLayouts.base,
        margin: { t: 20, r: 20, b: 20, l: 20 },
      },
      chartConfig
    );
    console.log('✓ AQI gauge rendered');

    // Chart 3: Hourly pattern
    console.log('Rendering hourly pattern...');
    Plotly.newPlot(
      'chart-hourly',
      [
        {
          x: charts.hourly_pattern.hours,
          y: charts.hourly_pattern.values,
          type: 'bar',
          marker: { color: '#1f7a8c' },
          name: 'Mean PM2.5',
          hovertemplate: 'Hour %{x}:00 UTC<br>PM2.5: %{y:.1f} µg/m³<extra></extra>',
        },
      ],
      {
        ...chartLayouts.base,
        title: 'Diurnal Cycle',
        xaxis: { title: 'Hour of Day (UTC)', ...chartLayouts.base.xaxis },
        yaxis: { title: 'PM2.5 (µg/m³)', ...chartLayouts.base.yaxis },
      },
      chartConfig
    );
    console.log('✓ Hourly pattern rendered');

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
          xaxis: { title: 'Date/Time (UTC)', ...chartLayouts.base.xaxis },
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
          xaxis: { title: 'Date/Time (UTC)', ...chartLayouts.base.xaxis },
          yaxis: { title: 'Difference (µg/m³)', ...chartLayouts.base.yaxis },
        },
        chartConfig
      );
    } else {
      document.getElementById('chart-humidity').innerHTML = '<p class="empty">Sensor drift analysis available in research report</p>';
    }
    console.log('✓ Sensor drift rendered');

    // Chart 7: Data Quality Profile Radar
    console.log('Rendering quality radar...');
    if (charts.radar_pattern) {
      const radarData = charts.radar_pattern;
      Plotly.newPlot(
        'chart-aqi-dist',
        [
          {
            type: 'scatterpolar',
            r: radarData.values || [],
            theta: radarData.labels || [],
            fill: 'toself',
            name: 'Quality Metrics',
            line: { color: '#1f7a8c' },
            fillcolor: 'rgba(31, 122, 140, 0.3)',
            hovertemplate: '<b>%{theta}</b><br>Score: %{r:.1f}%<extra></extra>',
          },
        ],
        {
          polar: {
            radialaxis: { visible: true, range: [0, 100] }
          },
          title: 'Data Quality Profile (Multi-Dimensional)',
          font: chartLayouts.base.font,
          height: 460,
          margin: { l: 60, r: 60, t: 60, b: 60 },
        },
        chartConfig
      );
    } else {
      document.getElementById('chart-aqi-dist').innerHTML = '<p class="empty">Data quality profile available in research report</p>';
    }
    console.log('✓ Quality radar rendered');

    // Chart 8: PM2.5 24-Hour Temporal Radar
    console.log('Rendering temporal radar...');
    if (charts.pm25_temporal_radar) {
      const tempRadar = charts.pm25_temporal_radar;
      Plotly.newPlot(
        'chart-quality',
        [
          {
            type: 'scatterpolar',
            r: tempRadar.values || [],
            theta: tempRadar.labels || [],
            fill: 'toself',
            name: 'PM2.5 by Hour',
            line: { color: '#f25c54' },
            fillcolor: 'rgba(242, 92, 84, 0.3)',
            hovertemplate: '<b>%{theta}</b><br>PM2.5: %{r:.1f} µg/m³<extra></extra>',
          },
        ],
        {
          polar: {
            radialaxis: { visible: true, range: [0, Math.max(...(tempRadar.values || [1])) * 1.2] }
          },
          title: 'PM2.5 Temporal Pattern (24-Hour Polar)',
          font: chartLayouts.base.font,
          height: 460,
          margin: { l: 60, r: 60, t: 60, b: 60 },
        },
        chartConfig
      );
    } else {
      document.getElementById('chart-quality').innerHTML = '<p class="empty">Temporal pattern analysis available in research report</p>';
    }
    console.log('✓ Temporal radar rendered');

    // Calendar — GitHub-style SVG with individual colored cells
    if (charts.calendar && charts.calendar.days && charts.calendar.days.length > 0) {
      renderAQICalendarSVG(charts.calendar.days);
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

  } catch (error) {
    console.error('Chart rendering failed:', error);
    console.error('Error stack:', error.stack);
    
    // Try to identify which chart failed
    const chartIds = ['chart-timeseries', 'chart-aqi', 'chart-hourly', 'chart-heatmap', 'chart-channel', 'chart-humidity', 'chart-aqi-dist', 'chart-quality'];
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
    'chart-aqi',
    'chart-hourly',
    'chart-heatmap',
    'chart-channel',
    'chart-humidity',
    'chart-aqi-dist',
    'chart-quality',
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

    return {
      File: a.filename || a.file_id,
      'Start': start,
      'End': end,
      'Avg PM2.5 (EPA corr.)': _formatMaybeNumber(thisPmCorr, 1),
      'Δ PM2.5 vs File 1': index === 0 ? '' : _formatDelta(thisPmCorr !== null && basePmCorr !== null ? thisPmCorr - basePmCorr : null, 1),
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

    buildOverviewCards(result.summary);
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
    alert('Add at least 2 files to compare.');
    return;
  }
  if (compareFiles.length > 10) {
    alert('Maximum 10 files allowed for comparison.');
    return;
  }

  const formData = new FormData();
  compareFiles.forEach((file) => formData.append('files', file));

  setLoadingText(`Comparing ${compareFiles.length} files… This may take a moment for large datasets.`);
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

    // Capture file_ids for comparison report download
    compareFileIds = analyses.map(a => a.file_id).filter(Boolean);
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
  // Temporarily widen margins so axis titles and legend are fully visible in the export.
  // Polar charts (scatterpolar) use a different layout key; handle both cases.
  const isPolar = chartEl.data && chartEl.data.some(t => t.type === 'scatterpolar' || t.type === 'indicator');
  const origLayout = chartEl.layout || {};

  // Build export-friendly layout patch
  const exportPatch = isPolar
    ? { margin: { l: 80, r: 80, t: 100, b: 100 } }
    : {
        margin: { l: 120, r: 260, t: 90, b: 110 },
        legend: {
          x: 1.02,
          xanchor: 'left',
          y: 1,
          yanchor: 'top',
          bgcolor: 'rgba(255,255,255,0.95)',
          bordercolor: '#ccc',
          borderwidth: 1,
          font: { size: 13 },
        },
        xaxis: { ...((origLayout.xaxis) || {}), title: { ...(origLayout.xaxis?.title || {}), font: { size: 14 } } },
        yaxis: { ...((origLayout.yaxis) || {}), title: { ...(origLayout.yaxis?.title || {}), font: { size: 14 } } },
        font: { size: 14 },
      };

  await Plotly.relayout(chartEl, exportPatch);

  const dataUrl = await Plotly.toImage(chartEl, {
    format: 'png',
    width: isPolar ? 1400 : 2600,
    height: isPolar ? 1400 : 1300,
    scale: 2,
  });

  // Restore original layout
  const restorePatch = {
    margin: origLayout.margin || { l: 70, r: 80, t: 50, b: 60 },
    legend: origLayout.legend || chartLayouts.base.legend,
    font: origLayout.font || chartLayouts.base.font,
  };
  if (!isPolar) {
    restorePatch.xaxis = origLayout.xaxis || {};
    restorePatch.yaxis = origLayout.yaxis || {};
  }
  await Plotly.relayout(chartEl, restorePatch);

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

function _updateCompareUI() {
  const count = compareFiles.length;
  if (compareSelection) {
    compareSelection.textContent = `${count} / 10 files selected.${count >= 2 ? ' Ready to compare.' : ' Add at least 2 files.'}`;
  }
  if (compareRunButton) compareRunButton.disabled = count < 2;
  if (compareClearBtn) compareClearBtn.style.display = count > 0 ? '' : 'none';

  // Rebuild file list display
  if (compareFileList) {
    const emptyNote = document.getElementById('compare-empty-note');
    // Remove old file rows (keep the empty note node)
    Array.from(compareFileList.querySelectorAll('.compare-file-row')).forEach(el => el.remove());

    if (count === 0) {
      if (emptyNote) emptyNote.style.display = '';
    } else {
      if (emptyNote) emptyNote.style.display = 'none';
      compareFiles.forEach((file, idx) => {
        const row = document.createElement('div');
        row.className = 'compare-file-row';
        row.innerHTML = `
          <span class="compare-file-index">${idx + 1}</span>
          <span class="compare-file-name" title="${file.name}">${file.name}</span>
          <span class="compare-file-size">${(file.size / 1024).toFixed(0)} KB</span>
          <button class="btn-remove-file" data-idx="${idx}" title="Remove this file">✕</button>
        `;
        compareFileList.appendChild(row);
      });
    }
  }

  // Disable add button when at max
  if (compareAddBtn) {
    compareAddBtn.style.opacity = count >= 10 ? '0.5' : '1';
    compareAddBtn.style.pointerEvents = count >= 10 ? 'none' : '';
  }
}

function setupCompareControls() {
  if (!compareRunButton || !compareSelection) return;

  // "Add File" button triggers the hidden file picker programmatically
  if (compareAddBtn && compareFilePicker) {
    compareAddBtn.addEventListener('click', () => {
      if (compareFiles.length >= 10) {
        alert('Maximum 10 files allowed for comparison.');
        return;
      }
      compareFilePicker.click();
    });
  }

  // File picker: one file at a time
  if (compareFilePicker) {
    compareFilePicker.addEventListener('change', () => {
      const file = compareFilePicker.files?.[0];
      if (!file) return;
      if (compareFiles.length >= 10) {
        alert('Maximum 10 files allowed for comparison.');
        compareFilePicker.value = '';
        return;
      }
      compareFiles.push(file);
      compareFilePicker.value = ''; // Reset so same file can be re-added from same path
      _updateCompareUI();
    });
  }

  // Remove individual file
  if (compareFileList) {
    compareFileList.addEventListener('click', (e) => {
      const btn = e.target.closest('.btn-remove-file');
      if (!btn) return;
      const idx = parseInt(btn.dataset.idx, 10);
      if (!isNaN(idx) && idx >= 0 && idx < compareFiles.length) {
        compareFiles.splice(idx, 1);
        _updateCompareUI();
      }
    });
  }

  // Clear all
  if (compareClearBtn) {
    compareClearBtn.addEventListener('click', () => {
      compareFiles = [];
      compareFileIds = [];
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
        filenames: compareFiles.map(f => f.name),
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
      buildOverviewCards(result.summary);
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

// ─── AQI Calendar SVG ────────────────────────────────────────────────────────

function _aqiColor(aqi) {
  if (aqi === null || aqi === undefined) return '#e8e8e8';
  if (aqi <= 50)  return '#00E400';
  if (aqi <= 100) return '#FFFF00';
  if (aqi <= 150) return '#FF7E00';
  if (aqi <= 200) return '#FF0000';
  if (aqi <= 300) return '#8F3F97';
  return '#7E0023';
}

function _aqiCategory(aqi) {
  if (aqi === null || aqi === undefined) return 'No data';
  if (aqi <= 50)  return 'Good';
  if (aqi <= 100) return 'Moderate';
  if (aqi <= 150) return 'Unhealthy for Sensitive Groups';
  if (aqi <= 200) return 'Unhealthy';
  if (aqi <= 300) return 'Very Unhealthy';
  return 'Hazardous';
}

function renderAQICalendarSVG(days) {
  const container = document.getElementById('chart-calendar');
  if (!container) return;

  const CELL = 20, GAP = 4, STEP = CELL + GAP;
  const DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const MONTH_NAMES = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const LEFT_PAD = 42, TOP_PAD = 42, BOTTOM_PAD = 52;

  // Sort days and build week columns (Monday-aligned)
  const sorted = [...days].sort((a, b) => a.date < b.date ? -1 : 1);
  const nWeeks = sorted.length > 0 ? (Math.max(...sorted.map(d => d.week_seq)) + 1) : 0;
  if (nWeeks === 0) {
    container.innerHTML = '<p class="empty" style="padding:2rem;text-align:center;color:#999;">No calendar data available.</p>';
    return;
  }

  const svgW = Math.max(LEFT_PAD + nWeeks * STEP + 24, 300);
  const svgH = TOP_PAD + 7 * STEP + BOTTOM_PAD;

  // Find the most recent day with data
  const latestDay = sorted.filter(d => d.aqi !== null && d.aqi !== undefined).slice(-1)[0];

  // Month label positions — only at month transitions, with minimum spacing of 28px
  const monthLabels = [];
  let lastMonth = null, lastLabelX = -999;
  sorted.forEach(d => {
    const mo = d.date.slice(0, 7);
    if (mo !== lastMonth) {
      const x = LEFT_PAD + d.week_seq * STEP;
      const monthIdx = parseInt(d.date.slice(5, 7), 10) - 1;
      const label = MONTH_NAMES[monthIdx] + ' \'' + d.date.slice(2, 4);
      if (x - lastLabelX >= 30) {
        monthLabels.push({ x, label });
        lastLabelX = x;
      }
      lastMonth = mo;
    }
  });

  // Build SVG (scrollable wrapper applied via CSS)
  let svgParts = [
    `<svg xmlns="http://www.w3.org/2000/svg" width="${svgW}" height="${svgH}" ` +
    `style="font-family:Space Grotesk,sans-serif;display:block;">`
  ];

  // Day-of-week labels
  DAY_LABELS.forEach((label, i) => {
    svgParts.push(
      `<text x="${LEFT_PAD - 6}" y="${TOP_PAD + i * STEP + CELL - 5}" ` +
      `text-anchor="end" font-size="11" fill="#666">${label}</text>`
    );
  });

  // Month labels (top, no overlap)
  monthLabels.forEach(({ x, label }) => {
    svgParts.push(`<text x="${x + 2}" y="${TOP_PAD - 10}" font-size="11" font-weight="600" fill="#444">${label}</text>`);
  });

  // Cells
  sorted.forEach(d => {
    const cx = LEFT_PAD + d.week_seq * STEP;
    const cy = TOP_PAD + d.weekday * STEP;
    const color = _aqiColor(d.aqi);
    const isLatest = latestDay && d.date === latestDay.date;
    const stroke = isLatest ? '#1f1c16' : '#e0dbd0';
    const sw = isLatest ? 2 : 0.5;
    svgParts.push(
      `<rect class="aqi-cal-cell" x="${cx}" y="${cy}" width="${CELL}" height="${CELL}" rx="3" ry="3" ` +
      `fill="${color}" stroke="${stroke}" stroke-width="${sw}" ` +
      `data-date="${d.date}" data-aqi="${d.aqi ?? ''}" data-cat="${_aqiCategory(d.aqi)}" />`
    );
  });

  // Legend row
  const legendItems = [
    { label: 'Good', color: '#00E400' },
    { label: 'Moderate', color: '#FFFF00' },
    { label: 'USG', color: '#FF7E00' },
    { label: 'Unhealthy', color: '#FF0000' },
    { label: 'V. Unhealthy', color: '#8F3F97' },
    { label: 'Hazardous', color: '#7E0023' },
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

  // Wrap SVG + subtitle
  const dataNote = latestDay
    ? `Daily average AQI from uploaded file data (not real-time) — latest: <strong>${latestDay.date}</strong>, AQI <strong>${latestDay.aqi ?? 'N/A'}</strong> (${_aqiCategory(latestDay.aqi)})`
    : 'Daily average AQI from uploaded file data (not real-time)';

  container.innerHTML =
    `<div class="aqi-cal-scroll">${svgParts.join('')}</div>` +
    `<p class="aqi-cal-note">${dataNote}</p>`;

  // Floating tooltip
  let tip = document.getElementById('aqi-cal-tip');
  if (!tip) {
    tip = document.createElement('div');
    tip.id = 'aqi-cal-tip';
    tip.className = 'aqi-cal-tip';
    document.body.appendChild(tip);
  }

  container.querySelectorAll('.aqi-cal-cell').forEach(cell => {
    cell.addEventListener('mouseenter', () => {
      const aqi = cell.dataset.aqi !== '' ? Number(cell.dataset.aqi) : null;
      tip.innerHTML = `<strong>${cell.dataset.date}</strong><br>AQI: ${aqi ?? 'N/A'} — ${cell.dataset.cat}`;
      tip.style.display = 'block';
    });
    cell.addEventListener('mousemove', (e) => {
      tip.style.left = (e.pageX + 14) + 'px';
      tip.style.top  = (e.pageY - 40) + 'px';
    });
    cell.addEventListener('mouseleave', () => {
      tip.style.display = 'none';
    });
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
