#!/bin/bash
# docker-entrypoint.sh — self-contained container: gcloud credentials, the ssh
# keypair and all state live in the /state volume. The host only needs docker.
#
# Subcommands:
#   (none) / --loop ...   run the watchdog (default)
#   auth <config-name>    add a Google account (interactive OAuth, you do the
#                         browser part; credentials stay inside the volume)
set -u

# route gcloud traffic through the same local proxy when one is configured
# (needed where googleapis.com is not directly reachable)
if [ -n "${PROBE_PROXY:-}" ]; then
  export https_proxy="$PROBE_PROXY" http_proxy="$PROBE_PROXY"
fi

STATE=/state
export CLOUDSDK_CONFIG="$STATE/gcloud"
mkdir -p "$CLOUDSDK_CONFIG" /root/.ssh /state/ssh
chmod 700 /root/.ssh

# persistent gcloud ssh keypair (gcloud uploads the public key automatically
# on every connect, so a fresh container key needs no manual registration)
if [ ! -f /state/ssh/google_compute_engine ]; then
  ssh-keygen -t rsa -b 2048 -f /state/ssh/google_compute_engine -N "" -C gcs-watchdog >/dev/null
fi
cp /state/ssh/google_compute_engine /state/ssh/google_compute_engine.pub /root/.ssh/
chmod 600 /root/.ssh/google_compute_engine

case "${1:-}" in
  auth)
    name="${2:?usage: docker compose run --rm watchdog auth <config-name>}"
    gcloud config configurations describe "$name" >/dev/null 2>&1 \
      || gcloud config configurations create "$name" --quiet
    echo "[*] Starting OAuth. Copy the URL into YOUR browser, authorize,"
    echo "    then paste the result back here. Nothing touches the host."
    CLOUDSDK_ACTIVE_CONFIG_NAME="$name" gcloud auth login --no-launch-browser --quiet
    if CLOUDSDK_ACTIVE_CONFIG_NAME="$name" gcloud auth print-access-token >/dev/null 2>&1; then
      echo "[+] $name authorized and stored in the state volume."
      echo "    First Cloud Shell connect for this account will happen automatically on failover."
    else
      echo "[-] auth failed for $name" >&2
      exit 1
    fi
    ;;
  *)
    exec /usr/local/bin/watchdog.sh "$@"
    ;;
esac
