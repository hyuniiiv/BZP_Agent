"""
환경설정 읽기/쓰기 — GUI와 분리된 순수 로직 (단위 검증 가능).

- config.yaml: load-mutate-dump (모델하지 않은 키도 보존)
- .env: 계정은 credentials_dialog.save_credentials 재사용 ("빈 PW = 기존 유지")
- 주기 라벨↔초, GitLab 시각 문자열↔hour/minute 매핑
"""
import sys
from pathlib import Path

import yaml

BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.yaml"

# 앱 표시 이름 (트레이 툴팁·창 제목·매뉴얼 공용)
APP_NAME = "BZP Agent"

# 폴링 주기 드롭다운: (초, 라벨) — 인증/lab 감시용
POLL_OPTIONS = [
    (30, "30초"), (60, "1분"), (120, "2분"), (180, "3분"),
    (300, "5분"), (600, "10분"), (1800, "30분"),
]
_SEC_TO_LABEL = {sec: label for sec, label in POLL_OPTIONS}
_LABEL_TO_SEC = {label: sec for sec, label in POLL_OPTIONS}

# GitLab 동기화 시각: 최대 슬롯 수 + "사용 안 함" 표시값
GIT_TIME_SLOTS = 3
GIT_TIME_NONE = "사용 안 함"


def interval_label(seconds: int) -> str:
    """초 → 드롭다운 라벨. 정확히 없으면 가장 가까운 옵션으로 표시."""
    if seconds in _SEC_TO_LABEL:
        return _SEC_TO_LABEL[seconds]
    nearest = min(POLL_OPTIONS, key=lambda o: abs(o[0] - seconds))
    return nearest[1]


def label_interval(label: str, default: int = 180) -> int:
    """드롭다운 라벨 → 초."""
    return _LABEL_TO_SEC.get(label, default)


def hhmm_options() -> list[str]:
    """GitLab 동기화 시각 드롭다운 목록 ("HH:MM", 10분 간격)."""
    return [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 10, 20, 30, 40, 50)]


def time_to_hhmm(hour: int, minute: int) -> str:
    """hour/minute → "HH:MM". 분은 10분 격자에 맞춰 표시."""
    snapped = min((0, 10, 20, 30, 40, 50), key=lambda m: abs(m - int(minute)))
    return f"{int(hour):02d}:{snapped:02d}"


def hhmm_to_time(text: str) -> tuple[int, int]:
    """"HH:MM" → (hour, minute). 파싱 실패 시 (8, 0)."""
    try:
        h, m = text.split(":")
        return int(h), int(m)
    except Exception:
        return 8, 0


def load_git_times(gs: dict) -> list[str]:
    """git_sync 설정 → GUI용 3개 슬롯 시각 리스트. 부족분은 '사용 안 함'.
    sync_times(신규)가 없으면 sync_hour/sync_minute(구버전)에서 폴백."""
    times = gs.get("sync_times")
    if not times:
        times = [time_to_hhmm(gs.get("sync_hour", 8), gs.get("sync_minute", 0))]
    slots = [str(t) for t in times[:GIT_TIME_SLOTS]]
    while len(slots) < GIT_TIME_SLOTS:
        slots.append(GIT_TIME_NONE)
    return slots


def normalize_git_times(slots) -> list[str]:
    """3개 슬롯 값 → 저장용 sync_times. '사용 안 함'/빈값 제외, 중복 제거, 정렬. 최소 1개 보장."""
    seen, out = set(), []
    for slot in slots or []:
        text = (slot or "").strip()
        if not text or text == GIT_TIME_NONE:
            continue
        h, m = hhmm_to_time(text)
        norm = f"{h:02d}:{m:02d}"
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return sorted(out) if out else ["08:00"]


def _load_yaml() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _dump_yaml(cfg: dict) -> None:
    header = "# 이 파일은 트레이 앱 '환경설정' 화면에서 관리됩니다. 직접 편집도 가능합니다.\n"
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


def load_settings() -> dict:
    """config.yaml + .env에서 현재 설정을 GUI 표시용 dict로."""
    cfg = _load_yaml()
    na = cfg.get("network_auth", {}) or {}
    gs = cfg.get("git_sync", {}) or {}
    ld = cfg.get("lab_dev", {}) or {}

    account_id = ""
    try:
        from credentials_dialog import load_current_id
        account_id = load_current_id()
    except Exception:
        pass

    peak_windows = na.get("peak_windows") or []
    return {
        "account_id": account_id,
        "auth_enabled": bool(na.get("enabled", True)),
        "auth_url": na.get("url", ""),
        "auth_interval": int(na.get("check_interval", 180)),
        "auth_peak_interval": int(na.get("peak_interval", 60)),
        "auth_peak_window": peak_windows[0] if peak_windows else "",
        "git_enabled": bool(gs.get("enabled", True)),
        "git_repo": gs.get("repo", ""),
        "git_times": load_git_times(gs),
        "lab_enabled": bool(ld.get("enabled", True)),
        "lab_repo": ld.get("repo", ""),
        "lab_url": ld.get("url", ""),
        "lab_interval": int(ld.get("check_interval", 60)),
        "lab_show_console": bool(ld.get("show_console", False)),
    }


def save_settings(values: dict) -> None:
    """GUI 값 dict를 config.yaml(+선택적으로 .env 계정)에 반영.
    load-mutate-dump로 모델하지 않은 키를 보존한다. 저장 실패 시 예외를 던진다
    (호출자는 실패 시 재시작하지 말 것)."""
    cfg = _load_yaml()

    na = cfg.setdefault("network_auth", {})
    na["enabled"] = bool(values["auth_enabled"])
    na["url"] = values["auth_url"]
    na["check_interval"] = int(values["auth_interval"])
    na["peak_interval"] = int(values["auth_peak_interval"])
    window = (values.get("auth_peak_window") or "").strip()
    na["peak_windows"] = [window] if window else []

    gs = cfg.setdefault("git_sync", {})
    gs["enabled"] = bool(values["git_enabled"])
    gs["repo"] = values["git_repo"]
    gs["sync_times"] = normalize_git_times(values.get("git_times", []))
    # 단일 시각(구버전) 키는 sync_times로 대체되므로 제거
    gs.pop("sync_hour", None)
    gs.pop("sync_minute", None)

    ld = cfg.setdefault("lab_dev", {})
    ld["enabled"] = bool(values["lab_enabled"])
    ld["repo"] = values["lab_repo"]
    ld["url"] = values["lab_url"]
    ld["check_interval"] = int(values["lab_interval"])
    ld["show_console"] = bool(values["lab_show_console"])

    _dump_yaml(cfg)

    # 계정: ID가 입력된 경우에만. "빈 PW = 기존 유지" 규칙 보존.
    account_id = (values.get("account_id") or "").strip()
    if account_id:
        from credentials_dialog import save_credentials, load_current_pw
        pw = values.get("account_pw") or load_current_pw()
        save_credentials(account_id, pw)
