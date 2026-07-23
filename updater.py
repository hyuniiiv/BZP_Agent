"""
자동 업데이트 — GitHub Releases 기반 원클릭 반자동 (Private 저장소 + 읽기전용 토큰 내장).

- check_latest(): 최신 릴리스 태그를 현재 버전과 비교, 더 높으면 업데이트 정보 반환
- download_and_apply(): 코드 zip 다운로드 → 압축 해제 → PowerShell 업데이터를 분리 실행.
  업데이터는 (1) 이 앱 프로세스 종료를 기다린 뒤 (2) exe/_internal 을 덮어쓰고 (3) 재실행한다.
  config.yaml / .env / ms-playwright 는 코드 zip에 없으므로 그대로 보존된다.

토큰(읽기전용, 이 저장소 한정)은 빌드 시 update_token.txt 로 번들되며, 없으면
비인증으로 시도한다(저장소가 공개일 때만 동작). 표준 라이브러리만 사용.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from version import APP_VERSION, GITHUB_REPO, UPDATE_ASSET_NAME

logger = logging.getLogger("updater")

API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_UA = f"BZP_Agent/{APP_VERSION}"

# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — 업데이터가 부모(이 앱)와 함께 죽지 않도록
_DETACHED = 0x00000008 | 0x00000200


def _load_token() -> str:
    """읽기전용 토큰을 (1) 환경변수 (2) 실행폴더 (3) 번들(_MEIPASS) 순으로 찾는다. 없으면 ''."""
    env = os.environ.get("BZP_UPDATE_TOKEN", "").strip()
    if env:
        return env
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "update_token.txt")
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "update_token.txt")
    else:
        candidates.append(Path(__file__).parent / "update_token.txt")
    for path in candidates:
        try:
            if path.exists():
                return path.read_text(encoding="utf-8-sig").strip()
        except Exception:
            pass
    return ""


def _api_headers(token: str, accept: str) -> dict:
    headers = {"Accept": accept, "User-Agent": _UA, "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def parse_version(text: str) -> tuple:
    """'v1.2.3' / '1.2' 등을 (1,2,3) 튜플로. 숫자 아닌 문자는 무시, 3자리로 패딩."""
    parts = []
    for chunk in str(text).lstrip("vV").strip().split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def check_latest(timeout: int = 10):
    """최신 릴리스 확인. 현재보다 높은 버전이 있으면 dict, 아니면(최신/오류/자산없음) None.

    반환 dict: {"version": "1.1.0", "asset_url": <자산 API URL>, "notes": <릴리스 노트>}
    """
    token = _load_token()
    try:
        req = urllib.request.Request(API_LATEST, headers=_api_headers(token, "application/vnd.github+json"))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except Exception as e:  # 네트워크 오류/레이트리밋/릴리스 없음/권한 등은 조용히 스킵
        logger.info(f"업데이트 확인 실패(무시): {e}")
        return None

    tag = data.get("tag_name") or ""
    if not tag or parse_version(tag) <= parse_version(APP_VERSION):
        return None

    asset_url = None
    for asset in data.get("assets", []):
        if asset.get("name") == UPDATE_ASSET_NAME:
            asset_url = asset.get("url")  # API URL (Private 저장소 다운로드에 필요)
            break
    if not asset_url:
        logger.info(f"새 릴리스 {tag} 이(가) 있으나 '{UPDATE_ASSET_NAME}' 자산이 없어 스킵")
        return None

    return {"version": tag.lstrip("vV"), "asset_url": asset_url, "notes": (data.get("body") or "").strip()}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """자동 리다이렉트 억제 — 서명 URL로 리다이렉트될 때 Authorization 헤더가
    엉뚱한 호스트(S3 등)로 새어나가지 않도록 직접 처리하기 위함."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _download_asset(asset_url: str, dest: Path, token: str, timeout: int = 120) -> None:
    """릴리스 자산을 dest로 다운로드. Private 저장소는 자산 API URL에 octet-stream을 요청하면
    서명된 임시 URL로 302가 오는데, 그 URL은 토큰 없이(오히려 토큰을 빼고) 받아야 한다."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(asset_url, headers=_api_headers(token, "application/octet-stream"))
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            signed = e.headers.get("Location")
            if not signed:
                raise
            # 서명 URL은 인증정보 없이(UA만) 받는다
            with urllib.request.urlopen(
                urllib.request.Request(signed, headers={"User-Agent": _UA}), timeout=timeout
            ) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
            return
        raise
    else:
        with resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)


# 앱 종료 대기 → 파일 교체 → 재시작. $Pid는 PowerShell 예약변수라 $ProcId 사용.
_APPLY_PS = r"""param([int]$ProcId, [string]$Src, [string]$Dst, [string]$Exe)
$ErrorActionPreference = "SilentlyContinue"
# 1) 기존 앱 프로세스 종료 대기 (최대 60초)
try { Wait-Process -Id $ProcId -Timeout 60 } catch {}
Start-Sleep -Seconds 1
# 2) 새 코드 덮어쓰기 (config.yaml/.env/ms-playwright 는 $Src에 없어 보존됨)
Copy-Item -Path (Join-Path $Src '*') -Destination $Dst -Recurse -Force
# 3) 재실행
Start-Process -FilePath $Exe -WorkingDirectory $Dst
"""


def download_and_apply(asset_url: str, install_dir, exe_name: str = "BZP_Agent.exe", app_pid: int | None = None) -> bool:
    """코드 zip 다운로드 후 업데이터를 분리 실행한다. 성공 반환 시 호출자는 즉시 앱을 종료해야 한다.
    실패하면 예외 없이 False (앱은 계속 실행)."""
    try:
        install_dir = Path(install_dir)
        work = Path(tempfile.gettempdir()) / "BZP_Agent_update"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)

        zip_path = work / "code.zip"
        _download_asset(asset_url, zip_path, _load_token())

        extract_dir = work / "extracted"
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)
        # zip 최상위가 BZP_Agent/ 로 감싸져 있으면 그 안을 payload로 사용
        inner = extract_dir / "BZP_Agent"
        payload = inner if inner.exists() else extract_dir

        # 안전장치: 교체 대상 exe가 payload에 실제로 있는지 확인
        if not (payload / exe_name).exists():
            logger.error(f"다운로드한 패키지에 {exe_name} 이(가) 없어 업데이트 중단")
            return False

        ps_path = work / "apply_update.ps1"
        ps_path.write_text(_APPLY_PS, encoding="utf-8-sig")

        pid = app_pid if app_pid is not None else os.getpid()
        exe_path = install_dir / exe_name
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps_path),
             "-ProcId", str(pid), "-Src", str(payload), "-Dst", str(install_dir), "-Exe", str(exe_path)],
            creationflags=_DETACHED, close_fds=True,
        )
        logger.info(f"업데이터 실행됨 → {exe_path} (pid {pid} 종료 대기 후 교체)")
        return True
    except Exception as e:
        logger.error(f"업데이트 적용 실패: {e}")
        return False
