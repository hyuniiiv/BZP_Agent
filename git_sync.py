"""
GitLab 일일 동기화 — 트레이 앱 부가 기능.

지정 repo의 로컬 브랜치에 origin의 변경사항을 반영한다.
안전 원칙(사용자 작업 보존 + 커밋 금지):
  1. 트래킹된 수정이 있을 때만 stash (untracked 데모는 건드리지 않음)
  2. fetch → merge --ff-only (강제 병합/리베이스 금지)
  3. stash 한 경우에만 pop (옛 stash 오염 방지)
  4. FF 불가 / pop 충돌 시 자동 해결하지 않고 '수동 확인 필요'로 중단
  5. git commit / push 절대 실행 안 함

subprocess로 시스템 git 호출 — 새 파이썬 의존성 없음.
"""
import logging
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("git_sync")

# git 명령 타임아웃(초). fetch는 네트워크 대기가 있어 길게.
DEFAULT_TIMEOUT = 120
FETCH_TIMEOUT = 180


class SyncResult:
    """동기화 결과. needs_attention=True면 사용자 수동 확인이 필요한 상황."""

    def __init__(self, ok: bool, message: str, needs_attention: bool = False, changed: bool = False):
        self.ok = ok
        self.message = message
        self.needs_attention = needs_attention
        self.changed = changed  # True면 실제로 커밋이 반영됨 → dev 서버 재기동 필요

    def __repr__(self) -> str:
        return f"SyncResult(ok={self.ok}, changed={self.changed}, attention={self.needs_attention}, msg={self.message!r})"


def _git(repo: str, *args: str, timeout: int = DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    """repo 경로를 항상 -C로 강제 지정해 git 실행 (프로세스 cwd에 의존하지 않음)."""
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _has_tracked_changes(status_stdout: str) -> bool:
    """`git status --porcelain` 출력에서 트래킹된 변경(수정/스테이징)이 있는지 판정.
    '??'(untracked)는 병합이 건드리지 않으므로 stash 대상에서 제외한다."""
    for line in status_stdout.splitlines():
        if line and not line.startswith("??"):
            return True
    return False


def sync(repo_path, remote_ref: str = "origin/main") -> SyncResult:
    """repo_path의 현재 브랜치에 remote_ref를 FF-only로 반영.

    반환 SyncResult:
      - ok=True                     : 최신화 완료(또는 이미 최신)
      - ok=False, needs_attention   : FF 불가 / pop 충돌 — 사용자 수동 확인 필요
      - ok=False                    : git 실행 오류(경로/네트워크 등)
    """
    repo = str(repo_path)
    if not Path(repo, ".git").exists():
        return SyncResult(False, f"git 저장소가 아님: {repo}")

    # 1. fetch 먼저 — 워킹트리를 건드리기 전에 "병합이 필요한지"부터 판정한다.
    #    (매 실행마다 stash 하면 이미 최신인 날에도 사용자 작업을 부질없이 들었다 놓아
    #     그 짧은 창에 외부 writer가 끼어들면 pop 충돌이 난다. 실제 FF 때만 stash.)
    try:
        fetch = _git(repo, "fetch", "origin", timeout=FETCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return SyncResult(False, "fetch 타임아웃 — 네트워크 확인 필요")
    if fetch.returncode != 0:
        return SyncResult(False, f"fetch 실패: {fetch.stderr.strip()}")

    local = _git(repo, "rev-parse", "HEAD").stdout.strip()
    remote = _git(repo, "rev-parse", remote_ref).stdout.strip()
    if not remote:
        return SyncResult(False, f"원격 참조 확인 불가: {remote_ref}")

    # 2. 이미 최신 — 워킹트리 무접촉 (stash 자체를 하지 않음)
    if local == remote:
        return SyncResult(True, "이미 최신 상태", changed=False)

    # 3. FF 가능 여부: local이 remote의 조상이어야 fast-forward 가능
    ancestor = _git(repo, "merge-base", "--is-ancestor", local, remote)
    if ancestor.returncode != 0:
        return SyncResult(
            False,
            f"FF 병합 불가 — 로컬 브랜치가 {remote_ref}에서 분기됨. 수동 확인 필요.",
            needs_attention=True,
        )

    # 4. 실제 FF 필요 — 이때만 트래킹 수정 대피
    status = _git(repo, "status", "--porcelain")
    if status.returncode != 0:
        return SyncResult(False, f"git status 실패: {status.stderr.strip()}")
    stashed = False
    if _has_tracked_changes(status.stdout):
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        push = _git(repo, "stash", "push", "-m", f"auto-sync {stamp}")
        if push.returncode != 0:
            return SyncResult(False, f"작업 대피(stash) 실패: {push.stderr.strip()}")
        stashed = True
        logger.info("트래킹 수정 대피 완료 (stash push)")

    # 5. merge --ff-only (강제 병합/리베이스 금지)
    merge = _git(repo, "merge", "--ff-only", remote_ref)
    if merge.returncode != 0:
        _restore_stash(repo, stashed)
        return SyncResult(False, f"FF 병합 실패: {merge.stderr.strip()}", needs_attention=True)

    # 6. 작업 복원 (stash 한 경우에만)
    if stashed:
        pop = _git(repo, "stash", "pop")
        if pop.returncode != 0:
            return SyncResult(
                False,
                "작업 복원(stash pop) 충돌 — stash에 보존됨. 수동 확인 필요.",
                needs_attention=True,
            )
        logger.info("대피 작업 복원 완료 (stash pop)")

    return SyncResult(True, _summarize(repo, local, remote), changed=True)


def _summarize(repo: str, before: str, after: str) -> str:
    """반영된 커밋 수·변경 파일 수를 사람이 읽기 좋은 한 줄로 요약 (파일명 나열 대신)."""
    commits = _git(repo, "rev-list", "--count", f"{before}..{after}").stdout.strip() or "?"
    diff = _git(repo, "diff", "--name-only", before, after).stdout
    files = len([ln for ln in diff.splitlines() if ln.strip()])
    return f"동기화 완료: {commits}개 커밋 · {files}개 파일"


def _restore_stash(repo: str, stashed: bool) -> None:
    """중단 경로에서 대피해둔 작업을 되돌린다 (stash 한 경우에만)."""
    if not stashed:
        return
    pop = _git(repo, "stash", "pop")
    if pop.returncode != 0:
        logger.error("중단 중 작업 복원 실패 — stash에 보존됨. 수동 확인 필요.")
