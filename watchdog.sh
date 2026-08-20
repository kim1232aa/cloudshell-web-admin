#!/bin/bash
# watchdog.sh — Cloud Shell proxy monitor, keepalive & multi-account failover.
#
# Each cycle:
#   1. probe https://<tunnel-host>/vless (dead = 000/502/503/530; anything else,
#      e.g. xray's 400, means a live tunnel connector)
#   2. alive  -> optionally "tickle" the current account's Cloud Shell with a
#                short ssh session, ahead of the 40-min non-interactive timeout
#   3. dead   -> rotate across gcloud configurations and boot the first account
#                whose Cloud Shell actually comes up. No quota bookkeeping:
#                a shell that cannot start — quota exhausted, auth broken,
#                anything — simply fails and the next account is tried.
#
# Usage:
#   watchdog.sh <tunnel-hostname>           single check (cron)
#   watchdog.sh <tunnel-hostname> --loop    endless loop (docker)
#   watchdog.sh <tunnel-hostname> --force   failover immediately (ops/testing)
#   TUNNEL_HOST may be given as env instead of the argument.
#
# Env knobs (all optional):
#   INTERVAL=600             seconds between cycles in --loop mode
#   PROBE_PROXY=             e.g. http://172.17.0.1:7890 — route probe via local proxy
#   KEEPALIVE=1              0 disables the keepalive tickle
#   KEEPALIVE_INTERVAL=1500  seconds between tickles (must stay < 40 min)
#   WS_PATH=/vless           probe path
#   STATE_DIR=~/.cache/gcs-watchdog
#
# cron example:
#   */3 * * * * /path/to/watchdog.sh gcs.example.com >>/tmp/gcs-watchdog.log 2>&1
set -u

LOOP=0; FORCE=0; TUNNEL_HOST="${TUNNEL_HOST:-}"
for a in "$@"; do
  case "$a" in
    --loop)  LOOP=1 ;;
    --force) FORCE=1 ;;
    --*) echo "unknown flag: $a" >&2; exit 64 ;;
    *) [ -z "$TUNNEL_HOST" ] && TUNNEL_HOST="$a" ;;
  esac
done
[ -z "$TUNNEL_HOST" ] && { echo "usage: watchdog.sh <tunnel-hostname> [--loop|--force]" >&2; exit 64; }

INTERVAL="${INTERVAL:-600}"
PROBE_PROXY="${PROBE_PROXY:-}"
KEEPALIVE="${KEEPALIVE:-1}"
KEEPALIVE_INTERVAL="${KEEPALIVE_INTERVAL:-1500}"
WS_PATH="${WS_PATH:-/vless}"
STATE_DIR="${STATE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/gcs-watchdog}"
mkdir -p "$STATE_DIR"
CUR_FILE="$STATE_DIR/current-account"
LINK_FILE="$STATE_DIR/proxy-link.txt"
STATUS_FILE="$STATE_DIR/status"
LAST_TICKLE="$STATE_DIR/last-tickle"

ts()  { date -u '+%F %T'; }
log() { echo "$(ts) $*" | tee -a "$STATE_DIR/watchdog.log" >&2; }   # stderr + file for web-admin's log tail

is_dead_code() { case "$1" in 000|502|503|530) return 0 ;; *) return 1 ;; esac; }

probe() {
  # 502/503/530 are deliberate Cloudflare "origin gone" answers -> dead at once.
  # 000 (connect/TLS reset) is flaky on some networks -> retry before believing it.
  local tries="${PROBE_RETRIES:-4}" i r
  local args=(-s -m 10 -o /dev/null -w '%{http_code}'
    -H 'Connection: Upgrade' -H 'Upgrade: websocket'
    -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: x3JJHMbDL1EzLkh9GBhXDw==')
  [ -n "$PROBE_PROXY" ] && args+=(-x "$PROBE_PROXY")
  for ((i = 0; i < tries; i++)); do
    r=$(curl "${args[@]}" "https://$TUNNEL_HOST$WS_PATH" 2>/dev/null)
    r="${r:-000}"
    [ "$r" != "000" ] && { echo "$r"; return; }
    [ $((i + 1)) -lt "$tries" ] && sleep 3
  done
  echo 000
}

list_configs() { gcloud config configurations list --format='value(name)' 2>/dev/null; }

account_proxy_env() { # $1=config name; prints "https_proxy=<url> http_proxy=<url>" or empty
  local f="$STATE_DIR/proxy-$1" p
  [ -f "$f" ] || return 0
  p=$(cat "$f")
  [ -n "$p" ] && printf 'https_proxy=%s http_proxy=%s' "$p" "$p"
}

account_ok() {
  env $(account_proxy_env "$1") CLOUDSDK_ACTIVE_CONFIG_NAME="$1" \
    gcloud auth print-access-token >/dev/null 2>&1
}

rebuild_on() { # $1=config; stdout=vless link; rc: 0 ok, 1 fail (any reason)
  local out rc link
  out=$(env $(account_proxy_env "$1") CLOUDSDK_ACTIVE_CONFIG_NAME="$1" timeout 240 gcloud cloud-shell ssh \
        --ssh-flag="-o BatchMode=yes" --quiet \
        --command="bash ~/proxy-start.sh >/dev/null 2>&1; head -1 ~/proxy-link.txt 2>/dev/null" 2>&1)
  rc=$?
  if [ $rc -ne 0 ]; then
    log "$1: shell could not start (rc=$rc): $(echo "$out" | tail -1)"
    return 1
  fi
  link=$(echo "$out" | grep -oE '^vless://[^[:space:]]+' | head -1)
  [ -z "$link" ] && { log "$1: rebuild ok but no link returned"; return 1; }
  echo "$link"
}

failover() {
  local cfgs=() cur=-1 k i cfg link order=()
  mapfile -t cfgs < <(list_configs)
  [ "${#cfgs[@]}" -eq 0 ] && { log "no gcloud configurations found — run the auth helper first"; return 1; }
  # sticky order: try the current account first, then rotate through the rest
  cur=-1
  [ -f "$CUR_FILE" ] && cur=$(cat "$CUR_FILE")
  case "$cur" in ''|*[!0-9]*) cur=0 ;; esac
  [ "$cur" -ge "${#cfgs[@]}" ] && cur=0
  order+=("$cur")
  for ((k = 1; k < ${#cfgs[@]}; k++)); do order+=( $(( (cur + k) % ${#cfgs[@]} )) ); done
  for i in "${order[@]}"; do
    cfg="${cfgs[$i]}"
    account_ok "$cfg" || { log "$cfg: skip (auth invalid — run: docker compose run --rm watchdog auth $cfg)"; continue; }
    log "$cfg: triggering Cloud Shell rebuild..."
    if link=$(rebuild_on "$cfg"); then
      echo "$i" > "$CUR_FILE"
      echo "$link" > "$LINK_FILE"
      date -u +%s > "$LAST_TICKLE"
      log "$cfg: UP -> $link"
      echo "$(ts) UP account=$cfg" > "$STATUS_FILE"
      return 0
    fi
  done
  log "FAILOVER FAILED: no account's Cloud Shell could be started"
  echo "$(ts) DOWN all-accounts-failed" > "$STATUS_FILE"
  return 1
}

tickle() { # keepalive: short ssh session on the current account
  [ "$KEEPALIVE" = "1" ] || return 0
  [ -f "$CUR_FILE" ] || return 0
  local cfgs=() cfg last=0 now
  mapfile -t cfgs < <(list_configs)
  [ "${#cfgs[@]}" -eq 0 ] && return 0
  cfg="${cfgs[$(cat "$CUR_FILE")]:-}"
  [ -z "$cfg" ] && return 0
  [ -f "$LAST_TICKLE" ] && last=$(cat "$LAST_TICKLE")
  now=$(date -u +%s)
  [ $(( now - last )) -lt "$KEEPALIVE_INTERVAL" ] && return 0
  if env $(account_proxy_env "$cfg") CLOUDSDK_ACTIVE_CONFIG_NAME="$cfg" timeout 60 gcloud cloud-shell ssh \
      --ssh-flag="-o BatchMode=yes" --quiet --command=true >/dev/null 2>&1; then
    date -u +%s > "$LAST_TICKLE"
    log "keepalive tickle ok ($cfg)"
  else
    log "keepalive tickle failed ($cfg)"
  fi
}

check_once() {
  local code
  code=$(probe)
  if is_dead_code "$code"; then
    log "probe DOWN ($code) for $TUNNEL_HOST — failover starting"
    echo "$(ts) DOWN probe=$code" > "$STATUS_FILE"
    failover
  else
    log "alive ($code)"
    echo "$(ts) UP probe=$code account=$(cat "$CUR_FILE" 2>/dev/null || echo '?')" > "$STATUS_FILE"
    tickle
  fi
}

if [ "$FORCE" = "1" ]; then
  log "force failover requested"
  failover
  exit $?
fi

FORCE_FLAG="$STATE_DIR/force-failover-requested"
check_once
LAST_CHECK=$(date -u +%s)
if [ "$LOOP" = "1" ]; then
  while sleep 10; do
    if [ -f "$FORCE_FLAG" ]; then
      rm -f "$FORCE_FLAG"
      log "force-failover flag seen — triggering immediately"
      failover
      LAST_CHECK=$(date -u +%s)
      continue
    fi
    NOW=$(date -u +%s)
    if [ $(( NOW - ${LAST_CHECK:-0} )) -ge "$INTERVAL" ]; then
      check_once
      LAST_CHECK=$NOW
    fi
  done
fi
