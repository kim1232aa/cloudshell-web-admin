"""Central definitions for the on-disk state layout under STATE_DIR.

Every path used by web-admin and by watchdog.sh's account-proxy binding is
defined exactly once here so the two never drift apart.
"""
import os


def _state_dir() -> str:
    return os.environ.get("STATE_DIR", "/state")


def gcloud_config_dir() -> str:
    return os.path.join(_state_dir(), "gcloud")


def proxies_file() -> str:
    return os.path.join(_state_dir(), "proxies.json")


def account_proxy_file(name: str) -> str:
    return os.path.join(_state_dir(), f"proxy-{name}")


def current_account_file() -> str:
    return os.path.join(_state_dir(), "current-account")


def proxy_link_file() -> str:
    return os.path.join(_state_dir(), "proxy-link.txt")


def watchdog_log_file() -> str:
    return os.path.join(_state_dir(), "watchdog.log")


def force_failover_flag() -> str:
    return os.path.join(_state_dir(), "force-failover-requested")


def gcloud_auth_log_file(name: str) -> str:
    return os.path.join(_state_dir(), f"gcloud-auth-{name}.log")
