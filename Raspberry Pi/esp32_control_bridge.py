#!/usr/bin/env python3
"""Pi MQTT <-> UART vehicle control bridge for developer command contract 0.2.0.

Requires website-provided command_protocol.py and gateway/command_handler.py.
NO home-made HMAC verification; without those files the bridge refuses to run.
The companion ESP32 firmware esp32_vehicle_controls.ino adds SET commands.

Usage: python3 esp32_control_bridge.py --config esp32_control_config.json
       python3 esp32_control_bridge.py --config esp32_control_config.json --terminal
"""
from __future__ import annotations
import argparse
import importlib
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time

import serial
import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("PiUARTController")

# These commands are implemented in the accompanying revised firmware.
EXPLICIT_COMMANDS = {
    "hoot": "HOOT",
    "left_indicator_on": "LEFT_INDICATOR_ON",
    "left_indicator_off": "LEFT_INDICATOR_OFF",
    "right_indicator_on": "RIGHT_INDICATOR_ON",
    "right_indicator_off": "RIGHT_INDICATOR_OFF",
    "hazards_on": "HAZARDS_ON",
    "hazards_off": "HAZARDS_OFF",
}

class ESP32Controller:
    def __init__(self, port="/dev/ttyUSB0", baudrate=115200, timeout=2.0):
        self.port, self.baudrate, self.timeout = port, baudrate, timeout
        self.ser = None
        self.lock = threading.Lock()

    def connect(self):
        with self.lock:
            if self.ser and self.ser.is_open:
                return
            self.ser = serial.Serial(self.port, self.baudrate, timeout=0.1, write_timeout=2)
            time.sleep(2)  # possible ESP32 auto-reset via USB serial adapter
            self.ser.reset_input_buffer()
            log.info("Connected to ESP32 %s @ %d", self.port, self.baudrate)

    def send_command(self, command: str) -> dict:
        """Exactly one in-flight command; never auto-retry unknown-outcome actions."""
        if not isinstance(command, str) or not command or '\n' in command or '\r' in command or '\0' in command:
            raise ValueError("Invalid serial command")
        expected = "OLED_TEXT" if command.startswith("OLED_TEXT:") else command
        with self.lock:
            if not self.ser or not self.ser.is_open:
                raise RuntimeError("ESP32 serial disconnected")
            try:
                # Suppress unrelated boot/debug lines; no blind retries.
                self.ser.reset_input_buffer()
                self.ser.write((command + "\n").encode("utf-8"))
                self.ser.flush()
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    raw = self.ser.readline()
                    if not raw or len(raw) > 8192:
                        continue
                    try:
                        reply = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(reply, dict) or reply.get("target") != "esp32":
                        continue
                    if reply.get("cmd") != expected or reply.get("status") not in ("ack", "error"):
                        continue
                    return reply
            except (serial.SerialException, OSError):
                self._close_unlocked()
                raise
            # Late reply must not be correlated to a later command.
            self._close_unlocked()
            raise TimeoutError("No matching ESP32 ACK before UART timeout; execution uncertain")

    def _close_unlocked(self):
        if self.ser is not None:
            try:
                self.ser.close()
            finally:
                self.ser = None

    def close(self):
        with self.lock:
            self._close_unlocked()


def import_website_handler(root: Path):
    """Require the website's actual signature/expiry/dedup implementation."""
    for path in (root, root / "gateway"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    try:
        importlib.import_module("command_protocol")
        module = importlib.import_module("command_handler")
        return module.CommandHandler, module.mqtt_callback
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            "Missing website security adapter. Copy command_protocol.py and "
            "gateway/command_handler.py to the Pi with their original imports; "
            "commands will NOT be accepted without them."
        ) from exc


class VehicleCommandBridge:
    def __init__(self, config: dict, adapter_root: Path):
        secret = os.getenv("COMMAND_SHARED_SECRET")
        if not secret:
            raise RuntimeError("Set COMMAND_SHARED_SECRET to the website's actual signing secret")
        CommandHandler, self.mqtt_callback = import_website_handler(adapter_root)
        self.config = config
        sc = config["serial"]
        self.controller = ESP32Controller(sc.get("port", "/dev/ttyUSB0"),
                                          int(sc.get("baudrate", 115200)),
                                          float(sc.get("timeout_seconds", 3)))
        mc = config["mqtt"]
        self.command_topic = os.getenv("MQTT_COMMANDS_TOPIC", mc.get("command_topic", "tukzie/commands"))
        self.ack_topic = os.getenv("MQTT_ACKS_TOPIC", mc.get("ack_topic", "tukzie/acks"))
        if not self.command_topic or not self.ack_topic or self.command_topic == self.ack_topic:
            raise ValueError("MQTT command and ACK topics must be nonempty and different")
        if any('#' in t or '+' in t for t in (self.command_topic, self.ack_topic)):
            raise ValueError("MQTT topics must be concrete names")
        self.handler = CommandHandler(secret=secret, execute=self.apply_vehicle_command)
        self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=mc.get("client_id", "tukzie-pi-esp32-control"))
        user = os.getenv("MQTT_USER", mc.get("username", ""))
        password = os.getenv("MQTT_PASSWORD", mc.get("password", ""))
        if user:
            self.client.username_pw_set(user, password)
        if mc.get("tls", False):
            self.client.tls_set(ca_certs=mc.get("ca_certs") or None)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_disconnect = self.on_disconnect
        self.stop = threading.Event()
        self.host = mc["host"]
        self.port = int(mc.get("port", 1883))

    def apply_vehicle_command(self, cmd: str, message: str | None) -> str:
        """Called ONLY by website CommandHandler after validating envelope.

        Explicit set-state operations, never blind toggles. Firmware state machine
        makes indicators/hazards mutually exclusive; OFF of inactive output is safe.
        """
        if cmd == "message":
            if not isinstance(message, str) or not (1 <= len(message) <= 280):
                raise ValueError("message must be 1-280 characters")
            if any(c in message for c in "\x00\n\r"):
                raise ValueError("message may not contain control line breaks")
            # ESP32 Serial2.readStringUntil('\n') and OLED limitations: preserve
            # original 280 chars in the log, but reject > firmware budget here
            # rather than send a silently-truncated message.
            max_len = int(self.config.get("max_oled_text_bytes", 1024))
            if len(message.encode("utf-8")) > max_len:
                raise ValueError(f"OLED message exceeds configured {max_len} UTF-8 byte limit")
            uart_command = "OLED_TEXT:" + message
        else:
            if cmd not in EXPLICIT_COMMANDS:
                raise ValueError(f"Unsupported vehicle command {cmd!r}")
            uart_command = EXPLICIT_COMMANDS[cmd]

        # If the UART link is down, connect on-demand. Failed commands will be
        # rejected by the website adapter; no success ACK is synthesized.
        if self.controller.ser is None or not self.controller.ser.is_open:
            self.controller.connect()
        reply = self.controller.send_command(uart_command)
        if reply.get("status") != "ack":
            raise RuntimeError(reply.get("note", "ESP32 reported hardware error"))
        return str(reply.get("note") or f"{cmd} completed")

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT connection refused: %s", reason_code)
            return
        client.subscribe(self.command_topic, qos=1)
        log.info("Subscribed to %s; acknowledgement topic %s", self.command_topic, self.ack_topic)

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        log.warning("MQTT disconnected: %s", reason_code)

    def on_message(self, client, userdata, msg):
        if msg.topic != self.command_topic:
            return
        # Do not pre-parse or mutate the signed JSON; pass original message to
        # website adapter, which handles signature verification, freshness,
        # duplicate IDs, retained commands and matching ACKs.
        try:
            self.mqtt_callback(self.handler, client, msg, ack_topic=self.ack_topic)
        except Exception:
            log.exception("Website command handler failed; no success ACK was fabricated")

    def run(self):
        self.client.connect_async(self.host, self.port, keepalive=30)
        self.client.loop_start()
        log.info("Single-broker control bridge running (Ctrl+C to stop)")
        try:
            while not self.stop.wait(1):
                pass
        finally:
            self.controller.close()
            self.client.disconnect()
            self.client.loop_stop()


def terminal(config):
    sc = config["serial"]
    ctl = ESP32Controller(sc.get("port", "/dev/ttyUSB0"), int(sc.get("baudrate", 115200)),
                          float(sc.get("timeout_seconds", 3)))
    try:
        ctl.connect()
        print("UART test mode: HOOT, LEFT_INDICATOR_ON/OFF, RIGHT_INDICATOR_ON/OFF, HAZARDS_ON/OFF, OLED_TEXT:...")
        while True:
            cmd = input("ESP32 > ").strip()
            if cmd.lower() in ("exit", "quit"):
                break
            if cmd:
                print(ctl.send_command(cmd))
    finally:
        ctl.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="esp32_control_config.json")
    ap.add_argument("--adapter-root", default=".", help="directory with command_protocol.py and gateway/command_handler.py")
    ap.add_argument("--terminal", action="store_true", help="local UART diagnostics only (no MQTT)")
    args = ap.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.terminal:
        terminal(config)
    else:
        bridge = VehicleCommandBridge(config, Path(args.adapter_root).resolve())
        try:
            bridge.run()
        except KeyboardInterrupt:
            bridge.stop.set()

if __name__ == "__main__":
    main()
