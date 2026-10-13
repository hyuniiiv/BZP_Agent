import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from login_guard import LoginGuard
from network_auth import is_rejection_text
from settings_store import format_guard_status


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

    def test_status_reports_count_and_blocked(self):
        self.guard.record_failure("id", "pw")
        st = self.guard.status("id", "pw")
        self.assertEqual((st["failures"], st["max_failures"], st["blocked"]), (1, 2, False))
        self.guard.record_failure("id", "pw")
        self.assertTrue(self.guard.status("id", "pw")["blocked"])

    def test_status_is_zero_for_other_credentials(self):
        self.guard.record_failure("id", "old")
        st = self.guard.status("id", "new")
        self.assertEqual((st["failures"], st["last_failure"]), (0, ""))

    def test_reset_clears_blocked_state(self):
        self.guard.record_failure("id", "pw")
        self.guard.record_failure("id", "pw")
        self.guard.reset()
        self.assertFalse(self.guard.is_blocked("id", "pw"))
        self.assertEqual(self.guard.status("id", "pw")["failures"], 0)

    def test_reset_without_state_file_is_noop(self):
        self.guard.reset()


class GuardStatusFormatTest(unittest.TestCase):
    def test_normal_state(self):
        text, blocked = format_guard_status({"failures": 0, "max_failures": 2, "blocked": False, "last_failure": ""})
        self.assertEqual(text, "연속 거부 0회 / 한도 2회")
        self.assertFalse(blocked)

    def test_blocked_state_shows_last_failure(self):
        text, blocked = format_guard_status(
            {"failures": 2, "max_failures": 2, "blocked": True, "last_failure": "2026-10-13T10:00:00"}
        )
        self.assertIn("자동 로그인 중단 중", text)
        self.assertIn("2026-10-13 10:00:00", text)
        self.assertTrue(blocked)


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
