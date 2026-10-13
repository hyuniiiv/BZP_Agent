import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from login_guard import LoginGuard
from network_auth import is_rejection_text


class LoginGuardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "data" / "guard.json"
        self.guard = LoginGuard(self.path, max_failures=2)

    def tearDown(self):
        self._tmp.cleanup()

    def test_not_blocked_initially(self):
        self.assertFalse(self.guard.is_blocked("id", "pw"))

    def test_blocked_after_max_failures(self):
        self.guard.record_failure("id", "pw")
        self.assertFalse(self.guard.is_blocked("id", "pw"))
        self.guard.record_failure("id", "pw")
        self.assertTrue(self.guard.is_blocked("id", "pw"))

    def test_changed_password_unblocks(self):
        self.guard.record_failure("id", "old")
        self.guard.record_failure("id", "old")
        self.assertFalse(self.guard.is_blocked("id", "new"))

    def test_failure_count_restarts_for_new_credentials(self):
        self.guard.record_failure("id", "old")
        self.assertEqual(self.guard.record_failure("id", "new"), 1)

    def test_success_resets_counter(self):
        self.guard.record_failure("id", "pw")
        self.guard.record_success()
        self.assertEqual(self.guard.failures("id", "pw"), 0)

    def test_state_persists_across_instances(self):
        self.guard.record_failure("id", "pw")
        self.guard.record_failure("id", "pw")
        self.assertTrue(LoginGuard(self.path, max_failures=2).is_blocked("id", "pw"))

    def test_corrupt_state_file_is_treated_as_empty(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertFalse(self.guard.is_blocked("id", "pw"))

    def test_password_not_stored_in_plaintext(self):
        self.guard.record_failure("id", "s3cret-pw")
        self.assertNotIn("s3cret-pw", self.path.read_text(encoding="utf-8"))

    def test_max_failures_minimum_is_one(self):
        guard = LoginGuard(self.path, max_failures=0)
        guard.record_failure("id", "pw")
        self.assertTrue(guard.is_blocked("id", "pw"))


class RejectionTextTest(unittest.TestCase):
    def test_detects_server_rejection(self):
        self.assertTrue(is_rejection_text("사용자 인증을 위해 인증정보를 입력해주세요. 인증취소 사용자 인증에 실패하였습니다."))

    def test_detects_across_whitespace_and_newlines(self):
        self.assertTrue(is_rejection_text("사용자 인증에\n  실패하였습니다."))

    def test_normal_login_page_is_not_rejection(self):
        self.assertFalse(is_rejection_text("사용자 인증을 위해 인증정보를 입력해주세요. 인증취소"))

    def test_authenticated_page_is_not_rejection(self):
        self.assertFalse(is_rejection_text("인증사용자 : 홍길동 LOGOUT"))


if __name__ == "__main__":
    unittest.main()
