// Offline map module (MapLibre, no network at runtime): inline style + GeoJSON sources
// built from the snapshot. All coordinates converted to GeoJSON order [lon, lat].

const C = {
  roadMinor: '#162231', roadMajor: '#1d3145', river: '#0f2f43', riverLine: '#1f7da0',
  inc: '#fb923c', incNew: '#e879f9', water: '#38bdf8', waterDry: '#566274',
  restr: '#f43f5e', warn: '#fbbf24', ink: '#e8edf4', dark: '#080c12',
};

const FC = (features) => ({ type: 'FeatureCollection', features });
const pt = (lat, lon, props) => ({ type: 'Feature', properties: props || {}, geometry: { type: 'Point', coordinates: [lon, lat] } });
const ln = (pts, props) => ({ type: 'Feature', properties: props || {}, geometry: { type: 'LineString', coordinates: pts.map(([la, lo]) => [lo, la]) } });
const pg = (ring, props) => ({ type: 'Feature', properties: props || {}, geometry: { type: 'Polygon', coordinates: [ring.map(([la, lo]) => [lo, la])] } });

function lineLayer(id, src, filter, color, width, dash) {
  const l = { id, type: 'line', source: src, paint: { 'line-color': ['get', 'color'], 'line-width': width } };
  if (filter) l.filter = filter;
  if (dash) l.paint['line-dasharray'] = dash;
  return l;
}
function circleLayer(id, src, radius, strokeW) {
  return {
    id, type: 'circle', source: src,
    paint: { 'circle-radius': radius, 'circle-color': ['get', 'color'],
             'circle-stroke-color': ['get', 'stroke'], 'circle-stroke-width': strokeW },
  };
}

export async function createMap(container, { bounds = [[107.65, -7.25], [107.81, -7.08]] } = {}) {
  const basemap = await fetch('/data/basemap.geojson').then((r) => r.json());
  // basemap features have no color props — normalize them for data-driven paint.
  const rivers = basemap.features.filter((f) => f.properties.kind === 'river');
  basemap.features = basemap.features.map((f) => ({
    ...f,
    properties: { ...f.properties, color: f.properties.kind === 'river' ? C.river : f.properties.major ? C.roadMajor : C.roadMinor },
  }));
  for (const r of rivers) {
    basemap.features.push({ ...r, properties: { ...r.properties, kind: 'riverline', color: C.riverLine } });
  }
  const empty = FC([]);
  const style = {
    version: 8,
    sources: {
      basemap: { type: 'geojson', data: basemap },
      stations: { type: 'geojson', data: empty },
      water: { type: 'geojson', data: empty },
      incidents: { type: 'geojson', data: empty },
      vehicles: { type: 'geojson', data: empty },
      members: { type: 'geojson', data: empty },
      restrictions: { type: 'geojson', data: empty },
      route: { type: 'geojson', data: empty },
    },
    layers: [
      { id: 'bg', type: 'background', paint: { 'background-color': '#080c12' } },
      lineLayer('river', 'basemap', ['==', ['get', 'kind'], 'river'], '#0f2f43', 16),
      lineLayer('riverline', 'basemap', ['==', ['get', 'kind'], 'riverline'], C.riverLine, 1.5),
      lineLayer('roads', 'basemap', ['==', ['get', 'kind'], 'road'], C.roadMinor, 4),
      lineLayer('roads-major', 'basemap', ['all', ['==', ['get', 'kind'], 'road'], ['==', ['get', 'major'], true]], C.roadMajor, 7),
      { id: 'restr-fill', type: 'fill', source: 'restrictions', filter: ['==', ['get', 'kind'], 'polygon'],
        paint: { 'fill-color': C.restr, 'fill-opacity': 0.22 } },
      lineLayer('restr-line', 'restrictions', ['==', ['get', 'kind'], 'polygon'], C.restr, 1.5),
      circleLayer('restr-pt', 'restrictions', 8, 2),
      lineLayer('route-solid', 'route', ['==', ['get', 'preview'], false], '', 3.5),
      lineLayer('route-preview', 'route', ['==', ['get', 'preview'], true], '', 3, [3, 3]),
      circleLayer('station-dot', 'stations', 9, 2),
      circleLayer('water-dot', 'water', 9, 2),
      circleLayer('member-dot', 'members', 4, 1.5),
      circleLayer('vehicle-dot', 'vehicles', 8, 2),
      circleLayer('incident-dot', 'incidents', 12, 3),
    ],
  };
  const map = new maplibregl.Map({ container, style, attributionControl: false });
  // Camera without waiting for 'load' (headless browsers may never fire it): approximate fit.
  try {
    const [sw, ne] = bounds;
    const w = container.clientWidth || 800, h = container.clientHeight || 450;
    const zoom = Math.log2(Math.min((w * 360) / (256 * (ne[0] - sw[0])), (h * 360) / (256 * (ne[1] - sw[1]))) * 0.85);
    map.setCenter([(sw[0] + ne[0]) / 2, (sw[1] + ne[1]) / 2]);
    map.setZoom(Math.max(1, zoom));
    map.fitBounds(bounds, { padding: 24 }); // exact once style loads
  } catch { /* keep default camera */ }
  return map;
}

// Fold snapshot + UI selection into source data. `ui = {inc, members:Set, water, route}`.
export function mapData(state, ui = {}) {
  const st = state;
  const stations = FC(st.stations.map((s) => pt(s.lat, s.lon, { id: s.id, name: s.name, color: C.dark, stroke: C.ink })));
  const waterFeatures = [];
  for (const w of st.water_sources) {
    const dry = w.status !== 'active';
    const picked = ui.water === w.id;
    waterFeatures.push(pt(w.location.lat, w.location.lon, {
      id: w.id, name: w.name, type: w.type, dry,
      color: dry ? C.dark : C.water, stroke: picked ? '#ffffff' : dry ? C.waterDry : C.water,
    }));
    for (const ap of w.access_points || []) {
      waterFeatures.push(pt(ap.lat, ap.lon, { id: w.id + ':ap', color: dry ? C.dark : C.water, stroke: dry ? C.waterDry : C.water }));
    }
  }
  const water = FC(waterFeatures);
  const incidents = FC(st.incidents.map((i) => {
    const l = i.location ?? { lat: i.lat, lon: i.lon };
    return pt(l.lat, l.lon, { id: i.id, color: i.status === 'Baru' ? C.incNew : C.inc, stroke: ui.inc === i.id ? '#ffffff' : C.dark });
  }));
  const vehicles = FC(st.vehicles.filter((v) => v.position).map((v) => pt(v.position.lat, v.position.lon, {
    id: v.id, color: ui.vehicleColor ? ui.vehicleColor(v) : '#ff9f68', stroke: C.dark,
  })));
  const members = FC(st.members.filter((m) => m.position).map((m) => pt(m.position.lat, m.position.lon, {
    id: m.id, color: ui.memberColor ? ui.memberColor(m) : C.ink, stroke: C.dark,
  })));
  const restrFeatures = st.road_restrictions
    .filter((r) => r.status === 'pending' || r.status === 'verified')
    .map((r) => {
      const pending = r.status === 'pending';
      const props = { id: r.id, kind: r.kind, pending, color: pending ? C.warn : C.restr, stroke: pending ? C.warn : C.restr };
      if (r.geometry.kind === 'point') return pt(r.geometry.coordinates[0][0], r.geometry.coordinates[0][1], props);
      return pg(r.geometry.coordinates, props);
    });
  const restrictions = FC(restrFeatures);
  const routeFeatures = [];
  if (ui.route) {
    for (const leg of ui.route.legs) {
      routeFeatures.push(ln(leg.geometry, { preview: true, color: leg.color || C.water }));
    }
  }
  const route = FC(routeFeatures);
  return { stations, water, incidents, vehicles, members, restrictions, route };
}

export function applyMap(map, data) {
  for (const [name, fc] of Object.entries(data)) map.getSource(name)?.setData(fc);
  map.triggerRepaint();
  // Headless browsers may not run the rAF loop; draw immediately.
  try { map.render(); } catch { /* render loop takes over in real browsers */ }
}
