#!/usr/bin/env bash
# Normalize video for the Pi Zero 2 W mirror prop.
#
#   encode.sh black   [out] [seconds]     generate the ambient black clip
#   encode.sh full    <in> <out>          input is full-range (phone footage)
#   encode.sh limited <in> <out>          input is already limited-range
#
# Output profile is the hardware-verified one: 1080p30, H.264 High @ 4.1,
# yuv420p, limited range, refs=1, no B-frames, plus a stereo AAC track so mpv
# never has to tear down and rebuild the ALSA device when switching clips.
set -euo pipefail

PROFILE=(
  -c:v libx264 -profile:v high -level 4.1 -pix_fmt yuv420p
  -color_range tv -crf 18 -g 30 -keyint_min 30 -sc_threshold 0
  -x264-params ref=1:bframes=0
  -c:a aac -b:a 128k -ar 48000 -ac 2
  -movflags +faststart
)

usage() {
  sed -n '2,9p' "$0"
  exit 1
}

case "${1:-}" in
  black)
    out="${2:-black.mp4}"
    dur="${3:-60}"
    ffmpeg -y \
      -f lavfi -i "color=c=black:s=1920x1080:r=30:d=${dur}" \
      -f lavfi -i "anullsrc=channel_layout=stereo:sample_rate=48000" \
      "${PROFILE[@]}" -shortest "$out"
    ;;

  full|limited)
    mode="$1"
    in="${2:?usage: encode.sh ${1} <in> <out>}"
    out="${3:?usage: encode.sh ${1} <in> <out>}"

    if [[ "$mode" == "full" ]]; then
      vf="scale=in_range=full:out_range=limited,fps=30"
    else
      vf="fps=30"
    fi

    # Guarantee an audio track even if the source has none.
    if ffprobe -v error -select_streams a -show_entries stream=index \
         -of csv=p=0 "$in" | grep -q .; then
      map=(-map 0:v:0 -map 0:a:0)
      extra_in=()
    else
      echo "note: $in has no audio; adding a silent stereo track" >&2
      map=(-map 0:v:0 -map 1:a:0)
      extra_in=(-f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000)
    fi

    ffmpeg -y -i "$in" "${extra_in[@]}" \
      -vf "$vf" "${map[@]}" -shortest "${PROFILE[@]}" "$out"
    ;;

  *)
    usage
    ;;
esac

echo "wrote $out"
ffprobe -v error -select_streams v:0 \
  -show_entries stream=width,height,r_frame_rate,color_range,profile \
  -of default=nw=1 "${out:-black.mp4}"

