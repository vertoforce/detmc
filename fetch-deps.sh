#!/bin/bash
# Pull the Minecraft 26.2 server jar + its bundled libraries into ./libs for the
# compile classpath. 26.2 ships Mojang-named (deobfuscated) classes and the
# official metadata carries no *_mappings download, so there is nothing to remap
# and no mapping toolchain is required.
set -e
cd "$(dirname "$0")"
MC=${1:-26.2}
IMG=eclipse-temurin:25@sha256:dcf835e52330939b6c9f90ecab8aafcbcaa8fbf48423db44de884cf978c10144

if [ -f "libs/minecraft-server-$MC.jar" ] && [ -d libs/mc-libs ]; then
  echo "libs/ already populated for $MC"; exit 0
fi

mkdir -p libs
URL=$(curl -fsSL https://piston-meta.mojang.com/mc/game/version_manifest_v2.json \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print([v['url'] for v in d['versions'] if v['id']=='$MC'][0])")
SRV=$(curl -fsSL "$URL" | python3 -c "import sys,json;print(json.load(sys.stdin)['downloads']['server']['url'])")
echo "server bundler: $SRV"
curl -fsSL -o libs/bundler.jar "$SRV"

docker run --rm -v "$PWD/libs:/libs" -w /libs "$IMG" bash -c "
set -e
rm -rf x && mkdir x && cd x
jar xf ../bundler.jar META-INF/versions META-INF/libraries
cp \"META-INF/versions/$MC/server-$MC.jar\" ../minecraft-server-$MC.jar
mkdir -p ../mc-libs
find META-INF/libraries -name '*.jar' -exec cp {} ../mc-libs/ \;
cd .. && rm -rf x bundler.jar
chmod -R a+r .
"
echo "libs/minecraft-server-$MC.jar + $(ls libs/mc-libs | wc -l) library jars"
