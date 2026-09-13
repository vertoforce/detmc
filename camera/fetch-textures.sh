#!/usr/bin/env bash
# Downloads the Minecraft client jar Chunky uses as its texture source.
# Kept out of the Docker image on purpose: it is Mojang's binary, not ours to bake in.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="${1:-26.2}"
mkdir -p "$HERE/cache"
OUT="$HERE/cache/mc-$VERSION-client.jar"
[ -f "$OUT" ] && { echo "already have $OUT"; exit 0; }

META=$(curl -fsSL https://launchermeta.mojang.com/mc/game/version_manifest_v2.json \
  | python3 -c "import json,sys;print(next(v['url'] for v in json.load(sys.stdin)['versions'] if v['id']=='$VERSION'))")
read -r SHA URL < <(curl -fsSL "$META" \
  | python3 -c "import json,sys;d=json.load(sys.stdin)['downloads']['client'];print(d['sha1'],d['url'])")

curl -fsSL --retry 3 -o "$OUT" "$URL"
echo "$SHA  $OUT" | sha1sum -c -
