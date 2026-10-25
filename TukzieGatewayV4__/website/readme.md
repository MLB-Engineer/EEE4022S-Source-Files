# Tukzie website — developer-view update

This is an updated copy of the website supplied with your change notes. It remains a FastAPI application with a static HTML/CSS/JavaScript frontend, connected to your existing Mosquitto, Telegraf and InfluxDB 2.x services. No React migration or new hosting service is needed.

## Install into your existing Docker Compose project

1. Back up your existing `website` directory and keep your current Compose file, `.env`, certificates and database volumes.
2. Extract this ZIP. Copy the contents of its `website/` folder into the website build context used by your existing Compose service. Keep any local `.env`, certificates and `users.db` that are not in this ZIP. Do not run `seed_db.py`: that script resets accounts.
3. Keep your working MQTT and InfluxDB settings. Add `INFLUX_UI_URL` to the **website service's environment**. This must be an address your browser can open:
   - On the same PC: `http://localhost:8086`
   - From another PC/phone: `http://YOUR_SERVER_IP:8086`
   - If you already use a reverse proxy: its existing InfluxDB HTTPS URL.
   The backend's `INFLUX_URL` remains the internal Docker address, for example `http://influxdb:8086`. Do not substitute `localhost` for the Docker service name inside the website container.
4. From the directory containing your existing Compose file, list your service names:

   ```sh
   docker compose config --services
   ```

5. Rebuild only the website service. Replace `YOUR_WEBSITE_SERVICE` with its name from step 4:

   ```sh
   docker compose up -d --build --no-deps YOUR_WEBSITE_SERVICE
   docker compose logs --tail=100 YOUR_WEBSITE_SERVICE
   ```

6. Hard-refresh the browser (`Ctrl+F5`) and sign in as Developer with your existing password.
7. Open Overview, System Health, Remote Control and Analytics. Follow `CHECKS.md` for the brief deployment checks.

The supplied ZIP did not contain your Compose file, running credentials, certificates or gateway hardware code. This package therefore preserves the existing integration addresses through environment variables rather than creating a competing Compose stack. `.env.example` shows the available settings; merge the needed entries into the **existing service**, not over your working configuration. A Compose `.env` file only supplies interpolation values: the service must also reference them with `environment:` or `env_file:`.

## Changes

- Login: centred logo/title; developer avatar now **D** (the internal access code remains C).
- Developer Overview: raw STM32 sensor cards only; removed the hero and all eight requested derived widgets. Field-name footnotes are hidden. Each raw value expires after five seconds without an update; IMU objects and JSON strings are both supported.
- Trips: concise empty-state text and a clearly labelled illustrative UCT–CBD preview with example date/time, distance and average speed. The supplied files contained no simulation route, so this is a schematic illustration, not an exact simulation trace or recorded trip.
- System Health: **Disconnected** replaces Critical; added receive rate, message counts, website uptime, active browser count, latest acknowledgement latency, InfluxDB connectivity and latest stored sample. The storage check is cached for ten seconds and infers ingestion from samples; it does not pretend to inspect Telegraf's process.
- Remote Control: four square buttons (Hoot, Left Indicator, Right Indicator, Hazards), independent timestamped feedback, on/off commands, and a gateway message box. Removed developer Lock/Start Engine controls. An acknowledgement timeout is **unconfirmed**, not successful.
- Analytics: removed developer Export JSON and the implementation description; added **Open Database Manager**. History auto-loads, axes have readable labels, CSV handles quotes/commas, old exports clear when a query fails, and late requests cannot overwrite a newer selection.
- Command menu: developer Custom tiles, Trip options and Theme are hidden; Brightness and Profiles remain.
- Developer desktop and mobile layouts are responsive. Existing Guest/Operator panels remain available; shared telemetry/analytics fixes apply to them too.

## Remote-control integration — required for the new commands

The website publishes genuine signed commands; it does not invent acknowledgements. Update the existing gateway subscriber to handle the command names and echo `command_id`, as described in **`gateway/README.md`**. A complete reusable validation/acknowledgement adapter is included in `gateway/command_handler.py`, with `command_protocol.py` as its shared signing helper.

Physical indicator/hooter wiring was not supplied, so you must connect that adapter to your existing vehicle-control functions. Until the gateway supports the protocol, the website correctly shows an acknowledgement timeout. The buttons only change their confirmed on/off state after the gateway responds. After an unconfirmed command, a separate **Send off command** action remains available. The displayed toggle state describes the last acknowledged command in this browser session; it is not hardware feedback or a synchronized state shared across users.

## Configuration reference

| Setting | Purpose |
| --- | --- |
| `SECRET_KEY` | Existing JWT signing secret; required |
| `TUKZIE_DB_PATH` | Persistent SQLite users DB path; preserve your current mount |
| `MQTT_HOST`, `MQTT_PORT` | Broker host and port visible from the website container |
| `MQTT_TLS` | `true` by default; set `false` only for a plain MQTT listener, usually port 1883 |
| `MQTT_USER`, `MQTT_PASSWORD` | Existing broker credentials |
| `MQTT_CA_CERT_PATH` | Existing CA certificate mount |
| `MQTT_TOPIC` | Defaults to `tukzie/telemetry` |
| `MQTT_COMMANDS_TOPIC`, `MQTT_ACKS_TOPIC` | Defaults to `tukzie/commands`, `tukzie/acks` |
| `COMMAND_SHARED_SECRET` | Must match the gateway signing secret |
| `INFLUX_URL`, `INFLUX_TOKEN`, `INFLUX_ORG`, `INFLUX_BUCKET` | Your existing InfluxDB 2.x/Flux connection |
| `INFLUX_MEASUREMENT` | Defaults to `telemetry`, matching the supplied Telegraf config |
| `INFLUX_UI_URL` | Browser-accessible InfluxDB login address; falls back to the website host on port 8086 |

The original local-development TLS policy is retained: with a CA certificate it verifies the CA but skips hostname checking; without the CA it uses unverified TLS. Keep the existing certificate mount. Commands use QoS 1 with `retain=false`; the gateway helper deduplicates IDs during its running process. Broker messages retained from an earlier run are ignored by the website so they cannot be mistaken for current telemetry or acknowledgements. Gateways should publish live telemetry without retention.

## Analytics notes

The existing `infra/telegraf.conf` has been retained. The website reads the `telemetry` measurement and the numeric fields already collected by that configuration. Its plain JSON parser does not automatically store arbitrary string fields such as a JSON-encoded IMU; configure Telegraf separately if those fields are needed in history. Live IMU cards work independently of InfluxDB.

The UI exports the newest **10,000 points** from the selected query and explicitly reports when this cap is reached. Select a shorter time range or use InfluxDB's Data Explorer for larger exports. Numeric series are plotted; textual fields are available through CSV. Times use the browser's local timezone. No telemetry sample values are fabricated when MQTT is unavailable.

## Local development and tests

Python 3.11+ is supported. The dependency now explicitly requires Paho MQTT 2.x because the original code already used its callback-version API.

```sh
python -m pip install -r requirements.txt
# Set SECRET_KEY and your existing environment variables first.
python -m uvicorn main:app --host 0.0.0.0 --port 8080
```

Backend regression tests use a temporary database and mocked MQTT/Influx connections:

```sh
python -m pip install pytest httpx
python -m pytest -q tests
```

See `CHECKS.md` for verified results and the remaining checks against your real containers and vehicle. The `previews/` screenshots use explicitly simulated test responses; they are examples of the updated layout, not experimental measurements.

Reference documentation used for the integration: [Paho MQTT Python](https://eclipse.dev/paho/clients/python/docs/) and [InfluxDB 2.x Flux queries](https://docs.influxdata.com/influxdb/v2/query-data/flux/).
