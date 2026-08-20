import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web_admin"))
import state_paths  # noqa: E402


class TestStatePaths(unittest.TestCase):
    def setUp(self):
        self._orig = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = "/tmp/test-state"

    def tearDown(self):
        if self._orig is None:
            os.environ.pop("STATE_DIR", None)
        else:
            os.environ["STATE_DIR"] = self._orig

    def test_gcloud_config_dir(self):
        self.assertEqual(state_paths.gcloud_config_dir(), "/tmp/test-state/gcloud")

    def test_proxies_file(self):
        self.assertEqual(state_paths.proxies_file(), "/tmp/test-state/proxies.json")

    def test_account_proxy_file(self):
        self.assertEqual(state_paths.account_proxy_file("acct-a"), "/tmp/test-state/proxy-acct-a")

    def test_current_account_file(self):
        self.assertEqual(state_paths.current_account_file(), "/tmp/test-state/current-account")

    def test_proxy_link_file(self):
        self.assertEqual(state_paths.proxy_link_file(), "/tmp/test-state/proxy-link.txt")

    def test_watchdog_log_file(self):
        self.assertEqual(state_paths.watchdog_log_file(), "/tmp/test-state/watchdog.log")

    def test_force_failover_flag(self):
        self.assertEqual(
            state_paths.force_failover_flag(), "/tmp/test-state/force-failover-requested"
        )

    def test_gcloud_auth_log_file(self):
        self.assertEqual(
            state_paths.gcloud_auth_log_file("acct-a"), "/tmp/test-state/gcloud-auth-acct-a.log"
        )

    def test_default_state_dir_when_unset(self):
        del os.environ["STATE_DIR"]
        self.assertEqual(state_paths.proxies_file(), "/state/proxies.json")


if __name__ == "__main__":
    unittest.main()
