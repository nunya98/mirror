"""Event types and the CALM / SCARE / COOLDOWN state machine."""
from __future__ import annotations

import logging
import random
import time
from collections import deque
from enum import Enum
from pathlib import Path

VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".m4v", ".webm"}


class State(str, Enum):
    CALM = "CALM"          # ambient playlist looping; PIR live
    SCARE = "SCARE"        # one scare clip playing; all triggers muted
    COOLDOWN = "COOLDOWN"  # back to ambient; PIR muted until the timer expires


class Event:
    """A single input for the engine.  Everything funnels through one queue."""

    __slots__ = ("kind", "payload")

    def __init__(self, kind: str, payload: object | None = None):
        self.kind = kind        # "pir" | "action" | "file_end" | "mpv_up" | "mpv_down"
        self.payload = payload

    def __repr__(self) -> str:
        return f"Event({self.kind!r}, {self.payload!r})"


def scan_media(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        (p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES),
        key=lambda p: p.name.lower(),
    )


class MirrorEngine:
    """Owns the state machine and drives mpv.  Single-threaded by design:
    every call to :meth:`handle` and :meth:`tick` happens on the main loop."""

    def __init__(self, cfg, mpv, log: logging.Logger | None = None):
        self.cfg = cfg
        self.mpv = mpv
        self.log = log or logging.getLogger("mirror.engine")

        self.state = State.CALM
        self.calm_files: list[Path] = []
        self.scare_files: list[Path] = []

        self._calm_bag: deque[Path] = deque()
        self._last_calm: Path | None = None
        self._last_scare: Path | None = None
        self._cooldown_until = 0.0
        self._end_guard_until = 0.0

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def load_media(self) -> None:
        self.calm_files = scan_media(self.cfg.calm_dir)
        self.scare_files = scan_media(self.cfg.scare_dir)

        if not self.calm_files:
            raise RuntimeError(
                f"no ambient clips in {self.cfg.calm_dir} — put black.mp4 there"
            )
        self.log.info("ambient clips: %s", ", ".join(p.name for p in self.calm_files))
        if self.scare_files:
            self.log.info("scare clips: %s", ", ".join(p.name for p in self.scare_files))
        else:
            self.log.warning(
                "no scare clips in %s — PIR and scare buttons will do nothing",
                self.cfg.scare_dir,
            )

    def start(self) -> None:
        self.load_media()
        self.state = State.CALM
        self._play_calm()

    # ------------------------------------------------------------------ #
    # event handling
    # ------------------------------------------------------------------ #
    def handle(self, event: Event) -> None:
        if event.kind == "pir":
            self._on_pir()
        elif event.kind == "action":
            self._on_action(str(event.payload))
        elif event.kind == "file_end":
            self._on_file_end(str(event.payload))
        elif event.kind == "mpv_up":
            self.log.warning("mpv (re)connected; restoring ambient playback")
            self.state = State.CALM
            self._play_calm()
        elif event.kind == "mpv_down":
            self.log.warning("mpv went away; holding state %s", self.state.value)
        else:
            self.log.debug("ignoring unknown event %r", event)

    def tick(self) -> None:
        if self.state is State.COOLDOWN and time.monotonic() >= self._cooldown_until:
            self.state = State.CALM
            self.log.info("cooldown over -> CALM")

    # ------------------------------------------------------------------ #
    # transitions
    # ------------------------------------------------------------------ #
    def _on_pir(self) -> None:
        if not self.scare_files:
            self.log.debug("motion ignored: no scare clips")
            return
        if self.state is not State.CALM:
            self.log.debug("motion ignored while %s", self.state.value)
            return
        self._fire_scare(self._random_scare(), source="pir")

    def _on_action(self, action: str) -> None:
        action = action.strip()
        scare_cfg = self.cfg["scare"]

        if action == "calm":
            if self.state is State.CALM:
                self.log.debug("already calm")
                return
            if self.state is State.SCARE and not scare_cfg["allow_abort"]:
                self.log.info("abort ignored (allow_abort = false)")
                return
            self.log.info("abort requested -> cooldown")
            self._enter_cooldown()
            return

        if action.startswith("scare:"):
            token = action.split(":", 1)[1].strip()
            path = self._resolve_scare(token)
            if path is None:
                self.log.warning("unknown scare %r (have %d)", token, len(self.scare_files))
                return
            if self.state is State.SCARE and not scare_cfg["interrupt_current"]:
                self.log.info("scare %s ignored while a scare is playing", token)
                return
            self._fire_scare(path, source="button")
            return

        if action == "toggle_pause":
            self.mpv.command("cycle", "pause")
            return

        self.log.warning("unknown action %r", action)

    def _on_file_end(self, reason: str) -> None:
        now = time.monotonic()
        if now < self._end_guard_until:
            self.log.debug("duplicate end signal (%s) ignored", reason)
            return
        self._end_guard_until = now + 1.0

        if self.state is State.SCARE:
            self.log.info("scare finished (%s)", reason)
            self._enter_cooldown()
        else:
            self.log.debug("ambient clip finished (%s); advancing", reason)
            self._play_calm()

    # ------------------------------------------------------------------ #
    # playback primitives
    # ------------------------------------------------------------------ #
    def _play_calm(self) -> None:
        if not self.calm_files:
            self.log.error("no ambient clips available")
            return
        path = self._next_calm()
        single = len(self.calm_files) == 1
        # A single ambient clip loops inside mpv: no EOF, no decoder churn, no
        # gap, and the DRM plane is never released.  Multiple clips are
        # sequenced by the engine on each EOF instead.
        self.mpv.set_property("loop-file", "inf" if single else "no")
        self.mpv.set_property("volume", self.cfg["mpv"]["volume_calm"])
        self.mpv.loadfile(path, "replace")
        self.log.info("CALM %s%s", path.name, " (looping)" if single else "")

    def _fire_scare(self, path: Path, source: str) -> None:
        self.log.info("SCARE %s (via %s)", path.name, source)
        self.state = State.SCARE
        self._last_scare = path
        self.mpv.set_property("loop-file", "no")
        self.mpv.set_property("volume", self.cfg["mpv"]["volume_scare"])
        self.mpv.loadfile(path, "replace")

    def _enter_cooldown(self) -> None:
        seconds = float(self.cfg["pir"]["cooldown_seconds"])
        self.state = State.COOLDOWN
        self._cooldown_until = time.monotonic() + seconds
        self._play_calm()
        self.log.info("COOLDOWN for %.0fs (PIR muted)", seconds)

    # ------------------------------------------------------------------ #
    # selection
    # ------------------------------------------------------------------ #
    def _next_calm(self) -> Path:
        if not self._calm_bag:
            bag = list(self.calm_files)
            if self.cfg["scare"]["shuffle_calm"]:
                random.shuffle(bag)
            # Avoid the same clip twice across a bag boundary.
            if len(bag) > 1 and self._last_calm is not None and bag[0] == self._last_calm:
                bag.append(bag.pop(0))
            self._calm_bag = deque(bag)
        path = self._calm_bag.popleft()
        self._last_calm = path
        return path

    def _random_scare(self) -> Path:
        pool = list(self.scare_files)
        if (
            self.cfg["scare"]["avoid_repeat"]
            and len(pool) > 1
            and self._last_scare in pool
        ):
            pool = [p for p in pool if p != self._last_scare]
        return random.choice(pool)

    def _resolve_scare(self, token: str) -> Path | None:
        if not self.scare_files:
            return None
        if token.isdigit():
            index = int(token)
            if 1 <= index <= len(self.scare_files):
                return self.scare_files[index - 1]
            return None
        lowered = token.lower()
        for path in self.scare_files:
            if path.stem.lower() == lowered:
                return path
        return None

