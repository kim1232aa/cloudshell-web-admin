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
