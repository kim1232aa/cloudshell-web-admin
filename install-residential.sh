#!/bin/bash
# install-residential.sh — add the kui residential-exit layer on top of an
# existing cloudshell-proxy-autostart install. Run INSIDE Cloud Shell, after
# install.sh and the named-tunnel setup (cf-tunnel-creds.json + cf-hostname).
#
# What it does:
#   1. clones/updates kui-local-multi-exit and applies the patches in kui-patches/
#   2. builds the kui-local docker image
#   3. generates ~/proxy-bin/kui-password (random) and ~/proxy-bin/singbox-res.json
#      (SLOT_COUNT vless+ws inbounds 38090..38089+N -> kui socks exits 7920..7919+N)
#   4. ensures ~/proxy-bin/sub-path exists (random secret subscription path)
#   5. installs supervise.sh into ~/proxy-bin, hooks it into ~/.bashrc, starts it
#   6. re-runs proxy-start.sh so the tunnel ingress picks up the /res-NN paths
#
# Idempotent: safe to re-run. Override slot count with KUI_SLOT_COUNT=N.
set -euo pipefail

BIN="$HOME/proxy-bin"
KUI_REPO_URL="${KUI_REPO_URL:-https://github.com/kim1232aa/kui-local-multi-exit.git}"
KUI_DIR="$HOME/kui-local-multi-exit"
SLOT_COUNT="${KUI_SLOT_COUNT:-24}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ -f "$BIN/uuid" ] || { echo "error: $BIN/uuid missing — run install.sh first" >&2; exit 1; }
[ -f "$BIN/cf-hostname" ] || { echo "error: $BIN/cf-hostname missing — set up the named tunnel first" >&2; exit 1; }

# --- sing-box binary (cached in $HOME like the other binaries) ---
if [ ! -x "$BIN/sing-box" ]; then
  TAG=$(curl -fsSL https://api.github.com/repos/SagerNet/sing-box/releases/latest \
        | grep -oE '"tag_name":[ ]*"[^"]+"' | head -1 | cut -d'"' -f4)
  [ -n "$TAG" ] || { echo "error: cannot resolve latest sing-box release tag" >&2; exit 1; }
  ( cd "$BIN"
    curl -fsSL "https://github.com/SagerNet/sing-box/releases/download/${TAG}/sing-box-${TAG#v}-linux-amd64.tar.gz" -o sing-box.tar.gz
    tar -xzf sing-box.tar.gz --strip-components=1 "sing-box-${TAG#v}-linux-amd64/sing-box"
    chmod +x sing-box && rm -f sing-box.tar.gz )
fi

# --- kui source + local patches ---
if [ -d "$KUI_DIR/.git" ]; then
  if ! git -C "$KUI_DIR" pull --ff-only; then
    echo "warn: kui repo pull failed (local changes or network?) — continuing with the existing checkout" >&2
    echo "      inspect: git -C $KUI_DIR status   |   reset to upstream: git -C $KUI_DIR stash" >&2
  fi
else
  git clone "$KUI_REPO_URL" "$KUI_DIR"
fi
for p in "$SCRIPT_DIR/kui-patches/"*.patch; do
  [ -e "$p" ] || continue
  if git -C "$KUI_DIR" apply --check "$p" 2>/dev/null; then
    git -C "$KUI_DIR" apply "$p" && echo "applied $(basename "$p")"
  else
    echo "skip $(basename "$p") (already applied or not applicable)"
  fi
done

# --- docker image ---
docker info >/dev/null 2>&1 || sudo service docker start
docker build -t kui-local:latest "$KUI_DIR"

# --- secrets + sing-box fan-in config ---
[ -f "$BIN/kui-password" ] || head -c 12 /dev/urandom | md5sum | cut -c1-24 > "$BIN/kui-password"
python3 - "$BIN/singbox-res.json" "$(cat "$BIN/uuid")" "$(cat "$BIN/kui-password")" "$SLOT_COUNT" <<'PY'
import json, sys
out_path, uuid, kui_pass, n = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
inbounds = [
    {"type": "vless", "tag": f"in-res-{i:02d}", "listen": "127.0.0.1",
     "listen_port": 38089 + i, "users": [{"uuid": uuid}],
     "transport": {"type": "ws", "path": f"/res-{i:02d}"}}
    for i in range(1, n + 1)
]
outbounds = [
    {"type": "socks", "tag": f"kui-exit-{i:02d}", "server": "127.0.0.1",
     "server_port": 7919 + i, "version": "5",
     "username": "admin", "password": kui_pass}
    for i in range(1, n + 1)
]
outbounds.append({"type": "direct", "tag": "direct"})
route = {"rules": [{"inbound": [f"in-res-{i:02d}"], "outbound": f"kui-exit-{i:02d}"}
                   for i in range(1, n + 1)]}
with open(out_path, "w") as f:
    json.dump({"log": {"level": "warn"}, "inbounds": inbounds,
               "outbounds": outbounds, "route": route}, f, indent=2)
PY

# --- subscription secret path ---
[ -f "$BIN/sub-path" ] || echo "/sub-$(openssl rand -hex 16)" > "$BIN/sub-path"

# --- example entry-domain lists (copied only when absent — edit them freely;
#     missing example files are not fatal, e.g. when running from ~/proxy-bin) ---
[ -f "$BIN/res-domains.txt" ]   || cp "$SCRIPT_DIR/res-domains.txt"   "$BIN/res-domains.txt"   2>/dev/null || true
[ -f "$BIN/front-domains.txt" ] || cp "$SCRIPT_DIR/front-domains.txt" "$BIN/front-domains.txt" 2>/dev/null || true

# --- supervisor + shell hook ---
cp "$SCRIPT_DIR/supervise.sh" "$BIN/supervise.sh"
cp "$SCRIPT_DIR/subserver.py" "$BIN/subserver.py"
cp "$SCRIPT_DIR/cf-optimize-refresh.sh" "$BIN/cf-optimize-refresh.sh"
chmod +x "$BIN/supervise.sh" "$BIN/cf-optimize-refresh.sh"
# seed cf-optimized.txt now instead of waiting for supervise.sh's first refresh tick
[ -f "$BIN/cf-optimized.txt" ] || bash "$BIN/cf-optimize-refresh.sh" || true
HOOK='[ -f "$HOME/proxy-bin/supervise.sh" ] && ! pgrep -f "proxy-bin/supervise.sh" >/dev/null 2>&1 && setsid "$HOME/proxy-bin/supervise.sh" >/dev/null 2>&1 &'
grep -qF 'proxy-bin/supervise.sh' "$HOME/.bashrc" 2>/dev/null || echo "$HOOK" >> "$HOME/.bashrc"

# --- (re)build tunnel ingress with /res-NN path split, then start everything ---
bash "$HOME/proxy-start.sh" || true
pgrep -f "proxy-bin/supervise.sh" >/dev/null 2>&1 || setsid "$BIN/supervise.sh" >/dev/null 2>&1 &

# warn when an existing kui-test container was created with a different slot
# count: docker run only executes on first creation, so changing KUI_SLOT_COUNT
# needs a recreate — supervise.sh recreates it automatically after removal
if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx kui-test; then
  CUR=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' kui-test 2>/dev/null \
        | grep '^KUI_SLOT_COUNT=' | cut -d= -f2)
  if [ -n "$CUR" ] && [ "$CUR" != "$SLOT_COUNT" ]; then
    echo "warn: kui-test container runs KUI_SLOT_COUNT=$CUR but singbox-res.json was" >&2
    echo "      generated for $SLOT_COUNT — apply with: docker rm -f kui-test" >&2
  fi
fi

echo
echo "residential layer installed. subscription URL:"
echo "  https://$(cat "$BIN/cf-hostname")$(cat "$BIN/sub-path")"
echo "kui API password: $BIN/kui-password (user: admin)"
echo "optional: cp res-domains.txt / front-domains.txt into $BIN to customize entry domains"
