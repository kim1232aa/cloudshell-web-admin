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


if __name__ == "__main__":
    unittest.main()
