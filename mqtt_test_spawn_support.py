"""Lightweight stdlib-only spawn targets for MQTT lifecycle tests."""

from __future__ import annotations

import time


def cpu_pressure_worker(stop, ready) -> None:
    """Run the existing bounded CPU-pressure loop after signalling readiness."""
    ready.set()
    value = 1
    while not stop.is_set():
        for _ in range(100_000):
            value = ((value * 1_103_515_245) + 12_345) & 0x7FFFFFFF
        time.sleep(0.001)


def exit_before_ready_worker(_stop, _ready) -> None:
    """Exit deterministically before readiness for parent cleanup tests."""
    raise SystemExit(23)


def wait_without_ready_worker(stop, _ready) -> None:
    """Remain alive without readiness until the parent requests cleanup."""
    while not stop.is_set():
        time.sleep(0.01)
