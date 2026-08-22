#!/bin/bash
# supervise.sh — keep the whole Cloud Shell proxy stack alive (15 s loop):
#   sing-box (residential fan-in), cloudflared named tunnel, kui exit
#   container, xray main inbound, dynamic subscription server.
# Installed to ~/proxy-bin by install-residential.sh and hooked into ~/.bashrc;
# safe to run manually. Components whose config is missing are skipped, so a
# base (non-residential) install can also use it.
CONFIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KUI_DIR="$HOME/kui-local-multi-exit"
KUI_DATA="$HOME/kui-data"
SLOT_COUNT="${KUI_SLOT_COUNT:-24}"
FIRST_SOCKS=7920
LAST_SOCKS=$((FIRST_SOCKS + SLOT_COUNT - 1))
CF_REFRESH_INTERVAL="${CF_REFRESH_INTERVAL:-172800}"  # 2 days; see cf-optimize-refresh.sh
CF_REFRESH_STAMP="$CONFIG_DIR/.cf-optimized-last-refresh"

# single instance: started from both the boot hook and ~/.bashrc
exec 9>/tmp/supervise.lock
flock -n 9 || exit 0

KUI_PASS=""
[ -f "$CONFIG_DIR/kui-password" ] && KUI_PASS=$(cat "$CONFIG_DIR/kui-password")

while true; do
  # sing-box residential fan-in (/res-NN @38090+)
  if [ -f "$CONFIG_DIR/singbox-res.json" ] \
     && ! pgrep -f "sing-box run -c $CONFIG_DIR/singbox-res.json" >/dev/null 2>&1; then
    setsid "$CONFIG_DIR/sing-box" run -c "$CONFIG_DIR/singbox-res.json" > /tmp/singbox-res.log 2>&1 &
  fi
  # cloudflared named tunnel
  if [ -f "$CONFIG_DIR/cf-config.yml" ] \
     && ! pgrep -f "cloudflared tunnel --config $CONFIG_DIR/cf-config.yml" >/dev/null 2>&1; then
    setsid "$CONFIG_DIR/cloudflared" tunnel --config "$CONFIG_DIR/cf-config.yml" --no-autoupdate --protocol http2 run > /tmp/cf-tunnel.log 2>&1 &
  fi
  # kui residential exit container (only when provisioned by install-residential.sh)
  if [ -n "$KUI_PASS" ]; then
    if ! docker info >/dev/null 2>&1; then
      sudo service docker start >/dev/null 2>&1
    fi
    if ! docker image inspect kui-local:latest >/dev/null 2>&1; then
      [ -d "$KUI_DIR" ] && docker build -t kui-local:latest "$KUI_DIR" >/dev/null 2>&1
    fi
    if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx kui-test; then
      # loopback-only publishing: kui's API and socks are only needed by the
      # local sing-box/subserver, never by the outside (Cloud Shell VMs have
      # no public IP, but keep the surface minimal anyway)
      docker start kui-test >/dev/null 2>&1 || docker run -d --name kui-test --cap-add NET_ADMIN --device /dev/net/tun \
        -e KUI_MANAGEMENT_USER=admin -e KUI_MANAGEMENT_PASSWORD="$KUI_PASS" \
        -e KUI_SLOT_COUNT="$SLOT_COUNT" -e KUI_DIAL_WORKERS=4 \
        -p "127.0.0.1:8090:8080" -p "127.0.0.1:$FIRST_SOCKS-$LAST_SOCKS:$FIRST_SOCKS-$LAST_SOCKS" \
        -v "$KUI_DATA:/opt/kui-local" \
        kui-local:latest >/dev/null 2>&1
    fi
  fi
  # xray main inbound (/vless @38080)
  if [ -f "$CONFIG_DIR/xray.json" ] \
     && ! pgrep -f "xray run -c $CONFIG_DIR/xray.json" >/dev/null 2>&1; then
    setsid "$CONFIG_DIR/xray" run -c "$CONFIG_DIR/xray.json" > /tmp/xray.log 2>&1 &
  fi
  # subscription server (@38081)
  if [ -f "$CONFIG_DIR/subserver.py" ] && [ -f "$CONFIG_DIR/sub-path" ] \
     && ! pgrep -f "subserver.py" >/dev/null 2>&1; then
    setsid python3 "$CONFIG_DIR/subserver.py" > /tmp/subserver.log 2>&1 &
  fi
  # CF-optimized front-domain list: refresh at most every CF_REFRESH_INTERVAL
  # (writes cf-optimized.txt, merged into front nodes by subserver.py)
  if [ -f "$CONFIG_DIR/cf-optimize-refresh.sh" ]; then
    now=$(date -u +%s)
    last=0
    [ -f "$CF_REFRESH_STAMP" ] && last=$(cat "$CF_REFRESH_STAMP" 2>/dev/null || echo 0)
    if [ $(( now - last )) -ge "$CF_REFRESH_INTERVAL" ]; then
      bash "$CONFIG_DIR/cf-optimize-refresh.sh" > /tmp/cf-optimize-refresh.log 2>&1
      echo "$now" > "$CF_REFRESH_STAMP"
    fi
  fi
  sleep 15
done
