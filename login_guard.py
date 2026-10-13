"""
네트워크 인증 계정 잠금 방지 가드.

비밀번호 변경 등으로 로그인이 거부되는데도 자동 재로그인을 반복하면 계정이 잠길 수 있다.
같은 자격증명(ID/PW)으로 연속 실패한 횟수를 파일에 기록해 두고, 한도에 도달하면
자격증명이 바뀔 때까지 자동 로그인 시도를 중단한다.

- 실패 횟수는 파일에 영속 저장 → 앱 재시작/자동 업데이트로 카운터가 초기화되지 않는다.
- 자격증명 지문(해시)이 바뀌면(환경설정에서 재입력 / .env 수정) 자동으로 해제된다.
"""

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MAX_FAILURES = 2
MAX_ALLOWED_FAILURES = 5  # 한도 상한 — 설정값이 이보다 커도 5회로 제한한다
GUARD_FILENAME = "network_auth_guard.json"  # <앱 폴더>/data/ 아래에 저장


def clamp_max_failures(value) -> int:
    """한도를 1~MAX_ALLOWED_FAILURES 범위로 보정 (잘못된 값은 기본값)."""
    try:
        return min(max(1, int(value)), MAX_ALLOWED_FAILURES)
    except (TypeError, ValueError):
        return DEFAULT_MAX_FAILURES


def _fingerprint(user_id: str, password: str) -> str:
    return hashlib.sha256(f"{user_id}\0{password}".encode("utf-8")).hexdigest()


class LoginGuard:
    def __init__(self, path: Path, max_failures: int = DEFAULT_MAX_FAILURES):
        self._path = path
        self._max_failures = clamp_max_failures(max_failures)

    def _load(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning(f"로그인 가드 상태 읽기 실패(초기화로 간주): {e}")
            return {}

    def _save(self, state: dict) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"로그인 가드 상태 저장 실패: {e}")

    def failures(self, user_id: str, password: str) -> int:
        """현재 자격증명으로 누적된 연속 실패 횟수 (자격증명이 바뀌었으면 0)."""
        state = self._load()
        if state.get("fingerprint") != _fingerprint(user_id, password):
            return 0
        return int(state.get("failures", 0))

    def is_blocked(self, user_id: str, password: str) -> bool:
        return self.failures(user_id, password) >= self._max_failures

    def record_failure(self, user_id: str, password: str) -> int:
        """실패 1회 기록 후 누적 횟수 반환."""
        count = self.failures(user_id, password) + 1
        self._save({
            "fingerprint": _fingerprint(user_id, password),
            "failures": count,
            "last_failure": datetime.now().isoformat(timespec="seconds"),
        })
        return count

    def status(self, user_id: str, password: str) -> dict:
        """환경설정 화면 표시용 상태 (현재 자격증명 기준)."""
        state = self._load()
        matches = state.get("fingerprint") == _fingerprint(user_id, password)
        failures = int(state.get("failures", 0)) if matches else 0
        return {
            "failures": failures,
            "max_failures": self._max_failures,
            "blocked": failures >= self._max_failures,
            "last_failure": state.get("last_failure", "") if matches else "",
        }

    def reset(self) -> None:
        """실패 카운터 초기화 (인증 성공 시 / 환경설정의 '초기화' 버튼)."""
        if self._path.exists():
            try:
                self._path.unlink()
            except Exception as e:
                logger.warning(f"로그인 가드 상태 삭제 실패: {e}")

    def record_success(self) -> None:
        """인증 성공 — 카운터 초기화."""
        self.reset()
