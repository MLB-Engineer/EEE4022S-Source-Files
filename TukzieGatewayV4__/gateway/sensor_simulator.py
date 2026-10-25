"""
STM sensor simulator — Version 1.

Stands in for the real STM32 sensor node (connected over USB/UART to the
Pi, feeding an aggregator.py that isn't wired up yet) so the rest of the
pipeline — validation, buffering, dashboard — can be built and tested
today without real hardware.

WHERE TO PLUG IN REAL HARDWARE LATER:
    Replace `read_from_stm()` below with real reads from your USB/serial
    link to the STM32 (e.g. pyserial). Everything downstream (schema,
    buffering, dual-broker publish) stays the same — only this one
    function changes.

WHERE TO ADD A NEW SENSOR:
    1. Add the field to TelemetryPacket in gateway/schema.py.
    2. Add a fake-value generator for it in `read_from_stm()` below.
    3. If the dashboard needs to display it, add the matching label in
       the dashboard's sensor_manager.js (Dashboard Team repo).
    No other file needs to change — schema.py is the single source of
    truth for what a telemetry packet contains.

Run (from the repo root, with both TUKZIE_LOCAL_MQTT_PASSWORD and, if you
have it, TUKZIE_HOSTINGER_MQTT_PASSWORD exported):

    python -m gateway.sensor_simulator
"""

from __future__ import annotations

import json
import logging
import math
import random
import time

from gateway.broker import BrokerConnection, load_profile
from gateway.buffering import StoreAndForwardBuffer
from gateway.schema import TelemetryPacket

logger = logging.getLogger("tukzie.sensor_simulator")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

PUBLISH_INTERVAL_SECONDS = 1.0

# Profiles to publish to simultaneously. Version 1 sends to both — no
# link scoring/switching yet, that's gateway/adaptive_scoring.py, saved
# for Version 2. If you don't yet have Hostinger credentials, comment
# out "hostinger_tukzie" here and only local_docker will be used.
TARGET_PROFILES = ["local_docker", "hostinger_tukzie"]


def read_from_stm(elapsed: float) -> TelemetryPacket:
    """Fake sensor reading. Swap this out for a real USB/serial read
    from the STM32 once the aggregator/hardware link exists."""
    return TelemetryPacket(
        cImu=json.dumps({"pitch": round(2.0 * math.sin(elapsed * 0.4), 2), "roll": round(1.0 * math.cos(elapsed * 0.3), 2)}),
        rImu=json.dumps({"pitch": round(1.8 * math.sin(elapsed * 0.4 + 0.2), 2), "roll": round(0.9 * math.cos(elapsed * 0.3 + 0.2), 2)}),
        fRadar=round(max(0.2, 8.0 + 3.0 * math.sin(elapsed * 0.6)), 2),
        bRadar=round(max(0.2, 6.0 + 2.0 * math.cos(elapsed * 0.5)), 2),
        brake=round(max(0.0, 20.0 * abs(math.sin(elapsed * 1.2))), 1),
        light=round(max(0.0, 400 + 200 * math.sin(elapsed * 0.1)), 0),
        lever=random.choice(["neutral", "engaged"]),
        throttle=round(max(0.0, 45.0 + 30.0 * math.sin(elapsed * 1.5)), 1),
        rain=round(max(0.0, random.random() * 5), 2),
        bVoltage=round(52.4 + 0.2 * math.sin(elapsed * 0.45), 2),
        bTemp=round(30.0 + 2.0 * math.sin(elapsed * 0.2), 1),
        bCurrent=round(3.5 + 0.5 * math.sin(elapsed * 0.5), 2),
        bSOC=round(max(10.0, 95.0 - elapsed * 0.01), 1),
        bFault=0,
        sequence=int(elapsed * (1 / PUBLISH_INTERVAL_SECONDS)),
    )


class DualBrokerPublisher:
    """Connects to each named profile once and publishes telemetry to
    all of them. Each profile gets its own StoreAndForwardBuffer, so a
    dropped connection to one broker (e.g. no internet -> Hostinger
    unreachable) never blocks or loses data meant for the other."""

    def __init__(self, profile_names: list[str], client_id_prefix: str = "stm-sim"):
        self._buffers: dict[str, StoreAndForwardBuffer] = {}
        self._conns: dict[str, BrokerConnection] = {}

        for name in profile_names:
            try:
                profile = load_profile(profile_override=name)
                conn = BrokerConnection(profile, client_id=f"{client_id_prefix}-{name}")
                conn.connect()
                conn.client.loop_start()
            except Exception as e:
                logger.error("Could not set up profile '%s': %s — skipping it for now.", name, e)
                continue

            self._conns[name] = conn
            self._buffers[name] = StoreAndForwardBuffer(
                publish_fn=self._make_publish_fn(conn),
                db_path=f"buffer_{name}.sqlite3",
            )

        if not self._conns:
            raise RuntimeError("No broker profiles connected — check config/config.yaml and your env vars.")

    @staticmethod
    def _make_publish_fn(conn: BrokerConnection):
        def _publish(payload: dict) -> None:
            info = conn.client.publish(conn.topic("telemetry"), json.dumps(payload), qos=1)
            info.wait_for_publish(timeout=5)
            if not info.is_published():
                raise RuntimeError(f"publish to {conn.profile.name} did not confirm")
        return _publish

    def send(self, packet: TelemetryPacket, extra_fields: dict | None = None) -> None:
        payload = packet.to_dict()
        if extra_fields:
            payload.update(extra_fields)
        for name, buf in self._buffers.items():
            went_live = buf.send(payload)
            if not went_live:
                logger.info("queued for later delivery on profile '%s'", name)

    def flush_all(self) -> None:
        for name, buf in self._buffers.items():
            buf.flush()


def main():
    publisher = DualBrokerPublisher(TARGET_PROFILES)
    start = time.time()
    logger.info("STM sensor simulator running. Publishing every %.1fs. Ctrl+C to stop.", PUBLISH_INTERVAL_SECONDS)

    try:
        while True:
            elapsed = time.time() - start
            packet = read_from_stm(elapsed)
            publisher.send(packet)
            logger.info("published telemetry seq=%s", packet.sequence)
            time.sleep(PUBLISH_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logger.info("Stopping simulator, flushing any queued payloads...")
        publisher.flush_all()


if __name__ == "__main__":
    main()
