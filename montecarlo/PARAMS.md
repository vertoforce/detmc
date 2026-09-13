# Parameters, derived from decompiled Mojang-named 26.2 source

Source root: a local decompile of the 26.2 server jar (version read from `version.json`).
Every line below is `class:line` in that tree. "GUESS" = not derivable from source.

## Enderman goal list
| Prio | Goal | cite |
|---|---|---|
| 0 | FloatGoal | EnderMan.java:94 |
| 1 | EndermanFreezeWhenLookedAt | EnderMan.java:95 |
| 2 | MeleeAttackGoal(1.0) | EnderMan.java:96 |
| 7 | WaterAvoidingRandomStrollGoal(this, 1.0, 0.0F) | EnderMan.java:97 |
| 8 | LookAtPlayerGoal / RandomLookAroundGoal | EnderMan.java:98-99 |
| 10 | EndermanLeaveBlockGoal | EnderMan.java:100 |
| 11 | EndermanTakeBlockGoal | EnderMan.java:101 |

Neither block goal calls `setFlags`, so both have an empty flag set (Goal.java:19 `flags`) and run
concurrently with the MOVE-flagged stroll goal (GoalSelector.java:61). With no player and no
endermite the priority-0..8 goals never fire, so only stroll + take + leave matter.

## Tick cadence (this halves every advertised goal rate)
- `Mob.serverAiStep` Mob.java:716-737: `goalSelector.tick()` (the call that evaluates `canUse`
  and calls `start`) runs only when `(tickCount + getId()) % 2 == 0`. On the other tick only
  `tickRunningGoals(false)` runs, which ticks nothing that does not override
  `requiresUpdateEveryTick` (GoalSelector.java:104, Goal.java:31 default `false`).
- `Goal.reducedTickDelay(n) = Mth.positiveCeilDiv(n, 2)` Goal.java:56-58.

So per **goal-selector tick** (= every 2 game ticks):
- take `canUse`: `nextInt(reducedTickDelay(20)) == 0` -> **1/10** (EnderMan.java:590)
- leave `canUse`: `nextInt(reducedTickDelay(2000)) == 0` -> **1/1000** (EnderMan.java:446)
- stroll `canUse`: `nextInt(reducedTickDelay(120)) == 0` -> **1/60** (RandomStrollGoal.java:47,
  DEFAULT_INTERVAL 120 RandomStrollGoal.java:10,21)

Per game tick that is 1/20, 1/2000, 1/120.

## EndermanTakeBlockGoal.tick (EnderMan.java:594-612)
```
xt = floor(getX() - 2.0 + rnd*4.0)      # 4 wide
yt = floor(getY() + rnd*3.0)            # 3 tall, STARTS AT THE FEET
zt = floor(getZ() - 2.0 + rnd*4.0)      # 4 deep
clip from (blockX+0.5, yt+0.5, blockZ+0.5) to (xt+0.5, yt+0.5, zt+0.5); needs hit == pos
requires blockState.is(BlockTags.ENDERMAN_HOLDABLE)
```
`getY()` of a standing mob is the top surface of the block it stands on, so `yt >= feetY` always
and the block underfoot is at `feetY - 1`. **An enderman can never take a block at or below its
own feet level.** `data/minecraft/tags/block/enderman_holdable.json` has 23 entries including
`#minecraft:dirt` and `#minecraft:grass_blocks`, so the whole slab is holdable.

## EndermanLeaveBlockGoal.tick / canPlaceBlock (EnderMan.java:450-481)
```
xt = floor(getX() - 1.0 + rnd*2.0); yt = floor(getY() + rnd*2.0); zt = floor(getZ() - 1.0 + rnd*2.0)
place iff target.isAir && !below.isAir && !below.is(BEDROCK)
     && below.isCollisionShapeFullBlock && carried.canSurvive
     && level.getEntities(this.enderman, unitCube(pos)).isEmpty()
```
`getEntities(this.enderman, ...)` **excludes the enderman itself**, so it may place a block in its
own feet cell (probability 1/2 for `yt == feetY`, 1/2 for the own column in x and z => 1/8 of
placements are self-pillaring). STEP_HEIGHT 1.0 (EnderMan.java:119) lets it walk back up onto
anything it built.

## Stroll target
`WaterAvoidingRandomStrollGoal.getPosition` -> `LandRandomPos.getPos(mob, 10, 7)`
(WaterAvoidingRandomStrollGoal.java:27; the 0.001 fallback to DefaultRandomPos is
WaterAvoidingRandomStrollGoal.java:9,27). `RandomPos.generateRandomPos` takes 10 candidates and
keeps the one with the highest `getWalkTargetValue` **using a strict `>`** (RandomPos.java:99-115).
`EnderMan.getWalkTargetValue` returns a constant `0.0F` (EnderMan.java:109-111), so ties never
replace: the target is simply the **first valid** of 10 uniform offsets in
`[-10,10] x [-7,7] x [-10,10]` (RandomPos.java:21-26). Speed modifier 1.0 (EnderMan.java:97),
MOVEMENT_SPEED attribute 0.3 (EnderMan.java:116).
**GUESS**: ground speed 0.3 blocks/tick while pathing (the attribute is not blocks/tick; real
walk speed is ~0.93x of it after friction, so the model over-travels by ~7%).

## Persistence / stroll availability
`RandomStrollGoal.canUse` refuses while `getNoActionTime() >= 100` (RandomStrollGoal.java:43).
`Mob.serverAiStep` increments `noActionTime` every tick (Mob.java:717) and `Mob.checkDespawn`
only resets it for a persistent mob (Mob.java:710-712) or a mob within 32 blocks of a player
(Mob.java:706-707). The scenario says the enderman is **persistent**, so `noActionTime == 0`
every tick and the stroll goal is always available.

## Daylight / water teleports
- `EnderMan.customServerAiStep` EnderMan.java:244-254: if `level.isBrightOutside()` and
  `tickCount >= targetChangeTime + 600` and brightness `br > 0.5` and `canSeeSky` and
  `random.nextFloat()*30.0 < (br-0.4)*2.0` then `teleport()`. At full daylight br = 1.0 so
  p = 1.2/30 = **0.04 per tick** -> a relocation every ~25 ticks all day.
  `Level.isBrightOutside` = `skyDarken < 4` (Level.java:385-387).
  **GUESS**: day = the first 12000 of each 24000-tick day with br = 1.0 (ignores the twilight ramp).
- `EnderMan.teleport()` EnderMan.java:256-265: `x +- 32`, `y + nextInt(64) - 32`, `z +- 32`,
  then `teleport(x,y,z)` scans down to the first `blocksMotion` block (EnderMan.java:277-283) and
  `LivingEntity.randomTeleport` (LivingEntity.java:3666-3694). 64-block box, treated here as a
  uniform relocation on the 64x64 slab.
- Rain: `LivingEntity.java:3165-3167` -> `isSensitiveToWater()` (EnderMan.java:239, true) and
  `isInWaterOrRain()` -> `hurtServer(drown, 1.0F)` **every tick**; `EnderMan.hurtServer`
  teleports on 9/10 of non-living-source hits (EnderMan.java:358-362). On an unroofed slab this
  also kills the enderman (40 HP, ~1 damage per invulnerability window). Modelled as
  "uniform relocation, no death" -> the OPEN numbers are an **over**-estimate.
  **GUESS**: rain 10% of ticks.

## There is no idle teleport
26.2 `EnderMan` has no `aiStep` override and no `1/N` idle teleport; `grep teleport EnderMan.java`
gives only lines 249 (daylight), 261/274/289 (helpers), 361/369 (on hurt) and 560/566 (aggro).
The pre-1.9 random idle teleport does not exist in this version.

## Consequences that the model does NOT have to simulate
1. **On perfectly flat ground the take goal can never fire** (yt >= feetY, and on flat ground every
   surface block is at feetY - 1). So an enderman on a flat slab never picks anything up, never
   places anything, and no pattern of any kind can ever form. Confirmed by the `flat` run.
2. **Pit depth >= 2 is impossible** for the same reason: digging below the base requires standing
   at base - 1, which requires a pit that cannot be dug. Even depth 1 is impossible on a flat base.
   Asserted in code (`assert min(h) >= 1`) and reported as exactly 0.
3. Total loose material is **conserved**: the enderman converts a 1-block step into a placed block
   somewhere else and then recycles it. It creates no new blocks. Pattern rates are therefore set
   by the amount of 1-block relief on the slab, which the prompt's "flat" slab has none of. The
   runs seed `L` loose blocks to make anything happen at all; `L` is a **GUESS**.

## Other model parameters
- Slab 64x64, base height 1 (block at y=0), all grass/dirt (holdable).
- In-game year = 365 x 24000 = 8,760,000 ticks.
- Villager: random walk, 1 move per 40 ticks, cannot climb more than 1 block. **GUESS** (the prompt's).
- Mixing shortcut: while carrying, if the fast-forward is longer than `MIX_TICKS` = 600 the position
  is redrawn uniformly instead of path-simulated. Validated against a `MIX_TICKS = inf` run.
