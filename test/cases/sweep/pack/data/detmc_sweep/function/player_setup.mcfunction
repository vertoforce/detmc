# path 13, ServerRecipeBook.pack(): `known` and `highlight` are identity hash sets,
# walked when the player .dat is written.  A full unlock puts ~1585 holders in them.
recipe give @a *
gamemode creative @a
effect give @a minecraft:resistance infinite 4 true
# The size of `known` is the evidence that pack() had more than one entry to order.
execute store result score #rb sweep run data get entity @a[limit=1] recipeBook.recipes
say [detmc_sweep] player setup complete
