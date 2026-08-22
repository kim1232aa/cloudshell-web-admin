#!/bin/bash
# install.sh — one-shot installer, run inside Google Cloud Shell:
#   bash <(curl -sSL https://raw.githubusercontent.com/kim1232aa/cloudshell-proxy-autostart/main/install.sh)
#
# Optional HA mode: before re-running proxy-start.sh, drop your named-tunnel
# credentials into place:
#   echo "<tunnel-token>" > ~/proxy-bin/cf-tunnel-token
#   echo "gcs.example.com" > ~/proxy-bin/cf-hostname
set -eu

echo "[*] Preparing ~/proxy-bin (persistent across recycling)..."
mkdir -p ~/proxy-bin
cd ~/proxy-bin

if [ ! -x xray ]; then
  echo "[*] Downloading xray..."
  wget -q https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip -O xray.zip
  unzip -qo xray.zip xray && chmod +x xray && rm -f xray.zip
fi

if [ ! -x cloudflared ]; then
  echo "[*] Downloading cloudflared..."
  wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O cloudflared
  chmod +x cloudflared
fi

echo "[*] Installing proxy-start.sh ..."
cat > ~/proxy-start.sh <<'PROXY_EOF'
#!/bin/bash
# proxy-start.sh — rebuild the proxy stack on a fresh Cloud Shell instance.
#
# Two modes:
#   named-tunnel (HA):   if ~/proxy-bin/cf-tunnel-token exists, join the fixed
#                        named tunnel (multi-instance replicas, stable hostname)
#   quick-tunnel (solo): otherwise, open an ephemeral *.trycloudflare.com tunnel
#
# Idempotent: safe to re-run; already-running components are left untouched.
# Invoked automatically at boot by ~/.customize_environment.
set -u

# single-instance guard: boot may trigger this script concurrently
exec 9>/tmp/proxy-start.lock
flock -n 9 || exit 0

HOME_DIR="$HOME"
BIN="$HOME_DIR/proxy-bin"
LOG="$HOME_DIR/proxy-runtime.log"
UUID_FILE="$BIN/uuid"
LINK="$HOME_DIR/proxy-link.txt"
TOKEN_FILE="$BIN/cf-tunnel-token"
HOST_FILE="$BIN/cf-hostname"
CREDS_FILE="$BIN/cf-tunnel-creds.json"
CF_CONFIG="$BIN/cf-config.yml"
VLESS_PORT=38080
SUB_PORT=38081
WS_PATH="/vless"

mkdir -p "$BIN"

# Optional subscription server: dynamic Clash YAML served at the exact secret
# path in ~/proxy-bin/sub-path, 404 for everything else (started even on the
# fast path so it can be added to an already-running instance)
if [ -f "$BIN/subserver.py" ] && [ -f "$BIN/sub-path" ] \
   && ! pgrep -f "subserver.py" >/dev/null 2>&1; then
  nohup python3 "$BIN/subserver.py" >>"$LOG" 2>&1 &
fi

# Residential layer (optional): start its supervisor if installed. This closes
# the recycle gap — the boot hook only runs this script, and supervise.sh was
# previously hooked to ~/.bashrc (interactive logins only), so a recycled VM
# never brought sing-box/kui back by itself. supervise.sh converges in the
# background (15 s loop, flock-guarded), missing components start within one
# tick of their config appearing.
if [ -f "$BIN/supervise.sh" ] && ! pgrep -f "proxy-bin/supervise.sh" >/dev/null 2>&1; then
  setsid "$BIN/supervise.sh" >/dev/null 2>&1 &
fi

# Fast path: proxy already running with a valid link — reprint and exit.
# (never clobber a good link file just because the URL isn't in the log anymore)
if pgrep -x xray >/dev/null 2>&1 && pgrep -f "cloudflared tunnel" >/dev/null 2>&1 \
   && [ -f "$LINK" ] && grep -q '^vless://' "$LINK" 2>/dev/null; then
  grep '^vless://' "$LINK" | head -1
  echo "PROXY_READY (already running)"
  exit 0
fi

# Wait for outbound network (early boot may have no connectivity yet)
for _ in $(seq 1 30); do
  curl -s -m 3 -o /dev/null https://github.com && break
  sleep 2
done

# Binaries are cached in $HOME (persistent across recycling); download only if missing
if [ ! -x "$BIN/xray" ]; then
  (cd "$BIN" && wget -q https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip -O xray.zip && unzip -qo xray.zip xray && chmod +x xray && rm -f xray.zip)
fi
if [ ! -x "$BIN/cloudflared" ]; then
  (cd "$BIN" && wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O cloudflared && chmod +x cloudflared)
fi

# Stable per-install UUID: generated once, persisted in $HOME.
# For multi-account HA, copy the same uuid file to every account's Cloud Shell.
if [ -f "$UUID_FILE" ]; then
  UUID=$(cat "$UUID_FILE")
else
  UUID=$(cat /proc/sys/kernel/random/uuid)
  echo "$UUID" > "$UUID_FILE"
fi

cat > "$BIN/xray.json" <<EOF
{"inbounds":[{"listen":"127.0.0.1","port":$VLESS_PORT,"protocol":"vless","settings":{"clients":[{"id":"$UUID"}],"decryption":"none"},"streamSettings":{"network":"ws","wsSettings":{"path":"$WS_PATH"}}}],"outbounds":[{"protocol":"freedom"}]}
EOF

# Start xray (idempotent)
pgrep -x xray >/dev/null 2>&1 || nohup "$BIN/xray" run -c "$BIN/xray.json" >>"$LOG" 2>&1 &

# Start cloudflared (idempotent; rotate log so we never read a stale quick-tunnel URL)
# named-tunnel priority: credentials-file mode (cf-setup.sh) > token mode (dashboard)
if ! pgrep -f "cloudflared tunnel" >/dev/null 2>&1; then
  [ -f "$LOG" ] && mv "$LOG" "$LOG.old"
  if [ -s "$CREDS_FILE" ]; then
    TID=$(grep -oE '"TunnelID"[ ]*:[ ]*"[^"]+"' "$CREDS_FILE" | cut -d'"' -f4)
    if [ -f "$BIN/subserver.py" ] && [ -f "$BIN/sub-path" ]; then
      # path-split: /vless -> xray, everything else -> subscription server
      cat > "$CF_CONFIG" <<EOF2
tunnel: $TID
credentials-file: $CREDS_FILE
ingress:
  - path: ^${WS_PATH}\$
    service: http://127.0.0.1:$VLESS_PORT
  - path: ^/res-01$
    service: http://127.0.0.1:38090
  - path: ^/res-02$
    service: http://127.0.0.1:38091
  - path: ^/res-03$
    service: http://127.0.0.1:38092
  - path: ^/res-04$
    service: http://127.0.0.1:38093
  - path: ^/res-05$
    service: http://127.0.0.1:38094
  - path: ^/res-06$
    service: http://127.0.0.1:38095
  - path: ^/res-07$
    service: http://127.0.0.1:38096
  - path: ^/res-08$
    service: http://127.0.0.1:38097
  - path: ^/res-09$
    service: http://127.0.0.1:38098
  - path: ^/res-10$
    service: http://127.0.0.1:38099
  - path: ^/res-11$
    service: http://127.0.0.1:38100
  - path: ^/res-12$
    service: http://127.0.0.1:38101
  - path: ^/res-13$
    service: http://127.0.0.1:38102
  - path: ^/res-14$
    service: http://127.0.0.1:38103
  - path: ^/res-15$
    service: http://127.0.0.1:38104
  - path: ^/res-16$
    service: http://127.0.0.1:38105
  - path: ^/res-17$
    service: http://127.0.0.1:38106
  - path: ^/res-18$
    service: http://127.0.0.1:38107
  - path: ^/res-19$
    service: http://127.0.0.1:38108
  - path: ^/res-20$
    service: http://127.0.0.1:38109
  - path: ^/res-21$
    service: http://127.0.0.1:38110
  - path: ^/res-22$
    service: http://127.0.0.1:38111
  - path: ^/res-23$
    service: http://127.0.0.1:38112
  - path: ^/res-24$
    service: http://127.0.0.1:38113
  - service: http://127.0.0.1:$SUB_PORT
EOF2
    else
      cat > "$CF_CONFIG" <<EOF2
tunnel: $TID
credentials-file: $CREDS_FILE
ingress:
  - service: http://127.0.0.1:$VLESS_PORT
EOF2
    fi
    nohup "$BIN/cloudflared" tunnel --config "$CF_CONFIG" --no-autoupdate --protocol http2 run >>"$LOG" 2>&1 &
  elif [ -s "$TOKEN_FILE" ]; then
    nohup "$BIN/cloudflared" tunnel --no-autoupdate --protocol http2 --token "$(cat "$TOKEN_FILE")" run >>"$LOG" 2>&1 &
  else
    nohup "$BIN/cloudflared" tunnel --url "http://127.0.0.1:$VLESS_PORT" --no-autoupdate --protocol http2 >>"$LOG" 2>&1 &
  fi
fi

HOST=""
if [ -s "$CREDS_FILE" ] || [ -s "$TOKEN_FILE" ]; then
  # named-tunnel mode: hostname is fixed, configured in Cloudflare dashboard
  if [ ! -s "$HOST_FILE" ]; then
    echo "FAILED $(date -u '+%F %T'): $HOST_FILE missing (write your tunnel public hostname into it)" > "$LINK"
    echo "PROXY_FAIL no cf-hostname"
    exit 1
  fi
  HOST=$(cat "$HOST_FILE")
  # wait until the edge answers (any HTTP status means a live replica)
  for _ in $(seq 1 20); do
    code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "https://$HOST$WS_PATH" || echo 000)
    [ "$code" != "000" ] && break
    sleep 3
  done
else
  # quick-tunnel mode: scrape the ephemeral URL from the log
  URL=""
  for _ in $(seq 1 20); do
    URL=$(grep -oE "https://[a-zA-Z0-9.-]+\.trycloudflare\.com" "$LOG" 2>/dev/null | tail -1)
    [ -n "$URL" ] && break
    sleep 3
  done
  [ -n "$URL" ] && HOST="${URL#https://}"
fi

if [ -n "$HOST" ]; then
  {
    echo "vless://$UUID@$HOST:443?type=ws&security=tls&sni=$HOST&fp=chrome&path=%2F${WS_PATH#/}&host=$HOST&encryption=none#CloudShell-auto"
    echo "# generated(UTC): $(date -u '+%F %T')"
    echo "# tip: replace the address after @ with a preferred Cloudflare IP/domain; keep sni/host unchanged"
  } > "$LINK"
  echo "PROXY_READY $HOST"
else
  echo "FAILED $(date -u '+%F %T'): tunnel URL not found, see $LOG" > "$LINK"
  echo "PROXY_FAIL"
fi
PROXY_EOF
chmod +x ~/proxy-start.sh

echo "[*] Installing subserver.py ..."
mkdir -p ~/proxy-bin
cat > ~/proxy-bin/subserver.py <<'SUB_EOF'
#!/usr/bin/env python3
"""Dynamic Clash subscription server for the Cloud Shell proxy stack.

Serves ONE secret path (read from `sub-path`), in three formats:
  {sub-path}         — Clash YAML (dynamic; proxies, groups, rules)
  {sub-path}/links   — standard v2ray base64 subscription (vless:// per line)
  {sub-path}/sb.json — ready-to-run sing-box client config (local socks5 :1080,
                       all nodes under a urltest outbound; for devices without
                       Clash, e.g. a VPS running the official sing-box image)

Content is generated per request from:
  - sub-front.yaml   : optional verbatim YAML fragment of front nodes (CF->CloudShell)
  - front-domains.txt: optional, one "domain [display name]" per line — front nodes
                       are generated from it when sub-front.yaml is absent
  - cf-optimized.txt : optional, same line format, auto-refreshed periodically by
                       cf-optimize-refresh.sh (see supervise.sh) — merged with
                       front-domains.txt, deduped by domain (front-domains.txt wins)
  - kui local API    : live residential exit slots (state/egress/ISP), when the
                       residential layer (install-residential.sh) is installed

Config files (all in the same directory as this script, i.e. ~/proxy-bin):
  cf-hostname   — tunnel public hostname (required; also used by proxy-start.sh)
  uuid          — vless UUID (required; created by install.sh)
  sub-path      — secret URL path, e.g. /sub-0123...  (required)
  kui-password  — kui management API password (optional; user is "admin").
                  Without it, only front nodes are served.

Falls back to last-good cache, then to a front-only config, if kui is down.
"""
import base64
import json
import pathlib
import time
import urllib.parse
import urllib.request
import http.server

BASE = pathlib.Path(__file__).resolve().parent
SUB_PATH_FILE = BASE / "sub-path"
FRONT_FILE = BASE / "sub-front.yaml"
KUI_API = "http://127.0.0.1:8090/api/local/exits"
CACHE_TTL = 20  # seconds
PORT = 38081

_cache = {"at": 0.0, "yaml": ""}
_kui_cache = {"at": 0.0, "exits": None}

ISP_SHORT = {
    "sony network communications": "SonyNURO",
    "so-net": "So-net",
    "kddi": "KDDI",
    "arteria networks": "ARTERIA",
    "korea telecom": "KT",
    "triple t": "TripleT",
    "ntt": "NTT",
    "asahi net": "ASAHI",
    "softbank": "SoftBank",
    "biglobe": "BIGLOBE",
    "ocn": "OCN",
    "plala": "Plala",
    "rakuten": "Rakuten",
    "k-opti": "K-Opti",
    "j:com": "JCOM",
    "nifty": "Nifty",
}


def cf_host() -> str:
    return (BASE / "cf-hostname").read_text(encoding="utf-8").strip()


def vless_uuid() -> str:
    return (BASE / "uuid").read_text(encoding="utf-8").strip()


def _active_lines(f: pathlib.Path) -> list[list[str]]:
    out = []
    for ln in f.read_text(encoding="utf-8").splitlines():
        parts = ln.split()
        if parts and not ln.lstrip().startswith("#"):
            out.append(parts)
    return out


# Residential entry domain: only the FIRST entry of res-domains.txt is used —
# one slot = one node, no manual-select fallback variants (they doubled the
# node count). Override via res-domains.txt next to this script:
# one "domain [tag]" per line. Default: the tunnel hostname itself.
def res_domains() -> list[tuple[str, str]]:
    f = BASE / "res-domains.txt"
    if f.exists():
        out = [(p[0], p[1] if len(p) > 1 else "") for p in _active_lines(f)]
        if out:
            return out[:1]
    return [(cf_host(), "")]


def _vless_node(name: str, domain: str, path: str) -> str:
    host = cf_host()
    return "\n".join((
        f"  - name: {json.dumps(name, ensure_ascii=False)}",
        "    type: vless",
        f"    server: {domain}",
        "    port: 443",
        f"    uuid: {vless_uuid()}",
        "    network: ws",
        "    tls: true",
        f"    servername: {host}",
        "    client-fingerprint: chrome",
        "    udp: false",
        "    ws-opts:",
        f"      path: {path}",
        "      headers:",
        f"        Host: {host}",
    ))


def _front_domain_entries() -> list[list[str]]:
    """front-domains.txt entries, then cf-optimized.txt entries (auto-
    refreshed, see cf-optimize-refresh.sh), deduped by domain — first match
    wins so a hand-picked entry always beats the auto-fetched one."""
    seen: set[str] = set()
    out: list[list[str]] = []
    for f in (BASE / "front-domains.txt", BASE / "cf-optimized.txt"):
        if not f.exists():
            continue
        for p in _active_lines(f):
            if p[0] in seen:
                continue
            seen.add(p[0])
            out.append(p)
    return out


def front_block() -> tuple[str, list[str]]:
    """Return (yaml fragment, node names) for the static CF front nodes."""
    if FRONT_FILE.exists():
        frag = FRONT_FILE.read_text(encoding="utf-8").rstrip()
        names = [ln.split('"')[1] for ln in frag.splitlines()
                 if ln.strip().startswith("- name:") and '"' in ln]
        return frag, names
    entries = _front_domain_entries()
    if entries:
        nodes, names = [], []
        for p in entries:
            name = p[1] if len(p) > 1 else p[0]
            nodes.append(_vless_node(name, p[0], "/vless"))
            names.append(name)
        return "\n".join(nodes), names
    return "", []


def isp_short(raw) -> str:
    if isinstance(raw, dict):  # testisp shape: isp is an object
        raw = raw.get("org") or raw.get("asname") or raw.get("as") or ""
    low = (raw or "").lower()
    for key, short in ISP_SHORT.items():
        if key in low:
            return short
    # fallback: first meaningful token, strip corporate suffixes
    for tok in (raw or "").replace(",", " ").split():
        if tok.lower() not in {"inc", "inc.", "corporation", "corp", "co", "co.", "ltd", "ltd.", "llc", "the"}:
            return tok[:12]
    return "RESI"


def kui_exits() -> list[dict]:
    now = time.time()
    if _kui_cache["exits"] is not None and now - _kui_cache["at"] < CACHE_TTL:
        return _kui_cache["exits"]
    password = (BASE / "kui-password").read_text(encoding="utf-8").strip()
    req = urllib.request.Request(KUI_API)
    req.add_header("Authorization",
                   "Basic " + base64.b64encode(f"admin:{password}".encode()).decode())
    with urllib.request.urlopen(req, timeout=3) as r:
        data = json.loads(r.read().decode("utf-8"))
    exits = data.get("exits", []) if isinstance(data, dict) else []
    _kui_cache.update(at=now, exits=exits)
    return exits


def res_node_name(slot: dict, tag: str) -> str:
    slot_id = slot["id"]  # exit-01
    country = slot.get("country") or "??"
    isp = ""
    egress_type = ""
    try:
        res = slot["check_result"]["residential"]
        isp = res["raw"].get("isp") or ""
        egress_type = res.get("egress_type") or ""
    except Exception:
        pass
    kind = "住宅" if egress_type == "residential" else ("机房" if egress_type == "datacenter" else "未知")
    name = f"{country}{kind}·{isp_short(isp)}·{slot_id}"
    if tag:
        name += f"·{tag}"
    return name


def res_node_yaml(slot: dict, domain: str, tag: str) -> str:
    num = slot["id"].split("-", 1)[1]
    return _vless_node(res_node_name(slot, tag), domain, f"/res-{num}")


def front_pairs() -> list[tuple[str, str]]:
    """(display name, entry domain) for each front node, from sub-front.yaml
    or front-domains.txt/cf-optimized.txt — same sources as front_block()."""
    if FRONT_FILE.exists():
        pairs, name = [], None
        for ln in FRONT_FILE.read_text(encoding="utf-8").splitlines():
            s = ln.strip()
            if s.startswith("- name:") and '"' in s:
                name = json.loads(s.split(":", 1)[1].strip())
            elif s.startswith("server:") and name is not None:
                pairs.append((name, s.split(":", 1)[1].strip()))
                name = None
        return pairs
    return [((p[1] if len(p) > 1 else p[0]), p[0]) for p in _front_domain_entries()]


def _live_exits() -> list[dict] | None:
    try:
        return [s for s in kui_exits() if s.get("state") == "ready" and s.get("egress_ip")]
    except Exception:
        return None


def _vless_link(name: str, domain: str, path: str) -> str:
    host = cf_host()
    q = urllib.parse.urlencode({
        "encryption": "none", "security": "tls", "sni": host,
        "type": "ws", "host": host, "path": path, "fp": "chrome",
    })
    return f"vless://{vless_uuid()}@{domain}:443?{q}#{urllib.parse.quote(name)}"


def build_links() -> str:
    """Standard v2ray base64 subscription (one vless:// link per line) — same
    nodes as the Clash YAML, for clients that only eat link lists."""
    links = [_vless_link(n, d, "/vless") for n, d in front_pairs()]
    for slot in _live_exits() or []:
        num = slot["id"].split("-", 1)[1]
        for domain, tag in res_domains():
            links.append(_vless_link(res_node_name(slot, tag), domain, f"/res-{num}"))
    return base64.b64encode("\n".join(links).encode()).decode() + "\n"


def build_sb() -> str:
    """Ready-to-run sing-box client config: local socks5+http proxy on :1080
    (auth required — user "gcs", password = the stack UUID), all nodes under
    a urltest outbound. For devices without Clash (browsers, a VPS, ...):

      docker run -d --name gcs-socks --restart unless-stopped \
        -p 127.0.0.1:1080:1080 -v $PWD/sb.json:/etc/sing-box/config.json:ro \
        ghcr.io/sagernet/sing-box:latest run -c /etc/sing-box/config.json

    Bind 0.0.0.0 instead of 127.0.0.1 to share with other devices — the
    inbound requires auth either way, so it is not an open proxy.
    """
    host, uuid = cf_host(), vless_uuid()

    def vless_out(name, domain, path):
        return {
            "type": "vless", "tag": name, "server": domain, "server_port": 443,
            "uuid": uuid,
            "tls": {"enabled": True, "server_name": host,
                    "utls": {"enabled": True, "fingerprint": "chrome"}},
            "transport": {"type": "ws", "path": path, "headers": {"Host": host}},
        }

    nodes = [vless_out(n, d, "/vless") for n, d in front_pairs()]
    for slot in _live_exits() or []:
        num = slot["id"].split("-", 1)[1]
        for domain, tag in res_domains():
            nodes.append(vless_out(res_node_name(slot, tag), domain, f"/res-{num}"))
    tags = [o["tag"] for o in nodes]
    cfg = {
        "log": {"level": "warn", "timestamp": True},
        "inbounds": [{"type": "mixed", "tag": "socks-in",
                      "listen": "0.0.0.0", "listen_port": 1080,
                      "users": [{"username": "gcs", "password": uuid}]}],
        "outbounds": [
            {"type": "selector", "tag": "proxy",
             "outbounds": ["auto"] + tags, "default": "auto"},
            {"type": "urltest", "tag": "auto", "outbounds": tags,
             "url": "http://www.gstatic.com/generate_204",
             "interval": "5m", "tolerance": 150},
            *nodes,
            {"type": "direct", "tag": "direct"},
        ],
    }
    return json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"


def build_yaml() -> str:
    front, front_names = front_block()
    domains = res_domains()

    res_names, res_blocks = [], []
    pure_names = []  # verified residential only (feeds the url-test group)
    try:
        exits = [s for s in kui_exits() if s.get("state") == "ready" and s.get("egress_ip")]
    except Exception:
        exits = None
    if exits is None and _cache["yaml"]:
        return _cache["yaml"]  # kui down: serve last good
    for slot in exits or []:
        try:
            is_resi = slot["check_result"]["residential"].get("egress_type") == "residential"
        except Exception:
            is_resi = False
        for domain, tag in domains:  # exactly one entry (see res_domains)
            block = res_node_yaml(slot, domain, tag)
            name = block.split('"')[1]
            res_names.append(name)
            res_blocks.append(block)
            if is_resi:
                pure_names.append(name)

    now = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    lines = [
        f"# Cloud Shell proxy subscription — generated {now} (dynamic)",
        f"# {len(front_names)} domain nodes (CF->CloudShell) + {len(res_names)} exit variants ({len(pure_names)} verified residential, live state)",
        "mixed-port: 7890",
        "allow-lan: false",
        "mode: rule",
        "log-level: warning",
    ]
    if front or res_blocks:
        lines.append("proxies:")
        if front:
            lines.append(front)
        lines.extend(res_blocks)
    else:
        lines.append("proxies: []")

    def q(n):
        return json.dumps(n, ensure_ascii=False)

    def lst(items, indent=6):
        pad = " " * indent
        return "\n".join(f"{pad}- {q(i)}" for i in items) if items else f"{pad}- DIRECT"

    g = ["proxy-groups:"]
    g.append('  - name: "🚀 节点选择"\n    type: select\n    proxies:\n' + lst(["⚡ 自动选择", "🏠 住宅自动"] + front_names + res_names + ["DIRECT"]))
    g.append('  - name: "⚡ 自动选择"\n    type: url-test\n    url: "http://www.gstatic.com/generate_204"\n    interval: 300\n    tolerance: 100\n    proxies:\n' + lst(front_names))
    if pure_names:
        g.append('  - name: "🏠 住宅自动"\n    type: url-test\n    url: "http://www.gstatic.com/generate_204"\n    interval: 300\n    tolerance: 150\n    proxies:\n' + lst(pure_names))
    else:
        g.append('  - name: "🏠 住宅自动"\n    type: select\n    proxies:\n      - "🚀 节点选择"')
    for grp in ('🧠 Claude', '🤖 ChatGPT', '🔵 Google·Gemini'):
        g.append(f'  - name: "{grp}"\n    type: select\n    proxies:\n' + lst(["🏠 住宅自动", "🚀 节点选择", "⚡ 自动选择"] + pure_names))
    g.append('  - name: "🌐 其他流量"\n    type: select\n    proxies:\n' + lst(["🚀 节点选择", "⚡ 自动选择", "🏠 住宅自动", "DIRECT"]))
    g.append('  - name: "🇨🇳 中国流量"\n    type: select\n    proxies:\n' + lst(["DIRECT", "🚀 节点选择"]))

    r = ["rules:"]
    for d in ("claude.ai", "claudeusercontent.com", "anthropic.com", "claude.com"):
        r.append(f"  - DOMAIN-SUFFIX,{d},🧠 Claude")
    for d in ("chatgpt.com", "openai.com", "oaistatic.com", "oaiusercontent.com", "chat.com", "openai-api.arkoselabs.io"):
        r.append(f"  - DOMAIN-SUFFIX,{d},🤖 ChatGPT")
    for d in ("gemini.google.com", "generativelanguage.googleapis.com", "bard.google.com", "deepmind.google", "aistudio.google.com"):
        r.append(f"  - DOMAIN-SUFFIX,{d},🔵 Google·Gemini")
    r.append("  - GEOIP,CN,🇨🇳 中国流量,no-resolve")
    r.append("  - MATCH,🌐 其他流量")

    return "\n".join(lines + g + r) + "\n"


def build_front_only() -> str:
    front, front_names = front_block()
    items = "\n".join(f"      - {json.dumps(n, ensure_ascii=False)}" for n in front_names)
    proxies = ("proxies:\n" + front + "\n") if front else "proxies: []\n"
    return (
        "# Cloud Shell proxy subscription — FRONT-ONLY FALLBACK (kui unavailable)\n"
        "mixed-port: 7890\nallow-lan: false\nmode: rule\nlog-level: warning\n"
        + proxies +
        "proxy-groups:\n"
        '  - name: "🚀 节点选择"\n    type: select\n    proxies:\n' + (items or "      - DIRECT") + "\n"
        "rules:\n  - GEOIP,CN,DIRECT,no-resolve\n  - MATCH,🚀 节点选择\n"
    )


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        expected = SUB_PATH_FILE.read_text(encoding="utf-8").strip()
        if self.path == expected:
            now = time.time()
            if _cache["yaml"] and now - _cache["at"] < CACHE_TTL:
                body = _cache["yaml"]
            else:
                try:
                    body = build_yaml()
                except Exception:
                    body = _cache["yaml"] or build_front_only()
                _cache.update(at=now, yaml=body)
            ctype = "text/yaml; charset=utf-8"
        elif self.path == expected + "/links":
            body = build_links()
            ctype = "text/plain; charset=utf-8"
        elif self.path == expected + "/sb.json":
            body = build_sb()
            ctype = "application/json; charset=utf-8"
        else:
            self.send_error(404)
            return
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # dynamic content: never let a client cache a stale exit list
        self.send_header("Cache-Control", "no-store")
        # Clash shows an info bar off this header; we have no real counters
        self.send_header("Subscription-Userinfo",
                         "upload=0; download=0; total=107374182400; expire=0")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    http.server.HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
SUB_EOF

echo "[*] Installing ~/.customize_environment boot hook ..."
cat > ~/.customize_environment <<'HOOK_EOF'
#!/bin/bash
# Cloud Shell boot hook — runs automatically as root when the instance boots.
USER_HOME=$(ls -d /home/*/ 2>/dev/null | head -1)
[ -z "$USER_HOME" ] && exit 0
USER_NAME=$(basename "$USER_HOME")
su - "$USER_NAME" -c "nohup $USER_HOME/proxy-start.sh >/dev/null 2>&1 &"
HOOK_EOF
chmod +x ~/.customize_environment

echo "[*] Starting proxy ..."
bash ~/proxy-start.sh

echo
echo "[+] Done. Your proxy link (also at ~/proxy-link.txt):"
cat ~/proxy-link.txt
