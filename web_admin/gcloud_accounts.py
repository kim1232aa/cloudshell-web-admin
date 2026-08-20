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


def current_account_name() -> str | None:
    # watchdog.sh writes the account *name* here (not an array index — a
    # position would silently point at the wrong account after the list
    # shrinks, e.g. when an earlier account gets deleted).
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
    """One in-progress `gcloud auth login --no-browser` subprocess."""

    def __init__(self, name: str, proxy_url: str | None):
        self.name = name
        self._output_lines: list[str] = []
        self.done = False
        self.success = False
        self._log_path = state_paths.gcloud_auth_log_file(name)
        self.process = subprocess.Popen(
            ["gcloud", "auth", "login", "--no-browser", "--quiet"],
            env={**_gcloud_env(proxy_url), "CLOUDSDK_ACTIVE_CONFIG_NAME": name},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_output, daemon=True)
        self._reader.start()

    def _read_output(self) -> None:
        # Read one character at a time, not line-by-line: gcloud's "Enter
        # authorization code:" prompt has no trailing newline, so a
        # line-buffered reader would never surface it until the process
        # produces more output — which never happens until the user (who
        # can't see the prompt yet) sends the code.
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
        with _lock:
            self.done = True

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


def start_login(name: str, proxy_url: str | None) -> None:
    validate_name(name)
    _ensure_config_exists(name)
    if proxy_url:
        write_account_proxy_url(name, proxy_url)
    with _lock:
        _login_sessions[name] = LoginSession(name, proxy_url)


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


def delete_account(name: str) -> None:
    validate_name(name)
    subprocess.run(
        ["gcloud", "config", "configurations", "delete", name, "--quiet"],
        env=_gcloud_env(), capture_output=True, text=True, timeout=20,
    )
    write_account_proxy_url(name, None)
    with _lock:
        _login_sessions.pop(name, None)
