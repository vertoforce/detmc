#!/bin/bash
# sweep-pair.sh -- the phase-8 "sweep" determinism pair.
#
#   ./sweep-pair.sh [case]            run A then B and compare   (case: sweep)
#   ./sweep-pair.sh sweep-control     the same script with -Ddetmc.syncRandom=false
#   LABELS=A ./sweep-pair.sh sweep    one half only
#
# Why this script exists: every one of the thirteen phase-8 fixes is off the path of
# the compose-det.yml world (no players, no villagers, no enchanted items, no furnace,
# no scoreboard, no structure block, no @e[sort=random]).  The 24000-tick pair in
# STATUS therefore proves only that the sweep does not regress that world.  This case
# builds a world that DOES walk those paths and runs the same A/B comparison on it.
#
# Shape: identical to the compose-det.yml pair -- one container at a time, fresh data
# dir, MEMORY=2G, memgate before every launch, `tick sprint`+`tick step` to land on an
# exact gametime, and the divergence question answered by the harness's own
# cross-machine/compare-dg.py over the per-tick [detmc-dg] digest.
#
# Everything the world does lives in the case directory (cases/<case>/), so the
# control case is the same commands with one JVM flag flipped.
set -e
cd "$(dirname "$0")"
CASE_NAME=${1:-sweep}
CASE=cases/$CASE_NAME
[ -d "$CASE" ] || { echo "no such case: $CASE" >&2; exit 2; }

# case.yaml is read by sweep-case.py, which honours every field test/cases/README.md
# defines (expect, ticks, checkpoints, world.*, jar.*) and passes the `self:` block
# through.  `inherit:` points pack/ and probes.txt at another case, so the control
# case is one flag and cannot drift from the case it is the control for.
PY_BIN=../runner/.venv/bin/python
[ -x "$PY_BIN" ] || PY_BIN=python3
eval "$("$PY_BIN" sweep-case.py "$CASE_NAME")"
ASSETS=cases/$ASSETS_CASE
# Env wins over the case file, for a short smoke run without editing the case.
TICKS=${TICKS_OVERRIDE:-$TICKS}
CHECKPOINTS=${CHECKPOINTS_OVERRIDE:-$CHECKPOINTS}
SPRINT_MARGIN=${SPRINT_MARGIN:-100000}
# Step-only by default, the same setting phase 8's own verification used ("tick step
# only, zero sprints").  `tick sprint` free-runs the server between the `tick unfreeze`
# rcon call and the `tick sprint` one, and again between the sprint ending and `tick
# freeze` landing; on a loaded host that window is tens of ticks.  Measured here on the
# first full pair: A landed on gametime 6025 and B on 6000 for the same checkpoint, the
# checkpoint probes then placed their integrity-0.5 structures at different gametimes,
# and the pair diverged at gametime 6077 for a reason that was the harness, not the
# mod.  `tick step` runs exactly N frozen ticks and cannot overshoot.
export DETMC_EXTRA_OPTS="$CASE_FLAGS"
export SWEEP_SEED SWEEP_LEVEL_TYPE SWEEP_MEMORY SWEEP_VIEW SWEEP_SIM DETMC_SEED

compose() { docker compose -f compose-sweep.yml --env-file .env "$@"; }
LABELS=${LABELS:-"A B"}

echo "=== case $CASE_NAME: ticks=$TICKS checkpoints='$CHECKPOINTS' expect=$EXPECT player=$WANT_PLAYER"
echo "=== opts: $DETMC_EXTRA_OPTS"

# --- mods: the jar that is already built, never a fresh build -------------------
# Deliberately NOT `../docker-gradle.sh :fabric:build` like run-pair.sh does: the
# hunt fleet (mc-run-hunt-*) launches new containers against
# fabric/build/libs/detmc-0.1.0.jar between generations, so rebuilding that file
# under a running search would change the mod mid-hunt.
mkdir -p sweep-mods
rm -f sweep-mods/*.jar
cp ../fabric/build/libs/detmc-*.jar sweep-mods/
if [ "$WANT_PLAYER" = "true" ]; then
  [ -f ../runner/libs/fabric-carpet-26.2+v260616.jar ] || ../runner/fetch-carpet.sh
  cp ../runner/libs/fabric-carpet-26.2+v260616.jar sweep-mods/
fi
echo "=== mods: $(cd sweep-mods && md5sum *.jar | tr '\n' ' ')"

run_one() { # $1 = A|B
  local label=$1 ctr=mc-sweep-$1 svc=mc$1 data=dataSweep$1
  local out=sweep-$CASE_NAME-$label.txt
  : > "$out"
  rc() { echo ">>> $1" | tee -a "$out"; docker exec $ctr rcon-cli "$1" 2>&1 | tee -a "$out"; }
  rcq() { docker exec $ctr rcon-cli "$1" 2>&1; }
  sec() { echo "### $1" | tee -a "$out"; }

  compose rm -sf $svc >/dev/null 2>&1 || true
  rm -rf $data; mkdir -p $data/world/datapacks
  # The pack is installed before the world exists.  26.2 auto-enables a datapack that
  # is already in world/datapacks when the world is created, so no `datapack enable`
  # round trip and no reload -- and both halves get byte-identical pack files.
  cp -r "$ASSETS/$PACK_DIR" "$data/world/datapacks/$PACK_NAME"
  python3 ../runner/memgate.py --need 5000 || { echo "MEMGATE TIMEOUT"; exit 9; }
  compose up -d $svc

  local i
  for i in $(seq 1 3000); do
    docker logs $ctr 2>&1 | grep -q 'Done (' && break
    if docker logs $ctr 2>&1 | grep -q 'Minecraft server failed'; then
      echo "CONTAINER_FAILED $ctr" | tee -a "$out"; exit 3
    fi
    sleep 0.1
  done
  sec "detmc startup lines"
  # `[detmc] ` with the space, not a bare case-insensitive "detmc": the log also holds
  # tens of thousands of [detmc-io]/[detmc-dg] trace lines under -Ddetmc.traceIo.
  docker logs $ctr 2>&1 | grep -E '\[detmc\] |Loading [0-9]+ mods|Carpet' | tee -a "$out" || true
  sec "datapacks"
  rc "datapack list enabled"

  # --- world under the harness's standard frozen setup ---------------------------
  rc "tick freeze"
  rc "gamerule advance_time false"
  rc "time set midnight"
  rc "weather clear 1000000"
  rc "gamerule spawn_mobs $SWEEP_SPAWN_MOBS"   # the case decides; `false` means every
                                              # entity here was placed by the case
  rc "gamerule mob_griefing true"
  rc "gamerule random_tick_speed 3"
  rc "forceload add -32 -32 96 96"
  rc "time query gametime"

  # Chunk loading keeps running while ticks are frozen; wait for the entity list to
  # stop changing so A and B are dumped at the same point of the same sequence.
  local prev="" same=0 now
  for i in $(seq 1 900); do
    now=$(rcq "execute as @e run data get entity @s Pos" | md5sum)
    if [ "$now" = "$prev" ]; then same=$((same+1)); else same=0; fi
    [ $same -ge 20 ] && break
    prev=$now; sleep 0.25
  done
  sec "settled"
  echo "polls=$i (advisory: wall-clock, not world state)" | tee -a "$out"

  sec "setup"
  rc "function $PACK_NS:setup"
  if [ "$WANT_PLAYER" = "true" ]; then
    sec "fake player"
    rc "player Sweeper spawn at 20 -59 -30 facing 0 0"
    # Poll, do not sleep.  Measured: with a flat `sleep 1` one half reported "0 of a
    # max of 20 players online" and the other "1", purely because the join landed on
    # the other side of the sleep -- the player was in fact present in both (identical
    # Pos, 1561 recipes, byte-identical .dat).  That harness race failed the case.
    # It also matters for correctness: `recipe give @a *` with nobody online unlocks
    # nothing, and path 13 would then not be exercised at all.
    for i in $(seq 1 200); do
      case "$(rcq list)" in *"There are 0 of"*) sleep 0.25;; *) break;; esac
    done
    rc "list"
    rc "function $PACK_NS:player_setup"
  fi
  rc "function $PACK_NS:arm"
  sec "post-setup entities"
  rc "execute as @e run data get entity @s Pos"

  # --- exact tick advance, same method as run-det.sh -----------------------------
  gametime() { rcq "time query gametime" | grep -oE '[0-9]+' | tail -1; }
  sprint() {
    local before now2
    before=$(docker logs $ctr 2>&1 | grep -c 'Sprint completed' || true)
    rcq "tick unfreeze" >/dev/null 2>&1
    rcq "tick sprint $1" >/dev/null 2>&1
    for i in $(seq 1 12000); do
      now2=$(docker logs $ctr 2>&1 | grep -c 'Sprint completed' || true)
      [ "$now2" -gt "$before" ] && break
      sleep 0.1
    done
    rcq "tick freeze" >/dev/null 2>&1
  }
  goto_tick() {
    local cur need prev2 stalls tries
    cur=$(gametime); need=$(( $1 - cur ))
    if [ "$need" -gt "$SPRINT_MARGIN" ]; then sprint $(( need - SPRINT_MARGIN )); fi
    # `tick step` can end early (the stall guard below), so re-issue until the target
    # is reached.  It never overshoots -- it runs exactly N frozen ticks -- so looping
    # is safe in a way that re-sprinting is not.
    for tries in $(seq 1 40); do
      cur=$(gametime); need=$(( $1 - cur ))
      if [ "$need" -le 0 ]; then break; fi
      rcq "tick step $need" >/dev/null 2>&1
      prev2=-1; stalls=0
      for i in $(seq 1 6000); do
        cur=$(gametime)
        if [ "$cur" -ge "$1" ]; then break; fi
        if [ "$cur" = "$prev2" ]; then stalls=$((stalls+1)); else stalls=0; fi
        if [ $stalls -ge 20 ]; then break; fi
        prev2=$cur; sleep 0.5
      done
    done
    cur=$(gametime)
    if [ "$cur" -ne "$1" ]; then
      # Not a warning: the two halves are then dumped at different gametimes and every
      # probe after this point compares two different worlds.  The run is void.
      echo "### VOID: wanted gametime $1, landed on $cur" | tee -a "$out"
    fi
    return 0
  }

  local base target
  base=$(gametime)
  sec "tick base"
  echo "base=$base" | tee -a "$out"
  # probes.txt is the per-path evidence list: one `path<TAB>command` per line, run at
  # every checkpoint.  Each probe gets its own section, so the section diff names the
  # phase-8 path that moved rather than just saying "the world differs".
  for target in $CHECKPOINTS; do
    goto_tick $(( base + target ))
    sec "gametime t$target"
    rc "time query gametime"
    while IFS=$'\t' read -r path cmd; do
      case "$path" in ''|'#'*) continue;; esac
      sec "[t$target] $path"
      rc "$cmd"
    done < "$ASSETS/$PROBES"
  done

  # --- flush and collect ---------------------------------------------------------
  local saves_before
  saves_before=$(docker logs $ctr 2>&1 | grep -c 'saveEverything done' || true)
  rc "save-all flush"
  for i in $(seq 1 600); do
    now=$(( saves_before + $(docker logs --tail 400 $ctr 2>&1 | grep -c 'saveEverything done.*flush=true' || true) ))
    if [ "$now" -gt "$saves_before" ]; then break; fi
    sleep 0.5
  done
  sec "save-all flush"
  docker logs $ctr 2>&1 | grep 'saveEverything done' | tail -1 | sed 's/.*\[detmc\] //' | tee -a "$out" || true
  sleep 1
  docker logs $ctr 2>&1 | grep -F '[detmc-dg]' | sed 's/.*\[detmc-dg\] //' > dg-sweep-$CASE_NAME-$label.txt || true
  docker logs $ctr 2>&1 | grep -F '[detmc-io]' | sed 's/.*\[detmc-io\] //' > io-sweep-$CASE_NAME-$label.txt || true
  # Per-path proof that the patched code was reached at all: the sweep mixins do not
  # log, so the evidence is the world state they touched.  These are the files only a
  # walked path can create.
  # The save-only paths (12 Scoreboard.packPlayerScores, 13 ServerRecipeBook.pack)
  # are visible nowhere except in these files.  26.2 layout, checked on the server:
  # world/data/minecraft/*.dat and world/players/data/<uuid>.dat.  Java's
  # GZIPOutputStream writes MTIME=0, so two runs minutes apart still md5 the same
  # when the content is the same.
  sec "saved data files"
  (cd $data/world && md5sum data/minecraft/scoreboard.dat data/minecraft/stopwatches.dat 2>/dev/null \
     || echo "no scoreboard/stopwatches dat") | tee -a "$out"
  sec "playerdata"
  (cd $data/world && md5sum players/data/*.dat 2>/dev/null || echo "no playerdata") | tee -a "$out"
  sec "region md5 no timestamps"
  python3 region-md5.py $data/world/dimensions/minecraft/overworld | tee -a "$out" || true
  mkdir -p sweep/$CASE_NAME-$label/entities sweep/$CASE_NAME-$label/region
  cp "$out" dg-sweep-$CASE_NAME-$label.txt io-sweep-$CASE_NAME-$label.txt sweep/$CASE_NAME-$label/ 2>/dev/null || true
  # Kept so test/nbt-canon.py can answer "same NBT, different key order?" after the
  # fact; a save-order divergence is invisible in every live probe.
  cp $data/world/dimensions/minecraft/overworld/entities/*.mca sweep/$CASE_NAME-$label/entities/ 2>/dev/null || true
  cp $data/world/dimensions/minecraft/overworld/region/*.mca sweep/$CASE_NAME-$label/region/ 2>/dev/null || true
  sec "RUN_FINISHED"
  # Only now: `compose rm -sf` gives the server a 10 s stop, and one half can get
  # further through its shutdown save than the other, which moves bytes in the copies
  # that no longer reflect the run (STATUS phase 7, the r.-1.-1 finding).
  compose rm -sf $svc >/dev/null 2>&1 || true
  echo "case=$CASE_NAME label=$label ticks=$TICKS opts=$DETMC_EXTRA_OPTS jar=$(md5sum sweep-mods/*.jar | tr '\n' ' ')" \
    > sweep/$CASE_NAME-$label/params.txt
}

if [ -z "${SWEEP_COMPARE_ONLY:-}" ]; then
  for L in $LABELS; do
    echo "######## $CASE_NAME $L ########"
    run_one $L
  done
  compose down >/dev/null 2>&1 || true
else
  echo "=== SWEEP_COMPARE_ONLY: re-verdict from sweep/$CASE_NAME-{A,B}, no servers"
  LABELS="A B"
fi

if [ "$LABELS" = "A B" ]; then
  echo "######## $CASE_NAME: per-gametime digest ########"
  # Read the archived copies, not the scratch files in test/: test/dg-*.txt is
  # gitignored working space that other chain scripts also write and clean.
  python3 cross-machine/compare-dg.py \
    sweep/$CASE_NAME-A/dg-sweep-$CASE_NAME-A.txt \
    sweep/$CASE_NAME-B/dg-sweep-$CASE_NAME-B.txt \
    2>&1 | tee sweep/$CASE_NAME-digest.txt || true
  dg_rc=${PIPESTATUS[0]}
  echo "######## $CASE_NAME: canonical NBT of the saved chunks ########"
  # The claim of record for "is the saved world the same": nbt-canon.py sorts compound
  # keys, so a file that differs only in NBT map iteration order still compares equal
  # here, while any real content difference shows up.  A raw .mca md5 is a screening
  # test only (STATUS: sector placement and partial neighbour chunks move bytes
  # without moving state).
  canon_rc=0
  : > sweep/$CASE_NAME-canon.txt
  for kind in entities region; do
    for a in sweep/$CASE_NAME-A/$kind/*.mca; do
      [ -s "$a" ] || continue
      b=sweep/$CASE_NAME-B/$kind/$(basename "$a")
      [ -s "$b" ] || { echo "$kind/$(basename "$a"): only in A" >> sweep/$CASE_NAME-canon.txt; canon_rc=1; continue; }
      python3 nbt-canon.py "$a" > /tmp/sweep-canon-a.$$ 2>/dev/null || true
      python3 nbt-canon.py "$b" > /tmp/sweep-canon-b.$$ 2>/dev/null || true
      n=$(wc -l < /tmp/sweep-canon-a.$$)
      if cmp -s /tmp/sweep-canon-a.$$ /tmp/sweep-canon-b.$$; then
        echo "$kind/$(basename "$a"): CANONICALLY IDENTICAL ($n chunks)" >> sweep/$CASE_NAME-canon.txt
      else
        echo "$kind/$(basename "$a"): CANON DIFFERS ($n chunks in A)" >> sweep/$CASE_NAME-canon.txt
        canon_rc=1
      fi
      rm -f /tmp/sweep-canon-a.$$ /tmp/sweep-canon-b.$$
    done
  done
  cat sweep/$CASE_NAME-canon.txt
  echo "######## $CASE_NAME: per-path sections ########"
  python3 sweep-diff.py $CASE_NAME | tee sweep/$CASE_NAME-sections.txt
  sec_rc=${PIPESTATUS[0]}
  # One verdict, in the vocabulary of test/cases/README.md: `match` passes when both
  # comparisons are identical, `diverge` passes when at least one is not.  run-tests.sh
  # takes this exit code for an external case.
  differed=0
  [ "$dg_rc" -ne 0 ] && differed=1
  [ "$sec_rc" -ne 0 ] && differed=1
  [ "$canon_rc" -ne 0 ] && differed=1
  verdict=FAIL
  if [ "$EXPECT" = "diverge" ]; then
    [ "$differed" -eq 1 ] && verdict=PASS
  else
    [ "$differed" -eq 0 ] && verdict=PASS
  fi
  echo "SWEEP_DONE $CASE_NAME expect=$EXPECT differed=$differed $verdict"
  [ "$verdict" = PASS ] || exit 1
fi
