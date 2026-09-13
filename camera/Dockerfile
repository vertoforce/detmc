# Headless Chunky path tracer for Minecraft world -> PNG.
# Base pinned by tag + digest per this project's image-pinning rule.
FROM eclipse-temurin:21@sha256:1f79c73404fb0cccf9a3459eda22892f368d994b1028d6fb1ae871c1f49749a6

# Chunky nightly 2.5.0-SNAPSHOT.478.g527cb4a (built 2026-09-05).
# This is the first channel with MC 26.2 block support (chunky-dev/chunky#1889,
# "Add support for 26.2 snapshot 7"). The last tagged release, 2.4.6 (2024-01),
# predates 1.21 and cannot read our worlds.
ARG CHUNKY_BUILD=2.5.0-SNAPSHOT.478.g527cb4a
ENV CHUNKY_BUILD=${CHUNKY_BUILD}

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    rm -rf /var/lib/apt/lists/*

# Every jar is verified against the sha256 published in chunkyupdate's
# snapshot.json, so the build is reproducible even though the channel floats.
COPY libs.sha256 /opt/chunky/libs.sha256
RUN set -eux; \
    mkdir -p /opt/chunky/lib; \
    cd /opt/chunky/lib; \
    while read -r _sha name; do \
        curl -fsSL --retry 3 -o "$name" "https://chunkyupdate.lemaik.de/lib/$name"; \
    done < /opt/chunky/libs.sha256; \
    sha256sum -c /opt/chunky/libs.sha256

# chunky-core itself is built from source by ./build-chunky.sh, because the
# published snapshot draws no hostile mobs. See RESEARCH-mobs.md.
ARG CHUNKY_JAR=dist/chunky-core-2.5.0-SNAPSHOT.478.g527cb4a-mobs1.jar
COPY ${CHUNKY_JAR} /opt/chunky/lib/

ENV JAVA_TOOL_OPTIONS="-Djava.awt.headless=true"
WORKDIR /work
ENTRYPOINT ["java", "-cp", "/opt/chunky/lib/*", "se.llbit.chunky.main.Chunky"]
CMD ["-help"]
