import os
import re

_SAFE_ACCOUNT_RE = re.compile(r"^[a-zA-Z0-9_\-\.]{1,64}$")


def state_dir() -> str:
    return os.environ.get("STATE_DIR", "/state")


def gcloud_config_dir() -> str:
    return os.path.join(state_dir(), "gcloud")


def proxies_file() -> str:
    return os.path.join(state_dir(), "proxies.json")


def account_proxy_file(account_name: str) -> str:
    return os.path.join(state_dir(), f"proxy-{account_name}")


def current_account_file() -> str:
    return os.path.join(state_dir(), "current-account")


def proxy_link_file() -> str:
    return os.path.join(state_dir(), "proxy-link.txt")


def watchdog_log_file() -> str:
    return os.path.join(state_dir(), "watchdog.log")


def force_failover_flag() -> str:
    return os.path.join(state_dir(), "force-failover-requested")


def gcloud_auth_log_file(account_name: str) -> str:
    if not _SAFE_ACCOUNT_RE.match(account_name):
        raise ValueError(f"invalid account name for auth log: {account_name!r}")
    return os.path.join(state_dir(), f"gcloud-auth-{account_name}.log")


def ssh_key_file() -> str:
    return os.path.join(state_dir(), "ssh", "google_compute_engine")


def provision_cache_dir() -> str:
    return os.path.join(state_dir(), "provision-bundle")


def provision_status_file(account_name: str) -> str:
    if not _SAFE_ACCOUNT_RE.match(account_name):
        raise ValueError(f"invalid account name for provision status: {account_name!r}")
    return os.path.join(state_dir(), f"provision-{account_name}.json")


def provision_log_file(account_name: str) -> str:
    if not _SAFE_ACCOUNT_RE.match(account_name):
        raise ValueError(f"invalid account name for provision log: {account_name!r}")
    return os.path.join(state_dir(), f"provision-{account_name}.log")
