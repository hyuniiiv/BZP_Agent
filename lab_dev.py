"""
lab dev 서버(localhost:3003) 감시·자동 재실행 — 트레이 앱 부가 기능.

주기적으로 http://localhost:3003 헬스체크 → 포트(3003/4401)가 실제로 안 열려 있을 때만
(=프로세스가 진짜 종료됐을 때만) 재시작한다:
  1. 관련 포트 점유 좀비 프로세스 강제 종료
  2. repo에서 `pnpm lab dev`를 콘솔 없이 백그라운드로 재실행

HTTP 응답이 없어도 포트가 여전히 listen 중이면(=빌드로 바쁜 것뿐일 수 있음) 재시작하지 않는다 —
"바쁜데 죽었다고 오판해 우리가 직접 킬"하는 오탐을 막기 위함(check_health 참고).
재실행 직후에는 부팅 시간이 필요하므로 STARTUP_GRACE 동안 재시작 판정을 보류한다.

subprocess + urllib(stdlib)만 사용 — 새 파이썬 의존성 없음.
"""
import ctypes
import logging
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger("lab_dev")


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _mem_snapshot() -> str:
    """현재 시스템 메모리 사용률/여유량을 한 줄로. 재시작 원인이 메모리 부족인지 진단하기 위함."""
    try:
        st = _MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        avail_mb = st.ullAvailPhys // (1024 * 1024)
        return f"메모리 사용률 {st.dwMemoryLoad}% (여유 {avail_mb}MB)"
    except Exception:
        return "메모리 확인 불가"

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


def check_health(url: str, ports: list[int], timeout: int = HEALTH_TIMEOUT) -> str:
    """dev 서버 상태 판정: 'up'(정상) | 'busy'(포트는 listen 중인데 응답 없음=빌드로 바쁨) | 'down'(포트 자체가 안 열림=진짜 죽음).

    HTTP 타임아웃/연결거부만으로 '죽음'을 판단하지 않는 이유: 이 환경에서는 방화벽이 SYN을
    조용히 drop해 '연결거부'조차 타임아웃으로 보여 예외 종류로는 구분이 불가능하다(실측 확인됨).
    대신 netstat 기반으로 포트가 실제 listen 중인지를 권위 있는 신호로 삼는다 — 포트가 열려
    있으면 프로세스는 살아있는 것이고, 리빌드로 응답만 늦은 것뿐일 수 있다(흔한 오탐 패턴 방지)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return "up" if resp.status < 500 else "busy"
    except urllib.error.HTTPError:
        return "up"  # 4xx/5xx라도 서버 프로세스는 응답 중 → 살아있음
    except Exception:
        pass
    return "busy" if any(_pids_on_port(p) for p in ports) else "down"


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
        # 보이는 콘솔 + 파일 로그 동시(Tee). 서버가 죽으면 종료 코드/시각을 파일에 남겨
        # "왜 꺼졌는지"(정상종료 exit=0 / 크래시 exit!=0 / 외부 kill=마커 없음)를 진단할 수 있게 한다.
        # PowerShell -NoExit 로 창을 유지(로그·에러 확인). 따옴표 지옥을 피하려고 스크립트 파일로 실행.
        log_path.parent.mkdir(exist_ok=True)
        script = log_path.parent / "lab_console.ps1"
        script.write_text(
            "$ErrorActionPreference='Continue'\n"
            # 콘솔/출력 인코딩을 UTF-8로 — node의 UTF-8 한글이 CP949로 깨지는 것 방지
            "chcp 65001 > $null\n"
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
            "$OutputEncoding = [System.Text.Encoding]::UTF8\n"
            # 콘솔 VT(가상터미널) 처리 활성화 → Vite 등의 ANSI 색상이 '?[39m' 날것 대신 실제 색으로 렌더됨
            "try {\n"
            "  $vt = Add-Type -Name VT -Namespace Con -PassThru -MemberDefinition @'\n"
            "[DllImport(\"kernel32.dll\")] public static extern IntPtr GetStdHandle(int n);\n"
            "[DllImport(\"kernel32.dll\")] public static extern bool GetConsoleMode(IntPtr h, out uint m);\n"
            "[DllImport(\"kernel32.dll\")] public static extern bool SetConsoleMode(IntPtr h, uint m);\n"
            "'@\n"
            "  $h = $vt::GetStdHandle(-11); $m = 0\n"
            "  [void]$vt::GetConsoleMode($h, [ref]$m); [void]$vt::SetConsoleMode($h, $m -bor 4)\n"
            "} catch {}\n"
            f"Set-Location -LiteralPath '{repo}'\n"
            # Tee-Object/Out-File 기본 인코딩은 Windows PowerShell 5.1에서 UTF-16LE라
            # 파일이 다른 도구(Python 등)로 읽을 때 깨진다. UTF-8로 고정.
            "$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'\n"
            f"Write-Host '[dev 시작: {command}]' -ForegroundColor Cyan\n"
            # 주의: PowerShell 5.1에서 네이티브 명령에 '2>&1'을 쓰면 stderr가 NativeCommandError로
            # 감싸져 정상 경고까지 빨간 에러 블록으로 보인다. cmd 레벨에서 병합(평문)한 뒤 PS로 받는다.
            f"cmd /c '{command} 2>&1' | Tee-Object -FilePath '{log_path}' -Append\n"
            # Add-Content 기본 인코딩은 Out-File과 달리 시스템 ANSI라 -Encoding을 별도로 지정해야 한다.
            f"Add-Content -Path '{log_path}' -Encoding utf8 -Value \"[dev 종료됨: exit=$LASTEXITCODE 시각=$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')]\"\n"
            "Write-Host ''; Write-Host '[dev 서버가 종료되었습니다. 위 로그에서 원인을 확인하세요. 곧 자동 재실행됩니다.]' -ForegroundColor Yellow\n",
            encoding="utf-8-sig",
        )
        return subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit", "-File", str(script)],
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

    def ensure_running(self, force: bool = False, reason: str | None = None) -> LabDevResult:
        """서버가 죽어 있으면 포트 정리 후 재실행. force=True면 유예 무시하고 즉시 재실행.
        reason: 로그에 남길 트리거 사유. 미지정 시 자동 판단(수동 요청 / 헬스체크 결과)."""
        if not Path(self.repo).exists():
            return LabDevResult("error", f"repo 경로 없음: {self.repo}")
        if reason is None and force:
            reason = "수동/외부 요청"

        # 동시 재실행 방지.
        # force(동기화 후 재기동·메뉴 요청)는 반드시 실행해야 하므로 락을 대기해서라도 획득한다.
        # 일반 주기 감시만 경합 시 건너뛴다(다른 호출이 이미 처리 중).
        if force:
            self._lock.acquire()
        elif not self._lock.acquire(blocking=False):
            return LabDevResult("grace", "다른 확인 진행 중")
        try:
            if not force:
                health = check_health(self.url, self.ports)
                logger.info(f"헬스체크: {health} (url={self.url})")
                if health == "up":
                    return LabDevResult("up", "정상 구동 중")
                if health == "busy":
                    # 포트는 listen 중 = 프로세스는 살아있음 = 빌드로 응답만 늦은 것뿐일 수 있다.
                    # 여기서 죽었다고 오판해 우리가 직접 킬하는 게 가장 흔한 오탐 패턴이라,
                    # 절대 자동으로 재시작하지 않고 다음 감시 주기에 다시 확인한다.
                    logger.info("포트는 열려있으나 응답 없음(빌드 중일 수 있음) — 재시작하지 않고 다음 주기에 재확인")
                    return LabDevResult("grace", "응답 지연 (빌드 중일 수 있음 — 재시작 안 함)")
                # health == "down": 포트 자체가 안 열려 있음 = 프로세스가 실제로 종료됨
                reason = reason or "포트 응답 없음 — 프로세스 실제 종료 확인됨"

            # 부팅 유예: 최근 재실행 직후면 아직 부팅 중일 수 있어 재시작하지 않는다
            if not force and self._within_grace():
                return LabDevResult("grace", "재실행 후 부팅 대기 중")

            # 직전 재실행 이후 생존 시간 — 원인 진단용(예: 항상 비슷한 초에 죽으면 특정 트리거 의심)
            uptime = (
                f"{time.monotonic() - self._last_start_monotonic:.0f}초"
                if self._last_start_monotonic is not None else "N/A(최초 실행)"
            )

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
            logger.info(
                f"lab dev 재실행 [사유: {reason} | 직전 생존시간: {uptime} | {_mem_snapshot()}]: "
                f"{self.command}{detail}"
            )
            return LabDevResult("restarted", f"재실행됨{detail}")
        finally:
            self._lock.release()

    def _within_grace(self) -> bool:
        if self._last_start_monotonic is None:
            return False
        return (time.monotonic() - self._last_start_monotonic) < STARTUP_GRACE
