# Every 20 ticks -- path 2, EntitySelectorParser.ORDER_RANDOM.
# @e[sort=random] shuffles the match list; vanilla uses Collections.shuffle with a
# JDK Random, EntitySelectorParserMixin uses the level's RandomSource.  The drawn
# stand is counted and nudged, so the choice shows up both in a score and in the
# per-tick entity digest.
scoreboard players set #c20 sweep 0
scoreboard players add #picks sweep 1
execute as @e[tag=sel,sort=random,limit=1] run scoreboard players add @s picked 1
execute as @e[tag=sel,sort=random,limit=1] at @s run tp @s ~0.01 ~ ~0.01
