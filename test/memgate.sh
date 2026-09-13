# Sourced helper: the shared host memory gate for the test/ chain scripts.
#
#   detmc_gate <compose-file> [servers]
#
# Need is `servers x (heap + 1024 MB overhead)`.  The heap is read from the
# compose file's MEMORY=/-Xmx line; `.env` is NEVER opened (it holds
# RCON_PASSWORD) and any line naming a PASSWORD, SECRET or TOKEN is dropped
# before parsing.  Blocks until the host has room; see runner/memgate.py and the
# 2026-09-12 OOM it exists to prevent.
detmc_need_mb() {
  local file="$1" servers="${2:-1}" spec
  spec=$(grep -ihE 'MEMORY=|-Xmx' "$file" \
         | grep -viE 'PASSWORD|SECRET|TOKEN' \
         | grep -oiE '[0-9]+[kmg]?' | sort -u | tr '\n' ' ')
  # max over every heap line, so a per-service override cannot be missed
  python3 -c 'import sys; sys.path.insert(0, "../runner"); import memgate
print(int(sys.argv[1]) * max(memgate.job_need_mb(a) for a in (sys.argv[2:] or [""])))' \
    "$servers" $spec
}

detmc_gate() {
  python3 ../runner/memgate.py --need "$(detmc_need_mb "$1" "${2:-1}")"
  # run-det.sh / run-reseed.sh are called with the servers already up; they must
  # not wait again for memory this script is already holding.
  export DETMC_MEMGATE_HELD=1
}
