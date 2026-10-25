"""
Command validation + rate limiting (Task 9, Day 2).

Single validate() call path handles schema, auth, replay, AND rate
limiting — deliberately one path, not bolted-on separately, so there's
one thing to test and one place rejection logging happens.

Usage:
    conn = BrokerConnection(load_profile())
    validator = CommandValidator(conn, shared_secret="...", max_commands_per_window=5, window_seconds=10)
    validator.start()   # subscribes and dispatches forever
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from .broker import BrokerConnection
from .schema import CommandPacket

logger = logging.getLogger("tukzie.command_validation")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MAX_COMMAND_AGE_SECONDS = 10  # reject commands older than this (replay protection)


@dataclass
class ValidationResult:
    accepted: bool
    reason: str = ""          # empty if accepted
    command: CommandPacket | None = None


class RateLimiter:
    """Sliding-window rate limiter, keyed by source (e.g. client_id or
    the 'target' field — whatever identifies who's sending commands)."""

    def __init__(self, max_commands: int, window_seconds: float):
        self.max_commands = max_commands
        self.window_seconds = window_seconds
        self._history: dict[str, deque] = defaultdict(deque)

    def allow(self, source: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        history = self._history[source]

        # drop anything outside the window
        while history and now - history[0] > self.window_seconds:
            history.popleft()

        if len(history) >= self.max_commands:
            return False

        history.append(now)
        return True


class CommandValidator:
    def __init__(
        self,
        conn: BrokerConnection,
        shared_secret: str,
        max_commands_per_window: int = 5,
        window_seconds: float = 10.0,
    ):
        self.conn = conn
        self.shared_secret = shared_secret
        self.rate_limiter = RateLimiter(max_commands_per_window, window_seconds)
        self.on_valid_command = None  # set this to a callback(CommandPacket) -> None

    # ---- validation ---------------------------------------------------

    def _check_auth(self, packet_raw: dict, token: str) -> bool:
        """HMAC over the command+target+timestamp using the shared secret.
        Swap for per-device keys before this leaves proof-of-concept stage."""
        payload = f"{packet_raw['cmd']}:{packet_raw['target']}:{packet_raw['timestamp']}"
        expected = hmac.new(self.shared_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, token)

    def validate(self, raw_payload: bytes, source: str = "unknown") -> ValidationResult:
        # 1. schema check
        try:
            raw = json.loads(raw_payload)
            packet = CommandPacket.from_dict(raw)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            return ValidationResult(False, f"malformed payload: {e}")

        # 2. auth check
        if not self._check_auth(raw, packet.auth_token):
            return ValidationResult(False, "auth token invalid")

        # 3. replay check
        age = time.time() - packet.timestamp
        if age > MAX_COMMAND_AGE_SECONDS:
            return ValidationResult(False, f"stale command, age={age:.1f}s")
        if age < -2:  # small tolerance for clock skew
            return ValidationResult(False, f"timestamp in the future, age={age:.1f}s")

        # 4. rate limit check — deliberately last, so a flood of otherwise
        #    valid commands is what actually exercises this path
        if not self.rate_limiter.allow(source):
            return ValidationResult(False, f"rate limit exceeded for source '{source}'")

        return ValidationResult(True, command=packet)

    # ---- MQTT wiring ----------------------------------------------------

    def _on_message(self, client, userdata, msg):
        source = msg.topic  # swap for a client-id/target field if you have one
        result = self.validate(msg.payload, source=source)

        if result.accepted:
            logger.info("ACCEPTED command=%s target=%s", result.command.command, result.command.target)
            if self.on_valid_command:
                self.on_valid_command(result.command)
        else:
            logger.warning("REJECTED source=%s reason=%s", source, result.reason)

    def start(self) -> None:
        topic = self.conn.topic("commands")
        self.conn.client.on_message = self._on_message
        self.conn.client.subscribe(topic)
        logger.info("Subscribed to %s, listening for commands...", topic)
        self.conn.client.loop_forever()
