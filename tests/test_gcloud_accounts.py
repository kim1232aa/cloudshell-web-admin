import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web_admin"))
import gcloud_accounts  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


class TestValidateName(unittest.TestCase):
    def test_accepts_simple_name(self):
        gcloud_accounts.validate_name("acct-a")  # no raise

    def test_rejects_empty(self):
        with self.assertRaises(gcloud_accounts.InvalidAccountName):
            gcloud_accounts.validate_name("")

    def test_rejects_shell_metacharacters(self):
        with self.assertRaises(gcloud_accounts.InvalidAccountName):
            gcloud_accounts.validate_name("acct-a; rm -rf /")

    def test_rejects_too_long(self):
        with self.assertRaises(gcloud_accounts.InvalidAccountName):
            gcloud_accounts.validate_name("a" * 51)


class TestWithFakeGcloud(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_state_dir = os.environ.get("STATE_DIR")
        self._orig_path = os.environ["PATH"]
        os.environ["STATE_DIR"] = self.tmpdir
        os.environ["PATH"] = FIXTURES + os.pathsep + self._orig_path

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        os.environ["PATH"] = self._orig_path
        if self._orig_state_dir is None:
            os.environ.pop("STATE_DIR", None)
        else:
            os.environ["STATE_DIR"] = self._orig_state_dir

    def test_list_account_names(self):
        self.assertEqual(gcloud_accounts.list_account_names(), ["acct-a", "acct-b"])

    def test_account_status_ok(self):
        self.assertEqual(gcloud_accounts.account_status("acct-a"), "ok")

    def test_account_status_broken(self):
        self.assertEqual(gcloud_accounts.account_status("acct-broken"), "auth-invalid")

    def test_account_email(self):
        self.assertEqual(gcloud_accounts.account_email("acct-a"), "fake-acct-a@example.com")

    def test_account_proxy_url_roundtrip(self):
        self.assertIsNone(gcloud_accounts.read_account_proxy_url("acct-a"))
        gcloud_accounts.write_account_proxy_url("acct-a", "socks5://1.2.3.4:1080")
        self.assertEqual(gcloud_accounts.read_account_proxy_url("acct-a"), "socks5://1.2.3.4:1080")
        gcloud_accounts.write_account_proxy_url("acct-a", None)
        self.assertIsNone(gcloud_accounts.read_account_proxy_url("acct-a"))

    def test_current_account_name_absent(self):
        self.assertIsNone(gcloud_accounts.current_account_name())

    def test_current_account_name_present(self):
        with open(os.path.join(self.tmpdir, "current-account"), "w") as f:
            f.write("acct-b")
        self.assertEqual(gcloud_accounts.current_account_name(), "acct-b")

    def test_list_accounts_shape(self):
        accounts = gcloud_accounts.list_accounts()
        self.assertEqual([a["name"] for a in accounts], ["acct-a", "acct-b"])
        self.assertEqual(accounts[0]["status"], "ok")
        self.assertEqual(accounts[0]["email"], "fake-acct-a@example.com")

    def test_is_current_survives_account_list_shrinking(self):
        # regression test: current-account must be matched by name, not by
        # position — otherwise deleting an earlier account in the list
        # silently reassigns "current" to whatever now sits at that index.
        with open(os.path.join(self.tmpdir, "current-account"), "w") as f:
            f.write("acct-b")
        accounts = gcloud_accounts.list_accounts()
        current = [a["name"] for a in accounts if a["is_current"]]
        self.assertEqual(current, ["acct-b"])

    def test_is_current_false_for_all_when_current_account_deleted(self):
        with open(os.path.join(self.tmpdir, "current-account"), "w") as f:
            f.write("acct-deleted")
        accounts = gcloud_accounts.list_accounts()
        self.assertFalse(any(a["is_current"] for a in accounts))


class TestLoginSession(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_state_dir = os.environ.get("STATE_DIR")
        self._orig_path = os.environ["PATH"]
        os.environ["STATE_DIR"] = self.tmpdir
        os.environ["PATH"] = FIXTURES + os.pathsep + self._orig_path

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        os.environ["PATH"] = self._orig_path
        if self._orig_state_dir is None:
            os.environ.pop("STATE_DIR", None)
        else:
            os.environ["STATE_DIR"] = self._orig_state_dir

    def _wait_for(self, predicate, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_successful_login_flow(self):
        gcloud_accounts.start_login("acct-new", None)
        self.assertTrue(self._wait_for(
            lambda: "authorization code" in gcloud_accounts.login_output("acct-new")["output"]
        ))
        gcloud_accounts.send_login_input("acct-new", "GOODCODE")
        self.assertTrue(self._wait_for(
            lambda: gcloud_accounts.login_output("acct-new")["done"]
        ))
        result = gcloud_accounts.login_output("acct-new")
        self.assertTrue(result["success"])
        self.assertIn("You are now logged in.", result["output"])

    def test_failed_login_flow(self):
        gcloud_accounts.start_login("acct-fail", None)
        self.assertTrue(self._wait_for(
            lambda: "authorization code" in gcloud_accounts.login_output("acct-fail")["output"]
        ))
        gcloud_accounts.send_login_input("acct-fail", "BADCODE")
        self.assertTrue(self._wait_for(
            lambda: gcloud_accounts.login_output("acct-fail")["done"]
        ))
        self.assertFalse(gcloud_accounts.login_output("acct-fail")["success"])

    def test_unknown_account_output_raises(self):
        with self.assertRaises(KeyError):
            gcloud_accounts.login_output("never-started")

    def test_proxy_url_persisted_when_provided(self):
        gcloud_accounts.start_login("acct-proxied", "socks5://1.2.3.4:1080")
        self.assertEqual(
            gcloud_accounts.read_account_proxy_url("acct-proxied"), "socks5://1.2.3.4:1080"
        )

    def test_delete_account_clears_proxy_binding(self):
        gcloud_accounts.write_account_proxy_url("acct-a", "socks5://1.2.3.4:1080")
        gcloud_accounts.delete_account("acct-a")
        self.assertIsNone(gcloud_accounts.read_account_proxy_url("acct-a"))


if __name__ == "__main__":
    unittest.main()
