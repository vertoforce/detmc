/*
 * seedscan.c -- cubiomes-backed seed prefilter for the detmc scenario runner.
 *
 * For each seed in a range:
 *   1. compute the approximate world spawn (cubiomes getSpawn)
 *   2. require the biome at spawn to be in an allowed list (optionally the
 *      whole disc of radius --spawn-radius sampled on a 16-block grid)
 *   3. for each requested structure, find the nearest viable instance within
 *      a max distance of the spawn (getStructurePos + isViableStructurePos)
 *
 * Accepted seeds are printed as one JSON object per line on stdout; a final
 * summary object is printed last. Everything else goes to stderr.
 *
 * NOTE: cubiomes has no surface-height model, so the "y" field of a structure
 * position is always 0 (vanilla /locate prints "~" for the same reason).
 */
#include "finders.h"
#include "generator.h"
#include "util.h"
#include "rng.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_REQ     16
#define MAX_BIOMES  32

typedef struct {
    char id[64];    /* resource id, e.g. "minecraft:village_plains" */
    int  stype;     /* cubiomes StructureType */
    int  variant;   /* required biome variant (villages), -1 = any */
    int  within;    /* max distance from world spawn, in blocks */
    Pos  found;     /* output: position of nearest viable instance */
} Req;

/* resource id -> (StructureType, required village variant biome) */
static const struct { const char *id; int stype; int variant; } STRUCT_IDS[] = {
    { "minecraft:village_plains",   Village,        plains       },
    { "minecraft:village_desert",   Village,        desert       },
    { "minecraft:village_savanna",  Village,        savanna      },
    { "minecraft:village_taiga",    Village,        taiga        },
    { "minecraft:village_snowy",    Village,        snowy_tundra },
    { "minecraft:village",          Village,        -1           },
    { "minecraft:pillager_outpost", Outpost,        -1           },
    { "minecraft:desert_pyramid",   Desert_Pyramid, -1           },
    { "minecraft:jungle_pyramid",   Jungle_Pyramid, -1           },
    { "minecraft:swamp_hut",        Swamp_Hut,      -1           },
    { "minecraft:igloo",            Igloo,          -1           },
    { "minecraft:shipwreck",        Shipwreck,      -1           },
    { "minecraft:monument",         Monument,       -1           },
    { "minecraft:mansion",          Mansion,        -1           },
    { "minecraft:ruined_portal",    Ruined_Portal,  -1           },
    { "minecraft:ancient_city",     Ancient_City,   -1           },
    { "minecraft:trail_ruins",      Trail_Ruins,    -1           },
    { "minecraft:trial_chambers",   Trial_Chambers, -1           },
    { "minecraft:buried_treasure",  Treasure,       -1           },
    { "minecraft:desert_well",      Desert_Well,    -1           },
    { "minecraft:mineshaft",        Mineshaft,      -1           },
};

static const char *strip_ns(const char *s)
{
    const char *c = strchr(s, ':');
    return c ? c + 1 : s;
}

static int parse_struct_id(const char *id, int *stype, int *variant)
{
    size_t i;
    for (i = 0; i < sizeof(STRUCT_IDS)/sizeof(STRUCT_IDS[0]); i++)
    {
        if (!strcmp(STRUCT_IDS[i].id, id) ||
            !strcmp(strip_ns(STRUCT_IDS[i].id), strip_ns(id)))
        {
            *stype = STRUCT_IDS[i].stype;
            *variant = STRUCT_IDS[i].variant;
            return 1;
        }
    }
    return 0;
}

/* biome resource id -> cubiomes biome id, via biome2str() */
static int parse_biome_id(int mc, const char *name)
{
    const char *want = strip_ns(name);
    int id;
    for (id = 0; id < 256; id++)
    {
        const char *p = biome2str(mc, id);
        if (p && !strcmp(p, want))
            return id;
    }
    return -1;
}

static int in_list(int id, const int *list, int n)
{
    int i;
    for (i = 0; i < n; i++)
        if (list[i] == id)
            return 1;
    return 0;
}

/* Nearest viable instance of 'r->stype' within r->within blocks of 'spawn'. */
static int find_nearest(Req *r, Generator *g, Pos spawn)
{
    StructureConfig sc;
    if (!getStructureConfig(r->stype, g->mc, &sc))
        return 0;

    int rs = sc.regionSize * 16;                 /* region size in blocks */
    int rx0 = floordiv(spawn.x - r->within, rs);
    int rx1 = floordiv(spawn.x + r->within, rs);
    int rz0 = floordiv(spawn.z - r->within, rs);
    int rz1 = floordiv(spawn.z + r->within, rs);
    long long lim = (long long)r->within * r->within;
    long long best = -1;
    int rx, rz;

    for (rz = rz0; rz <= rz1; rz++)
    {
        for (rx = rx0; rx <= rx1; rx++)
        {
            Pos p;
            if (!getStructurePos(r->stype, g->mc, g->seed, rx, rz, &p))
                continue;
            long long dx = p.x - spawn.x, dz = p.z - spawn.z;
            long long d2 = dx*dx + dz*dz;
            if (d2 > lim || (best >= 0 && d2 >= best))
                continue;
            if (!isViableStructurePos(r->stype, g, p.x, p.z,
                        r->variant < 0 ? 0 : (uint32_t)r->variant))
                continue;
            best = d2;
            r->found = p;
        }
    }
    return best >= 0;
}

static void usage(void)
{
    fprintf(stderr,
"usage: seedscan --mc <version> --start <seed> --count <n> [--accept <k>]\n"
"                --spawn-biome <id[,id...]> [--spawn-radius <blocks>]\n"
"                [--struct <id>:<within> ...]\n"
"\n"
"  --mc            MC version string (e.g. \"1.21\"); unknown versions fall\n"
"                  back to the newest version cubiomes supports\n"
"  --start         first world seed to test (inclusive)\n"
"  --count         number of consecutive seeds to test\n"
"  --accept        stop after this many accepted seeds (0 = no limit)\n"
"  --spawn-biome   allowed biomes at the world spawn\n"
"  --spawn-radius  also require every point on a 16-block grid within this\n"
"                  radius of spawn to be an allowed biome (default 0)\n"
"  --struct        structure requirement, repeatable\n");
}

int main(int argc, char **argv)
{
    const char *mcstr = NULL;
    long long start = 0, count = 0;
    int accept = 0, spawn_radius = 0;
    int biomes[MAX_BIOMES], nbiomes = 0;
    char biomenames[MAX_BIOMES][64];
    Req reqs[MAX_REQ];
    int nreqs = 0;
    int i;

    char *biomearg = NULL;
    for (i = 1; i < argc; i++)
    {
        const char *a = argv[i];
        const char *v = (i + 1 < argc) ? argv[i+1] : NULL;
        if      (!strcmp(a, "--mc") && v)            { mcstr = v; i++; }
        else if (!strcmp(a, "--start") && v)         { start = atoll(v); i++; }
        else if (!strcmp(a, "--count") && v)         { count = atoll(v); i++; }
        else if (!strcmp(a, "--accept") && v)        { accept = atoi(v); i++; }
        else if (!strcmp(a, "--spawn-radius") && v)  { spawn_radius = atoi(v); i++; }
        else if (!strcmp(a, "--spawn-biome") && v)   { biomearg = (char*)v; i++; }
        else if (!strcmp(a, "--struct") && v)
        {
            if (nreqs >= MAX_REQ) { fprintf(stderr, "too many --struct\n"); return 2; }
            char buf[128];
            snprintf(buf, sizeof(buf), "%s", v);
            char *colon = strrchr(buf, ':');
            if (!colon) { fprintf(stderr, "bad --struct '%s' (want id:within)\n", v); return 2; }
            *colon = 0;
            snprintf(reqs[nreqs].id, sizeof(reqs[nreqs].id), "%s", buf);
            reqs[nreqs].within = atoi(colon + 1);
            nreqs++;
            i++;
        }
        else { usage(); return 2; }
    }

    if (!mcstr || count <= 0) { usage(); return 2; }

    /* Resolve the MC version. cubiomes only knows the versions in its
     * MCVersion enum; anything newer falls back to MC_NEWEST. */
    int mc = str2mc(mcstr);
    int mismatch = 0;
    if (mc <= 0)
    {
        mc = MC_NEWEST;
        mismatch = 1;
        fprintf(stderr, "seedscan: cubiomes does not know MC '%s'; "
                "using its newest supported version '%s'\n", mcstr, mc2str(mc));
    }
    else if (mc < MC_NEWEST)
    {
        mismatch = 1;
    }

    if (biomearg)
    {
        char buf[512];
        snprintf(buf, sizeof(buf), "%s", biomearg);
        char *tok = strtok(buf, ",");
        while (tok)
        {
            while (*tok == ' ') tok++;
            if (nbiomes >= MAX_BIOMES) { fprintf(stderr, "too many biomes\n"); return 2; }
            int id = parse_biome_id(mc, tok);
            if (id < 0) { fprintf(stderr, "unknown biome '%s'\n", tok); return 2; }
            biomes[nbiomes] = id;
            snprintf(biomenames[nbiomes], sizeof(biomenames[0]), "%s", tok);
            nbiomes++;
            tok = strtok(NULL, ",");
        }
    }

    for (i = 0; i < nreqs; i++)
    {
        if (!parse_struct_id(reqs[i].id, &reqs[i].stype, &reqs[i].variant))
        {
            fprintf(stderr, "unknown structure id '%s'\n", reqs[i].id);
            return 2;
        }
    }

    Generator g;
    setupGenerator(&g, mc, 0);

    long long checked = 0, naccept = 0;
    long long seed;
    for (seed = start; checked < count; seed++, checked++)
    {
        applySeed(&g, DIM_OVERWORLD, (uint64_t)seed);

        Pos sp = getSpawn(&g);
        int sid = getBiomeAt(&g, 1, sp.x, 63, sp.z);
        if (nbiomes && !in_list(sid, biomes, nbiomes))
            continue;

        if (spawn_radius > 0)
        {
            int bad = 0, dx, dz;
            for (dz = -spawn_radius; dz <= spawn_radius && !bad; dz += 16)
            {
                for (dx = -spawn_radius; dx <= spawn_radius; dx += 16)
                {
                    if (dx*dx + dz*dz > spawn_radius*spawn_radius)
                        continue;
                    int id = getBiomeAt(&g, 1, sp.x + dx, 63, sp.z + dz);
                    if (nbiomes && !in_list(id, biomes, nbiomes)) { bad = 1; break; }
                }
            }
            if (bad)
                continue;
        }

        int ok = 1;
        for (i = 0; i < nreqs && ok; i++)
            ok = find_nearest(&reqs[i], &g, sp);
        if (!ok)
            continue;

        printf("{\"world_seed\":%lld,\"spawn\":[%d,%d],\"spawn_biome\":\"minecraft:%s\","
               "\"structures\":{", seed, sp.x, sp.z, biome2str(mc, sid));
        for (i = 0; i < nreqs; i++)
            printf("%s\"%s\":[%d,0,%d]", i ? "," : "", reqs[i].id,
                    reqs[i].found.x, reqs[i].found.z);
        printf("}}\n");
        fflush(stdout);

        naccept++;
        if (accept > 0 && naccept >= accept)
        {
            checked++;
            break;
        }
    }

    printf("{\"_summary\":true,\"checked\":%lld,\"accepted\":%lld,"
           "\"mc_requested\":\"%s\",\"mc_version\":\"%s\",\"version_mismatch\":%s}\n",
           checked, naccept, mcstr, mc2str(mc), mismatch ? "true" : "false");
    return 0;
}
