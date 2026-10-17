// Interactive map of one gmina's screening. Layers come from site/data/<teryt>/,
// written by `python -m pvscreen.webmap <teryt>`; colours match the QGIS print map.

const GROUP_STYLE = {
  'Buildings and built-up land': { fillColor: '#9e9e9e' },
  'Roads and railways': { fillColor: '#616161' },
  'Water': { fillColor: '#64b5f6' },
  'Forest': { fillColor: '#81c784' },
  'Protected nature (excluded)': { fillColor: '#ba68c8' },
  'Protected landscape (constraint only)': { fillColor: '#6a1b9a', fillOpacity: 0.12, color: '#6a1b9a', weight: 1.2, dashArray: '4 3' },
};
const SCORE_CLASSES = [ // YlOrBr, as on the QGIS map
  [0.0, 0.6, '#ffffd4'],
  [0.6, 0.7, '#fee391'],
  [0.7, 0.8, '#fec44f'],
  [0.8, 0.9, '#fe9929'],
  [0.9, 1.01, '#d95f0e'],
];

const map = L.map('map', { preferCanvas: true });
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);

const legend = document.getElementById('legend');
let current = [];

const fmt = (x, digits = 0) => Number(x).toLocaleString('en-GB', { minimumFractionDigits: digits, maximumFractionDigits: digits });
const scoreColour = s => (SCORE_CLASSES.find(([lo, hi]) => s >= lo && s < hi) || SCORE_CLASSES[0])[2];

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function candidatePopup(p) {
  const rows = [
    ['Area', `${fmt(p.area_ha, 1)} ha`],
    ['Score', fmt(p.score, 2)],
    ['· slope / aspect / grid', `${fmt(p.score_slope, 2)} / ${fmt(p.score_aspect, 2)} / ${fmt(p.score_grid, 2)}`],
    ['Mean slope', `${fmt(p.mean_slope_deg, 1)}°`],
    ['Nearest substation', `${fmt(p.substation_distance_m / 1000, 1)} km`],
    ['In protected landscape', `${fmt(100 * p.constraint_share, 0)} %`],
    ['Yield', `${fmt(p.yield_kwh_per_kwp)} kWh/kWp`],
    ['Energy if fully built', `${fmt(p.energy_mwh_year / 1000, 1)} GWh/yr`],
  ];
  return `<div class="popup"><h3>Candidate #${p.rank}</h3><table>${rows
    .map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('')}</table></div>`;
}

function legendEntry(title, swatch, layer, on = true) {
  const label = document.createElement('label');
  const box = document.createElement('input');
  box.type = 'checkbox';
  box.checked = on;
  box.addEventListener('change', () => (box.checked ? layer.addTo(map) : layer.remove()));
  const sw = document.createElement('span');
  sw.className = 'swatch ' + (swatch.cls || '');
  if (swatch.colour) sw.style.background = swatch.colour;
  label.append(box, sw, document.createTextNode(title));
  return label;
}

function heading(text) {
  const h = document.createElement('h2');
  h.textContent = text;
  return h;
}

async function loadJson(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

async function show(teryt) {
  current.forEach(l => l.remove());
  current = [];
  legend.replaceChildren();
  const base = `data/${teryt}/`;
  const meta = await loadJson(base + 'meta.json');
  const data = Object.fromEntries(await Promise.all(
    meta.layers.map(async name => [name, await loadJson(`${base}${name}.geojson`)])));

  document.getElementById('gmina-name').textContent = `— ${meta.name}`;
  document.getElementById('summary').textContent =
    `${meta.candidates} candidate areas, ${fmt(meta.candidate_area_ha)} ha ` +
    `(${fmt(100 * meta.candidate_area_ha / meta.gmina_area_ha, 1)} % of the gmina, TERYT ${meta.teryt}). ` +
    'Click an area for its score.';
  document.getElementById('report-link').href =
    `https://github.com/wyplerszymon0-lab/pv-site-screening-pl/blob/main/reports/${teryt}.md`;
  document.getElementById('generated').textContent = `Generated ${meta.generated} by pvscreen ${meta.pvscreen}.`;

  const add = layer => { layer.addTo(map); current.push(layer); return layer; };

  const exclusionLayers = {};
  L.geoJSON(data.exclusions, {
    style: f => ({ stroke: false, fillOpacity: 0.55, ...GROUP_STYLE[f.properties.group] }),
    onEachFeature: (f, layer) => {
      layer.bindTooltip(escapeHtml(f.properties.group), { sticky: true });
      exclusionLayers[f.properties.group] = add(L.layerGroup([layer]));
    },
  });
  const candidates = add(L.geoJSON(data.candidates, {
    style: f => ({ color: '#8c2d04', weight: 0.6, fillColor: scoreColour(f.properties.score), fillOpacity: 0.8 }),
    onEachFeature: (f, layer) => layer.bindPopup(candidatePopup(f.properties)),
  }));
  const farms = data.farms && add(L.geoJSON(data.farms, {
    style: { color: '#000', weight: 2, fill: false },
    onEachFeature: (f, layer) => layer.bindTooltip(
      `${escapeHtml(f.properties.name || 'Solar farm')} · ${fmt(f.properties.area_ha, 1)} ha`, { sticky: true }),
  }));
  const lines = data.power_lines && add(L.geoJSON(data.power_lines, { style: { color: '#d32f2f', weight: 1.6 } }));
  const subs = data.substations && add(L.geoJSON(data.substations, {
    pointToLayer: (f, ll) => L.circleMarker(ll, { radius: 5, color: '#fff', weight: 1.5, fillColor: '#d32f2f', fillOpacity: 1 }),
  }));
  const boundary = add(L.geoJSON(data.boundary, { style: { color: '#212121', weight: 3, fill: false }, interactive: false }));
  map.fitBounds(boundary.getBounds(), { padding: [10, 10] });

  legend.append(heading('Candidate areas by score'));
  SCORE_CLASSES.forEach(([lo, hi, colour], i) => {
    const label = document.createElement('label');
    const sw = document.createElement('span');
    sw.className = 'swatch';
    sw.style.background = colour;
    label.append(sw, document.createTextNode(`${lo.toFixed(1)} – ${Math.min(hi, 1).toFixed(1)}`));
    legend.append(label);
  });
  legend.append(legendEntry('show candidates', { colour: '#fe9929' }, candidates));
  legend.append(heading('Excluded'));
  Object.keys(GROUP_STYLE).forEach(group => {
    if (!exclusionLayers[group]) return;
    const swatch = group.startsWith('Protected landscape') ? { cls: 'hatch' } : { colour: GROUP_STYLE[group].fillColor };
    legend.append(legendEntry(group, swatch, exclusionLayers[group]));
  });
  legend.append(heading('Context'));
  if (farms) legend.append(legendEntry('Existing solar farms (OSM)', { cls: 'outline' }, farms));
  if (lines) legend.append(legendEntry('Power lines (OSM)', { cls: 'line' }, lines));
  if (subs) legend.append(legendEntry('Substations (OSM)', { cls: 'point' }, subs));
  legend.append(legendEntry('Gmina boundary', { cls: 'outline' }, boundary));
}

(async () => {
  try {
    const gminas = await loadJson('data/index.json');
    const select = document.getElementById('gmina-select');
    for (const teryt of gminas) {
      const meta = await loadJson(`data/${teryt}/meta.json`);
      select.append(new Option(`${meta.name} (${teryt})`, teryt));
    }
    select.hidden = gminas.length < 2;
    const wanted = new URLSearchParams(location.search).get('teryt');
    select.value = gminas.includes(wanted) ? wanted : gminas[0];
    select.addEventListener('change', () => {
      history.replaceState(null, '', `?teryt=${select.value}`);
      show(select.value);
    });
    await show(select.value);
  } catch (err) {
    document.getElementById('summary').textContent = `Could not load the data: ${err.message}`;
  }
})();
