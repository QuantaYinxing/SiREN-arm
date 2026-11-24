#!/usr/bin/env bash
# SiREN dev launcher (docs/rules.md: semua instalasi & data tetap di .sandbox/).
#   ./setup.sh gamemode          server game mode di localhost + buka browser (dev test)
#   ./setup.sh realmode          server real mode + Cloudflare tunnel publik + buka browser
#                                  (HP teman: <url>/field/)
#   ./setup.sh database          server real mode + buka GUI /db/ (manage akun & anggota;
#                                  DB tiap perangkat terpisah — .sandbox/data/siren.sqlite)
# Opsi: ./setup.sh realmode --no-browser   (tanpa membuka browser)
#       ./setup.sh realmode --fresh        (arsip siren.sqlite ke .sandbox/data/archive/,
#                                  lalu reset operasi surgical — hari baru, regu sama)
# Ctrl+C menghentikan server, tunnel, dan valhalla.
set -euo pipefail
cd "$(dirname "$0")"
MODE="${1:?usage: ./setup.sh gamemode|realmode|database [--no-browser]}"
case "$MODE" in
  gamemode)  SRV="game" ;;
  realmode)  SRV="real" ;;
  database)  SRV="real" ;;
  *) echo "mode tidak dikenal: $MODE (gamemode|realmode|database)" >&2; exit 1 ;;
esac
PORT="${PORT:-8094}"
PY=".sandbox/py/bin/python"
OPEN=1
FRESH=0
for arg in "$@"; do
  case "$arg" in
    --no-browser) OPEN=0 ;;
    --fresh) FRESH=1 ;;
  esac
done

if ss -tlnp 2>/dev/null | grep -qE ":$PORT\s"; then
  echo "port $PORT sudah dipakai — hentikan proses lain dulu" >&2
  exit 1
fi

# --fresh: start the day clean without losing the brigade. gamemode already wipes
# its scratch DB on start, so the flag only matters for real/database modes:
# copy the device DB to the archive, then the surgical store.reset_operations —
# incidents/assignments/reporter links/event log gone, crew and vehicles Standby,
# accounts, vehicles, water sources and restrictions survive (docs/siren.md).
if [ "$FRESH" = "1" ] && [ "$SRV" = "real" ] && [ -f ".sandbox/data/siren.sqlite" ]; then
  mkdir -p .sandbox/data/archive
  STAMP="$(date +%Y%m%d-%H%M%S)"
  cp ".sandbox/data/siren.sqlite" ".sandbox/data/archive/siren-$STAMP.sqlite"
  echo "→ DB diarsip ke .sandbox/data/archive/siren-$STAMP.sqlite"
  (cd backend && "../$PY" -c "
from siren.config import Config
from siren.store import Store
import json
cfg = Config.load('../contracts/config.yaml')
st = Store('../.sandbox/data/siren.sqlite', cfg)
print('→ operasi direset:', json.dumps(st.reset_operations()))"
  ) || { echo "reset gagal — DB tidak berubah" >&2; exit 1; }
fi

cleanup() {
  pkill -f "uvicorn siren.main:app --host 0.0.0.0 --port $PORT" 2>/dev/null || true
  for pid in "$SERVER_PID" "$TUNNEL_PID" "$VH_PID"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM
SERVER_PID="" TUNNEL_PID="" VH_PID=""

# routing engine lokal; routing.py tetap fallback ke estimate kalau crash
if [ "$MODE" != "database" ]; then
  if ss -tlnp 2>/dev/null | grep -qE ":8081\s"; then
    echo "→ valhalla :8081 sudah jalan — pakai yang ada"
  else
    # .sandbox/bin/valhalla_service.pb321 is the author's x86 build against protobuf
    # 3.21.12 (the version Valhalla 3.9.3 pins upstream); the plain binary crashes on
    # repeat requests under protobuf 36 (docs/blackbox.md 2026-11-23). On arm64
    # (scripts/valhalla_compile.sh) Ubuntu ships protobuf 3.21.12 itself, so the plain
    # binary is stable and the fallback order below covers both machines.
    VH=".sandbox/bin/valhalla_service.pb321"
    [ -x "$VH" ] || VH=".sandbox/bin/valhalla_service"
    LD_LIBRARY_PATH="$PWD/.sandbox/lib" nice -n 5 "$VH" \
      .sandbox/data/valhalla/valhalla.json 1 >/tmp/siren-valhalla.log 2>&1 &
    VH_PID=$!
  fi
fi

echo "→ server :$PORT ($SRV)"
(cd backend && SIREN_MODE="$SRV" nice -n 5 ../"$PY" -m uvicorn siren.main:app \
  --host 0.0.0.0 --port "$PORT" >/tmp/siren-server.log 2>&1) &
SERVER_PID=$!
ready=0
for i in $(seq 1 30); do
  if curl -s -o /dev/null "http://localhost:$PORT/docs"; then ready=1; break; fi
  sleep 1
done
if [ "$ready" = 0 ]; then echo "server tidak siap:"; tail -5 /tmp/siren-server.log >&2; exit 1; fi

URL="http://localhost:$PORT"
if [ "$MODE" = "realmode" ]; then
  CF=".sandbox/bin/cloudflared"
  if [ ! -x "$CF" ]; then
    echo "→ download cloudflared ke .sandbox/bin ..."
    case "$(uname -m)" in
      x86_64) CFARCH="linux-amd64" ;;
      aarch64) CFARCH="linux-arm64" ;;
      *) echo "arch $(uname -m) tidak punya build cloudflared" >&2; exit 1 ;;
    esac
    curl -L --fail -o "$CF.part" \
      "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-$CFARCH"
    mv "$CF.part" "$CF"
    chmod +x "$CF"
  fi
  "$CF" tunnel --url "$URL" >/tmp/siren-tunnel.log 2>&1 &
  TUNNEL_PID=$!
  echo "→ menunggu URL cloudflare ..."
  PUB=""
  for i in $(seq 1 60); do
    PUB=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" /tmp/siren-tunnel.log 2>/dev/null | head -1 || true)
    [ -n "$PUB" ] && break
    sleep 1
  done
  if [ -z "$PUB" ]; then echo "tunnel tidak muncul:"; tail -10 /tmp/siren-tunnel.log >&2; exit 1; fi
  echo "URL publik : $PUB"
  echo "HP teman   : $PUB/field/  (login akun petugas, password dev)"
  URL="$PUB"
fi

if [ "$MODE" = "database" ]; then
  echo "Database : $URL/db/  (login akun operator)"
  if [ "$OPEN" = 1 ]; then
    command -v xdg-open >/dev/null 2>&1 && xdg-open "$URL/db/" >/dev/null 2>&1 || echo "(browser tidak otomatis terbuka — buka URL di atas)"
  fi
else
  echo "Operator   : $URL/ops/"
  echo "Petugas    : $URL/field/"
  if [ "$OPEN" = 1 ]; then
    command -v xdg-open >/dev/null 2>&1 && xdg-open "$URL/ops/" >/dev/null 2>&1 || echo "(browser tidak otomatis terbuka — buka URL di atas)"
  fi
fi
echo "Ctrl+C untuk berhenti."
wait
