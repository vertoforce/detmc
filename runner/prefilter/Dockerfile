# Cubiomes-based seed prefilter for the detmc scenario runner.
#
# Base image pinned by tag + digest (house rule). Cubiomes is cloned and
# checked out at a pinned commit SHA, never a branch.
#
#   docker build -t mc-cubiomes:e61f905 runner/prefilter/

FROM debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171 AS build

# Cubiomes HEAD as of 2026-09-11 (repo last commit 2024-11-10).
# MC_NEWEST in this commit is MC_1_21 = MC_1_21_WD ("Winter Drop").
ARG CUBIOMES_COMMIT=e61f90580cbdd883214a8054670dacae655e59c0

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates gcc git libc6-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone https://github.com/Cubitect/cubiomes.git cubiomes \
 && git -C cubiomes checkout "${CUBIOMES_COMMIT}" \
 && git -C cubiomes rev-parse HEAD > /src/CUBIOMES_COMMIT

COPY seedscan.c /src/seedscan.c

# tests.c has its own main(), so the sources are listed explicitly.
RUN gcc -O3 -Wall -I/src/cubiomes -o /src/seedscan /src/seedscan.c \
      /src/cubiomes/biomenoise.c /src/cubiomes/biomes.c /src/cubiomes/finders.c \
      /src/cubiomes/generator.c /src/cubiomes/layers.c /src/cubiomes/noise.c \
      /src/cubiomes/quadbase.c /src/cubiomes/util.c -lm

FROM debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171
COPY --from=build /src/seedscan /usr/local/bin/seedscan
COPY --from=build /src/CUBIOMES_COMMIT /etc/cubiomes-commit
ENTRYPOINT ["/usr/local/bin/seedscan"]
