# detmc_sweep:tick -- runs every tick from the minecraft:tick function tag.
# Does nothing until detmc_sweep:arm sets #armed, so the frozen setup phase (which
# still ticks the server, just not the world) cannot start the cadences early.
execute unless score #armed sweep matches 1 run return 0
scoreboard players add #c20 sweep 1
scoreboard players add #c200 sweep 1
scoreboard players add #c400 sweep 1
scoreboard players add #c600 sweep 1
execute if score #c20 sweep matches 20.. run function detmc_sweep:t20
execute if score #c200 sweep matches 200.. run function detmc_sweep:t200
execute if score #c400 sweep matches 400.. run function detmc_sweep:t400
execute if score #c600 sweep matches 600.. run function detmc_sweep:t600
