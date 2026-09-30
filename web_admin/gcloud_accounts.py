"""List, add, and remove gcloud account configurations used by watchdog.sh.

Status checks mirror watchdog.sh's own account_ok(): a config is "ok" iff
`gcloud auth print-access-token` succeeds for it. Login is handled by
LoginSession.
"""
import json
import logging
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Callable

import state_paths

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,50}$")

_lock = threading.Lock()


class InvalidAccountName(ValueError):
    pass


class AccountError(RuntimeError):
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


def get_configurations() -> list[dict]:
    result = subprocess.run(
        ["gcloud", "config", "configurations", "list", "--format=json"],
        env=_gcloud_env(), capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
        return data if isinstance(data, list) else []
    except Exception:
        return []


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


def current_account_name() -> str | None:
    path = Path(state_paths.current_account_file())
    if not path.exists():
        return None
    content = path.read_text().strip()
    return content or None


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
    current_name = current_account_name()
    accounts = []
    for name in names:
        accounts.append({
            "name": name,
            "email": account_email(name),
            "status": account_status(name),
            "proxy_url": read_account_proxy_url(name),
            "is_current": name == current_name,
        })
    return accounts


_login_sessions: dict[str, "LoginSession"] = {}


class LoginSession:
    """One in-progress `gcloud auth login --no-launch-browser` subprocess."""

    def __init__(
        self,
        name: str,
        proxy_url: str | None,
        on_success: Callable[[str], None] | None = None,
    ):
        self.name = name
        self._output_lines: list[str] = []
        self.done = False
        self.success = False
        self.on_success = on_success
        self._log_path = state_paths.gcloud_auth_log_file(name)
        # Note: --no-launch-browser is the standard Google Cloud SDK flag for
        # headless environments that prompts the user to visit an auth URL and
        # enter the verification code on stdin.
        self.process = subprocess.Popen(
            ["gcloud", "auth", "login", "--no-launch-browser", "--quiet"],
            env={**_gcloud_env(proxy_url), "CLOUDSDK_ACTIVE_CONFIG_NAME": name},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_output, daemon=True)
        self._reader.start()

    def _read_output(self) -> None:
        with open(self._log_path, "a", encoding="utf-8") as log_file:
            while True:
                chunk = self.process.stdout.read(1)
                if chunk == "":
                    break
                with _lock:
                    self._output_lines.append(chunk)
                log_file.write(chunk)
                log_file.flush()
        self.process.wait()
        self.success = account_status(self.name) == "ok"
        callback = None
        with _lock:
            self.done = True
            if self.success and self.on_success:
                callback = self.on_success
        if callback:
            try:
                callback(self.name)
            except Exception as e:
                logger.warning("on_success callback failed for %s: %s", self.name, e)

    def send_input(self, text: str) -> None:
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.write(text + "\n")
            self.process.stdin.flush()

    def output(self) -> str:
        with _lock:
            return "".join(self._output_lines)


def _ensure_config_exists(name: str) -> None:
    check = subprocess.run(
        ["gcloud", "config", "configurations", "describe", name],
        env=_gcloud_env(), capture_output=True, text=True, timeout=20,
    )
    if check.returncode != 0:
        subprocess.run(
            ["gcloud", "config", "configurations", "create", name, "--quiet"],
            env=_gcloud_env(), capture_output=True, text=True, timeout=20, check=True,
        )


def start_login(
    name: str,
    proxy_url: str | None,
    on_success: Callable[[str], None] | None = None,
) -> None:
    validate_name(name)
    _ensure_config_exists(name)
    if proxy_url:
        write_account_proxy_url(name, proxy_url)
    with _lock:
        _login_sessions[name] = LoginSession(name, proxy_url, on_success=on_success)


def login_output(name: str) -> dict:
    session = _login_sessions.get(name)
    if session is None:
        raise KeyError(name)
    return {"output": session.output(), "done": session.done, "success": session.success}


def send_login_input(name: str, text: str) -> None:
    session = _login_sessions.get(name)
    if session is None:
        raise KeyError(name)
    session.send_input(text)


def delete_account(name: str) -> dict:
    """Delete a gcloud configuration and revoke credentials if orphaned.

    1. Checks if configuration is currently active; if so, switches active
       config to 'default' (or another config) before deletion.
    2. Runs `gcloud config configurations delete <name> --quiet`.
    3. Cleans up proxy bindings and in-progress login session.
    4. If the account's associated Google email is not shared by any other
       remaining configuration, revokes the credentials via `gcloud auth revoke`.

    Returns dict e.g. {"warning": ...} if revoke failed non-fatally.
    """
    validate_name(name)

    # Terminate any active login session process
    with _lock:
        session = _login_sessions.pop(name, None)
    if session and session.process and session.process.poll() is None:
        try:
            session.process.terminate()
        except Exception:
            pass

    configs = get_configurations()
    target_cfg = next((c for c in configs if c.get("name") == name), None)

    # If configuration does not exist in gcloud, clean up auxiliary files and exit
    if not target_cfg:
        write_account_proxy_url(name, None)
        return {}

    # Extract associated email before deletion
    target_email = (
        target_cfg.get("properties", {}).get("core", {}).get("account")
        or account_email(name)
    )

    # If this config is active, switch away to another config first
    is_active = bool(target_cfg.get("is_active"))
    if is_active:
        fallback = "default" if any(c.get("name") == "default" and c.get("name") != name for c in configs) else None
        if not fallback:
            other = next((c.get("name") for c in configs if c.get("name") != name), None)
            fallback = other
        if fallback:
            act_res = subprocess.run(
                ["gcloud", "config", "configurations", "activate", fallback],
                env=_gcloud_env(), capture_output=True, text=True, timeout=20,
            )
            if act_res.returncode != 0:
                logger.warning("failed to activate fallback config %s: %s", fallback, act_res.stderr)

    # Perform deletion
    del_res = subprocess.run(
        ["gcloud", "config", "configurations", "delete", name, "--quiet"],
        env=_gcloud_env(), capture_output=True, text=True, timeout=20,
    )
    if del_res.returncode != 0:
        err = del_res.stderr.strip() or del_res.stdout.strip() or f"exit code {del_res.returncode}"
        raise AccountError(f"Failed to delete gcloud configuration {name}: {err}")

    write_account_proxy_url(name, None)

    # Check if target_email is still used by remaining configurations
    warning = None
    if target_email:
        rem_configs = get_configurations()
        still_used = False
        for c in rem_configs:
            if c.get("name") == name:
                continue
            if c.get("properties", {}).get("core", {}).get("account") == target_email:
                still_used = True
                break
        if not still_used:
            rev_res = subprocess.run(
                ["gcloud", "auth", "revoke", target_email, "--quiet"],
                env=_gcloud_env(), capture_output=True, text=True, timeout=20,
            )
            if rev_res.returncode != 0:
                warning = f"配置已删除，但凭证吊销失败: {rev_res.stderr.strip() or 'unknown error'}"
                logger.warning("Revoke failed for %s: %s", target_email, rev_res.stderr)

    return {"warning": warning} if warning else {}
