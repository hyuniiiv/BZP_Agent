"""
GitLab 일일 동기화 — 트레이 앱 부가 기능.

remote_ref(기본 origin/main)에 대응하는 로컬 브랜치(기본 main)를 항상 동기화 대상으로
삼는다 — 현재 체크아웃된 브랜치가 아니다. 개발자는 보통 main이 아니라 작업 브랜치(feature/lab
등)에 있고, 그 브랜치는 origin/main과 원래 갈라져 있는 게 정상이라 그걸 FF하려 하면 항상
"분기됨" 실패가 난다(실측 확인). 대신 안전하게 main만 최신화하고 작업 브랜치는 건드리지 않는다:
  1. 현재 브랜치가 대상 브랜치와 다르면: 트래킹 수정만 stash(untracked는 건드리지 않음) →
     대상 브랜치로 체크아웃 → FF → 원래 브랜치로 복귀 → stash pop
  2. fetch → merge --ff-only (강제 병합/리베이스 금지)
  3. FF 불가 / 체크아웃-복귀 실패 / pop 충돌 시 자동 해결하지 않고 '수동 확인 필요'로 중단
  4. git commit / push 절대 실행 안 함

subprocess로 시스템 git 호출 — 새 파이썬 의존성 없음.
"""
import logging
import subprocess
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("git_sync")

# git 명령 타임아웃(초). fetch는 네트워크 대기가 있어 길게.
DEFAULT_TIMEOUT = 120
FETCH_TIMEOUT = 180

# 윈도우드 exe에서 git 호출 시 콘솔 창이 깜빡이지 않도록
_CREATE_NO_WINDOW = 0x08000000
# .git 잠금(index.lock 등) 충돌 시 재시도 정책 — 다른 git 프로세스가 끝나길 잠깐 대기
_LOCK_HINTS = ("index.lock", "unable to create", "another git process", "cannot lock ref")
_LOCK_RETRIES = 3
_LOCK_WAIT = 3  # 초


def _is_lock_error(cp: subprocess.CompletedProcess) -> bool:
    """git 실패가 .git 잠금 충돌 때문인지 판정 (stderr 힌트 기반)."""
    if cp.returncode == 0:
        return False
    text = (cp.stderr or "").lower()
    return any(h in text for h in _LOCK_HINTS)


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
    """repo 경로를 항상 -C로 강제 지정해 git 실행 (프로세스 cwd에 의존하지 않음).
    콘솔 창을 띄우지 않으며, 다른 git 프로세스와의 .git 잠금 충돌 시 잠깐 대기 후 재시도한다."""
    cp = None
    for attempt in range(_LOCK_RETRIES + 1):
        cp = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
        if not _is_lock_error(cp):
            return cp
        if attempt < _LOCK_RETRIES:
            logger.info(f"git 잠금 충돌 감지 — {_LOCK_WAIT}s 후 재시도 {attempt + 1}/{_LOCK_RETRIES}")
            time.sleep(_LOCK_WAIT)
    return cp  # 재시도 소진 — 마지막(잠금) 결과 반환, 호출부가 처리


def _has_tracked_changes(status_stdout: str) -> bool:
    """`git status --porcelain` 출력에서 트래킹된 변경(수정/스테이징)이 있는지 판정.
    '??'(untracked)는 병합이 건드리지 않으므로 stash 대상에서 제외한다."""
    for line in status_stdout.splitlines():
        if line and not line.startswith("??"):
            return True
    return False


def sync(repo_path, remote_ref: str = "origin/main", target_branch: str | None = None) -> SyncResult:
    """target_branch(미지정 시 remote_ref의 브랜치명, 예: origin/main → main)를 FF-only로 최신화한다.
    현재 다른 브랜치/커밋에 있으면 그 상태를 그대로 보존한 채 대상 브랜치만 갱신한다(아래 참고).

    반환 SyncResult:
      - ok=True                     : target_branch 최신화 완료(또는 이미 최신)
      - ok=False, needs_attention   : FF 불가 / 체크아웃-복귀 실패 / pop 충돌 — 사용자 수동 확인 필요
      - ok=False                    : git 실행 오류(경로/네트워크 등)
    """
    repo = str(repo_path)
    if not Path(repo, ".git").exists():
        return SyncResult(False, f"git 저장소가 아님: {repo}")
    if target_branch is None:
        target_branch = remote_ref.split("/", 1)[1] if "/" in remote_ref else remote_ref

    # 1. fetch 먼저 — 워킹트리를 건드리기 전에 "병합이 필요한지"부터 판정한다.
    #    (매 실행마다 stash 하면 이미 최신인 날에도 사용자 작업을 부질없이 들었다 놓아
    #     그 짧은 창에 외부 writer가 끼어들면 pop 충돌이 난다. 실제 FF 때만 stash.)
    try:
        fetch = _git(repo, "fetch", "origin", timeout=FETCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return SyncResult(False, "fetch 타임아웃 — 네트워크 확인 필요")
    if fetch.returncode != 0:
        return SyncResult(False, f"fetch 실패: {fetch.stderr.strip()}")

    remote = _git(repo, "rev-parse", remote_ref).stdout.strip()
    if not remote:
        return SyncResult(False, f"원격 참조 확인 불가: {remote_ref}")

    verify = _git(repo, "rev-parse", "--verify", target_branch)
    if verify.returncode != 0:
        return SyncResult(
            False, f"로컬에 '{target_branch}' 브랜치가 없어 동기화할 수 없습니다.", needs_attention=True
        )
    target_local = verify.stdout.strip()

    # 2. 이미 최신 — 워킹트리 무접촉 (stash 자체를 하지 않음)
    if target_local == remote:
        return SyncResult(True, f"'{target_branch}' 이미 최신 상태", changed=False)

    # 3. FF 가능 여부: target_local이 remote의 조상이어야 fast-forward 가능
    ancestor = _git(repo, "merge-base", "--is-ancestor", target_local, remote)
    if ancestor.returncode != 0:
        return SyncResult(
            False,
            f"FF 병합 불가 — 로컬 '{target_branch}' 브랜치가 {remote_ref}에서 분기됨. 수동 확인 필요.",
            needs_attention=True,
        )

    # 4. 현재 위치 파악. 대상 브랜치가 아니면(보통 작업 브랜치에 있음) 전환이 필요하다.
    #    detached HEAD(브랜치 아님)면 "HEAD"가 아니라 정확한 커밋으로 복귀해야 하므로 SHA를 쓴다.
    current_name = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if current_name == "HEAD":
        current_ref = _git(repo, "rev-parse", "HEAD").stdout.strip()
    else:
        current_ref = current_name
    switch_needed = current_ref != target_branch

    # 5. 전환이 필요할 때만 트래킹 수정 대피 (untracked는 건드리지 않음)
    stashed = False
    if switch_needed:
        status = _git(repo, "status", "--porcelain")
        if status.returncode != 0:
            return SyncResult(False, f"git status 실패: {status.stderr.strip()}")
        if _has_tracked_changes(status.stdout):
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            push = _git(repo, "stash", "push", "-m", f"auto-sync {stamp}")
            if push.returncode != 0:
                return SyncResult(False, f"작업 대피(stash) 실패: {push.stderr.strip()}")
            stashed = True
            logger.info(f"'{current_ref}' 트래킹 수정 대피 완료 (stash push)")

        checkout = _git(repo, "checkout", target_branch)
        if checkout.returncode != 0:
            _restore_stash(repo, stashed)
            return SyncResult(
                False, f"'{target_branch}' 체크아웃 실패: {checkout.stderr.strip()}", needs_attention=True
            )
        logger.info(f"'{target_branch}'로 전환 (원래 위치: '{current_ref}')")

    # 6. merge --ff-only (강제 병합/리베이스 금지)
    merge = _git(repo, "merge", "--ff-only", remote_ref)
    merge_ok = merge.returncode == 0
    merge_err = merge.stderr.strip()

    # 7. 전환했다면 병합 성공/실패와 무관하게 반드시 원래 위치로 복귀를 시도한다.
    if switch_needed:
        back = _git(repo, "checkout", current_ref)
        if back.returncode != 0:
            _restore_stash(repo, stashed)
            return SyncResult(
                False,
                f"원래 위치('{current_ref}') 복귀 실패 — 현재 '{target_branch}'에 있습니다. 수동 확인 필요.",
                needs_attention=True,
            )
        if stashed:
            pop = _git(repo, "stash", "pop")
            if pop.returncode != 0:
                return SyncResult(
                    False, "작업 복원(stash pop) 충돌 — stash에 보존됨. 수동 확인 필요.", needs_attention=True
                )
            logger.info(f"'{current_ref}' 대피 작업 복원 완료 (stash pop)")

    if not merge_ok:
        if _is_lock_error(merge):
            # 재시도(_git)까지 소진하고도 잠긴 상태 — 다른 git 작업 중이거나 스테일 락
            return SyncResult(
                False,
                "저장소가 잠겨 병합 보류(.git/index.lock). 다른 git 작업 중이거나 스테일 락일 수 있습니다. "
                "지속되면 .git/index.lock 을 확인하세요.",
                needs_attention=True,
            )
        if "would be overwritten" in merge_err.lower():
            # 로컬 변경 또는 skip-worktree 파일이 병합을 막음 — 자동 해결하지 않고 알림
            return SyncResult(
                False,
                f"'{target_branch}'의 로컬 변경(또는 skip-worktree 파일)이 병합을 막습니다 — 수동 확인 필요:\n{merge_err}",
                needs_attention=True,
            )
        return SyncResult(False, f"FF 병합 실패: {merge_err}", needs_attention=True)

    note = f" (작업 브랜치 '{current_ref}'는 그대로 유지)" if switch_needed else ""
    return SyncResult(True, f"'{target_branch}' " + _summarize(repo, target_local, remote) + note, changed=True)


def sync_current_branch(repo_path, remote_ref: str = "origin/main") -> SyncResult:
    """현재 체크아웃된 브랜치(작업 브랜치)에 remote_ref를 병합한다.

    sync()와 달리 대상이 '지금 있는 브랜치 그 자체'다. 작업 브랜치는 원격 main과
    갈라져 있는 게 정상이라 실제 3-way merge가 필요할 수 있는데, 무인 실행 중 충돌이 나면
    저장소가 충돌 상태로 멈춰버려 위험하다. 그래서 실제로 건드리기 전에
    `git merge-tree --write-tree`로 워킹트리/인덱스를 건드리지 않는 드라이런 병합을 먼저 하고,
    충돌 없이 깨끗할 때만 진짜 병합을 수행한다. 충돌이 예상되면 아무것도 건드리지 않고
    needs_attention만 반환한다(항상 사람이 직접 해결).

    detached HEAD면 대상 브랜치가 불분명하므로 건드리지 않는다.
    """
    repo = str(repo_path)
    if not Path(repo, ".git").exists():
        return SyncResult(False, f"git 저장소가 아님: {repo}")

    try:
        fetch = _git(repo, "fetch", "origin", timeout=FETCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return SyncResult(False, "fetch 타임아웃 — 네트워크 확인 필요")
    if fetch.returncode != 0:
        return SyncResult(False, f"fetch 실패: {fetch.stderr.strip()}")

    current_name = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if current_name == "HEAD":
        return SyncResult(False, "detached HEAD 상태 — 작업 브랜치 동기화 건너뜀", needs_attention=True)

    remote = _git(repo, "rev-parse", remote_ref).stdout.strip()
    if not remote:
        return SyncResult(False, f"원격 참조 확인 불가: {remote_ref}")
    current = _git(repo, "rev-parse", "HEAD").stdout.strip()

    if current == remote:
        return SyncResult(True, f"'{current_name}' 이미 {remote_ref}과 동일", changed=False)

    already_contains = _git(repo, "merge-base", "--is-ancestor", remote, current)
    if already_contains.returncode == 0:
        return SyncResult(True, f"'{current_name}'에 {remote_ref} 이미 반영됨", changed=False)

    # 드라이런: 워킹트리/인덱스를 전혀 건드리지 않고 병합 가능 여부만 확인
    dry_run = _git(repo, "merge-tree", "--write-tree", current, remote)
    if dry_run.returncode != 0:
        return SyncResult(
            False,
            f"'{current_name}' ← {remote_ref} 자동 병합 시 충돌 예상 — 작업 브랜치 미변경, 수동 병합 필요.",
            needs_attention=True,
        )

    status = _git(repo, "status", "--porcelain")
    if status.returncode != 0:
        return SyncResult(False, f"git status 실패: {status.stderr.strip()}")
    stashed = False
    if _has_tracked_changes(status.stdout):
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        push = _git(repo, "stash", "push", "-m", f"auto-sync-branch {stamp}")
        if push.returncode != 0:
            return SyncResult(False, f"작업 대피(stash) 실패: {push.stderr.strip()}")
        stashed = True
        logger.info(f"'{current_name}' 트래킹 수정 대피 완료 (stash push)")

    merge = _git(repo, "merge", "--no-edit", remote_ref)
    if merge.returncode != 0:
        # 드라이런은 깨끗했는데 실제 병합이 실패한 예외적인 경우 — 대피만 복원하고 알림
        _restore_stash(repo, stashed)
        return SyncResult(
            False,
            f"'{current_name}' 병합 실패(드라이런 이후 상태 변경 가능성): {merge.stderr.strip()}",
            needs_attention=True,
        )

    if stashed:
        pop = _git(repo, "stash", "pop")
        if pop.returncode != 0:
            return SyncResult(
                False,
                f"'{current_name}' 병합은 완료됐으나 작업 복원(stash pop) 충돌 — stash에 보존됨. 수동 확인 필요.",
                needs_attention=True,
            )
        logger.info(f"'{current_name}' 대피 작업 복원 완료 (stash pop)")

    return SyncResult(True, f"'{current_name}' " + _summarize(repo, current, remote), changed=True)


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
