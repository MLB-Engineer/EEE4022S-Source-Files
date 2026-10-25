/* Same-origin WebSocket relay. Values expire independently after five seconds. */
const RAW_FIELDS = {
  cImu: [['stm-fimu-pitch',1,'pitch'],['stm-fimu-roll',1,'roll']],
  rImu: [['stm-rimu-pitch',1,'pitch'],['stm-rimu-roll',1,'roll']],
  fRadar: [['stm-fradar',2]], bRadar: [['stm-bradar',2]],
  throttle: [['stm-throttle',0]], brake: [['stm-brake',0]],
  lever: [['stm-lever',null]], light: [['stm-light',0]], rain: [['stm-rain',2]],
  bSOC: [['stm-batt-soc',0]], bVoltage: [['stm-batt-voltage',1]],
  bTemp: [['stm-batt-temp',1]], bCurrent: [['stm-batt-current',2]], bFault: [['stm-batt-fault',null]]
};
function num(value) {
  if (value === null || value === undefined || typeof value === 'boolean' || typeof value === 'object' || String(value).trim() === '') return null;
  const n = Number(value); return Number.isFinite(n) ? n : null;
}
function updateRawSensorCards(raw) {
  for (const [key, targets] of Object.entries(RAW_FIELDS)) {
    let value=raw[key];
    if (key === 'cImu' || key === 'rImu') {
      try { value=typeof value==='string'?JSON.parse(value):value; } catch { value=null; }
    }
    for (const [id,decimals,axis] of targets) {
      const element=document.getElementById(id); if(!element)continue;
      const v=axis?value?.[axis]:value;
      element.textContent=decimals===null?(v??'--'):(num(v)===null?'--':num(v).toFixed(decimals));
    }
  }
  const gps=raw.gps && typeof raw.gps==='object'?raw.gps:raw;
  const lat=num(gps.latitude??gps.lat),lon=num(gps.longitude??gps.lon??gps.lng);
  const location=document.getElementById('location-note');
  if(location) {
    const precision=document.body.dataset.role==='guest'?2:6;
    location.textContent=lat!==null&&lon!==null&&Math.abs(lat)<=90&&Math.abs(lon)<=180
      ? `${lat.toFixed(precision)}, ${lon.toFixed(precision)}`:'No live GPS coordinates available.';
  }
}

/* ---- Sensor auto-discovery ------------------------------------------------
 * The page only shows what the vehicle actually sends:
 *  - a known sensor row (see RAW_FIELDS) is shown once its field has appeared
 *    in the telemetry and is hidden again if the field has been absent for
 *    SENSOR_GONE_MS (a field that is present but briefly late just shows "--");
 *  - any field NOT listed in RAW_FIELDS gets a row in an auto-created
 *    "Other sensors" card, so adding a sensor on the gateway needs no website
 *    change. Add an entry to RAW_FIELDS only if you want a nicer label/unit. */
const SENSOR_GONE_MS = 60000;
const NON_SENSOR_KEYS = new Set(['timestamp','sequence','schema_version','checksum','lat','lon','lng','latitude','longitude','gps']);
function prettyFieldName(name) {
  const t = String(name).replace(/[_-]+/g,' ').replace(/([a-z0-9])([A-Z])/g,'$1 $2').trim();
  return t.charAt(0).toUpperCase()+t.slice(1);
}
function formatDynamicValue(v) {
  if (v === null || v === undefined || v === '') return '--';
  if (typeof v === 'number') return Number.isFinite(v) ? String(Number(v.toFixed(3))) : '--';
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}
function ensureOtherSensorsCard() {
  let card = document.getElementById('other-sensors-card');
  if (card) return card;
  const anchor = document.querySelector('[data-widget="location"]');
  if (!anchor) return null;
  card = document.createElement('article');
  card.className = 'widget-card sensor-hidden'; card.id = 'other-sensors-card'; card.dataset.widget = 'other-sensors';
  card.innerHTML = '<div class="widget-header"><span aria-hidden="true" class="widget-icon"><svg fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="1.6" viewbox="0 0 24 24"><circle cx="12" cy="12" r="3"></circle><path d="M12 3v3m0 12v3M3 12h3m12 0h3"></path></svg></span><div><h3>Other sensors</h3></div></div><div id="other-sensors-rows"></div>';
  anchor.parentNode.insertBefore(card, anchor);
  return card;
}
function updateSensorAvailability(mgr) {
  const now = Date.now();
  const seen = key => mgr.timestamps[key] !== undefined && now - mgr.timestamps[key] <= SENSOR_GONE_MS;
  // 1) known rows: show only the sensors that exist; hide cards with no rows left.
  for (const [key, targets] of Object.entries(RAW_FIELDS)) {
    const present = seen(key);
    for (const [id] of targets) {
      const row = document.getElementById(id)?.closest('.metric-row');
      if (row) row.classList.toggle('sensor-hidden', !present);
    }
  }
  document.querySelectorAll('[aria-label="Raw STM sensor feed"] > .widget-card').forEach(card => {
    if (card.dataset.widget === 'location' || card.id === 'other-sensors-card') return;
    const rows = card.querySelectorAll('.metric-row');
    if (!rows.length) return;
    card.classList.toggle('sensor-hidden', !Array.from(rows).some(r => !r.classList.contains('sensor-hidden')));
  });
  // 2) unknown fields -> auto-generated rows.
  const topKeys = Object.keys(mgr.readings).filter(k => !(k in RAW_FIELDS) && !k.startsWith('_') && !NON_SENSOR_KEYS.has(k) && seen(k)).sort();
  // Objects (e.g. {"fl":12.1,"fr":12.0}) are expanded one level into separate rows.
  const entries = [];
  for (const k of topKeys) {
    const v = mgr.readings[k];
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      for (const [sub, sv] of Object.entries(v)) entries.push({id:`${k}.${sub}`, label:`${prettyFieldName(k)} · ${prettyFieldName(sub)}`, live:mgr.isSensorLive(k), value:sv});
    } else entries.push({id:k, label:prettyFieldName(k), live:mgr.isSensorLive(k), value:v});
  }
  const unknown = entries.map(e => e.id);
  const card = unknown.length ? ensureOtherSensorsCard() : document.getElementById('other-sensors-card');
  if (!card) return;
  const holder = card.querySelector('#other-sensors-rows');
  const wanted = new Set(unknown);
  holder.querySelectorAll('.metric-row').forEach(r => { if (!wanted.has(r.dataset.field)) r.remove(); });
  for (const e of entries) {
    let row = Array.from(holder.children).find(r => r.dataset.field === e.id);
    if (!row) {
      row = document.createElement('div'); row.className = 'metric-row'; row.dataset.field = e.id; row.title = e.id;
      row.innerHTML = '<span></span><strong></strong>';
      row.firstChild.textContent = e.label;
      holder.append(row);
    }
    const text = e.live ? formatDynamicValue(e.value) : '--';
    const out = row.lastChild; if (out.textContent !== text) out.textContent = text;
  }
  card.classList.toggle('sensor-hidden', !unknown.length);
}
class SensorManager {
  constructor() {
    this.readings={}; this.timestamps={}; this.ws=null; this.connected=false;
    this.sensorTimeoutMs=5000; this.reconnectTimer=null; this.stopped=false;
    this.lastPacketAt=null;
    this.staleTimer=setInterval(()=>this._checkStaleSensors(),500);
    updateSensorAvailability(this); // nothing is shown until the vehicle actually sends it
  }
  connect() {
    if(this.ws)return;
    this.stopped=false;
    const protocol=location.protocol==='https:'?'wss:':'ws:';
    const ws=new WebSocket(`${protocol}//${location.host}/ws/telemetry`); this.ws=ws;
    ws.onopen=()=>{
      this.connected=true;
      // Tell the server who is watching: only Operator/Developer sessions receive exact location.
      try { const token=localStorage.getItem('tukzie_token'); if(token) ws.send(JSON.stringify({auth:token})); } catch {}
    };
    ws.onmessage=event=>{
      try {
        const packet=JSON.parse(event.data);
        if(!packet || typeof packet!=='object' || Array.isArray(packet))return;
        const now=Date.now(),serverNow=packet._server_time ?? now/1000;
        for(const [key,value] of Object.entries(packet)) {
          if(key.startsWith('_'))continue;
          const received=packet._field_received_at?.[key] ?? packet._received_at ?? serverNow;
          this.readings[key]=value;
          this.timestamps[key]=now-Math.max(0,serverNow-received)*1000;
        }
        if(Object.keys(this.readings).length) this.lastPacketAt=now-Math.max(0,serverNow-(packet._received_at??serverNow))*1000;
        const current=this.getAllReadings();
        updateRawSensorCards(current);
        // Keep the existing higher-level widgets for Operator and Guest.
        window.dispatchEvent(new CustomEvent('telemetry-update',{detail:{telemetry:{...current,_received_at:(this.lastPacketAt??0)/1000}}}));
        this._checkStaleSensors();
      } catch(error) { console.warn('Invalid telemetry packet:',error.message); }
    };
    ws.onclose=()=>{
      if(this.ws!==ws)return;
      this.ws=null;this.connected=false;
      if(!this.stopped)this.reconnectTimer=setTimeout(()=>this.connect(),2000);
    };
    ws.onerror=()=>ws.close();
  }
  disconnect() {
    this.stopped=true;clearTimeout(this.reconnectTimer);
    const ws=this.ws;this.ws=null;this.connected=false;ws?.close();
  }
  _checkStaleSensors() {
    updateRawSensorCards(this.getAllReadings());
    updateSensorAvailability(this);
    const age=this.lastPacketAt===null?null:Date.now()-this.lastPacketAt;
    const status=document.getElementById('feed-status');
    if(status) {
      const fresh=age!==null&&age<=this.sensorTimeoutMs;
      status.classList.toggle('is-live',fresh);
      status.textContent=fresh?'Live telemetry · updated just now':age===null?'Waiting for telemetry':`Telemetry stale · last received ${Math.floor(age/1000)} s ago`;
    }
    // Don't leave the header looking live after a publisher stops.
    if(age===null||age>this.sensorTimeoutMs) {
      for(const id of ['status-temp','status-battery']) {const el=document.getElementById(id);if(el)el.textContent='--';}
    }
  }
  reset() {this.readings={};this.timestamps={};this.lastPacketAt=null;updateRawSensorCards({});updateSensorAvailability(this);}
  getReading(key) {return this.isSensorLive(key)?this.readings[key]??null:null;}
  getAllReadings() {return Object.fromEntries(Object.entries(this.readings).filter(([key])=>this.isSensorLive(key)));}
  isConnected() {return this.connected;}
  isSensorLive(key) {return this.timestamps[key]!==undefined&&Date.now()-this.timestamps[key]<=this.sensorTimeoutMs;}
}
window.sensorManager=new SensorManager();
