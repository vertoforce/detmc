#!/bin/bash
# Cross-machine determinism run. One container, fresh data dir, same jar+image+env.
# usage: xm-run.sh <label A|B> <compose service> <container name> <artifact tag>
# Env passthrough (must be identical on both machines): TARGETS, SPRINT_MARGIN, DETMC_EXTRA_OPTS
set -e
label=$1; svc=$2; ctr=$3; tag=$4
cd "$(dirname "$0")"
compose() { docker compose -f compose-det.yml --env-file .env "$@"; }
compose rm -sf $svc >/dev/null 2>&1 || true
rm -rf data$label; mkdir -p data$label
rm -f result-$label.txt dg-$label.txt io-$label.txt ew-$label.txt ec-$label.txt trace-$label.txt sl-$label.txt
compose up -d $svc
# Machine facts, recorded from inside the container and from the host.
mkdir -p out-$tag
{ echo "=== host lscpu ==="; lscpu | grep -E "^Architecture|^CPU\(s\)|^Model name|^Vendor ID|^CPU family|^Model:|^Stepping|^CPU max MHz|^Hypervisor|^Virtualization"
  echo "=== host /proc/cpuinfo model name ==="; grep -m1 "model name" /proc/cpuinfo
  echo "=== host cpu flags (sorted) ==="; grep -m1 ^flags /proc/cpuinfo | cut -d: -f2 | tr ' ' '\n' | grep -v '^$' | sort | tr '\n' ' '; echo
  echo "=== host kernel ==="; uname -srmo
  echo "=== host mem ==="; free -m | head -2
  echo "=== host loadavg at start ==="; cat /proc/loadavg
  echo "=== docker version ==="; docker version --format '{{.Server.Version}}'
  echo "=== image id ==="; docker inspect --format '{{.Id}}' $(grep -oE 'itzg/minecraft-server:[^ ]+' compose-det.yml | head -1)
  echo "=== jar sha256 ==="; sha256sum mods/*.jar
} > out-$tag/machine.txt 2>&1
# Wait for the JVM to exist, then record its version and the generated server.properties.
for i in $(seq 1 600); do docker exec $ctr java -version >/dev/null 2>&1 && break; sleep 0.5; done
{ echo "=== java -version (in container) ==="; docker exec $ctr java -version
  echo "=== JAVA_HOME / java path ==="; docker exec $ctr sh -c 'readlink -f $(command -v java)'
  echo "=== container uname ==="; docker exec $ctr uname -srmo
} >> out-$tag/machine.txt 2>&1
CTR=$ctr ./run-det-xm.sh $label
cp result-$label.txt dg-$label.txt io-$label.txt ew-$label.txt out-$tag/ 2>/dev/null || true
cp ec-$label.txt trace-$label.txt sl-$label.txt out-$tag/ 2>/dev/null || true
mkdir -p out-$tag/entities out-$tag/region
cp data$label/world/dimensions/minecraft/overworld/entities/*.mca out-$tag/entities/ 2>/dev/null || true
cp data$label/world/dimensions/minecraft/overworld/region/*.mca out-$tag/region/ 2>/dev/null || true
# server.properties with the rcon password line removed (it is identical on both by construction).
grep -v -i -e rcon.password -e management-server-secret data$label/server.properties > out-$tag/server.properties.txt 2>/dev/null || true
docker logs $ctr > out-$tag/container.log 2>&1 || true
gzip -f out-$tag/container.log
echo "TARGETS=$TARGETS SPRINT_MARGIN=$SPRINT_MARGIN DETMC_EXTRA_OPTS=$DETMC_EXTRA_OPTS label=$label ctr=$ctr" > out-$tag/params.txt
sha256sum mods/*.jar >> out-$tag/params.txt
compose rm -sf $svc
echo "XM_DONE $tag $(date -Is)"
