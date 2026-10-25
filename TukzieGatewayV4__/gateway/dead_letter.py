"""
Dead-letter / quarantine handling for malformed telemetry (Task 15 finish,
Day 6).

Wraps telemetry ingestion: valid payloads pass through to the normal
handler, malformed ones get republished (or logged, depending on the
publish_rejected_fn you pass in) with a reason attached instead of
silently vanishing the way plain Telegraf discard currently does.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Callable

from .schema import TelemetryPacket

logger = logging.getLogger("tukzie.dead_letter")

PublishRejectedFn = Callable[[dict], None]


@dataclass
class IngestResult:
    accepted: bool
    packet: TelemetryPacket | None = None
    reason: str = ""


class DeadLetterHandler:
    def __init__(self, publish_rejected_fn: PublishRejectedFn):
        self.publish_rejected_fn = publish_rejected_fn

    def ingest(self, raw_payload: bytes) -> IngestResult:
        try:
            raw = json.loads(raw_payload)
        except json.JSONDecodeError as e:
            return self._reject(raw_payload, f"invalid JSON: {e}")

        try:
            # TODO: once TelemetryPacket has real fields (Day 1/2), this
            # will actually validate shape. For now it only checks
            # schema_version is present, since that's all the packet has.
            packet = TelemetryPacket(**raw) if isinstance(raw, dict) else None
            if packet is None:
                return self._reject(raw_payload, "payload is not a JSON object")
        except TypeError as e:
            return self._reject(raw_payload, f"schema mismatch: {e}")

        return IngestResult(accepted=True, packet=packet)

    def _reject(self, raw_payload: bytes, reason: str) -> IngestResult:
        try:
            raw_text = raw_payload.decode(errors="replace")
        except AttributeError:
            raw_text = str(raw_payload)

        rejected_envelope = {
            "raw_payload": raw_text,
            "reason": reason,
            "rejected_at": time.time(),
        }
        logger.warning("REJECTED telemetry: %s", reason)
        self.publish_rejected_fn(rejected_envelope)
        return IngestResult(accepted=False, reason=reason)
