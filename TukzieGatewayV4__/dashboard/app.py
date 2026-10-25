"""
Dashboard control interface backend (Day 6, Tier 1).

Minimal Flask app: buttons in the browser POST to /command, which signs
and publishes via Day 2's CommandPacket/MQTT path. A background MQTT
subscriber listens for acks and stores the latest one per command so the
page can poll /status.

Run (with local_docker broker + mock_ack_responder.py both running):
    TUKZIE_LOCAL_MQTT_PASSWORD=xxxx FLASK_APP=dashboard.app flask run

Requires: pip install flask (not in requirements.txt yet — add it if
you go this route).
"""

import hashlib
import hmac
import json
import threading
import time

from flask import Flask, jsonify, render_template, request

from gateway.broker import BrokerConnection, load_profile

SHARED_SECRET = "test-secret-change-me"  # must match gateway/mock_ack_responder.py

app = Flask(__name__)

_latest_acks: dict[str, dict] = {}
_acks_lock = threading.Lock()


def _sign(cmd: str, target: str, timestamp: float) -> str:
    payload = f"{cmd}:{target}:{timestamp}"
    return hmac.new(SHARED_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _start_ack_listener(conn: BrokerConnection):
    def on_ack(client, userdata, msg):
        ack = json.loads(msg.payload)
        with _acks_lock:
            _latest_acks[ack["cmd"]] = ack

    listener_conn = BrokerConnection(conn.profile, client_id="dashboard-ack-listener")
    listener_conn.connect()
    listener_conn.client.on_message = on_ack
    listener_conn.client.subscribe(listener_conn.topic("acks"))
    threading.Thread(target=listener_conn.client.loop_forever, daemon=True).start()


_profile = load_profile()
_publish_conn = BrokerConnection(_profile, client_id="dashboard-publisher")
_publish_conn.connect()
_start_ack_listener(_publish_conn)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/command", methods=["POST"])
def send_command():
    cmd = request.json["cmd"]
    target = request.json.get("target", "vehicle")
    timestamp = time.time()

    payload = {
        "cmd": cmd,
        "target": target,
        "timestamp": timestamp,
        "auth_token": _sign(cmd, target, timestamp),
        "schema_version": "0.1.0",
    }
    _publish_conn.client.publish(_publish_conn.topic("commands"), json.dumps(payload))
    return jsonify({"published": True, "cmd": cmd})


@app.route("/status/<cmd>")
def status(cmd):
    with _acks_lock:
        ack = _latest_acks.get(cmd)
    return jsonify(ack or {"status": "no ack yet"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
