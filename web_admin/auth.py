"""Password hashing and session-cookie signing for the admin login.

Stdlib only: PBKDF2-HMAC-SHA256 for the password, plain HMAC for the cookie.
No bcrypt/JWT dependency.
"""
import base64
import hashlib
import hmac
import os
import time

PBKDF2_ITERATIONS = 210_000
SESSION_TTL_SECONDS = 12 * 60 * 60


def hash_password(password: str) -> str:
    # ":" separator, not "$": this value normally lives in a compose .env
    # file, and docker compose treats a bare "$word" in .env values as a
    # variable reference to interpolate — a "$"-separated hash would get
    # silently corrupted (unset-variable fragments replaced with "").
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"{PBKDF2_ITERATIONS}:{base64.b64encode(salt).decode()}:{base64.b64encode(digest).decode()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        iterations_s, salt_b64, digest_b64 = stored_hash.split(":")
        iterations = int(iterations_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(actual, expected)


def make_session_cookie(secret: str, issued_at: float | None = None) -> str:
    issued_at = issued_at if issued_at is not None else time.time()
    payload = str(int(issued_at))
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session_cookie(secret: str, cookie_value: str, now: float | None = None) -> bool:
    now = now if now is not None else time.time()
    try:
        payload, sig = cookie_value.split(".", 1)
    except ValueError:
        return False
    expected_sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return False
    try:
        issued_at = int(payload)
    except ValueError:
        return False
    return 0 <= (now - issued_at) < SESSION_TTL_SECONDS


class LoginRateLimiter:
    """Tracks failed login attempts per source IP, in-memory only."""

    def __init__(self, max_attempts: int = 5, window_seconds: int = 300):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = {}

    def is_locked(self, ip: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        attempts = [t for t in self._attempts.get(ip, []) if now - t < self.window_seconds]
        self._attempts[ip] = attempts
        return len(attempts) >= self.max_attempts

    def record_failure(self, ip: str, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        self._attempts.setdefault(ip, []).append(now)

    def record_success(self, ip: str) -> None:
        self._attempts.pop(ip, None)
