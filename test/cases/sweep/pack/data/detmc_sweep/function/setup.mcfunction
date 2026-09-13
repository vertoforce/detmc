# detmc_sweep:setup -- build the world that walks the thirteen phase-8 paths.
# Runs once, while ticks are frozen, from sweep-pair.sh.  Flat world: the ground
# surface is y=-61 and an entity stands at y=-60.
#
# Everything is placed by command so that both halves of the pair build the same
# world from the same seed with no terrain search anywhere.

scoreboard objectives add sweep dummy
scoreboard players set #armed sweep 0
scoreboard players set #c20 sweep 0
scoreboard players set #c200 sweep 0
scoreboard players set #c400 sweep 0
scoreboard players set #c600 sweep 0
scoreboard players set #picks sweep 0
scoreboard players set #spreads sweep 0
scoreboard players set #dmg sweep 0
scoreboard players set #places sweep 0
scoreboard players set #furnaces sweep 0
scoreboard players set #crafts sweep 0
scoreboard objectives add picked dummy

# --- path 1 InsideBrownianWalk + path 10 PoiSection ---------------------------
# A closed room at midnight.  Six villagers, two beds: the four with no bed are the
# ones that run InsideBrownianWalk ("stay inside" needs night, a roof and no bed).
# Seven POI types land in ONE PoiSection (x0-15, z0-15, y-64..-49), which is what
# makes PoiSection.byType.getRecords() iterate more than one entry.
fill -2 -60 -2 8 -57 8 minecraft:stone hollow
fill -1 -60 -1 7 -58 7 minecraft:air
setblock 0 -60 0 minecraft:white_bed[facing=east,part=foot]
setblock 1 -60 0 minecraft:white_bed[facing=east,part=head]
setblock 0 -60 2 minecraft:white_bed[facing=east,part=foot]
setblock 1 -60 2 minecraft:white_bed[facing=east,part=head]
setblock 3 -60 0 minecraft:composter
setblock 3 -60 2 minecraft:barrel
setblock 3 -60 4 minecraft:smoker
setblock 5 -60 0 minecraft:cartography_table
setblock 5 -60 2 minecraft:lectern
setblock 5 -60 4 minecraft:blast_furnace
summon minecraft:villager 4.5 -60 6.5 {PersistenceRequired:1b,VillagerData:{profession:"minecraft:none",level:1,type:"minecraft:plains"}}
summon minecraft:villager 4.5 -60 6.5 {PersistenceRequired:1b,VillagerData:{profession:"minecraft:none",level:1,type:"minecraft:plains"}}
summon minecraft:villager 4.5 -60 6.5 {PersistenceRequired:1b,VillagerData:{profession:"minecraft:none",level:1,type:"minecraft:plains"}}
summon minecraft:villager 4.5 -60 6.5 {PersistenceRequired:1b,VillagerData:{profession:"minecraft:none",level:1,type:"minecraft:plains"}}
summon minecraft:villager 4.5 -60 6.5 {PersistenceRequired:1b,VillagerData:{profession:"minecraft:none",level:1,type:"minecraft:plains"}}
summon minecraft:villager 4.5 -60 6.5 {PersistenceRequired:1b,VillagerData:{profession:"minecraft:none",level:1,type:"minecraft:plains"}}

# --- path 2 EntitySelectorParser ---------------------------------------------
# Eight marker armour stands.  Every 20 ticks one is drawn with @e[sort=random] and
# nudged, so the pick lands in the entity digest and in a per-stand counter.
summon minecraft:armor_stand 20.5 -60 20.5 {Tags:["sel"],Marker:1b,Invisible:1b,NoGravity:1b}
summon minecraft:armor_stand 21.5 -60 20.5 {Tags:["sel"],Marker:1b,Invisible:1b,NoGravity:1b}
summon minecraft:armor_stand 22.5 -60 20.5 {Tags:["sel"],Marker:1b,Invisible:1b,NoGravity:1b}
summon minecraft:armor_stand 23.5 -60 20.5 {Tags:["sel"],Marker:1b,Invisible:1b,NoGravity:1b}
summon minecraft:armor_stand 24.5 -60 20.5 {Tags:["sel"],Marker:1b,Invisible:1b,NoGravity:1b}
summon minecraft:armor_stand 25.5 -60 20.5 {Tags:["sel"],Marker:1b,Invisible:1b,NoGravity:1b}
summon minecraft:armor_stand 26.5 -60 20.5 {Tags:["sel"],Marker:1b,Invisible:1b,NoGravity:1b}
summon minecraft:armor_stand 27.5 -60 20.5 {Tags:["sel"],Marker:1b,Invisible:1b,NoGravity:1b}
scoreboard players set @e[tag=sel] picked 0

# --- path 4 SpreadPlayersCommand ---------------------------------------------
# A separate group, so a divergence in /spreadplayers cannot be confused with a
# divergence in the sort=random pick.
summon minecraft:armor_stand 40.5 -60 40.5 {Tags:["spread"],Marker:1b,Invisible:1b,NoGravity:1b}
summon minecraft:armor_stand 41.5 -60 40.5 {Tags:["spread"],Marker:1b,Invisible:1b,NoGravity:1b}
summon minecraft:armor_stand 42.5 -60 40.5 {Tags:["spread"],Marker:1b,Invisible:1b,NoGravity:1b}
summon minecraft:armor_stand 43.5 -60 40.5 {Tags:["spread"],Marker:1b,Invisible:1b,NoGravity:1b}
summon minecraft:armor_stand 44.5 -60 40.5 {Tags:["spread"],Marker:1b,Invisible:1b,NoGravity:1b}
summon minecraft:armor_stand 45.5 -60 40.5 {Tags:["spread"],Marker:1b,Invisible:1b,NoGravity:1b}

# --- path 8 ItemEnchantments -------------------------------------------------
# One stationary zombie in four enchanted pieces, eight enchantments in total and
# never fewer than two on a single item, which is what ItemEnchantmentsMixin's
# `entries.size() < 2` guard needs.  200 HP so 60 hits of /damage cannot kill it.
summon minecraft:zombie 30.5 -60 -10.5 {Tags:["ench"],NoAI:1b,PersistenceRequired:1b,Silent:1b,Health:200f,attributes:[{id:"minecraft:max_health",base:200}],equipment:{head:{id:"minecraft:diamond_helmet",count:1,components:{"minecraft:enchantments":{"minecraft:protection":4,"minecraft:unbreaking":3,"minecraft:respiration":3}}},chest:{id:"minecraft:diamond_chestplate",count:1,components:{"minecraft:enchantments":{"minecraft:protection":3,"minecraft:thorns":2}}},legs:{id:"minecraft:diamond_leggings",count:1,components:{"minecraft:enchantments":{"minecraft:blast_protection":3,"minecraft:unbreaking":2}}},mainhand:{id:"minecraft:diamond_sword",count:1,components:{"minecraft:enchantments":{"minecraft:sharpness":5,"minecraft:looting":3,"minecraft:fire_aspect":2,"minecraft:mending":1}}}}}

# --- path 7 Stopwatches ------------------------------------------------------
# Stopwatches.currentTime() is Util.getMillis() in vanilla and gameTime*50 under the
# fix, so on a frozen server a fixed stopwatch reads exactly 0 and a vanilla one
# counts wall clock.  Two of them, started at different gametimes.
stopwatch create sw_setup

# --- paths 9 / 11 platforms ---------------------------------------------------
# The furnace and the crafter are rebuilt and re-fired on a cadence; see t400.
fill 50 -60 0 56 -60 2 minecraft:stone
fill 50 -59 0 56 -59 2 minecraft:air

# --- path 5 StructureBlockEntity ---------------------------------------------
# Two triggers for the same createRandom(seed) call, both with seed 0:
#   * a structure block in LOAD mode fired by redstone (StructureBlockEntity), and
#   * `/place template ... <integrity> 0` (PlaceCommand calls the same method).
# Both place at integrity 0.5, so the surviving block set IS the RNG output.
# two placement areas, one per trigger, so the two never overwrite each other
fill 60 -60 -20 74 -46 -6 minecraft:air
fill 78 -60 -20 92 -46 -6 minecraft:air
setblock 76 -60 -20 minecraft:structure_block[mode=load]{mode:"LOAD",name:"minecraft:village/plains/houses/plains_small_house_1",posX:2,posY:0,posZ:0,integrity:0.5f,seed:0L}

# --- path 12 Scoreboard ------------------------------------------------------
# Twelve (holder, objective) cells over two objectives: Scoreboard.packPlayerScores
# walks PlayerScores.scores per holder, so more than one objective per holder is
# what makes the identity order observable.
scoreboard objectives add sweep2 dummy
scoreboard players set alpha sweep 1
scoreboard players set beta sweep 2
scoreboard players set gamma sweep 3
scoreboard players set delta sweep 4
scoreboard players set alpha sweep2 10
scoreboard players set beta sweep2 20
scoreboard players set gamma sweep2 30
scoreboard players set delta sweep2 40
say [detmc_sweep] setup complete
