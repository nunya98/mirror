#!/usr/bin/env python3
"""Standalone PIR bench test — no mpv, no display, no state machine.

    sudo tools/pir-test.py --gpio 17 --warmup 20
"""
import argparse
import logging
import queue
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mirror.config import load                      # noqa: E402
from mirror.pir import PirWatcher                   # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config")
    parser.add_argument("--gpio", type=int)
    parser.add_argument("--warmup", type=float)
    parser.add_argument("--active-low", action="store_true")
    parser.add_argument("--log-level", default="debug")
    args = parser.parse_args()

    overrides = ["general.root=" + str(ROOT)]
    if args.gpio is not None:
        overrides.append(f"pir.gpio={args.gpio}")
    if args.warmup is not None:
        overrides.append(f"pir.warmup_seconds={args.warmup}")
    if args.active_low:
        overrides.append("pir.active_high=false")

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    cfg = load(args.config, overrides=overrides)

    events: queue.Queue = queue.Queue()
    watcher = PirWatcher(cfg, events)
    watcher.start()

    print("watching for motion — Ctrl-C to stop", flush=True)
    try:
        count = 0
        while True:
            events.get()
            count += 1
            print(f"{time.strftime('%H:%M:%S')}  MOTION  (#{count})", flush=True)
    except KeyboardInterrupt:
        watcher.stop()
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

