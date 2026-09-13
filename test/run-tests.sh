#!/bin/bash
# Entry point for the determinism cases.  test/cases/README.md has the case format.
#   run-tests.sh --list
#   run-tests.sh --case baseline-24k
#   run-tests.sh --all --junit results.xml
# Uses the scenario runner's venv when it exists (it has PyYAML), else python3.
set -e
cd "$(dirname "$0")"
PY=../runner/.venv/bin/python
[ -x "$PY" ] || PY=python3
exec "$PY" run-tests.py "$@"
