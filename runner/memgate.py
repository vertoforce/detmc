#!/usr/bin/env python3
"""memgate.py -- one shared host memory budget for every detmc container launch.

Why this exists: a 73.6 GB machine with no swap took a global OOM because several
launchers started detmc work at the same time with no shared view of the host --
8 search nodes (~1.6 GB RSS each), 3 parallel Chunky renders (~1.9 GB each), a
4G-heap verification pair (~3.5 GB each) and one more run, on top of a ~37 GB
baseline of unrelated services already on the box.  The kernel killed a server
container and some system daemons.

Every launcher therefore calls `acquire(need_mb)` first.  It blocks until
MemAvailable is at least `need_mb + reserve_mb`, where the reserve is headroom
left to the host and is never spent by us.  A file lock serialises the CHECK so
two launchers cannot both pass on the same free megabytes; the lock is released
before the container starts, so a slow boot never blocks anyone else.

CLI:
    python3 runner/memgate.py --need 2500 [--reserve 12288] [--timeout 600]
    exit 0 = acquired (prints nothing), 1 = timed out.
"""
import argparse
import fcntl
import os
import re
import sys
import time

LOCK_PATH = "/tmp/detmc-memgate.lock"
# 12 GB left for the host: the unrelated baseline above spikes, and there is no
# swap to absorb it.
DEFAULT_RESERVE_MB = int(os.environ.get("DETMC_MEM_RESERVE_MB", 12288))
SETTLE_S = 3            # lock held this long after a pass, so the next checker
                        # sees a container that has at least begun to allocate
LOG_EVERY_S = 60


def mem_available_mb():
    """MemAvailable, not MemFree: free + reclaimable, which is what a new JVM can
    actually get without pushing the host into reclaim."""
    with open("/proc/meminfo") as fh:
        for line in fh:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    raise RuntimeError("/proc/meminfo has no MemAvailable")


def budget_nodes(per_node_mb, reserve_mb=DEFAULT_RESERVE_MB, cap=None):
    """How many `per_node_mb` jobs fit in (MemAvailable - reserve) right now.

    Returns at least 1 -- a caller that must run something is better served by one
    job that `acquire` then makes wait than by a zero it has no answer for."""
    if per_node_mb <= 0:
        return cap if cap else 1
    fits = max(0, mem_available_mb() - reserve_mb) // per_node_mb
    fits = max(1, int(fits))
    if cap:
        fits = min(fits, int(cap))
    return fits


def heap_mb(spec, default_mb=1024):
    """"1G" / "4096M" / "3072" (the itzg MEMORY / -Xmx form) -> MB.

    Anything unparseable falls back to `default_mb` rather than raising: a gate
    that cannot read the heap size must still gate, just conservatively."""
    m = re.match(r"\s*(\d+)\s*([kKmMgG])?", str(spec or ""))
    if not m:
        return int(default_mb)
    n, unit = int(m.group(1)), (m.group(2) or "M").upper()
    return {"K": n // 1024, "M": n, "G": n * 1024}[unit]


def job_need_mb(spec, overhead_mb=1024, default_mb=1024):
    """What one Minecraft container costs the host: heap + non-heap overhead.
    Measured (runner/README.md): container RSS is 1.46-1.61 GiB at MEMORY=1G."""
    return heap_mb(spec, default_mb) + int(overhead_mb)


def _log(msg):
    sys.stderr.write(f"[memgate] {msg}\n")
    sys.stderr.flush()


def acquire(need_mb, reserve_mb=DEFAULT_RESERVE_MB, timeout_s=None, poll_s=5):
    """Block until need_mb + reserve_mb is available; raise TimeoutError if not.

    The lock is held only around the check plus a short settle, never around the
    caller's container start."""
    need_mb = int(need_mb)
    want = need_mb + int(reserve_mb)
    t0 = time.time()
    last_log = 0.0
    while True:
        with open(LOCK_PATH, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                avail = mem_available_mb()
                if avail >= want:
                    time.sleep(SETTLE_S)
                    return avail
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        waited = time.time() - t0
        if timeout_s is not None and waited + poll_s > timeout_s:
            remaining = max(0.0, timeout_s - waited)
            if remaining:
                time.sleep(remaining)
            raise TimeoutError(
                f"waited {time.time() - t0:.0f}s for {need_mb} MB + {reserve_mb} MB "
                f"reserve; MemAvailable is {mem_available_mb()} MB")
        if waited - last_log >= LOG_EVERY_S or last_log == 0.0:
            last_log = waited
            _log(f"waiting for {need_mb} MB (+{reserve_mb} MB host reserve); "
                 f"MemAvailable {avail} MB, waited {waited:.0f}s")
        time.sleep(poll_s)


def main(argv=None):
    ap = argparse.ArgumentParser(description="host memory gate for detmc launches")
    ap.add_argument("--need", type=int, required=True, help="MB this job will use")
    ap.add_argument("--reserve", type=int, default=DEFAULT_RESERVE_MB,
                    help=f"MB left to the host (default {DEFAULT_RESERVE_MB}, "
                         f"env DETMC_MEM_RESERVE_MB)")
    ap.add_argument("--timeout", type=float, default=None, help="seconds")
    ap.add_argument("--poll", type=float, default=5.0, help="seconds between checks")
    args = ap.parse_args(argv)
    try:
        acquire(args.need, args.reserve, args.timeout, args.poll)
    except TimeoutError as exc:
        _log(f"TIMEOUT {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
