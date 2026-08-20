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
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"{PBKDF2_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        iterations_s, salt_b64, digest_b64 = stored_hash.split("$")
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
