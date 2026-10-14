# Halloween Smart Mirror

Motion-activated "smart mirror" prop for a bar: an HC-SR501 PIR (or a keypad
button) triggers a video fullscreen on a display behind a two-way mirror.
Headless, unattended, runs as a systemd service.

```
CALM (ambient clip loops) --PIR/button--> SCARE (one clip) --EOF--> COOLDOWN --timer--> CALM
```

## Hardware

- Raspberry Pi Zero 2 W, 32-bit Raspberry Pi OS Lite (Bookworm), headless
- HC-SR501 PIR sensor
- USB 2.4 GHz keypad with HID receiver dongle (e.g. LIANGSTAR RF22)
- Display over mini-HDMI
- Powered USB hub if using both a USB audio dongle and the keypad receiver

## Pinout

Only three header pins are used. The keypad and audio are USB/HDMI, so they
consume no GPIO.

| Pi pin | Function | Connects to |
|---|---|---|
| **2** | 5V | PIR VCC |
| **6** | GND | PIR GND |
| **11** | GPIO17 | PIR OUT |

The HC-SR501 OUT pin measures 3.3 V, so it wires directly to GPIO — no level
shifter.

If you prefer to solder one contiguous block instead of three scattered pins,
use **pins 1–12** (a 2×6 strip). That block contains pins 2, 6, and 11, plus
spare 5V/GND/GPIO pins. The PIR GPIO is configurable (`pir.gpio`), so if you
solder only pins 1–10 you can move the PIR to GPIO4 (pin 7) instead.

## Layout

```
/opt/mirror/
├── mirror.toml                 # configuration (env overrides it)
├── mirror.env                  # MIRROR_TOKEN — chmod 600, never committed
├── bin/mirror-supervisor       # entry point
├── src/mirror/                 # config, mpv, state, pir, input, web, supervisor
├── media/calm/                 # ambient clip(s), e.g. black.mp4
├── media/scares/               # scare clips, selected as scare:1, scare:2, ...
├── systemd/mirror.service
└── tools/                      # encode.sh, pir-test.py
```

## Install

```bash
sudo apt install mpv python3-evdev python3-lgpio
sudo mkdir -p /opt/mirror && sudo chown -R root:root /opt/mirror
# copy the tree in, then:
sudo chmod +x /opt/mirror/bin/mirror-supervisor /opt/mirror/tools/*.sh /opt/mirror/tools/*.py

# media
sudo /opt/mirror/tools/encode.sh black /opt/mirror/media/calm/black.mp4 60
sudo /opt/mirror/tools/encode.sh full  ~/scare-raw.mp4 /opt/mirror/media/scares/scare-01.mp4

# secret
sudo install -m 600 /dev/null /opt/mirror/mirror.env
sudo sh -c 'printf "MIRROR_TOKEN=%s\n" "$(head -c 24 /dev/urandom | base64)" > /opt/mirror/mirror.env'

# service
sudo cp /opt/mirror/systemd/mirror.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now mirror
journalctl -fu mirror
```

## Video format

All clips — ambient **and** scare — must be normalized to the
hardware-verified profile, or hwdec breaks:

**1080p30, H.264 High @ L4.1, yuv420p, limited range (`-color_range tv`),
refs=1, no B-frames, plus a stereo AAC track.** H.264 only (the Zero 2 W has
no H.265 decoder).

Use `tools/encode.sh` rather than encoding by hand. Full-range source
(e.g. phone footage) needs `encode.sh full`, which applies
`scale=in_range=full:out_range=limited`.

## Configuration

Settings live in `mirror.toml`. Precedence, lowest to highest:

```
built-in defaults  <  mirror.toml  <  MIRROR_* env vars  <  --set flags
```

Env key for `[section] key` is `MIRROR_<SECTION>_<KEY>` — e.g.
`[pir] cooldown_seconds` → `MIRROR_PIR_COOLDOWN_SECONDS`. The keypad mapping
lives in `[input.keymap]`; every key is remappable there, including both
NumLock-on (`KEY_KP*`) and NumLock-off (navigation) codes so an accidental
NumLock press can't break the prop.

`MIRROR_TOKEN` is the only secret and is env-only — never put it in
`mirror.toml`.

## Bench test order

```bash
# 1. Config sanity: no hardware, no mpv
/opt/mirror/bin/mirror-supervisor --dry-run --log-level debug

# 2. PIR alone
sudo /opt/mirror/tools/pir-test.py --gpio 17 --warmup 20

# 3. Full stack, no display
sudo /opt/mirror/bin/mirror-supervisor --dry-run --log-level debug

# 4. Keypad codes before trusting the keymap
sudo evtest
```

## Known constraints

- PIR needs ~60 s to stabilize after power-on; the watcher ignores it
  (`pir.warmup_seconds`).
- PIR is edge-triggered with a cooldown (`pir.cooldown_seconds`), because
  HC-SR501 H-mode holds OUT high for as long as motion continues.
- Never drop the HDMI output — mpv always plays something (the black ambient
  clip), so the link never drops and the panel never sleeps. Also disable
  display sleep in the monitor's own OSD.
- Black on an LCD is not truly black behind a two-way mirror; expect faint
  backlight bleed.
