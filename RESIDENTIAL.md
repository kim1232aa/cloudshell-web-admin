# Residential exit layer (kui)

Optional add-on that turns the Cloud Shell proxy into a **residential-IP exit**:
traffic still enters through the Cloudflare named tunnel, but instead of leaving
via Google's datacenter IP it is fan-out to
[kui-local-multi-exit](https://github.com/kim1232aa/kui-local-multi-exit) —
a dockerized pool of OpenVPN tunnels to VPNGate volunteer nodes (real residential
IPs, mostly JP/KR household ISPs).

```
client (Clash / mihomo ...)
   │  vless+ws+tls :443   (server = any preferred CF domain, SNI = your tunnel host)
   ▼
Cloudflare edge ──► cloudflared named tunnel (HTTP/2, path-split ingress)
   ▼
Cloud Shell VM
   ├─ /vless  → xray :38080 → freedom (Google DC egress, the base layer)
   ├─ /res-NN → sing-box :38089+NN → socks 127.0.0.1:7919+NN ─┐
   └─ /<secret-sub-path> → subserver.py :38081 (dynamic Clash YAML)
                                                              ▼
                                        kui-test container: N OpenVPN tunnels
                                        to VPNGate volunteers → residential IP
```

Slot `exit-NN` ⇔ ws path `/res-NN` ⇔ sing-box port `38089+NN` ⇔ kui socks port `7919+NN`.
Example: `exit-07` = `/res-07` = sing-box `:38096` = kui socks `:7926`.

## Install (inside Cloud Shell, after the base install + named tunnel)

```bash
git clone https://github.com/kim1232aa/cloudshell-web-admin
cd cloudshell-web-admin
bash install-residential.sh            # KUI_SLOT_COUNT=12 bash install-residential.sh for fewer slots
```

The installer is idempotent. It clones/updates the kui repo, applies the patches
from `kui-patches/`, builds the `kui-local:latest` image, generates a random
`~/proxy-bin/kui-password` and `~/proxy-bin/singbox-res.json`, installs
`supervise.sh` (15 s keep-alive loop for every component) into `~/proxy-bin` with a
`~/.bashrc` hook, and re-runs `proxy-start.sh` so the tunnel ingress picks up the
`/res-NN` paths. It prints your dynamic subscription URL when done.

`~/kui-data` (kui state) and `~/proxy-bin` (configs, password, binaries) live in
the persistent `$HOME`, so the whole layer comes back after a VM recycle: the
boot hook runs `proxy-start.sh`, which in turn starts `supervise.sh` (flock-guarded,
so the extra `~/.bashrc` hook on interactive logins is harmless).

## Subscription behavior

`subserver.py` builds the Clash YAML per request:

- **Front nodes** (CF→CloudShell, Google egress): from `~/proxy-bin/sub-front.yaml`
  (verbatim proxies fragment) or, if absent, generated from
  `~/proxy-bin/front-domains.txt` — one `domain [name]` per line.
- **Residential nodes**: live from the kui API. Each ready slot gets one node per
  line of `~/proxy-bin/res-domains.txt` (`domain [tag]`; first line = primary,
  joins url-test groups; rest = manual fallbacks). No slot×domain matrix.
  If `res-domains.txt` is missing the tunnel hostname itself is the entry domain.
- Nodes are **labeled honestly**: `JP住宅·SonyNURO·exit-03`, `FR机房·OVH·exit-10`.
  Only slots verified `residential` enter the `🏠 住宅自动` url-test group and the
  per-country groups; datacenter/unknown slots stay manual-select.
- kui down → serves last-good cache (20 s TTL), then a front-only config.

## Ops

```bash
docker logs -f kui-test              # slot dialing / recovery
tail -f /tmp/singbox-res.log /tmp/cf-tunnel.log /tmp/subserver.log
curl -u admin:$(cat ~/proxy-bin/kui-password) http://127.0.0.1:8090/api/local/exits | jq '.exits[] | {id,state,egress_ip,country}'
```

Slot failures (dead VPNGate volunteer, flaky dial) are retried and replaced by kui
itself; `supervise.sh` only restarts whole components. Both survive reboots.

## Why the patches (kui-patches/)

- `0001-exit-manager-from-rule.patch` — kui was built for a VPS where each tunnel
  gets its own policy route. Inside one docker container sharing a single netns
  with 24 tunnels, replies must be steered by *source* address: adds an
  `ip rule add from <tunnel-ip> lookup <table>` per slot (plus `--tun-mtu 1400
  --mssfix 1300`, which measurably helps PPPoE-ish volunteer uplinks).
- `0002-vpngate-ipapi-fallback.patch` — the testisp.info classifier has no data
  for many volunteer IPs; falls back to ip-api.com (`hosting` flag + datacenter
  keyword list) so slots still get an honest residential/datacenter label.

Apply upstream instead of carrying these locally once merged into kui.

## Known limits

- VPNGate volunteers are… volunteers: slots die, get replaced, and some are
  flagged `proxy:true` by IP databases or are actually VPS (labeled `机房`).
  Expect roughly 60–80 % of slots ready at any time; the subscription reflects
  live state, so just re-fetch it.
- Speed is bounded by the volunteer's uplink (typically 0.5–5 MB/s per slot);
  the front (Google-egress) nodes remain the fast path for bulk traffic.
- All base-layer limits still apply (50 h/week/account, 12 h sessions, ToS).
