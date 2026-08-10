# Network Auth Tray — 사용 설명서

사내 네트워크 인증을 상시 유지하는 Windows 시스템 트레이 앱.
부가로 **new-frontend GitLab 일일 동기화**와 **lab dev(3003) 서버 감시·재실행**을 함께 담당한다.

---

## 1. 기능 요약

| 기능 | 동작 | 주기 |
|------|------|------|
| **네트워크 인증** | 경량 상태 체크 → 끊기면 브라우저로 자동 재로그인 | 평상시 180초 / 민감창(새벽) 60초 |
| **GitLab 동기화** | origin 변경을 로컬에 FF 반영 (커밋·푸시 없음, 작업 보존) | 매일 08시 + 앱 시작 시 |
| **lab dev 감시** | localhost:3003 헬스체크 → 죽으면 포트 정리 후 재실행 | 60초 |

---

## 2. 실행 / 자동 시작

- **자동 시작**: 시작프로그램(`shell:startup`)의 `BZP_Agent.lnk`가
  `%LOCALAPPDATA%\BZP_Agent\BZP_Agent.exe`(빌드본)를 실행한다 (콘솔 없음).
  → 배포본 실행이므로 코드 수정 시 재빌드+업데이트가 필요하다(자동 업데이트 참고).
- **개발 중 소스 직접 실행**: `pythonw D:\BZP_Agent\network_auth_tray.py`
  → 코드 수정 시 재빌드 불필요, 재시작만 하면 반영된다.
- **재시작(소스 실행 중 코드 반영)**:
  ```powershell
  Get-Process pythonw | Where-Object { (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine -like '*network_auth_tray*' } | Stop-Process -Force
  Start-Process pythonw.exe -ArgumentList "D:\BZP_Agent\network_auth_tray.py" -WorkingDirectory "D:\BZP_Agent"
  ```
- **배포본(exe) 빌드**(선택): `pyinstaller NetworkAuthTray.spec` → `dist\NetworkAuthTray\`

> ⚠️ 재시작은 필요할 때만. GitLab 동기화가 진행 중일 때 강제 종료하면 stash가 남을 수 있다(§6 참고).

---

## 3. 트레이 메뉴

아이콘 색: 🟢 인증됨 · 🔴 실패 · 🟡 확인 중 · ⚪ 대기.

| 항목 | 설명 |
|------|------|
| 인증: … | 네트워크 인증 상태 |
| 마지막 확인: … | 마지막 인증 체크 시각 |
| GitLab: … | 최근 동기화 결과 · 시각 |
| lab 3003: … | lab dev 서버 상태 |
| **지금 인증 확인** | 즉시 완전 인증 체크(재로그인 포함) |
| **지금 GitLab 동기화** | 즉시 동기화 실행 |
| **지금 lab 서버 확인** | 즉시 3003 확인 후 필요 시 재실행 |
| 계정 설정… | 네트워크 인증 ID/PW 입력 |
| 로그 폴더 열기 | `logs/` 열기 |
| 종료 | 트레이 종료 (dev 서버는 계속 유지) |

---

## 4. 설정 (`config.yaml`)

```yaml
network_auth:
  url: http://172.28.200.250/cwp2/faces/common/userAuth.xhtml
  run_hour_start: 0        # 인증 체크 운영 시작 시 (0~24 = 상시)
  run_hour_end: 24         # 운영 종료 시
  check_interval: 180      # 평상시 경량 체크 주기(초)
  peak_interval: 60        # 민감 시간대 체크 주기(초)
  peak_windows:            # 촘촘히 볼 구간 ("HH:MM-HH:MM")
    - "04:55-05:15"        # 매일 05:00 서버 세션 리셋 관측

git_sync:
  enabled: true
  repo: D:\bzpExpense\new-frontend
  remote_ref: origin/main
  sync_hour: 8             # 매일 이 시각 정시 + 앱 시작 시 동기화

lab_dev:
  enabled: true
  repo: D:\bzpExpense\new-frontend
  url: http://localhost:3003
  command: pnpm dev
  ports: [3003, 4401]      # 재실행 시 정리할 포트 (lab, lab-api)
  check_interval: 60       # 헬스체크 주기(초)
```

설정 변경 후에는 트레이를 재시작해야 반영된다.

---

## 5. 동작 원리 (안전장치)

### 네트워크 인증
- `run_hour_start~run_hour_end` 내에서만 체크. 경량 체크(HTTP만)로 상태 확인, 끊김 감지 시 완전 재로그인.
- **적응형 주기**: `peak_windows` 안이면 `peak_interval`, 밖이면 `check_interval`.

### GitLab 동기화 (커밋 없이 작업 보존)
1. **fetch 먼저** → 병합이 필요한지부터 판정.
2. 이미 최신 → **워킹트리 무접촉**(stash 안 함).
3. 분기(FF 불가) → 강제 병합 없이 **알림만** ("수동 확인 필요").
4. 실제 FF 필요할 때만 → 트래킹 수정 stash → `merge --ff-only` → stash 복원.
   - stash pop 충돌 시 자동 해결하지 않고 **stash에 보존 + 알림**.
5. `commit`/`push` 절대 실행 안 함. untracked 파일(데모 등)은 건드리지 않음.
6. 실제 변경이 반영되면(`changed`) HMR이 못 따라잡는 새 파일/삭제 대비 **lab dev 자동 재기동**.

> 규칙: 실제 FF가 있을 때만 워킹트리를 건드린다 → 이미 최신인 날엔 stash 자체를 안 하므로 pop 충돌 여지가 없다.

### lab dev 감시
- 60초마다 3003 응답 확인. 살아 있으면 건드리지 않음.
- 죽었으면 3003·4401 포트 점유 프로세스를 트리 종료 후 `pnpm dev` 백그라운드 실행.
- 재실행 직후 90초(부팅 유예)는 재시작 판정 보류(재시작 루프 방지).

---

## 6. 로그 (`logs/`)

| 파일 | 내용 |
|------|------|
| `network_auth_tray.log` | 인증 체크·앱 전반 |
| `git_sync.log` | 동기화 결과 |
| `lab_dev.log` | lab 감시 이벤트(재실행 등) |
| `lab_dev_server.log` | `pnpm dev` 서버 stdout/stderr |

- 모든 로그는 **로테이션**(파일당 2MB × 백업 3개)으로 무한 증가를 막는다.

---

## 7. 트러블슈팅

**"GitLab 동기화 — FF 병합 불가" 알림이 매일 뜬다**
→ `lab/skhynix`에 로컬 커밋이 생겨 `origin/main`과 분기됨(의도된 안전 동작). 수동으로 rebase/정리 필요.

**"작업 복원(stash pop) 충돌" 알림 / 워킹트리에서 수정이 사라짐**
→ 작업은 stash에 안전하게 보존돼 있다. 복구:
```bash
git -C D:\bzpExpense\new-frontend stash list            # auto-sync 항목 확인
git -C D:\bzpExpense\new-frontend stash apply "stash@{0}"  # (충돌 없으면) 복원
git -C D:\bzpExpense\new-frontend stash drop  "stash@{0}"  # 복원 확인 후 제거
```
(이 경로는 `git_sync`/`lab_dev` 대상 저장소 `D:\bzpExpense\new-frontend`이며, `D:\BZP_Agent`(앱 소스)와는 별개다.
위치·메시지로 추측해서 pop하지 말 것. 반드시 내용 확인 후 apply.)

**lab 3003이 안 뜬다**
→ `logs/lab_dev_server.log`에서 `pnpm dev` 오류 확인. 포트 점유:
```powershell
Get-NetTCPConnection -State Listen -LocalPort 3003,4401
```

**코드를 고쳤는데 반영이 안 된다**
→ 트레이가 옛 프로세스로 돌고 있다. §2의 재시작 절차 실행.
