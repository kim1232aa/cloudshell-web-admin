#!/usr/bin/env python3
"""Dynamic Clash subscription server for the Cloud Shell proxy stack.

Serves ONE secret path (read from `sub-path`), in three formats:
  {sub-path}         — Clash YAML (dynamic; proxies, groups, rules)
  {sub-path}/links   — standard v2ray base64 subscription (vless:// per line)
  {sub-path}/sb.json — ready-to-run sing-box client config (local socks5 :1080,
                       all nodes under a urltest outbound; for devices without
                       Clash, e.g. a VPS running the official sing-box image)

Content is generated per request from:
  - cf-domains.txt   : the ONLY front-domain source, written by
                       cf-optimize-refresh.sh from the BestCF community list
                       (cmliu/CF-Pages-BestCF cf_domains.txt). One
                       "domain[:port] [display name]" per line. Hand-written
                       sub-front.yaml / front-domains.txt are ignored.
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
CF_DOMAINS_FILE = BASE / "cf-domains.txt"
KUI_API = "http://127.0.0.1:8090/api/local/exits"
CACHE_TTL = 20  # seconds
PORT = 38081

_cache = {"at": 0.0, "yaml": ""}
_kui_cache = {"at": 0.0, "exits": None}

_CLAUDE_RULES: tuple[str, ...] = (
    "DOMAIN-SUFFIX,anthropic.com",
    "DOMAIN-SUFFIX,claude.ai",
    "DOMAIN-SUFFIX,claude.com",
    "DOMAIN-SUFFIX,clau.de",
    "DOMAIN-SUFFIX,claudemcpclient.com",
    "DOMAIN-SUFFIX,claudeusercontent.com",
    "DOMAIN-SUFFIX,modelcontextprotocol.io",
    "DOMAIN,anthropic.com.cdn.cloudflare.net",
    "DOMAIN,servd-anthropic-website.b-cdn.net",
    "DOMAIN,anthropic.auth0.com",
    "DOMAIN-KEYWORD,claude",
    "DOMAIN-KEYWORD,anthropic",
)

_CHATGPT_RULES: tuple[str, ...] = (
    "DOMAIN-SUFFIX,chatgpt.com",
    "DOMAIN-SUFFIX,openai.com",
    "DOMAIN-SUFFIX,oaistatic.com",
    "DOMAIN-SUFFIX,oaiusercontent.com",
    "DOMAIN-SUFFIX,chat.com",
    "DOMAIN-SUFFIX,sora.com",
    "DOMAIN-SUFFIX,openai-api.arkoselabs.io",
    "DOMAIN-KEYWORD,openai",
    "DOMAIN-KEYWORD,chatgpt",
)

_GEMINI_RULES: tuple[str, ...] = (
    "DOMAIN-SUFFIX,aistudio.google.com",
    "DOMAIN-SUFFIX,makersuite.google.com",
    "DOMAIN-SUFFIX,generativelanguage.googleapis.com",
    "DOMAIN-SUFFIX,cloudcode-pa.googleapis.com",
    "DOMAIN-SUFFIX,ai.google.dev",
    "DOMAIN-SUFFIX,antigravity.google",
    "DOMAIN-SUFFIX,gemini.google.com",
    "DOMAIN-SUFFIX,vertexai.googleapis.com",
    "DOMAIN-SUFFIX,aiplatform.googleapis.com",
    "DOMAIN-SUFFIX,googleapis.com",
)

_LOCAL_DIRECT_RULES: tuple[str, ...] = (
    "DOMAIN-SUFFIX,local,DIRECT",
    "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve",
    "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
    "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve",
    "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
    "IP-CIDR,100.64.0.0/10,DIRECT,no-resolve",
    "IP-CIDR,198.18.0.0/15,DIRECT,no-resolve",
    "GEOIP,LAN,DIRECT,no-resolve",
)

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


def _cf_domain_for_slot(slot_id: str) -> tuple[str, int]:
    """Deterministically pick a (domain, port) from cf-domains.txt per slot."""
    entries = _cf_entries()
    if entries:
        num = slot_id.split("-", 1)[-1] if "-" in slot_id else slot_id
        if num.isdigit():
            idx = (int(num) - 1) % len(entries)
        else:
            import zlib
            idx = zlib.crc32(slot_id.encode()) % len(entries)
        return entries[idx][0], entries[idx][1]
    f = BASE / "res-domains.txt"
    if f.exists():
        out = _active_lines(f)
        if out:
            return out[0][0], 443
    return cf_host(), 443


def res_domains() -> list[tuple[str, str]]:
    f = BASE / "res-domains.txt"
    if f.exists():
        out = [(p[0], p[1] if len(p) > 1 else "") for p in _active_lines(f)]
        if out:
            return out[:1]
    return [(cf_host(), "")]


def _vless_node(name: str, domain: str, path: str, port: int = 443) -> str:
    host = cf_host()
    return "\n".join((
        f"  - name: {json.dumps(name, ensure_ascii=False)}",
        "    type: vless",
        f"    server: {domain}",
        f"    port: {int(port)}",
        f"    uuid: {vless_uuid()}",
        "    network: ws",
        "    tls: true",
        f"    servername: {host}",
        "    client-fingerprint: chrome",
        "    udp: true",
        "    ws-opts:",
        f"      path: {path}",
        "      headers:",
        f"        Host: {host}",
    ))


def _parse_cf_line(line: str):
    """domain[:port][#tag| tag] -> (domain, port, tag). Tag defaults to the domain."""
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    tag = ""
    if "#" in raw:
        raw, tag = raw.split("#", 1)
        raw, tag = raw.strip(), tag.strip()
    elif " " in raw or "\t" in raw:
        raw, tag = raw.split(None, 1)
        raw, tag = raw.strip(), tag.strip()
    port = 443
    if ":" in raw:
        host_part, port_part = raw.rsplit(":", 1)
        if port_part.isdigit() and 1 <= int(port_part) <= 65535:
            raw, port = host_part.strip(), int(port_part)
    if not raw:
        return None
    return raw, port, (tag or raw)

def _cf_entries():
    """(domain, port, unique display name) from cf-domains.txt — the BestCF list."""
    if not CF_DOMAINS_FILE.exists():
        return []
    out, seen, counts = [], set(), {}
    for line in CF_DOMAINS_FILE.read_text(encoding="utf-8").splitlines():
        parsed = _parse_cf_line(line)
        if not parsed or parsed[0] in seen:
            continue
        seen.add(parsed[0])
        counts[parsed[2]] = counts.get(parsed[2], 0) + 1
        name = parsed[2] if counts[parsed[2]] == 1 else "%s (%d)" % (parsed[2], counts[parsed[2]])
        out.append((parsed[0], parsed[1], name))
    return out

def _unique_front_pairs():
    """(unique display name, domain) — port-443 entries only, for link/sb builders."""
    return [(name, domain) for domain, port, name in _cf_entries() if port == 443]

def front_block():
    """Return (yaml fragment, node names) for the BestCF front nodes."""
    entries = _cf_entries()
    if entries:
        nodes = [_vless_node(name, domain, "/vless", port) for domain, port, name in entries]
        names = [name for _, _, name in entries]
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


def res_node_yaml(slot: dict) -> str:
    num = slot["id"].split("-", 1)[1]
    domain, port = _cf_domain_for_slot(slot["id"])
    return _vless_node(res_node_name(slot, ""), domain, f"/res-{num}", port)


def front_pairs() -> list[tuple[str, str]]:
    """(display name, entry domain) for each port-443 BestCF front node."""
    return _unique_front_pairs()


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
        domain, _ = _cf_domain_for_slot(slot["id"])
        links.append(_vless_link(res_node_name(slot, ""), domain, f"/res-{num}"))
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
        domain, _ = _cf_domain_for_slot(slot["id"])
        nodes.append(vless_out(res_node_name(slot, ""), domain, f"/res-{num}"))
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
        block = res_node_yaml(slot)
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
    g.append('  - name: "⚡ 自动选择"\n    type: url-test\n    url: "http://www.gstatic.com/generate_204"\n    interval: 60\n    tolerance: 100\n    lazy: false\n    proxies:\n' + lst(front_names))
    if pure_names:
        g.append('  - name: "🏠 住宅自动"\n    type: url-test\n    url: "http://www.gstatic.com/generate_204"\n    interval: 60\n    tolerance: 150\n    lazy: false\n    proxies:\n' + lst(pure_names))
    else:
        g.append('  - name: "🏠 住宅自动"\n    type: select\n    proxies:\n      - "🚀 节点选择"')
    for grp in ('🧠 Claude', '🤖 ChatGPT', '🔵 Google·Gemini'):
        g.append(f'  - name: "{grp}"\n    type: select\n    proxies:\n' + lst(["⚡ 自动选择", "🏠 住宅自动", "🚀 节点选择", "DIRECT"]))
    g.append('  - name: "🌐 其他流量"\n    type: select\n    proxies:\n' + lst(["🚀 节点选择", "⚡ 自动选择", "🏠 住宅自动", "DIRECT"]))
    g.append('  - name: "🇨🇳 中国流量"\n    type: select\n    proxies:\n' + lst(["DIRECT", "🚀 节点选择"]))

    r = ["rules:"]
    r.extend(f"  - {rule},🤖 ChatGPT" for rule in _CHATGPT_RULES)
    r.extend(f"  - {rule},🧠 Claude" for rule in _CLAUDE_RULES)
    r.extend(f"  - {rule},🔵 Google·Gemini" for rule in _GEMINI_RULES)
    r.extend(f"  - {rule}" for rule in _LOCAL_DIRECT_RULES)
    r.extend((
        "  - DOMAIN-SUFFIX,cn,🇨🇳 中国流量",
        "  - GEOSITE,cn,🇨🇳 中国流量",
        "  - GEOSITE,CN,🇨🇳 中国流量",
        "  - GEOIP,CN,🇨🇳 中国流量",
        "  - MATCH,🌐 其他流量",
    ))

    return "\n".join(lines + g + r) + "\n"


def build_front_only() -> str:
    front, front_names = front_block()
    items = "\n".join(f"      - {json.dumps(n, ensure_ascii=False)}" for n in front_names)
    proxies = ("proxies:\n" + front + "\n") if front else "proxies: []\n"
    r = ["rules:"]
    r.extend(f"  - {rule}" for rule in _LOCAL_DIRECT_RULES)
    r.extend((
        "  - DOMAIN-SUFFIX,cn,DIRECT",
        "  - GEOSITE,cn,DIRECT",
        "  - GEOSITE,CN,DIRECT",
        "  - GEOIP,CN,DIRECT",
        "  - MATCH,🚀 节点选择",
    ))
    return (
        "# Cloud Shell proxy subscription — FRONT-ONLY FALLBACK (kui unavailable)\n"
        "mixed-port: 7890\nallow-lan: false\nmode: rule\nlog-level: warning\n"
        + proxies +
        "proxy-groups:\n"
        '  - name: "🚀 节点选择"\n    type: select\n    proxies:\n' + (items or "      - DIRECT") + "\n"
        + "\n".join(r) + "\n"
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
