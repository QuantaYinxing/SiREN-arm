#!/usr/bin/env bash
# Keep the local Valhalla service running on 127.0.0.1:8081 (docs/rules.md: never
# port 8080). This build intermittently crashes in Thor (SIGSEGV in
# CostFactory::CreateModeCosting under heavy swap pressure), so the loop restarts
# it; while it is down the backend falls back to `estimate` with a warning
# (backend/siren/routing.py), which keeps dispatch working.
# No set -e: the service exits nonzero on crash and the loop must keep going.
cd "$(dirname "$0")/.."
export LD_LIBRARY_PATH="$PWD/.sandbox/lib"
LOG="${LOG:-/tmp/valhalla_keep.log}"
while :; do
  nice -n 10 .sandbox/bin/valhalla_service .sandbox/data/valhalla/valhalla.json 1 >>"$LOG" 2>&1
  echo "$(date -Is) valhalla_service exited ($?), restarting in 2s" >>"$LOG"
  sleep 2
done
