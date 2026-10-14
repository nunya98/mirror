"""mpv process management and IPC client.

The supervisor owns the mpv process (rather than running it as its own systemd
unit) so that every mpv argument — audio device, extra args, hwdec — is derived
from configuration and can be overridden with an env var at run time.
"""
from __future__ import annotations

import json
import logging
import socket
import subprocess
import threading
import time
from pathlib import Path

from .state import Event

ENCODED_EVENT_REASONS = {"eof", "error"}


class MpvError(RuntimeError):
    pass


class _IpcClient:
    """Newline-delimited JSON over a unix socket, per mpv's --input-ipc-server."""

    def __init__(self, path: str, on_message, log: logging.Logger):
        self.path = path
        self.on_message = on_message
        self.log = log
        self._sock: socket.socket | None = None
        self._file = None
        self._send_lock = threading.Lock()
        self._request_id = 0
        self._reader: threading.Thread | None = None
        self.connected = threading.Event()
        self._closing = False

    def connect(self, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(self.path)
                self._sock = sock
                self._file = sock.makefile("rwb")
                break
            except OSError as exc:
                last = exc
                time.sleep(0.2)
        else:
            raise MpvError(f"could not connect to mpv socket {self.path}: {last}")

        self.connected.set()
        self._reader = threading.Thread(
            target=self._read_loop, name="mpv-ipc", daemon=True
        )
        self._reader.start()

    def _read_loop(self) -> None:
        try:
            for raw in self._file:
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self.log.debug("unparseable mpv line: %r", line[:200])
                    continue
                try:
                    self.on_message(message)
                except Exception:
                    self.log.exception("mpv message handler failed")
        except OSError:
            pass
        finally:
            self.connected.clear()
            if not self._closing:
                self.log.warning("mpv IPC connection closed")

    def command(self, *args) -> int:
        with self._send_lock:
            if self._file is None:
                raise MpvError("not connected")
            self._request_id += 1
            payload = {"command": list(args), "request_id": self._request_id}
            try:
                self._file.write((json.dumps(payload) + "\n").encode())
                self._file.flush()
            except OSError as exc:
                self.connected.clear()
                raise MpvError(f"send failed: {exc}") from exc
            return self._request_id

    def close(self) -> None:
        self._closing = True
        try:
            if self._file is not None:
                self._file.close()
        except OSError:
            pass
        try:
            if self._sock is not None:
                self._sock.close()
        except OSError:
            pass
        self.connected.clear()


class MpvController:
    """Owns the mpv process and exposes the small command surface the engine needs."""

    def __init__(self, cfg, events, log: logging.Logger | None = None):
        self.cfg = cfg
        self.events = events
        self.log = log or logging.getLogger("mirror.mpv")
        self.socket_path = str(cfg["mpv"]["socket"])
        self.proc: subprocess.Popen | None = None
        self._client: _IpcClient | None = None

    # -- argv -------------------------------------------------------------- #
    def build_argv(self) -> list[str]:
        mpv = self.cfg["mpv"]
        argv = [
            str(mpv["binary"]),
            "--no-config",
            "--idle=yes",
            # Hold the last frame at EOF instead of unloading the file: the DRM
            # plane is never released, so we avoid a re-init on the same atomic
            # commit path that previously produced "Error 22" blanking.
            "--keep-open=yes",
            f"--input-ipc-server={self.socket_path}",
            "--vo=gpu",
            "--gpu-context=drm",
            "--hwdec=v4l2m2m",
            "--drm-draw-plane=overlay",
            "--drm-drmprime-video-plane=primary",
            "--osc=no",
            "--osd-bar=no",
            "--osd-level=0",
        ]
        if mpv["audio_enabled"]:
            argv += [
                "--ao=alsa",
                f"--audio-device={mpv['audio_device']}",
                "--audio-channels=stereo",
            ]
        else:
            argv.append("--no-audio")
        argv += [str(a) for a in mpv["extra_args"]]
        return argv

    # -- process ----------------------------------------------------------- #
    def start(self) -> None:
        sock = Path(self.socket_path)
        sock.parent.mkdir(parents=True, exist_ok=True)
        try:
            if sock.exists():
                sock.unlink()
        except OSError as exc:
            self.log.warning("could not remove stale socket %s: %s", sock, exc)

        argv = self.build_argv()
        self.log.info("starting: %s", " ".join(argv))
        self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL)

    def stop(self, timeout: float = 5.0) -> None:
        if self._client is not None:
            try:
                self._client.command("quit")
            except Exception:
                pass
            self._client.close()
            self._client = None
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.log.warning("mpv did not exit; terminating")
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        self.proc = None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def healthy(self) -> bool:
        return self.alive() and self._client is not None and self._client.connected.is_set()

    # -- ipc --------------------------------------------------------------- #
    def connect(self, timeout: float = 15.0) -> None:
        self._client = _IpcClient(self.socket_path, self._on_message, self.log)
        self._client.connect(timeout)

    def reconnect(self, timeout: float = 5.0) -> None:
        if self._client is not None:
            self._client.close()
        self.connect(timeout)

    def _on_message(self, message: dict) -> None:
        name = message.get("event")

        if name == "property-change":
            if message.get("name") == "eof-reached" and message.get("data"):
                self.events.put(Event("file_end", "eof-reached"))
            return

        if name == "end-file":
            reason = str(message.get("reason", "unknown"))
            if reason in ENCODED_EVENT_REASONS:
                self.events.put(Event("file_end", reason))
            else:
                self.log.debug("ignoring end-file reason %r", reason)

    def observe(self, name: str, ident: int) -> None:
        self.command("observe_property", ident, name)

    # -- command surface used by the engine -------------------------------- #
    def command(self, *args) -> None:
        if self._client is None:
            raise MpvError("mpv IPC not connected")
        self._client.command(*args)

    def set_property(self, name: str, value) -> None:
        self.command("set_property", name, value)

    def loadfile(self, path, mode: str = "replace") -> None:
        self.command("loadfile", str(path), mode)


class NullMpvController:
    """--dry-run stand-in: logs instead of driving a display."""

    def __init__(self, cfg, events, log: logging.Logger | None = None):
        self.cfg = cfg
        self.events = events
        self.log = log or logging.getLogger("mirror.mpv")
        self.socket_path = str(cfg["mpv"]["socket"])

    def build_argv(self) -> list[str]:
        return ["<dry-run>"]

    def start(self) -> None:
        self.log.info("[dry-run] mpv not started")

    def stop(self, timeout: float = 5.0) -> None:
        pass

    def alive(self) -> bool:
        return True

    def healthy(self) -> bool:
        return True

    def connect(self, timeout: float = 15.0) -> None:
        pass

    def reconnect(self, timeout: float = 5.0) -> None:
        pass

    def observe(self, name: str, ident: int) -> None:
        pass

    def command(self, *args) -> None:
        self.log.info("[dry-run] mpv command: %s", " ".join(map(str, args)))

    def set_property(self, name: str, value) -> None:
        self.command("set_property", name, value)

    def loadfile(self, path, mode: str = "replace") -> None:
        self.command("loadfile", str(path), mode)

