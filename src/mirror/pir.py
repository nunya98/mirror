"""HC-SR501 motion watcher.

Edge-triggered on purpose: in H (repeatable) mode the module's OUT pin stays
high for as long as motion continues, so a level poll would re-fire on every
sample.  We only ever queue an event on a rising edge.
"""
from __future__ import annotations

import logging
import threading
import time

from .state import Event


class PirWatcher(threading.Thread):
    def __init__(self, cfg, events, log: logging.Logger | None = None):
        super().__init__(name="pir", daemon=True)
        self.cfg = cfg
        self.events = events
        self.log = log or logging.getLogger("mirror.pir")
        self.pin = int(cfg["pir"]["gpio"])
        self._stop = threading.Event()
        self._backend: str | None = None
        self._handle = None

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ #
    # GPIO backends
    # ------------------------------------------------------------------ #
    def _open(self) -> None:
        try:
            import lgpio  # type: ignore

            handle = lgpio.gpiochip_open(0)
            lgpio.gpio_claim_input(handle, self.pin)
            self._backend = "lgpio"
            self._handle = handle
            self.log.info("PIR on BCM%d via lgpio (gpiochip0)", self.pin)
            return
        except Exception as exc:  # noqa: BLE001 - fall through to the next backend
            self.log.debug("lgpio unavailable: %s", exc)

        try:
            import RPi.GPIO as GPIO  # type: ignore

            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.pin, GPIO.IN)
            self._backend = "rpi"
            self._handle = GPIO
            self.log.info("PIR on BCM%d via RPi.GPIO", self.pin)
            return
        except Exception as exc:  # noqa: BLE001
            self.log.debug("RPi.GPIO unavailable: %s", exc)

        raise RuntimeError(
            "no GPIO backend available — install python3-lgpio or python3-rpi.gpio"
        )

    def _level(self) -> int:
        if self._backend == "lgpio":
            import lgpio  # type: ignore

            return int(lgpio.gpio_read(self._handle, self.pin))
        return int(self._handle.input(self.pin))

    def _close(self) -> None:
        try:
            if self._backend == "lgpio":
                import lgpio  # type: ignore

                lgpio.gpio_free(self._handle, self.pin)
                lgpio.gpiochip_close(self._handle)
            elif self._backend == "rpi":
                self._handle.cleanup(self.pin)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        cfg = self.cfg["pir"]
        if not cfg["enabled"]:
            self.log.info("PIR disabled by configuration")
            return

        try:
            self._open()
        except Exception as exc:  # noqa: BLE001
            self.log.error("PIR unavailable: %s", exc)
            return

        interval = 1.0 / max(1.0, float(cfg["poll_hz"]))
        debounce = float(cfg["debounce_ms"]) / 1000.0
        active_level = 1 if cfg["active_high"] else 0
        warmup = float(cfg["warmup_seconds"])
        ready_at = time.monotonic() + warmup

        if warmup > 0:
            self.log.info("warm-up: ignoring motion for %.0fs", warmup)

        last_level: int | None = None
        last_fire = 0.0
        try:
            while not self._stop.is_set():
                level = self._level()
                now = time.monotonic()

                if level != last_level:
                    self.log.debug("level %s -> %s", last_level, level)
                    last_level = level
                    if level == active_level:
                        if now < ready_at:
                            self.log.debug("motion during warm-up; ignored")
                        elif now - last_fire < debounce:
                            self.log.debug("motion within debounce window; ignored")
                        else:
                            last_fire = now
                            self.log.info("PIR motion detected")
                            self.events.put(Event("pir"))

                self._stop.wait(interval)
        finally:
            self._close()

