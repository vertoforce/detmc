# Every 600 ticks -- path 12, Scoreboard.packPlayerScores.
# Save-order only, and the scoreboard has to be mutated between saves or it is not
# dirty and is not rewritten.
scoreboard players set #c600 sweep 0
scoreboard players add alpha sweep2 1
scoreboard players add beta sweep2 2
scoreboard players add gamma sweep2 3
scoreboard players add delta sweep2 4
# path 7, Stopwatches.currentTime(): a second stopwatch created mid-run, so the pair
# has one started at gametime 0 and one started later.
stopwatch create sw_mid
