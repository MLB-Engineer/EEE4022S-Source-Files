#!/usr/bin/env python3
"""SocketCAN -> decoded sensor snapshots -> one MQTT broker (QoS 1).

Requires the ORIGINAL tools/pi/can_listen.py, tools/gen_messages.py and CAN
catalogue, plus paho-mqtt >= 2.0. The CAN decoder is not reimplemented.

Run from the repository root:
    python3 can_mqtt_bridge_fixed.py --config can_mqtt_config.json --show-can

This bridge has one writer for CAN, and an independent MQTT queue sender.
Messages are stored in SQLite until the broker acknowledges them. QoS 1 is
at-least-once delivery, so a message may be duplicated around a crash/reconnect.
"""

import argparse
import importlib.util
import json
import logging
import math
import os
from pathlib import Path
import socket
import ssl
import sqlite3
import struct
import sys
import threading
import time

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit('Missing paho-mqtt: python3 -m pip install "paho-mqtt>=2"')

LOG = logging.getLogger('can-mqtt')
BASE = Path(__file__).resolve().parent


def load_listener(path):
    """Load the proven decoder without changing its catalogue/field scales."""
    if not path.is_file():
        raise FileNotFoundError(f'CAN listener not found: {path}')
    spec = importlib.util.spec_from_file_location('tukzie_can_listen', path)
    if not spec or not spec.loader:
        raise RuntimeError(f'Cannot load CAN listener: {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Preserved MQTT/dashboard aliases from the ORIGINAL bridge.
ALIASES = {
    'throttle_pct': 'throttle', 'ambient_light_lx': 'light',
    'rain_pct': 'rain', 'brake_pct': 'brake',
    'front_radar_m': 'fRadar', 'rear_radar_m': 'bRadar',
    'battery_voltage_v': 'bVoltage', 'battery_temp_c': 'bTemp',
    'battery_current_a': 'bCurrent', 'battery_soc_pct': 'bSOC',
    'battery_fault': 'bFault',
}
IMU_MESSAGES = {'dash_imu': 'cImu', 'cab_imu': 'cImu', 'rear_imu': 'rImu'}


def valid_value(value):
    if value is None:
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return True  # Zero and False are real sensor readings.


class ActiveSensors:
    """Store latest decoded readings; include them if refreshed within hold s."""

    def __init__(self, hold):
        if not math.isfinite(hold) or hold < 0:
            raise ValueError('active_hold_seconds must be a finite nonnegative number')
        self.hold = hold
        self.latest = {}
        self.last_seen = {}
        self.imu = {}
        self.imu_last_seen = {}

    def update(self, name, decoded, now):
        if name in IMU_MESSAGES:
            key = IMU_MESSAGES[name]
            values = {k: v for k, v in decoded.items() if valid_value(v)}
            if values:
                self.imu[key] = values
                self.imu_last_seen[key] = now
            return
        for key, value in decoded.items():
            if not valid_value(value):
                continue
            mqtt_key = ALIASES.get(key, key)
            self.latest[mqtt_key] = value
            self.last_seen[mqtt_key] = now

    def packet(self, sequence, now):
        packet = {'sequence': sequence, 'timestamp': time.time()}
        for key, value in self.latest.items():
            if now - self.last_seen[key] <= self.hold:
                packet[key] = value
        for key, values in self.imu.items():
            if now - self.imu_last_seen[key] <= self.hold:
                # Retained for compatibility: dashboard expects IMU as JSON text.
                packet[key] = json.dumps(values, separators=(',', ':'), allow_nan=False)
        return packet


def load_dotenv(path):
    """Read the same simple .env format as the working tukzie_mqtt.py."""
    if not path.is_file():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        name, value = line.split('=', 1)
        value = value.strip()
        if value[:1] in ("\"", "'") and value.count(value[0]) >= 2:
            value = value[1:value.index(value[0], 1)]
        else:
            value = value.split(' #', 1)[0].split('\t#', 1)[0].strip()
        os.environ.setdefault(name.strip(), value)


def load_mqtt_credentials(config):
    """Accept password directly or from an environment variable."""
    cfg = dict(config)
    if 'password_env' in cfg:
        variable = cfg['password_env']
        if not isinstance(variable, str) or not variable:
            raise ValueError('mqtt.password_env must name an environment variable')
        cfg['password'] = os.environ.get(variable)
    if cfg.get('username') and not cfg.get('password'):
        raise ValueError('MQTT password missing: set mqtt.password or the mqtt.password_env variable')
    return cfg


class MQTTOutbox:
    """Independent SQLite outbox sender; never blocks the CAN reader on network I/O."""

    def __init__(self, cfg, config_dir):
        self.cfg = load_mqtt_credentials(cfg)
        self.connected = threading.Event()
        self.stopping = threading.Event()
        self.wakeup = threading.Event()
        self.db_lock = threading.Lock()

        queue_path = Path(self.cfg.get('queue_db', 'can_mqtt_queue.sqlite3')).expanduser()
        if not queue_path.is_absolute():
            queue_path = config_dir / queue_path
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = queue_path.resolve()
        # Both main CAN thread and MQTT thread use this single DB connection,
        # serialized by db_lock. check_same_thread=False is safe with the lock.
        self.db = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10)
        self.db.execute('CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
        self.db.commit()

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.cfg.get('client_id', 'tukzie-pi-can'),
        )
        if self.cfg.get('username'):
            self.client.username_pw_set(self.cfg['username'], self.cfg['password'])

        if self.cfg.get('tls', False):
            # tukzie_mqtt.py always enables TLS using ca.crt; same parameters here
            # when tls=true. Never bypass certificate checking by default.
            ca_file = Path(self.cfg.get('ca_cert') or os.environ.get('MQTT_CA_CERT', 'ca.crt')).expanduser()
            if not ca_file.is_absolute():
                ca_file = config_dir / ca_file
            if not ca_file.is_file():
                self.db.close()
                raise FileNotFoundError(f'TLS enabled, but CA certificate not found: {ca_file}')
            self.client.tls_set(ca_certs=str(ca_file), tls_version=ssl.PROTOCOL_TLS_CLIENT)
            if self.cfg.get('tls_skip_hostname_check', os.environ.get('MQTT_TLS_SKIP_HOSTNAME_CHECK') == '1'):
                LOG.warning('MQTT TLS hostname verification DISABLED (diagnostic only)')
                self.client.tls_insecure_set(True)
        else:
            LOG.warning('MQTT TLS is DISABLED. Unlike tukzie_mqtt.py, credentials are sent without TLS.')

        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.worker = threading.Thread(target=self._send_loop, name='mqtt-outbox', daemon=True)
        LOG.info('MQTT target %s:%s topic=%s, SQLite=%s',
                 self.cfg['host'], self.cfg['port'], self.cfg['topic'], self.db_path)
        self.client.connect_async(self.cfg['host'], int(self.cfg['port']), keepalive=30)
        self.client.loop_start()
        self.worker.start()

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.connected.clear()
            LOG.error('MQTT CONNECT REFUSED: %s (check credentials/TLS/ACL)', reason_code)
        else:
            self.connected.set()
            LOG.info('MQTT connected to %s:%s', self.cfg['host'], self.cfg['port'])
        self.wakeup.set()

    def on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        self.connected.clear()
        if not self.stopping.is_set():
            LOG.warning('MQTT disconnected: %s; retrying automatically', reason_code)
        self.wakeup.set()

    def enqueue(self, packet):
        payload = json.dumps(packet, separators=(',', ':'), allow_nan=False)
        with self.db_lock:
            self.db.execute('INSERT INTO outbox(payload) VALUES (?)', (payload,))
            self.db.commit()
        self.wakeup.set()

    def queued_count(self):
        with self.db_lock:
            return self.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]

    def _sleep(self, seconds):
        self.wakeup.wait(seconds)
        self.wakeup.clear()

    def _send_loop(self):
        while not self.stopping.is_set():
            if not self.connected.is_set():
                self._sleep(0.5)
                continue
            with self.db_lock:
                row = self.db.execute('SELECT id, payload FROM outbox ORDER BY id LIMIT 1').fetchone()
            if row is None:
                self._sleep(0.5)
                continue
            row_id, payload = row
            try:
                info = self.client.publish(self.cfg['topic'], payload, qos=1, retain=False)
                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                    LOG.warning('MQTT publish failed (code %s); retained in queue', info.rc)
                    self._sleep(1)
                    continue
                info.wait_for_publish(timeout=4)
                if not info.is_published():
                    LOG.warning('MQTT PUBACK timeout; retained in queue')
                    self._sleep(0.5)
                    continue
            except (RuntimeError, ValueError, OSError) as exc:
                LOG.warning('MQTT publish interrupted (%s); retained in queue', exc)
                self._sleep(0.5)
                continue
            with self.db_lock:
                self.db.execute('DELETE FROM outbox WHERE id = ?', (row_id,))
                self.db.commit()
            LOG.info('MQTT PUBACK received; removed queued message id=%s', row_id)

    def close(self):
        self.stopping.set()
        self.wakeup.set()
        self.worker.join(timeout=6)
        self.client.disconnect()
        self.client.loop_stop()
        if self.worker.is_alive():
            # Avoid concurrent DB use on shutdown even if a library hangs.
            LOG.error('MQTT sender still running; leaving SQLite connection open on exit')
        else:
            with self.db_lock:
                self.db.close()


def resolve_path(file, config_dir):
    path = Path(file).expanduser()
    return path if path.is_absolute() else (config_dir / path).resolve()


def run(config, config_dir, show_can=False):
    mqtt_cfg = config['mqtt']
    if not mqtt_cfg.get('host') or not mqtt_cfg.get('topic'):
        raise ValueError('mqtt.host and mqtt.topic are required')
    port = int(mqtt_cfg.get('port', 1883))
    if not 1 <= port <= 65535:
        raise ValueError('mqtt.port must be between 1 and 65535')
    mqtt_cfg['port'] = port
    interval = float(config.get('publish_interval_seconds', 1))
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError('publish_interval_seconds must be a finite positive number')
    sensors = ActiveSensors(float(config.get('active_hold_seconds', 10)))

    can_cfg = config.get('can', {})
    listener_path = resolve_path(can_cfg.get('listener', 'tools/pi/can_listen.py'), config_dir)
    listener = load_listener(listener_path)
    messages = listener.load_catalogue()
    LOG.info('Loaded %d CAN definitions using %s', len(messages), listener_path)
    iface = can_cfg.get('interface', 'can0')

    sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    try:
        sock.setsockopt(socket.SOL_CAN_RAW, listener.CAN_RAW_FD_FRAMES, 1)
        sock.bind((iface,))
        sock.settimeout(min(0.15, interval / 2))
        out = MQTTOutbox(mqtt_cfg, config_dir)
        try:
            deadline = time.monotonic() + interval
            sequence = 0
            LOG.info('CAN %s: publishing sensor snapshots every %ss (hold=%ss)',
                     iface, interval, sensors.hold)
            while True:
                try:
                    frame = sock.recv(listener.CANFD_MTU)
                except socket.timeout:
                    frame = None
                if frame:
                    # SocketCAN returns 16 bytes (classic) or 72 bytes (FD).
                    if len(frame) not in (listener.CAN_MTU, listener.CANFD_MTU):
                        LOG.warning('Unexpected CAN frame length %d; ignoring', len(frame))
                    else:
                        can_id, length, data = struct.unpack(
                            listener.CANFD_FRAME, frame.ljust(listener.CANFD_MTU, b'\x00')
                        )
                        if can_id & listener.CAN_ERR_FLAG:
                            LOG.warning('CAN bus error frame; check bus status and termination')
                        elif not can_id & listener.CAN_RTR_FLAG:
                            cid = can_id & (0x1FFFFFFF if can_id & listener.CAN_EFF_FLAG else listener.CAN_SFF_MASK)
                            message = messages.get(cid)
                            if message:
                                if length != message.dlc:
                                    # can_listen.py warns and attempts partial decode.
                                    # Do the same rather than dropping an otherwise valid frame.
                                    LOG.warning('CAN 0x%X DLC %d != catalogue %d; decoding available fields',
                                                cid, length, message.dlc)
                                try:
                                    values = listener.decode(message, data[:length])
                                    sensors.update(message.name, values, time.monotonic())
                                    if show_can:
                                        print(f'0x{cid:03X} {message.name}: {listener.fmt(message, values)}', flush=True)
                                except Exception:
                                    LOG.exception('Failed to decode CAN 0x%X; continuing', cid)
                now = time.monotonic()
                if now >= deadline:
                    packet = sensors.packet(sequence, now)
                    if len(packet) > 2:
                        out.enqueue(packet)
                        LOG.info('CAN snapshot queued: seq=%d fields=%s (pending=%d)',
                                 sequence, [k for k in packet if k not in ('sequence', 'timestamp')],
                                 out.queued_count())
                        sequence += 1
                    # Never network-block this loop. No backlog of missed timer ticks.
                    deadline = now + interval
        except KeyboardInterrupt:
            LOG.info('Stopped by user; %d unacknowledged messages remain in SQLite', out.queued_count())
        finally:
            out.close()
    finally:
        sock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', default=str(BASE / 'can_mqtt_config.json'))
    parser.add_argument('--show-can', action='store_true', help='print decoded CAN messages as they arrive')
    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve()
    try:
        load_dotenv(config_path.parent / '.env')
        config = json.loads(config_path.read_text(encoding='utf-8'))
        run(config, config_path.parent, show_can=args.show_can)
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        LOG.error('Bridge failed: %s', exc)
        sys.exit(1)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    main()
