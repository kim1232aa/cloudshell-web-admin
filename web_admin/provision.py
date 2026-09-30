"""Provisioning logic for Cloud Shell instances.

Deploys the proxy stack (xray, cloudflared, sing-box, kui-local-multi-exit)
to newly authorized Google Cloud Shell accounts so that failover/watchdog can
immediately utilize them.

Design:
1. Reusable credentials and shared configs (uuid, cf-hostname, cf-tunnel-creds.json,
   sub-path, and custom front/res domain lists) are maintained in a deployment bundle.
2. If the bundle is not pre-seeded in /state/provision-bundle, provision.py will
   automatically pull a snapshot bundle via SSH from the current active account.
3. For standby accounts (accounts that are NOT currently active in watchdog.sh),
   the proxy is installed and verified, but named tunnel connector processes
   (cloudflared with cf-tunnel-creds.json) are kept stopped to prevent concurrent
   tunnel collision. When watchdog fails over to this account, its boot hook starts
   the named tunnel cleanly.
"""
import base64
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import gcloud_accounts
import state_paths

logger = logging.getLogger(__name__)

# Essential configuration files needed across all Cloud Shell instances in a deployment
REQUIRED_BUNDLE_FILES = [
    "uuid",
    "cf-hostname",
    "cf-tunnel-creds.json",
    "sub-path",
]

OPTIONAL_BUNDLE_FILES = [
    "front-domains.txt",
    "res-domains.txt",
]

# Scripts to package from the web-admin repository
SCRIPTS_TO_PACKAGE = [
    "install.sh",
    "install-residential.sh",
    "supervise.sh",
    "subserver.py",
    "cf-optimize-refresh.sh",
    "front-domains.txt",
    "res-domains.txt",
]

_provision_threads: dict[str, threading.Thread] = {}
_provision_lock = threading.Lock()


class ProvisionError(RuntimeError):
    pass


def _cloudshell_scripts_dir() -> Path:
    env_dir = os.environ.get("CLOUDSHELL_SCRIPTS_DIR")
    if env_dir:
        return Path(env_dir)
    # Inside the web_admin container, we copy scripts to /app/cloudshell
    app_cloudshell = Path("/app/cloudshell")
    if app_cloudshell.is_dir():
        return app_cloudshell
    # Otherwise fallback to repository root
    return Path(__file__).resolve().parent.parent


def _run_gcloud_ssh(
    account_name: str,
    command: str,
    timeout: int = 180,
    check: bool = True,
) -> subprocess.CompletedProcess:
    proxy_url = gcloud_accounts.read_account_proxy_url(account_name)
    env = gcloud_accounts._gcloud_env(proxy_url)
    env["CLOUDSDK_ACTIVE_CONFIG_NAME"] = account_name

    ssh_key = state_paths.ssh_key_file()
    args = [
        "gcloud", "cloud-shell", "ssh",
        "--quiet",
        f"--command={command}",
        "--ssh-flag=-oBatchMode=yes",
        "--ssh-flag=-oStrictHostKeyChecking=no",
    ]
    if os.path.exists(ssh_key):
        args.append(f"--ssh-key-file={ssh_key}")

    res = subprocess.run(
        args,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and res.returncode != 0:
        err = res.stderr.strip() or res.stdout.strip() or f"exit code {res.returncode}"
        raise ProvisionError(f"gcloud ssh failed for {account_name}: {err}")
    return res


def _run_gcloud_scp(
    account_name: str,
    src: str,
    dest: str,
    timeout: int = 180,
) -> subprocess.CompletedProcess:
    proxy_url = gcloud_accounts.read_account_proxy_url(account_name)
    env = gcloud_accounts._gcloud_env(proxy_url)
    env["CLOUDSDK_ACTIVE_CONFIG_NAME"] = account_name

    ssh_key = state_paths.ssh_key_file()
    args = [
        "gcloud", "cloud-shell", "scp",
        "--quiet",
        "--ssh-flag=-oBatchMode=yes",
        "--ssh-flag=-oStrictHostKeyChecking=no",
        src,
        dest,
    ]
    if os.path.exists(ssh_key):
        args.append(f"--ssh-key-file={ssh_key}")

    res = subprocess.run(
        args,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if res.returncode != 0:
        err = res.stderr.strip() or res.stdout.strip() or f"exit code {res.returncode}"
        raise ProvisionError(f"gcloud scp failed for {account_name} ({src} -> {dest}): {err}")
    return res


def pull_bundle_from_source(source_account: str) -> dict[str, bytes]:
    """Pull required configuration files from a live Cloud Shell instance."""
    files_to_check = REQUIRED_BUNDLE_FILES + OPTIONAL_BUNDLE_FILES
    cmd = (
        'cd ~/proxy-bin 2>/dev/null && '
        'F=""; for f in ' + " ".join(files_to_check) + '; do [ -f "$f" ] && F="$F $f"; done; '
        'if [ -n "$F" ]; then '
        'echo "PULL_BUNDLE_BEGIN"; '
        'tar czf - $F 2>/dev/null | base64; '
        'echo "PULL_BUNDLE_END"; '
        'fi'
    )
    res = _run_gcloud_ssh(source_account, cmd, timeout=120, check=True)
    out = res.stdout
    if "PULL_BUNDLE_BEGIN" not in out or "PULL_BUNDLE_END" not in out:
        raise ProvisionError(f"Cloud Shell on {source_account} does not contain proxy-bin configuration files")

    b64_part = out.split("PULL_BUNDLE_BEGIN", 1)[1].split("PULL_BUNDLE_END", 1)[0].strip()
    tar_bytes = base64.b64decode(b64_part)

    with tempfile.TemporaryDirectory() as td:
        tpath = os.path.join(td, "bundle.tgz")
        with open(tpath, "wb") as f:
            f.write(tar_bytes)
        with tarfile.open(tpath, "r:gz") as tar:
            extracted: dict[str, bytes] = {}
            for member in tar.getmembers():
                name = os.path.basename(member.name)
                f = tar.extractfile(member)
                if f:
                    extracted[name] = f.read()

    # Save to local provision-bundle cache
    cache_dir = state_paths.provision_cache_dir()
    os.makedirs(cache_dir, exist_ok=True)
    for name, data in extracted.items():
        dst = os.path.join(cache_dir, name)
        with open(dst, "wb") as f:
            f.write(data)
        os.chmod(dst, 0o600)

    return extracted


def ensure_bundle(source_hint: str | None = None) -> Path:
    """Ensure a valid provision bundle exists in state_paths.provision_cache_dir().

    If missing and a source_hint (e.g. current-account) is available, tries
    pulling it over SSH.
    """
    cache_dir = Path(state_paths.provision_cache_dir())
    cache_dir.mkdir(parents=True, exist_ok=True)

    missing = [f for f in REQUIRED_BUNDLE_FILES if not (cache_dir / f).exists()]
    if missing and source_hint:
        try:
            pull_bundle_from_source(source_hint)
            missing = [f for f in REQUIRED_BUNDLE_FILES if not (cache_dir / f).exists()]
        except Exception as e:
            logger.warning("Failed to pull bundle from %s: %s", source_hint, e)

    if missing:
        raise ProvisionError(
            f"Deployment bundle missing required files: {missing}. "
            "Please ensure at least one active Cloud Shell instance has been set up "
            f"or place files in {cache_dir}."
        )

    return cache_dir


def create_provision_archive(target_is_standby: bool, output_tar: str) -> None:
    """Create a self-contained tarball containing bundle configs, scripts, and provision-runner."""
    cache_dir = Path(state_paths.provision_cache_dir())
    scripts_dir = _cloudshell_scripts_dir()

    with tarfile.open(output_tar, "w:gz") as tar:
        # 1. Add bundle configs
        for name in REQUIRED_BUNDLE_FILES + OPTIONAL_BUNDLE_FILES:
            src = cache_dir / name
            if src.is_file():
                tar.add(str(src), arcname=f"configs/{name}")

        # 2. Add repository deployment scripts
        for script_name in SCRIPTS_TO_PACKAGE:
            src = scripts_dir / script_name
            # If front/res domains exist in bundle configs, they take precedence
            if (cache_dir / script_name).is_file():
                src = cache_dir / script_name
            if src.is_file():
                tar.add(str(src), arcname=f"scripts/{script_name}")

        # 3. Add kui-patches directory if present
        kui_patches_dir = scripts_dir / "kui-patches"
        if kui_patches_dir.is_dir():
            tar.add(str(kui_patches_dir), arcname="scripts/kui-patches")

        # 4. Generate the runner script provision-remote.sh
        runner_content = _generate_remote_runner(target_is_standby)
        ti = tarfile.TarInfo(name="run.sh")
        ti.size = len(runner_content)
        ti.mode = 0o755
        ti.mtime = int(time.time())
        import io
        tar.addfile(ti, io.BytesIO(runner_content))


def _generate_remote_runner(is_standby: bool) -> bytes:
    standby_val = "1" if is_standby else "0"
    script = f"""#!/bin/bash
set -u

STAGE_DIR="$(cd "$(dirname "$0")" && pwd)"
STANDBY="{standby_val}"

echo "[*] Provision runner started (STANDBY=$STANDBY)..."

mkdir -p "$HOME/proxy-bin"

# 1. Back up existing proxy-bin if present
if [ -d "$HOME/proxy-bin" ] && [ "$(ls -A "$HOME/proxy-bin" 2>/dev/null)" ]; then
  tar czf "$HOME/proxy-bin-backup-$(date +%s).tar.gz" -C "$HOME" proxy-bin 2>/dev/null || true
fi

# 2. Stage configurations into ~/proxy-bin
if [ -d "$STAGE_DIR/configs" ]; then
  cp -f "$STAGE_DIR"/configs/* "$HOME/proxy-bin/" 2>/dev/null || true
  chmod 600 "$HOME/proxy-bin"/* 2>/dev/null || true
fi

# 3. If standby, hide tunnel credentials temporarily so install.sh does NOT start
# a conflicting named tunnel connector
if [ "$STANDBY" = "1" ]; then
  echo "[*] Standby mode: suppressing active tunnel connectors..."
  pkill -f supervise.sh 2>/dev/null || true
  pkill -f 'cloudflared tunnel' 2>/dev/null || true
  rm -f "$HOME/proxy-bin/cf-config.yml"
  if [ -f "$HOME/proxy-bin/cf-tunnel-creds.json" ]; then
    mv -f "$HOME/proxy-bin/cf-tunnel-creds.json" "$HOME/proxy-bin/cf-tunnel-creds.json.standby"
  fi
fi

# 4. Copy scripts into place
cp -f "$STAGE_DIR/scripts/install.sh" "$HOME/install.sh"
cp -f "$STAGE_DIR/scripts/install-residential.sh" "$HOME/install-residential.sh"
chmod +x "$HOME/install.sh" "$HOME/install-residential.sh"

# Copy supporting scripts and domain lists into ~/proxy-bin
for s in supervise.sh subserver.py cf-optimize-refresh.sh front-domains.txt res-domains.txt; do
  if [ -f "$STAGE_DIR/scripts/$s" ]; then
    cp -f "$STAGE_DIR/scripts/$s" "$HOME/proxy-bin/$s"
    chmod +x "$HOME/proxy-bin/$s" 2>/dev/null || true
  fi
done

if [ -d "$STAGE_DIR/scripts/kui-patches" ]; then
  rm -rf "$HOME/proxy-bin/kui-patches"
  cp -r "$STAGE_DIR/scripts/kui-patches" "$HOME/proxy-bin/kui-patches"
fi

# 5. Run install.sh (basic proxy, xray + cloudflared)
echo "[*] Executing install.sh..."
export PROMPT_TIMEOUT=1
bash "$HOME/install.sh" || echo "[!] install.sh finished with non-zero exit code (continuing verification)..."

# 6. Run install-residential.sh (sing-box + kui-local-multi-exit + supervision)
echo "[*] Executing install-residential.sh..."
bash "$HOME/install-residential.sh" || echo "[!] install-residential.sh finished with non-zero exit code (continuing verification)..."

# 7. Write canonical robust boot hook into ~/.customize_environment
cat << 'HOOK_EOF' > "$HOME/.customize_environment"
#!/bin/bash
USER_HOME=""
for d in /home/*; do
  if [ -d "$d" ] && [ -f "$d/proxy-start.sh" ]; then
    USER_HOME="$d"
    break
  fi
done
[ -z "$USER_HOME" ] && exit 0
CURRENT_USER=$(basename "$USER_HOME")
su - "$CURRENT_USER" -c 'nohup /bin/bash "$HOME/proxy-start.sh" >/dev/null 2>&1 &'
HOOK_EOF
chmod +x "$HOME/.customize_environment"

# 8. Post-install adjustments for standby mode
if [ "$STANDBY" = "1" ]; then
  echo "[*] Restoring credentials in standby state..."
  if [ -f "$HOME/proxy-bin/cf-tunnel-creds.json.standby" ]; then
    mv -f "$HOME/proxy-bin/cf-tunnel-creds.json.standby" "$HOME/proxy-bin/cf-tunnel-creds.json"
    chmod 600 "$HOME/proxy-bin/cf-tunnel-creds.json"
  fi
  # Terminate any quick tunnel or named tunnel cloudflared spawned during install
  pkill -f 'cloudflared tunnel' 2>/dev/null || true
  rm -f "$HOME/proxy-bin/cf-config.yml"
fi

# 9. Verification
echo "[*] Verifying provision artifacts..."
for req in "$HOME/proxy-start.sh" "$HOME/proxy-bin/uuid" "$HOME/proxy-bin/cf-hostname" "$HOME/proxy-bin/cf-tunnel-creds.json"; do
  if [ ! -f "$req" ]; then
    echo "[-] Missing critical artifact: $req"
    exit 1
  fi
done

if [ ! -x "$HOME/proxy-bin/sing-box" ]; then
  echo "[-] sing-box binary not executable"
  exit 1
fi

echo "[+] Cloud Shell proxy stack successfully provisioned!"
echo "PROVISION_OK"
exit 0
"""
    return script.encode("utf-8")


def run_provision(account_name: str, log_fp=None) -> None:
    """Synchronously provision account_name. Intended to run inside a worker thread."""
    def log(msg: str):
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
        if log_fp:
            log_fp.write(line)
            log_fp.flush()
        logger.info("[%s] %s", account_name, msg)

    current_account = gcloud_accounts.current_account_name()
    target_is_standby = (current_account is not None and current_account != account_name)

    log(f"Starting provisioning for account '{account_name}' (standby={target_is_standby})...")

    # Step 1: Ensure bundle
    log("Checking deployment bundle cache...")
    ensure_bundle(source_hint=current_account)
    log("Deployment bundle ready.")

    # Step 2: Build tarball
    with tempfile.TemporaryDirectory() as td:
        archive_path = os.path.join(td, "provision-payload.tar.gz")
        create_provision_archive(target_is_standby=target_is_standby, output_tar=archive_path)
        archive_size = os.path.getsize(archive_path)
        log(f"Created payload archive ({archive_size} bytes).")

        # Step 3: SCP tarball to Cloud Shell
        remote_tar = "localhost:~/provision-payload.tar.gz"
        log("Uploading provision bundle to Cloud Shell (this may wake the VM)...")
        _run_gcloud_scp(account_name, archive_path, remote_tar, timeout=240)
        log("Upload complete.")

        # Step 4: Extract and run remote runner
        remote_cmd = (
            'set -e; '
            'rm -rf ~/provision-stage && mkdir -p ~/provision-stage; '
            'tar xzf ~/provision-payload.tar.gz -C ~/provision-stage; '
            'rm -f ~/provision-payload.tar.gz; '
            'bash ~/provision-stage/run.sh; '
            'RC=$?; '
            'rm -rf ~/provision-stage; '
            'exit $RC'
        )
        log("Executing remote setup scripts in Cloud Shell...")
        res = _run_gcloud_ssh(account_name, remote_cmd, timeout=600, check=False)

        if res.stdout:
            for l in res.stdout.strip().splitlines():
                log(f"[remote stdout] {l}")
        if res.stderr:
            for l in res.stderr.strip().splitlines():
                log(f"[remote stderr] {l}")

        if res.returncode != 0 or "PROVISION_OK" not in res.stdout:
            raise ProvisionError(
                f"Remote provision failed with code {res.returncode}. "
                "See provision log for details."
            )

    log("Provisioning completed successfully!")


def _write_status(account_name: str, state: str, error: str | None = None) -> None:
    sf = state_paths.provision_status_file(account_name)
    data = {
        "account": account_name,
        "state": state,  # "running" | "ok" | "failed"
        "error": error,
        "updated_at": int(time.time()),
    }
    with open(sf, "w", encoding="utf-8") as f:
        json.dump(data, f)


def get_provision_status(account_name: str) -> dict[str, Any]:
    sf = state_paths.provision_status_file(account_name)
    if not os.path.exists(sf):
        return {"account": account_name, "state": "not_provisioned"}

    try:
        with open(sf, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"account": account_name, "state": "unknown"}

    # If status says running but no thread exists in memory, mark interrupted
    if data.get("state") == "running":
        with _provision_lock:
            t = _provision_threads.get(account_name)
            if not t or not t.is_alive():
                data["state"] = "interrupted"
                data["error"] = "Process interrupted by server restart"

    return data


def get_provision_log(account_name: str, max_lines: int = 100) -> str:
    lf = state_paths.provision_log_file(account_name)
    if not os.path.exists(lf):
        return ""
    try:
        with open(lf, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            return "".join(lines[-max_lines:])
    except Exception:
        return ""


def start_provision(account_name: str) -> None:
    """Start asynchronous provisioning for account_name."""
    gcloud_accounts.validate_name(account_name)

    with _provision_lock:
        t = _provision_threads.get(account_name)
        if t and t.is_alive():
            raise ProvisionError(f"Provisioning is already running for {account_name}")

        _write_status(account_name, "running")

        def _worker():
            log_path = state_paths.provision_log_file(account_name)
            with open(log_path, "a", encoding="utf-8") as lf:
                try:
                    run_provision(account_name, log_fp=lf)
                    _write_status(account_name, "ok")
                except Exception as e:
                    lf.write(f"\n[!] PROVISION FAILED: {e}\n")
                    lf.flush()
                    _write_status(account_name, "failed", error=str(e))
                finally:
                    with _provision_lock:
                        _provision_threads.pop(account_name, None)

        thread = threading.Thread(target=_worker, daemon=True)
        _provision_threads[account_name] = thread
        thread.start()


def cleanup_provision_data(account_name: str) -> None:
    """Remove provision status and log files for account_name."""
    try:
        sf = state_paths.provision_status_file(account_name)
        if os.path.exists(sf):
            os.unlink(sf)
    except Exception:
        pass
    try:
        lf = state_paths.provision_log_file(account_name)
        if os.path.exists(lf):
            os.unlink(lf)
    except Exception:
        pass
