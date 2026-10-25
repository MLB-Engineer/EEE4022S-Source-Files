/* Developer console UI. Backend credentials never enter this file. */
function escapeText(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function loadPublicConfig() {
  const link = document.getElementById('database-manager-link');
  const note = document.getElementById('database-manager-note');
  try {
    const response = await fetch('/api/v1/config', {headers: authHeaders()});
    if (!response.ok) throw new Error('Could not load database link.');
    const config = await response.json();
    // Use the public address, never the Docker-internal INFLUX_URL.
    if (config.influx_ui_url) {
      const url = new URL(config.influx_ui_url);
      if (!['https:', 'http:'].includes(url.protocol)) throw new Error('Invalid database link.');
      link.href = url.href;
      link.removeAttribute('aria-disabled');
      note.textContent = '';
    } else {
      const url = new URL(window.location.origin);
      url.port = '8086';
      url.pathname = '/';
      link.href = url.href;
      link.removeAttribute('aria-disabled');
      note.textContent = '';
    }
  } catch (error) {
    link.removeAttribute('href');
    link.setAttribute('aria-disabled', 'true');
    note.textContent = error.message;
  }
}

function renderDeveloperHealth(health) {
  const db = health.database || {};
  setText('#health-database', {healthy:'Connected', disconnected:'Disconnected', not_configured:'Not configured'}[db.status] || 'Unknown');
  setText('#health-ingestion', {healthy:'Receiving', warning:'No recent samples', no_data:'No samples in last 7 days', unknown:'Unknown'}[db.ingestion] || 'Unknown');
  setText('#health-stored-age', db.storage_age_seconds == null ? 'No sample available' : `${Math.round(db.storage_age_seconds)} s ago`);
  setText('#health-rate', `${health.telemetry_rate_hz ?? 0} msg/s`);
  setText('#health-message-count', health.telemetry_messages ?? 0);
  setText('#health-invalid-count', health.invalid_messages ?? 0);
  setText('#health-browsers', health.browser_connections ?? 0);
  const seconds = health.uptime_seconds || 0;
  setText('#health-uptime', `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m ${seconds % 60}s`);
  setText('#health-ack-latency', health.last_ack_latency_ms == null ? 'No acknowledgements yet' : `${health.last_ack_latency_ms} ms`);
  setText('#health-checked', `Last checked ${new Date(health.checked_at).toLocaleTimeString()}`);
}
function clearDeveloperHealth() {
  document.querySelectorAll('.health-card strong[id]').forEach(el => el.textContent = '--');
  setText('#health-checked', 'Health check unavailable. Check your connection or sign in again.');
}

const remoteState = {left_indicator: false, right_indicator: false, hazards: false};
let developerSession = 0;
function resetDeveloperSession() {
  developerSession++;
  Object.keys(remoteState).forEach(key => remoteState[key] = false);
  document.querySelectorAll('.remote-command').forEach(button => {
    button.disabled = false;
    if (button.hasAttribute('aria-pressed')) button.setAttribute('aria-pressed', 'false');
    button.querySelector('.command-state').textContent = button.dataset.action === 'hoot' ? 'Press to sound' : 'Turn on';
  });
  document.querySelectorAll('.command-history').forEach(list => list.replaceChildren());
  document.getElementById('gateway-message-send').disabled = false;
  document.getElementById('gateway-message').value = '';
  window.__analyticsPoints = [];
  if (analyticsAbort) analyticsAbort.abort();
}
function feedback(action, text, status = '', timestamp = Date.now()) {
  const list = document.getElementById(`feedback-${action}`);
  list.querySelector('.no-commands')?.remove();
  const item = document.createElement('li');
  item.className = status ? `feedback-${status}` : '';
  const label = document.createElement('span');
  label.textContent = text;
  const when = document.createElement('time');
  when.dateTime = new Date(timestamp).toISOString();
  when.textContent = new Date(timestamp).toLocaleTimeString();
  item.append(label, when);
  list.prepend(item);
  while (list.children.length > 12) list.lastElementChild.remove();
  return item;
}
async function sendDeveloperCommand(action, forceOff = false) {
  const session = developerSession;
  const toggle = Object.hasOwn(remoteState, action);
  const nextState = forceOff ? false : !remoteState[action];
  const cmd = toggle ? `${action}_${nextState ? 'on' : 'off'}` : action;
  const button = action === 'message' ? document.getElementById('gateway-message-send') : document.querySelector(`[data-action="${action}"]`);
  if (button.disabled) return;
  const body = {cmd, target: 'vehicle'};
  if (action === 'message') {
    body.message = document.getElementById('gateway-message').value.trim();
    if (!body.message) return;
  }
  button.disabled = true;
  const description = toggle ? `${nextState ? 'On' : 'Off'} command` : action === 'message' ? 'Message' : 'Hoot command';
  try {
    const response = await fetch('/api/v1/control/command', {method:'POST', headers:{'Content-Type':'application/json', ...authHeaders()}, body:JSON.stringify(body),signal:AbortSignal.timeout(5000)});
    const sent = await response.json();
    if (session !== developerSession) return;
    if (!response.ok) throw new Error(sent.detail || `Command failed (${response.status}).`);
    feedback(action, `${description} sent. Waiting for acknowledgement.`, '', sent.sent_at * 1000);
    const deadline = Date.now() + (sent.timeout_seconds || 10) * 1000 + 1000;
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 500));
      if (session !== developerSession) return;
      const statusResponse = await fetch(`/api/v1/control/status/${encodeURIComponent(cmd)}?command_id=${encodeURIComponent(sent.command_id)}`, {headers:authHeaders(),signal:AbortSignal.timeout(5000)});
      if (!statusResponse.ok) throw new Error(`Acknowledgement check failed (${statusResponse.status}).`);
      const ack = await statusResponse.json();
      if (session !== developerSession) return;
      if (ack.status === 'ack') {
        feedback(action, `${description} successful${toggle ? ` · ${nextState ? 'on' : 'off'}` : ''}. ${ack.note || ''} (${ack.latency_ms} ms)`, 'success', ack.acknowledged_at * 1000);
        if (toggle) {
          remoteState[action] = nextState;
          button.setAttribute('aria-pressed', String(nextState));
          button.querySelector('.command-state').textContent = nextState ? 'Turn off' : 'Turn on';
        }
        if (action === 'message' && document.getElementById('gateway-message').value.trim() === body.message) document.getElementById('gateway-message').value = '';
        return;
      }
      if (['rejected','error','failed'].includes(ack.status)) throw new Error(`Gateway ${ack.status}: ${ack.note || 'Command was not confirmed.'}`);
      if (ack.status === 'timeout') break;
    }
    throw new Error('No acknowledgement received. Outcome is unconfirmed.');
  } catch (error) {
    if (session !== developerSession) return;
    const row = feedback(action, error.message, 'error');
    if (toggle) {
      // Explicit OFF remains possible after a lost acknowledgement.
      const off = document.createElement('button');
      off.type = 'button'; off.className = 'ghost-button'; off.textContent = 'Send off command';
      off.addEventListener('click', () => sendDeveloperCommand(action, true));
      row.append(off);
    }
  } finally {
    if (session === developerSession) button.disabled = false;
  }
}

let analyticsAbort = null;
const FIELD_UNITS = {throttle:'%', brake:'%', bSOC:'%', bVoltage:'V', bTemp:'°C', bCurrent:'A', fRadar:'m', bRadar:'m', light:'lux'};
function analyticsExports(enabled) {
  for (const id of ['analytics-export-csv','analytics-export-json']) {
    const button = document.getElementById(id); if (button) button.disabled = !enabled;
  }
}
const ANALYTICS_HIDDEN_FIELDS = new Set(['timestamp','sequence','schema_version','checksum']);
const ANALYTICS_LABELS = {throttle:'Throttle', brake:'Brake', bSOC:'Battery SoC', bVoltage:'Battery Voltage', bTemp:'Battery Temp',
  bCurrent:'Battery Current', fRadar:'Front Radar', bRadar:'Rear Radar', light:'Ambient Light', rain:'Rain', bFault:'Battery Fault',
  lever:'Gear lever', cImu:'Front IMU (text)', rImu:'Rear IMU (text)'};
function analyticsLabel(name) {
  if (ANALYTICS_LABELS[name]) return ANALYTICS_LABELS[name];
  const t = name.replace(/[_-]+/g,' ').replace(/([a-z0-9])([A-Z])/g,'$1 $2').trim();
  return t.charAt(0).toUpperCase() + t.slice(1);
}
/* Fill the Field dropdown from what InfluxDB really holds. On any failure the
   existing static list stays, so the tab keeps working. */
async function refreshAnalyticsFields() {
  const select = document.getElementById('analytics-field-select');
  const range = document.getElementById('analytics-range-select').value;
  try {
    const response = await fetch(`/api/v1/telemetry/fields?range=${encodeURIComponent(range)}`, {headers:authHeaders()});
    if (!response.ok) return;
    const data = await response.json();
    const fields = (data.fields || []).filter(f => !ANALYTICS_HIDDEN_FIELDS.has(f));
    if (!fields.length) return;
    const current = select.value;
    select.replaceChildren(new Option('All fields (CSV only)', ''));
    for (const f of fields) select.add(new Option(analyticsLabel(f), f));
    if (fields.includes(current) || current === '') select.value = current;
    else select.value = fields.includes('bSOC') ? 'bSOC' : fields[0];
  } catch (error) { /* keep the static list */ }
}
async function loadAnalytics() {
  await refreshAnalyticsFields();
  analyticsAbort?.abort();
  const controller = new AbortController(); analyticsAbort = controller;
  const field = document.getElementById('analytics-field-select').value;
  const range = document.getElementById('analytics-range-select').value;
  const status = document.getElementById('analytics-status');
  const chart = document.getElementById('analytics-chart');
  window.__analyticsPoints = [];
  analyticsExports(false);
  chart.replaceChildren();
  status.textContent = 'Loading…';
  try {
    const query = new URLSearchParams({range}); if (field) query.set('field',field);
    const response = await fetch(`/api/v1/telemetry/history?${query}`, {headers:authHeaders(), signal:controller.signal});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `Unable to load history (${response.status}).`);
    if (controller.signal.aborted) return;
    const points = Array.isArray(data.points) ? data.points : [];
    window.__analyticsPoints = points;
    analyticsExports(points.length > 0);
    const limitNote = data.truncated ? ` Showing the newest ${data.limit.toLocaleString()} points; choose a shorter range for a complete CSV.` : '';
    if (!points.length) {status.textContent = 'No stored data for this field in this range. If every field is empty, check that Telegraf is writing the sensor fields (see infra/telegraf.conf).'; return;}
    const select = document.getElementById('analytics-field-select');
    const known = new Set(Array.from(select.options, o => o.value));
    for (const name of new Set(points.map(p => p.field))) {
      if (!known.has(name)) select.add(new Option(name,name));
    }
    if (!field) {status.textContent = `${points.length.toLocaleString()} points across all fields. Select a field to plot, or export CSV.${limitNote}`; return;}
    const numeric = points.map(p=>({t:Date.parse(p.time), v: typeof p.value === 'number' ? p.value : (typeof p.value === 'string' && p.value.trim() ? Number(p.value) : NaN)})).filter(p=>Number.isFinite(p.t)&&Number.isFinite(p.v)).sort((a,b)=>a.t-b.t);
    if (!numeric.length) {status.textContent = `${points.length} text readings. Export CSV to view these values.${limitNote}`; return;}
    drawAnalytics(chart, numeric, field);
    status.textContent = `${numeric.length.toLocaleString()} readings · ${select.selectedOptions[0].textContent} · ${document.getElementById('analytics-range-select').selectedOptions[0].textContent}.${limitNote}`;
  } catch(error) {
    if (error.name !== 'AbortError') status.textContent = error.message;
  }
}
function drawAnalytics(svg, points, field) {
  const w=900,h=310,left=86,right=25,top=40,bottom=60;
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  let min=Infinity,max=-Infinity;
  for (const point of points) {min=Math.min(min,point.v);max=Math.max(max,point.v);}
  const padding=(max-min)*.1 || Math.max(Math.abs(min)*.05,1); min-=padding; max+=padding;
  const t0=points[0].t,t1=points[points.length-1].t;
  const x=t=>left+(t1===t0?.5:(t-t0)/(t1-t0))*(w-left-right);
  const y=v=>h-bottom-(v-min)/(max-min)*(h-top-bottom);
  const el=(name,attrs,text)=>{const n=document.createElementNS('http://www.w3.org/2000/svg',name);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,v);if(text!==undefined)n.textContent=text;svg.append(n);return n;};
  for(let i=0;i<=4;i++) {
    const v=min+(max-min)*i/4,py=y(v);
    el('line',{x1:left,x2:w-right,y1:py,y2:py,stroke:'#315346','stroke-width':1});
    el('text',{x:left-12,y:py+4,fill:'#c7dfd3','font-size':12,'text-anchor':'end'},Number(v.toPrecision(4)).toString());
  }
  el('text',{x:left,y:22,fill:'#edfff6','font-size':14},`${field}${FIELD_UNITS[field] ? ` (${FIELD_UNITS[field]})` : ''}`);
  const label=t=>new Date(t).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
  el('text',{x:left,y:h-25,fill:'#c7dfd3','font-size':12},label(t0));
  if(t1!==t0) el('text',{x:w-right,y:h-25,fill:'#c7dfd3','font-size':12,'text-anchor':'end'},label(t1));
  // Preserve all points in CSV; reduce only SVG path density on long traces.
  const stride=Math.max(1,Math.ceil(points.length/2000));
  const plotted=points.filter((_,i)=>i%stride===0||i===points.length-1);
  el('path',{d:plotted.map((p,i)=>`${i?'L':'M'}${x(p.t).toFixed(2)},${y(p.v).toFixed(2)}`).join(' '),fill:'none',stroke:'#52ffc8','stroke-width':2});
  if(points.length===1)el('circle',{cx:x(t0),cy:y(points[0].v),r:4,fill:'#52ffc8'});
}
function exportAnalytics(format) {
  const points=window.__analyticsPoints || []; if(!points.length)return;
  // Quote commas/newlines/quotes, and neutralise spreadsheet formula prefixes.
  const cell=value=>{let s=String(value ?? '');if(typeof value==='string' && /^[=+@\-\t\r]/.test(s))s="'"+s;return '"'+s.replace(/"/g,'""')+'"';};
  const csv=['time,field,value',...points.map(p=>[p.time,p.field,p.value].map(cell).join(','))].join('\r\n');
  const url=URL.createObjectURL(new Blob([format==='csv'?csv:JSON.stringify(points,null,2)],{type:format==='csv'?'text/csv;charset=utf-8':'application/json'}));
  const a=document.createElement('a');a.href=url;a.download=`telemetry_history.${format}`;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
}

document.addEventListener('DOMContentLoaded',()=>{
  document.querySelectorAll('.remote-command').forEach(button=>button.addEventListener('click',()=>sendDeveloperCommand(button.dataset.action)));
  document.getElementById('gateway-message-form').addEventListener('submit',event=>{event.preventDefault();sendDeveloperCommand('message');});
});


/* Trips tab: if tools/sumo_route.py has produced static/data/trip_route.{svg,json},
   show that real simulated route instead of the schematic placeholder. */
async function loadSimulatedTrip() {
  try {
    const response = await fetch('/static/data/trip_route.json', {cache:'no-cache'});
    if (!response.ok) return;
    const t = await response.json();
    const set = (id, text) => {const el = document.getElementById(id); if (el) el.textContent = text;};
    set('trip-preview-title', `${t.from} → ${t.to}`);
    set('trip-preview-badge', 'SIMULATION');
    const note = document.getElementById('trip-preview-note'); if (note) note.hidden = true;
    set('trip-dt-label', 'Start'); set('trip-dd-date', t.start_time_label || '—');
    set('trip-dt-distance', 'Distance'); set('trip-dd-distance', `${t.distance_km} km`);
    set('trip-dt-speed', 'Average speed'); set('trip-dd-speed', `${t.avg_speed_kmh} km/h`);
    set('trip-dt-duration', 'Duration'); set('trip-dd-duration', t.duration_text);
    const figure = document.getElementById('trip-route-figure');
    const img = document.createElement('img');
    img.src = `/static/data/trip_route.svg?v=${encodeURIComponent(t.trace_points + '-' + t.distance_km)}`;
    img.alt = `Simulated route from ${t.from} to ${t.to}`;
    img.className = 'trip-route-sketch';
    figure.replaceChildren(img);
  } catch (error) { /* keep the placeholder */ }
}
document.addEventListener('DOMContentLoaded', loadSimulatedTrip);
