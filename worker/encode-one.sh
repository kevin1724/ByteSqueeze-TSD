#!/bin/sh
set -e

# SRC should be a FULL path as seen on the host,
# e.g. /mnt/media/media/Movies/MovieFolder/MyMovie.mkv
SRC="$SRC"

if [ -z "$SRC" ]; then
  echo "ERROR: You must pass the source file via -e SRC=/path/to/file.mkv"
  exit 1
fi

if [ ! -f "$SRC" ]; then
  echo "ERROR: File not found inside container: $SRC"
  echo "For headless jobs, confirm the controller download completed under /work/jobs."
  echo "For mounted-media jobs, confirm the controller-to-worker path mapping."
  exit 1
fi

if ! command -v HandBrakeCLI >/dev/null 2>&1; then
  echo "ERROR: HandBrakeCLI is not installed or is not on PATH."
  exit 127
fi

# Default suffix for transcoded files
SUFFIX="${SUFFIX:-TSD}"

DIR=$(dirname "$SRC")
BASE=$(basename "$SRC")
NAME="${BASE%.*}"

# The controller snapshots these global settings onto every queued job. Keep
# MKV as the default because that is what all bundled presets used before the
# setting existed. Container options are placed after preset/user arguments so
# the global selection is authoritative.
OUTPUT_CONTAINER=$(printf '%s\n' "${HB_OUTPUT_CONTAINER:-mkv}" | tr 'A-Z' 'a-z')
WEB_OPTIMIZED="${HB_WEB_OPTIMIZED:-0}"
case "$OUTPUT_CONTAINER" in
  mp4)
    EXT="mp4"
    CONTAINER_OPTS="--format av_mp4"
    case "$WEB_OPTIMIZED" in
      1|true|TRUE|yes|YES|on|ON) CONTAINER_OPTS="$CONTAINER_OPTS --optimize" ;;
      *) WEB_OPTIMIZED="0" ;;
    esac
    ;;
  *)
    OUTPUT_CONTAINER="mkv"
    EXT="mkv"
    WEB_OPTIMIZED="0"
    CONTAINER_OPTS="--format av_mkv"
    ;;
esac

# 🔒 Skip if this file already looks TSD-tagged
LOWER_NAME=$(printf '%s\n' "$NAME" | tr 'A-Z' 'a-z')
if echo "$LOWER_NAME" | grep -q -- '-tsd$'; then
  echo "INFO: Source already has -TSD tag, skipping encode: $SRC"
  exit 0
fi

OUT="${DIR}/${NAME}-${SUFFIX}.${EXT}"

# Preset info comes from env (set by jobs.py based on preset key)
PRESET_FILE="${HB_PRESET_FILE:-/presets/my-presets.json}"
PRESET_NAME="${HB_PRESET_NAME:-MyPresetName}"
DIMENSION_OPTS="${HB_DIMENSION_OPTS:-}"
HW_DECODE_OPTS="${HB_HW_DECODE_OPTS:---disable-hw-decoding}"
QSV_ADAPTER="${TSD_QSV_ADAPTER:-0}"
QSV_ADAPTER_OPTS=""

# Pin HandBrake to the same adapter that the Linux render-node preflight
# validates. HandBrake's patched hwaccel layer resolves this adapter index to
# its DRM render node (for example adapter 0 -> /dev/dri/renderD128).
QSV_JOB=0
case "${HB_VIDEO_ENCODER:-}" in
  qsv_*) QSV_JOB=1 ;;
esac
if [ "$HW_DECODE_OPTS" = "--enable-hw-decoding qsv" ]; then
  QSV_JOB=1
fi
if [ "$QSV_JOB" -eq 1 ]; then
  case "$QSV_ADAPTER" in
    ''|*[!0-9]*)
      echo "[ByteSqueeze] Invalid TSD_QSV_ADAPTER='$QSV_ADAPTER'; selecting adapter 0"
      QSV_ADAPTER=0
      ;;
  esac
  QSV_ADAPTER_OPTS="--qsv-adapter $QSV_ADAPTER"
  if [ -x /usr/local/bin/bytesqueeze-qsv-preflight ]; then
    if ! /usr/local/bin/bytesqueeze-qsv-preflight encode; then
      if [ "$HW_DECODE_OPTS" = "--enable-hw-decoding qsv" ]; then
        echo "[ByteSqueeze] Hardware decode: software fallback (QSV render-device preflight failed)"
        HW_DECODE_OPTS="--disable-hw-decoding"
        HB_HW_DECODE_LABEL="software fallback (QSV render-device preflight failed)"
      fi
    fi
  elif [ "$HW_DECODE_OPTS" = "--enable-hw-decoding qsv" ]; then
    echo "[ByteSqueeze] Hardware decode: software fallback (QSV preflight helper is missing)"
    HW_DECODE_OPTS="--disable-hw-decoding"
    HB_HW_DECODE_LABEL="software fallback (QSV preflight helper is missing)"
  fi
fi

echo "=== HandBrake one-shot encode ==="
echo "Source : $SRC"
echo "Target : $OUT"
echo "Suffix : -$SUFFIX"
echo "Preset file : $PRESET_FILE"
echo "Preset name : $PRESET_NAME"
echo "[ByteSqueeze] Output container: $(printf '%s' "$OUTPUT_CONTAINER" | tr 'a-z' 'A-Z')"
echo "[ByteSqueeze] Web optimized: $([ "$WEB_OPTIMIZED" = "0" ] && printf 'off' || printf 'on')"
echo "HandBrake : $(HandBrakeCLI --version 2>&1 | sed -n '1p')"
echo "[ByteSqueeze] Hardware decode: ${HB_HW_DECODE_LABEL:-software fallback (not configured)}"
echo "[ByteSqueeze] Video encoder: ${HB_VIDEO_ENCODER:-unknown}"
echo "[ByteSqueeze] Source resolution: ${HB_SOURCE_RESOLUTION:-unknown}"
echo "[ByteSqueeze] Target resolution: ${HB_TARGET_RESOLUTION:-unknown}"
echo "[ByteSqueeze] Selected preset: $PRESET_NAME"
echo "=================================="

# Safety: don't overwrite an existing output file
if [ -f "$OUT" ]; then
  echo "ERROR: Output already exists: $OUT"
  echo "Refusing to overwrite. Delete/rename it first."
  exit 1
fi

# ------------------------------------------------------------
# Optional: override CPU thread usage from HB_THREADS env
# (HB_THREADS is set in jobs.py based on the Settings page)
#
# If HB_THREADS is a positive integer, we build:
#   HB_THREAD_OPTS="--encopts threads=<N>"
#
# This is then included in the HandBrakeCLI calls below.
# If HB_THREADS is unset/0/invalid, HB_THREAD_OPTS stays empty.
# ------------------------------------------------------------
HB_THREAD_OPTS=""
if [ -n "$HB_THREADS" ]; then
  # Only treat HB_THREADS as valid if it looks like a positive integer
  case "$HB_THREADS" in
    *[!0-9]*)
      echo "INFO: Ignoring non-numeric HB_THREADS value: $HB_THREADS" ;;
    *)
      if [ "$HB_THREADS" -gt 0 ] 2>/dev/null; then
        HB_THREAD_OPTS="--encopts threads=$HB_THREADS"
        echo "INFO: Using HB_THREADS=$HB_THREADS (HB_THREAD_OPTS: $HB_THREAD_OPTS)"
      fi ;;
  esac
fi

# ------------------------------------------------------------
# Main encode path:
# - If preset file exists → use preset import + preset name
# - Else → fallback basic x264 settings
# In both cases, we append HB_THREAD_OPTS if present.
# ------------------------------------------------------------
if [ -f "$PRESET_FILE" ]; then
  echo "Using preset file + preset name..."
EXTRA_ARGS="${HB_EXTRA_ARGS:-}"

set +e
HandBrakeCLI \
  --preset-import-file "$PRESET_FILE" \
  -Z "$PRESET_NAME" \
  ${HB_THREAD_OPTS} \
  ${EXTRA_ARGS} \
  ${DIMENSION_OPTS} \
  ${QSV_ADAPTER_OPTS} \
  ${HW_DECODE_OPTS} \
  ${CONTAINER_OPTS} \
  -i "$SRC" \
  -o "$OUT"
ENCODE_STATUS=$?
set -e

# Some H.264/HEVC streams advertise a profile the host QSV implementation
# cannot decode. Keep the QSV encoder, discard only this new partial output,
# and retry once with software decoding instead of failing the whole job.
if [ "$ENCODE_STATUS" -ne 0 ] && [ "$HW_DECODE_OPTS" = "--enable-hw-decoding qsv" ]; then
  echo "[ByteSqueeze] Hardware decode: software fallback (QSV decode attempt exited $ENCODE_STATUS)"
  rm -f -- "$OUT"
  HandBrakeCLI \
    --preset-import-file "$PRESET_FILE" \
    -Z "$PRESET_NAME" \
    ${HB_THREAD_OPTS} \
    ${EXTRA_ARGS} \
    ${DIMENSION_OPTS} \
    ${QSV_ADAPTER_OPTS} \
    --disable-hw-decoding \
    ${CONTAINER_OPTS} \
    -i "$SRC" \
    -o "$OUT"
elif [ "$ENCODE_STATUS" -ne 0 ]; then
  exit "$ENCODE_STATUS"
fi

else
  echo "WARNING: Preset file not found, using basic fallback settings..."
  HandBrakeCLI \
    ${HB_THREAD_OPTS} \
    ${DIMENSION_OPTS} \
    ${HW_DECODE_OPTS} \
    ${CONTAINER_OPTS} \
    -i "$SRC" \
    -o "$OUT" \
    -e x264 -q 20 -B 160
fi

# ------------------------------------------------------------
# Post-encode checks + clean up
# ------------------------------------------------------------
if [ ! -f "$OUT" ]; then
  echo "ERROR: Encode failed, output file not found."
  exit 1
fi

echo "Done!"
echo "New file: $OUT"
echo "Source kept for app-level validation: $SRC"
