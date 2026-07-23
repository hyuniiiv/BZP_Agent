"""
lab dev 서버(localhost:3003) 감시·자동 재실행 — 트레이 앱 부가 기능.

주기적으로 http://localhost:3003 헬스체크 → 응답이 없으면:
  1. 관련 포트(3003 lab, 4401 lab-api) 점유 좀비 프로세스 강제 종료
  2. repo에서 `pnpm lab dev`를 콘솔 없이 백그라운드로 재실행

정상 응답 중이면 아무것도 하지 않는다(멀쩡한 서버를 죽이지 않음).
재실행 직후에는 부팅 시간이 필요하므로 STARTUP_GRACE 동안 재시작 판정을 보류한다.

subprocess + urllib(stdlib)만 사용 — 새 파이썬 의존성 없음.
"""
import logging
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger("lab_dev")

# 재실행 후 부팅 유예(초). 이 시간 안에는 헬스체크 실패해도 재시작하지 않는다.
STARTUP_GRACE = 90
# 헬스체크 HTTP 타임아웃(초)
HEALTH_TIMEOUT = 3

# Windows 프로세스 생성 플래그
_CREATE_NO_WINDOW = 0x08000000        # 보조 명령(netstat/taskkill)은 창 없이
_CREATE_NEW_CONSOLE = 0x00000010      # dev 서버는 '보이는' 새 콘솔 창으로 (실시간 로그)
_CREATE_NEW_PROCESS_GROUP = 0x00000200


class LabDevResult:
    """감시 결과. action: 'up'(정상) | 'restarted'(재실행함) | 'grace'(부팅 유예) | 'error'."""

    def __init__(self, action: str, message: str):
        self.action = action
        self.message = message

    def __repr__(self) -> str:
        return f"LabDevResult(action={self.action!r}, msg={self.message!r})"


def is_up(url: str, timeout: int = HEALTH_TIMEOUT) -> bool:
    """dev 서버가 HTTP 응답하는지 확인. 어떤 상태코드든 응답이 오면 살아있는 것으로 본다."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status < 500
    except urllib.error.HTTPError:
        # 4xx/5xx라도 서버 프로세스는 응답 중 → 살아있음
        return True
    except Exception:
        return False


def _pids_on_port(port: int) -> set[str]:
    """해당 포트를 LISTENING 중인 프로세스 PID 집합 (netstat 파싱)."""
    pids: set[str] = set()
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception as exc:
        logger.debug(f"netstat 실패: {exc}")
        return pids
    suffix = f":{port}"
    for line in out.stdout.splitlines():
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(suffix):
            pids.add(parts[-1])
    return pids


def _kill_ports(ports: list[int]) -> list[str]:
    """지정 포트를 물고 있는 프로세스를 트리 강제 종료. 종료한 PID 목록 반환."""
    killed: list[str] = []
    for port in ports:
        for pid in _pids_on_port(port):
            if pid in ("0", ""):
                continue
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", pid],
                    capture_output=True, text=True, timeout=15,
                    creationflags=_CREATE_NO_WINDOW,
                )
                killed.append(pid)
                logger.info(f"포트 {port} 점유 프로세스 종료 (PID {pid})")
            except Exception as exc:
                logger.debug(f"PID {pid} 종료 실패: {exc}")
    return killed


def _kill_tree(pid) -> None:
    """지정 PID와 그 자식들을 강제 종료 (콘솔 창을 포함해 정리)."""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, timeout=15, creationflags=_CREATE_NO_WINDOW,
        )
    except Exception as exc:
        logger.debug(f"프로세스 트리 종료 실패(pid {pid}): {exc}")


def _start_dev(repo: str, command: str, log_path: Path, show_console: bool = False) -> subprocess.Popen:
    """repo에서 dev 명령을 실행하고 Popen 핸들을 반환한다.
    show_console=True: '보이는' 새 콘솔 창으로 실행(실시간 로그, 개발자용).
    show_console=False: 창 없이 실행하고 출력을 log_path 파일로 기록(팀원용 조용)."""
    if show_console:
        # cmd /k = 명령이 끝나도(예: pnpm이 서버를 띄우고 반환해도) 콘솔 창을 닫지 않고 유지.
        # CREATE_NEW_CONSOLE 로 창 없는(windowed) 앱에서도 새 콘솔 창을 강제로 띄운다.
        return subprocess.Popen(
            f'cmd /k {command}',
            cwd=repo,
            creationflags=_CREATE_NEW_CONSOLE | _CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    log_path.parent.mkdir(exist_ok=True)
    log_file = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 — 백그라운드 프로세스가 계속 사용
    return subprocess.Popen(
        command,
        cwd=repo,
        shell=True,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )


class LabDevMonitor:
    """lab dev 서버 감시 상태를 들고 재실행 유예를 관리."""

    def __init__(self, repo: str, url: str, ports: list[int], command: str, log_path: Path,
                 show_console: bool = False):
        self.repo = str(repo)
        self.url = url
        self.ports = ports
        self.command = command
        self.log_path = log_path
        self.show_console = show_console
        self._launched_pid: int | None = None  # 직전에 띄운 프로세스(콘솔 포함) — 재실행 시 먼저 정리
        self._last_start_monotonic: float | None = None
        self._lock = threading.Lock()  # 감시 스레드 / 동기화 연동 / 메뉴 동시 호출 방지

    def ensure_running(self, force: bool = False) -> LabDevResult:
        """서버가 죽어 있으면 포트 정리 후 재실행. force=True면 유예 무시하고 즉시 재실행."""
        if not Path(self.repo).exists():
            return LabDevResult("error", f"repo 경로 없음: {self.repo}")

        # 동시 재실행 방지.
        # force(동기화 후 재기동·메뉴 요청)는 반드시 실행해야 하므로 락을 대기해서라도 획득한다.
        # 일반 주기 감시만 경합 시 건너뛴다(다른 호출이 이미 처리 중).
        if force:
            self._lock.acquire()
        elif not self._lock.acquire(blocking=False):
            return LabDevResult("grace", "다른 확인 진행 중")
        try:
            if not force and is_up(self.url):
                return LabDevResult("up", "정상 구동 중")

            # 부팅 유예: 최근 재실행 직후면 아직 부팅 중일 수 있어 재시작하지 않는다
            if not force and self._within_grace():
                return LabDevResult("grace", "재실행 후 부팅 대기 중")

            # 직전에 띄운 프로세스(콘솔 창 포함)를 먼저 정리 — 콘솔 창 누적 방지
            if self._launched_pid:
                _kill_tree(self._launched_pid)
                self._launched_pid = None
            killed = _kill_ports(self.ports)
            try:
                proc = _start_dev(self.repo, self.command, self.log_path, self.show_console)
            except Exception as exc:
                return LabDevResult("error", f"재실행 실패: {exc}")
            self._launched_pid = proc.pid
            self._last_start_monotonic = time.monotonic()
            detail = f" (종료 PID: {', '.join(killed)})" if killed else ""
            logger.info(f"lab dev 재실행: {self.command}{detail}")
            return LabDevResult("restarted", f"재실행됨{detail}")
        finally:
            self._lock.release()

    def _within_grace(self) -> bool:
        if self._last_start_monotonic is None:
            return False
        return (time.monotonic() - self._last_start_monotonic) < STARTUP_GRACE
