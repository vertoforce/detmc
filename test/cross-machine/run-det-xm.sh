#!/bin/bash
# usage: run-det.sh <A|B> [spawn_mobs true|false]
# Assumes container mc-det-<label> was just started from an empty ./data<label>.
# Adds, vs the original bench script: a 100-tick checkpoint before the
# full 24000-tick sprint, and entity UUID dumps, so a divergence can be dated.
label=$1; sm=${2:-true}; ctr=${CTR:-mc-det-$label}; out=result-$label.txt
cd "$(dirname "$0")"
rc() { echo ">>> $1" | tee -a $out; docker exec $ctr rcon-cli "$1" 2>&1 | tee -a $out; }
dump() { # $1 = stage label
  echo "### [$1] enderman Pos" | tee -a $out
  rc "execute as @e[type=enderman] run data get entity @s Pos"
  echo "### [$1] zombie Pos" | tee -a $out
  rc "execute as @e[type=zombie] run data get entity @s Pos"
  echo "### [$1] all entities UUID" | tee -a $out
  rc "execute as @e run data get entity @s UUID"
  echo "### [$1] all entities id" | tee -a $out
  rc "execute as @e run data get entity @s id"
  echo "### [$1] all entities Motion" | tee -a $out
  rc "execute as @e run data get entity @s Motion"
  echo "### [$1] enderman carriedBlockState" | tee -a $out
  rc "execute as @e[type=enderman] run data get entity @s carriedBlockState"
}
: > $out
for i in $(seq 1 3000); do
  if docker logs $ctr 2>&1 | grep -q 'Done ('; then break; fi
  if docker logs $ctr 2>&1 | grep -q 'Minecraft server failed'; then
    echo "CONTAINER_FAILED $ctr" | tee -a $out; exit 3
  fi
  sleep 0.1
done
echo "DONE_SEEN $(date +%s.%N)" | tee -a $out
# Phase 7: optional extra wall-clock delay while frozen, to test whether the number of
# frozen server ticks before the scripted setup is an input to the world state.
if [ -n "$PRE_DELAY" ]; then echo "### PRE_DELAY $PRE_DELAY s" | tee -a $out; sleep "$PRE_DELAY"; fi
echo "### detmc startup lines" | tee -a $out
docker logs $ctr 2>&1 | grep -i 'detmc\|Loading .* mods' | tee -a $out
rc "tick freeze"
rc "time query gametime"
rc "gamerule advance_time false"
rc "time set midnight"
rc "weather clear 1000000"
rc "forceload add -80 -80 80 80"
rc "forceload add 16 -112 176 48"
rc "gamerule spawn_mobs $sm"
rc "gamerule mob_griefing"
# Chunk loading keeps running while ticks are frozen, so the entity set is still
# growing right after the forceload. Wait until the entity list stops changing,
# otherwise A and B get dumped at different points of the same sequence.
settle() {
  # 6 polls (3 s) was too short: chunk generation is serial under detmc and a
  # 3 s lull mid-forceload is common, so the two runs got dumped at different
  # points of the same sequence. 30 stable polls, 900-poll cap.
  prev=""; same=0
  for i in $(seq 1 900); do
    now=$(docker exec $ctr rcon-cli "execute as @e run data get entity @s Pos" 2>&1 | md5sum)
    if [ "$now" = "$prev" ]; then same=$((same+1)); else same=0; fi
    [ $same -ge 30 ] && break
    prev=$now; sleep 0.5
  done
  echo "### settled after ${i} polls" | tee -a $out
}
settle
echo "### pre-existing entities" | tee -a $out
rc "execute as @e run data get entity @s Pos"
# Spawn-light trace, extracted BEFORE the sprint: natural spawning calls the same
# check thousands of times per second once ticks run, which would bury the worldgen
# reads. Worldgen reads carry a chunk tag; tick-time reads carry "chunk=-".
docker logs $ctr 2>&1 | grep -F '[detmc-sl]' | sed 's/.*\[detmc-sl\] //' > sl-$label.txt || true
echo "### spawn-light trace lines: $(wc -l < sl-$label.txt)" | tee -a $out
for k in $(seq 1 30); do rc "execute in minecraft:overworld run summon minecraft:enderman ~ ~ ~ {PersistenceRequired:1b}"; done
for k in $(seq 1 30); do rc "execute in minecraft:overworld run summon minecraft:zombie ~ ~ ~ {PersistenceRequired:1b}"; done
rc "time query gametime"

# Advancing ticks, exactly.
#
# The old helper did: tick unfreeze / tick sprint N / poll the log for "Sprint
# completed" / tick freeze. Two windows in that sequence run the server free at 20
# tps: between the unfreeze and the sprint starting, and between the sprint ending
# and the freeze landing, the latter bounded only by the 0.1 s log poll. Measured:
# two runs dumped at nominal t1 held endermen with Motion y -0.447 and -0.652, i.e.
# a different number of gravity ticks. That is the harness, not the mod.
#
# So: sprint to short of the target, freeze, then close the gap with "tick step",
# which runs exactly N ticks while frozen and stays frozen, and re-check gametime
# until it is exactly the target. Every dump is then taken at the same gametime in
# both runs, whatever the wall clock did.
gametime() { docker exec $ctr rcon-cli "time query gametime" 2>&1 | grep -oE '[0-9]+' | tail -1; }

sprint() {
  before=$(docker logs $ctr 2>&1 | grep -c 'Sprint completed')
  docker exec $ctr rcon-cli "tick unfreeze" >/dev/null 2>&1
  docker exec $ctr rcon-cli "tick sprint $1" >/dev/null 2>&1
  for i in $(seq 1 12000); do
    now=$(docker logs $ctr 2>&1 | grep -c 'Sprint completed')
    [ "$now" -gt "$before" ] && break
    sleep 0.1
  done
  docker exec $ctr rcon-cli "tick freeze" >/dev/null 2>&1
}

SPRINT_MARGIN=${SPRINT_MARGIN:-60}   # ticks left for "tick step" to place exactly; > any free-run window

goto_tick() { # $1 = absolute target gametime
  cur=$(gametime); need=$(( $1 - cur ))
  if [ "$need" -gt "$SPRINT_MARGIN" ]; then
    sprint $(( need - SPRINT_MARGIN ))
  fi
  cur=$(gametime); need=$(( $1 - cur ))
  if [ "$need" -gt 0 ]; then
    # One step only. Re-issuing while frozenTicksToRun is still counting down just
    # overwrites the counter and overshoots, so wait for gametime to stop moving.
    docker exec $ctr rcon-cli "tick step $need" >/dev/null 2>&1
    # "tick step" runs at the server tick rate, so 24000 ticks is 20 minutes. Wait on
    # the target, not on a fixed number of polls, and treat 5 s of no progress as the
    # step having ended early.
    prev=-1; stalls=0
    for i in $(seq 1 6000); do
      now=$(gametime)
      [ "$now" -ge "$1" ] && break
      if [ "$now" = "$prev" ]; then stalls=$((stalls+1)); else stalls=0; fi
      [ $stalls -ge 10 ] && break
      prev=$now; sleep 0.5
    done
  fi
  cur=$(gametime)
  if [ "$cur" -ne "$1" ]; then
    echo "### WARNING: wanted gametime $1, landed on $cur" | tee -a $out
  fi
}

base=$(gametime)
echo "### tick base $base" | tee -a $out

# TARGETS is a list of absolute tick offsets from the base. Each entry is reached
# from the previous one, so "3000" alone is a single uninterrupted 3000-tick segment
# while "100 24000" inserts a frozen barrier at t100. Phase 4 showed the barrier is
# what used to hide the drift, so the default is now one uninterrupted run.
for target in ${TARGETS:-24000}; do
  goto_tick $(( base + target ))
  rc "time query gametime"
  dump "t$target"
done

# `save-all flush` over rcon times out (the reply never arrives on a busy server) but the
# command still runs. Wait for the mod's "saveEverything done" line rather than sleeping,
# otherwise the region md5 below can hash a half-written or previous save.
saves_before=$(docker logs $ctr 2>&1 | grep -c 'saveEverything done')
rc "save-all flush"
for i in $(seq 1 600); do
  # --tail keeps this cheap: the full log is megabytes of [detmc-io] lines.
  now=$(( saves_before + $(docker logs --tail 400 $ctr 2>&1 | grep -c 'saveEverything done.*flush=true') ))
  [ "$now" -gt "$saves_before" ] && break
  sleep 0.5
done
echo "### save-all flush: $(docker logs $ctr 2>&1 | grep 'saveEverything done' | tail -1 | sed 's/.*\[detmc\] //') (waited $i polls)" | tee -a $out
sleep 1
docker logs $ctr 2>&1 | grep -F '[detmc-ea]' | sed 's/.*\[detmc-ea\] //' > trace-$label.txt || true
docker logs $ctr 2>&1 | grep -F '[detmc-dg]' | sed 's/.*\[detmc-dg\] //' > dg-$label.txt || true
docker logs $ctr 2>&1 | grep -F '[detmc-io]' | sed 's/.*\[detmc-io\] //' > io-$label.txt || true
docker logs $ctr 2>&1 | grep -F '[detmc-ew]' | sed 's/.*\[detmc-ew\] //' > ew-$label.txt || true
docker logs $ctr 2>&1 | grep -F '[detmc-ec]' | sed 's/.*\[detmc-ec\] //' > ec-$label.txt || true
echo "### trace lines: $(wc -l < trace-$label.txt)" | tee -a $out
echo "### region md5" | tee -a $out
(cd data$label/world/dimensions/minecraft/overworld && md5sum region/*.mca entities/*.mca) | tee -a $out
# RegionFile.java:156 getTimestamp() -> (int)(Util.getEpochMillis() / 1000L), written into
# bytes 4096..8191 of every region file on every save (RegionFile.java:278,309). Two runs
# taken minutes apart can therefore never have equal raw md5s whatever the world holds, so
# the section above is advisory only. This one blanks that table and is the real check.
echo "### entity region bytes" | tee -a $out
(cd data$label/world/dimensions/minecraft/overworld && du -b entities/*.mca 2>/dev/null | tee -a ../../../../../$out; echo "total $(cat entities/*.mca 2>/dev/null | wc -c)" | tee -a ../../../../../$out)
echo "### region md5 no timestamps" | tee -a $out
python3 region-md5.py data$label/world/dimensions/minecraft/overworld | tee -a $out
echo "RUN_FINISHED" | tee -a $out
