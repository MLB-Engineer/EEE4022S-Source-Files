"""
Mock ack responder (Day 6) — stands in for the real breadboard rig
(Task 14, Week 6) so the dashboard control interface has something to
talk to before real hardware exists.

Subscribes to the commands topic, validates like the real rig would
(reusing Day 2's CommandValidator), and publishes a fake ack. Throwaway
once the real rig is wired up in Week 8 — swap this script for the
real firmware, nothing else in the pipeline changes.

Run:
    TUKZIE_LOCAL_MQTT_PASSWORD=xxxx python -m gateway.mock_ack_responder
"""

import json
import logging
import time

from gateway.broker import BrokerConnection, load_profile
from gateway.command_validation import CommandValidator

logger = logging.getLogger("tukzie.mock_ack_responder")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SHARED_SECRET = "test-secret-change-me"  # must match dashboard/app.py


def main():
    profile = load_profile()
    conn = BrokerConnection(profile, client_id="mock-ack-responder")
    conn.connect()

    validator = CommandValidator(conn, shared_secret=SHARED_SECRET)

    def on_valid_command(command):
        ack = {
            "cmd": command.command,  # CommandPacket's field is `command`, not `cmd`
            "target": command.target,
            "status": "ack",
            "responded_at": time.time(),
            "note": "mock rig response — no real hardware attached yet",
        }
        conn.client.publish(conn.topic("acks"), json.dumps(ack))
        logger.info("mock rig acked: %s", ack)

    validator.on_valid_command = on_valid_command
    logger.info("Mock ack responder running. Waiting for commands...")
    validator.start()


if __name__ == "__main__":
    main()
