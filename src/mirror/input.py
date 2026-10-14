"""USB-dongle keypad watcher (HID keyboard)."""
from __future__ import annotations

import logging
import os
import select
import threading
import time
from pathlib import Path

from .state import Event

try:
    import evdev
    from evdev import ecodes
except Exception:  # noqa: BLE001 - optional dependency
    evdev = None
    ecodes = None


def code_for(name: str) -> int | None:
    """``KEY_KP1``, ``KP1`` or ``82`` -> an evdev key code."""
    if ecodes is None:
        return None
    name = str(name).strip()
    if name.isdigit():
        return int(name)
    if not name.startswith(("KEY_", "BTN_")):
        name = f"KEY_{name}"
    return getattr(ecodes, name, None)


def name_for(code: int) -> str:
    if ecodes is None:
        return str(code)
    return ecodes.KEY.get(code, str(code))


class InputWatcher(threading.Thread):
    def __init__(self, cfg, events, log: logging.Logger | None = None):
        super().__init__(name="input", daemon=True)
        self.cfg = cfg
        self.events = events
        self.log = log or logging.getLogger("mirror.input")
        self._stop = threading.Event()
        self._keymap: dict[int, str] = {}

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ #
    def _build_keymap(self) -> dict[int, str]:
        keymap: dict[int, str] = {}
        for name, action in self.cfg["input"]["keymap"].items():
            code = code_for(name)
            if code is None:
                self.log.warning("unknown key %r in keymap; skipped", name)
                continue
            keymap[code] = str(action)
        return keymap

    def _candidates(self) -> list[str]:
        configured = str(self.cfg["input"]["device"]).strip()
        if configured:
            return [configured]

        out: list[str] = []
        by_id = Path("/dev/input/by-id")
        if by_id.is_dir():
            out += [str(p) for p in sorted(by_id.iterdir()) if "event" in p.name]
        if evdev is not None:
            out += sorted(evdev.list_devices())
        return out

    def _pick(self):
        """First device whose key capabilities overlap our keymap."""
        wanted = set(self._keymap)
        seen: set[str] = set()
        for candidate in self._candidates():
            try:
                real = os.path.realpath(candidate)
            except OSError:
                continue
            if real in seen:
                continue
            seen.add(real)
            try:
                device = evdev.InputDevice(candidate)
            except Exception:  # noqa: BLE001 - permissions, vanished node, ...
                continue
            keys = set(device.capabilities().get(ecodes.EV_KEY, []))
            if wanted & keys:
                return device
            device.close()
        return None

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        cfg = self.cfg["input"]
        if not cfg["enabled"]:
            self.log.info("keypad input disabled by configuration")
            return
        if evdev is None:
            self.log.error("python-evdev not installed; keypad input disabled")
            return

        self._keymap = self._build_keymap()
        if not self._keymap:
            self.log.error("keymap is empty; keypad input disabled")
            return

        device = None
        while not self._stop.is_set() and device is None:
            device = self._pick()
            if device is None:
                self.log.warning("no input device matching keymap; retrying in 3s")
                self._stop.wait(3.0)
        if device is None:
            return

        self.log.info("keypad: %r at %s", device.name, device.path)

        if cfg["grab"]:
            try:
                device.grab()
                self.log.info("grabbed %s — console will not see its keys", device.path)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("could not grab %s: %s", device.path, exc)

        if cfg["set_numlock"]:
            try:
                device.set_led(ecodes.LED_NUML, 1)
            except Exception as exc:  # noqa: BLE001
                self.log.debug("could not set NumLock LED: %s", exc)

        debounce = float(cfg["debounce_ms"]) / 1000.0
        last_press: dict[int, float] = {}

        try:
            while not self._stop.is_set():
                ready, _, _ = select.select([device.fd], [], [], 0.5)
                if not ready:
                    continue
                for event in device.read():
                    # value 1 == key down; 0 == release, 2 == auto-repeat
                    if event.type != ecodes.EV_KEY or event.value != 1:
                        continue
                    action = self._keymap.get(event.code)
                    if action is None:
                        continue
                    now = time.monotonic()
                    if now - last_press.get(event.code, 0.0) < debounce:
                        continue
                    last_press[event.code] = now
                    self.log.info("key %s -> %s", name_for(event.code), action)
                    self.events.put(Event("action", action))
        except OSError as exc:
            self.log.warning("keypad read loop ended: %s", exc)
        finally:
            try:
                device.close()
            except Exception:  # noqa: BLE001
                pass

