#!/bin/bash
# Thin wrapper so the case name and the script name line up for run-tests.sh, which
# looks for test/<case>-pair.sh.  All the work is in sweep-pair.sh; the control case
# differs only in cases/sweep-control/case.yaml (jvm_opts: -Ddetmc.syncRandom=false).
exec "$(dirname "$0")/sweep-pair.sh" sweep-control
