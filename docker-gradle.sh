#!/bin/bash
# Run gradle in a container. No host JDK, no host gradle.
# Gradle home is cached in ./.gradle-home so repeated runs do not re-download.
set -e
cd "$(dirname "$0")"
mkdir -p .gradle-home
exec docker run --rm \
  -v "$PWD:/w" -w /w \
  -v "$PWD/.gradle-home:/root/.gradle" \
  eclipse-temurin:25@sha256:dcf835e52330939b6c9f90ecab8aafcbcaa8fbf48423db44de884cf978c10144 \
  ./gradlew --no-daemon "$@"
