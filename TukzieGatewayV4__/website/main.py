import asyncio
import csv
import hashlib
import hmac
import io
import json
import os
import ssl
import sys
import threading
import time
import re
import uuid
from collections import deque
from urllib.parse import urlparse
from command_protocol import sign_envelope
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from jose import JWTError, jwt

import database
import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# --- CONFIGURATION (set these in Dokploy environment variables) ---
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY environment variable is not set")

ALGORITHM = "HS256"
PORT = int(os.getenv("PORT", "80"))

# --- INFLUXDB CONFIGURATION (history endpoint, see website/infra/) ---
# Optional: if unset, /api/v1/telemetry/history just returns an error
# instead of crashing the whole app, since Version 1 works fine without it.
INFLUX_URL = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN")
INFLUX_ORG = os.getenv("INFLUX_ORG", "tukzie-dev")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "telemetry")
INFLUX_UI_URL = os.getenv("INFLUX_UI_URL", "")
INFLUX_MEASUREMENT = os.getenv("INFLUX_MEASUREMENT", "telemetry")
HISTORY_LIMIT = 10000
MQTT_TLS = os.getenv("MQTT_TLS", "true").lower() in ("true", "1", "yes")
COMMAND_ACK_TIMEOUT = 10
STARTED_AT = time.time()
telemetry_received = 0
invalid_messages = 0
telemetry_times = deque(maxlen=60000)
field_received_at = {}
_state_lock = threading.RLock()
_commands = {}
_influx_health_cache = {}


# --- MQTT CONFIGURATION ---
#MQTT_HOST = os.getenv("MQTT_HOST", "broker.tukzie.com")
#MQTT_PORT = int(os.getenv("MQTT_PORT", "8883"))
#MQTT_USER = os.getenv("MQTT_USER")
#MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
#MQTT_TOPIC = os.getenv("MQTT_TOPIC", "tukzie/telemetry")
# --- LOCAL MQTT CONFIGURATION ---
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "tukzie/telemetry")
# Control tab: commands out, acks back. Must match config/config.yaml's
# topic names at the repo root (gateway/mock_ack_responder.py is what
# actually validates and acks these).
MQTT_COMMANDS_TOPIC = os.getenv("MQTT_COMMANDS_TOPIC", "tukzie/commands")
MQTT_ACKS_TOPIC = os.getenv("MQTT_ACKS_TOPIC", "tukzie/acks")
# Must match gateway/mock_ack_responder.py's SHARED_SECRET (and
# dashboard/app.py's, if you're still running that too).
COMMAND_SHARED_SECRET = os.getenv("COMMAND_SHARED_SECRET", "test-secret-change-me")
# Default points at the same local broker cert the gateway/dashboard use
# (see broker-config/generate_local_certs.sh at the repo root). Override
# with MQTT_CA_CERT_PATH if you keep certs somewhere else.
MQTT_CA_CERT_PATH = os.getenv(
    "MQTT_CA_CERT_PATH",
    str(BASE_DIR / ".." / "broker-config" / "mosquitto" / "certs" / "ca.crt"),
)

# Safety Check: Ensure credentials exist before trying to connect
if not MQTT_USER or not MQTT_PASSWORD:
    print("[MQTT] WARNING: MQTT_USER or MQTT_PASSWORD not set. Connection will likely fail.")
    
# --- GLOBAL STATE ---
latest_telemetry = {}
telemetry_clients = set()
telemetry_roles = {}   # websocket -> "guest" | "operator" | "developer"
mqtt_client = None
main_loop = None
mqtt_connected = False              # is THIS website connected to the broker?
last_telemetry_at: float | None = None   # last time a telemetry message arrived
                                          # (used to infer whether the gateway
                                          # itself is still publishing)
GATEWAY_HEALTHY_AFTER_SECONDS = 5     # last telemetry within this -> "healthy"
GATEWAY_WARNING_AFTER_SECONDS = 15    # within this -> "warning", else "critical"

_latest_acks: dict[str, dict] = {}
_acks_lock = threading.Lock()

# In-memory only (resets on restart) — a real deployment would persist
# this. Good enough for "who's signed in" on Version 1.
_login_log: list[dict] = []
_login_log_lock = threading.Lock()
LOGIN_LOG_MAX_ENTRIES = 200

# --- LIFESPAN MANAGER (starts MQTT thread once) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_loop
    main_loop = asyncio.get_running_loop()  # Capture the loop here
    
    mqtt_thread_instance = threading.Thread(target=mqtt_thread, daemon=True)
    mqtt_thread_instance.start()
    yield
    if mqtt_client:
        mqtt_client.disconnect()

app = FastAPI(title="Tukzie API", version="1.0.0", lifespan=lifespan)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# Make sure the sqlite database exists on startup.
database.initialize_db()

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(hours=24)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


async def get_current_claims(token: str = Depends(oauth2_scheme)) -> dict:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str | None = payload.get("sub")
        role: str | None = payload.get("role")
        if username is None or role is None:
            raise HTTPException(status_code=401, detail="Invalid session")
        return {"username": username, "role": role}
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid session") from exc


async def get_current_user(claims: dict = Depends(get_current_claims)) -> str:
    """Kept for backward compatibility with anything expecting just the
    username string."""
    return claims["username"]


def require_role(*allowed_roles: str):
    """Dependency factory: 403s unless the caller's JWT role is one of
    allowed_roles. Mirrors the client-side data-access gating in
    app.js's applyAccessControl(), but enforced server-side too — the
    frontend hiding a tab was never actual access control."""

    async def _dependency(claims: dict = Depends(get_current_claims)) -> dict:
        if claims["role"] not in allowed_roles:
            raise HTTPException(status_code=403, detail=f"requires role in {allowed_roles}")
        return claims

    return _dependency


# Exact position is sensitive: only Operator/Developer sessions may receive it.
LOCATION_KEYS = {"lat", "lon", "lng", "latitude", "longitude", "gps", "location"}


def role_from_token(token) -> str:
    """Role in a valid JWT, else 'guest' (Guests have no token)."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("role") or "guest"
    except (JWTError, TypeError):
        return "guest"


def redact_for_role(data: dict, role: str) -> dict:
    if role in ("operator", "developer"):
        return data
    out = {k: v for k, v in data.items() if k not in LOCATION_KEYS}
    if isinstance(out.get("_field_received_at"), dict):
        out["_field_received_at"] = {k: v for k, v in out["_field_received_at"].items() if k not in LOCATION_KEYS}
    return out


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = database.get_user(form_data.username)

    if not user:
        raise HTTPException(status_code=400, detail="Incorrect email or password")

    is_valid = database.verify_password(form_data.password, user["password"])
    if not is_valid:
        raise HTTPException(status_code=400, detail="Incorrect email or password")

    access_token = create_access_token(data={"sub": form_data.username, "role": user["role"]})

    with _login_log_lock:
        _login_log.append({
            "username": form_data.username,
            "role": user["role"],
            "at": datetime.utcnow().isoformat() + "Z",
        })
        del _login_log[:-LOGIN_LOG_MAX_ENTRIES]  # cap growth

    return {"access_token": access_token, "token_type": "bearer", "role": user["role"]}


@app.post("/register")
async def register(username: str, password: str, role: str = "operator", _=Depends(require_role("developer"))):
    """Account creation now requires a valid Developer session — see
    README's "Accounts / login" section for the previous, open version
    of this route."""
    if database.get_user(username):
        raise HTTPException(status_code=400, detail="User already exists")
    database.create_user(username, password, role)
    return {"msg": "User created successfully"}


# --- WEBSOCKET ENDPOINT FOR TELEMETRY ---
@app.websocket("/ws/telemetry")
async def telemetry_websocket(websocket: WebSocket):
    await websocket.accept()
    telemetry_clients.add(websocket)
    telemetry_roles[websocket] = "guest"   # until the client proves otherwise
    print(f"[WS] Client connected. Total clients: {len(telemetry_clients)}")
    
    # Send current snapshot immediately so UI isn't blank
    if latest_telemetry:
        try:
            await websocket.send_json(telemetry_snapshot("guest"))
        except Exception:
            pass
    
    try:
        while True:
            data = await websocket.receive_text()
            # First message from a signed-in client: {"auth": "<JWT>"} upgrades its role.
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            if isinstance(msg, dict) and "auth" in msg:
                role = role_from_token(msg["auth"])
                telemetry_roles[websocket] = role
                if latest_telemetry:
                    await websocket.send_json(telemetry_snapshot(role))
    except WebSocketDisconnect:
        print("[WS] Client disconnected")
    except Exception as e:
        print(f"[WS] Error: {e}")
    finally:
        telemetry_clients.discard(websocket)
        telemetry_roles.pop(websocket, None)
        print(f"[WS] Client removed. Total clients: {len(telemetry_clients)}")


@app.get("/api/v1/telemetry/live")
async def get_telemetry(request: Request):
    """Last real telemetry, with reception times so stale data stays stale.
    Exact location is only included for a signed-in Operator/Developer."""
    auth = request.headers.get("authorization", "")
    role = role_from_token(auth[7:]) if auth.lower().startswith("bearer ") else "guest"
    return telemetry_snapshot(role)


def telemetry_snapshot(role: str = "developer"):
    with _state_lock:
        snap = {**latest_telemetry, "_received_at": last_telemetry_at,
                "_field_received_at": dict(field_received_at), "_server_time": time.time()} if latest_telemetry else {}
    return redact_for_role(snap, role)


@app.get("/api/v1/config")
async def public_config(_=Depends(require_role("operator", "developer"))):
    parsed = urlparse(INFLUX_UI_URL)
    url = INFLUX_UI_URL if parsed.scheme in ("http", "https") and parsed.netloc else None
    return {"influx_ui_url": url, "command_ack_timeout_seconds": COMMAND_ACK_TIMEOUT}


@app.get("/api/v1/telemetry/history")
def get_telemetry_history(
    range: str = "-1h",
    field: str | None = None,
    format: str = "json",
    _=Depends(require_role("operator", "developer")),
):
    """Historical telemetry from InfluxDB (filled in by Telegraf — see
    website/infra/). `range` is any Flux duration string InfluxDB
    accepts, e.g. -1h, -24h, -7d. `field` optionally restricts to one
    sensor field (e.g. bSOC); omit it to get everything. `format=csv`
    returns a downloadable CSV instead of JSON — used by the Analytics
    tab's export button."""
    if not INFLUX_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="INFLUX_TOKEN is not set — the InfluxDB/Telegraf stack "
                    "in website/infra/ isn't wired up yet.",
        )

    if not re.fullmatch(r"-[1-9][0-9]{0,3}[mhd]", range):
        raise HTTPException(status_code=422, detail="Use a range such as -1h, -24h or -7d.")
    if format not in ("json", "csv"):
        raise HTTPException(status_code=422, detail="Unsupported export format.")
    if field and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", field):
        raise HTTPException(status_code=422, detail="Invalid field name.")
    field_filter = f'|> filter(fn: (r) => r._field == {json.dumps(field)})' if field else ""
    # Limit per series, then cap the merged result below. Do not group mixed
    # string/numeric _value columns together (Flux schema collision).
    query = f"""
        from(bucket: {json.dumps(INFLUX_BUCKET)})
          |> range(start: {range})
          |> filter(fn: (r) => r._measurement == {json.dumps(INFLUX_MEASUREMENT)})
          {field_filter}
          |> sort(columns: ["_time"], desc: true)
          |> limit(n: {HISTORY_LIMIT + 1})
    """

    try:
        with InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG, timeout=10000) as client:
            tables = client.query_api().query(query)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"InfluxDB query failed: {e}") from e

    points = [
        {"time": record.get_time().isoformat(), "field": record.get_field(), "value": record.get_value()}
        for table in tables
        for record in table.records
    ]

    points.sort(key=lambda point: point["time"], reverse=True)
    truncated = len(points) > HISTORY_LIMIT
    points = list(reversed(points[:HISTORY_LIMIT]))

    if format == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["time", "field", "value"])
        writer.writeheader()
        writer.writerows(points)
        return PlainTextResponse(
            buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=telemetry_history.csv"},
        )

    return {"points": points, "truncated": truncated, "limit": HISTORY_LIMIT}


@app.get("/api/v1/telemetry/fields")
def get_telemetry_fields(range: str = "-24h", _=Depends(require_role("operator", "developer"))):
    """Names of the fields that actually have stored data in the window, so the
    Analytics dropdown follows whatever the vehicle sends (no code change when
    a sensor is added or removed)."""
    if not INFLUX_TOKEN:
        raise HTTPException(status_code=503, detail="INFLUX_TOKEN is not set.")
    if not re.fullmatch(r"-[1-9][0-9]{0,3}[mhd]", range):
        raise HTTPException(status_code=422, detail="Use a range such as -1h, -24h or -7d.")
    query = f"""
        from(bucket: {json.dumps(INFLUX_BUCKET)})
          |> range(start: {range})
          |> filter(fn: (r) => r._measurement == {json.dumps(INFLUX_MEASUREMENT)})
          |> keep(columns: ["_field"])
          |> group()
          |> distinct(column: "_field")
    """
    try:
        with InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG, timeout=10000) as client:
            tables = client.query_api().query(query)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"InfluxDB query failed: {e}") from e
    fields = sorted({r.get_value() for t in tables for r in t.records if isinstance(r.get_value(), str)})
    return {"fields": fields, "range": range}


# --- CONTROL TAB: sign + publish commands, track their acks ---
def _sign_command(cmd: str, target: str, timestamp: float) -> str:
    payload = f"{cmd}:{target}:{timestamp}"
    return hmac.new(COMMAND_SHARED_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


@app.post("/api/v1/control/command")
async def send_control_command(body: dict, claims=Depends(require_role("operator", "developer"))):
    cmd = body.get("cmd")
    allowed = {"hoot", "left_indicator_on", "left_indicator_off", "right_indicator_on",
               "right_indicator_off", "hazards_on", "hazards_off", "message"}
    # Preserve the pre-existing operator controls only for that role.
    if claims["role"] == "operator":
        allowed |= {"lock", "start_engine"}
    if not isinstance(cmd, str) or cmd not in allowed or body.get("target", "vehicle") != "vehicle":
        raise HTTPException(status_code=422, detail="Unsupported command or target.")
    message = body.get("message")
    if cmd == "message" and (not isinstance(message, str) or not message.strip() or len(message) > 280):
        raise HTTPException(status_code=422, detail="Enter a message of 1–280 characters.")
    if mqtt_client is None or not mqtt_connected:
        raise HTTPException(status_code=503, detail="Disconnected from broker. Command not sent.")

    timestamp = time.time()
    command_id = str(uuid.uuid4())
    payload = {"cmd": cmd, "target": "vehicle", "timestamp": timestamp,
               "command_id": command_id, "schema_version": "0.2.0",
               "auth_token": _sign_command(cmd, "vehicle", timestamp)}
    if cmd == "message":
        payload["message"] = message.strip()
    payload["auth_token_v2"] = sign_envelope(payload, COMMAND_SHARED_SECRET)
    with _acks_lock:
        for key in list(_commands):
            if timestamp - _commands[key]["timestamp"] > 3600:
                del _commands[key]
        if len(_commands) >= 1000:
            del _commands[next(iter(_commands))]
        _latest_acks.pop(cmd, None)
        _commands[command_id] = {"command_id": command_id, "cmd": cmd, "timestamp": timestamp,
            "status": "pending", "username": claims["username"], "role": claims["role"]}
    try:
        result = mqtt_client.publish(MQTT_COMMANDS_TOPIC, json.dumps(payload), qos=1, retain=False)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError("broker publish failed")
    except Exception as exc:
        with _acks_lock:
            _commands.pop(command_id, None)
        raise HTTPException(status_code=503, detail="Broker could not accept the command.") from exc
    return {"published": True, "cmd": cmd, "command_id": command_id,
            "sent_at": timestamp, "timeout_seconds": COMMAND_ACK_TIMEOUT}


@app.get("/api/v1/control/status/{cmd}")
async def get_control_status(cmd: str, command_id: str | None = None,
                             claims=Depends(require_role("operator", "developer"))):
    with _acks_lock:
        if command_id:
            command = _commands.get(command_id)
            if not command or command["cmd"] != cmd or command["username"] != claims["username"]:
                raise HTTPException(status_code=404, detail="Command not found.")
            result = dict(command)
            if result["status"] == "pending" and time.time() - result["timestamp"] > COMMAND_ACK_TIMEOUT:
                result["status"] = "timeout"
            result.pop("username", None)
            result.pop("role", None)
            return result
        return _latest_acks.get(cmd) or {"status": "pending"}


def record_ack(payload):
    """Match this request, never a retained/previous command's acknowledgement."""
    command_id = payload.get("command_id")
    with _acks_lock:
        command = _commands.get(command_id) if isinstance(command_id, str) else None
        if not command:
            # Older operator UI/rig keeps its legacy cmd-only acknowledgement.
            # Developer requests always require an echoed command_id.
            candidates = [c for c in _commands.values() if c["cmd"] == payload.get("cmd")
                          and c["role"] == "operator" and c["status"] == "pending"]
            command = candidates[-1] if candidates and not command_id else None
        if not command or command["cmd"] != payload.get("cmd"):
            return
        now = time.time()
        if command["status"] != "pending" or now - command["timestamp"] > COMMAND_ACK_TIMEOUT:
            return
        status = payload.get("status")
        if status not in ("ack", "rejected", "error", "failed"):
            return
        command.update(status=status, acknowledged_at=now,
                       latency_ms=round((now - command["timestamp"]) * 1000),
                       note=str(payload.get("note", ""))[:500])
        _latest_acks[command["cmd"]] = dict(command)


def influx_health():
    now = time.time()
    if now - _influx_health_cache.get("checked", 0) < 10:
        return _influx_health_cache["result"]
    result = {"status": "not_configured", "last_stored_at": None,
              "storage_age_seconds": None, "ingestion": "unknown"}
    if INFLUX_TOKEN:
        try:
            with InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG, timeout=3000) as client:
                if not client.ping():
                    raise RuntimeError("Database is unavailable")
                result["status"] = "healthy"
                query = f'''from(bucket: {json.dumps(INFLUX_BUCKET)}) |> range(start: -7d)
                    |> filter(fn: (r) => r._measurement == {json.dumps(INFLUX_MEASUREMENT)})
                    |> last()'''
                tables = client.query_api().query(query)
                times = [r.get_time() for t in tables for r in t.records]
                if times:
                    latest = max(times)
                    age = max(0, now - latest.timestamp())
                    result.update(last_stored_at=latest.isoformat(), storage_age_seconds=age,
                                  ingestion="healthy" if age <= 15 else "warning")
                else:
                    result["ingestion"] = "no_data"
        except Exception:
            result["status"] = "disconnected"
    _influx_health_cache.update(checked=now, result=result)
    return result


@app.get("/api/v1/system/health")
async def get_system_health(_=Depends(require_role("developer"))):
    now = time.time()
    with _state_lock:
        age = now - last_telemetry_at if last_telemetry_at else None
        rate = sum(t > now - 10 for t in telemetry_times) / min(10, max(1, now - STARTED_AT))
    gateway_status = ("disconnected" if age is None or age > GATEWAY_WARNING_AFTER_SECONDS
                      else "healthy" if age <= GATEWAY_HEALTHY_AFTER_SECONDS else "warning")
    with _acks_lock:
        confirmed = [c for c in _commands.values() if "acknowledged_at" in c]
        last_ack = max(confirmed, key=lambda c: c["acknowledged_at"]) if confirmed else None
    return {
        "web_to_broker": "healthy" if mqtt_connected else "disconnected",
        "gateway_to_broker": gateway_status, "seconds_since_last_telemetry": age,
        "telemetry_messages": telemetry_received, "telemetry_rate_hz": round(rate, 2),
        "invalid_messages": invalid_messages, "browser_connections": len(telemetry_clients),
        "uptime_seconds": round(now - STARTED_AT),
        "last_ack_latency_ms": last_ack["latency_ms"] if last_ack else None,
        "database": await asyncio.to_thread(influx_health),
        "checked_at": datetime.utcnow().isoformat() + "Z",
    }


@app.get("/api/v1/system/logs")
async def get_system_logs(_=Depends(require_role("developer"))):
    with _login_log_lock:
        return {"logins": list(reversed(_login_log))}


@app.middleware("http")
async def no_stale_frontend(request, call_next):
    """Make browsers revalidate the page, CSS and JS on every load so an
    updated deployment is never shown with an old cached stylesheet/script."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/") or (request.method == "GET" and "." in path.rsplit("/", 1)[-1]):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _asset_version() -> str:
    """Hash of the css/js/html files. Appended to asset URLs so a browser or
    proxy that cached an older stylesheet/script can never be reused."""
    h = hashlib.sha1()
    for f in sorted(STATIC_DIR.rglob("*")):
        if f.is_file() and f.suffix in (".css", ".js", ".html", ".svg"):
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:10]


@app.get("/")
async def serve_index():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    v = _asset_version()
    html = re.sub(r'((?:href|src)="/static/(?:css|js)/[^"?]+)"', lambda m: f'{m.group(1)}?v={v}"', html)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/{filename}")
async def serve_root_files(filename: str):
    file_path = STATIC_DIR / filename
    if file_path.is_file():
        return FileResponse(file_path)
    raise HTTPException(status_code=404, detail="File not found")


# --- MQTT BACKGROUND THREAD ---
def broadcast_to_clients(data: str):
    """Safely send data to WebSocket clients from the MQTT thread.
    Guests get a copy with the exact location removed."""
    if not telemetry_clients or main_loop is None:
        return
    guest_copy = None
    for ws in list(telemetry_clients):
        try:
            if telemetry_roles.get(ws, "guest") in ("operator", "developer"):
                payload = data
            else:
                if guest_copy is None:
                    guest_copy = json.dumps(redact_for_role(json.loads(data), "guest"))
                payload = guest_copy
            # Use call_soon_threadsafe to schedule the send in the main loop
            main_loop.call_soon_threadsafe(
                lambda w=ws, d=payload: asyncio.create_task(w.send_text(d))
            )
        except Exception as e:
            print(f"[WS] Broadcast error: {e}")


def on_mqtt_connect(client, userdata, flags, rc):
    global mqtt_connected
    if rc == 0:
        mqtt_connected = True
        print(f"[MQTT] Connected to {MQTT_HOST}:{MQTT_PORT}")
        client.subscribe(MQTT_TOPIC)
        client.subscribe(MQTT_ACKS_TOPIC)
        print(f"[MQTT] Subscribed to {MQTT_TOPIC} and {MQTT_ACKS_TOPIC}")
    else:
        mqtt_connected = False
        print(f"[MQTT] Connection failed with code {rc}")


def on_mqtt_message(client, userdata, msg):
    global last_telemetry_at, telemetry_received, invalid_messages
    # Retained packets must not appear as new telemetry or command success.
    if getattr(msg, "retain", False):
        return
    try:
        payload = json.loads(msg.payload)
        if not isinstance(payload, dict):
            raise ValueError("Expected an object")
        if msg.topic == MQTT_ACKS_TOPIC:
            record_ack(payload)
            return
        payload = {k: v for k, v in payload.items() if not k.startswith("_")}
        if not payload:
            return
        with _state_lock:
            now = time.time()
            latest_telemetry.update(payload)
            last_telemetry_at = now
            telemetry_received += 1
            telemetry_times.append(now)
            field_received_at.update({key: now for key in payload})
        broadcast_to_clients(json.dumps({**payload, "_received_at": now, "_server_time": now,
                                         "_field_received_at": {key: now for key in payload}}))
    except (ValueError, TypeError, UnicodeDecodeError):
        invalid_messages += 1


def on_mqtt_disconnect(client, userdata, rc):
    global mqtt_connected
    mqtt_connected = False
    if rc != 0:
        print(f"[MQTT] Unexpected disconnection (code {rc}), will retry...")


def mqtt_thread():
    """Runs MQTT client in a background thread."""
    global mqtt_client
    
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
    if MQTT_USER:
        mqtt_client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    
    # TLS with your CA certificate
    cert_path = Path(MQTT_CA_CERT_PATH)
    if not MQTT_TLS:
        print("[MQTT] Using plain MQTT (MQTT_TLS=false)")
    elif cert_path.exists():
        mqtt_client.tls_set(ca_certs=str(cert_path))
        # The server cert is CN=localhost (see
        # broker-config/generate_local_certs.sh), but in the Docker
        # compose setup we connect via the container name ("mosquitto")
        # instead — same reasoning as Telegraf's insecure_skip_verify.
        # The CA is still verified above; this only skips the
        # hostname-in-cert check.
        mqtt_client.tls_insecure_set(True)
        print(f"[MQTT] Using CA certificate: {cert_path}")
    else:
        # Fallback: disable verification (for local dev only)
        mqtt_client.tls_set(cert_reqs=ssl.CERT_NONE)
        mqtt_client.tls_insecure_set(True)
        print("[MQTT] WARNING: CA certificate not found, using insecure TLS")
    
    mqtt_client.on_connect = on_mqtt_connect
    mqtt_client.on_message = on_mqtt_message
    mqtt_client.on_disconnect = on_mqtt_disconnect
    
    try:
        mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)
        mqtt_client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
        mqtt_client.loop_forever(retry_first_connection=True)
    except Exception as e:
        print(f"[MQTT] Fatal error: {e}")



print("[MQTT] Background thread started")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")

    