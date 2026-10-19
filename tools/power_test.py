#!/usr/bin/env python3
"""power-test.py - soak-test CPU + video-decode load for the Halloween mirror.

Reproduces the mirror's real playback path (single mpv, --hwdec=v4l2m2m,
calm clip looping, periodic scare clips at the configured cooldown) while a
light CPU load runs alongside, and logs `vcgencmd get_throttled` /
`measure_temp` throughout. Use it to judge whether a given brick + cable can
sustain the prop for weeks.

Config precedence matches the supervisor:
    built-in defaults < mirror.toml < MIRROR_* env vars < --set flags

Exit status is 0 only if no sticky under-voltage / throttling bit was ever set.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm"}
HERE = Path(__file__).resolve().parent

DEFAULTS = {
    "general": {"root": None, "log_level": "info", "service_name": "mirror"},
    "paths": {"calm_dir": "media/calm", "scare_dir": "media/scares"},
    "mpv": {
        "binary": "mpv",
        "socket": "/run/mirror/mpv.sock",
        "audio_enabled": True,
        "audio_device": "alsa/hdmi:CARD=vc4hdmi",
        "volume_calm": 0,
        "volume_scare": 80,
        "extra_args": [],
    },
    "pir": {"cooldown_seconds": 50},
}

THROTTLE_BITS = [
    (0x1, "under-voltage NOW"),
    (0x2, "arm frequency capped NOW"),
    (0x4, "throttled NOW"),
    (0x8, "soft temp limit NOW"),
    (0x10000, "under-voltage HAS OCCURRED"),
    (0x20000, "arm frequency capped HAS OCCURRED"),
    (0x40000, "throttling HAS OCCURRED"),
    (0x80000, "soft temp limit HAS OCCURRED"),
]
STICKY = 0xF0000


# ---------------------------------------------------------------- config ----
def deep_merge(base: dict, over: dict) -> dict:
    for key, val in over.items():
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], val)
        else:
            base[key] = val
    return base


def parse_scalar(raw: str):
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def env_overrides(env: dict) -> dict:
    out: dict = {}
    for key, val in env.items():
        if not key.startswith("MIRROR_") or key == "MIRROR_TOKEN":
            continue
        parts = key[len("MIRROR_"):].lower().split("_")
        if len(parts) < 2:
            continue
        section, name = parts[0], "_".join(parts[1:])
        out.setdefault(section, {})[name] = parse_scalar(val)
    return out


def set_overrides(items: list[str]) -> dict:
    out: dict = {}
    for item in items:
        if "=" not in item or "." not in item.split("=", 1)[0]:
            raise SystemExit(f"--set expects section.key=value, got {item!r}")
        path, _, raw = item.partition("=")
        section, _, name = path.partition(".")
        out.setdefault(section.strip(), {})[name.strip()] = parse_scalar(raw)
    return out


def load_config(args) -> dict:
    cfg = deep_merge(json.loads(json.dumps(DEFAULTS)), {})

    explicit = args.config or os.environ.get("MIRROR_CONFIG")
    if explicit:
        cfg_path = Path(explicit)
    else:
        cfg_path = (Path(cfg["general"]["root"]) if cfg["general"]["root"]
                    else HERE.parent) / "mirror.toml"

    if cfg_path.is_file():
        if tomllib is None:
            raise SystemExit("python3 >= 3.11 needed to read mirror.toml")
        with cfg_path.open("rb") as fh:
            deep_merge(cfg, tomllib.load(fh))
    elif explicit:
        raise SystemExit(f"config not found: {cfg_path}")

    deep_merge(cfg, env_overrides(dict(os.environ)))
    deep_merge(cfg, set_overrides(args.set))

    if not cfg["general"]["root"]:
        cfg["general"]["root"] = str(HERE.parent)
    cfg["_config_path"] = str(cfg_path)
    return cfg


# ------------------------------------------------------------------ media ----
def videos(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir()
                  if p.is_file() and p.suffix.lower() in VIDEO_EXT)


def probe_duration(path: Path) -> float:
    if not shutil.which("ffprobe"):
        return 0.0
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=20, check=True)
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def pick_heaviest(directory: Path) -> tuple[Path, float]:
    """Longest clip wins (highest duty cycle); size breaks ties.

    All clips are normalized to the same encode profile, so per-frame decode
    cost is effectively identical - duration is what moves average power.
    """
    best, best_dur = None, -1.0
    for clip in videos(directory):
        dur = probe_duration(clip)
        if (dur, clip.stat().st_size) > (best_dur, best.stat().st_size if best else 0):
            best, best_dur = clip, dur
    if best is None:
        raise SystemExit(f"no video files in {directory}")
    return best, best_dur


# -------------------------------------------------------------------- mpv ----
def mpv_argv(cfg: dict, sock_path: Path, display: bool) -> list[str]:
    mpv = cfg["mpv"]
    argv = [str(mpv["binary"]), "--no-config", "--idle=yes", "--keep-open=yes",
            f"--input-ipc-server={sock_path}"]
    if display:
        argv += ["--vo=gpu", "--gpu-context=drm", "--hwdec=v4l2m2m",
                 "--drm-draw-plane=overlay", "--drm-drmprime-video-plane=primary"]
    else:
        argv += ["--vo=null", "--hwdec=v4l2m2m"]
    argv += ["--osc=no", "--osd-bar=no", "--osd-level=0"]
    if mpv["audio_enabled"]:
        argv += ["--ao=alsa", f"--audio-device={mpv['audio_device']}",
                 "--audio-channels=stereo"]
    else:
        argv.append("--no-audio")
    argv += [str(a) for a in mpv["extra_args"]]
    return argv


def mpv_cmd(sock_path: Path, *args, timeout: float = 5.0) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(sock_path))
        s.sendall((json.dumps({"command": list(args)}) + "\n").encode())
        handle = s.makefile("rb")
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = handle.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and "error" in msg:
                return msg
    return {}


# --------------------------------------------------------------- telemetry ---
def vcgencmd(*args) -> str:
    if not shutil.which("vcgencmd"):
        return ""
    try:
        return subprocess.run(["vcgencmd", *args], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def throttled_value() -> int:
    raw = vcgencmd("get_throttled")
    if "=" not in raw:
        return 0
    try:
        return int(raw.split("=", 1)[1], 16)
    except ValueError:
        return 0


def describe(bits: int) -> str:
    active = [label for bit, label in THROTTLE_BITS if bits & bit]
    return ", ".join(active) if active else "clean"


# ------------------------------------------------------------------ systemd --
def systemctl(*args, timeout: float = 30.0):
    """Run systemctl, or return None if it isn't usable."""
    if not shutil.which("systemctl"):
        return None
    try:
        return subprocess.run(["systemctl", *args], capture_output=True,
                              text=True, timeout=timeout)
    except Exception:
        return None


def service_is_active(name: str):
    """True/False, or None when systemd is unavailable (e.g. in a container)."""
    res = systemctl("is-active", name)
    if res is None:
        return None
    return res.stdout.strip() == "active"


def stop_service(name: str) -> bool:
    res = systemctl("stop", name)
    return res is not None and res.returncode == 0


def start_service(name: str) -> bool:
    res = systemctl("start", name)
    return res is not None and res.returncode == 0


# --------------------------------------------------------------- cpu load ----
class CpuLoad:
    """Duty-cycled busy loop - no external dependency, easy to keep 'light'."""

    def __init__(self, workers: int, load: float, period: float = 0.05):
        self.workers, self.load, self.period = workers, load, period
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._spin, daemon=True)
                         for _ in range(workers)]

    def _spin(self) -> None:
        busy = self.period * self.load
        while not self._stop.is_set():
            start = time.perf_counter()
            acc = 0.0
            while time.perf_counter() - start < busy and not self._stop.is_set():
                for i in range(1, 4000):
                    acc += i * i
            if acc == float("inf"):
                print("")
            time.sleep(max(0.0, self.period - busy))

    def start(self) -> None:
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2)


# ------------------------------------------------------------------- main ----
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Soak-test CPU + video-decode load for the Halloween mirror.")
    ap.add_argument("--config", help="path to mirror.toml (else $MIRROR_CONFIG, "
                                     "else <root>/mirror.toml)")
    ap.add_argument("--calm", help="ambient clip to loop (default: first in calm_dir)")
    ap.add_argument("--scare", help="scare clip to trigger (default: heaviest in "
                                    "scare_dir)")
    ap.add_argument("--hours", type=float, default=8.0,
                    help="run length in hours; 0 = until interrupted (default 8)")
    ap.add_argument("--duration", type=float, default=None,
                    help="run length in seconds; overrides --hours")
    ap.add_argument("--interval", type=float, default=None,
                    help="seconds between scare triggers (default: "
                         "pir.cooldown_seconds, floored at that value)")
    ap.add_argument("--cpu-workers", type=int, default=1)
    ap.add_argument("--cpu-load", type=float, default=0.25,
                    help="duty cycle per worker, 0.0-1.0 (default 0.25)")
    ap.add_argument("--no-cpu", action="store_true", help="decode load only")
    ap.add_argument("--no-display", action="store_true",
                    help="use --vo=null for headless runs (approximate load)")
    ap.add_argument("--no-service-check", action="store_true",
                    help="do not inspect or touch the mirror service")
    ap.add_argument("--no-stop-service", action="store_true",
                    help="abort instead of stopping the service if it is running")
    ap.add_argument("--keep-service", action="store_true",
                    help="leave the service stopped when the test finishes")
    ap.add_argument("--log", default=str(Path.home() / "power-test.log"))
    ap.add_argument("--log-interval", type=float, default=30.0)
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve config and clips, print, then exit")
    ap.add_argument("--set", action="append", default=[], metavar="SEC.KEY=VAL",
                    help="override a config value (highest precedence)")
    args = ap.parse_args()

    cfg = load_config(args)
    root = Path(cfg["general"]["root"])
    calm_dir, scare_dir = root / cfg["paths"]["calm_dir"], root / cfg["paths"]["scare_dir"]

    calm = Path(args.calm) if args.calm else (videos(calm_dir) or [None])[0]
    if calm is None:
        raise SystemExit(f"no ambient clip found in {calm_dir}")

    if args.scare:
        scare, scare_dur = Path(args.scare), probe_duration(Path(args.scare))
    else:
        scare, scare_dur = pick_heaviest(scare_dir)

    cooldown = float(cfg["pir"]["cooldown_seconds"])
    interval = max(cooldown, args.interval) if args.interval is not None else cooldown
    run_seconds = args.duration if args.duration is not None else args.hours * 3600.0

    sock = Path(cfg["mpv"]["socket"])
    sock_dir = sock.parent if sock.parent.is_dir() and os.access(sock.parent, os.W_OK) \
        else Path("/tmp")
    test_sock = sock_dir / "mirror-power-test.sock"
    test_sock.unlink(missing_ok=True)

    argv = mpv_argv(cfg, test_sock, display=not args.no_display)

    service = str(cfg["general"].get("service_name", "mirror"))
    need_display = not args.no_display
    svc_active = None if args.no_service_check else service_is_active(service)

    if args.dry_run:
        print(f"config        : {cfg['_config_path']}")
        print(f"root          : {root}")
        print(f"calm clip     : {calm}")
        print(f"scare clip    : {scare}  ({scare_dur:.1f}s)")
        print(f"cooldown      : {cooldown:.0f}s   trigger interval: {interval:.0f}s")
        print(f"run length    : {run_seconds / 3600:.2f}h")
        print(f"cpu load      : {'off' if args.no_cpu else f'{args.cpu_workers} x {args.cpu_load:.0%}'}")
        if args.no_service_check:
            svc = "not checked (--no-service-check)"
        elif svc_active is None:
            svc = "unknown (no systemd)"
        else:
            svc = "active -> will be stopped" if svc_active else "inactive"
        print(f"service       : {service} ({svc})")
        print(f"log           : {args.log}")
        print("mpv argv      : " + " ".join(argv))
        return 0

    log = Path(args.log).expanduser()
    log.parent.mkdir(parents=True, exist_ok=True)
    log_fh = log.open("a", buffering=1)
    state = {"phase": "startup"}
    stop = threading.Event()

    def emit(msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
        print(line, flush=True)
        log_fh.write(line + "\n")

    def on_signal(_sig, _frm):
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    start_bits = throttled_value()
    emit(f"start config={cfg['_config_path']} calm={calm.name} "
         f"scare={scare.name} throttled=0x{start_bits:x} ({describe(start_bits)})")

    proc = None
    cpu = None
    stopped_service = False
    worst = start_bits

    try:
        if svc_active:
            if args.no_stop_service:
                detail = ("it holds the DRM plane the test needs"
                          if need_display else
                          "it will skew the power measurement")
                raise SystemExit(
                    f"error: the {service} service is running and "
                    f"--no-stop-service was given.\n  {detail}.\n"
                    f"Stop it first:\n    sudo systemctl stop {service}")
            emit(f"stopping {service} service (it holds the display)")
            if not stop_service(service):
                raise SystemExit(
                    f"error: could not stop the {service} service.\n"
                    f"Run this script as root, or stop it yourself:\n"
                    f"    sudo systemctl stop {service}")
            stopped_service = True
            for _ in range(50):
                if not service_is_active(service):
                    break
                time.sleep(0.1)
            else:
                raise SystemExit(f"error: {service} is still active; aborting.")
            time.sleep(0.5)

        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL)
        for _ in range(100):
            if test_sock.exists() or proc.poll() is not None:
                break
            time.sleep(0.1)
        if proc.poll() is not None:
            emit(f"FATAL mpv exited with {proc.returncode}; argv={' '.join(argv)}")
            return 2
        time.sleep(0.5)

        if not args.no_cpu:
            cpu = CpuLoad(args.cpu_workers, max(0.0, min(1.0, args.cpu_load)))
            cpu.start()

        mpv_cmd(test_sock, "set_property", "volume", cfg["mpv"]["volume_calm"])
        mpv_cmd(test_sock, "set_property", "loop-file", "inf")
        mpv_cmd(test_sock, "loadfile", str(calm.resolve()), "replace")

        def sampler() -> None:
            while not stop.is_set():
                bits = throttled_value()
                emit(f"state={state['phase']} throttled=0x{bits:x} ({describe(bits)}) "
                     f"temp={vcgencmd('measure_temp')} "
                     f"arm={vcgencmd('measure_clock', 'arm')}")
                stop.wait(args.log_interval)

        threading.Thread(target=sampler, daemon=True).start()

        deadline = time.time() + run_seconds if run_seconds > 0 else None
        while not stop.is_set() and (deadline is None or time.time() < deadline):
            state["phase"] = "calm"
            wait = interval - scare_dur
            if wait > 0:
                stop.wait(wait)
            if stop.is_set():
                break
            state["phase"] = "scare"
            emit(f"trigger scare={scare.name} duration={scare_dur:.1f}s")
            mpv_cmd(test_sock, "set_property", "loop-file", "no")
            mpv_cmd(test_sock, "set_property", "volume", cfg["mpv"]["volume_scare"])
            mpv_cmd(test_sock, "loadfile", str(scare.resolve()), "replace")
            stop.wait(scare_dur + 0.5)
            mpv_cmd(test_sock, "set_property", "volume", cfg["mpv"]["volume_calm"])
            mpv_cmd(test_sock, "set_property", "loop-file", "inf")
            mpv_cmd(test_sock, "loadfile", str(calm.resolve()), "replace")
            worst |= throttled_value()
    finally:
        if cpu:
            cpu.stop()
        if proc is not None:
            mpv_cmd(test_sock, "quit")
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        test_sock.unlink(missing_ok=True)
        if stopped_service and not args.keep_service:
            emit(f"restarting {service} service")
            if not start_service(service):
                emit(f"WARNING could not restart {service}; start it manually: "
                     f"sudo systemctl start {service}")

    final = throttled_value()
    worst |= final
    verdict = "PASS" if not (worst & STICKY) else "FAIL"
    emit(f"stop verdict={verdict} final=0x{final:x} ({describe(final)}) "
         f"worst=0x{worst:x} ({describe(worst)})")
    log_fh.close()
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

