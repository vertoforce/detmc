#!/bin/bash
# Block until both halves of the cross-machine pair have written their .done file.
#   REMOTE=user@hostB REMOTE_DIR=~/detmc-xm ./wait-both.sh
REMOTE=${REMOTE:?set REMOTE to the ssh target running the B half}
REMOTE_DIR=${REMOTE_DIR:-~/detmc-xm}
cd "$(dirname "$0")"
until [ -f xm-local.done ] && timeout 30 ssh -o ConnectTimeout=10 "$REMOTE" "test -f $REMOTE_DIR/xm-remote.done" 2>/dev/null; do
  sleep 20
done
echo "BOTH DONE $(date -Is)"
echo "local exit=$(cat xm-local.done)"
timeout 30 ssh "$REMOTE" "echo remote exit=\$(cat $REMOTE_DIR/xm-remote.done)"
