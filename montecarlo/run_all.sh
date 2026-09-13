#!/usr/bin/env bash
# Fan out the Monte Carlo across cores. Every shard writes results/<cfg>_<i>.json
set -u
cd "$(dirname "$0")"
PY=python3
mkdir -p results logs
run () {  # name shards years_per_shard extra...
  local name=$1 shards=$2 yrs=$3; shift 3
  for i in $(seq 0 $((shards-1))); do
    $PY sim.py --years "$yrs" --seed "$i" --out "results/${name}_${i}.json" "$@" \
      > "logs/${name}_${i}.log" 2>&1 &
  done
}
run flat        4  25   --loose 0    --villager
run open64     40 250   --loose 64   --villager
run roofed64   10 200   --loose 64   --villager --roofed
run open512     8 125   --loose 512  --villager
run open8      10 200   --loose 8    --villager
run mix600      4  50   --loose 64   --villager --mix 600
run mixinf      4  50   --loose 64   --villager --mix 1e18
wait
echo DONE
