import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web_admin"))
import proxy_pool  # noqa: E402


class TestProxyPool(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = self.tmpdir

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if self._orig is None:
            os.environ.pop("STATE_DIR", None)
        else:
            os.environ["STATE_DIR"] = self._orig

    def test_list_empty_when_no_file(self):
        self.assertEqual(proxy_pool.list_proxies(), [])

    def test_add_and_list(self):
        entry = proxy_pool.add_proxy("HK-1", "socks5://user:pass@1.2.3.4:1080")
        self.assertEqual(entry["label"], "HK-1")
        proxies = proxy_pool.list_proxies()
        self.assertEqual(len(proxies), 1)
        self.assertEqual(proxies[0]["id"], entry["id"])

    def test_rejects_empty_label(self):
        with self.assertRaises(ValueError):
            proxy_pool.add_proxy("  ", "http://1.2.3.4:8080")

    def test_rejects_bad_scheme(self):
        with self.assertRaises(ValueError):
            proxy_pool.add_proxy("bad", "ftp://1.2.3.4:21")

    def test_accepts_http_https_socks5(self):
        proxy_pool.add_proxy("a", "http://1.2.3.4:8080")
        proxy_pool.add_proxy("b", "https://1.2.3.4:8443")
        proxy_pool.add_proxy("c", "socks5://1.2.3.4:1080")
        self.assertEqual(len(proxy_pool.list_proxies()), 3)

    def test_delete_existing(self):
        entry = proxy_pool.add_proxy("HK-1", "socks5://1.2.3.4:1080")
        self.assertTrue(proxy_pool.delete_proxy(entry["id"]))
        self.assertEqual(proxy_pool.list_proxies(), [])

    def test_delete_nonexistent_returns_false(self):
        proxy_pool.add_proxy("HK-1", "socks5://1.2.3.4:1080")
        self.assertFalse(proxy_pool.delete_proxy("does-not-exist"))
        self.assertEqual(len(proxy_pool.list_proxies()), 1)

    def test_get_proxy_url(self):
        entry = proxy_pool.add_proxy("HK-1", "socks5://1.2.3.4:1080")
        self.assertEqual(proxy_pool.get_proxy_url(entry["id"]), "socks5://1.2.3.4:1080")

    def test_get_proxy_url_missing(self):
        self.assertIsNone(proxy_pool.get_proxy_url("nope"))


if __name__ == "__main__":
    unittest.main()
