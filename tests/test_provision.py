import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web_admin"))
import provision  # noqa: E402
import state_paths  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


class TestProvision(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_state_dir = os.environ.get("STATE_DIR")
        self._orig_path = os.environ["PATH"]
        os.environ["STATE_DIR"] = self.tmpdir
        os.environ["PATH"] = FIXTURES + os.pathsep + self._orig_path

        # Seed minimal bundle files in provision cache
        self.cache_dir = state_paths.provision_cache_dir()
        os.makedirs(self.cache_dir, exist_ok=True)
        for req in provision.REQUIRED_BUNDLE_FILES:
            with open(os.path.join(self.cache_dir, req), "w") as f:
                f.write(f"fake-{req}-content\n")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        os.environ["PATH"] = self._orig_path
        os.environ.pop("FAKE_FAIL_SSH", None)
        os.environ.pop("FAKE_FAIL_SCP", None)
        os.environ.pop("FAKE_PROVISION_FAIL", None)
        if self._orig_state_dir is None:
            os.environ.pop("STATE_DIR", None)
        else:
            os.environ["STATE_DIR"] = self._orig_state_dir

    def test_ensure_bundle_success_when_present(self):
        cache = provision.ensure_bundle()
        self.assertTrue(cache.is_dir())

    def test_ensure_bundle_fails_when_missing_and_no_source(self):
        os.remove(os.path.join(self.cache_dir, "uuid"))
        with self.assertRaises(provision.ProvisionError):
            provision.ensure_bundle()

    def test_create_provision_archive_structure(self):
        archive_path = os.path.join(self.tmpdir, "test.tar.gz")
        provision.create_provision_archive(target_is_standby=True, output_tar=archive_path)
        self.assertTrue(os.path.exists(archive_path))
        import tarfile
        with tarfile.open(archive_path, "r:gz") as tar:
            names = tar.getnames()
            self.assertIn("run.sh", names)
            self.assertIn("configs/uuid", names)
            self.assertIn("configs/cf-hostname", names)

    def test_run_provision_success(self):
        provision.run_provision("acct-b")
        # should complete without exception

    def test_start_provision_flow_records_status_and_logs(self):
        provision.start_provision("acct-b")
        # Wait up to 5s for worker thread to finish
        deadline = time.time() + 5.0
        while time.time() < deadline:
            st = provision.get_provision_status("acct-b")
            if st.get("state") == "ok":
                break
            time.sleep(0.05)

        st = provision.get_provision_status("acct-b")
        self.assertEqual(st.get("state"), "ok")
        log = provision.get_provision_log("acct-b")
        self.assertIn("Provisioning completed successfully!", log)

    def test_start_provision_detects_failure(self):
        os.environ["FAKE_PROVISION_FAIL"] = "1"
        provision.start_provision("acct-fail")
        deadline = time.time() + 5.0
        while time.time() < deadline:
            st = provision.get_provision_status("acct-fail")
            if st.get("state") == "failed":
                break
            time.sleep(0.05)

        st = provision.get_provision_status("acct-fail")
        self.assertEqual(st.get("state"), "failed")
        self.assertIn("error", st)

    def test_parse_remote_check_output(self):
        out = "noise\nGCS_CHECK installed=1 cloudflared=2 supervise=1 link=1\nother"
        parsed = provision.parse_remote_check_output(out)
        self.assertIsNotNone(parsed)
        self.assertTrue(parsed["installed"])
        self.assertTrue(parsed["cloudflared_running"])
        self.assertTrue(parsed["supervise_running"])
        self.assertTrue(parsed["has_proxy_link"])

    def test_parse_remote_check_output_not_installed(self):
        out = "GCS_CHECK installed=0 cloudflared=0 supervise=0 link=0"
        parsed = provision.parse_remote_check_output(out)
        self.assertFalse(parsed["installed"])
        self.assertFalse(parsed["cloudflared_running"])

    def test_remote_check_flow_installed(self):
        provision.start_remote_check("acct-b")
        deadline = time.time() + 5.0
        while time.time() < deadline:
            res = provision.get_remote_check("acct-b")
            if res.get("state") in ("installed", "not_installed", "unreachable"):
                break
            time.sleep(0.05)
        res = provision.get_remote_check("acct-b")
        self.assertEqual(res.get("state"), "installed")
        self.assertTrue(res.get("cloudflared_running"))


if __name__ == "__main__":
    unittest.main()
