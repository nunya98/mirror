"""Entry point: wires config, mpv, the input threads and the state machine."""
from __future__ import annotations

import argparse
import logging
import queue
import signal
import threading
import time

from .config import load as load_config
from .input import InputWatcher
from .mpv import MpvController, NullMpvController
from .pir import PirWatcher
from .state import Event, MirrorEngine
from .web import WebTrigger

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mirror-supervisor",
        description="Halloween mirror prop: PIR + keypad -> mpv.",
    )
    parser.add_argument("-c", "--config", help="path to mirror.toml")
    parser.add_argument("--root", help="override [general] root")
    parser.add_argument("--log-level", help="override [general] log_level")
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="SECTION.KEY=VALUE",
        help="override any single setting (repeatable)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="do not start mpv; log every action instead",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="dump the merged configuration and exit",
    )
    return parser


class MpvWatchdog:
    """Restart or reconnect mpv, with a backoff so failures do not spin."""

    def __init__(self, mpv, events, log, backoff: float = 5.0):
        self.mpv = mpv
        self.events = events
        self.log = log
        self.backoff = backoff
        self._next_attempt = 0.0

    def check(self) -> None:
        if self.mpv.healthy():
            return
        now = time.monotonic()
        if now < self._next_attempt:
            return
        self._next_attempt = now + self.backoff

        if not self.mpv.alive():
            self.log.warning("mpv process is gone; restarting")
            self.events.put(Event("mpv_down"))
            try:
                self.mpv.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.mpv.start()
                self.mpv.connect(timeout=15.0)
                self.mpv.observe("eof-reached", 1)
                self.events.put(Event("mpv_up"))
            except Exception as exc:  # noqa: BLE001
                self.log.error("mpv restart failed: %s", exc)
            return

        self.log.warning("mpv IPC lost but the process lives; reconnecting")
        try:
            self.mpv.reconnect(timeout=5.0)
            self.mpv.observe("eof-reached", 1)
            self.events.put(Event("mpv_up"))
        except Exception as exc:  # noqa: BLE001
            self.log.error("mpv reconnect failed: %s", exc)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    overrides = list(args.overrides)
    if args.root:
        overrides.append(f"general.root={args.root}")
    if args.log_level:
        overrides.append(f"general.log_level={args.log_level}")

    cfg = load_config(args.config, overrides=overrides)

    if args.print_config:
        import json

        print(json.dumps(cfg.as_dict(), indent=2, sort_keys=True))
        return 0

    logging.basicConfig(
        level=str(cfg["general"]["log_level"]).upper(),
        format=LOG_FORMAT,
    )
    log = logging.getLogger("mirror")
    log.info("configuration: %s", cfg.describe())
    log.info("root: %s", cfg.root)

    events: queue.Queue[Event] = queue.Queue()

    if args.dry_run:
        mpv = NullMpvController(cfg, events, log)
    else:
        mpv = MpvController(cfg, events, log)

    engine = MirrorEngine(cfg, mpv, log)

    # -- bring up playback first so the engine can play calm immediately -- #
    if not args.dry_run:
        mpv.start()
        mpv.connect(timeout=20.0)
        mpv.observe("eof-reached", 1)
    else:
        mpv.start()

    try:
        engine.start()
    except RuntimeError as exc:
        log.error("%s", exc)
        mpv.stop()
        return 1

    # -- input threads ----------------------------------------------------- #
    threads = [
        PirWatcher(cfg, events, log),
        InputWatcher(cfg, events, log),
        WebTrigger(cfg, events, log),
    ]
    for thread in threads:
        thread.start()

    stop = threading.Event()

    def _request_stop(signum, _frame):
        log.info("signal %s received; shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    watchdog = MpvWatchdog(mpv, events, log)
    log.info("supervisor running")

    try:
        while not stop.is_set():
            try:
                event = events.get(timeout=0.25)
            except queue.Empty:
                event = None

            if event is not None:
                log.debug("event %r", event)
                try:
                    engine.handle(event)
                except Exception:  # noqa: BLE001
                    log.exception("engine failed handling %r", event)

            engine.tick()
            if not args.dry_run:
                watchdog.check()
    finally:
        log.info("stopping input threads")
        for thread in threads:
            try:
                thread.stop()
            except Exception:  # noqa: BLE001
                pass
        mpv.stop()
        log.info("bye")

    return 0

