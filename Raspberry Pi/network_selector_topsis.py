#!/usr/bin/env python3
"""Raspberry Pi four-link TOPSIS selector (Wi-Fi + three USB LTE routers).

Run: sudo --preserve-env=MODEM_USER,MODEM_PASSWORD python3 network_selector_topsis.py
Test without changing OS routes: python3 network_selector_topsis.py --dry-run --once

The device names (not old IPv4 leases) define the identities.  LTE1 usb0 uses
/api/json login; LTE2 usb1 and LTE3 usb2 use /ajax.  Existing policy routing tables 100-103 are reused, not flushed or duplicated.
Missing source rules may be installed at reserved priorities 1210-1213.
Handover timing separates Linux route-update time from post-switch ICMP verification.
"""

import argparse
import csv
import http.client
import json
import math
import os
import re
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

# Corrected physical connections, as of 15 October 2026. IPv4 leases may change.
NETWORKS = {"WiFi": "wlan0", "LTE1": "usb0", "LTE2": "usb1", "LTE3": "usb2"}
EXPECTED_IPS = {"wlan0": "192.168.137.218", "usb0": "192.168.100.122",
                "usb1": "192.168.100.174", "usb2": "192.168.100.127"}
MODEM_API = {"LTE1": "authenticated", "LTE2": "ajax", "LTE3": "ajax"}

PING_TARGET = "31.97.115.21"
PING_COUNT = 5
PING_INTERVAL_SECONDS = 0.2
PING_DEADLINE_SECONDS = 3
CHECK_INTERVAL_SECONDS = 5
SIGNAL_INTERVAL_SECONDS = 5

IPERF_SERVER = "31.97.115.21"
IPERF_PORT = 5202
IPERF_DURATION_SECONDS = 3
CAPACITY_INTERVAL_SECONDS = 120   # Use 60 for more frequent capacity updates.
# Recovery verification runs in the background so it does not block the 5 s TOPSIS loop.
# It checks that the default route uses the chosen device, then sends 1 ICMP echo.
RECOVERY_TIMEOUT_SECONDS = 6.0
RECOVERY_RETRY_DELAY_SECONDS = 0.25
# If the last usable value survives a failed refresh, its age is visible in CSV.
# Set to None to retain it until a successful replacement.
MAX_CAPACITY_CACHE_AGE_SECONDS = 900

WEIGHTS = {"latency": 0.30, "packet_loss": 0.30, "capacity": 0.20,
           "signal_quality": 0.10, "tariff": 0.10}
M = 0.08
TARIFFS = {"WiFi": 0.0, "LTE1": 0.4, "LTE2": 0.4, "LTE3": 0.4}

# Separate selector-managed default, below all of the DHCP route priorities
# shown by the user. Never overwrite the DHCP routes with metrics 50/101/etc.
MANAGED_DEFAULT_METRIC = 10
# Pre-existing tables from `ip route show table 100/101/102/103` on the Pi.
# Only source rules at the reserved priorities below may be managed by us.
# NEVER flush these shared/pre-existing tables.
POLICY = {"wlan0": (103, 1213), "usb0": (100, 1210),
          "usb1": (101, 1211), "usb2": (102, 1212)}
# An older selector used these separate tables with the same rule preferences.
# Remove ONLY its known old rules if encountered, not the tables themselves.
OLD_SELECTOR_TABLE = {"wlan0": 213, "usb0": 210,
                      "usb1": 211, "usb2": 212}
LOG_DIR = Path(__file__).resolve().parent / "network_selector_logs_topsis"
ROUND_LOG = LOG_DIR / "network_log.csv"
EVENT_LOG = LOG_DIR / "network_events.csv"
HANDOVER_LOG = LOG_DIR / "handover_log.csv"

PROBE_LOCK = threading.Lock()  # iperf never runs at the same time as a ping batch.
CACHE_LOCK = threading.Lock()
STOP = threading.Event()
HANDOVER_LOCK = threading.Lock()
HANDOVER_COUNTER = 0
HANDOVER_RUN_ID = uuid.uuid4().hex[:10]  # Avoid ID collisions across selector restarts.
ACTIVE_HANDOVER_ID = None
HANDOVER_THREADS = []
CAPACITY = {name: {"value": None, "measured_at": None,
                   "attempted_at": None, "status": "not_measured", "attempts": 0}
            for name in NETWORKS}
SIGNAL = {name: {"raw": None, "unit": "", "quality": None,
                 "measured_at": None, "status": "not_measured", "attempts": 0}
          for name in NETWORKS}
SESSION = {"token": "", "source": None}
EVENT_FIELDS = ["timestamp", "level", "event", "network", "interface", "detail"]
BASE_FIELDS = ["timestamp", "round", "round_duration_s", "schedule_lag_s",
               "selected_network", "selected_interface", "switch_event", "route_status", "decision",
               "handover_id", "route_switch_duration_ms"]
HANDOVER_FIELDS = ["timestamp", "handover_id", "evaluation_round",
                   "previous_network", "new_network", "previous_interface",
                   "new_interface", "decision", "route_switch_duration_ms",
                   "post_route_verification_ms", "total_handover_ms",
                   "verification_attempts", "status", "verification_target", "detail"]
PER_LINK_FIELDS = ["ip", "available", "reason", "latency_ms", "packet_loss_pct",
                   "ping_attempts", "capacity_mbps", "capacity_age_s", "capacity_status",
                   "capacity_attempts", "signal_raw", "signal_unit", "signal_quality_0_1",
                   "signal_age_s", "signal_status", "signal_attempts", "tariff", "topsis_score"]


def stamp():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def write_event(level, event, name="", detail=""):
    interface = NETWORKS.get(name, name if name in NETWORKS.values() else "")
    row = [stamp(), level, event, name, interface, detail]
    with CACHE_LOCK:
        with EVENT_LOG.open("a", newline="") as f:
            csv.writer(f).writerow(row)
    print(f"[{level}] {event}" + (f" {name}: {detail}" if name or detail else ""), flush=True)


def init_logs():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    for path, fields in ((EVENT_LOG, EVENT_FIELDS),
                         (HANDOVER_LOG, HANDOVER_FIELDS),
                         (ROUND_LOG, BASE_FIELDS + [f"{n.lower()}_{field}"
                                                     for n in NETWORKS for field in PER_LINK_FIELDS])):
        # Avoid silently putting new-format rows under an old header.
        if path.exists() and path.stat().st_size:
            with path.open(newline="") as f:
                if next(csv.reader(f), []) != fields:
                    # Keep earlier log files intact and create a fresh file with
                    # the new schema. Resolve repeated future schema revisions.
                    original = path
                    version = 2
                    while True:
                        candidate = original.with_name(f"{original.stem}_v{version}{original.suffix}")
                        if not candidate.exists() or candidate.stat().st_size == 0:
                            path = candidate
                            break
                        with candidate.open(newline="") as existing:
                            if next(csv.reader(existing), []) == fields:
                                path = candidate
                                break
                        version += 1
                    if original == ROUND_LOG:
                        globals()["ROUND_LOG"] = path
                    elif original == HANDOVER_LOG:
                        globals()["HANDOVER_LOG"] = path
                    elif original == EVENT_LOG:
                        globals()["EVENT_LOG"] = path
        if not path.exists() or path.stat().st_size == 0:
            with path.open("w", newline="") as f:
                csv.writer(f).writerow(fields)


def run(argv, timeout=5):
    """Preserve return code and stderr. Never use shell=True."""
    try:
        return subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 124, "", str(exc))


def attempt(action, label, name):
    """Retry failed measurement once, recording both failures."""
    for index in (1, 2):
        try:
            value = action()
            return value, index
        except Exception as exc:
            # A malformed modem reply or a missing utility must not terminate
            # the continuous selector. KeyboardInterrupt still propagates.
            # Do not log credentials, session token, or unfiltered response body.
            write_event("WARNING", "PROBE_FAILED", name,
                        f"{label} attempt {index}/2: {str(exc)[:160]}")
    return None, 2


def ipv4_for(dev):
    p = run(["ip", "-j", "-4", "addr", "show", "dev", dev], 3)
    if p.returncode:
        return None
    try:
        for entry in json.loads(p.stdout):
            for addr in entry.get("addr_info", []):
                if addr.get("family") == "inet" and addr.get("scope") == "global":
                    return addr.get("local")
    except (TypeError, ValueError):
        pass
    return None


def gateway_for(dev):
    p = run(["ip", "-j", "-4", "route", "show", "default"], 3)
    if p.returncode:
        return None
    try:
        choices = [r for r in json.loads(p.stdout) if r.get("dev") == dev and r.get("gateway")]
        return min(choices, key=lambda r: r.get("metric", 0)).get("gateway") if choices else None
    except (ValueError, TypeError):
        return None


def ip_checked(arguments):
    p = run(["ip", "-4"] + arguments, 4)
    if p.returncode:
        raise RuntimeError(f"ip {' '.join(arguments[:4])}: {p.stderr.strip()[:150]}")


def policy_routes(table):
    """Read existing routes without modifying a user-managed routing table."""
    p = run(["ip", "-j", "-4", "route", "show", "table", str(table)], 3)
    if p.returncode:
        raise RuntimeError(f"cannot read table {table}: {(p.stderr or p.stdout).strip()[:150]}")
    try:
        return json.loads(p.stdout)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"invalid route JSON for table {table}: {exc}") from exc


def policy_rules():
    """Return (preference, source, table) from existing IPv4 source rules."""
    p = run(["ip", "-4", "rule", "show"], 3)
    if p.returncode:
        raise RuntimeError(f"cannot read ip rules: {p.stderr.strip()[:150]}")
    rules = []
    for line in p.stdout.splitlines():
        match = re.search(r"^\s*(\d+):\s+from\s+(\S+).*?\blookup\s+(\S+)", line)
        if match:
            rules.append((int(match.group(1)), match.group(2), match.group(3)))
    return rules


def matches_ip(source_rule, address):
    # `ip rule show` may present a /32 as either X.Y.Z.W or X.Y.Z.W/32.
    return source_rule in (address, address + "/32")


class RouteManager:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.configured = {}
        self.active = None

    def refresh_policy(self, addresses):
        """Reuse tables 100-103; repair mismatched entries, never flush them.

        Existing ip rules are left alone when they already direct each source
        address to the correct table. Only missing source rules are added with
        our reserved preferences. Stale rules owned by the older selector at
        those preferences can be replaced; unknown rules are NEVER deleted.
        """
        for name, dev in NETWORKS.items():
            source, gw = addresses.get(dev), gateway_for(dev)
            params = (source, gw)
            if params == self.configured.get(dev):
                continue
            if not source or not gw:
                self.configured.pop(dev, None)
                write_event("WARNING", "POLICY_UNAVAILABLE", name,
                            "IPv4 address or gateway missing; retry next round")
                continue
            table, pref = POLICY[dev]
            if self.dry_run:
                self.configured[dev] = params
                write_event("INFO", "POLICY_DRY_RUN", name,
                            f"reuse existing table {table}, ip={source}, gateway={gw}")
                continue
            try:
                routes = policy_routes(table)
                default = next((r for r in routes if r.get("dst", "default") == "default"), None)
                if not default or default.get("dev") != dev or default.get("gateway") != gw:
                    ip_checked(["route", "replace", "default", "via", gw,
                                "dev", dev, "src", source, "table", str(table)])
                    write_event("INFO", "POLICY_ROUTE_REPAIRED", name,
                                f"table={table}, default via {gw} dev {dev}")
                # The local 192.168.100.1 modem API must be reached through the
                # correct dongle, even though all three LANs have the same CIDR.
                prefix = "192.168.137.0/24" if dev == "wlan0" else "192.168.100.0/24"
                subnet = next((r for r in routes if r.get("dst") == prefix), None)
                if (not subnet or subnet.get("dev") != dev or
                    subnet.get("prefsrc", subnet.get("src")) != source):
                    ip_checked(["route", "replace", prefix, "dev", dev, "src", source,
                                "scope", "link", "table", str(table)])
                    write_event("INFO", "POLICY_SUBNET_REPAIRED", name,
                                f"table={table}, {prefix} dev {dev} src {source}")
                rules = policy_rules()
                owned = [r for r in rules if r[0] == pref]
                for priority, rule_src, rule_table in owned:
                    if rule_table not in (str(table), str(OLD_SELECTOR_TABLE[dev])):
                        raise RuntimeError(f"rule pref {pref} belongs to an unknown table "
                                           f"{rule_table}; refusing to change it")
                # Any old selector-owned rule at this pref pointing to 210-213
                # must be removed, even if a second correct rule already exists.
                for priority, rule_src, rule_table in owned:
                    if rule_table != str(table) or not matches_ip(rule_src, source):
                        ip_checked(["rule", "del", "pref", str(pref),
                                    "from", rule_src, "table", rule_table])
                        write_event("INFO", "STALE_SELECTOR_RULE_REMOVED", name,
                                    f"pref={pref}, from={rule_src}, table={rule_table}")
                rules = policy_rules()
                if not any(matches_ip(rule_src, source) and rule_table == str(table)
                           for _, rule_src, rule_table in rules):
                    if any(priority == pref for priority, _, _ in rules):
                        raise RuntimeError(f"rule pref {pref} is occupied; refusing to overwrite")
                    ip_checked(["rule", "add", "pref", str(pref), "from", source + "/32",
                                "table", str(table)])
                    write_event("INFO", "POLICY_RULE_ADDED", name,
                                f"from={source}/32 lookup {table}, pref={pref}")
                self.configured[dev] = params
                write_event("INFO", "POLICY_REUSED", name,
                            f"existing table={table}; src={source}; via={gw}")
            except RuntimeError as exc:
                self.configured.pop(dev, None)
                write_event("ERROR", "POLICY_UPDATE_FAILED", name, str(exc))

    def managed_route(self):
        p = run(["ip", "-j", "-4", "route", "show", "default"], 3)
        if p.returncode:
            return None
        try:
            routes = [r for r in json.loads(p.stdout) if r.get("metric") == MANAGED_DEFAULT_METRIC]
            return routes[0] if routes else None
        except (TypeError, ValueError):
            return None

    def drop_managed(self):
        if self.dry_run:
            self.active = None
            return True
        old = self.managed_route()
        if old is None:
            self.active = None
            return True
        p = run(["ip", "-4", "route", "del", "default", "metric",
                 str(MANAGED_DEFAULT_METRIC)], 4)
        if p.returncode:
            write_event("ERROR", "ROUTE_DELETE_FAILED", detail=p.stderr.strip()[:150])
            return False
        self.active = None
        return True

    def select(self, name, addresses):
        dev = NETWORKS[name]
        source, gw = addresses.get(dev), gateway_for(dev)
        if not source or not gw:
            write_event("ERROR", "ROUTE_SWITCH_FAILED", name, "missing source IP/gateway")
            return False
        if self.dry_run:
            self.active = name
            write_event("INFO", "ROUTE_DRY_RUN", name, f"via {gw} src {source}")
            return True
        old = self.managed_route()
        if old and old.get("dev") == dev and old.get("gateway") == gw and old.get("prefsrc", old.get("src")) == source:
            self.active = name
            return True
        # Delete only our metric-10 route first to prevent multiple competing
        # metric-10 defaults when switching between the three LTE interfaces.
        # DHCP-managed routes (metric 50, 101, 102, 103, 600, etc.) remain.
        if old and not self.drop_managed():
            return False
        p = run(["ip", "-4", "route", "add", "default", "via", gw,
                 "dev", dev, "src", source, "metric", str(MANAGED_DEFAULT_METRIC)], 4)
        new = self.managed_route()
        if p.returncode or not new or new.get("dev") != dev:
            # Restore previous selected route when the new one could not be added.
            if old and old.get("dev") and old.get("gateway"):
                restore = ["ip", "-4", "route", "replace", "default", "via",
                           old["gateway"], "dev", old["dev"]]
                oldsrc = old.get("prefsrc", old.get("src"))
                if oldsrc:
                    restore += ["src", oldsrc]
                restore += ["metric", str(MANAGED_DEFAULT_METRIC)]
                run(restore, 4)
            write_event("ERROR", "ROUTE_SWITCH_FAILED", name,
                        f"{p.stderr.strip()[:160]}; route after update={new}")
            return False
        self.active = name
        write_event("INFO", "ROUTE_SWITCHED", name,
                    f"default via {gw} dev {dev} src {source}, metric {MANAGED_DEFAULT_METRIC}")
        return True


def ping_probe(dev):
    argv = ["ping", "-n", "-I", dev, "-c", str(PING_COUNT),
            "-i", str(PING_INTERVAL_SECONDS), "-W", "1",
            "-w", str(PING_DEADLINE_SECONDS), PING_TARGET]
    p = run(argv, PING_DEADLINE_SECONDS + 1.2)
    output = p.stdout + "\n" + p.stderr
    loss = re.search(r"(\d+(?:\.\d+)?)%\s*packet loss", output)
    rtt = re.search(r"(?:rtt|round-trip)\s+min/avg/max/(?:mdev|stddev)\s*=\s*"
                    r"[\d.]+/([\d.]+)/", output)
    if loss is None or p.returncode not in (0, 1):
        raise RuntimeError(f"ping rc={p.returncode}; {output.strip()[-110:]}")
    percentage = float(loss.group(1))
    if percentage >= 100:
        return {"available": False, "latency": None, "loss": 100.0,
                "reason": "100_percent_ping_loss"}
    if not rtt:
        raise RuntimeError("ping received replies but average RTT is missing")
    return {"available": True, "latency": float(rtt.group(1)),
            "loss": percentage, "reason": "ok"}


def probe_ping(name, address):
    if not address:
        return {"available": False, "latency": None, "loss": None,
                "reason": "no_ipv4", "attempts": 0}
    result, n = attempt(lambda: ping_probe(NETWORKS[name]), "ping", name)
    if result is None:
        return {"available": False, "latency": None, "loss": None,
                "reason": "ping_command_failed", "attempts": n}
    result["attempts"] = n
    if not result["available"]:
        write_event("WARNING", "LINK_UNAVAILABLE", name, result["reason"])
    return result


def signal_wifi():
    p = run(["iw", "dev", "wlan0", "link"], 2)
    if p.returncode:
        raise RuntimeError(f"iw rc={p.returncode}: {p.stderr.strip()[:100]}")
    match = re.search(r"\bsignal:\s*(-?\d+(?:\.\d+)?)\s*dBm", p.stdout)
    if not match:
        raise ValueError("no associated Wi-Fi signal in iw output")
    dbm = float(match.group(1))
    # Heuristic Wi-Fi RSSI scaling. Comparable quality scores are only estimates.
    return dbm, "dBm", max(0.0, min(1.0, (dbm + 100.0) / 50.0))


def signal_ajax(dev, src):
    p = run(["curl", "--silent", "--show-error", "--fail", "--noproxy", "*",
             "--interface", src, "--connect-timeout", "1", "--max-time", "3",
             "-H", "Content-Type: application/json", "-d", '{"funcNo":1001}',
             "http://192.168.100.1/ajax"], 3.7)
    if p.returncode:
        raise RuntimeError(f"curl rc={p.returncode}: {p.stderr.strip()[:110]}")
    try:
        body = json.loads(p.stdout)
    except (TypeError, ValueError):
        raise ValueError("modem /ajax did not return JSON")
    # The LTE2/LTE3 endpoint returns {"results": [{"rssi": 4, ...}], ...}.
    # 'rssi' here is the modem's 0..5 *bar level*, NOT an RSSI dBm reading.
    # Support the real nested-list response as well as direct response variants.
    candidates = [body]
    if isinstance(body, dict):
        for value in body.values():
            if isinstance(value, dict):
                candidates.append(value)
            elif isinstance(value, list):
                candidates.extend(item for item in value if isinstance(item, dict))
    bars = next((candidate["rssi"] for candidate in candidates
                 if isinstance(candidate, dict) and "rssi" in candidate), None)
    try:
        bars = float(bars)
    except (TypeError, ValueError):
        raise ValueError("rssi bars not present")
    if not (0 <= bars <= 5):
        raise ValueError("rssi is not within 0..5 bars")
    return bars, "bars/5", bars / 5.0


def modem_post(src, payload, token=""):
    headers = {"Content-Type": "application/json;charset=UTF-8"}
    if token:
        headers["Authorization"] = token
    conn = http.client.HTTPConnection("192.168.100.1", timeout=3,
                                      source_address=(src, 0))
    try:
        conn.request("POST", "/api/json", json.dumps(dict(payload, sessionId=token)), headers)
        response = conn.getresponse()
        data = response.read()
        if response.status != 200:
            raise RuntimeError(f"modem HTTP status {response.status}")
        try:
            return json.loads(data)
        except ValueError:
            raise ValueError("modem /api/json did not return JSON")
    finally:
        conn.close()


def signal_authenticated(src):
    # A cached session avoids re-login every five seconds. Refresh after an
    # auth rejection or when the USB address changes (which can be a swap).
    if SESSION["source"] != src:
        SESSION["token"], SESSION["source"] = "", src
    if not SESSION["token"]:
        if "MODEM_USER" not in os.environ or "MODEM_PASSWORD" not in os.environ:
            raise RuntimeError("MODEM_USER/MODEM_PASSWORD not set; LTE1 signal skipped")
        login = modem_post(src, {"fid": "login", "username": os.environ["MODEM_USER"],
                                 "password": os.environ["MODEM_PASSWORD"]})
        if not isinstance(login, dict) or login.get("reply") != "ok" or not login.get("session"):
            raise RuntimeError("LTE1 modem login failed")
        SESSION["token"] = login["session"]
    fields = {"signalStrength": -200, "simCardState": "", "internetState": ""}
    answer = modem_post(src, {"fid": "queryFields", "fields": fields}, SESSION["token"])
    if not isinstance(answer, dict) or answer.get("reply") != "ok":
        SESSION["token"] = ""  # Try a new login on next attempt.
        raise RuntimeError("LTE1 modem session rejected or query failed")
    fields_returned = answer.get("fields") or {}
    if not isinstance(fields_returned, dict):
        raise ValueError("LTE1 modem returned no signal fields")
    raw_value = fields_returned.get("signalStrength")
    state = (f"sim={str(fields_returned.get('simCardState', 'unknown'))[:20]}, "
             f"internet={str(fields_returned.get('internetState', 'unknown'))[:20]}")
    try:
        raw = float(raw_value)
    except (TypeError, ValueError):
        raise ValueError(f"LTE1 RSSI unavailable: raw={str(raw_value)[:20]!r}; {state}")
    # LTE1's 'signalStrength' is reported as RSSI in dBm on this hardware.
    # Do not mistake the modem's no-reading sentinel (-200) for weak coverage.
    # Keep the original provisional -120..-70 dBm -> 0..1 scaling unchanged.
    if not math.isfinite(raw) or not (-150 <= raw < 0):
        raise ValueError(f"LTE1 invalid RSSI dBm: raw={str(raw_value)[:20]!r}; {state}")
    quality = max(0.0, min(1.0, (raw + 120.0) / 50.0))
    # A connected LTE radio can still have no internet access; ping determines availability.
    return raw, "RSSI dBm", quality


def probe_signal(name, src):
    now = time.monotonic()
    old = SIGNAL[name]
    if old["measured_at"] is not None and now - old["measured_at"] < SIGNAL_INTERVAL_SECONDS:
        return
    if not src:
        old["status"] = "no_ipv4"
        return
    if name == "WiFi":
        fn = signal_wifi
    elif MODEM_API[name] == "authenticated":
        fn = lambda: signal_authenticated(src)
    else:
        fn = lambda: signal_ajax(NETWORKS[name], src)
    result, tries = attempt(fn, "signal", name)
    old["attempts"] = tries
    if result is None:
        old["status"] = "failed_using_cached" if old["quality"] is not None else "unavailable"
        return
    old.update({"raw": result[0], "unit": result[1], "quality": result[2],
                "measured_at": time.monotonic(), "status": "fresh"})


def iperf_probe(dev):
    # 8-second soft deadline, terminate after a further 2 s (same as the
    # accepted Pi command), plus small subprocess-management allowance.
    argv = ["timeout", "-k", "2s", "8s", "iperf3", "-c", IPERF_SERVER,
            "-p", str(IPERF_PORT), "--bind-dev", dev,
            "-t", str(IPERF_DURATION_SECONDS), "-J"]
    p = run(argv, 11)
    if p.returncode:
        raise RuntimeError(f"iperf3 rc={p.returncode}: {p.stderr.strip()[:110]}")
    try:
        data = json.loads(p.stdout)
        if data.get("error"):
            raise ValueError("iperf3 server/connection returned an error")
        bps = float(data["end"]["sum_received"]["bits_per_second"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid iperf3 receiver JSON: {type(exc).__name__}")
    if not math.isfinite(bps) or bps < 0:
        raise ValueError("invalid iperf3 receiver bitrate")
    return bps / 1e6


def capacity_worker(name, expected_ip):
    dev = NETWORKS[name]
    with PROBE_LOCK:  # Never overlap iperf traffic and ICMP measurements.
        write_event("INFO", "IPERF_STARTED", name, f"{dev} -> {IPERF_SERVER}:{IPERF_PORT}")
        value, tries = attempt(lambda: iperf_probe(dev), "iperf3", name)
        if ipv4_for(dev) != expected_ip:
            # A reconnect/swap happened during the background measurement.
            value = None
            write_event("WARNING", "IPERF_DISCARDED", name,
                        "IPv4 address changed during the capacity test")
        with CACHE_LOCK:
            item = CAPACITY[name]
            item["attempted_at"] = time.monotonic()
            item["attempts"] = tries
            if value is not None:
                item.update({"value": value, "measured_at": time.monotonic(),
                             "status": "fresh"})
            else:
                item["status"] = "failed_using_cached" if item["value"] is not None else "unavailable"
        write_event("INFO" if value is not None else "WARNING",
                    "IPERF_COMPLETED" if value is not None else "IPERF_UNAVAILABLE",
                    name, f"{value:.3f} Mbit/s" if value is not None else "no new capacity value")


def snapshot_capacity(name):
    with CACHE_LOCK:
        c = CAPACITY[name].copy()
    age = time.monotonic() - c["measured_at"] if c["measured_at"] is not None else None
    if age is not None and MAX_CAPACITY_CACHE_AGE_SECONDS is not None and age > MAX_CAPACITY_CACHE_AGE_SECONDS:
        return None, age, "expired", c["attempts"]
    status = c["status"]
    if c["value"] is not None and status == "fresh" and age is not None and age > CAPACITY_INTERVAL_SECONDS:
        status = "cached"
    return c["value"], age, status, c["attempts"]


def schedule_capacity(pings, addresses, running):
    if running is not None and running.is_alive():
        return running
    now = time.monotonic()
    due = []
    with CACHE_LOCK:
        for name, state in CAPACITY.items():
            if not pings[name]["available"] or not addresses.get(NETWORKS[name]):
                continue
            last = state["attempted_at"]
            if last is None or now - last >= CAPACITY_INTERVAL_SECONDS:
                due.append((last if last is not None else -1, name))
        if not due:
            return None
        name = min(due)[1]
        # Mark at dispatch so a thread that has not started can't be dispatched twice.
        CAPACITY[name]["attempted_at"] = now
        CAPACITY[name]["status"] = ("testing_using_cached" if CAPACITY[name]["value"] is not None
                                    else "testing")
    t = threading.Thread(target=capacity_worker,
                         args=(name, addresses.get(NETWORKS[name])), daemon=True,
                         name=f"iperf-{NETWORKS[name]}")
    t.start()
    return t


def calculate_topsis(measurements):
    """Compute weighted TOPSIS closeness for currently reachable links.

    Criteria, weights, and signal-quality calibration are unchanged from SAW:
        latency      (cost)       weight 0.30
        packet loss  (cost)       weight 0.30
        capacity     (benefit)    weight 0.20
        signal       (benefit)    weight 0.10
        tariff       (cost)       weight 0.10

    Steps: vector-normalize the current alternatives by criterion, multiply
    columns by their weights, construct positive/negative ideals, calculate
    Euclidean separation, then closeness = D- / (D+ + D-).

    To avoid unfair rankings, only use criteria with valid measurements for
    *every* eligible network in this round. E.g., if one accessible link has
    no capacity measurement yet, omit capacity from ALL links until available;
    renormalize the remaining weights to sum to one. Cached measurements from
    the existing probe layer count as available. Ineligible links never enter
    the normalization or ideal-point calculation.

    When all available alternatives have exactly the same weighted point,
    use neutral closeness 0.5. With only one reachable link choose 1.0.
    """
    scores = {name: None for name in measurements}
    eligible = {name: data for name, data in measurements.items() if data["available"]}
    if not eligible:
        return scores
    if len(eligible) == 1:
        scores[next(iter(eligible))] = 1.0
        return scores

    # The selector's internal property names differ from criterion labels.
    columns = {"latency": "latency", "packet_loss": "loss",
               "capacity": "capacity", "signal_quality": "signal_quality",
               "tariff": "tariff"}
    costs = {"latency", "packet_loss", "tariff"}
    active_columns = {}
    for criterion, field in columns.items():
        if WEIGHTS[criterion] <= 0:
            continue
        numbers = {}
        for name, item in eligible.items():
            raw = item.get(field)
            try:
                value = float(raw) if raw is not None else float("nan")
            except (TypeError, ValueError, OverflowError):
                break
            if not math.isfinite(value) or value < 0:
                break
            numbers[name] = value
        if len(numbers) == len(eligible):
            active_columns[criterion] = numbers

    # No shared measurement: keep all reachable links tied, rather than
    # inventing zero values or selecting an arbitrary supposedly better one.
    if not active_columns:
        for name in eligible:
            scores[name] = 0.5
        return scores

    weight_sum = sum(WEIGHTS[criterion] for criterion in active_columns)
    weighted = {name: {} for name in eligible}
    ideal_best = {}
    ideal_worst = {}
    for criterion, numbers in active_columns.items():
        denominator = math.sqrt(sum(value * value for value in numbers.values()))
        # A zero-norm column means all values are zero: it has no impact on
        # TOPSIS distances and is safe to assign a normalized value of zero.
        effective_weight = WEIGHTS[criterion] / weight_sum
        for name, value in numbers.items():
            weighted[name][criterion] = (value / denominator * effective_weight
                                         if denominator > 0 else 0.0)
        extrema = [weighted[name][criterion] for name in eligible]
        ideal_best[criterion] = min(extrema) if criterion in costs else max(extrema)
        ideal_worst[criterion] = max(extrema) if criterion in costs else min(extrema)

    for name in eligible:
        d_best = math.sqrt(sum((weighted[name][c] - ideal_best[c]) ** 2
                               for c in active_columns))
        d_worst = math.sqrt(sum((weighted[name][c] - ideal_worst[c]) ** 2
                                for c in active_columns))
        denominator = d_best + d_worst
        scores[name] = d_worst / denominator if denominator > 1e-15 else 0.5
    return scores


def new_handover_id():
    """A monotonically increasing ID; older verification jobs become obsolete."""
    global HANDOVER_COUNTER, ACTIVE_HANDOVER_ID
    with HANDOVER_LOCK:
        HANDOVER_COUNTER += 1
        ACTIVE_HANDOVER_ID = f"{HANDOVER_RUN_ID}-{HANDOVER_COUNTER}"
        return ACTIVE_HANDOVER_ID


def record_handover(handover_id, number, old_name, new_name, decision,
                    route_ms, post_ms, total_ms, attempts, status, detail=""):
    """Write a completed handover row (one per attempted route change)."""
    row = [stamp(), handover_id, number, old_name or "", new_name or "",
           NETWORKS.get(old_name, ""), NETWORKS.get(new_name, ""), decision,
           round(route_ms, 3), round(post_ms, 3) if post_ms is not None else "",
           round(total_ms, 3) if total_ms is not None else "", attempts,
           status, PING_TARGET, detail]
    with HANDOVER_LOCK:
        with HANDOVER_LOG.open("a", newline="") as f:
            csv.writer(f).writerow(row)
    write_event("INFO" if status == "verified" else "WARNING", "HANDOVER_RESULT",
                new_name or old_name or "",
                f"id={handover_id}, status={status}, route_ms={route_ms:.3f}, "
                f"total_ms={total_ms if total_ms is not None else 'N/A'}, "
                f"attempts={attempts}, {detail}")


def default_route_matches(name, expected_src):
    """Verify an unbound packet to the server would use the selected link."""
    cmd = ["ip", "-j", "-4", "route", "get", PING_TARGET]
    p = run(cmd, 2)
    if p.returncode:
        return False, "route_lookup_failed"
    try:
        routes = json.loads(p.stdout)
        route = routes[0]
        if route.get("dev") != NETWORKS[name]:
            return False, "default_route_uses_" + str(route.get("dev", "unknown"))
        source = route.get("prefsrc", route.get("src"))
        if source and source != expected_src:
            return False, "source_ip_changed"
    except (TypeError, ValueError, KeyError, IndexError):
        return False, "route_lookup_unreadable"
    return True, "ok"


def verification_ping():
    """Unbound ping checks the actual system default path after switching."""
    p = run(["ping", "-n", "-c", "1", "-W", "1", "-w", "1", PING_TARGET], 2)
    return p.returncode == 0 and bool(re.search(r"\b1\s+received\b", p.stdout))


def recovery_worker(handover_id, number, old_name, new_name, decision,
                    route_ms, started, route_finished, expected_src):
    """Poll until one post-switch ICMP reply, timeout or supersession.

    Results measure *observed ICMP confirmation* (includes RTT and probe
    scheduling), not zero-downtime application/service switchover.
    """
    attempts, status, detail = 0, "timeout", "no post-switch ICMP reply"
    post_ms, total_ms = None, None
    deadline = route_finished + RECOVERY_TIMEOUT_SECONDS
    while not STOP.is_set() and time.monotonic() < deadline:
        with HANDOVER_LOCK:
            if ACTIVE_HANDOVER_ID != handover_id:
                status, detail = "superseded", "another selection started"
                break
        valid, reason = default_route_matches(new_name, expected_src)
        if not valid:
            # Do not call failure on a transient route resolution issue; give
            # the default route a chance to settle until the timeout.
            detail = reason
        else:
            attempts += 1
            succeeded = verification_ping()
            valid_after, reason_after = default_route_matches(new_name, expected_src)
            with HANDOVER_LOCK:
                is_current = ACTIVE_HANDOVER_ID == handover_id
            if not is_current:
                status, detail = "superseded", "another selection started"
                break
            if succeeded and valid_after:
                finished = time.monotonic()
                post_ms = (finished - route_finished) * 1000
                total_ms = (finished - started) * 1000
                status, detail = "verified", "first ICMP echo reply on new default path"
                break
            detail = reason_after if not valid_after else "ICMP reply not received"
        # An event wait also stops promptly on Ctrl+C.
        STOP.wait(min(RECOVERY_RETRY_DELAY_SECONDS,
                      max(0.0, deadline - time.monotonic())))
    else:
        if STOP.is_set():
            status, detail = "stopped", "selector stopped before verification finished"
    record_handover(handover_id, number, old_name, new_name, decision,
                    route_ms, post_ms, total_ms, attempts, status, detail)


def start_recovery(number, old_name, new_name, decision, route_ms,
                   started, route_finished, expected_src):
    handover_id = new_handover_id()
    write_event("INFO", "HANDOVER_STARTED", new_name,
                f"id={handover_id}, from={old_name}, route_ms={route_ms:.3f}, "
                f"verification={PING_TARGET}")
    t = threading.Thread(target=recovery_worker,
                         args=(handover_id, number, old_name, new_name, decision,
                               route_ms, started, route_finished, expected_src),
                         daemon=True, name=f"handover-{handover_id}")
    HANDOVER_THREADS.append(t)
    # Prune completed workers to avoid accumulating thread references.
    HANDOVER_THREADS[:] = [worker for worker in HANDOVER_THREADS if worker.is_alive() or worker is t]
    t.start()
    return handover_id


def record_without_recovery(number, old_name, new_name, decision, route_ms, status):
    handover_id = new_handover_id()
    record_handover(handover_id, number, old_name, new_name, decision,
                    route_ms, None, None, 0, status, "ICMP verification not started")
    return handover_id


def write_round(number, took, lag, measurements, scores, chosen, changed, route_status, decision,
                handover_id=None, route_switch_ms=None):
    row = [stamp(), number, round(took, 3), round(lag, 3), chosen or "",
           NETWORKS.get(chosen, ""), int(changed), route_status, decision,
           handover_id or "", round(route_switch_ms, 3) if route_switch_ms is not None else ""]
    for name, v in measurements.items():
        row += [v.get(field) if v.get(field) is not None else "" for field in
                ("ip", "available", "reason", "latency", "loss", "ping_attempts",
                 "capacity", "capacity_age", "capacity_status", "capacity_attempts",
                 "signal_raw", "signal_unit", "signal_quality", "signal_age",
                 "signal_status", "signal_attempts", "tariff")]
        row.append(scores[name] if scores[name] is not None else "")
    with ROUND_LOG.open("a", newline="") as f:
        csv.writer(f).writerow(row)


def fast_round(addresses, signal_allowed=None):
    with PROBE_LOCK:
        # Concurrent short pings make the five-second loop achievable when links
        # respond, instead of doing four 3-second deadlines sequentially.
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = {name: pool.submit(probe_ping, name, addresses.get(dev))
                    for name, dev in NETWORKS.items()}
            pings = {name: task.result() for name, task in jobs.items()}
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = {name: pool.submit(probe_signal, name, addresses.get(dev))
                    for name, dev in NETWORKS.items()
                    if addresses.get(dev) and (signal_allowed is None or signal_allowed.get(dev, False))}
            for task in jobs.values():
                task.result()
        if signal_allowed is not None:
            for name, dev in NETWORKS.items():
                if not signal_allowed.get(dev, False):
                    SIGNAL[name]["status"] = "routing_unavailable"
    measurements = {}
    for name, dev in NETWORKS.items():
        ping = pings[name]
        cap, cap_age, cap_status, cap_attempts = snapshot_capacity(name)
        sig = SIGNAL[name]
        sig_age = time.monotonic() - sig["measured_at"] if sig["measured_at"] is not None else None
        measurements[name] = {
            "ip": addresses.get(dev), "available": ping["available"],
            "reason": ping["reason"], "latency": ping["latency"], "loss": ping["loss"],
            "ping_attempts": ping["attempts"], "capacity": cap, "capacity_age": cap_age,
            "capacity_status": cap_status, "capacity_attempts": cap_attempts,
            "signal_raw": sig["raw"], "signal_unit": sig["unit"],
            "signal_quality": sig["quality"], "signal_age": sig_age,
            "signal_status": sig["status"], "signal_attempts": sig["attempts"],
            "tariff": TARIFFS[name]}
    return pings, measurements


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="do not change any routes or rules")
    ap.add_argument("--once", action="store_true", help="perform one evaluation (and no background iperf)")
    args = ap.parse_args()
    if os.geteuid() != 0 and not args.dry_run:
        ap.error("root required to configure policy routes and --bind-dev; use sudo or --dry-run")
    init_logs()
    rm = RouteManager(args.dry_run)
    # Detect previously selected interface if another run of the script left a
    # managed default installed. No other DHCP route metrics are modified.
    previous = rm.managed_route() if not args.dry_run else None
    selected = next((n for n, dev in NETWORKS.items()
                     if previous and previous.get("dev") == dev), None)
    rm.active = selected
    write_event("INFO", "SELECTOR_STARTED", detail=f"method=TOPSIS, w={list(WEIGHTS.values())}, M={M}, "
                f"evaluation={CHECK_INTERVAL_SECONDS}s, capacity={CAPACITY_INTERVAL_SECONDS}s, "
                f"dry_run={args.dry_run}")
    if args.dry_run:
        write_event("WARNING", "DRY_RUN", detail="dry-run does not repair missing source rules or mismatched tables")
    last_addresses = {}
    next_run = time.monotonic()
    number = 0
    worker = None
    try:
        while not STOP.is_set():
            number += 1
            began = time.monotonic()
            lag = max(0.0, began - next_run)
            addresses = {dev: ipv4_for(dev) for dev in NETWORKS.values()}
            for dev, src in addresses.items():
                if src != last_addresses.get(dev):
                    write_event("INFO", "IP_MAPPING", dev,
                                f"old={last_addresses.get(dev)}, current={src}, expected_on_last_check={EXPECTED_IPS[dev]}")
            # A changed lease/swap invalidates potentially mismatched cached
            # raw signal and cached throughput from the previously attached IP.
            for dev, src in addresses.items():
                if dev in last_addresses and src != last_addresses[dev]:
                    n = next(n for n, d in NETWORKS.items() if d == dev)
                    SIGNAL[n].update({"raw": None, "unit": "", "quality": None,
                                      "measured_at": None, "status": "ip_changed", "attempts": 0})
                    with CACHE_LOCK:
                        CAPACITY[n].update({"value": None, "measured_at": None,
                                            "attempted_at": None, "status": "ip_changed", "attempts": 0})
            last_addresses = addresses
            rm.refresh_policy(addresses)
            signal_allowed = {dev: (args.dry_run or
                                   rm.configured.get(dev) == (src, gateway_for(dev)))
                              for dev, src in addresses.items()}
            pings, measurements = fast_round(addresses, signal_allowed)
            scores = calculate_topsis(measurements)
            available = [n for n in NETWORKS if measurements[n]["available"]]
            chosen, switch_event, route_status, decision = selected, False, "unchanged", "held"
            handover_id, route_switch_ms = None, None
            if not available:
                decision = "NO_AVAILABLE_LINK"
                if selected is not None:
                    previous_name = selected
                    started_switch = time.monotonic()
                    succeeded = rm.drop_managed()
                    route_switch_ms = (time.monotonic() - started_switch) * 1000
                    if succeeded:
                        selected, switch_event, route_status = None, True, "fallback_to_dhcp"
                    else:
                        route_status = "fallback_failed"
                    handover_id = record_without_recovery(
                        number, previous_name, None, "NO_AVAILABLE_LINK",
                        route_switch_ms, "fallback_to_dhcp" if succeeded else "route_failed")
                write_event("ERROR", "ALL_LINKS_UNAVAILABLE", detail="no healthy ping links; retry next round")
            else:
                best = max(available, key=lambda n: scores[n])
                failover = selected is None or not measurements.get(selected, {}).get("available", False)
                if failover or (best != selected and scores[best] >= scores[selected] + M):
                    target = best
                    previous_name = selected
                    started_switch = time.monotonic()
                    succeeded = rm.select(target, addresses)
                    route_finished = time.monotonic()
                    route_switch_ms = (route_finished - started_switch) * 1000
                    if succeeded:
                        switch_event = target != selected
                        selected = chosen = target
                        route_status = "ok"
                        decision = "FAILOVER" if failover else "HYSTERESIS_SWITCH"
                        if switch_event:
                            write_event("INFO", "SELECTED_LINK_CHANGED", target, decision)
                            if args.dry_run:
                                handover_id = record_without_recovery(
                                    number, previous_name, target, decision,
                                    route_switch_ms, "dry_run")
                            else:
                                handover_id = start_recovery(
                                    number, previous_name, target, decision,
                                    route_switch_ms, started_switch, route_finished,
                                    addresses.get(NETWORKS[target]))
                    else:
                        route_status, decision = "failed", "ROUTE_SWITCH_FAILED"
                        handover_id = record_without_recovery(
                            number, previous_name, target, decision,
                            route_switch_ms, "route_failed")
                else:
                    chosen = selected
                    decision = "HYSTERESIS_HOLD" if best != selected else "BEST_IS_ACTIVE"
                if (selected is not None and route_status == "unchanged"
                        and not rm.select(selected, addresses)):
                    route_status = "failed"
            took = time.monotonic() - began
            print(f"\n--- Evaluation {number}, {took:.2f}s, lag={lag:.2f}s ---")
            for name, v in measurements.items():
                print(f"{name:4} {NETWORKS[name]:5} ip={str(v['ip']):15} "
                      f"{'UP' if v['available'] else 'DOWN':4} "
                      f"rtt={v['latency']}ms loss={v['loss']}% "
                      f"iperf={v['capacity']}Mbps({v['capacity_status']}) "
                      f"signal={v['signal_raw']} {v['signal_unit']} "
                      f"TOPSIS={scores[name]}")
            print(f"ACTIVE={selected or 'NONE'} | {decision} | route={route_status} "
                  f"| route_switch_ms={route_switch_ms if route_switch_ms is not None else 'N/A'} "
                  f"| handover_id={handover_id or 'N/A'}\n", flush=True)
            write_round(number, took, lag, measurements, scores, selected,
                        switch_event, route_status, decision, handover_id, route_switch_ms)
            if args.once:
                break
            worker = schedule_capacity(pings, addresses, worker)
            next_run += CHECK_INTERVAL_SECONDS
            # If all measurement time was consumed, never run a burst of late loops.
            if next_run < time.monotonic():
                write_event("WARNING", "EVALUATION_DELAYED", detail=
                            "probes exceeded 5s target; next round starts immediately")
                next_run = time.monotonic()
            STOP.wait(max(0, next_run - time.monotonic()))
    except KeyboardInterrupt:
        print("\nStopped by user. The last chosen system default route remains installed.")
    finally:
        STOP.set()
        if worker is not None and worker.is_alive():
            worker.join(timeout=12)
        for handover_worker in HANDOVER_THREADS:
            if handover_worker.is_alive():
                handover_worker.join(timeout=2)
        write_event("INFO", "SELECTOR_STOPPED", detail=f"rounds={number}")


if __name__ == "__main__":
    main()
