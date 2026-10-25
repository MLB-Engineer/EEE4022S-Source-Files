"""
Telemetry + Command schema definitions.

Keep this file broker-agnostic — it should work identically whichever profile is active.

TODO:
- Define a TelemetryPacket dataclass/schema matching the real sensor list:
  IMU x2, front/back radar, brake pedal, handbrake+torque lever, ambient
  light, throttle, rain sensor, BMS/SoC.
- Include a `schema_version` field so the dashboard can tolerate future
  sensor-list changes gracefully (see notes/repo_review.md).
- Define a CommandPacket schema for whatever tukzie/commands ends up
  carrying (cmd name, target, auth token/signature, timestamp).
"""

import time 
from dataclasses import dataclass, field, asdict
from typing import Optional

SCHEMA_VERSION = "0.1.0"


@dataclass
class TelemetryPacket:
    # Numeric sensors are real numbers (float), not strings, so InfluxDB
    # stores them as numeric fields and Analytics can chart them directly.
    # None = "no reading yet" (published as JSON null, ignored by Telegraf).
    # cImu / rImu stay JSON-encoded strings ('{"pitch": 1.2, "roll": 0.3}')
    # and lever stays text ("neutral" / "engaged").
    cImu: str = ""
    rImu: str = ""
    fRadar: Optional[float] = None
    bRadar: Optional[float] = None
    brake: Optional[float] = None
    light: Optional[float] = None
    lever: str = ""
    throttle: Optional[float] = None
    rain: Optional[float] = None
    bVoltage: Optional[float] = None
    bTemp: Optional[float] = None
    bCurrent: Optional[float] = None
    bSOC: Optional[float] = None
    bFault: Optional[int] = None
    schema_version: str = SCHEMA_VERSION
    timestamp: float = field(default_factory=time.time)
    checksum: Optional[str] = None
    sequence: Optional[int] = None
    #If you expect them to always exist but sometimes be empty, you’d use str = "" instead. 
    #If you expect them to sometimes be missing entirely, Optional[str] = None is the right choice.

    def to_dict(self) -> dict:
        """Added: buffering.py's docstring already assumed this existed
        (`telemetry_packet.to_dict()`), and sensor_simulator.py needs it
        to serialize before publishing, but it was never actually
        defined until now — CommandPacket had one, TelemetryPacket
        didn't."""
        return asdict(self)

@dataclass
class CommandPacket:
    """A single command sent over the commands topic...

    auth_token: shared-secret/HMAC token for now 
    timestamp: (unix seconds) used for replay protection 
    (see command_validation.py — commands older than MAX_COMMAND_AGE_SECONDS are rejected).
    """
    command: str #where do i define the diff types of commands?
    target: str
    auth_token: str
    timestamp: float = field(default_factory=time.time)
    schema_version: str = SCHEMA_VERSION

    """ If an entry is missing or malformed...
        The object was never created. The caller caught the error and can now decide: reject the packet, log it, or ask for correction.
    """
    @classmethod
    def from_dict(cls, raw: dict) -> "CommandPacket":
        """Raises KeyError/TypeError on malformed input — caller decides
        how to turn that into a rejection reason."""
        return cls(
            command=raw["cmd"],
            target=raw["target"],
            auth_token=raw["auth_token"],
            timestamp=float(raw["timestamp"]),
            schema_version=raw.get("schema_version", "unknown"),
        )

    def to_dict(self) -> dict:
        return asdict(self)

#validation later
    

