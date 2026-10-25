# Tukzie Gateway

The Raspberry Pi-side gateway that sits between the vehicle's sensors/actuators
and the MQTT brokers (your local dev broker, and the Tukzie team's Hostinger
broker). It has three jobs:

1. Read sensor data (from the STM32, later) and publish it as telemetry.
2. Receive control commands from the dashboard, validate them, and either
   act on them (reject invalid/replayed/rate-limited ones) or ack them.
3. Publish telemetry to whichever MQTT broker(s) are reachable.

## Version roadmap

| Version | Where it runs | What's new |
|---|---|---|
| **V1 (today)** | Entirely on your laptop | Sensor data is *simulated* in software. Local Mosquitto broker (Docker) + optional dual-publish to the real Hostinger broker. Command validation + mock ack responder + dashboard, all wired together. |
| **V2** | Raspberry Pi 5 | Real STM32 sensor data over USB, adaptive network selection across the 3 USB dongles (`gateway/adaptive_scoring.py`, already scaffolded), real rig instead of the mock ack responder. |

**Version 1 deliberately skips:** real hardware (STM32/USB dongles) and
adaptive network switching. Everything is built so that swapping in real
hardware later only touches the one function documented below — nothing
else in the pipeline changes.

## Architecture (Version 1)

```
[gateway/sensor_simulator.py]  --publishes telemetry-->  [local Mosquitto : Docker, your laptop]
        (fakes STM data)                            \--->  [Hostinger broker : broker.tukzie.com]  (optional, if you have creds)

[dashboard/app.py + templates/index.html]  --publishes command-->  tukzie/commands
        (Flask control UI, buttons)                 <--reads ack--   tukzie/acks

[gateway/command_validation.py]  (run via gateway/mock_ack_responder.py)
        subscribes to tukzie/commands, checks schema + HMAC auth + replay
        age + rate limit, ACCEPTS or REJECTS each command, and if
        accepted, the mock rig publishes a fake ack to tukzie/acks
        (stands in for the real STM/rig hardware until it exists)

[website/main.py]  --subscribes to tukzie/telemetry-->  [local Mosquitto]
        (FastAPI dashboard, its own process)  --relays over WebSocket-->  browser
```

## Repo layout — what each file is for

```
docker-compose.yml            Runs the whole website side (Mosquitto +
                              InfluxDB + Telegraf + website) as one
                              stack. See "Fully containerized" below.
.env.example                   Copy to .env and fill in for the compose
                              stack above.

config/config.yaml           Single source of truth for broker connection
                              details (host, port, TLS, credentials, topic
                              names) for BOTH the local_docker and
                              hostinger_tukzie profiles. Nothing else in
                              the codebase should ever hardcode a host,
                              port, or password — change it here.

gateway/
  broker.py                  Loads a profile from config.yaml and wraps
                              paho-mqtt. Every other module only ever
                              talks to a BrokerConnection object, never
                              to config.yaml directly.
  schema.py                  TelemetryPacket and CommandPacket dataclasses.
                              This is the single source of truth for what
                              fields a telemetry/command payload has.
                              >>> Add new sensors here first. <<<
  sensor_simulator.py         NEW for V1. Fakes STM32 sensor readings and
                              publishes them to BOTH brokers at once via
                              DualBrokerPublisher. Swap `read_from_stm()`
                              for a real serial/USB read once hardware
                              exists — nothing else changes.
  command_validation.py       Validates incoming commands: schema check,
                              HMAC auth, replay-age check, rate limiting.
                              One validate() call path so there's one
                              thing to test.
  mock_ack_responder.py       Stands in for the real STM/rig hardware.
                              Subscribes to commands, runs them through
                              CommandValidator, and publishes a fake ack
                              for anything accepted. Delete this once real
                              hardware acks are wired up — nothing else
                              changes.
  buffering.py                 Store-and-forward: if a publish fails
                              (broker unreachable), the payload is queued
                              in a local SQLite file and retried later via
                              .flush(). Used per-broker inside
                              sensor_simulator.py so a dead Hostinger link
                              never blocks local telemetry.
  dead_letter.py                Quarantines malformed telemetry instead of
                              silently dropping it — republishes it (with
                              a reason) to tukzie/telemetry/rejected.
  adaptive_scoring.py           V2 only, not wired in yet. Scores the 3 USB
                              dongles (signal/latency/loss) and picks the
                              best one with hysteresis, so links don't
                              flap. Fully implemented — just not called
                              from anywhere until the Pi + dongles exist.

dashboard/
  app.py                       Minimal Flask control UI. Buttons POST to
                              /command, which signs + publishes to
                              tukzie/commands, and polls /status/<cmd> for
                              the ack.
  templates/index.html          The actual buttons/page.

broker-config/
  mosquitto/mosquitto.conf      Config for your local Docker Mosquitto
                              broker: TLS + username/password auth.
  mosquitto/acl.conf            Who can publish/subscribe to what, on the
                              local broker. Simplified to one full-access
                              dev user for V1 (see comments in the file
                              for the role-scoped version to switch back
                              to once this leaves your laptop).
  generate_local_certs.sh       One-off script: generates a self-signed
                              CA + server cert for the local broker.

website/
  main.py                        FastAPI app: JWT login, static file
                              serving, the MQTT->WebSocket relay that
                              feeds the live dashboard, and (if
                              INFLUX_TOKEN is set) a /history endpoint
                              backed by InfluxDB. Reads broker/InfluxDB
                              connection details from env vars (not
                              config.yaml — this process is intentionally
                              standalone from gateway/).
  database.py / seed_db.py      SQLite user store (login credentials) for
                              the dashboard's own auth, unrelated to MQTT.
  static/css/styles.css         Theme variables + all styling.
  static/js/sensor_manager.js   WebSocket client. >>> translateSchemaToDashboard()
                              at the top is the one place that maps real
                              gateway sensor fields to what the dashboard
                              displays — extend it here, not in gateway/,
                              as more real sensors come online. <<<
  static/js/app.js              The rest of the dashboard UI (login,
                              trips, settings, graphs).
  static/index.html             Page shell; asset links point at
                              /static/css/... and /static/js/....
  Dockerfile                    What docker-compose.yml (repo root)
                              builds the `website` container from.
  infra/telegraf.conf            Subscribes to tukzie/telemetry, writes
                              every field into InfluxDB automatically.
                              See "Fully containerized" section below.

tests/                          The existing "done when" proof scripts for
                              buffering, command validation, dead-letter,
                              and adaptive scoring. Run with e.g.
                              `python -m tests.test_command_validation`.
```

## How to add a new sensor (or any new data point)

1. **`gateway/schema.py`** — add the field to `TelemetryPacket`.
2. **`gateway/sensor_simulator.py`** — add a fake-value generator for it
   inside `read_from_stm()` (or, once hardware exists, wherever you're
   reading the real value from).
3. **Dashboard display (if needed)** — the Dashboard Team's app reads
   fields by name in `sensor_manager.js`; add the matching label there so
   it shows up on screen.

Nothing else needs to change — `schema.py` is the only place the shape of
a telemetry packet is defined, and everything downstream (buffering,
dead-letter handling, MQTT publish) just serializes whatever's on the
dataclass.

## Running Version 1 today (laptop only)

### 0. Install dependencies
```bash
python3 -m venv venv
source venv/bin/activate         # venv\Scripts\activate on Windows
pip install -r requirements.txt
```

### 1. Bring up your local broker (Docker + TLS)
```bash
bash broker-config/generate_local_certs.sh
mkdir -p broker-config/mosquitto/certs   # if the script didn't already
mosquitto_passwd -c broker-config/mosquitto/passwd dev_publisher
docker run -it -p 8883:8883 \
  -v "$(pwd)/broker-config/mosquitto:/mosquitto/config" \
  eclipse-mosquitto
```
(Don't have `mosquitto_passwd` locally? Run it inside the same Docker image:
`docker run --rm -it -v "$(pwd)/broker-config/mosquitto:/mosquitto/config" --entrypoint mosquitto_passwd eclipse-mosquitto -c /mosquitto/config/passwd dev_publisher`)

### 2. Export the password you just set
```bash
export TUKZIE_LOCAL_MQTT_PASSWORD="whatever you typed into mosquitto_passwd"
# Only if you also want to dual-publish to the real Hostinger broker:
export TUKZIE_HOSTINGER_MQTT_PASSWORD="..."   # see note below
```

### 3. Start the pieces (each in its own terminal, from the repo root)
```bash
python -m gateway.mock_ack_responder      # stands in for the real rig
python -m gateway.sensor_simulator        # fakes STM telemetry -> both brokers
FLASK_APP=dashboard/app.py PYTHONPATH=. flask run
```

Open `http://localhost:5000`, click a button, and you should see the
command get validated, acked by the mock rig, and the dashboard update —
end to end, no hardware required.

### About the Hostinger side

The Cyber Security README documents real, working credentials for the
production Hostinger broker/InfluxDB/SSH access. **Two things worth
doing before you go further:**

- That document explicitly says its credentials "must not be shared with
  untrusted parties or language learning models" — but it was included in
  the zip handed to this assistant. Treat those credentials as
  potentially exposed and **rotate them** (new Mosquitto passwords, new
  InfluxDB token, new root SSH password) before relying on them again,
  and avoid pasting live production secrets into any AI chat going
  forward.
- The `hostinger_tukzie` profile in `config/config.yaml` is written to
  need a **new, scoped username** (not `esp32_publisher` or
  `dashboard_reader`) with its own ACL entries for `tukzie/commands` and
  `tukzie/acks`, since those topics don't exist upstream yet. Get that
  provisioned (or provision it yourself via Dokploy, since you say you
  have access) before setting `TUKZIE_HOSTINGER_MQTT_PASSWORD` — until
  then, just comment `"hostinger_tukzie"` out of `TARGET_PROFILES` in
  `gateway/sensor_simulator.py` and run local-only.

## The website (telemetry dashboard) — how it's wired in

`website/` is the FastAPI dashboard app (login, live telemetry page,
WebSocket relay) you added. It's its own process — it doesn't import
anything from `gateway/` or `dashboard/` — it just reads MQTT, the same
way any other subscriber would. Wiring it in was three small things,
**all inside `website/`, none in `gateway/`:**

1. **Pointed it at the same local broker's TLS cert.** It was hardcoded
   to `website/certs/ca.crt`, a folder that doesn't exist — changed to
   default to `../broker-config/mosquitto/certs/ca.crt` (the same cert
   `gateway/` and `dashboard/` use), overridable via `MQTT_CA_CERT_PATH`.
2. **Reorganized `static/`** into `static/css/` and `static/js/`, and
   updated `index.html`'s asset links to `/static/css/...` and
   `/static/js/...` (served by the existing `StaticFiles` mount — no
   `main.py` routing changes needed).
3. **Bridged the payload-shape mismatch inside `sensor_manager.js`.**   The gateway publishes the real STM schema (`cImu`, `brake`, `bSOC`,
   `bVoltage`, ...). The dashboard's main renderer (in `app.js`) expects
   a different, higher-level shape (`speed`, `soc`, `voltage`,
   `driveMode`, ...). `sensor_manager.js` now has one function,
   `translateSchemaToDashboard()`, that bridges the two — **and only
   maps fields with a genuine real-sensor correspondence** (throttle,
   brake, SoC, voltage, battery temp, drive-mode-from-lever). Anything
   with no real sensor yet (speed, range, solar figures, ...) is left
   undefined, so the UI honestly shows `--` instead of a fabricated
   number, until real sensors or better derivations exist. See the
   comment block at the top of that file for exactly which fields map
   to what, and extend that function (not the gateway) as more real
   sensors come online.

I also fixed one pre-existing bug while wiring this up:
**`TelemetryPacket` had no `to_dict()` method** even though
`buffering.py`'s own docstring assumed it did — `CommandPacket` had one,
`TelemetryPacket` didn't. Added it in `gateway/schema.py`; nothing else
changed.

### Visual redesign — Tukzie Green + a real sensor panel

- **New default theme, "Tukzie Green"** (light, clean, green accent) —
  it's now the `:root` palette in `static/css/styles.css`, so it's what
  loads by default. The old dark theme still exists, renamed to
  `.theme-dark` and still selectable from the theme dropdown ("Neo
  Dark"), alongside the existing "Solar Light" and "Eco Green" options.
- **New "Raw sensor feed (STM32)" panel**, added to `static/index.html`
  right below the existing widgets. Six cards — Front IMU, Rear IMU,
  Radar, Pedals & lever, Ambient, Battery pack — each showing exactly
  one real field from `TelemetryPacket`, with the field name printed on
  the card so it's traceable back to `gateway/schema.py`. These are
  populated directly by a new `updateRawSensorCards()` function in
  `sensor_manager.js`, separate from `translateSchemaToDashboard()` — no
  derivation, no fallback math, just the real numbers (or `--` if a
  field hasn't arrived yet). The old hero widgets (speed dial, trip
  estimator, etc.) are untouched and still work off
  `translateSchemaToDashboard()`'s best-effort mapping.

### Running it locally

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt -r website/requirements.txt

export TUKZIE_LOCAL_MQTT_PASSWORD="..."   # same as before
export SECRET_KEY="pick-any-random-string-for-jwt-signing"
export MQTT_HOST=localhost
export MQTT_PORT=8883
export MQTT_USER=dev_publisher
export MQTT_PASSWORD="$TUKZIE_LOCAL_MQTT_PASSWORD"
export PORT=8000                           # avoid clashing with the Flask control UI on 5000

cd website && python main.py
```

Open `http://localhost:8000` — log in with `operator` / `operator2026` or
`developer` / `developer2026` (from `website/database.py`'s defaults;
run `python seed_db.py` to reset them) — with
`gateway/sensor_simulator.py` running (see above), the dashboard should
show live, updating values for the fields that have a real sensor
behind them; everything else reads `--` until it does.

**One more credential note:** `website/seed_db.py` and
`website/database.py` ship demo passwords
(`operator2026`/`developer2026`/`guest`) similar to what the Cyber
Security README documents as the *production* dashboard's real
credentials. Fine for a local demo db; just don't assume it's a secret,
and use different passwords if this ever runs anywhere but your laptop.
(While wiring up the Control/System Health tabs below, I found the
original `DEFAULT_USERS` hashes in `database.py` didn't actually match
their documented passwords at all — a pre-existing bug that would have
made every login fail on a truly first run, before anyone thought to
run `seed_db.py`. Regenerated them; both paths now log in correctly.
Separately, `database.py` no longer uses `passlib` at all — it broke
against `bcrypt` 4.1+ with a `password cannot be longer than 72 bytes`
crash on login, since passlib is unmaintained and its bcrypt backend
never updated for bcrypt dropping the `__about__` attribute it
depended on. Replaced with direct `bcrypt.hashpw`/`checkpw` calls,
which don't have that problem — tested working against bcrypt 5.0.0.)

### All four processes together (Version 1, fully wired)

```
Terminal 1:  docker run ... eclipse-mosquitto            # the broker
Terminal 2:  python -m gateway.mock_ack_responder         # accept/reject + fake acks
Terminal 3:  python -m gateway.sensor_simulator           # fakes STM data -> MQTT
Terminal 4:  FLASK_APP=dashboard/app.py PYTHONPATH=. flask run   # control UI, :5000
Terminal 5:  cd website && python main.py                 # telemetry dashboard, :8000
```

## Fully containerized: Mosquitto + InfluxDB + Telegraf + website

Everything the website depends on now runs as one Docker Compose stack
(`docker-compose.yml` at the repo root), all on one Docker network
(`tukzie-net`) — so nothing has to cross the host's network stack.
That matters because it's exactly what broke earlier: Telegraf trying
to reach Mosquitto via `host.docker.internal` hit a dead end on WSL2,
since WSL2's automatic localhost-forwarding only covers real `127.0.0.1`
traffic, not Docker's Hyper-V bridge IP — and a broker running inside
your WSL2 distro (rather than natively on Windows) was never reachable
that way. Container-to-container on a shared network sidesteps that
problem entirely; everything below just calls the others by container
name (`mosquitto`, `influxdb`).

```
[mosquitto container] <--tukzie/telemetry-- [gateway/sensor_simulator.py, on your laptop, via localhost:8883]
        |
        | (by container name, same Docker network)
        v
[telegraf container] --writes points--> [influxdb container]
                                                  ^
                                    [website container] ---'
                                    GET /api/v1/telemetry/history
```

- **`docker-compose.yml`** (repo root) — the whole stack: `mosquitto`,
  `influxdb`, `telegraf`, `website`. Only `mosquitto`'s and
  `influxdb`'s ports are published to the host (`8883`, `8086`) so
  `gateway/*.py` (still run directly on your laptop, not
  containerized) and any external Influx UI access keep working
  exactly as before. `website` is published on `8000`.
- **`.env.example`** (repo root) — copy to `.env` and fill in: your
  Mosquitto password, and an InfluxDB admin user/password/token/org/
  bucket of your choosing. `docker compose` reads `.env` in this
  folder automatically — no `--env-file` flag needed here.
- **`website/infra/telegraf.conf`** — unchanged in spirit: subscribes
  to `tukzie/telemetry`, writes every field to InfluxDB automatically.
  Now connects to `mosquitto:8883` (container name) instead of
  `host.docker.internal`, and sets `insecure_skip_verify = true` — the
  broker's TLS cert was generated for `CN=localhost`
  (`broker-config/generate_local_certs.sh`), so connecting by
  container name is a hostname mismatch even though the CA itself is
  still verified (`tls_ca` is still set). Fine for a self-signed local
  dev cert you generated yourself.
- **`website/main.py`** — same reasoning applied to the website's own
  MQTT connection (`tls_insecure_set(True)`), since it now also
  connects via `mosquitto` instead of `localhost`.
- **`website/Dockerfile`** — already existed, unchanged; it's what
  `docker-compose.yml`'s `website` service builds from.

### Bringing it up

```bash
cp .env.example .env        # fill in real values

# 1. Start just the broker first, so you can set its password before
#    anything else tries to connect.
docker compose up -d mosquitto
docker compose exec mosquitto mosquitto_passwd -c /mosquitto/config/passwd dev_publisher
# (enter the same password as MQTT_PASSWORD in your .env)
docker compose restart mosquitto

# 2. Now bring up everything else.
docker compose up -d
```

Check it's all healthy:

```bash
docker compose ps                    # all four should be "running"
docker compose logs -f telegraf      # look for a successful subscribe, not connection errors
```

Your laptop-side processes are unaffected — `config/config.yaml`'s
`local_docker` profile still points at `localhost:8883` (published by
the `mosquitto` service above), so:

```bash
python -m gateway.mock_ack_responder
python -m gateway.sensor_simulator
```

still work exactly as before. Open `http://localhost:8000` for the
website (now running in its own container), log in, and after a
minute or two of the simulator running, try the Analytics tab or:

```bash
curl "http://localhost:8000/api/v1/telemetry/history?range=-15m&field=bSOC"
```

**To rebuild the website after code changes**: `docker compose up -d --build website`.
**To reset everything** (fresh InfluxDB, fresh Mosquitto passwd, fresh
website accounts): `docker compose down -v` — this deletes the named
volumes (`influxdb-data`, `website-users-db`), so you'll redo the
one-time setup above.

## Roles, tabs, and access control

Three roles, matching the JWT's `role` claim:

| Role | Tabs |
|---|---|
| Guest | Overview only |
| Operator | Overview, Trips, Control, Analytics |
| Developer | Overview, Trips, System Health, Control, Analytics |

- **Frontend**: the tab bar (`static/index.html`) reuses the existing
  `data-access="ABC"`-style gating already used elsewhere in the page
  (A=Guest, B=Operator, C=Developer) — `applyAccessControl()` in
  `app.js` hides tabs the current role can't see; `switchTab()` shows
  the matching panel.
- **Backend**: hiding a tab was never real access control, so every new
  endpoint (`/api/v1/control/*`, `/api/v1/system/*`,
  `/api/v1/telemetry/history`) is also gated server-side via a
  `require_role(...)` FastAPI dependency in `main.py`, checked against
  the role embedded in the JWT at login — calling these directly with a
  Guest token now 403s, not just "the button isn't visible."

**Tab-by-tab, what's real vs. placeholder:**
- **Overview** — the raw sensor feed + widgets from before. The
  "Location" card is an honest placeholder (`--`) — there's no GPS
  field in `gateway/schema.py` yet.
- **Trips** — also an honest placeholder for now. No speed/odometer/GPS
  field exists yet to build a real trip log or route map from, so it
  says that plainly rather than inventing distance/duration numbers.
  (The pre-existing "My Trips" *pre-drive simulation* on the Overview
  tab's Navigation widget is a separate, untouched feature — it's a
  planning tool, not a log of real completed trips.)
- **System Health** — fully real, auto-refreshing every 3s:
  website→broker connection state (from the MQTT client's actual
  connect/disconnect callbacks) and gateway→broker health (inferred
  from how long ago the last telemetry message arrived — under 5s
  healthy, under 15s warning, else critical), plus a live login log.
- **Control** — Hoot/Lock/Start Engine publish a real HMAC-signed
  command to `tukzie/commands` (same envelope `gateway/mock_ack_responder.py`
  already validates) and poll for the real ack. No ack within ~6s is
  shown as a failure — that's genuinely what a rejected/lost command
  looks like, not a fake "success."
- **Analytics** — queries InfluxDB via `/api/v1/telemetry/history` for
  a real (if you've got Telegraf running) chart, with time-range
  filtering and CSV/JSON export. A link to InfluxDB's own UI covers
  "further querying"; the digital-twin link is a labeled placeholder.

## Accounts / login

The website has real login (`/token`, JWT-based) backed by
`website/database.py` (SQLite, `website/users.db`, auto-created on
first run). Three demo accounts exist by default (see "credential
note" above): `operator`, `developer`, `guest`.

**To create a new account**, there's a `/register` endpoint — no
signup form in the UI yet, so use curl or similar. It now requires a
**Developer** session (see below):

```bash
# 1. Get a developer token
TOKEN=$(curl -s -X POST http://localhost:8000/token \
  -d "username=developer&password=developer2026" | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# 2. Create the new account
curl -X POST "http://localhost:8000/register?username=you@example.com&password=your-password&role=operator" \
  -H "Authorization: Bearer $TOKEN"
```

`/register` previously had **no** access control at all — anyone who
could reach the site could create a `developer`-role account. Fixed:
it now 401s without a valid token and 403s unless that token's role is
`developer`.

## Moving to the Raspberry Pi 5 (start of Version 2)

Nothing in `gateway/` or `dashboard/` is laptop-specific — it's plain
Python. To move it:

```bash
scp -r tukzie-gateway piadmin@pi5:/home/piadmin/
ssh piadmin@pi5
cd tukzie-gateway
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
```

Then swap `read_from_stm()` in `gateway/sensor_simulator.py` for a real
serial/USB read, delete `gateway/mock_ack_responder.py` once real rig
hardware acks exist, and wire `gateway/adaptive_scoring.py` in once the 3
USB dongles are physically present.
