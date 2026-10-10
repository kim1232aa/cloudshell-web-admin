#!/bin/bash
# cf-optimize-refresh.sh — refresh ~/proxy-bin/cf-domains.txt from the BestCF
# community domain pool. This is the ONLY front-domain source subserver.py
# reads; hand-written sub-front.yaml / front-domains.txt are ignored.
#
# Source (plain text, one domain per line, optional :port):
#   https://raw.githubusercontent.com/cmliu/CF-Pages-BestCF/main/cf_domains.txt
# No liveness probing: domains are used as-is. TLS SNI/Host still point at the
# tunnel hostname, so Cloudflare's edge routes to this Cloud Shell.
#
# On fetch failure the previous cf-domains.txt is kept untouched.
# Called from supervise.sh (CF_REFRESH_INTERVAL, default 12h).
set -u
BIN="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$BIN/cf-domains.txt"
SRC="${CF_DOMAINS_URL:-https://raw.githubusercontent.com/cmliu/CF-Pages-BestCF/main/cf_domains.txt}"
LIMIT="${CF_DOMAINS_LIMIT:-40}"

body=$(curl -fsS -m 20 "$SRC" 2>/dev/null) || { echo "fetch failed, keeping $OUT" >&2; exit 1; }
printf '%s\n' "$body" | awk -v lim="$LIMIT" '
  BEGIN { n = 0 }
  /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
  {
    split($1, a, "#")
    d = a[1]
    if (d == "" || seen[d]++) next
    print d
    if (++n >= lim) exit
  }
' > "$OUT.tmp.$$" && mv "$OUT.tmp.$$" "$OUT" && echo "wrote $(wc -l < "$OUT") domains -> $OUT"
