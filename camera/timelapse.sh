#!/usr/bin/env bash
# timelapse.sh <container> <world_dir> <camera.json> <out.mp4> [opts]
#
# Advances a running Minecraft server in fixed tick steps, snapshots the world
# after each step, then renders every snapshot with the same camera and stitches
# them into an mp4.
#
#   --frames N     number of snapshots            (default 40)
#   --ticks N      ticks advanced per frame       (default 2000)
#   --fps N        output frame rate              (default 10)
#   --jobs N       parallel renders               (default 1, env CHUNKY_JOBS)
#   --snap-dir D   where snapshots land           (default ./timelapse-<ts>)
#   --keep         keep the per-frame PNGs and world copies
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FFMPEG_IMAGE="${FFMPEG_IMAGE:-linuxserver/ffmpeg:7.1.1@sha256:aea59a11c54291ac456bb2d67000445e5a8994f70bc3d96cdc29f022fbbf89fb}"  # pinned 2026-09-11

# One render at a time by default: 3 concurrent Chunky passes (~1.9 GB each)
# were part of the 2026-09-12 host OOM.  Raise deliberately, and only with
# the memory gate's reserve in mind: CHUNKY_JOBS=3 or --jobs 3.
FRAMES=40; TICKS=2000; FPS=10; JOBS="${CHUNKY_JOBS:-1}"; SNAPDIR=""; KEEP=0

[ $# -ge 4 ] || { echo "usage: $0 <container> <world_dir> <camera.json> <out.mp4> [opts]" >&2; exit 2; }
CONTAINER="$1"; WORLD="$(cd "$2" && pwd)"; CAM="$(cd "$(dirname "$3")" && pwd)/$(basename "$3")"; OUT="$4"
shift 4
while [ $# -gt 0 ]; do
  case "$1" in
    --frames) FRAMES="$2"; shift 2;;
    --ticks)  TICKS="$2";  shift 2;;
    --fps)    FPS="$2";    shift 2;;
    --jobs)   JOBS="$2";   shift 2;;
    --snap-dir) SNAPDIR="$2"; shift 2;;
    --keep)   KEEP=1; shift;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
done
mkdir -p "$(dirname "$OUT")"; OUT="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"
[ -n "$SNAPDIR" ] || SNAPDIR="$HERE/timelapse-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$SNAPDIR"

rcon() { docker exec "$CONTAINER" rcon-cli "$@"; }

wait_for_sprint() {
  # "tick sprint" returns immediately; the run itself is asynchronous.
  for _ in $(seq 1 300); do
    rcon "tick query" 2>&1 | grep -q "running normally" && return 0
    sleep 1
  done
  echo "sprint did not finish in 300s" >&2; return 1
}

DIM_REL="dimensions/minecraft/overworld"

capture() {
  local dest="$1"
  # Freeze first so nothing can write between the flush and the copy. We copy
  # level.dat, the region files and the entity files only - never session.lock,
  # which the running server holds and which Chunky does not need.
  # entities/ is what makes mobs show up: Chunky loads living entities from
  # entities/*.mca, not from region/*.mca, so a copy without it renders an
  # empty world.
  rcon "tick freeze"  >/dev/null
  rcon "save-all flush" >/dev/null
  mkdir -p "$dest/$DIM_REL"
  cp "$WORLD/level.dat" "$dest/level.dat"
  cp -r "$WORLD/$DIM_REL/region" "$dest/$DIM_REL/region"
  if [ -d "$WORLD/$DIM_REL/entities" ]; then
    cp -r "$WORLD/$DIM_REL/entities" "$dest/$DIM_REL/entities"
  fi
  rcon "tick unfreeze" >/dev/null
}

echo "capturing $FRAMES frames, $TICKS ticks apart, into $SNAPDIR"
for i in $(seq 0 $((FRAMES - 1))); do
  n=$(printf '%04d' "$i")
  if [ "$i" -gt 0 ]; then
    rcon "tick sprint $TICKS" >/dev/null
    wait_for_sprint
  fi
  capture "$SNAPDIR/world-$n"
  printf '\rcaptured frame %s/%s' "$((i + 1))" "$FRAMES"
done
echo

echo "rendering $FRAMES frames, $JOBS at a time"
mkdir -p "$SNAPDIR/frames"
RENDER_START=$(date +%s)
# Each Chunky gets a slice of the cores; the renders are independent so this is
# a straight throughput win over running them one at a time.
THREADS_EACH=$(( $(nproc) / JOBS )); [ "$THREADS_EACH" -lt 1 ] && THREADS_EACH=1
export CHUNKY_THREADS="$THREADS_EACH"
# Gate once here where the wait is visible: the xargs children below send
# snap.sh's stderr to /dev/null, so their own gate waits would be silent.
python3 "$HERE/../runner/memgate.py" --need $((2500 * JOBS))
printf '%s\n' "$SNAPDIR"/world-* \
  | xargs -P "$JOBS" -I{} bash -c \
      'd="{}"; n="${d##*world-}"; "$0" "$d" "$1" "$2/frames/frame-$n.png" >/dev/null 2>&1 || echo "FAILED $n" >&2' \
      "$HERE/snap.sh" "$CAM" "$SNAPDIR"
RENDER_END=$(date +%s)

COUNT=$(ls "$SNAPDIR/frames"/frame-*.png 2>/dev/null | wc -l)
[ "$COUNT" -eq "$FRAMES" ] || echo "warning: $COUNT/$FRAMES frames rendered" >&2
[ "$COUNT" -gt 0 ] || { echo "no frames rendered" >&2; exit 1; }

echo "stitching $COUNT frames at ${FPS}fps"
docker run --rm -u "$(id -u):$(id -g)" \
  -v "$SNAPDIR/frames":/frames \
  -v "$(dirname "$OUT")":/out \
  "$FFMPEG_IMAGE" \
  -y -framerate "$FPS" -pattern_type glob -i '/frames/frame-*.png' \
  -c:v libx264 -pix_fmt yuv420p -crf 18 \
  -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" \
  "/out/$(basename "$OUT")" >/dev/null 2>&1

TOTAL=$((RENDER_END - RENDER_START))
printf 'wrote %s (%s frames, %ss render wall, %.1fs/frame effective)\n' \
  "$OUT" "$COUNT" "$TOTAL" "$(awk "BEGIN{printf \"%.1f\", $TOTAL/$COUNT}")"

if [ "$KEEP" -eq 0 ]; then
  rm -rf "$SNAPDIR"/world-*
  echo "removed world copies (pass --keep to retain them); PNGs kept in $SNAPDIR/frames"
fi
