
"""Layered configuration.

Precedence, lowest to highest:

    built-in DEFAULTS  <  mirror.toml  <  MIRROR_* environment  <  --set flags

Env key for ``[section] key`` is ``MIRROR_<SECTION>_<KEY>``, uppercased, e.g.
``[pir] cooldown_seconds`` -> ``MIRROR_PIR_COOLDOWN_SECONDS``.  Lists are
comma-separated; mappings (``keymap``) are ``NAME=value,NAME=value``.
"""
from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any

ENV_PREFIX = "MIRROR"

# --------------------------------------------------------------------------- #
# Defaults.  The *type* of each value defines how its env override is coerced.
# --------------------------------------------------------------------------- #
DEFAULTS: dict[str, dict[str, Any]] = {
    "general": {
        "root": "/opt/mirror",
        "log_level": "info",
    },
    "paths": {
        "calm_dir": "media/calm",
        "scare_dir": "media/scares",
    },
    "mpv": {
        "binary": "mpv",
        "socket": "/run/mirror/mpv.sock",
        "audio_enabled": True,
        "audio_device": "alsa/hdmi:CARD=vc4hdmi",
        "volume_calm": 0,
        "volume_scare": 80,
        "extra_args": [],
    },
    "pir": {
        "enabled": True,
        "gpio": 17,
        "active_high": True,
        "warmup_seconds": 60,
        "cooldown_seconds": 50,
        "debounce_ms": 200,
        "poll_hz": 20,
    },
    "input": {
        "enabled": True,
        "device": "",
        "grab": True,
        "set_numlock": True,
        "debounce_ms": 250,
        "keymap": {
            "KEY_KP1": "scare:1",
            "KEY_KP2": "scare:2",
            "KEY_KP3": "scare:3",
            "KEY_END": "scare:1",
            "KEY_DOWN": "scare:2",
            "KEY_PGDN": "scare:3",
            "KEY_KP0": "calm",
            "KEY_INSERT": "calm",
        },
    },
    "scare": {
        "interrupt_current": False,
        "allow_abort": True,
        "avoid_repeat": True,
        "shuffle_calm": True,
    },
    "web": {
        "enabled": True,
        "bind": "0.0.0.0",
        "port": 8080,
    },
}

_TRUE = {"1", "true", "yes", "on", "y", "t"}


def _coerce(raw: str, like: Any) -> Any:
    """Coerce an env-var string into the type of the matching default."""
    raw = raw.strip()
    if isinstance(like, bool):
        return raw.lower() in _TRUE
    if isinstance(like, int):
        return int(raw, 0)
    if isinstance(like, float):
        return float(raw)
    if isinstance(like, list):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


def _parse_mapping(raw: str) -> dict[str, str]:
    """``KEY_KP1=scare:1,KEY_KP2=scare:2`` -> ``{'KEY_KP1': 'scare:1', ...}``"""
    out: dict[str, str] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"expected NAME=value, got {chunk!r}")
        name, value = chunk.split("=", 1)
        out[name.strip()] = value.strip()
    return out


class Config:
    """Read-only view over the merged configuration."""

    def __init__(self, data: dict[str, dict[str, Any]], sources: list[str]):
        self._data = data
        self.sources = sources

    # -- generic access ---------------------------------------------------- #
    def __getitem__(self, section: str) -> dict[str, Any]:
        return self._data[section]

    def section(self, name: str) -> dict[str, Any]:
        return self._data[name]

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self._data)

    # -- path helpers ------------------------------------------------------ #
    @property
    def root(self) -> Path:
        return Path(os.path.expanduser(self._data["general"]["root"]))

    def resolve(self, value: Any) -> Path:
        path = Path(os.path.expanduser(str(value)))
        return path if path.is_absolute() else (self.root / path)

    @property
    def calm_dir(self) -> Path:
        return self.resolve(self._data["paths"]["calm_dir"])

    @property
    def scare_dir(self) -> Path:
        return self.resolve(self._data["paths"]["scare_dir"])

    def describe(self) -> str:
        return " -> ".join(self.sources)


def _install_root() -> Path:
    """<root>/src/mirror/config.py -> <root>"""
    return Path(__file__).resolve().parents[2]


def load(
    config_path: str | None = None,
    overrides: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> Config:
    env = dict(os.environ) if env is None else dict(env)
    install_root = _install_root()
    data = copy.deepcopy(DEFAULTS)
    explicit: set[str] = set()
    sources = [f"defaults ({install_root})"]

    # -- 1. file ----------------------------------------------------------- #
    path = config_path or env.get(f"{ENV_PREFIX}_CONFIG") or str(install_root / "mirror.toml")
    if Path(path).is_file():
        with open(path, "rb") as handle:
            file_data = tomllib.load(handle)
        for section, values in file_data.items():
            data.setdefault(section, {})
            for key, value in values.items():
                data[section][key] = value
                explicit.add(f"{section}.{key}")
        sources.append(str(path))
    else:
        sources.append(f"(no config file at {path})")

    # -- 2. environment ---------------------------------------------------- #
    for section, values in DEFAULTS.items():
        for key, default in values.items():
            name = f"{ENV_PREFIX}_{section}_{key}".upper()
            if name not in env:
                continue
            if isinstance(default, dict):
                data[section][key] = _parse_mapping(env[name])
            else:
                data[section][key] = _coerce(env[name], default)
            explicit.add(f"{section}.{key}")
    sources.append("environment")

    # -- 3. --set flags ---------------------------------------------------- #
    for item in overrides or []:
        if "=" not in item:
            raise ValueError(f"--set expects SECTION.KEY=VALUE, got {item!r}")
        dotted, raw = item.split("=", 1)
        if "." not in dotted:
            raise ValueError(f"--set expects SECTION.KEY=VALUE, got {item!r}")
        section, key = dotted.split(".", 1)
        if section not in data:
            raise KeyError(f"--set: unknown section {section!r}")
        if key not in data[section]:
            raise KeyError(f"--set: unknown key {section}.{key!r}")
        current = data[section][key]
        if isinstance(current, dict):
            data[section][key] = _parse_mapping(raw)
        elif isinstance(current, bool):
            data[section][key] = _coerce(raw, current)
        elif isinstance(current, int) and not isinstance(current, bool):
            data[section][key] = _coerce(raw, current)
        elif isinstance(current, float):
            data[section][key] = _coerce(raw, current)
        elif isinstance(current, list):
            data[section][key] = _coerce(raw, current)
        else:
            data[section][key] = raw
        explicit.add(f"{section}.{key}")
    if overrides:
        sources.append("command line")

    # -- 4. root defaults to wherever the tree is installed ---------------- #
    if "general.root" not in explicit:
        data["general"]["root"] = str(install_root)

    return Config(data, sources)

