import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web_admin"))
import auth  # noqa: E402


class TestPasswordHashing(unittest.TestCase):
    def test_roundtrip(self):
        hashed = auth.hash_password("correct horse battery staple")
        self.assertTrue(auth.verify_password("correct horse battery staple", hashed))

    def test_wrong_password_rejected(self):
        hashed = auth.hash_password("correct horse battery staple")
        self.assertFalse(auth.verify_password("wrong password", hashed))

    def test_malformed_hash_rejected(self):
        self.assertFalse(auth.verify_password("anything", "not-a-real-hash"))

    def test_two_hashes_of_same_password_differ(self):
        # random salt each time
        a = auth.hash_password("same password")
        b = auth.hash_password("same password")
        self.assertNotEqual(a, b)


class TestSessionCookie(unittest.TestCase):
    def setUp(self):
        self.secret = "test-secret"

    def test_freshly_issued_cookie_is_valid(self):
        cookie = auth.make_session_cookie(self.secret)
        self.assertTrue(auth.verify_session_cookie(self.secret, cookie))

    def test_tampered_signature_rejected(self):
        cookie = auth.make_session_cookie(self.secret)
        payload, _sig = cookie.split(".", 1)
        tampered = f"{payload}.0000000000000000000000000000000000000000000000000000000000000000"
        self.assertFalse(auth.verify_session_cookie(self.secret, tampered))

    def test_wrong_secret_rejected(self):
        cookie = auth.make_session_cookie(self.secret)
        self.assertFalse(auth.verify_session_cookie("different-secret", cookie))

    def test_expired_cookie_rejected(self):
        issued_at = time.time() - (auth.SESSION_TTL_SECONDS + 60)
        cookie = auth.make_session_cookie(self.secret, issued_at=issued_at)
        self.assertFalse(auth.verify_session_cookie(self.secret, cookie))

    def test_garbage_cookie_rejected(self):
        self.assertFalse(auth.verify_session_cookie(self.secret, "not-even-a-cookie"))


class TestLoginRateLimiter(unittest.TestCase):
    def test_locks_after_max_attempts(self):
        limiter = auth.LoginRateLimiter(max_attempts=3, window_seconds=60)
        now = 1000.0
        for _ in range(3):
            limiter.record_failure("1.2.3.4", now=now)
        self.assertTrue(limiter.is_locked("1.2.3.4", now=now))

    def test_not_locked_before_max_attempts(self):
        limiter = auth.LoginRateLimiter(max_attempts=3, window_seconds=60)
        now = 1000.0
        limiter.record_failure("1.2.3.4", now=now)
        limiter.record_failure("1.2.3.4", now=now)
        self.assertFalse(limiter.is_locked("1.2.3.4", now=now))

    def test_window_expires_old_attempts(self):
        limiter = auth.LoginRateLimiter(max_attempts=3, window_seconds=60)
        for _ in range(3):
            limiter.record_failure("1.2.3.4", now=1000.0)
        self.assertFalse(limiter.is_locked("1.2.3.4", now=1000.0 + 61))

    def test_success_clears_attempts(self):
        limiter = auth.LoginRateLimiter(max_attempts=3, window_seconds=60)
        for _ in range(3):
            limiter.record_failure("1.2.3.4", now=1000.0)
        limiter.record_success("1.2.3.4")
        self.assertFalse(limiter.is_locked("1.2.3.4", now=1000.0))

    def test_ips_are_independent(self):
        limiter = auth.LoginRateLimiter(max_attempts=1, window_seconds=60)
        limiter.record_failure("1.1.1.1", now=1000.0)
        self.assertFalse(limiter.is_locked("2.2.2.2", now=1000.0))


if __name__ == "__main__":
    unittest.main()
