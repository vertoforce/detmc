#!/usr/bin/env bash
# Builds our patched chunky-core jar from chunky-src/chunky into dist/.
# Docker only: no host JDK, no host gradle. Gradle home is cached in
# chunky-src/.gradle-home so repeat builds do not re-download.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/chunky-src/chunky"
VER="${CHUNKY_VERSION:-2.5.0-SNAPSHOT.478.g527cb4a-mobs1}"
# Upstream commit behind the published 2.5.0-SNAPSHOT.478.g527cb4a nightly.
COMMIT="${CHUNKY_COMMIT:-527cb4a26418515e1babe2f217be4813c42a42ca}"

if [ ! -d "$SRC" ]; then
  git clone --filter=blob:none https://github.com/chunky-dev/chunky.git "$SRC"
  git -C "$SRC" checkout "$COMMIT"
  git -C "$SRC" apply "$HERE/chunky-mobs.patch"
fi

mkdir -p "$HERE/chunky-src/.gradle-home" "$HERE/dist"
docker run --rm \
  -v "$SRC":/w -w /w \
  -v "$HERE/chunky-src/.gradle-home":/root/.gradle \
  eclipse-temurin:17-jdk@sha256:36d9a76dc231587873b103c68a789b85d91b41d314dda69730d6bc43a777f2a9 \
  ./gradlew --no-daemon -PnewVersion="$VER" :chunky:jar "$@"
cp "$SRC/chunky/build/libs/chunky-core-$VER.jar" "$HERE/dist/"
echo "built dist/chunky-core-$VER.jar"
