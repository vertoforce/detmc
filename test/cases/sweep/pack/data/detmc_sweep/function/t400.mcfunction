# Every 400 ticks.
scoreboard players set #c400 sweep 0
# path 5, StructureBlockEntity.createRandom(0): both triggers, at integrity 0.5 and
# seed 0, which is the branch that reads the clock in vanilla.
scoreboard players add #places sweep 1
fill 60 -60 -20 74 -46 -6 minecraft:air
fill 78 -60 -20 92 -46 -6 minecraft:air
place template minecraft:village/plains/houses/plains_small_house_1 60 -60 -20 none none 0.5 0
setblock 76 -59 -20 minecraft:air
setblock 76 -59 -20 minecraft:redstone_block
# #placed is the count of blocks that survived integrity 0.5 in BOTH areas:
# a clone with `masked` returns the number of non-air blocks it copied.
execute store result score #placed sweep run clone 60 -60 -20 74 -46 -6 60 -60 60 masked
execute store result score #placed2 sweep run clone 78 -60 -20 92 -46 -6 60 -60 60 masked
fill 60 -60 60 74 -46 74 minecraft:air
# path 9, AbstractFurnaceBlockEntity.getRecipesToAwardAndPopExperience: reached with
# NO player by destroying a furnace that holds more than one RecipesUsed entry
# (Level.destroyBlock does not set UPDATE_SKIP_BLOCK_ENTITY_SIDEEFFECTS).  Each entry
# draws level.getRandom().nextFloat() for its XP fraction, so the map order moves the
# level stream, and the orbs it pops are entities in the digest.
scoreboard players add #furnaces sweep 1
setblock 52 -60 0 minecraft:furnace{RecipesUsed:{"minecraft:iron_ingot_from_smelting_raw_iron":11,"minecraft:baked_potato":7,"minecraft:glass":5}} replace
setblock 52 -60 0 minecraft:air destroy
execute store result score #orbs sweep if entity @e[type=experience_orb]
# path 11, StackedContents: the crafter is the only server-side caller of
# RecipePicker/StackedContents that needs no client packet.  A rising redstone edge
# makes it pick which stacks to spend.
scoreboard players add #crafts sweep 1
# Exactly the book recipe (3 paper + 1 leather, shapeless) with the other five slots
# disabled.  A fifth item makes the grid a 5-ingredient grid and the shapeless match
# fails, which is what the first dry run measured (crafts=2, books=0).
setblock 55 -60 0 minecraft:crafter{Items:[{Slot:0b,id:"minecraft:paper",count:1},{Slot:1b,id:"minecraft:paper",count:1},{Slot:2b,id:"minecraft:paper",count:1},{Slot:3b,id:"minecraft:leather",count:1}],disabled_slots:[I;4,5,6,7,8]} replace
setblock 55 -60 1 minecraft:air replace
setblock 55 -60 1 minecraft:redstone_block replace
