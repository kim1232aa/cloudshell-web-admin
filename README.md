# cloudshell-proxy-autostart

> **Branch `kui-residential`**: adds an optional residential-IP exit layer
> (kui / VPNGate pool behind sing-box, dynamic Clash subscription).
> See [RESIDENTIAL.md](RESIDENTIAL.md).

Self-healing VLESS+WS proxy on Google Cloud Shell, fronted by Cloudflare Tunnel —
plus a self-contained local watchdog container that keeps it alive and rotates
across multiple Google accounts when one dies or runs out of weekly quota.

> 中文速览：Cloud Shell 里跑一行 `install.sh`，自动装好 xray(vless+ws) + cloudflared 并输出 vless:// 链接。
> 本机只需 docker：watchdog 容器负责探活、保活、额度耗尽自动轮换账户（容器不过流量，中转由 Cloudflare 优选 IP 完成）。
> 授权在容器内完成（`--no-browser`，浏览器部分你自己点），凭证全部留在容器卷里。

## Architecture

```
client (Clash / v2rayN / Nekoray ...)
   │  vless+ws+tls :443
   ▼
Cloudflare edge  (preferred-IP capable)   ◄── the actual relay
   │  cloudflared tunnel (HTTP/2)
   ▼
Cloud Shell VM (any of your Google accounts)
   ├─ cloudflared ──► 127.0.0.1:38080
   └─ xray (vless+ws inbound, freedom outbound)

local watchdog container (no proxy traffic passes through it)
   ├─ probes https://<host>/vless every INTERVAL
   ├─ keepalive ssh tickle (< 40-min non-interactive timeout)
   └─ on failure: boots the next account's Cloud Shell (any boot failure —
      quota exhausted, broken auth — just skips to the next account)
```

- `xray` listens only on loopback; the only ingress is the cloudflared tunnel.
- cloudflared uses `--protocol http2` (measured much faster than QUIC from Cloud Shell egress).
- `$HOME` (5 GB) persists across recycling: binaries, UUID, tokens and the boot hook survive.
- `~/.customize_environment` is the official boot hook — it rebuilds the proxy at every boot.
- The watchdog container is fully self-contained: gcloud credentials, ssh keypair and
  state live in the `./state` volume. The host only needs docker.

## Measured throughput

Single-connection downloads through the proxy, from a residential line behind a CN ISP.
Your numbers will vary with time of day, CF edge and preferred IP.

| Path | Throughput |
|---|---|
| SSH dynamic forwarding (`ssh -D`) | ~0.3 MB/s |
| Quick tunnel, default CF IP | 0.1 – 3 MB/s |
| Quick tunnel, QUIC + preferred IP | 2.6 – 3.3 MB/s |
| **Quick tunnel, HTTP/2 + preferred IP** | **12 – 23 MB/s** |
| Cloud Shell raw egress (reference) | ~26 MB/s single stream |

"Preferred IP" = replace the address after `@` in the vless link with a fast Cloudflare
anycast IP/domain for your ISP; keep `sni`/`host` unchanged.

## Requirements

- A Google account with Cloud Shell access (several accounts for HA rotation).
- Docker (or any cron-capable shell, if you prefer running `watchdog.sh` directly).
- (HA mode) A Cloudflare account + a domain on Cloudflare nameservers.
- Client that speaks vless+ws+tls (Clash-Meta/mihomo, v2rayN, Nekoray, sing-box, ...).

## 1. Cloud Shell side (per Google account)

Inside [Google Cloud Shell](https://shell.cloud.google.com):

```bash
bash <(curl -sSL https://raw.githubusercontent.com/kim1232aa/cloudshell-proxy-autostart/main/install.sh)
```

It downloads xray + cloudflared into `~/proxy-bin`, installs `~/proxy-start.sh` and the
boot hook, starts everything, and prints your link (also saved to `~/proxy-link.txt`).

**After a VM recycle:** reopen Cloud Shell (web or `gcloud cloud-shell ssh`) — the boot
hook rebuilds the proxy; `cat ~/proxy-link.txt` for the (new, quick-tunnel) link.
The watchdog below automates exactly this.

## 2. Multi-account HA (named tunnel, fixed hostname)

Quick tunnels get a random hostname per boot — fine solo, annoying for failover.
A **named Cloudflare Tunnel** gives one fixed hostname backed by replicas on multiple
accounts; Cloudflare routes to whichever connector is alive.

One-time Cloudflare setup — two ways:

**Option A: `cf-setup.sh` (no dashboard, no API token)** — needs a local cloudflared
that has done `cloudflared tunnel login` once:

```bash
./cf-setup.sh gcs.example.com          # creates tunnel + DNS route, prints next steps
```

It hands you a credentials JSON; every Cloud Shell account will join the tunnel with it
(credentials-file mode, replicas supported natively).

**Option B: dashboard (token mode)** — [Zero Trust](https://one.dash.cloudflare.com) →
**Networks → Tunnels → Add a tunnel** → `cloudflared`, copy the `eyJ...` token, add a
**Public Hostname** `gcs.example.com` with service `http://127.0.0.1:38080`.

Per Google account (in that account's Cloud Shell, after step 1):

```bash
# option A: paste the credentials JSON
nano ~/proxy-bin/cf-tunnel-creds.json
# option B instead:
echo '<tunnel-token>'  > ~/proxy-bin/cf-tunnel-token

echo 'gcs.example.com' > ~/proxy-bin/cf-hostname
# every account MUST share one UUID: copy ~/proxy-bin/uuid from the first account
bash ~/proxy-start.sh
```

From now on every account's Cloud Shell joins the same tunnel; one client config works
regardless of which account is currently running.

### Hosting a Clash subscription on the tunnel (optional, creds mode)

The named tunnel can also serve your Clash subscription, so updating preferred IPs
is a server-side edit instead of re-importing links. `subserver.py` generates the
YAML **dynamically** per request:

```bash
# on each account's Cloud Shell:
echo "/sub-$(openssl rand -hex 16)" > ~/proxy-bin/sub-path   # one secret path, same everywhere
# front nodes — either format:
nano ~/proxy-bin/sub-front.yaml      # verbatim Clash proxies fragment, or
nano ~/proxy-bin/front-domains.txt   # one "domain [name]" per line → vless+ws nodes
bash ~/proxy-start.sh                # re-run: adds path-split ingress
```

Ingress becomes `/vless` → xray (plus `/res-NN` → sing-box when the residential
layer is installed), everything else → `subserver.py` (127.0.0.1:38081), which
returns the YAML at the exact secret path and a bare 404 for anything else
(no directory listing). Subscribe at `https://<host><secret-path>` — the URL is a
credential, treat it like a password. Token-mode (dashboard) tunnels: configure the
same path split as two Public Hostname/ingress rules in the dashboard instead.

The same secret path also serves two non-Clash formats:

- `{sub-path}/links` — standard v2ray base64 subscription (one `vless://` per
  line), for v2rayNG / Shadowrocket / Nekobox and friends
- `{sub-path}/sb.json` — ready-to-run sing-box **client** config: a mixed
  socks5+http proxy on `:1080` (auth: user `gcs`, password = the stack UUID),
  all nodes under a urltest outbound. For devices without Clash — browsers,
  curl, a VPS — using the official sing-box image:

  ```bash
  curl -fsSL "https://<host><secret-path>/sb.json" -o sb.json
  docker run -d --name gcs-socks --restart unless-stopped \
    -p 1080:1080 -v "$PWD/sb.json:/etc/sing-box/config.json:ro" \
    ghcr.io/sagernet/sing-box:latest run -c /etc/sing-box/config.json
  # apps / browsers then use  http://gcs:<uuid>@<this-host>:1080
  #                    or  socks5://gcs:<uuid>@<this-host>:1080
  # refresh nodes by re-curl + docker restart gcs-socks
  ```

  (Plain `socks5://` share links cannot express the ws+tls disguise the tunnel
  requires, so the socks5 entry is provided client-side by sing-box, not as a
  share-link protocol. The inbound always requires auth, so binding 0.0.0.0 to
  serve other devices does not create an open proxy.)

## 3. Watchdog container (self-contained monitor + failover)

```bash
git clone https://github.com/kim1232aa/cloudshell-proxy-autostart
cd cloudshell-proxy-autostart
echo 'TUNNEL_HOST=gcs.example.com' > .env     # or your current quick-tunnel host
docker compose up -d --build
```

It probes the hostname every 3 minutes. On failure it walks your gcloud
configurations, skips accounts with broken auth or quota cooldown, and boots the next
account's Cloud Shell — whose boot hook rebuilds the proxy and rejoins the tunnel.
Typical failover: 1–3 minutes.

### Adding accounts (you do the browser part)

```bash
docker compose run --rm watchdog auth acct-a
docker compose run --rm watchdog auth acct-b   # ...repeat per account
```

Each runs `gcloud auth login --no-browser` **inside the container**: you open the
printed URL in your own browser, authorize, paste the result back. Credentials are
stored only in the `./state` volume (`/state/gcloud`) — nothing is written to the host.
No host gcloud installation or host ssh keys are needed; the container generates and
persists its own ssh keypair (`/state/ssh`), which gcloud uploads automatically on
connect (verified: the very first `gcloud cloud-shell ssh` does keygen + registration
by itself).

### Failure-driven rotation

Cloud Shell gives **50 h/week per account** ([docs](https://docs.cloud.google.com/shell/docs/quotas-limits));
there is **no public API for remaining hours** — so the watchdog does not try to
track them. Rotation is purely failure-driven: when the probe dies it walks your
accounts and boots the first one whose `gcloud cloud-shell ssh` actually succeeds.
A quota-exhausted account fails that attempt (gcloud reports an error, rc ≠ 0)
and the next account is tried — no quota bookkeeping, no cooldowns. 4 accounts ×
50 h = 200 h > 168 h — enough to cover a full week.

### Ops

```bash
docker logs -f gcs-watchdog          # live monitor log (UTC timestamps)
cat state/status                     # last known state
cat state/proxy-link.txt             # latest link (changes on quick-tunnel rebuilds)
docker compose run --rm watchdog --force   # force an immediate failover
```

Useful env knobs (compose `.env`): `INTERVAL` (probe seconds, default 600),
`PROBE_RETRIES` (000-retries before declaring dead, default 4 — 000 is flaky on
some networks; 502/503/530 are believed immediately), `PROBE_PROXY` (route the
probe — and in docker also gcloud itself — via a local proxy, e.g.
`http://172.17.0.1:7890`), `KEEPALIVE` / `KEEPALIVE_INTERVAL` (default 1500 s <
40-min timeout).

### How auth persists

- **gcloud OAuth**: refresh tokens in `/state/gcloud` (container volume). They survive
  container restarts/rebuilds and only die if revoked, if the Google password changes,
  or after ~6 months unused. Re-run `auth <name>` to repair.
- **Cloud Shell side**: each account's `$HOME` persists (5 GB), so `cf-tunnel-token`,
  `cf-hostname`, `uuid`, the binaries and the boot hook all survive VM recycling.
  `$HOME` is deleted after **120 days** of account inactivity — the watchdog's regular
  sessions prevent that too.
- **ssh key**: container-generated, persisted in `/state/ssh`; gcloud re-uploads the
  public key on every connect, so a fresh container needs zero manual setup.

## 4. Web admin panel (recommended over the CLI `auth` flow)

```bash
python3 web_admin/hash_password.py                  # prints ADMIN_PASSWORD_HASH
python3 -c "import secrets; print(secrets.token_hex(32))"   # SESSION_SECRET
cp .env.example .env   # fill in TUNNEL_HOST, ADMIN_DOMAIN, ADMIN_PASSWORD_HASH, SESSION_SECRET
docker compose up -d --build
```

Open `https://$ADMIN_DOMAIN/` (Caddy provisions the certificate automatically —
`ADMIN_DOMAIN` must already point at this VPS). From there: add/remove Google
accounts (web-based `gcloud auth login`, no terminal needed), maintain a pool of
proxies and bind one to each account (every gcloud call for that account —
login, Cloud Shell rebuild, keepalive — then goes through it instead of this
VPS's bare IP), watch the current proxy link/QR/logs, and trigger an immediate
failover. The CLI flow (`docker compose run --rm watchdog auth <name>`) still
works as a fallback.

## Hard limits (read before relying on this)

- 50 h/week/account usage quota; sessions capped at 12 h; non-interactive sessions are
  terminated after 40 min (the keepalive + dead-probe + auto-rebuild combo covers this:
  worst case is one probe interval of downtime).
- Quick-tunnel hostnames are random per boot — use a named tunnel for anything stable.
- Cloud Shell egress is solid (~26 MB/s single stream in tests) but end-to-end speed
  depends heavily on your preferred-IP choice.
- Google can throttle or restrict accounts for ToS violations. See below.

## Files

| File | Runs where | Purpose |
|---|---|---|
| `install.sh` | Cloud Shell | One-shot installer (self-contained, embeds the other two scripts) |
| `proxy-start.sh` | Cloud Shell | Rebuilds xray + cloudflared; idempotent; named/quick tunnel auto-detect |
| `.customize_environment` | Cloud Shell | Official boot hook, hands off to `proxy-start.sh` at every boot |
| `watchdog.sh` | Local / container | Probe, keepalive, quota-aware multi-account failover; cron or `--loop` |
| `cf-setup.sh` | Local | Create named tunnel + DNS route via local cloudflared login (no dashboard) |
| `subserver.py` | Cloud Shell | Optional: dynamic subscription at one secret path — Clash YAML, v2ray base64 links, sing-box client config (front + live residential nodes) |
| `install-residential.sh` | Cloud Shell | Optional: add the kui residential-exit layer — see [RESIDENTIAL.md](RESIDENTIAL.md) |
| `supervise.sh` | Cloud Shell | 15 s keep-alive loop for all components (installed by `install-residential.sh`) |
| `res-domains.txt`, `front-domains.txt` | Cloud Shell | Examples: entry-domain lists for residential / front nodes |
| `kui-patches/` | Cloud Shell | Local patches applied to kui-local-multi-exit at install time |
| `Dockerfile`, `docker-entrypoint.sh`, `docker-compose.yml` | Local | Self-contained watchdog container (`state/` volume holds everything) |

## Disclaimer

For development, testing and learning purposes. Running a persistent proxy on Cloud
Shell may violate the [Google Cloud Shell Terms of Service](https://cloud.google.com/terms/cloud-shell);
Google may throttle, restrict or suspend accounts that do. Use at your own risk.
Cloudflare Tunnel usage is subject to Cloudflare's terms as well.
