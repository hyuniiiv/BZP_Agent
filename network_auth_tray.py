"""
네트워크 인증 자동화 — Windows 시스템 트레이 앱
network_auth.py 의 NetworkAuthenticator 를 시스템 트레이(우측 하단)에서 상시 구동합니다.

실행: pythonw.exe network_auth_tray.py  (콘솔 창 없이 실행)
자동 시작 등록: install_autostart.py 참고
"""
import asyncio
import logging
import os
import sys
import threading
import time
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 로그 파일 무한 증가 방지: 파일당 최대 크기 × 보관 개수
LOG_MAX_BYTES = 2 * 1024 * 1024  # 2MB
LOG_BACKUP_COUNT = 3

# PyInstaller로 패키징된 exe에서는 실행파일이 위치한 폴더를 기준으로 삼는다
# (개발 중에는 이 .py 파일이 있는 폴더 기준).
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
    # PyInstaller로 얼린 상태에서는 Playwright 자체의 "기본 브라우저 경로" 판단 로직이
    # 번들 내부 경로 기준으로 바뀌어버려서, 아무것도 안 정하면 시스템 전역 캐시를 못 찾는다.
    # 그래서 항상 명시적으로 지정한다:
    #   - 동봉한 ms-playwright 폴더가 있으면 그걸 사용 (완전 오프라인 설치)
    #   - 없으면 (설치 스크립트가 "이미 있다"고 판단해 번들을 뺀 경우) 시스템 전역 위치를 직접 지정
    _bundled_browsers = BASE_DIR / "ms-playwright"
    if _bundled_browsers.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_bundled_browsers)
    else:
        _system_cache = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "ms-playwright"
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_system_cache)
else:
    BASE_DIR = Path(__file__).parent

import pystray
import yaml
from dotenv import load_dotenv
from PIL import Image, ImageDraw

from network_auth import NetworkAuthenticator
from settings_store import APP_NAME
from version import APP_VERSION
import git_sync
import lab_dev
import updater
import manual

# 업데이트 확인 주기(초): 시작 시 1회 + 이후 하루 1회
UPDATE_CHECK_INTERVAL = 24 * 60 * 60

LOG_FILE = BASE_DIR / "logs" / "network_auth_tray.log"
LOG_FILE.parent.mkdir(exist_ok=True)
GIT_SYNC_LOG = BASE_DIR / "logs" / "git_sync.log"
LAB_DEV_LOG = BASE_DIR / "logs" / "lab_dev.log"            # 감시 이벤트(Python 로깅)
LAB_DEV_SERVER_LOG = BASE_DIR / "logs" / "lab_dev_server.log"  # pnpm dev 서버 stdout/stderr
ENV_FILE = BASE_DIR / ".env"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8")],
)
logger = logging.getLogger("network_auth_tray")


def _attach_file_logger(name: str, path: Path) -> logging.Logger:
    """부가 기능(git_sync/lab_dev)의 로그를 각자 파일로 분리 기록."""
    lg = logging.getLogger(name)
    handler = RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    lg.addHandler(handler)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    return lg


_attach_file_logger("git_sync", GIT_SYNC_LOG)
_attach_file_logger("lab_dev", LAB_DEV_LOG)

_ICON_COLORS = {
    "ok": (46, 204, 113, 255),       # 초록 — 인증됨
    "fail": (231, 76, 60, 255),      # 빨강 — 인증 실패
    "checking": (241, 196, 15, 255), # 노랑 — 확인 중
    "idle": (149, 165, 166, 255),    # 회색 — 운영시간 외 대기
}


def load_config() -> dict:
    load_dotenv(ENV_FILE, override=True)
    with open(BASE_DIR / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_credentials(user_id: str, password: str):
    """NETWORK_ID/NETWORK_PW를 .env에 저장 (다른 줄은 보존, BOM 없이 저장)."""
    lines = []
    seen_id = seen_pw = False
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("NETWORK_ID="):
                lines.append(f"NETWORK_ID={user_id}")
                seen_id = True
            elif line.startswith("NETWORK_PW="):
                lines.append(f"NETWORK_PW={password}")
                seen_pw = True
            else:
                lines.append(line)
    if not seen_id:
        lines.append(f"NETWORK_ID={user_id}")
    if not seen_pw:
        lines.append(f"NETWORK_PW={password}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ["NETWORK_ID"] = user_id
    os.environ["NETWORK_PW"] = password
    logger.info("계정 설정 저장 완료 (NETWORK_ID/NETWORK_PW)")


def _ensure_shortcuts():
    """시작 폴더 + 시작 메뉴 바로가기를 현재 exe로 (재)생성한다.
    앱이 직접 하므로 설치 스크립트 없이 '자동 업데이트'만으로 전파된다.
    콘솔 없이 PowerShell로 .lnk 생성(추가 의존성 없음). 개발 모드에선 스킵."""
    if not getattr(sys, "frozen", False):
        return
    import subprocess
    exe = str(Path(sys.executable))
    workdir = str(BASE_DIR)
    ps = (
        "$w=New-Object -ComObject WScript.Shell;"
        "foreach($p in @("
        "(Join-Path ([Environment]::GetFolderPath('Startup')) 'BZP_Agent.lnk'),"
        "(Join-Path ([Environment]::GetFolderPath('Programs')) 'BZP Agent.lnk'))){"
        f"$s=$w.CreateShortcut($p);$s.TargetPath='{exe}';$s.WorkingDirectory='{workdir}';"
        "$s.Description='BZP Agent 트레이 앱';$s.Save()}"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            creationflags=0x08000000, close_fds=True,  # CREATE_NO_WINDOW
        )
        logger.info("바로가기(시작폴더/시작메뉴) 보장 완료")
    except Exception as e:
        logger.debug(f"바로가기 보장 실패(무시): {e}")


class TrayState:
    def __init__(self):
        self.status = "checking"
        self.last_checked: datetime | None = None
        self.last_message = "시작 중..."
        self.busy = False  # True인 동안은 경량 루프가 상태를 덮어쓰지 않음
        # self.status = 인증(network_auth) 상태: checking/ok/fail/idle/off
        # 부가 기능 상태 (트레이 메뉴 표시용 + 아이콘 종합 계산용)
        self.git_last: datetime | None = None
        self.git_message = "대기 중"
        self.lab_message = "대기 중"
        self.git_status = "off"   # off/idle/ok/attention
        self.lab_status = "off"   # off/idle/ok/error
        # 자동 업데이트: 새 버전 감지 시 {"version","url","notes"} 저장
        self.update_info: dict | None = None


def compute_icon_status(state: "TrayState") -> str:
    """인증/git/lab/업데이트 상태를 종합해 아이콘 색상 키를 결정한다.
    빨강(fail) > 노랑(checking) > 초록(ok) > 회색(idle=모든 기능 꺼짐) 우선순위."""
    # 1) 어느 기능이든 문제 → 빨강
    if state.status == "fail" or state.git_status == "attention" or state.lab_status == "error":
        return "fail"
    # 2) 인증 확인 중 또는 새 버전 있음 → 노랑
    if state.status == "checking" or state.update_info:
        return "checking"
    # 3) 활성 기능 중 하나라도 정상/동작 중 → 초록 (인증 안 써도 git·lab 정상이면 초록)
    if state.status in ("ok", "idle") or state.git_status in ("ok", "idle") or state.lab_status in ("ok", "idle"):
        return "ok"
    # 4) 모든 기능 꺼짐 → 회색
    return "idle"


def make_icon_image(status: str) -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, size - 4, size - 4), fill=_ICON_COLORS.get(status, _ICON_COLORS["idle"]))
    return img


def update_icon(icon: pystray.Icon, state: TrayState):
    icon.icon = make_icon_image(compute_icon_status(state))
    icon.title = f"{APP_NAME} v{APP_VERSION} · {state.last_message}"
    # pystray 메뉴의 동적 텍스트는 update_menu() 호출 시에만 재평가된다
    try:
        icon.update_menu()
    except Exception:
        pass


def _lab_status_label(result: lab_dev.LabDevResult) -> str:
    """lab dev 감시 결과를 트레이 메뉴용 짧은 라벨로 변환."""
    labels = {"up": "정상", "restarted": "재실행함", "grace": "부팅 대기"}
    if result.action == "error":
        return f"오류: {result.message}"
    return labels.get(result.action, result.message)


def _parse_peak_windows(specs: list) -> list[tuple[int, int]]:
    """["HH:MM-HH:MM", ...] → [(시작분, 끝분), ...]. 잘못된 항목은 건너뛴다."""
    result: list[tuple[int, int]] = []
    for spec in specs or []:
        try:
            start_s, end_s = str(spec).split("-")
            sh, sm = (int(x) for x in start_s.strip().split(":"))
            eh, em = (int(x) for x in end_s.strip().split(":"))
            result.append((sh * 60 + sm, eh * 60 + em))
        except Exception:
            continue
    return result


async def do_check(authenticator: NetworkAuthenticator, icon: pystray.Icon, state: TrayState, notify: bool = True):
    """완전 체크 — 필요 시 브라우저를 띄워 실제 재로그인까지 수행."""
    state.busy = True
    state.status = "checking"
    state.last_message = "확인 중..."
    update_icon(icon, state)
    try:
        ok = await authenticator.check_and_login()
    except Exception as e:
        logger.error(f"인증 확인 오류: {e}")
        ok = False
        state.last_message = f"오류: {e}"
    else:
        state.last_message = "인증됨" if ok else "인증 실패 — 수동 확인 필요"

    state.status = "ok" if ok else "fail"
    state.last_checked = datetime.now()
    state.busy = False
    update_icon(icon, state)
    logger.info(f"인증 확인 결과: {state.last_message}")

    if notify and not ok:
        try:
            icon.notify("네트워크 인증에 실패했습니다. 수동 확인이 필요합니다.", "네트워크 인증 실패")
        except Exception:
            pass


async def monitor_loop(authenticator: NetworkAuthenticator, config: dict, icon: pystray.Icon, state: TrayState, interval: int = 60):
    """운영시간(run_hour_start~run_hour_end) 내에서만 경량 상태 체크(브라우저 미기동)를 짧은 주기로 수행.
    "인증 끊어짐"이 감지되면 즉시 완전 체크(do_check, 필요 시 브라우저 재로그인)를 실행한다.
    운영시간 외에는 체크 자체를 하지 않고 대기만 한다."""
    auth_cfg = config.get("network_auth", {})
    if not auth_cfg.get("enabled", True):
        state.status = "off"
        state.last_message = "인증 사용 안 함"
        update_icon(icon, state)
        logger.info("네트워크 인증 비활성화됨 (enabled=false) — 인증 루프 미실행")
        return
    run_hour_start = auth_cfg.get("run_hour_start", 0)
    run_hour_end = auth_cfg.get("run_hour_end", 24)
    base_interval = interval
    peak_interval = int(auth_cfg.get("peak_interval", interval))
    peak_windows = _parse_peak_windows(auth_cfg.get("peak_windows", []))

    def _in_operating_hours() -> bool:
        return run_hour_start <= datetime.now().hour < run_hour_end

    def _current_interval() -> int:
        """현재 시각이 민감창(peak_windows) 안이면 peak_interval, 아니면 base_interval."""
        now = datetime.now()
        minutes = now.hour * 60 + now.minute
        for start, end in peak_windows:
            if start <= minutes <= end:
                return peak_interval
        return base_interval

    # 시작 즉시 1회 완전 체크 (운영시간과 무관하게 항상 수행 —
    # 시작 직후 회색 대기 대신 실제 인증 상태(초록/빨강)를 바로 표시)
    await do_check(authenticator, icon, state)

    while True:
        if not _in_operating_hours():
            if not state.busy:
                state.status = "idle"
                state.last_message = f"대기 중 (운영시간 {run_hour_start}~{run_hour_end}시)"
                update_icon(icon, state)
            await asyncio.sleep(300)  # 운영시간 진입 여부를 5분마다 재확인
            continue

        await asyncio.sleep(_current_interval())
        if state.busy or not _in_operating_hours():
            continue

        try:
            result = await authenticator.check_status_only()
        except Exception as e:
            logger.debug(f"경량 상태 체크 오류: {e}")
            result = None
        if state.busy:  # 대기 중 다른 완전 체크가 이미 시작됐으면 그 결과를 덮어쓰지 않음
            continue

        if result is True:
            state.status = "ok"
            state.last_message = "인증됨"
        elif result is False:
            logger.info("경량 체크에서 인증 끊어짐 감지 — 즉시 재로그인 시도")
            await do_check(authenticator, icon, state)
            continue  # do_check가 이미 상태/아이콘/시각을 갱신함
        else:
            state.status = "idle"
            state.last_message = "상태 확인 불가 (네트워크 오류)"

        state.last_checked = datetime.now()
        update_icon(icon, state)
        logger.info(f"경량 상태 체크 결과: {state.last_message}")


def build_menu(
    state: TrayState,
    on_check_now,
    on_set_credentials,
    on_open_logs,
    on_quit,
    on_git_sync_now,
    on_lab_check_now,
    on_update_now,
    on_check_update,
    on_open_manual,
) -> pystray.Menu:
    def status_text(_item):
        return f"인증: {state.last_message}"

    def time_text(_item):
        ts = state.last_checked.strftime("%H:%M:%S") if state.last_checked else "-"
        return f"마지막 확인: {ts}"

    def git_text(_item):
        ts = state.git_last.strftime("%m/%d %H:%M") if state.git_last else "-"
        return f"GitLab: {state.git_message} ({ts})"

    def lab_text(_item):
        return f"lab 3003: {state.lab_message}"

    def version_text(_item):
        if state.update_info:
            return f"🔔 새 버전 v{state.update_info['version']} 있음 (현재 v{APP_VERSION})"
        return f"버전 v{APP_VERSION} (최신)"

    def has_update(_item):
        return state.update_info is not None

    return pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.MenuItem(time_text, None, enabled=False),
        pystray.MenuItem(git_text, None, enabled=False),
        pystray.MenuItem(lab_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("지금 인증 확인", on_check_now),
        pystray.MenuItem("지금 GitLab 동기화", on_git_sync_now),
        pystray.MenuItem("지금 lab 서버 확인", on_lab_check_now),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("환경설정...", on_set_credentials),
        pystray.MenuItem("사용 설명서", on_open_manual),
        pystray.MenuItem("로그 폴더 열기", on_open_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(version_text, None, enabled=False),
        pystray.MenuItem("지금 업데이트", on_update_now, visible=has_update),
        pystray.MenuItem("업데이트 확인", on_check_update),
        pystray.MenuItem("종료", on_quit),
    )


def main():
    config = load_config()
    state = TrayState()
    authenticator = NetworkAuthenticator(config)
    loop_holder: dict = {}
    git_cfg = config.get("git_sync", {}) or {}
    lab_cfg = config.get("lab_dev", {}) or {}
    # 아이콘 종합 계산용 초기 상태: 꺼진 기능은 off, 켜진 기능은 결과 나오기 전 idle
    state.git_status = "idle" if git_cfg.get("enabled", False) else "off"
    state.lab_status = "idle" if lab_cfg.get("enabled", False) else "off"
    git_lock = threading.Lock()  # 스케줄러/메뉴 동시 실행 방지 (index.lock 충돌 방지)

    # lab dev 모니터는 미리 생성 — 시작 시 첫 동기화가 변경을 반영하면 즉시 재기동할 수 있어야 함
    lab_monitor: lab_dev.LabDevMonitor | None = None
    if lab_cfg.get("enabled", False):
        lab_monitor = lab_dev.LabDevMonitor(
            repo=lab_cfg.get("repo", ""),
            url=lab_cfg.get("url", "http://localhost:3003"),
            ports=lab_cfg.get("ports", [3003, 4401]),
            command=lab_cfg.get("command", "pnpm lab dev"),
            log_path=LAB_DEV_SERVER_LOG,
            show_console=lab_cfg.get("show_console", False),
        )

    icon = pystray.Icon(
        "network_auth",
        make_icon_image("checking"),
        f"{APP_NAME} · 시작 중...",
    )

    def on_check_now(_icon, _item):
        loop = loop_holder.get("loop")
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(do_check(authenticator, icon, state, notify=False), loop)

    def on_set_credentials(_icon, _item):
        """계정 설정 창을 완전히 별도 프로세스로 띄운다.
        pystray(트레이)와 tkinter가 같은 스레드의 메시지 루프를 공유하면
        키보드 포커스가 제대로 전달되지 않는 문제가 있어, 별도 프로세스로 분리했다."""
        import subprocess
        if getattr(sys, "frozen", False):
            # 패키징된 exe에서는 자기 자신을 --credentials-dialog 인자로 재실행
            subprocess.Popen([sys.executable, "--credentials-dialog"], cwd=str(BASE_DIR))
        else:
            subprocess.Popen(
                [sys.executable, str(BASE_DIR / "credentials_dialog.py")],
                cwd=str(BASE_DIR),
            )

    def restart_self():
        """환경설정 변경 후 트레이 재시작.
        동기화 stash 창 한가운데서 죽으면 orphan stash가 생기므로,
        git_lock을 확보(=지금 동기화 중이 아님)한 뒤에만 재시작한다."""
        import subprocess
        if not git_lock.acquire(timeout=30):
            notify("환경설정", "동기화 중입니다. 잠시 후 다시 저장하세요.")
            return
        logger.info("환경설정 변경 — 트레이 재시작")
        args = ([sys.executable] if getattr(sys, "frozen", False)
                else [sys.executable, str(BASE_DIR / "network_auth_tray.py")])
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — 새 프로세스가 부모와 함께 죽지 않도록
        subprocess.Popen(args, cwd=str(BASE_DIR), creationflags=(0x00000008 | 0x00000200), close_fds=True)
        icon.stop()  # 현재 트레이 종료 (git_lock은 프로세스 종료로 자연 해제)

    def on_open_settings(_icon, _item):
        """환경설정 다이얼로그를 별도 프로세스로 띄우고, 저장(종료코드 42) 시 트레이 재시작."""
        import subprocess

        def _do():
            args = ([sys.executable, "--settings-dialog"] if getattr(sys, "frozen", False)
                    else [sys.executable, str(BASE_DIR / "network_auth_tray.py"), "--settings-dialog"])
            result = subprocess.run(args, cwd=str(BASE_DIR))
            if result.returncode == 42:
                restart_self()

        threading.Thread(target=_do, daemon=True).start()

    def on_open_logs(_icon, _item):
        os.startfile(str(LOG_FILE.parent))

    def on_open_manual(_icon, _item):
        try:
            manual.write_and_open(BASE_DIR)
        except Exception as e:
            logger.error(f"사용 설명서 열기 실패: {e}")

    def on_quit(icon_, _item):
        logger.info("종료 요청 수신")
        loop = loop_holder.get("loop")
        if loop and loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(authenticator.close(), loop)
            try:
                fut.result(timeout=5)
            except Exception:
                pass
            loop.call_soon_threadsafe(loop.stop)
        icon_.stop()

    def notify(title: str, message: str):
        try:
            icon.notify(message, title)
        except Exception:
            pass

    def refresh():
        """상태 변경 후 아이콘 색(종합)과 메뉴 텍스트를 즉시 다시 그린다."""
        try:
            update_icon(icon, state)
        except Exception:
            pass

    def run_git_sync(notify_ok: bool = False):
        """GitLab 동기화 실행. 락으로 중복 실행 방지, 결과를 상태/알림에 반영."""
        if not git_lock.acquire(blocking=False):
            logger.info("GitLab 동기화 이미 진행 중 — 건너뜀")
            return
        try:
            state.git_message = "동기화 중..."
            refresh()
            result = git_sync.sync(
                git_cfg.get("repo", ""),
                git_cfg.get("remote_ref", "origin/main"),
            )
            state.git_last = datetime.now()
            state.git_message = result.message
            state.git_status = "attention" if result.needs_attention else ("ok" if result.ok else "idle")
            refresh()
            logging.getLogger("git_sync").info(f"결과: {result}")
            if result.needs_attention:
                notify("GitLab 동기화 — 수동 확인 필요", result.message)
            elif result.ok and notify_ok:
                notify("GitLab 동기화", result.message)
            # 실제 변경이 반영됐을 때만 dev 서버 재기동 (HMR이 새 파일·삭제·의존성 변경은 못 따라잡음)
            if result.ok and result.changed and lab_monitor is not None:
                restart = lab_monitor.ensure_running(force=True, reason="GitLab 변경 반영")
                state.lab_message = _lab_status_label(restart)
                state.lab_status = "error" if restart.action == "error" else "ok"
                refresh()
                notify("변경 반영 — lab 재기동", restart.message)
        except Exception as e:
            # git 미설치·타임아웃 등 예기치 못한 오류로 스케줄러 스레드가 죽지 않도록 흡수
            logging.getLogger("git_sync").error(f"동기화 중 예외: {e}")
            state.git_message = f"오류: {e}"
            state.git_status = "idle"
            refresh()
        finally:
            git_lock.release()

    def on_git_sync_now(_icon, _item):
        threading.Thread(target=lambda: run_git_sync(notify_ok=True), daemon=True).start()

    def on_lab_check_now(_icon, _item):
        if lab_monitor is None:
            return

        def _do():
            result = lab_monitor.ensure_running(force=True)
            state.lab_message = _lab_status_label(result)
            state.lab_status = "error" if result.action == "error" else "ok"
            refresh()
            notify("lab 서버", result.message)

        threading.Thread(target=_do, daemon=True).start()

    def git_sync_scheduler():
        """매일 지정 시각(sync_times)에 1회 동기화. (앱 시작 시 동기화는 하지 않음)"""
        if not git_cfg.get("enabled", False):
            return
        # 동기화 시각 목록 (신규 sync_times, 없으면 구버전 sync_hour/sync_minute 폴백)
        sync_times = git_cfg.get("sync_times")
        if not sync_times:
            sh = int(git_cfg.get("sync_hour", 8))
            sm = int(git_cfg.get("sync_minute", 0))
            sync_times = [f"{sh:02d}:{sm:02d}"]
        done: set = set()  # (날짜, "HH:MM") 실행 완료 기록 — 시각별로 하루 1회 보장
        while True:
            time.sleep(30)
            now = datetime.now()
            hm = f"{now.hour:02d}:{now.minute:02d}"
            key = (now.date(), hm)
            if hm in sync_times and key not in done:
                done.add(key)
                run_git_sync()

    def lab_dev_watch():
        """lab dev(3003) 주기 감시 — 죽어 있으면 포트 정리 후 재실행."""
        if lab_monitor is None:
            return
        interval = int(lab_cfg.get("check_interval", 60))
        while True:
            result = lab_monitor.ensure_running()
            state.lab_message = _lab_status_label(result)
            state.lab_status = "error" if result.action == "error" else "ok"
            refresh()
            if result.action == "restarted":
                notify("lab 서버 재실행", result.message)
            time.sleep(interval)

    def _apply_update():
        info = state.update_info
        if not info:
            return
        notify("업데이트", f"v{info['version']} 다운로드 중...")
        ok = updater.download_and_apply(info["asset_url"], BASE_DIR, exe_name="BZP_Agent.exe")
        if ok:
            # 업데이터(PowerShell)가 이 프로세스 종료를 기다렸다가 교체 후 재시작한다
            logger.info("업데이트 적용 시작 — 앱 종료")
            on_quit(icon, None)
        else:
            notify("업데이트 실패", "잠시 후 다시 시도하거나 수동으로 설치해주세요.")

    def on_update_now(_icon, _item):
        threading.Thread(target=_apply_update, daemon=True).start()

    def _run_update_check(notify_result: bool = False):
        info = updater.check_latest()
        if info:
            state.update_info = info
            refresh()
            notify(f"새 버전 v{info['version']}", "트레이 메뉴 → '지금 업데이트'로 설치하세요.")
        elif notify_result:
            notify("업데이트 확인", f"현재 최신 버전입니다 (v{APP_VERSION}).")

    def on_check_update(_icon, _item):
        threading.Thread(target=lambda: _run_update_check(notify_result=True), daemon=True).start()

    def update_check_scheduler():
        """앱 시작 시 1회 + 이후 하루 1회 최신 버전 자동 확인 (환경설정에서 끄면 미실행).
        꺼져 있어도 메뉴의 '업데이트 확인'(수동)은 항상 동작한다."""
        if not (config.get("update", {}) or {}).get("enabled", True):
            logger.info("자동 업데이트 확인 비활성화됨 (enabled=false) — 수동 확인만 가능")
            return
        while True:
            _run_update_check()
            time.sleep(UPDATE_CHECK_INTERVAL)

    icon.menu = build_menu(
        state, on_check_now, on_open_settings, on_open_logs, on_quit,
        on_git_sync_now, on_lab_check_now, on_update_now, on_check_update, on_open_manual,
    )

    def run_background_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop_holder["loop"] = loop
        try:
            check_interval = int(config.get("network_auth", {}).get("check_interval", 60))
            loop.run_until_complete(monitor_loop(authenticator, config, icon, state, interval=check_interval))
        except Exception as e:
            logger.error(f"백그라운드 루프 종료: {e}")

    _ensure_shortcuts()  # 시작폴더/시작메뉴 바로가기 보장 (자동 업데이트로 전파)
    threading.Thread(target=run_background_loop, daemon=True).start()
    threading.Thread(target=git_sync_scheduler, daemon=True).start()
    threading.Thread(target=lab_dev_watch, daemon=True).start()
    threading.Thread(target=update_check_scheduler, daemon=True).start()

    logger.info("네트워크 인증 트레이 앱 시작")
    icon.run()
    logger.info("네트워크 인증 트레이 앱 종료")


if __name__ == "__main__":
    if "--credentials-dialog" in sys.argv:
        from credentials_dialog import main as _dialog_main
        _dialog_main()
    elif "--settings-dialog" in sys.argv:
        from settings_dialog import main as _settings_main
        _settings_main()
    else:
        main()
