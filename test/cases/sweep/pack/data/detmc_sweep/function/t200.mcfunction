# Every 200 ticks.
scoreboard players set #c200 sweep 0
# path 4, SpreadPlayersCommand: vanilla seeds from ThreadLocalRandom, the mixin from
# the level RandomSource.  Non-player entities are legal targets.
scoreboard players add #spreads sweep 1
spreadplayers 40 40 2 12 false @e[tag=spread]
# path 8, ItemEnchantments.entrySet: /damage on the armoured zombie runs
# EnchantmentHelper.runIterationOnItem over every enchantment of every worn item.
# minecraft:magic is not in bypasses_enchantments, so the surviving health is the
# enchantment maths and nothing else.
scoreboard players add #dmg sweep 1
damage @e[tag=ench,limit=1] 20 minecraft:magic
execute as @e[tag=ench,limit=1] store result score #health sweep run data get entity @s Health 1000000
execute as @e[tag=ench,limit=1] run data modify entity @s Health set value 200f
