"""
Adaptive communication manager (Task 12/13, Days 4-5).

Three USB dongles are the only data-transmission candidates (per project
decision — no Wi-Fi fallback in the scoring itself). One generic
poll_dongle() function is called three times rather than three bespoke
implementations, so there's one code path to trust.

Scoring uses Simple Additive Weighting (SAW) — chosen because it's easy
to implement and unit-test with a small dataset, not because it's
claimed optimal. Swap for TOPSIS/AHP later once you have enough logged
data to justify weights properly; say this explicitly in your report.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger("tukzie.adaptive_scoring")

DONGLE_IDS = ["dongle_1", "dongle_2", "dongle_3"]


@dataclass
class LinkMetrics:
    interface: str
    signal_strength: float   # dBm or CSQ-derived, higher = better
    latency_ms: float        # lower = better
    packet_loss_pct: float   # lower = better
    timestamp: float = 0.0


# ---- polling --------------------------------------------------------

def _default_signal_reader(interface: str) -> float:
    """Real implementation: shell out to mmcli/ModemManager for this
    interface's signal quality. Replace with the actual mmcli invocation
    for your dongles' modem IDs."""
    raise NotImplementedError(
        f"Wire this up to `mmcli -m <modem-id-for-{interface}> --signal-get` "
        f"(or equivalent) once you know the modem IDs."
    )


def _default_latency_reader(interface: str, host: str = "8.8.8.8", count: int = 5) -> tuple[float, float]:
    """Ping over a specific interface, return (avg_latency_ms, loss_pct).
    Real implementation — adjust the ping invocation for your OS/interface
    binding method (e.g. `ping -I <iface>` on Linux)."""
    result = subprocess.run(
        ["ping", "-I", interface, "-c", str(count), host],
        capture_output=True, text=True, timeout=count * 2 + 5,
    )
    # Parsing left as TODO — exact ping output format varies by OS.
    # For now this just proves the plumbing; fill in real parsing once
    # you're on the Pi with real dongles attached.
    raise NotImplementedError("Parse `result.stdout` for avg latency + loss % on your target OS.")


def poll_dongle(
    interface: str,
    signal_reader: Callable[[str], float] = _default_signal_reader,
    latency_reader: Callable[[str], tuple[float, float]] = _default_latency_reader,
) -> LinkMetrics:
    """One function, called once per dongle. Inject fake readers in tests."""
    signal = signal_reader(interface)
    latency, loss = latency_reader(interface)
    return LinkMetrics(
        interface=interface,
        signal_strength=signal,
        latency_ms=latency,
        packet_loss_pct=loss,
        timestamp=time.time(),
    )


def poll_all_dongles(
    signal_reader: Callable[[str], float] = _default_signal_reader,
    latency_reader: Callable[[str], tuple[float, float]] = _default_latency_reader,
) -> list[LinkMetrics]:
    return [poll_dongle(d, signal_reader, latency_reader) for d in DONGLE_IDS]


# ---- scoring: Simple Additive Weighting (SAW) ----------------------

DEFAULT_WEIGHTS = {
    "signal_strength": 0.4,   # benefit criterion
    "latency_ms": 0.35,       # cost criterion
    "packet_loss_pct": 0.25,  # cost criterion
}


def _normalize(values: list[float], is_benefit: bool) -> list[float]:
    """Min-max normalize to [0, 1]. Benefit criteria: higher raw = higher
    normalized. Cost criteria: lower raw = higher normalized."""
    lo, hi = min(values), max(values)
    if hi == lo:
        return [1.0] * len(values)  # all equal -> no discriminating info
    if is_benefit:
        return [(v - lo) / (hi - lo) for v in values]
    return [(hi - v) / (hi - lo) for v in values]


def score_dongles(samples: list[LinkMetrics], weights: dict = DEFAULT_WEIGHTS) -> dict[str, float]:
    """SAW score per dongle for one polling round (one sample per dongle)."""
    if len(samples) < 2:
        return {s.interface: 1.0 for s in samples}

    norm_signal = _normalize([s.signal_strength for s in samples], is_benefit=True)
    norm_latency = _normalize([s.latency_ms for s in samples], is_benefit=False)
    norm_loss = _normalize([s.packet_loss_pct for s in samples], is_benefit=False)

    scores = {}
    for s, ns, nl, nlo in zip(samples, norm_signal, norm_latency, norm_loss):
        scores[s.interface] = (
            weights["signal_strength"] * ns
            + weights["latency_ms"] * nl
            + weights["packet_loss_pct"] * nlo
        )
    return scores


# ---- hysteresis -------------------------------------------------------

class HysteresisSelector:
    """Only switch the active link if a candidate beats the current one
    by `margin` for `required_consecutive` rounds in a row — avoids
    flapping between two similarly-scored dongles."""

    def __init__(self, required_consecutive: int = 3, margin: float = 0.05):
        self.required_consecutive = required_consecutive
        self.margin = margin
        self.active: str | None = None
        self._streak: dict[str, int] = {}

    def update(self, scores: dict[str, float]) -> str:
        if self.active is None:
            self.active = max(scores, key=scores.get)
            return self.active

        current_score = scores.get(self.active, 0.0)
        for candidate, candidate_score in scores.items():
            if candidate == self.active:
                continue
            if candidate_score - current_score > self.margin:
                self._streak[candidate] = self._streak.get(candidate, 0) + 1
                if self._streak[candidate] >= self.required_consecutive:
                    logger.info("switching active link %s -> %s", self.active, candidate)
                    self.active = candidate
                    self._streak = {}
                    return self.active
            else:
                self._streak[candidate] = 0

        return self.active
