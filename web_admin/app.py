"""HTTP server for the cloudshell-web-admin panel.

Plain stdlib http.server — no framework — matching this project's existing
services (subserver.py, watchdog.sh). Routes for accounts/proxies are added
in later tasks; this task wires the server, login, session, and static files.
"""
import json
import os
import sys
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auth  # noqa: E402
import re

import gcloud_accounts
import proxy_pool
import state_paths

APP_DIR = Path(__file__).resolve().parent
ADMIN_PASSWORD_HASH = os.environ["ADMIN_PASSWORD_HASH"]
SESSION_SECRET = os.environ["SESSION_SECRET"]
SESSION_COOKIE = "session"

rate_limiter = auth.LoginRateLimiter()


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", 0))
    if length == 0:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def _has_valid_session(handler: BaseHTTPRequestHandler) -> bool:
    cookie_header = handler.headers.get("Cookie")
    if not cookie_header:
        return False
    cookie = SimpleCookie()
    cookie.load(cookie_header)
    if SESSION_COOKIE not in cookie:
        return False
    return auth.verify_session_cookie(SESSION_SECRET, cookie[SESSION_COOKIE].value)


class Handler(BaseHTTPRequestHandler):
    server_version = "cloudshell-web-admin/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: HTTPStatus, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.end_headers()

    def _require_session(self) -> bool:
        if _has_valid_session(self):
            return True
        self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "not authenticated"})
        return False

    def _require_same_origin(self) -> bool:
        if self.headers.get("X-Requested-With") == "cloudshell-web-admin":
            return True
        self._send_json(HTTPStatus.FORBIDDEN, {"error": "missing X-Requested-With header"})
        return False

    def _client_ip(self) -> str:
        return self.client_address[0]

    def _serve_static(self, path: str) -> None:
        rel = path[len("/static/"):]
        if ".." in rel:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad path"})
            return
        file_path = APP_DIR / "static" / rel
        if not file_path.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        content_type = (
            "text/css" if rel.endswith(".css")
            else "application/javascript" if rel.endswith(".js")
            else "application/octet-stream"
        )
        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/login":
            self._send_html(HTTPStatus.OK, (APP_DIR / "login.html").read_text())
            return
        if path.startswith("/static/"):
            self._serve_static(path)
            return
        if path == "/":
            if not _has_valid_session(self):
                self._redirect("/login")
                return
            self._send_html(HTTPStatus.OK, (APP_DIR / "index.html").read_text())
            return
        if path == "/api/accounts":
            if not self._require_session():
                return
            proxies_by_url = {p["url"]: p for p in proxy_pool.list_proxies()}
            accounts = gcloud_accounts.list_accounts()
            for a in accounts:
                p = proxies_by_url.get(a["proxy_url"])
                a["proxy_label"] = p["label"] if p else a["proxy_url"]
                a["proxy_id"] = p["id"] if p else None
            self._send_json(HTTPStatus.OK, {"accounts": accounts})
            return
        m = re.match(r"^/api/accounts/([a-zA-Z0-9_-]{1,50})/output$", path)
        if m:
            if not self._require_session():
                return
            try:
                self._send_json(HTTPStatus.OK, gcloud_accounts.login_output(m.group(1)))
            except KeyError:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "no login in progress"})
            return
        if path == "/api/proxies":
            if not self._require_session():
                return
            self._send_json(HTTPStatus.OK, {"proxies": proxy_pool.list_proxies()})
            return
        if path == "/api/status":
            if not self._require_session():
                return
            self._send_json(HTTPStatus.OK, self._status_payload())
            return
        if path == "/api/logs":
            if not self._require_session():
                return
            self._send_json(HTTPStatus.OK, {"lines": self._tail_log(200)})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/login":
            self._handle_login()
            return
        if path == "/logout":
            self._handle_logout()
            return
        if path == "/api/accounts":
            if not (self._require_session() and self._require_same_origin()):
                return
            body = _read_json_body(self)
            proxy_url = None
            proxy_id = body.get("proxy_id")
            if proxy_id:
                proxy_url = proxy_pool.get_proxy_url(proxy_id)
            try:
                gcloud_accounts.start_login(body.get("name", ""), proxy_url)
            except gcloud_accounts.InvalidAccountName as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        m = re.match(r"^/api/accounts/([a-zA-Z0-9_-]{1,50})/input$", path)
        if m:
            if not (self._require_session() and self._require_same_origin()):
                return
            body = _read_json_body(self)
            try:
                gcloud_accounts.send_login_input(m.group(1), body.get("text", ""))
            except KeyError:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "no login in progress"})
                return
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if path == "/api/proxies":
            if not (self._require_session() and self._require_same_origin()):
                return
            body = _read_json_body(self)
            try:
                entry = proxy_pool.add_proxy(body.get("label", ""), body.get("url", ""))
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.OK, entry)
            return
        if path == "/api/force-failover":
            if not (self._require_session() and self._require_same_origin()):
                return
            Path(state_paths.force_failover_flag()).touch()
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_DELETE(self) -> None:
        path = self.path.split("?", 1)[0]
        m = re.match(r"^/api/accounts/([a-zA-Z0-9_-]{1,50})$", path)
        if m:
            if not (self._require_session() and self._require_same_origin()):
                return
            gcloud_accounts.delete_account(m.group(1))
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        m = re.match(r"^/api/proxies/([a-zA-Z0-9]{1,32})$", path)
        if m:
            if not (self._require_session() and self._require_same_origin()):
                return
            proxy_pool.delete_proxy(m.group(1))
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_PUT(self) -> None:
        path = self.path.split("?", 1)[0]
        # Rebind an *existing*, already-authenticated account to a different
        # proxy (or back to direct) without touching its gcloud credentials —
        # the only other way to set this is at account-creation time, which
        # doesn't help accounts added via the old CLI flow.
        m = re.match(r"^/api/accounts/([a-zA-Z0-9_-]{1,50})/proxy$", path)
        if m:
            if not (self._require_session() and self._require_same_origin()):
                return
            name = m.group(1)
            try:
                gcloud_accounts.validate_name(name)
            except gcloud_accounts.InvalidAccountName as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            body = _read_json_body(self)
            proxy_id = body.get("proxy_id")
            proxy_url = proxy_pool.get_proxy_url(proxy_id) if proxy_id else None
            gcloud_accounts.write_account_proxy_url(name, proxy_url)
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _status_payload(self) -> dict:
        link_path = Path(state_paths.proxy_link_file())
        link = link_path.read_text().splitlines()[0] if link_path.exists() else None
        name = gcloud_accounts.current_account_name()
        return {"proxy_link": link, "current_account_name": name}

    def _tail_log(self, n: int) -> list[str]:
        log_path = Path(state_paths.watchdog_log_file())
        if not log_path.exists():
            return []
        return log_path.read_text().splitlines()[-n:]

    def _handle_login(self) -> None:
        ip = self._client_ip()
        if rate_limiter.is_locked(ip):
            self._send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "too many attempts, try later"})
            return
        body = _read_json_body(self)
        password = body.get("password", "")
        if not auth.verify_password(password, ADMIN_PASSWORD_HASH):
            rate_limiter.record_failure(ip)
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "wrong password"})
            return
        rate_limiter.record_success(ip)
        cookie_value = auth.make_session_cookie(SESSION_SECRET)
        body_bytes = json.dumps({"ok": True}).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}={cookie_value}; HttpOnly; Secure; SameSite=Lax; "
            f"Path=/; Max-Age={auth.SESSION_TTL_SECONDS}",
        )
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _handle_logout(self) -> None:
        body_bytes = json.dumps({"ok": True}).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Set-Cookie", f"{SESSION_COOKIE}=; HttpOnly; Path=/; Max-Age=0")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
