#!/bin/bash
# Fetch the Carpet mod jar for Minecraft 26.2 into ./libs, pinned by URL + hash.
# Carpet is only needed by scenarios that declare setup.fake_players (the
# /player command). Source: Modrinth project "carpet" (TQTTVgYE), version 26.2.
set -euo pipefail
cd "$(dirname "$0")"
URL="https://cdn.modrinth.com/data/TQTTVgYE/versions/bGrLxJ8v/fabric-carpet-26.2%2Bv260616.jar"
JAR="libs/fabric-carpet-26.2+v260616.jar"
SHA256_EXPECT="f6ada912af65c91536d4b0d80adf26cc438253252ddaf259f2c6617ae471311c"
mkdir -p libs
if [ ! -f "$JAR" ]; then
  echo "fetching $URL"
  curl -fsSL --retry 3 -o "$JAR.part" "$URL"
  mv "$JAR.part" "$JAR"
fi
got=$(sha256sum "$JAR" | cut -d' ' -f1)
if [ "$got" != "$SHA256_EXPECT" ]; then
  echo "sha256 mismatch: got $got want $SHA256_EXPECT" >&2; exit 1
fi
echo "$got  $JAR"
