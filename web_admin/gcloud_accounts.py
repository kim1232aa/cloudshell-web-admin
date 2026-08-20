"""List, add, and remove gcloud account configurations used by watchdog.sh.

Status checks mirror watchdog.sh's own account_ok(): a config is "ok" iff
`gcloud auth print-access-token` succeeds for it. Login is handled by
LoginSession, added in the next task.
"""
import os
import re
import subprocess
import threading
from pathlib import Path

import state_paths

NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,50}$")

_lock = threading.Lock()


class InvalidAccountName(ValueError):
    pass


def validate_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise InvalidAccountName(f"invalid account name: {name!r}")


def _gcloud_env(proxy_url: str | None = None) -> dict:
    env = dict(os.environ)
    env["CLOUDSDK_CONFIG"] = state_paths.gcloud_config_dir()
    if proxy_url:
        env["https_proxy"] = proxy_url
        env["http_proxy"] = proxy_url
    else:
        env.pop("https_proxy", None)
        env.pop("http_proxy", None)
    return env


def list_account_names() -> list[str]:
    result = subprocess.run(
        ["gcloud", "config", "configurations", "list", "--format=value(name)"],
        env=_gcloud_env(), capture_output=True, text=True, timeout=30,
    )
    return [line for line in result.stdout.splitlines() if line]


def account_status(name: str) -> str:
    result = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        env={**_gcloud_env(), "CLOUDSDK_ACTIVE_CONFIG_NAME": name},
        capture_output=True, text=True, timeout=20,
    )
    return "ok" if result.returncode == 0 else "auth-invalid"


def account_email(name: str) -> str | None:
    result = subprocess.run(
        ["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"],
        env={**_gcloud_env(), "CLOUDSDK_ACTIVE_CONFIG_NAME": name},
        capture_output=True, text=True, timeout=20,
    )
    lines = result.stdout.strip().splitlines()
    return lines[0] if lines else None


def current_account_index() -> int | None:
    path = Path(state_paths.current_account_file())
    if not path.exists():
        return None
    try:
        return int(path.read_text().strip())
    except ValueError:
        return None


def read_account_proxy_url(name: str) -> str | None:
    path = Path(state_paths.account_proxy_file(name))
    if not path.exists():
        return None
    content = path.read_text().strip()
    return content or None


def write_account_proxy_url(name: str, proxy_url: str | None) -> None:
    path = Path(state_paths.account_proxy_file(name))
    if proxy_url:
        path.write_text(proxy_url)
    else:
        path.unlink(missing_ok=True)


def list_accounts() -> list[dict]:
    names = list_account_names()
    current_idx = current_account_index()
    accounts = []
    for i, name in enumerate(names):
        accounts.append({
            "name": name,
            "email": account_email(name),
            "status": account_status(name),
            "proxy_url": read_account_proxy_url(name),
            "is_current": current_idx == i,
        })
    return accounts
