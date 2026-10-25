const API_MODE = false;
const API_BASE = "/api/v1";
const MAX_SPEED_KMH = 41.9;
const GRAPH_WINDOW_MS = 60 * 60 * 1000; // 60 minutes, per the telemetry-graph spec
const GRAPH_HISTORY_RETENTION_MS = GRAPH_WINDOW_MS * 2; // keep a buffer past the window before pruning

// ---------------------------------------------------------------------------
// Trip-estimation constants (fixed by spec — never overridden by user input,
// sensor data, or historical values).
// ---------------------------------------------------------------------------
const FIXED_AVG_SPEED_KMH = 35;       // ETA is always distance / 35 km/h
const MAX_TRIP_DISTANCE_KM = 70;      // Vehicle's maximum operational range
const LIVE_DATA_TIMEOUT_MS = 5000;    // Trip module clears if live data lapses this long
const DISTANCE_LIMIT_MESSAGE = "Selected destination exceeds the vehicle's maximum operational range of 70 km.";

const GRAPH_CONFIG = {
  performance: { key: "performance", label: "Performance Analysis", unit: "", min: 0, max: 100, decimals: 0 },
  speed: { key: "speed", label: "Speed", unit: "km/h", min: 0, max: 42, decimals: 1 },
  distance: { key: "range", label: "Range", unit: "km", min: 0, max: 80, decimals: 1 },
  battery: { key: "soc", label: "State of Charge", unit: "%", min: 0, max: 100, decimals: 0 },
  power: { key: "power", label: "Power Consumption", unit: "W", min: 0, max: 3000, decimals: 0 },
  consumption: { key: "consumption", label: "Consumption", unit: "%/km", min: 0, max: 0.5, decimals: 2 },
};

const GRAPH_HISTORY_STORAGE_KEY = "tukzie_graph_history";

const graphHistory = Object.fromEntries(
  Object.keys(GRAPH_CONFIG).map((type) => [type, []])
);

const graphTimestamps = Object.fromEntries(
  Object.keys(GRAPH_CONFIG).map((type) => [type, []])
);

function saveGraphHistory() {
  if (typeof localStorage === "undefined") return;
  try {
    localStorage.setItem(GRAPH_HISTORY_STORAGE_KEY, JSON.stringify({
      history: graphHistory,
      timestamps: graphTimestamps,
    }));
  } catch (err) {
    console.warn("Unable to save graph history:", err);
  }
}

function loadGraphHistory() {
  if (typeof localStorage === "undefined") return;
  try {
    const raw = localStorage.getItem(GRAPH_HISTORY_STORAGE_KEY);
    if (!raw) return;
    const saved = JSON.parse(raw);
    if (!saved || typeof saved !== "object") return;

    Object.entries(saved.history || {}).forEach(([type, values]) => {
      if (!Array.isArray(values) || !graphHistory[type]) return;
      graphHistory[type].splice(0, graphHistory[type].length, ...values.filter((v) => typeof v === "number" && Number.isFinite(v)));
    });
    Object.entries(saved.timestamps || {}).forEach(([type, values]) => {
      if (!Array.isArray(values) || !graphTimestamps[type]) return;
      graphTimestamps[type].splice(0, graphTimestamps[type].length, ...values.filter((v) => typeof v === "number" && Number.isFinite(v)));
    });
  } catch (err) {
    console.warn("Unable to load saved graph history:", err);
  }
}

const state = {
  activeUser: { role: "guest", code: "A", label: "Guest" },
  pendingRole: null,
  telemetry: {
    speed: null,
    throttle: null,
    brake: null,
    soc: null,
    range: null,
    consumption: null,
    ambientTemp: null,
    batteryHealth: null,
    motorTemp: null,
    tyrePressure: null,
    vehicleLoad: null,
    voltage: null,
    coreTemp: null,
    driveMode: null,
    power: null,
  },
  tripDistance: 22,
  chartType: "performance",
  navDistanceKm: null, // set when a distance is sourced from the Navigation widget
};

function createEmptyTelemetry() {
  return {
    speed: null,
    throttle: null,
    brake: null,
    soc: null,
    range: null,
    consumption: null,
    ambientTemp: null,
    batteryHealth: null,
    motorTemp: null,
    tyrePressure: null,
    vehicleLoad: null,
    voltage: null,
    coreTemp: null,
    driveMode: null,
    power: null,
  };
}

const roleConfig = {
  guest: { code: "A", label: "Guest" },
  operator: { code: "B", label: "Operator" },
  developer: { code: "C", label: "Developer" },
};

// ---------------------------------------------------------------------------
// PROGRAM 9 ASIS - PRE-DRIVE SIMULATION (Estimated Trip widget)
// Vehicle constants below are fixed configuration for the Jinpeng CH-TK1
// Cargo Tuk-Tuk and are not user-editable.
// ---------------------------------------------------------------------------
const VEHICLE = {
  model: "Jinpeng CH-TK1 Cargo Tuk-Tuk",
  curbMassKg: 350,
  ratedPayloadKg: 500,
  batteryVoltageV: 72,
  batteryCapacityAh: 100,
  batteryEnergyWh: 7200,
  motorPowerW: 3000,
  maxSpeedKmh: 40,
  frontalAreaM2: 2.0,
  dragCoefficient: 0.70,
  drivetrainEfficiency: 0.84,
  regenEfficiency: 0.45,
  maxRegenW: 1200,
  accessoryPowerW: 180,
  internalResistanceOhm: 0.055,
  passengerMassKg: 72,
};

const SURFACE_CRR = {
  asphalt: 0.015,
  cobblestone: 0.030,
  gravel: 0.035,
  dirt: 0.050,
};

// (rolling resistance multiplier, headwind m/s, air density multiplier, ETA delay %)
const WEATHER_MODEL = {
  clear: { crrMult: 1.00, headwindMs: 0, airDensityMult: 1.00, delayPct: 0.05 },
  rain: { crrMult: 1.20, headwindMs: 2.0, airDensityMult: 1.02, delayPct: 0.10 },
  wet: { crrMult: 1.25, headwindMs: 0, airDensityMult: 1.00, delayPct: 0.15 },
  windy: { crrMult: 1.00, headwindMs: 8.5, airDensityMult: 1.00, delayPct: 0.08 },
};

// Fallbacks only. The form now validates before it will calculate, and the
// live-backed fields (SOC, battery temperature, cargo weight, average speed) are
// seeded from telemetry when the modal opens -- so these are the last resort if
// a control is missing entirely, not the values a driver normally sees.
const TRIP_ESTIMATE_DEFAULTS = {
  startingSoc: 100,       // a full pack
  passengers: 1,          // the driver
  cargoKg: 0,             // assume nothing loaded until the sensor says otherwise
  weather: "clear",
  batteryTempC: 24,       // ~ambient
  distanceKm: 0,          // no assumed route; the driver must enter one
  elevationGainM: 0,      // flat
  elevationLossM: 0,      // flat
  surface: "asphalt",
  avgSpeedKmh: 25,        // a sensible urban cruise for this vehicle
};

// Advice thresholds. Not explicitly fixed by the vehicle spec, so ASIS
// mirrors the mechanical team's "warn at ~85-90% of a nameplate limit"
// convention used for structural/payload margins.
const LOW_SOC_RESERVE_PCT = 20;
const HEAVY_LOAD_PAYLOAD_FRACTION = 0.85;
const STEEP_GRADE_PCT = 4;
const AMBIENT_REF_C = 24; // reference ambient used for the passive-cooling estimate
const BATTERY_THERMAL_MASS_J_PER_C = 16200;

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function getTripInputs() {
  const parseNum = (id, fallback) => {
    const element = $(id);
    if (!element) return fallback;
    const num = Number(element.value);
    return Number.isFinite(num) ? num : fallback;
  };
  const parseSelect = (id, fallback) => {
    const element = $(id);
    return element && element.value ? element.value : fallback;
  };

  return {
    startingSoc: clamp(parseNum("#trip-soc-input", TRIP_ESTIMATE_DEFAULTS.startingSoc), 0, 100),
    passengers: clamp(Math.round(parseNum("#trip-passengers-input", TRIP_ESTIMATE_DEFAULTS.passengers)), 0, 3),
    cargoKg: clamp(parseNum("#trip-cargo-input", TRIP_ESTIMATE_DEFAULTS.cargoKg), 0, 500),
    weather: parseSelect("#trip-weather-input", TRIP_ESTIMATE_DEFAULTS.weather),
    batteryTempC: parseNum("#trip-battery-temp-input", TRIP_ESTIMATE_DEFAULTS.batteryTempC),
    // Clamped to the vehicle's 70 km maximum operational range. Values beyond
    // that are rejected by validateTripForm() before a calculation ever runs.
    distanceKm: clamp(Math.max(0, parseNum("#trip-distance-input", TRIP_ESTIMATE_DEFAULTS.distanceKm)), 0, MAX_TRIP_DISTANCE_KM),
    elevationGainM: Math.max(0, parseNum("#trip-elevation-gain-input", TRIP_ESTIMATE_DEFAULTS.elevationGainM)),
    elevationLossM: Math.max(0, parseNum("#trip-elevation-loss-input", TRIP_ESTIMATE_DEFAULTS.elevationLossM)),
    surface: parseSelect("#trip-surface-input", TRIP_ESTIMATE_DEFAULTS.surface),
    // Fixed by spec: never read from the input, live telemetry, or history.
    avgSpeedKmh: FIXED_AVG_SPEED_KMH,
  };
}

// ETA and Remaining Range at Arrival are always derived from the fixed 35 km/h
// speed and the 70 km maximum range — independent of the physics-based energy
// simulation below, which still drives arrival SOC / energy-use estimates.
function computeFixedTripMetrics(distanceKm) {
  const clampedDistance = clamp(distanceKm, 0, MAX_TRIP_DISTANCE_KM);
  const etaHours = clampedDistance / FIXED_AVG_SPEED_KMH;
  const remainingRangeKm = Math.max(0, MAX_TRIP_DISTANCE_KM - clampedDistance);
  return { etaHours, remainingRangeKm };
}

// Core Program 9 battery-power model. Pure function of inputs so it can be
// re-run with modified inputs to isolate the impact of weather/terrain/cargo.
function simulateProgram9Trip(inputs) {
  const weather = WEATHER_MODEL[inputs.weather] || WEATHER_MODEL.clear;
  const surfaceCrrBase = SURFACE_CRR[inputs.surface] ?? SURFACE_CRR.asphalt;
  const crr = surfaceCrrBase * weather.crrMult;

  const totalMassKg = VEHICLE.curbMassKg + inputs.passengers * VEHICLE.passengerMassKg + inputs.cargoKg;

  const vehicleSpeedMs = inputs.avgSpeedKmh / 3.6;
  const airSpeedMs = Math.max(0, vehicleSpeedMs + weather.headwindMs);

  const distanceM = inputs.distanceKm * 1000;
  // Net average grade across the whole route (positive = net climb).
  const grade = distanceM > 0 ? (inputs.elevationGainM - inputs.elevationLossM) / distanceM : 0;

  const rollingForceN = crr * totalMassKg * 9.81;
  const gradeForceN = totalMassKg * 9.81 * grade;
  const dragForceN = 0.5 * 1.225 * weather.airDensityMult * VEHICLE.dragCoefficient * VEHICLE.frontalAreaM2 * airSpeedMs * airSpeedMs;

  const rollingPowerW = rollingForceN * vehicleSpeedMs;
  const gradePowerW = gradeForceN * vehicleSpeedMs;
  const dragPowerW = dragForceN * vehicleSpeedMs;
  const rawMechanicalPowerW = rollingPowerW + gradePowerW + dragPowerW;
  // The motor cannot deliver more than its rated power; demand beyond that
  // is a real limitation (vehicle will not sustain the requested average
  // speed under this load/grade), flagged separately as motorLimited.
  const motorLimited = rawMechanicalPowerW > VEHICLE.motorPowerW;
  const mechanicalPowerW = Math.min(rawMechanicalPowerW, VEHICLE.motorPowerW);

  let batteryPowerW;
  let regenPowerW = 0;
  if (mechanicalPowerW >= 0) {
    batteryPowerW = mechanicalPowerW / VEHICLE.drivetrainEfficiency + VEHICLE.accessoryPowerW;
  } else {
    // Net-descending route: mechanical power is negative, so regenerative
    // braking recovers a portion of it instead of drawing motor power.
    regenPowerW = Math.min(Math.abs(mechanicalPowerW) * VEHICLE.regenEfficiency, VEHICLE.maxRegenW);
    batteryPowerW = VEHICLE.accessoryPowerW - regenPowerW;
  }

  const baseTripHours = inputs.avgSpeedKmh > 0 ? inputs.distanceKm / inputs.avgSpeedKmh : 0;
  const tripHours = baseTripHours * (1 + weather.delayPct);
  const tripSeconds = tripHours * 3600;

  const energyUsedWh = batteryPowerW * tripHours;
  const availableEnergyWh = VEHICLE.batteryEnergyWh * (inputs.startingSoc / 100);
  const remainingEnergyWh = availableEnergyWh - energyUsedWh;
  const arrivalSocPct = clamp((remainingEnergyWh / VEHICLE.batteryEnergyWh) * 100, 0, 100);

  const energyPerKmWh = inputs.distanceKm > 0 ? energyUsedWh / inputs.distanceKm : 0;
  let remainingRangeKm;
  if (inputs.distanceKm <= 0) {
    remainingRangeKm = 0; // no route distance configured yet
  } else if (energyPerKmWh > 0) {
    remainingRangeKm = clamp(Math.max(0, remainingEnergyWh) / energyPerKmWh, 0, 999);
  } else {
    // Net-regenerative trip (more downhill than up): this rate can't be
    // extrapolated forward since regen needs a descent to draw from.
    // Fall back to the same load/weather/speed on flat ground instead.
    const flatRate = simulateProgram9Trip({ ...inputs, elevationGainM: 0, elevationLossM: 0 }).energyPerKmWh;
    remainingRangeKm = flatRate > 0 ? clamp(Math.max(0, remainingEnergyWh) / flatRate, 0, 999) : 999;
  }

  // Battery temperature: I^2R heating from the average battery current,
  // offset by passive cooling toward a reference ambient temperature.
  const avgAmps = VEHICLE.batteryVoltageV > 0 ? Math.abs(batteryPowerW) / VEHICLE.batteryVoltageV : 0;
  const i2rHeatW = avgAmps * avgAmps * VEHICLE.internalResistanceOhm;
  const coolingW = 0.35 * Math.max(0, inputs.batteryTempC - AMBIENT_REF_C);
  const netThermalW = i2rHeatW - coolingW;
  const batteryTempChangeC = (netThermalW * tripSeconds) / BATTERY_THERMAL_MASS_J_PER_C;
  const batteryTempArrivalC = clamp(inputs.batteryTempC + batteryTempChangeC, -10, 90);

  // Breakdown: share of total (unsigned) mechanical force contribution.
  const toWh = (powerW) => (powerW / VEHICLE.drivetrainEfficiency) * tripHours;
  const rollingWh = toWh(rollingPowerW);
  const gradeWh = toWh(gradePowerW);
  const dragWh = toWh(dragPowerW);
  const accessoryWh = VEHICLE.accessoryPowerW * tripHours;
  const breakdownTotal = Math.abs(rollingWh) + Math.abs(gradeWh) + Math.abs(dragWh) + Math.abs(accessoryWh);

  return {
    inputs,
    totalMassKg,
    grade,
    mechanicalPowerW,
    motorLimited,
    batteryPowerW,
    regenPowerW,
    tripHours,
    tripSeconds,
    energyUsedWh,
    arrivalSocPct,
    energyPerKmWh,
    remainingRangeKm,
    batteryTempChangeC,
    batteryTempArrivalC,
    breakdown: {
      rollingWh, gradeWh, dragWh, accessoryWh, total: breakdownTotal,
    },
  };
}

function computeTripImpacts(inputs, baseResult) {
  const pctDelta = (base, alt) => (alt !== 0 ? ((base - alt) / Math.abs(alt)) * 100 : 0);

  const clearResult = simulateProgram9Trip({ ...inputs, weather: "clear" });
  const flatResult = simulateProgram9Trip({ ...inputs, elevationGainM: 0, elevationLossM: 0 });
  const noCargoResult = simulateProgram9Trip({ ...inputs, cargoKg: 0 });

  return {
    weatherImpactPct: pctDelta(baseResult.energyUsedWh, clearResult.energyUsedWh),
    terrainImpactPct: pctDelta(baseResult.energyUsedWh, flatResult.energyUsedWh),
    cargoImpactPct: pctDelta(baseResult.energyUsedWh, noCargoResult.energyUsedWh),
  };
}

function buildDriverAdvice(inputs, result) {
  const advice = [];

  if (result.arrivalSocPct < LOW_SOC_RESERVE_PCT) {
    advice.push("Arrival battery below safe reserve. Recharge before departure or reduce speed.");
  }

  if (result.motorLimited) {
    advice.push("Power demand exceeds the motor's rated output for this load and grade. Actual speed will fall below the requested average.");
  }

  const payloadKg = inputs.passengers * VEHICLE.passengerMassKg + inputs.cargoKg;
  if (payloadKg >= VEHICLE.ratedPayloadKg * HEAVY_LOAD_PAYLOAD_FRACTION) {
    advice.push("Vehicle mass is significantly increasing energy consumption.");
  }

  if (inputs.weather === "rain") {
    advice.push("Wet conditions detected. Increase following distance and reduce speed.");
  }

  if (inputs.weather === "windy") {
    advice.push("Headwind is increasing aerodynamic drag. Lower speeds may improve range.");
  }

  const distanceM = inputs.distanceKm * 1000;
  const climbGradePct = distanceM > 0 ? (inputs.elevationGainM / distanceM) * 100 : 0;
  if (climbGradePct >= STEEP_GRADE_PCT) {
    advice.push("Elevation gain is contributing heavily to battery consumption.");
  }

  if (advice.length === 0) {
    advice.push("Conditions look nominal. No special precautions flagged for this trip.");
  }

  return advice;
}

function openTripSimulationModal() {
  if (!state.activeUser || !["B", "C"].includes(state.activeUser.code)) return;
  closeModals();
  openModal("#trip-modal");
}

// ---------------------------------------------------------------------------
// Navigation widget (popup/modal): destination search + map, restricted to
// South African locations, feeding the Estimated Trip widget's route
// distance automatically so manual entry can be avoided.
// ---------------------------------------------------------------------------
const SOUTH_AFRICA_BOUNDS = { south: -34.9, west: 16.3, north: -22.1, east: 32.95 };
// Vehicle's current/depot location used as the route origin for distance
// calculations. Defaults to Cape Town, within the South African bounds above.
const VEHICLE_ORIGIN = { lat: -33.9249, lng: 18.4241 };

const navState = {
  map: null,
  originMarker: null,
  destinationMarker: null,
  routeLine: null,
  selected: null, // { name, lat, lng, distanceKm }
};

function haversineDistanceKm(lat1, lon1, lat2, lon2) {
  const R = 6371;
  const toRad = (deg) => (deg * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a = Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return R * (2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a)));
}

function initNavMap() {
  if (navState.map || typeof L === "undefined") return;
  navState.map = L.map("nav-map-container").setView([VEHICLE_ORIGIN.lat, VEHICLE_ORIGIN.lng], 12);
  L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
    attribution: "© OpenStreetMap contributors",
  }).addTo(navState.map);
  navState.originMarker = L.marker([VEHICLE_ORIGIN.lat, VEHICLE_ORIGIN.lng]).addTo(navState.map).bindPopup("Vehicle location");

  // Allow tapping the map directly to select a destination.
  navState.map.on("click", (e) => {
    setNavDestination({
      name: `Map location (${e.latlng.lat.toFixed(4)}, ${e.latlng.lng.toFixed(4)})`,
      lat: e.latlng.lat,
      lng: e.latlng.lng,
    });
  });
}

function setNavDestination({ name, lat, lng }) {
  const distanceKm = haversineDistanceKm(VEHICLE_ORIGIN.lat, VEHICLE_ORIGIN.lng, lat, lng);
  navState.selected = { name, lat, lng, distanceKm };

  if (navState.destinationMarker) navState.map.removeLayer(navState.destinationMarker);
  if (navState.routeLine) navState.map.removeLayer(navState.routeLine);

  navState.destinationMarker = L.marker([lat, lng]).addTo(navState.map).bindPopup(name).openPopup();
  navState.routeLine = L.polyline(
    [[VEHICLE_ORIGIN.lat, VEHICLE_ORIGIN.lng], [lat, lng]],
    { color: "#39f4ff", weight: 4, dashArray: "6, 6" }
  ).addTo(navState.map);
  navState.map.fitBounds(navState.routeLine.getBounds(), { padding: [30, 30] });

  setText("#nav-selected-name", name);
  setText("#nav-selected-distance", `${distanceKm.toFixed(1)} km`);
  $("#nav-selection-summary").style.display = "flex";

  const distanceError = $("#nav-distance-error");
  const confirmButton = $("#nav-confirm-button");
  if (distanceKm > MAX_TRIP_DISTANCE_KM) {
    if (distanceError) distanceError.textContent = DISTANCE_LIMIT_MESSAGE;
    if (confirmButton) confirmButton.disabled = true;
  } else {
    if (distanceError) distanceError.textContent = "";
    if (confirmButton) confirmButton.disabled = false;
  }
}

async function geocodeNavSearch() {
  const input = $("#nav-search-input");
  const status = $("#nav-search-status");
  const resultsList = $("#nav-search-results");
  if (!input || !resultsList) return;

  const query = input.value.trim();
  if (!query) return;

  if (status) status.textContent = "Searching…";
  resultsList.innerHTML = "";

  try {
    // Address search restricted and optimized for South African locations only.
    const url = `https://nominatim.openstreetmap.org/search?format=json&countrycodes=za&addressdetails=1&limit=6&q=${encodeURIComponent(query)}`;
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    const results = await response.json();

    if (!Array.isArray(results) || results.length === 0) {
      if (status) status.textContent = "No South African locations found for that search.";
      return;
    }

    if (status) status.textContent = "";
    results.forEach((place) => {
      const li = document.createElement("li");
      li.textContent = place.display_name;
      li.setAttribute("role", "option");
      li.addEventListener("click", () => {
        $$(".nav-search-results li").forEach((el) => el.classList.remove("is-selected"));
        li.classList.add("is-selected");
        setNavDestination({ name: place.display_name, lat: Number(place.lat), lng: Number(place.lon) });
      });
      resultsList.appendChild(li);
    });
  } catch (err) {
    if (status) status.textContent = "Search failed. Check your connection and try again.";
  }
}

function openNavigationModal() {
  openModal("#navigation-modal");
  setTimeout(() => {
    initNavMap();
    if (navState.map) navState.map.invalidateSize();
  }, 80);
}

// ---------------------------------------------------------------------------
// Navigation widget TILE: a small always-visible live map (vehicle location),
// shown on the dashboard before any popup is opened, plus a lightweight trip
// history view. Destination search still lives in the navigation-modal popup.
// ---------------------------------------------------------------------------
const navTileState = {
  map: null,
  vehicleMarker: null,
  historyLines: [],
};

function initNavTileMap() {
  if (navTileState.map || typeof L === "undefined") return;
  const container = document.getElementById("nav-tile-map");
  if (!container) return;

  navTileState.map = L.map("nav-tile-map", { zoomControl: false }).setView([VEHICLE_ORIGIN.lat, VEHICLE_ORIGIN.lng], 13);
  L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
    attribution: "© OpenStreetMap contributors",
  }).addTo(navTileState.map);
  navTileState.vehicleMarker = L.marker([VEHICLE_ORIGIN.lat, VEHICLE_ORIGIN.lng])
    .addTo(navTileState.map)
    .bindPopup("Current Location")
    .openPopup();
}

// Mirrors the "Live Only / 24h / 7d" selector: swaps the badge and (for demo
// purposes, since there is no history API yet) sketches representative past
// routes so the control is fully interactive.
function changeNavTileRange() {
  const select = $("#nav-tile-range-select");
  const badge = $("#nav-tile-live-badge");
  if (!select || !navTileState.map) return;
  const range = select.value;

  navTileState.historyLines.forEach((line) => navTileState.map.removeLayer(line));
  navTileState.historyLines = [];

  if (range === "live") {
    if (badge) badge.textContent = "Live";
    navTileState.map.setView([VEHICLE_ORIGIN.lat, VEHICLE_ORIGIN.lng], 13);
    return;
  }

  if (badge) badge.textContent = range === "24h" ? "24h History" : "7d History";

  const recentTrip = [[-33.9249, 18.4241], [-33.9300, 18.4400], [-33.9500, 18.4600]];
  const weekTrip = [[-33.9000, 18.4000], [-33.9200, 18.4100], ...recentTrip];
  const coords = range === "24h" ? recentTrip : weekTrip;

  const line = L.polyline(coords, { color: "#39f4ff", weight: 4 }).addTo(navTileState.map);
  navTileState.historyLines.push(line);
  navTileState.map.fitBounds(line.getBounds(), { padding: [20, 20] });
}

// Static demo trip log for the My Trips modal (no history API yet).
const MY_TRIPS_LOG = [
  { when: "Today, 09:30 AM", route: "UCT Upper Campus → Observatory", distanceKm: 4.2, durationMin: 14, avgSpeedKmh: 18 },
  { when: "Yesterday, 14:15 PM", route: "Observatory → Cape Town CBD", distanceKm: 6.8, durationMin: 22, avgSpeedKmh: 19 },
  { when: "Mon, 08:00 AM", route: "Cape Town CBD → Camps Bay", distanceKm: 8.5, durationMin: 25, avgSpeedKmh: 20 },
];

function renderMyTripsTable() {
  const body = $("#my-trips-table-body");
  if (!body) return;
  body.innerHTML = MY_TRIPS_LOG.map((trip) => `
    <tr>
      <td>${trip.when}</td>
      <td>${trip.route}</td>
      <td>${trip.distanceKm.toFixed(1)} km</td>
      <td>${trip.durationMin} min</td>
      <td>${trip.avgSpeedKmh} km/h</td>
    </tr>
  `).join("");

  const total = MY_TRIPS_LOG.reduce((sum, trip) => sum + trip.distanceKm, 0);
  setText("#trips-total-distance", `${total.toFixed(1)} km`);
}

function openMyTripsModal() {
  renderMyTripsTable();
  openModal("#my-trips-modal");
}

function exportMyTripsCsv() {
  const header = "Date & Time,Route Area,Distance (km),Duration (min),Avg Speed (km/h)\n";
  const rows = MY_TRIPS_LOG.map((t) => `"${t.when}","${t.route}",${t.distanceKm},${t.durationMin},${t.avgSpeedKmh}`).join("\n");
  const blob = new Blob([header + rows], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "tukzie-trip-history.csv";
  link.click();
  URL.revokeObjectURL(url);
}

// The navigation-derived distance takes precedence over manual entry: while
// active, the Route distance field is locked and clearly labelled.
function confirmNavDestination() {
  if (!navState.selected || navState.selected.distanceKm > MAX_TRIP_DISTANCE_KM) return;

  const distanceKm = Math.round(navState.selected.distanceKm * 10) / 10;
  state.navDistanceKm = distanceKm;

  const distanceInput = $("#trip-distance-input");
  if (distanceInput) {
    distanceInput.value = distanceKm;
    distanceInput.readOnly = true;
  }
  const sourceTag = $("#trip-distance-source-tag");
  if (sourceTag) sourceTag.textContent = "from navigation";
  const clearButton = $("#trip-clear-nav-button");
  if (clearButton) clearButton.style.display = "block";

  closeModals();
  openModal("#trip-modal");
  validateTripForm();
  refreshTripEstimate();
}

function clearNavigationDistanceSource() {
  state.navDistanceKm = null;
  const distanceInput = $("#trip-distance-input");
  if (distanceInput) distanceInput.readOnly = false;
  const sourceTag = $("#trip-distance-source-tag");
  if (sourceTag) sourceTag.textContent = "manual";
  const clearButton = $("#trip-clear-nav-button");
  if (clearButton) clearButton.style.display = "none";
}

function applyTripOptionValues() {
  refreshTripEstimate();
  closeModals();
}

class TelemetryService {
  async getTelemetry() {
    return {
      speed: 0,
      throttle: 0,
      brake: 0,
      soc: 81,
      range: 69.7,
      consumption: 0.18,
      ambientTemp: 24,
      batteryHealth: 91,
      motorTemp: 41,
      tyrePressure: 220,
      vehicleLoad: 320,
      voltage: 358,
      coreTemp: 48,
      driveMode: "D",
    };
  }
}

const telemetryService = new TelemetryService();

// --- UI HELPERS ---
const $ = (selector, parent = document) => parent.querySelector(selector);
const $$ = (selector, parent = document) => Array.from(parent.querySelectorAll(selector));

function setText(selector, value) {
  const element = $(selector);
  if (element) element.textContent = value;
}

function parseTelemetryTimestamp(payload) {
  if (!payload || typeof payload !== "object") return null;
  const candidate = payload.timestamp ?? payload.ts ?? payload.time ?? payload.time_ms ?? payload.timeMs;
  if (candidate === null || candidate === undefined) return null;

  if (typeof candidate === "number" && Number.isFinite(candidate)) {
    return candidate < 1e12 ? candidate * 1000 : candidate;
  }
  if (typeof candidate === "string") {
    const numeric = Number(candidate);
    if (Number.isFinite(numeric)) {
      return numeric < 1e12 ? numeric * 1000 : numeric;
    }
    const parsed = Date.parse(candidate);
    if (!Number.isNaN(parsed)) return parsed;
  }
  return null;
}

function mapSensorPayload(payload) {
  const mapping = {
    sensor_1: "speed",
    sensor_2: "throttle",
    sensor_3: "brake",
    bms_soc: "soc",
    sensor_5: "range",
    sensor_6: "consumption",
    sensor_7: "ambientTemp",
    sensor_8: "batteryHealth",
    sensor_9: "motorTemp",
    sensor_10: "tyrePressure",
    sensor_11: "vehicleLoad",
    bms_voltage: "voltage",
    bms_temp_t1: "coreTemp",
    sensor_15: "driveMode",
    // Real gateway schema (gateway/schema.py's TelemetryPacket) — this
    // is the one seam in app.js that translates whatever's on the wire
    // into the internal camelCase model below, so it's extended here
    // rather than duplicated elsewhere. Only fields with a genuine
    // real-sensor correspondence are mapped (throttle, brake, SoC,
    // voltage, battery temp). Anything with no real sensor yet (speed,
    // range, consumption, ambientTemp, motorTemp, tyrePressure,
    // vehicleLoad, batteryHealth) is deliberately left unmapped so the
    // UI honestly shows "--" instead of a fabricated number.
    throttle: "throttle",
    brake: "brake",
    bSOC: "soc",
    bVoltage: "voltage",
    bTemp: "coreTemp",
  };

  const mappedData = Object.entries(payload).reduce((acc, [sensorId, value]) => {
    if (mapping[sensorId]) {
      // Real-schema values arrive as numeric strings (e.g. "16.2") —
      // convert those to numbers so downstream math (graphs, trip
      // estimates) works the same as it already did for the old
      // sensor_N naming.
      const numeric = typeof value === "string" && value.trim() !== "" && !Number.isNaN(Number(value)) ? Number(value) : value;
      acc[mapping[sensorId]] = numeric;
    }
    return acc;
  }, {});

  // "lever" (gear lever) is the real-sensor equivalent of driveMode,
  // but needs a value conversion rather than a straight rename.
  if (payload.lever !== undefined) {
    mappedData.driveMode = payload.lever === "engaged" ? "D" : "N";
  }

  const timestamp = parseTelemetryTimestamp(payload);
  if (timestamp !== null) {
    mappedData.__timestamp = timestamp;
  }
  return mappedData;
}

function pushGraphValue(type, value, timestamp = null) {
  const history = graphHistory[type];
  const timestamps = graphTimestamps[type];
  if (!history || !timestamps) return;
  if (typeof value !== "number" || !Number.isFinite(value)) return;

  const ts = timestamp !== null && Number.isFinite(timestamp) ? timestamp : Date.now();
  history.push(value);
  timestamps.push(ts);
  saveGraphHistory();

  // Prune anything older than the retention buffer (window + slack) so memory
  // doesn't grow forever, without discarding points that are still on-screen.
  const cutoff = ts - GRAPH_HISTORY_RETENTION_MS;
  while (timestamps.length && timestamps[0] < cutoff) {
    timestamps.shift();
    history.shift();
  }
}

function updateGraphHistory(values, timestamp = null) {
  Object.entries(values).forEach(([type, value]) => {
    if (graphHistory[type]) {
      pushGraphValue(type, value, timestamp);
    }
  });
  renderGraph();
}

function formatGraphValue(value, config) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return `-- ${config.unit}`.trim();
  }
  return `${value.toFixed(config.decimals)} ${config.unit}`.trim();
}

// ---------------------------------------------------------------------------
// Performance Analysis radar chart. Each axis is a 0-100 score, 100 = ideal,
// following the tuk-tuk radar-chart methodology (every axis normalized
// against a target/ideal reference). Two of the commonly-referenced radar
// metrics are intentionally left out because this vehicle's telemetry can't
// support them honestly:
//   - Stability needs gyroscope/accelerometer (roll/pitch/yaw) data that
//     isn't broadcast on the telemetry topic at all.
//   - Braking Performance needs a measured stopping distance for a full
//     braking event, which isn't something a single brake-position reading
//     can reconstruct.
// Route Efficiency is also left out: there's no independent "optimal path"
// measurement in the current data model to compare an actual route against.
// ---------------------------------------------------------------------------
const RADAR_METRICS = ["speedEfficiency", "energyEfficiency", "drivingSmoothness", "reliability", "payloadUtilization"];
const RADAR_LABELS = {
  speedEfficiency: "Speed Efficiency",
  energyEfficiency: "Energy Efficiency",
  drivingSmoothness: "Driving Smoothness",
  reliability: "Reliability",
  payloadUtilization: "Payload Utilization",
};

const IDEAL_CONSUMPTION_PCT_PER_KM = 0.15; // benchmark "excellent" reading on the 0-0.5 %/km consumption scale
const SMOOTH_JERK_REFERENCE_KMH_PER_S = 3; // speed-change rate treated as "aggressive" driving
const MOTOR_TEMP_WARN_C = 85;
const MOTOR_TEMP_CRITICAL_C = 100;
const CORE_TEMP_WARN_C = 55;
const CORE_TEMP_CRITICAL_C = 65;

function clampScore(value) {
  return Math.min(100, Math.max(0, value));
}

// Speed Efficiency = 100 - (|target - actual| / target * 100)
function computeSpeedEfficiency(t) {
  if (typeof t.speed !== "number" || !Number.isFinite(t.speed)) return null;
  const target = VEHICLE.maxSpeedKmh;
  return clampScore(100 - (Math.abs(target - t.speed) / target) * 100);
}

// Energy Efficiency: consumption (%/km) scored against an ideal benchmark.
function computeEnergyEfficiency(t) {
  if (typeof t.consumption !== "number" || !Number.isFinite(t.consumption)) return null;
  if (t.consumption <= 0) return 100;
  return clampScore((IDEAL_CONSUMPTION_PCT_PER_KM / t.consumption) * 100);
}

// Driving Smoothness proxy: average rate of speed change (km/h per second)
// across the most recent samples, standing in for accelerometer-based jerk.
function computeDrivingSmoothness() {
  const history = graphHistory.speed;
  const timestamps = graphTimestamps.speed;
  if (history.length < 2) return null;
  const sampleCount = Math.min(8, history.length);
  const recentValues = history.slice(-sampleCount);
  const recentTimes = timestamps.slice(-sampleCount);
  let totalRate = 0;
  let pairs = 0;
  for (let i = 1; i < recentValues.length; i++) {
    const dtSeconds = (recentTimes[i] - recentTimes[i - 1]) / 1000;
    if (dtSeconds <= 0) continue;
    totalRate += Math.abs(recentValues[i] - recentValues[i - 1]) / dtSeconds;
    pairs += 1;
  }
  if (pairs === 0) return null;
  const avgRate = totalRate / pairs;
  return clampScore(100 - (avgRate / SMOOTH_JERK_REFERENCE_KMH_PER_S) * 100);
}

// Reliability: penalize motor/battery temperatures past safe thresholds.
function computeReliability(t) {
  const hasMotorTemp = typeof t.motorTemp === "number" && Number.isFinite(t.motorTemp);
  const hasCoreTemp = typeof t.coreTemp === "number" && Number.isFinite(t.coreTemp);
  if (!hasMotorTemp && !hasCoreTemp) return null;

  let penalty = 0;
  if (hasMotorTemp) {
    if (t.motorTemp > MOTOR_TEMP_CRITICAL_C) penalty += 40;
    else if (t.motorTemp > MOTOR_TEMP_WARN_C) penalty += 15;
  }
  if (hasCoreTemp) {
    if (t.coreTemp > CORE_TEMP_CRITICAL_C) penalty += 35;
    else if (t.coreTemp > CORE_TEMP_WARN_C) penalty += 15;
  }
  return clampScore(100 - penalty);
}

// Payload Utilization = current load / rated payload capacity * 100
function computePayloadUtilization(t) {
  if (typeof t.vehicleLoad !== "number" || !Number.isFinite(t.vehicleLoad)) return null;
  return clampScore((t.vehicleLoad / VEHICLE.ratedPayloadKg) * 100);
}

function computePerformanceMetrics() {
  const t = state.telemetry;
  return {
    speedEfficiency: computeSpeedEfficiency(t),
    energyEfficiency: computeEnergyEfficiency(t),
    drivingSmoothness: computeDrivingSmoothness(),
    reliability: computeReliability(t),
    payloadUtilization: computePayloadUtilization(t),
  };
}

// Geometry sized generously (relative to its own viewBox) so axis labels
// stay inside the SVG's own coordinate space — widget cards clip overflow,
// so labels can't rely on spilling outside the viewBox to stay visible.
const RADAR_GEOMETRY = { cx: 200, cy: 172, radius: 88 };

function radarPoint(index, count, valuePct) {
  // Start at the top (12 o'clock) and go clockwise.
  const angle = -Math.PI / 2 + (index * 2 * Math.PI) / count;
  const r = RADAR_GEOMETRY.radius * (valuePct / 100);
  return {
    x: RADAR_GEOMETRY.cx + r * Math.cos(angle),
    y: RADAR_GEOMETRY.cy + r * Math.sin(angle),
    angle,
  };
}

// Wraps a 1-2 word label onto separate lines so long labels ("Payload
// Utilization") don't run wide enough to get clipped by the widget card.
function radarLabelLines(label) {
  return label.split(" ");
}

function renderPerformanceRadar() {
  const grid = $("#radar-grid");
  const labelsLayer = $("#radar-axis-labels");
  const shape = $("#radar-shape");
  const pointsLayer = $("#radar-points");
  const overallScoreEl = $("#radar-overall-score");
  if (!grid || !labelsLayer || !shape || !pointsLayer || !overallScoreEl) return;

  const metrics = computePerformanceMetrics();
  const count = RADAR_METRICS.length;
  const { cx, cy, radius } = RADAR_GEOMETRY;

  // --- Background rings (25/50/75/100%) + spokes, minor + major graduations ---
  const ringLevels = [0.25, 0.5, 0.75, 1];
  grid.innerHTML = ringLevels
    .map((level) => {
      const ringPoints = RADAR_METRICS.map((_, i) => {
        const p = radarPoint(i, count, level * 100);
        return `${p.x.toFixed(1)},${p.y.toFixed(1)}`;
      }).join(" ");
      return `<polygon class="radar-ring${level === 1 ? " radar-ring-outer" : ""}" points="${ringPoints}" />`;
    })
    .join("") +
    RADAR_METRICS.map((_, i) => {
      const p = radarPoint(i, count, 100);
      return `<line class="radar-spoke" x1="${cx}" y1="${cy}" x2="${p.x.toFixed(1)}" y2="${p.y.toFixed(1)}" />`;
    }).join("");

  // --- Axis labels + per-metric score, positioned around the outside ---
  const labelRadius = radius + 30;
  labelsLayer.innerHTML = RADAR_METRICS.map((key, i) => {
    const angle = -Math.PI / 2 + (i * 2 * Math.PI) / count;
    const lx = cx + labelRadius * Math.cos(angle);
    const ly = cy + labelRadius * Math.sin(angle);
    const anchor = Math.abs(Math.cos(angle)) < 0.3 ? "middle" : Math.cos(angle) > 0 ? "start" : "end";
    const words = radarLabelLines(RADAR_LABELS[key]);
    const score = metrics[key];
    const scoreText = score === null ? "--" : String(Math.round(score));
    const lineTspans = words
      .map((word, wi) => `<tspan x="${lx.toFixed(1)}" dy="${wi === 0 ? 0 : 11}">${word}</tspan>`)
      .join("");
    const scoreTspan = `<tspan class="radar-axis-score" x="${lx.toFixed(1)}" dy="13">${scoreText}</tspan>`;
    return `<text class="radar-axis-label" x="${lx.toFixed(1)}" y="${ly.toFixed(1)}" text-anchor="${anchor}">${lineTspans}${scoreTspan}</text>`;
  }).join("");

  // --- Data shape: missing metrics plot as 0 but are marked "--" in the label ---
  const shapePoints = RADAR_METRICS.map((key, i) => {
    const p = radarPoint(i, count, metrics[key] ?? 0);
    return `${p.x.toFixed(1)},${p.y.toFixed(1)}`;
  }).join(" ");
  shape.setAttribute("points", shapePoints);

  pointsLayer.innerHTML = RADAR_METRICS.map((key, i) => {
    const p = radarPoint(i, count, metrics[key] ?? 0);
    return `<circle class="radar-dot" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="4" />`;
  }).join("");

  const known = RADAR_METRICS.map((key) => metrics[key]).filter((v) => v !== null);
  const overall = known.length ? Math.round(known.reduce((sum, v) => sum + v, 0) / known.length) : null;
  overallScoreEl.textContent = overall === null ? "-- / 100" : `${overall} / 100`;
}

function setGraphType(type) {
  if (!GRAPH_CONFIG[type]) return;
  state.chartType = type;
  $$(".graph-tabs button").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.graph === type);
  });

  const radarArea = $(".radar-area");
  const chartArea = $(".chart-area");
  if (radarArea) {
    radarArea.style.display = type === "performance" ? "block" : "none";
  }
  if (chartArea) {
    chartArea.style.display = type === "performance" ? "none" : "block";
  }

  if (type === "performance") {
    renderPerformanceRadar();
  } else {
    renderGraph();
  }
}

// Chart plotting geometry, shared by axis drawing and point placement.
// padLeft leaves room for the Y-axis value labels; padBottom leaves room for
// the X-axis time labels underneath the axis line. padLeft is computed per
// graph (see chartGeometryFor) since unit strings vary in width (e.g.
// "0.50 %/km" vs "0 W") and the widget card clips anything that doesn't fit
// inside the SVG's own coordinate space.
const CHART_GEOMETRY = { width: 460, height: 220, padRight: 16, padTop: 14, padBottom: 34 };
const CHART_AXIS_FONT_SIZE = 10; // must match .chart-axis-text font-size in styles.css
const CHART_AXIS_CHAR_WIDTH = CHART_AXIS_FONT_SIZE * 0.58; // approx average glyph width for that font/weight

function chartGeometryFor(config) {
  const candidateLabels = [
    formatGraphValue(config.max, config),
    formatGraphValue((config.max + config.min) / 2, config),
    formatGraphValue(config.min, config),
  ];
  const maxLabelLength = Math.max(...candidateLabels.map((label) => label.length));
  const padLeft = Math.min(100, Math.max(46, 8 /* tick length */ + 6 /* gap */ + maxLabelLength * CHART_AXIS_CHAR_WIDTH));
  return { ...CHART_GEOMETRY, padLeft };
}

// Y-axis ticks: `divisions` intervals between min and max. Every tick gets a
// gridline; only every other one (the "major" ticks) gets a text label, the
// rest are minor graduations for finer reading.
function computeYTicks(config, divisions = 4) {
  const ticks = [];
  for (let i = 0; i <= divisions; i++) {
    const t = i / divisions; // 0 = min (bottom), 1 = max (top)
    const value = config.min + t * (config.max - config.min);
    const major = i === 0 || i === divisions || i === Math.round(divisions / 2);
    ticks.push({ value, t, major });
  }
  return ticks;
}

// X-axis ticks across the visible time window. `divisions` minor intervals;
// every 3rd tick (i.e. every 30 minutes on a 60-minute/6-division window) is
// labelled as a major tick, matching the previous -1h / -30m / Now labels.
function computeXTicks(windowMs, divisions = 6) {
  const ticks = [];
  for (let i = 0; i <= divisions; i++) {
    const t = i / divisions; // 0 = windowStart (oldest, left), 1 = now (right)
    const minutesAgo = Math.round(((1 - t) * windowMs) / 60000);
    const major = i % 3 === 0;
    ticks.push({ t, minutesAgo, major });
  }
  return ticks;
}

function renderGraph() {
  const config = GRAPH_CONFIG[state.chartType];
  const line = $("#chart-line");
  const title = $("#chart-metric-title");
  const value = $("#chart-metric-value");
  const pointLayer = $("#chart-point-layer");
  const gridLayer = $("#chart-grid");
  const yAxisLayer = $("#chart-y-axis");
  const xAxisLayer = $("#chart-x-axis");
  if (!config || !line || !title || !value || !pointLayer || !gridLayer || !yAxisLayer || !xAxisLayer) return;

  title.textContent = config.label;

  const geo = chartGeometryFor(config);
  const plotWidth = geo.width - geo.padLeft - geo.padRight;
  const plotHeight = geo.height - geo.padTop - geo.padBottom;
  const axisLeft = geo.padLeft;
  const axisRight = geo.width - geo.padRight;
  const axisTop = geo.padTop;
  const axisBottom = geo.height - geo.padBottom;

  const now = Date.now();
  const windowStart = now - GRAPH_WINDOW_MS;

  const toX = (timestamp) => {
    const normalizedTime = Math.min(1, Math.max(0, (timestamp - windowStart) / GRAPH_WINDOW_MS));
    return axisLeft + normalizedTime * plotWidth;
  };
  const toY = (val) => {
    const normalizedValue = Math.min(1, Math.max(0, (val - config.min) / (config.max - config.min)));
    return axisBottom - normalizedValue * plotHeight;
  };

  // --- Y axis (left side): tick marks + value labels, major and minor ---
  const yTicks = computeYTicks(config);
  yAxisLayer.innerHTML = yTicks
    .map(({ value: tickValue, t, major }) => {
      const y = axisBottom - t * plotHeight;
      const tickLength = major ? 8 : 4;
      const label = major
        ? `<text class="chart-axis-text chart-axis-text-y" x="${(axisLeft - tickLength - 6).toFixed(1)}" y="${y.toFixed(1)}">${formatGraphValue(tickValue, config)}</text>`
        : "";
      return `<line class="chart-tick${major ? " chart-tick-major" : " chart-tick-minor"}" x1="${(axisLeft - tickLength).toFixed(1)}" y1="${y.toFixed(1)}" x2="${axisLeft.toFixed(1)}" y2="${y.toFixed(1)}" />${label}`;
    })
    .join("");

  // --- X axis (bottom): tick marks + time labels, major and minor ---
  const xTicks = computeXTicks(GRAPH_WINDOW_MS);
  xAxisLayer.innerHTML = xTicks
    .map(({ t, minutesAgo, major }) => {
      const x = axisLeft + t * plotWidth;
      const tickLength = major ? 8 : 4;
      const label = major
        ? `<text class="chart-axis-text chart-axis-text-x" x="${x.toFixed(1)}" y="${(axisBottom + tickLength + 14).toFixed(1)}">${minutesAgo === 0 ? "Now" : `-${minutesAgo}m`}</text>`
        : "";
      return `<line class="chart-tick${major ? " chart-tick-major" : " chart-tick-minor"}" x1="${x.toFixed(1)}" y1="${axisBottom.toFixed(1)}" x2="${x.toFixed(1)}" y2="${(axisBottom + tickLength).toFixed(1)}" />${label}`;
    })
    .join("");

  // --- Gridlines (drawn behind the axes) + the axis lines themselves ---
  const gridLinesY = yTicks
    .map(({ t, major }) => {
      const y = axisBottom - t * plotHeight;
      return `<line class="chart-grid-line${major ? " chart-grid-major" : " chart-grid-minor"}" x1="${axisLeft.toFixed(1)}" y1="${y.toFixed(1)}" x2="${axisRight.toFixed(1)}" y2="${y.toFixed(1)}" />`;
    })
    .join("");
  const gridLinesX = xTicks
    .map(({ t, major }) => {
      const x = axisLeft + t * plotWidth;
      return `<line class="chart-grid-line${major ? " chart-grid-major" : " chart-grid-minor"}" x1="${x.toFixed(1)}" y1="${axisTop.toFixed(1)}" x2="${x.toFixed(1)}" y2="${axisBottom.toFixed(1)}" />`;
    })
    .join("");
  gridLayer.innerHTML = `${gridLinesY}${gridLinesX}`
    + `<line class="chart-axis-line" x1="${axisLeft.toFixed(1)}" y1="${axisTop.toFixed(1)}" x2="${axisLeft.toFixed(1)}" y2="${axisBottom.toFixed(1)}" />`
    + `<line class="chart-axis-line" x1="${axisLeft.toFixed(1)}" y1="${axisBottom.toFixed(1)}" x2="${axisRight.toFixed(1)}" y2="${axisBottom.toFixed(1)}" />`;

  // --- Data: every retained point still inside the visible window, in order ---
  const history = graphHistory[state.chartType];
  const timestamps = graphTimestamps[state.chartType];
  const visible = [];
  for (let i = 0; i < history.length; i++) {
    const pointValue = history[i];
    const timestamp = timestamps[i];
    if (!Number.isFinite(pointValue) || !Number.isFinite(timestamp) || timestamp < windowStart) continue;
    visible.push({ x: toX(timestamp), y: toY(pointValue), raw: pointValue });
  }

  pointLayer.innerHTML = visible
    .map(({ x, y }) => `<circle class="chart-dot" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="4" />`)
    .join("");

  if (visible.length) {
    line.setAttribute("points", visible.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" "));
  } else {
    line.setAttribute("points", `${axisLeft.toFixed(1)},${axisBottom.toFixed(1)} ${axisRight.toFixed(1)},${axisBottom.toFixed(1)}`);
  }

  const lastValue = visible.length ? visible[visible.length - 1].raw : null;
  value.textContent = formatGraphValue(lastValue, config);
}

function openModal(id) {
  const modal = $(id);
  if (!modal) return;

  if (id === "#battery-modal") {
    updateDashboard(state.telemetry);
  }

  if (id === "#trip-modal") {
    // Seed the live-backed fields from the vehicle's current telemetry, then
    // work out whether anything is still outstanding before showing the form.
    applyLiveTripInputs();
    validateTripForm();
  }

  modal.classList.add("is-open");
  modal.setAttribute("aria-hidden", "false");
}

function closeModals() {
  $$(".modal").forEach((modal) => {
    modal.classList.remove("is-open");
    modal.setAttribute("aria-hidden", "true");
  });
}

function allowed(accessString) {
  if (!accessString) return true;
  return accessString.includes(state.activeUser.code);
}

function applyAccessControl() {
  document.body.dataset.role = state.activeUser.role;
  const userCode = state.activeUser.code; // Access code remains C; avatar is D.

  document.querySelectorAll('[data-access]').forEach((element) => {
    const requiredAccess = element.dataset.access; // e.g., "BC"
    
    // If the user's code (e.g., 'A') is NOT in the allowed string (e.g., 'BC'), hide it
    if (!requiredAccess.includes(userCode)) {
      element.hidden = true;
      element.style.display = "none";
    } else {
      element.hidden = false;
      element.style.removeProperty("display");
    }
  });

  // Update profile name in top bar
  document.getElementById("active-profile-name").textContent = state.activeUser.label;
  document.getElementById("active-profile-avatar").textContent = state.activeUser.label[0];
}

// --- AUTH LOGIC ---
const protectedRoles = ["operator", "developer"];

function startLogin(role) {
  if (!protectedRoles.includes(role)) {
    localStorage.removeItem("tukzie_token");
    state.activeUser = { role: "guest", code: "A", label: "Guest" };
    completeLogin();
    return;
  }

  state.pendingRole = role;
  const modal = document.getElementById("pin-modal");
  const pinInput = document.getElementById("pin-input");

  document.getElementById("pin-error").textContent = "";
  document.getElementById("pin-copy").textContent = `Enter password for ${role} access.`;
  pinInput.value = "";

  modal.classList.add("is-open");
  setTimeout(() => pinInput.focus(), 100);
}

async function verifyPin() {
  const role = state.pendingRole;
  const password = document.getElementById("pin-input").value;

  const formData = new FormData();
  formData.append("username", role);
  formData.append("password", password);

  try {
    const response = await fetch("/token", {
      method: "POST",
      body: formData,
    });

    if (response.ok) {
      const data = await response.json();
      localStorage.setItem("tukzie_token", data.access_token);
      state.activeUser = {
        role,
        code: role === "developer" ? "C" : "B",
        label: role.charAt(0).toUpperCase() + role.slice(1),
      };

      completeLogin();
    } else {
      document.getElementById("pin-error").textContent = "Incorrect Password.";
    }
  } catch (err) {
    document.getElementById("pin-error").textContent = "Server Error. Try again.";
  }
}

function completeLogin() {
  resetDeveloperSession();
  document.getElementById("login-screen").classList.remove("is-visible");
  document.getElementById("dashboard-screen").classList.add("is-visible");
  document.getElementById("pin-modal").classList.remove("is-open");
  applyAccessControl();
  // Re-open the live feed so the server applies this profile's data rules (no exact location for Guest).
  if (window.sensorManager) { window.sensorManager.reset(); window.sensorManager.disconnect(); window.sensorManager.connect(); }
  document.getElementById("side-menu").classList.remove("is-open");
  if (state.activeUser.role === "developer") document.body.className = "";
  if (state.activeUser.role !== "guest") loadPublicConfig();
  switchTab("overview"); // always land on a tab every role can see

  setTimeout(() => {
    if (state.activeUser.role !== "developer") initNavTileMap();
    if (navTileState.map) navTileState.map.invalidateSize();
  }, 100);
}

// --- TAB NAVIGATION ---
function switchTab(tabName) {
  const tab = document.querySelector(`[data-tab="${tabName}"]`);
  if (!tab || !allowed(tab.dataset.access)) return;
  $$(".tab-panel").forEach((panel) => {
    panel.classList.toggle("is-active", panel.dataset.tabPanel === tabName);
  });
  $$(".tab-button").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.tab === tabName);
  });

  if (tabName === "system-health") startSystemHealthPolling();
  else stopSystemHealthPolling();
  if (tabName === "analytics") loadAnalytics();
}

function authHeaders() {
  const token = localStorage.getItem("tukzie_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// --- SYSTEM HEALTH TAB ---
let healthPollTimer = null;

function setStatusDot(dotSelector, status) {
  const dot = $(dotSelector);
  if (!dot) return;
  dot.classList.remove("status-healthy", "status-warning", "status-critical", "status-disconnected");
  dot.classList.add(`status-${status}`);
}

async function refreshSystemHealth() {
  try {
    const response = await fetch("/api/v1/system/health", { headers: authHeaders() });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const health = await response.json();

    renderDeveloperHealth(health);
    setStatusDot("#health-web-broker-dot", health.web_to_broker);
    setText("#health-web-broker-text", health.web_to_broker === "healthy" ? "Connected" : "Disconnected");

    setStatusDot("#health-gateway-dot", health.gateway_to_broker);
    setText(
      "#health-gateway-text",
      { healthy: "Healthy", warning: "Warning", disconnected: "Disconnected", critical: "Disconnected" }[health.gateway_to_broker] || "--"
    );
    setText(
      "#health-gateway-age",
      health.seconds_since_last_telemetry === null
        ? "no telemetry received yet"
        : `${health.seconds_since_last_telemetry.toFixed(1)}s ago`
    );
  } catch (err) {
    clearDeveloperHealth();
    setStatusDot("#health-web-broker-dot", "warning");
    setStatusDot("#health-gateway-dot", "warning");
    setText("#health-web-broker-text", "Unavailable");
    setText("#health-gateway-text", "Unavailable");
    setText("#health-gateway-age", "unknown");
    console.warn("System health check failed:", err);
  }

  try {
    const response = await fetch("/api/v1/system/logs", { headers: authHeaders() });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const { logins } = await response.json();
    const list = $("#login-log-list");
    if (list) {
      list.innerHTML = logins.length
        ? logins
            .slice(0, 20)
            .map((entry) => `<div class="metric-row"><span>${escapeText(entry.username)} (${escapeText(entry.role)})</span><strong>${new Date(entry.at).toLocaleString()}</strong></div>`)
            .join("")
        : `<p class="widget-note">No logins recorded yet this session.</p>`;
    }
  } catch (err) {
    console.warn("Login log fetch failed:", err);
  }
}

function startSystemHealthPolling() {
  if (healthPollTimer) return;
  refreshSystemHealth();
  healthPollTimer = setInterval(refreshSystemHealth, 3000);
}

function stopSystemHealthPolling() {
  if (healthPollTimer) {
    clearInterval(healthPollTimer);
    healthPollTimer = null;
  }
}

// --- CONTROL TAB ---
function addControlFeedback(cmd, status, note) {
  const list = $("#control-feedback-list");
  if (!list) return;
  const row = document.createElement("div");
  row.className = "control-feedback-row";
  row.innerHTML = `<span class="status-dot status-${status}"></span> <strong>${cmd}</strong> — ${note} <small>${new Date().toLocaleTimeString()}</small>`;
  list.prepend(row);
  while (list.children.length > 10) list.removeChild(list.lastChild);
}

async function sendControlCommand(cmd) {
  addControlFeedback(cmd, "warning", "sending...");
  try {
    const response = await fetch("/api/v1/control/command", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ cmd, target: "vehicle" }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      addControlFeedback(cmd, "critical", body.detail || `send failed (HTTP ${response.status})`);
      return;
    }
  } catch (err) {
    addControlFeedback(cmd, "critical", `send failed: ${err.message}`);
    return;
  }

  // Poll for the real ack from gateway/mock_ack_responder.py. No ack at
  // all (bad auth, rate-limited, stale — see command_validation.py)
  // means REJECTED, surfaced here as a timeout, same as dashboard/app.py.
  const maxAttempts = 8;
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    await new Promise((r) => setTimeout(r, 700));
    try {
      const response = await fetch(`/api/v1/control/status/${encodeURIComponent(cmd)}`, { headers: authHeaders() });
      const ack = await response.json();
      if (ack.status === "ack") {
        addControlFeedback(cmd, "healthy", ack.note || "confirmed by rig");
        return;
      }
    } catch (err) {
      // keep polling — a transient fetch error shouldn't abort early
    }
  }
  addControlFeedback(cmd, "critical", "no ack received — command may have been rejected or the mock rig isn't running");
}

// --- ANALYTICS TAB ---
// Chart and export implementation is in developer.js.


// --- DASHBOARD UPDATES ---
function formatPlaceholder(value, suffix = "") {
  if (value === null || value === undefined || value === "") return "--";
  return `${value}${suffix}`;
}

function formatTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "--";
  const minutes = Math.floor(seconds / 60);
  const hrs = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return hrs > 0 ? `${hrs}h ${mins}m` : `${mins}m`;
}

function formatDuration(hours) {
  if (!Number.isFinite(hours) || hours < 0) return "--";
  return formatTime(hours * 3600);
}

function formatEtaClock(hours) {
  if (!Number.isFinite(hours) || hours < 0) return "--";
  const arrival = new Date(Date.now() + hours * 3600 * 1000);
  return arrival.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function setTripStatusClass(statusEl, level) {
  statusEl.classList.remove("status-good", "status-warning", "status-danger");
  if (level) statusEl.classList.add(level);
}

function hasFreshTelemetry() {
  if (lastTelemetryUpdateTime !== null && (Date.now() - lastTelemetryUpdateTime) <= LIVE_DATA_TIMEOUT_MS) {
    return true;
  }

  if (window.sensorManager && typeof window.sensorManager.getAllReadings === "function") {
    const readings = window.sensorManager.getAllReadings();
    return Object.values(readings).some((value) => value !== null && value !== undefined && value !== "");
  }

  return false;
}

function clearDashboardUI() {
  if (hasFreshTelemetry()) {
    return;
  }

  state.telemetry = createEmptyTelemetry();
  state.navDistanceKm = null;

  const textSelectors = [
    "#speed-value", "#soc-value", "#status-battery", "#range-value", "#hero-range",
    "#throttle-value", "#brake-value", "#consumption-value", "#status-temp",
    "#weather-temp", "#motor-temp", "#tyre-pressure", "#vehicle-load",
    "#hero-health", "#modal-health", "#vinfo-battery-health", "#modal-voltage",
    "#vinfo-voltage", "#modal-core-temp", "#vinfo-core-temp", "#modal-soc-value",
    "#hero-mode", "#vinfo-drive-mode", "#vinfo-power", "#vinfo-ambient-temp",
    "#vinfo-throttle", "#vinfo-brake", "#trip-arrival-soc", "#trip-remaining-range",
    "#trip-summary-eta", "#trip-out-arrival-soc", "#trip-out-energy-used",
    "#trip-out-consumption", "#trip-out-eta", "#trip-out-duration", "#trip-out-range",
    "#trip-out-mass", "#trip-out-batt-temp", "#trip-out-impact-weather",
    "#trip-out-impact-terrain", "#trip-out-impact-cargo", "#trip-result-status",
    "#trip-breakdown-rolling", "#trip-breakdown-grade", "#trip-breakdown-drag",
    "#trip-breakdown-accessory", "#radar-overall-score", "#chart-metric-value",
    "#hero-mode", "#status-time"
  ];

  textSelectors.forEach((selector) => {
    const element = $(selector);
    if (element) {
      element.textContent = selector === "#status-time" ? element.textContent : "--";
    }
  });

  const statusEl = $("#trip-result-status");
  if (statusEl) {
    statusEl.textContent = "";
    setTripStatusClass(statusEl, null);
  }

  $$("input, select, textarea").forEach((element) => {
    if (element.id === "pin-input") {
      return; // login modal's password field — not a dashboard display, never touch it here
    }
    if (element.id && ["trip-soc-input", "trip-battery-temp-input", "trip-passengers-input", "trip-cargo-input", "trip-distance-input", "trip-avg-speed-input", "trip-elevation-gain-input", "trip-elevation-loss-input", "trip-surface-input", "trip-weather-input", "nav-search-input"].includes(element.id)) {
      element.value = "";
    }
  });

  const dial = $(".speed-dial");
  if (dial) {
    dial.style.setProperty("--speed-progress", "0");
  }

  $$(".vehicle-soc").forEach((widget) => {
    widget.style.setProperty("--soc-level", "0%");
    widget.style.setProperty("--soc-color", "var(--muted)");
  });

  const modalSocValue = $("#modal-soc-value");
  if (modalSocValue) {
    modalSocValue.textContent = "--%";
  }

  Object.keys(graphHistory).forEach((type) => {
    graphHistory[type].length = 0;
    graphTimestamps[type].length = 0;
  });
  saveGraphHistory();

  renderPerformanceRadar();
  renderGraph();

  if (window.sensorManager && typeof window.sensorManager.reset === "function") {
    window.sensorManager.reset();
  }
}

/*
 * ---------------------------------------------------------------------------
 * Estimate Trip: live inputs + validation
 * ---------------------------------------------------------------------------
 * Some inputs are not really "user settings" -- the vehicle already knows them.
 * Rather than hard-coding a plausible-looking number, these fields are filled
 * from the same telemetry the dashboard is showing, so the simulation always
 * starts from the vehicle's actual state.
 *
 *   Starting SOC          <- telemetry.soc         (State of Charge widget)
 *   Battery temperature   <- telemetry.coreTemp    (battery core temperature)
 *   Cargo weight          <- telemetry.vehicleLoad (load sensor)
 *   Average speed         <- telemetry.speed       (Dynamics widget)
 *
 * If a value is not available the field is simply left blank and the driver is
 * asked for it -- a blank is honest, a made-up default is not.
 */
const TRIP_LIVE_FIELDS = [
  { selector: "#trip-soc-input", key: "soc", decimals: 0 },
  { selector: "#trip-battery-temp-input", key: "coreTemp", decimals: 0 },
  { selector: "#trip-cargo-input", key: "vehicleLoad", decimals: 0 },
];

function syncTripSocFromTelemetry() {
  const socInput = $("#trip-soc-input");
  if (!socInput) return;

  const liveSoc = state.telemetry?.soc;
  if (typeof liveSoc === "number" && Number.isFinite(liveSoc)) {
    const nextValue = String(Math.round(liveSoc));
    if (socInput.value !== nextValue) {
      socInput.value = nextValue;
    }
  }
}

function applyLiveTripInputs() {
  syncTripSocFromTelemetry();
  const t = state.telemetry || {};

  // Average speed is fixed by spec (35 km/h) and is never seeded from telemetry.
  const speedEl = $("#trip-avg-speed-input");
  if (speedEl) speedEl.value = FIXED_AVG_SPEED_KMH;

  // The trip module must not populate anything from stale/absent live data.
  if (isTripLiveDataStale()) {
    TRIP_LIVE_FIELDS.forEach(({ selector }) => {
      const element = $(selector);
      if (element) element.value = "";
    });
    return;
  }

  TRIP_LIVE_FIELDS.forEach(({ selector, key, decimals }) => {
    const element = $(selector);
    if (!element) return;

    const live = t[key];
    if (typeof live !== "number" || !Number.isFinite(live)) {
      element.value = "";
      return;
    }

    element.value = live.toFixed(decimals);
  });
}

/*
 * ---------------------------------------------------------------------------
 * Live-data dependency (Estimated Trip widget)
 * ---------------------------------------------------------------------------
 * The trip module must never show a calculated value that isn't backed by
 * live telemetry. `lastTelemetryUpdateTime` is stamped on every genuine
 * "telemetry-update" event (real sensor traffic only — never the one-off
 * static fallback used when no sensor manager is present). A watchdog checks
 * it continuously; once the gap exceeds LIVE_DATA_TIMEOUT_MS every trip input
 * and output is cleared, and everything resumes automatically the moment
 * fresh data arrives.
 */
let lastTelemetryUpdateTime = null;
let tripModuleIsCleared = false;

function isTripLiveDataStale() {
  return lastTelemetryUpdateTime === null || (Date.now() - lastTelemetryUpdateTime) > LIVE_DATA_TIMEOUT_MS;
}

function clearTripModule() {
  tripModuleIsCleared = true;

  // Clear every trip-modal input field (live-backed and manually entered).
  const modal = $("#trip-modal");
  if (modal) {
    modal.querySelectorAll("input").forEach((element) => {
      if (element.id === "trip-avg-speed-input") return; // fixed constant, not live data
      element.value = "";
    });
  }

  // Clear every calculated output.
  [
    "#trip-out-arrival-soc", "#trip-out-energy-used", "#trip-out-consumption",
    "#trip-out-eta", "#trip-out-duration", "#trip-out-range", "#trip-out-mass",
    "#trip-out-batt-temp", "#trip-out-impact-weather", "#trip-out-impact-terrain",
    "#trip-out-impact-cargo",
  ].forEach((selector) => setText(selector, "--"));
  setText("#trip-arrival-soc", "--");
  setText("#trip-remaining-range", "--");
  setText("#trip-summary-eta", "--");

  const statusEl = $("#trip-result-status");
  if (statusEl) {
    statusEl.textContent = "Live vehicle data unavailable. Trip estimation is paused.";
    setTripStatusClass(statusEl, "status-warning");
  }

  const adviceList = $("#trip-advice-list");
  if (adviceList) adviceList.innerHTML = "";

  const banner = $("#trip-live-data-banner");
  if (banner) banner.style.display = "block";

  clearNavigationDistanceSource();
  validateTripForm();
}

function checkTripLiveDataWatchdog() {
  if (isTripLiveDataStale()) {
    if (!tripModuleIsCleared) {
      clearTripModule();
    }
    clearDashboardUI();
    return;
  }

  if (tripModuleIsCleared) {
    // Live data has resumed: repopulate live-backed fields and recalculate.
    tripModuleIsCleared = false;
    const banner = $("#trip-live-data-banner");
    if (banner) banner.style.display = "none";
    applyLiveTripInputs();
    validateTripForm();
    refreshTripEstimate();
  }
}

/*
 * The estimate is only meaningful once every input has a real value, so the
 * Calculate button stays disabled until the form is complete and in range. The
 * note underneath names the fields still outstanding rather than just refusing
 * to work.
 */
function validateTripForm() {
  const button = $("#trip-calc-button");
  const note = $("#trip-validation-note");
  const modal = $("#trip-modal");
  if (!button || !modal) return false;

  const missing = [];
  const outOfRange = [];

  modal.querySelectorAll("input[required]").forEach((element) => {
    const labelEl = modal.querySelector(`label[for="${element.id}"]`);
    // Strip the "live" chip so the message reads "Starting SOC", not "Starting SOC live".
    const name = labelEl
      ? labelEl.textContent.replace(/\blive\b/i, "").replace(/\s+/g, " ").trim()
      : element.id;

    const raw = String(element.value).trim();
    if (raw === "") {
      missing.push(name);
      return;
    }

    const value = Number(raw);
    if (!Number.isFinite(value)) {
      missing.push(name);
      return;
    }

    // Read min/max from the ATTRIBUTES, not the properties. An absent max comes
    // back as "" and Number("") is 0 -- which would silently reject every
    // positive value on an unbounded field like Route distance.
    const minAttr = element.getAttribute("min");
    const maxAttr = element.getAttribute("max");
    const min = minAttr === null || minAttr === "" ? null : Number(minAttr);
    const max = maxAttr === null || maxAttr === "" ? null : Number(maxAttr);
    if ((min !== null && Number.isFinite(min) && value < min) ||
        (max !== null && Number.isFinite(max) && value > max)) {
      outOfRange.push(name);
    }
  });

  // Maximum allowable trip distance (70 km). Checked separately so the
  // dedicated validation message can be shown next to the distance field.
  const distanceEl = $("#trip-distance-input");
  const distanceError = $("#trip-distance-error");
  const distanceValue = distanceEl ? Number(distanceEl.value) : 0;
  const distanceExceeded = Number.isFinite(distanceValue) && distanceValue > MAX_TRIP_DISTANCE_KM;
  if (distanceError) {
    distanceError.textContent = distanceExceeded ? DISTANCE_LIMIT_MESSAGE : "";
  }

  // No trip calculations shall occur without valid, current live data.
  const liveDataOk = !isTripLiveDataStale();

  const ready = missing.length === 0 && outOfRange.length === 0 && !distanceExceeded && liveDataOk;
  button.disabled = !ready;

  if (note) {
    if (!liveDataOk) {
      note.textContent = "Live vehicle data unavailable. Trip estimation is paused.";
    } else if (distanceExceeded) {
      note.textContent = DISTANCE_LIMIT_MESSAGE;
    } else if (missing.length) {
      note.textContent = `Enter a value for: ${missing.join(", ")}.`;
    } else if (outOfRange.length) {
      note.textContent = `Value out of range: ${outOfRange.join(", ")}.`;
    } else {
      note.textContent = "";
    }
  }

  return ready;
}

function refreshTripEstimate() {
  // No trip calculations shall occur without valid live data.
  if (isTripLiveDataStale()) {
    clearTripModule();
    return;
  }

  // Trip estimation is disabled while the selected distance exceeds the
  // vehicle's maximum operational range — don't silently clamp and compute.
  const distanceEl = $("#trip-distance-input");
  const rawDistance = distanceEl ? Number(distanceEl.value) : 0;
  if (Number.isFinite(rawDistance) && rawDistance > MAX_TRIP_DISTANCE_KM) {
    validateTripForm();
    return;
  }

  const inputs = getTripInputs();
  const result = simulateProgram9Trip(inputs);
  const impacts = computeTripImpacts(inputs, result);
  const advice = buildDriverAdvice(inputs, result);

  // ETA and Remaining Range at Arrival always use the fixed formulas, not the
  // physics-derived trip duration/range from the energy simulation.
  const fixedMetrics = computeFixedTripMetrics(inputs.distanceKm);
  const etaClock = formatEtaClock(fixedMetrics.etaHours);
  const formatPct = (value) => `${value >= 0 ? "+" : ""}${value.toFixed(0)}%`;

  // Primary outputs
  setText("#trip-out-arrival-soc", `${result.arrivalSocPct.toFixed(0)}%`);
  setText("#trip-out-energy-used", `${result.energyUsedWh.toFixed(0)} Wh`);
  setText("#trip-out-consumption", `${result.energyPerKmWh.toFixed(1)} Wh/km`);
  setText("#trip-out-eta", etaClock);
  setText("#trip-out-duration", formatDuration(fixedMetrics.etaHours));
  setText("#trip-out-range", `${fixedMetrics.remainingRangeKm.toFixed(1)} km`);
  setText("#trip-out-mass", `${result.totalMassKg.toFixed(0)} kg`);
  setText("#trip-out-batt-temp", `${result.batteryTempArrivalC.toFixed(1)}°C`);
  setText("#trip-out-impact-weather", formatPct(impacts.weatherImpactPct));
  setText("#trip-out-impact-terrain", formatPct(impacts.terrainImpactPct));
  setText("#trip-out-impact-cargo", formatPct(impacts.cargoImpactPct));

  // Status line
  const statusEl = $("#trip-result-status");
  if (statusEl) {
    let level = "status-good";
    let message = `Trip is comfortably within range. Arrival battery about ${result.arrivalSocPct.toFixed(0)}%.`;
    if (result.arrivalSocPct <= 0) {
      level = "status-danger";
      message = "Insufficient battery charge to complete this trip as configured.";
    } else if (result.arrivalSocPct < LOW_SOC_RESERVE_PCT) {
      level = "status-warning";
      message = `Arrival battery projected at ${result.arrivalSocPct.toFixed(0)}%, below the ${LOW_SOC_RESERVE_PCT}% safe reserve.`;
    }
    statusEl.textContent = message;
    setTripStatusClass(statusEl, level);
  }

  // Visual breakdown: share of total (unsigned) energy demand
  const b = result.breakdown;
  const pctOfTotal = (wh) => (b.total > 0 ? (Math.abs(wh) / b.total) * 100 : 0);
  const rollingPct = pctOfTotal(b.rollingWh);
  const gradePct = pctOfTotal(b.gradeWh);
  const dragPct = pctOfTotal(b.dragWh);
  const accessoryPct = pctOfTotal(b.accessoryWh);

  const rollingSeg = $(".breakdown-rolling");
  const gradeSeg = $(".breakdown-grade");
  const dragSeg = $(".breakdown-drag");
  const accessorySeg = $(".breakdown-accessory");
  if (rollingSeg) rollingSeg.style.width = `${rollingPct}%`;
  if (gradeSeg) gradeSeg.style.width = `${gradePct}%`;
  if (dragSeg) dragSeg.style.width = `${dragPct}%`;
  if (accessorySeg) accessorySeg.style.width = `${accessoryPct}%`;

  setText("#trip-breakdown-rolling", `${b.rollingWh.toFixed(0)} Wh (${rollingPct.toFixed(0)}%)`);
  setText(
    "#trip-breakdown-grade",
    `${b.gradeWh < 0 ? "-" : ""}${Math.abs(b.gradeWh).toFixed(0)} Wh (${gradePct.toFixed(0)}%)${b.gradeWh < 0 ? " · regen credit" : ""}`
  );
  setText("#trip-breakdown-drag", `${b.dragWh.toFixed(0)} Wh (${dragPct.toFixed(0)}%)`);
  setText("#trip-breakdown-accessory", `${b.accessoryWh.toFixed(0)} Wh (${accessoryPct.toFixed(0)}%)`);

  // Driver advice
  const adviceList = $("#trip-advice-list");
  if (adviceList) {
    adviceList.innerHTML = "";
    advice.forEach((line) => {
      const li = document.createElement("li");
      li.textContent = line;
      adviceList.appendChild(li);
    });
  }

  // Collapsed widget-card summary
  setText("#trip-arrival-soc", result.arrivalSocPct.toFixed(0));
  setText("#trip-remaining-range", fixedMetrics.remainingRangeKm.toFixed(1));
  setText("#trip-summary-eta", etaClock);
}

function updateDashboard(newTelemetry) {
  const telemetryTimestamp = newTelemetry && typeof newTelemetry === "object"
    ? newTelemetry.__timestamp ?? parseTelemetryTimestamp(newTelemetry)
    : null;

  const telemetryPayload = { ...newTelemetry };
  delete telemetryPayload.__timestamp;

  // Merge new data into state
  if (telemetryPayload && Object.keys(telemetryPayload).length > 0) {
      state.telemetry = { ...state.telemetry, ...telemetryPayload };
  }

  const t = state.telemetry;
  const speed = typeof t.speed === "number" && Number.isFinite(t.speed)
    ? Math.min(MAX_SPEED_KMH, Math.max(0, t.speed))
    : null;

  // UI Elements
  const dial = $(".speed-dial");
  if (dial) {
    const dialProgress = speed === null ? 0 : (speed / MAX_SPEED_KMH).toFixed(3);
    dial.style.setProperty("--speed-progress", dialProgress);
  }

  setText("#speed-value", speed === null ? "--" : speed.toFixed(1));

  const soc = typeof t.soc === "number" && Number.isFinite(t.soc) ? Math.round(t.soc) : null;
  setText("#soc-value", soc === null ? "--" : String(soc));
  setText("#status-battery", soc === null ? "--" : `${soc}%`);
  syncTripSocFromTelemetry();

  const range = typeof t.range === "number" && Number.isFinite(t.range) ? t.range : null;
  setText("#range-value", range === null ? "--" : range.toFixed(1));
  setText("#hero-range", range === null ? "--" : range.toFixed(1));

  const throttle = typeof t.throttle === "number" && Number.isFinite(t.throttle) ? Math.round(t.throttle) : null;
  setText("#throttle-value", throttle === null ? "--" : `${throttle}%`);

  const brake = typeof t.brake === "number" && Number.isFinite(t.brake) ? Math.round(t.brake) : null;
  setText("#brake-value", brake === null ? "--" : `${brake}%`);

  const consumption = typeof t.consumption === "number" && Number.isFinite(t.consumption) ? t.consumption : null;
  setText("#consumption-value", consumption === null ? "--" : consumption.toFixed(2));

  // Power Consumption graph: approximated from throttle position against the
  // vehicle's rated motor power, since no dedicated instantaneous-power sensor
  // is broadcast on the telemetry topic.
  const power = throttle === null ? null : clamp((throttle / 100) * VEHICLE.motorPowerW, 0, VEHICLE.motorPowerW);

  const ambientTemp = typeof t.ambientTemp === "number" && Number.isFinite(t.ambientTemp) ? t.ambientTemp : null;
  setText("#status-temp", ambientTemp === null ? "--" : `${Math.round(ambientTemp)}°C`);
  setText("#weather-temp", ambientTemp === null ? "--" : `${Math.round(ambientTemp)}°C`);

  const motorTemp = typeof t.motorTemp === "number" && Number.isFinite(t.motorTemp) ? t.motorTemp : null;
  setText("#motor-temp", motorTemp === null ? "--" : `${Math.round(motorTemp)}°C`);
  setText("#vinfo-motor-temp", motorTemp === null ? "--" : `${Math.round(motorTemp)}°C`);

  const tyrePressure = typeof t.tyrePressure === "number" && Number.isFinite(t.tyrePressure) ? t.tyrePressure : null;
  setText("#tyre-pressure", tyrePressure === null ? "--" : `${Math.round(tyrePressure)} kPa`);
  setText("#vinfo-tyre-pressure", tyrePressure === null ? "--" : `${Math.round(tyrePressure)} kPa`);

  const vehicleLoad = typeof t.vehicleLoad === "number" && Number.isFinite(t.vehicleLoad) ? t.vehicleLoad : null;
  setText("#vehicle-load", vehicleLoad === null ? "--" : `${Math.round(vehicleLoad)} kg`);
  setText("#vinfo-vehicle-load", vehicleLoad === null ? "--" : `${Math.round(vehicleLoad)} kg`);

  const batteryHealth = typeof t.batteryHealth === "number" && Number.isFinite(t.batteryHealth) ? t.batteryHealth : null;
  setText("#hero-health", batteryHealth === null ? "--" : `${Math.round(batteryHealth)}%`);
  setText("#modal-health", batteryHealth === null ? "--" : `${Math.round(batteryHealth)}%`);
  setText("#vinfo-battery-health", batteryHealth === null ? "--" : `${Math.round(batteryHealth)}%`);

  const voltage = typeof t.voltage === "number" && Number.isFinite(t.voltage) ? t.voltage : null;
  setText("#modal-voltage", voltage === null ? "--" : `${voltage.toFixed(1)} V`);
  setText("#vinfo-voltage", voltage === null ? "--" : `${voltage.toFixed(1)} V`);

  const coreTemp = typeof t.coreTemp === "number" && Number.isFinite(t.coreTemp) ? t.coreTemp : null;
  setText("#modal-core-temp", coreTemp === null ? "--" : `${Math.round(coreTemp)}°C`);
  setText("#vinfo-core-temp", coreTemp === null ? "--" : `${Math.round(coreTemp)}°C`);

  setText("#modal-soc-value", soc === null ? "--%" : `${soc}%`);

  const driveMode = typeof t.driveMode === "string" && t.driveMode ? t.driveMode : null;
  setText("#hero-mode", driveMode === null ? "--" : driveMode);
  setText("#vinfo-drive-mode", driveMode === null ? "--" : driveMode);

  setText("#vinfo-power", power === null || !Number.isFinite(power) ? "--" : `${Math.round(power)} W`);
  setText("#vinfo-ambient-temp", ambientTemp === null ? "--" : `${Math.round(ambientTemp)}°C`);
  setText("#vinfo-throttle", throttle === null ? "--" : `${throttle}%`);
  setText("#vinfo-brake", brake === null ? "--" : `${brake}%`);

  updateGraphHistory({
    speed,
    distance: range,
    battery: soc,
    consumption,
    power,
  }, telemetryTimestamp);
  renderPerformanceRadar();

  // Battery Silhouette logic
  const batteryColor = soc === null || soc < 15 ? "var(--danger)" : soc < 40 ? "var(--warning)" : "var(--success)";
  $$(".vehicle-soc").forEach((widget) => {
    widget.style.setProperty("--soc-level", soc === null ? "0%" : `${soc}%`);
    widget.style.setProperty("--soc-color", batteryColor);
  });
}

function updateClock() {
  const now = new Date();
  setText("#status-time", now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
}

// --- EVENT BINDING ---
function bindEvents() {
  // Login buttons
  $$('[data-login-role]').forEach(btn => btn.addEventListener("click", () => startLogin(btn.dataset.loginRole)));
  $$('[data-switch-role]').forEach(btn => btn.addEventListener("click", () => startLogin(btn.dataset.switchRole)));

  // Tab bar
  $$(".tab-button").forEach((btn) => btn.addEventListener("click", () => switchTab(btn.dataset.tab)));

  // Control tab
  $$(".control-command-btn").forEach((btn) => btn.addEventListener("click", () => sendControlCommand(btn.dataset.cmd)));

  // Analytics tab
  $("#analytics-refresh")?.addEventListener("click", loadAnalytics);
  $("#analytics-field-select")?.addEventListener("change", loadAnalytics);
  $("#analytics-range-select")?.addEventListener("change", loadAnalytics);
  $("#analytics-export-csv")?.addEventListener("click", () => exportAnalytics("csv"));
  $("#analytics-export-json")?.addEventListener("click", () => exportAnalytics("json"));

  $("#profile-switch").addEventListener("click", () => {
    localStorage.removeItem("tukzie_token");
    resetDeveloperSession();
    $("#dashboard-screen").classList.remove("is-visible");
    $("#login-screen").classList.add("is-visible");
    stopSystemHealthPolling();
    clearDashboardUI();
  });

  // PIN Modal
  $("#pin-submit").addEventListener("click", verifyPin);
  $("#pin-input").addEventListener("keydown", (e) => { if (e.key === "Enter") verifyPin(); });
  $$('[data-close-modal]').forEach(el => el.addEventListener("click", closeModals));

  // Menu
  $("#menu-toggle").addEventListener("click", () => {
    const menu = $("#side-menu");
    menu.classList.toggle("is-open");
  });
  $("#menu-close").addEventListener("click", () => $("#side-menu").classList.remove("is-open"));

  // Theme/Brightness
  $("#theme-select").addEventListener("change", (e) => {
    document.body.className = e.target.value;
  });
  $("#brightness-slider").addEventListener("input", (e) => {
    document.documentElement.style.setProperty("--brightness", e.target.value / 100);
  });
  $("#open-options-button")?.addEventListener("click", openTripSimulationModal);
  $("#options-save-button")?.addEventListener("click", applyTripOptionValues);

  // Graph tabs
  $$(".graph-tabs button").forEach((button) => {
    button.addEventListener("click", () => setGraphType(button.dataset.graph));
  });

  // Modals for Widgets
  $(".battery-widget")?.addEventListener("click", () => openModal("#battery-modal"));
  $('[data-widget="estimated-trip"]')?.addEventListener("click", () => openModal("#trip-modal"));
  $('[data-widget="vehicle-info"]')?.addEventListener("click", () => openModal("#vehicle-modal"));

  // Navigation widget tile controls (live map is always visible; the popup
  // search modal opens only from this explicit button, so panning/zooming
  // the tile's own map never accidentally triggers it).
  $("#nav-tile-search-button")?.addEventListener("click", openNavigationModal);
  $("#open-my-trips-button")?.addEventListener("click", openMyTripsModal);
  $("#nav-tile-range-select")?.addEventListener("change", changeNavTileRange);
  $("#trips-export-button")?.addEventListener("click", exportMyTripsCsv);
  $("#trip-open-nav-button")?.addEventListener("click", openNavigationModal);
  $("#trip-clear-nav-button")?.addEventListener("click", () => {
    clearNavigationDistanceSource();
    validateTripForm();
  });
  $("#nav-search-button")?.addEventListener("click", geocodeNavSearch);
  $("#nav-search-input")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); geocodeNavSearch(); }
  });
  $("#nav-confirm-button")?.addEventListener("click", confirmNavDestination);
  $("#trip-calc-button")?.addEventListener("click", () => {
    // Belt and braces: the button is disabled while the form is incomplete, but
    // re-check here so the estimate can never run on partial inputs.
    if (!validateTripForm()) return;
    refreshTripEstimate();
  });

  // Any edit to the trip form re-runs validation, so Calculate enables the
  // moment the last field is filled in.
  $("#trip-modal")?.querySelectorAll("input, select").forEach((element) => {
    element.addEventListener("input", validateTripForm);
    element.addEventListener("change", validateTripForm);
  });
  const TRIP_LIVE_INPUT_IDS = [
    "#trip-soc-input",
    "#trip-battery-temp-input",
    "#trip-passengers-input",
    "#trip-cargo-input",
    "#trip-distance-input",
    "#trip-avg-speed-input",
    "#trip-elevation-gain-input",
    "#trip-elevation-loss-input",
    "#trip-surface-input",
    "#trip-weather-input",
  ];
  TRIP_LIVE_INPUT_IDS.forEach((selector) => {
    const el = $(selector);
    if (!el) return;
    const eventName = el.tagName === "SELECT" ? "change" : "input";
    el.addEventListener(eventName, refreshTripEstimate);
  });
}

async function telemetryLoop() {
  const data = await telemetryService.getTelemetry();
  updateDashboard(data);
}

function init() {
  bindEvents();
  applyAccessControl();
  loadGraphHistory();
  clearDashboardUI();
  updateClock();
  setGraphType(state.chartType);
  renderPerformanceRadar();
  renderGraph();

  setInterval(updateClock, 1000);
  // Live-data dependency watchdog for the Estimated Trip widget: runs
  // frequently so the 5-second timeout is enforced promptly.
  setInterval(checkTripLiveDataWatchdog, 250);

  if (window.sensorManager && typeof window.sensorManager.connect === "function") {
    window.sensorManager.connect();
  } else {
    // No live sensor connection available: the trip module stays cleared
    // (lastTelemetryUpdateTime is never stamped), consistent with "no
    // calculations without valid live data".
    updateDashboard(state.telemetry);
  }
}

window.addEventListener("telemetry-update", (event) => {
  const payload = event.detail?.telemetry;
  if (payload && typeof payload === "object") {
    lastTelemetryUpdateTime = payload._received_at ? payload._received_at * 1000 : Date.now();
    updateDashboard(mapSensorPayload(Object.fromEntries(Object.entries(payload).filter(([key]) => !key.startsWith("_")))));
  }
});

window.addEventListener("DOMContentLoaded", init);