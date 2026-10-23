#!/bin/sh
# usage: heavy.sh <owner> <worktree-dir> <timeout_s> <cmd...>
# Machine-wide heavy-job lock (one heavy suite at a time), 15-min stale rule, 600 s max wait.
LOCK=C:/Users/Sam/Documents/GitHub/pyrung-wt/.heavy-lock
OWNER=$1; WT=$2; T=$3; shift 3
waited=0
while ! mkdir $LOCK 2>/dev/null; do
  start=$(sed -n 's/.*start=\([0-9]*\).*/\1/p' $LOCK/owner.txt 2>/dev/null)
  now=$(date +%s)
  if [ -n "$start" ] && [ $((now - start)) -gt 900 ]; then echo "stale lock ($start), removing"; rm -rf $LOCK; continue; fi
  if [ $waited -ge 600 ]; then echo "LOCK BUSY after 600s: $(cat $LOCK/owner.txt 2>/dev/null)"; exit 99; fi
  sleep 2; waited=$((waited + 2))
done
echo "owner=$OWNER start=$(date +%s)" > $LOCK/owner.txt
trap 'rm -rf $LOCK' EXIT INT TERM
cd $WT
export UV_NO_SYNC=1
s=$(date +%s)
timeout $T "$@"; rc=$?
echo "[heavy] rc=$rc wall=$(( $(date +%s) - s ))s"
[ $rc -eq 124 ] && echo "[heavy] TIMED OUT at ${T}s"
exit $rc
