#!/usr/bin/env bash
# snap.sh <world_dir> <camera.json> <out.png>
# Renders one PNG of a Minecraft world from an arbitrary camera, via headless Chunky.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${CHUNKY_IMAGE:-mc-camera-chunky:2.5.0-478-mobs1}"
TEXTURES="${MC_TEXTURES:-$HERE/cache/mc-26.2-client.jar}"
THREADS="${CHUNKY_THREADS:-$(nproc)}"
MEM="${CHUNKY_MEM:-4G}"

usage() { echo "usage: $0 <world_dir> <camera.json> <out.png>" >&2; exit 2; }
[ $# -eq 3 ] || usage

WORLD="$(cd "$1" && pwd)"
CAM="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"
OUT="$3"
mkdir -p "$(dirname "$OUT")"
OUT="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"

[ -d "$WORLD" ] || { echo "no such world dir: $WORLD" >&2; exit 1; }
[ -f "$CAM" ]   || { echo "no such camera file: $CAM" >&2; exit 1; }
[ -f "$TEXTURES" ] || { echo "missing textures: $TEXTURES (run ./fetch-textures.sh)" >&2; exit 1; }

# Scratch scene dir. Chunky writes the octree, dumps and output PNG next to the
# scene file, so it must be writable and is cheapest to throw away each time.
WORK="$(mktemp -d "${TMPDIR:-/tmp}/chunky-scene.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# The scene records the world path as Chunky will see it *inside* the container.
python3 "$HERE/scene_gen.py" "$CAM" "$WORK/snap.json" \
  --world-path /world --scan-dir "$WORLD" --name snap >/dev/null

# -f is required on a first pass: a freshly generated scene has no .octree2 dump
# yet, and without it Chunky treats "failed to load octree" as fatal even though
# it has just rebuilt the octree from the region files.
chunky() {
  # Shared host memory gate before every render: a Chunky pass measured ~1.9 GB
  # RSS, and 3 of them plus a hunt wave OOM-killed the box on 2026-09-12.
  # Blocks (no timeout) until the budget has room; see runner/memgate.py.
  python3 "$HERE/../runner/memgate.py" --need 2500
  docker run --rm ${CHUNKY_NAME:+--name "$CHUNKY_NAME"} \
    -e JAVA_TOOL_OPTIONS="-Djava.awt.headless=true -Xmx$MEM" \
    -v "$WORLD":/world:ro \
    -v "$WORK":/scene \
    -v "$TEXTURES":/textures.jar:ro \
    "$IMAGE" \
    -texture /textures.jar \
    -threads "$THREADS" \
    "$@" \
    2>&1 | tr '\r' '\n' | grep -vE '[0-9]+\.[0-9]% \(|^Loading scene:|^$' | sed 's/^/[chunky] /' || true
}

START=$(date +%s.%N)
if [ "${MOB_MARKERS:-0}" != "0" ]; then
  # Chunky draws no living mobs, but its scene file takes a hand-written entity
  # list, and armor stands there are full models (see mob_markers.py). Measured
  # 2026-09-11: those injected entities are dropped whenever Chunky rebuilds the
  # octree from region files (-f), and survive only when the octree comes from a
  # .octree2 dump. So render twice: pass 1 at 1 spp purely to build the dump,
  # then inject the markers into the scene Chunky just saved and render for real.
  # Chunky rewrites the scene file itself on save, so the real sample target is
  # kept out here in the shell rather than in an extra JSON key it would drop.
  SPP_TARGET=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sppTarget"])' "$WORK/snap.json")
  python3 -c 'import json,sys; p=sys.argv[1]; s=json.load(open(p)); s["sppTarget"]=1; json.dump(s,open(p,"w"),indent=2)' "$WORK/snap.json"
  chunky -f -render /scene/snap.json | sed 's/^/[pass1] /'
  python3 -c 'import json,sys; p,t=sys.argv[1],int(sys.argv[2]); s=json.load(open(p)); s["sppTarget"]=t; s["spp"]=0; s["renderTime"]=0; json.dump(s,open(p,"w"),indent=2)' "$WORK/snap.json" "$SPP_TARGET"
  python3 "$HERE/mob_markers.py" "$WORLD" --patch-scene "$WORK/snap.json" \
    ${MOB_MARKERS_INVISIBLE:+--invisible-stand}
  rm -f "$WORK"/snapshots/*.png "$WORK/snap.dump"
  chunky -render /scene/snap.json
else
  chunky -f -render /scene/snap.json
fi
END=$(date +%s.%N)

# Headless Chunky writes "<scene-dir>/snapshots/<scene>-<spp>.png". The spp in the
# name is the pass boundary it actually stopped on, so it can exceed the target.
RENDERED="$(ls -t "$WORK"/snapshots/snap-*.png 2>/dev/null | head -1 || true)"
[ -n "$RENDERED" ] || { echo "render produced no PNG" >&2; exit 1; }
cp "$RENDERED" "$OUT"

printf 'rendered %s in %ss\n' "$OUT" "$(awk "BEGIN{printf \"%.1f\", $END - $START}")"
